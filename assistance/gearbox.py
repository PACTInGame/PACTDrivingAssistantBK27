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

    def __init__(self, event_bus: EventBus, settings: SettingsManager):
        super().__init__("automatic_gearbox", event_bus, settings)
        self.gearbox_active = False
        self.calibrating = False
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
        # TODO listen for gear key changes

    def save_calibrations_for_cars(self, cname):
        """Speichert Kalibrierungen pro Autos"""
        cname = str(cname)
        cname = cname[2:-1]
        print(cname)
        calibration_file = Path("data/gearbox_calibrations.json")
        calibration_file.parent.mkdir(parents=True, exist_ok=True)

        # Lade bestehende Daten oder erstelle leeres Dict
        calibrations = {}
        if calibration_file.exists():
            with open(calibration_file, 'r', encoding='utf-8') as f:
                calibrations = json.load(f)

        # Speichere Kalibrierung für aktuelles Fahrzeug
        calibrations[cname] = {
            'redline': self.redline,
            'idle': self.idle,
            'max_gears': self.max_gears
        }

        # Schreibe zurück in Datei
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

            # Lade Kalibrierung für aktuelles Fahrzeug, falls vorhanden
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
            pass  # Bei Fehler Standard-Werte beibehalten

    def process(self, own_vehicle: OwnVehicle, vehicles: Dict[int, Vehicle]) -> Dict[str, Any]:
        """Verarbeitet die Auto-Gearbox-Logik"""
        if not self.is_enabled():
            return {'auto_gearbox_active': False}

        if not self.calibrating and self.redline == 0 and self.idle == 0 and self.max_gears == 0:
            self.load_calibrations_for_cars(own_vehicle.data.cname)
            if self.redline != 0:
                self.car = own_vehicle.data.cname

        if self.car != own_vehicle.data.cname:
            if not self.calibrating:
                self.calibrating = True
                self.calibration_step = 0
                self.event_bus.emit("notification", {'notification': 'Gearbox Calibration Started'})
                self.event_bus.emit("notification", {'notification': '^1Keep the rpm at idle!'})
                self.event_bus.emit("notification", {'notification': '^1Recording idle rpm!'})
                self.time_in_step = time.perf_counter()

        if self.calibrating:
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
                self.calibrating = False
                self.car = own_vehicle.data.cname
        else:
            if time.perf_counter() - self.time_since_last_gear_change > 0.5:

                current_gear = own_vehicle.gear

                current_accelerator_pedal = own_vehicle.throttle
                self.last_throttle_values.append(current_accelerator_pedal)
                if len(self.last_throttle_values) > 3:
                    self.last_throttle_values.pop(0)
                average_throttle = sum(self.last_throttle_values) / len(self.last_throttle_values)
                current_accelerator_pedal = average_throttle
                current_brake_pedal = own_vehicle.brake
                upper_bound = max(0.3, min(current_accelerator_pedal + 0.05, 0.95))
                lower_bound = min(0.55, max(current_accelerator_pedal - 0.25, 0.15))
                current_rpm = own_vehicle.rpm
                size_of_gear_area = (self.redline - self.idle)

                if current_gear < self.max_gears and current_accelerator_pedal > 0.3:
                    if current_rpm > self.idle + size_of_gear_area * upper_bound:
                        # Shift up
                        pyautogui.keyDown(self.clutch_key)
                        pyautogui.keyDown(self.shift_up_key)
                        pyautogui.keyUp(self.shift_up_key)
                        pyautogui.keyUp(self.clutch_key)
                        self.time_since_last_gear_change = time.perf_counter()

                if current_gear > 2 and (current_accelerator_pedal > 0.1 or current_brake_pedal > 0.05):
                    if current_rpm < self.idle + size_of_gear_area * lower_bound :
                        # Shift down
                        pyautogui.keyDown(self.clutch_key)
                        pyautogui.keyDown(self.shift_down_key)
                        pyautogui.keyUp(self.shift_down_key)
                        pyautogui.keyUp(self.clutch_key)
                        self.time_since_last_gear_change = time.perf_counter()

        return {'auto_gearbox_active': True}