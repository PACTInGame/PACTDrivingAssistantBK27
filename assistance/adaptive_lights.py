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
        self.adaptive_brake_light_timer = time.perf_counter()

    def process(self, own_vehicle: OwnVehicle, vehicles: Dict[int, Vehicle]) -> Dict[str, Any]:
        """Verarbeitet die Adaptive-Licht-Logik"""
        if not self.is_enabled():
            return {'adaptive_lights': False}
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

        return {
            'adaptive_lights': adaptive_lights
        }


