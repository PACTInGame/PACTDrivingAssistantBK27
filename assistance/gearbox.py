import time
from typing import Dict, Any
import pyautogui
from assistance.base_system import AssistanceSystem
from core.event_bus import EventBus
from core.settings_manager import SettingsManager
from vehicles.own_vehicle import OwnVehicle
from vehicles.vehicle import Vehicle
import json
from pathlib import Path


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
    # ──────────────────────────────────────────────────────────────────

    def __init__(self, event_bus: EventBus, settings: SettingsManager):
        super().__init__("automatic_gearbox", event_bus, settings)
        self.gearbox_active = False
        self.calibrating = False
        self.calibration_requested = False
        self.redline = 0
        self.idle = 0
        self.max_gears = 0
        self.car = None
        self.calibration_step = 0
        self.time_in_step = time.perf_counter()
        self.shift_up_key = self.settings.get('user_shift_up_key')
        self.shift_down_key = self.settings.get('user_shift_down_key')
        self.clutch_key = self.settings.get('user_clutch_key')
        self.ignition_key = self.settings.get('user_ignition_key')
        self.last_throttle_values = []
        self.time_since_last_gear_change = time.perf_counter()
        self.last_shift_direction = None  # 'up', 'down', or None

        # Listen for calibration request from menu
        self.event_bus.subscribe('gearbox_calibrate', self._on_calibration_requested)

    def _on_calibration_requested(self, data=None):
        """Wird vom Menü über den Event-Bus ausgelöst"""
        self.calibration_requested = True

    def save_calibrations_for_cars(self, cname):
        """Speichert Kalibrierungen pro Autos"""
        cname = str(cname)
        cname = cname[2:-1]
        print(cname)
        calibration_file = Path("data/gearbox_calibrations.json")
        calibration_file.parent.mkdir(parents=True, exist_ok=True)

        calibrations = {}
        if calibration_file.exists():
            with open(calibration_file, 'r', encoding='utf-8') as f:
                calibrations = json.load(f)

        calibrations[cname] = {
            'redline': self.redline,
            'idle': self.idle,
            'max_gears': self.max_gears
        }

        with open(calibration_file, 'w', encoding='utf-8') as f:
            json.dump(calibrations, f, indent=4, ensure_ascii=False)

    def load_calibrations_for_cars(self, cname):
        """Lädt Kalibrierungen pro Autos"""
        calibration_file = Path("data/gearbox_calibrations.json")
        cname = str(cname)
        cname = cname[2:-1]

        if not calibration_file.exists():
            return

        try:
            with open(calibration_file, 'r', encoding='utf-8') as f:
                calibrations = json.load(f)

            if cname in calibrations:
                car_data = calibrations[cname]
                self.redline = car_data.get('redline', 6500)
                self.idle = car_data.get('idle', 800)
                self.max_gears = car_data.get('max_gears', 8)
            else:
                self.redline = 0
                self.idle = 0
                self.max_gears = 0
        except (json.JSONDecodeError, KeyError):
            pass

    def _start_calibration(self):
        """Startet die Kalibrierung"""
        self.calibrating = True
        self.calibration_step = 0
        self.time_in_step = time.perf_counter()
        self.event_bus.emit("notification", {'notification': 'Gearbox Calibration Started'})
        self.event_bus.emit("notification", {'notification': '^1Keep the rpm at idle!'})
        self.event_bus.emit("notification", {'notification': '^1Recording idle rpm!'})

    def _abort_calibration(self, reason=""):
        """Bricht die Kalibrierung ab"""
        self.calibrating = False
        self.calibration_step = 0
        self.calibration_requested = False
        msg = 'Gearbox Calibration Aborted'
        if reason:
            msg += f' - {reason}'
        self.event_bus.emit("notification", {'notification': f'^1{msg}'})

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
        elapsed = time.perf_counter() - self.time_since_last_gear_change

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

    def _execute_shift(self, direction: str):
        """Führt den Schaltvorgang aus und aktualisiert Tracking"""
        shift_key = self.shift_up_key if direction == 'up' else self.shift_down_key
        pyautogui.keyDown(self.clutch_key)
        pyautogui.keyDown(shift_key)
        pyautogui.keyUp(shift_key)
        pyautogui.keyUp(self.clutch_key)
        self.time_since_last_gear_change = time.perf_counter()
        self.last_shift_direction = direction

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
        if (current_gear >= 2                           # mindestens im 1. Vorwärtsgang
                and current_gear < self.max_gears       # nicht über den höchsten Gang
                and throttle > self.MIN_THROTTLE_FOR_UPSHIFT
                and current_rpm > upshift_rpm
                and self._can_shift('up')):
            self._execute_shift('up')

        # ── Runterschalten ──
        elif (current_gear > 2                          # nicht tiefer als 1. Gang
                and current_rpm < downshift_rpm
                and (throttle > 0.05 or current_brake > 0.05)
                and self._can_shift('down')):
            self._execute_shift('down')

    def process(self, own_vehicle: OwnVehicle, vehicles: Dict[int, Vehicle]) -> Dict[str, Any]:
        """Verarbeitet die Auto-Gearbox-Logik"""
        if not self.is_enabled():
            return {'auto_gearbox_active': False}

        # Kalibrierung laden wenn das Fahrzeug wechselt
        if self.car != own_vehicle.data.cname:
            if not self.calibrating:
                self.load_calibrations_for_cars(own_vehicle.data.cname)
                self.car = own_vehicle.data.cname

        # Kalibrierung über Menü angefragt
        if self.calibration_requested and not self.calibrating:
            self.calibration_requested = False
            if own_vehicle.data.speed > 1:
                self.event_bus.emit("notification",
                                   {'notification': '^1Vehicle must be stationary to calibrate!'})
            else:
                self._start_calibration()

        if self.calibrating:
            # Geschwindigkeits-Check während der Kalibrierung
            if own_vehicle.data.speed > 1:
                self._abort_calibration("Vehicle moved during calibration!")
                return {'auto_gearbox_active': True}

            if self.calibration_step == 0 and time.perf_counter() - self.time_in_step > 12:
                self.idle = round(own_vehicle.rpm)
                self.event_bus.emit("notification", {'notification': f'Idle RPM set to {self.idle}'})
                self.calibration_step = 1
                self.time_in_step = time.perf_counter()
                self.event_bus.emit("notification", {'notification': f'^1Rev it to the redline!'})
                self.event_bus.emit("notification", {'notification': '^1Recording redline!'})

            elif self.calibration_step == 1 and time.perf_counter() - self.time_in_step > 12:
                self.redline = round(own_vehicle.rpm)
                self.event_bus.emit("notification", {'notification': f'Redline RPM set to {self.redline}'})
                self.calibration_step = 2
                self.time_in_step = time.perf_counter()
                self.event_bus.emit("notification", {'notification': f'^1Shift into the highest gear!'})
                self.event_bus.emit("notification", {'notification': '^1Recording highest gear!'})

            elif self.calibration_step == 2 and time.perf_counter() - self.time_in_step > 12:
                self.max_gears = own_vehicle.gear
                self.event_bus.emit("notification", {'notification': f'Max gear set to {self.max_gears - 1}'})
                self.calibrating = False
                self.save_calibrations_for_cars(own_vehicle.data.cname)
                self.event_bus.emit("notification", {'notification': 'Gearbox Calibration Completed'})
                self.event_bus.emit("notification", {
                    'notification': f'Idle: {self.idle}, Redline: {self.redline}, Gears: {self.max_gears - 1}'})
                self.event_bus.emit("notification", {'notification': f'Reset possible in menu!'})
                self.car = own_vehicle.data.cname
        else:
            # Nur schalten wenn kalibriert
            if self.redline > 0 and self.idle > 0 and self.max_gears > 0:
                self._process_shifting(own_vehicle)

        return {'auto_gearbox_active': True}