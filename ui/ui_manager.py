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
MENU_RANGE = (20, 40)        # gehoert MenuSystem
BTN_PDC_FIRST = 41
BTN_PDC_LABEL = 60
PDC_RANGE = (41, 60)
BTN_NOTIFICATION = 61
BTN_SIREN = 62
BTN_STROBE = 63
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
#   Notification   (x   … x+26,   y+8 … y+13)
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

# Notifications: eine Zeile fuer 3 s. Ohne Obergrenze staut eine Serie
# (z.B. die Getriebekalibrierung) minutenlang.
NOTIFICATION_DISPLAY_S = 3
MAX_QUEUED_NOTIFICATIONS = 8


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

    Dort raeumt LFS seine eigene UI weg - auf dem Einstiegsbildschirm und in
    der Garage verschwinden dann LFS-Menues. Die ausgelieferte Standard-
    position liegt in diesem Bereich, deshalb wird hier nur gewarnt und
    nicht verschoben (siehe reference/ui.md §1.3).
    """
    x, y = clamp_hud_position(x, y)
    return (x + HUD_BOX_LEFT < RESERVED_RIGHT
            and x + HUD_BOX_RIGHT > RESERVED_LEFT
            and y + HUD_BOX_TOP < RESERVED_BOTTOM
            and y + HUD_BOX_BOTTOM > RESERVED_TOP)


class UIManager:
    """Verwaltet alle UI-Elemente und Menüs"""

    def __init__(self, event_bus: EventBus, message_sender: MessageSender, settings: SettingsManager):
        self.event_bus = event_bus
        self.message_sender = message_sender
        self.settings = settings
        self.active_elements: Dict[str, bool] = {}
        self.current_menu = None
        self.on_track = False
        self.screen = None
        self.buttons_allowed = False
        self.pdc_data = None
        self.notifications = deque(maxlen=MAX_QUEUED_NOTIFICATIONS)
        self.notification_time = 0.0
        self.current_notification = None
        self.collision_warning_level = 0
        self.cross_traffic_warning_level = 0
        self.cross_traffic_warning_side = None
        self.hud_enabled = False
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
        self.event_bus.subscribe('outgauge_data', self._get_hud_data)
        self.event_bus.subscribe('state_data', self._state_change)
        self.event_bus.subscribe("pdc_changed", self._update_pdc)
        self.event_bus.subscribe("notification", self._update_notifications)
        self.event_bus.subscribe("send_lfs_command", self._handle_lfs_command)
        self.event_bus.subscribe("show_siren_ui", self._show_siren_ui)
        # Sirene/Strobe gehoeren LightAssists; hier wird nur gezeichnet, was
        # es meldet (WP9, known-issues #17). Der frueher hier haengende
        # button_clicked-Handler fuehrte eine zweite Kopie des Zustands, die
        # beim Umschalten per Chat-Befehl auseinanderlief.
        self.event_bus.subscribe("siren_state_changed", self._on_siren_state_changed)
        self.event_bus.subscribe("strobe_state_changed", self._on_strobe_state_changed)
        self.event_bus.subscribe("buttons_cleared", self._on_buttons_cleared)
        #self.event_bus.subscribe("decel_debug", self._decel_debug)
        #self.event_bus.subscribe("dist_debug", self._dist_debug)

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

    def _handle_lfs_command(self, data):
        command = data['command']
        self.message_sender.send_command(command)

    # ─── Notifications ────────────────────────────────────────────────

    def _update_notifications(self, data):
        text = data.get('notification') if isinstance(data, dict) else None
        if not text:
            return
        if len(self.notifications) == self.notifications.maxlen:
            # deque wirft von selbst das aelteste weg - das aber melden,
            # sonst verschwindet eine Meldung spurlos.
            logger.warning("Notification queue full - dropping the oldest entry.")
        self.notifications.append(text)

    def show_notifications(self):
        """Zeigt eine Meldung fuer NOTIFICATION_DISPLAY_S Sekunden

        Wird jeden UI-Durchlauf aufgerufen und zeichnet die aktuelle Meldung
        jedes Mal neu. Die Button-Registry unterdrueckt den Wiederholfall,
        es geht also nichts zusaetzlich raus - dafuer kommt die Zeile nach
        einem SHIFT+B von selbst zurueck.
        """
        now = time.perf_counter()
        if now - self.notification_time >= NOTIFICATION_DISPLAY_S:
            self.current_notification = (self.notifications.popleft()
                                         if self.notifications else None)
            self.notification_time = now

        if self.current_notification is None:
            self.message_sender.remove_button(BTN_NOTIFICATION)
            return
        hud_x, hud_y = self.hud_origin()
        self.message_sender.create_button(BTN_NOTIFICATION, hud_x, hud_y + 8, 26, 5,
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
        if mode == 2:
            self.pdc_beeper.beep()

    # ─── Bildschirmwechsel ────────────────────────────────────────────

    def _state_change(self, data):
        """Reagiert auf den Bildschirm-Kontext aus lfs/lfs_state.py

        Vorher wurde bei jedem einzelnen state_data-Event, solange nicht
        on_track galt, der komplette Button-Bereich geloescht und der Banner
        neu gezeichnet - auch im Hauptmenue und in der Serverliste, wo laut
        reference/ui.md §1.1 nichts gezeichnet werden darf.
        """
        on_track = bool(data.get('on_track', False))
        screen = data.get('screen')
        self.buttons_allowed = bool(data.get('buttons_allowed', True))

        if on_track == self.on_track and screen == self.screen:
            return
        was_on_track = self.on_track
        self.on_track = on_track
        self.screen = screen

        if on_track:
            if not was_on_track:
                self.message_sender.remove_button(BTN_IDLE_BANNER)
            return

        if was_on_track:
            self._reset_on_leaving_track()
        self._draw_idle_screen()

    def _reset_on_leaving_track(self):
        self.message_sender.remove_range(*ALL_BUTTONS_RANGE)
        self.siren_active = False
        self.strobe_active = False
        self.siren_ui_visible = False
        self.pdc_data = None
        self.collision_warning_level = 0
        self.cross_traffic_warning_level = 0
        self.cross_traffic_warning_side = None
        self.current_menu = None
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
        if not self.on_track:
            self._draw_idle_screen()

    # ─── HUD ──────────────────────────────────────────────────────────

    def _get_hud_data(self, data):
        speed = _as_int(round(_as_float(getattr(data, 'Speed', 0.0)) * 3.6))
        self.speed = speed
        self.rpm = round(_as_float(getattr(data, 'RPM', 0.0)) / 1000, 1)
        # Rot ab Drehzahlgrenze: LFS' eigene Schaltleuchte. Vorher war die
        # "Redline" die hoechste je gesehene Drehzahl, der Wert wurde also
        # bei jedem neuen Maximum rot und blieb nach einem Fahrzeugwechsel
        # falsch stehen. ShowLights ist pro Auto richtig und funktioniert
        # auch fuer Mods (reference/conventions.md §4).
        self.shift_light = bool(_as_int(getattr(data, 'ShowLights', 0)) & pyinsim.DL_SHIFT)
        gear = _as_int(getattr(data, 'Gear', 1), 1)
        self.gear = "R" if gear == 0 else "N" if gear == 1 else str(gear - 1)

    def _advance_blink(self) -> bool:
        """Blinkphase - eine Uhr, unabhaengig von ui_refresh_rate"""
        now = time.perf_counter()
        if now - self._blink_changed >= WARNING_BLINK_INTERVAL_S:
            self._blink_changed = now
            self._blink_on = not self._blink_on
        return self._blink_on

    def update_hud(self):
        """Aktualisiert das Head-Up Display"""
        if not self.on_track:
            return
        if not (self.settings.get('hud_active') and self.buttons_allowed):
            self.hide_hud()
            return

        hud_x, hud_y = self.hud_origin()
        blink_on = self._advance_blink()

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
            self.event_bus.emit('play_audio', {'audio_file': 'fcw'})
            self.event_bus.emit('play_audio', {'audio_file': 'fcw'})
            self.event_bus.emit('play_audio', {'audio_file': 'fcw'})

        self.collision_warning_level = warning_level

    def _update_cross_traffic_warning_display(self, data):
        """Aktualisiert Querverkehrswarn-Anzeige"""
        if not isinstance(data, dict):
            return
        warning_level = _as_int(data.get('level', 0))
        warning_side = data.get('side')

        if warning_level >= 2 > self.cross_traffic_warning_level:
            self.event_bus.emit('play_audio', {'audio_file': 'fcw'})
            self.event_bus.emit('play_audio', {'audio_file': 'fcw'})
            self.event_bus.emit('play_audio', {'audio_file': 'fcw'})

        self.cross_traffic_warning_level = warning_level
        self.cross_traffic_warning_side = warning_side

    def _update_blind_spot_display(self, data):
        """Aktualisiert Toter-Winkel-Anzeige"""
        if not isinstance(data, dict):
            return
        _, hud_y = self.hud_origin()
        if data.get('left'):
            self.message_sender.create_button(BTN_BSW_LEFT, 20, hud_y, 10, 10, "^3!",
                                              pyinsim.ISB_DARK)
        else:
            self.message_sender.remove_button(BTN_BSW_LEFT)

        if data.get('right'):
            self.message_sender.create_button(BTN_BSW_RIGHT, 180, hud_y, 10, 10, "^3!",
                                              pyinsim.ISB_DARK)
        else:
            self.message_sender.remove_button(BTN_BSW_RIGHT)

