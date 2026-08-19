"""Outbound UI path: buttons, commands and chat lines (WP3).

The button registry is the point of this module. ``UIManager.update_hud`` runs
every 50 ms and repaints the HUD, the PDC display (up to 18 buttons), the siren
buttons and the notification line unconditionally; ``MenuSystem`` repaints a
whole menu page on every state change. Before the registry every one of those
calls put an ``IS_BTN`` on the wire, i.e. a continuous packet storm for content
that changes a few times a second at most. LFS's InSim socket is a
length-prefixed TCP stream shared with the keep-alive, so that traffic was not
free.

The registry keeps the last state actually sent per ``ClickID`` and drops
repeats.

(``known-issues.md`` #10 claimed that leaving the track sends 239 delete
packets. It does not: the old ``remove_button`` already checked its
``active_buttons`` map first, so the ``range(0, 239)`` loop in
``UIManager._state_change`` only ever produced one packet per visible button.
``remove_range`` replaces that loop with the same behaviour, expressed once.)
"""

import logging
import threading
from typing import Dict, Tuple

from lfs.connector import LFSConnector
from lfs.text_encoding import button_text_capacity, encode_button_text

logger = logging.getLogger(__name__)

# id -> (style, top, left, width, height, encoded text, inst)
ButtonState = Tuple[int, int, int, int, int, bytes, int]


class MessageSender:
    """Verwaltet das Senden von Nachrichten und UI-Elementen an LFS"""

    def __init__(self, connector: LFSConnector):
        self.connector = connector
        self._buttons: Dict[int, ButtonState] = {}
        # Buttons werden aus dem UI-Thread und aus Assistenz-Threads gesendet,
        # der Zustand muss also unter einem Lock stehen.
        self._lock = threading.Lock()
        # Diagnose/Test: wie viele Paket-Sendungen die Registry wirklich
        # ausgeloest hat.
        self.buttons_sent = 0
        self.buttons_suppressed = 0
        self.deletes_sent = 0
        self.deletes_suppressed = 0
        self.connector.event_bus.subscribe("send_command_to_lfs", self._on_send_command_to_lfs)
        self.connector.event_bus.subscribe("send_local_message_to_lfs", self.send_local_message)
        # Wenn LFS unsere Buttons selbst wegraeumt (SHIFT+B) oder die
        # Verbindung neu aufgebaut wird, ist die Registry veraltet: sie wuerde
        # den naechsten Repaint als 'unveraendert' unterdruecken und die
        # Buttons kaemen nie zurueck.
        self.connector.event_bus.subscribe("buttons_cleared", self._on_buttons_cleared)
        self.connector.event_bus.subscribe("lfs_connected", self._on_lfs_connected)

    def _on_buttons_cleared(self, data=None):
        logger.info("LFS cleared our buttons - registry invalidated.")
        self.invalidate()

    def _on_lfs_connected(self, data=None):
        self.invalidate()

    # ─── Chat / Kommandos ────────────────────────────────────────────────

    def _on_send_command_to_lfs(self, command: str):
        """Event-Handler für das Senden von Befehlen an LFS"""
        self.send_command(command)

    def send_local_message(self, message: str):
        """Sendet eine Chat-Nachricht"""
        self.connector.send_local_message_to_lfs(message)

    def send_command(self, command: str):
        """Sendet einen Befehl an LFS"""
        self.connector.send_command_to_lfs(command)

    # ─── Buttons ─────────────────────────────────────────────────────────

    @property
    def active_buttons(self) -> Dict[int, bool]:
        """Welche ClickIDs gerade auf dem Schirm sind (Lesezugriff)."""
        with self._lock:
            return {button_id: True for button_id in self._buttons}

    def is_live(self, button_id: int) -> bool:
        with self._lock:
            return button_id in self._buttons

    def create_button(self, button_id: int, x: int, y: int, width: int, height: int,
                      text: str, style: int = 0, inst: int = 0):
        """Erstellt oder aktualisiert einen Button.

        Sendet nur, wenn sich Position, Groesse, Stil oder Text gegenueber dem
        zuletzt gesendeten Stand geaendert haben. Der Text wird genau einmal
        hier kodiert (LFS-Codepages, siehe ``lfs/text_encoding.py``) und in
        kodierter Form verglichen, damit zwei Strings, die auf dem Schirm
        gleich aussehen, auch kein Paket erzeugen.
        """
        encoded = encode_button_text(text, button_text_capacity(width, height))
        state: ButtonState = (style, y, x, width, height, encoded, inst)

        with self._lock:
            previous = self._buttons.get(button_id)
            if previous == state:
                self.buttons_suppressed += 1
                return False
            self._buttons[button_id] = state
            self.buttons_sent += 1

        # Ausserhalb des Locks senden: der Socket-Write darf keine anderen
        # UI-Threads blockieren.
        sent = self.connector.send_button(button_id, style, y, x, width, height,
                                          encoded, inst)
        if sent is False:
            # Registry und LFS duerfen nicht auseinanderlaufen: sonst haelt die
            # Registry den Button fuer sichtbar und der naechste Repaint
            # unterdrueckt ihn fuer immer.
            self._restore(button_id, previous)
            return False
        return True

    def _restore(self, button_id: int, previous):
        with self._lock:
            if previous is None:
                self._buttons.pop(button_id, None)
            else:
                self._buttons[button_id] = previous

    def remove_button(self, button_id: int):
        """Entfernt einen Button - nur wenn er wirklich auf dem Schirm ist."""
        with self._lock:
            if button_id not in self._buttons:
                self.deletes_suppressed += 1
                return False
            previous = self._buttons.pop(button_id)
            self.deletes_sent += 1

        if self.connector.delete_button(button_id) is False:
            # Der Button steht in LFS noch - Registry entsprechend zuruecksetzen.
            self._restore(button_id, previous)
            return False
        return True

    def remove_range(self, first_id: int, last_id: int) -> int:
        """Entfernt alle lebenden Buttons im ClickID-Bereich [first, last].

        Ersetzt Schleifen der Form ``for i in range(0, 239): remove_button(i)``:
        es geht genau ein Paket pro tatsaechlich sichtbarem Button raus, ohne
        239 Aufrufe dafuer zu brauchen. Gibt die Anzahl der Loeschungen zurueck.
        """
        if last_id < first_id:
            first_id, last_id = last_id, first_id
        with self._lock:
            doomed = {button_id: state for button_id, state in self._buttons.items()
                      if first_id <= button_id <= last_id}
            for button_id in doomed:
                del self._buttons[button_id]
            self.deletes_sent += len(doomed)

        deleted = 0
        for button_id, previous in doomed.items():
            if self.connector.delete_button(button_id) is False:
                self._restore(button_id, previous)
            else:
                deleted += 1
        return deleted

    def remove_all(self) -> bool:
        """Entfernt alle Buttons dieser InSim-Verbindung.

        Nutzt ``BFN_CLEAR`` - ein einziges Paket, und es raeumt auch Buttons
        weg, von denen die Registry nichts (mehr) weiss. Genau das braucht der
        Shutdown, damit in LFS nichts stehen bleibt.
        """
        with self._lock:
            self._buttons.clear()
        self.connector.clear_all_buttons()
        return True

    def invalidate(self, button_id: int = None):
        """Vergisst gesendete Buttons, ohne Pakete zu schicken.

        Fuer den Fall, dass LFS unsere Buttons selbst geloescht hat
        (``BFN_USER_CLEAR``, SHIFT+B) - danach muss der naechste Repaint
        wieder senden, obwohl sich der Inhalt nicht geaendert hat.
        """
        with self._lock:
            if button_id is None:
                self._buttons.clear()
            else:
                self._buttons.pop(button_id, None)

    def reset_counters(self):
        """Setzt die Diagnosezaehler zurueck (Tests)."""
        with self._lock:
            self.buttons_sent = 0
            self.buttons_suppressed = 0
            self.deletes_sent = 0
            self.deletes_suppressed = 0
