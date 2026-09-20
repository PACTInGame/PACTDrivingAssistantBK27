"""Packet snapshot and unacknowledged handbrake regressions; no OS input."""
from types import SimpleNamespace

import pytest

from assistance.auto_hold import AutoHold
from vehicles.vehicle_manager import VehicleManager


def test_published_vehicles_survive_later_packets(
        bus, recorder, make_npl_packet, make_outgauge_packet,
        make_compcar, make_mci_frame):
    manager = VehicleManager(bus)
    seen = recorder('own_vehicle_updated', 'vehicles_updated')
    bus.emit('player_joined', make_npl_packet(plid=1))
    bus.emit('outgauge_data', make_outgauge_packet(plid=1, rpm=1500, brake=0.2))
    for packet in make_mci_frame([make_compcar(plid=1, y=10),
                                  make_compcar(plid=2, y=30)]):
        bus.emit('vehicle_data_received', packet)
    own = seen.last('own_vehicle_updated')
    others = seen.last('vehicles_updated')
    bus.emit('outgauge_data', make_outgauge_packet(plid=2, rpm=6000, brake=1))
    for packet in make_mci_frame([make_compcar(plid=1, y=50),
                                  make_compcar(plid=2, y=80)]):
        bus.emit('vehicle_data_received', packet)
    manager.own_vehicle.clear_local_driver()
    assert (own.rpm, own.brake, own.local_plid, own.viewed_plid) == (1500, 0.2, 1, 1)
    assert own.is_local_driver
    assert own.data.y == pytest.approx(10 * 65536)
    assert others[2].data.y == pytest.approx(30 * 65536)
    assert others[2].data.distance_to_player == pytest.approx(20)


@pytest.fixture
def hold(bus, make_settings, monkeypatch):
    calls = []
    tapper = SimpleNamespace(tap=lambda key: calls.append(key) or True)
    monkeypatch.setattr('assistance.auto_hold.get_key_tapper', lambda: tapper)
    system = AutoHold(bus, make_settings(auto_hold=True, language='en'))
    system.guard.foreground_check = lambda: True
    bus.emit('state_data', {'on_track': True})
    return system, calls


def test_ignored_handbrake_attempt_is_not_repeated_or_reported_as_success(
        hold, recorder, make_own_vehicle, monkeypatch):
    system, calls = hold
    now = [0.0]
    monkeypatch.setattr('assistance.auto_hold.time.monotonic', lambda: now[0])
    own = make_own_vehicle(speed=0, brake=0.7, control_mode=0)
    seen = recorder('notification')
    for cycle in range(100):
        now[0] = cycle / 10
        assert not system.process(own, {})['auto_hold_active']
    assert len(calls) == 1
    assert seen.count('notification') == 1
    assert 'check handbrake' in seen.last('notification')['notification']
    own.brake = 0
    system.process(own, {})
    own.brake = 0.7
    system.process(own, {})
    assert len(calls) == 2


def test_handbrake_confirmation_notifies_once_and_does_not_toggle_again(
        hold, recorder, make_own_vehicle):
    system, calls = hold
    own = make_own_vehicle(speed=0, brake=0.7)
    seen = recorder('notification')
    assert not system.process(own, {})['auto_hold_active']
    assert seen.count('notification') == 0
    own.handbrake_light = True
    for _ in range(20):
        assert system.process(own, {})['auto_hold_active']
    own.handbrake_light = False
    assert not system.process(own, {})['auto_hold_active']
    assert len(calls) == 1
    assert seen.count('notification') == 1


@pytest.mark.parametrize('blocked', ['dialog', 'text_entry', 'off_track', 'spectating'])
def test_auto_hold_never_reports_success_when_guard_blocks(
        hold, bus, make_own_vehicle, blocked):
    system, calls = hold
    own = make_own_vehicle(speed=0, brake=0.7, local_plid=1, viewed_plid=1)
    state = {'on_track': blocked != 'off_track'}
    if blocked == 'spectating':
        own.viewed_plid = 2
    else:
        state[blocked] = True
    bus.emit('state_data', state)
    assert not system.process(own, {})['auto_hold_active']
    assert calls == []


@pytest.mark.parametrize('reset', ['disabled', 'off_track', 'moving', 'rebind'])
def test_auto_hold_can_retry_after_context_reset(hold, bus, make_own_vehicle, reset):
    system, calls = hold
    own = make_own_vehicle(speed=0, brake=0.7)
    system.process(own, {})
    if reset == 'disabled':
        system.enabled = False
        assert not system.is_enabled()
        system.enabled = True
    elif reset == 'off_track':
        bus.emit('state_data', {'on_track': False})
        bus.emit('state_data', {'on_track': True})
    elif reset == 'moving':
        own.data.speed = 5
        system.process(own, {})
        own.data.speed = 0
    else:
        system.settings.set('user_handbrake_key', 'k')
    system.process(own, {})
    assert len(calls) == 2
