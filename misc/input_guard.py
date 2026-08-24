"""May a key be injected into LFS right now? -- one answer for every call site.

``AutoHold`` and ``Gearbox`` press **real OS keys** through ``pyautogui``: the
keystroke goes wherever the focus happens to be. Before WP9 ``AutoHold`` looked
at ``dialog`` / ``text_entry`` and ``Gearbox`` looked at nothing at all
(``known-issues.md`` #11), so a shift while the user was typing in the LFS chat
typed the clutch and shift keys into the chat line, and a shift while the user
had alt-tabbed away typed them into their browser.

``reference/ui.md`` §1.4 lists the conditions; this module is their single
implementation:

===============  ==========================================================
Condition        Source
===============  ==========================================================
on track         ``state_data['on_track']`` -- no control input is meaningful
                 anywhere else
no text entry    ``state_data['text_entry']`` (``ISS_TEXT_ENTRY``)
no dialog        ``state_data['dialog']`` (``ISS_DIALOG``)
no Shift / Ctrl  ``OutGaugePack.Flags & OG_SHIFT|OG_CTRL`` -- LFS binds many
                 SHIFT+key shortcuts, so an injected key becomes a command
LFS has focus    the foreground window (Windows only, see
                 :func:`lfs_has_focus`)
our own car      ``own_vehicle.is_local_driver`` -- OutGauge follows the
                 camera, and shifting on a spectated car's rpm is a hazard
                 (``conventions.md`` §5.2). A car LFS drives itself
                 (``data.is_ai``) is refused for the same reason.
===============  ==========================================================

Deliberately **not** a condition: the input mode (``own_control_mode`` /
``vehicle.data.control_mode``, mouse / keyboard / joystick). The keys we press
are the keys the user bound *in LFS*, and LFS accepts them in every input mode
-- a wheel user still has a keyboard handbrake binding. Gating on the mode
would switch auto-hold and the automatic gearbox off for those users, which is
a feature removal, not a safety measure. What *is* checked is whether the car
is under our control at all: a car LFS drives itself is refused.

Cost: the guard is asked **only at the moment an actuation would happen** --
once per auto-hold engagement, once per gear change -- never per cycle. Keeping
its own state costs two attribute writes per ``state_data`` and per OutGauge
packet.

Not covered: the user's *keyboard* Shift while OutGauge is not streaming.
OutGauge only streams on track in an internal view, which is exactly when
injection may happen at all (``conventions.md`` §5.3), so the flags are fresh
whenever the guard would allow anything; a stale reading (older than
``MODIFIER_STALE_AFTER_S``) is deliberately not treated as "Shift held", or a
single lost packet would disable the feature. ``ui.md`` §1.4 mentions a
``pynput`` fallback listener if a broader guarantee is ever needed.
"""

import logging
import time
from typing import Callable, Optional

import pyinsim

from misc.platform_shim import is_windows

logger = logging.getLogger(__name__)

# Refusal reasons. Returned by :meth:`InputGuard.may_inject`, and stable enough
# to be asserted on in tests.
REASON_OFF_TRACK = 'off_track'
REASON_TEXT_ENTRY = 'text_entry'
REASON_DIALOG = 'dialog'
REASON_MODIFIER_HELD = 'modifier_held'
REASON_NO_VEHICLE = 'no_own_vehicle'
REASON_NOT_LOCAL_DRIVER = 'not_local_driver'
REASON_AI_CONTROLLED = 'ai_controlled'
REASON_LFS_NOT_FOCUSED = 'lfs_not_focused'

# A modifier reading older than this is treated as "unknown", not as "held".
MODIFIER_STALE_AFTER_S = 1.0

# One log line per distinct reason per this many seconds. A refusal is not an
# error -- it is the guard doing its job -- so it stays at debug level and must
# never turn into one message per cycle.
REASON_LOG_INTERVAL_S = 30.0

# Window titles / process names that mean "LFS is in front". LFS titles its
# window "Live for Speed"; the executable is LFS.exe.
_LFS_TITLE_MARKERS = ('live for speed', 'lfs')
_LFS_PROCESS_MARKER = 'lfs'


def lfs_has_focus() -> bool:
    """Is LFS the foreground window?

    Windows-only by nature. Everywhere else -- and on any Win32 error -- this
    returns ``True``: the check may refuse a keystroke because the user really
    is somewhere else, never because we could not ask. Failing closed here
    would silently disable auto-hold and the gearbox on a machine whose window
    title we do not recognise.
    """
    if not is_windows():
        return True
    try:
        import ctypes

        user32 = ctypes.windll.user32
        hwnd = user32.GetForegroundWindow()
        if not hwnd:
            return False

        length = user32.GetWindowTextLengthW(hwnd)
        buffer = ctypes.create_unicode_buffer(length + 1)
        user32.GetWindowTextW(hwnd, buffer, length + 1)
        title = (buffer.value or '').lower()
        if any(marker in title for marker in _LFS_TITLE_MARKERS):
            return True

        # The title did not say so -- ask the process behind the window, which
        # survives an LFS build that titles its window differently.
        pid = ctypes.c_ulong()
        user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
        if not pid.value:
            return False
        import psutil

        return _LFS_PROCESS_MARKER in psutil.Process(pid.value).name().lower()
    except Exception as exc:  # pragma: no cover - Windows-only path
        logger.debug("Foreground window check failed (%s: %s) - allowing input",
                     type(exc).__name__, exc)
        return True


class InputGuard:
    """The gate every ``pyautogui`` keypress in this project goes through.

    One instance per injecting system; it subscribes to the bus itself, so the
    systems stay wired through events only (``CLAUDE.md`` §3).

    ``foreground_check`` and ``clock`` are injectable so the whole table can be
    driven from a test without a keyboard, a window manager or a wall clock.
    """

    def __init__(self, event_bus, foreground_check: Callable[[], bool] = None,
                 clock: Callable[[], float] = time.monotonic):
        self.event_bus = event_bus
        self.clock = clock
        self.foreground_check = foreground_check or lfs_has_focus

        self.on_track = False
        self.dialog = False
        self.text_entry = False
        self._modifiers = 0
        self._modifiers_seen_at = None   # None = no OutGauge packet yet

        # When each reason was last logged, so a blocked situation that lasts
        # for minutes produces one line, not one per attempt.
        self._reported = {}

        self.event_bus.subscribe('state_data', self._on_state_data)
        self.event_bus.subscribe('outgauge_data', self._on_outgauge_data)

    # ─── Bus ──────────────────────────────────────────────────────────

    def _on_state_data(self, data):
        if not isinstance(data, dict):
            return
        self.on_track = bool(data.get('on_track', False))
        self.dialog = bool(data.get('dialog', False))
        self.text_entry = bool(data.get('text_entry', False))

    def _on_outgauge_data(self, packet):
        """Keeps ``OutGaugePack.Flags`` -- the Shift/Ctrl state (ui.md §1.4)."""
        try:
            self._modifiers = int(getattr(packet, 'Flags', 0) or 0)
        except (TypeError, ValueError):
            self._modifiers = 0
        self._modifiers_seen_at = self.clock()

    # ─── Query ────────────────────────────────────────────────────────

    def modifier_held(self) -> bool:
        """Is the user holding Shift or Ctrl, as far as OutGauge told us?"""
        if self._modifiers_seen_at is None:
            return False
        if self.clock() - self._modifiers_seen_at > MODIFIER_STALE_AFTER_S:
            return False
        return bool(self._modifiers & (pyinsim.OG_SHIFT | pyinsim.OG_CTRL))

    def may_inject(self, own_vehicle=None) -> Optional[str]:
        """``None`` when a keystroke may be sent, otherwise the refusal reason.

        Ordered cheapest first; the Win32 foreground call is last, so it only
        runs for an attempt everything else already allows.
        """
        reason = self._refusal(own_vehicle)
        if reason is not None:
            self._report(reason)
        return reason

    def _refusal(self, own_vehicle) -> Optional[str]:
        if not self.on_track:
            return REASON_OFF_TRACK
        if self.text_entry:
            return REASON_TEXT_ENTRY
        if self.dialog:
            return REASON_DIALOG
        if self.modifier_held():
            return REASON_MODIFIER_HELD

        if own_vehicle is None:
            return REASON_NO_VEHICLE
        # OutGauge describes the *viewed* car; actuating on someone else's
        # gauges is the hazard conventions.md §5.2 warns about.
        if not getattr(own_vehicle, 'is_local_driver', True):
            return REASON_NOT_LOCAL_DRIVER
        if getattr(getattr(own_vehicle, 'data', None), 'is_ai', False):
            return REASON_AI_CONTROLLED

        if not self.foreground_check():
            return REASON_LFS_NOT_FOCUSED
        return None

    def _report(self, reason: str):
        now = self.clock()
        last = self._reported.get(reason)
        if last is not None and now - last < REASON_LOG_INTERVAL_S:
            return
        self._reported[reason] = now
        logger.debug("Key injection refused: %s", reason)
