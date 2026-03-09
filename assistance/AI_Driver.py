import math
import os
import time
from typing import Dict, Any, List, Optional, Tuple
import json

from AI_Control import AIControlState, IndicatorMode
from assistance.base_system import AssistanceSystem
from core.event_bus import EventBus
from core.settings_manager import SettingsManager
from misc.language import LanguageManager
from misc.helpers import resolve_path
from vehicles.own_vehicle import OwnVehicle
from vehicles.vehicle import Vehicle


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


def load_routes_from_file(file_path: str) -> Tuple[List[Dict[str, Any]], List[Dict[str, Any]]]:
    """Lädt Routen und Marker aus einer Datei. Bei inverted=True wird der Pfad umgekehrt.

    Returns:
        Tuple of (roads, markers)
    """
    with open(file_path, 'r', encoding='utf-8') as file:
        data = json.load(file)
    roads = data.get('roads', [])
    for road in roads:
        if road.get('inverted', False):
            road['path'] = list(reversed(road['path']))
    markers = data.get('markers', [])
    return roads, markers


def get_closest_index_on_route(carX, carY, carZ, route_points):
    """
    Find the index of the closest point on the route to the car's current position.

    Args:
        carX, carY, carZ: Current car position coordinates
        route_points: Dict containing 'path' key with list of [x, y, z] points

    Returns:
        int: Index of the closest point in the route
    """
    path = route_points.get('path', [])
    if not path:
        return 0

    car_pos = (carX, carY, carZ)
    min_distance = float('inf')
    closest_index = 0

    for i, point in enumerate(path):
        distance = dist(car_pos, tuple(point))
        if distance < min_distance:
            min_distance = distance
            closest_index = i

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
    ALLOWED_TRACKS = {b'BL1X', b'SO7', b'KY1X'}

    # Track-specific layout hint notifications (matched by track prefix)
    TRACK_LAYOUT_HINTS = {
        b'BL': '^7Select GP Track X',
        b'SO': '^7Select City',
        b'KY': '^7Select Oval X',
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

        self.event_bus.subscribe("AI_Controller_initialized", self._on_ai_controller_initialized)
        self.event_bus.subscribe("ai_traffic_start", self._on_start)
        self.event_bus.subscribe("ai_traffic_stop", self._on_stop)

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

        # Collision avoidance parameters
        self.CA_DETECTION_DISTANCE = 50.0   # Start reacting at this distance (meters)
        self.CA_EMERGENCY_DISTANCE = 10.0   # Full brake below this distance (meters)
        self.CA_MAX_SPEED_AT_LIMIT = 70.0   # Max allowed speed at CA_DETECTION_DISTANCE (km/h)
        self.CA_CONE_HALF_ANGLE = 12.0      # Half-angle of forward detection cone (degrees)

        # Smoothing: each cycle, move 1/SMOOTHING_STEPS of the remaining distance
        # toward the target. Handles targets that change every cycle gracefully.
        self.SMOOTHING_STEPS_THROTTLE = 10.0
        self.SMOOTHING_STEPS_BRAKE = 2.0
        self._smoothed: Dict[int, Dict[str, float]] = {}  # vehicle_id → {throttle, brake, steer}

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
        """Listen for track changes to reset AI traffic."""
        track = data.get('track')
        if track != self.current_track:
            self.current_track = track
            if self.state != self.STATE_INACTIVE:
                print(f"[AIDriver] Track changed to {track} – stopping AI traffic.")
                self._on_stop()
            self.routes = None

    def _on_ai_controller_initialized(self, ai_controller):
        self.ai_controller = ai_controller

    # ─── Start / Stop ─────────────────────────────────────────────────

    def _on_start(self, data=None):
        """Start AI traffic. Route assignment happens in the next process() call."""
        if self.state != self.STATE_INACTIVE:
            return

        # --- Validate track data file exists ---
        trackname = str(self.current_track[:2])[2:4]
        file_path = resolve_path("track_data", f"track_data_{trackname}.json")
        if not os.path.isfile(file_path):
            lang = self.settings.get('language')
            self.event_bus.emit("notification",
                                {'notification': '^1' + self.translator.get('Traffic not avail. on this map', lang)})
            print(f"[AIDriver] Track data file not found: {file_path}")

            return
        # --- Validate track configuration ---
        if self.current_track not in self.ALLOWED_TRACKS:
            lang = self.settings.get('language')
            self.event_bus.emit("notification",
                               {'notification': '^1' + self.translator.get('Wrong track config for traffic', lang)})
            print(f"[AIDriver] Track {self.current_track} is not a valid traffic track.")
            # Emit track-specific layout hint notification
            track_prefix = self.current_track[:2]
            hint = self.TRACK_LAYOUT_HINTS.get(track_prefix)
            if hint:
                self.event_bus.emit("notification", {'notification': hint})
            return



        # --- All checks passed – start traffic ---
        self.event_bus.emit("send_command_to_lfs", "/axload AI_Traffic")
        self.event_bus.emit("send_command_to_lfs", "/restart")

        self._load_routes()
        self.assigned_routes = {}
        self._smoothed = {}
        self.state = self.STATE_ACTIVE
        self.event_bus.emit("ai_traffic_state_changed", {"active": True})



        print("[AIDriver] AI traffic started.")

    def _on_stop(self, data=None):
        """Initiate stop sequence: brake all vehicles, then release control."""
        if self.state != self.STATE_ACTIVE:
            return
        self.state = self.STATE_STOPPING
        self.stop_counter = 0
        self.event_bus.emit("ai_traffic_state_changed", {"active": False})
        print("[AIDriver] AI traffic stopping – braking all vehicles...")

    @property
    def is_active(self) -> bool:
        return self.state == self.STATE_ACTIVE

    # ─── Route helpers ────────────────────────────────────────────────

    def _load_routes(self):
        """Load routes and markers from file (once)."""
        if self.routes is None:
            print(self.current_track)
            trackname = str(self.current_track[:2])[2:4] if self.current_track else None

            if trackname is not None:
                roads_list, markers_list = load_routes_from_file(resolve_path("track_data", f"track_data_{trackname}.json"))
                self.routes = {road['road_id']: road for road in roads_list}

                # Store and pre-split markers by type for fast lookup
                self._markers = markers_list
                self._stop_lines = [
                    tuple(m['position']) for m in markers_list if m['type'] == 'stop_line'
                ]
                self._arrows_left = [
                    tuple(m['position']) for m in markers_list if m['type'] == 'arrow_left'
                ]
                self._arrows_right = [
                    tuple(m['position']) for m in markers_list if m['type'] == 'arrow_right'
                ]
                print(f"[AIDriver] Loaded {len(self._stop_lines)} stop lines, "
                      f"{len(self._arrows_left)} left arrows, "
                      f"{len(self._arrows_right)} right arrows.")

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
        min_distance = float('inf')
        closest_route_id = None
        for road_id, road in self.routes.items():
            for point in road.get('path', []):
                d = dist((vx, vy, vz), tuple(point))
                if d < min_distance:
                    min_distance = d
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
            self.ai_controller.control_ai(plid, AIControlState(shift_up=False))
            pending["up"] = False
        elif wants_shift_up:
            # No pending reset → send the actual shift command
            self.ai_controller.control_ai(plid, AIControlState(shift_up=True))
            pending["up"] = True

        # ── Shift Down ──
        wants_shift_down = aii.RPM < 1700 and aii.Gear > 2
        if pending["down"]:
            # Last cycle sent True → always reset to False first
            self.ai_controller.control_ai(plid, AIControlState(shift_down=False))
            pending["down"] = False
        elif wants_shift_down:
            # No pending reset → send the actual shift command
            self.ai_controller.control_ai(plid, AIControlState(shift_down=True))
            pending["down"] = True

        # ── Stall recovery (ignition) ──
        if aii.RPM < 300:
            self.ai_controller.control_ai(plid, AIControlState(ignition=True))
        else:
            self.ai_controller.control_ai(plid, AIControlState(ignition=False))

    # ─── Main process loop ────────────────────────────────────────────

    def process(self, own_vehicle: OwnVehicle, vehicles: Dict[int, Vehicle]) -> Dict[str, Any]:
        """Main processing loop, called every 100 ms."""

        # ── Inactive: nothing to do ──
        if self.state == self.STATE_INACTIVE:
            return {'ai_active': False}

        # ── Stopping phase: brake all vehicles, then release control ──
        if self.state == self.STATE_STOPPING:
            return self._process_stopping(vehicles)

        # ── Active: drive all assigned vehicles ──
        return self._process_active(own_vehicle, vehicles)

    def _process_stopping(self, vehicles: Dict[int, Vehicle]) -> Dict[str, Any]:
        """Send brake commands during stop phase, then release control."""
        if self.ai_controller is None:
            self._finalize_stop(vehicles)
            return {'ai_active': False}

        # Send full brake to every assigned vehicle
        for vehicle_id in list(self.assigned_routes.keys()):
            self.ai_controller.control_ai(vehicle_id, AIControlState(
                throttle=0,
                brake=100,
            ))

        self.stop_counter += 1

        if self.stop_counter >= self.STOP_BRAKE_CYCLES:
            self._finalize_stop(vehicles)
            return {'ai_active': False}

        return {'ai_active': True, 'stopping': True}

    def _finalize_stop(self, vehicles: Dict[int, Vehicle]):
        """Release AI control on all vehicles and clean up."""
        if self.ai_controller is not None:
            for vehicle_id in list(self.assigned_routes.keys()):
                try:
                    self.ai_controller.stop_ai_control(vehicle_id)
                except Exception as e:
                    print(f"[AIDriver] Error stopping control for vehicle {vehicle_id}: {e}")

        # Reset route assignments on vehicle objects
        for vehicle_id in list(self.assigned_routes.keys()):
            vehicle = vehicles.get(vehicle_id)
            if vehicle is not None:
                vehicle.current_route = None

        self.assigned_routes.clear()
        self._smoothed.clear()
        self._shift_pending.clear()
        self._last_ai_info_time.clear()
        self._stop_state.clear()
        self._stop_cooldown.clear()
        self._indicator_timer.clear()
        self._marker_cooldown.clear()
        self.state = self.STATE_INACTIVE
        self.stop_counter = 0
        print("[AIDriver] AI traffic fully stopped.")

    def _is_local_ai_vehicle(self, vehicle) -> bool:
        """Check whether a vehicle is a local AI driver by its player name.

        LFS default AI driver names contain 'AI' (e.g. 'AI 1', 'AI 2').
        The pname field can be bytes or str depending on the source.
        """
        pname = vehicle.data.pname
        if isinstance(pname, bytes):
            return b'AI' in pname
        if isinstance(pname, str):
            return 'AI' in pname
        return False

    def _process_active(self, own_vehicle: OwnVehicle, vehicles: Dict[int, Vehicle]) -> Dict[str, Any]:
        """Normal active processing: assign routes and drive vehicles."""
        if not self.routes:
            return {'ai_active': False}

        # ── Build combined dict of all vehicles that could be AI-controlled ──
        # vehicles dict does NOT contain own_vehicle (VehicleManager separates it),
        # but own_vehicle might itself be an AI driver (e.g. camera on an AI car).
        all_candidates: Dict[int, Vehicle] = dict(vehicles)
        if (own_vehicle.data.player_id != 0
                and own_vehicle.data.player_id not in all_candidates):
            all_candidates[own_vehicle.data.player_id] = own_vehicle

        # ── Assign routes to new (unassigned) AI vehicles ──
        for vehicle_id, vehicle in all_candidates.items():
            if vehicle_id not in self.assigned_routes:
                # Only control vehicles whose player name contains "AI"
                if not self._is_local_ai_vehicle(vehicle):
                    continue

                route_id = self._find_closest_route(vehicle)
                if route_id is not None:
                    self.assigned_routes[vehicle_id] = route_id
                    vehicle.current_route = route_id
                    print(f"[AIDriver] Vehicle {vehicle_id} ({vehicle.data.pname}) assigned to route {route_id}")

                    # Bind AI info handler and request periodic updates
                    if self.ai_controller is not None:
                        self.ai_controller.bind_ai_info_handler(vehicle_id, self.monitor_ai)
                        self.ai_controller.request_ai_info(vehicle_id, repeat_interval=100)
                        self._last_ai_info_time[vehicle_id] = time.time()

        # ── Re-request AI info for vehicles that stopped receiving data ──
        if self.ai_controller is not None:
            now = time.time()
            for vehicle_id in self.assigned_routes:
                last_time = self._last_ai_info_time.get(vehicle_id)
                if last_time is not None and (now - last_time) > self.AI_INFO_TIMEOUT:
                    self.ai_controller.bind_ai_info_handler(vehicle_id, self.monitor_ai)
                    self.ai_controller.request_ai_info(vehicle_id, repeat_interval=100)
                    self._last_ai_info_time[vehicle_id] = now
                    print(f"[AIDriver] Re-requested AI info for vehicle {vehicle_id} (timeout)")

        # ── Remove vehicles that have left the track ──
        departed = [vid for vid in self.assigned_routes if vid not in all_candidates]
        for vid in departed:
            del self.assigned_routes[vid]
            self._smoothed.pop(vid, None)
            self._shift_pending.pop(vid, None)
            self._last_ai_info_time.pop(vid, None)
            self._stop_state.pop(vid, None)
            self._stop_cooldown.pop(vid, None)
            self._indicator_timer.pop(vid, None)
            self._marker_cooldown.pop(vid, None)
            print(f"[AIDriver] Vehicle {vid} left – removed from AI traffic.")

        # ── Drive each assigned vehicle along its route ──
        for vehicle_id, route_id in self.assigned_routes.items():
            vehicle = all_candidates.get(vehicle_id)
            if vehicle is None:
                continue

            route_data = self.routes.get(route_id)
            if route_data is None:
                continue

            self._drive_vehicle(vehicle_id, vehicle, route_data, own_vehicle, all_candidates)

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

    # ─── Collision Avoidance ──────────────────────────────────────────

    def _is_player_ahead_of_vehicle(self, vehicle: Vehicle,
                                    own_vehicle: OwnVehicle) -> Tuple[bool, float]:
        """
        Check whether the player's own vehicle is directly ahead of an AI vehicle.

        Uses the AI vehicle's heading to define a forward detection cone and checks
        if the player falls within it.

        Args:
            vehicle: The AI vehicle
            own_vehicle: The player's own vehicle

        Returns:
            Tuple of (is_ahead, distance_in_meters).
            distance is always returned (even when is_ahead is False) for convenience.
        """
        # Distance between AI vehicle and player (in meters)
        dx = (vehicle.data.x - own_vehicle.data.x) / 65536
        dy = (vehicle.data.y - own_vehicle.data.y) / 65536
        distance = math.sqrt(dx * dx + dy * dy)

        # Quick reject: too far away to matter
        if distance > self.CA_DETECTION_DISTANCE:
            return False, distance

        # Angle from AI vehicle toward the player, relative to AI vehicle's heading
        angle = calculate_angle(
            vehicle.data.x, vehicle.data.y,
            own_vehicle.data.x / 65536, own_vehicle.data.y / 65536,
            vehicle.data.heading
        )

        # Check if angle falls inside the forward cone (centered on 0°)
        in_cone = abs(angle) < self.CA_CONE_HALF_ANGLE
        return in_cone, distance

    def _is_ai_vehicle_ahead(self, vehicle: Vehicle,
                             other_vehicle: Vehicle) -> Tuple[bool, float]:
        """
        Check whether another AI vehicle is directly ahead of the given AI vehicle.

        Same logic as _is_player_ahead_of_vehicle but uses calculate_angle_meters
        since both vehicles have coordinates in game units (both need /65536).

        Args:
            vehicle: The AI vehicle whose forward cone is checked
            other_vehicle: The other AI vehicle to check against

        Returns:
            Tuple of (is_ahead, distance_in_meters).
        """
        # Convert both positions to meters
        vx = vehicle.data.x / 65536
        vy = vehicle.data.y / 65536
        ox = other_vehicle.data.x / 65536
        oy = other_vehicle.data.y / 65536

        dx = vx - ox
        dy = vy - oy
        distance = math.sqrt(dx * dx + dy * dy)

        # Quick reject: too far away to matter
        if distance > self.CA_DETECTION_DISTANCE:
            return False, distance

        # Angle from AI vehicle toward the other AI vehicle, relative to AI vehicle's heading
        angle = calculate_angle_meters(vx, vy, ox, oy, vehicle.data.heading)

        # Check if angle falls inside the forward cone (centered on 0°)
        in_cone = abs(angle) < self.CA_CONE_HALF_ANGLE
        return in_cone, distance

    def _calculate_following_speed(self, distance: float) -> float:
        """
        Calculate the maximum allowed speed based on distance to a vehicle ahead.

        Linear interpolation between CA_DETECTION_DISTANCE (full speed) and
        CA_EMERGENCY_DISTANCE (zero speed).  This method is intentionally simple
        so it can later be replaced with a more sophisticated model (e.g. TTC-based).

        Args:
            distance: Distance to the vehicle ahead in meters.
                      Expected range: [CA_EMERGENCY_DISTANCE, CA_DETECTION_DISTANCE]

        Returns:
            Maximum allowed speed in km/h (0.0 .. CA_MAX_SPEED_AT_LIMIT)
        """
        clamped = max(self.CA_EMERGENCY_DISTANCE,
                      min(self.CA_DETECTION_DISTANCE, distance))

        ratio = ((clamped - self.CA_EMERGENCY_DISTANCE)
                 / (self.CA_DETECTION_DISTANCE - self.CA_EMERGENCY_DISTANCE))

        return self.CA_MAX_SPEED_AT_LIMIT * ratio

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

    def _drive_vehicle(self, vehicle_id: int, vehicle: Vehicle,
                       route_data: Dict[str, Any], own_vehicle: OwnVehicle,
                       vehicles: Dict[int, Vehicle]):
        """
        Execute one control step for a single vehicle along its route.

        Args:
            vehicle_id: Player ID of the vehicle
            vehicle: Vehicle object with current position/state
            route_data: Route dict with 'path' key
            own_vehicle: The player's own vehicle (for collision avoidance)
            vehicles: All AI vehicles dict (for AI-to-AI collision avoidance)
        """
        if self.ai_controller is None:
            return

        # Get vehicle position (convert from game units)
        vehicle_x = vehicle.data.x / 65536
        vehicle_y = vehicle.data.y / 65536
        vehicle_z = vehicle.data.z / 65536

        # Dynamic brake lookahead: 15m at 10 km/h, 40m at 60 km/h (linear)
        current_speed = vehicle.data.speed if hasattr(vehicle.data, 'speed') else 0.0

        # ── Marker interaction (stop lines, indicators) ──
        brake_override, indicator_override = self._process_markers(
            vehicle_id, vehicle_x, vehicle_y, vehicle_z, current_speed
        )

        # Find closest point on route and get upcoming points
        closest_index = get_closest_index_on_route(
            vehicle_x, vehicle_y, vehicle_z, route_data
        )

        lookahead_dist = 15.0 + (current_speed - 10.0) * 0.5  # 25m over 50 km/h range
        lookahead_dist = max(15.0, min(40.0, lookahead_dist))

        upcoming_points = get_next_points_for_distance(
            closest_index, route_data, min_distance=lookahead_dist, min_points=5
        )

        # Analyze the upcoming track section
        curvature, target_point = analyze_upcoming_track(upcoming_points)

        # ── Long-straight detection: look 120m ahead for curves ──
        long_straight = False
        far_points = get_next_points_for_distance(
            closest_index, route_data,
            min_distance=self.STRAIGHT_LOOKAHEAD_DIST, min_points=5
        )
        far_curvature, _ = analyze_upcoming_track(far_points)
        if far_curvature < self.CURVATURE_THRESHOLD:
            long_straight = True

        # ── Speed feedforward ──
        target_speed = self.calculate_target_speed(curvature, long_straight=long_straight)

        # ── Collision avoidance override ──
        # Check player vehicle (only if own_vehicle is NOT a controlled AI vehicle –
        # otherwise it is already handled by the AI-to-AI loop below)
        emergency_brake = False
        closest_distance = float('inf')

        if own_vehicle.data.player_id not in self.assigned_routes:
            is_ahead, player_distance = self._is_player_ahead_of_vehicle(vehicle, own_vehicle)
            if is_ahead:
                closest_distance = player_distance

        # Check other AI vehicles
        for other_id, other_vehicle in vehicles.items():
            if other_id == vehicle_id:
                continue
            if other_id not in self.assigned_routes:
                continue
            ai_ahead, ai_distance = self._is_ai_vehicle_ahead(vehicle, other_vehicle)
            if ai_ahead and ai_distance < closest_distance:
                closest_distance = ai_distance

        # Apply collision avoidance based on closest obstacle
        if closest_distance < float('inf'):
            if closest_distance < self.CA_EMERGENCY_DISTANCE:
                emergency_brake = True
            else:
                ca_speed_limit = self._calculate_following_speed(closest_distance)
                target_speed = min(target_speed, ca_speed_limit)

        if emergency_brake:
            raw_throttle = 0.0
            raw_brake = 100.0
        else:
            speed_error = target_speed - current_speed
            raw_throttle, raw_brake = calculate_feedforward_throttle_brake(
                speed_error, gain=self.SPEED_GAIN
            )
            raw_throttle = min(raw_throttle, self.MAX_THROTTLE)

        # ── Steering feedforward ──
        target_angle = calculate_angle(
            vehicle.data.x, vehicle.data.y,
            target_point[0], target_point[1],
            vehicle.data.heading
        )

        raw_steer = calculate_feedforward_steering(
            target_angle, max_steering_angle=self.MAX_STEERING_ANGLE
        )

        # ── Apply stop-line brake override ──
        if brake_override is not None:
            raw_throttle = 0.0
            raw_brake = brake_override

        # ── Apply smoothing ──
        throttle, brake, steering = self._smooth(vehicle_id, raw_throttle, raw_brake, raw_steer)

        # ── Send control commands ──
        self.ai_controller.control_ai(vehicle_id, AIControlState(
            throttle=int(throttle),
            brake=int(brake),
            steer=int(steering),
        ))

        # ── Send indicator command (only when state changes) ──
        if indicator_override is not None:
            self.ai_controller.control_ai(vehicle_id, AIControlState(
                indicators=indicator_override
            ))

