import re
import random
import time

import pyinsim
from pyinsim.func import stripcols, stripenc
from core.event_bus import EventBus
from core.settings_manager import SettingsManager
from misc.language import LanguageManager


# Regex to strip LFS color codes and encoding markers
_COLOR_REGEX = re.compile(r'\^[0-9]')
_ENC_COL_REGEX = re.compile(r'\^[LETBJCGHSK0-9]')


def _strip_all(text: str) -> str:
    """Entfernt alle LFS-Encoding- und Farb-Marker aus einem String."""
    return _ENC_COL_REGEX.sub('', text)


class ChatCommandHandler:
    """Verarbeitet Chat-Befehle aus LFS ($-Prefix Nachrichten).

    Lauscht auf 'message_received' Events und führt entsprechende
    Aktionen aus, wenn der eigene Spieler einen $-Befehl sendet.
    Erweiterbar für zukünftige Chat-Befehle.
    """

    def __init__(self, event_bus: EventBus, settings: SettingsManager):
        self.event_bus = event_bus
        self.settings = settings
        self.translator = LanguageManager()
        self.player_name = ""

        # Event-Subscriptions
        self.event_bus.subscribe('message_received', self._on_message_received)
        self.event_bus.subscribe('player_name_changed', self._on_player_name_changed)

        # Command-Mapping: command_string -> handler_method
        self._commands = {
            'help': self._cmd_help,
            'siren': self._cmd_siren,
            'strobe': self._cmd_strobe,
            'fcw': self._cmd_fcw,
            'ctw': self._cmd_ctw,
            'autoh': self._cmd_autoh,
            'light': self._cmd_light,
            'highbeam': self._cmd_highbeam,
        }

        # Tooltip system
        self._tooltip_keys = [
            "tooltip_help_command",
            "tooltip_key_binding",
            "tooltip_fun_fact",
            "tooltip_bug_report",
            "tooltip_ai_traffic",
        ]
        self._last_tooltip_time = time.time()
        self._on_track = False

        # Subscribe to state data for on-track detection
        self.event_bus.subscribe('state_data', self._on_state_data)

    def _on_state_data(self, data):
        """Aktualisiert den On-Track-Status."""
        self._on_track = data.get('on_track', False)

    def check_tooltip(self):
        """Prüft ob ein periodischer Tooltip gesendet werden soll. Wird extern aufgerufen."""
        if not self._on_track:
            return

        interval = 360
        now = time.time()
        if now - self._last_tooltip_time >= interval:
            self._last_tooltip_time = now
            self._send_random_tooltip()

    def _on_player_name_changed(self, data):
        """Aktualisiert den eigenen Spielernamen."""
        self.player_name = data.get('player_name', b'')
        if isinstance(self.player_name, str):
            self.player_name = self.player_name.encode('latin-1', errors='ignore')

    def _normalize_name(self, name_bytes: bytes) -> str:
        """Normalisiert einen LFS-Spielernamen: decodiert und entfernt Encoding/Color-Marker."""
        try:
            name_str = name_bytes.decode('latin-1', errors='ignore')
        except (UnicodeDecodeError, AttributeError):
            name_str = str(name_bytes)
        return _strip_all(name_str).strip().lower()

    def _on_message_received(self, mso):
        """Verarbeitet eingehende MSO-Nachrichten.

        Prüft ob die Nachricht vom eigenen Spieler stammt und einen
        $-Befehl enthält. Nutzt MSO_PREFIX UserType und TextStart.
        """
        try:
            # Nur Prefix-Nachrichten verarbeiten (UserType == MSO_PREFIX == 2)
            if mso.UserType != pyinsim.MSO_PREFIX:
                return

            msg = mso.Msg
            if isinstance(msg, bytes):
                msg_str = msg.decode('latin-1', errors='ignore')
            else:
                msg_str = str(msg)

            # TextStart gibt den Offset des eigentlichen Textes in Msg an
            text_start = mso.TextStart
            if isinstance(mso.Msg, bytes):
                # Der Name-Teil ist alles vor TextStart
                name_part = mso.Msg[:text_start]
                text_part = mso.Msg[text_start:]
            else:
                name_part = msg_str[:text_start].encode('latin-1', errors='ignore')
                text_part = msg_str[text_start:].encode('latin-1', errors='ignore')

            # Spielernamen-Vergleich: Prüfe ob die Nachricht vom eigenen Spieler stammt
            if not self._is_own_player(name_part):
                return

            # Text-Teil decodieren und bereinigen
            if isinstance(text_part, bytes):
                text = text_part.decode('latin-1', errors='ignore')
            else:
                text = str(text_part)

            # Encoding-Marker entfernen und trimmen
            text = _strip_all(text).strip()

            # Prüfe ob es ein $-Befehl ist
            if not text.startswith('$'):
                return

            # Command extrahieren (ohne $, case-insensitive)
            command = text[1:].strip().lower()

            # Command ausführen
            if command in self._commands:
                self._commands[command]()

        except Exception as e:
            print(f"[ChatCommands] Error processing message: {e}")

    def _is_own_player(self, name_part_bytes: bytes) -> bool:
        """Prüft ob der Name-Teil der Nachricht zum eigenen Spieler gehört.

        Vergleicht den bereinigten Namen aus der Nachricht mit dem
        bereinigten eigenen Spielernamen.
        """
        if not self.player_name:
            return False

        # Eigenen Spielernamen normalisieren
        own_name_clean = self._normalize_name(self.player_name)

        # Name aus der Nachricht normalisieren
        # Format: "PlayerName : " - wir entfernen den Doppelpunkt und Leerzeichen
        name_clean = self._normalize_name(name_part_bytes).rstrip(' :').rstrip(':').strip()

        if not own_name_clean or not name_clean:
            return False

        # Vergleich: Der Name in der Nachricht sollte den eigenen Spielernamen enthalten
        return own_name_clean in name_clean or name_clean in own_name_clean

    def _notify(self, message_key: str, enabled: bool):
        """Sendet eine Notification mit übersetztem Status-Text."""
        lang = self.settings.get('language')
        name = self.translator.get(message_key, lang)
        if enabled:
            status = self.translator.get("enabled", lang)
            color = "^2"
        else:
            status = self.translator.get("disabled", lang)
            color = "^1"
        self.event_bus.emit("notification", {'notification': f'{color}{name}: {status}'})

    # ─── Command Handlers ─────────────────────────────────────────────

    def _cmd_siren(self):
        """Toggled die Sirene (nur im Cop-Modus)."""
        self.event_bus.emit('siren_toggle_requested', {})

    def _cmd_strobe(self):
        """Toggled die Stroboskoplichter (nur im Cop-Modus)."""
        self.event_bus.emit('strobe_toggle_requested', {})

    def _cmd_fcw(self):
        """Toggled die Frontkollisionswarnung."""
        current = self.settings.get('forward_collision_warning')
        self.settings.set('forward_collision_warning', not current)
        self._notify("Collision Warning", not current)

    def _cmd_ctw(self):
        """Toggled die Querverkehrswarnung."""
        current = self.settings.get('cross_traffic_warning')
        self.settings.set('cross_traffic_warning', not current)
        self._notify("Cross Traffic Warn.", not current)

    def _cmd_autoh(self):
        """Toggled Auto-Hold."""
        current = self.settings.get('auto_hold')
        self.settings.set('auto_hold', not current)
        self._notify("Auto Hold", not current)

    def _cmd_light(self):
        """Toggled adaptive Lichtfunktionen."""
        current = self.settings.get('adaptive_lights')
        self.settings.set('adaptive_lights', not current)
        self._notify("Adaptive Lights", not current)

    def _cmd_highbeam(self):
        """Toggled den Fernlichtassistenten."""
        current = self.settings.get('high_beam_assist')
        self.settings.set('high_beam_assist', not current)
        self._notify("High Beam Assist", not current)

    def _cmd_help(self):
        """Sendet eine Liste aller verfügbaren Commands als lokale Nachrichten."""
        help_lines = [
            "^7--- PACT Driving Assistant Commands ---",
            "^7$help    ^3- Show this help",
            "^7$siren   ^3- Turn on/off siren as a cop",
            "^7$strobe  ^3- Turn on/off strobe as a cop",
            "^7$fcw     ^3- Turn on/off forward collision warning",
            "^7$ctw     ^3- Turn on/off cross traffic warning",
            "^7$autoh   ^3- Turn on/off auto-hold (parking brake when stopping)",
            "^7$light   ^3- Turn on/off adaptive brake lights",
            "^7$highbeam^3- Turn on/off high beam assistant",
        ]
        for line in help_lines:
            self.event_bus.emit("send_local_message_to_lfs", line)

    def _send_random_tooltip(self):
        """Sendet einen zufälligen übersetzten Tooltip als lokale Nachricht."""
        lang = self.settings.get('language')
        key = random.choice(self._tooltip_keys)
        text = self.translator.get(key, lang)
        self.event_bus.emit("send_local_message_to_lfs", f"^3PACT ^7| {text}")

