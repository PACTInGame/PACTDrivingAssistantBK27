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
not a replay     ``state_data['replay']``. Since known-issues #55 the warning
                 systems and the HUD *do* run during a replay, so ``on_track``
                 alone no longer implies "nothing is happening"; a keystroke
                 there would land in the live game, not in the recording
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
OutGauge alive   the stream has produced a packet within
                 ``OUTGAUGE_STALE_AFTER_S`` **of the moment it was due to
                 produce one**. Without it every gauge field and
                 ``viewed_plid`` stand still, so no actuator can tell whose
                 car it would be acting on (``known-issues.md`` #51). LFS
                 sends OutGauge while the player sits in a car, so the clock
                 only runs while that is the case -- see
                 :meth:`InputGuard.outgauge_stale`.
===============  ==========================================================

The input mode is not a global guard condition: shift keys work in both modes.
Clutch and handbrake are different: wheel users may select an axis instead of
a key. Each actuator must verify its own result; AutoHold waits for the
handbrake dashboard light before reporting success. Disk configuration can be
stale while LFS is running and cannot prove the current input path.

Cost: the guard is asked **only at the moment an actuation would happen** --
once per auto-hold engagement, once per gear change -- never per cycle. Keeping
its own state costs two attribute writes per ``state_data`` and per OutGauge
packet.

Not covered: the user's *keyboard* Shift while OutGauge is not streaming.
OutGauge streams whenever the player sits in a car, in every camera view
(``conventions.md`` §5.3), which covers everything injection may happen in at
all, so the flags are fresh whenever the guard would allow anything; a stale reading (older than
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
REASON_NO_OUTGAUGE = 'no_outgauge'
REASON_REPLAY = 'replay'

# A modifier reading older than this is treated as "unknown", not as "held".
MODIFIER_STALE_AFTER_S = 1.0
# ...but the *stream* going quiet for this long is a different statement, and a
# much bigger one: no OutGauge means no pedals, no gauges and no ``viewed_plid``
# -- so nothing can tell whose car we are looking at, and nothing may actuate.
# Generous on purpose: LFS streams at 10-100 Hz, so three seconds is dozens of
# missed packets, not a hiccup.
OUTGAUGE_STALE_AFTER_S = 3.0

# One log line per distinct reason per this many seconds. A refusal is not an
# error -- it is the guard doing its job -- so it stays at debug level and must
# never turn into one message per cycle.
REASON_LOG_INTERVAL_S = 30.0

# What "LFS is in front" looks like. The **process** is the authoritative
# answer: LFS ships as ``LFS.exe``. The window title is only the fallback, for
# a renamed executable, and it has to be the full product name -- a bare "lfs"
# substring also matches a browser tab on the LFS forum, a file manager in a
# folder called LFS, or this project's own window, and each of those would let
# a keystroke through to exactly the application we are trying to protect.
_LFS_PROCESS_NAMES = frozenset(('lfs', 'lfs_dbg'))
_LFS_TITLE_MARKER = 'live for speed'


def looks_like_lfs(window_title, process_name) -> bool:
    """Does this foreground window belong to LFS?

    Split out from the Win32 plumbing so the matching rule itself is testable
    without Windows.
    """
    stem = (process_name or '').rsplit('.', 1)[0].strip().lower()
    if stem in _LFS_PROCESS_NAMES:
        return True
    return _LFS_TITLE_MARKER in (window_title or '').lower()


def lfs_has_focus() -> bool:
    """Is LFS the foreground window?

    Windows-only by nature. Everywhere else -- and on any Win32 error -- this
    returns ``True``: the check may refuse a keystroke because the user really
    is somewhere else, never because we could not ask. Failing closed here
    would silently disable auto-hold and the gearbox on a machine whose window
    we cannot inspect.
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
        title = buffer.value or ''

        # Ask the process behind the window first: it survives an LFS build
        # that titles its window differently, and it cannot be spoofed by a
        # browser tab that happens to mention LFS.
        process_name = ''
        pid = ctypes.c_ulong()
        user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
        if pid.value:
            import psutil
            process_name = psutil.Process(pid.value).name()

        return looks_like_lfs(title, process_name)
    except Exception as exc:  # pragma: no cover - Windows-only path
        logger.debug("Foreground window check failed (%s: %s) - allowing input",
                     type(exc).__name__, exc)
        return True


class InputGuard:
    """The gate every ``pyautogui`` keypress in this project goes through.

    One instance per injecting system; it subscribes to the bus itself, so the
    systems stay wired through events only (``AGENTS.md`` §3).

    ``foreground_check`` and ``clock`` are injectable so the whole table can be
    driven from a test without a keyboard, a window manager or a wall clock.
    """

    def __init__(self, event_bus, foreground_check: Callable[[], bool] = None,
                 clock: Callable[[], float] = time.monotonic):
        self.event_bus = event_bus
        self.clock = clock
        self.foreground_check = foreground_check or lfs_has_focus

        self.on_track = False
        self.replay = False
        # Seit wann OutGauge ueberhaupt senden *muesste*. LFS streamt, solange
        # der Spieler in einem Auto sitzt; im Menue schweigt der Strom voellig
        # zu Recht. Ohne diese Marke lief die Stille-Uhr im Menue mit, und der
        # erste Zyklus nach dem Streckeneintritt meldete "kein OutGauge" --
        # genau einen Frame lang, mit einer Notification "AEB nicht
        # verfuegbar" daran.
        self._streaming_expected_since = None
        self.dialog = False
        self.text_entry = False
        self._modifiers = 0
        self._modifiers_seen_at = None   # None = no OutGauge packet yet
        # Was the socket opened at all? ``None`` until the connector says.
        # Kept apart from the packet clock: a socket that never bound and a
        # camera that stopped the stream are the same refusal but not the same
        # advice, and the log has to be able to say which.
        self._outgauge_bound = None
        self._outgauge_bind_reason = None
        self._outgauge_bound_at = None

        # When each reason was last logged, so a blocked situation that lasts
        # for minutes produces one line, not one per attempt.
        self._reported = {}

        self.event_bus.subscribe('state_data', self._on_state_data)
        self.event_bus.subscribe('outgauge_data', self._on_outgauge_data)
        self.event_bus.subscribe('outgauge_status', self._on_outgauge_status)

    # ─── Bus ──────────────────────────────────────────────────────────

    def _on_state_data(self, data):
        if not isinstance(data, dict):
            return
        was_expected = self.on_track or self.replay
        self.on_track = bool(data.get('on_track', False))
        self.replay = bool(data.get('replay', False))
        if (self.on_track or self.replay) != was_expected:
            # Beide Richtungen: der Eintritt startet die Karenzzeit neu, das
            # Verlassen macht die Frage bedeutungslos.
            self._streaming_expected_since = (
                self.clock() if not was_expected else None)
        self.dialog = bool(data.get('dialog', False))
        self.text_entry = bool(data.get('text_entry', False))

    def _on_outgauge_data(self, packet):
        """Keeps ``OutGaugePack.Flags`` -- the Shift/Ctrl state (ui.md §1.4)."""
        try:
            self._modifiers = int(getattr(packet, 'Flags', 0) or 0)
        except (TypeError, ValueError):
            self._modifiers = 0
        self._modifiers_seen_at = self.clock()

    def _on_outgauge_status(self, data):
        """Der Socket wurde geoeffnet oder eben nicht (``lfs/connector.py``)."""
        if not isinstance(data, dict):
            return
        self._outgauge_bound = bool(data.get('bound', False))
        self._outgauge_bind_reason = data.get('reason')
        self._outgauge_bound_at = self.clock()

    # ─── Query ────────────────────────────────────────────────────────

    def outgauge_stale(self) -> bool:
        """Is the OutGauge stream silent right now?

        True means every gauge field and every pedal reading is standing
        still. Two causes, both of them real (``conventions.md`` §5.3 and
        ``known-issues.md`` #51): the socket never bound because something else
        holds port 30000, or ``OutGauge Mode`` is 0 in ``cfg.txt``. The camera
        is **not** one of them -- OutGauge keeps streaming in chase, heli and
        TV view (``known-issues.md`` #29, withdrawn after measuring it).

        Off track the stream stops and that is correct, so the question is not
        asked there; while it is asked, the clock runs from whichever came
        later, the last packet or the moment the stream became due. Without
        that, a minute in the menu came back as a minute of failure.

        Unlike :meth:`modifier_held` this one fails *closed*: acting on gauges
        that are standing still is the hazard, not the safeguard.
        """
        if self._outgauge_bound is False:
            return True
        if self._outgauge_bound is None:
            # Nobody has told us whether the socket is open -- a bare
            # ``InputGuard`` in a test, or a caller that does not run the
            # connector. The module's rule applies: refuse because the user
            # really is elsewhere, never because we could not ask.
            return False
        if not (self.on_track or self.replay):
            # LFS sends OutGauge while the player sits in a car. In the menu
            # the silence *is* the correct behaviour, and nothing may actuate
            # there anyway -- ``off_track`` answers that question, not this
            # one.
            return False
        # The clock starts at whichever came later: the last packet, or the
        # moment the stream became due. Measuring from the packet alone let a
        # minute in the menu count as a minute of failure.
        since = self._modifiers_seen_at
        expected = self._streaming_expected_since
        if since is None or (expected is not None and expected > since):
            since = expected
        if since is None:
            # Bound, but not one packet yet. ``OutGauge Mode = 0`` in
            # ``cfg.txt`` looks exactly like this and never recovers.
            since = self._outgauge_bound_at
            if since is None:
                return False
        return self.clock() - since > OUTGAUGE_STALE_AFTER_S

    def outgauge_reason(self) -> Optional[str]:
        """Why OutGauge is silent, in one word, or ``None`` if it is not."""
        if not self.outgauge_stale():
            return None
        return self._outgauge_bind_reason or 'no_packets'

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
        # Vor ``off_track``, obwohl beides zutrifft: ein Replay ist kein
        # "nicht auf der Strecke", sondern ein Zustand, in dem Systeme
        # laufen (known-issues #55) und ein Tastendruck ins *laufende* Spiel
        # ginge. Der Grund soll die Welt beschreiben, nicht die Variable.
        if self.replay:
            return REASON_REPLAY
        if not self.on_track:
            return REASON_OFF_TRACK
        if self.text_entry:
            return REASON_TEXT_ENTRY
        if self.dialog:
            return REASON_DIALOG
        if self.modifier_held():
            return REASON_MODIFIER_HELD
        # Before ``is_local_driver``, and deliberately so. Without OutGauge
        # that property is False for a driver who is sitting in their own car,
        # and the refusal that came out said ``not_local_driver`` -- a true
        # statement about a variable and a misleading one about the world.
        if self.outgauge_stale():
            return REASON_NO_OUTGAUGE

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
