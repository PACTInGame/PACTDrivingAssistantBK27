"""The wheel/joystick brake path: the vJoy axis, its handover marker, and the
guardian process that gives the brake back if we die holding it.

``reference/control-intervention.md`` §3.2 is the specification these tests
pin, and the numbers in it were **measured**, not derived -- on the development
machine raw 0 is *full* brake and raw 32767 is *no* brake. Getting that sign
wrong writes full braking where idle was meant, so both polarities are asserted
here and nothing in the mapping may assume a direction.

Everything runs on any OS, in CI, without LFS, without vJoy and without
spawning anything:

* the vJoy DLL is never loaded -- ``AxisBrakeOutput`` takes a device, and the
  three tests that do exercise :mod:`misc.vjoy_device` patch the DLL lookup out
  or hand the object a recording stand-in for it;
* no process is created -- ``AxisBrakeOutput`` takes a ``spawn`` callable;
* no socket is opened -- ``guardian.hand_back`` is either replaced outright or
  driven against a fake ``socket.create_connection``, and the bytes it would
  have put on the wire are compared against ``pyinsim``'s own packets;
* the marker file lives in ``tmp_path``.

The two orderings asserted below are the safety-critical part and are asserted
as an *interleaving*, not as "both happened":

    engage    value first, then ``/axis <vjoy> brake``  -- the local write is
              instant, the command is a TCP round trip away, so the right
              value is already waiting when LFS switches;
    release   ``/axis <driver> brake`` first, then park -- if anything after
              the handback fails, the driver already has their pedal.
"""

import json
import os
import struct
import threading
import time

import pytest

import guardian
import pyinsim
from Controls.brake_axis import AxisBrakeOutput, REASON_LOADING
from misc.vjoy_device import VJoyDevice


# ─── Fakes ───────────────────────────────────────────────────────────────────

class FakeVJoy:
    """``VJoyDevice``'s surface, with no driver behind it.

    Axis writes go into the shared *log* the bus commands also land in, which
    is what makes the engage/release ordering assertable.
    """

    def __init__(self, log, unavailable=None, acquirable=True,
                 raw_min=0, raw_max=32767):
        self.log = log
        self._unavailable = unavailable
        self._acquirable = acquirable
        self.raw_min = raw_min
        self.raw_max = raw_max
        self.acquired = False
        self.relinquished = 0
        self.reason_queries = 0
        # ``prepare`` is the real device's off-thread DLL warm-up. A fake has
        # nothing to load, so it is ready from the start; ``prepares`` records
        # that the output asks before it reports.
        self.prepares = 0

    def prepare(self) -> bool:
        self.prepares += 1
        return True

    def loading(self) -> bool:
        return False

    def unavailable_reason(self):
        self.reason_queries += 1
        return self._unavailable

    def acquire(self) -> bool:
        if not self._acquirable:
            return False
        self.acquired = True
        return True

    def set_raw(self, value) -> bool:
        if not self.acquired:
            return False
        self.log.append(('raw', int(value)))
        return True

    def relinquish(self):
        self.acquired = False
        self.relinquished += 1

    # --- convenience for the assertions --------------------------------
    @property
    def writes(self):
        return [value for kind, value in self.log if kind == 'raw']


class ExplodingVJoy:
    """A device that must never be touched."""

    def unavailable_reason(self):
        raise AssertionError("the device must not be consulted at all")

    def acquire(self):
        raise AssertionError("the device must not be acquired")

    def set_raw(self, value):
        raise AssertionError("the device must not be written to")

    def relinquish(self):
        raise AssertionError("the device must not be relinquished")


class RecordingSpawn:
    """Stands in for ``_spawn_guardian``. Creates no process whatsoever."""

    def __init__(self, result):
        self._result = result
        self.pids = []
        self.called = threading.Event()

    def __call__(self, watched_pid):
        self.pids.append(watched_pid)
        self.called.set()
        return self._result


class FakeSocket:
    """Enough of a socket for :func:`guardian.hand_back`, minus the network."""

    def __init__(self, sink):
        self.sink = sink

    def __enter__(self):
        return self

    def __exit__(self, *exc_info):
        self.sink.append(('closed', None))
        return False

    def sendall(self, payload):
        self.sink.append(('sent', bytes(payload)))


class FakeDll:
    """The two ctypes entry points ``set_raw`` reaches."""

    def __init__(self, result=1):
        self._result = result
        self.axis_writes = []

    def SetAxis(self, value, device_id, axis):
        self.axis_writes.append((value, device_id, axis))
        return self._result


# ─── Fixtures ────────────────────────────────────────────────────────────────

@pytest.fixture
def axis_settings(make_settings):
    """The development machine's measured configuration: inverted axis,
    vJoy on LFS axis 15, the driver's pedal on 12."""
    return make_settings(vjoy_brake_calibrated=True,
                         vjoy_axis_1=15,
                         user_axis_brake=12,
                         vjoy_raw_no_brake=32767,
                         vjoy_raw_full_brake=0)


@pytest.fixture
def log(bus):
    """One list holding vJoy writes and InSim commands in the order they
    happened -- ordering is the whole point of both swap directions."""
    entries = []
    bus.subscribe('send_command_to_lfs', lambda cmd: entries.append(('command', cmd)))
    return entries


@pytest.fixture
def marker_path(tmp_path):
    return str(tmp_path / 'brake_axis_held.marker')


@pytest.fixture
def device(log):
    return FakeVJoy(log)


@pytest.fixture
def axis_output(bus, axis_settings, device, marker_path):
    """An armed axis output whose guardian is a no-op recorder."""
    return AxisBrakeOutput(bus, axis_settings, device=device,
                           marker_path=marker_path,
                           spawn=RecordingSpawn(object()))


# ─── Controls/brake_axis.py -- the measured mapping ──────────────────────────

@pytest.mark.parametrize('fraction, expected', [(0.0, 32767),
                                                (0.5, 16384),
                                                (1.0, 0)])
def test_the_inverted_calibration_of_the_dev_machine_maps_brake_onto_raw_zero(
        bus, make_settings, fraction, expected):
    """Raw 0 is full brake here and raw 32767 is idle -- measured against LFS,
    never assumed (``control-intervention.md`` §3.2)."""
    settings = make_settings(vjoy_raw_no_brake=32767, vjoy_raw_full_brake=0)
    output = AxisBrakeOutput(bus, settings, device=ExplodingVJoy(),
                             marker_path='unused', spawn=RecordingSpawn(None))

    assert output._raw_for(fraction) == expected


@pytest.mark.parametrize('fraction, expected', [(0.0, 0),
                                                (0.5, 16384),
                                                (1.0, 32767)])
def test_a_machine_calibrated_the_other_way_round_maps_the_other_way_round(
        bus, make_settings, fraction, expected):
    """The polarity is per-machine, so nothing may hard-code a direction."""
    settings = make_settings(vjoy_raw_no_brake=0, vjoy_raw_full_brake=32767)
    output = AxisBrakeOutput(bus, settings, device=ExplodingVJoy(),
                             marker_path='unused', spawn=RecordingSpawn(None))

    assert output._raw_for(fraction) == expected


def test_the_endpoints_are_taken_from_settings_and_not_from_the_device_range(
        bus, make_settings):
    """A partial calibration is still honoured literally: interpolation runs
    between the two stored numbers, whatever they are."""
    settings = make_settings(vjoy_raw_no_brake=20000, vjoy_raw_full_brake=4000)
    output = AxisBrakeOutput(bus, settings, device=ExplodingVJoy(),
                             marker_path='unused', spawn=RecordingSpawn(None))

    assert output._raw_for(0.0) == 20000
    assert output._raw_for(1.0) == 4000
    assert output._raw_for(0.25) == 16000


# ─── Controls/brake_axis.py -- engage and release ────────────────────────────

def test_engaging_writes_the_value_before_it_tells_lfs_to_read_us(
        axis_output, log):
    """Value first, swap second: the vJoy write is local and instant, the
    ``/axis`` command is a TCP round trip away."""
    assert axis_output.apply(0.5) is True

    assert log == [('raw', 16384), ('command', '/axis 15 brake')]


def test_the_swap_command_is_sent_once_however_long_the_intervention_lasts(
        axis_output, log):
    axis_output.apply(0.4)
    axis_output.apply(0.6)
    axis_output.apply(1.0)

    commands = [entry for entry in log if entry[0] == 'command']
    assert commands == [('command', '/axis 15 brake')]
    assert len([entry for entry in log if entry[0] == 'raw']) == 3


def test_every_cycle_writes_the_demanded_value_to_the_axis(axis_output, device):
    axis_output.apply(0.0)
    axis_output.apply(1.0)

    assert device.writes == [32767, 0]


def test_a_device_that_cannot_be_acquired_never_takes_the_brake_away(
        bus, axis_settings, log, marker_path):
    """If we cannot feed the axis, pointing LFS at it would nail the brake to
    whatever value the device happens to hold."""
    output = AxisBrakeOutput(bus, axis_settings,
                             device=FakeVJoy(log, acquirable=False),
                             marker_path=marker_path,
                             spawn=RecordingSpawn(object()))

    assert output.apply(1.0) is False
    assert log == []
    assert output.holds_axis() is False
    assert not os.path.exists(marker_path)


def test_releasing_hands_lfs_back_before_it_parks_our_axis(axis_output, log):
    """Handback first: if the park below it fails, the driver already has
    their pedal."""
    axis_output.apply(1.0)
    log.clear()

    axis_output.release()

    assert log == [('command', '/axis 12 brake'), ('raw', 32767)]


def test_releasing_parks_the_axis_at_no_brake_and_not_at_the_last_demand(
        axis_output, device):
    """vJoy holds the last value it was fed forever, so the value left behind
    between interventions must be the harmless one."""
    axis_output.apply(0.8)
    axis_output.release()

    assert device.writes[-1] == 32767      # this machine's "no brake"


def test_releasing_without_ever_having_held_the_axis_does_nothing(
        axis_output, log):
    """Otherwise a clean shutdown would point ``brake`` at whatever number the
    settings hold -- and break a working configuration on every exit."""
    axis_output.release()

    assert log == []


def test_releasing_twice_hands_back_once(axis_output, log):
    axis_output.apply(0.5)
    log.clear()

    axis_output.release()
    axis_output.release()

    commands = [entry for entry in log if entry[0] == 'command']
    assert commands == [('command', '/axis 12 brake')]


def test_the_axis_can_be_taken_again_after_a_handback(axis_output, log):
    axis_output.apply(0.5)
    axis_output.release()
    log.clear()

    axis_output.apply(0.5)

    assert log == [('raw', 16384), ('command', '/axis 15 brake')]


def test_holds_axis_reports_the_swap_and_only_the_swap(axis_output):
    assert axis_output.holds_axis() is False
    axis_output.apply(0.3)
    assert axis_output.holds_axis() is True
    axis_output.release()
    assert axis_output.holds_axis() is False


# ─── Controls/brake_axis.py -- the handover marker ───────────────────────────

def test_engaging_writes_the_handback_command_into_the_marker(
        axis_output, marker_path):
    """The marker carries the command that restores what we took away, with
    the axis number that was in force at the moment of the swap."""
    axis_output.apply(0.5)

    assert os.path.exists(marker_path)
    with open(marker_path, encoding='utf-8') as handle:
        assert json.load(handle) == {'commands': ['/axis 12 brake']}


def test_handing_back_removes_the_marker(axis_output, marker_path):
    axis_output.apply(0.5)
    axis_output.release()

    assert not os.path.exists(marker_path)


def test_shutdown_removes_the_marker_and_lets_the_device_go(
        axis_output, marker_path, device, log):
    axis_output.apply(0.5)
    log.clear()

    axis_output.shutdown()

    assert not os.path.exists(marker_path)
    assert device.relinquished == 1
    assert log[0] == ('command', '/axis 12 brake')


def test_shutdown_relinquishes_even_when_the_handback_raises(
        axis_settings, log, marker_path):
    """``release`` sits in a ``try``/``finally`` for exactly this reason: at
    shutdown the bus may already be half torn down."""
    class DeadBus:
        def emit(self, *_args):
            raise RuntimeError("the bus is gone")

    device = FakeVJoy(log)
    output = AxisBrakeOutput(DeadBus(), axis_settings, device=device,
                             marker_path=marker_path,
                             spawn=RecordingSpawn(object()))
    output._holds_axis = True
    device.acquire()

    with pytest.raises(RuntimeError):
        output.shutdown()

    assert device.relinquished == 1


def test_a_marker_that_cannot_be_written_does_not_stop_the_intervention(
        bus, axis_settings, log, tmp_path):
    """Without the marker the guardian simply does nothing, which is where we
    were before it existed -- not a reason to refuse to brake."""
    unwritable = str(tmp_path / 'no-such-directory' / 'brake_axis_held.marker')
    output = AxisBrakeOutput(bus, axis_settings, device=FakeVJoy(log),
                             marker_path=unwritable,
                             spawn=RecordingSpawn(object()))

    assert output.apply(1.0) is True
    assert ('command', '/axis 15 brake') in log


def test_clearing_a_marker_that_is_already_gone_is_not_an_error(
        axis_output, marker_path):
    axis_output.apply(0.5)
    os.remove(marker_path)

    axis_output.release()          # must not raise

    assert axis_output.holds_axis() is False


# ─── Controls/brake_axis.py -- arming ────────────────────────────────────────

def test_an_uncalibrated_axis_is_refused_before_the_device_is_consulted(
        bus, make_settings, marker_path):
    """Polarity is measured, never assumed -- so an unmeasured axis must be
    refused whether or not vJoy is there at all."""
    settings = make_settings(vjoy_brake_calibrated=False,
                             vjoy_axis_1=15, user_axis_brake=12)
    output = AxisBrakeOutput(bus, settings, device=ExplodingVJoy(),
                             marker_path=marker_path,
                             spawn=RecordingSpawn(None))

    assert output.unavailable_reason() == 'vjoy_not_calibrated'


def test_two_identical_axis_numbers_are_refused(bus, make_settings, marker_path):
    """Handing back to ourselves is not a handback."""
    settings = make_settings(vjoy_brake_calibrated=True,
                             vjoy_axis_1=12, user_axis_brake=12)
    output = AxisBrakeOutput(bus, settings, device=ExplodingVJoy(),
                             marker_path=marker_path,
                             spawn=RecordingSpawn(None))

    assert output.unavailable_reason() == 'vjoy_and_driver_axis_identical'


def test_an_otherwise_valid_configuration_defers_to_the_device(
        bus, axis_settings, log, marker_path):
    device = FakeVJoy(log, unavailable='vjoy_device_busy')
    output = AxisBrakeOutput(bus, axis_settings, device=device,
                             marker_path=marker_path,
                             spawn=RecordingSpawn(None))

    assert output.unavailable_reason() == 'vjoy_device_busy'
    assert device.reason_queries == 1


def test_a_healthy_device_and_a_calibrated_axis_give_no_reason_at_all(
        axis_output):
    assert axis_output.unavailable_reason() is None


# ─── Controls/brake_axis.py -- the watchdog ──────────────────────────────────

def test_the_guardian_is_spawned_once_however_often_arming_is_rechecked(
        bus, axis_settings, device, marker_path):
    """``start_guardian`` is called from the assistance cycle, so ten times a
    second while the axis path is armed."""
    spawn = RecordingSpawn(object())
    output = AxisBrakeOutput(bus, axis_settings, device=device,
                             marker_path=marker_path, spawn=spawn)

    output.start_guardian()
    assert spawn.called.wait(2.0), "the spawn thread never ran"
    for _ in range(20):
        output.start_guardian()

    assert spawn.pids == [os.getpid()]


def test_a_guardian_that_could_not_be_started_is_not_retried_every_cycle(
        bus, axis_settings, device, marker_path):
    """``_spawn_guardian`` returns ``None`` when ``guardian.py`` is missing or
    ``Popen`` fails. ``start_guardian`` documents itself as idempotent, and the
    caller is the 100 ms assistance pass, so a failure must not turn into a
    thread-and-process-creation storm."""
    spawn = RecordingSpawn(None)
    output = AxisBrakeOutput(bus, axis_settings, device=device,
                             marker_path=marker_path, spawn=spawn)

    output.start_guardian()
    assert spawn.called.wait(2.0), "the spawn thread never ran"
    _settle(output)
    spawn.called.clear()

    output.start_guardian()

    assert not spawn.called.wait(0.5), \
        "a failed spawn is retried on the very next call"


def _settle(output, timeout: float = 2.0):
    """Wait for the spawn thread to have written its result back."""
    deadline = time.monotonic() + timeout
    while output._guardian is False and time.monotonic() < deadline:
        time.sleep(0.005)


# ─── guardian.py -- the handover marker ──────────────────────────────────────

def test_a_missing_marker_means_the_brake_was_never_ours(tmp_path):
    """``None`` is "do nothing" -- the normal case after a clean exit."""
    assert guardian.read_marker(str(tmp_path / 'absent.marker')) is None


@pytest.mark.parametrize('written, expected', [
    ('{"commands": ["/axis 12 brake"]}', ['/axis 12 brake']),
    ('{"commands": ["/axis 12 brake", "/axis 9 throttle"]}',
     ['/axis 12 brake', '/axis 9 throttle']),
    ('{"commands": ["/key up throttle"]}', ['/key up throttle']),
])
def test_a_marker_yields_the_commands_it_names(tmp_path, written, expected):
    path = tmp_path / 'brake_axis_held.marker'
    path.write_text(written, encoding='utf-8')

    assert guardian.read_marker(str(path)) == expected


@pytest.mark.parametrize('written, expected', [('12', 12),
                                               ('0', 0),
                                               ('31', 31),
                                               (' 15 \n', 15)])
def test_a_pre_json_marker_still_hands_the_brake_back(
        tmp_path, written, expected):
    """Markers from the version that could only ever hold the brake."""
    path = tmp_path / 'brake_axis_held.marker'
    path.write_text(written, encoding='utf-8')

    assert guardian.read_marker(str(path)) == [f'/axis {expected} brake']


@pytest.mark.parametrize('written', ['', '   ', 'twelve', '-1', '32', '3.5',
                                     '12 13', '\x00', '{"commands": "no"}',
                                     '{"other": []}', '{'])
def test_a_marker_we_cannot_read_still_means_something_was_held(
        tmp_path, written):
    """An empty list is "act, but fall back to settings": the file existing at
    all is the evidence that we died holding the axis."""
    path = tmp_path / 'brake_axis_held.marker'
    path.write_text(written, encoding='utf-8')

    assert guardian.read_marker(str(path)) == []


@pytest.mark.parametrize('command', [
    '/msg pwned', '/axis 12 steer', '/axis -1 brake', '/key -1 throttle',
    '/axis 12 brake; /msg x', 'axis 12 brake', '/end',
])
def test_a_marker_command_outside_the_whitelist_is_dropped(tmp_path, command):
    """The marker is a file on disk. A watchdog that typed whatever it found
    there into the game would not be a safety device -- and an *unassign* is
    never a restore, so those are refused too."""
    path = tmp_path / 'brake_axis_held.marker'
    path.write_text(json.dumps({'commands': [command]}), encoding='utf-8')

    assert guardian.read_marker(str(path)) == []


# ─── guardian.py -- the settings fallback ────────────────────────────────────

def test_the_configured_brake_axis_is_read_out_of_settings(tmp_path):
    path = tmp_path / 'settings.json'
    path.write_text(json.dumps({'user_axis_brake': 7}), encoding='utf-8')

    assert guardian.read_brake_axis(str(path)) == 7


@pytest.mark.parametrize('content', [None,                    # no file at all
                                     '',
                                     '{not json',
                                     '[]',
                                     '{"user_axis_brake": null}',
                                     '{"user_axis_brake": "twelve"}',
                                     '{}',
                                     '{"user_axis_brake": -1}',
                                     '{"user_axis_brake": 32}',
                                     '{"user_axis_brake": 999}'])
def test_an_unusable_settings_file_falls_back_instead_of_raising(
        tmp_path, content):
    """The guardian runs while the main app is being torn down; raising here
    would mean the brake is never handed back."""
    path = tmp_path / 'settings.json'
    if content is not None:
        path.write_text(content, encoding='utf-8')

    assert guardian.read_brake_axis(str(path)) == guardian.DEFAULT_BRAKE_AXIS


# ─── guardian.py -- the packets ──────────────────────────────────────────────
#
# The Size field is the byte count DIVIDED BY FOUR since InSim v9. Sending the
# byte count makes LFS drop the connection without a word, so the two
# hand-written packets are compared against pyinsim's own.

def test_the_handshake_is_byte_identical_to_pyinsims_is_isi():
    reference = pyinsim.IS_ISI(ReqI=0, UDPPort=0, Flags=guardian.ISF_LOCAL,
                               Prefix=b' ', Interval=0, Admin=b'',
                               IName=b'guardian').pack()

    assert guardian._isi_packet() == reference


def test_the_command_packet_is_byte_identical_to_pyinsims_is_mst():
    command = '/axis 12 brake'
    reference = pyinsim.IS_MST(ReqI=0, Msg=command.encode('latin-1')).pack()

    assert guardian._mst_packet(command) == reference


def test_the_size_field_is_the_byte_count_divided_by_four():
    isi, mst = guardian._isi_packet(), guardian._mst_packet('/axis 12 brake')

    assert (len(isi), isi[0]) == (44, 11)
    assert (len(mst), mst[0]) == (68, 17)


def test_the_guardian_speaks_the_same_insim_version_as_the_app():
    assert guardian.INSIM_VERSION == pyinsim.INSIM_VERSION


def test_a_command_longer_than_the_packet_is_truncated_rather_than_rejected():
    packet = guardian._mst_packet('/axis 12 brake' + 'x' * 200)

    assert len(packet) == 68
    assert struct.Struct('4B63sx').unpack(packet)[4].rstrip(b'\x00') == \
        ('/axis 12 brake' + 'x' * 200).encode('latin-1')[:63]


# ─── guardian.py -- hand_back ────────────────────────────────────────────────

def test_handing_back_sends_the_handshake_and_then_the_axis_command(
        monkeypatch):
    """No socket is opened; the bytes that would have gone out are asserted."""
    sink = []
    monkeypatch.setattr(guardian.socket, 'create_connection',
                        lambda address, timeout=None: FakeSocket(sink))
    monkeypatch.setattr(guardian.time, 'sleep', lambda _seconds: None)

    assert guardian.hand_back(['/axis 12 brake', '/axis 9 throttle']) is True

    sent = [payload for kind, payload in sink if kind == 'sent']
    assert sent == [guardian._isi_packet(),
                    guardian._mst_packet('/axis 12 brake'),
                    guardian._mst_packet('/axis 9 throttle')]
    assert sink[-1][0] == 'closed'


def test_handing_nothing_back_opens_no_connection(monkeypatch):
    """An empty command list means there was nothing to restore."""
    def explode(address, timeout=None):
        raise AssertionError("no connection should have been opened")

    monkeypatch.setattr(guardian.socket, 'create_connection', explode)

    assert guardian.hand_back([]) is False


def test_an_unreachable_lfs_is_not_an_error(monkeypatch):
    """If LFS is gone too, there is nothing to hand back to."""
    def refuse(address, timeout=None):
        raise ConnectionRefusedError(111, 'nobody is listening')

    monkeypatch.setattr(guardian.socket, 'create_connection', refuse)

    assert guardian.hand_back(['/axis 12 brake']) is False


# ─── guardian.py -- watch ────────────────────────────────────────────────────

@pytest.fixture
def handbacks(monkeypatch):
    """Records the command lists ``watch`` would have sent."""
    calls = []

    def fake_hand_back(commands):
        calls.append(list(commands))
        return True

    monkeypatch.setattr(guardian, 'hand_back', fake_hand_back)
    return calls


@pytest.fixture
def dead_process(monkeypatch):
    """A watched PID that is already gone."""
    monkeypatch.setattr(guardian, 'process_is_alive', lambda pid: False)


def test_a_clean_shutdown_leaves_the_brake_configuration_alone(
        tmp_path, handbacks, dead_process):
    """The important one. No marker means the brake was not ours, and forcing
    ``brake`` onto a possibly-wrong settings value would break a working
    configuration on every single exit."""
    settings = tmp_path / 'settings.json'
    settings.write_text(json.dumps({'user_axis_brake': 7}), encoding='utf-8')

    handed_back = guardian.watch(4242, str(settings),
                                 str(tmp_path / 'absent.marker'),
                                 sleep=_never_sleep)

    assert handed_back is False
    assert handbacks == []


def test_a_marker_left_behind_hands_the_axis_it_names_back(
        tmp_path, handbacks, dead_process):
    marker = tmp_path / 'brake_axis_held.marker'
    marker.write_text('9', encoding='utf-8')
    settings = tmp_path / 'settings.json'
    settings.write_text(json.dumps({'user_axis_brake': 7}), encoding='utf-8')

    handed_back = guardian.watch(4242, str(settings), str(marker),
                                 sleep=_never_sleep)

    assert handed_back is True
    # The marker wins over settings, and is cleaned up afterwards.
    assert handbacks == [['/axis 9 brake']]
    assert not marker.exists()


def test_an_unusable_marker_falls_back_to_the_configured_axis(
        tmp_path, handbacks, dead_process):
    marker = tmp_path / 'brake_axis_held.marker'
    marker.write_text('nonsense', encoding='utf-8')
    settings = tmp_path / 'settings.json'
    settings.write_text(json.dumps({'user_axis_brake': 7}), encoding='utf-8')

    assert guardian.watch(4242, str(settings), str(marker),
                          sleep=_never_sleep) is True
    assert handbacks == [['/axis 7 brake']]


def test_an_unusable_marker_and_no_settings_still_hands_something_back(
        tmp_path, handbacks, dead_process):
    marker = tmp_path / 'brake_axis_held.marker'
    marker.write_text('', encoding='utf-8')

    assert guardian.watch(4242, str(tmp_path / 'absent.json'), str(marker),
                          sleep=_never_sleep) is True
    assert handbacks == [[f'/axis {guardian.DEFAULT_BRAKE_AXIS} brake']]


def test_a_marker_survives_a_handback_lfs_never_received(
        tmp_path, monkeypatch, dead_process):
    """If LFS could not be reached the brake is still ours as far as anyone
    knows, so the evidence must not be thrown away."""
    monkeypatch.setattr(guardian, 'hand_back', lambda commands: False)
    marker = tmp_path / 'brake_axis_held.marker'
    marker.write_text('9', encoding='utf-8')

    assert guardian.watch(4242, str(tmp_path / 'absent.json'), str(marker),
                          sleep=_never_sleep) is False
    assert marker.exists()


def test_watching_polls_until_the_process_disappears(
        tmp_path, handbacks, monkeypatch):
    """Driven by an injected sleep and a fake liveness check -- no process is
    started and no wall clock is waited on."""
    alive = iter([True, True, True, False])
    monkeypatch.setattr(guardian, 'process_is_alive', lambda pid: next(alive))
    slept = []
    marker = tmp_path / 'brake_axis_held.marker'
    marker.write_text('9', encoding='utf-8')

    guardian.watch(4242, str(tmp_path / 'absent.json'), str(marker),
                   poll_interval=2.0, sleep=slept.append)

    assert slept == [2.0, 2.0, 2.0]
    assert handbacks == [['/axis 9 brake']]


def test_the_marker_is_only_read_after_the_process_is_gone(
        tmp_path, handbacks, monkeypatch):
    """A marker written *while* we are still alive is a live intervention, not
    a dead one -- the read has to happen after the PID disappears."""
    marker = tmp_path / 'brake_axis_held.marker'
    states = iter([True, False])

    def alive(pid):
        still_running = next(states)
        if not still_running:
            marker.write_text('9', encoding='utf-8')
        return still_running

    monkeypatch.setattr(guardian, 'process_is_alive', alive)

    guardian.watch(4242, str(tmp_path / 'absent.json'), str(marker),
                   sleep=lambda _seconds: None)

    assert handbacks == [['/axis 9 brake']]


def _never_sleep(_seconds):
    raise AssertionError("nothing should be waited on: the PID is already gone")


# ─── misc/vjoy_device.py ─────────────────────────────────────────────────────

def test_a_machine_without_vjoy_reports_it_instead_of_raising(monkeypatch):
    """The DLL is never loaded here -- the lookup is patched out, so this holds
    on a developer machine that does have vJoy installed."""
    monkeypatch.setattr('misc.vjoy_device._find_dll', lambda: None)
    device = VJoyDevice()

    assert device.unavailable_reason() == 'vjoy_not_installed'
    assert device.unavailable_reason() == 'vjoy_not_installed'   # and again
    assert device.acquire() is False
    assert device.version() is None
    device.relinquish()                                          # must not raise


def test_a_dll_that_will_not_load_is_reported_as_missing(monkeypatch, tmp_path):
    """A 32-bit DLL under a 64-bit Python raises inside ``CDLL``; degrade,
    never propagate."""
    bogus = tmp_path / 'vJoyInterface.dll'
    bogus.write_bytes(b'not a dll')
    monkeypatch.setattr('misc.vjoy_device._find_dll', lambda: str(bogus))
    device = VJoyDevice()

    assert device.unavailable_reason() == 'vjoy_not_installed'


def test_an_unacquired_device_refuses_to_write(monkeypatch):
    monkeypatch.setattr('misc.vjoy_device._find_dll', lambda: None)
    device = VJoyDevice()

    assert device.acquired is False
    assert device.set_raw(16384) is False


def test_a_raw_value_is_clamped_to_the_range_the_device_reported():
    """The mapping interpolates between calibrated endpoints, but a hand-edited
    settings file can put those outside the device's range."""
    device = VJoyDevice()
    dll = FakeDll()
    device._dll, device._loaded, device.acquired = dll, True, True
    device.raw_min, device.raw_max = 0, 32767

    assert device.set_raw(-5000) is True
    assert device.set_raw(99999) is True
    assert device.set_raw(16384.7) is True

    assert [value for value, _id, _axis in dll.axis_writes] == [0, 32767, 16384]


def test_a_device_with_a_signed_range_clamps_to_that_range_instead():
    """``acquire`` learns the range from the driver; nothing may assume
    0..32767."""
    device = VJoyDevice()
    dll = FakeDll()
    device._dll, device._loaded, device.acquired = dll, True, True
    device.raw_min, device.raw_max = -16384, 16383

    device.set_raw(-99999)
    device.set_raw(99999)

    assert [value for value, _id, _axis in dll.axis_writes] == [-16384, 16383]


def test_a_failing_setaxis_is_reported_rather_than_raised():
    device = VJoyDevice()

    class Exploding:
        def SetAxis(self, *_args):
            raise OSError("the driver went away")

    device._dll, device._loaded, device.acquired = Exploding(), True, True

    assert device.set_raw(0) is False


def test_relinquishing_a_device_we_never_took_does_nothing():
    device = VJoyDevice()

    device.relinquish()          # no DLL at all -- must not raise

    assert device.acquired is False


# ─── known-issues #56: the DLL load must not sit in the assistance pass ──────

def test_the_dll_is_loaded_off_the_calling_thread(monkeypatch):
    """``ctypes.CDLL`` on vJoyInterface costs ~72 ms with a warm file cache --
    most of a 100 ms assistance cycle. ``prepare`` moves it to a thread of its
    own and says "not yet" until it is done."""
    caller = threading.get_ident()
    loaded_on = []
    release = threading.Event()

    def slow_load(self):
        release.wait(5.0)
        loaded_on.append(threading.get_ident())
        self._dll = FakeDll()
        self._loaded = True

    monkeypatch.setattr(VJoyDevice, '_load_locked', slow_load, raising=True)
    device = VJoyDevice()

    assert device.prepare() is False          # started, not finished
    assert device.loading() is True
    assert device.prepare() is False          # idempotent: no second thread
    release.set()

    deadline = time.monotonic() + 5.0
    while device.loading() and time.monotonic() < deadline:
        time.sleep(0.005)

    assert device.prepare() is True
    assert device.loading() is False
    assert len(loaded_on) == 1
    assert loaded_on[0] != caller


def test_a_load_that_raises_does_not_block_every_later_attempt(monkeypatch):
    """A dead warm-up thread would leave ``_loading`` set forever, and the
    axis path would never arm -- the quiet failure AGENTS.md §3 forbids."""
    def exploding(_self):
        raise OSError("the driver went away")

    monkeypatch.setattr(VJoyDevice, '_load_locked', exploding, raising=True)
    device = VJoyDevice()
    device.prepare()

    deadline = time.monotonic() + 5.0
    while device.loading() and time.monotonic() < deadline:
        time.sleep(0.005)

    assert device.loading() is False


def test_the_output_says_loading_rather_than_broken_while_the_dll_comes_up(
        bus, axis_settings, marker_path):
    """A transient reason, not a fault: reporting it would put
    "AEB unavailable" on screen at every startup."""
    class Loading:
        def prepare(self):
            return False

        def loading(self):
            return True

        def unavailable_reason(self):
            raise AssertionError("must not be asked before the DLL is loaded")

    output = AxisBrakeOutput(bus, axis_settings, device=Loading(),
                             marker_path=marker_path, spawn=lambda _pid: None)

    assert output.unavailable_reason() == REASON_LOADING
