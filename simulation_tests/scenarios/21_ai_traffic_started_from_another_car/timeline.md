# 21_ai_traffic_started_from_another_car

## What this scenario is for

**Derived, not recorded.** It is `20_at_traffic_test` with a single TAB press
inserted at t=38.00 s, in the 5.8 s of idle between opening the AI-traffic menu
and the first click on its toggle. Nothing else was touched, so **every other
timestamp is identical to scenario 20** and the two traces line up row by row.

TAB moves the camera to another car. Everything the add-on needs to run AI
traffic comes from InSim — MCI for positions, IS_NPL for who is an AI and which
PLID is the local driver — and none of it from OutGauge, which follows the
camera (`reference/conventions.md` §5.3, §5.4). The menu still prints
*"Camera needs to be on own vehicle."* when traffic starts; this scenario exists
to find out whether that is still true.

Run 20 first, then 21, and compare. A difference between the two runs is a
camera dependency; the absence of one is the result this scenario is after.

## Preconditions

Same as scenario 20: SO7 with the AI-traffic layout available, a field of AI
cars on track, the add-on running next to the tracer through the OutGauge relay
(`README.md` §13), and LFS at the main menu.

## Timeline

Times are seconds from replay start; subtract `scenario_start` from the trace's
`t`. `timeline.draft.md` lists every recorded action.

| t [s] | Action | Expected in the trace |
|---:|---|---|
| 0–33.36 | Recorded menu clicks, `/track SO7`, `/axload AI_Traffic` | STA goes `on_track`; NPL for the local driver and every AI car |
| 33.36 | MARKER `ontrack` | on track, camera on the own car |
| 34.81–36.50 | SHIFT+U menu, AI traffic submenu | InSim buttons for the AI-traffic menu |
| **38.00** | **key `tab`** | the viewed car changes: `IS_STA.ViewPLID` and the OutGauge PLID move to another car, while MCI keeps reporting the whole field unchanged |
| **38.40** | MARKER `view_switched_to_other_car` | — |
| 42.28 | click on the toggle | first click only arms the confirmation, nothing is sent to LFS |
| 43.36 | MARKER `traffic_started` | — |
| 44.05 | second click | `/axload AI_Traffic` and `/restart` go out, then RST, then one NPL per car. **Every AI car must be adopted** — the app log prints one `assigned to route` line per car, and the count must match scenario 20's |
| 44–58 | traffic runs | AI cars move along their routes; no `no driver to control` in MSO |
| 58.65 | key `r` (1.13 s) | driver accelerates |
| 64.28 | MARKER `car_behind_should_brake` | the AI car approaching the player's car from behind must brake: its MCI speed falls before the gap closes, and no CON/OBH follows |
| 70–100 | mouse driving | — |
| 101.22 | MARKER `change_view` | — |
| 101.75–119.19 | 21× `tab`, 3× `v` | the camera walks the field; AI traffic must keep driving throughout, and no car may be dropped or re-assigned because of it |
| 143.54 | MARKER `exit_track` | — |
| 144.20 | key `esc` | leaving the race |
| 145–154 | menu clicks | **no IS_AIC at all after the cars disappear**: no `no driver to control` line in MSO, and the app log says `AI traffic dropped - the race was left` rather than `stopping`/`fully stopped` |
| 154.14 | end of recording | back at the main menu |

## What to read out of the trace

1. **`MSO` lines containing `no driver to control`** — must be zero, for the
   whole run. One per car at the end was the reported defect.
2. **The `assigned to route` count in `pact_assistant.log`** — must equal the
   number of AI cars on track, and must equal scenario 20's count. Fewer means
   the start depended on the camera.
3. **`CON` / `OBH` around t=64** — a contact there means the AI's collision
   avoidance did not brake in time.
4. **MCI speed of the car behind the player around t=60–66** — it should fall
   before the gap reaches ~6 m, not at it.

## Run

```bash
python simulation_tests/run_scenario.py 21_ai_traffic_started_from_another_car --require MCI
```

Not yet executed — this scenario was derived, not recorded, so its first run is
also its validation. If the inserted TAB lands on a menu instead of the track,
or LFS ignores it there, move it a second or two earlier and note it here.
