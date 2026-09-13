# 08_lights_and_indicators

Exercise indicators, hazards, low/high beam and the brake light.

> **Status: not recorded yet.** Record it with
> `python simulation_tests/record_scenario.py 08_lights_and_indicators`, then replace the timeline
> table below with the one from `timeline.draft.md` and fill in the last column.

## Why this scenario exists

Everything the add-on does to the lights goes out as SMALL_LCL, which LFS does not echo back — but `OutGauge.ShowLights` does report what the car's dashboard shows, which is the only observable proof a light command landed.

## Preconditions

- LFS is running with InSim enabled on port 29999
- LFS is at the main menu

## Recording guide

Press the marker key (**Scroll Lock** by default) at each step marked `MARKER`.
`record_scenario.py` names them in this order automatically, from the `markers`
list in `scenario.json`.

1. Start at the main menu; go on track with any road car.
2. MARKER `on_track`.
3. MARKER `indicator_left` — indicate left for ~4 s, then off.
4. MARKER `indicator_right` — indicate right for ~4 s, then off.
5. MARKER `hazards` — hazards on for ~4 s, then off.
6. MARKER `lights_low` — switch on the low beam.
7. MARKER `lights_high` — switch to high beam, then back off entirely.
8. MARKER `braking` — drive off and brake hard once.
9. MARKER `leaving` — leave to the menu.
10. MARKER `back_at_menu` — stop the recording at the main menu.

## Timeline

| t [s] | Δt [s] | input | expected in the trace |
|------:|-------:|-------|-----------------------|
| | | *not recorded yet* | |

## What to check in the trace

- `OutGauge.showlights_flags` carries `SIGNAL_L` / `SIGNAL_R` in the indicator windows and both during hazards.
- `DIPPED` and `FULLBEAM` follow the beam markers.
- Careful: `ShowLights & DL_SHIFT` is the shift light and only the race cars set it (conventions.md §5.2) — do not read it as a redline.

## Analysis starting points

```
python simulation_tests/analyze_trace.py runs/08_lights_and_indicators_<stamp>/
python simulation_tests/analyze_trace.py runs/08_lights_and_indicators_<stamp>/ --timeline
python simulation_tests/analyze_trace.py runs/08_lights_and_indicators_<stamp>/ \
    --signal OutGauge.showlights_flags
```
