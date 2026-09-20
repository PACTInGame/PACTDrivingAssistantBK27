"""Poses, oriented boxes and the ego footprint -- metres and radians only.

This module is the boundary at which LFS units stop. Everything above it works
in metres and mathematical radians (0 = +X, anticlockwise), which is the frame
``(heading + 16384) / 182.05`` produces and the one ``math.cos``/``math.sin``
expect (``reference/conventions.md`` §1-2). Nothing here imports LFS, pyinsim
or the event bus, so all of it is testable off Windows.

Two conversions exist and only one of them is ever needed in the hot path:

``pose_from_mci``   an MCI/``VehicleData`` snapshot -> :class:`Pose`
``OrientedBox``     centre, size and yaw -> the four corners, once

The overlap test is a separating-axis test specialised to two rectangles. For
two convex boxes there are only four candidate axes (two per box), and because
a rectangle's axes are perpendicular, two of the four projections are free.
That is ~30 floating point operations per pair -- an order of magnitude below
the general polygon intersection in :mod:`misc.spacial_hash_grid`, which walks
every edge pair. The parking collision check runs it a few hundred times per
plan (path samples x obstacles), so the constant matters; it never runs per
assistance cycle.
"""

import math
from dataclasses import dataclass
from typing import Iterable, List, Optional, Sequence, Tuple

# MCI and OutSim positions are 1/65536 m (``reference/conventions.md`` §1).
MCI_TO_M = 1.0 / 65536.0
# LFS heading word -> degrees, and the +90 deg that rotates "0 = +Y" to
# "0 = +X". Spelled out rather than reusing the 182.05 idiom because this
# module works in radians throughout.
HEADING_WORD_TO_RAD = 2.0 * math.pi / 65536.0
HEADING_WORD_QUARTER = 16384


def normalise_angle(angle: float) -> float:
    """Fold an angle into (-pi, pi].

    Every angle comparison in this package goes through this. Subtracting two
    raw headings is the bug class ``conventions.md`` §2 opens with, and the
    fix is the same here even though both operands are already radians.
    """
    return (angle + math.pi) % (2.0 * math.pi) - math.pi


def heading_to_rad(heading_word: float) -> float:
    """LFS heading word -> mathematical radians (0 = +X, anticlockwise)."""
    return normalise_angle((heading_word + HEADING_WORD_QUARTER) * HEADING_WORD_TO_RAD)


@dataclass(frozen=True)
class Pose:
    """Where a rigid body is: position in metres, yaw in mathematical radians.

    ``yaw`` is where the body *points*, never where it moves -- LFS keeps those
    apart as ``Heading`` and ``Direction`` and so does this package. A reversing
    car has an unchanged yaw and an inverted direction of travel, and the
    parking manoeuvre depends on exactly that distinction.
    """
    x: float
    y: float
    yaw: float

    @property
    def forward(self) -> Tuple[float, float]:
        """Unit vector the body points along."""
        return math.cos(self.yaw), math.sin(self.yaw)

    @property
    def left(self) -> Tuple[float, float]:
        """Unit vector to the body's left -- yaw + 90 deg."""
        return -math.sin(self.yaw), math.cos(self.yaw)

    def to_local(self, x: float, y: float) -> Tuple[float, float]:
        """World point -> (ahead, left) in this pose's frame, in metres."""
        dx = x - self.x
        dy = y - self.y
        cos_yaw = math.cos(self.yaw)
        sin_yaw = math.sin(self.yaw)
        return dx * cos_yaw + dy * sin_yaw, -dx * sin_yaw + dy * cos_yaw

    def to_world(self, ahead: float, left: float) -> Tuple[float, float]:
        """(ahead, left) in this pose's frame -> world point, in metres."""
        cos_yaw = math.cos(self.yaw)
        sin_yaw = math.sin(self.yaw)
        return (self.x + ahead * cos_yaw - left * sin_yaw,
                self.y + ahead * sin_yaw + left * cos_yaw)

    def offset(self, ahead: float, left: float = 0.0,
               turn: float = 0.0) -> 'Pose':
        """A pose displaced in this one's frame and optionally rotated."""
        x, y = self.to_world(ahead, left)
        return Pose(x, y, normalise_angle(self.yaw + turn))


def pose_from_mci(x_raw: float, y_raw: float, heading_word: float) -> Pose:
    """An MCI/``VehicleData`` snapshot -> :class:`Pose`.

    The one place raw LFS units are read in this package. ``x``/``y`` are the
    1/65536 m integers of ``CompCar``; ``heading_word`` is its ``Heading``.
    """
    return Pose(x_raw * MCI_TO_M, y_raw * MCI_TO_M, heading_to_rad(heading_word))


@dataclass(frozen=True)
class OrientedBox:
    """A rectangle with a heading: the footprint of a car or a layout object.

    ``length`` runs along ``yaw``, ``width`` across it -- the same order
    ``assistance/park_distance_control.get_vehicle_size`` returns.
    """
    x: float
    y: float
    yaw: float
    length: float
    width: float

    @property
    def pose(self) -> Pose:
        return Pose(self.x, self.y, self.yaw)

    def corners(self) -> List[Tuple[float, float]]:
        """The four corners, anticlockwise from front-left."""
        cos_yaw = math.cos(self.yaw)
        sin_yaw = math.sin(self.yaw)
        half_l = self.length * 0.5
        half_w = self.width * 0.5
        ax, ay = half_l * cos_yaw, half_l * sin_yaw       # along the body
        bx, by = -half_w * sin_yaw, half_w * cos_yaw      # across it
        return [(self.x + ax + bx, self.y + ay + by),
                (self.x - ax + bx, self.y - ay + by),
                (self.x - ax - bx, self.y - ay - by),
                (self.x + ax - bx, self.y + ay - by)]

    def inflated(self, margin: float) -> 'OrientedBox':
        """The same box grown by *margin* on every side."""
        return OrientedBox(self.x, self.y, self.yaw,
                           self.length + 2.0 * margin, self.width + 2.0 * margin)

    def extent_along(self, pose: Pose) -> Tuple[float, float, float, float]:
        """Extent in *pose*'s frame: ``(ahead_min, ahead_max, left_min, left_max)``.

        This is how a slot is measured: project a neighbouring car onto the
        direction we are driving and the direction we would move sideways, and
        the gap between two such projections is the slot.
        """
        ahead_min = left_min = math.inf
        ahead_max = left_max = -math.inf
        for cx, cy in self.corners():
            ahead, left = pose.to_local(cx, cy)
            if ahead < ahead_min:
                ahead_min = ahead
            if ahead > ahead_max:
                ahead_max = ahead
            if left < left_min:
                left_min = left
            if left > left_max:
                left_max = left
        return ahead_min, ahead_max, left_min, left_max


def box_from_corners(corners: Sequence[Tuple[float, float]]) -> Optional[OrientedBox]:
    """Recover an :class:`OrientedBox` from four corner points.

    Layout obstacles reach this package as the corner lists
    ``park_distance_control.create_rectangle_for_object`` builds, because that
    is what the spatial grid stores. Slot detection wants length, width and a
    heading, so the rectangle is read back rather than re-derived from the
    ``ObjectInfo`` -- one less place that has to know the object size table.

    Returns ``None`` for anything that is not four points; a degenerate
    rectangle (a zero-length edge) is returned as the zero-sized box it is,
    which the callers reject on size rather than on shape.

    **The yaw is only defined modulo pi.** A rectangle has no front, so
    nothing in four corner points says which end of the long axis is the nose.
    Everything that consumes this treats orientation modulo pi already --
    ``slot_detection._orientation_class`` folds the angle into 0..90 deg,
    because a car facing the other way down the same road is still parked
    along it.
    """
    if corners is None or len(corners) != 4:
        return None
    (x0, y0), (x1, y1), (x2, y2), (x3, y3) = corners
    cx = (x0 + x1 + x2 + x3) * 0.25
    cy = (y0 + y1 + y2 + y3) * 0.25
    # Edge 0->1 and edge 1->2 are the two side lengths; the longer one is
    # taken as the body's length so that ``yaw`` points along it.
    e1 = math.hypot(x1 - x0, y1 - y0)
    e2 = math.hypot(x2 - x1, y2 - y1)
    if e1 >= e2:
        yaw = math.atan2(y1 - y0, x1 - x0)
        return OrientedBox(cx, cy, normalise_angle(yaw), e1, e2)
    yaw = math.atan2(y2 - y1, x2 - x1)
    return OrientedBox(cx, cy, normalise_angle(yaw), e2, e1)


def boxes_overlap(a: OrientedBox, b: OrientedBox) -> bool:
    """Separating-axis test for two rectangles.

    Four candidate axes, two per box; a rectangle's own two axes are
    perpendicular, so each box's projection onto its own axes is just its half
    extents and costs nothing. Returns True when no separating axis exists,
    i.e. the two rectangles share at least a boundary point.

    A cheap radius rejection runs first: two boxes whose centres are further
    apart than the sum of their half diagonals cannot touch, and that is the
    overwhelmingly common case when sweeping a path past a handful of parked
    cars.
    """
    dx = b.x - a.x
    dy = b.y - a.y
    reach_a = math.hypot(a.length, a.width) * 0.5
    reach_b = math.hypot(b.length, b.width) * 0.5
    if dx * dx + dy * dy > (reach_a + reach_b) ** 2:
        return False

    a_half = (a.length * 0.5, a.width * 0.5)
    b_half = (b.length * 0.5, b.width * 0.5)
    a_axes = ((math.cos(a.yaw), math.sin(a.yaw)),
              (-math.sin(a.yaw), math.cos(a.yaw)))
    b_axes = ((math.cos(b.yaw), math.sin(b.yaw)),
              (-math.sin(b.yaw), math.cos(b.yaw)))

    for axis, own_half in ((a_axes[0], a_half[0]), (a_axes[1], a_half[1])):
        centre_gap = abs(dx * axis[0] + dy * axis[1])
        other = (b_half[0] * abs(b_axes[0][0] * axis[0] + b_axes[0][1] * axis[1])
                 + b_half[1] * abs(b_axes[1][0] * axis[0] + b_axes[1][1] * axis[1]))
        if centre_gap > own_half + other:
            return False

    for axis, own_half in ((b_axes[0], b_half[0]), (b_axes[1], b_half[1])):
        centre_gap = abs(dx * axis[0] + dy * axis[1])
        other = (a_half[0] * abs(a_axes[0][0] * axis[0] + a_axes[0][1] * axis[1])
                 + a_half[1] * abs(a_axes[1][0] * axis[0] + a_axes[1][1] * axis[1]))
        if centre_gap > own_half + other:
            return False

    return True


@dataclass(frozen=True)
class VehicleShape:
    """The ego car's dimensions and where its reference point sits.

    LFS reports a car's position at its own reference point, which is not the
    centre of the rear axle that every bicycle-model equation is written for.
    ``rear_axle_offset`` is how far *behind* the reported point that axle is,
    and ``wheelbase`` is what turns a steering angle into a curvature. Neither
    can be looked up for a vehicle mod (``conventions.md`` §4), so both carry
    conservative defaults and the planner never depends on them being exact:
    the achievable curvature is *measured* during the manoeuvre
    (``CurvatureModel`` in :mod:`Controls.vehicle_control`), and the footprint
    used for clearance is the conservative one.
    """
    length: float
    width: float
    wheelbase: float = 2.5
    rear_axle_offset: float = 1.25

    def footprint(self, pose: Pose, margin: float = 0.0) -> OrientedBox:
        """The body outline at *pose*, optionally grown by *margin*."""
        return OrientedBox(pose.x, pose.y, pose.yaw,
                           self.length + 2.0 * margin, self.width + 2.0 * margin)

    def rear_axle(self, pose: Pose) -> Pose:
        """The rear axle centre for a body at *pose*."""
        return pose.offset(-self.rear_axle_offset)

    def body_from_rear_axle(self, axle: Pose) -> Pose:
        """The inverse of :meth:`rear_axle`."""
        return axle.offset(self.rear_axle_offset)


def swept_collision(shape: VehicleShape, poses: Iterable[Pose],
                    obstacles: Sequence[OrientedBox],
                    margin: float = 0.0) -> Optional[int]:
    """Index of the first obstacle a body at any of *poses* would touch.

    Returns ``None`` when the whole sweep is clear. The obstacle index rather
    than a bare bool because the caller wants to say *what* blocked the path --
    a plan rejected without a reason is a plan nobody can debug.

    Cost: one :func:`boxes_overlap` per pose per obstacle, with the radius
    rejection carrying almost all of them. A parallel-parking plan samples
    ~60 poses; against 10 obstacles that is 600 tests, ~1 ms. It runs once per
    plan, never per assistance cycle.
    """
    if not obstacles:
        return None
    for pose in poses:
        body = shape.footprint(pose, margin)
        for index, obstacle in enumerate(obstacles):
            if boxes_overlap(body, obstacle):
                return index
    return None
