"""Turning a parking slot into a drivable path.

The output of this module is a :class:`Trajectory` -- a sequence of constant
curvature segments with a direction of travel, sampled into poses. It says
*where the car has to go*, never *what to do with the steering wheel*: that
belongs to :mod:`assistance.parking.path_follower` and, below it, to
:mod:`Controls.vehicle_control`. Anything else that ever needs "drive this
path" can reuse the pair without knowing parking exists.

### The car model

A kinematic bicycle at the **rear axle**, which is the only point on a car
whose path is a circular arc of the steered radius::

    yaw'  = yaw + kappa * s
    x'    = x + (sin(yaw') - sin(yaw)) / kappa
    y'    = y - (cos(yaw') - cos(yaw)) / kappa

with *s* the signed arc length -- positive forward, negative in reverse -- and
``kappa`` the curvature in 1/m, positive for a left turn. The sign convention
is the one thing to get right: a car reversing with the wheel turned left has a
*positive* curvature and a *negative* arc length, so its yaw decreases. Every
manoeuvre below is built from that.

Tyre slip is ignored, and at the 1-2 m/s a parking manoeuvre runs at that is
not an approximation worth arguing about -- the lateral acceleration on a 6 m
radius at 1.5 m/s is 0.37 m/s^2, about 4 % of what a road tyre can give. The
error that does matter is the *achievable* curvature, which depends on the
steering lock and wheelbase of a car this project cannot look up
(``conventions.md`` §4). That is handled by planning with a conservatively
**large** minimum radius: too large means the manoeuvre is gentler than it
needed to be and asks for more room, never that it asks for a turn the car
cannot make.

### The manoeuvres

**Parallel** -- the classic two arcs, mirrored so that one implementation
serves both sides of the road. Reversing with the wheel turned towards the kerb
swings the tail in; an equal and opposite arc straightens the car out again.
For equal radii the geometry closes in one line: a lateral shift of ``d`` needs
``theta = acos(1 - d / 2R)`` of heading swing and costs ``2R sin(theta)`` of
road length.

**Perpendicular** -- reverse into the bay. A 90 degree reverse arc plus a
straight would need the car to be at least one turning radius out from the
kerb line, which it usually is not. The manoeuvre therefore opens with a
forward arc *away* from the bay through an angle ``phi``: that costs nothing in
lateral room (the reverse arc gives it straight back) and it is what a driver
does by eye. The smallest ``phi`` that makes the straight non-negative is
chosen, so a car that is already far enough out simply gets ``phi = 0``.

### Validation is not optional

A plan is geometry; whether it fits is a question about the world. Every
manoeuvre is swept against the real obstacle boxes with a clearance margin
before it is returned, and a plan that touches anything is rejected with the
obstacle named. That is also why the planner tries several radii: the tightest
arc is not always the one that fits.
"""

import math
from dataclasses import dataclass, field
from typing import List, Optional, Sequence, Tuple

from assistance.parking.geometry import (OrientedBox, Pose, VehicleShape,
                                         boxes_overlap, normalise_angle,
                                         swept_collision)
from assistance.parking.slot_detection import (KIND_PARALLEL,
                                               KIND_PERPENDICULAR,
                                               ParkingSlot, SIDE_LEFT,
                                               required_depth)

# ─── Tuning ───────────────────────────────────────────────────────────────

# Rear-axle turning radius at full lock, in metres. Deliberately on the large
# side: a road car is typically 4.5-5.5 m, and planning with more than the car
# can do produces a gentler manoeuvre that needs more room -- which the
# clearance check then rejects honestly. Planning with *less* would produce a
# path the car cannot follow, and the follower would discover that halfway into
# a parking space. ``conventions.md`` §4: no table keyed on CName.
DEFAULT_MIN_TURN_RADIUS_M = 6.0
# How much wider than the car's lock the tightest *plan* is allowed to be.
#
# A plan at exactly the lock radius is a plan with no steering left to correct
# with: every disturbance that needs a tighter arc is simply unanswerable, and
# the follower spends the rest of the manoeuvre saturated. That is not a
# theoretical worry -- the live runs planned at 6.0 m in a car whose measured
# full-lock curvature was 0.15 1/m, i.e. a 6.7 m radius, so the plan was
# *tighter than the car could drive* and every arc came out short.
#
# 1.15 is the measured compromise: the closed-loop simulation parks within 2
# degrees of square with it against 4 degrees at 1.0, and it is the largest
# margin that still leaves the tight perpendicular space drivable. Wider plans
# need more room, so this is not free.
PLAN_RADIUS_MARGIN = 1.15
# Radii tried, as multiples of the minimum. A tighter arc needs less road
# length; a wider one sweeps less into the neighbours. Trying a few and keeping
# the first that is clear costs a handful of milliseconds, once, at plan time.
RADIUS_FACTORS = (1.0, 1.25, 1.6, 2.0)

# Clearance kept from every obstacle while sweeping the plan. 0.25 m is tight
# enough to park in a real space and wide enough to absorb the follower's own
# tracking error, which is measured in centimetres at these speeds.
CLEARANCE_M = 0.25

# How finely the path is sampled. 0.2 m at a 6 m radius is 1.9 degrees of
# heading per sample -- far finer than the follower's lookahead needs, and fine
# enough that the swept collision check cannot step over a bollard.
SAMPLE_STEP_M = 0.2

# How far the opening forward arc of a perpendicular manoeuvre may swing the
# nose away from the bay. Beyond 60 degrees the car is across the road.
MAX_SWING_RAD = math.radians(60.0)
# The reverse straight at the end of a perpendicular manoeuvre never shrinks
# below this, so the last metre into the bay is straight and correctable.
MIN_BAY_STRAIGHT_M = 0.4

# How far out of line with the road the car may be and still be given a plan.
# The plan is built from an *idealised* start -- the car's real position, the
# road's heading -- so the approach straight is where the follower washes the
# heading error out. A larger error than this is not a parking manoeuvre, it is
# a driver who has not lined up.
MAX_ALIGNMENT_ERROR_RAD = math.radians(12.0)
# How much approach straight one radian of heading error needs to wash out.
# At the follower's 1.5 m lookahead a pure-pursuit controller closes a heading
# error over roughly ten lookahead lengths, so 12 m per radian gives the full
# 12 degrees of tolerance 2.5 m of straight -- and a perfectly aligned car
# needs none at all.
ALIGNMENT_WASHOUT_M_PER_RAD = 12.0
# How far back the approach may reverse when the driver has already rolled
# past the start of the manoeuvre. Both plans use it: a space is spotted while
# driving past, so being beyond it is the normal case rather than the awkward
# one. For the parallel plan the arcs that follow reverse anyway and it costs
# no gear change; the perpendicular one pays one.
#
# 15 m is a bound on how much of the manoeuvre is a plain reverse down the
# road, not a claim that it is free: it is driven at the same crawl as the
# rest, with the rear PDC live and the driver able to stop it at any moment.
MAX_REVERSE_APPROACH_M = 15.0

# ─── The shuffle ──────────────────────────────────────────────────────────
#
# Used when no single manoeuvre fits. Every number here is a bound on work as
# much as it is a bound on behaviour: the shuffle is the expensive half of
# planning and it must stay in the tens of milliseconds.

# How many strokes a manoeuvre may take. Eight is four there-and-back pairs,
# which is more than a driver needs for any space this project would accept,
# and it bounds the planning cost.
MAX_SHUFFLE_STROKES = 8
# How far one stroke may run, and how finely it is integrated. 0.15 m at a 6 m
# radius is 1.4 degrees per step -- far below anything that could step over an
# obstacle, because the car itself is 1.8 m wide.
MAX_STROKE_M = 8.0
SHUFFLE_STEP_M = 0.15
# A stroke shorter than this is not worth a gear change.
MIN_STROKE_M = 0.3
# How far past its best point a stroke keeps looking before giving up on
# finding a better one. Two of the car's own lengths would be a search; half a
# metre is enough to get over the flat spot at the bottom of the cost.
SHUFFLE_BACKOFF_M = 0.5
# How much a stroke has to improve the pose error to be worth taking at all.
# Without it the shuffle happily adds strokes that move the car by a centimetre
# and burn a gear change each.
SHUFFLE_MIN_GAIN = 0.02

# When the backward search has got the car far enough out of the space: back
# on the driver's line across the road, and pointing along it. Both are
# residual errors the follower removes over the joining straight, so they sit
# where a pure-pursuit controller washes them out in a metre or two rather than
# where they would be invisible.
EXIT_LATERAL_TOLERANCE_M = 0.30
EXIT_HEADING_TOLERANCE_RAD = math.radians(6.0)
# Coming out further into the road than the driver's own line is not an error,
# it is the open road; this is how much of it counts as still being the same
# manoeuvre rather than a different one.
EXIT_OVERSHOOT_ALLOWANCE_M = 0.5

# ─── Where the manoeuvre is allowed to happen ─────────────────────────────
#
# Not every direction the car could physically drive in is a direction it may
# be driven in, and nothing in the obstacle list says so. Two boundaries are
# therefore added to every plan, and without them the planner cheerfully
# solved a tight parallel space by driving out through the kerb, round the
# back of the parked cars and in from behind -- collision-free against the two
# boxes it had been given, and completely wrong.
#
# **Beyond the kerb line** is unmeasured world: pavement, wall, grass, a drop.
# Parked cars say how deep the space is and nothing says what is behind them,
# so the manoeuvre may not go deeper than the space itself needs.
#
# **Across the road** is the oncoming lane. Its width is not known either, so
# the allowance is measured from the line the driver was already on: 4 m is
# their own half lane plus most of the one beside it, which is what a parallel
# manoeuvre's nose swing and a perpendicular bay's approach really use, and
# still far short of enough to drive round behind the parked cars. Measured on
# the 12 m parallel scene, a 4.5 m car at its 41 degree maximum angle needs
# 2.2 m of it; 2.5 m of allowance refused that manoeuvre by nine centimetres.
ROAD_ALLOWANCE_M = 4.0
# How much deeper than the parked space itself the manoeuvre may reach. A car
# turning inside a parallel space is angled, and an angled 4.5 m car has a
# corner 1.9 m off its own centreline against 0.9 m when it is straight -- so a
# limit set at exactly the depth the space needs refuses the manoeuvre the
# space exists for (measured: blocked by 5 cm at 30 degrees). One metre of
# allowance covers that and is still far less than the 4.1 m a car would need
# to get round behind the parked row, which is the thing these boundaries are
# here to prevent.
MANOEUVRE_DEPTH_ALLOWANCE_M = 1.0
# How long and thick the two boundary boxes are. Long enough to cover any
# manoeuvre, thick enough that no step can jump through one.
BOUNDARY_LENGTH_M = 80.0
BOUNDARY_THICKNESS_M = 4.0

# A repositioning stroke slides the car along the space; it is not a drive.
# Capped so that a space with nothing at one end cannot turn one into a lap of
# the car park.
MAX_REPOSITION_M = 3.0

# Only obstacles within this of the slot mouth take part in planning. The whole
# manoeuvre happens within a couple of car lengths of it, and the shuffle's
# inner loop runs thousands of times per plan.
OBSTACLE_REACH_M = 20.0

DIRECTION_FORWARD = 1
DIRECTION_REVERSE = -1

# Why a plan was refused. Returned rather than logged: the state machine turns
# these into one line for the driver and one for the log.
REASON_NOT_ALIGNED = 'not_aligned'
REASON_TOO_CLOSE_TO_KERB = 'too_close_to_kerb'
REASON_NO_ROOM = 'no_room'
REASON_BLOCKED = 'blocked'
REASON_UNKNOWN_KIND = 'unknown_kind'


def _straightening_curvature(local_yaw: float, sign: float, radius: float,
                             direction: int) -> float:
    """World curvature that drives *local_yaw* to zero in *direction*.

    The mirror is the whole point of this function existing. A slot on the
    right is the world reflected, so a local heading of ``psi`` is a world
    heading of ``sign * psi`` and a local curvature is ``sign`` times the world
    one. Working out the sign inline, twice, is how the first version of the
    shuffle came to straighten the car the wrong way -- it turned a car angled
    11 degrees one way into one angled 11 degrees the other, made no progress,
    and declared the space unparkable.
    """
    return -sign * math.copysign(1.0 / radius, local_yaw) * direction


def _merge_adjacent(segments: List['Segment']) -> List['Segment']:
    """Join consecutive segments that are the same arc in the same direction.

    The shuffle can produce two touching strokes that differ only because a
    search step ended between them. Left alone they would tell the follower to
    stop and set off again for no reason, and :attr:`Trajectory.direction_changes`
    would over-count the gear changes the manoeuvre really needs.
    """
    merged: List['Segment'] = []
    for segment in segments:
        if merged:
            previous = merged[-1]
            if (previous.direction == segment.direction
                    and abs(previous.curvature - segment.curvature) < 1e-9):
                merged[-1] = Segment(previous.curvature,
                                     previous.length + segment.length,
                                     previous.direction)
                continue
        merged.append(segment)
    return merged


def _exit_cost(local: Pose, target_lat: float, target_x: float) -> float:
    """How far this pose is from being back out on the road, in one number.

    The backward search's objective. Squared errors in the slot frame,
    weighted by what each one costs the manoeuvre rather than by any physical
    constant:

    * **lateral** (weight 1) -- how far the car still has to travel sideways,
      which is the expensive direction for a car and the thing the shuffle
      exists to buy;
    * **heading** (weight 4 per rad^2) -- so 0.5 rad, about 29 degrees, weighs
      the same as a metre sideways;
    * **longitudinal** (weight 0.02) -- nearly free, because the road is a line
      the driver can drive along. It is in the cost only to break ties towards
      the end the driver is at, and it has to stay small: give it real weight
      and the search spends its strokes driving up and down the road instead of
      getting out of the space.
    """
    lateral = local.y - target_lat
    longitudinal = local.x - target_x
    return (lateral * lateral + 4.0 * local.yaw * local.yaw
            + 0.02 * longitudinal * longitudinal)


@dataclass(frozen=True)
class Segment:
    """A stretch of constant curvature driven in one direction.

    ``length`` is unsigned arc length in metres; ``direction`` carries the
    sign. Keeping them apart is what lets the follower ask "how far is left in
    this segment" without ever taking an absolute value of something that might
    legitimately be negative.
    """
    curvature: float
    length: float
    direction: int


@dataclass(frozen=True)
class PathPoint:
    """One sampled pose along the path, with what to do when passing it."""
    pose: Pose
    s: float                 # distance along the whole path, metres
    curvature: float
    direction: int
    segment_index: int


@dataclass
class Trajectory:
    """A complete manoeuvre: what to drive, and where it puts the car."""
    kind: str
    segments: List[Segment]
    points: List[PathPoint] = field(default_factory=list)
    start: Optional[Pose] = None
    goal: Optional[Pose] = None
    # The radius the plan was built with, for the log line that explains why a
    # manoeuvre needed the room it needed.
    radius: float = 0.0

    @property
    def length(self) -> float:
        return sum(segment.length for segment in self.segments)

    @property
    def direction_changes(self) -> int:
        """How many times the car has to stop and select the other gear."""
        changes = 0
        for previous, current in zip(self.segments, self.segments[1:]):
            if previous.direction != current.direction:
                changes += 1
        return changes

    def segment_bounds(self) -> List[Tuple[float, float]]:
        """``(start_s, end_s)`` of every segment, in path distance."""
        bounds = []
        travelled = 0.0
        for segment in self.segments:
            bounds.append((travelled, travelled + segment.length))
            travelled += segment.length
        return bounds


# ─── The car model ────────────────────────────────────────────────────────

def advance(pose: Pose, curvature: float, arc_length: float) -> Pose:
    """One kinematic bicycle step. *arc_length* is signed; see the module docs."""
    if abs(curvature) < 1e-9:
        return Pose(pose.x + arc_length * math.cos(pose.yaw),
                    pose.y + arc_length * math.sin(pose.yaw),
                    pose.yaw)
    yaw_after = pose.yaw + curvature * arc_length
    return Pose(pose.x + (math.sin(yaw_after) - math.sin(pose.yaw)) / curvature,
                pose.y - (math.cos(yaw_after) - math.cos(pose.yaw)) / curvature,
                normalise_angle(yaw_after))


def sample(start: Pose, segments: Sequence[Segment],
           step: float = SAMPLE_STEP_M) -> List[PathPoint]:
    """Walk the segments from *start*, one :class:`PathPoint` every *step*.

    The first point is *start* itself and the last is the exact end of the last
    segment, so a follower can always ask for both ends without interpolating
    past them.
    """
    points = [PathPoint(start, 0.0,
                        segments[0].curvature if segments else 0.0,
                        segments[0].direction if segments else DIRECTION_FORWARD,
                        0)]
    pose = start
    travelled = 0.0
    for index, segment in enumerate(segments):
        remaining = segment.length
        while remaining > 1e-9:
            piece = min(step, remaining)
            pose = advance(pose, segment.curvature, piece * segment.direction)
            remaining -= piece
            travelled += piece
            points.append(PathPoint(pose, travelled, segment.curvature,
                                    segment.direction, index))
    return points


# ─── Planning ─────────────────────────────────────────────────────────────

@dataclass(frozen=True)
class PlanResult:
    """Either a trajectory or the reason there is none."""
    trajectory: Optional[Trajectory] = None
    reason: Optional[str] = None
    # Which obstacle blocked it, when the reason is ``REASON_BLOCKED``.
    blocked_by: Optional[object] = None

    @property
    def ok(self) -> bool:
        return self.trajectory is not None


def planning_radius_for(lock_radius: float) -> float:
    """The radius to plan at, given what the car can do at full lock.

    One function so that the margin lives in one place: the setting and the
    measurement both describe the *car*, the planner wants the *plan*, and
    confusing the two is what put a live run on a path it could not steer.
    """
    return max(0.5, float(lock_radius)) * PLAN_RADIUS_MARGIN


class ParkingPlanner:
    """Builds the manoeuvre for a slot the detector has already accepted.

    ``min_turn_radius`` is the radius of the tightest arc this planner will
    *plan*, not the car's steering lock. Callers that start from the car go
    through :func:`planning_radius_for`.
    """

    def __init__(self, shape: VehicleShape,
                 min_turn_radius: float = DEFAULT_MIN_TURN_RADIUS_M,
                 clearance: float = CLEARANCE_M,
                 sample_step: float = SAMPLE_STEP_M):
        self.shape = shape
        self.min_turn_radius = min_turn_radius
        self.clearance = clearance
        self.sample_step = sample_step

    def plan(self, ego: Pose, slot: ParkingSlot,
             obstacles: Sequence[OrientedBox]) -> PlanResult:
        """A validated manoeuvre from *ego* into *slot*, or why there is none.

        Two attempts, in this order:

        1. **The closed form.** One construction per radius in
           :data:`RADIUS_FACTORS`, swept for clearance. When the space is
           roomy this produces the clean, minimal manoeuvre a driver would
           recognise, and it costs about two milliseconds.
        2. **The shuffle.** A slot of the length this project's detector
           accepts -- roughly 1.3 times the car -- is *geometrically out of
           reach* of any single reverse manoeuvre. Measured on a 4.5 m car
           with a 3 m lateral shift: the two-arc path needs about 9.8 m of
           kerb, and the classical bound ``L + sqrt(R^2 - (R - d)^2)`` agrees
           at 9.1 m. Real drivers get into 6 m spaces by shuffling, and so
           does :meth:`_shuffle`.

        Cost: the closed form is negligible; the shuffle is tens of
        milliseconds. Neither may run on every assistance cycle -- the caller
        rate-limits planning and only re-plans when the slot changes.
        """
        sign = 1.0 if slot.side == SIDE_LEFT else -1.0
        local_ego = self._to_local(slot, sign, ego)
        if abs(local_ego.yaw) > MAX_ALIGNMENT_ERROR_RAD:
            return PlanResult(reason=REASON_NOT_ALIGNED)
        if slot.kind not in (KIND_PARALLEL, KIND_PERPENDICULAR):
            return PlanResult(reason=REASON_UNKNOWN_KIND)

        local_goal = self._to_local(slot, sign, slot.target)
        nearby = self._relevant(obstacles, slot)
        nearby.extend(self._boundaries(slot, sign, local_ego))

        result = self._plan_closed_form(slot, sign, local_ego, local_goal, nearby)
        if result.ok:
            return result

        shuffled = self._shuffle_any_radius(slot, sign, local_ego, local_goal,
                                            nearby)
        return shuffled if shuffled.ok else result

    def _relevant(self, obstacles: Sequence[OrientedBox],
                  slot: ParkingSlot) -> List[OrientedBox]:
        """Obstacles close enough to the slot to matter to the manoeuvre.

        The whole manoeuvre happens within a couple of car lengths of the slot
        mouth, so everything else is dead weight in an inner loop that runs
        thousands of times per plan. Typically this leaves two to four boxes.
        """
        reach = OBSTACLE_REACH_M
        reach_sq = reach * reach
        kept = []
        for box in obstacles:
            dx = box.x - slot.entry.x
            dy = box.y - slot.entry.y
            if dx * dx + dy * dy <= reach_sq:
                kept.append(box)
        return kept

    def _boundaries(self, slot: ParkingSlot, sign: float,
                    local_ego: Pose) -> List[OrientedBox]:
        """The two walls the manoeuvre may not cross. See ROAD_ALLOWANCE_M.

        Both are expressed in the slot frame and converted once: local ``y``
        zero is the kerb line, positive is into the space.
        """
        depth = min(slot.depth,
                    required_depth(self.shape, slot.kind)
                    + MANOEUVRE_DEPTH_ALLOWANCE_M)
        half = BOUNDARY_THICKNESS_M * 0.5
        boxes = []
        for local_y in (depth + half, local_ego.y - ROAD_ALLOWANCE_M - half):
            x, y = slot.entry.to_world(0.0, sign * local_y)
            boxes.append(OrientedBox(x, y, slot.entry.yaw,
                                     BOUNDARY_LENGTH_M, BOUNDARY_THICKNESS_M))
        return boxes

    # ─── Attempt 1: the closed form ───────────────────────────────────

    def _plan_closed_form(self, slot: ParkingSlot, sign: float, local_ego: Pose,
                          local_goal: Pose,
                          obstacles: Sequence[OrientedBox]) -> PlanResult:
        last_reason = REASON_NO_ROOM
        blocked_by = None
        for factor in RADIUS_FACTORS:
            radius = self.min_turn_radius * factor
            if slot.kind == KIND_PARALLEL:
                built = self._parallel_segments(local_ego, local_goal, radius)
            else:
                built = self._perpendicular_segments(local_ego, local_goal, radius)
            if built is None:
                continue
            local_segments, local_start = built
            segments = [Segment(sign * s.curvature, s.length, s.direction)
                        for s in local_segments]
            start = self._to_world(slot, sign, local_start)
            points = sample(start, segments, self.sample_step)

            hit = swept_collision(self.shape, (p.pose for p in points),
                                  obstacles, self.clearance)
            if hit is not None:
                last_reason = REASON_BLOCKED
                blocked_by = obstacles[hit]
                continue

            return PlanResult(Trajectory(
                kind=slot.kind, segments=segments, points=points,
                start=start, goal=self._to_world(slot, sign, local_goal),
                radius=radius))

        return PlanResult(reason=last_reason, blocked_by=blocked_by)

    # ─── Attempt 2: the shuffle, planned backwards ────────────────

    def _shuffle_any_radius(self, slot: ParkingSlot, sign: float,
                            local_ego: Pose, local_goal: Pose,
                            obstacles: Sequence[OrientedBox]) -> PlanResult:
        """The shuffle, over the same radii the closed form sweeps.

        The shuffle is a greedy search and a greedy search dead-ends: it takes
        the best stroke available now, and in a tight pocket the phase it
        happens to be in decides whether the next one exists at all. Swept
        over gap length that shows up as isolated failures -- a space that is
        *longer* than one the planner accepted, and rejected. Measured on a
        clean two-car scene: 8.15-8.25 m and 8.80-8.90 m failed while
        everything either side worked.

        Those holes are not geometry, they are phase, and a different turning
        radius puts the search in a different one. Every hole in that sweep
        closes at some radius in :data:`RADIUS_FACTORS`.

        Cost: the base radius is tried first and is the one that normally
        succeeds, so the usual case pays nothing extra. A failure costs one
        more shuffle per radius -- about 5 ms each, worst case ~20 ms -- and
        planning is rate-limited to once every couple of seconds and never
        happens on a cycle that is also driving the car (``AGENTS.md`` §1).
        """
        base = self.min_turn_radius
        last = PlanResult(reason=REASON_NO_ROOM)
        try:
            for factor in RADIUS_FACTORS:
                self.min_turn_radius = base * factor
                for first in (DIRECTION_REVERSE, DIRECTION_FORWARD):
                    # The boundaries were built for the caller's radius, but
                    # they only depend on the slot and the ego, so they hold.
                    last = self._plan_shuffle(slot, sign, local_ego,
                                              local_goal, obstacles, first)
                    if last.ok:
                        return last
        finally:
            self.min_turn_radius = base
        return last

    def _plan_shuffle(self, slot: ParkingSlot, sign: float, local_ego: Pose,
                      local_goal: Pose, obstacles: Sequence[OrientedBox],
                      first_direction: int = DIRECTION_REVERSE) -> PlanResult:
        """Shuffle the car **out** of the space, then drive that backwards.

        Two things had to be got right here, and both were got wrong first.

        **Search backwards, not forwards.** The parked pose is a point in a
        pocket: almost every move towards it from the road also moves the car
        *along* the road, so a search that starts at the car drives straight
        back until it is level with the space and then has nothing left to do
        but translate sideways, which a car cannot do. Measured on the 8 m
        parallel scene: stuck after three strokes, square beside the space.
        Starting from the parked pose and driving *out* turns the pocket into a
        gradient, and leaves the longitudinal position free -- the road is a
        line, and the driver can drive along it.

        **Judge a pose by where it will end up, not where it is.** A car
        leaving a tight space has to angle itself *away* from straight before
        it can come out, so any cost with a heading term in it rejects the one
        move that makes the manoeuvre possible. The cost here is therefore
        measured **after** an imaginary straightening arc
        (:meth:`_project_straight`): the heading is not an error to be
        punished, it is lateral room the car has already earned and has not
        spent yet.

        That projection has a consequence worth naming, because it is what
        makes the shuffle work rather than a flaw in it: driving along the
        straightening arc itself changes nothing in the cost -- the arc gives
        back exactly what it takes. Those strokes are the *repositioning* half
        of a shuffle. They buy no lateral progress and are taken anyway, to the
        limit of the space, because they move the car along the slot so that
        the next progress stroke has somewhere to go.

        **Collision-free by construction**: every stroke is integrated in
        :data:`SHUFFLE_STEP_M` steps with the full clearance margin and is cut
        at the last step that was checked, and the straight that joins the
        manoeuvre to the car is swept before the plan is returned.
        """
        goal = self._to_world(slot, sign, local_goal)
        radius = self.min_turn_radius
        curvatures = (1.0 / radius, 0.0, -1.0 / radius)
        target_lat, target_x = local_ego.y, local_ego.x

        def cost_of(pose: Pose) -> float:
            projected = self._project_straight(self._to_local(slot, sign, pose),
                                               radius)
            return _exit_cost(projected, target_lat, target_x)

        pose = goal
        strokes: List[Segment] = []
        best_cost = cost_of(pose)
        last_direction = first_direction
        repositioned = False

        for _ in range(MAX_SHUFFLE_STROKES):
            finish = self._finishing_stroke(slot, sign, pose, target_lat,
                                            radius, obstacles)
            if finish is not None:
                segment, end_pose = finish
                if segment is not None:
                    strokes.append(segment)
                return self._assemble(slot, sign, strokes, end_pose, goal,
                                      local_ego, obstacles, radius)

            chosen = None
            for direction in (DIRECTION_FORWARD, DIRECTION_REVERSE):
                for curvature in curvatures:
                    stroke = self._drive_stroke(pose, curvature, direction,
                                                cost_of, obstacles)
                    if stroke is None:
                        continue
                    if chosen is None or stroke[2] < chosen[2]:
                        chosen = stroke + (curvature, direction)

            if chosen is not None and chosen[2] < best_cost - SHUFFLE_MIN_GAIN:
                length, pose, best_cost, curvature, last_direction = chosen
                strokes.append(Segment(curvature, length, last_direction))
                repositioned = False
                continue

            if repositioned:
                break                       # two in a row is not a shuffle
            reposition = self._reposition_stroke(slot, sign, pose, radius,
                                                 -last_direction, obstacles)
            if reposition is None:
                break
            segment, pose = reposition
            strokes.append(segment)
            last_direction = segment.direction
            best_cost = cost_of(pose)
            repositioned = True

        return PlanResult(reason=REASON_NO_ROOM)

    # ─── Pieces of the shuffle ────────────────────────────────────────

    @staticmethod
    def _project_straight(local: Pose, radius: float) -> Pose:
        """Where this pose would be after straightening onto the road.

        A closed-form forward arc that takes the heading to zero. It is never
        driven and never collision-checked -- it exists so that the cost can
        credit the car for the heading it has built up, which is lateral room
        it has earned. :meth:`_finishing_stroke` is the one that has to be
        real.
        """
        if abs(local.yaw) < 1e-6:
            return local
        curvature = -math.copysign(1.0 / radius, local.yaw)
        return advance(local, curvature, radius * abs(local.yaw))

    def _finishing_stroke(self, slot: ParkingSlot, sign: float, pose: Pose,
                          target_lat: float, radius: float,
                          obstacles: Sequence[OrientedBox]):
        """The stroke that puts the car back on the road, if one fits.

        Returns ``(segment_or_None, end_pose)``, with ``None`` for the segment
        when the car is already there and nothing has to be driven. Both
        directions of travel are tried: leaving a space nose-first and
        tail-first are both legitimate, and which one fits is a question about
        the obstacles, not about the manoeuvre.
        """
        local = self._to_local(slot, sign, pose)
        if abs(local.yaw) < 1e-6:
            return (None, pose) if self._is_on_the_road(local, target_lat) else None

        length = radius * abs(local.yaw)
        for direction in (DIRECTION_FORWARD, DIRECTION_REVERSE):
            curvature = _straightening_curvature(local.yaw, sign,
                                                 radius, direction)
            end = self._drive_exactly(pose, curvature, direction, length,
                                      obstacles)
            if end is None:
                continue
            if self._is_on_the_road(self._to_local(slot, sign, end), target_lat):
                return Segment(curvature, length, direction), end
        return None

    def _reposition_stroke(self, slot: ParkingSlot, sign: float, pose: Pose,
                           radius: float, direction: int,
                           obstacles: Sequence[OrientedBox]):
        """The other half of a shuffle: move along the space, keep the angle.

        Driven on the straightening arc, which the cost is blind to, so this
        stroke neither gains nor loses anything laterally. It is run to the
        limit of the space on purpose -- its whole job is to give the next
        progress stroke somewhere to go.
        """
        local = self._to_local(slot, sign, pose)
        if abs(local.yaw) < 1e-6:
            return None
        # The *same arc* the straightening stroke would drive, travelled the
        # other way. Same curvature, opposite direction -- that is what makes
        # it neutral in the projected cost.
        curvature = _straightening_curvature(local.yaw, sign, radius,
                                             DIRECTION_FORWARD)
        length, end = self._drive_to_limit(pose, curvature, direction,
                                           obstacles, MAX_REPOSITION_M)
        if length < MIN_STROKE_M:
            return None
        return Segment(curvature, length, direction), end

    @staticmethod
    def _is_on_the_road(local: Pose, target_lat: float) -> bool:
        """Is the car back out where the driver is, and pointing along the road?

        The lateral test is deliberately one-sided. Coming out *further* than
        the driver's own line is harmless -- it is the open road -- and only
        costs the follower a little more to correct over the joining straight;
        stopping short of it means the car is still in among the parked ones.
        """
        if abs(local.yaw) > EXIT_HEADING_TOLERANCE_RAD:
            return False
        return (target_lat - EXIT_OVERSHOOT_ALLOWANCE_M
                <= local.y <= target_lat + EXIT_LATERAL_TOLERANCE_M)

    def _assemble(self, slot: ParkingSlot, sign: float, strokes: List[Segment],
                  exit_pose: Pose, goal: Pose, local_ego: Pose,
                  obstacles: Sequence[OrientedBox], radius: float) -> PlanResult:
        """Turn the way out into the way in, and join it to the car.

        The strokes were driven from the parked pose outwards, so the manoeuvre
        is that list read backwards with every direction of travel flipped.
        What is left is the gap between where the driver is and where that
        manoeuvre begins, which is a straight along the road -- forwards if the
        car has not reached it yet, backwards if it has rolled past.
        """
        segments = [Segment(stroke.curvature, stroke.length, -stroke.direction)
                    for stroke in reversed(strokes)]
        segments = _merge_adjacent(segments)
        local_exit = self._to_local(slot, sign, exit_pose)
        approach = local_exit.x - local_ego.x
        start = exit_pose
        if abs(approach) > 1e-3:
            direction = DIRECTION_FORWARD if approach > 0 else DIRECTION_REVERSE
            start = advance(exit_pose, 0.0, -abs(approach) * direction)
            segments.insert(0, Segment(0.0, abs(approach), direction))
            segments = _merge_adjacent(segments)

        points = sample(start, segments, self.sample_step)
        hit = swept_collision(self.shape, (point.pose for point in points),
                              obstacles, self.clearance)
        if hit is not None:
            return PlanResult(reason=REASON_BLOCKED, blocked_by=obstacles[hit])
        return PlanResult(Trajectory(
            kind=slot.kind, segments=segments, points=points,
            start=start, goal=goal, radius=radius))

    # ─── Integration with collision checking ──────────────────────────

    def _blocked(self, pose: Pose, obstacles: Sequence[OrientedBox]) -> bool:
        body = self.shape.footprint(pose, self.clearance)
        return any(boxes_overlap(body, obstacle) for obstacle in obstacles)

    def _drive_stroke(self, pose: Pose, curvature: float, direction: int,
                      cost_of, obstacles: Sequence[OrientedBox]):
        """Drive one arc as far as it keeps helping. ``None`` if it never does.

        Returns ``(length, end_pose, cost)`` for the **best** point reached,
        which is not always the last one: a stroke that drives past its own
        optimum is cut back to where it was best rather than run to a wall.
        """
        travelled = 0.0
        current = pose
        best = None
        while travelled < MAX_STROKE_M:
            nxt = advance(current, curvature, SHUFFLE_STEP_M * direction)
            if self._blocked(nxt, obstacles):
                break
            travelled += SHUFFLE_STEP_M
            current = nxt
            cost = cost_of(current)
            if best is None or cost < best[2]:
                best = (travelled, current, cost)
            elif travelled - best[0] > SHUFFLE_BACKOFF_M:
                break                       # past the best point, stop looking
        if best is None or best[0] < MIN_STROKE_M:
            return None
        return best

    def _drive_to_limit(self, pose: Pose, curvature: float, direction: int,
                        obstacles: Sequence[OrientedBox],
                        limit: float = MAX_STROKE_M):
        """Drive until something is in the way. Returns ``(length, end_pose)``."""
        travelled = 0.0
        current = pose
        while travelled < limit:
            nxt = advance(current, curvature, SHUFFLE_STEP_M * direction)
            if self._blocked(nxt, obstacles):
                break
            travelled += SHUFFLE_STEP_M
            current = nxt
        return travelled, current

    def _drive_exactly(self, pose: Pose, curvature: float, direction: int,
                       length: float, obstacles: Sequence[OrientedBox]):
        """Drive the whole length or nothing. ``None`` if anything is in the way."""
        travelled = 0.0
        current = pose
        while travelled < length - 1e-9:
            step = min(SHUFFLE_STEP_M, length - travelled)
            nxt = advance(current, curvature, step * direction)
            if self._blocked(nxt, obstacles):
                return None
            travelled += step
            current = nxt
        return current

    # ─── Slot-local frame ─────────────────────────────────────────────
    #
    # Origin at the slot mouth, +x along the direction the ego was driving,
    # +y **into** the slot. For a slot on the right that is a mirror of the
    # world, which flips yaw and curvature -- and is exactly why the two sides
    # need only one implementation.

    @staticmethod
    def _to_local(slot: ParkingSlot, sign: float, pose: Pose) -> Pose:
        ahead, left = slot.entry.to_local(pose.x, pose.y)
        return Pose(ahead, sign * left,
                    normalise_angle(sign * (pose.yaw - slot.entry.yaw)))

    @staticmethod
    def _to_world(slot: ParkingSlot, sign: float, pose: Pose) -> Pose:
        x, y = slot.entry.to_world(pose.x, sign * pose.y)
        return Pose(x, y, normalise_angle(slot.entry.yaw + sign * pose.yaw))

    # ─── Parallel ─────────────────────────────────────────────────────

    def _parallel_segments(self, ego: Pose, goal: Pose,
                           radius: float) -> Optional[Tuple[List[Segment], Pose]]:
        """Approach straight, then two equal and opposite reverse arcs.

        ``ego.y`` is where the car is now across the road and ``goal.y`` where
        it has to end up; the difference is the lateral shift the two arcs have
        to produce. A car already level with the target has nothing to shift
        and is refused rather than given a degenerate plan.
        """
        shift = goal.y - ego.y
        if shift <= 0.05:
            return None
        cosine = 1.0 - shift / (2.0 * radius)
        if cosine < -1.0:
            return None                     # more shift than two arcs can give
        theta = math.acos(max(-1.0, min(1.0, cosine)))
        road_length = 2.0 * radius * math.sin(theta)

        approach = goal.x + road_length - ego.x
        if not self._approach_is_usable(approach, ego.yaw, allow_reverse=True):
            return None

        segments: List[Segment] = []
        if approach > 1e-6:
            segments.append(Segment(0.0, approach, DIRECTION_FORWARD))
        elif approach < -1e-6:
            # Rolled past the start. Reversing back to it costs no gear change,
            # because everything after it reverses too.
            segments.append(Segment(0.0, -approach, DIRECTION_REVERSE))
        # Reversing with the wheel turned towards the slot swings the tail in;
        # the mirrored frame means "towards the slot" is always +1/R here.
        segments.append(Segment(1.0 / radius, radius * theta, DIRECTION_REVERSE))
        segments.append(Segment(-1.0 / radius, radius * theta, DIRECTION_REVERSE))
        return segments, Pose(ego.x, ego.y, 0.0)

    # ─── Perpendicular ────────────────────────────────────────────────

    def _perpendicular_segments(self, ego: Pose, goal: Pose,
                                radius: float) -> Optional[Tuple[List[Segment], Pose]]:
        """Swing the nose away, reverse through the corner, straighten in.

        With the opening swing ``phi`` the closed form is

            x_end = x0 + R (2 sin(phi) - 1)
            y_end = y0 + R (2 cos(phi) - 1) + d

        so the straight ``d`` shrinks as ``phi`` grows. ``phi = 0`` -- a plain
        90 degree reverse arc -- needs the car to be a full turning radius out
        from the kerb line, which it rarely is; the smallest ``phi`` that
        leaves ``d`` at :data:`MIN_BAY_STRAIGHT_M` is used instead.
        """
        available = goal.y - ego.y - MIN_BAY_STRAIGHT_M
        if available <= 0.0:
            return None
        cosine = (1.0 + available / radius) * 0.5
        if cosine >= 1.0:
            phi = 0.0
        else:
            phi = math.acos(max(-1.0, min(1.0, cosine)))
            if phi > MAX_SWING_RAD:
                return None
        straight = goal.y - ego.y - radius * (2.0 * math.cos(phi) - 1.0)
        if straight < 0.0:
            return None

        # A reverse approach is allowed here too, and it is the usual case: a
        # bay is spotted while driving past it, so the car is already beyond
        # the point the swing starts from. It costs one extra gear change --
        # reverse back, swing forward, reverse in -- which is exactly what a
        # driver does.
        approach = goal.x - radius * (2.0 * math.sin(phi) - 1.0) - ego.x
        if not self._approach_is_usable(approach, ego.yaw, allow_reverse=True):
            return None

        segments: List[Segment] = []
        if approach > 1e-6:
            segments.append(Segment(0.0, approach, DIRECTION_FORWARD))
        elif approach < -1e-6:
            segments.append(Segment(0.0, -approach, DIRECTION_REVERSE))
        if phi > 1e-6:
            # Forward, steering away from the slot: in the mirrored frame that
            # is a negative curvature, and it is what buys the room for the
            # corner without needing any.
            segments.append(Segment(-1.0 / radius, radius * phi, DIRECTION_FORWARD))
        segments.append(Segment(1.0 / radius, radius * (math.pi * 0.5 - phi),
                                DIRECTION_REVERSE))
        if straight > 1e-6:
            segments.append(Segment(0.0, straight, DIRECTION_REVERSE))
        return segments, Pose(ego.x, ego.y, 0.0)

    # ─── Shared ───────────────────────────────────────────────────────

    @staticmethod
    def _approach_is_usable(approach: float, yaw_error: float,
                            allow_reverse: bool) -> bool:
        """Is there enough straight to start the manoeuvre from?

        Two separate questions in one test. The car must not already be past
        the point the arcs start from (unless reversing back to it is free),
        and there must be enough straight left for the follower to pull the
        heading onto the path before the first arc -- because the plan is built
        as if the car were already square with the road.
        """
        if approach < 0.0 and not allow_reverse:
            return False
        if approach < -MAX_REVERSE_APPROACH_M:
            return False
        needed = abs(yaw_error) * ALIGNMENT_WASHOUT_M_PER_RAD
        return abs(approach) >= needed
