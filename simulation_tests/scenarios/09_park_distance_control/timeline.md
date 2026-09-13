# 09_park_distance_control

Creep up to a layout object until the parking sensors must react, front and rear.

> **Status: not recorded yet.** Record it with
> `python simulation_tests/record_scenario.py 09_park_distance_control`, then replace the timeline
> table below with the one from `timeline.draft.md` and fill in the last column.

## Why this scenario exists

PDC works against layout objects, so the trace has to carry the layout (IS_AXM) as well as the car's motion, and IS_OBH marks the moment the car actually touched something.

## Preconditions

- LFS is running with InSim enabled on port 29999
- LFS is at the main menu
- An autocross layout with objects is loaded

## Recording guide

Press the marker key (**Scroll Lock** by default) at each step marked `MARKER`.
`record_scenario.py` names them in this order automatically, from the `markers`
list in `scenario.json`.

1. Start at the main menu; load the agreed autocross layout with objects.
2. MARKER `on_track`.
3. MARKER `approach_front` — creep forwards towards an object until very close.
4. MARKER `stop_front` — stop just short of it.
5. MARKER `approach_rear` — reverse slowly towards another object.
6. MARKER `stop_rear` — stop just short of it.
7. MARKER `leaving` — leave to the menu.
8. MARKER `back_at_menu` — stop the recording at the main menu.

## Timeline

| t [s] | Δt [s] | input | expected in the trace |
|------:|-------:|-------|-----------------------|
| | | *not recorded yet* | |

## What to check in the trace

- `IS_AXM` records the layout; object positions are 1/16 m, car positions 1/65536 m — the trace gives both as `x_m` / `y_m`, do not mix the raw ones.
- Distance from the car to the nearest object over time.
- `IS_OBH` only if the car touched an object; in a clean run there is none.
- Against a running add-on: the sensor value falls monotonically on approach and the beep interval shortens with it.

## Analysis starting points

```
python simulation_tests/analyze_trace.py runs/09_park_distance_control_<stamp>/
python simulation_tests/analyze_trace.py runs/09_park_distance_control_<stamp>/ --timeline

```
