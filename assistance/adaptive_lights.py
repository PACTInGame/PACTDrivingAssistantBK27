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
        self.is_siren_enabled_role = False
        self.event_bus.subscribe("player_name_changed", self._on_player_name_changed)
        self.event_bus.subscribe("button_clicked", self._handle_button_click)
        self.event_bus.subscribe("state_data", self._handle_state_change)
        self.on_track = False
        self.player_name = "Unknown"
        self.strobe_active = False
        self.siren_active = False
        self.strobe_pattern = 0
        self.strobe_actions = {
            0: {"light": 2, "on": True},
            1: {"light": 5, "on": True},
            2: {"light": 8, "on": True},
            3: {"light": 1, "on": True},
            4: {"light": 5, "on": False},
            5: {"light": 3, "on": True},
            6: {"light": 2, "on": True},
            7: {"light": 8, "on": False},
            8: {"light": 4, "on": True},
            9: {"light": 1, "on": True},
            10: {"light": 3, "on": False},
            11: {"light": 4, "on": False}


        }

    def _handle_state_change(self, data):
        new_on_track = data['on_track']
        print(f"On Track changed: {new_on_track}")
        if new_on_track != self.on_track:
            if new_on_track:
                pn = self.player_name.lower()
                if '[cop]' in pn or '[tow]' in pn or '[res]' in pn:
                    self.is_siren_enabled_role = True
                    self.event_bus.emit("show_siren_ui", {"ui": True})
                else:
                    self.is_siren_enabled_role = False
                    self.event_bus.emit("show_siren_ui", {"ui": False})
                    self.disable_siren()
            else:
                self.is_siren_enabled_role = False
                self.siren_active = False
                self.strobe_active = False
                self.disable_siren()
        self.on_track = new_on_track


    def _handle_button_click(self, btc):
        button_id = btc.ClickID
        print(f"Button {button_id} clicked")
        if button_id == 62:
            self.siren_active = not self.siren_active
            self.event_bus.emit("siren_state_changed", {"siren_active": self.siren_active})
        elif button_id == 63:
            self.strobe_active = not self.strobe_active
            if not self.strobe_active:
                self.disable_siren()

    def _on_player_name_changed(self, data):

        player_name = data.get('player_name', '')
        player_name = str(player_name)
        self.player_name = player_name
        if '[cop]' in player_name.lower() or '[tow]' in player_name.lower() or '[res]' in player_name.lower():
            self.is_siren_enabled_role = True
            self.event_bus.emit("show_siren_ui", {"ui": True})
        else:
            self.is_siren_enabled_role = False
            self.event_bus.emit("show_siren_ui", {"ui": False})
            self.disable_siren()


    def disable_siren(self):
        self.event_bus.emit("send_light_command", {"light": 1, "on": True})
        self.event_bus.emit("send_light_command", {"light": 3, "on": False})
        self.event_bus.emit("send_light_command", {"light": 4, "on": False})
        self.event_bus.emit("send_light_command", {"light": 5, "on": False})
        self.event_bus.emit("send_light_command", {"light": 8, "on": False})


    def process(self, own_vehicle: OwnVehicle, vehicles: Dict[int, Vehicle]) -> Dict[str, Any]:
        """Verarbeitet die Adaptive-Licht-Logik"""
        if not self.is_enabled():
            return {'adaptive_lights': False}
        adaptive_lights = False
        # --- Adaptive Bremslichter ---
        if not self.strobe_active:
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
        if self.high_beam_assist and not self.strobe_active:
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
        # --- Sirenen-Management ---
        if self.is_siren_enabled_role:
            if self.strobe_active:
                self.strobe_pattern += 1
                if self.strobe_pattern > 11 :
                    self.strobe_pattern = 0
                light = self.strobe_actions[self.strobe_pattern]
                self.event_bus.emit("send_light_command", light)


        return {
            'adaptive_lights': adaptive_lights
        }

    def _is_vehicle_visible(self, other_vehicle: Vehicle) -> bool:
        """Prüft ob Fahrzeug sichtbar ist - keine Hindernisse werden berücksichtigt"""
        is_vehicle_ahead = other_vehicle.data.distance_to_player < 250 and other_vehicle.data.speed > 1
        player_in_cone = abs(other_vehicle.data.angle_to_player) < 15 or abs(other_vehicle.data.angle_to_player) > 345
        return is_vehicle_ahead and player_in_cone
