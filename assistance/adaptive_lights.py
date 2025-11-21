import math
import time
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


class LightAssists(AssistanceSystem):
    """Adaptive Lichtfunktionen"""

    def __init__(self, event_bus: EventBus, settings: SettingsManager):
        super().__init__("light_assist", event_bus, settings)
        self.indi_on = False
        self.high_beam_assist = True
        self.adaptive_brake_light_timer = time.perf_counter()

    def process(self, own_vehicle: OwnVehicle, vehicles: Dict[int, Vehicle]) -> Dict[str, Any]:
        """Verarbeitet die Adaptive-Licht-Logik"""
        if not self.is_enabled():
            return {'adaptive_lights': False}
        # --- Adaptive Bremslichter ---
        adaptive_lights = False
        reverse = (own_vehicle.data.heading - own_vehicle.data.direction) > 10000 or (own_vehicle.data.heading - own_vehicle.data.direction) < -10000
        if (time.perf_counter() - self.adaptive_brake_light_timer) > 0.15:
            self.adaptive_brake_light_timer = time.perf_counter()
            if (own_vehicle.data.acceleration < -8 or (own_vehicle.brake > 0.85 and own_vehicle.data.speed > 10)) and not reverse:
                adaptive_lights = True

                if self.indi_on:
                    self.indi_on = False
                    self.event_bus.emit("send_light_command", {"light": 8, "on": False})
                else:
                    self.indi_on = True
                    self.event_bus.emit("send_light_command", {"light": 8, "on": True})

            elif self.indi_on:
                self.indi_on = False
                self.event_bus.emit("send_light_command", {"light": 8, "on": False})
        else:
            adaptive_lights = True
        # --- Lichtautomatik ---
        if self.high_beam_assist:
            if not own_vehicle.low_beam_light and not own_vehicle.full_beam_light:
                self.event_bus.emit("send_light_command", {"light": 1, "on": True})
            any_vehicle_visible = False
            for vehicle in vehicles.values():
                if self._is_vehicle_visible(vehicle):
                    any_vehicle_visible = True
                    break
            # TODO seems to not work anymore!
            if any_vehicle_visible:
                self.event_bus.emit("send_light_command", {"light": 1, "on": True})
            else:
                self.event_bus.emit("send_light_command", {"light": 2, "on": True})

        return {
            'adaptive_lights': adaptive_lights
        }

    def _is_vehicle_visible(self, other_vehicle: Vehicle) -> bool:
        """Prüft ob Fahrzeug sichtbar ist - keine Hindernisse werden berücksichtigt"""
        is_vehicle_ahead = other_vehicle.data.distance_to_player < 250 and other_vehicle.data.speed > 1
        player_in_cone = abs(other_vehicle.data.angle_to_player) < 15 or abs(other_vehicle.data.angle_to_player) > 345
        return is_vehicle_ahead and player_in_cone
