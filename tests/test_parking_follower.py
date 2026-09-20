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
                                           advance, sample)

CAR_L, CAR_W = 4.5, 1.8
SHAPE = VehicleShape(CAR_L, CAR_W)
MIN_RADIUS = 6.0

# What the simulated car can do. Acceleration and steering rate are what a car
# at walking pace really manages; the curvature ceiling is the steering lock
# the planner assumed, so the follower is never handed a car that can do more
# than it planned for.
MAX_ACCEL_MPS2 = 1.5
MAX_CURVATURE = 1.0 / MIN_RADIUS
MAX_CURVATURE_RATE = 0.35        # 1/m per second, i.e. lock to lock in ~1 s
DT = 0.1                         # the assistance cycle


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
    follower = PathFollower(trajectory)
    elapsed = 0.0
    demand = follower.update(car.pose, car.speed)
    while elapsed < max_seconds:
        demand = follower.update(car.pose, car.speed)
        if demand.finished:
            break
        car.step(demand)
        elapsed += DT
    return car, demand, elapsed


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
    result = ParkingPlanner(SHAPE, min_turn_radius=MIN_RADIUS).plan(
        ego, slot, [o.box for o in obstacles])
    assert result.ok, result.reason
    return result.trajectory


class TestControlDemand:
    def test_progress_is_bounded(self):
        assert ControlDemand(0, 1, 0, travelled=5, total=10).progress == 0.5
        assert ControlDemand(0, 1, 0, travelled=20, total=10).progress == 1.0
        assert ControlDemand(0, 1, 0, finished=True).progress == 1.0
        assert ControlDemand(0, 1, 0).progress == 0.0


class TestPurePursuit:
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
        follower = PathFollower(trajectory)
        # Walked rather than jumped: the follower's progress marker only ever
        # moves forward, and only within a window, so it cannot be teleported
        # down the path (see ``PathFollower._advance_index``).
        speeds = {}
        for tenth in range(0, 99):
            x = tenth * 0.1
            speeds[round(x, 1)] = follower.update(Pose(x, 0, 0), 1.0).speed
        assert speeds[9.7] < speeds[1.0]
        # sqrt(2 * 0.8 * 0.3) = 0.69 m/s with 30 cm left to run.
        assert speeds[9.7] < 0.8

    def test_a_long_straight_is_driven_faster_than_the_crawl(self):
        trajectory = Trajectory('test', [Segment(0.0, 20.0, DIRECTION_FORWARD)])
        trajectory.points = sample(Pose(0, 0, 0), trajectory.segments, 0.2)
        trajectory.start = Pose(0, 0, 0)
        follower = PathFollower(trajectory)
        assert follower.update(Pose(1.0, 0, 0), 1.0).speed > CRUISE_MPS

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
        follower = PathFollower(trajectory)
        car = Car(trajectory.start)
        previous = None
        flipped = False
        for _ in range(600):
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
    # How square the car has to end up, per space. The tightest space the
    # detector offers gets a looser figure and it is a measurement, not a
    # concession: its manoeuvre is five strokes, the last two under 1.5 m, and
    # a car with a real steering rate limit cannot put full lock on inside
    # that distance. Measured at 10.1 degrees; 12 is the guard against it
    # getting worse. A roomier space ends within 8.
    @pytest.mark.parametrize('gap,ego_x,squareness_deg',
                             [(7.0, 13.5, 12.0), (8.0, 14.0, 8.0),
                              (12.0, 18.0, 8.0)])
    def test_the_car_ends_up_parked(self, gap, ego_x, squareness_deg):
        ego, slot, obstacles = parallel_scene(gap=gap, ego_x=ego_x)
        trajectory = plan_for(ego, slot, obstacles)
        car, demand, seconds = drive(trajectory)
        assert demand.finished, f"gave up after {seconds:.0f} s"
        assert math.hypot(car.pose.x - slot.target.x,
                          car.pose.y - slot.target.y) < 0.5
        assert abs(normalise_angle(car.pose.yaw - slot.target.yaw)) < math.radians(
            squareness_deg)

    @pytest.mark.parametrize('gap,ego_x', [(7.0, 13.5), (8.0, 14.0), (12.0, 18.0)])
    def test_nothing_is_touched_on_the_way(self, gap, ego_x):
        ego, slot, obstacles = parallel_scene(gap=gap, ego_x=ego_x)
        trajectory = plan_for(ego, slot, obstacles)
        car, _, _ = drive(trajectory)
        poses = car.path
        assert swept_collision(SHAPE, poses, [o.box for o in obstacles]) is None

    def test_it_never_reports_being_off_track(self):
        ego, slot, obstacles = parallel_scene(gap=8.0)
        trajectory = plan_for(ego, slot, obstacles)
        follower = PathFollower(trajectory)
        car = Car(trajectory.start)
        worst = 0.0
        for _ in range(2400):
            demand = follower.update(car.pose, car.speed)
            if demand.finished:
                break
            worst = max(worst, abs(demand.cross_track))
            assert not demand.off_track, f"off track by {demand.cross_track:.2f} m"
            car.step(demand)
        assert worst < 0.5, f"tracked the path to only {worst:.2f} m"

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
        ego, slot, obstacles = parallel_scene(gap=7.0, ego_x=13.5)
        trajectory = plan_for(ego, slot, obstacles)
        follower = PathFollower(trajectory)
        pose = trajectory.start
        follower.update(pose, 1.0)
        started = time.perf_counter()
        for _ in range(1000):
            follower.update(pose, 1.0)
        per_update_us = (time.perf_counter() - started) / 1000 * 1e6
        assert per_update_us < 300.0, f"{per_update_us:.0f} us per update"
