import logging
import time
from typing import Dict, Any
from assistance.base_system import AssistanceSystem
from core.event_bus import EventBus
from core.settings_manager import SettingsManager
from misc.input_guard import InputGuard
from misc.language import LanguageManager
from misc.platform_shim import get_keyboard
from vehicles.own_vehicle import OwnVehicle
from vehicles.vehicle import Vehicle
import json
from pathlib import Path
from misc.helpers import resolve_path

logger = logging.getLogger(__name__)


def _calibration_key(cname) -> str:
    """Schluessel fuer data/gearbox_calibrations.json

    CName kommt seit WP4 dekodiert als str aus dem VehicleManager. bytes
    werden weiter akzeptiert, damit ein direkter Aufruf mit Rohdaten nicht
    unter einem anderen Schluessel landet.
    """
    if isinstance(cname, (bytes, bytearray)):
        return bytes(cname).split(b'\x00', 1)[0].decode('latin-1', errors='replace')
    return "" if cname is None else str(cname)


def _as_int(value, default: int = 0) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


class Gearbox(AssistanceSystem):
    """Automatic Gearbox"""

    # ─── Shift Tuning Constants ───────────────────────────────────────
    # Upshift point = idle + rpm_range * (UPSHIFT_BASE + UPSHIFT_THROTTLE_SCALE * throttle)
    #   Low throttle:  ~50% of rpm range
    #   Full throttle: ~92% of rpm range
    UPSHIFT_BASE = 0.50
    UPSHIFT_THROTTLE_SCALE = 0.42

    # Downshift point = idle + rpm_range * (DOWNSHIFT_BASE + DOWNSHIFT_THROTTLE_SCALE * throttle)
    #   Low throttle:  ~15% of rpm range
    #   Full throttle: ~35% of rpm range
    DOWNSHIFT_BASE = 0.15
    DOWNSHIFT_THROTTLE_SCALE = 0.20

    # Cooldown times (seconds)
    COOLDOWN_AFTER_UPSHIFT = 1.5    # before a downshift is allowed
    COOLDOWN_AFTER_DOWNSHIFT = 0.8  # before an upshift is allowed
    COOLDOWN_SAME_DIRECTION = 0.4   # before another shift in the same direction

    # Throttle smoothing
    THROTTLE_HISTORY_SIZE = 5

    # Minimum throttle to consider upshifting
    MIN_THROTTLE_FOR_UPSHIFT = 0.05

    # ─── Kalibrierung ─────────────────────────────────────────────────
    # Dauer eines Kalibrierschritts. Unveraendert 12 s - lang genug, um die
    # Drehzahl zu stabilisieren; was fehlte, war die Rueckmeldung waehrend
    # der Wartezeit.
    CALIBRATION_STEP_S = 12.0
    # Verbleibende Sekunden, bei denen eine Erinnerung ausgegeben wird. Zwei
    # Meldungen pro Schritt: die Notification-Zeile zeigt jede 3 s, mit den
    # beiden Schrittmeldungen fuellt das die 12 s genau aus, ohne die auf 8
    # begrenzte Warteschlange zu ueberlaufen (reference/ui.md §3).
    CALIBRATION_COUNTDOWN_S = (6.0, 3.0)
    # Ueber dieser Geschwindigkeit gilt das Auto als bewegt (km/h).
    CALIBRATION_MAX_SPEED_KMH = 1.0
    # ──────────────────────────────────────────────────────────────────

    # Rohes OutGauge-Gangindex-Schema: 0 = Rueckwaerts, 1 = Leerlauf,
    # 2 = 1. Gang. Der hoechste Gang hat also den Index forward_gears + 1.
    FIRST_FORWARD_GEAR = 2

    def __init__(self, event_bus: EventBus, settings: SettingsManager):
        super().__init__("automatic_gearbox", event_bus, settings)
        self.translator = LanguageManager()
        # Ueberschreibbar, damit Tests die Kalibrierung und die Cooldowns
        # ohne Wartezeit durchfahren koennen.
        self.clock = time.perf_counter
        self.gearbox_active = False
        self.calibrating = False
        self.calibration_requested = False
        self.redline = 0
        self.idle = 0
        # Anzahl der Vorwaertsgaenge - nicht mehr der rohe Gangindex. Der
        # wurde frueher gespeichert und an manchen Stellen als max_gears, an
        # anderen als max_gears - 1 angezeigt.
        self.forward_gears = 0
        self.car = None
        self.calibration_step = 0
        self.time_in_step = self.clock()
        self._countdowns_done = set()
        self.last_throttle_values = []
        self.time_since_last_gear_change = self.clock()
        self.last_shift_direction = None  # 'up', 'down', or None

        # Tastendruck-Schutz (reference/ui.md §1.4). Vorher pruefte der
        # Gearbox ueberhaupt nichts: ein Schaltvorgang waehrend des Chats
        # tippte Kupplung und Gang in die Chatzeile (known-issues #11).
        self.guard = InputGuard(event_bus)

        # Listen for calibration request from menu
        self.event_bus.subscribe('gearbox_calibrate', self._on_calibration_requested)

    def _on_calibration_requested(self, data=None):
        """Wird vom Menü über den Event-Bus ausgelöst

        Waehrend einer laufenden Kalibrierung ist derselbe Menuepunkt der
        Abbruch - vorher war Wegfahren die einzige Moeglichkeit, aus den drei
        blinden 12-Sekunden-Schritten wieder herauszukommen.
        """
        self.calibration_requested = True

    # ─── Persistenz ───────────────────────────────────────────────────

    def save_calibrations_for_cars(self, cname) -> bool:
        """Speichert Kalibrierungen pro Autos

        Dateizugriff im Zyklus - aber genau einmal, am Ende einer
        Kalibrierung und im Stand. Ein Schreibfehler darf die gerade
        ermittelten Werte nicht mitnehmen, deshalb wird er gemeldet statt
        geworfen: eine Exception aus process() heraus wuerde das System nach
        fuenf Zyklen abschalten (assistance/manager.py).
        """
        cname = _calibration_key(cname)
        calibration_file = Path(resolve_path("data", "gearbox_calibrations.json"))

        calibrations = {}
        if calibration_file.exists():
            try:
                with open(calibration_file, 'r', encoding='utf-8') as f:
                    calibrations = json.load(f)
            except (OSError, json.JSONDecodeError):
                # Kaputte Datei darf die frische Kalibrierung nicht verhindern.
                calibrations = {}
        if not isinstance(calibrations, dict):
            calibrations = {}

        calibrations[cname] = {
            'redline': self.redline,
            'idle': self.idle,
            'forward_gears': self.forward_gears
        }

        try:
            calibration_file.parent.mkdir(parents=True, exist_ok=True)
            with open(calibration_file, 'w', encoding='utf-8') as f:
                json.dump(calibrations, f, indent=4, ensure_ascii=False)
        except OSError as exc:
            logger.warning("Storing the gearbox calibration for %s failed: %s: %s",
                           cname, type(exc).__name__, exc)
            return False
        return True

    def load_calibrations_for_cars(self, cname):
        """Lädt Kalibrierungen pro Autos

        Aeltere Dateien speichern unter ``max_gears`` den rohen Gangindex des
        hoechsten Gangs (2 = 1. Gang). Sie werden beim Laden umgerechnet, so
        dass eine vorhandene Kalibrierung weiter gilt.
        """
        calibration_file = Path(resolve_path("data", "gearbox_calibrations.json"))
        cname = _calibration_key(cname)

        self.redline = 0
        self.idle = 0
        self.forward_gears = 0

        if not calibration_file.exists():
            return

        try:
            with open(calibration_file, 'r', encoding='utf-8') as f:
                calibrations = json.load(f)
            car_data = calibrations.get(cname) if isinstance(calibrations, dict) else None
            if not isinstance(car_data, dict):
                return
            self.redline = _as_int(car_data.get('redline', 0))
            self.idle = _as_int(car_data.get('idle', 0))
            if 'forward_gears' in car_data:
                self.forward_gears = max(0, _as_int(car_data.get('forward_gears', 0)))
            else:
                legacy_index = _as_int(car_data.get('max_gears', 0))
                self.forward_gears = max(0, legacy_index - (self.FIRST_FORWARD_GEAR - 1))
        except (OSError, json.JSONDecodeError, KeyError, AttributeError):
            pass

    @property
    def is_calibrated(self) -> bool:
        return self.redline > 0 and self.idle > 0 and self.forward_gears > 0

    # ─── Sprache ──────────────────────────────────────────────────────

    def _lang(self):
        return self.settings.get('language')

    def _t(self, key):
        return self.translator.get(key, self._lang())

    def _notify(self, text: str):
        self.event_bus.emit("notification", {'notification': text})

    # ─── Kalibrierung ─────────────────────────────────────────────────

    # Schritt -> (Aufforderung, Bestaetigung). Beide Texte existieren in
    # misc/language.py; der Countdown haengt nur die Restzeit an.
    _CALIBRATION_PROMPTS = {
        0: ('Keep the rpm at idle!', 'Recording idle rpm!'),
        1: ('Rev it to the redline!', 'Recording redline!'),
        2: ('Shift into the highest gear!', 'Recording highest gear!'),
    }

    def _start_calibration(self):
        """Startet die Kalibrierung"""
        self.calibrating = True
        self._notify(self._t('Gearbox Calibration Started'))
        self._enter_step(0)

    def _enter_step(self, step: int):
        self.calibration_step = step
        self.time_in_step = self.clock()
        self._countdowns_done = set()
        prompt, recording = self._CALIBRATION_PROMPTS[step]
        self._notify('^1' + self._t(prompt))
        self._notify('^1' + self._t(recording))

    def _abort_calibration(self, reason=""):
        """Bricht die Kalibrierung ab"""
        self.calibrating = False
        self.calibration_step = 0
        self.calibration_requested = False
        self._countdowns_done = set()
        msg = self._t('Gearbox Calibration Aborted')
        if reason:
            msg += f' - {self._t(reason)}'
        self._notify(f'^1{msg}')

    def _announce_countdown(self, elapsed: float):
        """Sagt an, wie lange der laufende Schritt noch dauert"""
        remaining = self.CALIBRATION_STEP_S - elapsed
        for mark in self.CALIBRATION_COUNTDOWN_S:
            if remaining <= mark and mark not in self._countdowns_done:
                self._countdowns_done.add(mark)
                prompt = self._CALIBRATION_PROMPTS[self.calibration_step][0]
                self._notify(f'^3{self._t(prompt)} - {int(mark)} s')

    def _process_calibration(self, own_vehicle: OwnVehicle):
        """Ein Kalibrierschritt pro Zyklus - drei Vergleiche im Normalfall"""
        if own_vehicle.data.speed > self.CALIBRATION_MAX_SPEED_KMH:
            self._abort_calibration("Vehicle moved during calibration!")
            return
        # OutGauge folgt der Kamera: was hier aufgezeichnet wuerde, waere
        # sonst die Drehzahl eines fremden Autos (conventions.md §5.2).
        if not own_vehicle.is_local_driver:
            self._abort_calibration("Camera needs to be on own vehicle.")
            return

        elapsed = self.clock() - self.time_in_step
        if elapsed <= self.CALIBRATION_STEP_S:
            self._announce_countdown(elapsed)
            return

        if self.calibration_step == 0:
            self.idle = round(own_vehicle.rpm)
            self._notify(f'{self._t("Idle RPM set to")} {self.idle}')
            self._enter_step(1)
        elif self.calibration_step == 1:
            self.redline = round(own_vehicle.rpm)
            self._notify(f'{self._t("Redline RPM set to")} {self.redline}')
            self._enter_step(2)
        else:
            self._finish_calibration(own_vehicle)

    def _finish_calibration(self, own_vehicle: OwnVehicle):
        gear_index = _as_int(own_vehicle.gear)
        if gear_index < self.FIRST_FORWARD_GEAR:
            # Leerlauf oder Rueckwaerts: mit 0 Vorwaertsgaengen wuerde die
            # Automatik danach schweigend nichts mehr tun.
            self._abort_calibration('Shift into the highest gear!')
            return

        self.forward_gears = gear_index - (self.FIRST_FORWARD_GEAR - 1)
        self.calibrating = False
        self._countdowns_done = set()
        self._notify(f'{self._t("Max gear set to")} {self.forward_gears}')
        self.save_calibrations_for_cars(own_vehicle.data.cname)
        self._notify(self._t('Gearbox Calibration Completed'))
        self._notify(f'Idle: {self.idle}, Redline: {self.redline}, Gears: {self.forward_gears}')
        self._notify(self._t('Reset possible in menu!'))
        self.car = own_vehicle.data.cname

    # ─── Schalten ─────────────────────────────────────────────────────

    def _get_smoothed_throttle(self, raw_throttle: float) -> float:
        """Glättet den Gaspedalwert über die letzten N Werte"""
        self.last_throttle_values.append(raw_throttle)
        if len(self.last_throttle_values) > self.THROTTLE_HISTORY_SIZE:
            self.last_throttle_values.pop(0)
        return sum(self.last_throttle_values) / len(self.last_throttle_values)

    def _can_shift(self, direction: str) -> bool:
        """
        Prüft ob ein Schaltvorgang erlaubt ist, basierend auf
        richtungsabhängigen Cooldowns.

        Nach einem Hochschalten ist ein Runterschalten erst nach
        COOLDOWN_AFTER_UPSHIFT erlaubt (verhindert Gear Hunting).
        """
        elapsed = self.clock() - self.time_since_last_gear_change

        if self.last_shift_direction is None:
            return elapsed > self.COOLDOWN_SAME_DIRECTION

        # Gleiche Richtung wie letzter Schaltvorgang → kurzer Cooldown
        if direction == self.last_shift_direction:
            return elapsed > self.COOLDOWN_SAME_DIRECTION

        # Gegenrichtung → längerer Cooldown gegen Hunting
        if direction == 'down' and self.last_shift_direction == 'up':
            return elapsed > self.COOLDOWN_AFTER_UPSHIFT
        if direction == 'up' and self.last_shift_direction == 'down':
            return elapsed > self.COOLDOWN_AFTER_DOWNSHIFT

        return elapsed > self.COOLDOWN_SAME_DIRECTION

    def _execute_shift(self, direction: str, own_vehicle: OwnVehicle) -> bool:
        """Führt den Schaltvorgang aus und aktualisiert Tracking

        Gibt False zurueck, wenn der InputGuard den Tastendruck verweigert -
        dann hat kein Schaltvorgang stattgefunden, also darf auch der
        Cooldown nicht neu starten.
        """
        if self.guard.may_inject(own_vehicle) is not None:
            return False

        # Tasten live aus den Einstellungen: eine im Menue neu belegte Taste
        # wirkt sofort. Frueher wurden sie in __init__ zwischengespeichert,
        # eine Neubelegung also erst nach einem Neustart wirksam.
        shift_key = self.settings.get('user_shift_up_key' if direction == 'up'
                                      else 'user_shift_down_key')
        clutch_key = self.settings.get('user_clutch_key')

        keyboard = get_keyboard()
        keyboard.keyDown(clutch_key)
        keyboard.keyDown(shift_key)
        keyboard.keyUp(shift_key)
        keyboard.keyUp(clutch_key)
        self.time_since_last_gear_change = self.clock()
        self.last_shift_direction = direction
        return True

    def _process_shifting(self, own_vehicle: OwnVehicle):
        """
        Hauptlogik für das Schalten mit Hysterese.

        Die Upshift-Schwelle liegt deutlich höher als die Downshift-Schwelle.
        Dadurch entsteht eine "tote Zone" in der Mitte, in der kein
        Schaltvorgang ausgelöst wird. Das verhindert Gear Hunting:

            idle ─────[downshift]────────────[upshift]───── redline
                          ↑                      ↑
                     niedrig (15-35%)       hoch (50-92%)
                     je nach Throttle       je nach Throttle

        Zusätzlich sorgen richtungsabhängige Cooldowns dafür, dass nach
        einem Hochschalten nicht sofort zurückgeschaltet wird.
        """
        current_gear = own_vehicle.gear
        current_rpm = own_vehicle.rpm
        current_brake = own_vehicle.brake
        throttle = self._get_smoothed_throttle(own_vehicle.throttle)

        rpm_range = self.redline - self.idle
        if rpm_range <= 0:
            return

        # Rohindex des hoechsten Gangs (forward_gears zaehlt Vorwaertsgaenge).
        top_gear_index = self.forward_gears + (self.FIRST_FORWARD_GEAR - 1)

        # ── Upshift-Schwelle (gaspedalabhängig) ──
        # Vollgas → schalte spät (nahe Redline)
        # Wenig Gas → schalte früh (Komfort-Modus)
        upshift_rpm = self.idle + rpm_range * (
            self.UPSHIFT_BASE + self.UPSHIFT_THROTTLE_SCALE * throttle
        )

        # ── Downshift-Schwelle (gaspedalabhängig, deutlich tiefer) ──
        # Die große Lücke zwischen Upshift und Downshift ist der
        # Kern der Anti-Hunting-Strategie
        downshift_rpm = self.idle + rpm_range * (
            self.DOWNSHIFT_BASE + self.DOWNSHIFT_THROTTLE_SCALE * throttle
        )

        # ── Hochschalten ──
        if (current_gear >= self.FIRST_FORWARD_GEAR   # mindestens im 1. Vorwärtsgang
                and current_gear < top_gear_index     # nicht über den höchsten Gang
                and throttle > self.MIN_THROTTLE_FOR_UPSHIFT
                and current_rpm > upshift_rpm
                and self._can_shift('up')):
            self._execute_shift('up', own_vehicle)

        # ── Runterschalten ──
        elif (current_gear > self.FIRST_FORWARD_GEAR  # nicht tiefer als 1. Gang
                and current_rpm < downshift_rpm
                and (throttle > 0.05 or current_brake > 0.05)
                and self._can_shift('down')):
            self._execute_shift('down', own_vehicle)

    # ─── Zyklus ───────────────────────────────────────────────────────

    def process(self, own_vehicle: OwnVehicle, vehicles: Dict[int, Vehicle]) -> Dict[str, Any]:
        """Verarbeitet die Auto-Gearbox-Logik

        Kosten pro Zyklus: unveraendert eine Handvoll Vergleiche. Der
        InputGuard wird nur befragt, wenn wirklich geschaltet wuerde; der
        Kalibrier-Countdown laeuft nur waehrend der Kalibrierung.
        """
        if not self.is_enabled():
            return {'auto_gearbox_active': False}

        # Kalibrierung laden wenn das Fahrzeug wechselt
        if self.car != own_vehicle.data.cname:
            if not self.calibrating:
                self.load_calibrations_for_cars(own_vehicle.data.cname)
                self.car = own_vehicle.data.cname

        # Menuebefehl: startet die Kalibrierung - oder bricht sie ab.
        if self.calibration_requested:
            self.calibration_requested = False
            if self.calibrating:
                self._abort_calibration()
            elif own_vehicle.data.speed > self.CALIBRATION_MAX_SPEED_KMH:
                self._notify('^1' + self._t('Vehicle must be stationary to calibrate!'))
            else:
                self._start_calibration()

        if self.calibrating:
            self._process_calibration(own_vehicle)
        elif self.is_calibrated:
            self._process_shifting(own_vehicle)

        return {'auto_gearbox_active': True}
