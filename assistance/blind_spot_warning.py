import math
import time
from typing import Dict, Any

from assistance.base_system import AssistanceSystem
from core.event_bus import EventBus
from core.settings_manager import SettingsManager
from misc.helpers import calc_polygon_points
from vehicles.own_vehicle import OwnVehicle
from vehicles.vehicle import Vehicle
from shapely import Polygon


def _is_within_threshold(own_heading, other_heading):
        # Checks if the heading of another car is within a threshold
        lower_bound = (other_heading - 5000) % 65536
        upper_bound = (other_heading + 5000) % 65536

        if lower_bound > upper_bound:
            return own_heading > lower_bound or own_heading < upper_bound
        return lower_bound < own_heading < upper_bound


def _polygon_intersect(p1, p2):
    return p1.intersects(p2)


def _normalize_angle(angle):
        # Normalizes the angle value
        if angle < 0:
            angle *= -1
        return angle





def _create_rectangles_for_blindspot_warning(cars):
        rectangles = []
        factor = 2.3 * 65536
        heading_offset = 16384
        heading_divisor = 182.05
        angle_offsets = [22, 158, 202, 338]
        for car in cars.values():
            x, y, heading = car.data.x, car.data.y, car.data.heading
            angle_of_car = abs((heading - heading_offset) / heading_divisor)
            polygon_points = [calc_polygon_points(x, y, factor, angle_of_car + offset) for offset in angle_offsets]
            rectangles.append((car.data.speed, car.data.distance_to_player, Polygon(polygon_points), heading))

        return rectangles


def _create_blindspot_rectangle(vehicle, angle_of_car, angles):
        # Creates blind spot rectangle using provided angles
        multipliers = [4, 85, 85, 1]
        points = [calc_polygon_points(vehicle.data.x, vehicle.data.y, multiplier * 65536, angle_of_car + angle)
                  for multiplier, angle in zip(multipliers, angles)]
        return Polygon(points)


class BlindSpotWarning(AssistanceSystem):
    """Toter-Winkel-Warner"""

    def __init__(self, event_bus: EventBus, settings: SettingsManager):
        super().__init__("blind_spot_warning", event_bus, settings)
        self.detection_distance = 70.0
        self.left_warning = False
        self.right_warning = False
        self.last_exec = time.perf_counter()

    def process(self, own_vehicle: OwnVehicle, vehicles: Dict[int, Vehicle]) -> Dict[str, Any]:
        """Prüft auf Fahrzeuge im toten Winkel"""
        blindspot_r, blindspot_l = False, False
        angle_of_car = _normalize_angle((own_vehicle.data.heading + 16384) / 182.05)
        # Rectangles for right and left blind spots
        rectangle_right = _create_blindspot_rectangle(own_vehicle, angle_of_car, [270, 182, 183, 270])
        rectangle_left = _create_blindspot_rectangle(own_vehicle, angle_of_car, [90, 178, 177, 90])
        rectangles_others = _create_rectangles_for_blindspot_warning(vehicles)

        for rectangle in rectangles_others:

            if _is_within_threshold(own_vehicle.data.heading, rectangle[3]) and rectangle[1] < (
                    rectangle[0] - own_vehicle.data.speed + (5 if own_vehicle.data.speed > 15 else 0)) * 1.2:
                if _polygon_intersect(rectangle[2], rectangle_left):
                    blindspot_l = True
                if _polygon_intersect(rectangle[2], rectangle_right):
                    blindspot_r = True

        if blindspot_l != self.left_warning or blindspot_r != self.right_warning:
            self.left_warning = blindspot_l
            self.right_warning = blindspot_r

            self.event_bus.emit('blind_spot_warning_changed', {
                'left': self.left_warning,
                'right': self.right_warning
            })
        return {
            'left_warning': self.left_warning,
            'right_warning': self.right_warning
        }




