import threading
import time
import winsound
from typing import Dict, Optional


class PDCBeepController:
    def __init__(self):
        # TODO beeping not perfekt yet
        self.beep_thread = None
        self.stop_beeping = threading.Event()
        self.current_beep_state = None
        self.lock = threading.Lock()

        # Beep configurations
        self.FRONT_FREQUENCY = 800  # Hz
        self.REAR_FREQUENCY = 1000  # Hz (higher pitch)
        self.BEEP_DURATION = 200  # ms

        # Timing patterns for different distances
        self.BEEP_PATTERNS = {
            1: {"beep_duration": 300, "pause_duration": 500},  # Green: long beep, long pause
            2: {"beep_duration": 150, "pause_duration": 200},  # Yellow: short beep, medium pause
            3: {"beep_duration": 0, "pause_duration": 0}  # Red: continuous beep
        }

    def _beep_worker(self, frequency: int, distance: int):
        """Worker thread for playing beep patterns"""
        pattern = self.BEEP_PATTERNS[distance]

        if distance == 3:
            # Continuous beep for closest distance
            try:
                winsound.Beep(frequency, 5000)  # Long duration, will be interrupted when stopped
            except:
                pass  # Handle case where beep is interrupted
        else:
            # Intermittent beep pattern
            while not self.stop_beeping.is_set():
                try:
                    # Play beep
                    winsound.Beep(frequency, pattern["beep_duration"])

                    # Wait for pause (check stop event during pause)
                    pause_start = time.time()
                    while (time.time() - pause_start) < (pattern["pause_duration"] / 1000):
                        if self.stop_beeping.is_set():
                            return
                        time.sleep(0.05)  # Small sleep to prevent busy waiting

                except:
                    break  # Exit on any error

    def update_beep(self, pdc_data: Dict[int, int]):
        """Update beeping based on PDC sensor data"""
        with self.lock:
            # Find the closest obstacle (highest distance value > 0)
            active_sensors = {k: v for k, v in pdc_data.items() if v > 0}

            if not active_sensors:
                # No obstacles detected, stop beeping
                self._stop_current_beep()
                return

            # Get the sensor with the highest distance (closest obstacle)
            closest_sensor = max(active_sensors.keys(), key=lambda k: active_sensors[k])
            closest_distance = active_sensors[closest_sensor]

            # Determine if it's front or rear sensor
            is_rear = closest_sensor >= 3
            frequency = self.REAR_FREQUENCY if is_rear else self.FRONT_FREQUENCY

            # Create new beep state identifier
            new_beep_state = (closest_sensor, closest_distance, frequency)

            # Only change beeping if the state has changed
            if self.current_beep_state != new_beep_state:
                self._stop_current_beep()
                self._start_new_beep(frequency, closest_distance)
                self.current_beep_state = new_beep_state

    def _stop_current_beep(self):
        """Stop the current beeping thread"""
        if self.beep_thread and self.beep_thread.is_alive():
            self.stop_beeping.set()
            self.beep_thread.join(timeout=0.1)  # Quick timeout to avoid blocking

        self.stop_beeping.clear()
        self.current_beep_state = None

    def _start_new_beep(self, frequency: int, distance: int):
        """Start a new beeping thread"""
        self.beep_thread = threading.Thread(
            target=self._beep_worker,
            args=(frequency, distance),
            daemon=True
        )
        self.beep_thread.start()

    def stop_all_beeping(self):
        """Stop all beeping (call this when shutting down)"""
        with self.lock:
            self._stop_current_beep()