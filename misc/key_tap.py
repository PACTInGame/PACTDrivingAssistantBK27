"""Press a key now, release it later -- without blocking the caller.

Injecting a keystroke into LFS is not a point event: LFS reads the keyboard
once per rendered frame, so a key that goes down and up in 0.2 ms is simply
never seen.  The press has to be *held* for a few frames.  Until now that hold
came from ``pyautogui.PAUSE`` (0.1 s after every call), which meant the holding
was done by ``time.sleep`` **on the shared 100 ms assistance thread**
(``AGENTS.md`` section 1): one auto-hold engagement cost 323 ms of a 100 ms
budget, one gear change roughly 440 ms, and every other assistance system was
late by that much (``reference/known-issues.md`` #43).

The hold is real and must stay; only the *sleeping in the wrong thread* is the
defect.  This module keeps the hold and moves the whole injection -- press,
wait, release -- onto one dedicated daemon thread.  A caller enqueues and
returns in microseconds::

    get_key_tapper().tap('q')                                  # 100 ms tap
    get_key_tapper().tap(clutch, hold_s=0.30)                  # clutch down ...
    get_key_tapper().tap(shift, hold_s=0.10, delay_s=0.10)     # ... shift in it

Why one shared thread rather than a ``threading.Timer`` per press:

* ``pyautogui.PAUSE`` is module-global state that
  :func:`misc.platform_shim.instant_input` switches off and back on.  With a
  single injecting thread there is no window in which one caller's restore
  undoes another caller's press.
* The order of events for one key is guaranteed, which a pool of timers cannot
  promise.
* Importing ``pyautogui`` costs ~255 ms on the first call.  That import happens
  here, on this thread, never on an assistance cycle.
* Everything still held can be released in one place on shutdown.  A key left
  down when the process ends stays down for LFS -- a stuck clutch or handbrake
  is exactly the failure this must not have.

The keys are the names ``settings.json`` stores; :mod:`misc.key_names` does the
translation to pyautogui's spelling, so a bound ``page_up`` works here (the old
call sites passed the stored name straight to pyautogui, where ``page_up`` is
not a key at all).  Mouse buttons are supported for the same reason.

Not covered here: a press that has to *follow a control demand* from cycle to
cycle.  The emergency brake does that in ``Controls/brake_key.py``, which owns
its hold and arbitrates against the driver's own key state.
"""

import heapq
import itertools
import logging
import threading
import time
from typing import Any, Dict, List, Optional, Tuple

from misc.key_names import spelling_for
from misc.platform_shim import instant_input

logger = logging.getLogger(__name__)

# pyautogui does not know mouse buttons by the name settings.json stores.
_MOUSE_BUTTONS = {'mousel': 'left', 'mouser': 'right', 'mousem': 'middle'}

# How long a key stays down by default.
#
# LFS samples the keyboard once per rendered frame, so the hold has to survive
# at least one frame plus the jitter of a Windows timer.  0.1 s is >= 3 frames
# at 30 fps and is what the call sites accidentally used before (it *was*
# ``pyautogui.PAUSE``), so LFS sees exactly what it saw before this module.
DEFAULT_HOLD_S = 0.10

# Ceiling for the scheduler's idle wait, so a shutdown is never blocked longer.
_MAX_IDLE_WAIT_S = 0.5


class KeyTapper:
    """Timed key presses, executed on one dedicated thread.

    *physical* is an optional :class:`misc.physical_keys.PhysicalKeyState`.  When
    given, a release is skipped while the driver is holding that key on the
    hardware -- our injected release would otherwise take their input away
    (``reference/control-intervention.md`` section 3.1).  Without it the tapper
    still only ever releases keys it pressed itself.
    """

    def __init__(self, name: str = 'key-tapper', physical: Any = None,
                 clock=time.monotonic):
        self.clock = clock
        self._name = name
        self._physical = physical

        self._condition = threading.Condition()
        # Min-heap of (due_time, sequence, action, key, token). The sequence
        # number keeps equal timestamps in submission order and keeps the tuple
        # comparable without ever comparing the payload.
        self._queue: List[Tuple[float, int, str, str, int]] = []
        self._sequence = itertools.count()
        # key -> token of the press we are currently holding. A later tap of the
        # same key replaces the token, which invalidates the earlier release:
        # the key then stays down until the *last* hold expires, instead of
        # being dropped in the middle of it.
        self._held: Dict[str, int] = {}
        self._thread: Optional[threading.Thread] = None
        self._stopping = False
        # Injections in flight. Only :meth:`wait_idle` reads it, but it has to
        # exist for "idle" to mean "and nothing is half-done" -- an event is off
        # the queue before its keystroke is actually sent.
        self._active = 0

    # --- Public API ---------------------------------------------------

    def tap(self, key, hold_s: float = DEFAULT_HOLD_S,
            delay_s: float = 0.0) -> bool:
        """Schedule *key* down for *hold_s* seconds, starting in *delay_s*.

        Returns ``False`` if the key cannot be injected at all (unknown name, no
        pyautogui) -- the caller has to treat that as "no keystroke happened",
        exactly like a refusal from the input guard.

        Costs the calling thread a heap push and a notify; it never touches
        pyautogui and never sleeps.
        """
        spelling = spelling_for(key)
        if spelling is None:
            logger.warning("Cannot tap %r: not a key this project can name "
                           "(misc/key_names.py).", key)
            return False
        if spelling.pyautogui is None and not spelling.is_mouse:
            logger.warning("Cannot tap %r: pyautogui has no name for it.", key)
            return False
        # Deliberately *not* asking ``is_available('pyautogui')`` here: the first
        # such call imports pyautogui (~255 ms) and this runs on the assistance
        # thread. The import belongs on the tapper thread, where it happens by
        # itself on the first injection.

        hold_s = max(0.0, float(hold_s))
        delay_s = max(0.0, float(delay_s))
        stored = spelling.stored
        now = self.clock()

        with self._condition:
            if self._stopping:
                return False
            self._ensure_thread_locked()
            token = next(self._sequence)
            self._push_locked(now + delay_s, 'down', stored, token)
            self._push_locked(now + delay_s + hold_s, 'up', stored, token)
        return True

    def release_all(self):
        """Release everything we hold, drop what is queued, stop the thread.

        Called from the shutdown path (``AssistanceManager.shutdown``) and safe
        to call twice.  Releasing is always allowed: it can only ever take back
        a key *we* pressed.
        """
        with self._condition:
            self._stopping = True
            self._queue.clear()
            held = list(self._held.items())
            thread = self._thread
            self._condition.notify_all()

        for key, token in held:
            self._execute('up', key, token, force=True)

        if thread is not None and thread is not threading.current_thread():
            thread.join(timeout=1.0)
            if thread.is_alive():
                logger.warning("Key tapper thread did not stop in time.")

        with self._condition:
            self._thread = None
            self._held.clear()
            self._stopping = False

    def wait_idle(self, timeout: float = 2.0) -> bool:
        """Block until nothing is queued, held or in flight. Tests only.

        Never call this from an assistance cycle -- waiting for the hold to
        expire on the assistance thread is the very defect this module removes.
        """
        deadline = time.monotonic() + timeout
        with self._condition:
            while self._queue or self._held or self._active:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    return False
                self._condition.wait(min(remaining, 0.02))
        return True

    def pending(self) -> int:
        """Queued events. Tests and diagnostics only."""
        with self._condition:
            return len(self._queue)

    def holding(self) -> List[str]:
        """Keys we are holding down right now. Tests and diagnostics only."""
        with self._condition:
            return list(self._held)

    def is_running(self) -> bool:
        thread = self._thread
        return thread is not None and thread.is_alive()

    # --- Scheduling ---------------------------------------------------

    def _push_locked(self, due: float, action: str, key: str, token: int):
        heapq.heappush(self._queue,
                       (due, next(self._sequence), action, key, token))
        self._condition.notify_all()

    def _ensure_thread_locked(self):
        if self._thread is not None and self._thread.is_alive():
            return
        self._thread = threading.Thread(target=self._run, name=self._name,
                                        daemon=True)
        self._thread.start()

    def _run(self):
        while True:
            with self._condition:
                while not self._queue and not self._stopping:
                    self._condition.wait(_MAX_IDLE_WAIT_S)
                if self._stopping:
                    return
                due = self._queue[0][0]
                remaining = due - self.clock()
                if remaining > 0:
                    self._condition.wait(min(remaining, _MAX_IDLE_WAIT_S))
                    continue
                _, _, action, key, token = heapq.heappop(self._queue)
                self._active += 1
            # Outside the lock: injection can block for a moment and must not
            # keep a caller's ``tap()`` waiting.
            try:
                self._execute(action, key, token)
            finally:
                with self._condition:
                    self._active -= 1
                    self._condition.notify_all()

    # --- Injection ----------------------------------------------------

    def _execute(self, action: str, key: str, token: int, force: bool = False):
        if action == 'down':
            with self._condition:
                # Already down from an earlier tap: keep it, only the release
                # moves out. Pressing twice would make LFS see a key repeat.
                already_down = key in self._held
                self._held[key] = token
            if already_down:
                return
        else:
            with self._condition:
                if not force and self._held.get(key) != token:
                    # A later tap owns this key now; its release will end it.
                    return
                self._held.pop(key, None)
            if self._driver_holds(key):
                # The user has taken the key over on the hardware. Our press has
                # become their press, and releasing it would take their input.
                logger.debug("Not releasing %r: held on the hardware.", key)
                return

        try:
            self._inject(action, key)
        except Exception as exc:
            logger.error("Key %s for %r failed: %s: %s",
                         action, key, type(exc).__name__, exc)
            if action == 'down':
                # The press did not happen, so nothing holds the key -- forget
                # it, or the next tap would believe it is still down.
                with self._condition:
                    if self._held.get(key) == token:
                        self._held.pop(key, None)

    def _inject(self, action: str, key: str):
        spelling = spelling_for(key)
        with instant_input() as keyboard:
            if spelling.is_mouse:
                button = _MOUSE_BUTTONS[spelling.stored]
                if action == 'down':
                    keyboard.mouseDown(button=button)
                else:
                    keyboard.mouseUp(button=button)
            elif action == 'down':
                keyboard.keyDown(spelling.pyautogui)
            else:
                keyboard.keyUp(spelling.pyautogui)

    def _driver_holds(self, key: str) -> bool:
        if self._physical is None:
            return False
        try:
            return bool(self._physical.physically_down(key))
        except Exception as exc:
            logger.debug("Physical key state unavailable for %r: %s", key, exc)
            return False


_shared: Optional[KeyTapper] = None
_shared_lock = threading.Lock()


def get_key_tapper() -> KeyTapper:
    """The tapper every assistance system shares.

    One instance, therefore one injecting thread for the whole process: two
    systems can never be inside ``instant_input()`` at the same time, and
    :meth:`KeyTapper.release_all` on shutdown really does cover every key.

    It is wired to the shared :class:`~misc.physical_keys.PhysicalKeyState`,
    so a tap never releases a key the driver is holding on the hardware. That
    is not an optimisation, it is the key-release trap
    (``control-intervention.md`` section 3.1) and it became reachable here the
    moment the parking manoeuvre started *pulsing* the brake rather than
    holding it: a pulse that ends while the driver is on the brake would take
    their brake away, ten times a second. Asking costs a dict lookup, only on
    release, and the tracker answers ``False`` harmlessly when its hooks were
    never installed.
    """
    global _shared
    if _shared is not None:
        return _shared
    with _shared_lock:
        if _shared is None:
            # Imported here rather than at module scope: the tracker pulls in
            # the input-listener shim, and this module is imported by things
            # that never tap a key.
            from misc.physical_keys import get_physical_keys
            _shared = KeyTapper(physical=get_physical_keys())
        return _shared
