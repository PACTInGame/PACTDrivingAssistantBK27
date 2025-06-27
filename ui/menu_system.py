class MenuSystem:
    """Verwaltet das Menüsystem"""

    def __init__(self, ui_manager: UIManager, settings: SettingsManager):
        self.ui_manager = ui_manager
        self.settings = settings
        self.current_menu = 'none'
        self.menu_stack: List[str] = []

    def open_main_menu(self):
        """Öffnet das Hauptmenü"""
        self.current_menu = 'main'
        self._clear_menu_buttons()

        # Hauptmenü-Buttons
        buttons = [
            (21, 50, 50, 100, 30, "Main Menu"),
            (22, 50, 85, 100, 30, "Driving"),
            (23, 50, 120, 100, 30, "Parking"),
            (24, 50, 155, 100, 30, "Bus"),
            (25, 50, 190, 100, 30, "Language"),
            (40, 50, 225, 100, 30, "Close"),
        ]

        for button_id, x, y, w, h, text in buttons:
            self.ui_manager.message_sender.create_button(button_id, x, y, w, h, text)

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
        for button_id in range(21, 41):
            self.ui_manager.message_sender.remove_button(button_id)

    def handle_menu_click(self, button_id: int):
        """Verarbeitet Menü-Klicks"""
        if self.current_menu == 'main':
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