# Standalone LFS simulation tests

Record once, then replay mouse/keyboard actions while a separate InSim client
collects evidence. Run from the repository root with **Python 3.10 or 3.11 on
Windows**. The add-on does not start or import this tool. It can be running,
stopped, or replaced by another version during a test.

Only two infrastructure helpers are shared: the local `pyinsim` protocol library
and `misc/platform_shim.py` for lazy loading of pynput. No assistance system,
EventBus, settings manager, vehicle model or application lifecycle is imported.
Install input capture with `python -m pip install -r simulations-tests/requirements.txt`.
Offline validation and trace analysis need no input library or running game.

## First recording

1. Start LFS, enable InSim (`/insim 29999`), then return to the **main menu**.
2. Use a repeatable window position/size, screen resolution, DPI, UI language,
   keyboard layout and mouse sensitivity. Driving scenarios require mouse/keyboard
   controls; physical steering wheel/pedal axes are **not recorded**.
3. Prefer recording with the add-on stopped, so its interventions cannot change
   the reference manoeuvre. Settle the car/setup, layouts and AI prerequisites.
4. Run:

   ```powershell
   python simulations-tests/run.py record 04-drive-stop
   ```

5. During the five-second countdown focus LFS and release all controls. Perform
   the scenario. **F10** inserts a timestamped marker. Return to the main menu,
   release all controls, then **F11** finishes. **F12** aborts. These keys are
   reserved, omitted from the replay, and not suppressed in LFS; avoid assigning
   them to game functions that affect the scenario.
6. Fill in `scenarios/04-drive-stop/timeline.md`: describe each phase, expected
   behaviour, signal/PLID, time windows and tolerances. Markers and a timestamped
   input listing are generated; their semantic meaning cannot be inferred from
   mouse coordinates alone. Remove the DRAFT label after review.
7. Replay once manually and confirm that it repeats the intended manoeuvre.

A successful recording creates `replay.json`, `timeline.md` and `monitor.py`.
Existing scenario directories are never overwritten. A failed attempt retains
its trace under `_temp/runs`; if capture reached its end, a candidate replay is
also saved there. Use a new scenario name for another attempt, or deliberately
remove the incomplete scenario directory yourself after reviewing it.

Mouse position is sampled every **50 ms** (stationary samples are omitted).
Keyboard transitions, clicks and wheel events are captured by hooks with their
actual timestamps and drained on the same 50 ms cadence. Thus a short click
between samples is retained. Key auto-repeat is represented by one held key.
Injected input from the add-on is excluded. Clicks include their mouse position.
This is best-effort OS scheduling, not hard real-time input.

## Run a scenario

Start the version of the add-on being tested yourself (`python main.py`), arrange
LFS in the main menu and run:

```powershell
python simulations-tests/run.py inspect 04-drive-stop
python simulations-tests/run.py replay 04-drive-stop
# For a driving test, also reject absent streams:
python simulations-tests/run.py replay 04-drive-stop --require OutGaugePack --require IS_MCI
```

Each execution creates a unique `_temp/runs/<UTC>-<id>/` directory:

- `trace.jsonl`: independent packet records, input events, markers and failures.
- `summary.json`: execution status, packet counts, duration, replay and monitor
  hashes. It is written as `incomplete` before execution so an interrupted process
  cannot leave a success result.

Exit code 0 means execution completed; 1 means a caught runtime failure; invalid
arguments use 2. **`completed` does not mean the feature passed.** The functional
verdict remains `not_evaluated` until the expected outcomes have been checked.
Missing telemetry is not a zero value and must never count as a successful test.
There is no automatic launch/termination of the add-on or modification of its settings.

Replay uses absolute deadlines, logs actual dispatch time and lateness, and aborts
if an event is over 250 ms late. It refuses a different window geometry/resolution,
checks the foreground executable before each action, and aborts on focus loss,
physical key/button/wheel input, connection failure, F11 or F12. Pressed replay
controls are released in `finally`; a physically held human key is not released.
Do not move the mouse during playback. Force-killing Python cannot run cleanup;
press and release any control that remains held before continuing manually.

Start/end are checked with a fresh `IS_STA`. LFS's state flags do not distinguish
the main menu from every other front-end screen (notably the server list), so the
recording precondition must also be checked visually. On abort the tool releases
inputs and stops; it cannot safely guess a sequence back to the main menu.
Return there manually before the next run.

## Independent monitor and custom measurements

Each scenario's `monitor.py` is a standalone entry point:

```powershell
python simulations-tests/scenarios/04-drive-stop/monitor.py --seconds 90
# Before a scenario exists:
python simulations-tests/run.py monitor --seconds 15
```

The default observer records every inbound InSim packet supported by this
repository's decoder, including STA, CIM, NPL, MCI, CON (car-to-car contact),
OBH (object hit), HLV, and layout events, plus OutGauge and OutSim. It requests
initial state/players/connections, enables contact flags and requests 50 ms
MCI/OutGauge/OutSim updates. Protocol fields remain unmodified.

The TCP connection uses 29999 alongside the add-on. UDP defaults to **30001**;
29998 and 30000 are rejected because the add-on already uses them. Do not use
socket reuse to make two processes compete for the same datagrams.
`SMALL_SSG`/`SMALL_SSP` request streams via this InSim connection. Their behaviour
alongside cfg.txt streams must be verified on your installed LFS version: if a
driving trace lacks OutGauge/OutSim, it is **inconclusive**. Do not silently change
cfg.txt or redirect the add-on's stream automatically. See the
[official InSim reference](https://www.lfs.net/programmer/insim).

For a cfg.txt stream shared reliably by both processes, `udp_relay.py` provides
an optional transparent fan-out. With LFS **closed**, back up cfg.txt and change
only `OutGauge Port` to **31000** and `OutSim Port` to **30998** (keep both IPs
127.0.0.1, existing modes/delays/OutSim options). Then start the relay before LFS:

```powershell
python simulations-tests/udp_relay.py
# In another terminal, with LFS and optionally the add-on running:
python simulations-tests/run.py replay 04-drive-stop --cfg-streams --require OutGaugePack --require IS_MCI
```

Use `--cfg-streams` for recording and standalone monitoring too when using the
relay. It forwards identical datagrams to the add-on's original ports and the
monitor's 30001 without decoding or competing for sockets. It runs for one hour
by default (`--seconds` to change; Ctrl+C to stop). **The relay must stay running
while that cfg.txt configuration is in use**, including outside tests. Restore
the original ports (OutGauge 30000, OutSim 29998) with LFS closed if you no longer
want this setup. A setup wizard that rewrites those ports requires checking the
configuration again. The tool never edits cfg.txt itself.

**AI agents may freely adapt copied monitor scripts under `_temp/`. They must
not edit recorded scenarios, their baseline monitors, timelines or replay data
to make a test pass.** Keep baseline artifacts in version control. Example:

```powershell
New-Item -ItemType Directory -Force simulations-tests/_temp/autohold
Copy-Item simulations-tests/scenarios/04-drive-stop/monitor.py simulations-tests/_temp/autohold/monitor.py
# Edit the copy's Monitor.observe(name, packet), then:
python simulations-tests/run.py replay 04-drive-stop --monitor simulations-tests/_temp/autohold/monitor.py
```

`observe` can write derived records with `self.trace.write("signal", {...})`.
Keep callbacks short and nonblocking; they share the collector/replay thread.
Raising an exception invalidates the run. Prefer doing expensive analysis after
capture. Custom monitors are trusted Python code and should remain observational.
Raw packets are logged before the custom callback. The collector does not expose
internal add-on events or automatically subscribe to other InSim clients' buttons.

The observer has a local v10 decoder for IS_CON because the shared pyinsim decoder
still expects its older layout. Unsupported packet types are retained as hex;
decoder/callback failures save the offending bytes and fail the run. This adapter
does not change the add-on's protocol implementation.

## Read and compare traces

```powershell
python simulations-tests/analyze.py simulations-tests/_temp/runs/<run>/trace.jsonl
python simulations-tests/analyze.py simulations-tests/_temp/runs/<run>/trace.jsonl --packet OutGaugePack --csv simulations-tests/_temp/gauges.csv
```

Every JSONL row has `seq`, `t` (seconds since a single monotonic session origin),
`kind`, and `data`. Packet `Time` fields retain the game's clock and original
units; never assume they equal receive time or another packet type's clock.
UTC is metadata only. Bytes carry both `hex` (lossless) and `text` (Latin-1).
Nested arrays such as MCI.Info remain intact, including split-packet boundaries.

`replay_start.t` is time zero for the scenario timeline. Input rows also include
`planned_t` and `lateness_s`; packet times minus replay start give observed
scenario time. Recording uses `record_start.origin_t`. CSV exports expose both
receive and scenario time. Packets are not interpolated or resampled; UDP can
arrive late, reordered or be lost. Use per-stream gaps and original timestamps
when checking freshness, and document any interpolation in custom analysis.
CON/OBH are sparse events: absence is meaningful only when connection health,
contact flags, vehicle identity and the relevant driving window are established.

OutGauge Speed is **m/s**, Brake/Throttle/Clutch are **0..1 pedal values**, not
brake pressure. OutGauge follows the **camera's** PLID and needs an internal
camera. Identify the local driver from NPL (`UCID == 0`, not AI), match PLID and
watch for car/camera changes. InSim MCI speed uses `raw * 100 / 32768` m/s,
position uses `raw / 65536` m. OutSim availability depends on configuration and
the packet sizes supported by the local decoder; this tool cannot invent signals
that LFS does not expose.

For AutoHold compare repeated standstill windows, speed and pedal values, and
the subsequent departure. Define tolerances and maximum data gaps in the timeline.
For warning-only features, vehicle motion/contact establishes the situation but
does **not** prove the HUD or sound warning fired. That needs separate observable
evidence (manual review, screenshots/audio, or a future explicit diagnostic output).
No diagnostic output is added to the add-on by this tool.

## Scenarios to record manually

| Suggested name | Content | Required annotations |
|---|---|---|
| `01-menus` | Navigate menus, return to main menu | Screens and expected UI behaviour |
| `02-garage` | Visit all vehicle settings in pits | Garage submodes and settings changed |
| `03-idle` | Select car/track, idle 60 s, main menu | On-track/standstill interval, camera |
| `04-drive-stop` | Select car, accelerate, stop, main menu | Pedal phases, stopped interval, departure |
| `05-front-contact` | Load layout/AI, approach rear, wait 10 s | AI PLID, expected warning/contact windows |
| `06-blind-spot` | Repeatable side approach/overtake | Left/right overlap and relative speed |
| `07-cross-traffic` | Repeatable crossing trajectory | Paths, direction, near miss/contact window |

Also useful: camera switch, focus loss, pause, restart/teleport, no local driver,
connection loss, and layout/car-mod variants. AI motion and loading times may vary;
fixed input replay is not a deterministic physics simulation. Record generous
loading waits and verify the trajectory on every run. Do not treat an incorrect
starting state or a missed menu click as a feature regression.

## Offline verification

```powershell
python -m pytest tests/test_simulation_harness.py -q
```

These tests exercise trace serialization, input validation, timing, cancellation,
cleanup, observer requests and menu gates using fakes. They install no hooks,
open no sockets and do not operate LFS. Live recordings remain a separate,
explicitly launched test layer.
