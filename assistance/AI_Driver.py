import math
from typing import Dict, Any, List, Tuple
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

    def __init__(self, event_bus: EventBus, settings: SettingsManager):
        super().__init__("AIDriver", event_bus, settings)
        self.routes = None
        self.ai_controller = None
        self.event_bus.subscribe("AI_Controller_initialized", self._on_ai_controller_initialized)

        # Feedforward tuning parameters
        self.MAX_STEERING_ANGLE = 45.0   # ±degrees that map to full steering
        self.SPEED_GAIN = 3.0            # Proportional gain for speed error → throttle/brake

        # Speed parameters
        self.BASE_SPEED = 70.0           # Base speed in km/h on straight sections
        self.MIN_SPEED = 10.0            # Minimum speed on tight curves
        self.CURVATURE_THRESHOLD = 0.002 # Curvature above which to slow down

    def _on_ai_controller_initialized(self, ai_controller):
        self.ai_controller = ai_controller

    def calculate_target_speed(self, curvature: float) -> float:
        """
        Calculate target speed based on upcoming curvature.

        Args:
            curvature: Average curvature of upcoming section

        Returns:
            Target speed
        """
        if curvature < self.CURVATURE_THRESHOLD:
            return self.BASE_SPEED
        else:
            speed_reduction = (curvature - self.CURVATURE_THRESHOLD) * 1500.0
            return max(self.MIN_SPEED, self.BASE_SPEED - speed_reduction)

    def monitor_ai(self, aii):
        if aii.RPM > 5000:
            self.ai_controller.control_ai(aii.PLID, AIControlState(
                shift_up=True,
            ))
        if aii.RPM < 2000 and aii.Gear > 2:
            self.ai_controller.control_ai(aii.PLID, AIControlState(
                shift_down=True,
            ))
        if aii.Gear < 1:
            self.ai_controller.control_ai(aii.PLID, AIControlState(
                shift_up=True,
            ))
        if aii.RPM < 300:
            self.ai_controller.control_ai(aii.PLID, AIControlState(
                ignition=True,
            ))
        print(f"AI Info for Vehicle {aii.PLID}: RPM={aii.RPM}, Gear={aii.Gear}")

    def process(self, own_vehicle: OwnVehicle, vehicles: Dict[int, Vehicle]) -> Dict[str, Any]:
        """Verarbeitet die AI-Driver-Logik"""
        if self.routes is None:
            self.routes = load_routes_from_file("StreetMapCreator/track_data.json")
            self.routes = {road['road_id']: road for road in self.routes}
        print("AI Driver Processing...")

        # Initialize test vehicle with route 20
        for vehicle_id in vehicles.keys():
            if vehicle_id == list(vehicles.keys())[0]:
                if vehicles.get(vehicle_id).current_route is None:
                    vehicles.get(vehicle_id).current_route = 20
                    self.ai_controller.bind_ai_info_handler(list(vehicles.keys())[0], self.monitor_ai)
                    self.ai_controller.request_ai_info(list(vehicles.keys())[0], repeat_interval=100)

        debug_angle = calculate_angle(own_vehicle.data.x, own_vehicle.data.y, 290.375, -139.9375,
                                      own_vehicle.data.heading)
        print(f"Debug Angle to Point (290.375, -139.9375): {debug_angle:.2f} degrees")

        # Process each vehicle that has a route assigned
        for vehicle_id in vehicles.keys():
            vehicle = vehicles.get(vehicle_id)
            print(f"Processing Vehicle ID: {vehicle_id}, with {vehicle.data.player_id}")
            # TODO current route to vehicle
            if vehicle.current_route is not None:
                route_points = self.routes[vehicle.current_route]

                # Get vehicle position (convert from game units)
                vehicle_x = vehicle.data.x / 65536
                vehicle_y = vehicle.data.y / 65536
                vehicle_z = vehicle.data.z / 65536

                # Find closest point and get upcoming points (50m or min 5 points)
                closest_index = get_closest_index_on_route(
                    vehicle_x, vehicle_y, vehicle_z, route_points
                )
                upcoming_points = get_next_points_for_distance(
                    closest_index, route_points, min_distance=50.0, min_points=5
                )

                # Analyze the upcoming track section
                curvature, target_point = analyze_upcoming_track(upcoming_points)
                print(f"Analyzing {len(upcoming_points)} points for curvature calculation")

                # --- Speed feedforward ---
                target_speed = self.calculate_target_speed(curvature)
                current_speed = vehicle.data.speed if hasattr(vehicle.data, 'speed') else 0.0
                speed_error = target_speed - current_speed

                throttle, brake = calculate_feedforward_throttle_brake(speed_error, gain=self.SPEED_GAIN)

                print(f"Calculated Target Speed: {target_speed:.2f} km/h based on Curvature: {curvature:.4f}")

                # --- Steering feedforward ---
                print(f"Target Point: {target_point}")
                print(f"Vehicle Position: ({vehicle_x:.2f}, {vehicle_y:.2f})")
                print(f"Vehicle Heading: {vehicle.data.heading if hasattr(vehicle.data, 'heading') else 'N/A'}")

                target_angle = calculate_angle(
                    vehicle.data.x, vehicle.data.y,
                    target_point[0], target_point[1],
                    vehicle.data.heading
                )
                print(f"Target Angle: {target_angle:.2f} degrees")

                steering = calculate_feedforward_steering(
                    target_angle, max_steering_angle=self.MAX_STEERING_ANGLE
                )
                print(f"Calculated Steering: {steering:.2f}")

                # Send control commands to AI controller
                if self.ai_controller is not None:
                    self.ai_controller.control_ai(vehicle_id, AIControlState(
                        throttle=int(throttle),
                        brake=int(brake),
                        steer=int(steering),
                    ))

                    print(f"Vehicle {vehicle_id}: Curvature={curvature:.4f}, Target={target_point}")
                    print(
                        f"  Speed: {current_speed:.1f} → {target_speed:.1f} | Throttle: {throttle:.0f}% | Brake: {brake:.0f}%")
                    print(f"  Target Angle: {target_angle:.2f}° | Steer: {steering:.0f}")

        return {
            'ai_active': True
        }