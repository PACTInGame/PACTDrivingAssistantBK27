"""Tests for the standalone simulation-test harness (``simulation_tests/``).

The harness drives LFS with real mouse and keyboard input, so almost nothing in
it can be exercised for real on a CI box. What *is* tested here is everything
that decides whether a run is usable: the unit conversions a trace is read with,
the ordering guarantee of the trace writer, the input recording format, the
schedule and the release-everything invariant of the replay, and one end-to-end
run of the tracer against a fake LFS.

pynput is replaced by a recording double, so no key is ever actually pressed.
"""

from __future__ import annotations

import json
import os
import socket
import struct
import subprocess
import sys
import time
import types

import pytest

from simulation_tests import (analyze_trace, input_model, insim_patch, packet_dump,
                              paths, scenario as scenario_mod, timeline_draft)
from simulation_tests.control_channel import ControlClient, ControlServer
from simulation_tests.trace_format import TraceWriter, load_trace, meta_of


# ── packet decoding and unit conversion ──────────────────────────────────────
def test_decode_flags_names_known_bits_and_reports_unknown_ones():
    assert packet_dump.decode_flags(1 | 16384, packet_dump.ISS_FLAGS) == ["GAME", "VISIBLE"]
    assert packet_dump.decode_flags(0, packet_dump.ISS_FLAGS) == []
    # bit 2 (value 4) is not in the OG table -> reported, not silently dropped
    assert "bit2" in packet_dump.decode_flags(4, packet_dump.OG_FLAGS)


def test_compcar_is_converted_to_metres_kmh_and_degrees():
    car = {"X": 10 * 65536, "Y": -3 * 65536, "Z": 65536 // 2,
           "Speed": 9102, "Heading": 16384, "Direction": 16384,
           "AngVel": 16384, "Info": 64 | 128}
    packet_dump._derive_compcar(car)
    assert car["x_m"] == 10.0
    assert car["y_m"] == -3.0
    assert car["z_m"] == 0.5
    assert car["speed_kmh"] == pytest.approx(100.0, abs=0.05)
    # 16384/65536 == a quarter turn; LFS measures from +Y anticlockwise
    assert car["heading_deg"] == pytest.approx(90.0)
    # ... which is 180 deg in the +X-based frame the repo's trig uses
    assert car["heading_math_deg"] == pytest.approx(180.0)
    assert car["angvel_deg_s"] == pytest.approx(360.0)
    assert car["info_flags"] == ["FIRST", "LAST"]


def test_packet_to_dict_decodes_flags_and_drops_framing_fields():
    packet = types.SimpleNamespace(Size=7, Type=5, ReqI=0, Zero=0, Flags=1 | 256,
                                   InGameCam=3, Track=b"BL1\x00\x00", NumP=2)
    data = packet_dump.packet_to_dict("STA", packet)
    assert "Size" not in data and "Type" not in data
    assert data["Track"] == "BL1"
    assert data["cam"] == "DRIVER"
    assert data["flags_flags"] == ["GAME", "FRONT_END"]
    # GAME and FRONT_END together is the entry screen, not the track
    assert data["on_track"] is False


def test_mod_car_name_keeps_its_bytes_readable():
    """A vehicle mod puts arbitrary bytes in Car/CName (conventions.md §4)."""
    packet = types.SimpleNamespace(Car=b"\x06\xc8\xd3", Speed=10.0, Gear=2,
                                   Flags=0, DashLights=0, ShowLights=0)
    data = packet_dump.packet_to_dict("OutGauge", packet)
    assert data["Car"] == {"hex": "06c8d3", "text": "\x06\xc8\xd3"}
    assert data["speed_kmh"] == 36.0
    assert data["gear_label"] == "1"


# ── trace writer ─────────────────────────────────────────────────────────────
def test_trace_writer_keeps_capture_order_and_counts(tmp_path):
    path = str(tmp_path / "trace.jsonl")
    writer = TraceWriter(path)
    writer.write("tracer", "meta", {"format": 1}, t=0.0)
    for i in range(200):
        writer.write("insim", "MCI", {"i": i})
    writer.write("marker", "done")
    writer.close()

    records = load_trace(path)
    assert records[0]["ev"] == "meta"
    assert records[-1]["ev"] == "done"
    assert [r["d"]["i"] for r in records if r["ev"] == "MCI"] == list(range(200))
    assert writer.counts["MCI"] == 200
    assert writer.dropped == 0
    writer.close()  # idempotent


def test_trace_writer_survives_unencodable_payloads(tmp_path):
    path = str(tmp_path / "trace.jsonl")
    writer = TraceWriter(path)
    writer.write("insim", "ODD", {"blob": b"\x00\x01", "obj": object()})
    writer.close()
    record = load_trace(path)[0]
    assert record["d"]["blob"] == "0001"
    assert isinstance(record["d"]["obj"], str)


def test_read_trace_skips_a_torn_final_line(tmp_path):
    path = tmp_path / "trace.jsonl"
    path.write_text('{"t":0,"src":"tracer","ev":"meta","d":{}}\n{"t":1,"src":"in',
                    encoding="utf-8")
    records = load_trace(str(path))
    assert len(records) == 1
    assert meta_of(records) == {}


# ── input recording format ───────────────────────────────────────────────────
def test_recording_round_trip_sorts_and_validates(tmp_path):
    path = str(tmp_path / "input.jsonl")
    events = [
        {"t": 0.5, "kind": "key", "action": "down", "name": "w", "vk": 87},
        {"t": 0.1, "kind": "move", "x": 10, "y": 20},
        {"t": 0.9, "kind": "marker", "name": "rolling"},
    ]
    input_model.write_recording(path, {"format": 1, "screen_size": [1920, 1080]}, events)
    meta, loaded = input_model.read_recording(path)
    assert meta["screen_size"] == [1920, 1080]
    assert [e["t"] for e in loaded] == [0.1, 0.5, 0.9]
    assert input_model.markers(loaded) == [(0.9, "rolling")]
    assert input_model.duration(loaded) == 0.9


def test_reading_a_file_without_meta_is_an_error(tmp_path):
    path = tmp_path / "input.jsonl"
    path.write_text('{"t":0.1,"kind":"move","x":1,"y":2}\n', encoding="utf-8")
    with pytest.raises(ValueError, match="no meta record"):
        input_model.read_recording(str(path))


def test_unknown_event_kind_is_rejected_rather_than_silently_skipped(tmp_path):
    path = tmp_path / "input.jsonl"
    path.write_text('{"kind":"meta","d":{}}\n{"t":0.1,"kind":"teleport"}\n',
                    encoding="utf-8")
    with pytest.raises(ValueError, match="teleport"):
        input_model.read_recording(str(path))


class _FakeKeyCode:
    def __init__(self, vk=None, char=None):
        self.vk = vk
        self.char = char

    @classmethod
    def from_vk(cls, vk):
        return cls(vk=vk)

    @classmethod
    def from_char(cls, char):
        return cls(char=char)

    def __eq__(self, other):
        return (self.vk, self.char) == (other.vk, other.char)

    def __hash__(self):
        return hash((self.vk, self.char))


class _FakeKeyMember:
    """Stands in for a pynput ``Key`` enum member, which wraps a KeyCode."""

    def __init__(self, name, vk):
        self.name = name
        self.value = _FakeKeyCode(vk=vk)


class _FakeKeyEnum:
    esc = _FakeKeyMember("esc", 27)
    shift = _FakeKeyMember("shift", 16)


def test_keys_are_identified_by_virtual_key_code_not_by_character():
    # shift+a reports char 'A'; the physical key is still vk 65
    assert input_model.describe_key(_FakeKeyCode(vk=65, char="A")) == ("a", 65)
    # ctrl+a reports an unprintable char -- the label has to come from the vk
    assert input_model.describe_key(_FakeKeyCode(vk=65, char="\x01")) == ("a", 65)
    assert input_model.describe_key(_FakeKeyEnum.esc) == ("esc", 27)


def test_resolve_key_prefers_the_vk_and_falls_back_to_the_name():
    keyboard = types.SimpleNamespace(KeyCode=_FakeKeyCode, Key=_FakeKeyEnum)
    assert input_model.resolve_key("a", 65, keyboard).vk == 65
    assert input_model.resolve_key("esc", None, keyboard) is _FakeKeyEnum.esc
    assert input_model.resolve_key("q", None, keyboard).char == "q"
    with pytest.raises(ValueError):
        input_model.resolve_key("f19", None, keyboard)


def test_key_matches_accepts_names_and_vk_specs():
    assert input_model.key_matches("scroll_lock", "scroll_lock", 145)
    assert input_model.key_matches("vk145", "scroll_lock", 145)
    assert not input_model.key_matches("pause", "scroll_lock", 145)
    assert not input_model.key_matches("", "scroll_lock", 145)


# ── timeline draft ───────────────────────────────────────────────────────────
def test_timeline_actions_pair_key_presses_and_ignore_mouse_movement():
    events = [
        {"t": 0.05, "kind": "move", "x": 1, "y": 1},
        {"t": 0.10, "kind": "key", "action": "down", "name": "w", "vk": 87},
        {"t": 0.15, "kind": "key", "action": "down", "name": "w", "vk": 87},  # auto-repeat
        {"t": 1.60, "kind": "key", "action": "up", "name": "w", "vk": 87},
        {"t": 2.00, "kind": "click", "action": "down", "button": "left", "x": 5, "y": 6},
        {"t": 2.05, "kind": "click", "action": "up", "button": "left", "x": 5, "y": 6},
        {"t": 3.00, "kind": "marker", "name": "stopped"},
    ]
    actions = timeline_draft.build_actions(events)
    assert [a["type"] for a in actions] == ["key", "click", "marker"]
    assert actions[0]["hold"] == pytest.approx(1.5)
    assert "1.50 s" in timeline_draft.describe(actions[0])
    assert timeline_draft.describe(actions[1]) == "left click @ (5, 6)"


def test_a_key_left_held_at_the_end_is_called_out():
    events = [{"t": 0.1, "kind": "key", "action": "down", "name": "w", "vk": 87}]
    action = timeline_draft.build_actions(events)[0]
    assert action["hold"] is None
    assert "never released" in timeline_draft.describe(action)


def test_timeline_draft_renders_markers_and_idle_gaps():
    events = [
        {"t": 0.1, "kind": "click", "action": "down", "button": "left", "x": 1, "y": 2},
        {"t": 5.0, "kind": "marker", "name": "on_track"},
    ]
    text = timeline_draft.render({"screen_size": [1920, 1080]}, events, scenario="demo")
    assert "# demo — timeline (draft)" in text
    assert "**MARKER `on_track`**" in text
    assert "idle 4.90 s" in text


# ── trace analysis ───────────────────────────────────────────────────────────
def test_resolve_path_walks_dicts_lists_and_field_selectors():
    data = {"cars": [{"PLID": 1, "speed_kmh": 50.0}, {"PLID": 7, "speed_kmh": 12.5}]}
    assert analyze_trace.resolve_path(data, "cars.0.speed_kmh") == 50.0
    assert analyze_trace.resolve_path(data, "cars[PLID=7].speed_kmh") == 12.5
    assert analyze_trace.resolve_path(data, "cars[PLID=99].speed_kmh") is None
    assert analyze_trace.resolve_path(data, "nope.deeper") is None


def _records():
    return [
        {"t": 0.0, "src": "tracer", "ev": "meta",
         "d": {"outgauge_interval_ms": 50, "mci_interval_ms": 100}},
        {"t": 1.0, "src": "insim", "ev": "STA",
         "d": {"on_track": True, "Track": "BL1", "cam": "DRIVER", "flags_flags": ["GAME"]}},
        {"t": 1.1, "src": "outgauge", "ev": "OutGauge", "d": {"speed_kmh": 10.0}},
        {"t": 1.2, "src": "marker", "ev": "scenario_start", "d": {}},
        {"t": 1.3, "src": "outgauge", "ev": "OutGauge", "d": {"speed_kmh": 30.0}},
        {"t": 5.0, "src": "outgauge", "ev": "OutGauge", "d": {"speed_kmh": 0.0}},
        {"t": 5.1, "src": "marker", "ev": "scenario_end", "d": {}},
        {"t": 5.2, "src": "insim", "ev": "STA",
         "d": {"on_track": False, "Track": "BL1", "cam": "FOLLOW",
               "flags_flags": ["FRONT_END"]}},
    ]


def test_extract_signal_and_marker_window():
    records = _records()
    series = analyze_trace.extract_signal(records, "OutGauge.speed_kmh")
    assert [value for _, value in series] == [10.0, 30.0, 0.0]
    inside = analyze_trace.window(records, "scenario_start", "scenario_end")
    assert [r["t"] for r in inside][0] == 1.2
    assert [r["t"] for r in inside][-1] == 5.1


def test_on_track_phases_and_stall_detection():
    records = _records()
    phases = analyze_trace.on_track_phases(records)
    assert len(phases) == 1
    assert phases[0]["start"] == 1.0 and phases[0]["end"] == 5.2
    # 1.3 -> 5.0 is far more than 4x the 50 ms OutGauge interval
    gaps = analyze_trace.stream_gaps(records, "OutGauge", 0.05)
    assert len(gaps) == 1
    assert gaps[0]["gap_s"] == pytest.approx(3.7)


def test_summary_warns_about_a_truncated_trace_and_an_outgauge_stall(tmp_path):
    path = tmp_path / "trace.jsonl"
    path.write_text("\n".join(json.dumps(r) for r in _records()) + "\n", encoding="utf-8")
    summary = analyze_trace.summarise(str(path))
    assert summary["duration_s"] == 5.2
    assert summary["speed_kmh"]["max"] == 30.0
    assert any("no 'end' record" in w for w in summary["warnings"])
    assert any("OutGauge stalled" in w for w in summary["warnings"])
    assert "trace" in analyze_trace.format_summary(summary)


def test_summary_of_an_empty_trace_is_an_error_not_a_crash(tmp_path):
    path = tmp_path / "trace.jsonl"
    path.write_text("", encoding="utf-8")
    assert analyze_trace.summarise(str(path))["error"] == "trace is empty"


# ── control channel ──────────────────────────────────────────────────────────
def test_control_channel_round_trip_and_error_paths():
    seen = []
    server = ControlServer(0, {
        "ping": lambda message: {"pong": 1},
        "marker": lambda message: seen.append(message["name"]) or {},
        "boom": lambda message: (_ for _ in ()).throw(RuntimeError("nope")),
    })
    client = ControlClient(server.port)
    try:
        assert client.ping() is True
        assert client.marker("x") is True
        assert seen == ["x"]
        assert client.request("unknown")["ok"] is False
        assert "RuntimeError" in client.request("boom")["error"]
    finally:
        client.close()
        server.stop()


def test_a_marker_with_no_tracer_listening_fails_quietly():
    client = ControlClient(_free_udp_port(), timeout=0.2)
    try:
        assert client.ping() is False
        assert client.marker("x") is False
    finally:
        client.close()


# ── replay ───────────────────────────────────────────────────────────────────
class _FakeController:
    def __init__(self):
        self.pressed = []
        self.released = []
        self.position = (0, 0)
        self.scrolls = []

    def press(self, key):
        self.pressed.append(key)

    def release(self, key):
        self.released.append(key)

    def scroll(self, dx, dy):
        self.scrolls.append((dx, dy))


class _FakeListener:
    def __init__(self, *args, **kwargs):
        self.on_press = kwargs.get("on_press")

    def start(self):
        pass

    def stop(self):
        pass


class _FakeButton:
    left = "left"
    right = "right"


@pytest.fixture
def fake_pynput(monkeypatch):
    """Install a recording stand-in for pynput so no real input is ever sent."""
    keyboard_controller = _FakeController()
    mouse_controller = _FakeController()
    keyboard = types.SimpleNamespace(
        Controller=lambda: keyboard_controller,
        Listener=_FakeListener,
        KeyCode=_FakeKeyCode,
        Key=_FakeKeyEnum,
    )
    mouse = types.SimpleNamespace(
        Controller=lambda: mouse_controller,
        Listener=_FakeListener,
        Button=_FakeButton,
    )
    module = types.ModuleType("pynput")
    module.keyboard = keyboard
    module.mouse = mouse
    monkeypatch.setitem(sys.modules, "pynput", module)
    monkeypatch.setitem(sys.modules, "pynput.keyboard", keyboard)
    monkeypatch.setitem(sys.modules, "pynput.mouse", mouse)
    return types.SimpleNamespace(keyboard=keyboard_controller, mouse=mouse_controller)


def _player(events, **kwargs):
    from simulation_tests import player as player_mod
    return player_mod.Player({"screen_size": None}, events, **kwargs)


def test_replay_applies_events_in_order_and_respects_the_schedule(fake_pynput):
    events = [
        {"t": 0.00, "kind": "move", "x": 100, "y": 200},
        {"t": 0.02, "kind": "key", "action": "down", "name": "w", "vk": 87},
        {"t": 0.04, "kind": "click", "action": "down", "button": "left", "x": 5, "y": 6},
        {"t": 0.05, "kind": "click", "action": "up", "button": "left", "x": 5, "y": 6},
        {"t": 0.06, "kind": "key", "action": "up", "name": "w", "vk": 87},
        {"t": 0.07, "kind": "scroll", "dx": 0, "dy": -1, "x": 5, "y": 6},
    ]
    started = time.perf_counter()
    result = _player(events, require_focus=False).play()
    elapsed = time.perf_counter() - started

    assert result["aborted"] is False
    assert result["events_applied"] == 6
    assert [k.vk for k in fake_pynput.keyboard.pressed] == [87]
    assert [k.vk for k in fake_pynput.keyboard.released] == [87]
    assert fake_pynput.mouse.pressed == ["left"]
    assert fake_pynput.mouse.scrolls == [(0, -1)]
    # the schedule is absolute: at least the recorded span, and not wildly more
    assert 0.07 <= elapsed < 1.0


def test_an_aborted_replay_releases_every_key_and_button(fake_pynput):
    events = [
        {"t": 0.00, "kind": "key", "action": "down", "name": "w", "vk": 87},
        {"t": 0.01, "kind": "click", "action": "down", "button": "left", "x": 1, "y": 2},
        {"t": 5.00, "kind": "key", "action": "up", "name": "w", "vk": 87},
    ]
    play = _player(events, require_focus=False)
    import threading
    threading.Timer(0.15, play.abort, ["test abort"]).start()
    result = play.play()

    assert result["aborted"] is True
    assert result["abort_reason"] == "test abort"
    # both were still down when the abort hit; neither may stay pressed
    assert [k.vk for k in fake_pynput.keyboard.released] == [87]
    assert fake_pynput.mouse.released == ["left"]


def test_markers_are_pushed_to_the_tracer_during_a_replay(fake_pynput):
    seen = []
    server = ControlServer(0, {"marker": lambda m: seen.append(m["name"]) or {}})
    client = ControlClient(server.port)
    try:
        events = [{"t": 0.0, "kind": "marker", "name": "brake_now"}]
        result = _player(events, require_focus=False, control=client).play()
        assert result["markers_sent"] == 1
        assert seen == ["brake_now"]
    finally:
        client.close()
        server.stop()


def test_speed_must_be_positive(fake_pynput):
    with pytest.raises(ValueError):
        _player([], speed=0)


def test_preflight_refuses_to_replay_where_it_cannot_drive_lfs(fake_pynput, monkeypatch):
    from simulation_tests import win_focus
    monkeypatch.setattr(win_focus, 'screen_size', lambda: None)
    problems = _player([], require_focus=False).preflight()
    assert any("not running on Windows" in problem for problem in problems)


# ── taking the foreground instead of demanding it ────────────────────────────
class _FakeFocus:
    """Stands in for ``win_focus``: scripts the foreground, records the raises.

    ``drop_after`` makes the Nth ``is_foreground`` call report that LFS has
    lost the foreground, which is what a menu/track switch looks like.
    """

    IS_WINDOWS = True

    def __init__(self, *, in_front=True, drop_after=None, can_raise=True):
        self.foreground = in_front
        self.drop_after = drop_after
        self.can_raise = can_raise
        self.checks = 0
        self.raises = 0

    def is_foreground(self, _match):
        self.checks += 1
        if self.drop_after is not None and self.checks == self.drop_after:
            self.foreground = False
        return self.foreground

    def raise_window(self, _match, settle_s=0.35):
        self.raises += 1
        if self.can_raise:
            self.foreground = True
            return True
        return False

    # the rest of what pre-flight touches
    def screen_size(self):
        return (1920, 1080)

    def lfs_window(self, _match):
        return {"hwnd": 1, "title": "Live for Speed", "rect": (0, 0, 1920, 1080)}

    def any_key_physically_down(self):
        return False


def _with_focus(monkeypatch, fake):
    from simulation_tests import player as player_mod
    monkeypatch.setattr(player_mod, "win_focus", fake)


def test_preflight_does_not_refuse_a_run_just_because_lfs_is_behind(monkeypatch, fake_pynput):
    """An unattended agent has nobody to alt-tab for it -- this must not block."""
    _with_focus(monkeypatch, _FakeFocus(in_front=False))
    player = _player([], require_focus=True)
    player.meta = {"screen_size": (1920, 1080),
                   "lfs_window": {"rect": (0, 0, 1920, 1080)}}

    problems = player.preflight()

    assert not any("foreground" in problem for problem in problems)
    assert any("raise it itself" in warning for warning in player.warnings)


def test_the_replay_takes_the_foreground_before_the_first_event(monkeypatch, fake_pynput):
    fake = _FakeFocus(in_front=False)
    _with_focus(monkeypatch, fake)
    events = [{"t": 0.0, "kind": "key", "action": "down", "name": "w", "vk": 87},
              {"t": 0.01, "kind": "key", "action": "up", "name": "w", "vk": 87}]

    result = _player(events, require_focus=True).play()

    assert fake.raises >= 1
    assert result["aborted"] is False
    assert result["events_applied"] == 2


def test_a_focus_blip_is_taken_back_instead_of_killing_the_run(monkeypatch, fake_pynput):
    """LFS drops the foreground for a moment switching menu -> track."""
    fake = _FakeFocus(drop_after=1, can_raise=True)
    _with_focus(monkeypatch, fake)
    events = [{"t": 0.00, "kind": "key", "action": "down", "name": "w", "vk": 87},
              {"t": 0.05, "kind": "key", "action": "up", "name": "w", "vk": 87}]

    result = _player(events, require_focus=True, on_focus_loss="abort").play()

    assert result["aborted"] is False
    assert result["events_applied"] == 2
    assert result["refocused"] >= 1


def test_a_foreground_we_cannot_take_back_still_aborts(monkeypatch, fake_pynput):
    """The guard is not gone: input that cannot reach LFS must not be sent."""
    fake = _FakeFocus(drop_after=2, can_raise=False)
    _with_focus(monkeypatch, fake)
    events = [{"t": 0.00, "kind": "key", "action": "down", "name": "w", "vk": 87},
              {"t": 0.50, "kind": "key", "action": "up", "name": "w", "vk": 87}]

    result = _player(events, require_focus=True, on_focus_loss="abort").play()

    assert result["aborted"] is True
    assert "lost focus" in result["abort_reason"]
    # and nothing may stay pressed
    assert [k.vk for k in fake_pynput.keyboard.released] == [87]


# ── scenario files ───────────────────────────────────────────────────────────
def test_a_scenario_is_enabled_unless_it_says_otherwise(tmp_path):
    scenario_mod.save(str(tmp_path), {"name": "demo"})
    data = scenario_mod.load(str(tmp_path))
    assert data["disabled"] is False
    assert scenario_mod.disabled_reason(data) == ""


def test_a_disabled_scenario_reports_why(tmp_path):
    scenario_mod.save(str(tmp_path), {"name": "demo", "disabled": True,
                                      "disabled_reason": "recording drifted"})
    assert scenario_mod.disabled_reason(scenario_mod.load(str(tmp_path))) == "recording drifted"


def test_a_scenario_disabled_without_a_reason_still_refuses(tmp_path):
    """The flag alone must be enough -- a missing reason may not read as 'fine'."""
    scenario_mod.save(str(tmp_path), {"name": "demo", "disabled": True})
    assert scenario_mod.disabled_reason(scenario_mod.load(str(tmp_path)))


def test_the_runner_refuses_a_disabled_scenario_before_touching_lfs(tmp_path, capsys):
    from simulation_tests import run_scenario as run_mod
    scenario_dir = tmp_path / "77_broken"
    scenario_mod.save(str(scenario_dir), {"name": "77_broken", "disabled": True,
                                          "disabled_reason": "recording drifted"})
    (scenario_dir / "input.jsonl").write_text("", encoding="utf-8")

    code = run_mod.main([str(scenario_dir)])

    assert code == 8
    err = capsys.readouterr().err
    assert "recording drifted" in err
    # nothing was started: no run directory, no tracer
    assert not list((tmp_path).glob("**/trace.jsonl"))


def test_scenario_defaults_are_filled_in_for_a_minimal_file(tmp_path):
    scenario_mod.save(str(tmp_path), {"name": "demo"})
    data = scenario_mod.load(str(tmp_path))
    assert data["tracer"]["packets"]
    assert data["run"]["require_main_menu"] is True
    argv = scenario_mod.tracer_argv(data, "out.jsonl", 30111, run_id="r1")
    assert "--out" in argv and "out.jsonl" in argv
    assert "--control-port" in argv and "30111" in argv


def test_scenario_file_overrides_win_over_defaults(tmp_path):
    scenario_mod.save(str(tmp_path), {"name": "demo",
                                      "tracer": {"packets": ["STA"], "outsim_interval_ms": 100},
                                      "run": {"tail_s": 9.0}})
    data = scenario_mod.load(str(tmp_path))
    assert data["tracer"]["packets"] == ["STA"]
    assert data["tracer"]["mci_interval_ms"] == 100      # default kept
    assert data["run"]["tail_s"] == 9.0
    assert data["run"]["require_main_menu"] is True      # default kept


def test_every_shipped_scenario_has_a_loadable_definition():
    names = paths.list_scenarios()
    assert names, "no scenarios are shipped"
    for name in names:
        data = scenario_mod.load(paths.scenario_dir(name))
        assert data["name"] == name
        assert data["description"]
        for packet in data["tracer"]["packets"]:
            assert packet.isupper()


def test_no_module_in_the_harness_imports_the_add_on():
    """The harness must keep working when the add-on is refactored."""
    forbidden = ("core.", "assistance.", "ui.", "lfs.", "vehicles.", "misc.", "audio.")
    for entry in sorted(os.listdir(paths.PACKAGE_DIR)):
        if not entry.endswith(".py"):
            continue
        text = open(os.path.join(paths.PACKAGE_DIR, entry), encoding="utf-8").read()
        for line in text.splitlines():
            stripped = line.strip()
            if not stripped.startswith(("import ", "from ")):
                continue
            for package in forbidden:
                assert not stripped.startswith((f"import {package}", f"from {package}")), \
                    f"{entry}: {stripped}"


# ── end to end against a fake LFS ────────────────────────────────────────────
def _free_udp_port() -> int:
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    sock.bind(("127.0.0.1", 0))
    port = sock.getsockname()[1]
    sock.close()
    return port


@pytest.mark.skipif(sys.version_info >= (3, 12),
                    reason="pyinsim needs asyncore, removed in 3.12")
def test_tracer_records_a_full_session_against_a_fake_lfs(tmp_path):
    from tests import fake_lfs

    trace_path = str(tmp_path / "trace.jsonl")
    control_port = _free_udp_port()
    udp_port = _free_udp_port()
    lfs = fake_lfs.FakeLFS()
    process = subprocess.Popen(
        [sys.executable, os.path.join(paths.PACKAGE_DIR, "insim_trace.py"),
         "--out", trace_path, "--scenario", "e2e", "--quiet",
         "--insim-port", str(lfs.port), "--udp-port", str(udp_port),
         "--control-port", str(control_port),
         "--packets", "STA,CIM,NPL,MCI,CON"],
        cwd=paths.REPO_ROOT, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    client = ControlClient(control_port)
    try:
        deadline = time.monotonic() + 20
        while time.monotonic() < deadline and not client.ping():
            time.sleep(0.1)
        assert client.ping(), "tracer did not come up"
        # LFS only streams OutGauge once the tracer asks via SMALL_SSG
        assert lfs.wait_for_ssg(5.0)

        lfs.send(fake_lfs.sta(1 | 16384, b"BL1", cam=3, num_p=2))
        lfs.send(fake_lfs.npl(1, 0, 0, b"Tester", b"XFG"))
        lfs.send(fake_lfs.npl(2, 0, 2, b"AI 1", b"XFG"))
        time.sleep(0.2)
        for i in range(4):
            lfs.send(fake_lfs.mci([fake_lfs.compcar(1, 10.0 + i, 20.0, 50.0, 0.0, 64 | 128)]))
            lfs.send_outgauge(fake_lfs.outgauge(i * 50, 13.8, 4200.0, 3, 0.5, 0.0, plid=1))
            time.sleep(0.05)
        client.marker("contact_expected")
        lfs.send(fake_lfs.con(1, 2, 55))
        # MSO is deliberately absent from --packets above. Every scenario must
        # still record normal chat and LFS errors, including non-ASCII text.
        lfs.send(fake_lfs.mso(b'User: invalid parameter?', user_type=1))
        lfs.send(fake_lfs.mso(b'^1Ung\xfcltiger Parameter'))
        lfs.send(fake_lfs.sta(256 | 16384, b"BL1", cam=0, num_p=0))
        time.sleep(0.4)

        state = client.state()
        assert state["on_track"] is False
        assert "FRONT_END" in state["flags"]

        client.stop_tracer()
        process.wait(timeout=20)
    finally:
        if process.poll() is None:
            process.kill()
        client.close()
        lfs.close()

    summary = analyze_trace.summarise(trace_path)
    assert summary["meta"]["scenario"] == "e2e"
    assert summary["counts"]["MCI"] == 4
    assert summary["counts"]["OutGauge"] == 4
    assert [m["name"] for m in summary["markers"]] == ["contact_expected"]
    assert {p["plid"] for p in summary["players"]} == {1, 2}
    assert [p["ptype"] for p in summary["players"] if p["plid"] == 2] == [["AI"]]
    assert summary["contacts"][0]["plid_a"] == 1
    assert summary["end"] is not None, "the trace must end with an 'end' record"
    assert summary["end"]["dropped"] == 0
    assert summary['counts']['MSO'] == 2
    assert summary['chat_check']['status'] == 'failed'
    assert summary['chat_check']['capture_complete']
    assert [m['text'] for m in summary['chat_check']['diagnostics']] == ['Ungültiger Parameter']
    assert len(summary['chat_check']['messages']) == 2

    speeds = analyze_trace.extract_signal(load_trace(trace_path), "OutGauge.speed_kmh")
    assert speeds and speeds[0][1] == pytest.approx(49.68, abs=0.01)


# ── the corrected IS_CON decoder (simulation_tests/insim_patch.py) ───────────
#
# The tracer compatibility imports use the shared pyinsim contact decoder.
def _car_contact_bytes(plid=1, steer=5, thr_brk=0xF0, clu_han=0x0A, gear_sp=0x30,
                       speed=200, direction=64, heading=250, accel_f=-9, accel_r=3,
                       x=160, y=320):
    return struct.pack("<3Bb6B2b2h", plid, 0, 0, steer, thr_brk, clu_han, gear_sp,
                       speed, direction, heading, accel_f, accel_r, x, y)


def _con_bytes(layout, sp_close=123):
    if layout == 44:
        header = struct.pack("<4B2HI", 11, 50, 0, 0, sp_close, 0, 1234000)
    else:
        header = struct.pack("<4B2H", 10, 50, 0, 0, sp_close, 1234)
    return header + _car_contact_bytes() + _car_contact_bytes(plid=2, speed=30)


@pytest.mark.parametrize("layout", [40, 44])
def test_both_is_con_layouts_decode_and_say_which_one_arrived(layout):
    packet = insim_patch.IS_CON().unpack(_con_bytes(layout))
    assert packet.con_layout == layout
    assert packet.A.PLID == 1 and packet.B.PLID == 2


def test_car_contact_pedals_and_angles_are_unsigned_accelerations_signed():
    packet = insim_patch.IS_CON().unpack(_con_bytes(44))
    data = packet_dump.packet_to_dict("CON", packet)
    a = data["A"]
    # Speed 200 m/s and heading 250/256 would both come back negative if these
    # bytes were read as signed, which was the legacy decoder bug.
    assert a["Speed"] == 200 and a["speed_kmh"] == pytest.approx(720.0)
    assert a["heading_deg"] == pytest.approx(351.56, abs=0.01)
    # ...while AccelF must stay negative: forward is positive, so this is braking.
    assert a["AccelF"] == -9 and a["accel_f_ms2"] == -9
    assert a["accel_f_g"] == pytest.approx(-9 / 9.80665)
    assert a["brake"] == pytest.approx(0.0) and a["throttle"] == pytest.approx(1.0)
    assert a["clutch"] == 0 and a["handbrake"] == pytest.approx(10 / 15)
    assert a["gear"] == 3


def test_contact_independent_pedal_nibbles_and_reverse():
    packet = insim_patch.CarContact(_car_contact_bytes(
        thr_brk=0x3C, clu_han=0xA5, gear_sp=0xF7))
    a = packet_dump.packet_to_dict("CON", types.SimpleNamespace(A=packet))["A"]
    assert a["throttle"] == pytest.approx(3 / 15)
    assert a["brake"] == pytest.approx(12 / 15)
    assert a["clutch"] == pytest.approx(10 / 15)
    assert a["handbrake"] == pytest.approx(5 / 15)
    assert a["gear"] == 15


def test_an_is_con_of_an_unknown_size_is_a_clear_error_not_a_struct_error():
    with pytest.raises(ValueError, match="unexpected size"):
        insim_patch.IS_CON().unpack(_con_bytes(40)[:36])


def test_compatibility_entry_point_uses_shared_decoder_without_mutating_map():
    import pyinsim

    original = dict(pyinsim.core._PACKET_MAP)
    try:
        insim_patch.apply(pyinsim)
        assert insim_patch.IS_CON is pyinsim.IS_CON
        assert pyinsim.core._PACKET_MAP == original
        assert {k: v for k, v in pyinsim.core._PACKET_MAP.items() if k != pyinsim.ISP_CON} \
            == {k: v for k, v in original.items() if k != pyinsim.ISP_CON}
    finally:
        pyinsim.core._PACKET_MAP.clear()
        pyinsim.core._PACKET_MAP.update(original)


# ── a recording that would be dangerous to replay ────────────────────────────
def test_a_recording_that_leaves_a_key_down_is_reported():
    events = [
        {"t": 0.1, "kind": "key", "action": "down", "name": "w", "vk": 87},
        {"t": 0.2, "kind": "key", "action": "up", "name": "w", "vk": 87},
        {"t": 0.3, "kind": "key", "action": "down", "name": "s", "vk": 83},
    ]
    problems = input_model.check_recording(events)
    assert len(problems) == 1 and "still held" in problems[0]


def test_a_release_without_a_press_is_reported():
    events = [{"t": 0.5, "kind": "click", "action": "up", "button": "left", "x": 1, "y": 2}]
    problems = input_model.check_recording(events)
    assert len(problems) == 1 and "without ever being pressed" in problems[0]


def test_a_balanced_recording_has_no_problems():
    events = [
        {"t": 0.1, "kind": "key", "action": "down", "name": "w", "vk": 87},
        {"t": 0.2, "kind": "key", "action": "down", "name": "w", "vk": 87},  # auto-repeat
        {"t": 0.4, "kind": "key", "action": "up", "name": "w", "vk": 87},
        {"t": 0.5, "kind": "click", "action": "down", "button": "left", "x": 1, "y": 2},
        {"t": 0.6, "kind": "click", "action": "up", "button": "left", "x": 1, "y": 2},
        {"t": 0.7, "kind": "move", "x": 3, "y": 4},
    ]
    assert input_model.check_recording(events) == []


def test_preflight_refuses_a_recording_that_would_leave_the_throttle_down(fake_pynput):
    player = _player([{"t": 0.1, "kind": "key", "action": "down", "name": "w", "vk": 87}])
    assert any("still held" in problem for problem in player.preflight())


# ── the recorder's own filters ───────────────────────────────────────────────
def test_key_auto_repeat_is_collapsed_into_one_press():
    from simulation_tests import recorder as recorder_mod

    rec = recorder_mod.Recorder()
    key = _FakeKeyCode(char="w", vk=87)
    for _ in range(5):
        rec._on_press(key)
    rec._on_release(key)
    rec._on_press(key)
    assert [(e["action"], e["vk"]) for e in rec.events] == [
        ("down", 87), ("up", 87), ("down", 87)]


def test_the_marker_and_stop_keys_never_reach_the_stream():
    from simulation_tests import recorder as recorder_mod

    rec = recorder_mod.Recorder(marker_names=["entered_garage"])
    rec._on_press(_FakeKeyMember("scroll_lock", 0x91))
    rec._on_release(_FakeKeyMember("scroll_lock", 0x91))
    rec._on_press(_FakeKeyMember("pause", 0x13))
    assert [e["kind"] for e in rec.events] == ["marker"]
    assert rec.events[0]["name"] == "entered_garage"


# ── replay timing accounting ─────────────────────────────────────────────────
def test_lateness_is_measured_so_a_shifted_trace_is_not_read_as_a_regression(fake_pynput):
    ticks = iter([0.0, 0.5])
    player = _player([], clock=lambda: next(ticks))
    player._t0 = next(ticks)
    player._record_lateness(0.0)
    assert player._late_max == pytest.approx(0.5)
    assert player._late_over_budget == 1


def test_strict_timing_aborts_instead_of_clicking_into_the_wrong_menu_page(fake_pynput):
    from simulation_tests import player as player_mod

    ticks = iter([0.0, 1.0])
    player = _player([], strict_timing=True, clock=lambda: next(ticks))
    player._t0 = next(ticks)
    with pytest.raises(player_mod.ReplayAbort):
        player._record_lateness(0.0)
    assert "late" in player._abort_reason


# ── the optional UDP fan-out ─────────────────────────────────────────────────
def test_the_relay_forwards_identical_bytes_to_both_consumers():
    import threading

    from simulation_tests import udp_relay

    def _bind():
        sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        sock.bind(("127.0.0.1", 0))
        sock.settimeout(0.5)
        return sock

    lfs_side, addon_side, tracer_side = _bind(), _bind(), _bind()
    source_port = lfs_side.getsockname()[1]
    lfs_side.close()  # the relay binds this one itself

    counts = {}
    thread = threading.Thread(
        target=lambda: counts.update(udp_relay.relay(
            tracer_side.getsockname()[1], 3.0,
            routes=((source_port, addon_side.getsockname()[1]),), quiet=True)),
        daemon=True)
    thread.start()
    try:
        payload = bytes(range(96))
        sender = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        deadline = time.monotonic() + 5.0
        while time.monotonic() < deadline:
            sender.sendto(payload, ("127.0.0.1", source_port))
            try:
                assert addon_side.recv(4096) == payload
                assert tracer_side.recv(4096) == payload
                break
            except socket.timeout:  # pragma: no cover - the relay is not up yet
                continue
        else:  # pragma: no cover
            pytest.fail("the relay never forwarded a datagram")
        sender.close()
    finally:
        thread.join(timeout=8)
        addon_side.close()
        tracer_side.close()
    assert counts.get(source_port, 0) >= 1
