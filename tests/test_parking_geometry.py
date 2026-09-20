"""Poses, boxes and the overlap test -- the units every parking module rests on."""

import math

import pytest

from assistance.parking.geometry import (MCI_TO_M, OrientedBox, Pose,
                                         VehicleShape, box_from_corners,
                                         boxes_overlap, heading_to_rad,
                                         normalise_angle, pose_from_mci,
                                         swept_collision)


class TestAngles:
    def test_normalise_folds_into_half_turn(self):
        """Half a turn lands on one of the two ends; both name the same ray."""
        assert abs(normalise_angle(3 * math.pi)) == pytest.approx(math.pi)
        assert abs(normalise_angle(-3 * math.pi)) == pytest.approx(math.pi)
        assert normalise_angle(0.5) == pytest.approx(0.5)
        assert -math.pi <= normalise_angle(7.9) <= math.pi

    def test_heading_zero_points_north(self):
        """LFS heading 0 is +Y; the maths frame calls that +90 deg."""
        assert heading_to_rad(0) == pytest.approx(math.pi / 2)

    def test_heading_quarter_points_east(self):
        """16384 is a quarter turn anticlockwise from +Y, i.e. -X.

        The +16384 offset in the conversion means heading 16384 lands on
        180 deg, which ``normalise_angle`` reports as +pi.
        """
        assert abs(heading_to_rad(16384)) == pytest.approx(math.pi)

    def test_matches_the_project_idiom(self):
        """Must agree with ``(heading + 16384) / 182.05`` used everywhere else.

        Not to the last bit: 182.05 is the rounded form of 65536/360 =
        182.0444..., so the two drift by up to 0.02 deg over a full turn. That
        is the existing constant's error, not a disagreement about the frame.
        """
        for heading in (0, 1000, 20000, 45000, 65000):
            legacy_deg = (heading + 16384) / 182.05
            delta = normalise_angle(heading_to_rad(heading)
                                    - math.radians(legacy_deg))
            assert abs(delta) < math.radians(0.02)


class TestPose:
    def test_local_frame_round_trip(self):
        pose = Pose(10.0, -4.0, 0.7)
        x, y = pose.to_world(3.0, -1.5)
        ahead, left = pose.to_local(x, y)
        assert ahead == pytest.approx(3.0)
        assert left == pytest.approx(-1.5)

    def test_forward_and_left_are_perpendicular(self):
        pose = Pose(0, 0, 1.1)
        fx, fy = pose.forward
        lx, ly = pose.left
        assert fx * lx + fy * ly == pytest.approx(0.0, abs=1e-12)

    def test_point_ahead_is_positive(self):
        pose = Pose(0.0, 0.0, 0.0)          # pointing +X
        ahead, left = pose.to_local(5.0, 0.0)
        assert ahead == pytest.approx(5.0)
        assert left == pytest.approx(0.0)
        # +Y is to the left of a car pointing +X.
        _, left = pose.to_local(0.0, 2.0)
        assert left == pytest.approx(2.0)

    def test_from_mci_converts_units(self):
        pose = pose_from_mci(65536 * 12, 65536 * -3, 0)
        assert pose.x == pytest.approx(12.0)
        assert pose.y == pytest.approx(-3.0)
        assert MCI_TO_M * 65536 == pytest.approx(1.0)


class TestOrientedBox:
    def test_axis_aligned_corners(self):
        box = OrientedBox(0.0, 0.0, 0.0, 4.0, 2.0)
        xs = sorted(c[0] for c in box.corners())
        ys = sorted(c[1] for c in box.corners())
        assert xs[0] == pytest.approx(-2.0)
        assert xs[-1] == pytest.approx(2.0)
        assert ys[0] == pytest.approx(-1.0)
        assert ys[-1] == pytest.approx(1.0)

    def test_extent_in_another_frame(self):
        ego = Pose(0.0, 0.0, 0.0)
        box = OrientedBox(10.0, 3.0, 0.0, 4.0, 2.0)
        ahead_min, ahead_max, left_min, left_max = box.extent_along(ego)
        assert ahead_min == pytest.approx(8.0)
        assert ahead_max == pytest.approx(12.0)
        assert left_min == pytest.approx(2.0)
        assert left_max == pytest.approx(4.0)

    def test_corners_round_trip_through_box_from_corners(self):
        box = OrientedBox(3.0, -1.0, 0.6, 5.0, 2.0)
        recovered = box_from_corners(box.corners())
        assert recovered.x == pytest.approx(box.x)
        assert recovered.y == pytest.approx(box.y)
        assert recovered.length == pytest.approx(box.length)
        assert recovered.width == pytest.approx(box.width)
        # A rectangle has no front, so the yaw is only defined modulo pi.
        assert abs(math.sin(recovered.yaw - box.yaw)) == pytest.approx(0.0, abs=1e-9)

    def test_box_from_corners_rejects_wrong_shape(self):
        assert box_from_corners(None) is None
        assert box_from_corners([(0, 0), (1, 1)]) is None


class TestOverlap:
    def test_disjoint_boxes(self):
        a = OrientedBox(0, 0, 0, 4, 2)
        b = OrientedBox(10, 0, 0, 4, 2)
        assert not boxes_overlap(a, b)

    def test_identical_boxes(self):
        a = OrientedBox(0, 0, 0.3, 4, 2)
        assert boxes_overlap(a, a)

    def test_touching_counts_as_overlap(self):
        a = OrientedBox(0, 0, 0, 4, 2)
        b = OrientedBox(4, 0, 0, 4, 2)
        assert boxes_overlap(a, b)

    def test_rotation_separates(self):
        """A pair that overlaps as AABBs but not once rotated.

        This is the case a bounding-box-only test gets wrong, and the reason
        the separating-axis test is here at all.
        """
        # A long plank along the y = x diagonal. Its bounding box is the whole
        # square; the plank itself is only 1 m wide.
        a = OrientedBox(0.0, 0.0, math.pi / 4, 8.0, 1.0)
        # Well inside that bounding box, but 2.8 m off the diagonal.
        b = OrientedBox(-2.0, 2.0, -math.pi / 4, 2.0, 1.0)
        assert not boxes_overlap(a, b)
        # The same short plank moved onto the diagonal does touch it.
        c = OrientedBox(-0.5, 0.5, -math.pi / 4, 2.0, 1.0)
        assert boxes_overlap(a, c)

    def test_radius_rejection_agrees_with_full_test(self):
        """The cheap pre-check must never reject a genuine overlap."""
        a = OrientedBox(0, 0, 0.4, 4.5, 1.8)
        for dx in [i * 0.25 for i in range(-24, 25)]:
            for dy in [i * 0.25 for i in range(-12, 13)]:
                b = OrientedBox(dx, dy, 1.1, 4.5, 1.8)
                reach = (math.hypot(a.length, a.width)
                         + math.hypot(b.length, b.width)) * 0.5
                if math.hypot(dx, dy) > reach:
                    assert not boxes_overlap(a, b)


class TestVehicleShape:
    def test_rear_axle_round_trip(self):
        shape = VehicleShape(4.5, 1.8, wheelbase=2.6, rear_axle_offset=1.3)
        body = Pose(5.0, 2.0, 0.9)
        assert shape.body_from_rear_axle(shape.rear_axle(body)).x == pytest.approx(body.x)
        assert shape.body_from_rear_axle(shape.rear_axle(body)).y == pytest.approx(body.y)

    def test_rear_axle_is_behind_the_body_centre(self):
        shape = VehicleShape(4.5, 1.8)
        axle = shape.rear_axle(Pose(0.0, 0.0, 0.0))
        assert axle.x < 0.0

    def test_footprint_margin_grows_both_axes(self):
        shape = VehicleShape(4.0, 2.0)
        box = shape.footprint(Pose(0, 0, 0), margin=0.25)
        assert box.length == pytest.approx(4.5)
        assert box.width == pytest.approx(2.5)


class TestSweptCollision:
    def test_clear_sweep(self):
        shape = VehicleShape(4.5, 1.8)
        poses = [Pose(x, 0.0, 0.0) for x in range(0, 10)]
        obstacles = [OrientedBox(0.0, 6.0, 0.0, 4.5, 1.8)]
        assert swept_collision(shape, poses, obstacles) is None

    def test_reports_which_obstacle(self):
        shape = VehicleShape(4.5, 1.8)
        poses = [Pose(x, 0.0, 0.0) for x in range(0, 10)]
        obstacles = [OrientedBox(0.0, 20.0, 0.0, 4.5, 1.8),
                     OrientedBox(7.0, 0.0, 0.0, 4.5, 1.8)]
        assert swept_collision(shape, poses, obstacles) == 1

    def test_margin_makes_a_near_miss_a_hit(self):
        shape = VehicleShape(4.0, 2.0)
        poses = [Pose(0.0, 0.0, 0.0)]
        obstacles = [OrientedBox(0.0, 2.3, 0.0, 4.0, 2.0)]
        assert swept_collision(shape, poses, obstacles) is None
        assert swept_collision(shape, poses, obstacles, margin=0.4) == 0

    def test_no_obstacles_is_clear(self):
        assert swept_collision(VehicleShape(4.5, 1.8), [Pose(0, 0, 0)], []) is None
