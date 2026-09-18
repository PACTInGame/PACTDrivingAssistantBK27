# 05_fcw_rear_end

Add an AI driver, approach it from behind and close in until the collision warning must fire.

> **Status: not recorded yet.** Record it with
> `python simulation_tests/record_scenario.py 05_fcw_rear_end`, then replace the timeline
> table below with the one from `timeline.draft.md` and fill in the last column.

## Why this scenario exists

Forward collision warning is judged on required deceleration, so the trace needs both cars' positions at 50 ms and the contact packet that says whether they actually touched (reference/systems.md).

## Preconditions

- LFS is running with InSim enabled on port 29999
- LFS is at the main menu
- At least one AI driver is added before the race starts

## Recording guide

Press the marker key (**Scroll Lock** by default) at each step marked `MARKER`.
`record_scenario.py` names them in this order automatically, from the `markers`
list in `scenario.json`.

1. Start at the main menu.
2. Single Player, pick a car and the agreed test track.
3. Add one AI driver (Esc > Entries, or `/ai` from the chat) before starting.
4. MARKER `on_track` — both cars on track.
5. MARKER `ai_ahead` — the AI car is ahead of you on a straight, driving away.
6. MARKER `closing` — accelerate so you close on it clearly faster than it drives.
7. MARKER `warning_expected` — press this the moment you judge a warning is due.
8. MARKER `contact_or_avoid` — either you touch it or you brake off; press either way.
9. Wait ~10 s.
10. MARKER `leaving` — leave to the menu.
11. MARKER `back_at_menu` — stop the recording at the main menu.

## Timeline

| t [s] | Δt [s] | input | expected in the trace |
|------:|-------:|-------|-----------------------|
| | | *not recorded yet* | |

## What to check in the trace

- Distance between the two PLIDs over time, from `MCI.cars[PLID=n].x_m/y_m`.
- Closing speed from the two `speed_kmh` traces.
- `IS_CON` says whether the cars touched, when, and at what closing speed — the presence of the packet is the hard fact, the speed value is indicative.
- Against a running add-on: the warning must be up before `warning_expected`, and must not be up during `ai_ahead`.

## Analysis starting points

```
python simulation_tests/analyze_trace.py runs/05_fcw_rear_end_<stamp>/
python simulation_tests/analyze_trace.py runs/05_fcw_rear_end_<stamp>/ --timeline
python simulation_tests/analyze_trace.py runs/05_fcw_rear_end_<stamp>/ \
    --signal 'MCI.cars[PLID=0].speed_kmh' \
    --signal 'MCI.cars[PLID=1].speed_kmh' --csv closing.csv
python simulation_tests/analyze_trace.py runs/05_fcw_rear_end_<stamp>/ --events CON,OBH
```
