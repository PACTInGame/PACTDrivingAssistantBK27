"""The whole stack, offline: plan, follow, control, pulse keys, move a car.

``test_parking_follower.py`` drives the follower against a car that does
exactly what it is told. This file does not. Its car is the one in the game:

* it **creeps in gear with no throttle at all**, which is most of a parking
  manoeuvre's speed demand and the single fact that broke the first design;
* its throttle and brake are **keys**, held for whole milliseconds by the
  pulse modulator and sampled by the car every 10 ms, so a demand of 0.3 is
  three hundredths of a second of pedal and not a number;
* its brake has far more authority than its throttle, as a real one does.

So this is the offline replica of the live test, and it is the loop to use
before going back in game. What it cannot say anything about is whether a
keystroke reaches LFS at all -- that is what ``simulation_tests/`` is for.

The two numbers it pins are the two the live runs got wrong: how much the
speed wanders off its demand, and how often the throttle is down. A live run
recorded ``thr 1.00`` on nearly every line and the speed sawing between 0.3
and 2.3 m/s against a 1.1 m/s demand; if either of those comes back, one of
these fails.
"""

import math

import pytest

from assistance.parking.geometry import normalise_angle, swept_collision
from assistance.parking.path_follower import PathFollower
from assistance.parking.trajectory import (ParkingPlanner, advance,
                                           planning_radius_for)
from Controls.pulse_modulator import PulseModulator
from Controls.vehicle_control import (GEAR_FIRST, GEAR_NEUTRAL, GEAR_REVERSE,
                                      VehicleController, VehicleState)
from tests.test_parking_follower import SHAPE, parallel_scene

# ─── The car ──────────────────────────────────────────────────────────────

PHYSICS_DT = 0.01                # LFS runs its physics at 100 Hz
CYCLE_S = 0.10                   # the assistance cycle
LOCK_RADIUS_M = 6.0              # what the car can steer at full lock
MAX_CURVATURE = 1.0 / LOCK_RADIUS_M
MAX_CURVATURE_RATE = 0.35        # 1/m per second, lock to lock in about a second

# In gear with the throttle shut, LFS's auto-clutch lets the car crawl. 1.0 m/s
# is the order of magnitude a live run showed in reverse -- close enough to the
# 1.1 m/s crawl that a controller which ignores it has almost no work left to
# do, and every bit of work it does do is an overshoot.
CREEP_MPS = 1.0
CREEP_ACCEL = 1.6
THROTTLE_ACCEL = 3.0
# Sized for 50 m/s, used at 1 m/s. This asymmetry is why the controller scales
# its braking demand (``BRAKE_AUTHORITY``).
BRAKE_ACCEL = 5.0
DRAG_ACCEL = 0.25
# A gear change is not instant in LFS either.
SHIFT_DELAY_S = 0.35


class FakeClock:
    def __init__(self):
        self.now = 0.0

    def __call__(self):
        return self.now


class TimedTapper:
    """A key tapper whose holds expire against the simulation's own clock."""

    def __init__(self, clock):
        self.clock = clock
        self._until = {}

    def tap(self, key, hold_s=0.1, delay_s=0.0):
        if hold_s <= 0.0:
            self._until[key] = self.clock()
        else:
            self._until[key] = self.clock() + delay_s + hold_s
        return True

    def down(self, key):
        return self._until.get(key, 0.0) > self.clock() + 1e-12


class PulsedPedals:
    """``KeyPedalOutput`` with the LFS-specific parts taken out."""

    def __init__(self, tapper):
        self.throttle = PulseModulator(tapper, CYCLE_S, 'throttle')
        self.brake = PulseModulator(tapper, CYCLE_S, 'brake')

    def set(self, throttle, brake):
        self.brake.apply('B', brake)
        self.throttle.apply('T', throttle)
        return True

    def release(self):
        self.throttle.release('T')
        self.brake.release('B')

    def unavailable_reason(self):
        return None


class RecordingSteering:
    def __init__(self):
        self.value = 0.0

    def set(self, value):
        self.value = value
        return True

    def release(self):
        self.value = 0.0

    def unavailable_reason(self):
        return None


class DelayedGears:
    """Shifts one step per request, and takes LFS's own time over it."""

    def __init__(self, clock):
        self.clock = clock
        self.gear = GEAR_FIRST
        self._pending = None
        self._at = 0.0

    def select(self, gear):
        if self._pending is None:
            self._pending = GEAR_REVERSE if gear <= GEAR_NEUTRAL else GEAR_FIRST
            self._at = self.clock() + SHIFT_DELAY_S
        return True

    def settle(self):
        if self._pending is not None and self.clock() >= self._at:
            self.gear = self._pending
            self._pending = None

    def release(self):
        self._pending = None

    def unavailable_reason(self):
        return None


class SimulatedCar:
    """Longitudinal creep and pedals, lateral kinematic bicycle."""

    def __init__(self, pose, clock, tapper, gears):
        self.pose = pose
        self.clock = clock
        self.tapper = tapper
        self.gears = gears
        self.speed = 0.0
        self.curvature = 0.0
        self.yaw_rate = 0.0
        self.path = [pose]

    @property
    def direction(self):
        return -1 if self.gears.gear == GEAR_REVERSE else 1

    def step(self, steer_command, curvature_gain, dt=PHYSICS_DT):
        self.gears.settle()

        wanted = max(-MAX_CURVATURE,
                     min(MAX_CURVATURE, steer_command * curvature_gain))
        limit = MAX_CURVATURE_RATE * dt
        self.curvature += max(-limit, min(limit, wanted - self.curvature))

        accel = -DRAG_ACCEL if self.speed > 0.01 else 0.0
        if self.gears.gear != GEAR_NEUTRAL:
            accel += CREEP_ACCEL * max(0.0, (CREEP_MPS - self.speed) / CREEP_MPS)
        if self.tapper.down('T'):
            accel += THROTTLE_ACCEL
        if self.tapper.down('B'):
            accel -= BRAKE_ACCEL
        self.speed = max(0.0, self.speed + accel * dt)

        travelled = self.speed * dt * self.direction
        self.yaw_rate = self.curvature * travelled / dt
        self.pose = advance(self.pose, self.curvature, travelled)
        self.path.append(self.pose)


class Run:
    """What one simulated manoeuvre did."""

    def __init__(self):
        self.samples = []        # (t, speed, demand, throttle, brake)
        self.finished = False
        self.off_track = False
        self.seconds = 0.0


def drive(trajectory, seconds=90.0, curvature_gain=MAX_CURVATURE):
    """Run a whole manoeuvre through every layer. Returns ``(car, Run)``."""
    clock = FakeClock()
    tapper = TimedTapper(clock)
    gears = DelayedGears(clock)
    steering = RecordingSteering()
    controller = VehicleController(steering, PulsedPedals(tapper), gears,
                                   clock=clock)
    follower = PathFollower(trajectory, clock=clock)
    car = SimulatedCar(trajectory.start, clock, tapper, gears)
    result = Run()

    next_cycle = 0.0
    while clock.now < seconds:
        if clock.now >= next_cycle - 1e-9:
            next_cycle += CYCLE_S
            demand = follower.update(car.pose, car.speed)
            if demand.off_track:
                result.off_track = True
                break
            if demand.finished:
                result.finished = True
                break
            status = controller.apply(
                demand,
                VehicleState(speed_mps=car.speed, yaw_rate=car.yaw_rate,
                             gear=gears.gear, reversing=demand.direction < 0))
            result.samples.append((clock.now, car.speed, demand.speed,
                                   status.throttle, status.brake))
        car.step(steering.value, curvature_gain)
        clock.now += PHYSICS_DT
    result.seconds = clock.now
    return car, result


def plan(gap=8.0, ego_x=14.0):
    ego, slot, obstacles = parallel_scene(gap=gap, ego_x=ego_x)
    outcome = ParkingPlanner(
        SHAPE, min_turn_radius=planning_radius_for(LOCK_RADIUS_M)).plan(
        ego, slot, [o.box for o in obstacles])
    assert outcome.ok, outcome.reason
    return outcome.trajectory, slot, obstacles


def cruising(run):
    """The samples where the demand is the crawl and has settled there.

    Not the whole manoeuvre: the demand also contains an acceleration ramp and
    a stopping curve, and lagging *those* by a few tenths is a car obeying
    physics, not a controller misbehaving. What a sawtooth shows up in is the
    steady part, which is where the live runs went to 2.3 m/s.
    """
    return [s for s in run.samples if s[2] >= CRUISE_DEMAND - 0.05]


CRUISE_DEMAND = 1.1     # ``path_follower.CRUISE_MPS``


class TestTheManoeuvreCompletes:
    @pytest.mark.parametrize('gap,ego_x', [(7.3, 13.6), (8.0, 14.0),
                                           (12.0, 18.0)])
    def test_the_car_parks_square_and_touches_nothing(self, gap, ego_x):
        trajectory, slot, obstacles = plan(gap, ego_x)
        car, run = drive(trajectory)
        assert run.finished, f"gave up after {run.seconds:.0f} s"
        assert not run.off_track
        assert math.hypot(car.pose.x - slot.target.x,
                          car.pose.y - slot.target.y) < 0.5
        assert abs(normalise_angle(car.pose.yaw - slot.target.yaw)) \
            < math.radians(6.0)
        assert swept_collision(SHAPE, car.path,
                               [o.box for o in obstacles]) is None


class TestLongitudinal:
    """The defect the driver saw: full throttle, full brake, and a lurch."""

    def test_the_speed_never_runs_away(self):
        """The single clearest symptom: 2.3 m/s against a 1.1 m/s demand.

        Measured here at 1.18 m/s, i.e. 7 % over the crawl and reached only
        on the ramp down from the approach.
        """
        trajectory, _, _ = plan()
        _, run = drive(trajectory)
        fastest = max(speed for _, speed, _, _, _ in run.samples)
        assert fastest < CRUISE_DEMAND * 1.3, (
            f"reached {fastest:.2f} m/s on a {CRUISE_DEMAND} m/s manoeuvre")

    def test_the_speed_holds_its_demand_while_cruising(self):
        trajectory, _, _ = plan()
        _, run = drive(trajectory)
        samples = cruising(run)
        assert len(samples) > 20, "the manoeuvre never settled at the crawl"
        worst = max(abs(speed - wanted) for _, speed, wanted, _, _ in samples)
        # Measured at 0.24 m/s, most of which is the first second of a stroke
        # while the integral finds the creep offset for that gear.
        assert worst < 0.4, f"speed wandered {worst:.2f} m/s from its demand"

    def test_the_throttle_is_not_simply_held_open(self):
        """``thr 1.00`` on nearly every line is what this catches."""
        trajectory, _, _ = plan()
        _, run = drive(trajectory)
        full = [1 for _, _, _, throttle, _ in run.samples if throttle >= 0.9]
        assert len(full) < 0.25 * len(run.samples), (
            f"{len(full)} of {len(run.samples)} cycles asked for full throttle")

    def test_the_car_is_never_asked_for_both_pedals_at_once(self):
        trajectory, _, _ = plan()
        _, run = drive(trajectory)
        assert not [s for s in run.samples if s[3] > 0.0 and s[4] > 0.0]

    def test_it_does_not_alternate_throttle_and_brake(self):
        """The sawtooth, measured as how often the pedal changes side.

        Every direction change legitimately swaps pedals, and there are five
        strokes, so a handful of swaps is the manoeuvre working. Dozens is the
        bang-bang loop back.
        """
        trajectory, _, _ = plan()
        _, run = drive(trajectory)
        swaps = 0
        previous = 0
        for _, _, _, throttle, brake in run.samples:
            side = 1 if throttle > 0.0 else (-1 if brake > 0.0 else previous)
            if previous and side != previous:
                swaps += 1
            previous = side
        assert swaps <= 12, f"the pedals swapped sides {swaps} times"


class TestASlowerCarStillParks:
    def test_a_car_that_steers_less_than_planned_still_finishes(self):
        """The steering gain is *learned*, and starts deliberately wrong.

        Here the car turns only 80 % of what the planner assumed, which is the
        error the live runs actually had: planned at 6 m, measured 6.7.
        """
        trajectory, slot, obstacles = plan()
        car, run = drive(trajectory, curvature_gain=MAX_CURVATURE * 0.8)
        assert run.finished
        assert swept_collision(SHAPE, car.path,
                               [o.box for o in obstacles]) is None
