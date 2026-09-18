"""Offline regression tests: no live hooks, desktop input or network."""
import importlib.util
import json
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

HARNESS_ROOT = Path(__file__).resolve().parents[1] / "simulations-tests"
sys.path.insert(0, str(HARNESS_ROOT))
from harness.scenario import load, play
from harness.trace import Trace, plain
from harness.monitor import Collector


def replay(events=None, duration=1):
    return dict(schema=1, sample_ms=50, duration=duration, events=events or [])


def key(t, down):
    return dict(t=t, kind="key", code=65, down=down)


@pytest.mark.parametrize("events", [
    [key(.2, True)], [key(.2, False)], [key(.2, True), key(.1, False)],
    [key(float("nan"), True)], [dict(t=.1, kind="shell")],
    [dict(t=.1, kind="key", code=123, down=True)],
])
def test_invalid_replay_rejected_before_input(tmp_path, events):
    path = tmp_path / "replay.json"
    path.write_text(json.dumps(replay(events)), encoding="utf-8")
    with pytest.raises(ValueError):
        load(path)


def test_short_press_is_preserved(tmp_path):
    path = tmp_path / "replay.json"
    events = [key(.011, True), key(.023, False)]
    path.write_text(json.dumps(replay(events)), encoding="utf-8")
    assert load(path)["events"] == events


class Clock:
    value = 0

    def now(self):
        return self.value

    def pump(self, delta):
        self.value += delta


def test_replay_absolute_schedule_and_trailing_wait():
    clock = Clock()
    actual = []
    backend = SimpleNamespace(apply=lambda e: actual.append((e, clock.now())),
                              release=lambda: actual.append(("released", clock.now())))
    trace = SimpleNamespace(write=lambda *a, **k: None)
    play(replay([key(.1, True), key(.3, False)]), backend, trace,
         clock.pump, clock.now, lambda: None)
    assert actual[0][1] == pytest.approx(.1)
    assert actual[1][1] == pytest.approx(.3)
    assert actual[-1] == ("released", 1)


def test_guard_failure_always_releases_controls():
    released = []
    clock = Clock()
    def guard():
        if clock.now() >= .15:
            raise RuntimeError("focus lost")
    backend = SimpleNamespace(apply=lambda e: None, release=lambda: released.append(True))
    with pytest.raises(RuntimeError, match="focus lost"):
        play(replay([key(.1, True), key(.3, False)]), backend,
             SimpleNamespace(write=lambda *a, **k: None), clock.pump, clock.now, guard)
    assert released == [True]


def test_scheduler_overrun_does_not_burst_old_inputs():
    applied = []
    clock = Clock()
    backend = SimpleNamespace(apply=applied.append, release=lambda: None)
    with pytest.raises(RuntimeError, match="overrun"):
        play(replay([key(.1, True), key(.2, False)]), backend,
             SimpleNamespace(write=lambda *a, **k: None), lambda _: clock.pump(1), clock.now, lambda: None)
    assert not applied


def test_trace_preserves_nested_packets_and_device_time(tmp_path):
    clock = Clock()
    path = tmp_path / "trace.jsonl"
    trace = Trace(path, clock=lambda: int(clock.now() * 1e9))
    clock.pump(.017)
    trace.write("OutGaugePack", SimpleNamespace(Time=987, Car=b"\xffX\x00"))
    clock.pump(.023)
    trace.write("IS_MCI", SimpleNamespace(Info=[SimpleNamespace(PLID=2, Speed=100)]))
    trace.close()
    rows = [json.loads(line) for line in path.read_text().splitlines()]
    assert [r["seq"] for r in rows] == [0, 1, 2]
    assert rows[1]["t"] == pytest.approx(.017)
    assert rows[2]["t"] == pytest.approx(.04)
    assert rows[1]["data"]["Time"] == 987
    assert bytes.fromhex(rows[1]["data"]["Car"]["hex"]) == b"\xffX\x00"
    assert rows[2]["data"]["Info"][0]["PLID"] == 2


@pytest.mark.parametrize("request_streams", [True, False])
def test_observer_requests_separate_streams_and_contact_flags(monkeypatch, request_streams):
    import pyinsim
    sent, bound = [], []
    connection = SimpleNamespace(send=lambda *a, **k: sent.append((a, k)),
                                 bind=lambda *a: bound.append(a), close=lambda: None,
                                 _handle_insim_packet=lambda data: None)
    calls = []
    def connect(*a, **k):
        calls.append(k)
        return connection
    monkeypatch.setattr(pyinsim, "insim", connect)
    observer = Collector(SimpleNamespace(write=lambda *a: None), request_streams=request_streams)
    assert calls[0]["UDPPort"] == 30001
    assert calls[0]["Flags"] & pyinsim.ISF_CON
    assert calls[0]["Flags"] & pyinsim.ISF_OBH
    streams = {kw["SubT"] for a, kw in sent if a[0] == pyinsim.ISP_SMALL}
    assert streams == ({pyinsim.SMALL_SSG, pyinsim.SMALL_SSP} if request_streams else set())
    observer.failed(connection)
    with pytest.raises(RuntimeError):
        observer.check()


@pytest.mark.parametrize("flags,valid", [(0, True), (1, False), (256, False), (16, False), (32768, False)])
def test_menu_requires_fresh_state(flags, valid):
    observer = Collector.__new__(Collector)
    observer.state = SimpleNamespace(Flags=0)
    observer.menu_request_id = 1
    observer.error = None
    observer.connection = SimpleNamespace(send=lambda *a, **k: None)
    def pump(_):
        observer.menu_state = SimpleNamespace(Flags=flags)
    if valid:
        observer.menu(pump)
    else:
        with pytest.raises(RuntimeError, match="main menu"):
            observer.menu(pump)


def test_keyboard_auto_repeat_and_reserved_keys_do_not_enter_replay():
    import threading
    from harness.desktop import Desktop
    desktop = Desktop.__new__(Desktop)
    desktop.lock = threading.Lock()
    desktop.pending, desktop.physical = [], set()
    desktop.clock = lambda: .1
    desktop.stop = desktop.abort = False
    desktop.key(SimpleNamespace(vk=65), True)
    desktop.key(SimpleNamespace(vk=65), True)
    desktop.key(SimpleNamespace(vk=65), False)
    desktop.key(SimpleNamespace(vk=122), True)
    assert desktop.stop
    assert [e["down"] for e in desktop.pending] == [True, False]


def test_release_attempts_all_keys_even_if_one_fails():
    from harness.desktop import Desktop
    desktop = Desktop.__new__(Desktop)
    desktop.held = {("key", 65), ("key", 66)}
    desktop.physical = set()
    released = []
    def release(code):
        released.append(code)
        if code == 65:
            raise OSError("injection failed")
    desktop.target = lambda token: (SimpleNamespace(release=release), token[1])
    with pytest.raises(RuntimeError, match="cleanup"):
        desktop.release()
    assert set(released) == {65, 66}


def test_standalone_template_imports_without_opening_connection():
    spec = importlib.util.spec_from_file_location("observer_template_test", HARNESS_ROOT / "monitor_template.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    assert issubclass(module.Monitor, Collector)


def test_v10_car_contact_unsigned_pedals_and_signed_acceleration():
    import struct
    import pyinsim
    from harness.protocol import IS_CON
    a = struct.pack("<3Bb6B2b2h", 1, 0, 0, -20, 0xF2, 0x80, 0xF0, 200, 240, 250, -8, -5, -30, 90)
    b = struct.pack("<3Bb6B2b2h", 2, 0, 0, 20, 0, 0, 0, 0, 0, 0, 8, 5, 30, -90)
    packet = IS_CON(struct.pack("<4B2HI", 11, pyinsim.ISP_CON, 0, 0, 350, 0, 1234567) + a + b)
    assert packet.Time == 1234567
    assert packet.A.PLID == 1 and packet.B.PLID == 2
    assert packet.A.Speed == 200 and packet.A.ThrBrk == 0xF2
    assert packet.A.AccelF == -8 and packet.A.Heading == 250


def test_receive_keeps_unknown_packets_and_bad_decode_evidence():
    import pyinsim
    observer = Collector.__new__(Collector)
    recorded = []
    observer.trace = SimpleNamespace(write=lambda *a: recorded.append(a))
    observer.packet_types = {}
    observer.connection = None
    observer.receive(bytes([1, 254, 0, 0]))
    assert recorded[0][0] == "unsupported_packet"
    with pytest.raises(ValueError, match="44 bytes"):
        observer.receive(bytes([1, pyinsim.ISP_CON, 0, 0]))
    assert recorded[-1][0] == "decode_or_callback_error"


def test_udp_fanout_preserves_bytes_and_both_destinations():
    from udp_relay import forward
    sent = []
    forward(SimpleNamespace(sendto=lambda *a: sent.append(a)), b"\x00\xff\x10", (30000, 30001))
    assert sent == [(b"\x00\xff\x10", ("127.0.0.1", 30000)),
                    (b"\x00\xff\x10", ("127.0.0.1", 30001))]


def test_unsolicited_state_does_not_complete_fresh_menu_request():
    observer = Collector.__new__(Collector)
    observer.trace = SimpleNamespace(write=lambda *a: None)
    observer.packets = {}
    observer.menu_request_id = 7
    observer.menu_state = None
    packet_type = type("IS_STA", (), {})
    packet = packet_type()
    packet.ReqI, packet.Flags = 0, 0
    observer.packet(None, packet)
    assert observer.menu_state is None
    packet.ReqI = 7
    observer.packet(None, packet)
    assert observer.menu_state is packet
