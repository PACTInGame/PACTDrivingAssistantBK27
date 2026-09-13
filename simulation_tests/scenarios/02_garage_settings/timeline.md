# 02_garage_settings

Open the garage (the 'box') and click through every vehicle setup page.

> **Status: not recorded yet.** Record it with
> `python simulation_tests/record_scenario.py 02_garage_settings`, then replace the timeline
> table below with the one from `timeline.draft.md` and fill in the last column.

## Why this scenario exists

IS_CIM's SubMode is the only reliable way to tell the garage pages apart, and the add-on's buttons behave differently in the pit/garage (reference/ui.md §1.2). This scenario walks every GRG_* submode once.

## Preconditions

- LFS is running with InSim enabled on port 29999
- LFS is at the main menu

## Recording guide

Press the marker key (**Scroll Lock** by default) at each step marked `MARKER`.
`record_scenario.py` names them in this order automatically, from the `markers`
list in `scenario.json`.

1. Start at the main menu, enter Single Player and pick a car and track, but stay in the garage (do not drive out).
2. MARKER `garage_open` — the garage screen is up.
3. MARKER `page_colours` — open the Colours page.
4. MARKER `page_brake_tc` — Brakes / TC.
5. MARKER `page_susp` — Suspension.
6. MARKER `page_steer` — Steering.
7. MARKER `page_drive` — Drive / gears.
8. MARKER `page_tyres` — Tyres.
9. MARKER `page_aero` — Aero.
10. MARKER `page_pass` — Passengers / ballast.
11. MARKER `leaving_garage` — leave the garage back to the menu, then stop.

## Timeline

| t [s] | Δt [s] | input | expected in the trace |
|------:|-------:|-------|-----------------------|
| | | *not recorded yet* | |

## What to check in the trace

- Every `IS_CIM` submode from `INFO` to `PASS` appears exactly once, in order.
- `IS_NPL` for the local driver arrives with `PType` carrying neither AI nor REMOTE.
- `IS_SLC` reports the car that was picked.
- No `IS_MCI` is expected: the car is not on track.

## Analysis starting points

```
python simulation_tests/analyze_trace.py runs/02_garage_settings_<stamp>/
python simulation_tests/analyze_trace.py runs/02_garage_settings_<stamp>/ --timeline

```
