"""Replays a recorded input stream against LFS.

Three things this has to get right, in order of how badly they hurt:

1. **Nothing may stay pressed.** An aborted replay that leaves W and the left
   mouse button down hands the user a car at full throttle. Every key and button
   the player presses is tracked and released in a ``finally``, on every exit
   path including an abort and a crash.
2. **Only LFS may receive the input.** This injects global OS input, so LFS
   has to own the foreground -- the same rule the add-on follows for its own
   key injection (reference/ui.md §1.4). The player *takes* the foreground
   (``win_focus.raise_window``) immediately before the first event and again
   whenever it is lost mid-run, instead of refusing until a human has arranged
   it: an unattended run has nobody to alt-tab for it, and a focus blip while
   LFS switches between the menu and the track is routine. Only a raise that
   actually fails falls back to ``on_focus_loss``.
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
    #: An event dispatched more than this far past its deadline is counted and,
    #: with ``strict_timing``, aborts the run. 0.25 s is roughly the point where
    #: an LFS menu click lands on the wrong page and the rest of the scenario is
    #: meaningless anyway.
    LATE_BUDGET_S = 0.25

    def __init__(self, meta: Dict[str, Any], events: List[Dict[str, Any]], *,
                 speed: float = 1.0,
                 require_focus: bool = True,
                 on_focus_loss: str = "abort",
                 abort_key: str = config.DEFAULT_ABORT_KEY,
                 control: Any = None,
                 on_event: Optional[Callable[[Dict[str, Any]], None]] = None,
                 lfs_match: str = config.LFS_WINDOW_MATCH,
                 strict_timing: bool = False,
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
        self.strict_timing = strict_timing
        self._clock = clock
        self._late_max = 0.0
        self._late_total = 0.0
        self._late_over_budget = 0
        self._abort = threading.Event()
        self._abort_reason = ""
        self._t0 = 0.0
        self._held_keys: Dict[int, Any] = {}
        self._held_buttons: Dict[str, Any] = {}
        self._keyboard = None
        self._mouse = None
        self._paused_total = 0.0
        self._refocused = 0
        #: Non-blocking observations from pre-flight and from the raise itself.
        self.warnings: List[str] = []

    # -- pre-flight ---------------------------------------------------------
    def preflight(self) -> List[str]:
        """Reasons this replay is likely to misfire. Empty list = good to go.

        Blocking problems only. Things worth saying but not worth refusing over
        land in :attr:`warnings`; the caller prints those and carries on.
        """
        problems: List[str] = []
        self.warnings = []
        # The recording itself first: a stream that leaves a key down is a
        # danger regardless of which machine replays it.
        problems.extend(input_model.check_recording(self.events))
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
            return problems
        window = win_focus.lfs_window(self.lfs_match)
        if window is None:
            problems.append(f"no window matching {self.lfs_match!r} -- is LFS running?")
            return problems
        if self.require_focus and win_focus.is_foreground(self.lfs_match) is not True:
            # Deliberately not a refusal. ``play()`` raises LFS itself just
            # before the first event, so demanding the foreground here would
            # only block an unattended run. Said out loud because if that raise
            # fails, the opening events land in the wrong window.
            self.warnings.append("LFS is not the foreground window -- the replay "
                                 "will raise it itself before the first event")
        # The screen can be the same size while LFS sits somewhere else on it:
        # a windowed LFS that moved invalidates every recorded click just as
        # thoroughly as a resolution change does.
        recorded_window = self.meta.get("lfs_window") or {}
        recorded_rect = recorded_window.get("rect")
        if recorded_rect and window.get("rect") and                 list(recorded_rect) != list(window["rect"]):
            problems.append(
                f"the LFS window moved or resized: recorded {tuple(recorded_rect)}, "
                f"now {tuple(window['rect'])} -- clicks are absolute screen "
                "coordinates and will miss")
        if win_focus.any_key_physically_down():
            problems.append("a key or mouse button is being held down -- release "
                            "everything before the replay starts")
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
        self._late_max = 0.0
        self._late_total = 0.0
        self._late_over_budget = 0
        self._abort.clear()
        self._abort_reason = ""
        self._paused_total = 0.0
        started = time.time()

        if self.require_focus:
            self._take_foreground("before the first event")

        try:
            with high_resolution_timer():
                self._t0 = self._clock()
                for event in self.events:
                    self._wait_until(event["t"])
                    self._record_lateness(event["t"])
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
            "refocused": self._refocused,
            "warnings": list(self.warnings),
            "wall_duration_s": round(time.time() - started, 3),
            "speed": self.speed,
            # How well the schedule actually held. A late event does not shift
            # the rest (deadlines are absolute), but it does mean the input
            # landed later in the game than the timeline says, so a trace that
            # disagrees with its timeline by this much is not a finding.
            "lateness_max_s": round(self._late_max, 4),
            "lateness_mean_s": round(self._late_total / applied, 4) if applied else 0.0,
            "lateness_over_budget": self._late_over_budget,
            "lateness_budget_s": self.LATE_BUDGET_S,
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

    def _record_lateness(self, event_time: float) -> None:
        """How far past its deadline this event is actually being dispatched."""
        late = max(0.0, self._clock() - self._deadline(event_time))
        self._late_total += late
        self._late_max = max(self._late_max, late)
        if late > self.LATE_BUDGET_S:
            self._late_over_budget += 1
            if self.strict_timing:
                self.abort(f"event at t={event_time:.3f} dispatched {late:.3f} s late")
                raise ReplayAbort(self._abort_reason)

    def _check_abort(self) -> None:
        if self._abort.is_set():
            raise ReplayAbort(self._abort_reason)

    def _take_foreground(self, when: str) -> bool:
        """Put LFS in front. False only when the attempt was made and failed.

        ``None`` from :func:`win_focus.raise_window` means the platform cannot
        tell (or LFS is gone, which pre-flight already refuses over) -- not a
        failure to report a second time.
        """
        # Generous here: this runs before the schedule clock starts, so waiting
        # costs nothing, and a full-screen LFS can take a second to come up.
        raised = win_focus.raise_window(self.lfs_match, settle_s=4.0)
        if raised is None:
            return True
        if raised:
            return True
        self.warnings.append(f"could not bring LFS to the foreground {when}")
        return False

    def _check_focus(self) -> None:
        if not self.require_focus or self.on_focus_loss == "ignore":
            return
        focused = win_focus.is_foreground(self.lfs_match)
        if focused is not False:
            return
        # Take it back before deciding anything. Losing the foreground for a
        # moment while LFS switches between the menu and the track is routine,
        # and aborting a 70 s scenario over it wastes the whole run.
        if win_focus.raise_window(self.lfs_match) is True:
            self._refocused += 1
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
