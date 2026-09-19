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

**Measured on the current LFS installation (2026-09-19, LFS 0.8C28).** Two
things were established by measurement and supersede the original design claim
that the tracer's own `SMALL_SSG` stream makes it independent of `cfg.txt`:

* **`SMALL_SSG` does not deliver here.** With `cfg.txt` pointing OutGauge at
  30000, the tracer's own UDP port received no gauges. For a run *without* the
  add-on, pass `--udp-port 30000` and the tracer gets both MCI and OutGauge.
* **Running the tracer next to the add-on works, through a relay.** One process
  owns 30000 and forwards every datagram unchanged to the add-on (30010, via a
  patched `pyinsim.outgauge`) and to the tracer (30011, its default). Validated
  end to end: 2795 datagrams received, 2795 to each consumer, 0 send errors, and
  the tracer's own OutGauge count matched exactly — no duplication from `SSG`.
  `_temp/addon_mouse_parallel.py` is the reference implementation; §13.

Never start a second receiver on 30000 alongside the add-on without such a relay.

**And stop the relay when the run is over.** A `_temp` harness is a normal process and
outlives the scenario it was written for. One left running held 30000 for over an hour
on 2026-09-19; every add-on start after it came up blind, and every actuator was
silently refused (`reference/known-issues.md` #51). The add-on now says so instead of
looking healthy, but the port is still only released by killing the process:

```powershell
Get-NetUDPEndpoint -LocalPort 30000 | ForEach-Object { Get-Process -Id $_.OwningProcess }
```

| | |
|---|---|
| OS | **Windows** — replaying input is global OS input; LFS is Windows-only |
| Python | **3.11 or older** (pyinsim uses `asyncore`, removed in 3.12) |
| Packages | `pip install -r simulation_tests/requirements.txt` (just `pynput`) |
| LFS | running, InSim enabled on port 29999 (`/insim 29999`, normally from `autoexec.lfs`) |

**MCI needs LFS's InSim UDP socket, and it is not always there.** LFS opens
both TCP *and* UDP on the InSim port. If only the TCP listener exists, STA/CIM/
NPL keep arriving and **every UDP reply silently disappears** — no MCI on any
port, however it is configured. `Get-NetUDPEndpoint` (PowerShell) on 29999 is
the one-line check; a run that shows STA but `MCI=0` is this, not a code fault.

Ports used (all loopback, all configurable in `config.py`):

| Port | Used for |
|---|---|
| 29999 | InSim — shared with LFS, TCP *and* UDP; a second connection is fine (LFS allows 8) |
| 30000 | `cfg.txt` OutGauge. The tracer binds it directly when the add-on is not running; the relay owns it when it is |
| 30010 | the add-on's OutGauge, when it is fed by the relay instead of by `cfg.txt` |
| 30011 | the tracer's MCI/OutGauge/OutSim UDP stream (its default) |
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
3. counts down 5 s (a chance to intervene; you do **not** have to alt-tab, the
   replay raises LFS itself), marks `scenario_start`, replays the recorded
   input, marks `scenario_end`;
4. keeps tracing for a couple more seconds so a late reaction is still caught;
5. stops the tracer, writes `run.json`, prints the summary.

Useful flags:

```
--speed 1.5           replay faster (be careful: LFS menus can drop fast clicks)
--tracer _temp/x.py   use a modified tracer (see §7)
--countdown 10        longer pause before the replay starts
--no-wait-menu        start regardless of where LFS is
--force               run even though pre-flight complained
--tail 5              keep tracing 5 s after the replay
--require OutGauge    exit 7 if that stream never arrived (repeatable)
--strict-timing       abort if an event is dispatched >0.25 s past its deadline
```

### A run that completed is not a run that passed

`run.json` always says `"functional_verdict": "not_evaluated"`. Nothing in this
harness can decide whether the *add-on* behaved — that is what the scenario's
`timeline.md` and a reading of the trace are for. The independent `chat_check`
is enforced on every run, including `--no-summary` (see below). Further checks:

- **Missing telemetry is not a zero reading.** A capture with no OutGauge looks
  exactly like "the brake never moved". `--require OutGauge --require MCI` on any
  driving scenario turns that into exit code 7.
- **A different outcome is not always a different behaviour.** Two runs of the
  same recording are not identical: LFS's AI and its physics drift a little.
  Measured over the five driving scenarios, the ego speed profiles of a
  baseline and an add-on run agree to **0.1–0.3 km/h mean** before the add-on
  does anything — so a real intervention stands out easily. But in the
  *crossing* scenarios that tiny drift decides the outcome: in `09`, a 2 m
  difference in where the two cars met at t−0.5 s turned a glancing 47 km/h
  contact into a square 97 km/h one, with the add-on having taken no action at
  all. **Always check how far the runs had already diverged before the add-on
  acted** — `--signal OutGauge.speed_kmh` on both, aligned on a marker — before
  attributing an outcome to the add-on. For a T-bone, n=1 proves nothing.
- **A warning-only feature is not proven by the trace.** MCI and OutGauge show
  that the situation happened, not that the HUD or the beeper fired. That still
  needs a human, or a diagnostic output the add-on does not have yet.

#### Some scenarios have to be run three times, not once

Where the *situation* is produced by the driver meeting an AI car at a
particular place, a fraction of a second of drift at the start moves the
meeting point by metres, and metres are the whole scenario. Such a run that
shows nothing has not shown that nothing happens — it may simply not have set
the situation up.

`24_blind_spot_warning_false_positive` is the shipped example and the reason
this paragraph exists. It is recorded cornering alongside another car, and the
false positive it was recorded for depends on being a couple of metres further
forward than the AI. In one run the ego was a few metres further back, at which
point an acute warning was not *possible* — a green result that proved
nothing. **Run it at least three times and read all three**; a false-positive
scenario passes only when every run is quiet, and a positive one only when the
situation actually occurred in the run you are reading.

The cheaper move for anything that is a *threshold* rather than an outcome:
replay the traces you already have through the systems offline with
`tools/replay_trace.py`, which is exactly repeatable, and use the in-game runs
to confirm.

Exit codes: `0` ok · `2` bad arguments · `3` pre-flight refused · `4` tracer did
not start · `5` LFS not at the main menu · `6` replay aborted · `7` required
telemetry missing · `8` the scenario is disabled · `9` LFS chat diagnostics ·
`10` chat review required or capture incomplete.

### Mandatory chat review

Every scenario records `MSO` (system and user chat, including hidden prefix and
`/o` messages), `III` (`/i` messages delivered to this connection), and `ACR`
(admin command reports delivered to this connection), even if its packet list
omits them. The runner also adds these packets for custom tracer scripts; the
base tracer enforces them for direct CLI runs. `ISF_MSO_COLS` preserves colours.
InSim only exposes messages delivered to that connection; it is not a chat-history
API and cannot recover messages from before the tracer connected.

`trace.jsonl` retains packet fields, raw message bytes (`raw_text_hex`), decoded
Unicode `text`, author/type and timestamps. `MSOData` selects the initial code
page; inline code-page switches are decoded too. The summary and `run.json`
include every chat message and a `chat_check`:

- `failed`: a recognised DE/EN system diagnostic (e.g. **Ungültiger Parameter**)
  or ACR rejection/unknown command. Exit 9 if no earlier failure already applies.
- `needs_review`: an unrecognised system message. Exit 10; inspect its text and
  timestamp. MSO has **no severity field**, so an unfamiliar/localised warning
  must never pass just because a keyword filter did not recognise it.
- `incomplete`: missing mandatory subscriptions, missing end record, dropped
  trace records, or an InSim connection error. Exit 10. No chat packets is valid
  only when the metadata and trace completeness establish capture was enabled.
- `passed`: complete capture, no diagnostics or unresolved system messages.
  This does not evaluate the assistance feature itself.

`chat_review.py` recognises only narrow informational forms already observed
in traces (checkpoint/layout/race-result announcements). User chat is retained
without treating quoted error text as an LFS error. Unknown system messages
must be reviewed before accepting a scenario; add a narrowly defined benign
form only after confirming its meaning, never suppress diagnostics to pass.

A scenario passes only when its feature expectations hold **and** no LFS
warnings remain. Always review `chat_check` when analysing live runs. Existing
traces can be reanalysed, including their old `{hex, text}` message fields;
they cannot prove complete chat capture if their metadata lacks subscriptions.

### Safety

Replaying injects global keyboard and mouse input. Three guards run during the
replay, all on by default:

- **Abort key: `Pause`.** Stops the replay immediately, anywhere.
- **Focus guard.** LFS must own the foreground while input is being injected —
  but the replay **takes** it rather than demanding it. It raises LFS right
  before the first event and takes it back whenever it is lost mid-run. Only a
  raise that actually fails falls through to `--on-focus-loss` (`abort` by
  default, `pause` holds the schedule, `ignore` switches the guard off).
  `run.json` reports `refocused`, so a run that fought for the foreground the
  whole way is visible rather than silent.
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
- LFS is not running, or pynput is unusable.

LFS being *behind* another window is deliberately **not** a refusal: an
unattended run has nobody to alt-tab for it, and the replay raises LFS itself
anyway. It is reported as a `note:` and in `run.json`'s `preflight_warnings`.

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
  "disabled": false,
  "disabled_reason": "",
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

### Disabling a scenario that no longer replays

Set `"disabled": true` and say why in `"disabled_reason"`. `run_scenario.py` and
`replay_scenario.py` then refuse it with exit code `8`, before the tracer starts
and before any input is injected; `--list` marks it `[DISABLED]` and prints the
reason; `--force` runs it anyway.

**Disable rather than delete.** The recording stays the reference for the
re-recording, and the runs under `runs/` that point at it stay readable.

The reason is worth writing properly, because the failure is rarely obvious
later. The one shipped example, `14_car_in_front_brakes_collision_warning_test`,
did not fail by aborting: it completed with exit 0 and a full trace of a car
that never moved, because LFS started the race 11.7 s later than in the
baseline and every driving input was replayed while the car was still in
neutral. **A recording that types `/track` and `/axload` and then drives is
betting on a fixed load time**, and that bet is what breaks first on a busy
machine. It also left LFS on the wrong track, which broke the next scenario in
the batch — the reason a disabled scenario is refused *before* anything starts.

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

### `NPL` in a trace is a snapshot, not the current state

Every shipped scenario traces `NPL` and none of them traces `PFL`. `IS_NPL` carries the
driver's help flags **as they were on joining**; `IS_PFL` is the change notification,
and SHIFT+G on track produces only the latter (`reference/insim.md` §7). So a trace can
show `Flags=0x0401` (no `AUTOGEARS`) for a run that was driven from start to finish with
LFS's automatic gearbox on — which is exactly what happened on 2026-09-19 and sent one
analysis of `known-issues.md` #47 down the wrong path for an afternoon. **Add `PFL` to
`packets` in any new scenario that touches gearbox, clutch or brake behaviour**, and do
not read a help flag off an `NPL` alone.

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

**You no longer have to arrange the foreground, and pre-flight no longer blocks
on it.** The player raises LFS itself (`win_focus.raise_window`) before the first
event and takes it back on every loss, so an agent can start a run from a
background console with LFS behind its own window. `run.json` reports
`refocused` and `preflight_warnings` if it had to work for it.

**Still avoid running other commands while a replay is live.** Each recovery
costs a moment of foreground the input did not reach, and a console window that
keeps stealing it turns a clean run into a stuttering one. If you must, watch
`refocused` in the result before trusting the trace.

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

Scenarios 05-21 exist in two families, recorded on this machine: the numbered
collision/gearbox/vehicle-management cases, and the two AI-traffic ones.

| Scenario | What it does |
|---|---|
| `20_at_traffic_test` | a full field on SO7, AI traffic started from the menu, views changed, race left at the end |
| `21_ai_traffic_started_from_another_car` | **derived from 20**, not recorded: one TAB at t=38.00 s puts the camera on another car *before* traffic is started. Every other timestamp is identical, so the two traces compare row by row. Its `timeline.md` says what to read out |

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
    insim_patch.py       compatibility imports for the shared IS_CON decoder
    chat_review.py       chat decoding, diagnostics and conservative run checks
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
| `could not bring LFS to the foreground` | the raise itself failed — a UAC-elevated window or a full-screen exclusive app is holding it. Close that, or run with `--no-focus-check` if you accept where the input lands |
| clicks land in the wrong place | the recording was made at another resolution; re-record |
| `no 'end' record` | the tracer was killed; the trace may be missing its tail |
| `required telemetry missing` (exit 7) | a `--require`d stream never arrived — the capture is inconclusive, not a finding |
| pre-flight: "still held when the recording ends" | the recording was stopped while a key was down; re-record it |
| pre-flight: "the LFS window moved or resized" | put LFS back where it was, or re-record |
| **no `MCI` at all**, on any UDP port, while STA/CIM still arrive | LFS has no InSim **UDP** socket. Check `Get-NetUDPEndpoint` for 29999; if only the TCP listener is there, restart LFS / re-issue `/insim 29999`. Every UDP reply (MCI *and* OutGauge) is dead until it is back. |
| summary warns "OutGauge never arrived" **and** the trace shows `cam=FOLLOW` | the scenario was recorded in the cockpit view and LFS re-uses its last camera. Enter the track once in the driver view, or force it with `/view driver`. |
| replay aborts with "LFS lost focus" for no visible reason | something took the foreground — including a console window spawned by *your own* tooling. See §7. |
| the replay clicks "start" before the track has loaded | a recording-side timing problem: re-record with a longer pause after the track selection. LFS may exit instead of returning to the menu, which then breaks the *next* run's pre-flight. |

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

## 12. Contact decoder compatibility

The tracer and add-on use the same `pyinsim.IS_CON` decoder. It accepts 40-byte
legacy and 44-byte current layouts and records `con_layout` for interpretation.
Pedal, speed and angle bytes are unsigned; accelerations are signed.
`insim_patch.py` retains imports and a no-op `apply()` for existing tracer copies;
it no longer installs or maintains a separate decoder. See `reference/insim.md`.

---

## 13. Running a scenario *with* the add-on

Validated on 2026-09-19 against `05_fcw_rear_end_keyboard`, both directions
(baseline and add-on) in the same session. The arrangement:

```
LFS ──cfg OutGauge──► 30000  relay (inside the add-on launcher)
                               ├──► 30010  the add-on (pyinsim.outgauge patched)
                               └──► 30011  the tracer
LFS ──InSim TCP 29999──► add-on connection  +  tracer connection (MCI on 30011)
```

`_temp/addon_mouse_parallel.py` is the launcher. Three things it does that a
plain `python main.py` cannot, and that any replacement has to keep:

1. **Own 30000 before the add-on starts** and fan out unchanged. Never let two
   processes bind it.
2. **Override the pedal bindings in memory**, not in `settings.json`. The add-on
   *pushes* its bindings into LFS with `/key`, and LFS keeps exactly one key per
   function — so an add-on configured for `b` / `up` will take the mouse buttons
   away from a recording that drives on them, and the replayed car then cannot
   accelerate at all. This is not hypothetical: it is what silently ruined the
   runs of 10:16 and 10:24 (zero contacts recorded, no `RST`). Patch
   `app.settings.get` after `LFSAssistantApp()` and before `app.start()`.
3. **Capture the add-on's own decisions** to a second JSONL. MCI and OutGauge
   show what the car did; only the bus shows *why*. Subscribing to
   `collision_warning_changed`, `needed_deceleration_update`,
   `emergency_brake_changed`, `*_availability` and `send_command_to_lfs` turns a
   run from "the car stopped" into "FCW reached level 3 at −1.08 s with a demand
   of 8.5 m/s², cut the throttle, and engaged". `gearbox_availability` belongs in
   that set too: it is the only place a run says whether the automatic gearbox
   was shifting at all, or had stood down because LFS was.

**Aligning the two files.** Both processes are on the same machine, so
`time.monotonic()` is directly comparable. The launcher records the monotonic
timestamp of every relayed datagram; the tracer records the same datagrams with
its own trace clock. Matching them by index gave an offset with **0.000 s spread
over all 2795 pairs** — the two files can be read as one timeline.

**Leave the LFS bindings as you found them.** The add-on's `/key` pushes persist
in LFS, and LFS writes them to disk on exit. A session that ran the add-on with
non-recording bindings leaves the *recording* broken until they are pushed back.
