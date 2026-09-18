"""Records mouse and keyboard into a replayable ``input.jsonl``.

Sampling model (this is the "50 ms Takt"):

* **Mouse position** is sampled on a fixed grid, 50 ms by default. pynput's own
  move callback fires hundreds of times a second and would bloat the file
  without making the replay more faithful.
  Consecutive identical positions are dropped unless ``dense`` is set -- a
  replay only needs the changes, and a still mouse is the common case in menus.
* **Keys, clicks and wheel** are recorded as events with their real timestamp,
  *not* snapped to the grid. A click that happens 12 ms after a key press must
  replay in that order; rounding both to the same 50 ms slot would lose it.

Two keys are consumed by the recorder itself and never reach the stream:
the **marker key** (drops a named marker at the current time) and the
**stop key** (ends the recording). Defaults are Scroll Lock and Pause, because
LFS binds neither.

pynput is imported lazily so this module imports on Linux for the test suite --
the same trick misc/platform_shim.py plays for the add-on.
"""

from __future__ import annotations

import platform
import threading
import time
from typing import Any, Callable, Dict, List, Optional

from . import config, input_model, win_focus
from .pynput_access import import_pynput


class Recorder:
    """Captures global input until the stop key is pressed or :meth:`stop` is called."""

    def __init__(self,
                 sample_interval_ms: int = config.INPUT_SAMPLE_INTERVAL_MS,
                 marker_key: str = config.DEFAULT_MARKER_KEY,
                 stop_key: str = config.DEFAULT_STOP_KEY,
                 marker_names: Optional[List[str]] = None,
                 dense: bool = False,
                 clock: Callable[[], float] = time.perf_counter):
        self.sample_interval = max(1, int(sample_interval_ms)) / 1000.0
        self.marker_key = marker_key
        self.stop_key = stop_key
        self.marker_names = list(marker_names or [])
        self.dense = dense
        self._clock = clock
        self._events: List[Dict[str, Any]] = []
        self._lock = threading.Lock()
        self._stopped = threading.Event()
        self._t0 = 0.0
        self._keyboard = None
        self._mouse = None
        self._listeners: List[Any] = []
        self._sampler: Optional[threading.Thread] = None
        self._last_pos = None
        self.marker_count = 0

    # -- lifecycle ----------------------------------------------------------
    def record(self, on_status: Optional[Callable[[float, int, int], None]] = None,
               status_interval: float = 1.0) -> List[Dict[str, Any]]:
        """Record until stopped. Returns the event list, sorted by time."""
        keyboard, mouse = import_pynput()
        self._keyboard, self._mouse = keyboard, mouse
        mouse_controller = mouse.Controller()
        self._t0 = self._clock()
        self._stopped.clear()

        key_listener = keyboard.Listener(on_press=self._on_press, on_release=self._on_release)
        mouse_listener = mouse.Listener(on_click=self._on_click, on_scroll=self._on_scroll)
        self._listeners = [key_listener, mouse_listener]
        for listener in self._listeners:
            listener.start()

        self._sampler = threading.Thread(
            target=self._sample_mouse, args=(mouse_controller,),
            name="input-sampler", daemon=True)
        self._sampler.start()

        try:
            next_status = self._clock() + status_interval
            while not self._stopped.is_set():
                self._stopped.wait(0.1)
                if on_status is not None and self._clock() >= next_status:
                    on_status(self.elapsed, len(self._events), self.marker_count)
                    next_status = self._clock() + status_interval
        except KeyboardInterrupt:
            self._stopped.set()
        finally:
            for listener in self._listeners:
                try:
                    listener.stop()
                except Exception:
                    pass
            if self._sampler is not None:
                self._sampler.join(1.0)

        with self._lock:
            self._events.sort(key=lambda e: e["t"])
            return list(self._events)

    def stop(self) -> None:
        self._stopped.set()

    @property
    def elapsed(self) -> float:
        return self._clock() - self._t0

    @property
    def events(self) -> List[Dict[str, Any]]:
        with self._lock:
            return list(self._events)

    # -- capture ------------------------------------------------------------
    def _append(self, event: Dict[str, Any]) -> None:
        with self._lock:
            self._events.append(event)

    def _now(self) -> float:
        return round(self._clock() - self._t0, 4)

    def _sample_mouse(self, controller: Any) -> None:
        """Fixed-grid position sampling; drift-free because it targets absolute ticks."""
        tick = 0
        start = self._clock()
        while not self._stopped.is_set():
            tick += 1
            target = start + tick * self.sample_interval
            delay = target - self._clock()
            if delay > 0:
                self._stopped.wait(delay)
            if self._stopped.is_set():
                return
            try:
                position = controller.position
            except Exception:
                continue
            if position is None:
                continue
            point = (int(position[0]), int(position[1]))
            if self.dense or point != self._last_pos:
                self._last_pos = point
                self._append({"t": self._now(), "kind": input_model.KIND_MOVE,
                              "x": point[0], "y": point[1]})

    def _on_press(self, key: Any) -> None:
        name, vk = input_model.describe_key(key)
        if input_model.key_matches(self.stop_key, name, vk):
            self._stopped.set()
            return
        if input_model.key_matches(self.marker_key, name, vk):
            self._add_marker()
            return
        self._append({"t": self._now(), "kind": input_model.KIND_KEY,
                      "action": "down", "name": name, "vk": vk})

    def _on_release(self, key: Any) -> None:
        name, vk = input_model.describe_key(key)
        if input_model.key_matches(self.stop_key, name, vk):
            return
        if input_model.key_matches(self.marker_key, name, vk):
            return
        self._append({"t": self._now(), "kind": input_model.KIND_KEY,
                      "action": "up", "name": name, "vk": vk})

    def _add_marker(self) -> None:
        index = self.marker_count
        self.marker_count += 1
        if index < len(self.marker_names):
            name = self.marker_names[index]
        else:
            name = f"marker_{index + 1}"
        self._append({"t": self._now(), "kind": input_model.KIND_MARKER, "name": name})

    def _on_click(self, x: int, y: int, button: Any, pressed: bool) -> None:
        self._append({"t": self._now(), "kind": input_model.KIND_CLICK,
                      "action": "down" if pressed else "up",
                      "button": input_model.button_name(button),
                      "x": int(x), "y": int(y)})

    def _on_scroll(self, x: int, y: int, dx: int, dy: int) -> None:
        self._append({"t": self._now(), "kind": input_model.KIND_SCROLL,
                      "dx": int(dx), "dy": int(dy), "x": int(x), "y": int(y)})


def build_meta(recorder: Recorder, events: List[Dict[str, Any]],
               scenario: str = "", note: str = "") -> Dict[str, Any]:
    """Everything a replay needs to decide whether it can trust this recording.

    Screen size matters: menu clicks are absolute screen coordinates, so a
    recording made at 2560x1440 replays into the wrong place at 1920x1080.
    """
    return {
        "format": config.INPUT_FORMAT,
        "scenario": scenario,
        "note": note,
        "recorded_utc": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "platform": platform.platform(),
        "screen_size": win_focus.screen_size(),
        "lfs_window": win_focus.lfs_window(config.LFS_WINDOW_MATCH),
        "sample_interval_ms": int(recorder.sample_interval * 1000),
        "dense": recorder.dense,
        "marker_key": recorder.marker_key,
        "stop_key": recorder.stop_key,
        "duration_s": round(input_model.duration(events), 3),
        "event_count": len(events),
        "marker_count": recorder.marker_count,
    }
