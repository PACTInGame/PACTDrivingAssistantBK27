# ui/ui_manager.py
from typing import Dict, List

from core.event_bus import EventBus
from core.settings_manager import SettingsManager
from lfs.message_sender import MessageSender


class UIManager:
    """Verwaltet alle UI-Elemente und Menüs"""

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

    def _handle_button_click(self, btc_packet):
        """Verarbeitet Button-Klicks"""
        click_id = btc_packet.ClickID
        self.event_bus.emit('ui_action', {
            'action': 'button_click',
            'button_id': click_id
        })

    def show_hud(self):
        """Zeigt das Head-Up Display"""
        # HUD-Elemente erstellen
        self.message_sender.create_button(1, 10, 10, 200, 30, "Speed: 0 km/h")
        self.message_sender.create_button(2, 10, 45, 200, 30, "Gear: N")
        # Weitere HUD-Elemente...

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
            self.message_sender.create_button(7, 10, 80, 300, 40, text, color)
        else:
            self.message_sender.remove_button(7)

    def _update_blind_spot_display(self, data):
        """Aktualisiert Toter-Winkel-Anzeige"""
        if data['left']:
            self.message_sender.create_button(44, 5, 150, 50, 30, "BSW L")
        else:
            self.message_sender.remove_button(44)

        if data['right']:
            self.message_sender.create_button(45, 345, 150, 50, 30, "BSW R")
        else:
            self.message_sender.remove_button(45)