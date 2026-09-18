# 01_menu_walkthrough

Click through the LFS menus and back, without ever entering a track.

> **Status: not recorded yet.** Record it with
> `python simulation_tests/record_scenario.py 01_menu_walkthrough`, then replace the timeline
> table below with the one from `timeline.draft.md` and fill in the last column.

## Why this scenario exists

Buttons must never be drawn on the main menu or the multiplayer list, and must disappear by themselves in dialogs and text entry (reference/ui.md §1). This scenario produces the IS_STA/IS_CIM sequence those rules are written against.

## Preconditions

- LFS is running with InSim enabled on port 29999
- LFS is at the main menu

## Recording guide

Press the marker key (**Scroll Lock** by default) at each step marked `MARKER`.
`record_scenario.py` names them in this order automatically, from the `markers`
list in `scenario.json`.

1. Start at the main menu.
2. MARKER `menu_single_player` — open Single Player.
3. MARKER `menu_multiplayer` — go back, open Multiplayer (the host list).
4. MARKER `menu_options` — go back, open Options.
5. MARKER `options_sweep` — walk through the options tabs (Game, Display, Audio, Controls).
6. MARKER `dialog_open` — open any dialog that covers the screen (e.g. Options > Display > a confirm).
7. MARKER `dialog_closed` — close it again.
8. MARKER `back_at_menu` — return to the main menu, then stop the recording.

## Timeline

| t [s] | Δt [s] | input | expected in the trace |
|------:|-------:|-------|-----------------------|
| | | *not recorded yet* | |

## What to check in the trace

- `IS_STA.Flags` carries `FRONT_END` for the whole run and never `GAME` without it.
- `ISS_DIALOG` appears between `dialog_open` and `dialog_closed` and nowhere else.
- `IS_CIM.Mode` reaches `OPTIONS` during `options_sweep` and returns to `NORMAL`.
- `ISS_VISIBLE` — whenever it is clear, the add-on must have no buttons on screen.

## Analysis starting points

```
python simulation_tests/analyze_trace.py runs/01_menu_walkthrough_<stamp>/
python simulation_tests/analyze_trace.py runs/01_menu_walkthrough_<stamp>/ --timeline

```
