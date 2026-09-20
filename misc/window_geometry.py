"""Where the LFS window is on screen, for anything that has to aim at it.

Windows only, ctypes only, and every accessor answers ``None`` rather than
raising when it cannot tell -- the same contract as :mod:`misc.platform_shim`,
so the modules that use it stay importable and testable off Windows.

This duplicates a little of ``simulation_tests/win_focus.py`` on purpose. That
package is the in-game test harness and is deliberately independent of the
add-on (``simulation_tests/README.md``): nothing in ``core``, ``assistance``,
``ui``, ``lfs``, ``vehicles`` or ``misc`` may import from it, and the rule is
worth more than the twenty lines it costs.
"""

import logging
from typing import Optional, Tuple

logger = logging.getLogger(__name__)

# Title fragment that identifies the game's window.
LFS_WINDOW_MATCH = "Live for Speed"

try:  # pragma: no cover - Windows only
    import ctypes
    from ctypes import wintypes

    _user32 = ctypes.windll.user32
    _ENUM_PROC = ctypes.WINFUNCTYPE(ctypes.c_bool, ctypes.c_void_p,
                                    ctypes.POINTER(ctypes.c_int))
    IS_WINDOWS = True
except Exception:  # pragma: no cover - anywhere else
    ctypes = None
    wintypes = None
    _user32 = None
    _ENUM_PROC = None
    IS_WINDOWS = False

_SM_CXSCREEN, _SM_CYSCREEN = 0, 1


def screen_size() -> Optional[Tuple[int, int]]:
    """``(width, height)`` of the primary screen, or ``None``."""
    if not IS_WINDOWS:
        return None
    try:  # pragma: no cover - Windows only
        return (_user32.GetSystemMetrics(_SM_CXSCREEN),
                _user32.GetSystemMetrics(_SM_CYSCREEN))
    except Exception as exc:
        logger.debug("Screen size query failed: %s: %s", type(exc).__name__, exc)
        return None


def _window_title(hwnd) -> str:  # pragma: no cover - Windows only
    length = _user32.GetWindowTextLengthW(hwnd)
    if not length:
        return ""
    buffer = ctypes.create_unicode_buffer(length + 1)
    _user32.GetWindowTextW(hwnd, buffer, length + 1)
    return buffer.value


def lfs_rect(match: str = LFS_WINDOW_MATCH) -> Optional[Tuple[int, int, int, int]]:
    """``(left, top, right, bottom)`` of the LFS window, or ``None``.

    ``None`` means "cannot tell" -- not running, not Windows, or the API said
    no. Callers fall back to the screen, which is right for a full-screen game
    and close enough for a maximised window.
    """
    if not IS_WINDOWS:
        return None
    try:  # pragma: no cover - Windows only
        needle = match.lower()
        found = []

        def _callback(hwnd, _lparam):
            if _user32.IsWindowVisible(hwnd) and needle in _window_title(hwnd).lower():
                found.append(hwnd)
                return False
            return True

        _user32.EnumWindows(_ENUM_PROC(_callback), None)
        if not found:
            return None
        rect = wintypes.RECT()
        if not _user32.GetWindowRect(wintypes.HWND(found[0]), ctypes.byref(rect)):
            return None
        if rect.right <= rect.left or rect.bottom <= rect.top:
            return None
        return (rect.left, rect.top, rect.right, rect.bottom)
    except Exception as exc:
        logger.debug("LFS window rect query failed: %s: %s",
                     type(exc).__name__, exc)
        return None


def lfs_centre() -> Optional[Tuple[int, int]]:
    """Middle of the LFS window, falling back to the middle of the screen."""
    rect = lfs_rect()
    if rect is not None:
        left, top, right, bottom = rect
        return ((left + right) // 2, (top + bottom) // 2)
    size = screen_size()
    if size is None:
        return None
    return (size[0] // 2, size[1] // 2)


def lfs_width() -> Optional[int]:
    """Width of the LFS window, falling back to the screen width."""
    rect = lfs_rect()
    if rect is not None:
        return rect[2] - rect[0]
    size = screen_size()
    return None if size is None else size[0]
