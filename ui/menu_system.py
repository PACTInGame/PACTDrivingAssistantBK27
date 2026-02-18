from typing import List

import pyinsim
from core.settings_manager import SettingsManager
from misc import key_binder
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
        self.keybinder = key_binder.Keybinder(self.ui_manager.event_bus)
        self.ui_manager.event_bus.subscribe('state_data', self._state_change)
        self.ui_manager.event_bus.subscribe('button_clicked', self._handle_ui_action)
        self.ui_manager.event_bus.subscribe('player_name_changed', self._handle_player_change)
        self.ui_manager.event_bus.subscribe('new_keybinding', self._rebind_key)

    def _rebind_key(self, data):
        setting = data['setting']
        new_key = data['button']
        self.settings.set(setting, new_key)
        print(f"Rebound {setting} to {new_key}")
        if self.current_menu == 'await_key':
            self.open_keys_settings()

    def _handle_player_change(self, data):
        control_mode = data.get('control_mode', 0)
        self.settings.set('own_control_mode', control_mode)

    def _state_change(self, data):
        new_on_track = data['on_track']
        if new_on_track != self.on_track:
            self.on_track = new_on_track
            if new_on_track and self.current_menu == 'none':
                self.create_open_menu_button()
            else:
                self.close_menu()

    # ─── Main Menu ────────────────────────────────────────────────────

    def open_main_menu(self):
        """Öffnet das Hauptmenü"""
        self.current_menu = 'main'
        self._clear_menu_buttons()

        buttons = [
            (21, 0, 80, 20, 5, self.translator.get("Main Menu", self.set_language),
             pyinsim.ISB_LIGHT),
            (22, 0, 85, 20, 5, self.translator.get("Driving", self.set_language),
             pyinsim.ISB_DARK | pyinsim.ISB_CLICK),
            (23, 0, 90, 20, 5, self.translator.get("Parking", self.set_language),
             pyinsim.ISB_DARK | pyinsim.ISB_CLICK),
            (24, 0, 95, 20, 5, self.translator.get("System", self.set_language),
             pyinsim.ISB_DARK | pyinsim.ISB_CLICK),
            (25, 0, 100, 20, 5, self.translator.get("Cop Mode", self.set_language),
             pyinsim.ISB_DARK | pyinsim.ISB_CLICK),
            (26, 0, 105, 20, 5, self.translator.get("Keys and Axes", self.set_language),
             pyinsim.ISB_DARK | pyinsim.ISB_CLICK),
            (27, 0, 110, 20, 5,
             self.translator.get("Language", self.set_language) + f": {self.set_language}",
             pyinsim.ISB_DARK | pyinsim.ISB_CLICK),
            (40, 0, 115, 20, 5, "^1" + self.translator.get("Close", self.set_language),
             pyinsim.ISB_DARK | pyinsim.ISB_CLICK),
        ]

        for button_id, x, y, w, h, text, style in buttons:
            self.ui_manager.message_sender.create_button(button_id, x, y, w, h, text, style)

    # ─── Driving Menu ─────────────────────────────────────────────────

    def open_driving_menu(self):
        """Öffnet das Fahrer-Menü"""
        self.current_menu = 'driving'
        self._clear_menu_buttons()

        fcw = "^2" if self.settings.get('forward_collision_warning') else "^1"
        bsw = "^2" if self.settings.get('blind_spot_warning') else "^1"
        ctw = "^2" if self.settings.get('cross_traffic_warning') else "^1"
        agb = "^2" if self.settings.get('automatic_gearbox') else "^1"
        ah = "^2" if self.settings.get('auto_hold') else "^1"
        al = "^2" if self.settings.get('adaptive_lights') else "^1"
        hba = "^2" if self.settings.get('high_beam_assist') else "^1"

        distance = self.settings.get('collision_warning_distance')
        distance_text = "^2Early" if distance == 0 else "^3Medium" if distance == 1 else "^1Late"

        buttons = [
            (21, 0, 70, 25, 5, self.translator.get("Driving Settings", self.set_language),
             pyinsim.ISB_LIGHT),
            (22, 0, 75, 25, 5, fcw + self.translator.get("Collision Warning", self.set_language),
             pyinsim.ISB_DARK | pyinsim.ISB_CLICK),
            (23, 25, 75, 15, 5, distance_text,
             pyinsim.ISB_DARK | pyinsim.ISB_CLICK),
            (24, 0, 80, 25, 5, bsw + self.translator.get("Blind Spot Warn.", self.set_language),
             pyinsim.ISB_DARK | pyinsim.ISB_CLICK),
            (25, 0, 85, 25, 5, ctw + self.translator.get("Cross Traffic Warn.", self.set_language),
             pyinsim.ISB_DARK | pyinsim.ISB_CLICK),
            (26, 0, 90, 25, 5, agb + self.translator.get("Automatic Gearbox", self.set_language),
             pyinsim.ISB_DARK | pyinsim.ISB_CLICK),
            (30, 25, 90, 15, 5, self.translator.get("Calibrate", self.set_language),
             pyinsim.ISB_DARK | pyinsim.ISB_CLICK),
            (27, 0, 95, 25, 5, ah + self.translator.get("Auto Hold", self.set_language),
             pyinsim.ISB_DARK | pyinsim.ISB_CLICK),
            (28, 0, 100, 25, 5, al + self.translator.get("Adaptive Lights", self.set_language),
             pyinsim.ISB_DARK | pyinsim.ISB_CLICK),
            (29, 0, 105, 25, 5, hba + self.translator.get("High Beam Assist", self.set_language),
             pyinsim.ISB_DARK | pyinsim.ISB_CLICK),
            (40, 0, 110, 25, 5, "^1" + self.translator.get("Close", self.set_language),
             pyinsim.ISB_DARK | pyinsim.ISB_CLICK),
        ]

        for button_id, x, y, w, h, text, style in buttons:
            self.ui_manager.message_sender.create_button(button_id, x, y, w, h, text, style)

    # ─── Parking Menu ─────────────────────────────────────────────────

    def open_parking_menu(self):
        """Öffnet das Parken-Menü"""
        self.current_menu = 'parking'
        self._clear_menu_buttons()

        pdc_on = self.settings.get('park_distance_control')
        pdc = "^2" if pdc_on else "^1"
        pdc_mode = self.settings.get('park_distance_control_mode')
        pdc_mode_text = (
            self.translator.get("Visual", self.set_language) if pdc_mode == 1
            else self.translator.get("Visual & Audio", self.set_language) if pdc_mode == 2
            else "^1Off"
        )

        buttons = [
            (21, 0, 80, 25, 5, self.translator.get("Parking Settings", self.set_language),
             pyinsim.ISB_LIGHT),
            (22, 0, 85, 25, 5, pdc + self.translator.get("Park Distance Control", self.set_language),
             pyinsim.ISB_DARK | pyinsim.ISB_CLICK),
            (23, 25, 85, 20, 5, pdc_mode_text,
             (pyinsim.ISB_DARK | pyinsim.ISB_CLICK) if pdc_on else pyinsim.ISB_LIGHT),
            (40, 0, 90, 25, 5, "^1" + self.translator.get("Close", self.set_language),
             pyinsim.ISB_DARK | pyinsim.ISB_CLICK),
        ]

        for button_id, x, y, w, h, text, style in buttons:
            self.ui_manager.message_sender.create_button(button_id, x, y, w, h, text, style)

    # ─── System Settings Menu ─────────────────────────────────────────

    def open_system_settings(self):
        """Öffnet die Systemeinstellungen"""
        self.current_menu = 'system'
        self._clear_menu_buttons()

        unit = self.settings.get('unit')
        unit_text = "^2Metric" if unit == "metric" else "^2Imperial"
        hud_on = self.settings.get('hud_active')
        hud_text = "^2" if hud_on else "^1"
        hud_h = self.settings.get('hud_height')
        hud_w = self.settings.get('hud_width')

        buttons = [
            (21, 0, 75, 25, 5, self.translator.get("System Settings", self.set_language),
             pyinsim.ISB_LIGHT),
            (22, 0, 80, 20, 5, self.translator.get("Unit", self.set_language),
             pyinsim.ISB_DARK | pyinsim.ISB_CLICK),
            (23, 20, 80, 10, 5, unit_text,
             pyinsim.ISB_LIGHT),
            (24, 0, 85, 20, 5, hud_text + self.translator.get("Head-Up Display", self.set_language),
             pyinsim.ISB_DARK | pyinsim.ISB_CLICK),
            (25, 0, 90, 25, 5, f"^7HUD Position  (V:{hud_h}  H:{hud_w})",
             pyinsim.ISB_LIGHT),
            (26, 25, 90, 5, 5, "^7Up",
             pyinsim.ISB_DARK | pyinsim.ISB_CLICK),
            (27, 30, 90, 5, 5, "^7Down",
             pyinsim.ISB_DARK | pyinsim.ISB_CLICK),
            (28, 35, 90, 5, 5, "^7Left",
             pyinsim.ISB_DARK | pyinsim.ISB_CLICK),
            (29, 40, 90, 5, 5, "^7Right",
             pyinsim.ISB_DARK | pyinsim.ISB_CLICK),
            (40, 0, 95, 25, 5, "^1" + self.translator.get("Close", self.set_language),
             pyinsim.ISB_DARK | pyinsim.ISB_CLICK),
        ]

        for button_id, x, y, w, h, text, style in buttons:
            self.ui_manager.message_sender.create_button(button_id, x, y, w, h, text, style)

    # ─── Cop Mode Menu ────────────────────────────────────────────────

    def open_cop_menu(self):
        """Öffnet das Cop-Mode-Menü"""
        self.current_menu = 'cop'
        self._clear_menu_buttons()

        cop = "^2" if self.settings.get('cop_assistance') else "^1"

        buttons = [
            (21, 0, 80, 25, 5, self.translator.get("Cop Mode Settings", self.set_language),
             pyinsim.ISB_LIGHT),
            (22, 0, 85, 25, 5, cop + self.translator.get("Cop Assistance", self.set_language),
             pyinsim.ISB_DARK | pyinsim.ISB_CLICK),
            (40, 0, 90, 25, 5, "^1" + self.translator.get("Close", self.set_language),
             pyinsim.ISB_DARK | pyinsim.ISB_CLICK),
        ]

        for button_id, x, y, w, h, text, style in buttons:
            self.ui_manager.message_sender.create_button(button_id, x, y, w, h, text, style)

    # ─── Keys and Axes Menu ───────────────────────────────────────────

    def open_keys_settings(self):
        """Öffnet die Einstellungen für Tastenbelegung und Achsen"""
        self.current_menu = 'keys'
        self._clear_menu_buttons()

        handbrake_key = self.settings.get('user_handbrake_key').upper()
        shift_up_key = self.settings.get('user_shift_up_key').upper()
        shift_down_key = self.settings.get('user_shift_down_key').upper()
        clutch_key = self.settings.get('user_clutch_key').upper()
        ignition_key = self.settings.get('user_ignition_key').upper()
        brake_key = self.settings.get('user_brake_key').upper()
        brake_axis = self.settings.get('user_axis_brake')
        vjoy_axis = self.settings.get('vjoy_axis_1')

        buttons = [
            (21, 0, 75, 25, 5, self.translator.get("Keys and Axes", self.set_language),
             pyinsim.ISB_LIGHT),
            (24, 0, 85, 20, 5, self.translator.get("Handbrake Key", self.set_language),
             pyinsim.ISB_DARK | pyinsim.ISB_CLICK),
            (25, 0, 90, 20, 5, self.translator.get("Shift Up Key", self.set_language),
             pyinsim.ISB_DARK | pyinsim.ISB_CLICK),
            (35, 0, 95, 20, 5, self.translator.get("Shift Down Key", self.set_language),
             pyinsim.ISB_DARK | pyinsim.ISB_CLICK),
            (26, 0, 100, 20, 5, self.translator.get("Clutch Key", self.set_language),
             pyinsim.ISB_DARK | pyinsim.ISB_CLICK),
            (27, 0, 105, 20, 5, self.translator.get("Ignition Key", self.set_language),
             pyinsim.ISB_DARK | pyinsim.ISB_CLICK),
            (28, 20, 85, 5, 5, f"{handbrake_key}", pyinsim.ISB_LIGHT),
            (29, 20, 90, 5, 5, f"{shift_up_key}", pyinsim.ISB_LIGHT),
            (36, 20, 95, 5, 5, f"{shift_down_key}", pyinsim.ISB_LIGHT),
            (30, 20, 100, 5, 5, f"{clutch_key}", pyinsim.ISB_LIGHT),
            (31, 20, 105, 5, 5, f"{ignition_key}", pyinsim.ISB_LIGHT),
            (40, 0, 110, 25, 5, "^1" + self.translator.get("Close", self.set_language),
             pyinsim.ISB_DARK | pyinsim.ISB_CLICK),
        ]

        control_mode = self.settings.get('own_control_mode')
        if control_mode in [0, 1]:
            buttons.append((22, 0, 80, 20, 5, self.translator.get("Brake Key", self.set_language),
                            pyinsim.ISB_DARK | pyinsim.ISB_CLICK))
            buttons.append((32, 20, 80, 5, 5, f"{brake_key}", pyinsim.ISB_LIGHT))
        elif control_mode == 2:
            buttons.append((22, 0, 80, 20, 5, self.translator.get("Brake Axis", self.set_language),
                            pyinsim.ISB_DARK | pyinsim.ISB_CLICK))
            buttons.append((23, 25, 80, 20, 5, self.translator.get("Vjoy Axis", self.set_language),
                            pyinsim.ISB_DARK | pyinsim.ISB_CLICK))
            buttons.append((33, 20, 80, 5, 5, f"{brake_axis}", pyinsim.ISB_LIGHT))
            buttons.append((34, 45, 80, 5, 5, f"{vjoy_axis}", pyinsim.ISB_LIGHT))

        for button_id, x, y, w, h, text, style in buttons:
            self.ui_manager.message_sender.create_button(button_id, x, y, w, h, text, style)

    # ─── Await Key Binding ────────────────────────────────────────────

    def open_awaiting_key(self, setting):
        """Show user prompt to press a key for binding"""
        self.current_menu = 'await_key'
        self._clear_menu_buttons()

        text = f"^7Key {setting}, currently bound to '{self.settings.get(setting)}'."

        buttons = [
            (21, 0, 80, 25, 5, self.translator.get("Rebind Key", self.set_language),
             pyinsim.ISB_LIGHT),
            (22, 0, 85, 25, 5, self.translator.get("Press a key to bind...", self.set_language),
             pyinsim.ISB_LIGHT),
            (23, 0, 90, 50, 5, text,
             pyinsim.ISB_LIGHT),
            (40, 0, 95, 25, 5, "^1" + self.translator.get("Cancel", self.set_language),
             pyinsim.ISB_DARK | pyinsim.ISB_CLICK),
        ]

        for button_id, x, y, w, h, text, style in buttons:
            self.ui_manager.message_sender.create_button(button_id, x, y, w, h, text, style)

    # ─── Helpers ──────────────────────────────────────────────────────

    def _clear_menu_buttons(self):
        """Löscht alle Menü-Buttons"""
        for button_id in range(20, 41):
            self.ui_manager.message_sender.remove_button(button_id)

    def create_open_menu_button(self):
        self.ui_manager.message_sender.create_button(
            20, 0, 100, 20, 10,
            self.translator.get("Main Menu", self.settings.get('language')),
            pyinsim.ISB_DARK | pyinsim.ISB_CLICK,
        )

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

    # ─── Click Handling ───────────────────────────────────────────────

    def _handle_ui_action(self, data):
        """Verarbeitet UI-Aktionen"""
        btc = data
        button_id = btc.ClickID
        if 20 <= button_id <= 40:
            self._handle_menu_click(button_id)

    def _handle_menu_click(self, button_id: int):
        """Verarbeitet Menü-Klicks"""

        # ── Open menu from the floating button ──
        if self.current_menu == 'none':
            if button_id == 20:
                self.open_main_menu()
            return

        # ── Close button (40) — always returns to main menu (except from main) ──
        if button_id == 40:
            if self.current_menu == 'main':
                self.close_menu()
            elif self.current_menu == 'await_key':
                self.keybinder.stop_listening()
                self.ui_manager.event_bus.emit(
                    "notification", {'notification': '^1Keybinding cancelled.'})
                self.open_keys_settings()
            else:
                self.open_main_menu()
            return

        # ── Main Menu ──
        if self.current_menu == 'main':
            if button_id == 22:
                self.open_driving_menu()
            elif button_id == 23:
                self.open_parking_menu()
            elif button_id == 24:
                self.open_system_settings()
            elif button_id == 25:
                self.open_cop_menu()
            elif button_id == 26:
                self.open_keys_settings()
            elif button_id == 27:
                self.change_language()

        # ── Driving Menu ──
        elif self.current_menu == 'driving':
            if button_id == 22:  # Toggle FCW
                current = self.settings.get('forward_collision_warning')
                self.settings.set('forward_collision_warning', not current)
                self.open_driving_menu()
            elif button_id == 23:  # Change Collision Warning Distance
                current = self.settings.get('collision_warning_distance')
                self.settings.set('collision_warning_distance', (current + 1) % 3)
                self.open_driving_menu()
            elif button_id == 24:  # Toggle BSW
                current = self.settings.get('blind_spot_warning')
                self.settings.set('blind_spot_warning', not current)
                self.open_driving_menu()
            elif button_id == 25:  # Toggle CTW
                current = self.settings.get('cross_traffic_warning')
                self.settings.set('cross_traffic_warning', not current)
                self.open_driving_menu()
            elif button_id == 26:  # Toggle Automatic Gearbox
                current = self.settings.get('automatic_gearbox')
                self.settings.set('automatic_gearbox', not current)
                self.open_driving_menu()
            elif button_id == 27:  # Toggle Auto Hold
                current = self.settings.get('auto_hold')
                self.settings.set('auto_hold', not current)
                self.open_driving_menu()
            elif button_id == 28:  # Toggle Adaptive Lights
                current = self.settings.get('adaptive_lights')
                self.settings.set('adaptive_lights', not current)
                self.open_driving_menu()
            elif button_id == 29:  # Toggle High Beam Assist
                current = self.settings.get('high_beam_assist')
                self.settings.set('high_beam_assist', not current)
                self.open_driving_menu()
            elif button_id == 30:  # Calibrate Gearbox
                self.ui_manager.event_bus.emit('gearbox_calibrate', {})
                self.ui_manager.event_bus.emit(
                    "notification", {'notification': '^3Gearbox calibration requested...'})
                self.close_menu()

        # ── Parking Menu ──
        elif self.current_menu == 'parking':
            if button_id == 22:  # Toggle PDC
                current = self.settings.get('park_distance_control')
                self.settings.set('park_distance_control', not current)
                if not current:
                    self.settings.set('park_distance_control_mode', 2)
                else:
                    self.settings.set('park_distance_control_mode', 0)
                    self.ui_manager.remove_pdc_display()
                self.open_parking_menu()
            elif button_id == 23:  # Cycle PDC mode (only when PDC is on)
                if self.settings.get('park_distance_control'):
                    current = self.settings.get('park_distance_control_mode')
                    new_value = current + 1
                    if new_value > 2:
                        new_value = 1
                    self.settings.set('park_distance_control_mode', new_value)
                    self.open_parking_menu()

        # ── System Settings ──
        elif self.current_menu == 'system':
            if button_id == 22:  # Toggle Unit
                current = self.settings.get('unit')
                new_unit = "imperial" if current == "metric" else "metric"
                self.settings.set('unit', new_unit)
                self.open_system_settings()
            elif button_id == 24:  # Toggle HUD
                current = self.settings.get('hud_active')
                self.settings.set('hud_active', not current)
                self.open_system_settings()
            elif button_id == 26:  # HUD Up
                current = self.settings.get('hud_height')
                self.settings.set('hud_height', max(0, current - 2))
                self.open_system_settings()
            elif button_id == 27:  # HUD Down
                current = self.settings.get('hud_height')
                self.settings.set('hud_height', min(200, current + 2))
                self.open_system_settings()
            elif button_id == 28:  # HUD Left
                current = self.settings.get('hud_width')
                self.settings.set('hud_width', max(0, current - 2))
                self.open_system_settings()
            elif button_id == 29:  # HUD Right
                current = self.settings.get('hud_width')
                self.settings.set('hud_width', min(200, current + 2))
                self.open_system_settings()

        # ── Cop Mode Menu ──
        elif self.current_menu == 'cop':
            if button_id == 22:  # Toggle Cop Assistance
                current = self.settings.get('cop_assistance')
                self.settings.set('cop_assistance', not current)
                self.open_cop_menu()

        # ── Keys and Axes Menu ──
        elif self.current_menu == 'keys':
            setting = ''
            if button_id == 22:
                if self.settings.get('own_control_mode') in [0, 1]:
                    setting = 'user_brake_key'
                    self.ui_manager.event_bus.emit('await_keybinding', {'setting': setting})
                    self.open_awaiting_key(setting)
                else:
                    current_axis = self.settings.get('user_axis_brake')
                    self.settings.set('user_axis_brake', (current_axis + 1) % 25)
                    self.open_keys_settings()
            elif button_id == 23:
                current_axis = self.settings.get('vjoy_axis_1')
                self.settings.set('vjoy_axis_1', (current_axis + 1) % 25)
                self.open_keys_settings()
            elif button_id == 24:
                setting = 'user_handbrake_key'
            elif button_id == 25:
                setting = 'user_shift_up_key'
            elif button_id == 35:
                setting = 'user_shift_down_key'
            elif button_id == 26:
                setting = 'user_clutch_key'
            elif button_id == 27:
                setting = 'user_ignition_key'

            if button_id in [24, 25, 35, 26, 27]:
                self.ui_manager.event_bus.emit('await_keybinding', {'setting': setting})
                self.open_awaiting_key(setting)

        # ── Await Key ──
        elif self.current_menu == 'await_key':
            pass  # Close/cancel is handled above via button_id == 40