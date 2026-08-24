"""Acoustic half of the park distance control.

One long-lived beeper thread, fed by state. It used to spawn a **new thread
per beep** running a blocking ``winsound.Beep`` (known-issues #14): approaching
an obstacle raised the beep rate, and every one of those beeps was another
thread creation from the 50 ms UI cycle. Nothing bounded them, and a beep that
outlived its pause simply overlapped with the next one.

The thread is a daemon and idles on an Event, so it costs nothing while no
obstacle is near and dies with the process.
"""

import logging
import threading
import time

from misc.platform_shim import get_sound

logger = logging.getLogger(__name__)


class PDCBeepController:
    # Wie lange ein beep()-Aufruf den Ton freigibt. Die UI ruft alle 50 ms,
    # also ist das reichlich Luft; bleibt der Aufruf aus (PDC aus, Anzeige
    # weg, Modus umgestellt), verstummt der Ton von selbst.
    REQUEST_TIMEOUT_S = 0.5
    # Leerlauf-Wartezeit, falls kein Zustandswechsel signalisiert wird.
    IDLE_POLL_S = 0.25

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

        self._thread = None
        self._stop = threading.Event()
        self._wake = threading.Event()
        self._enabled_until = 0.0
        # Wie viele Threads dieser Controller insgesamt gestartet hat. Genau
        # dafuer gab es #14 - der Test haelt den Wert bei 1 fest.
        self.threads_started = 0

    # ─── Zustand ──────────────────────────────────────────────────────

    def _update_pdc_data(self, pdc_data):
        """Uebernimmt das ``pdc_changed``-Ergebnis.

        Der Payload kommt aus einem Packet-Pfad, also wird nichts angenommen:
        fehlende Sensoren zaehlen als "frei" (0), -1 heisst "System inaktiv"
        und faellt beim max() ohnehin durch.
        """
        if not isinstance(pdc_data, dict):
            return
        front = max((pdc_data.get(i, 0) for i in (0, 1, 2)), default=0)
        rear = max((pdc_data.get(i, 0) for i in (3, 4, 5)), default=0)
        if (front, rear) != (self.current_pdc_state_front, self.current_pdc_state_rear):
            self.current_pdc_state_front = front
            self.current_pdc_state_rear = rear
            self._wake.set()

    def _current_tone(self):
        """(Stufe, Frequenz) oder (0, 0), wenn gerade nichts toenen darf."""
        if time.monotonic() > self._enabled_until:
            return 0, 0
        front = self.current_pdc_state_front
        rear = self.current_pdc_state_rear
        frequency = self.FRONT_FREQUENCY if front > rear else self.REAR_FREQUENCY
        return max(front, rear), frequency

    # ─── Steuerung von der UI ─────────────────────────────────────────

    def beep(self):
        """Main method to handle PDC beeping logic

        Wird vom UI-Zyklus aufgerufen, solange der PDC-Modus Ton vorsieht.
        Der Aufruf *erlaubt* den Ton nur - die Pausen und die Tonlaenge macht
        der Beeper-Thread selbst, damit hier nichts blockiert und keine
        weiteren Threads entstehen.
        """
        self._enabled_until = time.monotonic() + self.REQUEST_TIMEOUT_S
        self._wake.set()
        self._ensure_thread()

    def _ensure_thread(self):
        if self._thread is not None and self._thread.is_alive():
            return
        self._stop.clear()
        self._thread = threading.Thread(target=self._run, name="pact-pdc-beep",
                                        daemon=True)
        self.threads_started += 1
        self._thread.start()

    def stop(self, timeout: float = 1.0):
        """Beendet den Beeper-Thread (Shutdown und Tests)."""
        self._stop.set()
        self._wake.set()
        thread = self._thread
        if thread is not None and thread.is_alive():
            thread.join(timeout)

    # ─── Beeper-Thread ────────────────────────────────────────────────

    def _run(self):
        while not self._stop.is_set():
            level, frequency = self._current_tone()
            pattern = self.BEEP_PATTERNS.get(level)
            if pattern is None:
                self._idle()
                continue
            try:
                get_sound().Beep(frequency, pattern["beep_duration"])
            except Exception as e:
                # Kein winsound, kein Audiogeraet, Beep abgelehnt: einmal
                # melden und im Leerlauf weiterlaufen, statt heiss zu drehen.
                logger.debug("PDC beep failed: %s: %s", type(e).__name__, e)
                self._idle()
                continue
            self._stop.wait(pattern["pause_duration"] / 1000.0)

    def _idle(self):
        self._wake.wait(self.IDLE_POLL_S)
        self._wake.clear()
