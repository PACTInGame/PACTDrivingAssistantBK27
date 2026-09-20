"""Gearbox availability and passive calibration, without native input."""
from types import SimpleNamespace

import pyinsim
import pytest

from assistance.gearbox import Gearbox


@pytest.fixture
def gearbox(bus, make_settings, monkeypatch, tmp_path):
    def unexpected_input(*args, **kwargs):
        pytest.fail("Passive calibration must not inject input")
    monkeypatch.setattr('assistance.gearbox.get_key_tapper', lambda: SimpleNamespace(
        tap=unexpected_input, sequence=unexpected_input))
    monkeypatch.setattr('assistance.gearbox.resolve_path', lambda *parts: str(tmp_path.joinpath(*parts)))
    return Gearbox(bus, make_settings(automatic_gearbox=False, language='en'))


@pytest.mark.parametrize('enabled', [False, True])
def test_calibration_completes_with_lfs_automatic_gears(gearbox, bus, make_own_vehicle, enabled):
    gearbox.settings.set('automatic_gearbox', enabled)
    own = make_own_vehicle(speed=0, gear=1, rpm=900, local_plid=1, plid=1,
                           player_flags=pyinsim.PIF_AUTOGEARS)
    now = [100.0]
    gearbox.clock = lambda: now[0]
    bus.emit('gearbox_calibrate', {})
    assert gearbox.is_enabled()  # manager must schedule calibration even with the toggle off
    assert not gearbox.process(own, {})['auto_gearbox_active']
    assert gearbox.calibrating
    for rpm, gear in [(900, 1), (7000, 1), (7000, 6)]:
        own.rpm, own.gear = rpm, gear
        gearbox.process(own, {})
        now[0] += gearbox.CALIBRATION_STEP_S + 0.1
        gearbox.process(own, {})
    assert not gearbox.calibrating
    assert gearbox.is_calibrated
    assert gearbox.forward_gears == 5
    assert gearbox.settings.get('automatic_gearbox') is enabled
    assert gearbox.process(own, {})['suppressed_by'] == 'lfs_auto_gears'


def test_availability_recovers_while_toggle_is_off(gearbox, bus, recorder, make_own_vehicle, monkeypatch):
    seen = recorder('gearbox_availability')
    own = make_own_vehicle(cname='XFG', player_flags=pyinsim.PIF_AUTOGEARS)
    shifts = []
    monkeypatch.setattr(gearbox, '_process_shifting', lambda own: shifts.append(own))
    for _ in range(3):
        gearbox.process(own, {})
    assert seen.count('gearbox_availability') == 1
    assert seen.last('gearbox_availability')['reason'] == 'lfs_auto_gears'
    own.data.player_flags = 0
    # VehicleData exposes the decoded help flags as a stored value.
    own.data.lfs_auto_gears = False
    gearbox.process(own, {})
    assert seen.last('gearbox_availability')['reason'] is None
    assert not shifts
    gearbox.settings.set('automatic_gearbox', True)
    gearbox.process(own, {})
    assert len(shifts) == 1
