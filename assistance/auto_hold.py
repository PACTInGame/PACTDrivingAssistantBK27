import math
from typing import Dict, Any

import pyautogui
import shapely
from shapely.geometry.point import Point
from shapely.geometry.polygon import Polygon

from assistance.base_system import AssistanceSystem
from core.event_bus import EventBus
from core.settings_manager import SettingsManager
from misc.helpers import calc_polygon_points, point_in_rectangle
from vehicles.own_vehicle import OwnVehicle
from vehicles.vehicle import Vehicle


class AutoHold(AssistanceSystem):
    """Automatic Parking Brake"""

    def __init__(self, event_bus: EventBus, settings: SettingsManager):
        super().__init__("auto_hold", event_bus, settings)
        self.current_warning_level = 0
        self.own_rectangle = None

    def process(self, own_vehicle: OwnVehicle, vehicles: Dict[int, Vehicle]) -> Dict[str, Any]:
        """Verarbeitet die Auto-Hold-Logik"""
        if not self.is_enabled():
            return {'auto_hold_active': False}
        auto_hold = False
        if own_vehicle.data.speed < 0.05 and own_vehicle.brake > 0.05:
            auto_hold = True
            if not own_vehicle.handbrake_light:
                user_handbrake_key = self.settings.get('user_handbrake_key')
                # Press the handbrake key to activate auto-hold using direct input, right here
                pyautogui.keyDown(user_handbrake_key)
                pyautogui.keyUp(user_handbrake_key)
                self.event_bus.emit("notification", {'notification': 'Auto Hold'})

        return {
            'auto_hold_active': auto_hold
        }

    def _is_vehicle_ahead(self, other_vehicle: Vehicle) -> bool:
        """Prüft ob Fahrzeug vor uns ist"""
        # Vereinfachte Implementierung - kann erweitert werden

        is_vehicle_ahead = point_in_rectangle(other_vehicle.data.x, other_vehicle.data.y, self.own_rectangle)

        return is_vehicle_ahead  # Innerhalb 45° voraus

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
        if needed_braking > own_vehicle.data.acceleration:
            return float('inf')
        # Return the absolute value since we want braking force (negative acceleration)
        return abs(needed_braking)
