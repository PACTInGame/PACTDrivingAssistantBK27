# 07_cross_traffic

Approach a junction while an AI car crosses in front, from the left and from the right.

> **Status: not recorded yet.** Record it with
> `python simulation_tests/record_scenario.py 07_cross_traffic`, then replace the timeline
> table below with the one from `timeline.draft.md` and fill in the last column.

## Why this scenario exists

Cross traffic warning works on time-to-collision and a size-aware arrival window, so both crossing directions have to be in the trace, each with the moment the crossing car actually passes.

## Preconditions

- LFS is running with InSim enabled on port 29999
- LFS is at the main menu
- A junction layout is loaded
- At least one AI driver is added

## Recording guide

Press the marker key (**Scroll Lock** by default) at each step marked `MARKER`.
`record_scenario.py` names them in this order automatically, from the `markers`
list in `scenario.json`.

1. Start at the main menu.
2. Single Player, pick a car and the agreed junction layout; add one AI driver.
3. MARKER `on_track` — both cars on track.
4. MARKER `approach_1` — roll up to the junction while the AI crosses from the left.
5. MARKER `crossed_1` — the AI has passed in front of you.
6. MARKER `approach_2` — repeat with the AI crossing from the right.
7. MARKER `crossed_2` — it has passed.
8. MARKER `leaving` — leave to the menu.
9. MARKER `back_at_menu` — stop the recording at the main menu.

## Timeline

| t [s] | Δt [s] | input | expected in the trace |
|------:|-------:|-------|-----------------------|
| | | *not recorded yet* | |

## What to check in the trace

- The crossing car's `direction_deg` relative to the own car's `heading_deg` gives the side; check it matches the side the warning claims.
- Time-to-collision from the MCI positions and speeds.
- Against a running add-on: a warning in both approach windows, none after the crossing car has passed.

## Analysis starting points

```
python simulation_tests/analyze_trace.py runs/07_cross_traffic_<stamp>/
python simulation_tests/analyze_trace.py runs/07_cross_traffic_<stamp>/ --timeline

```
