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
            if new_on_track and self.current_menu == 'none':
                self.create_open_menu_button()
            else:
                self.close_menu()

    def open_main_menu(self):
        """Öffnet das Hauptmenü"""
        self.current_menu = 'main'
        self._clear_menu_buttons()

        # Hauptmenü-Buttons
        buttons = [
            (21, 0, 70, 20, 10, self.translator.get("Main Menu", self.set_language),
             pyinsim.ISB_LIGHT),
            (22, 0, 80, 20, 10, self.translator.get("Driving", self.set_language),
             pyinsim.ISB_DARK | pyinsim.ISB_CLICK),
            (23, 0, 90, 20, 10, self.translator.get("Parking", self.set_language),
             pyinsim.ISB_DARK | pyinsim.ISB_CLICK),
            (24, 0, 100, 20, 10, (self.translator.get("Language", self.set_language) + f": {self.set_language}"),
             pyinsim.ISB_DARK | pyinsim.ISB_CLICK),
            (25, 0, 110, 20, 10, self.translator.get("System", self.set_language),
             pyinsim.ISB_DARK | pyinsim.ISB_CLICK),
            (40, 0, 120, 20, 10, "^1" + self.translator.get("Close", self.set_language),
             pyinsim.ISB_DARK | pyinsim.ISB_CLICK),
        ]

        for button_id, x, y, w, h, text, style in buttons:
            self.ui_manager.message_sender.create_button(button_id, x, y, w, h, text, style)

    def open_driving_menu(self):
        """Öffnet das Fahrer-Menü"""
        self.current_menu = 'driving'
        self._clear_menu_buttons()

        # Fahrer-Menü mit aktuellen Einstellungen
        fcw = "^2" if self.settings.get('forward_collision_warning') else "^1"
        bsw = "^2" if self.settings.get('blind_spot_warning') else "^1"
        ctw = "^2" if self.settings.get('cross_traffic_warning') else "^1"
        distance = self.settings.get('collision_warning_distance')
        distance = "^2Early" if distance == 0 else "^3Medium" if distance == 1 else "^1Late"
        aeb = self.settings.get('automatic_emergency_brake') # 0 = Off, 1 = Warn, 2 = Warn & Brake
        aeb_text = "^3Warn only" if aeb == 1 else "^2Warn & Brake" if aeb == 2 else "^1Off"
        buttons = [
            (21, 0, 70, 25, 10, self.translator.get("Driving Menu", self.set_language),
             pyinsim.ISB_LIGHT),
            (22, 0, 80, 25, 10, fcw + self.translator.get("Collision Warning", self.set_language),
             pyinsim.ISB_DARK | pyinsim.ISB_CLICK),
            (23, 0, 90, 25, 10, bsw + self.translator.get("Blind Spot Warn.", self.set_language),
             pyinsim.ISB_DARK | pyinsim.ISB_CLICK),
            (24, 0, 100, 25, 10, ctw + (self.translator.get("Cross Traffic Warn.", self.set_language)),
             pyinsim.ISB_DARK | pyinsim.ISB_CLICK),
            (25, 0, 110, 25, 10, (self.translator.get("Autom. Braking", self.set_language)),
             pyinsim.ISB_DARK | pyinsim.ISB_CLICK),
            (26, 25, 80, 15, 10, distance,
             pyinsim.ISB_DARK | pyinsim.ISB_CLICK),
            (27, 25, 110, 15, 10, aeb_text,
             pyinsim.ISB_LIGHT | pyinsim.ISB_CLICK),
            (40, 0, 120, 25, 10, "^1" + self.translator.get("Close", self.set_language),
             pyinsim.ISB_DARK | pyinsim.ISB_CLICK),
        ]

        for button_id, x, y, w, h, text, style in buttons:
            self.ui_manager.message_sender.create_button(button_id, x, y, w, h, text, style)

    def open_parking_menu(self):
        """Öffnet das Parken-Menü"""
        self.current_menu = 'parking'
        self._clear_menu_buttons()
        pdc_mode = self.settings.get('park_distance_control_mode')
        pdc_text = self.translator.get("Visual") if pdc_mode == 1 else self.translator.get("Visual & Audio") if pdc_mode == 2 else "^1Off"
        pdc = "^2" if self.settings.get('park_distance_control') else "^1"
        peb = "^2" if self.settings.get('parking_emergency_brake') else "^1"
        pdc_on = self.settings.get('park_distance_control')

        # Parken-Menü-Buttons
        buttons = [
            (21, 0, 70, 25, 10, self.translator.get("Parking Menu", self.set_language),
             pyinsim.ISB_LIGHT),
            (22, 0, 80, 25, 10, pdc + self.translator.get("Park Distance Control", self.set_language),
             pyinsim.ISB_DARK | pyinsim.ISB_CLICK),
            (23, 0, 90, 25, 10, peb + self.translator.get("Parking Emer. Brake", self.set_language),
             pyinsim.ISB_DARK | pyinsim.ISB_CLICK),
            (24, 25, 80, 20, 10, pdc_text,
             (pyinsim.ISB_DARK | pyinsim.ISB_CLICK) if pdc_on else pyinsim.ISB_LIGHT),
            (40, 0, 100, 25, 10, "^1" + self.translator.get("Close", self.set_language),
             pyinsim.ISB_DARK | pyinsim.ISB_CLICK),
        ]
        for button_id, x, y, w, h, text, style in buttons:
            self.ui_manager.message_sender.create_button(button_id, x, y, w, h, text, style)
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
            elif button_id == 23:
                self.open_parking_menu()
            elif button_id == 24:
                self.change_language()
            elif button_id == 25:
                self.open_system_settings()
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
            elif button_id == 24:  # Toggle CTW
                current = self.settings.get('cross_traffic_warning')
                self.settings.set('cross_traffic_warning', not current)
                self.open_driving_menu()
            elif button_id == 25:  # Toggle AEB
                current = self.settings.get('automatic_emergency_brake')
                new_value = (current + 1) % 3  # 0 = Off, 1 = Warn, 2 = Warn & Brake
                self.settings.set('automatic_emergency_brake', new_value)
                self.open_driving_menu()
            elif button_id == 26:  # Change Collision Warning Distance
                current = self.settings.get('collision_warning_distance')
                new_value = (current + 1) % 3  # 0 = Early, 1 = Normal, 2 = Late
                self.settings.set('collision_warning_distance', new_value)
                self.open_driving_menu()

        elif self.current_menu == 'parking':
            if button_id == 22:
                current = self.settings.get('park_distance_control')
                self.settings.set('park_distance_control', not current)
                if not current:
                    self.settings.set('park_distance_control_mode', 2)
                else:
                    self.settings.set('park_distance_control_mode', 0)
                self.open_parking_menu()
            elif button_id == 23:
                current = self.settings.get('parking_emergency_brake')
                self.settings.set('parking_emergency_brake', not current)
                self.open_parking_menu()
            elif button_id == 24:  # Change Park Distance Control Mode
                current = self.settings.get('park_distance_control_mode')
                new_value = (current + 1) % 3
                self.settings.set('park_distance_control_mode', new_value)
                self.open_parking_menu()

        if self.current_menu not in ['none', 'main']:
            if button_id == 40:
                self.close_menu()
                self.open_main_menu()

    def close_menu(self):
        """Schließt alle Menüs"""
        self.current_menu = 'none'
        self._clear_menu_buttons()
        self.create_open_menu_button()

    def change_language(self):
        """Wechselt die Sprache"""
        available_langs = self.translator.supported_languages
        current_index = available_langs.index(self.set_language)
        new_index = (current_index + 1) % len(available_langs)
        new_language = available_langs[new_index]
        self.settings.set('language', new_language)
        self.set_language = new_language
        self.open_main_menu()


