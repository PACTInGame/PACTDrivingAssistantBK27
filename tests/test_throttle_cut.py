"""Cutting the throttle for the duration of an intervention.

Three things are checked, in this order of importance:

1. the throttle always comes **back** -- from a release, a shutdown, and from a
   process that never gets to send anything at all (the handover marker);
2. the destructive half (``/axis <n> throttle``) stays refused until the number
   behind it has been proven;
3. the commands themselves.
"""

import json
import os

import pytest

import guardian
from Controls.handover_marker import HandoverMarker
from Controls.throttle_axis_check import ThrottleAxisCheck
from Controls.throttle_cut import AxisThrottleCut, KeyThrottleCut


@pytest.fixture
def marker(tmp_path):
    return HandoverMarker(str(tmp_path / 'brake_axis_held.marker'))


@pytest.fixture
def commands(recorder):
    return recorder('send_command_to_lfs')


def sent(commands):
    return commands.payloads('send_command_to_lfs')


# ─── The shared handover marker ──────────────────────────────────────────────

def test_a_marker_with_nothing_claimed_does_not_exist(marker):
    """Presence is the whole signal: a file left behind by a clean exit would
    make the guardian force a configuration on every shutdown."""
    assert not os.path.exists(marker.path)


def test_two_owners_end_up_in_one_file(marker):
    marker.claim('brake', '/axis 12 brake')
    marker.claim('throttle', '/axis 9 throttle')

    with open(marker.path, encoding='utf-8') as handle:
        assert json.load(handle) == {
            'commands': ['/axis 12 brake', '/axis 9 throttle']}


def test_releasing_one_owner_leaves_the_other_behind(marker):
    """The brake and the throttle are given back at slightly different moments;
    whichever is still held has to stay in the file."""
    marker.claim('brake', '/axis 12 brake')
    marker.claim('throttle', '/axis 9 throttle')

    marker.release('throttle')

    with open(marker.path, encoding='utf-8') as handle:
        assert json.load(handle) == {'commands': ['/axis 12 brake']}


def test_the_file_disappears_once_the_last_owner_lets_go(marker):
    marker.claim('brake', '/axis 12 brake')
    marker.claim('throttle', '/axis 9 throttle')

    marker.release('brake')
    marker.release('throttle')

    assert not os.path.exists(marker.path)


def test_releasing_something_never_claimed_is_not_an_error(marker):
    marker.release('throttle')          # must not raise

    assert not os.path.exists(marker.path)


def test_what_the_marker_writes_is_what_the_guardian_replays(marker):
    """The two halves have to agree on the format, so they are tested against
    each other rather than against a copy of it."""
    marker.claim('brake', '/axis 12 brake')
    marker.claim('throttle', '/axis 9 throttle')

    assert guardian.read_marker(marker.path) == ['/axis 12 brake',
                                                 '/axis 9 throttle']


def test_an_unwritable_marker_does_not_stop_anything(tmp_path):
    """Without the marker the guardian does nothing, which is where we were
    before it existed -- not a reason to refuse to intervene."""
    marker = HandoverMarker(str(tmp_path / 'nope' / 'marker'))

    marker.claim('brake', '/axis 12 brake')      # must not raise

    assert marker.holds_anything() is True


# ─── mouse_kb: the key path ──────────────────────────────────────────────────

@pytest.fixture
def key_cut(bus, make_settings, marker):
    settings = make_settings(user_throttle_key='up')
    return KeyThrottleCut(bus, settings, marker)


def test_the_key_path_is_refused_until_the_binding_was_pushed(key_cut):
    """A restore can only be trusted if we were the ones who wrote it."""
    assert key_cut.unavailable_reason() == 'throttle_binding_not_pushed'

    key_cut.push_binding()

    assert key_cut.unavailable_reason() is None


def test_a_key_lfs_cannot_bind_is_refused(bus, make_settings, marker):
    cut = KeyThrottleCut(bus, make_settings(user_throttle_key='f13'), marker)

    assert cut.push_binding() is False
    assert cut.unavailable_reason() == 'throttle_key_not_bindable_in_lfs'


def test_cutting_and_restoring_the_key(key_cut, commands, marker):
    key_cut.push_binding()

    assert key_cut.engage() is True
    assert key_cut.holds_throttle() is True
    assert sent(commands)[-1] == '/key -1 throttle'
    assert guardian.read_marker(marker.path) == ['/key up throttle']

    key_cut.release()

    assert key_cut.holds_throttle() is False
    assert sent(commands)[-1] == '/key up throttle'
    assert guardian.read_marker(marker.path) is None


def test_engaging_twice_sends_one_command(key_cut, commands):
    key_cut.push_binding()
    key_cut.engage()
    before = len(sent(commands))

    key_cut.engage()

    assert len(sent(commands)) == before


def test_releasing_without_engaging_sends_nothing(key_cut, commands):
    key_cut.push_binding()
    before = len(sent(commands))

    key_cut.release()

    assert len(sent(commands)) == before


def test_pushing_the_binding_while_the_throttle_is_cut_is_refused(
        key_cut, commands):
    """That command *is* the restore; sending it now would hand the throttle
    back in the middle of an emergency stop."""
    key_cut.push_binding()
    key_cut.engage()

    assert key_cut.push_binding() is False
    assert sent(commands)[-1] == '/key -1 throttle'


# ─── wheel_js: the axis path ─────────────────────────────────────────────────

from misc.lfs_config import AxisAssignment


class FakePedals:
    """Stands in for ``PedalWatch``: the hardware reading, and how sure it is."""

    def __init__(self, throttle=None, confidence=1.0):
        self.throttle = throttle
        self._confidence = confidence

    def driver_throttle(self):
        return self.throttle

    def confidence(self, _which):
        return self._confidence


def axis_cut(bus, make_settings, marker, assignments=None, pedals=None,
             **overrides):
    overrides.setdefault('throttle_axis_verified', True)
    settings = make_settings(user_axis_brake=12, **overrides)
    if assignments is None:
        assignments = {'brake': AxisAssignment(12, 1),
                       'throttle': AxisAssignment(9, 1)}
    return AxisThrottleCut(bus, settings, marker,
                           pedals=pedals or FakePedals(),
                           lfs_axes=lambda: assignments), settings


def test_the_axis_and_its_polarity_come_from_lfs_own_file(bus, make_settings,
                                                          marker):
    """Nobody types the number in, and the settings file is corrected to match
    what LFS actually has."""
    cut, settings = axis_cut(bus, make_settings, marker, user_axis_throttle=3)

    assert cut.axis == 9
    assert settings.get('user_axis_throttle') == 9


def test_an_unreadable_controller_file_leaves_the_cut_refused(bus, make_settings,
                                                              marker):
    """No file, an unknown device, a format that changed: all of them mean the
    restore command cannot be written, so the throttle is simply not cut."""
    cut, _settings = axis_cut(bus, make_settings, marker, assignments={})

    assert cut.unavailable_reason() == 'throttle_axis_unknown'
    assert cut.engage() is False


def test_a_pedal_nobody_has_confirmed_leaves_the_cut_refused(bus, make_settings,
                                                             marker):
    """Taking the throttle away is only meaningful if LFS is demonstrably
    reading the pedal we are about to give it back to."""
    cut, _settings = axis_cut(bus, make_settings, marker,
                              pedals=FakePedals(confidence=0.4))

    assert cut.unavailable_reason() == 'throttle_pedal_not_confirmed'


def test_an_unverified_axis_is_refused(bus, make_settings, marker):
    """Everything can look right on paper and still not work in the game."""
    cut, _settings = axis_cut(bus, make_settings, marker,
                              throttle_axis_verified=False)

    assert cut.unavailable_reason() == 'throttle_axis_not_verified'
    assert cut.engage() is False


def test_a_restore_that_failed_once_is_never_tried_again(bus, make_settings,
                                                         marker):
    """A second attempt would take a second axis away for nothing."""
    cut, _settings = axis_cut(bus, make_settings, marker,
                              throttle_axis_broken=True)

    assert cut.unavailable_reason() == 'throttle_restore_failed'


def test_a_throttle_axis_equal_to_the_brake_axis_is_refused(bus, make_settings,
                                                            marker):
    """One axis carries one function. If these two agree, one of them is wrong,
    and restoring would point the throttle at the brake pedal."""
    cut, _settings = axis_cut(bus, make_settings, marker,
                              assignments={'throttle': AxisAssignment(12, 1)})

    assert cut.unavailable_reason() == 'throttle_and_brake_axis_identical'


def test_a_refused_cut_does_not_stop_the_brake(bus, make_settings, marker,
                                               commands):
    """Refusing the cut must cost the throttle only, never the intervention."""
    cut, _settings = axis_cut(bus, make_settings, marker,
                              throttle_axis_verified=False)
    cut.engage()

    assert sent(commands) == []
    assert not os.path.exists(marker.path)


def test_cutting_and_restoring_the_axis(bus, make_settings, marker, commands):
    cut, _settings = axis_cut(bus, make_settings, marker)

    assert cut.engage() is True
    assert sent(commands)[-1] == '/axis -1 throttle'
    assert guardian.read_marker(marker.path) == ['/axis 9 throttle',
                                                 '/invert 1 throttle']

    cut.release()

    assert sent(commands)[-2:] == ['/axis 9 throttle', '/invert 1 throttle']
    assert guardian.read_marker(marker.path) is None


def test_the_polarity_is_restored_with_the_assignment(bus, make_settings,
                                                      marker, commands):
    """Measured: ``/axis -1 throttle`` clears the invert flag too, so restoring
    the axis alone hands the driver a throttle that reads full at rest. That
    looked exactly like a wrong axis number for a whole session."""
    cut, _settings = axis_cut(
        bus, make_settings, marker,
        assignments={'throttle': AxisAssignment(9, 1)})
    cut.engage()

    cut.release()

    assert '/invert 1 throttle' in sent(commands)


def test_the_marker_is_claimed_before_the_throttle_is_taken(
        bus, make_settings, tmp_path):
    """There must be no window in which LFS has stopped reading the throttle
    and nothing on disk says how to give it back."""
    order = []

    class WatchingMarker(HandoverMarker):
        def claim(self, owner, commands):
            order.append('marker')
            super().claim(owner, commands)

    class WatchingBus:
        def emit(self, name, payload=None):
            order.append(payload)

        def subscribe(self, *_args):
            pass

    settings = make_settings(user_axis_brake=12, throttle_axis_verified=True)
    cut = AxisThrottleCut(WatchingBus(), settings, WatchingMarker(str(tmp_path / 'm')),
                          pedals=FakePedals(),
                          lfs_axes=lambda: {'throttle': AxisAssignment(9, 1)})
    cut.engage()

    assert order == ['marker', '/axis -1 throttle']


# ─── The axis check ──────────────────────────────────────────────────────────

def run_check(bus, make_settings, pedals, lfs_throttle_sequence,
              assignments=None):
    """Drive one check to completion with a scripted OutGauge reading.

    The first entry is the baseline; the rest arrive as each command goes out.
    """
    settings = make_settings(user_axis_brake=12)
    if assignments is None:
        assignments = {'throttle': AxisAssignment(9, 1)}
    marker = HandoverMarker(str(_tmp()))
    cut = AxisThrottleCut(bus, settings, marker, pedals=pedals,
                          lfs_axes=lambda: assignments)
    check = ThrottleAxisCheck(bus, settings, pedals, cut, sleep=lambda _s: None)
    readings = iter(lfs_throttle_sequence[1:])

    def next_reading(_command=None):
        try:
            check._lfs_throttle = next(readings)
        except StopIteration:
            pass

    check._lfs_throttle = lfs_throttle_sequence[0]
    bus.subscribe('send_command_to_lfs', next_reading)
    check._run()
    return settings


def _tmp():
    import tempfile
    import os as _os
    return _os.path.join(tempfile.mkdtemp(), 'marker')


def test_the_check_passes_when_lfs_follows_the_pedal_again(bus, make_settings,
                                                           commands):
    """Cut, then restore: LFS goes blind and then tracks the pedal again."""
    settings = run_check(bus, make_settings, FakePedals(0.9),
                         [0.9, 0.0, 0.9, 0.9])

    assert settings.get('throttle_axis_verified') is True
    assert settings.get('throttle_axis_broken') is False
    assert sent(commands)[:3] == ['/axis -1 throttle', '/axis 9 throttle',
                                  '/invert 1 throttle']


def test_a_restore_that_does_not_work_disables_the_cut_for_good(
        bus, make_settings, commands):
    """LFS never comes back, so the number or the polarity is wrong -- and a
    second attempt would take another axis away to find out the same thing."""
    settings = run_check(bus, make_settings, FakePedals(0.9),
                         [0.9, 0.0, 0.0, 0.0])

    assert settings.get('throttle_axis_verified') is False
    assert settings.get('throttle_axis_broken') is True
    # The restore was still attempted: leaving the throttle unassigned because
    # a measurement was inconclusive would be far worse.
    assert '/axis 9 throttle' in sent(commands)


def test_a_cut_that_lfs_ignores_fails_the_check(bus, make_settings):
    settings = run_check(bus, make_settings, FakePedals(0.9),
                         [0.9, 0.9, 0.9, 0.9])

    assert settings.get('throttle_axis_verified') is False


def test_lifting_off_mid_check_proves_nothing_and_breaks_nothing(
        bus, make_settings):
    """A coincidence of timing must not disable the feature for the session."""
    class Lifting(FakePedals):
        def __init__(self):
            super().__init__(0.9)
            self.reads = 0

        def driver_throttle(self):
            self.reads += 1
            return 0.9 if self.reads < 3 else 0.0

    settings = run_check(bus, make_settings, Lifting(), [0.9, 0.0, 0.9, 0.9])

    assert settings.get('throttle_axis_verified') is False
    assert settings.get('throttle_axis_broken') is False


# ─── When the check runs at all ──────────────────────────────────────────────

class FakeOwn:
    def __init__(self, speed):
        self.data = type('D', (), {'speed': speed})()


def waiting_check(bus, make_settings, pedals, **overrides):
    settings = make_settings(user_axis_brake=12, **overrides)
    marker = HandoverMarker(str(_tmp()))
    cut = AxisThrottleCut(bus, settings, marker, pedals=pedals,
                          lfs_axes=lambda: {'throttle': AxisAssignment(9, 1)})
    check = ThrottleAxisCheck(bus, settings, pedals, cut, sleep=lambda _s: None)
    check._lfs_throttle = 0.9
    return check


def test_the_check_waits_until_the_pedal_is_confirmed(bus, make_settings):
    check = waiting_check(bus, make_settings, FakePedals(0.9, confidence=0.5))

    assert check.maybe_run(FakeOwn(60.0)) is False


def test_the_check_waits_for_a_speed_where_losing_the_throttle_is_nothing(
        bus, make_settings):
    check = waiting_check(bus, make_settings, FakePedals(0.9))

    assert check.maybe_run(FakeOwn(5.0)) is False


def test_the_check_waits_until_the_driver_is_on_the_throttle(bus, make_settings):
    check = waiting_check(bus, make_settings, FakePedals(0.0))

    assert check.maybe_run(FakeOwn(60.0)) is False


def test_the_check_runs_by_itself_once_everything_lines_up(bus, make_settings):
    """No menu, no ritual: the driver just drives."""
    check = waiting_check(bus, make_settings, FakePedals(0.9))

    assert check.maybe_run(FakeOwn(60.0)) is True
    check._thread.join(timeout=2.0)


def test_the_check_never_runs_twice(bus, make_settings):
    check = waiting_check(bus, make_settings, FakePedals(0.9),
                          throttle_axis_verified=True)

    assert check.maybe_run(FakeOwn(60.0)) is False
