import logging
import threading
import time
from typing import Dict, List, Callable, Optional

from core.event_bus import EventBus
from misc.logging_setup import ErrorThrottle

logger = logging.getLogger(__name__)

# Ein Task, der k mal hintereinander wirft, wird abgeschaltet statt weiter
# jeden Zyklus zu werfen. Frueher riss die erste Exception den ganzen Thread
# mit, alle anderen Tasks desselben Intervalls verstummten still mit.
MAX_CONSECUTIVE_FAILURES = 5

# Watchdog: ein Task, der laenger als WATCHDOG_FACTOR x Intervall nicht mehr
# gelaufen ist, haengt (blockierendes I/O, Deadlock). last_execution wurde
# bisher nur geschrieben und nie gelesen.
WATCHDOG_FACTOR = 5
WATCHDOG_INTERVAL_S = 1.0

# Ueberlauf-Meldungen sind ratenbegrenzt - eine Zeile pro Intervallgruppe und
# 30 s, sonst schreibt ein dauerhaft zu langsamer Zyklus das Logfile voll.
OVERRUN_LOG_INTERVAL_S = 30.0


class ScheduledTask:
    """Repräsentiert eine geplante Aufgabe"""

    def __init__(self, name: str, callback: Callable, interval_ms: int, enabled: bool = True):
        self.name = name
        self.callback = callback
        self.interval_ms = interval_ms
        self.enabled = enabled
        self.last_execution = 0.0
        # Vom Watchdog gesetzt, damit ein haengender Task nur einmal gemeldet wird.
        self.stalled = False


class ThreadManager:
    """Verwaltet alle Threads und geplante Aufgaben"""

    def __init__(self, event_bus: EventBus, error_throttle: ErrorThrottle = None):
        self.event_bus = event_bus
        self._tasks: Dict[int, List[ScheduledTask]] = {}  # interval -> tasks
        self._threads: Dict[int, threading.Thread] = {}
        self._running = False
        self._lock = threading.Lock()
        self._stop_event = threading.Event()
        self._watchdog: Optional[threading.Thread] = None
        self._errors = error_throttle or ErrorThrottle(logger)
        self._last_overrun_log: Dict[int, float] = {}

    def add_task(self, task: ScheduledTask):
        """Fügt eine neue geplante Aufgabe hinzu"""
        if task.interval_ms <= 0:
            raise ValueError(f"task {task.name!r} needs a positive interval, got {task.interval_ms}")
        with self._lock:
            interval = task.interval_ms
            if interval not in self._tasks:
                self._tasks[interval] = []
            self._tasks[interval].append(task)

    def start(self):
        """Startet alle Thread-Zyklen"""
        self._running = True
        self._stop_event.clear()
        now = time.monotonic()
        with self._lock:
            intervals = list(self._tasks.keys())
            for tasks in self._tasks.values():
                for task in tasks:
                    # Ohne Startwert wuerde der Watchdog beim ersten Durchlauf
                    # jeden Task als haengend melden.
                    task.last_execution = now
                    task.stalled = False

        for interval in intervals:
            thread = threading.Thread(target=self._run_cycle, args=(interval,),
                                      name=f"pact-{interval}ms", daemon=True)
            self._threads[interval] = thread
            thread.start()
            logger.info("Started %d ms task thread (%d task(s)).", interval,
                        len(self._tasks.get(interval, ())))

        self._watchdog = threading.Thread(target=self._run_watchdog,
                                          name="pact-watchdog", daemon=True)
        self._watchdog.start()

    def stop(self, timeout: float = 2.0):
        """Stoppt alle Threads und wartet begrenzt auf ihr Ende"""
        self._running = False
        self._stop_event.set()

        threads = list(self._threads.values())
        if self._watchdog is not None:
            threads.append(self._watchdog)

        for thread in threads:
            thread.join(timeout=timeout)

        still_alive = [t.name for t in threads if t.is_alive()]
        if still_alive:
            logger.warning("Task threads did not stop within %.1f s: %s",
                           timeout, ", ".join(still_alive))

        self._threads.clear()
        self._watchdog = None
        return not still_alive

    def _tasks_for(self, interval_ms: int) -> List[ScheduledTask]:
        with self._lock:
            return list(self._tasks.get(interval_ms, ()))

    def all_tasks(self) -> List[ScheduledTask]:
        with self._lock:
            return [task for tasks in self._tasks.values() for task in tasks]

    def _run_cycle(self, interval_ms: int):
        """Führt einen Thread-Zyklus aus"""
        while self._running:
            elapsed_ms = self._run_cycle_once(interval_ms)
            self._check_budget(interval_ms, elapsed_ms)
            sleep_time = max(0.0, (interval_ms - elapsed_ms) / 1000.0)
            # Event statt sleep: der Shutdown muss nicht auf den Rest des
            # Zyklus warten.
            if self._stop_event.wait(sleep_time):
                break

    def _run_cycle_once(self, interval_ms: int) -> float:
        """Ein einzelner Durchlauf aller Tasks eines Intervalls

        Gibt die verbrauchte Zeit in ms zurueck. Als eigene Methode, damit die
        Fehler-Isolation ohne laufende Threads testbar ist.
        """
        start_time = time.monotonic()
        for task in self._tasks_for(interval_ms):
            if task.enabled:
                self._run_task(task, start_time)
        return (time.monotonic() - start_time) * 1000.0

    def _run_task(self, task: ScheduledTask, start_time: float):
        """Führt einen Task isoliert aus - eine Exception killt den Thread nicht"""
        try:
            task.callback()
        except Exception as e:
            consecutive = self._errors.report(f"task '{task.name}'", e)
            if consecutive >= MAX_CONSECUTIVE_FAILURES:
                self._disable_task(task, consecutive)
        else:
            self._errors.succeeded(f"task '{task.name}'")
        finally:
            # Auch nach einem Fehler: der Zyklus lebt, der Watchdog darf nicht
            # zusaetzlich anschlagen.
            task.last_execution = start_time
            task.stalled = False

    def _disable_task(self, task: ScheduledTask, consecutive: int):
        task.enabled = False
        logger.error("Task '%s' disabled after %d consecutive failures.",
                     task.name, consecutive)
        self.event_bus.emit('notification',
                            {'notification': f"^1{task.name} stopped - see log"})

    def _check_budget(self, interval_ms: int, elapsed_ms: float):
        if elapsed_ms <= interval_ms:
            return
        now = time.monotonic()
        last = self._last_overrun_log.get(interval_ms)
        if last is not None and (now - last) < OVERRUN_LOG_INTERVAL_S:
            return
        self._last_overrun_log[interval_ms] = now
        logger.warning("%d ms cycle overran its budget: %.1f ms.",
                       interval_ms, elapsed_ms)

    def _run_watchdog(self):
        while not self._stop_event.wait(WATCHDOG_INTERVAL_S):
            self.check_stalled_tasks()

    def check_stalled_tasks(self, now: float = None) -> List[ScheduledTask]:
        """Meldet Tasks, die seit >WATCHDOG_FACTOR x Intervall nicht liefen.

        Als eigene Methode, damit sie ohne Thread testbar ist.
        """
        if now is None:
            now = time.monotonic()
        stalled = []
        for task in self.all_tasks():
            if not task.enabled:
                continue
            deadline = WATCHDOG_FACTOR * task.interval_ms / 1000.0
            if now - task.last_execution <= deadline:
                continue
            stalled.append(task)
            if task.stalled:
                continue        # schon gemeldet, nicht jede Sekunde wiederholen
            task.stalled = True
            logger.error("Task '%s' has not run for %.1f s (interval %d ms).",
                         task.name, now - task.last_execution, task.interval_ms)
        return stalled
