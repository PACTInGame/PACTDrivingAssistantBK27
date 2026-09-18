"""Foreground-window and screen-geometry helpers (Windows, ctypes only).

Replaying a scenario means injecting **global OS input**. The project's own rule
for the add-on applies here with more force: never send keys or clicks unless LFS
is the foreground window (reference/ui.md §1.4). If the user alt-tabs mid-replay,
the remaining keystrokes would land in whatever has focus.

Off Windows every function returns ``None`` -- "unknown", not "fine". Callers
must treat ``None`` as a refusal when they need a guarantee, which is why the
replay's ``--require-focus`` default fails closed.
"""

from __future__ import annotations

import sys
from typing import List, Optional, Tuple

IS_WINDOWS = sys.platform.startswith("win")

if IS_WINDOWS:  # pragma: no cover - needs Windows
    import ctypes
    from ctypes import wintypes

    _user32 = ctypes.windll.user32
    _SM_CXSCREEN, _SM_CYSCREEN = 0, 1

    _EnumWindowsProc = ctypes.WINFUNCTYPE(
        wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)


def screen_size() -> Optional[Tuple[int, int]]:
    """Primary screen size in pixels, or None off Windows."""
    if not IS_WINDOWS:  # pragma: no cover
        return None
    return (_user32.GetSystemMetrics(_SM_CXSCREEN),
            _user32.GetSystemMetrics(_SM_CYSCREEN))


def _window_title(hwnd) -> str:  # pragma: no cover - needs Windows
    length = _user32.GetWindowTextLengthW(hwnd)
    if length <= 0:
        return ""
    buf = ctypes.create_unicode_buffer(length + 1)
    _user32.GetWindowTextW(hwnd, buf, length + 1)
    return buf.value


def foreground_title() -> Optional[str]:
    """Title of the foreground window, or None off Windows."""
    if not IS_WINDOWS:  # pragma: no cover
        return None
    hwnd = _user32.GetForegroundWindow()
    if not hwnd:
        return ""
    return _window_title(hwnd)


def find_windows(match: str) -> List[Tuple[int, str]]:
    """Visible top-level windows whose title contains ``match`` (case-insensitive)."""
    if not IS_WINDOWS:  # pragma: no cover
        return []
    needle = match.lower()
    found: List[Tuple[int, str]] = []

    def _callback(hwnd, _lparam):  # pragma: no cover - needs Windows
        if _user32.IsWindowVisible(hwnd):
            title = _window_title(hwnd)
            if needle in title.lower():
                found.append((int(hwnd), title))
        return True

    _user32.EnumWindows(_EnumWindowsProc(_callback), 0)
    return found


def window_rect(hwnd: int) -> Optional[Tuple[int, int, int, int]]:
    """``(left, top, right, bottom)`` of a window, or None."""
    if not IS_WINDOWS:  # pragma: no cover
        return None
    rect = wintypes.RECT()
    if not _user32.GetWindowRect(wintypes.HWND(hwnd), ctypes.byref(rect)):
        return None
    return (rect.left, rect.top, rect.right, rect.bottom)


def any_key_physically_down() -> Optional[bool]:
    """Is the human holding a key or mouse button right now? None off Windows.

    A replay that starts while the driver still has a key down fights them for
    the whole run, and the release-everything cleanup will not release it (the
    replay never pressed it). Worth refusing before injecting anything.
    """
    if not IS_WINDOWS:  # pragma: no cover
        return None
    # 0x01..0xFE covers mouse buttons and every virtual key; the high bit of
    # GetAsyncKeyState is "currently down".
    return any(_user32.GetAsyncKeyState(vk) & 0x8000 for vk in range(1, 255))


def lfs_window(match: str) -> Optional[dict]:
    """First LFS window with its rect, or None if not found / not Windows."""
    for hwnd, title in find_windows(match):
        return {"hwnd": hwnd, "title": title, "rect": window_rect(hwnd)}
    return None


def is_foreground(match: str) -> Optional[bool]:
    """True/False when it can be decided, None when the platform cannot tell."""
    title = foreground_title()
    if title is None:
        return None
    return match.lower() in title.lower()
