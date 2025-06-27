from typing import Dict
from lfs.connector import LFSConnector


class MessageSender:
    """Verwaltet das Senden von Nachrichten und UI-Elementen an LFS"""

    def __init__(self, connector: LFSConnector):
        self.connector = connector
        self.active_buttons: Dict[int, bool] = {}

    def send_message(self, message: str):
        """Sendet eine Chat-Nachricht"""
        # Implementation für Chat-Nachrichten
        pass

    def create_button(self, button_id: int, x: int, y: int, width: int, height: int,
                      text: str, style: int = 0):
        """Erstellt einen Button"""
        if button_id not in self.active_buttons or not self.active_buttons[button_id]:
            self.connector.send_button(button_id, style, y, x, width, height, text)
            self.active_buttons[button_id] = True

    def remove_button(self, button_id: int):
        """Entfernt einen Button"""
        if self.active_buttons.get(button_id, False):
            self.connector.delete_button(button_id)
            self.active_buttons[button_id] = False