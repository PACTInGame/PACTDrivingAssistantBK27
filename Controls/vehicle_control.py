"""Turning a :class:`ControlDemand` into steering, pedals and a gear.

The layer below :mod:`assistance.parking.path_follower`, and the last one that
is still car-agnostic. It takes "1.1 m/s, backwards, curving left at 1/6 per
metre" and produces "steer this far, throttle this much, be in reverse" --
without knowing what a parking slot is, and without touching an input device
itself. The devices are injected, so the whole controller is testable with
three recording stubs and no Windows.

### Curvature, not steering angle

The demand is a curvature because that is the quantity this project can
*measure*: ``yaw_rate / speed``, straight out of ``CompCar`` (``conventions.md``
§2-3). A steering angle would need a wheelbase and a steering lock, and
``conventions.md`` §4 is unambiguous about tables keyed on ``CName`` -- every
vehicle mod would get somebody else's car.

So the controller learns instead. :class:`CurvatureModel` watches what
curvature each steering command actually produced and fits the one number that
connects them::

    kappa = gain * command

It starts from a deliberately **weak** guess: an under-estimated gain asks for
more steering than needed, and the loop corrects it within a metre; an
over-estimated one asks for less and the car misses the corner. The integral
trim removes whatever the fit has not learned yet, so the first manoeuvre in an
unknown car works while the model is still settling.

### What it refuses to do

Everything here runs behind ``misc/input_guard.py`` and behind
``own_vehicle.is_local_driver``; those checks live in the state machine above,
because they decide whether a *manoeuvre* may run at all, not whether one
control cycle may. What this class does own is the rule that
**a gear change happens at a standstill or not at all**: LFS will not take
reverse from a rolling car, and a manoeuvre that thinks it is reversing while
the car creeps forward is the one failure this design cannot recover from.
"""

import logging
import math
import time
from dataclasses import dataclass
from typing import Optional

from assistance.parking.path_follower import ControlDemand

logger = logging.getLogger(__name__)

# ─── The steering model ───────────────────────────────────────────────────

# Curvature produced per unit of steering command, before anything is measured.
# 0.05 1/m means a full command is assumed to give only a 20 m radius -- far
# less than any car does. That direction is the safe one: the controller asks
# for too much steering, sees too much curvature, and the fit pulls it back
# within a metre of travel. The opposite error is a corner taken too wide with
# a parked car on the outside of it.
DEFAULT_CURVATURE_GAIN = 0.05
# The gain can never be learned as less than this, or the command needed for
# any real curvature would exceed full lock and the output would just saturate.
MIN_CURVATURE_GAIN = 0.01
MAX_CURVATURE_GAIN = 2.0

# A sample only teaches something when the car is actually moving and the wheel
# is actually turned. Below these, ``yaw_rate / speed`` is noise divided by
# nothing.
LEARN_MIN_SPEED_MPS = 0.35
LEARN_MIN_COMMAND = 0.12
# How fast the fit moves. An exponential filter rather than a least-squares
# accumulator: the relationship is one number, it changes when the car changes,
# and a filter forgets the old car by itself. 0.08 settles in roughly 30
# samples, i.e. 3 s at the 100 ms cycle.
GAIN_LEARNING_RATE = 0.08

# ─── The loops ────────────────────────────────────────────────────────────

# Integral trim on the curvature error, in command units per (1/m) per second.
# It exists to remove what the model has not learned yet, so it is slow enough
# not to fight the feedforward and bounded so it can never become the whole
# command.
CURVATURE_TRIM_RATE = 1.2
MAX_CURVATURE_TRIM = 0.6

# Longitudinal control is bang-bang with hysteresis, because the throttle on a
# mouse/keyboard driver's car is a key and a key has no travel. At the 1.1 m/s
# a manoeuvre runs at, a +/-15 % band is +/-0.17 m/s and the resulting sawtooth
# is smaller than the noise in the speed reading.
SPEED_DEADBAND = 0.15
# Below this fraction of the demand the brake comes in rather than just lifting
# off. Coasting is enough for small overspeeds; a real overspeed needs braking.
BRAKE_OVERSPEED = 1.45
# And a demand of zero always brakes, whatever the speed: that is a stop, not a
# coast.
STOP_BRAKE_SPEED_MPS = 0.15

# ─── Gears ────────────────────────────────────────────────────────────────

# ``OutGaugePack.Gear``: 0 = reverse, 1 = neutral, 2 = first (conventions §3).
GEAR_REVERSE = 0
GEAR_NEUTRAL = 1
GEAR_FIRST = 2
# The car has to be this slow before a gear change is even attempted. Same
# number as the follower's standstill, and for the same reason.
GEAR_CHANGE_SPEED_MPS = 0.08
# One shift request per this long, so a slow LFS response is not answered with
# a second one. The gearbox's own tap takes 300 ms end to end.
GEAR_REQUEST_INTERVAL_S = 0.45
# How long a gear change may fail to arrive before the manoeuvre is told.
GEAR_TIMEOUT_S = 4.0


@dataclass(frozen=True)
class VehicleState:
    """What the car is doing, in the units this controller thinks in.

    Assembled by the caller from OutGauge and MCI, so that nothing below this
    line has to know which packet a number came from.
    """
    speed_mps: float          # magnitude, never signed
    yaw_rate: float           # rad/s, positive anticlockwise (conventions §2)
    gear: int                 # OutGauge Gear: 0 = R, 1 = N, 2 = first
    reversing: bool           # is the car actually moving backwards?


@dataclass(frozen=True)
class ControlStatus:
    """What the controller did this cycle, for the screen and the log."""
    steer: float = 0.0
    throttle: float = 0.0
    brake: float = 0.0
    gear_wanted: int = GEAR_NEUTRAL
    gear_ready: bool = True
    curvature_error: float = 0.0
    # Set when something the controller depends on is not working. The state
    # machine turns this into an abort; the controller never aborts itself.
    fault: Optional[str] = None


class CurvatureModel:
    """How much curvature a steering command produces, learned while driving.

    One number, filtered. It is deliberately not a least-squares fit over a
    history: the relationship changes when the car changes, and a filter
    forgets the old one without anybody having to notice that it should.
    """

    def __init__(self, gain: float = DEFAULT_CURVATURE_GAIN,
                 learning_rate: float = GAIN_LEARNING_RATE):
        self.gain = _clamp(gain, MIN_CURVATURE_GAIN, MAX_CURVATURE_GAIN)
        self.learning_rate = learning_rate
        self.samples = 0

    def observe(self, command: float, state: VehicleState):
        """Feed one cycle's measurement. Cheap: a divide and a filter step."""
        if state.speed_mps < LEARN_MIN_SPEED_MPS or abs(command) < LEARN_MIN_COMMAND:
            return
        # The car's path curvature. Reversing does not change it: yaw rate and
        # speed are both measured about the same body, and the *path* still
        # curves the way the wheels point.
        signed_speed = -state.speed_mps if state.reversing else state.speed_mps
        measured = state.yaw_rate / signed_speed
        observed = measured / command
        if not math.isfinite(observed):
            return
        observed = _clamp(observed, MIN_CURVATURE_GAIN, MAX_CURVATURE_GAIN)
        self.gain += self.learning_rate * (observed - self.gain)
        self.gain = _clamp(self.gain, MIN_CURVATURE_GAIN, MAX_CURVATURE_GAIN)
        self.samples += 1

    def command_for(self, curvature: float) -> float:
        """The steering command that should produce *curvature*."""
        return _clamp(curvature / self.gain, -1.0, 1.0)

    def curvature_for(self, command: float) -> float:
        """The inverse, for comparing what was asked with what arrived."""
        return command * self.gain


class VehicleController:
    """Drives one manoeuvre through injected steering, pedal and gear outputs.

    The three outputs are duck-typed rather than subclassed, because they have
    nothing in common but being asked to do something and being able to refuse:

    * ``steering.set(value) -> bool`` with ``value`` in -1..1, plus
      ``release()`` and ``unavailable_reason()``;
    * ``pedals.set(throttle, brake) -> bool``, plus ``release()`` and
      ``unavailable_reason()``;
    * ``gears.select(gear) -> bool``, returning whether the request was sent.
    """

    def __init__(self, steering, pedals, gears,
                 model: Optional[CurvatureModel] = None, clock=time.monotonic):
        self.steering = steering
        self.pedals = pedals
        self.gears = gears
        self.model = model or CurvatureModel()
        self.clock = clock
        self._trim = 0.0
        self._last_command = 0.0
        self._last_update = None
        self._gear_requested_at = None
        self._gear_waiting_since = None

    # ─── Lifecycle ────────────────────────────────────────────────────

    def unavailable_reason(self) -> Optional[str]:
        """Why this controller cannot run, or ``None``. Asked before arming."""
        for output in (self.steering, self.pedals, self.gears):
            reason = getattr(output, 'unavailable_reason', lambda: None)()
            if reason is not None:
                return reason
        return None

    def reset(self):
        """Forget the loop state, keep what has been learned about the car."""
        self._trim = 0.0
        self._last_command = 0.0
        self._last_update = None
        self._gear_requested_at = None
        self._gear_waiting_since = None

    def release(self):
        """Give every input back. Always allowed, from any state, any thread.

        The one method that must never be blocked by a guard: it can only ever
        *return* control to the driver (``control-intervention.md`` §1).
        """
        for output in (self.pedals, self.steering, self.gears):
            try:
                output.release()
            except Exception as exc:
                logger.error("Releasing %s failed: %s: %s",
                             type(output).__name__, type(exc).__name__, exc)
        self.reset()

    # ─── One cycle ────────────────────────────────────────────────────

    def apply(self, demand: ControlDemand, state: VehicleState) -> ControlStatus:
        """Actuate one control cycle. Costs a handful of float operations."""
        now = self.clock()
        dt = 0.0 if self._last_update is None else max(0.0, now - self._last_update)
        self._last_update = now

        # Learn from what the *previous* command did before issuing a new one.
        self.model.observe(self._last_command, state)

        gear_wanted = _gear_for(demand.direction)
        gear_ready = self._manage_gear(gear_wanted, state, now)

        steer = self._steer_command(demand, state, dt)
        throttle, brake = self._pedal_commands(demand, state, gear_ready)

        fault = None
        if not self.steering.set(steer):
            fault = 'steering_failed'
            steer = 0.0
        if not self.pedals.set(throttle, brake):
            fault = fault or 'pedals_failed'
        if (self._gear_waiting_since is not None
                and now - self._gear_waiting_since > GEAR_TIMEOUT_S):
            fault = fault or 'gear_change_failed'

        self._last_command = steer
        return ControlStatus(steer=steer, throttle=throttle, brake=brake,
                             gear_wanted=gear_wanted, gear_ready=gear_ready,
                             curvature_error=demand.curvature
                             - self.model.curvature_for(steer),
                             fault=fault)

    # ─── Lateral ──────────────────────────────────────────────────────

    def _steer_command(self, demand: ControlDemand, state: VehicleState,
                       dt: float) -> float:
        """Feedforward from the learned gain, plus an integral trim.

        The trim only runs while the car is moving fast enough for the measured
        curvature to mean anything. Integrating an error computed from a
        standstill would wind the command to full lock while the car sits
        still, and then apply it the moment it rolls.
        """
        command = self.model.command_for(demand.curvature)
        if dt > 0.0 and state.speed_mps >= LEARN_MIN_SPEED_MPS:
            signed_speed = -state.speed_mps if state.reversing else state.speed_mps
            measured = state.yaw_rate / signed_speed
            error = demand.curvature - measured
            self._trim = _clamp(self._trim + CURVATURE_TRIM_RATE * error * dt,
                                -MAX_CURVATURE_TRIM, MAX_CURVATURE_TRIM)
        elif state.speed_mps < LEARN_MIN_SPEED_MPS:
            # Standing still between strokes: let the trim decay rather than
            # carrying one stroke's correction into the next, which curves the
            # other way.
            self._trim *= 0.9
        return _clamp(command + self._trim, -1.0, 1.0)

    # ─── Longitudinal ─────────────────────────────────────────────────

    def _pedal_commands(self, demand: ControlDemand, state: VehicleState,
                        gear_ready: bool):
        """Bang-bang with hysteresis; the throttle is a key, not a pedal.

        Three cases, in order of how much they matter:

        * the gear is not in yet, or the demand is zero -- **brake**, because
          both mean the car has to be still and neither is a coast;
        * well over the demanded speed -- brake;
        * under it -- throttle. In between, neither: coasting is what a car
          does at walking pace and it is smoother than either pedal.
        """
        if not gear_ready or demand.speed <= 0.0:
            return 0.0, (1.0 if state.speed_mps > STOP_BRAKE_SPEED_MPS else 0.35)
        if state.speed_mps > demand.speed * BRAKE_OVERSPEED:
            return 0.0, 0.5
        if state.speed_mps < demand.speed * (1.0 - SPEED_DEADBAND):
            return 1.0, 0.0
        return 0.0, 0.0

    # ─── Gears ────────────────────────────────────────────────────────

    def _manage_gear(self, wanted: int, state: VehicleState, now: float) -> bool:
        """Ask for the gear the stroke needs; answer whether it is in.

        Nothing is requested while the car is still rolling. That is not
        caution, it is how LFS works: reverse is not selectable from a moving
        car, and a request that is silently ignored would leave the manoeuvre
        believing it had a gear it does not have.
        """
        if state.gear == wanted:
            self._gear_requested_at = None
            self._gear_waiting_since = None
            return True
        if state.speed_mps > GEAR_CHANGE_SPEED_MPS:
            return False
        if self._gear_waiting_since is None:
            self._gear_waiting_since = now
        if (self._gear_requested_at is not None
                and now - self._gear_requested_at < GEAR_REQUEST_INTERVAL_S):
            return False
        if self.gears.select(wanted):
            self._gear_requested_at = now
        return False


def _gear_for(direction: int) -> int:
    return GEAR_REVERSE if direction < 0 else GEAR_FIRST


def _clamp(value: float, low: float, high: float) -> float:
    return max(low, min(high, value))
