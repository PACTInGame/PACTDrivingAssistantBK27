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
        super().__init__("forward_collision_warning", event_bus, settings)
        self.current_warning_level = 0
        self.own_rectangle = None

    def process(self, own_vehicle: OwnVehicle, vehicles: Dict[int, Vehicle]) -> Dict[str, Any]:
        """Prüft auf Kollisionsgefahr voraus"""
        warning_level = 0
        reversing = (own_vehicle.data.heading - own_vehicle.data.direction) > 10000 or (own_vehicle.data.heading - own_vehicle.data.direction) < -10000
        if not self.is_enabled() or own_vehicle.data.speed < 9 or reversing:
            if warning_level != self.current_warning_level:
                self.event_bus.emit('needed_deceleration_update', {
                    'deceleration': 0,
                })
                self.current_warning_level = warning_level
                self.event_bus.emit('collision_warning_changed', {
                    'level': warning_level,
                })
            return {'level': 0}


        angle_of_car = abs((own_vehicle.data.heading + 16384) / 182.05)
        ang1, ang2, ang3, ang4 = angle_of_car + 1, angle_of_car - 20, angle_of_car + 20, angle_of_car - 1
        (x1, y1) = calc_polygon_points(own_vehicle.data.x, own_vehicle.data.y, 85 * 65536, ang1)
        (x2, y2) = calc_polygon_points(own_vehicle.data.x, own_vehicle.data.y, 3.0 * 65536, ang2)
        (x3, y3) = calc_polygon_points(own_vehicle.data.x, own_vehicle.data.y, 3.0 * 65536, ang3)
        (x4, y4) = calc_polygon_points(own_vehicle.data.x, own_vehicle.data.y, 85 * 65536, ang4)
        self.own_rectangle = [(x1, y1), (x2, y2), (x3, y3), (x4, y4)]
        max_needed_deceleration = 0
        for vehicle in vehicles.values():
            if self._is_vehicle_ahead(vehicle):
                needed_braking = self._calculate_needed_braking(own_vehicle, vehicle)  # Nötiges Bremsen in m/s^2
                max_needed_deceleration = max(max_needed_deceleration, needed_braking)
                if needed_braking != float('inf'):
                    if needed_braking > 6:
                        warn = 3
                    elif needed_braking > 5 or needed_braking > 0 and self.current_warning_level > 1:
                        warn = 2
                    elif -needed_braking < own_vehicle.data.acceleration and needed_braking > 2:
                        warn = 1
                    else:
                        warn = 0
                    if warn > warning_level:
                        warning_level = warn
        if warning_level > 1:
            self.event_bus.emit('needed_deceleration_update', {
                'deceleration': max_needed_deceleration,
            })
        else:
            self.event_bus.emit('needed_deceleration_update', {
                'deceleration': 0,
            })
        if warning_level != self.current_warning_level:
            self.current_warning_level = warning_level
            self.event_bus.emit('collision_warning_changed', {
                'level': warning_level,
            })

        return {
            'level': warning_level,
        }

    def _is_vehicle_ahead(self, other_vehicle: Vehicle) -> bool:
        """Prüft ob Fahrzeug vor uns ist"""

        is_vehicle_ahead = point_in_rectangle(other_vehicle.data.x, other_vehicle.data.y, self.own_rectangle)

        return is_vehicle_ahead

    def _calculate_needed_braking(self, own_vehicle: OwnVehicle, other_vehicle: Vehicle) -> float:
        """Calculates the needed braking to avoid collision in m/s^2"""
        relative_speed = (own_vehicle.data.speed - other_vehicle.data.speed) * 0.277778  # Convert from km/h to m/s
        vehicle_acceleration = other_vehicle.data.acceleration # Relevant, because it can drastically shorten or lengthen the distance
        distance_to_vehicle = other_vehicle.data.distance_to_player - 5  # -4 because of coordinates distance to front bumper
        if relative_speed <= 0:
            return float('inf')

        # Using kinematic equation: v² = u² + 2as
        # We want final relative speed to be 0, so: 0 = relative_speed² + 2 * relative_acceleration * distance
        # Solving for acceleration: a = -relative_speed² / (2 * distance)

        # However, we need to account for the fact that the other vehicle is also accelerating
        # The relative acceleration we need is: needed_relative_acceleration = -relative_speed² / (2 * distance)
        needed_relative_acceleration = -(relative_speed ** 2) / (2 * distance_to_vehicle)

        # To get the absolute braking needed for our vehicle:
        # needed_relative_acceleration = our_new_acceleration - vehicle_acceleration
        # So: our_new_acceleration = needed_relative_acceleration + vehicle_acceleration
        needed_braking = needed_relative_acceleration + vehicle_acceleration
        # Return the absolute value since we want braking force (negative acceleration)
        return abs(needed_braking)
