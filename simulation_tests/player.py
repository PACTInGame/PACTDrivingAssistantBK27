"""Replays a recorded input stream against LFS.

Three things this has to get right, in order of how badly they hurt:

1. **Nothing may stay pressed.** An aborted replay that leaves W and the left
   mouse button down hands the user a car at full throttle. Every key and button
   the player presses is tracked and released in a ``finally``, on every exit
   path including an abort and a crash.
2. **Only LFS may receive the input.** This injects global OS input, so the
   replay refuses to start, and aborts by default, unless LFS is the foreground
   window -- the same rule the add-on follows for its own key injection
   (reference/ui.md §1.4).
3. **The schedule must hold.** Events carry absolute times, so the player waits
   for ``t0 + t/speed`` rather than sleeping the gap between events: a late event
   never pushes the rest of the scenario back.

The abort key (default Pause) is watched on its own listener and works even when
LFS has focus.
"""

from __future__ import annotations

import contextlib
import sys
import threading
import time
from typing import Any, Callable, Dict, List, Optional

from . import config, input_model, win_focus
from .pynput_access import ReplayUnavailable, import_pynput


class ReplayAbort(Exception):
    """Raised inside the player when the run must stop early."""


@contextlib.contextmanager
def high_resolution_timer():
    """Ask Windows for a 1 ms scheduler tick, so short sleeps are not 15 ms."""
    if not win_focus.IS_WINDOWS:  # pragma: no cover
        yield
        return
    import ctypes  # pragma: no cover - Windows only

    winmm = ctypes.windll.winmm
    winmm.timeBeginPeriod(1)
    try:
        yield
    finally:
        winmm.timeEndPeriod(1)


class Player:
    """Plays back an ``input.jsonl`` recording."""

    #: Below this many seconds to go, spin instead of sleeping.
    SPIN_THRESHOLD_S = 0.002
    #: Longest single sleep while waiting for the next event.
    SLEEP_CHUNK_S = 0.005

    def __init__(self, meta: Dict[str, Any], events: List[Dict[str, Any]], *,
                 speed: float = 1.0,
                 require_focus: bool = True,
                 on_focus_loss: str = "abort",
                 abort_key: str = config.DEFAULT_ABORT_KEY,
                 control: Any = None,
                 on_event: Optional[Callable[[Dict[str, Any]], None]] = None,
                 lfs_match: str = config.LFS_WINDOW_MATCH,
                 clock: Callable[[], float] = time.perf_counter):
        if speed <= 0:
            raise ValueError("speed must be > 0")
        self.meta = meta
        self.events = sorted(events, key=lambda e: e["t"])
        self.speed = float(speed)
        self.require_focus = require_focus
        self.on_focus_loss = on_focus_loss
        self.abort_key = abort_key
        self.control = control
        self.on_event = on_event
        self.lfs_match = lfs_match
        self._clock = clock
        self._abort = threading.Event()
        self._abort_reason = ""
        self._t0 = 0.0
        self._held_keys: Dict[int, Any] = {}
        self._held_buttons: Dict[str, Any] = {}
        self._keyboard = None
        self._mouse = None
        self._paused_total = 0.0

    # -- pre-flight ---------------------------------------------------------
    def preflight(self) -> List[str]:
        """Reasons this replay is likely to misfire. Empty list = good to go."""
        problems: List[str] = []
        try:
            import_pynput()
        except ReplayUnavailable as exc:
            problems.append(str(exc))
        recorded = self.meta.get("screen_size")
        current = win_focus.screen_size()
        if recorded and current and list(recorded) != list(current):
            problems.append(
                f"screen size changed: recorded {tuple(recorded)}, now {tuple(current)} -- "
                "menu clicks are absolute screen coordinates and will miss")
        if current is None:
            problems.append("not running on Windows: input replay cannot drive LFS here")
        elif not win_focus.find_windows(self.lfs_match):
            problems.append(f"no window matching {self.lfs_match!r} -- is LFS running?")
        elif self.require_focus and win_focus.is_foreground(self.lfs_match) is not True:
            problems.append("LFS is not the foreground window")
        return problems

    # -- playback -----------------------------------------------------------
    def play(self) -> Dict[str, Any]:
        """Run the whole recording. Always returns; never leaves a key held."""
        keyboard, mouse = import_pynput()
        self._keyboard, self._mouse = keyboard, mouse
        keyboard_controller = keyboard.Controller()
        mouse_controller = mouse.Controller()

        abort_listener = keyboard.Listener(on_press=self._watch_abort)
        abort_listener.start()

        applied = 0
        markers_sent = 0
        self._abort.clear()
        self._abort_reason = ""
        self._paused_total = 0.0
        started = time.time()

        try:
            with high_resolution_timer():
                self._t0 = self._clock()
                for event in self.events:
                    self._wait_until(event["t"])
                    self._apply(event, keyboard_controller, mouse_controller)
                    applied += 1
                    if event.get("kind") == input_model.KIND_MARKER:
                        markers_sent += 1
                # let the final key-up settle before the caller tears things down
                self._wait_for(0.05)
        except ReplayAbort:
            pass
        except KeyboardInterrupt:
            self._abort.set()
            self._abort_reason = self._abort_reason or "interrupted by user"
        finally:
            self._release_everything(keyboard_controller, mouse_controller)
            try:
                abort_listener.stop()
            except Exception:
                pass

        return {
            "aborted": self._abort.is_set(),
            "abort_reason": self._abort_reason,
            "events_total": len(self.events),
            "events_applied": applied,
            "markers_sent": markers_sent,
            "paused_s": round(self._paused_total, 3),
            "wall_duration_s": round(time.time() - started, 3),
            "speed": self.speed,
        }

    def abort(self, reason: str = "aborted") -> None:
        if not self._abort.is_set():
            self._abort_reason = reason
            self._abort.set()

    # -- internals ----------------------------------------------------------
    def _watch_abort(self, key: Any) -> None:
        name, vk = input_model.describe_key(key)
        if input_model.key_matches(self.abort_key, name, vk):
            self.abort(f"abort key {self.abort_key!r} pressed")

    def _deadline(self, event_time: float) -> float:
        return self._t0 + event_time / self.speed

    def _wait_for(self, seconds: float) -> None:
        end = self._clock() + seconds
        while self._clock() < end:
            self._check_abort()
            time.sleep(min(self.SLEEP_CHUNK_S, max(0.0, end - self._clock())))

    def _wait_until(self, event_time: float) -> None:
        while True:
            self._check_abort()
            self._check_focus()
            remaining = self._deadline(event_time) - self._clock()
            if remaining <= 0:
                return
            if remaining <= self.SPIN_THRESHOLD_S:
                while self._deadline(event_time) - self._clock() > 0:
                    pass
                return
            time.sleep(min(self.SLEEP_CHUNK_S, remaining - self.SPIN_THRESHOLD_S))

    def _check_abort(self) -> None:
        if self._abort.is_set():
            raise ReplayAbort(self._abort_reason)

    def _check_focus(self) -> None:
        if not self.require_focus or self.on_focus_loss == "ignore":
            return
        focused = win_focus.is_foreground(self.lfs_match)
        if focused is not False:
            return
        if self.on_focus_loss == "pause":
            # Hold the schedule rather than firing the rest into another window.
            paused_at = self._clock()
            while win_focus.is_foreground(self.lfs_match) is False:
                self._check_abort()
                time.sleep(0.1)
            gap = self._clock() - paused_at
            self._t0 += gap
            self._paused_total += gap
            return
        self.abort("LFS lost focus")
        raise ReplayAbort(self._abort_reason)

    def _apply(self, event: Dict[str, Any], keyboard_controller: Any,
               mouse_controller: Any) -> None:
        kind = event.get("kind")
        if kind == input_model.KIND_MOVE:
            mouse_controller.position = (event["x"], event["y"])
        elif kind == input_model.KIND_KEY:
            self._apply_key(event, keyboard_controller)
        elif kind == input_model.KIND_CLICK:
            mouse_controller.position = (event["x"], event["y"])
            button = input_model.resolve_button(event["button"], self._mouse)
            if event.get("action") == "down":
                mouse_controller.press(button)
                self._held_buttons[event["button"]] = button
            else:
                mouse_controller.release(button)
                self._held_buttons.pop(event["button"], None)
        elif kind == input_model.KIND_SCROLL:
            mouse_controller.position = (event["x"], event["y"])
            mouse_controller.scroll(event.get("dx", 0), event.get("dy", 0))
        elif kind == input_model.KIND_MARKER:
            if self.control is not None:
                self.control.marker(event.get("name", ""), source="replay",
                                    replay_t=event["t"])
        if self.on_event is not None:
            self.on_event(event)

    def _apply_key(self, event: Dict[str, Any], keyboard_controller: Any) -> None:
        vk = event.get("vk")
        try:
            key = input_model.resolve_key(event.get("name", ""), vk, self._keyboard)
        except ValueError:
            return  # an unresolvable key is skipped, never fatal
        identity = vk if vk is not None else event.get("name")
        if event.get("action") == "down":
            keyboard_controller.press(key)
            self._held_keys[identity] = key
        else:
            keyboard_controller.release(key)
            self._held_keys.pop(identity, None)

    def _release_everything(self, keyboard_controller: Any, mouse_controller: Any) -> None:
        """Never leave the car driving itself."""
        for key in list(self._held_keys.values()):
            try:
                keyboard_controller.release(key)
            except Exception:
                pass
        self._held_keys.clear()
        for button in list(self._held_buttons.values()):
            try:
                mouse_controller.release(button)
            except Exception:
                pass
        self._held_buttons.clear()
        if self._abort.is_set():
            print(f"[replay] released all input after abort: {self._abort_reason}",
                  file=sys.stderr)
