"""Replay context, ``IS_STA.ViewPLID`` and the "OutGauge is silent" warning.

Three known issues meet in this file, and they meet for a reason: all three
are about the app not being able to say *what it is looking at*.

* **#55** -- a replay sets ``ISS_REPLAY`` and not ``ISS_GAME``, so ``on_track``
  was False and every system, the HUD included, sat out the one situation a
  driver would use to review an incident.
* **#51** -- ``own_vehicle.viewed_plid`` came from OutGauge alone. Wherever
  that stream is silent -- off track, a wrong ``cfg.txt``, a taken port, or a
  replay without ``OutGauge Mode = 2`` -- the value froze on its last reading
  and ``is_local_driver`` answered about a car the camera had long left.
  ``IS_STA.ViewPLID`` carries the same information over InSim, and in a replay
  it is the only source there is.
* **#24** -- the warning systems run off MCI, so a driver whose ``cfg.txt``
  has OutGauge off saw a HUD that looked healthy while half the app was blind.
"""

import pyinsim
import pytest

from assistance.manager import REPLAY_SYSTEMS, AssistanceManager
from lfs.lfs_state import SCREEN_GAME, SCREEN_MAIN_MENU, SCREEN_REPLAY, StateHandler
from misc.input_guard import (OUTGAUGE_STALE_AFTER_S, REASON_OFF_TRACK,
                              REASON_REPLAY, InputGuard)
from ui import ui_manager as ui_module
from ui.ui_manager import (BTN_HUD_SPEED, BTN_OUTGAUGE_WARNING,
                           OUTGAUGE_WARNING_TEXTS, UIManager)
from vehicles.own_vehicle import OwnVehicle
from vehicles.vehicle_manager import VehicleManager


# The replay signature measured live on 2026-09-19 (reference/ui.md §1.1):
# REPLAY | SHOW_2D | MPSPEEDUP | WINDOWED | VISIBLE, and no ISS_GAME.
REPLAY_FLAGS = pyinsim.ISS_REPLAY | pyinsim.ISS_VISIBLE


@pytest.fixture
def state(fake_connector) -> StateHandler:
    return StateHandler(fake_connector)


# ─── #55: the replay is its own screen ───────────────────────────────────────

def test_a_replay_is_not_the_main_menu(bus, recorder, state, make_sta_packet):
    """Without its own branch the replay fell into SCREEN_MAIN_MENU, where
    nothing may be drawn -- so even reading the flag would not have helped."""
    seen = recorder('state_data')
    bus.emit('game_state_changed', make_sta_packet(base_flags=REPLAY_FLAGS))

    published = seen.last('state_data')
    assert published['screen'] == SCREEN_REPLAY
    assert published['replay'] is True
    assert published['buttons_allowed'] is True
    # Still not "on track": actuation keeps hanging off this flag.
    assert published['on_track'] is False


def test_being_on_track_is_never_a_replay(bus, recorder, state,
                                          make_sta_packet):
    """ISS_REPLAY is also set while a race is being recorded to MPR."""
    seen = recorder('state_data')
    bus.emit('game_state_changed',
             make_sta_packet(on_track=True, flags=pyinsim.ISS_REPLAY))

    published = seen.last('state_data')
    assert published['screen'] == SCREEN_GAME
    assert published['replay'] is False


# ─── #55: what runs there, and what must not ─────────────────────────────────

def test_only_the_publishing_systems_run_in_a_replay(bus, settings):
    """A keystroke does nothing in a replay, but an InSim command does not go
    to the recording -- it goes to the running game."""
    manager = AssistanceManager(bus, settings)
    assert REPLAY_SYSTEMS == frozenset(('fcw', 'bsw', 'ctw', 'pdc'))
    for name in REPLAY_SYSTEMS:
        assert name in manager.systems, name
    for actuating in ('aeb', 'autoh', 'gearbox', 'lighta', 'ai_traffic'):
        assert actuating not in REPLAY_SYSTEMS


def test_the_input_guard_names_the_replay_rather_than_the_track(bus):
    """``off_track`` was true but told the driver nothing: the systems *are*
    running, the keystroke just has nowhere to land."""
    guard = InputGuard(bus, foreground_check=lambda: True)
    bus.emit('state_data', {'on_track': False, 'replay': True})
    assert guard.may_inject(OwnVehicle()) == REASON_REPLAY

    bus.emit('state_data', {'on_track': False, 'replay': False})
    assert guard.may_inject(OwnVehicle()) == REASON_OFF_TRACK


def test_the_hud_draws_in_a_replay_and_is_cleared_when_it_ends(
        bus, message_sender, settings):
    ui = UIManager(bus, message_sender, settings)
    bus.emit('state_data', {'on_track': False, 'replay': True,
                            'screen': SCREEN_REPLAY, 'buttons_allowed': True})
    ui.update_hud()
    assert message_sender.connector.last_button(BTN_HUD_SPEED) is not None

    # Back to the main menu: the buttons a replay left behind used to stay
    # there, because the clean-up path hung on ``on_track``.
    message_sender.connector.reset()
    bus.emit('state_data', {'on_track': False, 'replay': False,
                            'screen': SCREEN_MAIN_MENU,
                            'buttons_allowed': False})
    assert BTN_HUD_SPEED in message_sender.connector.deletes


# ─── #29 / #51: ViewPLID ─────────────────────────────────────────────────────

def test_is_sta_answers_whose_car_the_camera_is_on_without_outgauge(
        bus, state, make_sta_packet):
    """The bug in one line: a driver sitting in their own car was not the
    local driver, because ``viewed_plid`` had never been filled."""
    manager = VehicleManager(bus)
    manager.own_vehicle.set_local_driver(7)
    assert manager.own_vehicle.is_local_driver is False

    bus.emit('game_state_changed', make_sta_packet(on_track=True, view_plid=7))
    assert manager.own_vehicle.viewed_plid == 7
    assert manager.own_vehicle.is_local_driver is True


def test_a_camera_on_somebody_else_is_reported_as_such(bus, state,
                                                       make_sta_packet):
    """TAB in an external view: OutGauge is silent, so before this the value
    stayed frozen on our own PLID and every actuator thought it was aiming at
    our car."""
    manager = VehicleManager(bus)
    manager.own_vehicle.set_local_driver(7)
    bus.emit('game_state_changed', make_sta_packet(on_track=True, view_plid=7))

    bus.emit('game_state_changed', make_sta_packet(on_track=True, view_plid=12))
    assert manager.own_vehicle.viewed_plid == 12
    assert manager.own_vehicle.is_local_driver is False


def test_the_viewed_car_is_the_own_car_while_no_is_npl_has_arrived(bus, state,
                                                                   make_sta_packet):
    """LFS sends no IS_NPL in a replay, so ViewPLID is the only pointer to the
    car being watched (reference/ui.md §1.1)."""
    manager = VehicleManager(bus)
    bus.emit('game_state_changed',
             make_sta_packet(base_flags=REPLAY_FLAGS, view_plid=24))
    assert manager.own_vehicle.data.player_id == 24


# ─── #24: the silent OutGauge stream is now visible ──────────────────────────

def _replay_free_track(bus):
    bus.emit('state_data', {'on_track': True, 'replay': False,
                            'screen': SCREEN_GAME, 'buttons_allowed': True})


def test_nothing_is_claimed_before_the_connector_has_said_anything(
        bus, message_sender, settings):
    ui = UIManager(bus, message_sender, settings)
    _replay_free_track(bus)
    ui.update_hud()
    assert message_sender.connector.last_button(BTN_OUTGAUGE_WARNING) is None


def test_a_socket_that_never_bound_is_named_on_screen(bus, message_sender,
                                                      settings):
    ui = UIManager(bus, message_sender, settings)
    _replay_free_track(bus)
    bus.emit('outgauge_status', {'bound': False, 'reason': 'port_in_use'})
    ui.update_hud()

    drawn = message_sender.connector.last_button(BTN_OUTGAUGE_WARNING)
    assert drawn is not None
    assert drawn[6] == OUTGAUGE_WARNING_TEXTS['port_in_use'].encode('latin-1')


def test_a_bound_but_silent_socket_is_named_once_it_is_really_silent(
        bus, message_sender, settings, monkeypatch, make_outgauge_packet):
    """``cfg.txt`` with OutGauge off looks exactly like this and never
    recovers -- and every warning system kept running, so the HUD looked fine."""
    now = [1000.0]
    monkeypatch.setattr(ui_module.time, 'time', lambda: now[0])
    ui = UIManager(bus, message_sender, settings)
    _replay_free_track(bus)
    bus.emit('outgauge_status', {'bound': True, 'reason': None})
    bus.emit('outgauge_data', make_outgauge_packet())

    ui.update_hud()
    assert message_sender.connector.last_button(BTN_OUTGAUGE_WARNING) is None

    now[0] += OUTGAUGE_STALE_AFTER_S + 0.1
    ui.update_hud()
    drawn = message_sender.connector.last_button(BTN_OUTGAUGE_WARNING)
    assert drawn is not None
    assert drawn[6] == OUTGAUGE_WARNING_TEXTS['no_packets'].encode('latin-1')

    # ...and it goes away again when the stream comes back.
    bus.emit('outgauge_data', make_outgauge_packet())
    message_sender.connector.reset()
    ui.update_hud()
    assert BTN_OUTGAUGE_WARNING in message_sender.connector.deletes


# ─── #56: the silence clock must not run where silence is correct ────────────

class _Clock:
    def __init__(self):
        self.now = 1000.0

    def __call__(self):
        return self.now

    def advance(self, seconds):
        self.now += seconds


def test_time_spent_in_the_menu_is_not_outgauge_failure(bus,
                                                        make_outgauge_packet):
    """Reported live: entering the track showed "no OutGauge data" and an
    "emergency braking unavailable" notification for exactly one frame.

    LFS sends OutGauge while the player sits in a car, so the stream is
    *supposed* to stop in the menu. The staleness clock measured from the last
    packet regardless, so any visit to the menu longer than
    ``OUTGAUGE_STALE_AFTER_S`` came back as a fault -- until the first packet
    of the new session arrived a few milliseconds later.
    """
    clock = _Clock()
    guard = InputGuard(bus, foreground_check=lambda: True, clock=clock)
    bus.emit('outgauge_status', {'bound': True, 'reason': None})
    bus.emit('state_data', {'on_track': True, 'replay': False})
    bus.emit('outgauge_data', make_outgauge_packet())
    assert guard.outgauge_stale() is False

    # Off to the menu for a minute. Nothing arrives, and nothing should.
    bus.emit('state_data', {'on_track': False, 'replay': False})
    clock.advance(60.0)
    assert guard.outgauge_stale() is False

    # Back on track, first assistance pass, no packet yet: the grace period
    # starts here, not sixty seconds ago.
    bus.emit('state_data', {'on_track': True, 'replay': False})
    assert guard.outgauge_stale() is False
    assert guard.outgauge_reason() is None


def test_a_stream_that_really_stays_silent_on_track_is_still_reported(
        bus, make_outgauge_packet):
    """The grace period is a delay, not an exemption -- ``OutGauge Mode = 0``
    in ``cfg.txt`` looks like this and never recovers."""
    clock = _Clock()
    guard = InputGuard(bus, foreground_check=lambda: True, clock=clock)
    bus.emit('outgauge_status', {'bound': True, 'reason': None})
    bus.emit('state_data', {'on_track': True, 'replay': False})

    clock.advance(OUTGAUGE_STALE_AFTER_S + 0.1)
    assert guard.outgauge_stale() is True
    assert guard.outgauge_reason() == 'no_packets'


def test_the_hud_warning_does_not_flash_up_on_entering_the_track(
        bus, message_sender, settings, monkeypatch, make_outgauge_packet):
    """The same fault seen from the screen instead of from the guard."""
    now = [1000.0]
    monkeypatch.setattr(ui_module.time, 'time', lambda: now[0])
    ui = UIManager(bus, message_sender, settings)
    bus.emit('outgauge_status', {'bound': True, 'reason': None})
    _replay_free_track(bus)
    bus.emit('outgauge_data', make_outgauge_packet())

    bus.emit('state_data', {'on_track': False, 'replay': False,
                            'screen': SCREEN_MAIN_MENU,
                            'buttons_allowed': False})
    now[0] += 60.0
    _replay_free_track(bus)
    ui.update_hud()
    assert message_sender.connector.last_button(BTN_OUTGAUGE_WARNING) is None
