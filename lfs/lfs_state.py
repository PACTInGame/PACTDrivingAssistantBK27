"""Screen context: which LFS screen are we on, and may we draw on it (WP5).

LFS is not one screen. ``IS_STA`` tells us the coarse state (in game, entry
screen, dialog, text entry) and ``IS_CIM`` tells us which sub-interface the
local connection is in (options, garage and its nine submodes, car/track
select, SHIFT+U free view). Before WP5 only three booleans were derived and
``in_game_interface`` / ``submode_interface`` were placeholders that were
initialised to 0 and never written, although seven components read them out of
``state_data`` (known-issues #25, ``reference/ui.md`` §1).
"""

import logging
import time

import pyinsim

logger = logging.getLogger(__name__)

# Wie lange nach dem Verlassen der Strecke gewartet wird, bevor der
# OutGauge-Socket beim naechsten Streckeneintritt neu geoeffnet wird.
OUTGAUGE_REINIT_AFTER_S = 30

# Bildschirm-Kontexte (reference/ui.md §1.1). Werte des state_data-Schluessels
# 'screen'.
SCREEN_MAIN_MENU = 'main_menu'   # Hauptmenue / Serverliste - hier nichts zeichnen
SCREEN_ENTRY = 'entry'           # Einstiegsbildschirm eines Rennens
SCREEN_GARAGE = 'garage'         # Box / Garage
SCREEN_OPTIONS = 'options'       # Optionen, Host-Optionen, Auto-/Streckenwahl
SCREEN_SHIFTU = 'shiftu'         # SHIFT+U Freikamera
SCREEN_GAME = 'game'             # auf der Strecke

# In diesen Kontexten zeigt LFS normale Buttons ueberhaupt nicht an; nur
# INST_ALWAYS_ON ueberlebt dort (InSim.txt, reference/ui.md §1.1).
_SCREENS_WITHOUT_BUTTONS = (SCREEN_MAIN_MENU, SCREEN_OPTIONS)

_CIM_MENU_MODES = (pyinsim.CIM_OPTIONS, pyinsim.CIM_HOST_OPTIONS,
                   pyinsim.CIM_CAR_SELECT, pyinsim.CIM_TRACK_SELECT)


def _as_int(value, default: int = 0) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def _decode_track(value) -> str:
    """``IS_STA.Track`` als str - ein kurzer ASCII-Code wie ``SO6R``."""
    if isinstance(value, (bytes, bytearray)):
        return bytes(value).split(b'\x00', 1)[0].decode('latin-1', errors='replace')
    if value is None:
        return ""
    return str(value)


class StateHandler:
    """Leitet den Bildschirm-Kontext aus IS_STA und IS_CIM ab"""

    def __init__(self, connector):
        self.connector = connector
        self.text_entry = False
        self.dialog = False
        self.on_track = False
        self.track = ""
        self.in_game_cam = 0  # 0 = Follow, 1 = Heli, 2 = TV, 3 = Driver, 4 = Custom, 255 = Another
        self.in_game_interface = 0  # CIM_NORMAL/OPTIONS/HOST_OPTIONS/GARAGE/CAR_SELECT/TRACK_SELECT/SHIFTU
        self.submode_interface = 0  # NRM_* / GRG_* / FVM_*, je nach in_game_interface
        self.select_type = 0        # IS_CIM.SelType - was gerade ausgewaehlt ist
        self.ui_visible = False     # ISS_VISIBLE - LFS zeigt InSim-Buttons an
        self.shift_u = False        # ISS_SHIFTU
        self.multiplayer = False    # ISS_MULTI
        self.front_end = False      # ISS_FRONT_END
        self.in_game = False        # ISS_GAME
        self.screen = SCREEN_MAIN_MENU
        self.time_menu_opened = time.time()
        self._seen_sta = False
        self.connector.event_bus.subscribe('game_state_changed', self.insim_state)
        self.connector.event_bus.subscribe('interface_mode_changed', self.insim_interface_mode)

    # ─── IS_STA ───────────────────────────────────────────────────────

    def insim_state(self, sta):
        """
        this method receives the is_sta packet from LFS. It contains information about the game state, that will
        be read and saved inside "flags" variable.
        """
        logger.debug("IS_STA: Flags=%s InGameCam=%s Track=%r",
                     getattr(sta, 'Flags', None), getattr(sta, 'InGameCam', None),
                     getattr(sta, 'Track', None))

        # Use proper bitwise operations for flag parsing
        flags_raw = _as_int(getattr(sta, 'Flags', 0))
        self.in_game_cam = _as_int(getattr(sta, 'InGameCam', 0))

        self.in_game = bool(flags_raw & pyinsim.ISS_GAME)
        self.front_end = bool(flags_raw & pyinsim.ISS_FRONT_END)
        self.text_entry = bool(flags_raw & pyinsim.ISS_TEXT_ENTRY)
        self.dialog = bool(flags_raw & pyinsim.ISS_DIALOG)
        self.ui_visible = bool(flags_raw & pyinsim.ISS_VISIBLE)
        self.shift_u = bool(flags_raw & pyinsim.ISS_SHIFTU)
        self.multiplayer = bool(flags_raw & pyinsim.ISS_MULTI)
        self.track = _decode_track(getattr(sta, 'Track', b''))

        # on_track is True when ISS_GAME is set AND ISS_FRONT_END (entry screen) is NOT set.
        # ISS_DIALOG (in-game options menu) must NOT affect on_track.
        game = self.in_game and not self.front_end

        if not self.on_track and game:
            self._start_game_insim()
        elif self.on_track and not game:
            self._start_menu_insim()

        self._seen_sta = True
        self._publish()

    def _start_game_insim(self):
        self.on_track = True
        if time.time() - self.time_menu_opened >= OUTGAUGE_REINIT_AFTER_S:
            self.connector.start_outgauge()
        insim = getattr(self.connector, 'insim', None)
        if insim is None:
            return
        try:
            insim.send(pyinsim.ISP_TINY, ReqI=255, SubT=pyinsim.TINY_NPL)
        except Exception as e:
            logger.warning("Requesting the player list failed: %s: %s",
                           type(e).__name__, e)

    def _start_menu_insim(self):
        self.time_menu_opened = time.time()
        self.on_track = False

    # ─── IS_CIM ───────────────────────────────────────────────────────

    def insim_interface_mode(self, cim):
        """Fuellt in_game_interface/submode_interface aus IS_CIM

        LFS schickt IS_CIM ohne ISF_*-Flag, sobald sich der Interface-Modus
        der lokalen Verbindung aendert. Vorher blieben beide Felder auf 0 -
        die App konnte Hauptmenue, Garage, Optionen und Streckenwahl nicht
        auseinanderhalten.
        """
        self.in_game_interface = _as_int(getattr(cim, 'Mode', 0))
        self.submode_interface = _as_int(getattr(cim, 'SubMode', 0))
        self.select_type = _as_int(getattr(cim, 'SelType', 0))
        logger.debug("IS_CIM: Mode=%s SubMode=%s SelType=%s",
                     self.in_game_interface, self.submode_interface, self.select_type)
        if self._seen_sta:
            self._publish()

    # ─── Ableitung und Veroeffentlichung ──────────────────────────────

    def _derive_screen(self) -> str:
        """Ordnet Flags und Interface-Modus einem der Kontexte zu"""
        if not self.in_game and not self.front_end:
            # Weder Rennen noch Einstiegsbildschirm: Hauptmenue oder
            # Serverliste. LFS zeigt hier keine Buttons an.
            return SCREEN_MAIN_MENU
        if self.in_game_interface in _CIM_MENU_MODES:
            return SCREEN_OPTIONS
        if self.shift_u or self.in_game_interface == pyinsim.CIM_SHIFTU:
            return SCREEN_SHIFTU
        if self.in_game_interface == pyinsim.CIM_GARAGE:
            return SCREEN_GARAGE
        if self.front_end:
            return SCREEN_ENTRY
        return SCREEN_GAME

    def _publish(self):
        self.screen = self._derive_screen()
        state_data = {
            'on_track': self.on_track,
            'text_entry': self.text_entry,
            'dialog': self.dialog,
            'track': self.track,
            'in_game_cam': self.in_game_cam,
            'in_game_interface': self.in_game_interface,
            'submode_interface': self.submode_interface,
            # ── ab WP5 ──
            'select_type': self.select_type,
            'screen': self.screen,
            'ui_visible': self.ui_visible,
            'shift_u': self.shift_u,
            'multiplayer': self.multiplayer,
            # Einziges Signal, das die UI auswerten muss: darf hier ueberhaupt
            # etwas gezeichnet werden? (reference/ui.md §1.1)
            'buttons_allowed': self.screen not in _SCREENS_WITHOUT_BUTTONS,
        }
        self.connector.event_bus.emit('state_data', state_data)
