"""Forward collision warning: physics, gating and warning levels (WP7).

Every expectation below is hand-computed from the model documented in
``ForwardCollisionWarning`` and ``reference/systems.md``:

    d      = distance_to_player − (len_own + len_other)/2 − SAFETY_BUFFER
             − REACTION_TIME · Δv        (the last term only while closing)
    a_req  = Δv² / (2 d)                 (lead at constant speed)

so the numbers in the asserts are derived, not observed.
"""

import math

import pytest

from assistance.collision_warning import ForwardCollisionWarning
from assistance import park_distance_control
from assistance.path_conflict import direction_vector
from misc.helpers import heading_difference, is_reversing
from vehicles.vehicle import (ACCEL_SMOOTHING_TAU_S, MAX_SAMPLE_DT_S, Vehicle)

from conftest import lfs_heading, metres


KMH_TO_MS = 0.277778
XFG_LENGTH_M = 3.7


@pytest.fixture
def fcw(bus, settings):
    return ForwardCollisionWarning(bus, settings)


def effective_distance(gap_m, own_kmh, other_kmh=0.0,
                       own_length=XFG_LENGTH_M, other_length=XFG_LENGTH_M):
    """The ``d`` the model brakes against, from a centre-to-centre gap."""
    d = gap_m - (own_length + other_length) / 2 - ForwardCollisionWarning.SAFETY_BUFFER_M
    closing = (own_kmh - other_kmh) * KMH_TO_MS
    if closing > 0:
        d -= closing * ForwardCollisionWarning.REACTION_TIME_S
    return d


# ─── Reverse detection across the heading wrap ───────────────────────────────

def test_heading_difference_is_modular():
    """0.75° apart across the wrap point, not 65400 units apart."""
    assert heading_difference(100, 65500) == 136
    assert heading_difference(65500, 100) == -136
    assert heading_difference(0, 32768) == -32768


def test_driving_straight_across_the_wrap_is_not_reversing():
    # The old expression was a plain subtraction against ±10000 and read
    # 100 - 65500 = -65400 as "reversing" (WP7 defect 1).
    assert (100 - 65500) < -10000          # what the old code computed
    assert is_reversing(100, 65500) is False


def test_actually_reversing_is_detected():
    # Pointing north, moving south.
    assert is_reversing(0, 32768) is True


def test_fcw_still_warns_while_driving_across_the_wrap(
        fcw, recorder, make_own_vehicle, make_vehicle, relate_to_own):
    seen = recorder('collision_warning_changed')
    # heading 100 units, direction 65500 units -- straight ahead, over the wrap.
    heading_deg = 100 / 182.0444
    own = make_own_vehicle(speed=100.0, heading=heading_deg,
                           direction=65500 / 182.0444)
    assert own.data.heading == 100 and own.data.direction == 65500
    # 50 m along that heading (LFS: 0 = +Y, anticlockwise).
    bearing = math.radians(heading_deg)
    lead = make_vehicle(plid=2, x=-50.0 * math.sin(bearing),
                        y=50.0 * math.cos(bearing), speed=0.0)
    relate_to_own(own, lead)

    result = fcw.process(own, {2: lead})

    assert result['level'] == 3
    assert seen.last('collision_warning_changed')['level'] == 3


# ─── The braking maths ───────────────────────────────────────────────────────

def test_closing_on_a_stationary_car_matches_v_squared_over_2d(
        fcw, make_own_vehicle, make_vehicle, relate_to_own):
    own = make_own_vehicle(speed=100.0)
    lead = make_vehicle(plid=2, y=50.0, speed=0.0)
    relate_to_own(own, lead)

    d = effective_distance(50.0, 100.0)
    expected = (100.0 * KMH_TO_MS) ** 2 / (2 * d)

    assert fcw._calculate_needed_braking(own.data, lead.data) == pytest.approx(expected)
    assert expected == pytest.approx(9.586, abs=1e-3)


def test_being_slower_than_a_lead_that_is_not_braking_needs_nothing(
        fcw, make_own_vehicle, make_vehicle, relate_to_own):
    own = make_own_vehicle(speed=60.0)
    lead = make_vehicle(plid=2, y=30.0, speed=80.0, acceleration=0.0)
    relate_to_own(own, lead)

    assert fcw._calculate_needed_braking(own.data, lead.data) == 0.0


def test_a_faster_lead_that_is_not_braking_reports_no_demand(
        fcw, make_own_vehicle, make_vehicle, relate_to_own):
    """WP7 defect 2: ``abs(req_accel)`` turned "you may accelerate" into a
    large braking demand and could raise a warning level."""
    own = make_own_vehicle(speed=95.0)
    lead = make_vehicle(plid=2, y=50.0, speed=100.0, acceleration=0.0)
    relate_to_own(own, lead)

    assert fcw._calculate_needed_braking(own.data, lead.data) == 0.0


def test_a_lead_pulling_away_gets_no_credit_for_its_acceleration(
        fcw, make_own_vehicle, make_vehicle, relate_to_own):
    """Its speed is measured; the speed it has not gained yet is a guess.

    Measured in ``simulation_tests`` 32, sub-case 4 (2026-09-20): a lead that
    accelerated away at 5 m/s² and then braked at 10 m/s² kept the demand at
    **exactly 0.0 for 1.8 s** while the gap fell from 55 m to 44 m at 82 km/h.
    The warning arrived 1.15 s before contact and the car hit at 54 km/h.

    With the acceleration clamped away the same geometry asks for what closing
    on a car at its *current* speed costs -- which is what the driver is
    actually doing.
    """
    own = make_own_vehicle(speed=100.0)
    lead = make_vehicle(plid=2, y=50.0, speed=20.0, acceleration=+5.0)
    relate_to_own(own, lead)

    d = effective_distance(50.0, 100.0, 20.0)
    closing = (100.0 - 20.0) * KMH_TO_MS
    expected = closing ** 2 / (2 * d)          # a_lead treated as 0

    assert expected > ForwardCollisionWarning.WARNING_THRESHOLDS[1][1]
    assert fcw._calculate_needed_braking(own.data, lead.data) == \
        pytest.approx(expected)


def test_a_braking_lead_is_still_extrapolated(
        fcw, make_own_vehicle, make_vehicle, relate_to_own):
    """The clamp is one-sided: braking only ever raises the demand."""
    own = make_own_vehicle(speed=80.0)
    coasting = make_vehicle(plid=2, y=60.0, speed=40.0, acceleration=0.0)
    braking = make_vehicle(plid=3, y=60.0, speed=40.0, acceleration=-8.0)
    relate_to_own(own, coasting, braking)

    assert fcw._calculate_needed_braking(own.data, braking.data) > \
        fcw._calculate_needed_braking(own.data, coasting.data)


def test_the_demand_never_exceeds_the_panic_value(
        fcw, make_own_vehicle, make_vehicle, relate_to_own):
    """Beyond it the division is rounding error, not information.

    Traces of 2026-09-20 carried 198, 1354 and 4758 m/s² into the logs and
    into every average taken over the demand.
    """
    own = make_own_vehicle(speed=120.0)
    # Just outside the safety buffer, so the panic shortcut does not apply and
    # the closed form runs with a very small ``d``.
    lead = make_vehicle(plid=2, y=11.5, speed=0.0)
    relate_to_own(own, lead)

    need = fcw._calculate_needed_braking(own.data, lead.data)
    assert need == ForwardCollisionWarning.PANIC_DECELERATION_MS2


def test_inside_the_safety_buffer_returns_the_panic_value(
        fcw, make_own_vehicle, make_vehicle, relate_to_own):
    own = make_own_vehicle(speed=50.0)
    lead = make_vehicle(plid=2, y=3.0, speed=0.0)     # bumper to bumper
    relate_to_own(own, lead)

    assert effective_distance(3.0, 50.0) <= 0.01
    assert (fcw._calculate_needed_braking(own.data, lead.data)
            == ForwardCollisionWarning.PANIC_DECELERATION_MS2)


def test_a_braking_lead_car_is_treated_as_a_wall_at_its_stopping_point(
        fcw, make_own_vehicle, make_vehicle, relate_to_own):
    """Static case: the lead stops before we reach it."""
    own = make_own_vehicle(speed=80.0)
    lead = make_vehicle(plid=2, y=60.0, speed=40.0, acceleration=-8.0)
    relate_to_own(own, lead)

    v_own = 80.0 * KMH_TO_MS
    v_other = 40.0 * KMH_TO_MS
    d = effective_distance(60.0, 80.0, 40.0)
    t_match = 2 * d / (v_own - v_other)
    t_stop = v_other / 8.0
    assert t_stop < t_match                      # the static branch applies
    d_total = d + v_other ** 2 / (2 * 8.0)
    expected = v_own ** 2 / (2 * d_total)

    assert fcw._calculate_needed_braking(own.data, lead.data) == pytest.approx(expected)


# ─── Detection geometry ──────────────────────────────────────────────────────

# The corridor's half-width is the lateral-overlap condition, so for two XFGs
# (1.7 m wide) it is 1.7 m. These are the numbers the in-game misses of
# 2026-09-20 were measured against: 1.68 m and 1.77 m lateral on the two cars
# that were hit, 2.74 m on the one that was passed safely.
XFG_WIDTH_M = 1.7
# The forward vector ``process`` hands to the corridor test: heading 0 is +Y
# in LFS's frame (``reference/conventions.md`` §2).
NORTH = (0.0, 1.0)
EAST = (1.0, 0.0)


@pytest.mark.parametrize('heading', [0, 45, 90, 180, 270, 359])
@pytest.mark.parametrize('ahead', [5.0, 50.0, 80.0])
@pytest.mark.parametrize('side', [-1.0, 1.0])
def test_the_corridor_holds_its_width_over_its_whole_length(
        fcw, make_own_vehicle, make_vehicle, relate_to_own, heading, ahead, side):
    """A car within the overlap width is seen at 5 m and at 80 m alike.

    The wedge this replaced narrowed to 1.03-1.48 m half-width, so a lead car
    1.6 m off our axis - ordinary lane keeping at 90 km/h - dropped out of it
    exactly while the demand was crossing the intervention threshold.
    """
    own = make_own_vehicle(x=123.0, y=-456.0, speed=150.0,
                           heading=heading, direction=heading)
    bearing = math.radians(heading)
    # Independent local-to-world transform: forward is north at heading 0.
    for lateral, expected in [(1.6 * side, 3), (2.5 * side, 0)]:
        lead = make_vehicle(
            plid=2, speed=0.0,
            x=123.0 - ahead * math.sin(bearing) + lateral * math.cos(bearing),
            y=-456.0 + ahead * math.cos(bearing) + lateral * math.sin(bearing))
        relate_to_own(own, lead)
        assert fcw.process(own, {2: lead})['level'] == expected


def test_the_corridor_is_the_two_half_widths_added(
        fcw, make_own_vehicle, make_vehicle, relate_to_own):
    """Not a tuned constant: it is when the two bodies share lane width."""
    own = make_own_vehicle(speed=50.0)
    inside = make_vehicle(plid=2, x=XFG_WIDTH_M - 0.01, y=40.0)
    outside = make_vehicle(plid=3, x=XFG_WIDTH_M + 0.01, y=40.0)
    relate_to_own(own, inside, outside)

    assert fcw._is_vehicle_ahead(own.data, inside.data, NORTH) is True
    assert fcw._is_vehicle_ahead(own.data, outside.data, NORTH) is False


def test_a_wider_car_is_seen_further_off_the_axis(
        fcw, make_own_vehicle, make_vehicle, relate_to_own):
    """An FZ5 is 2.0 m wide, an XFG 1.7 m - the corridor follows the pair."""
    own = make_own_vehicle(speed=50.0)
    lateral = (XFG_WIDTH_M + 2.0) / 2 - 0.01
    narrow = make_vehicle(plid=2, x=lateral, y=40.0)
    wide = make_vehicle(plid=3, x=lateral, y=40.0, cname=b"FZ5")
    relate_to_own(own, narrow, wide)

    assert fcw._is_vehicle_ahead(own.data, narrow.data, NORTH) is False
    assert fcw._is_vehicle_ahead(own.data, wide.data, NORTH) is True


def test_an_unknown_mod_gets_the_conservative_width(fcw):
    """A mod must never be assumed narrow - that only delays the warning."""
    assert fcw._vehicle_width(b"NOSUCHCAR") == \
        ForwardCollisionWarning.FALLBACK_VEHICLE_WIDTH_M
    assert fcw._vehicle_width(b"XFG") == XFG_WIDTH_M


def test_a_car_three_metres_to_the_side_is_not_ahead(
        fcw, make_own_vehicle, make_vehicle, relate_to_own):
    own = make_own_vehicle(speed=50.0)
    beside = make_vehicle(plid=2, x=3.0, y=0.0)
    relate_to_own(own, beside)
    assert fcw._is_vehicle_ahead(own.data, beside.data, NORTH) is False


def test_a_car_straight_ahead_is_ahead(fcw, make_own_vehicle, make_vehicle,
                                       relate_to_own):
    own = make_own_vehicle(speed=50.0)
    lead = make_vehicle(plid=2, y=40.0)
    relate_to_own(own, lead)
    assert fcw._is_vehicle_ahead(own.data, lead.data, NORTH) is True


def test_the_range_gate_rejects_a_far_car_before_any_geometry(
        fcw, make_own_vehicle, make_vehicle, relate_to_own):
    own = make_own_vehicle(speed=100.0)
    far = make_vehicle(plid=2, y=200.0)
    relate_to_own(own, far)
    assert far.data.distance_to_player > ForwardCollisionWarning.RANGE_GATE_M
    assert fcw._is_vehicle_ahead(own.data, far.data, NORTH) is False


def test_a_car_behind_us_is_not_a_rear_end_case(
        fcw, make_own_vehicle, make_vehicle, relate_to_own):
    """It belongs to the blind spot warning, whatever the lateral offset."""
    own = make_own_vehicle(speed=100.0)
    behind = make_vehicle(plid=2, y=-30.0)
    relate_to_own(own, behind)
    assert fcw._is_vehicle_ahead(own.data, behind.data, NORTH) is False


def test_the_corridor_follows_the_car_heading(fcw, make_own_vehicle,
                                              make_vehicle, relate_to_own):
    """Pointing east, the car 40 m east is ahead and the one 40 m north is not."""
    own = make_own_vehicle(speed=50.0, heading=270.0)   # LFS: 270 = +X (east)
    east = make_vehicle(plid=2, x=40.0)
    north = make_vehicle(plid=3, y=40.0)
    relate_to_own(own, east, north)

    # The vector really is the one ``process`` derives from the heading.
    # 1e-3, not exact: the conversion goes through 182.05 units per degree,
    # so a cardinal heading is a cardinal direction only to within rounding.
    assert direction_vector(own.data.heading) == pytest.approx(EAST, abs=1e-3)
    assert fcw._is_vehicle_ahead(own.data, east.data, EAST) is True
    assert fcw._is_vehicle_ahead(own.data, north.data, EAST) is False


# ─── Warning levels ──────────────────────────────────────────────────────────

def test_levels_rise_at_the_configured_thresholds(fcw):
    early, middle, late = ForwardCollisionWarning.WARNING_THRESHOLDS[1]

    assert fcw._warning_level(early + 0.1, 0.0, (early, middle, late)) == 3
    fcw.current_warning_level = 0
    assert fcw._warning_level(middle + 0.1, 0.0, (early, middle, late)) == 2
    fcw.current_warning_level = 0
    assert fcw._warning_level(late + 0.1, 0.0, (early, middle, late)) == 1
    fcw.current_warning_level = 0
    assert fcw._warning_level(late - 0.1, 0.0, (early, middle, late)) == 0


def test_level_one_is_suppressed_while_we_already_brake_hard_enough(fcw):
    thresholds = ForwardCollisionWarning.WARNING_THRESHOLDS[1]
    needed = thresholds[2] + 1.0

    assert fcw._warning_level(needed, -needed - 1.0, thresholds) == 0
    assert fcw._warning_level(needed, 0.0, thresholds) == 1


def test_a_reached_level_falls_again_with_hysteresis_and_does_not_latch(fcw):
    """WP7 defect 3: level 3 used to hold for any demand above 0."""
    thresholds = ForwardCollisionWarning.WARNING_THRESHOLDS[1]      # 7.5 / 5.0 / 2.5
    release = ForwardCollisionWarning.HYSTERESIS_RELEASE

    fcw.current_warning_level = 3
    # Still inside the release band of level 3 -> held.
    assert fcw._warning_level(thresholds[0] * release + 0.1, 0.0, thresholds) == 3
    # Below it, but still inside level 2's band -> falls to 2, not to 0.
    assert fcw._warning_level(4.5, 0.0, thresholds) == 2
    fcw.current_warning_level = 3
    # No demand at all -> all the way down.
    assert fcw._warning_level(0.0, 0.0, thresholds) == 0


def test_the_warning_level_follows_the_situation_up_and_down(
        fcw, recorder, make_own_vehicle, make_vehicle, relate_to_own):
    seen = recorder('collision_warning_changed')

    def level_at(gap_m):
        own = make_own_vehicle(speed=100.0)
        lead = make_vehicle(plid=2, y=gap_m, speed=0.0)
        relate_to_own(own, lead)
        return fcw.process(own, {2: lead})['level']

    assert level_at(60.0) == 3          # a_req 7.68 > 7.5
    assert level_at(80.0) == 2          # a_req 5.49: released from 3, held at 2
    assert level_at(300.0) == 0         # out of range entirely

    assert [payload['level'] for payload in seen.payloads('collision_warning_changed')] \
        == [3, 2, 0]


def test_identical_inputs_emit_the_level_only_once(
        fcw, recorder, make_own_vehicle, make_vehicle, relate_to_own):
    seen = recorder('collision_warning_changed', 'needed_deceleration_update')
    own = make_own_vehicle(speed=100.0)
    lead = make_vehicle(plid=2, y=50.0, speed=0.0)
    relate_to_own(own, lead)

    fcw.process(own, {2: lead})
    fcw.process(own, {2: lead})

    assert seen.count('collision_warning_changed') == 1
    # needed_deceleration_update keeps its contract: every cycle.
    assert seen.count('needed_deceleration_update') == 2


def test_the_deceleration_event_is_zero_below_level_three(
        fcw, recorder, make_own_vehicle, make_vehicle, relate_to_own):
    """The intervention waits for the top warning level, not for 6.0 m/s².

    ``EmergencyBrake.ENGAGE_DECELERATION_MS2`` is 6.0, level 3 starts above
    7.5, and this gate is what makes 7.5 the number that actually decides.
    That is deliberate while the actuator is digital: engaging at a demand of
    6 m/s² and then braking with the ~9.7 m/s² a tyre gives stops the car about
    a quarter of the braking distance early. Measured in game on 2026-09-20,
    publishing from level 2 left the car standing 6 m, 11 m and 11 m short in
    scenarios 07, 12 and 26.
    """
    seen = recorder('needed_deceleration_update')
    own = make_own_vehicle(speed=100.0)
    lead = make_vehicle(plid=2, y=80.0, speed=0.0)
    relate_to_own(own, lead)

    assert fcw.process(own, {2: lead})['level'] == 2
    assert fcw._calculate_needed_braking(own.data, lead.data) > 0.0
    assert seen.last('needed_deceleration_update')['deceleration'] == 0.0


@pytest.mark.parametrize('distance_setting',
                         sorted(ForwardCollisionWarning.WARNING_THRESHOLDS))
def test_nothing_is_published_before_the_driver_is_warned(
        fcw, settings, recorder, make_own_vehicle, make_vehicle, relate_to_own,
        distance_setting):
    """Display, then sound, then brake -- by construction, not by luck.

    Tying the demand to the *level* rather than to a number is what makes this
    hold in every distance setting. It is not automatic: on "late" the audible
    level begins at 6.5 m/s², above ``EmergencyBrake.ENGAGE_DECELERATION_MS2``,
    so a demand published on a numeric rule could brake before it ever beeped.
    """
    from assistance.emergency_brake import EmergencyBrake

    settings.set('collision_warning_distance', distance_setting)
    seen = recorder('needed_deceleration_update')
    own = make_own_vehicle(speed=60.0)

    warned = False
    for gap in [80.0, 60.0, 45.0, 35.0, 28.0, 22.0, 18.0, 15.0]:
        lead = make_vehicle(plid=2, y=gap, speed=0.0)
        relate_to_own(own, lead)
        level = fcw.process(own, {2: lead})['level']
        warned = warned or level >= 2
        published = seen.last('needed_deceleration_update')['deceleration']
        if published >= EmergencyBrake.ENGAGE_DECELERATION_MS2:
            assert warned, 'braking demand delivered before any audible warning'


# ─── Suppression ─────────────────────────────────────────────────────────────

def test_no_warning_below_the_minimum_speed(
        fcw, make_own_vehicle, make_vehicle, relate_to_own):
    own = make_own_vehicle(speed=5.0)
    lead = make_vehicle(plid=2, y=6.0, speed=0.0)
    relate_to_own(own, lead)

    assert fcw.process(own, {2: lead})['level'] == 0


def test_no_warning_while_the_system_is_switched_off(
        bus, make_settings, make_own_vehicle, make_vehicle, relate_to_own):
    settings = make_settings(forward_collision_warning=False)
    system = ForwardCollisionWarning(bus, settings)
    own = make_own_vehicle(speed=100.0)
    lead = make_vehicle(plid=2, y=50.0, speed=0.0)
    relate_to_own(own, lead)

    assert system.process(own, {2: lead})['level'] == 0


def test_dist_debug_is_no_longer_emitted(
        fcw, recorder, make_own_vehicle, make_vehicle, relate_to_own):
    """known-issues #15: emitted per vehicle per cycle, subscriber commented out."""
    seen = recorder('dist_debug')
    own = make_own_vehicle(speed=100.0)
    lead = make_vehicle(plid=2, y=50.0, speed=0.0)
    relate_to_own(own, lead)

    fcw.process(own, {2: lead})

    assert seen.count('dist_debug') == 0


# ─── Car length fallback for mods (known-issues #28) ─────────────────────────

def test_a_known_car_uses_its_real_length(fcw):
    assert fcw._vehicle_length('XFG') == XFG_LENGTH_M


def test_an_unknown_car_name_falls_back_to_the_longest_plausible_car(fcw):
    # The shared table would silently hand out a mid-size saloon here.
    assert park_distance_control.get_vehicle_size('SomeMod')[0] == 4.5
    assert fcw._vehicle_length('SomeMod') == \
        ForwardCollisionWarning.FALLBACK_VEHICLE_LENGTH_M


def test_the_mod_fallback_warns_earlier_not_later(
        fcw, make_own_vehicle, make_vehicle, relate_to_own):
    own = make_own_vehicle(speed=100.0, cname=b"XFG")
    known = make_vehicle(plid=2, y=50.0, speed=0.0, cname=b"XFG")
    mod = make_vehicle(plid=3, y=50.0, speed=0.0, cname=b"MOD9")
    relate_to_own(own, known, mod)

    assert (fcw._calculate_needed_braking(own.data, mod.data)
            > fcw._calculate_needed_braking(own.data, known.data))


# ─── Acceleration is derived from real elapsed time (known-issues #13) ───────

def _sample(vehicle, speed_kmh, at):
    vehicle.update_position(0, 0, 0, 0, 0, speed_kmh, timestamp=at)
    return vehicle.data.acceleration


def test_the_first_packet_yields_no_acceleration():
    vehicle = Vehicle(2)

    assert _sample(vehicle, 50.0, at=1.0) == 0.0


@pytest.mark.parametrize("dt, expected", [
    (0.05, (10 / 3.6) / 0.05),
    (0.10, (10 / 3.6) / 0.10),
    (0.20, (10 / 3.6) / 0.20),
])
def test_acceleration_scales_with_the_real_packet_interval(dt, expected):
    """The old code multiplied Δ km/h by a constant 2.778, i.e. assumed 100 ms.

    At 50 ms it was half the truth, at 200 ms double it -- and that value feeds
    FCW's braking maths and the adaptive brake light.
    """
    vehicle = Vehicle(2)
    _sample(vehicle, 50.0, at=0.0)
    result = _sample(vehicle, 60.0, at=dt)

    assert result == pytest.approx(expected)


def test_the_hundred_millisecond_case_still_matches_the_old_constant():
    vehicle = Vehicle(2)
    _sample(vehicle, 50.0, at=0.0)

    assert _sample(vehicle, 60.0, at=0.1) == pytest.approx(10 * 2.778, rel=1e-3)


def test_a_gap_in_the_packet_stream_resets_instead_of_inventing_a_value():
    vehicle = Vehicle(2)
    _sample(vehicle, 50.0, at=0.0)
    _sample(vehicle, 60.0, at=0.1)

    assert _sample(vehicle, 10.0, at=0.1 + MAX_SAMPLE_DT_S + 0.01) == 0.0


def test_repeated_samples_are_low_pass_filtered_towards_the_raw_value():
    vehicle = Vehicle(2)
    dt = 0.1
    _sample(vehicle, 0.0, at=0.0)
    raw = (10 / 3.6) / dt
    first = _sample(vehicle, 10.0, at=dt)          # filter empty: taken as is
    second = _sample(vehicle, 30.0, at=2 * dt)     # raw doubles

    assert first == pytest.approx(raw)
    alpha = 1.0 - math.exp(-dt / ACCEL_SMOOTHING_TAU_S)
    assert second == pytest.approx(first + alpha * (2 * raw - first))
    assert first < second < 2 * raw                # lags, does not overshoot


def test_a_frame_snapshot_carries_the_acceleration():
    """The filter state lives on the Vehicle, the value on the frame copy."""
    vehicle = Vehicle(2)
    _sample(vehicle, 50.0, at=0.0)
    vehicle.begin_frame()
    vehicle.update_position(metres(1.0), 0, 0, lfs_heading(0.0), lfs_heading(0.0),
                            60.0, timestamp=0.1)
    staged = vehicle._staged.acceleration
    vehicle.commit_frame()

    assert vehicle.data.acceleration == staged == pytest.approx((10 / 3.6) / 0.1)
