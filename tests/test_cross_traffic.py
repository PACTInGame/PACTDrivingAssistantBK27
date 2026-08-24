"""Cross traffic warning: gating, side and the size-aware arrival window (WP8).

Geometry convention (``reference/conventions.md`` §1): X east, Y north,
right-handed, LFS headings anticlockwise from +Y. So heading 0° drives north,
90° drives west, 270° drives east. Every expectation below is derived from
that, not observed.
"""

import pytest

from assistance.cross_traffic_warning import (
    CrossTrafficWarning, _compute_side, _direction_vector, _find_intersection)

KMH_TO_MS = 0.277778


@pytest.fixture
def ctw(bus, settings):
    return CrossTrafficWarning(bus, settings)


def process(ctw, own, others):
    vehicles = {vehicle.data.player_id: vehicle for vehicle in others}
    return ctw.process(own, vehicles)


# ─── The coordinate system the comments used to get wrong ────────────────────

@pytest.mark.parametrize("heading_deg, expected", [
    (0.0, (0.0, 1.0)),      # north  -> +Y
    (90.0, (-1.0, 0.0)),    # 90° anticlockwise from north -> west
    (180.0, (0.0, -1.0)),   # south
    (270.0, (1.0, 0.0)),    # east
])
def test_direction_vector_is_anticlockwise_from_north(heading_deg, expected):
    """known-issues #16: the docstring claimed Y grows south and headings run
    clockwise. The code never did that - this pins what it really does."""
    from conftest import lfs_heading
    dx, dy = _direction_vector(lfs_heading(heading_deg))
    assert (dx, dy) == pytest.approx(expected, abs=1e-3)


def test_compute_side_is_plain_right_handed_maths():
    north = (0.0, 1.0)
    assert _compute_side(*north, 0.0, 0.0, 10.0, 0.0) == 'right'    # east of us
    assert _compute_side(*north, 0.0, 0.0, -10.0, 0.0) == 'left'    # west of us


def test_find_intersection_rejects_paths_already_behind_us():
    # Both driving away from the crossing point.
    assert _find_intersection(0.0, 0.0, 0.0, 1.0, 10.0, 10.0, 1.0, 0.0) is None
    # Parallel paths never intersect.
    assert _find_intersection(0.0, 0.0, 0.0, 1.0, 10.0, 0.0, 0.0, 1.0) is None


# ─── Acceptance: perpendicular paths warn, with the correct side ─────────────

def junction(make_own_vehicle, make_vehicle, own_gap_m, other_gap_m,
             own_kmh=36.0, other_kmh=36.0, side='right', other_cname=b"XFG",
             own_gear=3):
    """Own car south of the crossing driving north, other car crossing it.

    ``side='right'`` puts the other car east of the crossing driving west,
    ``side='left'`` puts it west driving east. The crossing is the origin.
    """
    own = make_own_vehicle(plid=1, x=0.0, y=-own_gap_m, heading=0.0,
                           speed=own_kmh, gear=own_gear)
    if side == 'right':
        other = make_vehicle(plid=2, x=other_gap_m, y=0.0, heading=90.0,
                             speed=other_kmh, cname=other_cname)
    else:
        other = make_vehicle(plid=2, x=-other_gap_m, y=0.0, heading=270.0,
                             speed=other_kmh, cname=other_cname)
    return own, [other]


def test_perpendicular_paths_warn_from_the_right(ctw, make_own_vehicle, make_vehicle,
                                                 recorder):
    events = recorder('cross_traffic_warning_changed')
    # 36 km/h = 10 m/s, 20 m to the crossing -> 2.0 s, inside the medium
    # visual threshold of 2.5 s and outside the acoustic one of 1.5 s.
    own, others = junction(make_own_vehicle, make_vehicle, 20.0, 20.0)
    result = process(ctw, own, others)

    assert result['level'] == 1
    assert result['side'] == 'right'
    assert result['ttc'] == pytest.approx(2.0, abs=0.05)
    assert events.last('cross_traffic_warning_changed') == {'level': 1, 'side': 'right'}


def test_perpendicular_paths_warn_from_the_left(ctw, make_own_vehicle, make_vehicle):
    own, others = junction(make_own_vehicle, make_vehicle, 20.0, 20.0, side='left')
    result = process(ctw, own, others)
    assert result['level'] == 1
    assert result['side'] == 'left'


def test_a_close_junction_raises_the_acoustic_level(ctw, make_own_vehicle, make_vehicle):
    # 12 m at 10 m/s -> 1.2 s, below the medium acoustic threshold of 1.5 s.
    own, others = junction(make_own_vehicle, make_vehicle, 12.0, 12.0)
    assert process(ctw, own, others)['level'] == 2


def test_parallel_paths_produce_no_warning(ctw, make_own_vehicle, make_vehicle):
    """Same direction and oncoming are both below MIN_CROSSING_ANGLE_DEG."""
    own = make_own_vehicle(plid=1, x=0.0, y=0.0, heading=0.0, speed=50.0, gear=3)
    same_lane = make_vehicle(plid=2, x=3.0, y=20.0, heading=0.0, speed=50.0)
    oncoming = make_vehicle(plid=3, x=-3.0, y=20.0, heading=180.0, speed=50.0)

    assert process(ctw, own, [same_lane, oncoming]) == {
        'level': 0, 'side': None, 'ttc': float('inf')}


def test_a_far_away_junction_is_ignored(ctw, make_own_vehicle, make_vehicle):
    own, others = junction(make_own_vehicle, make_vehicle,
                           CrossTrafficWarning.MAX_INTERSECTION_DISTANCE + 20.0,
                           CrossTrafficWarning.MAX_INTERSECTION_DISTANCE + 20.0,
                           own_kmh=120.0, other_kmh=120.0)
    assert process(ctw, own, others)['level'] == 0


# ─── Acceptance: the gear gate is gone ───────────────────────────────────────

def test_neutral_gear_no_longer_suppresses_the_warning(ctw, make_own_vehicle, make_vehicle):
    """The old gate was ``own_vehicle.gear <= 1``.

    Rolling towards a junction in neutral - or with no OutGauge gear at all,
    which reads as 0 - switched cross traffic warning off completely.
    """
    for gear in (0, 1):
        ctw.current_warning_level, ctw.current_side = 0, None
        own, others = junction(make_own_vehicle, make_vehicle, 20.0, 20.0,
                               own_gear=gear)
        assert process(ctw, own, others)['level'] == 1, f"gear={gear}"


def test_reversing_suppresses_the_warning(ctw, make_own_vehicle, make_vehicle):
    """Driving backwards makes the heading-based direction vector meaningless."""
    own = make_own_vehicle(plid=1, x=0.0, y=-20.0, heading=0.0, direction=180.0,
                           speed=36.0, gear=0)
    other = make_vehicle(plid=2, x=20.0, y=0.0, heading=90.0, speed=36.0)
    assert process(ctw, own, [other])['level'] == 0


def test_standing_still_suppresses_the_warning(ctw, make_own_vehicle, make_vehicle):
    own = make_own_vehicle(plid=1, x=0.0, y=-20.0, heading=0.0, speed=2.0, gear=2)
    other = make_vehicle(plid=2, x=20.0, y=0.0, heading=90.0, speed=36.0)
    assert process(ctw, own, [other])['level'] == 0


def test_a_disabled_system_stays_silent(bus, make_settings, make_own_vehicle, make_vehicle):
    system = CrossTrafficWarning(bus, make_settings(cross_traffic_warning=False))
    own, others = junction(make_own_vehicle, make_vehicle, 20.0, 20.0)
    assert process(system, own, others)['level'] == 0


# ─── Acceptance: the size-aware arrival window ───────────────────────────────

def test_arrival_window_grows_with_vehicle_size_and_shrinks_with_speed(ctw):
    """Two cars at 10 m/s barely need more than the base tolerance..."""
    fast_pair = ctw._arrival_window(3.7, 1.7, 10.0, 'XFG', 10.0)
    assert fast_pair == pytest.approx(
        CrossTrafficWarning.ARRIVAL_TIME_TOLERANCE
        + (3.7 + 1.7) / 10.0 / 2 + (3.7 + 1.7) / 10.0 / 2, abs=1e-6)

    # ...while a 5.0 m car crossing at 10 km/h blocks the junction far longer.
    slow_long = ctw._arrival_window(3.7, 1.7, 10.0, 'FXR', 10.0 * KMH_TO_MS)
    assert slow_long > fast_pair
    assert slow_long > 1.5


def test_unknown_car_names_fall_back_instead_of_raising(ctw):
    """Vehicle mods carry an arbitrary CName (``conventions.md`` §4)."""
    known = ctw._arrival_window(3.7, 1.7, 10.0, 'XFG', 10.0)
    modded = ctw._arrival_window(3.7, 1.7, 10.0, 'q7Xk', 10.0)
    assert modded > 0 and modded != known


def test_a_long_slow_crossing_vehicle_is_no_longer_missed(
        ctw, make_own_vehicle, make_vehicle):
    """The plan's case: point-sized vehicles with a fixed +-0.5 s tolerance.

    We arrive at the crossing in 2.0 s, an FXR crossing at 10 km/h arrives in
    3.4 s. The old fixed tolerance rejected the 1.4 s difference; the real
    5.0 m long car occupies the junction for 2.4 s, so it is still there when
    we get to it.
    """
    own_gap, own_kmh = 20.0, 36.0             # 10 m/s -> 2.0 s
    other_kmh = 10.0                          # 2.78 m/s
    other_gap = 3.4 * other_kmh * KMH_TO_MS   # -> 3.4 s

    own, others = junction(make_own_vehicle, make_vehicle, own_gap, other_gap,
                           own_kmh=own_kmh, other_kmh=other_kmh,
                           other_cname=b"FXR")

    time_diff = 3.4 - 2.0
    assert time_diff > CrossTrafficWarning.ARRIVAL_TIME_TOLERANCE
    assert time_diff <= ctw._arrival_window(3.7, 1.7, own_kmh * KMH_TO_MS,
                                            'FXR', other_kmh * KMH_TO_MS)
    assert process(ctw, own, others)['level'] == 1


def test_a_vehicle_that_is_long_gone_still_does_not_warn(
        ctw, make_own_vehicle, make_vehicle):
    """The window is wider, not infinite."""
    own_kmh, other_kmh = 36.0, 50.0
    own, others = junction(make_own_vehicle, make_vehicle,
                           20.0, 0.5 * other_kmh * KMH_TO_MS,
                           own_kmh=own_kmh, other_kmh=other_kmh)
    assert process(ctw, own, others)['level'] == 0


# ─── Event contract ──────────────────────────────────────────────────────────

def test_the_event_is_only_emitted_on_change(ctw, make_own_vehicle, make_vehicle,
                                             recorder):
    events = recorder('cross_traffic_warning_changed')
    own, others = junction(make_own_vehicle, make_vehicle, 20.0, 20.0)
    for _ in range(3):
        process(ctw, own, others)
    assert events.count('cross_traffic_warning_changed') == 1
