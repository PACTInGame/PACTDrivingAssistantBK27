"""The three input devices a manoeuvre drives, for a mouse/keyboard driver.

:class:`~Controls.vehicle_control.VehicleController` says *what* to do; these
say *how*, on the one control mode this feature ships with. They are duck-typed
rather than an inheritance hierarchy, because all three have in common is that
they can be asked to do something and can refuse.

### Steering: the mouse cursor

LFS's mouse steering reads the **absolute cursor position**, not relative
movement: the recorded scenarios show the cursor sweeping from x 460 to 1690
across a 1920-wide screen while the car is being steered, with no recentring.
So steering the car means putting the cursor somewhere, which
``pyautogui.moveTo`` does.

What a given cursor offset is worth in curvature is **not** assumed. It cannot
be: it depends on the screen, on ``/wheel_turn``, and on the car's steering lock
(``conventions.md`` §4). :class:`~Controls.vehicle_control.CurvatureModel`
learns it from ``yaw_rate / speed`` while the manoeuvre runs, and everything
here has to provide is a command in -1..1 that maps monotonically onto cursor
offset. :data:`DEFAULT_SPAN_FRACTION` therefore sets the *resolution* of the
command, not its meaning.

**The vertical axis is driven too, to neutral.** In mouse mode LFS takes
throttle and brake from the cursor's Y position, and a cursor left wherever the
driver happened to leave it is a throttle setting nobody asked for -- in the
recorded parking scenario the cursor sits 120 px above centre for the whole
idle window. Parking it on the centre line removes that input so the keys below
are the only longitudinal command. It can only ever *remove* mouse throttle or
mouse braking, never add either.

### Throttle and brake: the driver's own keys

A key has no travel, so this is on-or-off and the controller above does the
modulating by switching. The keys are the ones the driver configured and that
the emergency brake already pushes bindings for, so nothing new is taken away
from them -- and mouse buttons count as keys here, which is how a mouse driver
is covered by the same mechanism (``control-intervention.md`` §2.2, measured).

Braking reuses :class:`~Controls.brake_key.KeyBrakeOutput` unchanged, including
its arbitration against the driver's own key. That class exists because of the
key-release trap and there is no second implementation of it.

### Gears: shift up and down until the right one is in

No new bindings. LFS keeps exactly one key per function, so a private "select
reverse" key would have to take the driver's ``reverse`` key away
(``control-intervention.md`` §2.2); shifting with the keys the automatic
gearbox already uses takes nothing. The gear is read back from OutGauge, so the
result is observed rather than assumed.
"""

import logging
from typing import Optional

from Controls.brake_key import KeyBrakeOutput
from misc import window_geometry
from misc.key_names import is_mouse_button, spelling_for
from misc.key_tap import get_key_tapper
from misc.platform_shim import instant_input, is_available

logger = logging.getLogger(__name__)

# How much of the LFS window's half width a full steering command uses. Only
# the resolution of the command depends on this -- what it is worth in
# curvature is measured (see the module docstring). A quarter of the half width
# is roughly what the recorded scenarios' steering sweeps cover.
DEFAULT_SPAN_FRACTION = 0.25
# Below this the window is not believable and the output refuses rather than
# aiming at coordinates that are not on any screen.
MIN_WINDOW_WIDTH_PX = 320

# Gear codes, mirrored from ``Controls.vehicle_control`` so this module does not
# import it -- they are OutGauge's, not either module's (``conventions.md`` §3).
GEAR_REVERSE = 0
GEAR_NEUTRAL = 1

_MOUSE_BUTTONS = {'mousel': 'left', 'mouser': 'right', 'mousem': 'middle'}


class MouseSteeringOutput:
    """Steers by putting the cursor somewhere, for an LFS mouse driver."""

    def __init__(self, settings, geometry=window_geometry):
        self.settings = settings
        self.geometry = geometry
        self._held = False
        self._last_x = None

    # ─── Availability ─────────────────────────────────────────────────

    def unavailable_reason(self) -> Optional[str]:
        """Why the cursor cannot be used to steer, or ``None``."""
        if not is_available('pyautogui'):
            return 'pyautogui_missing'
        if self.geometry.lfs_centre() is None:
            return 'lfs_window_not_found'
        width = self.geometry.lfs_width()
        if not width or width < MIN_WINDOW_WIDTH_PX:
            return 'lfs_window_not_found'
        return None

    # ─── Actuation ────────────────────────────────────────────────────

    def set(self, value: float) -> bool:
        """Command steering in -1..1. Returns False when nothing was sent."""
        centre = self.geometry.lfs_centre()
        width = self.geometry.lfs_width()
        if centre is None or not width:
            return False
        value = max(-1.0, min(1.0, float(value)))
        span = width * 0.5 * float(
            self.settings.get('park_assist_mouse_span') or DEFAULT_SPAN_FRACTION)
        # Positive curvature is a left turn, and left on screen is a *smaller*
        # x. Getting this sign wrong steers into the obstacle the manoeuvre is
        # avoiding, so it is spelled out rather than folded into the maths.
        x = int(round(centre[0] - value * span))
        y = centre[1]
        if self._last_x == x and self._held:
            return True
        try:
            with instant_input() as mouse:
                mouse.moveTo(x, y)
        except Exception as exc:
            logger.error("Steering the mouse failed: %s: %s",
                         type(exc).__name__, exc)
            return False
        self._held = True
        self._last_x = x
        return True

    def release(self):
        """Put the cursor back on the centre line and stop steering.

        Centre rather than "wherever it was": the driver gets the car back with
        the wheel straight, which is the only handback that is safe without
        knowing what they were doing with it before.
        """
        if not self._held:
            return
        self._held = False
        self._last_x = None
        centre = self.geometry.lfs_centre()
        if centre is None:
            return
        try:
            with instant_input() as mouse:
                mouse.moveTo(centre[0], centre[1])
        except Exception as exc:
            logger.error("Centring the mouse failed: %s: %s",
                         type(exc).__name__, exc)


class KeyPedalOutput:
    """Throttle and brake as keys, for an LFS mouse/keyboard driver."""

    def __init__(self, event_bus, settings, physical_keys):
        self.event_bus = event_bus
        self.settings = settings
        self.brake_output = KeyBrakeOutput(event_bus, settings, physical_keys)
        self.physical = physical_keys
        self._throttle_down = False

    @property
    def throttle_key(self) -> str:
        """Read at press time, so a rebind in the menu takes effect at once."""
        return self.settings.get('user_throttle_key')

    def unavailable_reason(self) -> Optional[str]:
        reason = self.brake_output.unavailable_reason()
        if reason is not None:
            return reason
        if spelling_for(self.throttle_key) is None:
            return 'throttle_key_unknown'
        return None

    def push_bindings(self) -> bool:
        """Make LFS agree with our settings about which keys these are.

        The same reasoning as ``KeyBrakeOutput.push_binding``: an injected
        keystroke only does anything if LFS has that key bound to that
        function, and we cannot read its bindings back, so the only way to know
        is to have written them.
        """
        ok = self.brake_output.push_binding()
        key = self.throttle_key
        spelling = spelling_for(key)
        if spelling is None or spelling.lfs is None:
            logger.error("Throttle key %r has no LFS spelling - a manoeuvre "
                         "cannot drive this car.", key)
            return False
        self.event_bus.emit('send_command_to_lfs',
                            f"/key {spelling.lfs} throttle")
        return ok

    def set(self, throttle: float, brake: float) -> bool:
        """Apply both pedals. Either may be zero; both being zero is a coast."""
        ok = True
        if brake > 0.0:
            self.brake_output.apply(1.0)
        else:
            self.brake_output.release()
        if throttle > 0.0:
            ok = self._press_throttle() and ok
        else:
            self._release_throttle()
        return ok

    def release(self):
        """Drop both. Always allowed, including on shutdown."""
        self._release_throttle()
        self.brake_output.release()

    # ─── Throttle ─────────────────────────────────────────────────────

    def _press_throttle(self) -> bool:
        key = self.throttle_key
        if self.physical.down_for_lfs(key):
            # Already down -- either ours from last cycle or the driver's.
            # Either way LFS sees throttle and a second press adds nothing.
            self._throttle_down = True
            return True
        spelling = spelling_for(key)
        if spelling is None:
            return False
        try:
            with instant_input() as keyboard:
                if is_mouse_button(key):
                    keyboard.mouseDown(button=_MOUSE_BUTTONS[key])
                else:
                    keyboard.keyDown(spelling.pyautogui)
        except Exception as exc:
            logger.error("Throttle key press failed: %s: %s",
                         type(exc).__name__, exc)
            return False
        self._throttle_down = True
        return True

    def _release_throttle(self):
        """Drop our throttle press, unless the driver is holding the key.

        The mirror image of the brake's rule and it points the same way here:
        our release would take away throttle *the driver commanded*, so it is
        suppressed while the key is physically down. Their own release will
        arrive when they let go.
        """
        if not self._throttle_down:
            return
        self._throttle_down = False
        key = self.throttle_key
        if self.physical.physically_down(key):
            return
        spelling = spelling_for(key)
        if spelling is None:
            return
        try:
            with instant_input() as keyboard:
                if is_mouse_button(key):
                    keyboard.mouseUp(button=_MOUSE_BUTTONS[key])
                else:
                    keyboard.keyUp(spelling.pyautogui)
        except Exception as exc:
            logger.error("Throttle key release failed: %s: %s",
                         type(exc).__name__, exc)


class GearSelector:
    """Selects forward or reverse with the driver's own shift keys.

    One request per call, never a burst: the gear is read back from OutGauge
    and the caller asks again next cycle if it has not arrived. LFS takes about
    300 ms over a shift and answering that with more keystrokes overshoots.
    """

    def __init__(self, settings, tapper=None, clutch_hold_s: float = 0.30,
                 shift_hold_s: float = 0.10, clutch_lead_s: float = 0.10):
        self.settings = settings
        self.tapper = tapper or get_key_tapper()
        self.clutch_hold_s = clutch_hold_s
        self.shift_hold_s = shift_hold_s
        self.clutch_lead_s = clutch_lead_s
        self._current_target = None

    def unavailable_reason(self) -> Optional[str]:
        for setting in ('user_shift_up_key', 'user_shift_down_key',
                        'user_clutch_key'):
            if spelling_for(self.settings.get(setting)) is None:
                return 'shift_key_unknown'
        return None

    def select(self, gear: int) -> bool:
        """Shift one step towards *gear*. Returns whether a request went out.

        The caller has already established that the car is stopped; this only
        decides which way to shift. From neutral, reverse is one step down and
        first is one step up, which is how a sequential box is laid out in LFS.
        """
        self._current_target = gear
        direction = 'down' if gear <= GEAR_NEUTRAL else 'up'
        return self._shift(direction)

    def release(self):
        """Nothing is held between shifts; the tapper owns its own keys.

        It is still worth existing: the controller releases all three outputs
        together, and an output that cannot be released is one that gets
        forgotten when a fourth is added.
        """
        self._current_target = None

    def _shift(self, direction: str) -> bool:
        shift_key = self.settings.get('user_shift_up_key' if direction == 'up'
                                      else 'user_shift_down_key')
        clutch_key = self.settings.get('user_clutch_key')
        # Same timing as the automatic gearbox: clutch down, gear 100 ms later,
        # both released on their own. The tapper holds them on its own thread,
        # so this returns in microseconds (``misc/key_tap.py``).
        if not self.tapper.tap(clutch_key, hold_s=self.clutch_hold_s):
            return False
        if not self.tapper.tap(shift_key, hold_s=self.shift_hold_s,
                               delay_s=self.clutch_lead_s):
            return False
        logger.info("Parking manoeuvre shift %s (towards gear %s).",
                    direction, self._current_target)
        return True
