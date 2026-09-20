# ui/ui_manager.py
import logging
import time
from collections import deque
from typing import Dict, Tuple

import pyinsim
from core.event_bus import EventBus
from core.settings_manager import SettingsManager
from lfs.lfs_state import SCREEN_ENTRY
from lfs.message_sender import MessageSender
from misc.input_guard import OUTGAUGE_STALE_AFTER_S
from misc.pdc_beep import PDCBeepController

logger = logging.getLogger(__name__)


# ─── Button-ID-Karte (reference/ui.md §2) ─────────────────────────────────
# ClickIDs sind global und kollidieren still. Diese Konstanten sind die
# einzige Quelle - der frueher hier stehende Kommentarblock war bereits
# falsch: ID 1 war gleichzeitig die HUD-Geschwindigkeit und der
# Off-Track-Banner, also hat das Verlassen der Strecke den HUD-Button
# ueberschrieben und das Zurueckkehren den Banner geloescht.
BTN_HUD_SPEED = 1
BTN_HUD_RPM = 2
BTN_HUD_GEAR = 3
BTN_IDLE_BANNER = 4          # eigener Slot, frueher 1
HUD_RANGE = (1, 10)
BTN_BSW_LEFT = 13
BTN_BSW_RIGHT = 14
# 15 liegt zwischen den Warnanzeigen (11-14) und dem Menue (ab 20) und war
# als einzige ID dort noch frei: der Interventionsanzeiger gehoert genau in
# diese Gruppe.
BTN_EMERGENCY_BRAKE = 15
# 16-17: die Getriebekalibrierung. Eigener Slot, weil sie eine zeitkritische,
# modale Prozedur ist - ueber die Meldungsschlange (61) gelesen hing ihre
# Aufforderung dem gemessenen Schritt hinterher (reference/ui.md §1.7).
# Konfigurationswarnung "OutGauge schweigt" (known-issues #24). Eigener
# Slot, weil sie neben allem anderen stehen muss und keine Meldung ist.
BTN_OUTGAUGE_WARNING = 18
BTN_CALIBRATION_PROMPT = 16
BTN_CALIBRATION_STATUS = 17
CALIBRATION_RANGE = (16, 17)
MENU_RANGE = (20, 40)        # gehoert MenuSystem
BTN_PDC_FIRST = 41
BTN_PDC_LABEL = 60
PDC_RANGE = (41, 60)
BTN_NOTIFICATION = 61
BTN_SIREN = 62
BTN_STROBE = 63
# Selbst-Einparken (assistance/park_assist.py). Drei eigene Slots, weil das
# Angebot *anklickbar* sein muss und deshalb weder in die Meldungszeile noch
# in das Menue passt: die Meldungszeile zeigt eine Zeile nach der anderen und
# das Menue ist beim Rangieren zu. Die Klicks nimmt ParkAssist selbst entgegen.
BTN_PARK_OFFER = 64
BTN_PARK_CANCEL = 65
BTN_PARK_STATUS = 66
PARK_RANGE = (64, 66)
BTN_DEBUG_DECEL = 100
BTN_DEBUG_DIST = 101
ALL_BUTTONS_RANGE = (0, 239)

# ─── HUD-Geometrie ────────────────────────────────────────────────────────
# LFS-Button-Koordinaten laufen von 0 bis 200 in beiden Achsen.
SCREEN_MAX = 200

# Alle HUD-Elemente liegen relativ zu (hud_width, hud_height). Die Box ist
# die Vereinigung ihrer Rechtecke - sie bestimmt, wie weit die Position
# verschoben werden darf, ohne dass etwas vom Schirm faellt:
#   PDC vorne      (x-3 … x,      y-6 … y+2)
#   Sirene/Strobe  (x   … x+13,   y-5 … y)
#   Speed/RPM/Gang (x   … x+29,   y   … y+8)
#   PDC hinten     (x-3 … x,      y+2 … y+8)
#   Notification / AEB-Anzeige (x … x+26, y+8 … y+13) - ein Slot, siehe
#                  NOTIFICATION_SLOT und UIManager.notification_slot()
HUD_BOX_LEFT = -3
HUD_BOX_RIGHT = 29
HUD_BOX_TOP = -6
HUD_BOX_BOTTOM = 13

# InSim.txt: Buttons in diesem Rechteck lassen LFS den Bereich fuer die
# eigene UI freiraeumen (reference/ui.md §1.3, known-issues #27).
RESERVED_LEFT = 0
RESERVED_RIGHT = 110
RESERVED_TOP = 30
RESERVED_BOTTOM = 170

# Warnblinken: eine einzige Uhr statt "einmal pro update_hud umschalten".
# Vorher haing die Blinkfrequenz an ui_refresh_rate, und Stufe 2 blinkte
# ueberhaupt nicht, weil derselbe Durchlauf den Wechsel direkt wieder
# zuruecksetzte.
WARNING_BLINK_INTERVAL_S = 0.25

# Meldungsslot: die Zeile direkt unter dem HUD, als (dx, dy, Breite, Hoehe)
# relativ zur HUD-Position. Notification und Notbrems-Anzeiger teilen sich
# diesen Platz - beides sind kurze Zeilen, und unten mittig unter dem HUD ist
# die Stelle, an der der Fahrer sie ohne Blicksprung liest.
NOTIFICATION_SLOT = (0, 8, 26, 5)
# Kalibrierfeld: absolute Bildschirmkoordinaten, mittig ueber der Bildmitte.
# Nicht relativ zum HUD, weil der Fahrer es waehrend der Prozedur ansehen
# soll und der HUD-Platz frei konfigurierbar ist.
# Konfigurationswarnung: absolut, ueber dem Kalibrierfeld. Sie meldet einen
# Fehler in der LFS-Konfiguration, keinen Fahrzustand, und gehoert deshalb
# nicht an das frei verschiebbare HUD.
OUTGAUGE_WARNING_SLOT = (55, 32, 90, 6)
CALIBRATION_SLOT_PROMPT = (55, 40, 90, 7)
CALIBRATION_SLOT_STATUS = (55, 47, 90, 6)

# Einparkhilfe: ueber der Bildmitte, wo der Fahrer beim Rangieren ohnehin
# hinsieht, und deutlich neben HUD (ab x 87) und Menue (0-50 x, 70-120 y).
# Absolute Koordinaten wie beim Kalibrierfeld: der Fahrer soll es ansehen, und
# der HUD-Platz ist frei verschiebbar.
PARK_SLOT_OFFER = (60, 150, 46, 7)
PARK_SLOT_CANCEL = (106, 150, 16, 7)
PARK_SLOT_STATUS = (60, 157, 62, 6)

# Was die drei Zustaende auf dem Schirm sagen. Nur ``offered`` ist anklickbar -
# ein Angebot wird *bestaetigt*, nie automatisch ausgefuehrt
# (reference/control-intervention.md).
PARK_KIND_TEXTS = {'parallel': "parallel", 'perpendicular': "bay"}
PARK_SIDE_TEXTS = {'left': "left", 'right': "right"}

# Notbremseingriff: im Meldungsslot, direkt unter dem Tacho.
# Bewusst nicht blinkend: ein Eingriff dauert oft unter einer Sekunde, ein
# blinkendes Feld kann genau dann dunkel sein, wenn der Fahrer hinsieht.
EMERGENCY_BRAKE_TEXT = "^1!! BRAKE !!"

# Toter Winkel, drei Stufen (assistance/blind_spot_warning.py). Stufe 1 ist
# die Anzeige "da ist jemand", Stufe 2 die Akutwarnung (blinkend, mit Ton),
# Stufe 3 der Bremseingriff. Die Zeichen werden breiter statt nur roter: ein
# Feld von 10x10 in der Peripherie wird ueber die Form gelesen, nicht ueber
# die Farbe.
BSW_TEXTS = {1: "^3!", 2: "^1!!", 3: "^1!!!"}
# Wie oft die Akutwarnung toent, solange sie steht. ``fcw.wav`` faellt aus:
# ``AudioPlayer`` unterdrueckt dessen Wiederholung 3 s lang (es ist der
# einmalige Gong der Kollisionswarnung). ``warning_3`` ist 0.88 s lang, also
# ist 1.0 s die kuerzeste Wiederholung ohne Ueberlappung.
BSW_ACUTE_AUDIO = 'warning_3'
BSW_ACUTE_BEEP_INTERVAL_S = 1.0
# Wie oft der Gong der Kollisions- und der Querverkehrswarnung anschlaegt.
# Ein Ereignis bekommt genau einen Gong, keine direkte Wiederholung.
FCW_BEEPS = 1

# Notifications: eine Zeile fuer 3 s. Ohne Obergrenze staut eine Serie
# (z.B. die Getriebekalibrierung) minutenlang.
NOTIFICATION_DISPLAY_S = 3
MAX_QUEUED_NOTIFICATIONS = 8
# Eine Ueberlaufmeldung je so viele Sekunden (sie kam frueher pro Zyklus).
NOTIFICATION_OVERFLOW_LOG_S = 5.0
# So weit unter der gemessenen Hoechstdrehzahl faerbt sich die HUD-Drehzahl
# rot. Der Wert ist der, mit dem dieses Projekt frueher gearbeitet hat.
RED_ZONE_RPM = 1000

# ─── OutGauge-Stille ──────────────────────────────────────────────────────
# Ohne OutGauge stehen Tacho, Drehzahl, Gang und alle Pedalwerte still, und
# jeder Aktuator ist blind (known-issues #24/#51). Die Warnsysteme laufen
# weiter -- sie brauchen nur MCI -- weshalb das HUD *gesund aussah*, waehrend
# die halbe Anwendung nichts tat. Genau diese Haelfte von #24 schliesst die
# Warnung hier.
#
# Derselbe Schwellwert wie in ``misc/input_guard.py``: was den Bremseingriff
# verweigern laesst, soll auch auf dem Schirm stehen. Dort ist er die
# Wahrheit fuer die Aktuierung, hier fuer den Fahrer -- der Wert wird
# importiert, damit die beiden nicht auseinanderlaufen koennen.
OUTGAUGE_WARNING_TEXTS = {
    'port_in_use': "^1No OutGauge - port 30000 is taken",
    'open_failed': "^1No OutGauge - port 30000 is taken",
    'no_packets': "^1No OutGauge data - check OutGauge Mode in cfg.txt",
}
OUTGAUGE_WARNING_DEFAULT = "^1No OutGauge data"


def _as_int(value, default: int = 0) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def _as_float(value, default: float = 0.0) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def _clamp(value: int, low: int, high: int) -> int:
    if low > high:      # kann nur bei absurden Konstanten passieren
        return low
    return max(low, min(high, value))


def clamp_hud_position(x, y) -> Tuple[int, int]:
    """Haelt den gesamten HUD-Block auf dem Schirm

    Die Menue-Pfeile verschieben die Position in 2er-Schritten ohne jede
    Grenze; PDC-Block, Sirenen-Buttons und Notification haengen daran und
    landeten dabei ausserhalb von 0…200 (known-issues #27).
    """
    x = _clamp(_as_int(x), -HUD_BOX_LEFT, SCREEN_MAX - HUD_BOX_RIGHT)
    y = _clamp(_as_int(y), -HUD_BOX_TOP, SCREEN_MAX - HUD_BOX_BOTTOM)
    return x, y


def hud_overlaps_reserved_area(x, y) -> bool:
    """Liegt der HUD-Block im von LFS reservierten Rechteck?

    Dort raeumt LFS seine eigene UI weg. Das ist die Regel fuer **alles, was
    ausserhalb der Strecke gezeichnet wird** - auf dem Einstiegsbildschirm und
    in der Garage wuerden LFS-Menues verschwinden.

    Fuer den HUD selbst ist die Frage ohne Belang, und deshalb wird sie im
    Menue nicht mehr gestellt (known-issues #27): jedes HUD-Element haengt an
    ``UIManager.drawing``, und das Verlassen dieses Zustands raeumt den
    gesamten Button-Bereich ab. Der HUD existiert auf genau den Bildschirmen
    nicht, auf denen dieses Rechteck etwas bedeutet.
    """
    x, y = clamp_hud_position(x, y)
    return (x + HUD_BOX_LEFT < RESERVED_RIGHT
            and x + HUD_BOX_RIGHT > RESERVED_LEFT
            and y + HUD_BOX_TOP < RESERVED_BOTTOM
            and y + HUD_BOX_BOTTOM > RESERVED_TOP)


class UIManager:
    """Verwaltet alle UI-Elemente und Menüs"""

    def __init__(self, event_bus: EventBus, message_sender: MessageSender,
                 settings: SettingsManager, car_profiles=None):
        self.event_bus = event_bus
        self.message_sender = message_sender
        self.settings = settings
        # Gelernte Fahrzeugprofile (vehicles/car_profiles.py); optional, ohne
        # sie bleibt die Drehzahlanzeige einfarbig.
        self.car_profiles = car_profiles
        self.active_elements: Dict[str, bool] = {}
        self.current_menu = None
        self.on_track = False
        # Laeuft gerade ein Replay? Seit known-issues #55 zeichnet das HUD
        # auch dort - LFS setzt ISS_VISIBLE, die Buttons sind also sichtbar,
        # und die Warnsysteme laufen mit (reference/ui.md §1.1).
        self.replay = False
        # ``on_track or replay``: der Zustand, in dem ueberhaupt etwas
        # Fahrzeugbezogenes gezeichnet wird. Jede Zeichenstelle fragt diesen
        # Wert, nicht ``on_track``.
        self.drawing = False
        self.screen = None
        self.buttons_allowed = False
        self.pdc_data = None
        # OutGauge-Ueberwachung (siehe OUTGAUGE_WARNING_TEXTS). ``None``
        # heisst "noch kein Paket" bzw. "noch keine Auskunft ueber den
        # Socket"; nur ein *gebundener*, aber stummer Socket wird gemeldet,
        # sonst warnte die App in der Sekunde vor dem ersten Paket.
        self._outgauge_seen_at = None
        self._outgauge_bound = None
        self._outgauge_reason = None
        # Seit wann der Strom faellig ist. Dieselbe Marke wie in
        # ``misc/input_guard.py``: im Menue schweigt OutGauge zu Recht, und
        # ohne sie stand die Warnung im ersten Frame nach dem
        # Streckeneintritt auf dem Schirm.
        self._drawing_since = None
        # Ob der Parkpieper toenen darf - siehe ``_update_pdc_beep``.
        self.pdc_beep_allowed = True
        self.notifications = deque(maxlen=MAX_QUEUED_NOTIFICATIONS)
        self.notification_time = 0.0
        # Ueberlauf-Buchhaltung: eine volle Warteschlange bedeutet, dass der
        # Nutzer eine bis zu MAX_QUEUED_NOTIFICATIONS * NOTIFICATION_DISPLAY_S
        # Sekunden alte Meldung liest - bei der Getriebekalibrierung also den
        # Schritt davor. Eine Zeile pro verworfener Meldung war dabei selbst
        # eine Flut und nannte nicht, *was* geflutet hat.
        self._dropped_count = 0
        self._dropped_logged_at = 0.0
        self.current_notification = None
        self.collision_warning_level = 0
        self.cross_traffic_warning_level = 0
        self.cross_traffic_warning_side = None
        self.blind_spot_left_level = 0
        self.blind_spot_right_level = 0
        self._blind_spot_beeped_at = 0.0
        self.hud_enabled = False
        self.emergency_brake_active = False
        # Zustand der Getriebekalibrierung, oder None wenn keine laeuft.
        self.calibration_state = None
        self.siren_active = False
        self.strobe_active = False
        self.siren_ui_visible = False

        # Blinkphase - genau ein Besitzer (Punkt 7 in WP5)
        self._blink_on = False
        self._blink_changed = time.perf_counter()

        # Event-Handler
        self.event_bus.subscribe('collision_warning_changed', self._update_collision_warning_display)
        self.event_bus.subscribe('cross_traffic_warning_changed', self._update_cross_traffic_warning_display)
        self.event_bus.subscribe('blind_spot_warning_changed', self._update_blind_spot_display)
        self.event_bus.subscribe('emergency_brake_changed', self._update_emergency_brake_display)
        self.event_bus.subscribe('outgauge_data', self._get_hud_data)
        self.event_bus.subscribe('outgauge_status', self._on_outgauge_status)
        self.event_bus.subscribe('state_data', self._state_change)
        self.event_bus.subscribe("pdc_changed", self._update_pdc)
        self.event_bus.subscribe("park_assist_changed", self._update_park_assist)
        self.event_bus.subscribe("pdc_beep_allowed", self._update_pdc_beep)
        self.event_bus.subscribe("notification", self._update_notifications)
        self.event_bus.subscribe("show_siren_ui", self._show_siren_ui)
        # Sirene/Strobe gehoeren LightAssists; hier wird nur gezeichnet, was
        # es meldet (WP9, known-issues #17). Der frueher hier haengende
        # button_clicked-Handler fuehrte eine zweite Kopie des Zustands, die
        # beim Umschalten per Chat-Befehl auseinanderlief.
        self.event_bus.subscribe("siren_state_changed", self._on_siren_state_changed)
        self.event_bus.subscribe("strobe_state_changed", self._on_strobe_state_changed)
        self.event_bus.subscribe("buttons_cleared", self._on_buttons_cleared)
        self.event_bus.subscribe("gearbox_calibration_state",
                                 self._update_calibration_panel)
        #self.event_bus.subscribe("decel_debug", self._decel_debug)
        #self.event_bus.subscribe("dist_debug", self._dist_debug)

        # Zustand der Einparkhilfe, oder None wenn sie nichts zu sagen hat.
        self.park_state = None

        self.speed = 0
        self.rpm = 0
        self.gear = 'N'
        self.shift_light = False
        self.pdc_beeper = PDCBeepController(self.event_bus)

    # ─── Position ─────────────────────────────────────────────────────

    def hud_origin(self) -> Tuple[int, int]:
        """Geklemmte HUD-Position - einziger Zugriff auf die Einstellung"""
        return clamp_hud_position(self.settings.get("hud_width"),
                                  self.settings.get("hud_height"))

    def notification_slot(self) -> Tuple[int, int, int, int]:
        """(x, y, w, h) der Meldungszeile unter dem HUD

        Einzige Quelle dieser Geometrie: Notification und Notbrems-Anzeiger
        zeichnen beide genau hierhin (NOTIFICATION_SLOT).
        """
        hud_x, hud_y = self.hud_origin()
        dx, dy, width, height = NOTIFICATION_SLOT
        return hud_x + dx, hud_y + dy, width, height

    # ─── Sirene / Stroboskop (nur Darstellung) ────────────────────────

    def _on_siren_state_changed(self, data):
        """Reine Anzeige - den Zustand besitzt LightAssists"""
        self.siren_active = bool(data.get('siren_active', False)) if isinstance(data, dict) else False
        if self.siren_ui_visible:
            self._update_siren_buttons()

    def _on_strobe_state_changed(self, data):
        """Reine Anzeige - den Zustand besitzt LightAssists"""
        self.strobe_active = bool(data.get('strobe_active', False)) if isinstance(data, dict) else False
        if self.siren_ui_visible:
            self._update_siren_buttons()

    def _show_siren_ui(self, data):
        ui = data.get('ui') if isinstance(data, dict) else None
        if ui:
            self.siren_ui_visible = True
            self._update_siren_buttons()
        else:
            self.siren_ui_visible = False
            self.message_sender.remove_button(BTN_SIREN)
            self.message_sender.remove_button(BTN_STROBE)

    def _update_siren_buttons(self):
        """Aktualisiert Position und Zustand der Siren/Strobe-Buttons basierend auf HUD-Position."""
        siren_text = "^7Siren" if not self.siren_active else "^4Siren"
        strobe_text = "^7Strobe" if not self.strobe_active else "^4Strobe"
        hud_x, hud_y = self.hud_origin()
        self.message_sender.create_button(BTN_SIREN, hud_x, hud_y - 5, 6, 5, siren_text,
                                          pyinsim.ISB_DARK | pyinsim.ISB_CLICK)
        self.message_sender.create_button(BTN_STROBE, hud_x + 6, hud_y - 5, 7, 5, strobe_text,
                                          pyinsim.ISB_DARK | pyinsim.ISB_CLICK)

    def _decel_debug(self, data):
        hud_x, hud_y = self.hud_origin()
        decel = data['deceleration']
        self.message_sender.create_button(BTN_DEBUG_DECEL, hud_x, hud_y - 10, 20, 5,
                                          f"Decel: {decel:.2f} m/s²", pyinsim.ISB_DARK)

    def _dist_debug(self, data):
        hud_x, hud_y = self.hud_origin()
        distance = data['distance']
        self.message_sender.create_button(BTN_DEBUG_DIST, hud_x, hud_y - 15, 20, 5,
                                          f"Distance: {distance:.2f} m", pyinsim.ISB_DARK)

    # ─── Getriebekalibrierung ─────────────────────────────────────────

    def _update_calibration_panel(self, data):
        """Nimmt den Zustand entgegen - gezeichnet wird im UI-Durchlauf

        Das Ereignis kommt vom Assistenzthread, gezeichnet wird auf dem
        UI-Thread: hier wird nur abgelegt.
        """
        if not isinstance(data, dict) or not data.get('active'):
            self.calibration_state = None
            self.message_sender.remove_range(*CALIBRATION_RANGE)
            return
        self.calibration_state = data

    def _draw_calibration_panel(self):
        """Aufforderung, Restzeit und der bisher gemessene Wert

        Zwei Zeilen ueber der Bildmitte, ausserhalb von HUD (ab x 87), Menue
        (0-50 x, 70-120 y) und Meldungszeile. Die Restzeit steht in ganzen
        Sekunden, der Text aendert sich also einmal pro Sekunde und die
        Button-Registry unterdrueckt alles dazwischen.
        """
        state = self.calibration_state
        if state is None:
            return
        if not self.buttons_allowed:
            return
        remaining = int(state.get('remaining', 0) + 0.5)
        self.message_sender.create_button(
            BTN_CALIBRATION_PROMPT, *CALIBRATION_SLOT_PROMPT,
            f"^3{state.get('prompt', '')}", pyinsim.ISB_DARK)
        self.message_sender.create_button(
            BTN_CALIBRATION_STATUS, *CALIBRATION_SLOT_STATUS,
            f"^7{state.get('reading', '')}  ^3{remaining} s", pyinsim.ISB_DARK)

    # ─── Notifications ────────────────────────────────────────────────

    def _update_notifications(self, data):
        text = data.get('notification') if isinstance(data, dict) else None
        if not text:
            return
        if len(self.notifications) == self.notifications.maxlen:
            # deque wirft von selbst das aelteste weg - das aber melden,
            # sonst verschwindet eine Meldung spurlos.
            self._report_dropped(self.notifications[0], text)
        self.notifications.append(text)

    def _report_dropped(self, dropped: str, incoming: str):
        """Meldet den Ueberlauf ratenbegrenzt und mit Text

        Wer flutet, steht im Text - ohne ihn war die Warnung nicht auswertbar.
        Eine Zeile pro NOTIFICATION_OVERFLOW_LOG_S, mit der Anzahl seither.
        """
        self._dropped_count += 1
        now = time.perf_counter()
        if now - self._dropped_logged_at < NOTIFICATION_OVERFLOW_LOG_S:
            return
        self._dropped_logged_at = now
        logger.warning("Notification queue full - %d dropped since the last "
                       "report. Dropped now: %r, incoming: %r",
                       self._dropped_count, dropped, incoming)
        self._dropped_count = 0

    def show_notifications(self):
        """Zeigt eine Meldung fuer NOTIFICATION_DISPLAY_S Sekunden

        Wird jeden UI-Durchlauf aufgerufen und zeichnet die aktuelle Meldung
        jedes Mal neu. Die Button-Registry unterdrueckt den Wiederholfall,
        es geht also nichts zusaetzlich raus - dafuer kommt die Zeile nach
        einem SHIFT+B von selbst zurueck.
        """
        if self.emergency_brake_active:
            # Der Anzeiger hat den Slot (_draw_emergency_brake) und behaelt
            # ihn: eine Meldung kann drei Sekunden warten, eine laufende
            # Notbremsung nicht.
            return
        now = time.perf_counter()
        if now - self.notification_time >= NOTIFICATION_DISPLAY_S:
            self.current_notification = (self.notifications.popleft()
                                         if self.notifications else None)
            self.notification_time = now

        if self.current_notification is None:
            self.message_sender.remove_button(BTN_NOTIFICATION)
            return
        x, y, width, height = self.notification_slot()
        self.message_sender.create_button(BTN_NOTIFICATION, x, y, width, height,
                                          self.current_notification, pyinsim.ISB_DARK)

    def clear_notifications(self):
        self.notifications.clear()
        self.current_notification = None
        self.notification_time = 0.0

    # ─── PDC ──────────────────────────────────────────────────────────

    def _update_pdc(self, data):
        """Aktualisiert Park Distance Control (PDC) Anzeige"""
        self.pdc_data = data
        if not isinstance(data, dict) or data.get(0, -1) == -1:
            self.remove_pdc_display()

    def _update_pdc_beep(self, data):
        """Der Parkpieper verstummt im Stillstand (park_distance_control.py).

        Nur der Ton - die Anzeige bleibt stehen, denn dass da noch etwas ist,
        aendert sich durch das Anhalten nicht.
        """
        if isinstance(data, dict):
            self.pdc_beep_allowed = bool(data.get('allowed', True))

    def remove_pdc_display(self):
        self.message_sender.remove_range(*PDC_RANGE)

    def _show_pdc_display(self):
        mode = _as_int(self.settings.get("park_distance_control_mode", 0))
        if mode > 0:
            hud_x, hud_y = self.hud_origin()
            top_left = (hud_x - 3, hud_y - 6)
            bottom_left = (hud_x - 3, hud_y + 6)
            self.message_sender.create_button(BTN_PDC_LABEL, top_left[0], top_left[1] + 6,
                                              3, 2, "^7PDC", pyinsim.ISB_DARK)
            # create buttons for each PDC sensor
            for i, distance in enumerate(self.pdc_data.values()):
                if i < 3:  # Front sensors (0, 1, 2)
                    # Green button (furthest distance)
                    if distance >= 1:
                        self.message_sender.create_button(41 + i, top_left[0] + i, top_left[1],
                                                          1, 2, "^2o", pyinsim.ISB_DARK)
                    else:
                        self.message_sender.remove_button(41 + i)

                    # Yellow button (medium distance)
                    if distance >= 2:
                        self.message_sender.create_button(44 + i, top_left[0] + i, top_left[1] + 2,
                                                          1, 2, "^3o", pyinsim.ISB_DARK)
                    else:
                        self.message_sender.remove_button(44 + i)

                    # Red button (closest distance)
                    if distance >= 3:
                        self.message_sender.create_button(47 + i, top_left[0] + i, top_left[1] + 4,
                                                          1, 2, "^1o", pyinsim.ISB_DARK)
                    else:
                        self.message_sender.remove_button(47 + i)

                else:  # Rear sensors (3, 4, 5)
                    x = i - 3  # Correct offset for rear sensors

                    # Green button (furthest distance) - bottom position for rear
                    if distance >= 1:
                        self.message_sender.create_button(51 + x, bottom_left[0] + x, bottom_left[1],
                                                          1, 2, "^2o", pyinsim.ISB_DARK)
                    else:
                        self.message_sender.remove_button(51 + x)

                    # Yellow button (medium distance)
                    if distance >= 2:
                        self.message_sender.create_button(54 + x, bottom_left[0] + x, bottom_left[1] - 2,
                                                          1, 2, "^3o", pyinsim.ISB_DARK)
                    else:
                        self.message_sender.remove_button(54 + x)

                    # Red button (closest distance) - top position for rear
                    if distance >= 3:
                        self.message_sender.create_button(57 + x, bottom_left[0] + x, bottom_left[1] - 4,
                                                          1, 2, "^1o", pyinsim.ISB_DARK)
                    else:
                        self.message_sender.remove_button(57 + x)
        if mode == 2 and self.pdc_beep_allowed:
            self.pdc_beeper.beep()

    # ─── Einparkhilfe ─────────────────────────────────────────────────

    def _update_park_assist(self, data):
        """Nimmt den Zustand entgegen - gezeichnet wird im UI-Durchlauf.

        Das Ereignis kommt vom Assistenzthread, gezeichnet wird auf dem
        UI-Thread; hier wird nur abgelegt. Genau wie bei der
        Getriebekalibrierung.
        """
        self.park_state = data if isinstance(data, dict) else None

    def _draw_park_assist(self):
        """Angebot, Abbruch und Fortschritt des Selbst-Einparkens.

        Unabhaengig von ``hud_active``, wie der Notbrems-Anzeiger: das ist
        keine Anzeige, sondern die Bedienung eines Eingriffs, und ein Fahrer,
        der den HUD abgeschaltet hat, soll trotzdem abbrechen koennen
        (reference/ui.md §1.5). Jeden Durchlauf neu gezeichnet, damit es nach
        SHIFT+B von selbst wiederkommt; die Button-Registry unterdrueckt die
        Wiederholung.
        """
        state = self.park_state or {}
        name = state.get('state')
        if not (self.drawing and self.buttons_allowed) or name in (None, 'off'):
            self.message_sender.remove_range(*PARK_RANGE)
            return

        if name == 'offered':
            kind = PARK_KIND_TEXTS.get(state.get('kind'), "space")
            side = PARK_SIDE_TEXTS.get(state.get('side'), "")
            self.message_sender.create_button(
                BTN_PARK_OFFER, *PARK_SLOT_OFFER,
                f"^3Click: park here ^7({kind} {side}, "
                f"{state.get('length', 0)} m)",
                pyinsim.ISB_DARK | pyinsim.ISB_CLICK)
            self.message_sender.create_button(
                BTN_PARK_CANCEL, *PARK_SLOT_CANCEL, "^1No",
                pyinsim.ISB_DARK | pyinsim.ISB_CLICK)
            # Deliberately *not* an invitation to click: this line is not
            # clickable and the offer above it is. The first live test had
            # "click to let the car park itself" here, and the driver clicked
            # it -- reasonably -- and nothing happened.
            strokes = state.get('strokes', 0)
            self.message_sender.create_button(
                BTN_PARK_STATUS, *PARK_SLOT_STATUS,
                f"^7{state.get('distance', 0)} m behind you, {strokes} move(s)",
                pyinsim.ISB_DARK)
            return

        self.message_sender.remove_button(BTN_PARK_OFFER)
        if name == 'parking':
            self.message_sender.create_button(
                BTN_PARK_CANCEL, *PARK_SLOT_CANCEL, "^1Stop",
                pyinsim.ISB_LIGHT | pyinsim.ISB_CLICK)
            percent = int(round(state.get('progress', 0.0) * 100))
            self.message_sender.create_button(
                BTN_PARK_STATUS, *PARK_SLOT_STATUS,
                f"^3Parking - move {state.get('stroke', 1)}/"
                f"{state.get('strokes', 1)}, {percent} %", pyinsim.ISB_LIGHT)
            return

        self.message_sender.remove_button(BTN_PARK_CANCEL)
        if name == 'done':
            self.message_sender.create_button(BTN_PARK_STATUS,
                                              *PARK_SLOT_STATUS, "^2Parked.",
                                              pyinsim.ISB_DARK)
        else:
            # 'scanning' und 'aborted' brauchen keine stehende Zeile: das eine
            # ist der Normalzustand, das andere hat schon eine Meldung erzeugt.
            self.message_sender.remove_button(BTN_PARK_STATUS)

    # ─── Bildschirmwechsel ────────────────────────────────────────────

    def _state_change(self, data):
        """Reagiert auf den Bildschirm-Kontext aus lfs/lfs_state.py

        Vorher wurde bei jedem einzelnen state_data-Event, solange nicht
        on_track galt, der komplette Button-Bereich geloescht und der Banner
        neu gezeichnet - auch im Hauptmenue und in der Serverliste, wo laut
        reference/ui.md §1.1 nichts gezeichnet werden darf.
        """
        on_track = bool(data.get('on_track', False))
        replay = bool(data.get('replay', False))
        screen = data.get('screen')
        self.buttons_allowed = bool(data.get('buttons_allowed', True))

        if (on_track == self.on_track and replay == self.replay
                and screen == self.screen):
            return
        # Nicht ``was_on_track``: ein beendetes Replay verlaesst keine
        # Strecke, laesst aber genau dieselben Buttons stehen. Vorher blieben
        # sie im Hauptmenue haengen, weil der Aufraeumpfad an on_track hing.
        was_drawing = self.drawing
        self.on_track = on_track
        self.replay = replay
        self.drawing = on_track or replay
        if self.drawing != was_drawing:
            self._drawing_since = time.time() if self.drawing else None
        self.screen = screen

        if self.drawing:
            if not was_drawing:
                self.message_sender.remove_button(BTN_IDLE_BANNER)
            return

        if was_drawing:
            self._reset_on_leaving_track()
        self._draw_idle_screen()

    def _reset_on_leaving_track(self):
        self.message_sender.remove_range(*ALL_BUTTONS_RANGE)
        self.siren_active = False
        self.strobe_active = False
        self.siren_ui_visible = False
        self.pdc_data = None
        self.pdc_beep_allowed = True
        self.collision_warning_level = 0
        self.cross_traffic_warning_level = 0
        self.cross_traffic_warning_side = None
        self.blind_spot_left_level = 0
        self.blind_spot_right_level = 0
        self.emergency_brake_active = False
        self.current_menu = None
        self.calibration_state = None
        self.park_state = None
        self.clear_notifications()

    def _draw_idle_screen(self):
        """Der Banner gehoert auf den Einstiegsbildschirm - sonst nirgends"""
        if self.screen == SCREEN_ENTRY:
            self.message_sender.create_button(BTN_IDLE_BANNER, 0, 180, 25, 5,
                                              "PACT Driving Assist Active.",
                                              pyinsim.ISB_DARK)
        else:
            self.message_sender.remove_button(BTN_IDLE_BANNER)

    def _on_buttons_cleared(self, data=None):
        """SHIFT+B: LFS hat unsere Buttons geworfen (reference/ui.md §1.5)

        Der MessageSender hat seine Registry schon verworfen, alles was
        zyklisch gezeichnet wird (HUD, PDC, Sirene, Notification) kommt also
        von selbst zurueck. Der Banner wird nur bei Zustandswechseln
        gezeichnet und braucht diesen Anstoss.
        """
        if not self.drawing:
            self._draw_idle_screen()

    # ─── HUD ──────────────────────────────────────────────────────────

    def _on_outgauge_status(self, data):
        """Hat der OutGauge-Socket gebunden? (``lfs/connector.py``)"""
        if not isinstance(data, dict):
            return
        self._outgauge_bound = bool(data.get('bound', False))
        self._outgauge_reason = data.get('reason')
        self._outgauge_seen_at = None

    def _outgauge_warning(self):
        """Der Text fuer die OutGauge-Warnung, oder ``None``.

        Kosten: ein Vergleich pro UI-Durchlauf.
        """
        if self._outgauge_bound is None:
            return None            # niemand hat etwas gesagt - nicht raten
        if self._outgauge_bound is False:
            return OUTGAUGE_WARNING_TEXTS.get(self._outgauge_reason,
                                              OUTGAUGE_WARNING_DEFAULT)
        since = self._outgauge_seen_at
        due = self._drawing_since
        if since is None or (due is not None and due > since):
            since = due
        if since is None:
            return None            # gebunden, erstes Paket steht noch aus
        if time.time() - since <= OUTGAUGE_STALE_AFTER_S:
            return None
        return OUTGAUGE_WARNING_TEXTS['no_packets']

    def _draw_outgauge_warning(self):
        """Sagt dem Fahrer, dass die halbe Anwendung blind ist.

        Bewusst keine ``notification``: die Warteschlange zeigt eine Meldung
        3 s lang und ist fuer alles Periodische strukturell ungeeignet
        (reference/ui.md §1.7). Das hier ist ein Dauerzustand.
        """
        text = self._outgauge_warning()
        if text is None or not self.buttons_allowed:
            self.message_sender.remove_button(BTN_OUTGAUGE_WARNING)
            return
        x, y, width, height = OUTGAUGE_WARNING_SLOT
        self.message_sender.create_button(BTN_OUTGAUGE_WARNING, x, y,
                                          width, height, text,
                                          pyinsim.ISB_DARK)

    def _get_hud_data(self, data):
        self._outgauge_seen_at = time.time()
        speed = _as_int(round(_as_float(getattr(data, 'Speed', 0.0)) * 3.6))
        self.speed = speed
        self.rpm = round(_as_float(getattr(data, 'RPM', 0.0)) / 1000, 1)
        # Rot ab Drehzahlgrenze: LFS' eigene Schaltleuchte. Vorher war die
        # "Redline" die hoechste je gesehene Drehzahl, der Wert wurde also
        # bei jedem neuen Maximum rot und blieb nach einem Fahrzeugwechsel
        # falsch stehen. ShowLights ist pro Auto richtig und funktioniert
        # auch fuer Mods (reference/conventions.md §4).
        # Rot ab kurz vor der Hoechstdrehzahl. Die Quelle dafuer ist die
        # *gemessene* Hoechstdrehzahl dieses Autos, nicht mehr LFS'
        # Schaltleuchte: ShowLights & DL_SHIFT ist nur in den Rennwagen
        # ueberhaupt aktiv, in den meisten Strassenautos also nie - dort wurde
        # die Anzeige nie rot (live geprueft, siehe vehicles/car_profiles.py).
        # Das Auto kommt aus *diesem* Paket, nicht aus einer zweiten Quelle:
        # bei einem Kamerawechsel gehoeren Drehzahl und Fahrzeugname sonst
        # fuer einen Takt nicht zusammen (conventions.md §5.2).
        self.shift_light = self._near_redline(getattr(data, 'Car', None),
                                              _as_float(getattr(data, 'RPM', 0.0)))
        gear = _as_int(getattr(data, 'Gear', 1), 1)
        self.gear = "R" if gear == 0 else "N" if gear == 1 else str(gear - 1)

    def _near_redline(self, car, rpm: float) -> bool:
        """Ist die Drehzahl nah genug an der Hoechstdrehzahl fuer Rot?

        ``RED_ZONE_RPM`` unterhalb des hoechsten je in diesem Auto gemessenen
        Werts. Solange nichts gemessen ist, bleibt die Anzeige weiss - lieber
        keine Warnfarbe als eine falsche.
        """
        if self.car_profiles is None or rpm <= 0:
            return False
        redline = self.car_profiles.redline(car)
        if not redline:
            return False
        return rpm >= redline - RED_ZONE_RPM

    def _advance_blink(self) -> bool:
        """Blinkphase - eine Uhr, unabhaengig von ui_refresh_rate"""
        now = time.perf_counter()
        if now - self._blink_changed >= WARNING_BLINK_INTERVAL_S:
            self._blink_changed = now
            self._blink_on = not self._blink_on
        return self._blink_on

    def update_hud(self):
        """Aktualisiert das Head-Up Display"""
        if not self.drawing:
            return
        # Der Notbrems-Anzeiger haengt nicht an hud_active: er meldet, dass
        # der Wagen gerade selbst bremst (control-intervention.md §4), und das
        # muss auch sichtbar sein, wenn der Fahrer die Anzeigen abgeschaltet
        # hat. Nur der Bildschirmkontext darf ihn unterdruecken. Er wird - wie
        # HUD, PDC und Sirene - jeden Durchlauf neu gezeichnet, die Registry
        # macht die Wiederholung frei und nach SHIFT+B kommt er von selbst
        # zurueck (reference/ui.md §1.5).
        self._draw_emergency_brake()
        # Eine Uhr fuer alles, was blinkt - auch fuer die Anzeigen, die vor
        # dem hud_active-Test gezeichnet werden.
        blink_on = self._advance_blink()
        self._draw_blind_spot(blink_on)
        if max(self.blind_spot_left_level, self.blind_spot_right_level) >= 2:
            self._blind_spot_beep()
        # Wie der Notbrems-Anzeiger unabhaengig von hud_active: eine laufende
        # Kalibrierung fordert den Fahrer gerade zu etwas auf, das sie in
        # 12 s misst. Jeden Durchlauf neu gezeichnet, damit sie nach SHIFT+B
        # von selbst wiederkommt (reference/ui.md §1.5).
        self._draw_calibration_panel()
        # Ebenfalls unabhaengig von ``hud_active``: das Angebot ist ein
        # Bedienelement, kein Anzeigewert.
        self._draw_park_assist()
        # Ebenfalls unabhaengig von ``hud_active``: sie erklaert, warum die
        # Anzeigen stehen, und waere ausgerechnet dann weg, wenn jemand sie
        # abgeschaltet hat (known-issues #24).
        self._draw_outgauge_warning()
        if not (self.settings.get('hud_active') and self.buttons_allowed):
            self.hide_hud()
            return

        hud_x, hud_y = self.hud_origin()

        speed_text = f"{self.speed} km/h" if self.settings.get(
            "unit") == "metric" else f"{round(self.speed * 0.621371)} mph "
        rpm_text = f"^1{self.rpm} rpm" if self.shift_light else f"{self.rpm} rpm"

        # Bestimme den aktiven Warnzustand (Frontkollision hat Priorität)
        hud_style = pyinsim.ISB_DARK

        if self.collision_warning_level > 0:
            speed_text = "^1- - -"
            rpm_text = "^1- - -"
            if self.collision_warning_level >= 2 and blink_on:
                hud_style = pyinsim.ISB_LIGHT
        elif self.cross_traffic_warning_level > 0:
            # Querverkehrswarnung (nur wenn keine Frontkollisionswarnung aktiv)
            ctw_symbol = "^1< < <" if self.cross_traffic_warning_side == 'right' else "^1> > >"
            speed_text = ctw_symbol
            rpm_text = ctw_symbol
            if self.cross_traffic_warning_level >= 2 and blink_on:
                hud_style = pyinsim.ISB_LIGHT

        self.message_sender.create_button(BTN_HUD_SPEED, hud_x, hud_y, 13, 8,
                                          speed_text, hud_style)
        self.message_sender.create_button(BTN_HUD_RPM, hud_x + 13, hud_y, 13, 8,
                                          rpm_text, hud_style)
        self.message_sender.create_button(BTN_HUD_GEAR, hud_x + 26, hud_y, 3, 4,
                                          f"{self.gear}", pyinsim.ISB_DARK)

        if isinstance(self.pdc_data, dict) and self.pdc_data.get(0, -1) != -1:
            self._show_pdc_display()

        if self.siren_ui_visible:
            self._update_siren_buttons()

        self.show_notifications()

    def hide_hud(self):
        """Versteckt das Head-Up Display"""
        self.message_sender.remove_range(*HUD_RANGE)

    # ─── Warnanzeigen ─────────────────────────────────────────────────

    def _update_collision_warning_display(self, data):
        """Aktualisiert Kollisionswarn-Anzeige"""
        warning_level = _as_int(data.get('level', 0)) if isinstance(data, dict) else 0
        if warning_level >= 2 > self.collision_warning_level:
            # Ein Gong pro Warnbeginn; AudioPlayer entprellt weitere Events.
            self.event_bus.emit('play_audio',
                                {'audio_file': 'fcw', 'repeat': FCW_BEEPS})

        self.collision_warning_level = warning_level

    def _update_cross_traffic_warning_display(self, data):
        """Aktualisiert Querverkehrswarn-Anzeige"""
        if not isinstance(data, dict):
            return
        warning_level = _as_int(data.get('level', 0))
        warning_side = data.get('side')

        if warning_level >= 2 > self.cross_traffic_warning_level:
            self.event_bus.emit('play_audio',
                                {'audio_file': 'fcw', 'repeat': FCW_BEEPS})

        self.cross_traffic_warning_level = warning_level
        self.cross_traffic_warning_side = warning_side

    def _update_emergency_brake_display(self, data):
        """Notbremseingriff sichtbar machen (control-intervention.md §4)

        Bewusst kein ``notification``: die Zeile wird eine nach der anderen
        fuer 3 s gezeigt, der Fahrer haette den Hinweis also erst nach dem
        Bremsvorgang gesehen. Der Anzeiger geht mit dem Eingriff an und aus.
        """
        self.emergency_brake_active = (bool(data.get('active', False))
                                       if isinstance(data, dict) else False)
        self._draw_emergency_brake()

    def _draw_emergency_brake(self):
        """Zeichnet oder entfernt den Interventionsanzeiger

        Steht im Meldungsslot (notification_slot) - dem Platz, den sich
        Anzeiger und Notification teilen. Solange der Eingriff laeuft,
        gewinnt der Anzeiger: er meldet, dass der Wagen gerade selbst
        bremst, eine Meldung kann warten (show_notifications haelt sich
        dann zurueck).
        """
        if not (self.emergency_brake_active and self.drawing
                and self.buttons_allowed):
            self.message_sender.remove_button(BTN_EMERGENCY_BRAKE)
            return
        # Auch auf dem Sofort-Pfad (Event, nicht UI-Durchlauf) darf keine
        # Meldung unter dem Anzeiger stehenbleiben.
        self.message_sender.remove_button(BTN_NOTIFICATION)
        x, y, width, height = self.notification_slot()
        self.message_sender.create_button(BTN_EMERGENCY_BRAKE, x, y, width, height,
                                          EMERGENCY_BRAKE_TEXT,
                                          pyinsim.ISB_LIGHT)

    def _update_blind_spot_display(self, data):
        """Uebernimmt den Toter-Winkel-Zustand (assistance/blind_spot_warning.py)

        Gezeichnet wird in ``_draw_blind_spot`` - einmal sofort, damit die
        Warnung nicht bis zum naechsten UI-Durchlauf wartet, und danach in
        jedem Durchlauf, weil Stufe 2 blinkt und ein Blinken eine Uhr braucht.

        ``left_level``/``right_level`` sind neu; ein Payload, der nur
        ``left``/``right`` traegt (aeltere Emitter, Tests), wird als Stufe 1
        gelesen.
        """
        if not isinstance(data, dict):
            return
        previous = max(self.blind_spot_left_level, self.blind_spot_right_level)
        self.blind_spot_left_level = _as_int(
            data.get('left_level', 1 if data.get('left') else 0))
        self.blind_spot_right_level = _as_int(
            data.get('right_level', 1 if data.get('right') else 0))
        # Der Ton gehoert an die *Flanke*: er soll beim Erreichen der
        # Akutstufe kommen, nicht erst beim naechsten Wiederholungstakt.
        if max(self.blind_spot_left_level,
               self.blind_spot_right_level) >= 2 > previous:
            self._blind_spot_beep()
        self._draw_blind_spot(self._blink_on)

    def _blind_spot_beep(self):
        """Wiederholter Warnton, solange die Akutstufe steht.

        **Ein** Taktgeber fuer beide Ausloeser, und keiner darf ihn umgehen.
        Die Flanke hatte frueher ein ``force``, damit der Ton nicht bis zum
        naechsten UI-Durchlauf wartet - nur ist eine Flanke nichts Seltenes:
        stehen mehrere Fahrzeuge in den Akutstufen, laufen die Haltezeiten
        beider Seiten versetzt ab, die Stufe faellt auf 0 und kommt sofort
        zurueck, und jede dieser Flanken schlug den Ton erneut an. Im
        50-ms-UI-Takt lagen so bis zu siebzehn Kopien eines 0.88-s-Samples
        uebereinander - das Rauschen und Knacken aus known-issues #54.

        Die Flanke behaelt, was sie wirklich braucht: sie loest *sofort* aus
        statt erst im naechsten Durchlauf. Sie loest nur nicht mehr
        *zusaetzlich* aus.
        """
        now = time.perf_counter()
        if now - self._blind_spot_beeped_at < BSW_ACUTE_BEEP_INTERVAL_S:
            return
        self._blind_spot_beeped_at = now
        self.event_bus.emit('play_audio', {'audio_file': BSW_ACUTE_AUDIO})

    def _draw_blind_spot(self, blink_on: bool):
        """Zeichnet die beiden Felder links und rechts neben dem HUD.

        Wie der Notbrems-Anzeiger unabhaengig von ``hud_active``: das ist eine
        Warnung, keine Anzeige. Der Bildschirmkontext darf sie unterdruecken,
        die HUD-Einstellung nicht (reference/ui.md §1.5).

        Vorher wurde ausschliesslich im Event gezeichnet, ohne
        ``buttons_allowed`` zu pruefen - eine Warnung, die waehrend eines
        LFS-Dialogs eintraf, landete auf einem Bildschirm, auf dem wir nichts
        zu suchen haben, und kam nach SHIFT+B nie von selbst zurueck.
        """
        _, hud_y = self.hud_origin()
        allowed = self.drawing and self.buttons_allowed
        for button, level, x in ((BTN_BSW_LEFT, self.blind_spot_left_level, 20),
                                 (BTN_BSW_RIGHT, self.blind_spot_right_level, 180)):
            if not allowed or level <= 0:
                self.message_sender.remove_button(button)
                continue
            # Stufe 2 blinkt, Stufe 3 steht: waehrend eines Bremseingriffs
            # darf das Feld nicht ausgerechnet dann dunkel sein, wenn der
            # Fahrer hinsieht - dieselbe Begruendung wie beim
            # Notbrems-Anzeiger.
            style = pyinsim.ISB_DARK
            if level >= 3 or (level == 2 and blink_on):
                style = pyinsim.ISB_LIGHT
            self.message_sender.create_button(button, x, hud_y, 10, 10,
                                              BSW_TEXTS[min(level, 3)], style)

