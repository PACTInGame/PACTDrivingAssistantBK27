import threading
import time
import winsound
from typing import Dict, Optional


class PDCBeepController:
    def __init__(self, event_bus):
        self.event_bus = event_bus
        self.current_pdc_state_front = 0
        self.current_pdc_state_rear = 0

        # Beep configurations
        self.FRONT_FREQUENCY = 1000  # Hz
        self.REAR_FREQUENCY = 800  # Hz (higher pitch)
        self.BEEP_DURATION = 200  # ms

        # Timing patterns for different distances
        self.BEEP_PATTERNS = {
            1: {"beep_duration": 300, "pause_duration": 500},  # Green: long beep, long pause
            2: {"beep_duration": 200, "pause_duration": 300},  # Yellow: short beep, medium pause
            3: {"beep_duration": 100, "pause_duration": 150}  # Red: continuous beep
        }
        self.event_bus.subscribe('pdc_changed', self._update_pdc_data)
        self.time_last_beep = time.perf_counter()

    def _update_pdc_data(self, pdc_data):
        self.current_pdc_state_front, self.current_pdc_state_rear = max(list(pdc_data.values())[0:3]), max(list(pdc_data.values())[3:6])


    def _play_beep(self, frequency: int, distance: int):
        """Worker thread for playing beep patterns"""
        pattern = self.BEEP_PATTERNS[distance]

        # Intermittent beep pattern
        try:
            # Play beep
            winsound.Beep(frequency, pattern["beep_duration"])



        except:
            pass

    def beep(self):
        """Main method to handle PDC beeping logic"""
        current_time = time.perf_counter()
        frequency = self.FRONT_FREQUENCY if self.current_pdc_state_front > self.current_pdc_state_rear else self.REAR_FREQUENCY
        max_distance = max(self.current_pdc_state_front, self.current_pdc_state_rear)

        if max_distance in self.BEEP_PATTERNS:
            pattern = self.BEEP_PATTERNS[max_distance]
            print((pattern["beep_duration"]+pattern["pause_duration"])/1000.0)
            if current_time - self.time_last_beep >= (pattern["beep_duration"] + pattern["pause_duration"]) / 1000.0:
                self.time_last_beep = time.perf_counter()
                threading.Thread(target=self._play_beep, args=[frequency, max_distance]).start()


