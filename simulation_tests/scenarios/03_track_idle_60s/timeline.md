# 03_track_idle_60s

Menu -> pick a car and track -> stand still for one minute -> back to the menu.

> **Status: not recorded yet.** Record it with
> `python simulation_tests/record_scenario.py 03_track_idle_60s`, then replace the timeline
> table below with the one from `timeline.draft.md` and fill in the last column.

## Why this scenario exists

The quiet baseline. Anything that moves, warns, beeps or actuates during this minute is a false positive, and the OutGauge stream must not stall once.

## Preconditions

- LFS is running with InSim enabled on port 29999
- LFS is at the main menu

## Recording guide

Press the marker key (**Scroll Lock** by default) at each step marked `MARKER`.
`record_scenario.py` names them in this order automatically, from the `markers`
list in `scenario.json`.

1. Start at the main menu.
2. Enter Single Player, pick a car, pick a track, start the race.
3. MARKER `on_track` — the car is on track, engine running, handbrake as LFS left it.
4. Do nothing at all for 60 seconds. Do not touch the camera.
5. MARKER `idle_done` — leave to the menu (Esc > End Race).
6. MARKER `back_at_menu` — stop the recording at the main menu.

## Timeline

| t [s] | Δt [s] | input | expected in the trace |
|------:|-------:|-------|-----------------------|
| | | *not recorded yet* | |

## What to check in the trace

- `OutGauge.speed_kmh` stays at 0 for the whole idle window.
- No `IS_CON` and no `IS_OBH` at all.
- The summary reports no OutGauge stall — a stall here means the camera left the internal view and the capture, not the add-on, is at fault.
- Against a running add-on: no warning of any kind should fire.

## Analysis starting points

```
python simulation_tests/analyze_trace.py runs/03_track_idle_60s_<stamp>/
python simulation_tests/analyze_trace.py runs/03_track_idle_60s_<stamp>/ --timeline
python simulation_tests/analyze_trace.py runs/03_track_idle_60s_<stamp>/ \
    --signal OutGauge.speed_kmh --from-marker on_track --to-marker idle_done
```
