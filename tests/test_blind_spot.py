"""Blind spot warning: trigger condition, geometry and hot-path cost (WP8).

Coordinates below are the ones a driver would describe: the own car sits at
the origin pointing **north** (LFS heading 0 = +Y), so -X is its left and -Y
is behind it. ``relate_to_own`` fills in ``distance_to_player`` /
``angle_to_player`` exactly as ``VehicleManager._apply_frame`` does per MCI
frame, because the pre-filter reads both.
"""

import pytest
from shapely import Polygon

from assistance.emergency_brake import EmergencyBrake

from assistance.blind_spot_warning import (
    BlindSpotWarning, _create_blindspot_rectangle, _normalize_angle,
    car_angle_degrees, create_rectangle_for_car,
    _CORRIDOR_ANGLES_LEFT, _CORRIDOR_ANGLES_RIGHT, _CORRIDOR_MULTIPLIERS)
from misc.helpers import calc_polygon_points

from conftest import METRE


class FakeClock:
    """Monotonic clock a test drives by hand."""

    def __init__(self, start: float = 1000.0):
        self.now = start

    def __call__(self) -> float:
        return self.now

    def advance(self, seconds: float):
        self.now += seconds


@pytest.fixture
def bsw(bus, settings):
    system = BlindSpotWarning(bus, settings)
    system.clock = FakeClock()
    return system


NO_WARNING = {'left_warning': False, 'right_warning': False}

# ``CompCar.AngVel``: 16384 units = 360 deg/s anticlockwise
# (``reference/conventions.md`` section 2). Negative turns clockwise, i.e. to
# the right.
YAW_20_DEG_S_RIGHT = -int(round(20.0 / 360.0 * 16384))


def yaw_units(deg_per_s):
    """Degrees per second -> the raw ``CompCar.AngVel`` word."""
    return int(round(deg_per_s / 360.0 * 16384))


def warnings(result):
    """Only the two booleans, so a test can ignore the levels and the demand."""
    return {'left_warning': result['left_warning'],
            'right_warning': result['right_warning']}


def run(bsw, make_own_vehicle, make_vehicle, relate_to_own, others, **own_kwargs):
    """One process() pass with the own car at the origin heading north."""
    own = make_own_vehicle(**own_kwargs)
    vehicles = {}
    for index, spec in enumerate(others, start=2):
        vehicle = make_vehicle(plid=index, **spec)
        vehicles[index] = vehicle
    if vehicles:
        relate_to_own(own, *vehicles.values())
    return bsw.process(own, vehicles)


# ─── The angle helpers ───────────────────────────────────────────────────────

def test_normalize_angle_wraps_instead_of_mirroring():
    """``abs()`` mirrored negative angles across the X axis; ``% 360`` does not."""
    assert _normalize_angle(-30.0) == pytest.approx(330.0)
    assert _normalize_angle(450.0) == pytest.approx(90.0)
    assert _normalize_angle(90.0) == pytest.approx(90.0)


@pytest.mark.parametrize("heading_units, expected_deg", [
    (0, 90.0),          # pointing north  -> +Y  -> 90° in math frame
    (16384, 180.0),     # pointing west   -> -X
    (32768, 270.0),     # pointing south  -> -Y
    (49152, 360.0),     # pointing east   -> +X (0 == 360)
])
def test_car_angle_degrees_matches_the_project_convention(heading_units, expected_deg):
    assert car_angle_degrees(heading_units) == pytest.approx(expected_deg, abs=0.02)


def _nose_of(outline):
    """The mid-point of the two front corners, in metres relative to the centre."""
    corners = [(x / METRE, y / METRE) for x, y in outline.exterior.coords[:-1]]
    front_left, front_right = corners[0], corners[3]   # offsets +22° and -22°
    return ((front_left[0] + front_right[0]) / 2.0,
            (front_left[1] + front_right[1]) / 2.0)


def test_other_car_outline_is_not_mirrored_below_half_a_turn():
    """``abs((heading - 16384) / 182.05)`` mirrored every heading below 16384.

    Heading 8192 is 45° anticlockwise from north, i.e. **north-west**. The old
    expression turned that into +45° in the math frame, i.e. north-east: the
    4.3 m long outline pointed into the wrong quadrant. For headings from
    16384 upwards it was only a 180° rotation, which this centrally symmetric
    box does not notice - which is why the bug survived.
    """
    north_west = create_rectangle_for_car(0.0, 0.0, 8192)
    nose_x, nose_y = _nose_of(north_west)
    assert nose_x < 0 and nose_y > 0
    assert (nose_x, nose_y) == pytest.approx((-1.507, 1.507), abs=0.01)

    mirrored_angle = abs((8192 - 16384) / 182.05)
    mirrored = Polygon([calc_polygon_points(0.0, 0.0, 2.3 * METRE, mirrored_angle + off)
                        for off in (22, 158, 202, 338)])
    assert _nose_of(mirrored)[0] > 0     # the old code pointed north-east


# ─── The corridor polygon ────────────────────────────────────────────────────

@pytest.mark.parametrize("angles", [_CORRIDOR_ANGLES_LEFT, _CORRIDOR_ANGLES_RIGHT])
def test_corridor_polygon_is_simple(angles):
    """The two far corners used to be swapped, which crossed two edges.

    shapely then had an invalid polygon whose ``intersects`` covered a
    bow-tie of ~64 m² instead of the intended ~190 m² corridor.
    """
    corridor = _create_blindspot_rectangle(0.0, 0.0, car_angle_degrees(0), angles)
    assert corridor.is_valid
    assert corridor.area / (METRE * METRE) == pytest.approx(190.3, abs=1.0)

    swapped = list(angles)
    swapped[1], swapped[2] = swapped[2], swapped[1]
    bowtie = _create_blindspot_rectangle(0.0, 0.0, car_angle_degrees(0), tuple(swapped))
    assert not bowtie.is_valid


def test_corridor_sides_are_left_and_right():
    left = _create_blindspot_rectangle(0.0, 0.0, car_angle_degrees(0), _CORRIDOR_ANGLES_LEFT)
    right = _create_blindspot_rectangle(0.0, 0.0, car_angle_degrees(0), _CORRIDOR_ANGLES_RIGHT)
    # Facing north, left is -X (west) and right is +X (east).
    assert left.centroid.x < 0
    assert right.centroid.x > 0
    # Both reach the full corridor length backwards.
    assert min(y for _, y in left.exterior.coords) / METRE == pytest.approx(
        -_CORRIDOR_MULTIPLIERS[1], abs=0.2)


# ─── Acceptance: the case that could never warn ──────────────────────────────

def test_car_of_the_same_speed_in_the_blind_spot_warns(
        bsw, make_own_vehicle, make_vehicle, relate_to_own, recorder):
    """The regression this package exists for.

    ``distance < (other_kmh - own_kmh + 5) * 1.2`` is <= 0 for any car that is
    not faster than us, so the most common real case - somebody sitting in the
    blind spot at exactly our speed - could never raise a warning.
    """
    events = recorder('blind_spot_warning_changed')
    result = run(bsw, make_own_vehicle, make_vehicle, relate_to_own,
                 [dict(x=-3.0, y=-5.0, heading=0.0, speed=50.0)],
                 speed=50.0)

    assert result['left_warning'] is True
    assert result['right_warning'] is False
    assert events.last('blind_spot_warning_changed') == {
        'left': True, 'right': False, 'left_level': 1, 'right_level': 0}


def test_same_speed_on_the_right_warns_on_the_right(
        bsw, make_own_vehicle, make_vehicle, relate_to_own):
    result = run(bsw, make_own_vehicle, make_vehicle, relate_to_own,
                 [dict(x=3.0, y=-5.0, heading=0.0, speed=50.0)],
                 speed=50.0)
    assert result['right_warning'] is True
    assert result['left_warning'] is False


def test_a_car_60_m_behind_does_not_warn(
        bsw, make_own_vehicle, make_vehicle, relate_to_own):
    """Neither in our own lane nor in the next one, as long as it does not close."""
    in_lane = run(bsw, make_own_vehicle, make_vehicle, relate_to_own,
                  [dict(x=0.0, y=-60.0, heading=0.0, speed=50.0)],
                  speed=50.0)
    assert warnings(in_lane) == NO_WARNING

    bsw.clock.advance(10.0)
    next_lane = run(bsw, make_own_vehicle, make_vehicle, relate_to_own,
                    [dict(x=-3.0, y=-60.0, heading=0.0, speed=50.0)],
                    speed=50.0)
    assert warnings(next_lane) == NO_WARNING


def test_a_car_directly_behind_is_never_in_the_blind_spot(
        bsw, make_own_vehicle, make_vehicle, relate_to_own):
    """The corridor starts 1 m off the axis; a car in our own lane misses it.

    It must miss the acute stages too, and for a different reason: at 3 m
    centre-to-centre two 3.7 m cars already overlap, so the contact prediction
    says "touching, and has been forever". ``_is_plain_following`` rejects the
    pair before that - same lane, same direction, nothing happening.
    """
    result = run(bsw, make_own_vehicle, make_vehicle, relate_to_own,
                 [dict(x=0.0, y=-3.0, heading=0.0, speed=50.0)],
                 speed=50.0)
    assert warnings(result) == NO_WARNING


def test_a_fast_approach_from_far_back_still_warns(
        bsw, make_own_vehicle, make_vehicle, relate_to_own):
    """60 m back closing at 60 km/h reaches us in 3.2 s - inside APPROACH_TIME_S."""
    result = run(bsw, make_own_vehicle, make_vehicle, relate_to_own,
                 [dict(x=-3.0, y=-60.0, heading=0.0, speed=110.0)],
                 speed=50.0)
    assert result['left_warning'] is True


def test_a_slow_approach_from_far_back_does_not_warn_yet(
        bsw, make_own_vehicle, make_vehicle, relate_to_own):
    """Same 60 m, only 20 km/h faster: 9.5 s away, no reason to warn."""
    result = run(bsw, make_own_vehicle, make_vehicle, relate_to_own,
                 [dict(x=-3.0, y=-60.0, heading=0.0, speed=70.0)],
                 speed=50.0)
    assert result['left_warning'] is False


def test_a_car_pointing_the_other_way_is_ignored(
        bsw, make_own_vehicle, make_vehicle, relate_to_own):
    """``_is_within_threshold``: oncoming traffic is not blind-spot traffic."""
    result = run(bsw, make_own_vehicle, make_vehicle, relate_to_own,
                 [dict(x=-3.0, y=-5.0, heading=180.0, speed=50.0)],
                 speed=50.0)
    assert warnings(result) == NO_WARNING


# ─── Movement filter ─────────────────────────────────────────────────────────

def test_a_parked_car_in_the_blind_spot_does_not_warn(
        bsw, make_own_vehicle, make_vehicle, relate_to_own):
    """Driving past parked cars was a permanent warning.

    The geometry is identical to the same-speed case above; only the other
    car's speed differs. Without the movement filter every car at the kerb sat
    in the corridor for as long as it took to drive past it.
    """
    result = run(bsw, make_own_vehicle, make_vehicle, relate_to_own,
                 [dict(x=-3.0, y=-5.0, heading=0.0, speed=0.0)],
                 speed=50.0)
    assert warnings(result) == NO_WARNING


def test_a_car_falling_behind_does_not_warn(
        bsw, make_own_vehicle, make_vehicle, relate_to_own):
    """20 km/h slower is not a lane-change conflict; it disappears backwards."""
    result = run(bsw, make_own_vehicle, make_vehicle, relate_to_own,
                 [dict(x=-3.0, y=-5.0, heading=0.0, speed=30.0)],
                 speed=50.0)
    assert warnings(result) == NO_WARNING


def test_a_car_two_kmh_slower_still_warns(
        bsw, make_own_vehicle, make_vehicle, relate_to_own):
    """The tolerance band: matching speeds are never measured exactly equal."""
    result = run(bsw, make_own_vehicle, make_vehicle, relate_to_own,
                 [dict(x=-3.0, y=-5.0, heading=0.0, speed=48.0)],
                 speed=50.0)
    assert result['left_warning'] is True


def test_creeping_traffic_below_the_movement_floor_does_not_warn(
        bsw, make_own_vehicle, make_vehicle, relate_to_own):
    """Both cars nearly stopped: nobody is changing lanes at 3 km/h."""
    result = run(bsw, make_own_vehicle, make_vehicle, relate_to_own,
                 [dict(x=-3.0, y=-5.0, heading=0.0, speed=3.0)],
                 speed=3.0)
    assert warnings(result) == NO_WARNING


# ─── Hold time ───────────────────────────────────────────────────────────────

def test_the_warning_is_held_after_the_car_leaves(
        bsw, make_own_vehicle, make_vehicle, relate_to_own):
    """A single missed detection cycle must not blank the warning."""
    run(bsw, make_own_vehicle, make_vehicle, relate_to_own,
        [dict(x=-3.0, y=-5.0, heading=0.0, speed=50.0)], speed=50.0)
    assert bsw.left_warning is True

    # Car gone. At equal speed the hold time is clamped to HOLD_MAX_S.
    bsw.clock.advance(0.1)
    held = run(bsw, make_own_vehicle, make_vehicle, relate_to_own, [], speed=50.0)
    assert held['left_warning'] is True

    bsw.clock.advance(BlindSpotWarning.HOLD_MAX_S)
    released = run(bsw, make_own_vehicle, make_vehicle, relate_to_own, [], speed=50.0)
    assert released['left_warning'] is False


def test_hold_time_shrinks_with_relative_speed():
    """One vehicle length of relative travel, clamped to [HOLD_MIN, HOLD_MAX]."""
    hold = BlindSpotWarning._hold_time
    system = BlindSpotWarning.__new__(BlindSpotWarning)
    assert hold(system, 20.0, 20.0) == pytest.approx(BlindSpotWarning.HOLD_MAX_S)
    assert hold(system, 20.0, 40.0) == pytest.approx(BlindSpotWarning.HOLD_MIN_S)
    assert hold(system, 20.0, 24.0) == pytest.approx(4.5 / 4.0)


# ─── Acceptance: the pre-filter bounds the polygon count ─────────────────────

def test_no_polygon_is_built_for_cars_the_prefilter_rejects(
        bsw, make_own_vehicle, make_vehicle, relate_to_own):
    """known-issues #7: one shapely polygon per car per cycle, unconditionally.

    40 cars scattered beyond the corridor must now cost comparisons only.
    """
    far_away = [dict(x=float(120 + 5 * i), y=-10.0, heading=0.0, speed=50.0)
                for i in range(40)]
    result = run(bsw, make_own_vehicle, make_vehicle, relate_to_own, far_away, speed=50.0)

    assert warnings(result) == NO_WARNING
    assert bsw.polygons_built == 0


def test_only_the_surviving_car_costs_a_polygon(
        bsw, make_own_vehicle, make_vehicle, relate_to_own):
    others = [dict(x=-3.0, y=-5.0, heading=0.0, speed=50.0)]
    others += [dict(x=float(120 + 5 * i), y=-10.0, heading=0.0, speed=50.0)
               for i in range(39)]
    run(bsw, make_own_vehicle, make_vehicle, relate_to_own, others, speed=50.0)

    # Two own-car corridors, built once, plus one outline for the one car
    # that passed every gate.
    assert bsw.polygons_built == 3


def test_cars_ahead_are_rejected_by_the_side_gate(
        bsw, make_own_vehicle, make_vehicle, relate_to_own):
    ahead = [dict(x=float(i - 2), y=30.0, heading=0.0, speed=50.0) for i in range(5)]
    run(bsw, make_own_vehicle, make_vehicle, relate_to_own, ahead, speed=50.0)
    assert bsw.polygons_built == 0


def test_the_event_is_only_emitted_on_change(
        bsw, make_own_vehicle, make_vehicle, relate_to_own, recorder):
    events = recorder('blind_spot_warning_changed')
    for _ in range(4):
        run(bsw, make_own_vehicle, make_vehicle, relate_to_own,
            [dict(x=-3.0, y=-5.0, heading=0.0, speed=50.0)], speed=50.0)
    assert events.count('blind_spot_warning_changed') == 1


# --- Level 2: the acute warning ---------------------------------------------
#
# Level 1 says "somebody is there". Level 2 says "and you are about to hit
# them" - it blinks and it beeps, so it has to be right. The two ways in are
# the two the user named: coming too close, and driving into their path.


def test_driving_into_the_path_of_an_overtaking_car_raises_level_two(
        bsw, make_own_vehicle, make_vehicle, relate_to_own):
    """Swerving left at 60 km/h with a faster car 8 m back in the left lane."""
    result = run(bsw, make_own_vehicle, make_vehicle, relate_to_own,
                 [dict(x=-3.5, y=-8.0, heading=0.0, speed=70.0)],
                 heading=10.0, speed=60.0)
    assert result['left_level'] == 2
    assert result['right_level'] == 0


def test_the_turn_itself_is_the_signal_not_the_angle_it_has_reached(
        bsw, make_own_vehicle, make_vehicle, relate_to_own):
    """A decision worth knowing about, because it is also a limitation.

    A car in the next lane 8 m back, faster. What decides whether that is a
    warning is not where we are pointing - it is whether the wheel is moving.

    The alternative was a criterion that asked "are we in his lane soon" and
    "is he close behind" separately. It caught the held-angle case and it also
    caught ``simulation_tests`` scenario 25, where the two questions were true
    at two different moments and nothing was ever going to happen. Comparing
    moments that are not the same moment is the worse error of the two, so
    this is the trade that was made.
    """
    others = [dict(x=-3.5, y=-8.0, heading=0.0, speed=70.0)]

    # Straight, wheel still: nothing is happening.
    quiet = run(bsw, make_own_vehicle, make_vehicle, relate_to_own, others,
                heading=0.0, speed=60.0)
    assert quiet['left_level'] <= 1

    # Straight, wheel going over: the merge has started and it warns, although
    # the heading has not moved a degree yet.
    bsw.clock.advance(10.0)
    turning = run(bsw, make_own_vehicle, make_vehicle, relate_to_own, others,
                  heading=0.0, speed=60.0, ang_vel=yaw_units(15.0))
    assert turning['left_level'] == 2

    # Twenty degrees off the lane with the wheel held: told that, the
    # prediction has us out the other side before the other car arrives, and
    # it is right about what it was told. This is the limitation.
    bsw.clock.advance(10.0)
    held = run(bsw, make_own_vehicle, make_vehicle, relate_to_own, others,
               heading=20.0, speed=60.0)
    assert held['left_level'] <= 1


def test_driving_straight_beside_the_same_car_stays_at_level_one(
        bsw, make_own_vehicle, make_vehicle, relate_to_own):
    """Same two cars, same speeds - we are simply not going anywhere."""
    result = run(bsw, make_own_vehicle, make_vehicle, relate_to_own,
                 [dict(x=-3.5, y=-8.0, heading=0.0, speed=70.0)],
                 heading=0.0, speed=60.0)
    assert result['left_level'] == 1


def test_a_two_degree_wobble_is_not_a_lane_change(
        bsw, make_own_vehicle, make_vehicle, relate_to_own):
    """Steering noise must not blink and beep: at 2 degrees the next lane is
    still 50 m of travel away."""
    result = run(bsw, make_own_vehicle, make_vehicle, relate_to_own,
                 [dict(x=-3.5, y=-20.0, heading=0.0, speed=70.0)],
                 heading=2.0, speed=60.0)
    assert result['left_level'] <= 1


def test_the_acute_warning_picks_the_side_the_car_is_on(
        bsw, make_own_vehicle, make_vehicle, relate_to_own):
    result = run(bsw, make_own_vehicle, make_vehicle, relate_to_own,
                 [dict(x=3.5, y=-8.0, heading=0.0, speed=70.0)],
                 heading=-10.0, speed=60.0)
    assert result['right_level'] == 2
    assert result['left_level'] == 0


def test_crossing_traffic_is_not_a_blind_spot_case(
        bsw, make_own_vehicle, make_vehicle, relate_to_own):
    """90 degrees across our path belongs to the cross traffic warning; the
    acute heading gate (66 degrees) is what keeps the two apart."""
    result = run(bsw, make_own_vehicle, make_vehicle, relate_to_own,
                 [dict(x=-8.0, y=-3.0, heading=90.0, speed=50.0)],
                 speed=25.0)
    assert result['left_level'] == 0
    assert result['right_level'] == 0


# --- What the acute stages are not: the car in front, and standing still -----


def test_the_car_we_are_running_into_is_not_a_blind_spot_case(
        bsw, make_own_vehicle, make_vehicle, relate_to_own):
    """known-issues #52, the way it was reported: drive into the car ahead.

    ``_is_plain_following`` used to be the only thing keeping a lead car out
    of the acute stages, and it stops holding the moment of impact - both cars
    rotate, the heading difference passes two degrees, and the pair became
    "interesting" again. Which side it was on was then decided by the sign of
    a cross product that is essentially zero straight ahead.
    """
    result = run(bsw, make_own_vehicle, make_vehicle, relate_to_own,
                 [dict(x=0.3, y=5.0, heading=6.0, speed=40.0)],
                 heading=0.0, speed=55.0)

    assert result['left_level'] == 0
    assert result['right_level'] == 0


def test_a_lead_car_never_picks_a_side_however_it_is_offset(
        bsw, make_own_vehicle, make_vehicle, relate_to_own):
    """The "sometimes left, sometimes right, sometimes both" of #51.

    The same collision with the lead car a few centimetres to either side. A
    criterion that decides on the sign of the lateral offset flips here; this
    one does not, because "ahead" is not a blind spot on either side.
    """
    for lateral in (-0.4, -0.05, 0.05, 0.4):
        bsw.clock.advance(10.0)          # let any hold time expire
        result = run(bsw, make_own_vehicle, make_vehicle, relate_to_own,
                     [dict(x=lateral, y=4.6, heading=8.0, speed=35.0)],
                     heading=0.0, speed=55.0)
        assert result['left_level'] == 0, lateral
        assert result['right_level'] == 0, lateral


def test_a_car_drawing_level_with_us_is_still_an_acute_warning(
        bsw, make_own_vehicle, make_vehicle, relate_to_own):
    """...and the gate must not cost the case it exists beside.

    Being overtaken, the other car's nose reaches past ours long before the
    conflict is over. The bar is its *centre* against our front bumper, so a
    car alongside still warns while we turn into it.
    """
    result = run(bsw, make_own_vehicle, make_vehicle, relate_to_own,
                 [dict(x=-3.5, y=1.5, heading=0.0, speed=70.0)],
                 heading=8.0, speed=60.0, ang_vel=yaw_units(15.0))

    assert result['left_level'] >= 2


def test_standing_still_is_never_an_acute_warning(
        bsw, make_own_vehicle, make_vehicle, relate_to_own):
    """known-issues #53: at a stop, everything that drove past beeped.

    Our outline does not move over the prediction horizon, so every contact
    the window finds comes from the other car alone - and braking a car that
    already stands is not an answer to anything.
    """
    result = run(bsw, make_own_vehicle, make_vehicle, relate_to_own,
                 [dict(x=-3.2, y=-6.0, heading=0.0, speed=35.0)],
                 heading=0.0, speed=0.0)

    assert result['left_level'] <= 1
    assert result['deceleration'] == 0.0


def test_the_same_car_warns_again_once_we_pull_away(
        bsw, make_own_vehicle, make_vehicle, relate_to_own):
    """The merge out of a junction is exactly why the floor is 1 km/h, not 5.

    Same scene twice: stopped at the give-way line, then rolling out of it at
    8 km/h with the wheel going over. The first is somebody else's traffic
    driving past; the second is us putting our car in front of it.
    """
    others = [dict(x=-3.2, y=-14.0, heading=0.0, speed=55.0)]
    quiet = run(bsw, make_own_vehicle, make_vehicle, relate_to_own, others,
                heading=15.0, speed=0.0, ang_vel=yaw_units(15.0))
    assert quiet['left_level'] <= 1
    assert quiet['deceleration'] == 0.0

    bsw.clock.advance(10.0)
    moving = run(bsw, make_own_vehicle, make_vehicle, relate_to_own, others,
                 heading=15.0, speed=8.0, ang_vel=yaw_units(15.0))
    assert moving['left_level'] >= 2


def test_level_one_still_works_while_we_are_stopped(
        bsw, make_own_vehicle, make_vehicle, relate_to_own):
    """Only the blinking and the beeping go away, not the information.

    Somebody sitting in the mirror's blind spot is worth knowing about before
    the driver pulls out, which is precisely the moment they are stopped.
    """
    result = run(bsw, make_own_vehicle, make_vehicle, relate_to_own,
                 [dict(x=-3.2, y=-4.0, heading=0.0, speed=20.0)],
                 heading=0.0, speed=0.0)

    assert result['left_level'] == 1


# --- Level 3: the braking demand --------------------------------------------


def merging(bsw, make_own_vehicle, make_vehicle, relate_to_own,
            own_kmh=25.0, heading=15.0, other_kmh=55.0, gap=15.0):
    """Turning left into flowing traffic: a faster car ``gap`` m back, left."""
    return run(bsw, make_own_vehicle, make_vehicle, relate_to_own,
               [dict(x=-3.5, y=-gap, heading=0.0, speed=other_kmh)],
               heading=heading, speed=own_kmh)


def test_merging_into_faster_traffic_asks_for_braking(
        bsw, make_own_vehicle, make_vehicle, relate_to_own):
    """The case the level exists for: we pull out at 25 km/h in front of
    something doing 55, and the only thing that still prevents it is not
    going any further."""
    result = merging(bsw, make_own_vehicle, make_vehicle, relate_to_own)
    assert result['left_level'] == 3
    assert result['deceleration'] >= EmergencyBrake.ENGAGE_DECELERATION_MS2


def test_above_the_speed_floor_it_warns_but_does_not_brake(
        bsw, make_own_vehicle, make_vehicle, relate_to_own):
    """At 45 km/h a lane change is corrected with the wheel. A full stop in
    moving traffic would only move the problem to the car behind."""
    result = merging(bsw, make_own_vehicle, make_vehicle, relate_to_own,
                     own_kmh=45.0, other_kmh=75.0)
    assert result['left_level'] == 2
    assert result['deceleration'] == 0.0


def test_a_car_that_is_barely_faster_is_not_braked_for(
        bsw, make_own_vehicle, make_vehicle, relate_to_own):
    """"Deutlich schneller": 5 km/h of difference is a normal merge."""
    result = merging(bsw, make_own_vehicle, make_vehicle, relate_to_own,
                     other_kmh=30.0)
    assert result['left_level'] < 3
    assert result['deceleration'] == 0.0


def test_nothing_is_braked_for_when_we_are_already_in_his_lane(
        bsw, make_own_vehicle, make_vehicle, relate_to_own):
    """The asymmetry against the cross traffic warning, and it matters.

    He is behind us and faster. Once our car is in his lane, braking does not
    take us out of it - it only lengthens the time he needs to reach us and
    raises the speed he arrives with. So the warning stays and the demand
    goes.
    """
    result = run(bsw, make_own_vehicle, make_vehicle, relate_to_own,
                 [dict(x=-3.4, y=-12.0, heading=0.0, speed=55.0)],
                 heading=5.0, speed=25.0, x=-3.4)
    assert result['left_level'] + result['right_level'] > 0
    assert result['deceleration'] == 0.0


def test_the_demand_is_published_every_cycle_with_its_source(
        bsw, make_own_vehicle, make_vehicle, relate_to_own, recorder):
    events = recorder('needed_deceleration_update')
    for _ in range(3):
        merging(bsw, make_own_vehicle, make_vehicle, relate_to_own)

    assert events.count('needed_deceleration_update') == 3
    assert events.last('needed_deceleration_update')['source'] == 'blind_spot'


def test_a_quiet_road_publishes_a_zero_demand(
        bsw, make_own_vehicle, make_vehicle, relate_to_own, recorder):
    """The contract ``EmergencyBrake`` relies on: a demand of zero arrives,
    so a demand that stops arriving means something else."""
    events = recorder('needed_deceleration_update')
    run(bsw, make_own_vehicle, make_vehicle, relate_to_own, [], speed=50.0)
    assert events.last('needed_deceleration_update') == {
        'deceleration': 0.0, 'source': 'blind_spot'}


def test_the_level_reaches_the_ui_in_the_event(
        bsw, make_own_vehicle, make_vehicle, relate_to_own, recorder):
    """``left``/``right`` stay for subscribers that predate the levels."""
    events = recorder('blind_spot_warning_changed')
    merging(bsw, make_own_vehicle, make_vehicle, relate_to_own)
    assert events.last('blind_spot_warning_changed') == {
        'left': True, 'right': False, 'left_level': 3, 'right_level': 0}

def test_the_yaw_rate_makes_the_acute_warning_arrive_before_the_heading_does(
        bsw, make_own_vehicle, make_vehicle, relate_to_own):
    """Measured in ``simulation_tests`` scenario 22 and fixed here.

    One degree of heading across a 1.8 m gap is six seconds of travel, so the
    heading alone says "no conflict" - while the driver is already 20 deg/s
    into the turn and a second from contact. Same instant, same heading, only
    ``ang_vel`` differs.
    """
    others = [dict(x=3.5, y=-15.0, heading=0.0, speed=55.0)]
    straight = run(bsw, make_own_vehicle, make_vehicle, relate_to_own, others,
                   heading=-1.0, speed=25.0)
    assert straight['right_level'] <= 1

    bsw.clock.advance(10.0)
    turning = run(bsw, make_own_vehicle, make_vehicle, relate_to_own, others,
                  heading=-1.0, speed=25.0, ang_vel=YAW_20_DEG_S_RIGHT)
    assert turning['right_level'] >= 2


def test_cornering_beside_another_car_is_not_an_acute_warning(
        bsw, make_own_vehicle, make_vehicle, relate_to_own):
    """``simulation_tests`` scenario 24, and it took three runs to see it.

    Two cars side by side at 107 km/h through a long bend, 5.5 m apart, with a
    2.4 degree heading difference between them - the rest of the corner one
    has already taken and the other has not. Extrapolated for 2.5 s that is
    70 m of travel, over which a couple of degrees is several metres of
    lateral error, and the prediction has them meeting in 1.9 s. They did not
    meet; they drove on like that.

    What keeps it quiet is ``STEADY_TTC_S``: without a relative yaw rate that
    shows somebody actually steering into somebody, the warning only looks
    1.5 s ahead, where that error is under half a metre.

    The *relative* yaw here is 2 deg/s. Over the whole run it never exceeded
    10.6, which is the measurement ``path_conflict._MIN_RELATIVE_YAW_RATE``
    sits above.
    """
    others = [dict(x=-5.5, y=0.5, heading=2.4, speed=107.0,
                   ang_vel=yaw_units(10.4))]
    result = run(bsw, make_own_vehicle, make_vehicle, relate_to_own, others,
                 heading=0.0, speed=107.0, ang_vel=yaw_units(8.7))
    assert result['left_level'] <= 1
    assert result['deceleration'] == 0.0


def test_turning_in_behind_traffic_that_has_already_passed_is_quiet(
        bsw, make_own_vehicle, make_vehicle, relate_to_own):
    """``simulation_tests`` scenario 25, at the frame it used to fire.

    The numbers are LFS's own: we are turning right at 22 km/h with 36 deg/s
    on the wheel, and an RB4 doing 93 km/h is drawing level 7 m away. We are
    indeed about to be in its lane - in 1.4 s - and it is indeed close behind
    - 0.06 s. Those are two different moments, and by the first one it is 27 m
    down the road. Nothing happens, and nothing should be announced.
    """
    others = [dict(x=-71.06, y=87.61, heading=0.02, speed=92.8,
                   cname=b"RB4", ang_vel=-1)]
    result = run(bsw, make_own_vehicle, make_vehicle, relate_to_own, others,
                 x=-75.58, y=93.25, heading=357.08, speed=22.3,
                 cname=b"RB4", ang_vel=-623)
    assert result['right_level'] <= 1
    assert result['deceleration'] == 0.0


def test_the_full_warning_time_needs_a_manoeuvre_behind_it(
        bsw, make_own_vehicle, make_vehicle, relate_to_own):
    """``STEADY_TTC_S`` against ``ACUTE_TTC_S``, on one geometry.

    A car closing on a shallow angle two seconds out. Held at that angle it is
    a prediction from a couple of degrees over 2 s, and the warning waits; with
    the wheel visibly going over it is a manoeuvre, and the warning comes at
    once. The cost of the first case is one second of warning time, and there
    is still a second left before contact.
    """
    others = [dict(x=-3.5, y=-8.0, heading=0.0, speed=70.0)]

    steady = run(bsw, make_own_vehicle, make_vehicle, relate_to_own, others,
                 heading=2.0, speed=60.0)
    assert steady['left_level'] <= 1

    bsw.clock.advance(10.0)
    manoeuvring = run(bsw, make_own_vehicle, make_vehicle, relate_to_own,
                      others, heading=2.0, speed=60.0,
                      ang_vel=yaw_units(15.0))
    assert manoeuvring['left_level'] == 2


def test_racing_side_by_side_through_a_bend_is_never_an_acute_warning(
        bsw, make_own_vehicle, make_vehicle, relate_to_own):
    """Scenario 24 again, from the run where the two cars were closer.

    3.4 m apart instead of 5.5 at 105 km/h, and the same persistent 2.5
    degrees between them now predicts contact in 1.0 s - inside even the
    shortened horizon. Both cars are cornering (8.1 and 6.4 deg/s) and neither
    is steering into the other, so the angle between them *is* the bend, and
    nothing is extrapolated at all: only a real overlap would warn.

    Driving alongside somebody through a corner must not blink and beep, or
    the driver switches the system off and it protects nothing.
    """
    others = [dict(x=-3.4, y=0.3, heading=0.0, speed=105.0,
                   ang_vel=yaw_units(6.4))]
    result = run(bsw, make_own_vehicle, make_vehicle, relate_to_own, others,
                 heading=2.5, speed=105.0, ang_vel=yaw_units(8.1))
    assert result['left_level'] <= 1
    assert result['deceleration'] == 0.0


def test_a_straight_road_still_gets_the_steady_warning(
        bsw, make_own_vehicle, make_vehicle, relate_to_own):
    """The corner rule must not silence the case it is not about.

    Same convergence, nobody cornering: one car is drifting into the other on
    a straight, and that is worth a warning even without a yaw rate to prove
    a manoeuvre.
    """
    others = [dict(x=-3.4, y=0.3, heading=0.0, speed=105.0)]
    result = run(bsw, make_own_vehicle, make_vehicle, relate_to_own, others,
                 heading=2.5, speed=105.0)
    assert result['left_level'] == 2
