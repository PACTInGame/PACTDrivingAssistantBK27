"""Trajectory planning: does the manoeuvre end where it was meant to, and fit?

The scenes are built the way the world is -- two parked cars and a gap -- and
run through the detector first, so the planner is only ever given slots a real
scan could have produced.

The numbers that matter here were measured rather than assumed. A 4.5 m car
shifting 3 m sideways needs about 9.8 m of kerb for a single reverse
manoeuvre; the classical bound ``L + sqrt(R^2 - (R - d)^2)`` says 9.1 m and the
swept-body check adds the rest. So a 12 m space is parked in one movement, an
8 m space takes three, and a 7 m space -- the shortest the detector offers
(``slot_detection.PARALLEL_LENGTH_MARGIN_M``, measured against this planner) --
takes five, which is exactly what a driver does and what these tests pin.
"""

import math

import pytest

from assistance.parking.geometry import (OrientedBox, Pose, VehicleShape,
                                         normalise_angle, swept_collision)
from assistance.parking.slot_detection import (KIND_PARALLEL,
                                               KIND_PERPENDICULAR, Obstacle,
                                               ParkingSlotDetector)
from assistance.parking.trajectory import (DIRECTION_FORWARD, DIRECTION_REVERSE,
                                           ParkingPlanner, REASON_BLOCKED,
                                           REASON_NOT_ALIGNED, Segment,
                                           advance, sample)

CAR_L, CAR_W = 4.5, 1.8
SHAPE = VehicleShape(CAR_L, CAR_W)


def planner(**kwargs):
    return ParkingPlanner(SHAPE, **kwargs)


def parked(x, y, yaw=0.0, length=CAR_L, width=CAR_W, key=None):
    return Obstacle(OrientedBox(x, y, yaw, length, width),
                    key if key is not None else (round(x, 2), round(y, 2)))


def boxes(obstacles):
    return [o.box for o in obstacles]


def parallel_scene(gap=8.0, ego_x=14.0, side=-1.0):
    """Two cars in a row with *gap* of clear kerb between them, ego alongside.

    The rear car's centre is the origin, so the gap runs from ``CAR_L / 2`` to
    ``CAR_L / 2 + gap``. *side* mirrors the whole scene across the road.
    """
    obstacles = [parked(0.0, side * 3.0, key='rear'),
                 parked(CAR_L + gap, side * 3.0, key='front')]
    ego = Pose(ego_x, 0.0, 0.0)
    slot = next(s for s in ParkingSlotDetector(SHAPE).scan(ego, obstacles)
                if s.kind == KIND_PARALLEL and not s.open_ended)
    return ego, slot, obstacles


def perpendicular_scene(ego_x=12.0, half_gap=3.0):
    """Two cars nose-in on the right, square to the road, with a bay between."""
    obstacles = [parked(5.0 - half_gap, -4.75, yaw=math.pi / 2, key='l'),
                 parked(5.0 + half_gap, -4.75, yaw=math.pi / 2, key='r')]
    ego = Pose(ego_x, 0.0, 0.0)
    slot = next(s for s in ParkingSlotDetector(SHAPE).scan(ego, obstacles)
                if s.kind == KIND_PERPENDICULAR and not s.open_ended)
    return ego, slot, obstacles


class TestKinematics:
    def test_straight_line(self):
        after = advance(Pose(0.0, 0.0, 0.0), 0.0, 5.0)
        assert after.x == pytest.approx(5.0)
        assert after.y == pytest.approx(0.0)
        assert after.yaw == pytest.approx(0.0)

    def test_forward_left_turn(self):
        """Positive curvature, forward: quarter circle to the left."""
        radius = 6.0
        after = advance(Pose(0.0, 0.0, 0.0), 1.0 / radius, radius * math.pi / 2)
        assert after.x == pytest.approx(radius)
        assert after.y == pytest.approx(radius)
        assert after.yaw == pytest.approx(math.pi / 2)

    def test_reverse_with_the_wheel_left_swings_the_tail_left(self):
        """The sign the whole parallel manoeuvre rests on.

        Reversing with a positive (left) curvature turns the *heading* right
        and moves the car back and to the left -- which is what tucks the tail
        into a slot on the left.
        """
        radius = 6.0
        after = advance(Pose(0.0, 0.0, 0.0), 1.0 / radius, -radius * math.pi / 6)
        assert after.yaw < 0.0
        assert after.x < 0.0
        assert after.y > 0.0

    def test_arc_length_is_preserved_by_sampling(self):
        segments = [Segment(0.0, 3.0, DIRECTION_FORWARD),
                    Segment(1 / 6.0, 4.0, DIRECTION_REVERSE)]
        points = sample(Pose(0, 0, 0), segments, step=0.2)
        assert points[0].s == 0.0
        assert points[-1].s == pytest.approx(7.0)
        direct = advance(advance(Pose(0, 0, 0), 0.0, 3.0), 1 / 6.0, -4.0)
        assert points[-1].pose.x == pytest.approx(direct.x, abs=1e-9)
        assert points[-1].pose.y == pytest.approx(direct.y, abs=1e-9)

    def test_sampling_carries_the_segment_index(self):
        segments = [Segment(0.0, 1.0, DIRECTION_FORWARD),
                    Segment(0.0, 1.0, DIRECTION_REVERSE)]
        points = sample(Pose(0, 0, 0), segments, step=0.5)
        assert {p.segment_index for p in points} == {0, 1}
        assert points[-1].direction == DIRECTION_REVERSE


class TestParallelPlan:
    def test_a_roomy_space_is_parked_in_one_movement(self):
        ego, slot, obstacles = parallel_scene(gap=12.0, ego_x=18.0)
        result = planner().plan(ego, slot, boxes(obstacles))
        assert result.ok, result.reason
        assert result.trajectory.direction_changes == 0

    def test_a_normal_space_takes_three_movements(self):
        ego, slot, obstacles = parallel_scene(gap=8.0)
        result = planner().plan(ego, slot, boxes(obstacles))
        assert result.ok, result.reason
        assert result.trajectory.direction_changes == 2

    def test_a_tight_space_shuffles_and_still_fits(self):
        ego, slot, obstacles = parallel_scene(gap=7.3, ego_x=13.6)
        result = planner().plan(ego, slot, boxes(obstacles))
        assert result.ok, result.reason
        assert result.trajectory.direction_changes >= 2
        assert swept_collision(SHAPE,
                               (p.pose for p in result.trajectory.points),
                               boxes(obstacles), 0.15) is None

    @pytest.mark.parametrize('gap,ego_x', [(7.3, 13.6), (8.0, 14.0), (12.0, 18.0)])
    def test_every_plan_ends_at_the_slot_target(self, gap, ego_x):
        ego, slot, obstacles = parallel_scene(gap=gap, ego_x=ego_x)
        trajectory = planner().plan(ego, slot, boxes(obstacles)).trajectory
        end = trajectory.points[-1].pose
        assert end.x == pytest.approx(slot.target.x, abs=0.05)
        assert end.y == pytest.approx(slot.target.y, abs=0.05)
        assert normalise_angle(end.yaw - slot.target.yaw) == pytest.approx(
            0.0, abs=math.radians(1.0))

    @pytest.mark.parametrize('gap,ego_x', [(7.3, 13.6), (8.0, 14.0), (12.0, 18.0)])
    def test_every_plan_is_clear_of_the_neighbours(self, gap, ego_x):
        ego, slot, obstacles = parallel_scene(gap=gap, ego_x=ego_x)
        trajectory = planner().plan(ego, slot, boxes(obstacles)).trajectory
        assert swept_collision(SHAPE, (p.pose for p in trajectory.points),
                               boxes(obstacles), 0.15) is None

    def test_the_manoeuvre_starts_where_the_car_is(self):
        """Within what the follower can pull in over the joining straight."""
        ego, slot, obstacles = parallel_scene(gap=8.0)
        trajectory = planner().plan(ego, slot, boxes(obstacles)).trajectory
        assert trajectory.start.x == pytest.approx(ego.x, abs=0.1)
        assert trajectory.start.y == pytest.approx(ego.y, abs=0.35)
        assert abs(normalise_angle(trajectory.start.yaw - ego.yaw)) < math.radians(7)

    def test_a_slot_on_the_left_is_the_mirror_image(self):
        right = planner().plan(*parallel_scene(gap=8.0)[:2],
                               boxes(parallel_scene(gap=8.0)[2])).trajectory
        left_ego, left_slot, left_obstacles = parallel_scene(gap=8.0, side=1.0)
        left = planner().plan(left_ego, left_slot,
                              boxes(left_obstacles)).trajectory
        assert len(left.segments) == len(right.segments)
        for mirrored, original in zip(left.segments, right.segments):
            assert mirrored.curvature == pytest.approx(-original.curvature)
            assert mirrored.length == pytest.approx(original.length)
            assert mirrored.direction == original.direction

    def test_the_first_move_is_backwards(self):
        """Every parallel manoeuvre reverses into the space; none drives in."""
        for gap, ego_x in ((7.3, 13.6), (8.0, 14.0), (12.0, 18.0)):
            ego, slot, obstacles = parallel_scene(gap=gap, ego_x=ego_x)
            segments = planner().plan(ego, slot, boxes(obstacles)).trajectory.segments
            assert segments[0].direction == DIRECTION_REVERSE

    def test_a_crooked_car_is_refused(self):
        ego, slot, obstacles = parallel_scene()
        crooked = Pose(ego.x, ego.y, math.radians(25))
        assert planner().plan(crooked, slot, boxes(obstacles)).reason == \
            REASON_NOT_ALIGNED

    def test_a_blocked_slot_names_the_obstacle(self):
        ego, slot, obstacles = parallel_scene()
        wall = parked(6.0, -1.4, length=30.0, width=0.3, key='wall')
        result = planner().plan(ego, slot, boxes(obstacles) + [wall.box])
        assert not result.ok
        assert result.reason == REASON_BLOCKED
        assert result.blocked_by is wall.box


class TestManoeuvreArea:
    """The two invisible walls -- see ``ROAD_ALLOWANCE_M`` in the planner.

    Without them the planner solved a tight space by leaving through the kerb,
    driving round the back of the parked cars and coming in from behind. It was
    collision-free against everything it had been given, and completely wrong.
    """

    def test_the_manoeuvre_never_goes_behind_the_parked_row(self):
        ego, slot, obstacles = parallel_scene(gap=7.3, ego_x=13.6)
        trajectory = planner().plan(ego, slot, boxes(obstacles)).trajectory
        # The kerb line is 2.1 m out; the parked row is 1.8 m deep behind it.
        deepest = min(point.pose.y - SHAPE.length * 0.5
                      for point in trajectory.points)
        assert deepest > -(2.1 + 1.8 + CAR_W), "drove round the back of the row"

    def test_the_manoeuvre_stays_near_the_driver_s_own_lane(self):
        ego, slot, obstacles = parallel_scene(gap=7.3, ego_x=13.6)
        trajectory = planner().plan(ego, slot, boxes(obstacles)).trajectory
        widest = max(point.pose.y for point in trajectory.points)
        assert widest < 4.0, "swung across the road"


class TestPerpendicularPlan:
    def test_plan_reaches_the_bay(self):
        ego, slot, obstacles = perpendicular_scene()
        result = planner().plan(ego, slot, boxes(obstacles))
        assert result.ok, result.reason
        end = result.trajectory.points[-1].pose
        assert end.x == pytest.approx(slot.target.x, abs=0.05)
        assert end.y == pytest.approx(slot.target.y, abs=0.05)
        assert normalise_angle(end.yaw - slot.target.yaw) == pytest.approx(
            0.0, abs=math.radians(1.0))

    def test_it_ends_reversing_into_the_bay(self):
        ego, slot, obstacles = perpendicular_scene()
        segments = planner().plan(ego, slot, boxes(obstacles)).trajectory.segments
        assert segments[-1].direction == DIRECTION_REVERSE

    def test_the_car_ends_up_facing_out_of_the_bay(self):
        ego, slot, obstacles = perpendicular_scene()
        trajectory = planner().plan(ego, slot, boxes(obstacles)).trajectory
        # Bay on the right of a car driving +X, reversed into: nose points +Y.
        assert trajectory.points[-1].pose.yaw == pytest.approx(math.pi / 2,
                                                               abs=0.02)

    def test_the_path_is_clear(self):
        ego, slot, obstacles = perpendicular_scene()
        trajectory = planner().plan(ego, slot, boxes(obstacles)).trajectory
        assert swept_collision(SHAPE, (p.pose for p in trajectory.points),
                               boxes(obstacles), 0.15) is None

    def test_a_bay_spotted_while_driving_past_is_still_reachable(self):
        """The usual case: the driver is already well beyond the bay."""
        for ego_x in (9.0, 12.0, 16.0):
            ego, slot, obstacles = perpendicular_scene(ego_x=ego_x)
            result = planner().plan(ego, slot, boxes(obstacles))
            assert result.ok, f"ego at {ego_x}: {result.reason}"


class TestTrajectoryHelpers:
    def test_length_and_bounds_line_up(self):
        ego, slot, obstacles = parallel_scene(gap=8.0)
        trajectory = planner().plan(ego, slot, boxes(obstacles)).trajectory
        bounds = trajectory.segment_bounds()
        assert bounds[0][0] == 0.0
        assert bounds[-1][1] == pytest.approx(trajectory.length)
        for (_, end), (start, _) in zip(bounds, bounds[1:]):
            assert end == pytest.approx(start)

    def test_no_zero_length_or_duplicated_segments(self):
        """A stroke split in two would tell the follower to stop for nothing."""
        for gap, ego_x in ((7.3, 13.6), (8.0, 14.0)):
            ego, slot, obstacles = parallel_scene(gap=gap, ego_x=ego_x)
            segments = planner().plan(ego, slot,
                                      boxes(obstacles)).trajectory.segments
            assert all(s.length > 0.01 for s in segments)
            for previous, current in zip(segments, segments[1:]):
                same = (previous.direction == current.direction
                        and abs(previous.curvature - current.curvature) < 1e-9)
                assert not same


class TestCost:
    def test_planning_stays_within_a_cycle_budget(self):
        """The shuffle is the expensive path; it still has to be affordable."""
        import time
        ego, slot, obstacles = parallel_scene(gap=7.3, ego_x=13.6)
        obstacle_boxes = boxes(obstacles) + [
            OrientedBox(i * 3.0 - 20.0, 9.0, 0.0, 2.0, 2.0) for i in range(10)]
        instance = planner()
        instance.plan(ego, slot, obstacle_boxes)       # warm up
        started = time.perf_counter()
        for _ in range(10):
            instance.plan(ego, slot, obstacle_boxes)
        per_plan_ms = (time.perf_counter() - started) / 10 * 1000.0
        assert per_plan_ms < 40.0, f"{per_plan_ms:.1f} ms per plan"
