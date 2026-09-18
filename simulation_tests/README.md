# Simulation tests — driving LFS from a recording

An in-game test harness for the PACT Driving Assistant. It replays recorded
mouse/keyboard input into Live for Speed and records what the game reports back
over InSim and OutGauge, so a change can be checked against real game behaviour
without a human driving.

**It is independent of the add-on.** Nothing here imports `core`, `assistance`,
`ui`, `lfs`, `vehicles` or `misc`; the only shared code is `pyinsim`, the LFS
protocol library. The harness runs *next to* the add-on, in its own process,
with its own InSim connection and its own UDP ports — so the normal workflow is:
start the add-on, then start a scenario, then read the trace.

---

## 1. What a run produces

```
simulation_tests/runs/04_drive_and_stop_20260913-141207/
    trace.jsonl     everything LFS reported, one JSON record per line, time-ordered
    run.json        what was run, how it went, and the summary
    tracer.log      the tracer's stderr
    timeline.md     a copy of the scenario's expected timeline
```

## 2. Requirements

| | |
|---|---|
| OS | **Windows** — replaying input is global OS input; LFS is Windows-only |
| Python | **3.11 or older** (pyinsim uses `asyncore`, removed in 3.12) |
| Packages | `pip install -r simulation_tests/requirements.txt` (just `pynput`) |
| LFS | running, InSim enabled on port 29999 (`/insim 29999`, normally from `autoexec.lfs`) |

**OutGauge does not need `cfg.txt`.** The tracer asks LFS for its own OutGauge
stream with `SMALL_SSG`, on its own UDP port. That is why it can run while the
add-on holds OutGauge on 30000.

Ports used (all loopback, all configurable in `config.py`):

| Port | Used for |
|---|---|
| 29999 | InSim — shared with LFS, a second connection is fine (LFS allows 8) |
| 30011 | the tracer's own MCI/OutGauge/OutSim UDP stream |
| 30111 | control channel between the runner/replay and the tracer |

## 3. Running a scenario

```
python simulation_tests/run_scenario.py --list          # what is there, and what is recorded
python simulation_tests/run_scenario.py 04_drive_and_stop
```

What it does:

1. starts the tracer in its own process and waits until it answers;
2. waits until LFS reports the **main menu** — every scenario starts and ends
   there, so a run always begins from a known state;
3. counts down 5 s (alt-tab into LFS), marks `scenario_start`, replays the
   recorded input, marks `scenario_end`;
4. keeps tracing for a couple more seconds so a late reaction is still caught;
5. stops the tracer, writes `run.json`, prints the summary.

Useful flags:

```
--speed 1.5           replay faster (be careful: LFS menus can drop fast clicks)
--tracer _temp/x.py   use a modified tracer (see §7)
--countdown 10        more time to alt-tab
--no-wait-menu        start regardless of where LFS is
--force               run even though pre-flight complained
--tail 5              keep tracing 5 s after the replay
--require OutGauge    exit 7 if that stream never arrived (repeatable)
--strict-timing       abort if an event is dispatched >0.25 s past its deadline
```

### A run that completed is not a run that passed

`run.json` always says `"functional_verdict": "not_evaluated"`. Nothing in this
harness can decide whether the *add-on* behaved — that is what the scenario's
`timeline.md` and a reading of the trace are for. Two traps it does guard:

- **Missing telemetry is not a zero reading.** A capture with no OutGauge looks
  exactly like "the brake never moved". `--require OutGauge --require MCI` on any
  driving scenario turns that into exit code 7.
- **A warning-only feature is not proven by the trace.** MCI and OutGauge show
  that the situation happened, not that the HUD or the beeper fired. That still
  needs a human, or a diagnostic output the add-on does not have yet.

Exit codes: `0` ok · `2` bad arguments · `3` pre-flight refused · `4` tracer did
not start · `5` LFS not at the main menu · `6` replay aborted · `7` required
telemetry missing.

### Safety

Replaying injects global keyboard and mouse input. Three guards run during the
replay, all on by default:

- **Abort key: `Pause`.** Stops the replay immediately, anywhere.
- **Focus guard.** The replay refuses to start unless LFS is the foreground
  window, and aborts if LFS loses focus mid-run (`--on-focus-loss pause` holds
  the schedule instead, `ignore` disables it).
- **Release-on-exit.** Every key and mouse button the replay presses is released
  on every exit path, including an abort or a crash. An aborted run never leaves
  the car accelerating.

Pre-flight refuses the run, before injecting anything, when:

- the **recording itself leaves a key or button down** — replaying it would hand
  the car back with the throttle held;
- the **screen size or the LFS window rect** differs from the recording: menu
  clicks are absolute screen coordinates, so a moved or resized LFS misses every
  one of them;
- **you are holding a key** as the countdown ends — the replay would fight you
  for the whole run, and release-on-exit will not let go of a key it never
  pressed;
- LFS is not running, not in the foreground, or pynput is unusable.

`--force` runs anyway. The replay also records how late each event was actually
dispatched (`lateness_max_s`, `lateness_over_budget` in `run.json`): a trace that
disagrees with its timeline by less than that is a scheduling artefact, not a
behaviour change.

## 4. Recording a new scenario

```
python simulation_tests/record_scenario.py 11_my_scenario --description "..."
```

1. A countdown gives you time to alt-tab into LFS. **Start at the main menu.**
2. Everything you do is recorded: the mouse position on a 50 ms grid, keys and
   clicks with their real timestamps. Two things are filtered out — **input the
   add-on injected itself** (its emergency-brake and light keypresses look
   identical to yours in the hook; recording them would bake one run's reaction
   into the next run's stimulus) and **key auto-repeat** (one press is recorded,
   the replay holds the key).
3. Press **Scroll Lock** whenever something noteworthy happens — that drops a
   named marker. If the scenario directory already has a `scenario.json` with a
   `markers` list, the markers are named from it, in order.
4. Press **Pause** to stop. **End at the main menu.**

Written into `scenarios/11_my_scenario/`:

| File | What it is |
|---|---|
| `input.jsonl` | the recorded input stream — this is the replay |
| `timeline.draft.md` | a generated table of every action with its timestamp |
| `scenario.json` | what to trace, and how to run it (created if missing) |

Then: fill in the "expected in the trace" column of the draft, rename it to
`timeline.md`, and run the scenario once to check it plays back.

`replay_scenario.py <name>` replays without tracing, for a quick check.

### What belongs in `scenario.json`

```json
{
  "name": "04_drive_and_stop",
  "description": "...",
  "preconditions": ["LFS is at the main menu"],
  "markers": ["on_track", "throttle_on", "brake_on", "stopped", "back_at_menu"],
  "tracer": {
    "script": "insim_trace.py",
    "packets": ["STA", "CIM", "NPL", "PLL", "MCI", "CON", "OBH", "MSO"],
    "mci_interval_ms": 100,
    "outgauge_interval_ms": 25,
    "outsim_interval_ms": 0
  },
  "run": { "tail_s": 3.0, "require_main_menu": true, "menu_timeout_s": 20.0 }
}
```

`python simulation_tests/insim_trace.py --list-packets` prints every packet name
that can go in `packets`. Packets that need a handshake flag (`MCI`, `CON`,
`OBH`, `HLV`, `AXM`, `NLP`) get it automatically.

## 5. The trace format

One JSON object per line, in capture order:

```json
{"t":12.345,"src":"outgauge","ev":"OutGauge","d":{"Speed":13.8,"speed_kmh":49.68, ...}}
{"t":12.400,"src":"insim","ev":"MCI","d":{"cars":[{"PLID":1,"x_m":102.3,"speed_kmh":49.9, ...}]}}
{"t":12.510,"src":"marker","ev":"brake_on","d":{"source":"replay","replay_t":11.2}}
```

| Field | Meaning |
|---|---|
| `t` | seconds since the trace started, monotonic |
| `src` | `insim` / `outgauge` / `outsim` / `marker` / `tracer` |
| `ev` | the packet name, `OutGauge`, a marker name, or `meta` / `end` / `warning` |
| `d` | the decoded fields |

Rate differences are handled by construction: OutGauge, MCI and event packets
each land in the file when they arrive, with their own timestamp. Nothing is
resampled or interleaved into a common grid — reading a trace means walking it
in order, not zipping columns.

**Every packet carries SI values next to the raw LFS integers**: `x_m`, `y_m`,
`speed_kmh`, `heading_deg` (0 = north, anticlockwise — LFS's own frame),
`heading_math_deg` (0 = +X, the frame this repo's trig uses), and readable flag
lists (`flags_flags`, `showlights_flags`, `ptype_flags`). Never convert a raw
field by hand; see `reference/conventions.md` §1–§3 for why that is the project's
most common bug class.

The first record is always `meta`, the last is always `end`. **A trace with no
`end` record was truncated** — the analyzer says so.

Markers put the replay and the trace on one timeline: the replay pushes each
marker into the tracer over the control channel, and the tracer stamps it with
its own clock. `run_scenario.py` adds `scenario_start` and `scenario_end` around
the replay.

## 6. Reading a trace

```
python simulation_tests/analyze_trace.py runs/04_drive_and_stop_20260913-141207/
```

```
--timeline                       every discrete event in order (streams hidden)
--timeline --all                 ... including MCI and OutGauge
--events CON,OBH                 full records of those packets, as JSON
--signal OutGauge.speed_kmh      a time series; repeatable
--signal 'MCI.cars[PLID=1].speed_kmh'
--csv out.csv                    write the signals to one merged CSV
--from-marker brake_on --to-marker stopped
--json                           the whole summary as JSON
```

The summary calls out the two things that make a good add-on look broken:

- **OutGauge stalls.** OutGauge stops dead outside an internal camera view and
  in the pits (`reference/conventions.md` §5.3), and every assistance system
  freezes with it. If the summary reports a stall, the capture is at fault, not
  the code.
- **dropped records**, i.e. the trace has holes.

## 7. Rules for an automated agent

**You may freely adapt the standalone InSim script.** If a change needs
measurements the base tracer does not produce — a different packet, a derived
signal, a different rate — then:

1. **copy** `simulation_tests/insim_trace.py` into `simulation_tests/_temp/`,
   e.g. `_temp/autohold_tracer.py`;
2. edit the copy;
3. run it with `run_scenario.py <scenario> --tracer _temp/autohold_tracer.py`.

**Do not edit `insim_trace.py` itself, and do not edit anything under
`scenarios/`.** A scenario — its `input.jsonl`, its `timeline.md`, its
`scenario.json` — is a fixed reference: the whole point is that two runs weeks
apart are comparable. Only the human who records a scenario changes it.

`_temp/` is git-ignored and disposable. If something in a temporary tracer turns
out to be generally useful, move it into `packet_dump.py` or `config.py`
properly, with a test, and delete the copy.

Also:

- Most options are already on the command line (`--packets`, `--add-packets`,
  `--mci-interval`, `--outgauge-interval`, `--outsim-interval`). Try those before
  copying the script.
- Adding a derived field is a one-line entry in `packet_dump.DERIVERS`; adding a
  flag decode is one entry in `packet_dump.FLAG_FIELDS`.
- Recording and replaying needs Windows and a human at the keyboard to set up
  the game state. An agent on another machine can still **read traces** and
  **write tracers**.

### A worked example

The agent changes the auto-hold logic and wants to see what the brake actually
did:

```
# 1. start the add-on as usual
python main.py

# 2. run the scenario that drives off and stops, with a finer OutGauge rate
python simulation_tests/run_scenario.py 04_drive_and_stop

# 3. read the brake trace around the stop
python simulation_tests/analyze_trace.py runs/04_drive_and_stop_<stamp>/ \
    --signal OutGauge.speed_kmh --signal OutGauge.Brake \
    --from-marker brake_on --to-marker leaving --csv brake.csv
```

Compare `brake.csv` against the run from before the change. The markers make the
two runs line up even though they are not the same length.

## 8. Shipped scenarios

| Scenario | What it does |
|---|---|
| `01_menu_walkthrough` | the menus, dialogs and the options screens, never on track |
| `02_garage_settings` | every garage ("box") setup page, once each |
| `03_track_idle_60s` | on track, standing still for a minute — the quiet baseline |
| `04_drive_and_stop` | drive off, hold a speed, brake to a stop, wait |
| `05_fcw_rear_end` | close on an AI car from behind — forward collision warning |
| `06_blind_spot` | sit alongside an AI car, left and right — blind spot warning |
| `07_cross_traffic` | an AI car crossing a junction, from both sides |
| `08_lights_and_indicators` | indicators, hazards, beams, brake light |
| `09_park_distance_control` | creep up to layout objects, front and rear |
| `10_screen_and_dialog_sweep` | text entry, SHIFT+U, SHIFT+B, camera, TAB, pits |

**They ship without a recording.** `input.jsonl` has to be recorded once, on the
machine that runs the tests, because menu clicks are screen coordinates. Each
`timeline.md` carries the step-by-step recording guide.

## 9. Layout

```
simulation_tests/
    run_scenario.py      tracer + replay + summary (start here)
    record_scenario.py   record a new scenario
    replay_scenario.py   replay only, no tracing
    insim_trace.py       the standalone tracer -- copy it into _temp/ to modify
    analyze_trace.py     summary, timeline, signal extraction, CSV

    udp_relay.py         optional cfg.txt UDP fan-out -- see §11, rarely needed

    config.py            ports, rates, defaults
    paths.py             directory layout
    scenario.py          scenario.json
    trace_format.py      the trace record format and its writer
    packet_dump.py       packet -> dict, with SI conversions and flag names
    insim_patch.py       corrected IS_CON decoder, tracer-process only
    input_model.py       the recorded input format and key naming
    recorder.py          input capture
    player.py            input replay
    timeline_draft.py    input.jsonl -> draft timeline.md
    control_channel.py   loopback UDP between runner/replay and tracer
    win_focus.py         foreground window and screen geometry (ctypes)
    pynput_access.py     lazy pynput import, so every module loads off Windows

    scenarios/           the scenarios themselves -- do not edit
    _temp/               modified tracers (git-ignored)
    runs/                run output (git-ignored)
```

Tests: `python -m pytest tests/test_simulation_tests.py`. They run on any
platform without LFS — `pynput` is replaced by a recording double, and one test
drives the real tracer against a fake LFS (`tests/fake_lfs.py`).

## 10. Troubleshooting

| Symptom | Cause |
|---|---|
| `could not connect to LFS on 127.0.0.1:29999` | LFS not running, or InSim not enabled (`/insim 29999`) |
| tracer starts, trace has only `meta` | LFS is on the entry screen and the scenario traces nothing there — expected for menu scenarios |
| summary warns "OutGauge never arrived" | the camera is not an internal view, or the car is in the pits (`conventions.md` §5.3) |
| summary warns "OutGauge stalled" | same, mid-run — the capture is bad, re-run it |
| `LFS is not the foreground window` | alt-tab into LFS during the countdown, or raise `--countdown` |
| clicks land in the wrong place | the recording was made at another resolution; re-record |
| `no 'end' record` | the tracer was killed; the trace may be missing its tail |
| `required telemetry missing` (exit 7) | a `--require`d stream never arrived — the capture is inconclusive, not a finding |
| pre-flight: "still held when the recording ends" | the recording was stopped while a key was down; re-record it |
| pre-flight: "the LFS window moved or resized" | put LFS back where it was, or re-record |

## 11. If a driving trace has no OutGauge: `udp_relay.py`

**Normally you never need this.** The tracer asks LFS for its own OutGauge/OutSim
stream over its own InSim connection (`SMALL_SSG` / `SMALL_SSP`), which is a
per-connection setting, so it does not compete with the add-on's cfg.txt streams
on 30000/29998.

If that turns out not to hold on the installed LFS version — a driving trace with
no `OutGauge` records while the add-on's gauges are clearly live — `udp_relay.py`
is the fallback. It needs a one-time cfg.txt port change (with LFS closed) and a
relay process that forwards every datagram unchanged to *both* consumers:

```
python simulation_tests/udp_relay.py
```

The module docstring has the exact ports, the ordering (relay before LFS) and the
restore procedure. **The relay has to stay running while that cfg.txt is in
place**, including outside tests — otherwise the add-on gets no OutGauge and every
assistance system silently does nothing.

Never instead bind a second listener to 30000/29998 and hope both processes see
every packet. One of them will miss datagrams, unpredictably.

## 12. `insim_patch.py` — why the tracer decodes IS_CON itself

`pyinsim` is the add-on's protocol library and is not modified from here.
Its `IS_CON` expects the 40-byte layout and unpacks `CarContact` with the
signedness inverted, so on an LFS that sends the 44-byte layout the decode raises
*inside the asyncore loop* and the tracer loses its InSim connection mid-scenario.

`insim_patch.apply()` installs a corrected `IS_CON` into the **tracer process's
own** packet map — the tracer is a separate process, so this can never reach a
running add-on. It picks the layout by `Size` (both are decoded, and the trace
records which one arrived as `con_layout`) and reads the pedal nibbles, speed and
angle bytes as unsigned and the two accelerations as signed.

The add-on's own decoder is still uncorrected; it does not subscribe to CON today,
so nothing is broken, but anything that starts consuming CON must port this first
(`reference/known-issues.md`).
