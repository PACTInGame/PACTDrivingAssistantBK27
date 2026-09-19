"""Cross traffic warning: gating, side and the size-aware arrival window (WP8).

Geometry convention (``reference/conventions.md`` §1): X east, Y north,
right-handed, LFS headings anticlockwise from +Y. So heading 0° drives north,
90° drives west, 270° drives east. Every expectation below is derived from
that, not observed.
"""

import pytest

from assistance.emergency_brake import EmergencyBrake
from assistance.cross_traffic_warning import (
    CrossTrafficWarning, _compute_side, _direction_vector)
from assistance.path_conflict import (
    INF, PANIC_DECELERATION_MS2, body_from, contact_window, free_distance)

KMH_TO_MS = 0.277778


@pytest.fixture
def ctw(bus, settings):
    return CrossTrafficWarning(bus, settings)


def process(ctw, own, others):
    """One pass, with the per-frame relations the VehicleManager would fill in.

    ``distance_to_player`` is the system's range gate, and a ``Vehicle`` built
    in isolation has it at 0 - which would let every car through, however far
    away it is (``conftest.relate_to_own`` says the same thing for BSW).
    """
    own_data = own.data
    vehicles = {}
    for vehicle in others:
        vehicle.update_distance_to_player(own_data.x, own_data.y, own_data.z)
        vehicle.update_angle_to_player(own_data.x, own_data.y, own_data.heading)
        vehicles[vehicle.data.player_id] = vehicle
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


def test_free_distance_is_infinite_for_a_crossing_already_behind_us(
        make_own_vehicle, make_vehicle):
    """``inf`` means "never, or already through" - both say "do not brake"."""
    own = body_from(make_own_vehicle(x=0.0, y=0.0, heading=0.0, speed=36.0).data)
    # Crossing 20 m *behind* us, driving away.
    passed = body_from(make_vehicle(x=20.0, y=-20.0, heading=90.0,
                                    speed=36.0).data)
    assert free_distance(own, passed) == INF
    # Parallel: never enters our way at all.
    parallel = body_from(make_vehicle(x=20.0, y=0.0, heading=0.0,
                                      speed=36.0).data)
    assert free_distance(own, parallel) == INF


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
    # 2.0 s would be the time for the two *centres* to meet. The two bodies
    # touch 0.27 s earlier: half our 3.7 m length plus half their 1.7 m width,
    # at 10 m/s.
    assert result['ttc'] == pytest.approx(1.73, abs=0.05)
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
        'level': 0, 'side': None, 'ttc': INF, 'deceleration': 0.0}


def test_a_far_away_junction_is_ignored(ctw, make_own_vehicle, make_vehicle):
    own, others = junction(make_own_vehicle, make_vehicle,
                           CrossTrafficWarning.MAX_RANGE_M + 20.0,
                           CrossTrafficWarning.MAX_RANGE_M + 20.0,
                           own_kmh=120.0, other_kmh=120.0)
    result = process(ctw, own, others)
    assert result['level'] == 0
    # ...and it asks for no braking either, although the geometry really does
    # predict contact in 3.5 s. The range gate is what keeps it quiet.
    assert result['deceleration'] == 0.0


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


# --- Acceptance: both vehicles are bodies, not points -----------------------

def test_a_long_slow_crossing_vehicle_is_no_longer_missed(
        ctw, make_own_vehicle, make_vehicle):
    """The plan's case, and the reason the point model had to go.

    We reach the crossing in 2.0 s; an FXR crossing at 10 km/h reaches it in
    3.4 s. As two points that is a 1.4 s miss. As two bodies it is a hit: the
    5.0 m car needs (5.0 + 1.7) / 2.78 = 2.4 s to clear our lane, so it is
    still in it when we arrive.
    """
    own_gap, own_kmh = 20.0, 36.0             # 10 m/s -> 2.0 s
    other_kmh = 10.0                          # 2.78 m/s
    other_gap = 3.4 * other_kmh * KMH_TO_MS   # -> 3.4 s

    own, others = junction(make_own_vehicle, make_vehicle, own_gap, other_gap,
                           own_kmh=own_kmh, other_kmh=other_kmh,
                           other_cname=b"FXR")

    assert process(ctw, own, others)['level'] == 1


def test_a_vehicle_that_is_long_gone_still_does_not_warn(
        ctw, make_own_vehicle, make_vehicle):
    """Being a body makes the window wider, not infinite."""
    own_kmh, other_kmh = 36.0, 50.0
    own, others = junction(make_own_vehicle, make_vehicle,
                           20.0, 0.5 * other_kmh * KMH_TO_MS,
                           own_kmh=own_kmh, other_kmh=other_kmh)
    result = process(ctw, own, others)
    assert result['level'] == 0
    assert result['deceleration'] == 0.0


def test_vehicle_length_moves_the_contact_time(make_own_vehicle, make_vehicle):
    """A longer crossing car is reached sooner and blocks the lane longer."""
    own = body_from(make_own_vehicle(x=0.0, y=-20.0, heading=0.0,
                                     speed=36.0).data)
    small = body_from(make_vehicle(x=20.0, y=0.0, heading=90.0, speed=36.0,
                                   cname=b"UF1").data)
    large = body_from(make_vehicle(x=20.0, y=0.0, heading=90.0, speed=36.0,
                                   cname=b"FXR").data)
    assert contact_window(own, large)[0] < contact_window(own, small)[0]
    assert contact_window(own, large)[1] > contact_window(own, small)[1]


def test_an_unknown_car_name_falls_back_instead_of_raising(
        ctw, make_own_vehicle, make_vehicle):
    """Vehicle mods carry an arbitrary CName (``conventions.md`` section 4)."""
    own, others = junction(make_own_vehicle, make_vehicle, 20.0, 20.0,
                           other_cname=b"q7Xk")
    assert process(ctw, own, others)['level'] == 1


# --- Acceptance: the braking demand -----------------------------------------

DECELERATION_EVENT = 'needed_deceleration_update'


def test_a_distant_junction_asks_for_no_meaningful_braking(
        ctw, make_own_vehicle, make_vehicle):
    """20 m at 10 m/s: 3.5 m/s2 is ordinary braking, well below AEB's 6.0."""
    own, others = junction(make_own_vehicle, make_vehicle, 20.0, 20.0)
    result = process(ctw, own, others)
    assert result['deceleration'] == pytest.approx(3.5, abs=0.2)
    assert result['deceleration'] < EmergencyBrake.ENGAGE_DECELERATION_MS2


def test_the_demand_crosses_the_engage_threshold_as_the_junction_closes(
        ctw, make_own_vehicle, make_vehicle):
    """The point of the whole exercise: it engages late, and only once.

    The demand is v^2 / 2s, so it rises as the remaining distance is used up.
    Somewhere between 20 m and 12 m at 36 km/h it passes 6 m/s2 - that is the
    last moment at which the car can still be stopped short of the other
    one's path, and therefore the right moment to take over.
    """
    demands = []
    for gap in (30.0, 25.0, 20.0, 16.0, 12.0):
        ctw.current_warning_level, ctw.current_side = 0, None
        own, others = junction(make_own_vehicle, make_vehicle, gap, gap)
        demands.append(process(ctw, own, others)['deceleration'])

    assert demands == sorted(demands), demands
    assert demands[0] < EmergencyBrake.ENGAGE_DECELERATION_MS2
    assert demands[-1] > EmergencyBrake.ENGAGE_DECELERATION_MS2


def test_no_braking_for_a_crossing_we_will_clear_first(
        ctw, make_own_vehicle, make_vehicle):
    """We are 12 m out at 36 km/h, they are 40 m out at the same speed.

    We are through the junction long before they arrive, so there is nothing
    to brake for - although the distance alone would demand 7.9 m/s2.
    """
    own, others = junction(make_own_vehicle, make_vehicle, 12.0, 40.0)
    result = process(ctw, own, others)
    assert result['level'] == 0
    assert result['deceleration'] == 0.0


def test_no_braking_for_a_crossing_that_is_already_past(
        ctw, make_own_vehicle, make_vehicle):
    """Their path crosses ours behind us: nothing in front to stop for."""
    own = make_own_vehicle(plid=1, x=0.0, y=0.0, heading=0.0, speed=36.0)
    behind = make_vehicle(plid=2, x=20.0, y=-20.0, heading=90.0, speed=36.0)
    assert process(ctw, own, [behind])['deceleration'] == 0.0


def test_the_demand_is_published_every_cycle_with_its_source(
        ctw, make_own_vehicle, make_vehicle, recorder):
    """``EmergencyBrake`` tells the sources apart by this key, and a demand
    that stops arriving has to be distinguishable from one of zero."""
    events = recorder(DECELERATION_EVENT)
    own, others = junction(make_own_vehicle, make_vehicle, 12.0, 12.0)
    for _ in range(3):
        process(ctw, own, others)

    assert events.count(DECELERATION_EVENT) == 3
    last = events.last(DECELERATION_EVENT)
    assert last['source'] == 'cross_traffic'
    assert last['deceleration'] > EmergencyBrake.ENGAGE_DECELERATION_MS2


def test_a_disabled_system_publishes_a_zero_demand(
        bus, make_settings, make_own_vehicle, make_vehicle, recorder):
    """Switching the warning off must not strand the last demand it sent."""
    events = recorder(DECELERATION_EVENT)
    system = CrossTrafficWarning(bus, make_settings(cross_traffic_warning=False))
    own, others = junction(make_own_vehicle, make_vehicle, 12.0, 12.0)
    process(system, own, others)
    assert events.last(DECELERATION_EVENT) == {'deceleration': 0.0,
                                               'source': 'cross_traffic'}


# ─── Event contract ──────────────────────────────────────────────────────────

def test_the_event_is_only_emitted_on_change(ctw, make_own_vehicle, make_vehicle,
                                             recorder):
    events = recorder('cross_traffic_warning_changed')
    own, others = junction(make_own_vehicle, make_vehicle, 20.0, 20.0)
    for _ in range(3):
        process(ctw, own, others)
    assert events.count('cross_traffic_warning_changed') == 1


def test_no_braking_for_a_car_overtaking_us_from_behind(
        ctw, make_own_vehicle, make_vehicle):
    """20 degrees is the lower end of "crossing", and a car overtaking us
    close alongside at that angle looks exactly like it.

    The numbers are the ones LFS reported in ``simulation_tests`` scenario 22
    at the moment the overtaking car drew level: 22.5 degrees of heading
    difference, 2.7 m apart, it doing 87 km/h and us 13 while turning into its
    lane. Warning about that is fine; braking is not. It is behind us and
    faster, so braking cannot take us out of its way, only lengthen its
    approach (``reference/control-intervention.md``, the table of sources).
    The blind spot warning owns that geometry and decides it correctly.
    """
    own = make_own_vehicle(plid=1, x=-73.80, y=82.08, heading=337.5,
                           speed=12.8, cname=b"RB4")
    overtaking = make_vehicle(plid=2, x=-71.06, y=76.44, heading=0.0,
                              speed=87.0, cname=b"RB4")
    assert process(ctw, own, [overtaking])['deceleration'] == 0.0


def test_the_warning_always_arrives_before_the_intervention(
        ctw, make_own_vehicle, make_vehicle):
    """Measured in ``simulation_tests`` scenario 08 and fixed here.

    The stopping distance grows with v-squared, the contact time only with v,
    so a car accelerating hard towards a junction reaches the deceleration the
    brake acts on while the contact is still 3.7 s away - outside every time
    threshold. The driver got the intervention with no warning in front of it.

    Swept over the whole approach: whenever the demand is at the engage
    threshold, the warning has already been at level 2 for at least one step.
    """
    seen_two_at = None
    for gap in (44.0, 40.0, 36.0, 32.0, 28.0, 24.0, 20.0, 16.0, 12.0):
        ctw.current_warning_level, ctw.current_side = 0, None
        own, others = junction(make_own_vehicle, make_vehicle, gap, gap,
                               own_kmh=42.0, other_kmh=42.0)
        result = process(ctw, own, others)
        if result['level'] >= 2 and seen_two_at is None:
            seen_two_at = gap
        if result['deceleration'] >= EmergencyBrake.ENGAGE_DECELERATION_MS2:
            assert seen_two_at is not None and seen_two_at > gap, (
                gap, result)
            return
    pytest.fail("the demand never reached the engage threshold")


def test_the_demand_never_exceeds_the_panic_value(
        ctw, make_own_vehicle, make_vehicle):
    """A number like 53 m/s2 in a log is a division by almost nothing, not
    information."""
    own, others = junction(make_own_vehicle, make_vehicle, 4.0, 4.0,
                           own_kmh=60.0, other_kmh=60.0)
    result = process(ctw, own, others)
    assert result['deceleration'] == PANIC_DECELERATION_MS2
