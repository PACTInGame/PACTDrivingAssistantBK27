# ui/ui_manager.py
from typing import Dict, List

import pyinsim
from core.event_bus import EventBus
from core.settings_manager import SettingsManager
from lfs.message_sender import MessageSender


class UIManager:
    """Verwaltet alle UI-Elemente und Menüs"""
    # Buttons:
    # 1-10: HUD-Anzeige
    # 11-12: Forward Collision Warning
    # 13-14: Blind Spot Warning

    def __init__(self, event_bus: EventBus, message_sender: MessageSender, settings: SettingsManager):
        self.event_bus = event_bus
        self.message_sender = message_sender
        self.settings = settings
        self.active_elements: Dict[str, bool] = {}
        self.current_menu = None

        # Event-Handler
        self.event_bus.subscribe('button_clicked', self._handle_button_click)
        self.event_bus.subscribe('collision_warning_changed', self._update_collision_warning_display)
        self.event_bus.subscribe('blind_spot_warning_changed', self._update_blind_spot_display)
        self.event_bus.subscribe('outgauge_data', self._get_hud_data)

        self.speed = 0
        self.rpm = 0
        self.gear = 'N'
        self.redline = 0

    def _get_hud_data(self, data):
        self.speed = round(data.Speed * 3.6)
        self.rpm = round(data.RPM/1000,1)
        self.redline = self.rpm if self.rpm > self.redline else self.redline
        self.gear = "R" if data.Gear == 0 else "N" if data.Gear == 1 else str(data.Gear - 1)


    def _handle_button_click(self, btc_packet):
        """Verarbeitet Button-Klicks"""
        click_id = btc_packet.ClickID
        self.event_bus.emit('ui_action', {
            'action': 'button_click',
            'button_id': click_id
        })

    def update_hud(self):
        """Aktualisiert das Head-Up Display"""
        speed_text = f"{self.speed} km/h" if self.settings.get("unit") == "metric" else f"{round(self.speed * 0.621371)} mph "
        self.message_sender.create_button(1, self.settings.get("hud_width"), self.settings.get("hud_height"), 13, 8, speed_text, pyinsim.ISB_DARK)
        rpm_text = f"{self.rpm} rpm" if self.rpm < self.redline - 1 else f"^1{self.rpm} rpm"
        self.message_sender.create_button(2, self.settings.get("hud_width") + 13, self.settings.get("hud_height"), 13, 8, rpm_text, pyinsim.ISB_DARK)
        self.message_sender.create_button(3, self.settings.get("hud_width") + 26, self.settings.get("hud_height"), 3, 4, f"{self.gear}", pyinsim.ISB_DARK)

    def hide_hud(self):
        """Versteckt das Head-Up Display"""
        for i in range(1, 11):  # HUD button IDs 1-10
            self.message_sender.remove_button(i)

    def _update_collision_warning_display(self, data):
        """Aktualisiert Kollisionswarn-Anzeige"""
        warning_level = data['level']
        if warning_level > 0:
            color = 0  # Rot für Warnung
            text = f"COLLISION WARNING! Distance: {data['distance']:.1f}m"
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