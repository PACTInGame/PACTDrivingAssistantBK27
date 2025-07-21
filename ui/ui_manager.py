# ui/ui_manager.py
from typing import Dict, List

import pyinsim
from core.event_bus import EventBus
from core.settings_manager import SettingsManager
from lfs.message_sender import MessageSender
from misc.pdc_beep import PDCBeepController


class UIManager:
    """Verwaltet alle UI-Elemente und Menüs"""

    # Buttons:
    # 1-10: HUD-Anzeige
    # 11-12: Forward Collision Warning
    # 13-14: Blind Spot Warning
    # 20-40: Menü-Buttons
    # 41-60: PDC-Anzeige

    def __init__(self, event_bus: EventBus, message_sender: MessageSender, settings: SettingsManager):
        self.event_bus = event_bus
        self.message_sender = message_sender
        self.settings = settings
        self.active_elements: Dict[str, bool] = {}
        self.current_menu = None
        self.on_track = False
        self.pdc_data = None

        # Event-Handler
        self.event_bus.subscribe('collision_warning_changed', self._update_collision_warning_display)
        self.event_bus.subscribe('blind_spot_warning_changed', self._update_blind_spot_display)
        self.event_bus.subscribe('outgauge_data', self._get_hud_data)
        self.event_bus.subscribe('state_data', self._state_change)
        self.event_bus.subscribe("pdc_changed", self._update_pdc)

        self.speed = 0
        self.rpm = 0
        self.gear = 'N'
        self.redline = 0
        self.pdc_beeper = PDCBeepController()


    def _update_pdc(self, data):
        """Aktualisiert Park Distance Control (PDC) Anzeige"""
        self.pdc_data = data
        if self.pdc_data[0] == -1:
            self._remove_pdc_display()

    def _remove_pdc_display(self):
        for i in range(41, 61):
            self.message_sender.remove_button(i)

    def _show_pdc_display(self):
        top_left = (self.settings.get("hud_width") - 6, self.settings.get("hud_height") - 6)
        bottom_left = (self.settings.get("hud_width") - 6, self.settings.get("hud_height") + 6)
        self.message_sender.create_button(60 , top_left[0] , top_left[1] +6,
                                          3, 2, "^7PDC", pyinsim.ISB_DARK)
        # create buttons for each PDC sensor
        for i, distance in enumerate(self.pdc_data.values()):
            if i < 3:  # Front sensors (0, 1, 2)
                # Green button (furthest distance)
                if distance >= 1:
                    self.message_sender.create_button(41 + i, top_left[0] + i, top_left[1],
                                                      1, 2, "^2o", pyinsim.ISB_DARK)
                else:
                    self.message_sender.remove_button(41 + i)

                # Yellow button (medium distance)
                if distance >= 2:
                    self.message_sender.create_button(44 + i, top_left[0] + i, top_left[1] + 2,
                                                      1, 2, "^3o", pyinsim.ISB_DARK)
                else:
                    self.message_sender.remove_button(44 + i)

                # Red button (closest distance)
                if distance >= 3:
                    self.message_sender.create_button(47 + i, top_left[0] + i, top_left[1] + 4,
                                                      1, 2, "^1o", pyinsim.ISB_DARK)
                else:
                    self.message_sender.remove_button(47 + i)

            else:  # Rear sensors (3, 4, 5)
                x = i - 3  # Correct offset for rear sensors

                # Green button (furthest distance) - bottom position for rear
                if distance >= 1:
                    self.message_sender.create_button(51 + x, bottom_left[0] + x, bottom_left[1],
                                                      1, 2, "^2o", pyinsim.ISB_DARK)
                else:
                    self.message_sender.remove_button(51 + x)

                # Yellow button (medium distance)
                if distance >= 2:
                    self.message_sender.create_button(54 + x, bottom_left[0] + x, bottom_left[1] - 2,
                                                      1, 2, "^3o", pyinsim.ISB_DARK)
                else:
                    self.message_sender.remove_button(54 + x)

                # Red button (closest distance) - top position for rear
                if distance >= 3:
                    self.message_sender.create_button(57 + x, bottom_left[0] + x, bottom_left[1] - 4,
                                                      1, 2, "^1o", pyinsim.ISB_DARK)
                else:
                    self.message_sender.remove_button(57 + x)

        self.pdc_beeper.update_beep(self.pdc_data)


    def _state_change(self, data):
        self.on_track = data['on_track']

    def _get_hud_data(self, data):
        self.speed = round(data.Speed * 3.6)
        self.rpm = round(data.RPM / 1000, 1)
        self.redline = self.rpm if self.rpm > self.redline else self.redline
        self.gear = "R" if data.Gear == 0 else "N" if data.Gear == 1 else str(data.Gear - 1)

    def update_hud(self):
        """Aktualisiert das Head-Up Display"""
        if self.on_track:
            speed_text = f"{self.speed} km/h" if self.settings.get(
                "unit") == "metric" else f"{round(self.speed * 0.621371)} mph "
            self.message_sender.create_button(1, self.settings.get("hud_width"), self.settings.get("hud_height"), 13, 8,
                                              speed_text, pyinsim.ISB_DARK)
            rpm_text = f"{self.rpm} rpm" if self.rpm < self.redline - 1 else f"^1{self.rpm} rpm"
            self.message_sender.create_button(2, self.settings.get("hud_width") + 13, self.settings.get("hud_height"),
                                              13,
                                              8, rpm_text, pyinsim.ISB_DARK)
            self.message_sender.create_button(3, self.settings.get("hud_width") + 26, self.settings.get("hud_height"),
                                              3, 4,
                                              f"{self.gear}", pyinsim.ISB_DARK)
            if self.pdc_data and self.pdc_data[0] != -1:
                self._show_pdc_display()

        else:
            self.hide_hud()

    def hide_hud(self):
        """Versteckt das Head-Up Display"""
        for i in range(1, 11):  # HUD button IDs 1-10
            self.message_sender.remove_button(i)

    def _update_collision_warning_display(self, data):
        """Aktualisiert Kollisionswarn-Anzeige"""
        warning_level = data['level']
        if warning_level > 0:
            color = 0  # Rot für Warnung
            text = f"{data['level']}"
            self.message_sender.create_button(11, 10, 80, 100, 40, text, color)
        else:
            self.message_sender.remove_button(11)

    def _update_blind_spot_display(self, data):
        """Aktualisiert Toter-Winkel-Anzeige"""
        if data['left']:
            self.message_sender.create_button(13, 20, self.settings.get("hud_height"), 10, 10, "^3!", pyinsim.ISB_DARK)
        else:
            self.message_sender.remove_button(13)

        if data['right']:
            self.message_sender.create_button(14, 180, self.settings.get("hud_height"), 10, 10, "^3!", pyinsim.ISB_DARK)
        else:
            self.message_sender.remove_button(14)
