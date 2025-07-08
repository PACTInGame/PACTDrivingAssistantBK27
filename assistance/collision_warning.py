import math
from typing import Dict, Any

import shapely
from shapely.geometry.point import Point
from shapely.geometry.polygon import Polygon

from assistance.base_system import AssistanceSystem
from core.event_bus import EventBus
from core.settings_manager import SettingsManager
from misc.helpers import calc_polygon_points
from vehicles.own_vehicle import OwnVehicle
from vehicles.vehicle import Vehicle


class ForwardCollisionWarning(AssistanceSystem):
    """Kollisionswarnung für Fahrzeuge voraus"""

    def __init__(self, event_bus: EventBus, settings: SettingsManager):
        super().__init__("ForwardCollisionWarning", event_bus, settings)
        self.warning_distance = 50.0  # Meter
        self.current_warning_level = 0

    def process(self, own_vehicle: OwnVehicle, vehicles: Dict[int, Vehicle]) -> Dict[str, Any]:
        """Prüft auf Kollisionsgefahr voraus"""
        if not self.is_enabled():
            return {'warning_level': 0}

        warning_level = 0
        closest_vehicle = None
        min_distance = float('inf')

        for vehicle in vehicles.values():
            if self._is_vehicle_ahead(own_vehicle, vehicle):
                distance = self._calculate_collision_time(own_vehicle, vehicle)
                if distance < min_distance:
                    min_distance = distance
                    closest_vehicle = vehicle

        if min_distance < self.warning_distance:
            if min_distance < 20:
                warning_level = 3  # Kritisch
            elif min_distance < 35:
                warning_level = 2  # Warnung
            else:
                warning_level = 1  # Aufmerksamkeit

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

    def _is_vehicle_ahead(self, own_vehicle: OwnVehicle, other_vehicle: Vehicle) -> bool:
        """Prüft ob Fahrzeug vor uns ist"""
        # TODO make this calculate less often
        # Vereinfachte Implementierung - kann erweitert werden
        angle_of_car = abs(own_vehicle.data.heading + 16384 / 182.05)
        ang1, ang2, ang3, ang4 = angle_of_car + 1, angle_of_car - 20, angle_of_car + 20, angle_of_car - 1

        (x1, y1) = calc_polygon_points(own_vehicle.data.x, own_vehicle.data.y, 85 * 65536, ang1)
        (x2, y2) = calc_polygon_points(own_vehicle.data.x, own_vehicle.data.y, 2.0 * 65536, ang2)
        (x3, y3) = calc_polygon_points(own_vehicle.data.x, own_vehicle.data.y, 2.0 * 65536, ang3)
        (x4, y4) = calc_polygon_points(own_vehicle.data.x, own_vehicle.data.y, 85 * 65536, ang4)

        own_rectangle = Polygon([(x1, y1), (x2, y2), (x3, y3), (x4, y4)])
        print(own_rectangle)
        print(own_vehicle.data.x, own_vehicle.data.y)
        print(other_vehicle.data.x, other_vehicle.data.y)
        # TODO contains doesnt work somehow
        is_vehicle_ahead = own_rectangle.contains(Point(other_vehicle.data.x, other_vehicle.data.y))
        print(is_vehicle_ahead)


        return is_vehicle_ahead  # Innerhalb 45° voraus

    def _calculate_collision_time(self, own_vehicle: OwnVehicle, other_vehicle: Vehicle) -> float:
        """Berechnet Zeit bis zur Kollision"""
        relative_speed = own_vehicle.data.speed - other_vehicle.data.speed
        if relative_speed <= 0:
            return float('inf')

        distance = other_vehicle.data.distance_to_player
        return distance / (relative_speed / 3.6)  # Convert km/h to m/s
