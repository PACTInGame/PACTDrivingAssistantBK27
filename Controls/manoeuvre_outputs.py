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

### Throttle and brake: the driver's own keys, pulsed

A key has no travel, so a fractional pedal is a **duty cycle**: the key is
held for that fraction of the control period and released for the rest, which
the car's inertia averages into a fractional pedal.
:class:`~Controls.pulse_modulator.PulseModulator` does the arithmetic and
:mod:`misc.key_tap` does the holding, on its own thread -- never on the
assistance thread (``ui.md`` §1.6).

This used to be on-or-off, with the controller above modulating by switching
between the two. It does not work, and the reason is specific to LFS: the car
**creeps in gear with no throttle at all**, so at the 1.1 m/s a manoeuvre runs
at, a whole cycle of throttle is far more than the loop asked for. The live
trace is in ``pulse_modulator.py``; the symptom the driver sees is a car that
alternates full throttle and full brake all the way into the space.

The keys are the ones the driver configured and that the emergency brake
already pushes bindings for, so nothing new is taken away from them -- and
mouse buttons count as keys here, which is how a mouse driver is covered by
the same mechanism (``control-intervention.md`` §2.2, measured).

:class:`~Controls.brake_key.KeyBrakeOutput` is still here, but only as the
authority on whether the brake key is *usable*: it owns ``/key <k> brake`` and
the refusals that go with it, and there is no second implementation of that.
The pressing is the modulator's, because a held press and a pulsed one cannot
share a key.

### Gears: shift up and down until the right one is in

No new bindings. LFS keeps exactly one key per function, so a private "select
reverse" key would have to take the driver's ``reverse`` key away
(``control-intervention.md`` §2.2); shifting with the keys the automatic
gearbox already uses takes nothing. The gear is read back from OutGauge, so the
result is observed rather than assumed.
"""

import logging
import time
from typing import Optional

from Controls.brake_key import KeyBrakeOutput
from Controls.pulse_modulator import PulseModulator
from misc import mouse_input, window_geometry
from misc.key_names import spelling_for
from misc.key_tap import get_key_tapper
from misc.platform_shim import instant_input, is_available

logger = logging.getLogger(__name__)

# How much of the LFS window's half width a full steering command uses.
#
# This was a quarter, on the reasoning that only the *resolution* of the
# command depends on it and what it is worth in curvature gets measured. That
# reasoning is sound right up to the point where the command saturates, and a
# quarter saturates long before the car turns: measured in game, a full +1.00
# command put the cursor 240 px from the centre of a 1920 px window -- exactly
# where it was asked to go -- and the car's path curvature came back at
# 0.002 1/m. A 500 m radius. The manoeuvre left its path within three strokes
# every time, and the learned gain sank to its floor, which is the model
# correctly reporting that steering was doing nothing.
#
# LFS's mouse steering uses very nearly the whole window width: the recorded
# scenarios show the driver's own cursor sweeping from x 460 to 1690 of 1920.
# So a full command has to mean very nearly the whole half width. Slightly
# under it rather than exactly, to stay off the screen edge -- pyautogui's
# own corner failsafe lives there, and clamping is the game's job, not ours.
DEFAULT_SPAN_FRACTION = 0.95
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
        self._last_trace = 0.0

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
        self._trace(value, x, span, width)
        return True

    def _trace(self, value, wanted_x, span, width):
        """Did the cursor actually go where we put it? DEBUG, once a second.

        The one question the car's own telemetry cannot answer. A live run
        commanded full lock for ten seconds and measured no curvature at all,
        and from the outside that looks the same whether the cursor never
        moved, moved somewhere useless, or moved exactly as intended and the
        span is simply far too small to steer with. This line separates the
        three. Costs nothing unless DEBUG is on.
        """
        if not logger.isEnabledFor(logging.DEBUG):
            return
        now = time.monotonic()
        if now - self._last_trace < 1.0:
            return
        self._last_trace = now
        logger.debug("Steering: command %+.2f -> x %d (span %.0f px of a "
                     "%.0f px window); cursor is at %s",
                     value, wanted_x, span, width, mouse_input.cursor_position())

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
    """Throttle and brake as pulsed keys, for an LFS mouse/keyboard driver."""

    def __init__(self, event_bus, settings, physical_keys, tapper=None):
        self.event_bus = event_bus
        self.settings = settings
        # Kept for what it owns -- the LFS brake binding and the refusals that
        # come with it. Its ``apply`` is not used; see the module docstring.
        self.brake_output = KeyBrakeOutput(event_bus, settings, physical_keys)
        self.physical = physical_keys
        tapper = tapper or get_key_tapper()
        period = self._period_s()
        self._throttle = PulseModulator(tapper, period, name='throttle')
        self._brake = PulseModulator(tapper, period, name='brake')

    # ─── Configuration ────────────────────────────────────────────────

    @property
    def throttle_key(self) -> str:
        """Read at press time, so a rebind in the menu takes effect at once."""
        return self.settings.get('user_throttle_key')

    @property
    def brake_key(self) -> str:
        return self.brake_output.key

    def _period_s(self) -> float:
        """The control period the duty cycle is measured over.

        Read from the setting rather than assumed, because
        ``assistance_refresh_rate`` is adjustable (50-200 ms) and a duty cycle
        against the wrong period is simply the wrong pedal.
        """
        try:
            rate = float(self.settings.get('assistance_refresh_rate') or 100.0)
        except (TypeError, ValueError):
            rate = 100.0
        return max(0.02, min(0.5, rate / 1000.0))

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

    # ─── Actuation ────────────────────────────────────────────────────

    def set(self, throttle: float, brake: float) -> bool:
        """Apply both pedals as fractions, 0..1. Both zero is a coast.

        Costs two float comparisons and at most two enqueues on the key
        tapper; nothing here sleeps or touches pyautogui, so it is safe on the
        assistance thread (``ui.md`` §1.6).
        """
        period = self._period_s()
        self._throttle.period_s = period
        self._brake.period_s = period
        # Brake first. The two are never both non-zero, but if a caller ever
        # asks for both, the brake is the one that should win.
        ok = self._brake.apply(self.brake_key, brake)
        return self._throttle.apply(self.throttle_key, throttle) and ok

    def release(self):
        """Drop both. Always allowed, including on shutdown.

        A pulse carries its own release, but a *continuous* hold is re-armed
        every cycle and would otherwise stay down for two more periods after
        the last one. Handing the car back with the throttle still on for a
        fifth of a second is not a handback, so the holds are cut here.
        """
        self._throttle.release(self.throttle_key)
        self._brake.release(self.brake_key)


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
