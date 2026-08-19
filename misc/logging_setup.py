"""Central logging setup and the rate limiter the three hot loops share.

Rationale (``CLAUDE.md`` rule 1): this add-on runs a 100 ms assistance pass and
a 50 ms UI pass. A ``print()`` in either of them is unbuffered blocking I/O to
the console and costs more than the work it reports on, which is why the
original code had to comment out its exception handlers to stay usable. The
replacement therefore has two halves:

* :func:`setup_logging` -- console + rotating file handler, installed **once**
  by ``main.py``. Every other module only ever does
  ``logging.getLogger(__name__)``; importing this module has no side effects.
* :class:`ErrorThrottle` -- the shared "report the first few, then shut up"
  policy. Loops call it *only* on the exception path, so a healthy cycle pays
  nothing.

No logging call belongs on a successful ``process()`` path at default level.
"""

import logging
import logging.handlers
import os
import threading
import time
from typing import Optional

from misc.helpers import resolve_path

__all__ = [
    'DEFAULT_LOG_FILE',
    'ErrorThrottle',
    'get_logger',
    'setup_logging',
]

# Next to the executable / project root, as required by WP2. Two 512 KiB files
# is enough to survive a session without ever needing attention.
DEFAULT_LOG_FILE = 'pact_assistant.log'
_MAX_BYTES = 512 * 1024
_BACKUP_COUNT = 2

_LOG_FORMAT = '%(asctime)s %(levelname)-7s %(name)s: %(message)s'
_DATE_FORMAT = '%H:%M:%S'

_configured = False
_configure_lock = threading.Lock()


def setup_logging(level: int = logging.INFO,
                  log_file: Optional[str] = None,
                  console: bool = True) -> logging.Logger:
    """Install the console and rotating-file handlers on the root logger.

    Idempotent -- calling it twice does not duplicate handlers. If the log file
    cannot be opened (read-only install directory, file locked by a second
    instance) logging degrades to console only instead of killing startup.

    Args:
        level: root level, ``logging.INFO`` by default.
        log_file: path or bare filename; a bare name is resolved next to the
            executable via :func:`misc.helpers.resolve_path`. ``None`` uses
            :data:`DEFAULT_LOG_FILE`.
        console: set false to suppress the console handler (tests).

    Returns:
        The root logger.
    """
    global _configured

    root = logging.getLogger()
    with _configure_lock:
        if _configured:
            return root

        root.setLevel(level)
        formatter = logging.Formatter(_LOG_FORMAT, _DATE_FORMAT)

        if console:
            stream_handler = logging.StreamHandler()
            stream_handler.setFormatter(formatter)
            root.addHandler(stream_handler)

        path = log_file if log_file is not None else DEFAULT_LOG_FILE
        if not os.path.isabs(path):
            path = resolve_path(path)
        try:
            file_handler = logging.handlers.RotatingFileHandler(
                path, maxBytes=_MAX_BYTES, backupCount=_BACKUP_COUNT,
                encoding='utf-8', delay=True)
            file_handler.setFormatter(formatter)
            root.addHandler(file_handler)
        except OSError as exc:
            root.warning("No log file at %s (%s) - console logging only.", path, exc)

        _configured = True

    return root


def get_logger(name: str) -> logging.Logger:
    """``logging.getLogger`` with no import-order surprises."""
    return logging.getLogger(name)


def _reset_for_tests():
    """Forget that logging was configured. Used by the test suite only."""
    global _configured
    with _configure_lock:
        _configured = False


class ErrorThrottle:
    """Rate-limited failure reporting with a consecutive-failure counter.

    One instance per loop (``EventBus``, ``ThreadManager``,
    ``AssistanceManager``). A *source* is a stable string: the subscriber name,
    the task name, the assistance system key.

    Policy: the first ``burst`` failures of a source are logged in full, with a
    traceback. After that at most one message per ``interval`` seconds, carrying
    the number of failures suppressed in between. This is what keeps a system
    that fails every cycle from writing 10 lines a second into the log file --
    the reason the original code commented its handlers out instead.

    Cost: nothing on the success path (``succeeded`` only touches the dict when
    a counter is actually set); one uncontended lock on the failure path.
    Thread-safe -- packet handlers report from the main thread while the
    assistance and UI workers report from theirs.
    """

    def __init__(self, logger: logging.Logger, burst: int = 3,
                 interval: float = 30.0, time_source=time.monotonic):
        self._logger = logger
        self._burst = burst
        self._interval = interval
        self._now = time_source
        self._lock = threading.Lock()
        # source -> [total, consecutive, last_log_time, suppressed_since_last_log]
        self._state = {}

    def report(self, source: str, exc: BaseException, context: str = '') -> int:
        """Record a failure of *source*; log it unless rate-limited.

        Returns the number of **consecutive** failures for this source, so the
        caller can decide when to give up on it.
        """
        with self._lock:
            entry = self._state.get(source)
            if entry is None:
                entry = [0, 0, None, 0]
                self._state[source] = entry
            entry[0] += 1
            entry[1] += 1
            total, consecutive, last_log, suppressed = entry

            now = self._now()
            if total <= self._burst:
                should_log = True
            elif last_log is None or (now - last_log) >= self._interval:
                should_log = True
            else:
                should_log = False

            if should_log:
                entry[2] = now
                entry[3] = 0
            else:
                entry[3] = suppressed + 1

        if should_log:
            suffix = ''
            if suppressed:
                suffix = f" ({suppressed} further failures suppressed)"
            where = f"{source} {context}".strip()
            self._logger.error("%s failed: %s: %s%s", where,
                               type(exc).__name__, exc, suffix,
                               exc_info=exc)
        return consecutive

    def succeeded(self, source: str):
        """Reset the consecutive counter after a clean run of *source*."""
        state = self._state
        entry = state.get(source)
        if entry is None or entry[1] == 0:
            return
        with self._lock:
            entry = state.get(source)
            if entry is not None:
                entry[1] = 0

    def consecutive(self, source: str) -> int:
        entry = self._state.get(source)
        return entry[1] if entry else 0

    def total(self, source: str) -> int:
        entry = self._state.get(source)
        return entry[0] if entry else 0

    def forget(self, source: str):
        with self._lock:
            self._state.pop(source, None)
