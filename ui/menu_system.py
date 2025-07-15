from typing import List

import pyinsim
from core.settings_manager import SettingsManager
from misc.language import LanguageManager
from ui.ui_manager import UIManager


class MenuSystem:
    """Verwaltet das Menüsystem"""

    def __init__(self, ui_manager: UIManager, settings: SettingsManager):
        self.ui_manager = ui_manager
        self.settings = settings
        self.current_menu = 'none'
        self.menu_stack: List[str] = []
        self.on_track = False
        self.set_language = settings.get('language')
        self.translator = LanguageManager()
        self.ui_manager.event_bus.subscribe('state_data', self._state_change)
        self.ui_manager.event_bus.subscribe('button_clicked', self._handle_ui_action)

    def _state_change(self, data):
        new_on_track = data['on_track']
        if new_on_track != self.on_track:
            if new_on_track:
                self.create_open_menu_button()
            else:
                self.close_menu()

    def open_main_menu(self):
        """Öffnet das Hauptmenü"""
        self.current_menu = 'main'
        self._clear_menu_buttons()

        # Hauptmenü-Buttons
        buttons = [
            (21, 0, 70, 20, 10, self.translator.get("Main Menu", self.settings.get('language')),
             pyinsim.ISB_LIGHT),
            (22, 0, 80, 20, 10, self.translator.get("Driving", self.settings.get('language')),
             pyinsim.ISB_DARK | pyinsim.ISB_CLICK),
            (23, 0, 90, 20, 10, self.translator.get("Parking", self.settings.get('language')),
             pyinsim.ISB_DARK | pyinsim.ISB_CLICK),
            (24, 0, 100, 20, 10, self.translator.get("Language", self.settings.get('language')),
             pyinsim.ISB_DARK | pyinsim.ISB_CLICK),
            (25, 0, 110, 20, 10, self.translator.get("System", self.settings.get('language')),
             pyinsim.ISB_DARK | pyinsim.ISB_CLICK),
            (40, 0, 120, 20, 10, "^1" + self.translator.get("Close", self.settings.get('language')),
             pyinsim.ISB_DARK | pyinsim.ISB_CLICK),
        ]

        for button_id, x, y, w, h, text, style in buttons:
            self.ui_manager.message_sender.create_button(button_id, x, y, w, h, text, style)

    def open_driving_menu(self):
        """Öffnet das Fahrer-Menü"""
        self.current_menu = 'driving'
        self._clear_menu_buttons()

        # Fahrer-Menü mit aktuellen Einstellungen
        fcw_text = "FCW: ON" if self.settings.get('forward_collision_warning') else "FCW: OFF"
        bsw_text = "BSW: ON" if self.settings.get('blind_spot_warning') else "BSW: OFF"

        buttons = [
            (21, 50, 50, 150, 30, "Driving Settings"),
            (22, 50, 85, 150, 30, fcw_text),
            (23, 50, 120, 150, 30, bsw_text),
            (40, 50, 300, 100, 30, "Back"),
        ]

        for button_id, x, y, w, h, text in buttons:
            self.ui_manager.message_sender.create_button(button_id, x, y, w, h, text)

    def _clear_menu_buttons(self):
        """Löscht alle Menü-Buttons"""
        for button_id in range(20, 41):
            self.ui_manager.message_sender.remove_button(button_id)

    def create_open_menu_button(self):
        self.ui_manager.message_sender.create_button(20, 0, 100, 20, 10,
                                                     self.translator.get("Main Menu", self.settings.get('language')),
                                                     pyinsim.ISB_DARK | pyinsim.ISB_CLICK),

    def _handle_ui_action(self, data):
        """Verarbeitet UI-Aktionen"""
        btc = data
        button_id = btc.ClickID

        # Menü-Buttons
        if 20 <= button_id <= 40:
            self._handle_menu_click(button_id)

    def _handle_menu_click(self, button_id: int):
        """Verarbeitet Menü-Klicks"""
        if self.current_menu == 'none':
            if button_id == 20:
                self.open_main_menu()

        elif self.current_menu == 'main':
            if button_id == 22:
                self.open_driving_menu()
            elif button_id == 40:
                self.close_menu()

        elif self.current_menu == 'driving':
            if button_id == 22:  # Toggle FCW
                current = self.settings.get('forward_collision_warning')
                self.settings.set('forward_collision_warning', not current)
                self.open_driving_menu()  # Refresh menu
            elif button_id == 23:  # Toggle BSW
                current = self.settings.get('blind_spot_warning')
                self.settings.set('blind_spot_warning', not current)
                self.open_driving_menu()  # Refresh menu
            elif button_id == 40:
                self.open_main_menu()

    def close_menu(self):
        """Schließt alle Menüs"""
        self.current_menu = 'none'
        self._clear_menu_buttons()
        self.create_open_menu_button()
