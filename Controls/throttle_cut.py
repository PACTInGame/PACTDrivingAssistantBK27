"""Taking the throttle away for the duration of an emergency intervention.

Braking while the engine still pulls is the single largest avoidable error in
an automatic stop. On a 1100 kg road car a modern engine is worth roughly 2-4
m/s² of tractive acceleration; against a 9 m/s² brake demand that is a third of
the deceleration gone, and the stopping distance grows by the same fraction --
from ~12 m at 50 km/h to ~17 m. Every production AEB cuts the throttle first
and brakes second, and so does this.

Taking the function away, and why that is not enough
====================================================

The first half is to take the **function** away from the input for as long as
the intervention lasts::

    engage    /key -1 throttle        LFS stops reading the throttle key
              /axis -1 throttle       LFS stops reading the throttle axis
    release   /key <their key> throttle
              /axis <their axis> throttle  +  /invert <their polarity> throttle

and the same control-mode split as the brake decides which pair is used: keys
are simply ignored for throttle in ``wheel_js``, axes do not exist in
``mouse_kb`` (§2.1).

**On the key path that is not sufficient, and it was measured not to be**
(known-issues #46). ``/key -1 throttle`` stops LFS reading *new* presses, but
an input the driver is **already holding** keeps delivering full throttle:
LFS latches the held state and does not re-evaluate it until the input is
released. In ``simulation_tests`` scenario 08 the cut went out and OutGauge
reported ``Throttle = 1.00`` for the entire braking phase; scenario 26 (a
different hazard, the same path) did the same. The car stopped anyway, because
the brake beats the engine, but it stopped over a third more distance than it
had to, and the system reported a cut that never happened.

So the key path also **un-presses the input**, which the brake path may never
do. The asymmetry is the whole point:

===============  =========================================  ==================
                 what an injected release does              is that allowed?
===============  =========================================  ==================
brake            takes away braking the driver commanded     **never** (§3.1,
                                                             the key-release
                                                             trap)
throttle         takes away throttle the driver commanded     yes - that is
                                                             the feature
===============  =========================================  ==================

``misc/physical_keys.py`` separates "the driver is holding it" from "LFS thinks
it is down" using ``LLKHF_INJECTED``, so the release is issued only against
LFS's belief, and on handback the input is pressed again **only if the driver
never let go**. Without those hooks running the key path refuses to arm rather
than report a cut it cannot deliver -- in practice they are always up by then,
because the brake half of the same intervention refuses without them too.

Fail-safe: a process that dies between the release and the handback leaves the
driver with a throttle that needs one tap to come back. That is the benign
direction, and it is the reverse of what a stranded *brake* press would do.

The asymmetry that decides the safety design
============================================

**Engaging needs no knowledge; releasing does.** ``-1`` is the same command for
everyone, but the restore has to name the input the driver actually uses, and
LFS holds exactly one input per function: naming the wrong one takes the
function away from the right input *and* destroys whatever the named one was
doing (the box in §2.2). The two paths therefore earn their trust differently:

* **key** -- this app writes the binding itself (``/key <key> throttle``, the
  way ``brake_key`` does), so LFS and our setting are consistent by
  construction. A wrong setting shows up as a throttle key that does not work
  from the moment the app starts, not as a surprise after the first
  intervention.
* **axis** -- nobody can write an axis assignment blind: ``/axis n throttle``
  *is* the destructive command. The number is therefore read out of LFS's own
  controller file (:mod:`misc.lfs_config`), anchored on the brake axis we
  already know, and the path still stays refused until the whole chain has been
  proven end to end by :mod:`Controls.throttle_axis_check`.

**Restoring the axis restores only the axis.** Measured: after ``/axis -1
throttle`` the ``/invert`` flag is gone too, so a plain ``/axis 9 throttle``
gives the driver an *inverted* throttle -- a released pedal reads as full
throttle. The polarity comes out of the same controller file and is pushed back
with the assignment, always, as one operation.

Both paths register their restore in the shared handover marker before they cut
anything, so a process killed mid-intervention leaves ``guardian.py`` able to
give the throttle back too -- otherwise the driver would be left with a car that
does not accelerate and no idea why.

Cost: two or three ``IS_MST`` at each end of an intervention, nothing per cycle.
"""

import logging
from typing import List, Optional

from Controls.handover_marker import HandoverMarker
from misc.key_names import is_mouse_button, lfs_name_for, spelling_for
from misc.platform_shim import instant_input

logger = logging.getLogger(__name__)

MARKER_OWNER = 'throttle'

# pyautogui names the mouse buttons differently from LFS and from
# ``settings.json``; the same table as in ``Controls/brake_key.py``.
_MOUSE_BUTTONS = {'mousel': 'left', 'mouser': 'right', 'mousem': 'middle'}


class _ThrottleCut:
    """Shared shape: cut on engage, restore on release, never twice."""

    def __init__(self, event_bus, settings, marker: HandoverMarker):
        self.event_bus = event_bus
        self.settings = settings
        self.marker = marker
        self._engaged = False

    # ─── State ────────────────────────────────────────────────────────

    def holds_throttle(self) -> bool:
        """Is the throttle currently taken away from the driver?"""
        return self._engaged

    def unavailable_reason(self) -> Optional[str]:
        raise NotImplementedError

    # ─── Actuation ────────────────────────────────────────────────────

    def engage(self, force: bool = False) -> bool:
        """Take the throttle away. Returns True while it is ours.

        The marker is claimed **before** the cut goes out, so there is no
        window in which LFS has stopped reading the throttle and nothing on
        disk says how to give it back.

        *force* is for :mod:`Controls.throttle_axis_check` alone: the check
        exists to clear the very refusal that would otherwise block it, and it
        has to exercise this exact code path rather than a copy of it -- a
        verification of something other than what runs in anger verifies
        nothing.
        """
        if self._engaged:
            return True
        if not force and self.unavailable_reason() is not None:
            return False
        commands = self._restore_commands()
        if not commands:
            return False
        self.marker.claim(MARKER_OWNER, commands)
        self.event_bus.emit('send_command_to_lfs', self._cut_command())
        self._engaged = True
        logger.info("Throttle cut for the intervention (%s).",
                    self._cut_command())
        return True

    def release(self):
        """Give the throttle back. Safe from any state, and idempotent.

        Restore first, marker second: if anything fails in between, the worst
        case is a guardian that sends a command LFS has already carried out.
        The reverse order could leave the throttle unassigned with nothing left
        that knows it.
        """
        if not self._engaged:
            return
        self._engaged = False
        commands = self._restore_commands()
        for command in commands:
            self.event_bus.emit('send_command_to_lfs', command)
        self.marker.release(MARKER_OWNER)
        logger.info("Throttle handed back to the driver (%s).",
                    ', '.join(commands))

    # ─── Commands ─────────────────────────────────────────────────────

    def _cut_command(self) -> str:
        raise NotImplementedError

    def _restore_commands(self) -> List[str]:
        raise NotImplementedError


class KeyThrottleCut(_ThrottleCut):
    """``mouse_kb``: unassign the throttle key *and* un-press it.

    The key is the driver's own, and this class pushes that binding into LFS
    itself, for the same reason ``KeyBrakeOutput`` does: a restore can only be
    trusted if we were the ones who wrote what it restores. The injected
    release is the half that actually removes throttle from a held input --
    see the module docstring.
    """

    def __init__(self, event_bus, settings, marker: HandoverMarker,
                 physical=None):
        super().__init__(event_bus, settings, marker)
        self._bound_key: Optional[str] = None
        # ``misc.physical_keys.PhysicalKeyState``. Optional so the command
        # half can still be exercised without a hook, but the cut refuses to
        # arm without it (see ``unavailable_reason``).
        self.physical = physical
        self._released_by_us = False

    @property
    def key(self) -> str:
        """The configured throttle key, read fresh every time."""
        return self.settings.get('user_throttle_key')

    def unavailable_reason(self) -> Optional[str]:
        if lfs_name_for(self.key) is None:
            return 'throttle_key_not_bindable_in_lfs'
        if self._bound_key != self.key:
            return 'throttle_binding_not_pushed'
        if self.physical is None or not self.physical.is_running():
            # Without the hooks we cannot tell our own injected release from
            # the driver's, so we could neither un-press safely nor know
            # whether to press again on handback. Saying so beats reporting a
            # cut that leaves the engine pulling (known-issues #46).
            return 'no_physical_key_tracking'
        return None

    def push_binding(self) -> bool:
        """Tell LFS which key means throttle. False if the key is unusable.

        One ``IS_MST``, cheap enough to repeat after a reconnect or a rebind.
        Not sent while the throttle is cut -- that would hand it back early.
        """
        key = self.key
        lfs_key = lfs_name_for(key)
        if lfs_key is None:
            logger.error("Throttle key %r has no LFS spelling - the throttle "
                         "cannot be cut for a mouse/keyboard driver.", key)
            self._bound_key = None
            return False
        if self._engaged:
            return False
        self.event_bus.emit('send_command_to_lfs', f"/key {lfs_key} throttle")
        self._bound_key = key
        logger.info("Pushed LFS throttle binding: /key %s throttle", lfs_key)
        return True

    def binding_lost(self):
        """Forget the binding (disconnect, rebind). Leaves this path unarmed."""
        self._bound_key = None

    def _cut_command(self) -> str:
        return "/key -1 throttle"

    def _restore_commands(self) -> List[str]:
        lfs_key = lfs_name_for(self.key)
        return [f"/key {lfs_key} throttle"] if lfs_key else []

    # ─── The half that removes a *held* throttle ──────────────────────

    def suppress(self):
        """Un-press the throttle input, if LFS currently believes it is down.

        Called every cycle while the intervention runs, not once on engage: a
        driver who lets go and stands on it again mid-intervention produces a
        fresh press, and that one has to go the same way as the first.

        Costs one dictionary lookup per cycle while nothing is held.
        """
        if not self._engaged or self.physical is None:
            return
        key = self.key
        if not self.physical.down_for_lfs(key):
            return
        if self._inject(key, press=False):
            self._released_by_us = True

    def release(self):
        """Give the throttle back: the binding, and the press if it is owed.

        The press comes back only when the driver **never let go**. If they
        released while we had it suppressed, LFS already agrees with their
        hardware and injecting anything here would give them throttle they are
        not asking for.
        """
        was_engaged = self._engaged
        super().release()
        if not was_engaged:
            return
        key = self.key
        if self._released_by_us and self.physical is not None \
                and self.physical.physically_down(key) \
                and not self.physical.down_for_lfs(key):
            self._inject(key, press=True)
        self._released_by_us = False

    def _inject(self, key, press: bool) -> bool:
        """One injected press or release. False if it could not be sent.

        ``instant_input`` for the same reason as in ``Controls/brake_key.py``:
        pyautogui sleeps 0.1 s after every call, which is a whole assistance
        cycle spent doing nothing.
        """
        try:
            with instant_input() as keyboard:
                if is_mouse_button(key):
                    (keyboard.mouseDown if press else keyboard.mouseUp)(
                        button=_MOUSE_BUTTONS[key])
                else:
                    (keyboard.keyDown if press else keyboard.keyUp)(
                        spelling_for(key).pyautogui)
        except Exception as exc:
            logger.error("Throttle key %s failed: %s: %s",
                         'press' if press else 'release',
                         type(exc).__name__, exc)
            return False
        return True


class AxisThrottleCut(_ThrottleCut):
    """``wheel_js``: unassign the throttle axis, then point it back.

    Everything it needs is *measured*, never typed in:

    * which axis, and with which polarity, from LFS's own controller file,
      anchored on the brake axis an intervention proves every time it hands
      back (:mod:`misc.lfs_config`);
    * that LFS really is reading the driver's throttle pedal right now, from
      :class:`~misc.pedal_watch.PedalWatch`'s running confidence;
    * that the cut and the restore actually work, from
      :mod:`Controls.throttle_axis_check`, once.

    Any one of those missing means the throttle is not cut. The brake
    intervention is unaffected by all of it -- it is the more important half and
    must never wait on the lesser one.
    """

    # How sure we have to be that LFS is reading the pedal we think it is.
    # 1.0 is "a quarter-minute of driving during which every sample agreed and
    # the pedal was actually used"; see PedalWatch.confidence.
    REQUIRED_CONFIDENCE = 1.0

    def __init__(self, event_bus, settings, marker: HandoverMarker,
                 pedals=None, lfs_axes=None):
        super().__init__(event_bus, settings, marker)
        self.pedals = pedals
        # Callable returning the resolved axis assignments, or None. Injected
        # so the file reading can be tested without a file.
        self._lfs_axes = lfs_axes
        self._resolved = None
        self._resolve_failed = False

    # ─── What LFS is configured to do ─────────────────────────────────

    def assignment(self):
        """The driver's throttle axis and polarity, read once and remembered.

        ``None`` while it is not known. A failed read is remembered as failed:
        the file does not appear halfway through a session, and retrying it
        every cycle would put a file open in the hot path.
        """
        if self._resolved is not None or self._resolve_failed:
            return self._resolved
        if self._lfs_axes is None:
            self._resolve_failed = True
            return None
        assignments = self._lfs_axes()
        if not assignments or 'throttle' not in assignments:
            self._resolve_failed = True
            return None
        self._resolved = assignments['throttle']
        if self.settings.get('user_axis_throttle') != self._resolved.axis:
            # LFS's own file now says something different from what we last
            # acted on -- the driver reassigned the throttle in Options and
            # LFS rewrote the file on exit. Whatever verdict the old number
            # earned says nothing about this one, in either direction.
            logger.info("The throttle axis changed from %s to %d - it will be "
                        "verified again.",
                        self.settings.get('user_axis_throttle'),
                        self._resolved.axis)
            self.settings.set('throttle_axis_verified', False)
            self.settings.set('throttle_axis_broken', False)
        # Keep the settings file honest: it is what a human reads when
        # something looks wrong.
        self.settings.set('user_axis_throttle', self._resolved.axis)
        return self._resolved

    @property
    def axis(self) -> Optional[int]:
        assignment = self.assignment()
        return None if assignment is None else assignment.axis

    def confidence(self) -> float:
        if self.pedals is None:
            return 0.0
        return self.pedals.confidence('throttle')

    def unavailable_reason(self) -> Optional[str]:
        if self.settings.get('throttle_axis_broken'):
            # A restore that did not work once will not work the next time
            # either, and trying again would break another axis.
            return 'throttle_restore_failed'
        assignment = self.assignment()
        if assignment is None:
            return 'throttle_axis_unknown'
        if not 0 <= assignment.axis <= 31:
            return 'throttle_axis_out_of_range'
        if assignment.axis == self.settings.get('user_axis_brake'):
            # One axis, one function: if these two agree, one of them is wrong,
            # and restoring would point the throttle at the brake pedal.
            return 'throttle_and_brake_axis_identical'
        if self.confidence() < self.REQUIRED_CONFIDENCE:
            return 'throttle_pedal_not_confirmed'
        if not self.settings.get('throttle_axis_verified'):
            return 'throttle_axis_not_verified'
        return None

    # ─── Commands ─────────────────────────────────────────────────────

    def _cut_command(self) -> str:
        return "/axis -1 throttle"

    def _restore_commands(self) -> List[str]:
        """Assignment **and** polarity, always both.

        Measured: ``/axis -1 throttle`` clears the invert flag along with the
        assignment, so restoring the axis alone gives the driver a throttle
        that reads full when the pedal is released. That was diagnosed as "the
        configured axis number is wrong" for a whole session before the
        controller file showed ``throttle invert 1``.
        """
        assignment = self.assignment()
        if assignment is None:
            return []
        return [f"/axis {assignment.axis} throttle",
                f"/invert {assignment.invert} throttle"]
