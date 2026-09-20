"""Injecting mouse movement that a game actually sees.

``pyautogui.moveTo`` moves the cursor with ``SetCursorPos``. That is the right
call for clicking a button on a form and the wrong one for steering a car:
**Raw Input and DirectInput never see it.** ``SetCursorPos`` moves the cursor
the window manager draws; it does not generate an input event, so a game that
reads the mouse through ``WM_INPUT`` -- which is how every modern game reads a
mouse, LFS included -- is told nothing at all.

That is not a theory. The first live run in which the parking manoeuvre moved
the car commanded full steering lock for ten seconds and measured a path
curvature of ``+0.000 1/m`` throughout, while the throttle -- injected as a
*key*, through ``SendInput`` -- worked perfectly. The learned steering gain
collapsed to its floor, which is the model correctly concluding that steering
did nothing.

``SendInput`` with ``MOUSEEVENTF_MOVE`` is the same path a physical mouse
takes: Raw Input sees it, DirectInput sees it, and the cursor moves too, so
anything reading the cursor position still works. It is strictly the better
call and there is no case where the other one is preferable.

### Relative, and what that costs

Raw mouse input is **relative** -- a mouse reports "12 counts left", not "at
x=840" -- so this module moves by deltas and the caller keeps track of where
it has got to. What the caller tracks is its own idea of the position, and it
can drift from the game's when the game clamps at its own limit; the caller
clamps to the same span for exactly that reason, and the control loop above it
(``Controls/vehicle_control.CurvatureModel``) measures what the car actually
did rather than trusting either number.

Windows only, ctypes only, and every function answers ``False`` rather than
raising when it cannot do the job -- the same contract as
:mod:`misc.platform_shim`, so this module stays importable off Windows.
"""

import logging
from typing import Optional

logger = logging.getLogger(__name__)

try:  # pragma: no cover - Windows only
    import ctypes
    from ctypes import wintypes

    _user32 = ctypes.windll.user32
    IS_WINDOWS = True
except Exception:  # pragma: no cover - anywhere else
    ctypes = None
    wintypes = None
    _user32 = None
    IS_WINDOWS = False

INPUT_MOUSE = 0
MOUSEEVENTF_MOVE = 0x0001

if IS_WINDOWS:  # pragma: no cover - Windows only
    class _MOUSEINPUT(ctypes.Structure):
        _fields_ = [('dx', wintypes.LONG), ('dy', wintypes.LONG),
                    ('mouseData', wintypes.DWORD), ('dwFlags', wintypes.DWORD),
                    ('time', wintypes.DWORD),
                    ('dwExtraInfo', ctypes.POINTER(ctypes.c_ulong))]

    class _INPUT(ctypes.Structure):
        class _UNION(ctypes.Union):
            _fields_ = [('mi', _MOUSEINPUT)]
        _anonymous_ = ('u',)
        _fields_ = [('type', wintypes.DWORD), ('u', _UNION)]


def available() -> bool:
    """Can mouse movement be injected at all on this machine?"""
    return IS_WINDOWS and _user32 is not None


def move_relative(dx: int, dy: int = 0) -> bool:
    """Move the mouse by *dx*, *dy* counts. Returns whether it was sent.

    A zero move is a success and sends nothing: the caller asks on every
    control cycle and the command usually has not changed.
    """
    if not available():
        return False
    dx, dy = int(dx), int(dy)
    if dx == 0 and dy == 0:
        return True
    try:  # pragma: no cover - Windows only
        event = _INPUT(type=INPUT_MOUSE,
                       mi=_MOUSEINPUT(dx=dx, dy=dy, mouseData=0,
                                      dwFlags=MOUSEEVENTF_MOVE, time=0,
                                      dwExtraInfo=None))
        sent = _user32.SendInput(1, ctypes.byref(event), ctypes.sizeof(event))
        if sent != 1:
            logger.debug("SendInput sent %s of 1 mouse events.", sent)
            return False
        return True
    except Exception as exc:
        logger.error("Injecting mouse movement failed: %s: %s",
                     type(exc).__name__, exc)
        return False


def cursor_position() -> Optional[tuple]:
    """Where the cursor is now, or ``None``. For diagnostics only."""
    if not available():
        return None
    try:  # pragma: no cover - Windows only
        point = wintypes.POINT()
        if not _user32.GetCursorPos(ctypes.byref(point)):
            return None
        return (point.x, point.y)
    except Exception:
        return None
