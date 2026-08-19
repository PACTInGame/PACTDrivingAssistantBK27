"""The fixtures themselves.

Every later work package builds its scenarios out of these factories, so their
unit conversions are load-bearing: a wrong factor here would make a whole test
file agree with a bug (``reference/conventions.md`` §1-§3).
"""

import pytest

from conftest import lfs_heading, mci_speed, metres
from vehicles.vehicle_manager import VehicleManager


# ─── Unit conversions ────────────────────────────────────────────────────────

def test_metres_conversion():
    assert metres(1.0) == 65536
    assert metres(0.0) == 0
    assert metres(-2.5) == -163840


@pytest.mark.parametrize("degrees,word", [
    (0.0, 0),           # +Y, north
    (90.0, 16384),      # anticlockwise: -X, west
    (180.0, 32768),
    (270.0, 49152),
    (360.0, 0),
    (-90.0, 49152),
])
def test_heading_conversion(degrees, word):
    assert lfs_heading(degrees) == word


def test_speed_conversion():
    # CompCar.Speed: 32768 = 100 m/s = 360 km/h
    assert mci_speed(360.0) == pytest.approx(32767, abs=2)
    assert mci_speed(0.0) == 0


# ─── Vehicle factories ───────────────────────────────────────────────────────

def test_make_vehicle_stores_lfs_units_and_metadata(make_vehicle):
    vehicle = make_vehicle(plid=7, x=10.0, y=-4.0, heading=90.0, speed=50.0,
                           cname=b'FZ5', pname=b'Ben')

    assert vehicle.data.player_id == 7
    assert vehicle.data.x == metres(10.0)
    assert vehicle.data.y == metres(-4.0)
    assert vehicle.data.heading == lfs_heading(90.0)
    assert vehicle.data.speed == 50.0          # km/h, as everywhere else
    assert (vehicle.data.cname, vehicle.data.pname) == (b'FZ5', b'Ben')


def test_make_vehicle_defaults_to_zero_acceleration(make_vehicle):
    assert make_vehicle(speed=80.0).data.acceleration == pytest.approx(0.0)


def test_make_vehicle_reproduces_a_requested_deceleration(make_vehicle):
    """Braking at 6 m/s² must come back out of the derived acceleration."""
    vehicle = make_vehicle(speed=80.0, acceleration=-6.0)

    assert vehicle.data.acceleration == pytest.approx(-6.0, abs=1e-6)


def test_distance_and_angle_to_player_are_metres_and_degrees(make_vehicle):
    """0° = straight ahead of the observer (``reference/conventions.md`` §2)."""
    ahead = make_vehicle(plid=2, x=0.0, y=30.0)

    ahead.update_distance_to_player(metres(0.0), metres(0.0), 0)
    ahead.update_angle_to_player(metres(0.0), metres(0.0), lfs_heading(0.0))

    assert ahead.data.distance_to_player == pytest.approx(30.0, abs=1e-3)
    assert min(ahead.data.angle_to_player, 360.0 - ahead.data.angle_to_player) \
        == pytest.approx(0.0, abs=0.5)


def test_make_own_vehicle_applies_outgauge_fields(make_own_vehicle):
    own = make_own_vehicle(plid=3, speed=72.0, gear=4, rpm=4200.0,
                           throttle=0.5, brake=0.0, full_beam=True,
                           handbrake=True)

    assert own.data.player_id == 3
    assert own.data.speed == pytest.approx(72.0)   # km/h
    assert own.gear == 4
    assert own.rpm == pytest.approx(4200.0)
    assert own.throttle == pytest.approx(0.5)
    assert own.full_beam_light is True
    assert own.handbrake_light is True
    assert own.low_beam_light is False


def test_show_lights_rejects_unknown_flags(make_outgauge_packet):
    with pytest.raises(KeyError):
        make_outgauge_packet(headlight_washer=True)


# ─── Packet factories against the real VehicleManager ────────────────────────

def test_mci_and_npl_packets_drive_the_vehicle_manager(
        bus, recorder, make_compcar, make_mci_packet, make_npl_packet,
        make_outgauge_packet):
    """The fake packets must carry the field names the handlers actually read."""
    seen = recorder('vehicles_updated', 'player_data_updated')
    manager = VehicleManager(bus)

    bus.emit('player_joined', make_npl_packet(plid=1, pname=b'Me', cname=b'XFG'))
    bus.emit('player_joined', make_npl_packet(plid=2, pname=b'Other', cname=b'FZ5'))
    bus.emit('outgauge_data', make_outgauge_packet(plid=1, speed=50.0))
    bus.emit('vehicle_data_received', make_mci_packet([
        make_compcar(plid=1, x=0.0, y=0.0, heading=0.0, speed=50.0),
        make_compcar(plid=2, x=0.0, y=25.0, heading=0.0, speed=0.0),
    ]))

    assert manager.own_vehicle.data.player_id == 1
    assert set(manager.vehicles) == {2}

    other = manager.vehicles[2]
    assert other.data.speed == pytest.approx(0.0, abs=0.02)
    assert other.data.cname == b'FZ5'  # NPL delivers bytes
    assert other.data.distance_to_player == pytest.approx(25.0, abs=0.01)
    assert seen.count('vehicles_updated') == 1


def test_pll_packet_removes_the_vehicle(
        bus, make_compcar, make_mci_packet, make_npl_packet, make_pll_packet,
        make_outgauge_packet):
    manager = VehicleManager(bus)
    bus.emit('player_joined', make_npl_packet(plid=1))
    bus.emit('player_joined', make_npl_packet(plid=2))
    bus.emit('outgauge_data', make_outgauge_packet(plid=1))
    bus.emit('vehicle_data_received', make_mci_packet([make_compcar(plid=2, y=10.0)]))

    assert 2 in manager.vehicles

    bus.emit('player_left', make_pll_packet(plid=2))

    assert 2 not in manager.vehicles and 2 not in manager.players


def test_sta_packet_drives_the_state_handler(bus, recorder, fake_connector,
                                             make_sta_packet):
    from lfs.lfs_state import StateHandler

    seen = recorder('state_data')
    StateHandler(fake_connector)

    bus.emit('game_state_changed', make_sta_packet(on_track=False))
    assert seen.last('state_data')['on_track'] is False

    bus.emit('game_state_changed', make_sta_packet(on_track=True, track=b'SO6R'))
    state = seen.last('state_data')
    assert state['on_track'] is True
    assert state['track'] == b'SO6R'
    assert state['text_entry'] is False
    assert state['dialog'] is False
