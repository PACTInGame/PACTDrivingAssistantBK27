import math
from typing import Dict, Any, List, Optional, Tuple
import json

from AI_Control import AIControlState
from assistance.base_system import AssistanceSystem
from core.event_bus import EventBus
from core.settings_manager import SettingsManager
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


def dist(a=(0, 0, 0), b=(0, 0, 0)):
    """Determine the distance between two points."""
    return math.sqrt((b[0] - a[0]) ** 2 + (b[1] - a[1]) ** 2 + (b[2] - a[2]) ** 2)


def load_routes_from_file(file_path: str) -> List[Dict[str, Any]]:
    """Lädt Routen aus einer Datei"""
    with open(file_path, 'r', encoding='utf-8') as file:
        data = json.load(file)
    return data.get('roads', [])


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
    if len(route_points) >= 3:
        target_point = (
            (route_points[1][0] + route_points[2][0]) / 2,
            (route_points[1][1] + route_points[2][1]) / 2,
            (route_points[1][2] + route_points[2][2]) / 2
        )
    else:
        target_point = tuple(route_points[1])

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

    def __init__(self, event_bus: EventBus, settings: SettingsManager):
        super().__init__("ai_traffic", event_bus, settings)
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
        self.MIN_SPEED = 15.0            # Minimum speed on tight curves
        self.CURVATURE_THRESHOLD = 0.01  # Curvature above which to slow down

        # Collision avoidance parameters
        self.CA_DETECTION_DISTANCE = 50.0   # Start reacting at this distance (meters)
        self.CA_EMERGENCY_DISTANCE = 10.0   # Full brake below this distance (meters)
        self.CA_MAX_SPEED_AT_LIMIT = 70.0   # Max allowed speed at CA_DETECTION_DISTANCE (km/h)
        self.CA_CONE_HALF_ANGLE = 12.0      # Half-angle of forward detection cone (degrees)

        # Smoothing: each cycle, move 1/SMOOTHING_STEPS of the remaining distance
        # toward the target. Handles targets that change every cycle gracefully.
        self.SMOOTHING_STEPS_THROTTLE = 4.0
        self.SMOOTHING_STEPS_BRAKE = 2.0
        self.SMOOTHING_STEPS_STEER = 2.0
        self._smoothed: Dict[int, Dict[str, float]] = {}  # vehicle_id → {throttle, brake, steer}

    def _on_ai_controller_initialized(self, ai_controller):
        self.ai_controller = ai_controller

    # ─── Start / Stop ─────────────────────────────────────────────────

    def _on_start(self, data=None):
        """Start AI traffic. Route assignment happens in the next process() call."""
        if self.state != self.STATE_INACTIVE:
            return
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
        """Load routes from file (once)."""
        if self.routes is None:
            routes_list = load_routes_from_file("track_data/track_data.json")
            self.routes = {road['road_id']: road for road in routes_list}

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

    def calculate_target_speed(self, curvature: float) -> float:
        """
        Calculate target speed based on upcoming curvature.

        Args:
            curvature: Average curvature of upcoming section

        Returns:
            Target speed in km/h
        """
        print(curvature)
        if curvature < self.CURVATURE_THRESHOLD:
            return self.BASE_SPEED
        else:
            speed_reduction = (curvature - self.CURVATURE_THRESHOLD) * 1000.0
            return max(self.MIN_SPEED, self.BASE_SPEED - speed_reduction)

    # ─── AI Info monitoring ───────────────────────────────────────────

    def monitor_ai(self, aii):
        """Handle AI info packets – automatic gear shifting and stall recovery."""
        if self.ai_controller is None:
            return
        if aii.RPM > 4500:
            self.ai_controller.control_ai(aii.PLID, AIControlState(shift_up=True))
        if aii.RPM < 2000 and aii.Gear > 2:
            self.ai_controller.control_ai(aii.PLID, AIControlState(shift_down=True))
        if aii.Gear < 1:
            self.ai_controller.control_ai(aii.PLID, AIControlState(shift_up=True))
        if aii.RPM < 300:
            self.ai_controller.control_ai(aii.PLID, AIControlState(ignition=True))

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
            if vehicle_id in vehicles:
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
        self.state = self.STATE_INACTIVE
        self.stop_counter = 0
        print("[AIDriver] AI traffic fully stopped.")

    def _process_active(self, own_vehicle: OwnVehicle, vehicles: Dict[int, Vehicle]) -> Dict[str, Any]:
        """Normal active processing: assign routes and drive vehicles."""
        self._load_routes()
        if not self.routes:
            return {'ai_active': False}

        # ── Assign routes to new (unassigned) vehicles ──
        for vehicle_id, vehicle in vehicles.items():
            if vehicle_id not in self.assigned_routes:
                route_id = self._find_closest_route(vehicle)
                if route_id is not None:
                    self.assigned_routes[vehicle_id] = route_id
                    vehicle.current_route = route_id
                    print(f"[AIDriver] Vehicle {vehicle_id} assigned to route {route_id}")

                    # Bind AI info handler and request periodic updates
                    if self.ai_controller is not None:
                        self.ai_controller.bind_ai_info_handler(vehicle_id, self.monitor_ai)
                        self.ai_controller.request_ai_info(vehicle_id, repeat_interval=100)

        # ── Remove vehicles that have left the track ──
        departed = [vid for vid in self.assigned_routes if vid not in vehicles]
        for vid in departed:
            del self.assigned_routes[vid]
            self._smoothed.pop(vid, None)
            print(f"[AIDriver] Vehicle {vid} left – removed from AI traffic.")

        # ── Drive each assigned vehicle along its route ──
        for vehicle_id, route_id in self.assigned_routes.items():
            vehicle = vehicles.get(vehicle_id)
            if vehicle is None:
                continue

            route_data = self.routes.get(route_id)
            if route_data is None:
                continue

            self._drive_vehicle(vehicle_id, vehicle, route_data, own_vehicle)

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
        alpha_steer = 1.0 / self.SMOOTHING_STEPS_STEER
        alpha_brake = 1.0 / self.SMOOTHING_STEPS_BRAKE

        s['throttle'] += (raw_throttle - s['throttle']) * alpha_throttle
        s['brake'] += (raw_brake - s['brake']) * alpha_brake
        s['steer'] += (raw_steer - s['steer']) * alpha_steer

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

    # ─── Vehicle control ──────────────────────────────────────────────

    def _drive_vehicle(self, vehicle_id: int, vehicle: Vehicle,
                       route_data: Dict[str, Any], own_vehicle: OwnVehicle):
        """
        Execute one control step for a single vehicle along its route.

        Args:
            vehicle_id: Player ID of the vehicle
            vehicle: Vehicle object with current position/state
            route_data: Route dict with 'path' key
            own_vehicle: The player's own vehicle (for collision avoidance)
        """
        if self.ai_controller is None:
            return

        # Get vehicle position (convert from game units)
        vehicle_x = vehicle.data.x / 65536
        vehicle_y = vehicle.data.y / 65536
        vehicle_z = vehicle.data.z / 65536

        # Find closest point on route and get upcoming points
        closest_index = get_closest_index_on_route(
            vehicle_x, vehicle_y, vehicle_z, route_data
        )
        upcoming_points = get_next_points_for_distance(
            closest_index, route_data, min_distance=50.0, min_points=5
        )

        # Analyze the upcoming track section
        curvature, target_point = analyze_upcoming_track(upcoming_points)

        # ── Speed feedforward ──
        target_speed = self.calculate_target_speed(curvature)

        # ── Collision avoidance override ──
        is_ahead, player_distance = self._is_player_ahead_of_vehicle(vehicle, own_vehicle)
        emergency_brake = False
        if is_ahead:
            if player_distance < self.CA_EMERGENCY_DISTANCE:
                emergency_brake = True
            else:
                ca_speed_limit = self._calculate_following_speed(player_distance)
                target_speed = min(target_speed, ca_speed_limit)

        current_speed = vehicle.data.speed if hasattr(vehicle.data, 'speed') else 0.0

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

        # ── Apply smoothing ──
        throttle, brake, steering = self._smooth(vehicle_id, raw_throttle, raw_brake, raw_steer)

        # ── Send control commands ──
        self.ai_controller.control_ai(vehicle_id, AIControlState(
            throttle=int(throttle),
            brake=int(brake),
            steer=int(steering),
        ))