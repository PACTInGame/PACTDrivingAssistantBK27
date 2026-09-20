"""Following a planned path: pose in, control demand out.

This is the seam the whole feature is built around. Above it, everything is
geometry and knows nothing about LFS; below it, everything is input devices and
knows nothing about parking. What crosses it is a :class:`ControlDemand` --
**how fast, which way round, how tightly** -- and nothing else.

That is deliberate and it is what makes the pair reusable. A cruise control, a
lane keeper or an automated test driver needs exactly the same three numbers;
none of them needs to know that the path came from a parking slot. Whatever
actuates the car reads a ``ControlDemand`` and never sees a ``Trajectory``.

### Lateral: the path's own arc, plus a correction for the error against it

The steering demand is a **curvature**, not a steering angle, and that is the
important choice. A steering angle would have to be converted through a
wheelbase and a steering lock that this project cannot look up for a vehicle
mod (``conventions.md`` §4), and the conversion would be wrong for every modded
car. Curvature is measurable from data LFS already sends -- ``yaw_rate / v``
from ``CompCar`` -- so the loop that turns a demanded curvature into steering
can *learn* the car it is driving instead of assuming one.

The law is feedforward plus error feedback, and nothing else::

    kappa = kappa_path  -  CROSS_TRACK_GAIN * e  -  HEADING_GAIN * direction * theta

``kappa_path`` is the arc the path is on under the car, ``e`` is how far the
car is to the **left** of it and ``theta`` how far its heading is turned out of
it. Both errors are zero when the car is on its path, so a car that is tracking
perfectly asks for exactly the arc that was planned.

That last sentence is the whole point of this rewrite. The previous version fed
the path's arc forward and then added a **pure-pursuit** term on top -- and
pure pursuit is not an error term. Aimed at a point on the path, it asks for
the arc that reaches that point, which on a curved path is the path's own arc
again. A car sitting exactly on a 6 m arc therefore demanded 0.233 1/m where
the plan said 0.167: half as much steering again as the manoeuvre was planned
with, before any error at all. Only :data:`MAX_CORRECTION_FACTOR` kept it
finite, which is why a live run found the demand pinned at that cap for ten
seconds and the car ended up visibly crooked in the space.

### Why the heading term flips with direction and the cross-track term does not

For a rear-axle kinematic model with signed speed ``v``, the errors move as::

    de/dt     = v * theta
    dtheta/dt = v * (kappa - kappa_path)

Writing the correction as ``-alpha*e - beta*theta`` gives a closed loop with
trace ``-v*beta`` and determinant ``v^2*alpha``. The determinant is positive
either way round, so the cross-track gain is the same forwards and backwards;
the trace is only negative when ``beta`` carries the sign of ``v``, so the
heading gain must flip. Get that one sign wrong and the controller is a saddle
point -- stable driving forwards, divergent reversing into the space, which is
the half of the manoeuvre that matters.

The gains are stated as a settling length rather than tuned: see
:data:`CORRECTION_LENGTH_M`.

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
import time
from dataclasses import dataclass
from typing import Tuple

from assistance.parking.geometry import Pose, normalise_angle
from assistance.parking.trajectory import (DIRECTION_FORWARD,
                                           PLAN_RADIUS_MARGIN,
                                           Trajectory)

# ─── Lateral ──────────────────────────────────────────────────────────────

# Over what distance an error against the path is taken out. Stated as a
# length rather than as gains, because that is the quantity that has to be
# right: a parking stroke is one to five metres long, so an error has to be
# gone inside a couple of metres of travel or the stroke ends before the
# correction does -- which is exactly how a five-stroke shuffle accumulates a
# crooked finish.
#
# The gains follow from it. With ``de/ds = theta`` and ``dtheta/ds = -alpha*e
# - beta*theta`` the closed loop is a second-order system in *distance*:
# ``alpha = 1/L^2`` sets the length scale and ``beta = 2/L`` makes it
# critically damped, i.e. it converges without ever crossing the path. No
# overshoot is worth more here than speed is: an overshoot in a parking space
# is a wheel against a kerb.
CORRECTION_LENGTH_M = 2.0
# 1/m per metre of cross-track error.
CROSS_TRACK_GAIN = 1.0 / (CORRECTION_LENGTH_M * CORRECTION_LENGTH_M)
# 1/m per radian of heading error, applied with the sign of travel.
HEADING_GAIN = 2.0 / CORRECTION_LENGTH_M

# How much tighter than the planned arc the total demand may go. The plan's own
# arcs are at the planning radius and the planning radius is deliberately
# *wider* than the car's steering lock by
# :data:`~assistance.parking.trajectory.PLAN_RADIUS_MARGIN`, so this is the
# margin that exists -- less a little, so that a saturated correction is still
# a curvature the car can actually produce.
#
# A demand no car can follow is not a correction, it is an oscillator: a live
# run watched an uncapped correction ask for a 1.3 m radius on a manoeuvre
# planned at 6 m, and the car swung hard one way, hit the parked car it was
# avoiding, and then swung hard back.
#
# It is the planning margin itself, which makes the cap exactly the car's
# steering lock: the plan is that factor wider than the lock, so a demand that
# factor tighter than the plan is the lock. Anything beyond it is a number the
# steering cannot produce.
MAX_CORRECTION_FACTOR = PLAN_RADIUS_MARGIN

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

# How fast the *demand* may rise, in m/s^2. Without it a stroke begins with a
# step from standstill to the crawl, and a step is a demand no controller can
# meet: whatever drives the car answers it with everything it has, overshoots,
# and has to brake -- which is the throttle-and-brake sawtooth a live run
# recorded at every direction change, 2.3 m/s against a 1.1 m/s demand.
#
# Matched to the deceleration below so that a stroke's speed profile is
# symmetric, and well inside what a car does at walking pace, so the demand is
# always something the car is physically able to be doing.
ACCELERATION_MPS2 = 0.8
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
                 cruise_mps: float = CRUISE_MPS, clock=time.monotonic):
        self.trajectory = trajectory
        self.cruise_mps = cruise_mps
        self.clock = clock
        self.bounds = trajectory.segment_bounds()
        self._index = 0          # index into trajectory.points
        self._segment = 0
        self._awaiting_stop = False
        # The demand the last cycle issued, and when. Both only exist for the
        # acceleration limit; see ACCELERATION_MPS2.
        self._demanded_speed = 0.0
        self._last_update = None

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

        Cost: the bounded forward search plus a handful of float operations,
        well under 50 microseconds. It runs once per assistance cycle while a
        manoeuvre is active and not at all otherwise.
        """
        points = self.trajectory.points
        if not points:
            return ControlDemand(0.0, DIRECTION_FORWARD, 0.0, finished=True)

        now = self.clock()
        dt = 0.0 if self._last_update is None else max(0.0, now - self._last_update)
        self._last_update = now

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
            self._demanded_speed = 0.0
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
            self._demanded_speed = 0.0
            return self._demand(0.0, segment.direction, 0.0, current,
                                finished=True, cross_track=cross_track,
                                heading_error=heading_error)

        # Feedforward the arc the path is actually on, plus a correction that
        # is zero while the car is on it. The sum is capped at something the
        # car can actually steer; see MAX_CORRECTION_FACTOR.
        curvature = self._capped(current.curvature + self._correction(
            segment.direction, cross_track, heading_error))
        speed = self._speed_limit(remaining_segment, remaining_total, segment,
                                  dt)
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

    def _capped(self, curvature: float) -> float:
        """Clamp a demand to what the planning radius says the car can do."""
        radius = getattr(self.trajectory, 'radius', 0.0) or 0.0
        if radius <= 0.0:
            return curvature
        limit = MAX_CORRECTION_FACTOR / radius
        return max(-limit, min(limit, curvature))

    def _correction(self, direction: int, cross_track: float,
                    heading_error: float) -> float:
        """Curvature to add for being off the path. Zero when on it.

        Four multiplications, and the only thing in it worth reading twice is
        the ``direction`` on the heading term and its absence on the
        cross-track one. The module docstring derives why: the closed loop's
        determinant is even in the direction of travel and its trace is odd,
        so one gain flips and the other does not. Flipping both -- or neither
        -- gives a controller that converges driving forwards and diverges
        reversing, which is the half of a parking manoeuvre that matters.
        """
        return (-CROSS_TRACK_GAIN * cross_track
                - HEADING_GAIN * heading_error * direction)

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
                     segment, dt: float) -> float:
        """The smallest of the crawl, the stopping distance, and the finish --
        then rate-limited on the way up.

        ``v = sqrt(2 a s)`` with the deceleration stated in
        :data:`DECELERATION_MPS2`. Both distances are used because the end of
        the last stroke is also the end of the manoeuvre, and a demand that
        only watched the stroke would arrive at the space at walking pace.

        The rate limit applies to **rising** demands only. Falling ones are
        already a physical limit -- the stopping-distance curve -- and slewing
        those would ask the car to arrive somewhere faster than it can stop.
        """
        cruise = self.cruise_mps
        if (segment.curvature == 0.0
                and segment.length >= APPROACH_LENGTH_M
                and remaining_segment > APPROACH_LENGTH_M * 0.5):
            # A long straight with nothing to steer around: no reason to crawl.
            cruise = max(cruise, APPROACH_MPS)
        stopping = math.sqrt(2.0 * DECELERATION_MPS2
                             * max(0.0, min(remaining_segment, remaining_total)))
        wanted = max(0.0, min(cruise, stopping))
        # ``dt`` is zero on the very first cycle of a manoeuvre, and it has to
        # limit that one too: without it the first demand a stroke ever issues
        # is the full crawl against a standing car, which is the step this
        # whole limit exists to remove.
        wanted = min(wanted, self._demanded_speed + ACCELERATION_MPS2 * dt)
        self._demanded_speed = wanted
        return wanted


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
