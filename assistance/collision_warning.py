import math
from typing import Dict, Any

import shapely
from shapely.geometry.point import Point
from shapely.geometry.polygon import Polygon

from assistance.base_system import AssistanceSystem
from core.event_bus import EventBus
from core.settings_manager import SettingsManager
from misc.helpers import calc_polygon_points, point_in_rectangle
from vehicles.own_vehicle import OwnVehicle
from vehicles.vehicle import Vehicle


class ForwardCollisionWarning(AssistanceSystem):
    """Kollisionswarnung für Fahrzeuge voraus"""

    def __init__(self, event_bus: EventBus, settings: SettingsManager):
        super().__init__("ForwardCollisionWarning", event_bus, settings)
        self.current_warning_level = 0
        self.own_rectangle = None

    def process(self, own_vehicle: OwnVehicle, vehicles: Dict[int, Vehicle]) -> Dict[str, Any]:
        """Prüft auf Kollisionsgefahr voraus"""
        if not self.is_enabled():
            return {'warning_level': 0}

        warning_level = 0
        closest_vehicle = None
        min_distance = float('inf')

        angle_of_car = abs((own_vehicle.data.heading + 16384) / 182.05)
        ang1, ang2, ang3, ang4 = angle_of_car + 1, angle_of_car - 20, angle_of_car + 20, angle_of_car - 1
        (x1, y1) = calc_polygon_points(own_vehicle.data.x, own_vehicle.data.y, 85 * 65536, ang1)
        (x2, y2) = calc_polygon_points(own_vehicle.data.x, own_vehicle.data.y, 3.0 * 65536, ang2)
        (x3, y3) = calc_polygon_points(own_vehicle.data.x, own_vehicle.data.y, 3.0 * 65536, ang3)
        (x4, y4) = calc_polygon_points(own_vehicle.data.x, own_vehicle.data.y, 85 * 65536, ang4)
        self.own_rectangle = [(x1, y1), (x2, y2), (x3, y3), (x4, y4)]

        for vehicle in vehicles.values():
            if self._is_vehicle_ahead(vehicle):
                distance = vehicle.data.distance_to_player
                print(distance)




        if warning_level != self.current_warning_level:
            self.current_warning_level = warning_level
            self.event_bus.emit('collision_warning_changed', {
                'level': warning_level,
                'distance': min_distance,
                'vehicle': closest_vehicle
            })

        return {
            'warning_level': warning_level,
            'distance': min_distance,
            'target_vehicle': closest_vehicle
        }

    def _is_vehicle_ahead(self, other_vehicle: Vehicle) -> bool:
        """Prüft ob Fahrzeug vor uns ist"""
        # Vereinfachte Implementierung - kann erweitert werden

        is_vehicle_ahead = point_in_rectangle(other_vehicle.data.x, other_vehicle.data.y, self.own_rectangle)

        return is_vehicle_ahead  # Innerhalb 45° voraus

    def _calculate_needed_braking(self, own_vehicle: OwnVehicle, other_vehicle: Vehicle) -> float:
        """Berechnet Zeit bis zur Kollision"""
        relative_speed = own_vehicle.data.speed - other_vehicle.data.speed
        if relative_speed <= 0:
            return float('inf')

        distance = other_vehicle.data.distance_to_player
        return distance / (relative_speed / 3.6)  # Convert km/h to m/s
