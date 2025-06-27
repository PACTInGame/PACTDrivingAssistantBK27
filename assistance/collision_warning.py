import math
from typing import Dict, Any

from assistance.base_system import AssistanceSystem
from core.event_bus import EventBus
from core.settings_manager import SettingsManager
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
        # Vereinfachte Implementierung - kann erweitert werden
        dx = other_vehicle.data.x - own_vehicle.data.x
        dy = other_vehicle.data.y - own_vehicle.data.y

        # Berechne relativen Winkel
        angle_to_vehicle = math.atan2(dy, dx)
        heading_diff = abs(angle_to_vehicle - own_vehicle.data.heading)

        return heading_diff < math.pi / 4  # Innerhalb 45° voraus

    def _calculate_collision_time(self, own_vehicle: OwnVehicle, other_vehicle: Vehicle) -> float:
        """Berechnet Zeit bis zur Kollision"""
        relative_speed = own_vehicle.data.speed - other_vehicle.data.speed
        if relative_speed <= 0:
            return float('inf')

        distance = other_vehicle.data.distance_to_player
        return distance / (relative_speed / 3.6)  # Convert km/h to m/s
