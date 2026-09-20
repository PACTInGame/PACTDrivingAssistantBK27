"""Slot detection: what counts as a parking space, and what must not.

Every scene is built in the ego's own frame -- the car sits at the origin
pointing +X, so "ahead" is +x and "left" is +y. That keeps the arithmetic in
the test readable and still exercises the real transform, because the detector
does not know it is being handed an axis-aligned world.
"""

import math

import pytest

from assistance.parking.geometry import OrientedBox, Pose, VehicleShape
from assistance.parking.slot_detection import (KIND_PARALLEL,
                                               KIND_PERPENDICULAR, Obstacle,
                                               PARALLEL_LENGTH_MARGIN_M,
                                               ParkingSlotDetector, SIDE_LEFT,
                                               SIDE_RIGHT)

EGO = Pose(0.0, 0.0, 0.0)
CAR_L, CAR_W = 4.5, 1.8


def shape():
    return VehicleShape(CAR_L, CAR_W)


def detector():
    return ParkingSlotDetector(shape())


def parked(x, y, yaw=0.0, length=CAR_L, width=CAR_W, key=None, speed=0.0):
    return Obstacle(OrientedBox(x, y, yaw, length, width),
                    key if key is not None else (round(x, 2), round(y, 2)),
                    speed)


class TestParallelSlots:
    def test_gap_between_two_cars_is_found(self):
        """Two cars in a row on the right, 7.5 m of clear kerb between them."""
        obstacles = [parked(0.0, -3.0), parked(12.0, -3.0)]
        slots = detector().scan(EGO, obstacles)
        assert len(slots) >= 1
        slot = next(s for s in slots if not s.open_ended)
        assert slot.kind == KIND_PARALLEL
        assert slot.side == SIDE_RIGHT
        assert slot.length == pytest.approx(7.5)
        assert slot.ahead_of_ego == pytest.approx(6.0)

    def test_target_lines_up_with_the_neighbours(self):
        obstacles = [parked(0.0, -3.0), parked(12.0, -3.0)]
        slot = next(s for s in detector().scan(EGO, obstacles) if not s.open_ended)
        # Neighbours occupy y -3.9..-2.1; a 1.8 m wide car flush with their
        # near edge has its centre at -2.1 - 0.9 = -3.0.
        assert slot.target.y == pytest.approx(-3.0)
        assert slot.target.x == pytest.approx(6.0)
        assert slot.target.yaw == pytest.approx(0.0)

    def test_gap_one_metre_too_short_is_rejected(self):
        """4.5 m car, 2.5 m margin -- 7.0 m is the line, 6.0 m is not a slot."""
        obstacles = [parked(0.0, -3.0), parked(10.5, -3.0)]  # 6.0 m gap
        closed = [s for s in detector().scan(EGO, obstacles) if not s.open_ended]
        assert closed == []

    def test_gap_just_long_enough_is_accepted(self):
        obstacles = [parked(0.0, -3.0), parked(11.6, -3.0)]  # 7.1 m gap
        closed = [s for s in detector().scan(EGO, obstacles) if not s.open_ended]
        assert len(closed) == 1

    def test_slot_on_the_left(self):
        obstacles = [parked(0.0, 3.0), parked(12.0, 3.0)]
        slot = next(s for s in detector().scan(EGO, obstacles) if not s.open_ended)
        assert slot.side == SIDE_LEFT
        assert slot.target.y == pytest.approx(3.0)

    def test_behind_the_ego_is_still_a_slot(self):
        """The car has driven past -- that is exactly when it can reverse in."""
        obstacles = [parked(-20.0, -3.0), parked(-8.0, -3.0)]
        slot = next(s for s in detector().scan(EGO, obstacles) if not s.open_ended)
        assert slot.ahead_of_ego < 0.0
        assert slot.length == pytest.approx(7.5)

    def test_open_ended_slot_behind_the_last_car(self):
        slots = detector().scan(EGO, [parked(0.0, -3.0)])
        assert slots, "a single parked car still bounds a space at each end"
        assert all(s.open_ended for s in slots)
        # Two of them: one in front of the car, one behind it.
        assert len(slots) == 2
        centres = sorted(s.ahead_of_ego for s in slots)
        # Half a car plus half the cut-off open slot, either side of it.
        reach = CAR_L * 0.5 + (CAR_L + PARALLEL_LENGTH_MARGIN_M + 1.5) * 0.5
        assert centres[0] == pytest.approx(-reach, abs=0.01)
        assert centres[1] == pytest.approx(reach, abs=0.01)

    def test_open_end_is_cut_off_not_unbounded(self):
        slot = detector().scan(EGO, [parked(0.0, -3.0)])[0]
        assert slot.length == pytest.approx(CAR_L + PARALLEL_LENGTH_MARGIN_M + 1.5)


class TestRejections:
    def test_no_obstacles_no_slots(self):
        assert detector().scan(EGO, []) == []

    def test_moving_car_is_not_a_boundary(self):
        obstacles = [parked(0.0, -3.0, speed=10.0), parked(12.0, -3.0, speed=10.0)]
        assert detector().scan(EGO, obstacles) == []

    def test_a_car_rolling_through_the_gap_blocks_it(self):
        obstacles = [parked(0.0, -3.0), parked(12.0, -3.0),
                     parked(6.0, -3.0, speed=8.0, key='rolling')]
        closed = [s for s in detector().scan(EGO, obstacles) if not s.open_ended]
        assert closed == []

    def test_something_standing_in_the_gap_blocks_it(self):
        obstacles = [parked(0.0, -3.0), parked(12.0, -3.0),
                     parked(6.0, -3.2, length=1.0, width=1.0, key='bollard')]
        closed = [s for s in detector().scan(EGO, obstacles) if not s.open_ended]
        assert closed == []

    def test_mixed_orientation_neighbours_are_not_a_slot(self):
        """One car along the road, one square to it -- that is not a bay."""
        obstacles = [parked(0.0, -3.0, yaw=0.0),
                     parked(12.0, -4.0, yaw=math.pi / 2)]
        closed = [s for s in detector().scan(EGO, obstacles) if not s.open_ended]
        assert closed == []

    def test_diagonal_neighbours_are_neither_kind(self):
        obstacles = [parked(0.0, -3.0, yaw=math.radians(45)),
                     parked(12.0, -3.0, yaw=math.radians(45))]
        assert detector().scan(EGO, obstacles) == []

    def test_row_starting_inside_our_own_flank_is_rejected(self):
        """A kerb line closer than half our width is an obstruction."""
        obstacles = [parked(0.0, -1.2), parked(12.0, -1.2)]
        assert detector().scan(EGO, obstacles) == []

    def test_far_side_of_the_road_is_out_of_range(self):
        obstacles = [parked(0.0, -9.0), parked(12.0, -9.0)]
        assert detector().scan(EGO, obstacles) == []

    def test_wall_behind_a_parallel_row_does_not_block_it(self):
        """A parallel slot only needs one car width of depth.

        Regression guard: measuring the depth from the neighbours' own width
        rejects every kerb in the game, because a row of parallel-parked cars
        is exactly one car deep.
        """
        obstacles = [parked(0.0, -3.0), parked(12.0, -3.0),
                     parked(6.0, -6.0, length=20.0, width=0.3, key='wall')]
        closed = [s for s in detector().scan(EGO, obstacles) if not s.open_ended]
        assert len(closed) == 1
        # Depth runs from the kerb line at 2.1 m out to the wall's near face.
        assert closed[0].depth == pytest.approx(6.0 - 0.15 - 2.1, abs=0.01)


class TestPerpendicularSlots:
    @staticmethod
    def _bay_neighbours(gap_centre_x=6.0):
        """Two cars parked nose-in on the right, square to the road.

        Each is 4.5 m long pointing -Y, so it occupies y -2.5 .. -7.0, and
        1.8 m wide. The gap between them is centred on *gap_centre_x*.
        """
        left = parked(gap_centre_x - 1.5, -4.75, yaw=math.pi / 2)
        right = parked(gap_centre_x + 1.5, -4.75, yaw=math.pi / 2)
        return [left, right]

    def test_bay_too_narrow_is_rejected(self):
        # 1.5 m apart, minus 1.8 m of car width -> no gap at all.
        assert [s for s in detector().scan(EGO, self._bay_neighbours())
                if s.kind == KIND_PERPENDICULAR and not s.open_ended] == []

    def test_wide_enough_bay_is_found(self):
        left = parked(2.0, -4.75, yaw=math.pi / 2)
        right = parked(8.0, -4.75, yaw=math.pi / 2)
        slots = [s for s in detector().scan(EGO, [left, right])
                 if s.kind == KIND_PERPENDICULAR and not s.open_ended]
        assert len(slots) == 1
        slot = slots[0]
        # Their near edges are at y = -2.5, so the kerb line is 2.5 m out.
        # Gap between the two bodies: 2.9 .. 7.1 -> 4.2 m wide.
        assert slot.length == pytest.approx(4.2)
        assert slot.side == SIDE_RIGHT

    def test_target_points_back_out_of_the_bay(self):
        left = parked(2.0, -4.75, yaw=math.pi / 2)
        right = parked(8.0, -4.75, yaw=math.pi / 2)
        slot = [s for s in detector().scan(EGO, [left, right])
                if s.kind == KIND_PERPENDICULAR and not s.open_ended][0]
        # Bay on the right, reversed into -> the nose ends up pointing left.
        assert slot.target.yaw == pytest.approx(math.pi / 2)
        assert slot.target.x == pytest.approx(5.0)
        # Nothing closes the bay, so the car lines up with its neighbours:
        # nose on the kerb line at -2.5, tail level with theirs at -7.0.
        assert slot.target.y == pytest.approx(-4.75, abs=0.01)

    def test_a_wall_at_the_back_keeps_the_tail_clear(self):
        """Long neighbours would align us deeper than the wall allows.

        Two 5.5 m vans nose-in, their tails 0.3 m off a wall. Lining up with
        them would put our tail 0.3 m off it too; the back clearance wins and
        the car stops 0.4 m short.
        """
        left = parked(2.0, -5.25, yaw=math.pi / 2, length=5.5)
        right = parked(8.0, -5.25, yaw=math.pi / 2, length=5.5)
        wall = parked(5.0, -8.45, length=6.0, width=0.3, key='wall')
        slot = [s for s in detector().scan(EGO, [left, right, wall])
                if s.kind == KIND_PERPENDICULAR and not s.open_ended][0]
        # Kerb line 2.5 m out, wall's near face 8.3 m out -> 5.8 m of depth.
        assert slot.depth == pytest.approx(5.8, abs=0.01)
        # Tail at 8.3 - 0.4 = 7.9 m out, so the centre is 7.9 - 2.25 = 5.65.
        assert slot.target.y == pytest.approx(-5.65, abs=0.01)


class TestStability:
    def test_slot_id_survives_the_car_moving(self):
        """The offer on screen must not flicker while the driver rolls past."""
        obstacles = [parked(0.0, -3.0), parked(12.0, -3.0)]
        first = next(s for s in detector().scan(EGO, obstacles) if not s.open_ended)
        moved = Pose(3.0, 0.2, math.radians(4))
        second = next(s for s in detector().scan(moved, obstacles)
                      if not s.open_ended)
        assert first.slot_id == second.slot_id

    def test_slots_are_returned_nearest_first(self):
        obstacles = [parked(-30.0, -3.0), parked(-18.0, -3.0),
                     parked(2.0, -3.0), parked(14.0, -3.0)]
        slots = detector().scan(EGO, obstacles)
        distances = [abs(s.ahead_of_ego) for s in slots]
        assert distances == sorted(distances)


class TestCost:
    def test_scan_is_cheap_enough_for_the_cycle(self):
        """40 obstacles, both sides, well under a tenth of the 100 ms budget."""
        import time
        obstacles = [parked(i * 6.0 - 60.0, -3.0, key=i) for i in range(20)]
        obstacles += [parked(i * 6.0 - 60.0, 3.0, key=100 + i) for i in range(20)]
        scanner = detector()
        scanner.scan(EGO, obstacles)          # warm up
        started = time.perf_counter()
        for _ in range(20):
            scanner.scan(EGO, obstacles)
        per_scan_ms = (time.perf_counter() - started) / 20 * 1000.0
        assert per_scan_ms < 10.0, f"{per_scan_ms:.2f} ms per scan"
