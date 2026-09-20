"""The follower, driven in closed loop against the same car model it plans for.

These are the tests that actually say the feature works. Everything else checks
a piece; this drives the whole manoeuvre -- plan, follow, steer, change gear,
stop -- through a kinematic bicycle and asks the two questions that matter: did
the car end up parked, and did it touch anything on the way.

The simulator is deliberately *not* a perfect integrator of the planned path.
It has a speed that has to be built up and shed at a finite rate, a steering
rate limit, and a curvature ceiling, so a follower that only works when the car
tracks its plan exactly will fail here. What it does not have is tyre slip: at
1 m/s on a 6 m radius the lateral acceleration is 0.2 m/s^2, so there is
nothing to model.
"""

import math

import pytest

from assistance.parking.geometry import (OrientedBox, Pose, VehicleShape,
                                         normalise_angle, swept_collision)
from assistance.parking.path_follower import (CRUISE_MPS, ControlDemand,
                                              PathFollower)
from assistance.parking.slot_detection import (KIND_PARALLEL,
                                               KIND_PERPENDICULAR, Obstacle,
                                               ParkingSlotDetector)
from assistance.parking.trajectory import (DIRECTION_FORWARD, DIRECTION_REVERSE,
                                           ParkingPlanner, Segment, Trajectory,
                                           advance, planning_radius_for, sample)

CAR_L, CAR_W = 4.5, 1.8
SHAPE = VehicleShape(CAR_L, CAR_W)
# What the *car* can do at full lock. The planner is given a deliberately
# wider radius (``planning_radius_for``), so these tests drive a car that can
# steer tighter than its own plan -- which is the whole point of that margin
# and the only way the follower has anything to correct with.
MIN_RADIUS = 6.0

# What the simulated car can do. Acceleration and steering rate are what a car
# at walking pace really manages; the curvature ceiling is the real steering
# lock, so a follower that demands more than the car has simply does not get
# it, exactly as in the game.
MAX_ACCEL_MPS2 = 1.5
MAX_CURVATURE = 1.0 / MIN_RADIUS
MAX_CURVATURE_RATE = 0.35        # 1/m per second, i.e. lock to lock in ~1 s
DT = 0.1                         # the assistance cycle


class FakeClock:
    """A clock that advances exactly one assistance cycle per step.

    The follower rate-limits its own speed demand, so it needs a clock. A real
    one would make these tests depend on how fast the machine runs the loop.
    """

    def __init__(self):
        self.now = 0.0

    def __call__(self):
        return self.now

    def tick(self, dt=DT):
        self.now += dt


class Car:
    """A kinematic bicycle with rate limits, driven by a ControlDemand."""

    def __init__(self, pose: Pose):
        self.pose = pose
        self.speed = 0.0          # magnitude, m/s
        self.direction = DIRECTION_FORWARD
        self.curvature = 0.0
        self.path = [pose]

    def step(self, demand: ControlDemand, dt: float = DT):
        # A gear change only happens at a standstill, exactly as the real one
        # does -- so a follower that asks for the wrong direction while the car
        # is still rolling gets ignored here too.
        if self.speed <= 1e-3:
            self.direction = demand.direction

        target = demand.speed if self.direction == demand.direction else 0.0
        change = max(-MAX_ACCEL_MPS2 * dt,
                     min(MAX_ACCEL_MPS2 * dt, target - self.speed))
        self.speed = max(0.0, self.speed + change)

        wanted = max(-MAX_CURVATURE, min(MAX_CURVATURE, demand.curvature))
        limit = MAX_CURVATURE_RATE * dt
        self.curvature += max(-limit, min(limit, wanted - self.curvature))

        self.pose = advance(self.pose, self.curvature,
                            self.speed * dt * self.direction)
        self.path.append(self.pose)


def drive(trajectory, start=None, max_seconds=240.0):
    """Run the manoeuvre to completion. Returns ``(car, demand, seconds)``."""
    car = Car(start or trajectory.start)
    clock = FakeClock()
    follower = PathFollower(trajectory, clock=clock)
    demand = follower.update(car.pose, car.speed)
    while clock.now < max_seconds:
        clock.tick()
        demand = follower.update(car.pose, car.speed)
        if demand.finished:
            break
        car.step(demand)
    return car, demand, clock.now


def parked(x, y, yaw=0.0, length=CAR_L, width=CAR_W, key=None):
    return Obstacle(OrientedBox(x, y, yaw, length, width),
                    key if key is not None else (round(x, 2), round(y, 2)))


def parallel_scene(gap=8.0, ego_x=14.0, side=-1.0):
    obstacles = [parked(0.0, side * 3.0, key='rear'),
                 parked(CAR_L + gap, side * 3.0, key='front')]
    ego = Pose(ego_x, 0.0, 0.0)
    slot = next(s for s in ParkingSlotDetector(SHAPE).scan(ego, obstacles)
                if s.kind == KIND_PARALLEL and not s.open_ended)
    return ego, slot, obstacles


def perpendicular_scene(ego_x=12.0, half_gap=3.0):
    obstacles = [parked(5.0 - half_gap, -4.75, yaw=math.pi / 2, key='l'),
                 parked(5.0 + half_gap, -4.75, yaw=math.pi / 2, key='r')]
    ego = Pose(ego_x, 0.0, 0.0)
    slot = next(s for s in ParkingSlotDetector(SHAPE).scan(ego, obstacles)
                if s.kind == KIND_PERPENDICULAR and not s.open_ended)
    return ego, slot, obstacles


def plan_for(ego, slot, obstacles):
    result = ParkingPlanner(
        SHAPE, min_turn_radius=planning_radius_for(MIN_RADIUS)).plan(
        ego, slot, [o.box for o in obstacles])
    assert result.ok, result.reason
    return result.trajectory


class TestControlDemand:
    def test_progress_is_bounded(self):
        assert ControlDemand(0, 1, 0, travelled=5, total=10).progress == 0.5
        assert ControlDemand(0, 1, 0, travelled=20, total=10).progress == 1.0
        assert ControlDemand(0, 1, 0, finished=True).progress == 1.0
        assert ControlDemand(0, 1, 0).progress == 0.0


class TestSteeringLaw:
    def test_a_car_on_a_curved_path_asks_for_exactly_that_curve(self):
        """The defect that made the manoeuvre crooked, pinned.

        Pure pursuit aimed at a point on the path reproduces the path's own
        arc, so adding it to the feedforward asked for half as much steering
        again as the plan -- 0.233 1/m on a 6 m arc, measured -- before the
        car was off the path by a millimetre. The demand was then pinned at
        its cap for whole strokes at a time, which is what a live run saw.
        """
        trajectory = Trajectory('test', [Segment(1 / 6.0, 10.0, DIRECTION_FORWARD)])
        trajectory.points = sample(Pose(0, 0, 0), trajectory.segments, 0.2)
        trajectory.start = Pose(0, 0, 0)
        trajectory.radius = 6.0
        follower = PathFollower(trajectory, clock=FakeClock())
        on_path = trajectory.points[5]
        demand = follower.update(on_path.pose, 1.1)
        assert demand.curvature == pytest.approx(on_path.curvature, abs=1e-6)

    def test_straight_path_asks_for_no_steering(self):
        trajectory = Trajectory('test', [Segment(0.0, 10.0, DIRECTION_FORWARD)])
        trajectory.points = sample(Pose(0, 0, 0), trajectory.segments, 0.2)
        trajectory.start = Pose(0, 0, 0)
        follower = PathFollower(trajectory)
        demand = follower.update(Pose(0, 0, 0), 1.0)
        assert demand.curvature == pytest.approx(0.0, abs=1e-9)

    def test_offset_to_the_right_steers_left_going_forward(self):
        trajectory = Trajectory('test', [Segment(0.0, 10.0, DIRECTION_FORWARD)])
        trajectory.points = sample(Pose(0, 0, 0), trajectory.segments, 0.2)
        trajectory.start = Pose(0, 0, 0)
        follower = PathFollower(trajectory)
        demand = follower.update(Pose(0.0, -0.4, 0.0), 1.0)
        assert demand.curvature > 0.0

    def test_reverse_steers_the_other_way_round(self):
        """The sign that decides whether reversing converges or diverges.

        A car 40 cm to the right of its path has to come back to it either
        way, but turning the wheel left sends it left going forward and right
        going backwards. Both cases here ask for a positive curvature and mean
        opposite things by it; getting the flip wrong steers out of the space.
        """
        forward = Trajectory('test', [Segment(0.0, 10.0, DIRECTION_FORWARD)])
        forward.points = sample(Pose(0, 0, 0), forward.segments, 0.2)
        forward.start = Pose(0, 0, 0)
        reverse = Trajectory('test', [Segment(0.0, 10.0, DIRECTION_REVERSE)])
        reverse.points = sample(Pose(0, 0, 0), reverse.segments, 0.2)
        reverse.start = Pose(0, 0, 0)
        off_path = Pose(0.0, -0.4, 0.0)
        assert PathFollower(forward).update(off_path, 1.0).curvature > 0.0
        assert PathFollower(reverse).update(off_path, 1.0).curvature > 0.0

    def test_a_reversing_car_converges_on_its_path(self):
        trajectory = Trajectory('test', [Segment(0.0, 12.0, DIRECTION_REVERSE)])
        trajectory.points = sample(Pose(0, 0, 0), trajectory.segments, 0.2)
        trajectory.start = Pose(0, 0, 0)
        car, _, _ = drive(trajectory, start=Pose(0.0, -0.4, 0.0))
        assert abs(car.pose.y) < 0.1


class TestSpeedProfile:
    def test_it_slows_to_a_stop_at_the_end(self):
        trajectory = Trajectory('test', [Segment(0.0, 10.0, DIRECTION_FORWARD)])
        trajectory.points = sample(Pose(0, 0, 0), trajectory.segments, 0.2)
        trajectory.start = Pose(0, 0, 0)
        clock = FakeClock()
        follower = PathFollower(trajectory, clock=clock)
        # Walked rather than jumped: the follower's progress marker only ever
        # moves forward, and only within a window, so it cannot be teleported
        # down the path (see ``PathFollower._advance_index``).
        speeds = {}
        for tenth in range(0, 99):
            x = tenth * 0.1
            clock.tick()
            speeds[round(x, 1)] = follower.update(Pose(x, 0, 0), 1.0).speed
        assert speeds[9.7] < speeds[1.0]
        # sqrt(2 * 0.8 * 0.3) = 0.69 m/s with 30 cm left to run.
        assert speeds[9.7] < 0.8

    def test_a_long_straight_is_driven_faster_than_the_crawl(self):
        trajectory = Trajectory('test', [Segment(0.0, 20.0, DIRECTION_FORWARD)])
        trajectory.points = sample(Pose(0, 0, 0), trajectory.segments, 0.2)
        trajectory.start = Pose(0, 0, 0)
        clock = FakeClock()
        follower = PathFollower(trajectory, clock=clock)
        # The demand is acceleration-limited, so it takes a few cycles to get
        # there -- which is the point of the limit.
        speed = 0.0
        for _ in range(60):
            clock.tick()
            speed = follower.update(Pose(1.0, 0, 0), speed).speed
        assert speed > CRUISE_MPS

    def test_an_arc_is_never_driven_faster_than_the_crawl(self):
        trajectory = Trajectory('test', [Segment(1 / 6.0, 20.0, DIRECTION_FORWARD)])
        trajectory.points = sample(Pose(0, 0, 0), trajectory.segments, 0.2)
        trajectory.start = Pose(0, 0, 0)
        follower = PathFollower(trajectory)
        assert follower.update(trajectory.points[3].pose, 1.0).speed <= CRUISE_MPS


class TestGearChanges:
    @staticmethod
    def _two_stroke():
        trajectory = Trajectory('test', [Segment(0.0, 4.0, DIRECTION_FORWARD),
                                         Segment(0.0, 4.0, DIRECTION_REVERSE)])
        trajectory.points = sample(Pose(0, 0, 0), trajectory.segments, 0.2)
        trajectory.start = Pose(0, 0, 0)
        return trajectory

    def test_the_demand_goes_to_zero_before_the_direction_flips(self):
        """LFS will not take reverse while the car rolls, so nor may we."""
        from assistance.parking.path_follower import STANDSTILL_MPS
        trajectory = self._two_stroke()
        clock = FakeClock()
        follower = PathFollower(trajectory, clock=clock)
        car = Car(trajectory.start)
        previous = None
        flipped = False
        for _ in range(600):
            clock.tick()
            demand = follower.update(car.pose, car.speed)
            if demand.finished:
                break
            if previous is not None and demand.direction != previous:
                assert car.speed <= STANDSTILL_MPS, (
                    f"flipped direction at {car.speed:.3f} m/s")
                flipped = True
            if demand.changing_direction:
                assert demand.speed == 0.0
            previous = demand.direction
            car.step(demand)
        assert flipped, "the two strokes never changed direction"

    def test_both_strokes_are_driven(self):
        trajectory = self._two_stroke()
        car, demand, _ = drive(trajectory)
        assert demand.finished
        assert car.pose.x == pytest.approx(0.0, abs=0.3)


class TestParallelManoeuvre:
    # How square the car has to end up, in degrees. One figure for every
    # space, which it could not be before: while the follower fed the path's
    # arc forward *and* added pure pursuit on top, the tightest space the
    # detector offers ended 10.1 degrees crooked and needed its own allowance.
    # With the correction that is actually zero on the path, the same
    # manoeuvre measures 2.1 degrees and the roomier ones under 1. Four is the
    # guard against a regression, not the achieved figure.
    @pytest.mark.parametrize('gap,ego_x,squareness_deg',
                             [(7.3, 13.6, 4.0), (8.0, 14.0, 4.0),
                              (12.0, 18.0, 4.0)])
    def test_the_car_ends_up_parked(self, gap, ego_x, squareness_deg):
        ego, slot, obstacles = parallel_scene(gap=gap, ego_x=ego_x)
        trajectory = plan_for(ego, slot, obstacles)
        car, demand, seconds = drive(trajectory)
        assert demand.finished, f"gave up after {seconds:.0f} s"
        assert math.hypot(car.pose.x - slot.target.x,
                          car.pose.y - slot.target.y) < 0.5
        assert abs(normalise_angle(car.pose.yaw - slot.target.yaw)) < math.radians(
            squareness_deg)

    @pytest.mark.parametrize('gap,ego_x', [(7.3, 13.6), (8.0, 14.0), (12.0, 18.0)])
    def test_nothing_is_touched_on_the_way(self, gap, ego_x):
        ego, slot, obstacles = parallel_scene(gap=gap, ego_x=ego_x)
        trajectory = plan_for(ego, slot, obstacles)
        car, _, _ = drive(trajectory)
        poses = car.path
        assert swept_collision(SHAPE, poses, [o.box for o in obstacles]) is None

    def test_it_never_reports_being_off_track(self):
        ego, slot, obstacles = parallel_scene(gap=8.0)
        trajectory = plan_for(ego, slot, obstacles)
        clock = FakeClock()
        follower = PathFollower(trajectory, clock=clock)
        car = Car(trajectory.start)
        worst = 0.0
        for _ in range(2400):
            clock.tick()
            demand = follower.update(car.pose, car.speed)
            if demand.finished:
                break
            worst = max(worst, abs(demand.cross_track))
            assert not demand.off_track, f"off track by {demand.cross_track:.2f} m"
            car.step(demand)
        # Measured at 0.02 m. The old pure-pursuit follower managed 0.45 m on
        # the same space, so this is the number that moved most.
        assert worst < 0.12, f"tracked the path to only {worst:.2f} m"

    def test_a_car_that_starts_slightly_off_still_parks(self):
        """The plan is built as if the car were square with the road."""
        ego, slot, obstacles = parallel_scene(gap=8.0)
        trajectory = plan_for(ego, slot, obstacles)
        offset = Pose(trajectory.start.x, trajectory.start.y - 0.3,
                      trajectory.start.yaw + math.radians(4))
        car, demand, _ = drive(trajectory, start=offset)
        assert demand.finished
        assert math.hypot(car.pose.x - slot.target.x,
                          car.pose.y - slot.target.y) < 0.6
        assert swept_collision(SHAPE, car.path,
                               [o.box for o in obstacles]) is None

    def test_a_slot_on_the_left_parks_too(self):
        ego, slot, obstacles = parallel_scene(gap=8.0, side=1.0)
        trajectory = plan_for(ego, slot, obstacles)
        car, demand, _ = drive(trajectory)
        assert demand.finished
        assert math.hypot(car.pose.x - slot.target.x,
                          car.pose.y - slot.target.y) < 0.5


class TestPerpendicularManoeuvre:
    @pytest.mark.parametrize('ego_x', [9.0, 12.0, 16.0])
    def test_the_car_ends_up_in_the_bay(self, ego_x):
        ego, slot, obstacles = perpendicular_scene(ego_x=ego_x)
        trajectory = plan_for(ego, slot, obstacles)
        car, demand, seconds = drive(trajectory)
        assert demand.finished, f"gave up after {seconds:.0f} s"
        assert math.hypot(car.pose.x - slot.target.x,
                          car.pose.y - slot.target.y) < 0.5
        assert abs(normalise_angle(car.pose.yaw - slot.target.yaw)) < math.radians(8)

    def test_nothing_is_touched(self):
        ego, slot, obstacles = perpendicular_scene()
        trajectory = plan_for(ego, slot, obstacles)
        car, _, _ = drive(trajectory)
        assert swept_collision(SHAPE, car.path,
                               [o.box for o in obstacles]) is None


class TestCost:
    def test_one_update_is_negligible(self):
        import time
        ego, slot, obstacles = parallel_scene(gap=7.3, ego_x=13.6)
        trajectory = plan_for(ego, slot, obstacles)
        follower = PathFollower(trajectory)
        pose = trajectory.start
        follower.update(pose, 1.0)
        started = time.perf_counter()
        for _ in range(1000):
            follower.update(pose, 1.0)
        per_update_us = (time.perf_counter() - started) / 1000 * 1e6
        assert per_update_us < 300.0, f"{per_update_us:.0f} us per update"
