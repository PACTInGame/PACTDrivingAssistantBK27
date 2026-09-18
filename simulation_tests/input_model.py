"""Schema of a recorded input stream, and the key naming that survives a replay.

File layout (``input.jsonl``): the first line is the meta record, every further
line is one event, ordered by ``t`` (seconds since recording start).

    {"kind":"meta","v":1,"d":{...}}
    {"t":0.05,"kind":"move","x":960,"y":540}
    {"t":0.31,"kind":"key","action":"down","name":"esc","vk":27}
    {"t":0.38,"kind":"click","action":"down","button":"left","x":960,"y":540}
    {"t":1.20,"kind":"marker","name":"entered_garage"}

**Keys are replayed by virtual-key code, not by character.** ``KeyCode.char``
depends on the modifiers held at the time (shift+a is ``'A'``, ctrl+a is
``'\\x01'``), so replaying by character would press the wrong physical key as soon
as a scenario holds shift. ``vk`` is the physical key; ``name`` is there for the
human reading the file and is the fallback when a recording carries no vk.
"""

from __future__ import annotations

import json
import os
from typing import Any, Dict, Iterable, List, Optional, Tuple

KIND_META = "meta"
KIND_MOVE = "move"
KIND_KEY = "key"
KIND_CLICK = "click"
KIND_SCROLL = "scroll"
KIND_MARKER = "marker"

EVENT_KINDS = frozenset({KIND_MOVE, KIND_KEY, KIND_CLICK, KIND_SCROLL, KIND_MARKER})

#: Windows virtual-key codes that have an obvious label.
_VK_LABELS = {
    0x08: "backspace", 0x09: "tab", 0x0D: "enter", 0x10: "shift", 0x11: "ctrl",
    0x12: "alt", 0x13: "pause", 0x14: "caps_lock", 0x1B: "esc", 0x20: "space",
    0x21: "page_up", 0x22: "page_down", 0x23: "end", 0x24: "home",
    0x25: "left", 0x26: "up", 0x27: "right", 0x28: "down",
    0x2D: "insert", 0x2E: "delete", 0x5B: "cmd", 0x90: "num_lock", 0x91: "scroll_lock",
}
for _i in range(12):
    _VK_LABELS[0x70 + _i] = f"f{_i + 1}"
for _i in range(10):
    _VK_LABELS[0x60 + _i] = f"num_{_i}"


def vk_label(vk: Optional[int]) -> str:
    """A readable name for a virtual-key code."""
    if vk is None:
        return "unknown"
    if vk in _VK_LABELS:
        return _VK_LABELS[vk]
    if 0x30 <= vk <= 0x39 or 0x41 <= vk <= 0x5A:
        return chr(vk).lower()
    return f"vk{vk}"


def describe_key(key: Any) -> Tuple[str, Optional[int]]:
    """``(name, vk)`` for a pynput key object, without importing pynput here."""
    value = getattr(key, "value", None)
    if value is not None and hasattr(value, "vk"):
        # pynput.keyboard.Key member (an enum wrapping a KeyCode)
        vk = getattr(value, "vk", None)
        name = getattr(key, "name", None) or vk_label(vk)
        return name, vk
    vk = getattr(key, "vk", None)
    char = getattr(key, "char", None)
    if isinstance(char, str) and len(char) == 1 and char.isprintable():
        return char.lower(), vk
    return vk_label(vk), vk


def resolve_key(name: str, vk: Optional[int], keyboard_module: Any) -> Any:
    """Turn a recorded key back into something pynput can press.

    ``vk`` wins whenever it is present: it is the physical key. ``name`` is only
    used for recordings made without one, or for keys given on the command line.
    """
    key_code = keyboard_module.KeyCode
    if vk is not None:
        return key_code.from_vk(vk)
    key_enum = keyboard_module.Key
    member = getattr(key_enum, name, None)
    if member is not None:
        return member
    if len(name) == 1:
        return key_code.from_char(name)
    raise ValueError(f"cannot resolve key {name!r} without a vk code")


def key_matches(spec: str, name: str, vk: Optional[int]) -> bool:
    """Does a pressed key match a CLI key spec such as ``scroll_lock`` or ``vk145``?"""
    spec = spec.strip().lower()
    if not spec:
        return False
    if spec == name.lower():
        return True
    if spec.startswith("vk") and vk is not None:
        try:
            return int(spec[2:]) == vk
        except ValueError:
            return False
    return False


def button_name(button: Any) -> str:
    """``left`` / ``right`` / ``middle`` for a pynput mouse button."""
    return getattr(button, "name", str(button)).lower()


def resolve_button(name: str, mouse_module: Any) -> Any:
    button = getattr(mouse_module.Button, name, None)
    if button is None:
        raise ValueError(f"unknown mouse button {name!r}")
    return button


# ── file I/O ────────────────────────────────────────────────────────────────
def write_recording(path: str, meta: Dict[str, Any], events: Iterable[Dict[str, Any]]) -> int:
    """Write a recording. Returns the number of events written."""
    directory = os.path.dirname(os.path.abspath(path))
    if directory:
        os.makedirs(directory, exist_ok=True)
    count = 0
    with open(path, "w", encoding="utf-8", newline="\n") as fh:
        fh.write(json.dumps({"kind": KIND_META, "d": meta}, separators=(",", ":")) + "\n")
        for event in events:
            fh.write(json.dumps(event, separators=(",", ":")) + "\n")
            count += 1
    return count


def read_recording(path: str) -> Tuple[Dict[str, Any], List[Dict[str, Any]]]:
    """``(meta, events)``; events are sorted by time and validated.

    Raises:
        ValueError: the file carries no meta record or an unusable event.
    """
    meta: Optional[Dict[str, Any]] = None
    events: List[Dict[str, Any]] = []
    with open(path, "r", encoding="utf-8") as fh:
        for lineno, line in enumerate(fh, 1):
            line = line.strip()
            if not line:
                continue
            try:
                record = json.loads(line)
            except ValueError as exc:
                raise ValueError(f"{path}:{lineno}: not valid JSON ({exc})") from exc
            kind = record.get("kind")
            if kind == KIND_META:
                meta = record.get("d", {})
                continue
            if kind not in EVENT_KINDS:
                raise ValueError(f"{path}:{lineno}: unknown event kind {kind!r}")
            if not isinstance(record.get("t"), (int, float)):
                raise ValueError(f"{path}:{lineno}: event has no numeric 't'")
            events.append(record)
    if meta is None:
        raise ValueError(f"{path}: no meta record -- not a recording")
    events.sort(key=lambda e: e["t"])
    return meta, events


def markers(events: Iterable[Dict[str, Any]]) -> List[Tuple[float, str]]:
    """``(time, name)`` of every marker in the stream."""
    return [(e["t"], e.get("name", "")) for e in events if e.get("kind") == KIND_MARKER]


def duration(events: Iterable[Dict[str, Any]]) -> float:
    times = [e["t"] for e in events]
    return max(times) if times else 0.0
