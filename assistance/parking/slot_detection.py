"""Finding a parking space in a set of obstacles.

This is the sensing half of the self-parking feature, and it is written to work
the way an ultrasonic slot scanner in a real car works: drive past, watch the
side of the road, and measure the gap between two things that are standing
there. The difference is that LFS hands us the obstacles directly -- other cars
over MCI, layout objects over AXM -- so nothing has to be integrated over time
and there is no scan history to keep stale. **Every scan is computed from the
current frame alone**, which is why a slot cannot drift, cannot survive the
obstacle that defined it being removed, and needs no reset when the car is
teleported to the pits.

The whole search happens in the ego's own frame: ``ahead`` along the direction
the car points, ``lat`` towards the side being searched. A slot is then just a
gap on the ``ahead`` axis between two obstacles that both lie within a band of
``lat``.

### What makes a gap a parking space

Four questions, in the order they are cheapest to answer:

1. **Is it long enough?** ``ego length + PARALLEL_LENGTH_MARGIN_M`` for a
   parallel slot, ``ego width + PERPENDICULAR_WIDTH_MARGIN_M`` for a
   perpendicular one.
2. **Is it deep enough?** Deep enough to hold the car, and not blocked by
   something standing inside the gap further in (a wall at the back of a bay).
3. **Do its neighbours agree on an orientation?** Two cars parallel to the road
   mean a parallel slot, two cars square to it mean a perpendicular one. A gap
   between one of each is not a parking space, it is a coincidence, and it is
   rejected rather than guessed at.
4. **Is anything in it moving?** A gap bounded by a car that is still rolling is
   not a slot. ``MOVING_SPEED_KMH`` is the line.

### Slots with one neighbour

The common real case -- the space behind the last parked car -- has no obstacle
closing its far end. Those are accepted, with the open end cut off at
``required length + OPEN_END_EXTRA_M`` so the reported slot is a space and not
the rest of the road. A slot with no neighbour at all is not accepted: without
one there is nothing to take the kerb line and the parking orientation from,
and the result would be a guess about where the road edge is.

### Cost

One pass over the nearby obstacles per side: four corner transforms each, a
sort, and a walk over consecutive pairs. With the ~10-20 obstacles that survive
the range filter that is well under 0.2 ms, and it only runs below
``SCAN_MAX_SPEED_KMH`` (``AGENTS.md`` §1).
"""

import math
from dataclasses import dataclass
from typing import Hashable, List, Optional, Sequence, Tuple

from assistance.parking.geometry import (OrientedBox, Pose, VehicleShape,
                                         normalise_angle)

# ─── What counts as a slot ────────────────────────────────────────────────

# Parallel: how much longer than the car a space has to be before it is worth
# offering. **Measured against the planner**, not assumed, because a detector
# that is more optimistic than the planner is worse than one that is too
# strict: it offers the driver a space, the plan then fails, and the offer
# blinks on and off -- which is exactly what two live runs did with the 1.2 m
# this used to be.
#
# The measurement is a grid over car sizes 4.5-5.2 m, turning radii 4-7 m and
# lateral offsets 2.6-4.2 m, run against the planner itself
# (``tests/test_parking_agreement.py`` pins the result). Over that grid the
# shortest space the planner can enter needs between 1.5 and 2.45 m more than
# the car, depending mostly on the car's size. 2.5 m is the figure that holds
# everywhere rather than on average, and it agrees with what driving schools
# teach -- roughly one and a half car lengths: 4.5 + 2.5 = 7.0 m.
#
# One corner of the grid is deliberately outside that promise and is called
# out here rather than averaged away: the **largest** car (5.2 m) with the
# driver **closest** to the row (2.6 m centre-to-centre) needs up to 4.3 m,
# and no length constant that also serves a small car can cover it.
#
# **Length is the only thing this constant can answer for.** The same grid
# shows the binding limit is often not length at all but how far out from the
# parked row the driver stopped: at 2.4 m the requirement jumps to over 5 m
# of slack and at some radii nothing works at any length, because the car is
# too close to the row to swing in. The detector cannot answer that -- it is
# a property of the manoeuvre, not of the gap -- so the planner rejects it,
# and ``ParkAssist`` remembers the rejection and offers the next candidate
# instead of re-offering this one (``park_assist.UNPLANNABLE_TTL_S``). That
# split is deliberate: the detector owns the gap, the planner owns the
# manoeuvre.
PARALLEL_LENGTH_MARGIN_M = 2.5
# Perpendicular: the door has to open, and the swept path of the rear end needs
# room. 0.7 m total, i.e. 35 cm a side, is the narrow end of a marked bay.
PERPENDICULAR_WIDTH_MARGIN_M = 0.7
# How much deeper than the car the space has to be, in both kinds.
DEPTH_MARGIN_M = 0.5

# How far out from the car's own side a slot may start. Below the lower bound
# the obstacle is against our flank and is an obstruction, not a boundary;
# above the upper bound it is on the far side of the road.
MIN_LATERAL_M = 0.3
MAX_LATERAL_M = 7.0
# How far *past* the kerb line the detector is willing to look for the back of
# a bay. This is not the same number as MAX_LATERAL_M and conflating the two
# was a real defect: a perpendicular bay begins 2.5 m out and is 5 m deep, so a
# single 7 m limit on both made every marked bay look too shallow to park in.
MAX_DEPTH_M = 8.0
# How far ahead and behind obstacles are considered at all. Anything outside is
# either not yet relevant or long gone; the range is generous enough that a
# slot stays visible for the whole approach.
SCAN_RANGE_M = 30.0

# An open-ended slot is cut off this far past the length the car needs, so the
# reported space is a space rather than "everything beyond the last car".
OPEN_END_EXTRA_M = 1.5

# Orientation classification, against the direction the ego points. Folded into
# 0..90 deg, because a car facing the other way down the same road is still
# parallel to it.
PARALLEL_TOLERANCE_RAD = math.radians(30.0)
PERPENDICULAR_TOLERANCE_RAD = math.radians(30.0)

# Above this the obstacle is traffic, not a slot boundary.
MOVING_SPEED_KMH = 1.5
# The scan itself only runs below this; a slot found at road speed would be
# gone before it could be offered, and the cost belongs to nobody.
SCAN_MAX_SPEED_KMH = 30.0

# Where the car ends up across the slot. For a parallel slot it lines up with
# the near edge of its neighbours, which is what "parked in line" means; the
# offset moves it that much further from the kerb.
PARALLEL_KERB_OFFSET_M = 0.0
# For a perpendicular bay the tail stops this far short of whatever closes the
# bay at the back.
PERPENDICULAR_BACK_CLEARANCE_M = 0.4

SIDE_LEFT = 'left'
SIDE_RIGHT = 'right'
KIND_PARALLEL = 'parallel'
KIND_PERPENDICULAR = 'perpendicular'


def required_length(shape: VehicleShape, kind: str,
                    parallel_margin: float = PARALLEL_LENGTH_MARGIN_M,
                    perpendicular_margin: float = PERPENDICULAR_WIDTH_MARGIN_M
                    ) -> float:
    """How long the gap has to be along the road for this kind of slot."""
    if kind == KIND_PARALLEL:
        return shape.length + parallel_margin
    return shape.width + perpendicular_margin


def required_depth(shape: VehicleShape, kind: str,
                   depth_margin: float = DEPTH_MARGIN_M) -> float:
    """How deep it has to be, measured away from the road.

    Module level rather than a method because the planner needs the same
    number: it is what bounds how far into the unmeasured world beyond the
    kerb a manoeuvre may reach. Two copies of this formula would be two
    answers to "where does the parking space end".
    """
    if kind == KIND_PARALLEL:
        return shape.width + depth_margin
    return shape.length + depth_margin


@dataclass(frozen=True)
class Obstacle:
    """Something standing in the world that a slot can be measured against.

    *key* identifies it across cycles -- a PLID for a car, the AXM object key
    for a layout object -- so that a slot keeps its identity while the driver
    rolls past it and the offer on screen does not flicker. *speed_kmh* is what
    disqualifies a boundary that is still moving.
    """
    box: OrientedBox
    key: Hashable
    speed_kmh: float = 0.0

    @property
    def is_moving(self) -> bool:
        return self.speed_kmh > MOVING_SPEED_KMH


@dataclass(frozen=True)
class ParkingSlot:
    """A space the car could be parked in, measured in the world frame.

    ``target`` is where the **body centre** ends up, which is the pose the
    trajectory planner is asked to reach. ``entry`` is the reference pose at
    the mouth of the slot: its origin is the centre of the slot opening and its
    yaw is the slot's road direction, i.e. the direction the ego was driving.
    Everything the planner needs is expressed relative to those two.
    """
    kind: str
    side: str
    entry: Pose
    target: Pose
    length: float
    depth: float
    bounded_by: Tuple[Hashable, ...]
    open_ended: bool
    # Signed position of the slot centre along the ego's forward axis at the
    # moment it was measured. Negative means the car has already driven past
    # it, which is the state a reverse manoeuvre needs.
    ahead_of_ego: float

    @property
    def slot_id(self) -> Tuple:
        """Stable identity: kind, side and the obstacles that bound it."""
        return (self.kind, self.side) + tuple(sorted(map(repr, self.bounded_by)))


def _orientation_class(box_yaw: float, road_yaw: float) -> Optional[str]:
    """Is this obstacle parked along the road or across it?

    Folded into 0..90 deg: a car facing the other way down the same road is
    parallel to it, and a bay entered from either end is perpendicular to it.
    ``None`` means neither, i.e. an obstacle that says nothing about how cars
    park here.
    """
    delta = abs(normalise_angle(box_yaw - road_yaw))
    if delta > math.pi * 0.5:
        delta = math.pi - delta
    if delta <= PARALLEL_TOLERANCE_RAD:
        return KIND_PARALLEL
    if delta >= math.pi * 0.5 - PERPENDICULAR_TOLERANCE_RAD:
        return KIND_PERPENDICULAR
    return None


@dataclass
class _Projection:
    """One obstacle, measured in the ego frame of the side being scanned."""
    obstacle: Obstacle
    ahead_min: float
    ahead_max: float
    lat_min: float
    lat_max: float
    kind: Optional[str]


def _project(obstacle: Obstacle, ego: Pose, sign: float) -> _Projection:
    """Project an obstacle into the ego frame, mirrored for the right side.

    *sign* is +1 for the left side and -1 for the right, so that ``lat`` always
    grows *away from the car towards the side being scanned* and the rest of
    the algorithm is written once rather than twice.
    """
    ahead_min, ahead_max, left_min, left_max = obstacle.box.extent_along(ego)
    if sign > 0:
        lat_min, lat_max = left_min, left_max
    else:
        lat_min, lat_max = -left_max, -left_min
    return _Projection(obstacle, ahead_min, ahead_max, lat_min, lat_max,
                       _orientation_class(obstacle.box.yaw, ego.yaw))


class ParkingSlotDetector:
    """Turns obstacles into :class:`ParkingSlot` candidates.

    Stateless by construction: :meth:`scan` reads only its arguments. The
    caller owns everything that has to persist -- which slot was offered, which
    one the driver accepted -- because that is a decision, and decisions belong
    in the state machine, not in a measurement.
    """

    def __init__(self, shape: VehicleShape,
                 parallel_margin: float = PARALLEL_LENGTH_MARGIN_M,
                 perpendicular_margin: float = PERPENDICULAR_WIDTH_MARGIN_M,
                 depth_margin: float = DEPTH_MARGIN_M,
                 max_lateral: float = MAX_LATERAL_M,
                 max_depth: float = MAX_DEPTH_M):
        self.shape = shape
        self.parallel_margin = parallel_margin
        self.perpendicular_margin = perpendicular_margin
        self.depth_margin = depth_margin
        self.max_lateral = max_lateral
        self.max_depth = max_depth

    # ─── Requirements ─────────────────────────────────────────────────

    def required_length(self, kind: str) -> float:
        """How long the gap has to be along the road for this kind of slot."""
        return required_length(self.shape, kind, self.parallel_margin,
                               self.perpendicular_margin)

    def required_depth(self, kind: str) -> float:
        """How deep it has to be, measured away from the road."""
        return required_depth(self.shape, kind, self.depth_margin)

    # ─── Scanning ─────────────────────────────────────────────────────

    def scan(self, ego: Pose, obstacles: Sequence[Obstacle],
             sides: Sequence[str] = (SIDE_LEFT, SIDE_RIGHT)) -> List[ParkingSlot]:
        """Every slot visible from *ego* right now, nearest first.

        *obstacles* must already exclude the ego's own car. Moving obstacles
        are kept in the list -- they are not boundaries, but they still block a
        gap, and dropping them here would report a slot with a car rolling
        through it.
        """
        found: List[ParkingSlot] = []
        for side in sides:
            found.extend(self._scan_side(ego, obstacles, side))
        found.sort(key=lambda slot: abs(slot.ahead_of_ego))
        return found

    def _scan_side(self, ego: Pose, obstacles: Sequence[Obstacle],
                   side: str) -> List[ParkingSlot]:
        sign = 1.0 if side == SIDE_LEFT else -1.0
        projections: List[_Projection] = []
        for obstacle in obstacles:
            projected = _project(obstacle, ego, sign)
            if projected.ahead_max < -SCAN_RANGE_M or projected.ahead_min > SCAN_RANGE_M:
                continue
            if projected.lat_max < MIN_LATERAL_M:
                continue
            # Generous on the far side: an obstacle beyond the kerb line is
            # not a boundary but it can still be the back wall of a bay, and
            # _free_depth has to see it.
            if projected.lat_min > self.max_lateral + MAX_DEPTH_M:
                continue
            projections.append(projected)

        if not projections:
            return []
        projections.sort(key=lambda p: p.ahead_min)

        # Boundaries are the standing obstacles that say how cars park here.
        # Three things disqualify one, and each has its own reason:
        #
        # * a moving obstacle is traffic, not a neighbour -- but it stays in
        #   ``projections``, because it still blocks the gap it is rolling
        #   through;
        # * so does anything with no clear orientation relative to the road;
        # * and so does anything beyond the kerb band. That last one is not
        #   cosmetic: a wall running along the back of a row of perpendicular
        #   bays is perfectly parallel to the road, and treating it as a
        #   neighbour paired it with the cars in the bays, which then disagreed
        #   about the kind and rejected every bay in the row. A boundary is
        #   something standing *at* the kerb, not behind it.
        boundaries = [p for p in projections
                      if p.kind is not None and not p.obstacle.is_moving
                      and p.lat_min <= self.max_lateral]
        if not boundaries:
            return []

        slots: List[ParkingSlot] = []
        for index, first in enumerate(boundaries):
            second = boundaries[index + 1] if index + 1 < len(boundaries) else None
            slot = self._slot_between(ego, side, sign, first, second, projections)
            if slot is not None:
                slots.append(slot)
            if index == 0:
                # The open space *before* the first parked car is a slot too --
                # the one you reverse into from in front of the row.
                slot = self._slot_between(ego, side, sign, None, first, projections)
                if slot is not None:
                    slots.append(slot)
        return slots

    def _slot_between(self, ego: Pose, side: str, sign: float,
                      first: Optional[_Projection], second: Optional[_Projection],
                      all_projections: Sequence[_Projection]) -> Optional[ParkingSlot]:
        """Measure the gap between two boundaries; either may be missing.

        Returns ``None`` whenever the gap fails any of the four questions in
        the module docstring. Nothing here logs: it runs for every pair on
        every scan, and the interesting event is a slot being *found*, which
        the caller reports once.
        """
        present = [p for p in (first, second) if p is not None]
        if not present:
            return None

        # Both neighbours have to mean the same thing by "parked here".
        kind = present[0].kind
        if any(p.kind != kind for p in present):
            return None

        required_length = self.required_length(kind)
        required_depth = self.required_depth(kind)

        open_ended = first is None or second is None
        if first is None:
            start = second.ahead_min - (required_length + OPEN_END_EXTRA_M)
            end = second.ahead_min
        elif second is None:
            start = first.ahead_max
            end = first.ahead_max + (required_length + OPEN_END_EXTRA_M)
        else:
            start = first.ahead_max
            end = second.ahead_min
        length = end - start
        if length < required_length:
            return None

        # The kerb line: the near edge of the neighbours, i.e. how far from us
        # the row of parked cars begins. Measured from the ego *centreline*, so
        # a row that starts inside our own half width is something we are about
        # to hit, not something we can park behind.
        near_lat = min(p.lat_min for p in present)
        if near_lat < self.shape.width * 0.5 or near_lat > self.max_lateral:
            return None

        depth = self._free_depth(near_lat, start, end, all_projections, present)
        if depth < required_depth:
            return None

        # Nothing may be standing inside the gap itself, at any depth we intend
        # to use. ``_free_depth`` already limits the depth to the first such
        # obstacle, so this only has to catch one that starts at the kerb line.
        if self._gap_obstructed(near_lat, start, end, all_projections, present):
            return None

        neighbour_depth = max(p.lat_max for p in present) - near_lat
        return self._build_slot(ego, side, sign, kind, start, end, near_lat,
                                depth, neighbour_depth, present, open_ended)

    def _free_depth(self, near_lat: float, start: float, end: float,
                    all_projections: Sequence[_Projection],
                    boundaries: Sequence[_Projection]) -> float:
        """How far the space extends away from the road before something stops it.

        Free space, not occupied space. The neighbours are deliberately **not**
        a limit: a row of parallel-parked cars is exactly one car wide, and
        taking their width for the depth of the space would reject every kerb
        in the game. What does limit the depth is something standing *inside*
        the gap further out than the kerb line -- the wall at the back of a
        bay, a fence, a bollard.

        With nothing in the way the answer is how far this detector is willing
        to look at all (``MAX_DEPTH_M``). That is a bound on the measurement,
        not a claim about the world, and :meth:`_build_slot` treats it as one.
        """
        limit = self.max_depth
        boundary_keys = {id(p) for p in boundaries}
        for projected in all_projections:
            if id(projected) in boundary_keys:
                continue
            if projected.ahead_max <= start or projected.ahead_min >= end:
                continue
            if projected.lat_max <= near_lat:
                continue
            limit = min(limit, max(0.0, projected.lat_min - near_lat))
        return limit

    def _gap_obstructed(self, near_lat: float, start: float, end: float,
                        all_projections: Sequence[_Projection],
                        boundaries: Sequence[_Projection]) -> bool:
        """Is something standing in the mouth of the gap?

        Anything overlapping the gap on the ``ahead`` axis and reaching in to
        the kerb line or nearer blocks the entrance, whatever its depth --
        including a car that is still moving through it.
        """
        boundary_keys = {id(p) for p in boundaries}
        for projected in all_projections:
            if id(projected) in boundary_keys:
                continue
            if projected.ahead_max <= start or projected.ahead_min >= end:
                continue
            if projected.lat_min <= near_lat + 0.05:
                return True
        return False

    def _build_slot(self, ego: Pose, side: str, sign: float, kind: str,
                    start: float, end: float, near_lat: float, depth: float,
                    neighbour_depth: float, boundaries: Sequence[_Projection],
                    open_ended: bool) -> ParkingSlot:
        """Turn the measured gap into world poses.

        The entry pose sits at the centre of the slot mouth, pointing the way
        the ego is driving. The target pose is where the body centre has to end
        up, and it differs per kind:

        * **parallel** -- in line with the neighbours, so the car's near flank
          matches theirs, and pointing along the road;
        * **perpendicular** -- as deep into the bay as the back clearance
          allows, pointing *out* of it, because the manoeuvre reverses in.
        """
        centre_ahead = (start + end) * 0.5
        mouth_x, mouth_y = ego.to_world(centre_ahead, sign * near_lat)
        entry = Pose(mouth_x, mouth_y, ego.yaw)

        if kind == KIND_PARALLEL:
            target_lat = near_lat + self.shape.width * 0.5 + PARALLEL_KERB_OFFSET_M
            target_yaw = ego.yaw
        else:
            # How deep the tail ends up, decided by whichever of two rules is
            # the more cautious:
            #
            #   aligned  -- level with the neighbours, which is what parking
            #               "in the bay" means, and never less deep than the
            #               car is long (a short neighbour must not leave our
            #               nose sticking out into the road);
            #   blocked  -- the clearance to whatever actually closes the bay.
            #
            # With nothing closing it, ``depth`` is the detector's own look
            # limit and the blocked rule is far away, so alignment decides.
            aligned_tail = near_lat + max(neighbour_depth, self.shape.length)
            blocked_tail = near_lat + depth - PERPENDICULAR_BACK_CLEARANCE_M
            tail_lat = min(aligned_tail, blocked_tail)
            # The nose never starts before the kerb line, however shallow the
            # bay turned out to be.
            target_lat = max(tail_lat - self.shape.length * 0.5,
                             near_lat + self.shape.length * 0.5)
            # Reversing in leaves the nose pointing back **out** of the bay:
            # for a bay on the right the car ends up facing left of the road,
            # and the other way round on the left. Getting this sign wrong
            # plans a nose-in manoeuvre, which is a different (and much
            # tighter) problem than the one the planner solves.
            target_yaw = normalise_angle(ego.yaw - sign * math.pi * 0.5)

        target_x, target_y = ego.to_world(centre_ahead, sign * target_lat)
        return ParkingSlot(
            kind=kind,
            side=side,
            entry=entry,
            target=Pose(target_x, target_y, target_yaw),
            length=end - start,
            depth=depth,
            bounded_by=tuple(p.obstacle.key for p in boundaries),
            open_ended=open_ended,
            ahead_of_ego=centre_ahead,
        )
