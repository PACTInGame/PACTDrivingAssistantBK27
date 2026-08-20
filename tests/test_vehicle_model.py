"""WP4 -- vehicle data model, identity and MCI reassembly.

Covers the acceptance list of WP4: split MCI frames, a stale ``players`` dict,
snapshot immutability, control-mode parsing from the documented ``PIF_*``
masks, the own PLID surviving a camera change, and the AI flag from ``PType``.
"""


import pytest

import pyinsim
from conftest import lfs_heading, metres
from vehicles.vehicle import (PTYPE_AI, PTYPE_REMOTE, Vehicle, decode_car_name,
                              decode_player_name)
from vehicles.vehicle_manager import FRAME_TIMEOUT_S, VehicleManager


@pytest.fixture
def manager(bus) -> VehicleManager:
    return VehicleManager(bus)


def _join(bus, make_npl_packet, **kwargs):
    bus.emit('player_joined', make_npl_packet(**kwargs))


# ─── MCI frame reassembly (known-issues #6) ──────────────────────────────────

def test_split_mci_frame_produces_one_update_with_every_car(
        bus, recorder, manager, make_compcar, make_mci_frame, make_npl_packet,
        make_outgauge_packet):
    """17 cars arrive in two packets and must become exactly one snapshot."""
    seen = recorder('vehicles_updated')
    _join(bus, make_npl_packet, plid=1, pname=b'Me')
    bus.emit('outgauge_data', make_outgauge_packet(plid=1))

    cars = [make_compcar(plid=plid, y=float(plid)) for plid in range(1, 18)]
    packets = make_mci_frame(cars)
    assert len(packets) == 2                     # 16 + 1, as LFS splits it

    for packet in packets:
        bus.emit('vehicle_data_received', packet)

    assert seen.count('vehicles_updated') == 1
    snapshot = seen.last('vehicles_updated')
    # 17 cars minus our own = 16 foreign vehicles.
    assert len(snapshot) == 16
    assert set(snapshot) == set(range(2, 18))


def test_stale_players_dict_does_not_stall_updates(
        bus, recorder, manager, make_compcar, make_mci_frame, make_npl_packet,
        make_outgauge_packet):
    """The old code compared the car count against len(players) and froze."""
    seen = recorder('vehicles_updated')
    _join(bus, make_npl_packet, plid=1)
    bus.emit('outgauge_data', make_outgauge_packet(plid=1))
    # players knows one player; MCI reports three. Frame must still complete.
    cars = [make_compcar(plid=plid, y=float(plid)) for plid in (1, 2, 3)]

    for packet in make_mci_frame(cars):
        bus.emit('vehicle_data_received', packet)
    for packet in make_mci_frame(cars):
        bus.emit('vehicle_data_received', packet)

    assert seen.count('vehicles_updated') == 2
    assert set(seen.last('vehicles_updated')) == {2, 3}


def test_packet_without_cci_bits_is_treated_as_a_whole_frame(
        bus, recorder, manager, make_compcar, make_mci_frame):
    """An LFS build or mod that never sets CCI_FIRST/CCI_LAST still works."""
    seen = recorder('vehicles_updated')
    cars = [make_compcar(plid=plid) for plid in (2, 3)]

    for packet in make_mci_frame(cars, mark=False):
        bus.emit('vehicle_data_received', packet)

    assert seen.count('vehicles_updated') == 1
    assert set(seen.last('vehicles_updated')) == {2, 3}


def test_incomplete_frame_is_flushed_after_the_timeout(
        bus, recorder, manager, make_compcar, make_mci_packet):
    """CCI_LAST never arrives: publish what we have rather than freeze."""
    seen = recorder('vehicles_updated')
    opener = make_compcar(plid=2)
    opener.Info = pyinsim.CCI_FIRST
    bus.emit('vehicle_data_received', make_mci_packet([opener]))
    assert seen.count('vehicles_updated') == 0   # still waiting for CCI_LAST

    manager._frame_started -= FRAME_TIMEOUT_S + 0.1
    later = make_compcar(plid=3)
    later.Info = pyinsim.CCI_FIRST | pyinsim.CCI_LAST
    bus.emit('vehicle_data_received', make_mci_packet([later]))

    # One flush of the stale partial frame, one for the new complete frame.
    assert seen.count('vehicles_updated') == 2
    assert set(seen.payloads('vehicles_updated')[0]) == {2}
    assert set(seen.payloads('vehicles_updated')[1]) == {2, 3}


def test_empty_or_malformed_mci_packet_does_not_raise(bus, manager, make_mci_packet):
    bus.emit('vehicle_data_received', make_mci_packet([]))

    class Broken:
        pass

    bus.emit('vehicle_data_received', Broken())         # no Info attribute
    assert manager.vehicles == {}


def test_compcar_without_fields_falls_back_to_zero(bus, recorder, manager,
                                                   make_mci_packet):
    """A modded/older LFS must not raise inside the packet handler."""
    class BareCar:
        PLID = 4
        Info = pyinsim.CCI_FIRST | pyinsim.CCI_LAST

    seen = recorder('vehicles_updated')
    bus.emit('vehicle_data_received', make_mci_packet([BareCar()]))

    assert set(seen.last('vehicles_updated')) == {4}
    assert manager.vehicles[4].data.x == 0


# ─── Snapshot immutability (known-issues #12) ────────────────────────────────

def test_snapshot_is_a_fresh_dict_per_frame(
        bus, recorder, manager, make_compcar, make_mci_frame):
    seen = recorder('vehicles_updated')
    for plid in (2, 3):
        for packet in make_mci_frame([make_compcar(plid=plid)]):
            bus.emit('vehicle_data_received', packet)

    first, second = seen.payloads('vehicles_updated')
    assert first is not second
    assert set(first) == {2}          # the first snapshot did not grow
    assert set(second) == {2, 3}


def test_published_vehicle_data_is_not_mutated_by_the_next_frame(
        bus, recorder, manager, make_compcar, make_mci_frame):
    seen = recorder('vehicles_updated')
    for packet in make_mci_frame([make_compcar(plid=2, x=10.0, speed=50.0)]):
        bus.emit('vehicle_data_received', packet)
    published = seen.last('vehicles_updated')[2].data
    assert published.x == metres(10.0)

    for packet in make_mci_frame([make_compcar(plid=2, x=99.0, speed=90.0)]):
        bus.emit('vehicle_data_received', packet)

    # The object handed to the worker thread must still describe frame 1.
    assert published.x == metres(10.0)
    assert published.speed == pytest.approx(50.0, abs=0.02)
    assert manager.vehicles[2].data.x == metres(99.0)


def test_removing_a_player_does_not_change_a_published_snapshot(
        bus, recorder, manager, make_compcar, make_mci_frame, make_pll_packet):
    seen = recorder('vehicles_updated')
    for packet in make_mci_frame([make_compcar(plid=2), make_compcar(plid=3)]):
        bus.emit('vehicle_data_received', packet)
    snapshot = seen.last('vehicles_updated')

    bus.emit('player_left', make_pll_packet(plid=3))

    assert set(snapshot) == {2, 3}          # the worker keeps iterating safely
    assert set(manager.vehicles) == {2}


# ─── Control mode from PIF_* masks ───────────────────────────────────────────

@pytest.mark.parametrize("flags,expected", [
    (0, 2),                                                   # wheel
    (pyinsim.PIF_MOUSE, 0),
    (pyinsim.PIF_KB_NO_HELP, 1),
    (pyinsim.PIF_KB_STABILISED, 1),
    (pyinsim.PIF_AUTOGEARS | pyinsim.PIF_MOUSE, 0),
    (pyinsim.PIF_SHIFTER | pyinsim.PIF_AXIS_CLUTCH, 2),
    (pyinsim.PIF_INPITS | pyinsim.PIF_KB_STABILISED, 1),
    (pyinsim.PIF_CUSTOM_VIEW, 2),
])
def test_control_mode_is_derived_from_the_documented_masks(manager, flags, expected):
    assert manager._get_control_mode(flags) == expected


def test_control_mode_survives_a_missing_flags_field(bus, manager, make_npl_packet):
    packet = make_npl_packet(plid=2)
    del packet.Flags
    bus.emit('player_joined', packet)

    assert manager.players[2]['ControlMode'] == 2


# ─── Identity: own PLID, AI flag (known-issues #30, #31) ─────────────────────

def test_own_plid_comes_from_npl_and_ignores_the_camera(
        bus, manager, make_npl_packet, make_outgauge_packet):
    _join(bus, make_npl_packet, plid=1, ucid=0, ptype=0, pname=b'Me')
    _join(bus, make_npl_packet, plid=5, ucid=0, ptype=PTYPE_AI, pname=b'AI 1')
    bus.emit('outgauge_data', make_outgauge_packet(plid=1))
    assert manager.own_vehicle.data.player_id == 1

    # TAB: OutGauge now reports the AI car it is looking at.
    bus.emit('outgauge_data', make_outgauge_packet(plid=5))

    assert manager.own_vehicle.data.player_id == 1
    assert manager.own_vehicle.viewed_plid == 5
    assert manager.own_vehicle.is_local_driver is False


def test_outgauge_is_the_fallback_while_no_npl_was_seen(
        bus, manager, make_outgauge_packet):
    bus.emit('outgauge_data', make_outgauge_packet(plid=4))

    assert manager.own_vehicle.data.player_id == 4
    assert manager.own_vehicle.local_plid == 0
    # Unknown identity keeps the previous behaviour rather than disabling
    # everything that actuates.
    assert manager.own_vehicle.is_local_driver is True


def test_spectated_speed_does_not_overwrite_our_own(
        bus, manager, make_npl_packet, make_outgauge_packet, make_compcar,
        make_mci_frame):
    _join(bus, make_npl_packet, plid=1, ucid=0, ptype=0)
    for packet in make_mci_frame([make_compcar(plid=1, speed=80.0)]):
        bus.emit('vehicle_data_received', packet)
    assert manager.own_vehicle.data.speed == pytest.approx(80.0, abs=0.02)

    bus.emit('outgauge_data', make_outgauge_packet(plid=9, speed=200.0))

    assert manager.own_vehicle.data.speed == pytest.approx(80.0, abs=0.02)


def test_ai_flag_comes_from_ptype_not_from_the_name(
        bus, manager, make_npl_packet, make_compcar, make_mci_frame):
    _join(bus, make_npl_packet, plid=1, ucid=0, ptype=0, pname=b'Me')
    _join(bus, make_npl_packet, plid=2, ucid=0, ptype=PTYPE_AI, pname=b'AI 1')
    # A human whose name happens to contain "AI" - the old substring test
    # adopted this car as a traffic AI (known-issues #31).
    _join(bus, make_npl_packet, plid=3, ucid=1, ptype=PTYPE_REMOTE, pname=b'MAIK')

    cars = [make_compcar(plid=plid) for plid in (1, 2, 3)]
    for packet in make_mci_frame(cars):
        bus.emit('vehicle_data_received', packet)

    assert manager.vehicles[2].data.is_ai is True
    assert manager.vehicles[3].data.is_ai is False
    assert manager.vehicles[3].data.is_remote is True


def test_remote_and_ai_players_never_become_the_local_driver(
        bus, manager, make_npl_packet):
    _join(bus, make_npl_packet, plid=7, ucid=0, ptype=PTYPE_AI)
    _join(bus, make_npl_packet, plid=8, ucid=3, ptype=PTYPE_REMOTE)

    assert manager.own_vehicle.local_plid == 0


def test_local_driver_is_forgotten_when_the_player_leaves(
        bus, manager, make_npl_packet, make_pll_packet):
    _join(bus, make_npl_packet, plid=1, ucid=0, ptype=0)
    assert manager.own_vehicle.local_plid == 1

    bus.emit('player_left', make_pll_packet(plid=1))

    assert manager.own_vehicle.local_plid == 0


def test_own_car_is_never_listed_as_a_foreign_vehicle(
        bus, recorder, manager, make_npl_packet, make_compcar, make_mci_frame):
    seen = recorder('vehicles_updated')
    for packet in make_mci_frame([make_compcar(plid=1), make_compcar(plid=2)]):
        bus.emit('vehicle_data_received', packet)
    assert set(seen.last('vehicles_updated')) == {1, 2}   # own PLID unknown yet

    _join(bus, make_npl_packet, plid=1, ucid=0, ptype=0)
    for packet in make_mci_frame([make_compcar(plid=1), make_compcar(plid=2)]):
        bus.emit('vehicle_data_received', packet)

    assert set(seen.last('vehicles_updated')) == {2}


def test_npl_without_plid_is_ignored(bus, manager, make_npl_packet):
    bus.emit('player_joined', make_npl_packet(plid=0))

    assert manager.players == {}


# ─── Decoding (known-issues, conventions §4) ─────────────────────────────────

def test_names_are_decoded_once_at_ingress(
        bus, manager, make_npl_packet, make_compcar, make_mci_frame):
    _join(bus, make_npl_packet, plid=2, ucid=1, ptype=PTYPE_REMOTE,
          cname=b'FZ5', pname=b'[COP] Bob')
    for packet in make_mci_frame([make_compcar(plid=2)]):
        bus.emit('vehicle_data_received', packet)

    data = manager.vehicles[2].data
    assert data.cname == 'FZ5'
    assert data.pname == '[COP] Bob'
    assert data.cname_bytes == b'FZ5'
    # The repr hack str(b'[COP] Bob') used to produce this instead.
    assert not data.pname.startswith("b'")


def test_decoders_accept_bytes_and_str_and_strip_nul_padding():
    assert decode_car_name(b'XFG\x00') == 'XFG'
    assert decode_car_name('XRT') == 'XRT'
    assert decode_car_name(None) == ''
    assert decode_player_name(b'Bob') == 'Bob'
    assert decode_player_name('Bob') == 'Bob'


def test_a_mod_cname_survives_decoding(bus, manager, make_npl_packet,
                                       make_compcar, make_mci_frame):
    """Mods carry an arbitrary CName - it must round-trip, not raise."""
    _join(bus, make_npl_packet, plid=2, ucid=1, ptype=PTYPE_REMOTE,
          cname=b'\xc48\x9a\x01')
    for packet in make_mci_frame([make_compcar(plid=2)]):
        bus.emit('vehicle_data_received', packet)

    assert manager.vehicles[2].data.cname_bytes == b'\xc48\x9a\x01'
    from assistance.park_distance_control import get_vehicle_size
    assert get_vehicle_size(manager.vehicles[2].data.cname) == (4.5, 1.8)


def test_get_vehicle_size_still_resolves_a_known_car():
    from assistance.park_distance_control import get_vehicle_size
    assert get_vehicle_size('XFG') == (3.7, 1.7)
    assert get_vehicle_size(b'XFG') == (3.7, 1.7)


# ─── player_name_changed contract ────────────────────────────────────────────

def test_player_name_changed_carries_a_decoded_name(
        bus, recorder, manager, make_npl_packet, make_outgauge_packet,
        make_compcar, make_mci_frame):
    seen = recorder('player_name_changed')
    _join(bus, make_npl_packet, plid=1, ucid=0, ptype=0, pname=b'[COP] Bob')
    for packet in make_mci_frame([make_compcar(plid=1)]):
        bus.emit('vehicle_data_received', packet)

    assert seen.count('player_name_changed') == 1
    assert seen.last('player_name_changed')['player_name'] == '[COP] Bob'

    # Unchanged identity must not re-emit.
    for packet in make_mci_frame([make_compcar(plid=1)]):
        bus.emit('vehicle_data_received', packet)
    assert seen.count('player_name_changed') == 1


# ─── Vehicle frame primitives ────────────────────────────────────────────────

def test_begin_and_commit_frame_swap_the_data_object():
    vehicle = Vehicle(2)
    published = vehicle.data

    vehicle.begin_frame()
    vehicle.update_position(metres(5.0), 0, 0, lfs_heading(0.0), lfs_heading(0.0), 30.0)
    assert published.x == 0                 # not visible before the commit
    assert vehicle.data is published

    vehicle.commit_frame()
    assert vehicle.data is not published
    assert vehicle.data.x == metres(5.0)


def test_abort_frame_discards_the_staged_copy():
    vehicle = Vehicle(2)
    vehicle.begin_frame()
    vehicle.update_position(metres(5.0), 0, 0, 0, 0, 30.0)
    vehicle.abort_frame()

    assert vehicle.data.x == 0
