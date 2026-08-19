"""Pure geometry helpers (``misc/helpers.py``).

Angles here are *maths* degrees: 0 = +X, counting anticlockwise. That is what
``calc_polygon_points`` expects, and what the LFS heading idiom
``(heading + 16384) / 182.05`` produces (``reference/conventions.md`` §2).
"""

import math

import pytest

from misc.helpers import calc_polygon_points, point_in_rectangle

from conftest import lfs_heading


def approx_point(point, expected, tol=1e-6):
    assert point[0] == pytest.approx(expected[0], abs=tol)
    assert point[1] == pytest.approx(expected[1], abs=tol)


# ─── calc_polygon_points ─────────────────────────────────────────────────────

@pytest.mark.parametrize("angle,expected", [
    (0.0, (10.0, 0.0)),
    (90.0, (0.0, 10.0)),
    (180.0, (-10.0, 0.0)),
    (270.0, (0.0, -10.0)),
    (360.0, (10.0, 0.0)),
    (-90.0, (0.0, -10.0)),
])
def test_calc_polygon_points_cardinal_directions(angle, expected):
    approx_point(calc_polygon_points(0.0, 0.0, 10.0, angle), expected)


def test_calc_polygon_points_is_relative_to_the_origin_given():
    approx_point(calc_polygon_points(100.0, -50.0, 5.0, 0.0), (105.0, -50.0))


def test_calc_polygon_points_zero_length_returns_the_origin():
    approx_point(calc_polygon_points(3.0, 4.0, 0.0, 137.0), (3.0, 4.0))


def test_calc_polygon_points_negative_length_points_backwards():
    approx_point(calc_polygon_points(0.0, 0.0, -10.0, 0.0), (-10.0, 0.0))


def test_lfs_heading_idiom_feeds_calc_polygon_points():
    """A car heading north (+Y) must project a point north of itself.

    This is the conversion every assistance system does before building its
    detection polygons; getting it wrong is a silent 90° error.
    """
    heading_north = lfs_heading(0.0)          # CompCar.Heading, 0 = +Y
    angle_of_car = (heading_north + 16384) / 182.05

    approx_point(calc_polygon_points(0.0, 0.0, 10.0, angle_of_car), (0.0, 10.0), tol=1e-3)


# ─── point_in_rectangle ──────────────────────────────────────────────────────

AXIS_ALIGNED = [(0.0, 0.0), (4.0, 0.0), (4.0, 2.0), (0.0, 2.0)]


def rotated_rectangle(cx, cy, length, width, angle_deg):
    """Corners of a rectangle centred on (cx, cy), rotated by *angle_deg*."""
    half_l, half_w = length / 2.0, width / 2.0
    rad = math.radians(angle_deg)
    cos_a, sin_a = math.cos(rad), math.sin(rad)
    return [
        (cx + dx * cos_a - dy * sin_a, cy + dx * sin_a + dy * cos_a)
        for dx, dy in ((-half_l, -half_w), (half_l, -half_w),
                       (half_l, half_w), (-half_l, half_w))
    ]


@pytest.mark.parametrize("point,inside", [
    ((2.0, 1.0), True),      # centre
    ((0.1, 0.1), True),      # just inside a corner
    ((-0.1, 1.0), False),    # left of it
    ((4.1, 1.0), False),     # right of it
    ((2.0, -0.1), False),    # below
    ((2.0, 2.1), False),     # above
])
def test_point_in_axis_aligned_rectangle(point, inside):
    assert point_in_rectangle(point[0], point[1], AXIS_ALIGNED) is inside


@pytest.mark.parametrize("point", [(0.0, 0.0), (4.0, 0.0), (4.0, 2.0), (0.0, 2.0),
                                   (2.0, 0.0)])
def test_points_on_the_edge_count_as_inside(point):
    """The cross-product test is inclusive -- a car touching the boundary warns."""
    assert point_in_rectangle(point[0], point[1], AXIS_ALIGNED) is True


@pytest.mark.parametrize("angle", [0.0, 30.0, 45.0, 90.0, 137.0, 180.0, -45.0])
def test_rotated_rectangle_contains_its_own_centre_and_not_its_far_side(angle):
    rect = rotated_rectangle(10.0, -5.0, 6.0, 2.0, angle)

    assert point_in_rectangle(10.0, -5.0, rect) is True
    # A point one length beyond the front edge, along the rectangle's own axis.
    outside_x = 10.0 + 6.0 * math.cos(math.radians(angle))
    outside_y = -5.0 + 6.0 * math.sin(math.radians(angle))
    assert point_in_rectangle(outside_x, outside_y, rect) is False


def test_rotated_rectangle_excludes_the_corner_of_its_bounding_box():
    """A 45°-rotated rectangle must not behave like its axis-aligned hull."""
    rect = rotated_rectangle(0.0, 0.0, 4.0, 4.0, 45.0)   # diamond, half-diagonal 2*sqrt(2)

    assert point_in_rectangle(0.0, 2.5, rect) is True    # on the vertical diagonal
    assert point_in_rectangle(2.0, 2.0, rect) is False   # hull corner, outside the diamond


def test_winding_direction_does_not_matter():
    clockwise = list(reversed(AXIS_ALIGNED))

    assert point_in_rectangle(2.0, 1.0, clockwise) is True
    assert point_in_rectangle(5.0, 1.0, clockwise) is False


@pytest.mark.xfail(strict=False, reason=(
    "point_in_rectangle uses only cross-product signs, so a degenerate "
    "(zero-area) rectangle swallows the whole line it lies on. No work package "
    "owns misc/helpers.py; reported by WP1."))
def test_degenerate_rectangle_does_not_extend_past_its_own_ends():
    flat = [(0.0, 0.0), (10.0, 0.0), (10.0, 0.0), (0.0, 0.0)]

    assert point_in_rectangle(5.0, 0.0, flat) is True     # on the segment
    assert point_in_rectangle(-1.0, 0.0, flat) is False   # before it
    assert point_in_rectangle(11.0, 0.0, flat) is False   # past it


@pytest.mark.xfail(strict=False, reason=(
    "a rectangle collapsed to a single point currently contains every point. "
    "No work package owns misc/helpers.py; reported by WP1."))
def test_rectangle_collapsed_to_a_point_contains_only_that_point():
    collapsed = [(0.0, 0.0)] * 4

    assert point_in_rectangle(0.0, 0.0, collapsed) is True
    assert point_in_rectangle(1.0, 1.0, collapsed) is False
