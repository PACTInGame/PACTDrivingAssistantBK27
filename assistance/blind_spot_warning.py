import math
from typing import Dict, Any

from assistance.base_system import AssistanceSystem
from core.event_bus import EventBus
from core.settings_manager import SettingsManager
from vehicles.own_vehicle import OwnVehicle
from vehicles.vehicle import Vehicle


class BlindSpotWarning(AssistanceSystem):
    """Toter-Winkel-Warner"""

    def __init__(self, event_bus: EventBus, settings: SettingsManager):
        super().__init__("BlindSpotWarning", event_bus, settings)
        self.detection_distance = 30.0
        self.left_warning = False
        self.right_warning = False

    def process(self, own_vehicle: OwnVehicle, vehicles: Dict[int, Vehicle]) -> Dict[str, Any]:
        """Prüft auf Fahrzeuge im toten Winkel"""
        if not self.is_enabled():
            return {'left_warning': False, 'right_warning': False}

        left_warning = False
        right_warning = False

        for vehicle in vehicles.values():
            if vehicle.data.distance_to_player > self.detection_distance:
                continue

            side = self._get_vehicle_side(own_vehicle, vehicle)
            if side == 'left':
                left_warning = True
            elif side == 'right':
                right_warning = True

        # Emit Events bei Änderungen
        if left_warning != self.left_warning or right_warning != self.right_warning:
            self.left_warning = left_warning
            self.right_warning = right_warning
            self.event_bus.emit('blind_spot_warning_changed', {
                'left': left_warning,
                'right': right_warning
            })

        return {
            'left_warning': left_warning,
            'right_warning': right_warning
        }

    def _get_vehicle_side(self, own_vehicle: OwnVehicle, other_vehicle: Vehicle) -> str:
        """Bestimmt auf welcher Seite sich das andere Fahrzeug befindet"""
        # Vereinfachte Implementierung
        dx = other_vehicle.data.x - own_vehicle.data.x
        dy = other_vehicle.data.y - own_vehicle.data.y

        # Rotiere Koordinaten basierend auf eigener Fahrzeugrichtung
        cos_h = math.cos(own_vehicle.data.heading)
        sin_h = math.sin(own_vehicle.data.heading)

        local_x = dx * cos_h + dy * sin_h
        local_y = -dx * sin_h + dy * cos_h

        if local_y > 0:
            return 'left'
        elif local_y < 0:
            return 'right'
        else:
            return 'center'