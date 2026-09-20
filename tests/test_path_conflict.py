"""The shared conflict geometry (``assistance/path_conflict.py``).

Everything here is derived from the coordinate convention, not observed:
``reference/conventions.md`` §1 - X east, Y north, right-handed, LFS headings
anticlockwise from +Y. So heading 0 drives north, 90 drives west, 270 drives
east.

The point of this module is that vehicles are **rectangles**. Two cars whose
centres are 4 m apart have already crashed; a point model says they are 4 m
apart. Every test below is a statement about that difference.
"""

import math

import pytest

from assistance.emergency_brake import EmergencyBrake
from assistance.path_conflict import (
    BRAKE_DEMAND_MS2, INF, PANIC_DECELERATION_MS2, Body, body_from,
    contact_window, direction_vector, free_distance, stopping_deceleration)

KMH_TO_MS = 1.0 / 3.6


def car(x, y, heading_deg, kmh, length=4.5, width=1.8,
        yaw_deg_s=0.0) -> Body:
    """A body from human units. ``heading_deg`` is the LFS frame (0 = north),
    and so is ``yaw_deg_s``: positive turns anticlockwise, i.e. to the left."""
    radians = math.radians(heading_deg + 90.0)
    return Body(x, y, math.cos(radians), math.sin(radians),
                kmh * KMH_TO_MS, length, width, math.radians(yaw_deg_s))


# ─── The direction vector ────────────────────────────────────────────────────

@pytest.mark.parametrize("heading_deg, expected", [
    (0.0, (0.0, 1.0)),      # north -> +Y
    (90.0, (-1.0, 0.0)),    # 90 deg anticlockwise from north -> west
    (180.0, (0.0, -1.0)),   # south
    (270.0, (1.0, 0.0)),    # east
])
def test_direction_vector_is_anticlockwise_from_north(heading_deg, expected):
    from conftest import lfs_heading
    assert direction_vector(lfs_heading(heading_deg)) == pytest.approx(
        expected, abs=1e-3)


# ─── The support function: a car is longer than it is wide ───────────────────

def test_half_extent_is_length_across_the_car_and_width_along_it():
    north = car(0.0, 0.0, 0.0, 50.0, length=4.5, width=1.8)
    # Along its own axis (+Y) it reaches half its length...
    assert north.half_extent(0.0, 1.0) == pytest.approx(2.25)
    # ...and across it, half its width.
    assert north.half_extent(1.0, 0.0) == pytest.approx(0.9)
    # 45 deg: both contribute.
    diagonal = math.sqrt(0.5)
    assert north.half_extent(diagonal, diagonal) == pytest.approx(
        2.25 * diagonal + 0.9 * diagonal)


def test_body_from_reads_the_size_out_of_the_car_name(make_vehicle):
    small = body_from(make_vehicle(cname=b"UF1").data)
    large = body_from(make_vehicle(cname=b"FXR").data)
    unknown = body_from(make_vehicle(cname=b"q7Xk").data)
    assert small.length < large.length
    # A vehicle mod does not raise -- and it does not get the mid-size
    # default either. These bodies feed the contact windows of the cross
    # traffic and blind spot warnings, where a car assumed too small simply
    # never produces a contact, so the fallback is the largest standard car
    # (known-issues #28, ``conventions.md`` section 4).
    assert unknown.length == pytest.approx(5.0)
    assert unknown.width == pytest.approx(2.1)


# ─── Contact window ──────────────────────────────────────────────────────────

def test_head_on_contact_happens_before_the_centres_meet():
    """Two cars 100 m apart closing at 10 m/s each.

    The centres would meet at 5.0 s. The bumpers meet 0.45 s earlier - two
    half-lengths of 2.25 m at 20 m/s of closing speed.
    """
    own = car(0.0, 0.0, 0.0, 36.0)
    other = car(0.0, 100.0, 180.0, 36.0)
    window = contact_window(own, other)
    assert window[0] == pytest.approx(5.0 - 4.5 / 20.0, abs=1e-3)


def test_a_perpendicular_crossing_is_gated_by_length_and_width():
    """We drive north at 10 m/s, they cross west to east 20 m ahead.

    Contact starts when our nose (2.25 m) reaches the edge of their swept
    band (0.9 m): 20 - 3.15 = 16.85 m, i.e. 1.685 s.
    """
    own = car(0.0, -20.0, 0.0, 36.0)
    other = car(20.0, 0.0, 90.0, 36.0)
    window = contact_window(own, other)
    assert window[0] == pytest.approx(1.685, abs=0.01)


def test_two_cars_side_by_side_in_separate_lanes_never_touch():
    own = car(0.0, 0.0, 0.0, 50.0)
    beside = car(3.5, 0.0, 0.0, 70.0)
    assert contact_window(own, beside) is None


def test_a_car_that_already_passed_has_a_window_in_the_past():
    """The caller has to tell "over" from "never" - both are not a warning,
    but only one of them was ever a conflict."""
    own = car(0.0, 0.0, 0.0, 36.0)
    # Crossed our line 20 m behind us and is driving away.
    gone = car(30.0, -20.0, 90.0, 72.0)
    window = contact_window(own, gone)
    assert window is None or window[1] < 0.0


def test_merging_predicts_contact_although_nothing_is_ahead():
    """The blind-spot case: we drift left at 15 deg, they come up the left lane.

    Neither car is in front of the other, and the lateral gap is 3.5 m. It is
    the *combination* that collides, which is exactly what a distance or an
    angle on its own cannot see.
    """
    own = car(0.0, 0.0, 15.0, 25.0)          # turning left
    overtaking = car(-3.5, -15.0, 0.0, 55.0)  # left lane, 15 m back, faster
    window = contact_window(own, overtaking)
    assert window is not None
    assert 0.0 < window[0] < 2.5


# ─── Free distance ───────────────────────────────────────────────────────────

def test_free_distance_stops_us_short_of_the_other_ones_band():
    """Same geometry as the perpendicular crossing above, and the same number:
    the distance at which our nose touches their swept band."""
    own = car(0.0, -20.0, 0.0, 36.0)
    other = car(20.0, 0.0, 90.0, 36.0)
    assert free_distance(own, other) == pytest.approx(16.85, abs=0.01)


def test_free_distance_ignores_the_other_ones_speed():
    """Geometry only. Whether they are *there* at the time is a separate
    question, and mixing the two is how a warning ends up depending on which
    of two independent facts changed."""
    own = car(0.0, -20.0, 0.0, 36.0)
    slow = car(20.0, 0.0, 90.0, 5.0)
    fast = car(20.0, 0.0, 90.0, 120.0)
    assert free_distance(own, slow) == pytest.approx(free_distance(own, fast))


def test_free_distance_is_zero_when_we_are_already_in_the_band():
    """Their path runs straight through where we stand: no room at all, and
    braking can only hold us in it. Which side that argues for is the
    caller's decision - see the two systems."""
    own = car(0.0, 0.0, 0.0, 20.0)
    crossing_here = car(20.0, 0.0, 90.0, 20.0)
    assert free_distance(own, crossing_here) == 0.0


def test_free_distance_is_infinite_for_a_parallel_neighbour():
    """A car in the next lane going the same way never crosses our path."""
    own = car(0.0, 0.0, 0.0, 36.0)
    next_lane = car(3.5, -10.0, 0.0, 60.0)
    assert free_distance(own, next_lane) == INF


def test_free_distance_measures_to_the_tail_of_a_car_ahead():
    """It is not a lateral quantity. A car in our own lane 30 m ahead leaves
    us 25.5 m - the gap minus the two half-lengths - before we are in the
    road it occupies. That is longitudinal, and it is the reason the
    blind-spot system checks the *direction* of the conflict before it asks
    for braking."""
    own = car(0.0, 0.0, 0.0, 36.0)
    ahead = car(0.0, 30.0, 0.0, 36.0)
    assert free_distance(own, ahead) == pytest.approx(25.5)


# ─── Deceleration ────────────────────────────────────────────────────────────

def test_stopping_deceleration_is_v_squared_over_two_s():
    # 10 m/s, 20 m of room, no buffer and no reaction time: 2.5 m/s².
    assert stopping_deceleration(20.0, 10.0, 0.0, 0.0) == pytest.approx(2.5)


def test_the_buffer_and_the_reaction_time_come_off_the_distance():
    plain = stopping_deceleration(20.0, 10.0, 0.0, 0.0)
    guarded = stopping_deceleration(20.0, 10.0, 1.0, 0.2)
    # 20 - 1 - 2 = 17 m left.
    assert guarded == pytest.approx(100.0 / (2 * 17.0))
    assert guarded > plain


def test_no_room_left_is_the_panic_value_and_no_room_needed_is_zero():
    assert stopping_deceleration(0.5, 20.0, 1.0, 0.2) == PANIC_DECELERATION_MS2
    assert stopping_deceleration(INF, 20.0, 1.0, 0.2) == 0.0


def test_the_brake_threshold_copy_matches_the_system_that_owns_it():
    """``BRAKE_DEMAND_MS2`` exists so a warning system can say "this is the
    level at which it brakes" without importing the brake. It has to stay
    equal to the real threshold, and this is what keeps it that way."""
    assert BRAKE_DEMAND_MS2 == EmergencyBrake.ENGAGE_DECELERATION_MS2

# --- Turning: the yaw rate is what makes a lane change visible early --------
#
# Measured in `simulation_tests` scenario 22: with the heading alone, a car
# 0.7 s into a turn still reads as "never crosses their path", because 2 deg of
# yaw across a 1.8 m gap is four seconds of travel - while the yaw rate was
# already 20 deg/s and the contact was one second away.


def test_a_turn_brings_the_encroachment_forward():
    """Same instant, same heading, only the yaw rate differs."""
    straight = car(0.0, 0.0, 357.0, 16.5)
    turning = car(0.0, 0.0, 357.0, 16.5, yaw_deg_s=-20.0)
    neighbour = car(3.5, -15.0, 0.0, 70.0)

    assert free_distance(turning, neighbour) < free_distance(straight,
                                                             neighbour)


def test_the_turn_can_only_bring_it_forward_never_push_it_away():
    """A model with a constant yaw rate is a claim, not a prediction. It is
    allowed to warn earlier and never allowed to silence a warning."""
    for yaw in (-40.0, -5.0, 0.0, 5.0, 40.0):
        turning = car(0.0, 0.0, 357.0, 16.5, yaw_deg_s=yaw)
        straight = car(0.0, 0.0, 357.0, 16.5)
        neighbour = car(3.5, -15.0, 0.0, 70.0)
        assert free_distance(turning, neighbour) <= free_distance(
            straight, neighbour) + 1e-6, yaw


def test_turning_away_changes_nothing():
    turning_away = car(0.0, 0.0, 357.0, 16.5, yaw_deg_s=+20.0)
    straight = car(0.0, 0.0, 357.0, 16.5)
    neighbour = car(3.5, -15.0, 0.0, 70.0)
    assert free_distance(turning_away, neighbour) == pytest.approx(
        free_distance(straight, neighbour))


def test_a_corner_both_cars_take_together_is_not_a_lane_change():
    """The *relative* yaw rate is the quantity. On a circuit both cars turn
    all the time; what matters is whether one turns into the other."""
    ours = car(0.0, 0.0, 357.0, 16.5, yaw_deg_s=-20.0)
    theirs = car(3.5, -15.0, 0.0, 70.0, yaw_deg_s=-20.0)
    straight = car(0.0, 0.0, 357.0, 16.5)
    assert free_distance(ours, theirs) == pytest.approx(
        free_distance(straight, theirs))


@pytest.mark.parametrize("yaw_deg_s", [-1.0, -5.0, -10.0])
def test_cornering_noise_is_below_the_relative_yaw_gate(yaw_deg_s):
    """The threshold this is checking was measured, not chosen.

    Over `simulation_tests` scenario 24 - two cars side by side through a long
    corner - the *relative* yaw rate reached 10.6 deg/s from line choice and
    steering corrections alone, with a median of 2.8. A real turn-in is at
    20 deg/s and above when it matters (scenario 22). Everything in the first
    band has to read as straight, or the arc turns corner noise into a
    predicted collision.
    """
    wobbling = car(0.0, 0.0, 0.0, 16.5, yaw_deg_s=yaw_deg_s)
    straight = car(0.0, 0.0, 0.0, 16.5)
    neighbour = car(3.5, -15.0, 0.0, 70.0)
    assert free_distance(wobbling, neighbour) == free_distance(straight,
                                                               neighbour)
    assert contact_window(wobbling, neighbour) == contact_window(straight,
                                                                 neighbour)


def test_the_arc_matches_the_straight_line_when_the_yaw_is_zero():
    """``Body.at`` has two branches; they must meet."""
    straight = car(0.0, 0.0, 0.0, 36.0)
    x, y, dx, dy = straight.at(2.0)
    assert (x, y) == pytest.approx((0.0, 20.0))
    assert (dx, dy) == pytest.approx((straight.dx, straight.dy), abs=1e-9)


def test_the_arc_turns_the_body_as_well_as_moving_it():
    """A quarter turn to the left at 90 deg/s: after one second the car points
    west and sits on the quarter circle of radius v/omega."""
    turning = car(0.0, 0.0, 0.0, 36.0, yaw_deg_s=90.0)
    radius = 10.0 / math.radians(90.0)
    x, y, dx, dy = turning.at(1.0)
    assert (x, y) == pytest.approx((-radius, radius), abs=1e-6)
    assert (dx, dy) == pytest.approx((-1.0, 0.0), abs=1e-6)


def test_the_yaw_prediction_is_bounded_in_time():
    """The arc is walked for at most 1.5 s. A car far enough away that the
    turn cannot reach it inside that window gets the straight-line answer."""
    turning = car(0.0, 0.0, 0.0, 16.5, yaw_deg_s=-20.0)
    straight = car(0.0, 0.0, 0.0, 16.5)
    far_neighbour = car(60.0, -15.0, 0.0, 70.0)
    assert free_distance(turning, far_neighbour) == pytest.approx(
        free_distance(straight, far_neighbour))


# --- The contact window walks the arc too -----------------------------------


def test_a_turn_finds_a_contact_the_straight_line_cannot_see():
    """The merge from scenario 22: one degree off the lane, 20 deg/s on the
    wheel, something faster 15 m back. The heading alone says "no conflict"
    for another four seconds."""
    merging = car(0.0, 0.0, 359.0, 25.0, yaw_deg_s=-20.0)
    straight = car(0.0, 0.0, 359.0, 25.0)
    overtaking = car(3.5, -15.0, 0.0, 55.0)

    assert contact_window(straight, overtaking) is None
    window = contact_window(merging, overtaking)
    assert window is not None
    assert 0.0 < window[0] < 2.0


def test_the_arc_can_only_bring_the_contact_forward():
    """Same rule as the free distance: a constant yaw rate may warn earlier
    and may never silence a warning the straight line found."""
    head_on = car(0.0, 100.0, 180.0, 36.0)
    straight = car(0.0, 0.0, 0.0, 36.0)
    reference = contact_window(straight, head_on)[0]
    for yaw in (-40.0, -20.0, 20.0, 40.0):
        window = contact_window(car(0.0, 0.0, 0.0, 36.0, yaw_deg_s=yaw),
                                head_on)
        assert window is not None, yaw
        assert window[0] <= reference + 1e-6, yaw


def test_a_corner_both_cars_take_together_predicts_no_contact():
    """Concentric arcs at the same rate: nothing between them changes."""
    ours = car(0.0, 0.0, 0.0, 60.0, yaw_deg_s=-20.0)
    theirs = car(3.5, -8.0, 0.0, 62.0, yaw_deg_s=-20.0)
    assert contact_window(ours, theirs) is None


def test_traffic_that_has_already_gone_past_is_not_a_contact():
    """`simulation_tests` scenario 25, reduced to its geometry: turning in
    behind something that is drawing level at four times our speed. We really
    are heading for its lane, and it really is close - but it is 27 m down the
    road by the time we get there, and a prediction that looks at one moment
    at a time says so."""
    turning_in = car(0.0, 0.0, 357.0, 22.0, yaw_deg_s=-36.0)
    passing = car(4.5, -5.5, 0.0, 93.0)
    window = contact_window(turning_in, passing)
    assert window is None or window[0] > 2.5


def test_two_cars_in_a_corner_are_not_saved_by_their_own_arcs():
    """Why the corner case is solved at the caller and not here.

    Scenario 24's numbers: 107 km/h, 5.5 m apart, 2.4 degrees of heading
    between them, measured yaw rates of 8.7 and 10.4 deg/s. Those are radii of
    196 m and 164 m - two circles that meet. Extrapolation of either kind
    predicts a contact that did not happen, which is why
    ``BlindSpotWarning.STEADY_TTC_S`` shortens the horizon instead of the
    model being made cleverer.
    """
    inner = car(0.0, 0.0, 0.0, 107.0, yaw_deg_s=8.7)
    outer = car(-5.5, 0.0, -2.4, 107.0, yaw_deg_s=10.4)

    window = contact_window(inner, outer)
    # It still "sees" a contact...
    assert window is not None and window[0] > 0.0
    # ...and it is more than 1.5 s away, which is exactly the band the caller
    # declines to warn about.
    assert window[0] > 1.5
