import time
from typing import Any, Dict

from assistance.base_system import AssistanceSystem
from core.event_bus import EventBus
from core.settings_manager import SettingsManager
from misc.input_guard import InputGuard
from misc.key_tap import get_key_tapper
from misc.language import LanguageManager
from vehicles.own_vehicle import OwnVehicle
from vehicles.vehicle import Vehicle


class AutoHold(AssistanceSystem):
    """Automatic Parking Brake"""

    # ─── Ausloeseschwellen ────────────────────────────────────────────
    # Stillstand: OutGauge liefert die Geschwindigkeit in m/s, umgerechnet in
    # km/h. 0.05 km/h ist 1.4 cm/s - langsamer als jedes Kriechen.
    STANDSTILL_SPEED_KMH = 0.05
    # Bremse muss wirklich getreten sein, nicht nur beruehrt.
    MIN_BRAKE = 0.05
    CONFIRM_TIMEOUT_S = 1.0

    def __init__(self, event_bus: EventBus, settings: SettingsManager):
        super().__init__("auto_hold", event_bus, settings)
        self.current_warning_level = 0
        self.own_rectangle = None
        self.translator = LanguageManager()
        # Der Schutz vor Tastendruecken an der falschen Stelle liegt komplett
        # im InputGuard (reference/ui.md §1.4). Er abonniert state_data und
        # outgauge_data selbst; frueher hat AutoHold nur dialog/text_entry
        # geprueft und weder Shift noch das Vordergrundfenster noch ob die
        # OutGauge-Daten ueberhaupt das eigene Auto beschreiben.
        self.guard = InputGuard(event_bus)
        # Der Tastendruck laeuft ueber den gemeinsamen KeyTapper: druecken und
        # loslassen passieren auf dessen eigenem Thread, der Assistenzzyklus
        # zahlt nur das Einreihen. Frueher hielt pyautogui.PAUSE die Taste
        # 100 ms - mit time.sleep im 100-ms-Thread, also 220 ms Blockade pro
        # Ausloesung (known-issues.md #43).
        self.tapper = get_key_tapper()
        self._attempted = False
        self._pending_since = None
        self._context = None
        self.event_bus.subscribe('state_data', self._on_state)

    def _reset_attempt(self):
        self._attempted = False
        self._pending_since = None

    def is_enabled(self):
        enabled = super().is_enabled()
        if not enabled:
            self._reset_attempt()
        return enabled

    def _on_state(self, data):
        if not data.get('on_track', False):
            self._reset_attempt()

    def process(self, own_vehicle: OwnVehicle, vehicles: Dict[int, Vehicle]) -> Dict[str, Any]:
        """Verarbeitet die Auto-Hold-Logik

        Kosten pro Zyklus: konstante Vergleiche, waehrend einer ausstehenden
        Bestaetigung eine monotonic-Abfrage. Kein I/O, keine Konfigurationsscans.
        """
        if not self.is_enabled():
            self._reset_attempt()
            return {'auto_hold_active': False}
        key = self.settings.get('user_handbrake_key')
        context = (own_vehicle.data.player_id, own_vehicle.data.control_mode, key)
        if context != self._context:
            self._reset_attempt()
            self._context = context
        if (own_vehicle.data.speed >= self.STANDSTILL_SPEED_KMH
                or own_vehicle.brake <= self.MIN_BRAKE
                or not own_vehicle.is_local_driver):
            self._reset_attempt()
            return {'auto_hold_active': False}

        # Ein eingereihter Tastendruck bestaetigt keine Handbremse. Gerade
        # bei einer Handbremsachse kann LFS den Tastendruck ignorieren.
        if own_vehicle.handbrake_light:
            if self._pending_since is not None:
                self.event_bus.emit('notification', {'notification':
                    self.translator.get('Auto Hold', self.settings.get('language'))})
            self._pending_since = None
            self._attempted = True
            return {'auto_hold_active': True}

        if self._pending_since is not None:
            if time.monotonic() - self._pending_since >= self.CONFIRM_TIMEOUT_S:
                self._pending_since = None
                self.event_bus.emit('notification', {'notification':
                    '^1' + self.translator.get('Check handbrake binding',
                                               self.settings.get('language'))})
            return {'auto_hold_active': False}
        if self._attempted or self.guard.may_inject(own_vehicle) is not None:
            return {'auto_hold_active': False}
        # Hoechstens ein Versuch je Stillstands-/Bremsphase: wiederholte
        # Toggles koennten eine inzwischen angezogene Handbremse loesen.
        self._attempted = True
        if self.tapper.tap(key):
            self._pending_since = time.monotonic()
        return {'auto_hold_active': False}

    def shutdown(self):
        """Keine Taste darf gedrueckt bleiben, wenn der Prozess endet

        Der KeyTapper ist prozessweit geteilt, ``release_all()`` ist idempotent
        und deckt auch das Getriebe mit ab (reference/control-intervention.md
        §1, Fail-Safe).
        """
        self.tapper.release_all()
