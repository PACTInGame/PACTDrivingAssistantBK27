# 10_screen_and_dialog_sweep

Every LFS screen state the UI has to survive: SHIFT+U, dialogs, text entry, SHIFT+B, camera changes, pit and garage.

> **Status: not recorded yet.** Record it with
> `python simulation_tests/record_scenario.py 10_screen_and_dialog_sweep`, then replace the timeline
> table below with the one from `timeline.draft.md` and fill in the last column.

## Why this scenario exists

This is the state machine reference/ui.md §1 describes, recorded once. It is also the scenario that proves key injection is blocked in text entry and while Shift is held — running it against the add-on must produce no injected input at all.

## Preconditions

- LFS is running with InSim enabled on port 29999
- LFS is at the main menu

## Recording guide

Press the marker key (**Scroll Lock** by default) at each step marked `MARKER`.
`record_scenario.py` names them in this order automatically, from the `markers`
list in `scenario.json`.

1. Start at the main menu; go on track with any car.
2. MARKER `on_track`.
3. MARKER `text_entry` — press T, type something, press Esc (do not send).
4. MARKER `shift_u` — enter SHIFT+U free view, move around, leave it.
5. MARKER `shift_b` — press SHIFT+B to clear InSim buttons, then again to ask for them back.
6. MARKER `camera_sweep` — cycle the camera views with V (chase, heli, TV, driver).
7. MARKER `tab_view` — press TAB to look at another car, then TAB back.
8. MARKER `pits` — go into the pits / garage (Shift+P or Esc > Garage).
9. MARKER `back_on_track` — leave the garage back onto the track.
10. MARKER `leaving` — leave to the menu.
11. MARKER `back_at_menu` — stop the recording at the main menu.

## Timeline

| t [s] | Δt [s] | input | expected in the trace |
|------:|-------:|-------|-----------------------|
| | | *not recorded yet* | |

## What to check in the trace

- `ISS_TEXT_ENTRY` is set for the whole `text_entry` window.
- `ISS_SHIFTU` and `IS_CIM` mode `SHIFTU` during `shift_u`.
- `IS_BFN` with `BFN_USER_CLEAR` then `BFN_REQUEST` around `shift_b`.
- `IS_STA.InGameCam` visits FOLLOW/HELI/CAM/DRIVER during `camera_sweep`.
- `ViewPLID` changes during `tab_view` — everything that actuates must stop.
- Against a running add-on: **no key injection at all** anywhere in this run.

## Analysis starting points

```
python simulation_tests/analyze_trace.py runs/10_screen_and_dialog_sweep_<stamp>/
python simulation_tests/analyze_trace.py runs/10_screen_and_dialog_sweep_<stamp>/ --timeline
python simulation_tests/analyze_trace.py runs/10_screen_and_dialog_sweep_<stamp>/ \
    --events BFN,CIM
```
