"""Braking for a mouse/keyboard driver: inject their own brake key.

The path for LFS's ``mouse_kb`` control mode. In ``wheel_js`` LFS ignores keys
for throttle and brake entirely, so this class must never be used there --
``reference/control-intervention.md`` §2.1 has the measurement.

Two decisions shape everything here.

**The binding is pushed, not guessed.** ``/key <key> brake`` makes LFS agree
with our setting instead of us hoping it already does (§3.1). Without it an
injected keystroke is a coin flip.

**The key we press is the driver's own brake key**, not a private one we
control. That keeps us out of the input path: the hardware reaches LFS
directly, and if this process dies the driver's brake still works. The price is
that our press and theirs are the same event as far as LFS is concerned, which
is what the arbitration below exists for.

Arbitration, given :class:`misc.physical_keys.PhysicalKeyState`::

    want brake      and LFS does not see the key down   -> press
    stop braking    and we pressed it, user is not      -> release
                        holding it physically

Both conditions are *observed*, not remembered, so every awkward ordering falls
out correctly:

* driver already holding when we engage -- LFS sees it down, we press nothing,
  and we have nothing to release afterwards.
* driver lets go mid-intervention -- LFS sees the key go up, so the next cycle
  presses it again (up to one assistance cycle, ~100 ms, of gap).
* driver presses while we are engaged and keeps holding after we stop -- we
  never send the release, because the key is physically down. This is the
  key-release trap, and it is the reason this class exists.

The brake is digital here: a key is on or off. Modulation belongs to the analog
path; this one brakes fully or not at all, which is defensible for an emergency
stop and nothing else.

**Residual failure mode:** if the process is killed between our press and our
release, LFS is left with the brake key down. The driver clears it by tapping
the key once. A stuck brake in a simulator is recoverable; the alternative
design (LFS's brake on a key only we own) would have left them with no brake at
all, which is why it was rejected.
"""

import logging
from typing import Optional

from misc.key_names import is_mouse_button, lfs_name_for, spelling_for
from misc.physical_keys import PhysicalKeyState
from misc.platform_shim import instant_input, is_available

logger = logging.getLogger(__name__)

_MOUSE_BUTTONS = {'mousel': 'left', 'mouser': 'right', 'mousem': 'middle'}


class KeyBrakeOutput:
    """Digital brake actuation by injecting the driver's LFS brake key."""

    def __init__(self, event_bus, settings, physical: PhysicalKeyState):
        self.event_bus = event_bus
        self.settings = settings
        self.physical = physical

        # The key the binding was last pushed for. Compared against the setting
        # on every use so a rebind in the menu takes effect without a restart,
        # the way the gearbox reads its keys at press time.
        self._bound_key: Optional[str] = None
        # True while we hold a press of our own that still needs releasing.
        self._pressed_by_us = False

    # ─── Configuration ────────────────────────────────────────────────

    @property
    def key(self) -> str:
        """The configured brake key, read fresh every time."""
        return self.settings.get('user_brake_key')

    def holds_press(self) -> bool:
        """Is there a press of ours still outstanding?

        Asked by the owning system to decide whether it must keep running even
        though the feature has been switched off: something has to release it.
        """
        return self._pressed_by_us

    def unavailable_reason(self) -> Optional[str]:
        """Why this output cannot be armed, or ``None`` if it can.

        Every caller must ask before arming. Refusing loudly here is the whole
        difference between "the assistant is off" and "the assistant is on and
        does nothing", which is the failure mode this project keeps hitting.

        Ordered by cost, cheapest first, because this runs on the assistance
        thread. ``is_available('pyautogui')`` is last of the plumbing checks
        for a reason: the first call imports pyautogui, which takes ~255 ms.
        Asked before the hook check it paid for that import on the very first
        cycle and produced a 110 ms budget overrun at every startup; asked
        after it, the answer arrives only once the background warm-up has
        already done the import.
        """
        if not self.physical.is_running():
            # Without hardware key tracking we could not tell our own release
            # from the driver's -- the key-release trap, unguarded.
            return 'no_physical_key_tracking'
        if not is_available('pyautogui'):
            return 'pyautogui_missing'
        if lfs_name_for(self.key) is None:
            return 'key_not_bindable_in_lfs'
        if self._bound_key != self.key:
            return 'binding_not_pushed'
        return None

    def push_binding(self) -> bool:
        """Tell LFS which key means brake. Returns False if the key is unusable.

        Cheap enough to repeat: one ``IS_MST``. Called when the control mode
        becomes ``mouse_kb``, after a reconnect, and after a rebind -- LFS
        forgets nothing, but we cannot read its bindings back, so the only way
        to *know* is to have written them.
        """
        key = self.key
        lfs_key = lfs_name_for(key)
        if lfs_key is None:
            logger.error("Brake key %r has no LFS spelling - automatic braking "
                         "cannot be armed for a mouse/keyboard driver.", key)
            self._bound_key = None
            return False

        if self._bound_key is not None and self._bound_key != key:
            # A rebind: the old key's tracked state says nothing about the new
            # one, and a stale "physically down" would suppress our release.
            self.physical.forget(self._bound_key)

        self.event_bus.emit('send_command_to_lfs', f"/key {lfs_key} brake")
        self._bound_key = key
        logger.info("Pushed LFS brake binding: /key %s brake", lfs_key)
        return True

    def binding_lost(self):
        """Forget that the binding was pushed (disconnect, control mode change).

        Leaves the feature unarmed until :meth:`push_binding` succeeds again,
        rather than injecting into an LFS whose bindings we have not written.
        """
        self._bound_key = None

    # ─── Actuation ────────────────────────────────────────────────────

    def apply(self, fraction) -> bool:
        """Assert or drop our share of the brake. Returns True while we press.

        *fraction* is the assistant's demand, 0..1, and this output can only
        answer it with "all" or "nothing" -- a key has no travel. Anything
        above zero is a press. The analog path is the one that modulates.

        The driver's own braking is never touched: the two are merged by LFS,
        and this method only ever adds.
        """
        key = self.key
        if fraction > 0:
            if not self.physical.down_for_lfs(key):
                self._press(key)
            return True

        self.release()
        return False

    def release(self):
        """Drop our press, if we are the one holding it.

        Always allowed, including when :class:`~misc.input_guard.InputGuard`
        would refuse a press and on shutdown: releasing can only ever remove
        braking *we* added. Leaving it asserted because LFS lost focus would
        strand the driver with a brake they did not ask for.
        """
        if not self._pressed_by_us:
            return
        key = self.key
        # The driver has taken the key over physically. Our press has become
        # their press; releasing it now would take away braking they command.
        if not self.physical.physically_down(key):
            self._release_key(key)
        self._pressed_by_us = False

    # ─── Injection ────────────────────────────────────────────────────

    def _press(self, key):
        # ``instant_input`` rather than a bare ``get_keyboard()``: pyautogui
        # sleeps 0.1 s after every call, which is a whole assistance cycle
        # spent doing nothing. Safe here because our press and release are
        # always at least one cycle apart, so nothing depends on that sleep
        # to give LFS time to see the key.
        try:
            with instant_input() as keyboard:
                if is_mouse_button(key):
                    keyboard.mouseDown(button=_MOUSE_BUTTONS[key])
                else:
                    keyboard.keyDown(spelling_for(key).pyautogui)
        except Exception as exc:
            logger.error("Brake key press failed: %s: %s", type(exc).__name__, exc)
            return
        self._pressed_by_us = True

    def _release_key(self, key):
        try:
            with instant_input() as keyboard:
                if is_mouse_button(key):
                    keyboard.mouseUp(button=_MOUSE_BUTTONS[key])
                else:
                    keyboard.keyUp(spelling_for(key).pyautogui)
        except Exception as exc:
            # Nothing sensible left to do -- the driver can clear a stuck key
            # by tapping it, and re-raising here would take the worker thread
            # down with it.
            logger.error("Brake key release failed: %s: %s",
                         type(exc).__name__, exc)
