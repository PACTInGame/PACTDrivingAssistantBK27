"""Following a planned path: pose in, control demand out.

This is the seam the whole feature is built around. Above it, everything is
geometry and knows nothing about LFS; below it, everything is input devices and
knows nothing about parking. What crosses it is a :class:`ControlDemand` --
**how fast, which way round, how tightly** -- and nothing else.

That is deliberate and it is what makes the pair reusable. A cruise control, a
lane keeper or an automated test driver needs exactly the same three numbers;
none of them needs to know that the path came from a parking slot. Whatever
actuates the car reads a ``ControlDemand`` and never sees a ``Trajectory``.

### Lateral: the path's own arc, corrected by pure pursuit

The steering demand is a **curvature**, not a steering angle, and that is the
important choice. A steering angle would have to be converted through a
wheelbase and a steering lock that this project cannot look up for a vehicle
mod (``conventions.md`` §4), and the conversion would be wrong for every modded
car. Curvature is measurable from data LFS already sends -- ``yaw_rate / v``
from ``CompCar`` -- so the loop that turns a demanded curvature into steering
can *learn* the car it is driving instead of assuming one.

The demand is the **curvature of the path under the car**, plus a pure-pursuit
correction for being off it. Feeding the path's own arc forward matters most
exactly where this feature lives: a parking shuffle's last strokes are under a
metre long, and pure pursuit alone has to *infer* their arc from a lookahead
point that is barely further away than the car is long. Measured on the
shortest space the detector offers, pure pursuit alone parked the car 12.6
degrees crooked; with the feedforward the same manoeuvre ends square.

Pure pursuit then picks a point on the path a lookahead ahead of the car and
asks for the arc that reaches it::

    kappa = 2 * sin(alpha) / lookahead

with ``alpha`` the angle to that point in the car's frame. Reversing flips the
frame: the car chases a point behind itself, and the arc that takes it there is
the mirror image, which the code handles by projecting into the *travel*
direction rather than the heading. Get that wrong and the steering fights the
manoeuvre exactly when it matters.

The lookahead is short -- :data:`MIN_LOOKAHEAD_M` -- because a parking path
turns inside its own length. Long lookaheads cut corners, and the corner being
cut is usually a bumper.

### Longitudinal: stop where the path says to stop

The speed demand is the smallest of three limits: a crawl, what the remaining
distance in the current segment allows at a comfortable deceleration, and zero
while a gear change is pending. The middle one is plain kinematics,
``v = sqrt(2 a s)``, with the deceleration stated rather than tuned: 0.8 m/s^2
is gentle enough to be smooth at 1 m/s and nowhere near any grip limit
(``conventions.md`` §7).

### The follower does not decide to stop the manoeuvre

It reports ``finished`` when the path is driven and ``off_track`` when the car
is further from the path than it should ever be. What to do about either is the
state machine's call, because that is where the driver, the screen and the
abort conditions live.
"""

import math
from dataclasses import dataclass
from typing import List, Optional, Tuple

from assistance.parking.geometry import Pose, normalise_angle
from assistance.parking.trajectory import (DIRECTION_FORWARD, DIRECTION_REVERSE,
                                           Trajectory)

# ─── Lateral ──────────────────────────────────────────────────────────────

# Pure-pursuit lookahead. A parking manoeuvre's arcs have a 6 m radius and run
# for two or three metres, so the lookahead has to be short or the controller
# cuts across them. It grows a little with speed so that the crawl down a long
# straight is not twitchy.
MIN_LOOKAHEAD_M = 1.2
LOOKAHEAD_PER_MPS = 0.6
MAX_LOOKAHEAD_M = 3.0
# Never look further than the stroke being driven, and never shorter than this.
# The shuffle into a tight space is made of strokes under a metre long, where a
# fixed 1.2 m lookahead means aiming permanently at the end of the stroke and
# ignoring the arc in between.
ABSOLUTE_MIN_LOOKAHEAD_M = 0.4

# Pure pursuit tracks a *position*; on a stroke shorter than its own lookahead
# it will happily arrive there pointing the wrong way, and in a five-stroke
# shuffle those errors add up -- measured at 9.5 degrees of final heading error
# in a 6.5 m space before this term existed. This is a proportional correction
# on the heading error against the path, in 1/m per radian: 0.6 turns 10
# degrees of error into a 10 m radius correction, gentle next to the 6 m radius
# the manoeuvre itself uses.
HEADING_GAIN = 0.6

# How far off the path the car may be before the follower says so. Well beyond
# anything it produces at these speeds, and inside the clearance the plan was
# validated with, so "off track" means something really went wrong -- a kerb, a
# collision, the driver grabbing the wheel.
MAX_CROSS_TRACK_M = 0.9
# And how far out of line. A parking path never asks for a heading the car
# cannot hold, so this is the same kind of alarm.
MAX_HEADING_ERROR_RAD = math.radians(35.0)

# ─── Longitudinal ─────────────────────────────────────────────────────────

# The speed a parking manoeuvre runs at. Walking pace: fast enough not to be
# tedious, slow enough that a metre of stopping distance is generous and that
# the kinematic model's neglect of tyre slip is beyond argument.
CRUISE_MPS = 1.1
# And on the long straight part of an approach, where there is nothing to hit.
APPROACH_MPS = 2.2
# The straight is only "long" past this; below it the crawl applies.
APPROACH_LENGTH_M = 6.0

# Deceleration used to plan the stop at the end of each segment. Gentle by
# design -- this is a comfort limit, not a grip limit (``conventions.md`` §7).
DECELERATION_MPS2 = 0.8
# Below this the car counts as stopped, which is what a gear change waits for.
# 0.08 m/s is 0.3 km/h -- the same standstill the emergency brake and auto-hold
# use (``control-intervention.md`` §2.2), and low enough that LFS really will
# take reverse.
STANDSTILL_MPS = 0.08

# How close to the end of a segment counts as having driven it. Smaller than
# the sampling step, so the follower cannot skip past a direction change.
SEGMENT_TOLERANCE_M = 0.10
# And how close to the end of the whole path counts as parked.
FINISH_TOLERANCE_M = 0.20


@dataclass(frozen=True)
class ControlDemand:
    """What the car is being asked to do, independent of how it is driven.

    ``speed`` is a **magnitude** in m/s and ``direction`` carries the sign, for
    the same reason :class:`~assistance.parking.trajectory.Segment` does: a
    controller that has to take the absolute value of a speed has already lost
    track of which way the car is pointing.

    ``curvature`` is in 1/m, positive for a left turn -- the sign yaw rate has,
    so that a measured ``yaw_rate / speed`` can be compared with it directly.
    """
    speed: float
    direction: int
    curvature: float
    finished: bool = False
    off_track: bool = False
    # Where the manoeuvre has got to, for the screen: metres driven, metres
    # total, and which stroke of how many.
    travelled: float = 0.0
    total: float = 0.0
    stroke: int = 0
    strokes: int = 0
    # Diagnostics. Published rather than logged per cycle -- the state machine
    # decides what is worth a line.
    cross_track: float = 0.0
    heading_error: float = 0.0
    # True while the car has to come to a stop before the next stroke.
    changing_direction: bool = False

    @property
    def progress(self) -> float:
        """0..1 along the whole path."""
        if self.total <= 0.0:
            return 1.0 if self.finished else 0.0
        return max(0.0, min(1.0, self.travelled / self.total))


class PathFollower:
    """Drives one :class:`~assistance.parking.trajectory.Trajectory`.

    Stateful in exactly one thing: how far along the path the car has got. That
    has to be remembered, because a parking path crosses itself in position --
    the car passes the same spot forwards and backwards -- and a nearest-point
    search with no memory would jump between strokes. Everything else is
    computed from the pose it is handed.
    """

    def __init__(self, trajectory: Trajectory,
                 cruise_mps: float = CRUISE_MPS,
                 lookahead_min: float = MIN_LOOKAHEAD_M):
        self.trajectory = trajectory
        self.cruise_mps = cruise_mps
        self.lookahead_min = lookahead_min
        self.bounds = trajectory.segment_bounds()
        self._index = 0          # index into trajectory.points
        self._segment = 0
        self._awaiting_stop = False

    # ─── Progress ─────────────────────────────────────────────────────

    @property
    def segment_index(self) -> int:
        return self._segment

    @property
    def travelled(self) -> float:
        points = self.trajectory.points
        return points[self._index].s if points else 0.0

    def _advance_index(self, pose: Pose):
        """Move the progress marker forward to the closest point ahead of it.

        Two restrictions, and both are load-bearing. The search goes **forward
        only**, because a parking path visits the same ground more than once
        and a global nearest-point search would teleport the follower into a
        stroke it has not driven yet. And it never leaves the **current
        stroke**: right at a direction change the path doubles back on itself,
        so the nearest point is often in the next stroke -- which would skip
        the stop and the gear change and drive the car the wrong way. Strokes
        are advanced deliberately, in :meth:`_direction_change_pending`, and
        only once the car has actually stopped.
        """
        points = self.trajectory.points
        if not points:
            return
        best = self._index
        best_distance = _distance_sq(points[self._index].pose, pose)
        limit = min(len(points) - 1, self._index + _SEARCH_WINDOW)
        for index in range(self._index + 1, limit + 1):
            if points[index].segment_index != self._segment:
                break
            distance = _distance_sq(points[index].pose, pose)
            if distance < best_distance:
                best, best_distance = index, distance
        self._index = best

    # ─── The demand ───────────────────────────────────────────────────

    def update(self, pose: Pose, speed_mps: float) -> ControlDemand:
        """One control cycle. *speed_mps* is an unsigned speed.

        Cost: the forward search window plus a lookahead walk, both bounded --
        a few dozen float operations, well under 50 microseconds. It runs once
        per assistance cycle while a manoeuvre is active and not at all
        otherwise.
        """
        points = self.trajectory.points
        if not points:
            return ControlDemand(0.0, DIRECTION_FORWARD, 0.0, finished=True)

        self._advance_index(pose)
        current = points[self._index]
        segment = self.trajectory.segments[self._segment]

        cross_track, heading_error = self._errors(pose, current)
        off_track = (abs(cross_track) > MAX_CROSS_TRACK_M
                     or abs(heading_error) > MAX_HEADING_ERROR_RAD)

        # A gear change waits for a real standstill, because LFS will not
        # select reverse while the car is rolling and a half-engaged gear is
        # the one state this manoeuvre cannot recover from.
        remaining_segment = self.bounds[self._segment][1] - current.s
        if self._direction_change_pending(remaining_segment, speed_mps):
            return self._demand(0.0, segment.direction, 0.0, current,
                                cross_track=cross_track,
                                heading_error=heading_error,
                                off_track=off_track, changing_direction=True)
        # The call above may have moved on to the next stroke; everything
        # below has to be about that one, not the one just finished.
        current = points[self._index]
        segment = self.trajectory.segments[self._segment]
        remaining_segment = self.bounds[self._segment][1] - current.s

        remaining_total = self.trajectory.length - current.s
        if remaining_total <= FINISH_TOLERANCE_M:
            return self._demand(0.0, segment.direction, 0.0, current,
                                finished=True, cross_track=cross_track,
                                heading_error=heading_error)

        # Feedforward the arc the path is actually on, and let pure pursuit
        # correct the error rather than reproduce the arc from scratch.
        curvature = current.curvature + self._pursuit_curvature(
            pose, segment.direction, speed_mps, remaining_segment,
            heading_error)
        speed = self._speed_limit(remaining_segment, remaining_total, segment)
        return self._demand(speed, segment.direction, curvature, current,
                            cross_track=cross_track,
                            heading_error=heading_error, off_track=off_track)

    def _demand(self, speed, direction, curvature, current, **kwargs):
        return ControlDemand(
            speed=speed, direction=direction, curvature=curvature,
            travelled=current.s, total=self.trajectory.length,
            stroke=self._segment + 1, strokes=len(self.trajectory.segments),
            **kwargs)

    # ─── Lateral ──────────────────────────────────────────────────────

    def _lookahead(self, speed_mps: float, remaining_segment: float) -> float:
        """How far ahead to aim: speed-scaled, but never past this stroke."""
        wanted = min(MAX_LOOKAHEAD_M,
                     self.lookahead_min + LOOKAHEAD_PER_MPS * max(0.0, speed_mps))
        return max(ABSOLUTE_MIN_LOOKAHEAD_M, min(wanted, remaining_segment))

    def _pursuit_curvature(self, pose: Pose, direction: int, speed_mps: float,
                           remaining_segment: float,
                           heading_error: float) -> float:
        """Pure pursuit to a point one lookahead further along the path.

        The reversing case is the one worth reading twice. The car chases a
        point *behind* itself, so the geometry is done in the frame of the
        direction of travel -- the heading turned through 180 degrees -- and
        the resulting curvature is then negated, because turning the wheel one
        way sends the car the other way round when it is going backwards. Doing
        it in the heading frame instead produces a controller that steers away
        from the path precisely while reversing into a parking space.
        """
        lookahead = self._lookahead(speed_mps, remaining_segment)
        target = self._lookahead_point(lookahead)
        travel = Pose(pose.x, pose.y,
                      pose.yaw if direction == DIRECTION_FORWARD
                      else normalise_angle(pose.yaw + math.pi))
        ahead, left = travel.to_local(target.x, target.y)
        distance = math.hypot(ahead, left)
        pursuit = 0.0
        if distance >= 1e-3:
            pursuit = 2.0 * left / (distance * distance)
            if direction != DIRECTION_FORWARD:
                pursuit = -pursuit
        # Heading term. Reducing a heading error needs the opposite sign of
        # curvature depending on which way the car is travelling, for the same
        # reason the pursuit term does: ``dyaw = kappa * s``, and ``s`` carries
        # the direction.
        return pursuit - HEADING_GAIN * heading_error * direction

    def _lookahead_point(self, lookahead: float) -> Pose:
        """The path point *lookahead* further on, clamped to the current stroke.

        Clamped on purpose: a lookahead that reaches past a direction change
        would aim the car at where it will be going *after* it has reversed,
        which is the wrong way round by exactly 180 degrees.
        """
        points = self.trajectory.points
        end_of_segment = self.bounds[self._segment][1]
        target_s = min(points[self._index].s + lookahead, end_of_segment)
        index = self._index
        while index + 1 < len(points) and points[index].s < target_s:
            if points[index + 1].segment_index != self._segment:
                break
            index += 1
        return points[index].pose

    def _errors(self, pose: Pose, current) -> Tuple[float, float]:
        """Cross-track and heading error against the nearest path point."""
        _, left = current.pose.to_local(pose.x, pose.y)
        heading = normalise_angle(pose.yaw - current.pose.yaw)
        return left, heading

    # ─── Longitudinal ─────────────────────────────────────────────────

    def _direction_change_pending(self, remaining_segment: float,
                                  speed_mps: float) -> bool:
        """Is the car at the end of a stroke and not yet stopped?

        Latched: once the end of a stroke is reached the demand stays at zero
        until the car is genuinely at a standstill, so that a body still
        settling on its springs cannot be read as "moving again" and send the
        manoeuvre off before the gear is in.
        """
        if remaining_segment > SEGMENT_TOLERANCE_M:
            self._awaiting_stop = False
            return False
        if self._segment + 1 >= len(self.trajectory.segments):
            return False
        if speed_mps > STANDSTILL_MPS:
            self._awaiting_stop = True
            return True
        if self._awaiting_stop:
            # Stopped. Hand the next stroke over; the gear change itself is
            # somebody else's job and it will hold the demand at zero while it
            # happens.
            self._awaiting_stop = False
            self._segment += 1
            self._index = _first_index_of(self.trajectory, self._segment,
                                          self._index)
        return False

    def _speed_limit(self, remaining_segment: float, remaining_total: float,
                     segment) -> float:
        """The smallest of the crawl, the stopping distance, and the finish.

        ``v = sqrt(2 a s)`` with the deceleration stated in
        :data:`DECELERATION_MPS2`. Both distances are used because the end of
        the last stroke is also the end of the manoeuvre, and a demand that
        only watched the stroke would arrive at the space at walking pace.
        """
        cruise = self.cruise_mps
        if (segment.curvature == 0.0
                and segment.length >= APPROACH_LENGTH_M
                and remaining_segment > APPROACH_LENGTH_M * 0.5):
            # A long straight with nothing to steer around: no reason to crawl.
            cruise = max(cruise, APPROACH_MPS)
        stopping = math.sqrt(2.0 * DECELERATION_MPS2
                             * max(0.0, min(remaining_segment, remaining_total)))
        return max(0.0, min(cruise, stopping))


# How many sampled points ahead the progress search looks. At the 0.2 m
# sampling step that is 5 m, far more than a 100 ms cycle covers at 2 m/s, and
# short enough that the search cannot jump into a later stroke.
_SEARCH_WINDOW = 25


def _distance_sq(a: Pose, b: Pose) -> float:
    dx = a.x - b.x
    dy = a.y - b.y
    return dx * dx + dy * dy


def _first_index_of(trajectory: Trajectory, segment_index: int,
                    from_index: int) -> int:
    """Index of the first sampled point belonging to *segment_index*."""
    points = trajectory.points
    for index in range(from_index, len(points)):
        if points[index].segment_index == segment_index:
            return index
    return len(points) - 1
