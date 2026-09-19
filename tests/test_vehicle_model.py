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
from vehicles.vehicle_manager import (FRAME_TIMEOUT_S, STALE_VEHICLE_S,
                                      VehicleManager)


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
    # Car 2 is not in the second frame, and that frame is complete -- so it is
    # gone, not kept (known-issues #49). Before that fix the manager never
    # removed anybody and the snapshot read {2, 3} here.
    assert set(seen.payloads('vehicles_updated')[1]) == {3}


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
    # Car 2 stays in the second frame on purpose: a car that drops out of a
    # complete frame is now removed (known-issues #49), which would make the
    # dict shrink rather than grow and hide what this test is about.
    seen = recorder('vehicles_updated')
    for cars in ([2], [2, 3]):
        for packet in make_mci_frame([make_compcar(plid=p) for p in cars]):
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


# ─── Cars that vanish from MCI (known-issues #49) ────────────────────────────

def _frame(bus, make_compcar, make_mci_frame, cars):
    for packet in make_mci_frame([make_compcar(**car) for car in cars]):
        bus.emit('vehicle_data_received', packet)


def test_a_car_missing_from_a_complete_frame_is_dropped(
        bus, manager, make_npl_packet, make_compcar, make_mci_frame):
    """LFS sends no IS_PLL when a race ends -- the cars just stop appearing.

    Measured over nine consecutive scenarios: zero IS_PLL, while every car
    disappeared and came back under a different PLID each time.
    """
    _join(bus, make_npl_packet, plid=1)
    _join(bus, make_npl_packet, plid=3, ptype=PTYPE_AI)
    _frame(bus, make_compcar, make_mci_frame,
           [{'plid': 1}, {'plid': 3, 'y': 20.0}])
    assert set(manager.vehicles) == {3}

    # The next race: same connection, a new car, and PLID 3 never returns.
    _join(bus, make_npl_packet, plid=2, ptype=PTYPE_AI)
    _frame(bus, make_compcar, make_mci_frame,
           [{'plid': 1}, {'plid': 2, 'y': 15.0}])

    assert set(manager.vehicles) == {2}


def test_the_ghost_never_reaches_a_consumer(
        bus, recorder, manager, make_npl_packet, make_compcar, make_mci_frame):
    """The snapshot is what the assistance systems iterate.

    This is the failure that cost a live emergency-brake intervention: the
    ghost kept the ``distance_to_player`` of its last frame for ever, because
    ``_apply_frame`` only recomputes distances for cars *in* the frame. FCW
    then braked for a car that had not existed for two minutes.
    """
    seen = recorder('vehicles_updated')
    _join(bus, make_npl_packet, plid=1)
    _join(bus, make_npl_packet, plid=3, ptype=PTYPE_AI)
    _frame(bus, make_compcar, make_mci_frame,
           [{'plid': 1}, {'plid': 3, 'y': 6.0}])
    ghost = manager.vehicles[3]
    assert ghost.data.distance_to_player == pytest.approx(6.0, abs=0.1)

    _join(bus, make_npl_packet, plid=2, ptype=PTYPE_AI)
    _frame(bus, make_compcar, make_mci_frame,
           [{'plid': 1}, {'plid': 2, 'y': 40.0}])

    snapshot = seen.payloads('vehicles_updated')[-1]
    assert 3 not in snapshot
    # And the distance that *is* served is a fresh one.
    assert snapshot[2].data.distance_to_player == pytest.approx(40.0, abs=0.1)


def test_a_partial_frame_does_not_drop_anybody(
        bus, manager, make_npl_packet, make_compcar, make_mci_frame):
    """The timeout path publishes a fragment on purpose (known-issues #6).

    "Not in this fragment" says nothing about whether the car is still there,
    so only age may remove it.
    """
    _join(bus, make_npl_packet, plid=1)
    _join(bus, make_npl_packet, plid=3, ptype=PTYPE_AI)
    _frame(bus, make_compcar, make_mci_frame,
           [{'plid': 1}, {'plid': 3, 'y': 20.0}])
    assert set(manager.vehicles) == {3}

    manager._apply_frame([make_compcar(plid=1)], complete=False)

    assert set(manager.vehicles) == {3}


def test_a_car_missing_from_partial_frames_ages_out(
        bus, manager, make_npl_packet, make_compcar, make_mci_frame):
    _join(bus, make_npl_packet, plid=1)
    _join(bus, make_npl_packet, plid=3, ptype=PTYPE_AI)
    _frame(bus, make_compcar, make_mci_frame,
           [{'plid': 1}, {'plid': 3, 'y': 20.0}])
    manager.vehicles[3].last_seen -= STALE_VEHICLE_S + 0.1

    manager._apply_frame([make_compcar(plid=1)], complete=False)

    assert manager.vehicles == {}


def test_dropping_a_car_leaves_the_own_vehicle_alone(
        bus, manager, make_npl_packet, make_compcar, make_mci_frame,
        make_outgauge_packet):
    _join(bus, make_npl_packet, plid=1)
    _join(bus, make_npl_packet, plid=3, ptype=PTYPE_AI)
    bus.emit('outgauge_data', make_outgauge_packet(plid=1))
    _frame(bus, make_compcar, make_mci_frame,
           [{'plid': 1, 'y': 5.0}, {'plid': 3, 'y': 20.0}])

    _frame(bus, make_compcar, make_mci_frame, [{'plid': 1, 'y': 9.0}])

    assert manager.vehicles == {}
    assert manager.own_vehicle.local_plid == 1
    assert manager.own_vehicle.data.y == pytest.approx(metres(9.0))


# ─── LFS' own driving aids from the same flags (known-issues #47) ────────────

def _drive_one_frame(bus, make_compcar, make_mci_frame, plid=1):
    """One MCI frame carrying *plid*, so the NPL data reaches the vehicle."""
    for packet in make_mci_frame([make_compcar(plid=plid)]):
        bus.emit('vehicle_data_received', packet)


def test_lfs_auto_gears_reaches_the_own_vehicle(
        bus, manager, make_npl_packet, make_compcar, make_mci_frame):
    _join(bus, make_npl_packet, plid=1,
          flags=pyinsim.PIF_SWAPSIDE | pyinsim.PIF_AUTOGEARS)
    _drive_one_frame(bus, make_compcar, make_mci_frame)

    data = manager.own_vehicle.data
    assert data.player_flags == pyinsim.PIF_SWAPSIDE | pyinsim.PIF_AUTOGEARS
    assert data.lfs_auto_gears is True


def test_without_the_bit_lfs_does_not_shift(
        bus, manager, make_npl_packet, make_compcar, make_mci_frame):
    _join(bus, make_npl_packet, plid=1,
          flags=pyinsim.PIF_SWAPSIDE | pyinsim.PIF_MOUSE)
    _drive_one_frame(bus, make_compcar, make_mci_frame)

    assert manager.own_vehicle.data.lfs_auto_gears is False


def test_a_mid_session_toggle_arrives_via_is_pfl(
        bus, manager, make_npl_packet, make_pfl_packet, make_compcar,
        make_mci_frame):
    """IS_NPL comes on joining; a help toggled on track only sends IS_PFL.

    Without the IS_PFL handler the flags stayed on the joining value until
    the next pit stop.
    """
    _join(bus, make_npl_packet, plid=1, flags=pyinsim.PIF_SWAPSIDE)
    _drive_one_frame(bus, make_compcar, make_mci_frame)
    assert manager.own_vehicle.data.lfs_auto_gears is False

    bus.emit('player_flags_changed', make_pfl_packet(
        plid=1, flags=pyinsim.PIF_SWAPSIDE | pyinsim.PIF_AUTOGEARS))
    _drive_one_frame(bus, make_compcar, make_mci_frame)

    assert manager.own_vehicle.data.lfs_auto_gears is True


def test_is_pfl_also_moves_the_control_mode(
        bus, manager, make_npl_packet, make_pfl_packet):
    """Switching to mouse+keyboard mid-session changes the actuation path."""
    _join(bus, make_npl_packet, plid=1, flags=pyinsim.PIF_SWAPSIDE)
    assert manager.players[1]['ControlMode'] == 2          # wheel

    bus.emit('player_flags_changed', make_pfl_packet(
        plid=1, flags=pyinsim.PIF_SWAPSIDE | pyinsim.PIF_MOUSE))

    assert manager.players[1]['ControlMode'] == 0          # mouse


def test_is_pfl_for_an_unknown_player_is_ignored(bus, manager, make_pfl_packet):
    """A packet before the first IS_NPL carries no car and no driver."""
    bus.emit('player_flags_changed', make_pfl_packet(plid=9, flags=8))

    assert manager.players == {}


def test_is_pfl_survives_a_missing_flags_field(
        bus, manager, make_npl_packet, make_pfl_packet):
    _join(bus, make_npl_packet, plid=1, flags=pyinsim.PIF_AUTOGEARS)
    packet = make_pfl_packet(plid=1)
    del packet.Flags

    bus.emit('player_flags_changed', packet)

    assert manager.players[1]['Flags'] == 0


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


def test_an_mci_frame_also_publishes_the_own_vehicle(
        bus, recorder, manager, make_npl_packet, make_compcar, make_mci_frame):
    """MCI arrives in every camera view; OutGauge only from an internal one.

    Without this, an app started while LFS is already on track in a chase
    camera never learns that an own vehicle exists at all, and
    ``AssistanceManager.process_all_systems`` skips every pass - including the
    AI traffic, which needs nothing from OutGauge.
    """
    seen = recorder('own_vehicle_updated')
    _join(bus, make_npl_packet, plid=1, ucid=0, ptype=0)

    for packet in make_mci_frame([make_compcar(plid=1, y=12.0)]):
        bus.emit('vehicle_data_received', packet)

    assert seen.count('own_vehicle_updated') == 1
    assert seen.last('own_vehicle_updated') is not manager.own_vehicle
    assert seen.last('own_vehicle_updated').data == manager.own_vehicle.data


def test_a_race_restart_moves_the_local_plid_to_the_new_one(
        bus, manager, make_npl_packet):
    """``/restart`` re-announces the whole field and LFS may hand out other
    PLIDs. It sends **no** IS_PLL for the old ones, so without IS_RST the
    first candidate kept the title for ever - and a PLID that now belongs to
    an AI car dragged the whole own-vehicle object onto that car.
    """
    _join(bus, make_npl_packet, plid=1, ucid=0, ptype=0)
    assert manager.own_vehicle.local_plid == 1

    bus.emit('race_restarted', object())
    _join(bus, make_npl_packet, plid=5, ucid=0, ptype=0)

    assert manager.own_vehicle.local_plid == 5


def test_a_race_restart_without_a_new_player_list_keeps_the_old_plid(
        bus, manager, make_npl_packet):
    """A restart that is never followed by an IS_NPL must not blind the app."""
    _join(bus, make_npl_packet, plid=1, ucid=0, ptype=0)

    bus.emit('race_restarted', object())

    assert manager.own_vehicle.local_plid == 1


def test_a_restart_does_not_hand_the_title_to_an_ai(bus, manager, make_npl_packet):
    _join(bus, make_npl_packet, plid=1, ucid=0, ptype=0)

    bus.emit('race_restarted', object())
    _join(bus, make_npl_packet, plid=4, ucid=0, ptype=PTYPE_AI)

    assert manager.own_vehicle.local_plid == 1


def test_the_camera_car_is_not_adopted_as_our_own_while_it_is_an_ai(
        bus, manager, make_npl_packet, make_outgauge_packet, make_compcar,
        make_mci_frame):
    """Before the first IS_NPL for the local driver, OutGauge is the only
    source of an own PLID - and TAB points it at any car on track. A car LFS
    itself calls an AI is never ours: taking it would both re-point the own
    vehicle and make that car vanish from ``vehicles``, which is exactly the
    car the AI traffic needs to see.
    """
    _join(bus, make_npl_packet, plid=5, ucid=0, ptype=PTYPE_AI, pname=b'AI 1')
    bus.emit('outgauge_data', make_outgauge_packet(plid=5))
    for packet in make_mci_frame([make_compcar(plid=5, y=20.0)]):
        bus.emit('vehicle_data_received', packet)

    assert 5 in manager.vehicles
    assert manager.vehicles[5].data.is_ai is True
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
