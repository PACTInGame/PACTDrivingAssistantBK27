import json
import logging
import math
import os
import time
from typing import Dict, Any, List, Optional, Sequence, Tuple

from AI_Control import AIControlState, IndicatorMode
from assistance.base_system import AssistanceSystem
from core.event_bus import EventBus
from core.settings_manager import SettingsManager
from misc.language import LanguageManager
from misc.helpers import resolve_path
from vehicles.own_vehicle import OwnVehicle
from vehicles.vehicle import Vehicle

logger = logging.getLogger(__name__)

# ─── Route search tuning ─────────────────────────────────────────────────────
# The nearest-point search runs once per controlled car per cycle. Scanning the
# whole path costs O(path length) -- up to 528 points on SO -- so the search is
# windowed around the index the car had last cycle (known-issues #9).
#
# Point spacing in track_data/*.json is 1.1 m … 41 m (median ~6 m), so ±20
# points is at least ±28 m of route. A car at STRAIGHT_SPEED (107 km/h) covers
# ~3 m per 100 ms cycle, i.e. well under one index -- two orders of magnitude
# of headroom.
ROUTE_SEARCH_WINDOW = 20

# If the nearest point inside the window is further away than this, the car is
# not where we think it is (teleport, respawn, /restart, or a route it never
# joined). The window result is then discarded and the full path is scanned.
ROUTE_RESYNC_DISTANCE_M = 25.0

# How far off the road a car has to be before its *route assignment* is
# questioned. Measured against the road itself -- see distance_to_route_sq for
# why that distinction is the whole point of this constant.
OFF_ROUTE_DISTANCE_M = 25.0

# How many route segments either side of the nearest point that measurement
# looks at. Four covers both cases the shipped data actually contains: a
# closed loop whose seam puts the nearest point two segments away, and a road
# that repeats its first point as its last, which leaves a zero-length segment
# next to it. Measured worst case over every shipped road: 2 segments.
OFF_ROUTE_SEGMENT_WINDOW = 4


def _point_segment_distance_sq(x: float, y: float, z: float,
                               a: Tuple[float, float, float],
                               b: Tuple[float, float, float]) -> float:
    """Squared distance from a point to the segment a-b. No square roots."""
    abx = b[0] - a[0]
    aby = b[1] - a[1]
    abz = b[2] - a[2]
    apx = x - a[0]
    apy = y - a[1]
    apz = z - a[2]
    ab_sq = abx * abx + aby * aby + abz * abz
    if ab_sq <= 0.0:
        # Degenerate segment: the two points coincide.
        return apx * apx + apy * apy + apz * apz
    t = (apx * abx + apy * aby + apz * abz) / ab_sq
    if t <= 0.0:
        t = 0.0
    elif t >= 1.0:
        t = 1.0
    dx = apx - t * abx
    dy = apy - t * aby
    dz = apz - t * abz
    return dx * dx + dy * dy + dz * dz


def distance_to_route_sq(path, index: int, x: float, y: float, z: float,
                         closed_loop: bool = False,
                         window: int = OFF_ROUTE_SEGMENT_WINDOW) -> float:
    """How far this point is from the **road**, squared.

    Not from the nearest stored point -- from the centreline between them, and
    not only from the one segment the nearest point sits on. Both halves of
    that were measured on the shipped data (2026-09-19) and both fail at a
    *fixed place on a fixed corner, every lap*, which is the worst kind of
    false positive:

    * **Point spacing is authored by hand** and runs from 1.1 m to 41.3 m.
      Halfway along the longest gap on SO road 33, a car driving down the
      middle is 20.6 m from the nearest point -- against a 25 m threshold. How
      far off the road a car is would then depend on how densely somebody drew
      that road.
    * **The nearest point is not always on the nearest segment.** At the seam
      of that same closed loop, a car 3 m off the centre line of the 41.3 m
      segment has its nearest *point* at index 0, two segments away around the
      wrap. Measuring only the two segments beside that point reports 20.8 m
      for a car that is on the road.

    So the ``2 * window`` segments around ``index`` are measured and the
    smallest wins. The window is in segments, not metres, which is what makes
    the answer independent of spacing: a sparse road has few segments *and*
    long ones. Cost: 8 segment distances, about 160 flops, against the 41
    squared distances the nearest-point search already spends.

    A too-small answer is the safe direction here. The caller uses this to
    decide whether to *question* a route assignment, so under-reporting means
    "carry on as before" while over-reporting throws a car off a road it is
    driving correctly.
    """
    count = len(path)
    if count == 0:
        return float('inf')
    if count == 1:
        return _point_segment_distance_sq(x, y, z, path[0], path[0])

    best = float('inf')
    for offset in range(-window, window):
        start = index + offset
        if closed_loop:
            start %= count
        elif start < 0 or start >= count - 1:
            continue
        end = start + 1
        if end >= count:
            if not closed_loop:
                continue
            end = 0
        distance_sq = _point_segment_distance_sq(x, y, z, path[start], path[end])
        if distance_sq < best:
            best = distance_sq
    return best


class RouteDataError(ValueError):
    """A ``track_data_XX.json`` file is missing, unreadable or malformed.

    Raised by :func:`load_routes_from_file` with a message that names the file
    and the offending element, so the failure never reaches the caller as a
    bare ``KeyError`` from inside an InSim packet handler.
    """


def calculate_angle(own_x: float, own_y: float, point_x: float, point_y: float, own_heading: float) -> float:
    ang = (math.atan2((own_x / 65536 - point_x),
                      (own_y / 65536 - point_y)) * 180.0) / 3.1415926535897931
    if ang < 0.0:
        ang = 360.0 + ang
    consider_dir = ang + own_heading / 182
    if consider_dir > 360.0:
        consider_dir -= 360.0
    angle = (consider_dir + 180.0) % 360.0

    if angle > 180.0:
        angle -= 360.0
    return angle


def calculate_angle_meters(own_x_m: float, own_y_m: float, target_x_m: float, target_y_m: float,
                           own_heading: float) -> float:
    """Calculate angle from own position to target, both already in meters.

    Same logic as calculate_angle but without the /65536 conversion on own_x/own_y,
    since both coordinate pairs are expected to be in meters already.

    Args:
        own_x_m: Own X position in meters
        own_y_m: Own Y position in meters
        target_x_m: Target X position in meters
        target_y_m: Target Y position in meters
        own_heading: Own heading in game units

    Returns:
        Angle in degrees (-180 to +180), 0 = straight ahead
    """
    ang = (math.atan2((own_x_m - target_x_m),
                      (own_y_m - target_y_m)) * 180.0) / 3.1415926535897931
    if ang < 0.0:
        ang = 360.0 + ang
    consider_dir = ang + own_heading / 182
    if consider_dir > 360.0:
        consider_dir -= 360.0
    angle = (consider_dir + 180.0) % 360.0

    if angle > 180.0:
        angle -= 360.0
    return angle


def dist(a=(0, 0, 0), b=(0, 0, 0)):
    """Determine the distance between two points."""
    return math.sqrt((b[0] - a[0]) ** 2 + (b[1] - a[1]) ** 2 + (b[2] - a[2]) ** 2)


MIN_ROUTE_POINTS = 2          # a path shorter than this cannot be driven
MARKER_TYPES = ('stop_line', 'arrow_left', 'arrow_right')


def _as_point(value: Any, where: str, file_path: str) -> Tuple[float, float, float]:
    """One [x, y, z] entry as a tuple of floats, or a readable error.

    Points are converted once, at load time, so the hot path never has to
    guess what a JSON element contains.
    """
    if isinstance(value, (str, bytes)) or not isinstance(value, Sequence) or len(value) < 3:
        raise RouteDataError(
            f"{file_path}: {where} is not an [x, y, z] point: {value!r}")
    try:
        return (float(value[0]), float(value[1]), float(value[2]))
    except (TypeError, ValueError):
        raise RouteDataError(
            f"{file_path}: {where} has non-numeric coordinates: {value!r}") from None


def load_routes_from_file(file_path: str) -> Tuple[List[Dict[str, Any]], List[Dict[str, Any]]]:
    """Lädt Routen und Marker aus einer Datei. Bei inverted=True wird der Pfad umgekehrt.

    The file is authored by an offline tool (``MapBuilder.py``) and can be
    hand-edited, so its shape is verified here rather than trusted: every
    violation raises :class:`RouteDataError` naming the file and the element.
    Positions are converted to tuples of floats once, at load time.

    Returns:
        Tuple of (roads, markers)

    Raises:
        RouteDataError: file missing, not readable, not JSON, or malformed.
    """
    try:
        with open(file_path, 'r', encoding='utf-8') as file:
            data = json.load(file)
    except OSError as e:
        raise RouteDataError(f"{file_path}: cannot be read ({e.strerror or e})") from None
    except json.JSONDecodeError as e:
        raise RouteDataError(f"{file_path}: is not valid JSON (line {e.lineno}: {e.msg})") from None

    if not isinstance(data, dict):
        raise RouteDataError(f"{file_path}: top level must be an object, got {type(data).__name__}")

    raw_roads = data.get('roads', [])
    if not isinstance(raw_roads, list):
        raise RouteDataError(f"{file_path}: 'roads' must be a list, got {type(raw_roads).__name__}")

    roads: List[Dict[str, Any]] = []
    seen_ids = set()
    for position, road in enumerate(raw_roads):
        where = f"roads[{position}]"
        if not isinstance(road, dict):
            raise RouteDataError(f"{file_path}: {where} must be an object")
        if 'road_id' not in road:
            raise RouteDataError(f"{file_path}: {where} has no 'road_id'")
        road_id = road['road_id']
        if not isinstance(road_id, int) or isinstance(road_id, bool):
            raise RouteDataError(f"{file_path}: {where} has a non-integer road_id {road_id!r}")
        if road_id in seen_ids:
            raise RouteDataError(f"{file_path}: {where} repeats road_id {road_id}")
        seen_ids.add(road_id)

        raw_path = road.get('path')
        if not isinstance(raw_path, list):
            raise RouteDataError(f"{file_path}: {where} has no 'path' list")
        if len(raw_path) < MIN_ROUTE_POINTS:
            raise RouteDataError(
                f"{file_path}: {where} (road_id {road_id}) has {len(raw_path)} points, "
                f"at least {MIN_ROUTE_POINTS} are needed")
        path = [_as_point(point, f"{where}.path[{i}]", file_path)
                for i, point in enumerate(raw_path)]
        if road.get('inverted', False):
            path.reverse()

        checked = dict(road)
        checked['road_id'] = road_id
        checked['path'] = path
        checked['closed_loop'] = bool(road.get('closed_loop', False))
        roads.append(checked)

    raw_markers = data.get('markers', [])
    if not isinstance(raw_markers, list):
        raise RouteDataError(
            f"{file_path}: 'markers' must be a list, got {type(raw_markers).__name__}")

    markers: List[Dict[str, Any]] = []
    for position, marker in enumerate(raw_markers):
        where = f"markers[{position}]"
        if not isinstance(marker, dict):
            raise RouteDataError(f"{file_path}: {where} must be an object")
        marker_type = marker.get('type')
        if marker_type not in MARKER_TYPES:
            # Unknown marker types are ignored rather than fatal: a newer
            # MapBuilder may add types this build does not drive on yet.
            logger.warning("%s: %s has unknown type %r - ignored",
                           file_path, where, marker_type)
            continue
        checked_marker = dict(marker)
        checked_marker['position'] = _as_point(
            marker.get('position'), f"{where}.position", file_path)
        markers.append(checked_marker)

    return roads, markers


def _scan_whole_path(path, car_x: float, car_y: float, car_z: float) -> Tuple[int, float]:
    """Nearest point over the whole path. Returns (index, squared distance).

    Squared distances only -- the comparison is monotonic in the distance and
    a square root per point is pure cost (``conventions.md`` §6).
    """
    closest_index = 0
    min_d_sq = float('inf')
    for i, point in enumerate(path):
        dx = car_x - point[0]
        dy = car_y - point[1]
        dz = car_z - point[2]
        d_sq = dx * dx + dy * dy + dz * dz
        if d_sq < min_d_sq:
            min_d_sq = d_sq
            closest_index = i
    return closest_index, min_d_sq


def get_closest_index_on_route(carX, carY, carZ, route_points,
                               previous_index: Optional[int] = None,
                               window: int = ROUTE_SEARCH_WINDOW):
    """
    Find the index of the closest point on the route to the car's current position.

    Args:
        carX, carY, carZ: Current car position coordinates (metres)
        route_points: Dict containing 'path' key with list of [x, y, z] points.
            ``closed_loop`` decides whether the window wraps around the end.
        previous_index: Where this car was on the route last cycle. Given that,
            only ``2*window + 1`` points around it are examined instead of the
            whole path. ``None`` (the default, and what every caller outside
            :class:`AIDriver` uses) scans everything, exactly as before.
        window: Half-width of that search window, in route points.

    The window is discarded and the full path scanned when the car cannot
    plausibly be inside it: the nearest point found is further away than
    ``ROUTE_RESYNC_DISTANCE_M`` (teleport, respawn, /restart, wrong route), or
    it sits on the window's edge, where a nearer point may lie just outside.
    So the result is the true nearest point whenever the car is on its route,
    and the cost of being wrong is one extra full scan, not a wrong index.

    Returns:
        int: Index of the closest point in the route
    """
    path = route_points.get('path') or []
    if not path:
        return 0

    point_count = len(path)
    if previous_index is None or window <= 0 or 2 * window + 1 >= point_count:
        # Nothing to save -- the window would cover the whole path anyway.
        return _scan_whole_path(path, carX, carY, carZ)[0]

    closed_loop = bool(route_points.get('closed_loop', False))
    if closed_loop:
        start = previous_index % point_count
    else:
        start = max(0, min(point_count - 1, previous_index))

    closest_index = None
    closest_offset = 0
    min_d_sq = float('inf')
    for offset in range(-window, window + 1):
        i = start + offset
        if closed_loop:
            i %= point_count
        elif i < 0 or i >= point_count:
            continue
        point = path[i]
        dx = carX - point[0]
        dy = carY - point[1]
        dz = carZ - point[2]
        d_sq = dx * dx + dy * dy + dz * dz
        if d_sq < min_d_sq:
            min_d_sq = d_sq
            closest_index = i
            closest_offset = offset

    if (closest_index is None
            or abs(closest_offset) >= window
            or min_d_sq > ROUTE_RESYNC_DISTANCE_M * ROUTE_RESYNC_DISTANCE_M):
        return _scan_whole_path(path, carX, carY, carZ)[0]

    return closest_index


def get_next_points_on_route(current_index, route_points, num_points=5):
    """
    Get the next points on the route, wrapping around if necessary.

    Args:
        current_index: Current position index on the route
        route_points: Dict containing 'path' key with list of [x, y, z] points
        num_points: Number of points to retrieve (default: 5)

    Returns:
        List of next points on the route
    """
    path = route_points.get('path', [])
    if not path:
        return []

    next_points = []
    path_length = len(path)

    for i in range(num_points):
        index = (current_index + i) % path_length
        next_points.append(path[index])

    return next_points


def get_next_points_for_distance(current_index, route_points, min_distance=50.0, min_points=5):
    """
    Get points on the route until either min_distance is covered OR min_points are collected,
    whichever results in MORE points.

    Args:
        current_index: Current position index on the route
        route_points: Dict containing 'path' key with list of [x, y, z] points
        min_distance: Minimum distance to cover in meters (default: 50.0)
        min_points: Minimum number of points to collect (default: 5)

    Returns:
        List of points covering at least min_distance or min_points (whichever is more)
    """
    path = route_points.get('path', [])
    if not path:
        return []

    path_length = len(path)
    collected_points = []
    total_distance = 0.0

    i = 0
    while True:
        index = (current_index + i) % path_length
        collected_points.append(path[index])

        if len(collected_points) >= 2:
            prev_point = collected_points[-2]
            curr_point = collected_points[-1]
            segment_dist = dist(tuple(prev_point), tuple(curr_point))
            total_distance += segment_dist

        if len(collected_points) >= min_points and total_distance >= min_distance:
            break

        if len(collected_points) >= path_length:
            break

        i += 1

    return collected_points


def analyze_upcoming_track(route_points) -> Tuple[float, Tuple[float, float, float]]:
    """
    Analyze the upcoming track section to determine curvature and target steering point.

    Args:
        route_points: List of upcoming points (variable length, minimum 5 or 50m coverage)

    Returns:
        Tuple containing:
        - average_curvature: Average curvature of the upcoming section (all points)
        - target_point: Average position of points 2-3 (indices 1-2) to steer towards
    """
    if not route_points:
        # Only reachable with an empty path; the caller must not steer at all.
        return 0.0, (0.0, 0.0, 0.0)

    if len(route_points) < 3:
        return 0.0, tuple(route_points[1] if len(route_points) > 1 else route_points[0])

    curvatures = []

    for i in range(len(route_points) - 2):
        p1 = route_points[i]
        p2 = route_points[i + 1]
        p3 = route_points[i + 2]

        v1 = (p2[0] - p1[0], p2[1] - p1[1])
        v2 = (p3[0] - p2[0], p3[1] - p2[1])

        angle1 = math.atan2(v1[1], v1[0])
        angle2 = math.atan2(v2[1], v2[0])

        angle_diff = angle2 - angle1

        while angle_diff > math.pi:
            angle_diff -= 2 * math.pi
        while angle_diff < -math.pi:
            angle_diff += 2 * math.pi

        segment_length = dist(tuple(p1), tuple(p2))
        if segment_length > 0:
            curvature = abs(angle_diff) / segment_length
            curvatures.append(curvature)


    average_curvature = sum(curvatures) / len(curvatures) if curvatures else 0.0

    # Weighted steering target using indices 1, 2, 3 (weights: 25%, 50%, 25%).
    # Index 2 (directly ahead) dominates, while 1 and 3 provide stability.
    if len(route_points) >= 4:
        p1, p2, p3 = route_points[1], route_points[2], route_points[3]
        target_point = (
            p1[0] * 0.25 + p2[0] * 0.50 + p3[0] * 0.25,
            p1[1] * 0.25 + p2[1] * 0.50 + p3[1] * 0.25,
            p1[2] * 0.25 + p2[2] * 0.50 + p3[2] * 0.25,
        )
    elif len(route_points) >= 3:
        # Not enough points for full weighting, average indices 1-2
        p1, p2 = route_points[1], route_points[2]
        target_point = (
            (p1[0] + p2[0]) / 2, (p1[1] + p2[1]) / 2, (p1[2] + p2[2]) / 2,
        )
    else:
        target_point = tuple(route_points[1] if len(route_points) > 1 else route_points[0])

    return average_curvature, target_point


def calculate_feedforward_steering(target_angle: float,
                                   max_steering_angle: float = 45.0,
                                   max_steering_output: float = 100.0) -> float:
    """
    Calculate feedforward steering based on target angle.
    Maps ±max_steering_angle to ±max_steering_output linearly.

    Args:
        target_angle: Desired steering angle in degrees (-180 to +180)
        max_steering_angle: Maximum angle that maps to full steering (default: 45°)
        max_steering_output: Maximum steering output value (default: 100)

    Returns:
        Steering value clamped to [-max_steering_output, +max_steering_output]
    """
    clamped_angle = max(-max_steering_angle, min(max_steering_angle, target_angle))
    return (clamped_angle / max_steering_angle) * max_steering_output


def calculate_feedforward_throttle_brake(speed_error: float,
                                         gain: float = 3.0) -> Tuple[float, float]:
    """
    Calculate throttle and brake from speed error using simple proportional feedforward.

    Args:
        speed_error: target_speed - current_speed (positive = too slow, negative = too fast)
        gain: Proportional gain mapping speed error to throttle/brake (default: 3.0)

    Returns:
        Tuple of (throttle, brake), each in range [0, 100]
    """
    control = speed_error * gain

    if control > 0:
        throttle = min(100.0, max(0.0, control))
        brake = 0.0
    else:
        throttle = 0.0
        brake = min(100.0, max(0.0, -control))

    return throttle, brake


class AIDriver(AssistanceSystem):
    """AI Driver – controls AI vehicles along predefined routes using feedforward control."""

    # States for the AI traffic system
    STATE_INACTIVE = 0
    STATE_ACTIVE = 1
    STATE_STOPPING = 2

    # Allowed track configurations for AI traffic
    ALLOWED_TRACKS = {'BL1X', 'SO7', 'KY1X'}

    # Track-specific layout hint notifications (matched by track prefix)
    TRACK_LAYOUT_HINTS = {
        'BL': '^7Select GP Track X',
        'SO': '^7Select City',
        'KY': '^7Select Oval X',
    }

    def __init__(self, event_bus: EventBus, settings: SettingsManager):
        super().__init__("ai_traffic", event_bus, settings)
        self.translator = LanguageManager()
        self.current_track = None
        self.event_bus.subscribe("state_data", self._on_state_data)
        self.routes = None
        self.ai_controller = None
        self.state = self.STATE_INACTIVE
        self.assigned_routes: Dict[int, int] = {}  # vehicle_id -> route_id
        self.stop_counter = 0

        # Stop phase: brake for this many process cycles before releasing control
        self.STOP_BRAKE_CYCLES = 20  # 20 × 100ms = 2 seconds

        # PLIDs LFS reported in the most recent pass. **No IS_AIC is ever sent
        # to a PLID outside this set**: a packet for a car that has left is
        # answered with "IS_AIC - no driver to control" in the chat, once per
        # car, which is what the driver saw when a race ended while traffic
        # was running. Rebound, never mutated - monitor_ai reads it on the
        # packet thread while process() rebuilds it on a worker
        # (conventions.md §6).
        self._live_plids: frozenset = frozenset()
        # Last known on_track state, so leaving the race can be recognised as
        # an edge rather than polled.
        self._on_track = False

        self.event_bus.subscribe("AI_Controller_initialized", self._on_ai_controller_initialized)
        self.event_bus.subscribe("ai_traffic_start", self._on_start)
        self.event_bus.subscribe("ai_traffic_stop", self._on_stop)
        self.event_bus.subscribe("player_left", self._on_player_left)

        # Feedforward tuning parameters
        self.MAX_STEERING_ANGLE = 45.0   # ±degrees that map to full steering
        self.SPEED_GAIN = 3.0            # Proportional gain for speed error → throttle/brake
        self.MAX_THROTTLE = 60.0         # Maximum throttle percentage

        # Speed parameters
        self.BASE_SPEED = 70.0           # Base speed in km/h on straight sections
        self.STRAIGHT_SPEED = 107.0      # Speed in km/h on long straights (no curve for ≥STRAIGHT_LOOKAHEAD_DIST)
        self.STRAIGHT_LOOKAHEAD_DIST = 120.0  # Minimum distance (m) without curve to allow STRAIGHT_SPEED
        self.MIN_SPEED = 22.0            # Minimum speed on tight curves
        self.CURVATURE_THRESHOLD = 0.004 # Curvature above which to slow down (lower = react to gentle curves)

        # ── Collision avoidance ──
        # Geometry: a forward *corridor*, not a cone. A +-12 deg cone is only
        # +-1.0 m wide at 5 m, so a car standing directly ahead but half a car
        # width off centre dropped out of it exactly when braking mattered
        # most. The corridor is one car wide at zero range and widens slowly,
        # which keeps a gentle curve inside it without catching the oncoming
        # lane. Both halves are measured centre to centre.
        self.CA_DETECTION_DISTANCE = 50.0      # Look this far ahead (meters)
        self.CA_CORRIDOR_HALF_WIDTH = 1.9      # Half of one car plus half of another (m)
        self.CA_CORRIDOR_SPREAD = 0.04         # Extra half-width per meter of range

        # Following model (Gipps' safe speed, see _calculate_following_speed).
        # CA_COMFORT_DECEL is what an ordinary street car reaches on dry
        # tarmac well inside its Kamm circle (mu ~ 0.9 would allow ~8.8 m/s2);
        # 4.0 m/s2 leaves the whole second half of the friction budget for
        # steering. CA_REACTION_TIME is this controller's own dead time: one
        # 100 ms process cycle plus the lag of the brake filter.
        self.CA_STANDSTILL_GAP = 6.0           # Centre-to-centre gap at rest (m)
        self.CA_COMFORT_DECEL = 4.0            # m/s^2 (~0.4 g)
        self.CA_REACTION_TIME = 0.35           # s
        # Above this required deceleration the situation is no longer a
        # following manoeuvre: brake fully and skip the brake filter.
        self.CA_EMERGENCY_DECEL = 4.0          # m/s^2

        # Smoothing: each cycle, move 1/SMOOTHING_STEPS of the remaining distance
        # toward the target. Handles targets that change every cycle gracefully.
        self.SMOOTHING_STEPS_THROTTLE = 10.0
        self.SMOOTHING_STEPS_BRAKE = 2.0
        self._smoothed: Dict[int, Dict[str, float]] = {}  # vehicle_id → {throttle, brake, steer}

        # Last known route index per vehicle, so the nearest-point search only
        # has to look at ROUTE_SEARCH_WINDOW points either side of it instead
        # of the whole path (known-issues #9).
        self._route_index: Dict[int, int] = {}   # vehicle_id → index on its route

        # Consecutive cycles a car has spent further than ROUTE_RESYNC_DISTANCE_M
        # from the route it was assigned to. A route assignment used to be for
        # ever, so a car that was assigned before ``/restart`` teleported it to
        # the grid kept steering towards a road on the other side of town.
        self._off_route: Dict[int, int] = {}     # vehicle_id → cycles off route
        self.OFF_ROUTE_REASSIGN_CYCLES = 10      # 10 × 100 ms = 1 s before re-picking
        # Distance from the road itself, not from the nearest stored point.
        self.OFF_ROUTE_DISTANCE_M = OFF_ROUTE_DISTANCE_M

        # Long-straight lookahead per (route_id, index). The 120 m analysis is
        # the most expensive part of the cycle and depends only on the route,
        # which never changes while it is loaded -- so it is computed at most
        # once per route point instead of once per car per cycle. Bounded by
        # the number of points in the loaded track (~2500 booleans).
        self._straight_cache: Dict[Tuple[int, int], bool] = {}

        # Marker data (stop lines, arrows) – loaded together with routes
        self._markers: List[Dict[str, Any]] = []
        # Pre-split marker lists for fast iteration (tuples of (x, y, z))
        self._stop_lines: List[Tuple[float, float, float]] = []
        self._arrows_left: List[Tuple[float, float, float]] = []
        self._arrows_right: List[Tuple[float, float, float]] = []

        # Per-vehicle shift retry state for monitor_ai toggle logic.
        # After sending shift_up=True or shift_down=True, the next cycle always
        # sends False first (reset), so a stuck command is retried automatically.
        # Keys: PLID → {"up": bool, "down": bool}
        self._shift_pending: Dict[int, Dict[str, bool]] = {}

        # Per-vehicle timestamp of last received AI info packet.
        # Used to detect when the repeating request was lost (e.g. after map reload)
        # and needs to be re-issued.
        self._last_ai_info_time: Dict[int, float] = {}
        self.AI_INFO_TIMEOUT = 2.0  # seconds – re-request after this silence

        # Per-vehicle marker interaction state
        # stop_line: "idle" → "braking" → "stopped" → "departing" (then back to idle)
        self._stop_state: Dict[int, str] = {}          # vehicle_id → state
        self._stop_cooldown: Dict[int, int] = {}       # vehicle_id → remaining cooldown cycles
        # indicator: remaining cycles until cancel (-1 = inactive)
        self._indicator_timer: Dict[int, int] = {}     # vehicle_id → remaining cycles
        # Track which markers a vehicle has already interacted with (to avoid re-trigger)
        self._marker_cooldown: Dict[int, set] = {}     # vehicle_id → set of marker indices

        self.MARKER_TRIGGER_DISTANCE = 3.0   # meters – activation radius for markers
        self.MARKER_COOLDOWN_DISTANCE = 8.0  # meters – must move this far before re-trigger
        self.STOP_BRAKE_POWER = 50.0         # % brake force at stop lines
        self.INDICATOR_DURATION_CYCLES = 50  # 50 × 100ms = 5 seconds

    def _on_state_data(self, data):
        """Listen for track changes and for leaving the race.

        Both mean the same thing for the cars we control: **they do not exist
        any more.** Anything sent to them from here on is answered with
        "IS_AIC - no driver to control" in the chat, once per car, so traffic
        is dropped silently instead of being stopped gracefully.
        """
        if 'on_track' in data:
            on_track = bool(data.get('on_track'))
            if self._on_track and not on_track and self.state != self.STATE_INACTIVE:
                self._abandon_control("the race was left")
            self._on_track = on_track

        # state_data['track'] is a decoded str since WP5; bytes stay accepted
        # so a direct emit with raw packet data cannot silently mismatch.
        track = data.get('track')
        if isinstance(track, (bytes, bytearray)):
            track = bytes(track).split(b'\x00', 1)[0].decode('latin-1', errors='replace')
        if not track:
            # No track name in this packet is "unknown", not "the track
            # changed" -- treating it as a change would stop running traffic
            # and drop the loaded routes on a single malformed IS_STA.
            return
        if track == self.current_track:
            return

        self.current_track = track
        if self.state != self.STATE_INACTIVE:
            self._abandon_control(f"the track changed to {track}")
        # Rebound, never mutated: _process_active runs on a worker thread while
        # this handler runs on the packet thread (conventions.md §6).
        self.routes = None
        self._straight_cache = {}
        self._route_index = {}

    def _on_player_left(self, pll):
        """IS_PLL - this PLID is gone, so nothing may be sent to it again.

        LFS does *not* send IS_PLL when a race ends (measured over nine
        scenarios, ``vehicle_manager.py``), so this is a shortcut, not the
        guarantee - the guarantee is ``_live_plids``.
        """
        try:
            plid = int(getattr(pll, 'PLID', 0) or 0)
        except (TypeError, ValueError):
            return
        if not plid:
            return
        self._live_plids = self._live_plids - {plid}
        if plid in self.assigned_routes:
            self._forget_vehicle(plid)
            logger.info("Vehicle %s left the race - removed from AI traffic.", plid)

    def _on_ai_controller_initialized(self, ai_controller):
        self.ai_controller = ai_controller

    # ─── Start / Stop ─────────────────────────────────────────────────

    def _on_start(self, data=None):
        """Start AI traffic. Route assignment happens in the next process() call."""
        if self.state != self.STATE_INACTIVE:
            return

        # --- Validate track data file exists ---
        trackname = (self.current_track or "")[:2]
        file_path = resolve_path("track_data", f"track_data_{trackname}.json")
        if not os.path.isfile(file_path):
            self._notify_error('Traffic not avail. on this map')
            logger.warning("Track data file not found: %s", file_path)
            return

        # --- Validate track configuration ---
        if self.current_track not in self.ALLOWED_TRACKS:
            self._notify_error('Wrong track config for traffic')
            logger.info("Track %s is not a valid traffic track.", self.current_track)
            # Emit track-specific layout hint notification
            hint = self.TRACK_LAYOUT_HINTS.get(trackname)
            if hint:
                self.event_bus.emit("notification", {'notification': hint})
            return

        # --- Load the route data before anything is done to the session ---
        # /axload + /restart throw away the layout the driver had loaded, so
        # they are only sent once the data this run needs is actually usable.
        if not self._load_routes():
            self._notify_error('Traffic not avail. on this map')
            return

        # --- All checks passed – start traffic ---
        self.event_bus.emit("send_command_to_lfs", "/axload AI_Traffic")
        self.event_bus.emit("send_command_to_lfs", "/restart")
        # /restart puts every car back on the grid and LFS re-announces the
        # whole field. Ask for that list rather than driving on the identities
        # left over from before the restart: which car is an AI, and which
        # PLID is the local driver, both come from IS_NPL, and adopting a car
        # on stale data is what makes the first seconds after a start go
        # wrong. Cheap: one TINY_NPL, answered once per player.
        self.event_bus.emit("request_player_list", {})

        self.assigned_routes = {}
        self._smoothed = {}
        self._route_index = {}
        self._off_route = {}
        self.state = self.STATE_ACTIVE
        self.event_bus.emit("ai_traffic_state_changed", {"active": True})

        logger.info("AI traffic started on %s (%d routes).",
                    self.current_track, len(self.routes or {}))

    def _notify_error(self, key: str):
        """Red notification for a translated key."""
        self.event_bus.emit(
            "notification",
            {'notification': '^1' + self.translator.get(key, self.settings.get('language'))})

    def _on_stop(self, data=None):
        """Initiate stop sequence: brake all vehicles, then release control."""
        if self.state != self.STATE_ACTIVE:
            return
        self.state = self.STATE_STOPPING
        self.stop_counter = 0
        self.event_bus.emit("ai_traffic_state_changed", {"active": False})
        logger.info("AI traffic stopping - braking all vehicles...")

    def _abandon_control(self, reason: str):
        """Drop every controlled car **without sending anything to LFS**.

        The graceful stop brakes for two seconds and then hands each car back.
        That is right while the cars exist and wrong the moment they do not:
        every packet then comes back as "IS_AIC - no driver to control". So
        whenever the cars are known to be gone - the race was left, the track
        changed - control is simply let go of.
        """
        was_running = self.state != self.STATE_INACTIVE
        self._live_plids = frozenset()
        self._clear_vehicle_state()
        self.state = self.STATE_INACTIVE
        self.stop_counter = 0
        if was_running:
            self.event_bus.emit("ai_traffic_state_changed", {"active": False})
            logger.info("AI traffic dropped - %s.", reason)

    @property
    def is_active(self) -> bool:
        return self.state == self.STATE_ACTIVE

    # ─── Sending ──────────────────────────────────────────────────────

    def _control(self, plid: int, state: AIControlState) -> bool:
        """One IS_AIC, but only to a car LFS is currently reporting.

        The single choke point for everything this system sends. ``plid`` must
        be in ``_live_plids``, which is rebuilt from the vehicle list every
        pass - a PLID that is not there either never existed or has left, and
        LFS answers such a packet with a chat line rather than ignoring it.
        """
        controller = self.ai_controller
        if controller is None or plid not in self._live_plids:
            return False
        try:
            controller.control_ai(plid, state)
        except Exception as e:
            logger.warning("IS_AIC for vehicle %s failed: %s: %s",
                           plid, type(e).__name__, e)
            return False
        return True

    # ─── Route helpers ────────────────────────────────────────────────

    def _load_routes(self) -> bool:
        """Load routes and markers from file (once).

        Returns:
            True when routes are available afterwards. A malformed or
            unreadable ``track_data_XX.json`` is reported and returns False -
            it must not raise, because this runs inside the InSim packet
            handler that delivered the menu click.
        """
        if self.routes is not None:
            return bool(self.routes)

        trackname = self.current_track[:2] if self.current_track else None
        if trackname is None:
            return False

        file_path = resolve_path("track_data", f"track_data_{trackname}.json")
        try:
            roads_list, markers_list = load_routes_from_file(file_path)
        except RouteDataError as e:
            logger.error("AI traffic route data unusable: %s", e)
            return False

        if not roads_list:
            logger.error("AI traffic route data unusable: %s contains no roads", file_path)
            return False

        self.routes = {road['road_id']: road for road in roads_list}
        self._straight_cache = {}

        # Store and pre-split markers by type for fast lookup
        self._markers = markers_list
        self._stop_lines = [m['position'] for m in markers_list if m['type'] == 'stop_line']
        self._arrows_left = [m['position'] for m in markers_list if m['type'] == 'arrow_left']
        self._arrows_right = [m['position'] for m in markers_list if m['type'] == 'arrow_right']
        logger.info("Loaded %d routes, %d stop lines, %d left arrows, %d right arrows.",
                    len(self.routes), len(self._stop_lines),
                    len(self._arrows_left), len(self._arrows_right))
        return True

    def _find_closest_route(self, vehicle) -> Optional[int]:
        """
        Find the route whose path passes closest to the vehicle's current position.

        Args:
            vehicle: Vehicle object with position data

        Returns:
            route_id of the closest route, or None if no routes are loaded
        """
        if not self.routes:
            return None

        vx = vehicle.data.x / 65536
        vy = vehicle.data.y / 65536
        vz = vehicle.data.z / 65536
        min_d_sq = float('inf')
        closest_route_id = None
        # Runs once per vehicle, when it is adopted -- not per cycle. Squared
        # distances keep even that one pass off the square root.
        for road_id, road in self.routes.items():
            _index, d_sq = _scan_whole_path(road.get('path') or [], vx, vy, vz)
            if d_sq < min_d_sq:
                min_d_sq = d_sq
                closest_route_id = road_id
        return closest_route_id

    # ─── Speed ────────────────────────────────────────────────────────

    def calculate_target_speed(self, curvature: float, long_straight: bool = False) -> float:
        """
        Calculate target speed based on upcoming curvature.

        Args:
            curvature: Average curvature of upcoming section
            long_straight: True if no curve detected for ≥STRAIGHT_LOOKAHEAD_DIST ahead

        Returns:
            Target speed in km/h
        """
        base = self.STRAIGHT_SPEED if long_straight else self.BASE_SPEED
        if curvature < self.CURVATURE_THRESHOLD:
            return base
        else:
            speed_reduction = (curvature - self.CURVATURE_THRESHOLD) * 1500.0
            return max(self.MIN_SPEED, base - speed_reduction)

    # ─── AI Info monitoring ───────────────────────────────────────────

    def monitor_ai(self, aii):
        """Handle AI info packets – automatic gear shifting and stall recovery.

        Uses a toggle mechanism to prevent shift commands from getting stuck.
        After sending shift_up=True (or shift_down=True), the next cycle
        *always* sends False first (reset cycle).  If the RPM still requires
        a shift, True is sent again the cycle after that.  This means a shift
        can only happen every other cycle, but it guarantees that a failed
        shift is always retried.
        """
        if self.ai_controller is None:
            return

        plid = aii.PLID

        # An IS_AII can still arrive after the stop sequence finished, or for a
        # car this system never adopted (the repeat request outlives us). Both
        # would answer with an IS_AIC nobody asked for.
        if self.state != self.STATE_ACTIVE or plid not in self.assigned_routes:
            return

        # Receiving IS_AII *is* proof that this PLID exists, whatever the last
        # vehicle list said - so it counts as live for the answer below.
        if plid not in self._live_plids:
            self._live_plids = self._live_plids | {plid}

        # Record reception time for timeout detection
        self._last_ai_info_time[plid] = time.time()

        # Lazy-init per-vehicle shift state
        if plid not in self._shift_pending:
            self._shift_pending[plid] = {"up": False, "down": False}

        pending = self._shift_pending[plid]

        # ── Shift Up ──
        wants_shift_up = aii.RPM > 3600 and aii.Gear < 6
        if pending["up"]:
            # Last cycle sent True → always reset to False first
            self._control(plid, AIControlState(shift_up=False))
            pending["up"] = False
        elif wants_shift_up:
            # No pending reset → send the actual shift command
            self._control(plid, AIControlState(shift_up=True))
            pending["up"] = True

        # ── Shift Down ──
        wants_shift_down = aii.RPM < 1700 and aii.Gear > 2
        if pending["down"]:
            # Last cycle sent True → always reset to False first
            self._control(plid, AIControlState(shift_down=False))
            pending["down"] = False
        elif wants_shift_down:
            # No pending reset → send the actual shift command
            self._control(plid, AIControlState(shift_down=True))
            pending["down"] = True

        # ── Stall recovery (ignition) ──
        self._control(plid, AIControlState(ignition=aii.RPM < 300))

    # ─── Main process loop ────────────────────────────────────────────

    def process(self, own_vehicle: OwnVehicle, vehicles: Dict[int, Vehicle]) -> Dict[str, Any]:
        """Main processing loop, called every 100 ms."""

        # -- Inactive: nothing to do --
        if self.state == self.STATE_INACTIVE:
            return {'ai_active': False}

        # -- Stopping phase: brake all vehicles, then release control --
        if self.state == self.STATE_STOPPING:
            return self._process_stopping(own_vehicle, vehicles)

        # -- Active: drive all assigned vehicles --
        return self._process_active(own_vehicle, vehicles)

    def _candidates(self, own_vehicle: OwnVehicle,
                    vehicles: Dict[int, Vehicle]) -> Dict[int, Vehicle]:
        """Every car LFS is reporting right now, own car included.

        ``vehicles`` never holds the own car -- ``VehicleManager`` keeps that
        one separate -- but it is a real obstacle for the traffic, and while
        ``local_plid`` is still unknown ``OwnVehicle`` may be standing in for
        whichever car the camera is on. Both are reasons to put it back in.
        """
        candidates: Dict[int, Vehicle] = dict(vehicles)
        own_plid = own_vehicle.data.player_id
        if own_plid and own_plid not in candidates:
            candidates[own_plid] = own_vehicle
        return candidates

    def _process_stopping(self, own_vehicle: OwnVehicle,
                          vehicles: Dict[int, Vehicle]) -> Dict[str, Any]:
        """Send brake commands during stop phase, then release control.

        Only cars LFS still reports are braked. A stop that begins just as the
        race ends would otherwise send 20 brake packets plus one hand-back per
        car into an empty session.
        """
        candidates = self._candidates(own_vehicle, vehicles)
        self._live_plids = frozenset(candidates)

        # Over a snapshot: _on_player_left deletes from the packet thread.
        live = [vid for vid in list(self.assigned_routes) if vid in candidates]
        if self.ai_controller is None or not live:
            self._finalize_stop(candidates)
            return {'ai_active': False}

        for vehicle_id in live:
            self._control(vehicle_id, AIControlState(throttle=0, brake=100))

        self.stop_counter += 1

        if self.stop_counter >= self.STOP_BRAKE_CYCLES:
            self._finalize_stop(candidates)
            return {'ai_active': False}

        return {'ai_active': True, 'stopping': True}

    def _finalize_stop(self, vehicles: Dict[int, Vehicle]):
        """Hand every car that still exists back to LFS' own AI, then clean up."""
        controller = self.ai_controller
        if controller is not None:
            for vehicle_id in list(self.assigned_routes.keys()):
                if vehicle_id not in self._live_plids:
                    continue
                try:
                    controller.stop_ai_control(vehicle_id)
                except Exception as e:
                    logger.warning("Error stopping control for vehicle %s: %s: %s",
                                   vehicle_id, type(e).__name__, e)

        # Reset route assignments on vehicle objects
        for vehicle_id in list(self.assigned_routes.keys()):
            vehicle = vehicles.get(vehicle_id)
            if vehicle is not None:
                vehicle.current_route = None

        self._clear_vehicle_state()
        self.state = self.STATE_INACTIVE
        self.stop_counter = 0
        logger.info("AI traffic fully stopped.")

    def _clear_vehicle_state(self):
        """Forget every per-vehicle working set in one place.

        One method rather than nine ``.clear()`` calls repeated at each exit:
        a dict forgotten there is a vehicle that carries its stop-line state
        or its route index into the next run.
        """
        self.assigned_routes.clear()
        self._smoothed.clear()
        self._route_index.clear()
        self._off_route.clear()
        self._shift_pending.clear()
        self._last_ai_info_time.clear()
        self._stop_state.clear()
        self._stop_cooldown.clear()
        self._indicator_timer.clear()
        self._marker_cooldown.clear()

    def _forget_vehicle(self, vehicle_id: int):
        """Drop one vehicle from every per-vehicle working set."""
        self.assigned_routes.pop(vehicle_id, None)
        self._smoothed.pop(vehicle_id, None)
        self._route_index.pop(vehicle_id, None)
        self._off_route.pop(vehicle_id, None)
        self._shift_pending.pop(vehicle_id, None)
        self._last_ai_info_time.pop(vehicle_id, None)
        self._stop_state.pop(vehicle_id, None)
        self._stop_cooldown.pop(vehicle_id, None)
        self._indicator_timer.pop(vehicle_id, None)
        self._marker_cooldown.pop(vehicle_id, None)

    def _is_local_ai_vehicle(self, vehicle) -> bool:
        """Check whether a vehicle is an AI driver.

        IS_NPL.PType bit 1 is the authoritative AI flag; VehicleManager stores
        it as ``data.is_ai`` (reference/conventions.md 5.5). The old name
        substring test adopted every human called MAIK, RAID or CAIN.
        """
        return bool(getattr(vehicle.data, 'is_ai', False))

    def _process_active(self, own_vehicle: OwnVehicle, vehicles: Dict[int, Vehicle]) -> Dict[str, Any]:
        """Normal active processing: assign routes and drive vehicles."""
        # Bound once: _on_state_data may drop self.routes from the packet
        # thread at any point during this pass (conventions.md 6).
        routes = self.routes
        if not routes:
            return {'ai_active': False}

        all_candidates = self._candidates(own_vehicle, vehicles)
        # Everything this pass sends is gated on this set (see _control).
        self._live_plids = frozenset(all_candidates)

        # -- Forget cars that are gone, before anything is sent --
        # This used to run *after* the AI-info re-request below, so the one
        # pass in which a race ended still sent an IS_AIC to every car that
        # had just disappeared: "no driver to control", once per car, which is
        # exactly what the driver sees in the chat.
        for vehicle_id in [vid for vid in list(self.assigned_routes)
                           if vid not in all_candidates]:
            self._forget_vehicle(vehicle_id)
            logger.info("Vehicle %s left - removed from AI traffic.", vehicle_id)

        # -- Assign routes to new (unassigned) AI vehicles --
        # The local driver is never a candidate, whatever OutGauge says: while
        # local_plid is unknown, own_vehicle.data.player_id is only the car the
        # *camera* is on (conventions.md 5.4).
        local_plid = getattr(own_vehicle, 'local_plid', 0)
        for vehicle_id, vehicle in all_candidates.items():
            if vehicle_id in self.assigned_routes or vehicle_id == local_plid:
                continue
            # Only control cars LFS itself marks as AI (IS_NPL.PType bit 1)
            if not self._is_local_ai_vehicle(vehicle):
                continue

            route_id = self._find_closest_route(vehicle)
            if route_id is None:
                continue
            self.assigned_routes[vehicle_id] = route_id
            vehicle.current_route = route_id
            self._route_index.pop(vehicle_id, None)
            self._off_route.pop(vehicle_id, None)
            logger.info("Vehicle %s (%s) assigned to route %s",
                        vehicle_id, vehicle.data.pname, route_id)

            # Bind AI info handler and request periodic updates
            if self.ai_controller is not None:
                self.ai_controller.bind_ai_info_handler(vehicle_id, self.monitor_ai)
                self.ai_controller.request_ai_info(vehicle_id, repeat_interval=100)
                self._last_ai_info_time[vehicle_id] = time.time()

        # -- Re-request AI info for vehicles that stopped receiving data --
        if self.ai_controller is not None:
            now = time.time()
            for vehicle_id in list(self.assigned_routes):
                if vehicle_id not in self._live_plids:
                    continue
                last_time = self._last_ai_info_time.get(vehicle_id)
                if last_time is not None and (now - last_time) > self.AI_INFO_TIMEOUT:
                    self.ai_controller.bind_ai_info_handler(vehicle_id, self.monitor_ai)
                    self.ai_controller.request_ai_info(vehicle_id, repeat_interval=100)
                    self._last_ai_info_time[vehicle_id] = now
                    # Debug, not info: with no IS_AII coming back this repeats
                    # once per vehicle every AI_INFO_TIMEOUT seconds.
                    logger.debug("Re-requested AI info for vehicle %s (timeout)", vehicle_id)

        # -- Obstacle snapshot, built once per pass --
        # Collision avoidance used to convert every other car's position from
        # game units inside the per-car loop, i.e. O(cars^2) divisions for one
        # answer that is the same for all of them. One tuple per car per pass,
        # ~40 tuples on a full grid.
        obstacles = [
            (plid, candidate.data.x / 65536, candidate.data.y / 65536,
             getattr(candidate.data, 'speed', 0.0) or 0.0)
            for plid, candidate in all_candidates.items()
        ]

        # -- Drive each assigned vehicle along its route --
        # Over a snapshot: _on_player_left may delete an entry from the packet
        # thread while this runs (conventions.md 6).
        for vehicle_id, route_id in list(self.assigned_routes.items()):
            vehicle = all_candidates.get(vehicle_id)
            if vehicle is None:
                continue

            route_data = routes.get(route_id)
            if route_data is None:
                continue

            self._drive_vehicle(vehicle_id, vehicle, route_id, route_data, obstacles)

        return {'ai_active': True}

    def _smooth(self, vehicle_id: int, raw_throttle: float,
                raw_brake: float, raw_steer: float) -> Tuple[float, float, float]:
        """
        Apply first-order smoothing to control outputs.
        Each cycle moves 1/SMOOTHING_STEPS toward the target.
        Handles targets that change every cycle gracefully.

        Returns:
            (smoothed_throttle, smoothed_brake, smoothed_steer)
        """
        if vehicle_id not in self._smoothed:
            # First cycle: jump to target immediately
            self._smoothed[vehicle_id] = {
                'throttle': raw_throttle,
                'brake': raw_brake,
                'steer': raw_steer,
            }
            return raw_throttle, raw_brake, raw_steer

        s = self._smoothed[vehicle_id]
        alpha_throttle = 1.0 / self.SMOOTHING_STEPS_THROTTLE
        alpha_brake = 1.0 / self.SMOOTHING_STEPS_BRAKE

        s['throttle'] += (raw_throttle - s['throttle']) * alpha_throttle
        s['brake'] += (raw_brake - s['brake']) * alpha_brake

        # Steering is NOT smoothed – smoothing causes overshoot and oscillation
        s['steer'] = raw_steer

        return s['throttle'], s['brake'], s['steer']

    # --- Collision Avoidance -----------------------------------------

    @staticmethod
    def heading_vector(heading: float) -> Tuple[float, float]:
        """Unit vector the car points along, from an MCI heading.

        MCI heading is in LFS units (182 per degree) and 0 means +Y, growing
        counter-clockwise -- the same convention ``calculate_angle`` encodes
        implicitly (``conventions.md`` 3). Written out here because the
        corridor test needs the vector itself, not an angle difference, and
        because getting this wrong points the whole collision check backwards.
        """
        radians = math.radians(heading / 182.0)
        return -math.sin(radians), math.cos(radians)

    def _closest_obstacle_ahead(self, vehicle_id: int, vx: float, vy: float,
                                forward_x: float, forward_y: float,
                                obstacles) -> Tuple[float, float]:
        """Nearest car inside this car's forward corridor.

        Returns ``(gap, lead_speed)`` -- centre-to-centre distance along the
        direction of travel in metres and that car's speed in km/h -- or
        ``(inf, 0.0)`` when the corridor is clear.

        The corridor replaces the old +-12 deg cone. A cone is a fixed
        *angle*, so it narrows to nothing exactly where the geometry matters:
        at 5 m it was 1.0 m wide, less than one car, and a stopped car half a
        width off centre was invisible until the moment of contact. The
        corridor is a fixed *width* instead, widening slowly with range so a
        gentle curve stays inside it without reaching into the oncoming lane.

        Every car LFS reports counts, not only the ones this system drives:
        the player, an AI that was never adopted and a car parked across the
        road are all equally solid.

        Cost: one dot and one cross product per other car, no square roots and
        no trigonometry -- that lives in ``heading_vector``, once per car.
        """
        best_gap = float('inf')
        best_speed = 0.0
        half_width = self.CA_CORRIDOR_HALF_WIDTH
        spread = self.CA_CORRIDOR_SPREAD
        reach = self.CA_DETECTION_DISTANCE

        for other_id, ox, oy, ospeed in obstacles:
            if other_id == vehicle_id:
                continue
            dx = ox - vx
            dy = oy - vy
            ahead = dx * forward_x + dy * forward_y
            if ahead <= 0.0 or ahead >= best_gap or ahead > reach:
                continue
            side = abs(dx * forward_y - dy * forward_x)
            if side > half_width + spread * ahead:
                continue
            best_gap = ahead
            best_speed = ospeed

        return best_gap, best_speed

    def _calculate_following_speed(self, distance: float,
                                   lead_speed: float = 0.0) -> float:
        """Highest speed this car may hold behind the one ahead, in km/h.

        Gipps' safe-speed law: the speed from which, after one reaction time
        at constant velocity, a constant deceleration ``CA_COMFORT_DECEL``
        still brings the car to ``CA_STANDSTILL_GAP`` behind an obstacle that
        is itself travelling at ``lead_speed``:

            v = sqrt((a*tau)^2 + v_lead^2 + 2*a*(gap - gap_min)) - a*tau

        Assumptions, all deliberate and all conservative:

        * ``a = 4.0 m/s2`` (~0.4 g). Dry tarmac and a street tyre give roughly
          mu = 0.9, i.e. ~8.8 m/s2 -- half of the friction circle is left over
          for steering, which is what keeps the car on its route while it
          brakes (Kamm, ``AGENTS.md`` rule 2).
        * ``tau = 0.35 s`` is this controller's own dead time, not a human's:
          one 100 ms process cycle plus the lag of the brake filter.
        * ``gap_min = 6 m`` is centre to centre, so about 1.5 m of air between
          the bumpers of two 4.5 m cars.

        The previous law was a straight line from 70 km/h at 50 m to 0 at 10 m
        that ignored the speed of the car ahead entirely: a car following
        another at the same speed was braked for no reason, and a car closing
        on a standing one was allowed 16 km/h at 10 m, which is 1.6 m of
        stopping distance short.
        """
        lead_ms = max(0.0, lead_speed) / 3.6
        free = max(0.0, distance - self.CA_STANDSTILL_GAP)
        at = self.CA_COMFORT_DECEL * self.CA_REACTION_TIME
        safe_ms = math.sqrt(at * at + lead_ms * lead_ms
                            + 2.0 * self.CA_COMFORT_DECEL * free) - at
        return max(0.0, safe_ms) * 3.6

    def _required_deceleration(self, gap: float, speed: float,
                               lead_speed: float) -> float:
        """Constant deceleration needed to avoid touching the car ahead.

        ``(v^2 - v_lead^2) / (2 * free distance)`` -- the textbook form, with
        the standstill gap taken off the distance so "avoided" means stopping
        behind the other car rather than inside it. Zero when this car is not
        closing.
        """
        own_ms = max(0.0, speed) / 3.6
        lead_ms = max(0.0, lead_speed) / 3.6
        if own_ms <= lead_ms:
            return 0.0
        free = gap - self.CA_STANDSTILL_GAP
        if free <= 0.0:
            return float('inf')
        return (own_ms * own_ms - lead_ms * lead_ms) / (2.0 * free)

    # ─── Marker interaction ─────────────────────────────────────────

    def _process_markers(self, vehicle_id: int, vx: float, vy: float, vz: float,
                         current_speed: float) -> Tuple[Optional[float], Optional[IndicatorMode]]:
        """
        Check proximity to markers and return overrides.

        Returns:
            (brake_override, indicator_override)
            - brake_override: brake % if stop line active, else None
            - indicator_override: IndicatorMode if arrow active, else None
        """
        brake_override: Optional[float] = None
        indicator_override: Optional[IndicatorMode] = None

        # Lazy-init per-vehicle state
        if vehicle_id not in self._marker_cooldown:
            self._marker_cooldown[vehicle_id] = set()

        cooldown_set = self._marker_cooldown[vehicle_id]
        stop_state = self._stop_state.get(vehicle_id, "idle")

        # ── Stop-line state machine ──
        if stop_state == "braking":
            # Keep braking until nearly stopped
            if current_speed < 1.0:
                self._stop_state[vehicle_id] = "stopped"
                self._stop_cooldown[vehicle_id] = 5  # 5 cycles = 500ms pause
                brake_override = 100.0
            else:
                brake_override = self.STOP_BRAKE_POWER
        elif stop_state == "stopped":
            remaining = self._stop_cooldown.get(vehicle_id, 0) - 1
            if remaining <= 0:
                self._stop_state[vehicle_id] = "idle"
                self._stop_cooldown.pop(vehicle_id, None)
            else:
                self._stop_cooldown[vehicle_id] = remaining
                brake_override = 100.0  # hold brake while waiting
        elif stop_state == "idle":
            # Check proximity to stop lines
            for i, (sx, sy, sz) in enumerate(self._stop_lines):
                dx = vx - sx
                dy = vy - sy
                d_sq = dx * dx + dy * dy
                if d_sq < self.MARKER_TRIGGER_DISTANCE * self.MARKER_TRIGGER_DISTANCE:
                    marker_key = ("stop", i)
                    if marker_key not in cooldown_set:
                        self._stop_state[vehicle_id] = "braking"
                        brake_override = self.STOP_BRAKE_POWER
                        cooldown_set.add(marker_key)
                        break
                elif d_sq > self.MARKER_COOLDOWN_DISTANCE * self.MARKER_COOLDOWN_DISTANCE:
                    cooldown_set.discard(("stop", i))

        # ── Indicator timer countdown ──
        ind_remaining = self._indicator_timer.get(vehicle_id, 0)
        if ind_remaining > 0:
            self._indicator_timer[vehicle_id] = ind_remaining - 1
        elif ind_remaining == 0 and vehicle_id in self._indicator_timer:
            # Timer just expired – cancel indicator
            indicator_override = IndicatorMode.CANCEL
            del self._indicator_timer[vehicle_id]

        # Only check new arrow triggers when no indicator is active
        if vehicle_id not in self._indicator_timer:
            # Check left arrows
            for i, (ax, ay, az) in enumerate(self._arrows_left):
                dx = vx - ax
                dy = vy - ay
                d_sq = dx * dx + dy * dy
                if d_sq < self.MARKER_TRIGGER_DISTANCE * self.MARKER_TRIGGER_DISTANCE:
                    marker_key = ("left", i)
                    if marker_key not in cooldown_set:
                        indicator_override = IndicatorMode.LEFT
                        self._indicator_timer[vehicle_id] = self.INDICATOR_DURATION_CYCLES
                        cooldown_set.add(marker_key)
                        break
                elif d_sq > self.MARKER_COOLDOWN_DISTANCE * self.MARKER_COOLDOWN_DISTANCE:
                    cooldown_set.discard(("left", i))

            # Check right arrows (only if left wasn't just triggered)
            if indicator_override is None and vehicle_id not in self._indicator_timer:
                for i, (ax, ay, az) in enumerate(self._arrows_right):
                    dx = vx - ax
                    dy = vy - ay
                    d_sq = dx * dx + dy * dy
                    if d_sq < self.MARKER_TRIGGER_DISTANCE * self.MARKER_TRIGGER_DISTANCE:
                        marker_key = ("right", i)
                        if marker_key not in cooldown_set:
                            indicator_override = IndicatorMode.RIGHT
                            self._indicator_timer[vehicle_id] = self.INDICATOR_DURATION_CYCLES
                            cooldown_set.add(marker_key)
                            break
                    elif d_sq > self.MARKER_COOLDOWN_DISTANCE * self.MARKER_COOLDOWN_DISTANCE:
                        cooldown_set.discard(("right", i))

        return brake_override, indicator_override

    # ─── Vehicle control ──────────────────────────────────────────────

    def _drive_vehicle(self, vehicle_id: int, vehicle: Vehicle, route_id: int,
                       route_data: Dict[str, Any], obstacles):
        """
        Execute one control step for a single vehicle along its route.

        Args:
            vehicle_id: Player ID of the vehicle
            vehicle: Vehicle object with current position/state
            route_id: Key of route_data in self.routes (cache key)
            route_data: Route dict with 'path' key
            obstacles: ``(plid, x_m, y_m, speed_kmh)`` for every car LFS is
                reporting, the player's included, built once per pass

        Cost per car per cycle: 2*ROUTE_SEARCH_WINDOW+1 = 41 squared-distance
        comparisons for the nearest-point search (was: one per route point,
        up to 528 on SO, each with a square root), one distance to the point
        found to notice a car that is no longer on its route, one curvature
        analysis over the ~5-8 points of the speed-dependent lookahead, one
        dict lookup for the 120 m long-straight result, the marker scan, and
        one dot-plus-cross product per other car for collision avoidance.
        """
        if self.ai_controller is None:
            return

        data = vehicle.data          # one consistent snapshot (conventions.md 6)

        # Get vehicle position (convert from game units)
        vehicle_x = data.x / 65536
        vehicle_y = data.y / 65536
        vehicle_z = data.z / 65536

        # Dynamic brake lookahead: 15m at 10 km/h, 40m at 60 km/h (linear)
        current_speed = getattr(data, 'speed', 0.0) or 0.0

        # -- Marker interaction (stop lines, indicators) --
        brake_override, indicator_override = self._process_markers(
            vehicle_id, vehicle_x, vehicle_y, vehicle_z, current_speed
        )

        # Find closest point on route and get upcoming points. The search is
        # windowed around last cycle's index and falls back to a full scan
        # when the car is not where that index says (respawn, /restart).
        closest_index = get_closest_index_on_route(
            vehicle_x, vehicle_y, vehicle_z, route_data,
            previous_index=self._route_index.get(vehicle_id)
        )
        self._route_index[vehicle_id] = closest_index

        # -- Is this car still on the route it was given? --
        # Route assignment used to be permanent. A car adopted in the moment
        # before /restart teleported it kept steering towards a road on the
        # other side of town, at full throttle, for the rest of the run. The
        # nearest point is already known, so noticing costs one distance.
        if self._is_off_route(vehicle_id, vehicle, route_data, closest_index,
                              vehicle_x, vehicle_y, vehicle_z):
            return

        lookahead_dist = 15.0 + (current_speed - 10.0) * 0.5  # 25m over 50 km/h range
        lookahead_dist = max(15.0, min(40.0, lookahead_dist))

        upcoming_points = get_next_points_for_distance(
            closest_index, route_data, min_distance=lookahead_dist, min_points=5
        )
        if not upcoming_points:
            # Empty path -- validated against at load time, but never steer on
            # a guess if it happens anyway.
            return

        # Analyze the upcoming track section
        curvature, target_point = analyze_upcoming_track(upcoming_points)

        # -- Long-straight detection: look 120m ahead for curves --
        # Depends only on the route and the index, both of which are fixed
        # while the route is loaded, so the answer is cached instead of
        # recomputed for every car every cycle.
        cache_key = (route_id, closest_index)
        long_straight = self._straight_cache.get(cache_key)
        if long_straight is None:
            far_points = get_next_points_for_distance(
                closest_index, route_data,
                min_distance=self.STRAIGHT_LOOKAHEAD_DIST, min_points=5
            )
            far_curvature, _ = analyze_upcoming_track(far_points)
            long_straight = far_curvature < self.CURVATURE_THRESHOLD
            self._straight_cache[cache_key] = long_straight

        # -- Speed feedforward --
        target_speed = self.calculate_target_speed(curvature, long_straight=long_straight)

        # -- Collision avoidance override --
        # One corridor test against every car on track: the player, the other
        # controlled cars, and any car this system does not drive. Which of
        # them it is does not change how solid it is, and the old split
        # ("check the player unless the own PLID happens to be an adopted
        # car") made the player invisible to the whole field as soon as
        # OutGauge pointed at somebody else.
        forward_x, forward_y = self.heading_vector(data.heading)
        gap, lead_speed = self._closest_obstacle_ahead(
            vehicle_id, vehicle_x, vehicle_y, forward_x, forward_y, obstacles)

        emergency_brake = False
        if gap < float('inf'):
            target_speed = min(target_speed,
                               self._calculate_following_speed(gap, lead_speed))
            # Below the comfort limit this is no longer a following manoeuvre.
            emergency_brake = (self._required_deceleration(gap, current_speed, lead_speed)
                               > self.CA_EMERGENCY_DECEL)

        if emergency_brake:
            raw_throttle = 0.0
            raw_brake = 100.0
        else:
            speed_error = target_speed - current_speed
            raw_throttle, raw_brake = calculate_feedforward_throttle_brake(
                speed_error, gain=self.SPEED_GAIN
            )
            raw_throttle = min(raw_throttle, self.MAX_THROTTLE)

        # -- Steering feedforward --
        target_angle = calculate_angle(
            data.x, data.y,
            target_point[0], target_point[1],
            data.heading
        )

        raw_steer = calculate_feedforward_steering(
            target_angle, max_steering_angle=self.MAX_STEERING_ANGLE
        )

        # -- Apply stop-line brake override --
        if brake_override is not None:
            raw_throttle = 0.0
            raw_brake = brake_override

        # -- Apply smoothing --
        throttle, brake, steering = self._smooth(vehicle_id, raw_throttle, raw_brake, raw_steer)

        if emergency_brake:
            # The brake filter takes four cycles to reach full pressure. That
            # is right for ride comfort and wrong for an emergency stop: 0.4 s
            # at 50 km/h is 5.5 m, more than the gap this fires at.
            brake = 100.0
            throttle = 0.0
            state = self._smoothed.get(vehicle_id)
            if state is not None:
                state['brake'] = 100.0
                state['throttle'] = 0.0

        # -- Send control commands --
        self._control(vehicle_id, AIControlState(
            throttle=int(throttle),
            brake=int(brake),
            steer=int(steering),
        ))

        # -- Send indicator command (only when state changes) --
        if indicator_override is not None:
            self._control(vehicle_id, AIControlState(indicators=indicator_override))

    def _is_off_route(self, vehicle_id: int, vehicle: Vehicle,
                      route_data: Dict[str, Any], closest_index: int,
                      vehicle_x: float, vehicle_y: float, vehicle_z: float) -> bool:
        """Has this car left its route for good, and has it been re-assigned?

        Two independent conditions, because one alone gives false positives on
        real data:

        * the distance is measured to the **road**, not to the nearest stored
          point (``distance_to_route_sq``). Point spacing is authored by hand
          and reaches 41 m, which would put a car driving down the middle of
          SO road 33 within 4.4 m of the threshold -- at the same place, every
          lap.
        * a single cycle beyond it means nothing anyway: a cut corner, a
          kerbed wheel, an MCI frame one update behind. Only after
          ``OFF_ROUTE_REASSIGN_CYCLES`` consecutive cycles is the route picked
          again from scratch, and one cycle back on the road resets the count.

        Returns True when the assignment changed, i.e. the caller must not
        steer this cycle: the route index belongs to the old road.
        """
        path = route_data.get('path') or []
        if not path:
            return False
        distance_sq = distance_to_route_sq(
            path, closest_index, vehicle_x, vehicle_y, vehicle_z,
            closed_loop=bool(route_data.get('closed_loop', False)))
        if distance_sq <= self.OFF_ROUTE_DISTANCE_M * self.OFF_ROUTE_DISTANCE_M:
            if self._off_route.get(vehicle_id):
                self._off_route[vehicle_id] = 0
            return False

        cycles = self._off_route.get(vehicle_id, 0) + 1
        self._off_route[vehicle_id] = cycles
        if cycles < self.OFF_ROUTE_REASSIGN_CYCLES:
            return False

        self._off_route[vehicle_id] = 0
        new_route = self._find_closest_route(vehicle)
        if new_route is None or new_route == self.assigned_routes.get(vehicle_id):
            return False
        self.assigned_routes[vehicle_id] = new_route
        vehicle.current_route = new_route
        self._route_index.pop(vehicle_id, None)
        logger.info("Vehicle %s was off its route - re-assigned to route %s",
                    vehicle_id, new_route)
        return True
