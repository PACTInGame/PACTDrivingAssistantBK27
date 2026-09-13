# 04_drive_and_stop

Menu -> pick a car and track -> drive off, accelerate, brake to a full stop -> menu.

> **Status: not recorded yet.** Record it with
> `python simulation_tests/record_scenario.py 04_drive_and_stop`, then replace the timeline
> table below with the one from `timeline.draft.md` and fill in the last column.

## Why this scenario exists

The reference scenario for anything that reads or writes the pedals: auto-hold, the gearbox, the brake light, emergency braking. 25 ms OutGauge gives the brake trace enough resolution to see when a pedal actually moved.

## Preconditions

- LFS is running with InSim enabled on port 29999
- LFS is at the main menu

## Recording guide

Press the marker key (**Scroll Lock** by default) at each step marked `MARKER`.
`record_scenario.py` names them in this order automatically, from the `markers`
list in `scenario.json`.

1. Start at the main menu.
2. Enter Single Player, pick a car, pick a track, start the race.
3. MARKER `on_track` — standing still, ready.
4. MARKER `throttle_on` — accelerate to roughly 50 km/h.
5. MARKER `cruise` — hold that speed for ~5 s.
6. MARKER `brake_on` — brake firmly to a complete stop.
7. MARKER `stopped` — stay stopped for ~10 s (this is the auto-hold window).
8. MARKER `leaving` — leave to the menu.
9. MARKER `back_at_menu` — stop the recording at the main menu.

## Timeline

| t [s] | Δt [s] | input | expected in the trace |
|------:|-------:|-------|-----------------------|
| | | *not recorded yet* | |

## What to check in the trace

- `OutGauge.speed_kmh` rises after `throttle_on` and reaches 0 after `brake_on`.
- `OutGauge.Brake` (0.0-1.0) is the driver's pedal; with the add-on running, compare it against what auto-hold claims to be doing after `stopped`.
- `OutGauge.Gear` / `gear_label` shows the gearbox's shift points.
- `ShowLights` carries `HANDBRAKE` when the handbrake is applied.
- No `IS_CON`, no `IS_OBH`.

## Analysis starting points

```
python simulation_tests/analyze_trace.py runs/04_drive_and_stop_<stamp>/
python simulation_tests/analyze_trace.py runs/04_drive_and_stop_<stamp>/ --timeline
python simulation_tests/analyze_trace.py runs/04_drive_and_stop_<stamp>/ \
    --signal OutGauge.speed_kmh --signal OutGauge.Brake \
    --signal OutGauge.Throttle --signal OutGauge.gear_label \
    --from-marker brake_on --to-marker leaving --csv brake.csv
```
