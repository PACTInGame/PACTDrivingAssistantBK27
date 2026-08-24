"""Blind spot warning: trigger condition, geometry and hot-path cost (WP8).

Coordinates below are the ones a driver would describe: the own car sits at
the origin pointing **north** (LFS heading 0 = +Y), so -X is its left and -Y
is behind it. ``relate_to_own`` fills in ``distance_to_player`` /
``angle_to_player`` exactly as ``VehicleManager._apply_frame`` does per MCI
frame, because the pre-filter reads both.
"""

import pytest
from shapely import Polygon

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
    assert events.last('blind_spot_warning_changed') == {'left': True, 'right': False}


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
    assert in_lane == {'left_warning': False, 'right_warning': False}

    bsw.clock.advance(10.0)
    next_lane = run(bsw, make_own_vehicle, make_vehicle, relate_to_own,
                    [dict(x=-3.0, y=-60.0, heading=0.0, speed=50.0)],
                    speed=50.0)
    assert next_lane == {'left_warning': False, 'right_warning': False}


def test_a_car_directly_behind_is_never_in_the_blind_spot(
        bsw, make_own_vehicle, make_vehicle, relate_to_own):
    """The corridor starts 1 m off the axis; a car in our own lane misses it."""
    result = run(bsw, make_own_vehicle, make_vehicle, relate_to_own,
                 [dict(x=0.0, y=-3.0, heading=0.0, speed=50.0)],
                 speed=50.0)
    assert result == {'left_warning': False, 'right_warning': False}


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
    assert result == {'left_warning': False, 'right_warning': False}


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

    assert result == {'left_warning': False, 'right_warning': False}
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
