"""WP9 -- key injection and light commands.

Everything that reaches out of the app: the two systems that press real OS
keys (``AutoHold``, ``Gearbox``), the guard that decides whether they may
(``misc/input_guard.py``), and the light/siren commands of ``LightAssists``.

No keyboard is involved: off Windows ``misc/platform_shim`` hands out a null
module that records every call, so ``platform_shim.recorded_calls()`` answers
"was a key pressed, and which one" exactly (``reference/testing.md``).
"""

import json
import logging

import pytest

import pyinsim
from assistance.adaptive_lights import (BTN_SIREN, BTN_STROBE, LIGHT_HAZARDS,
                                        LIGHT_HIGH_BEAM, LIGHT_LOW_BEAM,
                                        LightAssists)
from assistance.auto_hold import AutoHold
from assistance.chat_commands import ChatCommandHandler
from assistance.gearbox import Gearbox
from lfs.lfs_state import StateHandler
from misc import platform_shim
from misc.input_guard import (REASON_AI_CONTROLLED, REASON_DIALOG,
                              REASON_LFS_NOT_FOCUSED, REASON_MODIFIER_HELD,
                              REASON_NO_OUTGAUGE, REASON_NOT_LOCAL_DRIVER,
                              REASON_OFF_TRACK, REASON_TEXT_ENTRY, InputGuard,
                              looks_like_lfs)
from misc.key_tap import get_key_tapper
from ui.ui_manager import BTN_SIREN as UI_BTN_SIREN
from ui.ui_manager import UIManager

from conftest import FakePacket

# The recording only exists while the real modules are absent -- which is the
# case everywhere the suite is meant to run (Linux CI, macOS).
pytestmark = pytest.mark.skipif(
    platform_shim.is_available('pyautogui'),
    reason="key injection is only observable through the null module")


# ─── Helpers ─────────────────────────────────────────────────────────────────

class FakeClock:
    """A monotonic clock a test drives by hand."""

    def __init__(self, now: float = 1000.0):
        self.now = now

    def __call__(self) -> float:
        return self.now

    def advance(self, seconds: float):
        self.now += seconds
        return self.now


def track_state(**overrides) -> dict:
    """A ``state_data`` payload as ``StateHandler`` publishes it on track."""
    state = {
        'on_track': True, 'text_entry': False, 'dialog': False,
        'track': 'SO6R', 'in_game_cam': 3, 'in_game_interface': 0,
        'submode_interface': 0, 'select_type': 0, 'screen': 'game',
        'ui_visible': True, 'shift_u': False, 'multiplayer': False,
        'buttons_allowed': True,
    }
    state.update(overrides)
    return state


def keys_pressed():
    """Every key ``pyautogui.keyDown`` was called with since the last reset.

    The keystroke no longer happens on the caller's thread: ``AutoHold`` and
    ``Gearbox`` hand it to the shared :class:`misc.key_tap.KeyTapper`, which
    presses, holds and releases on its own thread (known-issues #43). So wait
    for the tapper to run dry first. With nothing scheduled that is immediate,
    which keeps the "no key was pressed" assertions fast.
    """
    assert get_key_tapper().wait_idle(timeout=3.0), "the key tapper never finished"
    return [args[0] for path, args, _ in platform_shim.recorded_calls()
            if path == 'pyautogui.keyDown' and args]


@pytest.fixture(autouse=True)
def _clean_recording():
    platform_shim.reset_recorded_calls()
    yield
    platform_shim.reset_recorded_calls()


@pytest.fixture
def auto_hold(bus, make_settings):
    return AutoHold(bus, make_settings(auto_hold=True, language='en'))


@pytest.fixture
def stopped_car(make_own_vehicle):
    """Standing still with the brake pressed and the handbrake light off."""
    return make_own_vehicle(speed=0.0, brake=0.6, local_plid=1, plid=1)


def with_clock(system, clock=None):
    """Puts a system on a fake clock and re-bases the timers it started with.

    ``__init__`` stamped them from ``time.perf_counter``, which has nothing to
    do with the fake clock's origin.
    """
    clock = clock or FakeClock()
    system.clock = clock
    if hasattr(system, 'adaptive_brake_light_timer'):
        system.adaptive_brake_light_timer = clock() - 10.0
    if hasattr(system, '_strobe_step_at'):
        system._strobe_step_at = clock() - 10.0
    return clock


def light_payloads(seen, *light_ids):
    return [p for p in seen.payloads('send_light_command')
            if not light_ids or p.get('light') in light_ids]


# ─── The guard itself ────────────────────────────────────────────────────────

def test_guard_allows_injection_in_the_normal_case(bus, make_own_vehicle):
    guard = InputGuard(bus, foreground_check=lambda: True)
    bus.emit('state_data', track_state())
    assert guard.may_inject(make_own_vehicle(local_plid=1, plid=1)) is None


@pytest.mark.parametrize("state,expected", [
    ({'on_track': False}, REASON_OFF_TRACK),
    ({'text_entry': True}, REASON_TEXT_ENTRY),
    ({'dialog': True}, REASON_DIALOG),
])
def test_guard_refuses_by_screen_state(bus, make_own_vehicle, state, expected):
    guard = InputGuard(bus, foreground_check=lambda: True)
    bus.emit('state_data', track_state(**state))
    assert guard.may_inject(make_own_vehicle(local_plid=1, plid=1)) == expected


def test_guard_refuses_while_shift_is_held(bus, make_own_vehicle, make_outgauge_packet):
    clock = FakeClock()
    guard = InputGuard(bus, foreground_check=lambda: True, clock=clock)
    bus.emit('state_data', track_state())
    bus.emit('outgauge_data', make_outgauge_packet(flags=pyinsim.OG_SHIFT))
    assert guard.may_inject(make_own_vehicle(local_plid=1, plid=1)) == REASON_MODIFIER_HELD


def test_guard_refuses_while_ctrl_is_held(bus, make_own_vehicle, make_outgauge_packet):
    clock = FakeClock()
    guard = InputGuard(bus, foreground_check=lambda: True, clock=clock)
    bus.emit('state_data', track_state())
    bus.emit('outgauge_data', make_outgauge_packet(flags=pyinsim.OG_CTRL))
    assert guard.may_inject(make_own_vehicle(local_plid=1, plid=1)) == REASON_MODIFIER_HELD


def test_guard_ignores_a_stale_modifier_reading(bus, make_own_vehicle, make_outgauge_packet):
    """A lost OutGauge stream must not disable the feature for good."""
    clock = FakeClock()
    guard = InputGuard(bus, foreground_check=lambda: True, clock=clock)
    bus.emit('state_data', track_state())
    bus.emit('outgauge_data', make_outgauge_packet(flags=pyinsim.OG_SHIFT))
    clock.advance(5.0)
    assert guard.may_inject(make_own_vehicle(local_plid=1, plid=1)) is None


@pytest.mark.parametrize("title, process, expected", [
    ("Live for Speed", "LFS.exe", True),        # the normal case
    ("LFS 0.7E", "LFS.exe", True),              # any title, LFS is the process
    ("Live for Speed", "renamed.exe", True),    # renamed exe, title still says it
    ("LFS Forum - Mozilla Firefox", "firefox.exe", False),
    ("C:\\LFS - File Explorer", "explorer.exe", False),
    ("", "", False),
    (None, None, False),
])
def test_looks_like_lfs_does_not_match_anything_that_merely_says_lfs(
        title, process, expected):
    """The process name decides; the title is only a fallback.

    A bare "lfs" title substring matches a browser tab on the LFS forum and a
    file manager in a folder called LFS - i.e. exactly the applications the
    foreground check exists to protect from an injected keystroke.
    """
    assert looks_like_lfs(title, process) is expected


def test_guard_refuses_when_lfs_is_not_the_foreground_window(bus, make_own_vehicle):
    guard = InputGuard(bus, foreground_check=lambda: False)
    bus.emit('state_data', track_state())
    assert guard.may_inject(make_own_vehicle(local_plid=1, plid=1)) == REASON_LFS_NOT_FOCUSED


def test_guard_refuses_while_outgauge_describes_another_car(bus, make_own_vehicle):
    """TAB points OutGauge at someone else -- do not shift on their rpm."""
    guard = InputGuard(bus, foreground_check=lambda: True)
    bus.emit('state_data', track_state())
    spectating = make_own_vehicle(local_plid=1, viewed_plid=7)
    assert spectating.is_local_driver is False
    assert guard.may_inject(spectating) == REASON_NOT_LOCAL_DRIVER


def test_guard_refuses_a_car_lfs_drives_itself(bus, make_own_vehicle):
    guard = InputGuard(bus, foreground_check=lambda: True)
    bus.emit('state_data', track_state())
    own = make_own_vehicle(local_plid=1, plid=1)
    own.data.is_ai = True
    assert guard.may_inject(own) == REASON_AI_CONTROLLED


def test_guard_refuses_when_outgauge_never_bound(bus, make_own_vehicle):
    """Port 30000 taken by something else -- known-issues #51.

    The refusal used to be ``not_local_driver``, which is what the variable
    said and not what was wrong: the driver was sitting in their own car, and
    ``viewed_plid`` was simply never filled because no packet ever arrived.
    """
    clock = FakeClock()
    guard = InputGuard(bus, foreground_check=lambda: True, clock=clock)
    bus.emit('state_data', track_state())
    bus.emit('outgauge_status', {'bound': False, 'reason': 'port_in_use'})

    assert guard.may_inject(make_own_vehicle(local_plid=1, plid=1))         == REASON_NO_OUTGAUGE
    assert guard.outgauge_reason() == 'port_in_use'


def test_guard_refuses_when_the_bound_stream_falls_silent(
        bus, make_own_vehicle, make_outgauge_packet):
    """The camera left the cockpit, or ``OutGauge Mode`` is 0 (conventions §5.3).

    Note the difference to the modifier reading, which goes back to "unknown"
    when it ages out: this one fails *closed*. A stale Shift flag disables a
    feature; a stale stream means nothing knows whose car it is looking at.
    """
    clock = FakeClock()
    guard = InputGuard(bus, foreground_check=lambda: True, clock=clock)
    bus.emit('state_data', track_state())
    bus.emit('outgauge_status', {'bound': True, 'reason': None})
    bus.emit('outgauge_data', make_outgauge_packet())
    assert guard.may_inject(make_own_vehicle(local_plid=1, plid=1)) is None

    clock.advance(5.0)
    assert guard.may_inject(make_own_vehicle(local_plid=1, plid=1))         == REASON_NO_OUTGAUGE
    assert guard.outgauge_reason() == 'no_packets'

    bus.emit('outgauge_data', make_outgauge_packet())
    assert guard.may_inject(make_own_vehicle(local_plid=1, plid=1)) is None


def test_guard_without_a_connector_does_not_invent_an_outgauge_fault(
        bus, make_own_vehicle):
    """Nobody said whether the socket is open, so the guard does not guess.

    Same rule as :func:`lfs_has_focus`: refuse because the user really is
    elsewhere, never because we could not ask.
    """
    guard = InputGuard(bus, foreground_check=lambda: True, clock=FakeClock())
    bus.emit('state_data', track_state())
    assert guard.outgauge_stale() is False
    assert guard.may_inject(make_own_vehicle(local_plid=1, plid=1)) is None


def test_guard_reads_the_real_state_data_payload(bus, fake_connector, make_sta_packet):
    """The keys the guard reads are the keys StateHandler really publishes."""
    guard = InputGuard(bus, foreground_check=lambda: True)
    StateHandler(fake_connector)

    bus.emit('game_state_changed', make_sta_packet(on_track=True))
    assert guard.on_track is True
    bus.emit('game_state_changed', make_sta_packet(on_track=True, flags=pyinsim.ISS_TEXT_ENTRY))
    assert guard.text_entry is True
    bus.emit('game_state_changed', make_sta_packet(on_track=True, flags=pyinsim.ISS_DIALOG))
    assert guard.dialog is True
    bus.emit('game_state_changed', make_sta_packet(on_track=False))
    assert guard.on_track is False


# ─── AutoHold ────────────────────────────────────────────────────────────────

def test_auto_hold_presses_the_handbrake_key_when_everything_is_allowed(
        bus, auto_hold, stopped_car, recorder):
    seen = recorder('notification')
    auto_hold.guard.foreground_check = lambda: True
    bus.emit('state_data', track_state())

    result = auto_hold.process(stopped_car, {})

    assert result['auto_hold_active'] is False
    assert keys_pressed() == ['q']
    assert seen.count('notification') == 0
    stopped_car.handbrake_light = True
    assert auto_hold.process(stopped_car, {})['auto_hold_active'] is True
    assert seen.count('notification') == 1


@pytest.mark.parametrize("state", [
    {'on_track': False}, {'text_entry': True}, {'dialog': True},
])
def test_auto_hold_presses_no_key_in_a_blocked_screen_state(
        bus, auto_hold, stopped_car, state):
    auto_hold.guard.foreground_check = lambda: True
    bus.emit('state_data', track_state(**state))

    auto_hold.process(stopped_car, {})

    assert keys_pressed() == []


def test_auto_hold_presses_no_key_while_shift_is_held(
        bus, auto_hold, stopped_car, make_outgauge_packet):
    auto_hold.guard.foreground_check = lambda: True
    bus.emit('state_data', track_state())
    bus.emit('outgauge_data', make_outgauge_packet(flags=pyinsim.OG_SHIFT))

    auto_hold.process(stopped_car, {})

    assert keys_pressed() == []


def test_auto_hold_presses_no_key_while_lfs_is_in_the_background(
        bus, auto_hold, stopped_car):
    auto_hold.guard.foreground_check = lambda: False
    bus.emit('state_data', track_state())

    auto_hold.process(stopped_car, {})

    assert keys_pressed() == []


def test_auto_hold_presses_no_key_while_spectating(bus, auto_hold, make_own_vehicle):
    auto_hold.guard.foreground_check = lambda: True
    bus.emit('state_data', track_state())
    spectated = make_own_vehicle(speed=0.0, brake=0.6, local_plid=1, viewed_plid=7)

    auto_hold.process(spectated, {})

    assert keys_pressed() == []


def test_auto_hold_key_rebind_takes_effect_without_a_restart(
        bus, auto_hold, stopped_car):
    auto_hold.guard.foreground_check = lambda: True
    bus.emit('state_data', track_state())
    auto_hold.process(stopped_car, {})
    assert keys_pressed() == ['q']

    auto_hold.settings.set('user_handbrake_key', 'k')
    platform_shim.reset_recorded_calls()
    auto_hold.process(stopped_car, {})

    assert keys_pressed() == ['k']


def test_auto_hold_does_nothing_once_the_handbrake_light_is_on(
        bus, auto_hold, make_own_vehicle):
    auto_hold.guard.foreground_check = lambda: True
    bus.emit('state_data', track_state())
    holding = make_own_vehicle(speed=0.0, brake=0.6, local_plid=1, plid=1,
                               handbrake=True)

    result = auto_hold.process(holding, {})

    assert result['auto_hold_active'] is True
    assert keys_pressed() == []


# ─── Gearbox ─────────────────────────────────────────────────────────────────

@pytest.fixture
def gearbox_factory(bus, make_settings, tmp_path, monkeypatch):
    """A calibrated gearbox on a fake clock, with its own calibration file."""
    monkeypatch.setattr('assistance.gearbox.resolve_path',
                        lambda *parts: str(tmp_path.joinpath(*parts)))

    def _make(clock=None, calibrated=True, **overrides):
        settings = make_settings(automatic_gearbox=True, language='en', **overrides)
        gearbox = Gearbox(bus, settings)
        gearbox.clock = clock or FakeClock()
        gearbox.time_in_step = gearbox.clock()
        # Far enough back that the shift cooldown is over.
        gearbox.time_since_last_gear_change = gearbox.clock() - 10.0
        # And the drivetrain has been closed and settled for a while, as it
        # is on any car that has simply been driving (known-issues #47).
        gearbox._clutch_closed_since = gearbox.clock() - 10.0
        gearbox.guard.foreground_check = lambda: True
        if calibrated:
            gearbox.idle, gearbox.redline, gearbox.forward_gears = 900, 7000, 6
            gearbox.car = 'XFG'
            # As if these had come out of the driver's own calibration file:
            # they are then final and the stock table / learned profile must
            # not overwrite them per cycle.
            gearbox._from_calibration_file = True
        return gearbox

    return _make


@pytest.fixture
def revving_car(make_own_vehicle):
    """Third gear at 6500 rpm, full throttle -- an upshift is due."""
    return make_own_vehicle(speed=90, gear=4, rpm=6800, throttle=1.0,
                            local_plid=1, plid=1)


def test_gearbox_shifts_up_when_everything_is_allowed(bus, gearbox_factory, revving_car):
    gearbox = gearbox_factory()
    bus.emit('state_data', track_state())

    gearbox.process(revving_car, {})

    assert keys_pressed() == ['c', 's']       # clutch, shift up


def test_every_executed_shift_is_logged(bus, gearbox_factory, revving_car,
                                        caplog):
    """The gearbox takes the car out of the driver's hands and used to say
    nothing about it, unlike every other actuator here.

    That is what made known-issues #47 unverifiable: the tracer gets no
    OutGauge while the add-on holds port 30000, and MCI carries no gear, so a
    whole live run on 2026-09-20 produced no evidence at all. The line carries
    the fields the two suspected faults are recognised by -- several gears at
    constant speed, and an upshift under braking.
    """
    gearbox = gearbox_factory()
    bus.emit('state_data', track_state())

    with caplog.at_level(logging.INFO, logger='assistance.gearbox'):
        gearbox.process(revving_car, {})

    # Also drains the key tapper's thread, so the keystrokes cannot land in
    # the next test's recording -- and proves the line describes a shift that
    # really happened.
    assert keys_pressed() == ['c', 's']
    lines = [r.getMessage() for r in caplog.records
             if r.getMessage().startswith('Gearbox shift')]
    assert len(lines) == 1
    assert 'up' in lines[0]
    assert '90.0 km/h' in lines[0]
    assert '6800 min-1' in lines[0]


def test_a_refused_shift_is_not_logged_as_one(bus, gearbox_factory,
                                              revving_car, caplog):
    """A log line that says "shift" when no key was pressed would be worse
    than no line at all."""
    gearbox = gearbox_factory()
    bus.emit('state_data', track_state(on_track=False))

    with caplog.at_level(logging.INFO, logger='assistance.gearbox'):
        gearbox.process(revving_car, {})

    assert keys_pressed() == []
    assert [r for r in caplog.records
            if r.getMessage().startswith('Gearbox shift')] == []


@pytest.mark.parametrize("state", [
    {'on_track': False}, {'text_entry': True}, {'dialog': True},
])
def test_gearbox_presses_no_key_in_a_blocked_screen_state(
        bus, gearbox_factory, revving_car, state):
    """The gearbox used to check nothing at all (known-issues #11)."""
    gearbox = gearbox_factory()
    bus.emit('state_data', track_state(**state))

    gearbox.process(revving_car, {})

    assert keys_pressed() == []


def test_gearbox_presses_no_key_while_shift_is_held(
        bus, gearbox_factory, revving_car, make_outgauge_packet):
    gearbox = gearbox_factory()
    bus.emit('state_data', track_state())
    bus.emit('outgauge_data', make_outgauge_packet(flags=pyinsim.OG_SHIFT))

    gearbox.process(revving_car, {})

    assert keys_pressed() == []


def test_gearbox_presses_no_key_while_lfs_is_in_the_background(
        bus, gearbox_factory, revving_car):
    gearbox = gearbox_factory()
    gearbox.guard.foreground_check = lambda: False
    bus.emit('state_data', track_state())

    gearbox.process(revving_car, {})

    assert keys_pressed() == []


def test_a_refused_shift_does_not_start_the_cooldown(
        bus, gearbox_factory, revving_car):
    """Blocked is not shifted: the next allowed cycle must still shift."""
    clock = FakeClock()
    gearbox = gearbox_factory(clock=clock)
    bus.emit('state_data', track_state(text_entry=True))
    gearbox.process(revving_car, {})
    assert keys_pressed() == []

    bus.emit('state_data', track_state())
    gearbox.process(revving_car, {})

    assert keys_pressed() == ['c', 's']


def test_gearbox_key_rebind_takes_effect_without_a_restart(
        bus, gearbox_factory, revving_car):
    clock = FakeClock()
    gearbox = gearbox_factory(clock=clock)
    bus.emit('state_data', track_state())
    gearbox.process(revving_car, {})
    assert keys_pressed() == ['c', 's']

    gearbox.settings.set('user_shift_up_key', 'e')
    gearbox.settings.set('user_clutch_key', 'v')
    platform_shim.reset_recorded_calls()
    clock.advance(2.0)
    gearbox.process(revving_car, {})

    assert keys_pressed() == ['v', 'e']


def test_gearbox_does_not_shift_beyond_the_highest_gear(
        bus, gearbox_factory, make_own_vehicle):
    gearbox = gearbox_factory()          # 6 forward gears -> top index 7
    bus.emit('state_data', track_state())
    top_gear = make_own_vehicle(speed=200, gear=7, rpm=6900, throttle=1.0,
                                local_plid=1, plid=1)

    gearbox.process(top_gear, {})

    assert keys_pressed() == []


def test_gearbox_does_nothing_without_a_calibration(
        bus, gearbox_factory, revving_car):
    gearbox = gearbox_factory(calibrated=False)
    gearbox.car = 'XFG'
    bus.emit('state_data', track_state())

    gearbox.process(revving_car, {})

    assert keys_pressed() == []


# ─── The shift decision needs a closed drivetrain (known-issues #47) ─────────

def test_no_shift_while_the_clutch_is_open(bus, gearbox_factory, make_own_vehicle):
    """A free-revving engine says nothing about which gear is right.

    This is the salvo: the gearbox held its own clutch, read the engine on
    the limiter, and called that "still too high a gear".
    """
    gearbox = gearbox_factory()
    bus.emit('state_data', track_state())
    declutched = make_own_vehicle(speed=55, gear=4, rpm=6980, throttle=1.0,
                                  clutch=1.0, local_plid=1, plid=1)

    gearbox.process(declutched, {})

    assert keys_pressed() == []


def test_no_shift_until_the_rpm_has_settled(bus, gearbox_factory, make_own_vehicle):
    """Right after the clutch closes the engine is still on the old speed."""
    clock = FakeClock()
    gearbox = gearbox_factory(clock=clock)
    bus.emit('state_data', track_state())
    revving = dict(speed=55, gear=4, rpm=6980, throttle=1.0, local_plid=1, plid=1)

    gearbox.process(make_own_vehicle(clutch=1.0, **revving), {})
    clock.advance(0.1)
    gearbox.process(make_own_vehicle(clutch=0.0, **revving), {})   # just closed
    assert keys_pressed() == []

    clock.advance(Gearbox.RPM_SETTLE_S - 0.01)
    gearbox.process(make_own_vehicle(clutch=0.0, **revving), {})
    assert keys_pressed() == []

    clock.advance(0.02)
    gearbox.process(make_own_vehicle(clutch=0.0, **revving), {})

    assert keys_pressed() == ['c', 's']


def test_the_settle_timer_restarts_when_the_clutch_opens_again(
        bus, gearbox_factory, make_own_vehicle):
    """Half-closing and re-opening must not count towards the settle time."""
    clock = FakeClock()
    gearbox = gearbox_factory(clock=clock)
    bus.emit('state_data', track_state())
    revving = dict(speed=55, gear=4, rpm=6980, throttle=1.0, local_plid=1, plid=1)

    gearbox.process(make_own_vehicle(clutch=1.0, **revving), {})   # open
    clock.advance(0.2)
    gearbox.process(make_own_vehicle(clutch=0.0, **revving), {})   # closing
    clock.advance(0.2)
    gearbox.process(make_own_vehicle(clutch=0.8, **revving), {})   # open again
    clock.advance(0.2)
    gearbox.process(make_own_vehicle(clutch=0.0, **revving), {})

    assert keys_pressed() == []


def test_a_full_throttle_run_no_longer_walks_up_the_whole_box(
        bus, gearbox_factory, make_own_vehicle):
    """The measured failure of #47, reproduced against the clock.

    The engine is pinned on the limiter because the gearbox's own clutch is
    open -- which is what a 100 ms cycle really sees during the 0.30 s hold
    plus the ~0.18 s ramp with which LFS lets the clutch come back. The old
    code shifted every 0.40 s regardless and walked 2 -> 3 -> 4 -> 5.
    """
    clock = FakeClock()
    gearbox = gearbox_factory(clock=clock)
    bus.emit('state_data', track_state())
    gear = 4

    def cycle(clutch):
        return make_own_vehicle(speed=55, gear=gear, rpm=6980, throttle=1.0,
                                clutch=clutch, local_plid=1, plid=1)

    gearbox.process(cycle(0.0), {})
    assert keys_pressed() == ['c', 's']       # the one shift that is due
    gear += 1

    # 0.48 s of open clutch, at the 100 ms assistance rate, then closed.
    platform_shim.reset_recorded_calls()
    for _ in range(5):
        clock.advance(0.1)
        gearbox.process(cycle(1.0), {})
    for _ in range(2):
        clock.advance(0.1)
        gearbox.process(cycle(0.0), {})

    assert keys_pressed() == []


def test_no_upshift_under_braking(bus, gearbox_factory, make_own_vehicle):
    """Upshifting during an intervention removes the engine braking."""
    gearbox = gearbox_factory()
    bus.emit('state_data', track_state())
    braking = make_own_vehicle(speed=55, gear=4, rpm=6980, throttle=1.0,
                               brake=1.0, local_plid=1, plid=1)

    gearbox.process(braking, {})

    assert keys_pressed() == []


def test_downshifting_under_braking_still_works(
        bus, gearbox_factory, make_own_vehicle):
    """The brake blocks the *up*shift only -- engine braking is wanted."""
    gearbox = gearbox_factory()
    bus.emit('state_data', track_state())
    slowing = make_own_vehicle(speed=30, gear=5, rpm=1200, throttle=0.0,
                               brake=1.0, local_plid=1, plid=1)

    gearbox.process(slowing, {})

    assert keys_pressed() == ['c', 'x']       # clutch, shift down


# ─── Gearbox versus LFS' own automatic gearbox (known-issues #47) ────────────

def gearbox_reasons(seen):
    return [p.get('reason') for p in seen.payloads('gearbox_availability')]


def test_gearbox_stands_down_when_lfs_shifts_by_itself(
        bus, gearbox_factory, make_own_vehicle):
    """Two automatics on one crankshaft fight each other.

    LFS turns ``PIF_AUTOGEARS`` on by itself for mouse/keyboard drivers
    (``control-intervention.md`` §2.1), so this is not an exotic setup.
    """
    gearbox = gearbox_factory()
    bus.emit('state_data', track_state())
    revving = make_own_vehicle(speed=90, gear=4, rpm=6800, throttle=1.0,
                               local_plid=1, plid=1,
                               player_flags=pyinsim.PIF_AUTOGEARS)

    result = gearbox.process(revving, {})

    assert keys_pressed() == []
    assert result['auto_gearbox_active'] is False
    assert result['suppressed_by'] == 'lfs_auto_gears'


def test_gearbox_shifts_again_when_lfs_stops_shifting(
        bus, gearbox_factory, make_own_vehicle, recorder):
    """The suppression follows the flag, in both directions."""
    seen = recorder('gearbox_availability')
    clock = FakeClock()
    gearbox = gearbox_factory(clock=clock)
    bus.emit('state_data', track_state())
    with_lfs_auto = make_own_vehicle(speed=90, gear=4, rpm=6800, throttle=1.0,
                                     local_plid=1, plid=1,
                                     player_flags=pyinsim.PIF_AUTOGEARS)
    gearbox.process(with_lfs_auto, {})
    assert keys_pressed() == []

    # The driver switched it off in Options -> Controls; LFS reports the new
    # flags on IS_PFL and the next MCI carries them into the vehicle data.
    clock.advance(2.0)
    manual = make_own_vehicle(speed=90, gear=4, rpm=6800, throttle=1.0,
                              local_plid=1, plid=1,
                              player_flags=pyinsim.PIF_SWAPSIDE)
    gearbox.process(manual, {})

    assert keys_pressed() == ['c', 's']
    assert gearbox_reasons(seen) == ['lfs_auto_gears', None]


def test_the_suppression_is_reported_once_not_once_per_cycle(
        bus, gearbox_factory, make_own_vehicle, recorder):
    """The event repaints the menu -- one per cycle would be a button storm."""
    seen = recorder('gearbox_availability')
    clock = FakeClock()
    gearbox = gearbox_factory(clock=clock)
    bus.emit('state_data', track_state())
    revving = make_own_vehicle(speed=90, gear=4, rpm=6800, throttle=1.0,
                               local_plid=1, plid=1,
                               player_flags=pyinsim.PIF_AUTOGEARS)

    for _ in range(10):
        clock.advance(0.1)
        gearbox.process(revving, {})

    assert gearbox_reasons(seen) == ['lfs_auto_gears']


def test_calibration_can_start_while_lfs_shifts_by_itself(
        bus, gearbox_factory, make_own_vehicle):
    """Calibration observes telemetry without issuing shifts."""
    gearbox = gearbox_factory(calibrated=False)
    bus.emit('state_data', track_state())
    standing = make_own_vehicle(speed=0.0, gear=1, rpm=900, local_plid=1,
                                plid=1, player_flags=pyinsim.PIF_AUTOGEARS)

    bus.emit('gearbox_calibrate', {})
    gearbox.process(standing, {})

    assert gearbox.calibrating is True


def test_a_running_calibration_continues_when_lfs_takes_over(
        bus, gearbox_factory, make_own_vehicle):
    clock = FakeClock()
    gearbox = gearbox_factory(clock=clock, calibrated=False)
    bus.emit('state_data', track_state())
    standing = make_own_vehicle(speed=0.0, gear=1, rpm=900, local_plid=1, plid=1)
    bus.emit('gearbox_calibrate', {})
    gearbox.process(standing, {})
    assert gearbox.calibrating is True

    clock.advance(1.0)
    lfs_took_over = make_own_vehicle(speed=0.0, gear=1, rpm=900, local_plid=1,
                                     plid=1,
                                     player_flags=pyinsim.PIF_AUTOGEARS)
    gearbox.process(lfs_took_over, {})

    assert gearbox.calibrating is True


def test_unknown_flags_leave_the_gearbox_working(bus, gearbox_factory, revving_car):
    """Before the first IS_NPL the flags are 0 -- that must not switch it off.

    ``revving_car`` carries no flags at all, which is exactly that state.
    """
    gearbox = gearbox_factory()
    bus.emit('state_data', track_state())

    gearbox.process(revving_car, {})

    assert keys_pressed() == ['c', 's']


# ─── Gearbox calibration ─────────────────────────────────────────────────────

def notifications(seen):
    return [p['notification'] for p in seen.payloads('notification')]


def run_calibration(gearbox, clock, bus, own_vehicle, gear_at_the_end=8,
                    redline_rpm=7000, gear_at_the_very_end=None):
    """Drives the three 12 s steps to the end, doing what each one asks for.

    Every step is *measured over its whole 12 s* — lowest rpm, highest rpm,
    highest gear — so this has to actually idle, actually rev and actually
    shift, not just sit at one value. ``gear_at_the_very_end`` is what the gear
    has fallen back to when the step expires; LFS drops a standing car back into
    neutral, which is what broke the calibration in the field.
    """
    bus.emit('gearbox_calibrate', {})
    gearbox.process(own_vehicle, {})            # step 0: idle

    clock.advance(12.1)
    gearbox.process(own_vehicle, {})            # -> idle stored, step 1 starts

    own_vehicle.rpm = redline_rpm               # the driver revs it
    gearbox.process(own_vehicle, {})
    clock.advance(12.1)
    gearbox.process(own_vehicle, {})            # -> redline stored, step 2 starts

    own_vehicle.gear = gear_at_the_end          # the driver shifts up
    gearbox.process(own_vehicle, {})
    if gear_at_the_very_end is not None:
        own_vehicle.gear = gear_at_the_very_end
    clock.advance(12.1)
    gearbox.process(own_vehicle, {})


def test_calibration_publishes_a_live_panel_instead_of_queued_messages(
        bus, gearbox_factory, make_own_vehicle, recorder):
    """The prompts must not go through the 3-s-per-message notification queue.

    A step emitted five of them for twelve seconds of display time, so the
    driver read the *previous* step's instruction and the calibration measured
    something they were no longer doing (`reference/ui.md` §1.7). The panel
    carries the current prompt, the remaining time and what has been measured
    so far, and it is republished every cycle.
    """
    seen = recorder('gearbox_calibration_state')
    notified = recorder('notification')
    clock = FakeClock()
    gearbox = gearbox_factory(clock=clock, calibrated=False)
    standing = make_own_vehicle(speed=0.0, rpm=900, cname=b'ZZZ',
                                local_plid=1, plid=1)
    gearbox.car = 'ZZZ'

    bus.emit('gearbox_calibrate', {})
    gearbox.process(standing, {})
    clock.advance(6.1)
    gearbox.process(standing, {})

    states = seen.payloads('gearbox_calibration_state')
    assert states, "the calibration published no state at all"
    assert all(state['active'] for state in states)
    assert states[0]['remaining'] == pytest.approx(12.0, abs=0.01)
    assert states[-1]['remaining'] == pytest.approx(5.9, abs=0.01)
    assert 'idle' in states[-1]['prompt'].lower()
    # The step's live reading, so the driver can see it is being measured.
    assert '900' in states[-1]['reading']
    # And none of that reached the queue.
    assert not any('12 s' in text or '6 s' in text for text in notifications(notified))


def test_the_calibration_panel_is_taken_down_when_it_ends(
        bus, gearbox_factory, make_own_vehicle, recorder):
    seen = recorder('gearbox_calibration_state')
    clock = FakeClock()
    gearbox = gearbox_factory(clock=clock, calibrated=False)
    standing = make_own_vehicle(speed=0.0, rpm=900, cname=b'ZZZ',
                                local_plid=1, plid=1)
    gearbox.car = 'ZZZ'

    run_calibration(gearbox, clock, bus, standing)

    states = seen.payloads('gearbox_calibration_state')
    assert states[-1] == {'active': False}


# ─── Values without a calibration ────────────────────────────────────────────

class FakeProfiles:
    """Stands in for ``CarProfiles`` -- the gearbox only ever queries it."""

    def __init__(self, idle=None, redline=None, forward_gears=0):
        self._idle, self._redline, self._gears = idle, redline, forward_gears

    def idle(self, car):
        return self._idle

    def redline(self, car):
        return self._redline

    def forward_gears(self, car):
        return self._gears


def test_a_car_with_no_calibration_shifts_on_measured_values(
        bus, make_settings, make_own_vehicle, tmp_path, monkeypatch):
    """36 s of standing still should not be the price of an automatic gearbox."""
    monkeypatch.setattr('assistance.gearbox.resolve_path',
                        lambda *parts: str(tmp_path.joinpath(*parts)))
    settings = make_settings(automatic_gearbox=True, language='en')
    gearbox = Gearbox(bus, settings,
                      car_profiles=FakeProfiles(idle=950, redline=7979,
                                                forward_gears=5))
    gearbox.clock = FakeClock()
    # Far enough back that the shift cooldown is over (the clock was swapped
    # after __init__ took its first reading), and the drivetrain has been
    # closed long enough to decide on (known-issues #47).
    gearbox.time_since_last_gear_change = gearbox.clock() - 10.0
    gearbox._clutch_closed_since = gearbox.clock() - 10.0
    gearbox.guard.foreground_check = lambda: True
    bus.emit('state_data', track_state())
    revving = make_own_vehicle(speed=90, gear=4, rpm=7800, throttle=1.0,
                               local_plid=1, plid=1)

    gearbox.process(revving, {})

    assert gearbox.is_calibrated is True
    assert (gearbox.idle, gearbox.redline, gearbox.forward_gears) == (950, 7979, 5)
    assert keys_pressed() == ['c', 's']


def test_measured_values_without_a_usable_rev_range_do_not_arm_the_gearbox(
        bus, make_settings, make_own_vehicle, tmp_path, monkeypatch):
    """A car that has never been revved has no shift points to compute.

    A mod name throughout, so the built-in ``STOCK_PROFILES`` cannot answer
    instead of the measurement under test.
    """
    monkeypatch.setattr('assistance.gearbox.resolve_path',
                        lambda *parts: str(tmp_path.joinpath(*parts)))
    settings = make_settings(automatic_gearbox=True, language='en')
    gearbox = Gearbox(bus, settings,
                      car_profiles=FakeProfiles(idle=950, redline=1200,
                                                forward_gears=5))
    gearbox.clock = FakeClock()
    bus.emit('state_data', track_state())
    standing = make_own_vehicle(speed=0, gear=2, rpm=950, cname=b'ZZZ',
                                local_plid=1, plid=1)

    gearbox.process(standing, {})

    assert gearbox.is_calibrated is False


def test_the_drivers_own_calibration_outranks_the_measured_values(
        bus, make_settings, make_own_vehicle, tmp_path, monkeypatch):
    """An explicit statement is not overwritten by a later measurement."""
    monkeypatch.setattr('assistance.gearbox.resolve_path',
                        lambda *parts: str(tmp_path.joinpath(*parts)))
    data_dir = tmp_path / 'data'
    data_dir.mkdir(parents=True, exist_ok=True)
    (data_dir / 'gearbox_calibrations.json').write_text(json.dumps(
        {'XFG': {'idle': 900, 'redline': 7000, 'forward_gears': 5}}))
    settings = make_settings(automatic_gearbox=True, language='en')
    gearbox = Gearbox(bus, settings,
                      car_profiles=FakeProfiles(idle=1100, redline=8500,
                                                forward_gears=6))
    gearbox.clock = FakeClock()
    bus.emit('state_data', track_state())
    driving = make_own_vehicle(speed=50, gear=3, rpm=3000, local_plid=1, plid=1)

    gearbox.process(driving, {})

    assert (gearbox.idle, gearbox.redline, gearbox.forward_gears) == (900, 7000, 5)


def test_a_better_measurement_reaches_the_gearbox_without_a_car_change(
        bus, make_settings, make_own_vehicle, tmp_path, monkeypatch):
    """The learned redline rises as the car is driven; freezing it on car
    change would leave the first, worst estimate in place for the session."""
    monkeypatch.setattr('assistance.gearbox.resolve_path',
                        lambda *parts: str(tmp_path.joinpath(*parts)))
    settings = make_settings(automatic_gearbox=True, language='en')
    profiles = FakeProfiles(idle=950, redline=6000, forward_gears=5)
    gearbox = Gearbox(bus, settings, car_profiles=profiles)
    gearbox.clock = FakeClock()
    bus.emit('state_data', track_state())
    driving = make_own_vehicle(speed=50, gear=3, rpm=3000, cname=b'ZZZ',
                               local_plid=1, plid=1)
    gearbox.process(driving, {})
    assert gearbox.redline == 6000

    profiles._redline = 7979
    gearbox.process(driving, {})

    assert gearbox.redline == 7979


def test_a_stock_car_shifts_without_anyone_calibrating_it(
        bus, make_settings, make_own_vehicle, tmp_path, monkeypatch):
    """The built-in table covers the LFS standard cars (`STOCK_PROFILES`)."""
    monkeypatch.setattr('assistance.gearbox.resolve_path',
                        lambda *parts: str(tmp_path.joinpath(*parts)))
    settings = make_settings(automatic_gearbox=True, language='en')
    gearbox = Gearbox(bus, settings)          # no learned profiles at all
    gearbox.clock = FakeClock()
    bus.emit('state_data', track_state())
    driving = make_own_vehicle(speed=50, gear=3, rpm=3000, cname='XFG',
                               local_plid=1, plid=1)

    gearbox.process(driving, {})

    assert gearbox.is_calibrated is True
    assert (gearbox.idle, gearbox.redline, gearbox.forward_gears) == (950, 7979, 5)


def test_a_single_seater_does_not_arm_itself(
        bus, make_settings, make_own_vehicle, tmp_path, monkeypatch):
    """On a formula car the driver wants the gears; calibrating is opt-in."""
    monkeypatch.setattr('assistance.gearbox.resolve_path',
                        lambda *parts: str(tmp_path.joinpath(*parts)))
    settings = make_settings(automatic_gearbox=True, language='en')
    gearbox = Gearbox(bus, settings,
                      car_profiles=FakeProfiles(idle=3000, redline=19000,
                                                forward_gears=7))
    gearbox.clock = FakeClock()
    bus.emit('state_data', track_state())
    driving = make_own_vehicle(speed=200, gear=5, rpm=17000, cname='BF1',
                               local_plid=1, plid=1)

    gearbox.process(driving, {})

    assert gearbox.is_calibrated is False
    assert keys_pressed() == []


def test_a_single_seater_still_works_if_the_driver_calibrates_it(
        bus, make_settings, make_own_vehicle, tmp_path, monkeypatch):
    """Opt-in means opt-in: an explicit calibration is honoured."""
    monkeypatch.setattr('assistance.gearbox.resolve_path',
                        lambda *parts: str(tmp_path.joinpath(*parts)))
    data_dir = tmp_path / 'data'
    data_dir.mkdir(parents=True, exist_ok=True)
    (data_dir / 'gearbox_calibrations.json').write_text(json.dumps(
        {'BF1': {'idle': 4000, 'redline': 19000, 'forward_gears': 7}}))
    settings = make_settings(automatic_gearbox=True, language='en')
    gearbox = Gearbox(bus, settings)
    gearbox.clock = FakeClock()
    bus.emit('state_data', track_state())
    driving = make_own_vehicle(speed=200, gear=5, rpm=17000, cname='BF1',
                               local_plid=1, plid=1)

    gearbox.process(driving, {})

    assert gearbox.is_calibrated is True


def test_the_motorbike_gearbox_is_refused_even_with_a_calibration(
        bus, make_settings, make_own_vehicle, tmp_path, monkeypatch, recorder):
    """The MRT5 has a motorbike gearbox - this shift logic does not describe it.

    Ignoring a calibration silently would leave the driver wondering; the
    calibration request is refused out loud instead.
    """
    seen = recorder('notification')
    monkeypatch.setattr('assistance.gearbox.resolve_path',
                        lambda *parts: str(tmp_path.joinpath(*parts)))
    data_dir = tmp_path / 'data'
    data_dir.mkdir(parents=True, exist_ok=True)
    (data_dir / 'gearbox_calibrations.json').write_text(json.dumps(
        {'MRT': {'idle': 2000, 'redline': 11000, 'forward_gears': 6}}))
    settings = make_settings(automatic_gearbox=True, language='en')
    gearbox = Gearbox(bus, settings)
    gearbox.clock = FakeClock()
    gearbox.time_since_last_gear_change = gearbox.clock() - 10.0
    gearbox.guard.foreground_check = lambda: True
    bus.emit('state_data', track_state())
    revving = make_own_vehicle(speed=60, gear=3, rpm=10500, throttle=1.0,
                               cname='MRT', local_plid=1, plid=1)

    result = gearbox.process(revving, {})

    assert result['auto_gearbox_active'] is False
    assert keys_pressed() == []

    bus.emit('gearbox_calibrate', {})
    gearbox.process(revving, {})

    assert gearbox.calibrating is False
    assert any('not available' in text for text in notifications(seen))


def test_calibration_can_be_cancelled_from_the_menu(
        bus, gearbox_factory, make_own_vehicle, recorder):
    seen = recorder('notification')
    clock = FakeClock()
    gearbox = gearbox_factory(clock=clock, calibrated=False)
    standing = make_own_vehicle(speed=0.0, rpm=900, cname=b'ZZZ',
                                local_plid=1, plid=1)
    gearbox.car = 'ZZZ'

    bus.emit('gearbox_calibrate', {})
    gearbox.process(standing, {})
    assert gearbox.calibrating is True

    bus.emit('gearbox_calibrate', {})     # same menu entry again = cancel
    gearbox.process(standing, {})

    assert gearbox.calibrating is False
    assert any('Aborted' in text for text in notifications(seen))


def test_calibration_stores_the_number_of_forward_gears(
        bus, gearbox_factory, make_own_vehicle, recorder, tmp_path):
    seen = recorder('notification')
    clock = FakeClock()
    gearbox = gearbox_factory(clock=clock, calibrated=False)
    standing = make_own_vehicle(speed=0.0, rpm=900, cname=b'ZZZ',
                                local_plid=1, plid=1)
    gearbox.car = 'ZZZ'

    run_calibration(gearbox, clock, bus, standing, gear_at_the_end=8)

    # gear 8 is the 7th forward gear (0 = reverse, 1 = neutral, 2 = 1st).
    assert gearbox.forward_gears == 7
    assert any('Max gear set to 7' in text for text in notifications(seen))
    stored = json.loads((tmp_path / 'data' / 'gearbox_calibrations.json').read_text())
    assert stored['ZZZ']['forward_gears'] == 7
    assert 'max_gears' not in stored['ZZZ']


def test_calibration_refuses_neutral_as_the_highest_gear(
        bus, gearbox_factory, make_own_vehicle, recorder):
    seen = recorder('notification')
    clock = FakeClock()
    gearbox = gearbox_factory(clock=clock, calibrated=False)
    standing = make_own_vehicle(speed=0.0, rpm=900, cname=b'ZZZ',
                                local_plid=1, plid=1)
    gearbox.car = 'ZZZ'

    run_calibration(gearbox, clock, bus, standing, gear_at_the_end=1)

    assert gearbox.forward_gears == 0
    assert gearbox.calibrating is False
    assert any('Aborted' in text for text in notifications(seen))


def test_calibration_keeps_the_highest_gear_even_when_it_drops_back_to_neutral(
        bus, gearbox_factory, make_own_vehicle, recorder):
    """The field failure: LFS puts a standing car back into neutral.

    The step used to be read in the single cycle in which it expired, and the
    log then said "step 2 ended - gear 1" although the driver had shifted all
    the way up. What the step is asking for is the highest gear *reached*.
    """
    seen = recorder('notification')
    clock = FakeClock()
    gearbox = gearbox_factory(clock=clock, calibrated=False)
    standing = make_own_vehicle(speed=0.0, rpm=900, cname=b'ZZZ',
                                local_plid=1, plid=1)
    gearbox.car = 'ZZZ'

    run_calibration(gearbox, clock, bus, standing,
                    gear_at_the_end=8, gear_at_the_very_end=1)

    assert gearbox.forward_gears == 7
    assert gearbox.calibrating is False
    assert any('Max gear set to 7' in text for text in notifications(seen))


def test_calibration_keeps_the_highest_rpm_of_the_step_not_the_last_one(
        bus, gearbox_factory, make_own_vehicle):
    """Letting off a moment early used to store idle rpm as the redline."""
    clock = FakeClock()
    gearbox = gearbox_factory(clock=clock, calibrated=False)
    standing = make_own_vehicle(speed=0.0, rpm=900, cname=b'ZZZ',
                                local_plid=1, plid=1)
    gearbox.car = 'ZZZ'

    bus.emit('gearbox_calibrate', {})
    gearbox.process(standing, {})
    clock.advance(12.1)
    gearbox.process(standing, {})               # idle stored, step 1 starts

    standing.rpm = 7200                         # revved ...
    gearbox.process(standing, {})
    standing.rpm = 1100                         # ... and off the throttle again
    clock.advance(12.1)
    gearbox.process(standing, {})

    assert gearbox.redline == 7200


def pump_idle_step(gearbox, clock, own_vehicle, rpm_sequence):
    """Run the idle step through *rpm_sequence*, one assistance cycle each."""
    for rpm in rpm_sequence:
        own_vehicle.rpm = rpm
        clock.advance(0.1)
        gearbox.process(own_vehicle, {})
    clock.advance(12.1)
    gearbox.process(own_vehicle, {})


def test_calibration_takes_the_rpm_that_was_there_most_of_the_idle_step(
        bus, gearbox_factory, make_own_vehicle):
    """A blip during the idle step must not become the idle speed.

    The median, not the minimum and not the last value: the idle governor
    hunts around its target, and a step regularly contains rpm that is not
    idle at all.
    """
    clock = FakeClock()
    gearbox = gearbox_factory(clock=clock, calibrated=False)
    standing = make_own_vehicle(speed=0.0, rpm=900, cname=b'ZZZ',
                                local_plid=1, plid=1)
    gearbox.car = 'ZZZ'

    bus.emit('gearbox_calibrate', {})
    gearbox.process(standing, {})
    # 100 cycles of idle hunting around 900, with a 20-cycle blip in the middle.
    pump_idle_step(gearbox, clock, standing,
                   [890, 900, 910] * 20 + [3000] * 20 + [895, 905] * 10)

    assert gearbox.idle == pytest.approx(900, abs=15)


def test_a_stopped_engine_is_not_mistaken_for_idle_speed(
        bus, gearbox_factory, make_own_vehicle):
    """The field failure: LFS shuts a standing engine down, and rpm is then 0.

    Starting the calibration before starting the engine used to store an idle
    speed of 0, which makes every shift threshold nonsense.
    """
    clock = FakeClock()
    gearbox = gearbox_factory(clock=clock, calibrated=False)
    standing = make_own_vehicle(speed=0.0, rpm=0, cname=b'ZZZ',
                                local_plid=1, plid=1)
    gearbox.car = 'ZZZ'

    bus.emit('gearbox_calibrate', {})
    gearbox.process(standing, {})
    # Engine off for the first third of the step, then started and idling.
    pump_idle_step(gearbox, clock, standing, [0] * 40 + [900, 905, 895] * 27)

    assert gearbox.idle == pytest.approx(900, abs=15)


def test_calibration_aborts_when_the_engine_never_runs(
        bus, gearbox_factory, make_own_vehicle, recorder):
    """Better to say so than to store an idle speed of zero."""
    seen = recorder('notification')
    clock = FakeClock()
    gearbox = gearbox_factory(clock=clock, calibrated=False)
    standing = make_own_vehicle(speed=0.0, rpm=0, cname=b'ZZZ',
                                local_plid=1, plid=1)
    gearbox.car = 'ZZZ'

    bus.emit('gearbox_calibrate', {})
    gearbox.process(standing, {})
    pump_idle_step(gearbox, clock, standing, [0] * 50)

    assert gearbox.calibrating is False
    assert gearbox.is_calibrated is False
    assert any('Aborted' in text for text in notifications(seen))


def test_calibration_refuses_a_redline_that_was_never_revved(
        bus, gearbox_factory, make_own_vehicle, recorder):
    """Storing redline == idle leaves an automatic that never shifts.

    It used to be stored silently, and the driver had no way to tell why the
    gearbox did nothing afterwards.
    """
    seen = recorder('notification')
    clock = FakeClock()
    gearbox = gearbox_factory(clock=clock, calibrated=False)
    standing = make_own_vehicle(speed=0.0, rpm=900, cname=b'ZZZ',
                                local_plid=1, plid=1)
    gearbox.car = 'ZZZ'

    run_calibration(gearbox, clock, bus, standing, redline_rpm=1200)

    assert gearbox.calibrating is False
    assert gearbox.is_calibrated is False
    assert any('Aborted' in text for text in notifications(seen))


def test_calibration_aborts_when_the_camera_leaves_the_own_car(
        bus, gearbox_factory, make_own_vehicle):
    clock = FakeClock()
    gearbox = gearbox_factory(clock=clock, calibrated=False)
    standing = make_own_vehicle(speed=0.0, rpm=900, cname=b'ZZZ',
                                local_plid=1, plid=1)
    gearbox.car = 'ZZZ'
    bus.emit('gearbox_calibrate', {})
    gearbox.process(standing, {})

    spectating = make_own_vehicle(speed=0.0, rpm=900, local_plid=1, viewed_plid=7)
    gearbox.process(spectating, {})

    assert gearbox.calibrating is False


def test_a_calibration_file_from_an_older_build_still_applies(
        bus, gearbox_factory, tmp_path):
    """Older files store the raw top gear index under ``max_gears``."""
    gearbox = gearbox_factory(calibrated=False)
    data_dir = tmp_path / 'data'
    data_dir.mkdir(parents=True, exist_ok=True)
    (data_dir / 'gearbox_calibrations.json').write_text(json.dumps(
        {'XFG': {'idle': 900, 'redline': 7000, 'max_gears': 7}}))

    gearbox.load_calibrations_for_cars('XFG')

    assert (gearbox.idle, gearbox.redline, gearbox.forward_gears) == (900, 7000, 6)
    assert gearbox.is_calibrated is True


def test_a_car_without_a_calibration_does_not_inherit_the_previous_one(
        bus, gearbox_factory, tmp_path):
    gearbox = gearbox_factory()          # calibrated for XFG
    data_dir = tmp_path / 'data'
    data_dir.mkdir(parents=True, exist_ok=True)
    (data_dir / 'gearbox_calibrations.json').write_text(json.dumps(
        {'XFG': {'idle': 900, 'redline': 7000, 'forward_gears': 6}}))

    gearbox.load_calibrations_for_cars('FBM')

    assert gearbox.is_calibrated is False


# ─── Light commands ──────────────────────────────────────────────────────────

@pytest.fixture
def lights(bus, make_settings):
    system = LightAssists(bus, make_settings(adaptive_lights=True, language='en'))
    with_clock(system)
    return system


def test_high_beam_assist_emits_one_command_per_state_change(
        bus, lights, make_own_vehicle, make_vehicle, relate_to_own, recorder):
    seen = recorder('send_light_command')
    with_low_beam = make_own_vehicle(speed=80, low_beam=True, local_plid=1, plid=1)

    for _ in range(5):
        lights.process(with_low_beam, {})
    assert light_payloads(seen, LIGHT_HIGH_BEAM, LIGHT_LOW_BEAM) == [
        {'light': LIGHT_HIGH_BEAM, 'on': True}]

    # LFS answers: the car now carries full beam. A car appears ahead.
    with_full_beam = make_own_vehicle(speed=80, full_beam=True, local_plid=1, plid=1)
    ahead = make_vehicle(plid=2, y=60, speed=70)
    relate_to_own(with_full_beam, ahead)
    for _ in range(5):
        lights.process(with_full_beam, {2: ahead})

    assert light_payloads(seen, LIGHT_HIGH_BEAM, LIGHT_LOW_BEAM) == [
        {'light': LIGHT_HIGH_BEAM, 'on': True},
        {'light': LIGHT_LOW_BEAM, 'on': True},
    ]


def test_high_beam_assist_leaves_a_driver_without_lights_alone(
        bus, lights, make_own_vehicle, recorder):
    """Driving without lights was impossible: every cycle switched them on."""
    seen = recorder('send_light_command')
    dark = make_own_vehicle(speed=80, local_plid=1, plid=1)

    for _ in range(10):
        lights.process(dark, {})

    assert light_payloads(seen, LIGHT_HIGH_BEAM, LIGHT_LOW_BEAM) == []


def test_high_beam_assist_does_not_fight_a_manual_change(
        bus, lights, make_own_vehicle, recorder):
    seen = recorder('send_light_command')
    with_low_beam = make_own_vehicle(speed=80, low_beam=True, local_plid=1, plid=1)
    lights.process(with_low_beam, {})            # -> high beam requested
    assert len(light_payloads(seen, LIGHT_HIGH_BEAM, LIGHT_LOW_BEAM)) == 1

    # The driver dips again by hand while nothing is ahead.
    for _ in range(10):
        lights.process(with_low_beam, {})

    assert len(light_payloads(seen, LIGHT_HIGH_BEAM, LIGHT_LOW_BEAM)) == 1


def test_high_beam_assist_can_be_switched_off(
        bus, make_settings, make_own_vehicle, recorder):
    seen = recorder('send_light_command')
    system = LightAssists(bus, make_settings(adaptive_lights=True,
                                             high_beam_assist=False))
    with_clock(system)

    for _ in range(5):
        system.process(make_own_vehicle(speed=80, low_beam=True,
                                        local_plid=1, plid=1), {})

    assert light_payloads(seen, LIGHT_HIGH_BEAM, LIGHT_LOW_BEAM) == []


def test_light_commands_stop_while_the_camera_is_on_another_car(
        bus, lights, make_own_vehicle, recorder):
    seen = recorder('send_light_command')
    spectating = make_own_vehicle(speed=80, low_beam=True, local_plid=1, viewed_plid=7)

    for _ in range(5):
        lights.process(spectating, {})

    assert seen.count('send_light_command') == 0


def test_adaptive_brake_light_flashes_across_the_heading_wrap(
        bus, lights, make_own_vehicle, recorder):
    """Heading 0.5° and direction 359.5° is straight ahead, not reversing."""
    seen = recorder('send_light_command')
    clock = lights.clock
    braking = make_own_vehicle(speed=100, acceleration=-9.0, brake=1.0,
                               heading=0.5, direction=359.5,
                               local_plid=1, plid=1)

    clock.advance(0.2)
    lights.process(braking, {})

    assert light_payloads(seen, LIGHT_HAZARDS) == [{'light': LIGHT_HAZARDS, 'on': True}]


def test_adaptive_brake_light_stays_off_while_reversing(
        bus, lights, make_own_vehicle, recorder):
    seen = recorder('send_light_command')
    reversing = make_own_vehicle(speed=20, acceleration=-9.0, brake=1.0,
                                 heading=0.0, direction=180.0,
                                 local_plid=1, plid=1)

    lights.clock.advance(0.2)
    lights.process(reversing, {})

    assert light_payloads(seen, LIGHT_HAZARDS) == []


def test_disable_siren_does_not_switch_the_low_beam_on(bus, lights, recorder):
    seen = recorder('send_light_command')

    lights.disable_siren()

    assert all(payload['light'] != LIGHT_LOW_BEAM
               for payload in seen.payloads('send_light_command'))
    assert all(payload['on'] is False for payload in seen.payloads('send_light_command'))


# ─── Strobe timing ───────────────────────────────────────────────────────────

def count_strobe_steps(lights, own_vehicle, cycle_s, duration_s=2.0):
    """Pattern steps produced by ``duration_s`` of driving at this cycle time."""
    steps = 0
    cycles = int(round(duration_s / cycle_s))
    for _ in range(cycles):
        lights.clock.advance(cycle_s)
        before = lights.strobe_pattern
        lights.process(own_vehicle, {})
        if lights.strobe_pattern != before:
            steps += 1
    return steps


@pytest.fixture
def cop_lights_factory(bus, make_settings):
    def _make():
        system = LightAssists(bus, make_settings(adaptive_lights=True,
                                                 cop_assistance=True, language='en'))
        with_clock(system)
        system.player_name = "[COP] Tester"
        system.is_siren_enabled_role = True
        system._set_strobe(True)
        return system
    return _make


@pytest.fixture
def cop_lights(cop_lights_factory):
    return cop_lights_factory()


def test_strobe_speed_is_independent_of_the_refresh_rate(
        bus, cop_lights_factory, make_own_vehicle):
    """It used to be exactly one pattern step per ``process()`` call.

    At 50 ms the pattern therefore ran twice as fast as at 100 ms. Two seconds
    of driving must now produce the same ~20 steps either way (STROBE_STEP_S
    is 0.1 s). The cycle time is still the ceiling: at 200 ms the pattern
    cannot step more often than it is asked to.
    """
    own = make_own_vehicle(speed=50, local_plid=1, plid=1)

    fast = count_strobe_steps(cop_lights_factory(), own, 0.05)
    slow = count_strobe_steps(cop_lights_factory(), own, 0.1)

    assert 19 <= fast <= 21
    assert 19 <= slow <= 21
    assert abs(fast - slow) <= 1


def test_strobe_emits_the_pattern_as_light_commands(
        bus, cop_lights, make_own_vehicle, recorder):
    seen = recorder('send_light_command')
    own = make_own_vehicle(speed=50, local_plid=1, plid=1)

    cop_lights.clock.advance(0.11)
    cop_lights.process(own, {})

    assert seen.payloads('send_light_command')[-1] == cop_lights.strobe_actions[
        cop_lights.strobe_pattern]


def test_the_strobe_suspends_the_high_beam_assist(
        bus, cop_lights, make_own_vehicle, recorder):
    seen = recorder('send_light_command')
    own = make_own_vehicle(speed=50, low_beam=True, local_plid=1, plid=1)

    cop_lights.clock.advance(0.11)
    cop_lights.process(own, {})

    assert all(payload in cop_lights.strobe_actions.values()
               for payload in seen.payloads('send_light_command'))


# ─── One owner for siren and strobe (known-issues #17) ───────────────────────

@pytest.fixture
def cop_ui(bus, make_settings, message_sender, fake_connector):
    """LightAssists + UIManager on one bus, with the siren UI shown."""
    settings = make_settings(adaptive_lights=True, cop_assistance=True, language='en')
    ui = UIManager(bus, message_sender, settings)
    system = LightAssists(bus, settings)
    with_clock(system)
    bus.emit('player_name_changed', {'player_name': '[COP] Tester', 'control_mode': 0})
    return system, ui, fake_connector


def siren_caption(connector):
    entry = connector.last_button(UI_BTN_SIREN)
    assert entry is not None, "the siren button was never drawn"
    text = entry[6]
    return text.decode('latin-1') if isinstance(text, (bytes, bytearray)) else text


def test_chat_command_updates_the_siren_button_caption(bus, cop_ui, settings):
    """The chat path used to toggle only the LightAssists copy of the state."""
    system, ui, connector = cop_ui
    assert '^7Siren' in siren_caption(connector)

    handler = ChatCommandHandler(bus, system.settings)
    bus.emit('player_name_changed', {'player_name': '[COP] Tester', 'control_mode': 0})
    bus.emit('message_received', FakePacket(
        UserType=pyinsim.MSO_PREFIX, Msg=b"[COP] Tester: $siren",
        TextStart=len(b"[COP] Tester: "), UCID=0, PLID=1))

    assert system.siren_active is True
    assert ui.siren_active is True
    assert '^4Siren' in siren_caption(connector)


def test_button_click_and_chat_command_share_one_state(bus, cop_ui):
    system, ui, connector = cop_ui

    bus.emit('button_clicked', FakePacket(ClickID=BTN_SIREN, UCID=0, ClickMax=0))
    assert (system.siren_active, ui.siren_active) == (True, True)

    bus.emit('siren_toggle_requested', {})
    assert (system.siren_active, ui.siren_active) == (False, False)
    assert '^7Siren' in siren_caption(connector)


def test_strobe_state_is_published_to_the_ui(bus, cop_ui):
    system, ui, connector = cop_ui

    bus.emit('button_clicked', FakePacket(ClickID=BTN_STROBE, UCID=0, ClickMax=0))

    assert system.strobe_active is True
    assert ui.strobe_active is True
    entry = connector.last_button(BTN_STROBE)
    text = entry[6]
    text = text.decode('latin-1') if isinstance(text, (bytes, bytearray)) else text
    assert '^4Strobe' in text


def test_leaving_the_track_switches_the_siren_off_everywhere(bus, cop_ui):
    system, ui, connector = cop_ui
    bus.emit('state_data', track_state())
    bus.emit('button_clicked', FakePacket(ClickID=BTN_SIREN, UCID=0, ClickMax=0))
    assert system.siren_active is True

    bus.emit('state_data', track_state(on_track=False, screen='entry'))

    assert system.siren_active is False
    assert ui.siren_active is False


def test_siren_state_is_republished_when_the_ui_reappears(bus, cop_ui, recorder):
    system, ui, connector = cop_ui
    seen = recorder('siren_state_changed', 'strobe_state_changed')

    bus.emit('player_name_changed', {'player_name': '[COP] Tester', 'control_mode': 0})

    assert seen.count('siren_state_changed') == 1
    assert seen.count('strobe_state_changed') == 1


# ─── Key binding ─────────────────────────────────────────────────────────────

def test_a_second_key_capture_request_does_not_hijack_the_first(bus, settings):
    """Both requests used to share one target, so the key landed on the wrong
    setting."""
    from misc.key_binder import Keybinder

    binder = Keybinder(bus, settings)
    bus.emit('await_keybinding', {'setting': 'user_handbrake_key'})
    bus.emit('await_keybinding', {'setting': 'user_clutch_key'})

    assert binder._current_setting == 'user_handbrake_key'


def test_key_capture_reports_the_setting_it_was_asked_for(bus, settings, recorder):
    from misc.key_binder import Keybinder

    seen = recorder('new_keybinding')
    binder = Keybinder(bus, settings)
    bus.emit('await_keybinding', {'setting': 'user_clutch_key'})
    binder._emit_keybinding('v')

    assert seen.last('new_keybinding') == {'button': 'v', 'setting': 'user_clutch_key'}
