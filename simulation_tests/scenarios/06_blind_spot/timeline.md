# 06_blind_spot

Drive alongside an AI car so it sits in the blind spot, on both sides.

> **Status: not recorded yet.** Record it with
> `python simulation_tests/record_scenario.py 06_blind_spot`, then replace the timeline
> table below with the one from `timeline.draft.md` and fill in the last column.

## Why this scenario exists

Blind spot warning is pure geometry in a corridor beside the car, plus a relative-speed relevance test, so the trace needs both cars at 50 ms and a marker for each side.

## Preconditions

- LFS is running with InSim enabled on port 29999
- LFS is at the main menu
- At least one AI driver is added before the race starts

## Recording guide

Press the marker key (**Scroll Lock** by default) at each step marked `MARKER`.
`record_scenario.py` names them in this order automatically, from the `markers`
list in `scenario.json`.

1. Start at the main menu.
2. Single Player, pick a car and the agreed test track; add one AI driver.
3. MARKER `on_track` — both cars on track.
4. MARKER `overtaking_left` — pull alongside the AI on its left and hold there ~3 s.
5. MARKER `left_clear` — pull ahead so the blind spot is clear again.
6. MARKER `overtaken_right` — let the AI come alongside on your right, hold ~3 s.
7. MARKER `right_clear` — let it pass or drop back.
8. MARKER `leaving` — leave to the menu.
9. MARKER `back_at_menu` — stop the recording at the main menu.

## Timeline

| t [s] | Δt [s] | input | expected in the trace |
|------:|-------:|-------|-----------------------|
| | | *not recorded yet* | |

## What to check in the trace

- Lateral and longitudinal offset between the PLIDs, from the MCI positions and the own car's heading.
- Against a running add-on: the warning is up during the two alongside windows and down in the two clear windows, with the hold time respected.
- No `IS_CON` — a touch means the scenario was driven too tight to judge.

## Analysis starting points

```
python simulation_tests/analyze_trace.py runs/06_blind_spot_<stamp>/
python simulation_tests/analyze_trace.py runs/06_blind_spot_<stamp>/ --timeline

```
