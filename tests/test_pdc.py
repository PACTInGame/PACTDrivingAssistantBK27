"""Park distance control: object identity, unit scale and the beeper (WP8)."""

import threading
import time

import pytest

import pyinsim
from assistance.park_distance_control import (
    AXM_TO_MCI, NO_HITBOX_OBJECTS, PDC_CLEAR, PDC_INACTIVE, PDC_MAX_SPEED_KMH,
    ParkDistanceControl, axm_object_id, create_rectangle_for_object)
from misc.pdc_beep import PDCBeepController
from misc import platform_shim

from conftest import FakePacket


def axm_object(index=96, x=0, y=0, zbyte=0, heading=0):
    """One ``ObjectInfo`` entry. X/Y are in 1/16 m, Zbyte in 1/4 m."""
    return FakePacket(Index=index, X=x, Y=y, Zbyte=zbyte, Heading=heading, Flags=0)


def axm_packet(action, objects):
    return FakePacket(Size=8 + 8 * len(objects), Type=pyinsim.ISP_AXM,
                      ReqI=0, NumO=len(objects), UCID=0, PMOAction=action,
                      PMOFlags=0, Sp3=0, Info=list(objects))


@pytest.fixture
def pdc(bus, settings):
    return ParkDistanceControl(bus, settings)


# ─── Acceptance: object ids that used to collide ─────────────────────────────

def old_style_id(info):
    """The id the code built before WP8, for the collision to be visible."""
    return int(str(info.Index) + str(abs(info.X)) + str(abs(info.Y)) + str(abs(info.Zbyte)))


def test_the_old_object_id_really_did_collide():
    """``X=1, Y=23`` and ``X=12, Y=3`` concatenate to the same digits."""
    a = axm_object(index=96, x=1, y=23)
    b = axm_object(index=96, x=12, y=3)
    assert old_style_id(a) == old_style_id(b)
    assert axm_object_id(a) != axm_object_id(b)


def test_the_new_object_id_keeps_the_sign():
    """``abs()`` made a mirrored object share its twin's id."""
    assert axm_object_id(axm_object(x=-40)) != axm_object_id(axm_object(x=40))


def test_colliding_objects_are_stored_and_removed_independently(pdc, bus):
    a = axm_object(index=96, x=1, y=23)
    b = axm_object(index=96, x=12, y=3)

    bus.emit('layout_received', axm_packet(pyinsim.PMO_ADD_OBJECTS, [a, b]))
    assert len(pdc.park_grid.static_objects) == 2

    # Deleting one must leave the other in the grid. With the old id both
    # shared one key, so this removed an obstacle that is still there.
    bus.emit('layout_received', axm_packet(pyinsim.PMO_DEL_OBJECTS, [a]))
    assert list(pdc.park_grid.static_objects) == [axm_object_id(b)]

    bus.emit('layout_received', axm_packet(pyinsim.PMO_DEL_OBJECTS, [b]))
    assert pdc.park_grid.static_objects == {}


def test_clear_all_empties_the_grid_and_asks_for_a_refresh(pdc, bus, recorder):
    requests = recorder('request_axm_update')
    bus.emit('layout_received', axm_packet(pyinsim.PMO_ADD_OBJECTS, [axm_object(x=1)]))
    bus.emit('layout_received', axm_packet(pyinsim.PMO_CLEAR_ALL, []))

    assert pdc.park_grid.static_objects == {}
    assert requests.count('request_axm_update') == 1


def test_objects_without_a_hitbox_are_not_inserted(pdc, bus):
    no_hitbox = sorted(NO_HITBOX_OBJECTS)[0]
    bus.emit('layout_received',
             axm_packet(pyinsim.PMO_ADD_OBJECTS, [axm_object(index=no_hitbox)]))
    assert pdc.park_grid.static_objects == {}


# ─── The AXM -> MCI unit scale ───────────────────────────────────────────────

def test_axm_to_mci_scale_is_the_documented_one():
    """AXM positions are 1/16 m, MCI ones 1/65536 m (conventions.md §1)."""
    assert AXM_TO_MCI == 65536 // 16 == 4096


def test_an_object_lands_where_its_coordinates_say():
    """X=16 in AXM units is 1 m, which is 65536 MCI units."""
    corners = create_rectangle_for_object(x=16, y=32, index=96, heading=0)
    centre_x = sum(x for x, _ in corners) / 4
    centre_y = sum(y for _, y in corners) / 4
    assert centre_x == pytest.approx(1.0 * 65536, abs=1.0)
    assert centre_y == pytest.approx(2.0 * 65536, abs=1.0)


def test_an_object_without_a_hitbox_reports_minus_one():
    assert create_rectangle_for_object(x=0, y=0, index=0, heading=0) == [-1]


@pytest.mark.parametrize("packet", [
    FakePacket(),                                        # no PMOAction at all
    FakePacket(PMOAction=pyinsim.PMO_ADD_OBJECTS),       # no Info list
    FakePacket(PMOAction=pyinsim.PMO_DEL_OBJECTS, Info=None),
])
def test_a_malformed_axm_packet_does_not_raise(pdc, bus, packet):
    bus.emit('layout_received', packet)
    assert pdc.park_grid.static_objects == {}


# ─── The -1 / 0 contract UIManager._update_pdc reads ─────────────────────────

def test_above_the_speed_limit_every_sensor_reports_inactive(
        pdc, make_own_vehicle, recorder):
    events = recorder('pdc_changed')
    own = make_own_vehicle(speed=PDC_MAX_SPEED_KMH + 5.0)
    assert pdc.process(own, {}) == dict.fromkeys(range(6), PDC_INACTIVE)
    # Nothing changed from the initial state, so nothing is emitted.
    assert events.count('pdc_changed') == 0


def test_below_the_speed_limit_an_empty_scene_reports_clear(
        pdc, make_own_vehicle, recorder):
    events = recorder('pdc_changed')
    own = make_own_vehicle(speed=3.0)
    assert pdc.process(own, {}) == dict.fromkeys(range(6), PDC_CLEAR)
    assert events.last('pdc_changed') == dict.fromkeys(range(6), PDC_CLEAR)


def test_the_result_is_only_emitted_on_change(pdc, make_own_vehicle, recorder):
    events = recorder('pdc_changed')
    own = make_own_vehicle(speed=3.0)
    for _ in range(5):
        pdc.process(own, {})
    assert events.count('pdc_changed') == 1


def test_a_car_right_behind_us_lights_the_rear_sensors(
        pdc, make_own_vehicle, make_vehicle, relate_to_own):
    own = make_own_vehicle(speed=2.0, heading=0.0)
    other = make_vehicle(plid=2, x=0.0, y=-4.0, heading=0.0, speed=0.0)
    relate_to_own(own, other)

    result = pdc.process(own, {2: other})
    assert max(result[i] for i in (3, 4, 5)) > 0     # rear sensors 3..5
    assert max(result[i] for i in (0, 1, 2)) == PDC_CLEAR


def test_a_car_further_away_than_the_range_is_ignored(
        pdc, make_own_vehicle, make_vehicle, relate_to_own):
    own = make_own_vehicle(speed=2.0, heading=0.0)
    other = make_vehicle(plid=2, x=0.0, y=-40.0, heading=0.0, speed=0.0)
    relate_to_own(own, other)

    assert pdc.process(own, {2: other}) == dict.fromkeys(range(6), PDC_CLEAR)
    assert pdc.park_grid.dynamic_objects == {}


def test_an_unknown_car_name_does_not_raise(
        pdc, make_own_vehicle, make_vehicle, relate_to_own):
    """A vehicle mod carries an arbitrary CName (``conventions.md`` §4)."""
    own = make_own_vehicle(speed=2.0, heading=0.0, cname=b"\x9a\x11\x02\x00")
    other = make_vehicle(plid=2, x=0.0, y=-4.0, heading=0.0, cname=b"zzzz")
    relate_to_own(own, other)
    assert pdc.process(own, {2: other})[4] > 0


# ─── Acceptance: the beeper spawns at most one thread ────────────────────────

@pytest.fixture
def beeper(bus):
    controller = PDCBeepController(bus)
    yield controller
    controller.stop()


def test_the_beeper_spawns_at_most_one_thread(beeper, bus):
    """known-issues #14: one thread per beep, from the 50 ms UI cycle."""
    before = threading.active_count()
    bus.emit('pdc_changed', {0: 0, 1: 0, 2: 0, 3: 3, 4: 3, 5: 3})
    for _ in range(200):
        beeper.beep()

    assert beeper.threads_started == 1
    assert threading.active_count() - before <= 1


def test_no_thread_starts_before_the_first_beep_request(beeper, bus):
    bus.emit('pdc_changed', {0: 3, 1: 3, 2: 3, 3: 0, 4: 0, 5: 0})
    assert beeper.threads_started == 0


def test_the_beeper_thread_is_a_daemon(beeper):
    beeper.beep()
    assert beeper._thread.daemon is True


def test_the_beeper_actually_sounds_while_requested(beeper, bus):
    """On Linux the shim records the call instead of making noise."""
    platform_shim.reset_recorded_calls()
    bus.emit('pdc_changed', {0: 0, 1: 0, 2: 0, 3: 3, 4: 3, 5: 3})
    beeper.beep()

    deadline = time.monotonic() + 2.0
    while time.monotonic() < deadline:
        calls = [c for c in platform_shim.recorded_calls() if c[0] == 'winsound.Beep']
        if calls:
            break
        time.sleep(0.01)
    else:
        pytest.fail("the beeper thread never called winsound.Beep")

    frequency, duration = calls[0][1]
    assert frequency == beeper.REAR_FREQUENCY
    assert duration == beeper.BEEP_PATTERNS[3]["beep_duration"]


def test_the_beeper_falls_silent_without_a_request(beeper, bus):
    bus.emit('pdc_changed', {0: 0, 1: 0, 2: 0, 3: 3, 4: 3, 5: 3})
    beeper.beep()
    beeper._enabled_until = 0.0            # as if the UI had stopped calling
    assert beeper._current_tone() == (0, 0)


@pytest.mark.parametrize("payload", [None, [], "nonsense", {}, {0: -1}])
def test_the_beeper_survives_a_malformed_payload(beeper, payload):
    beeper._update_pdc_data(payload)
    assert beeper._current_tone() == (0, 0)
