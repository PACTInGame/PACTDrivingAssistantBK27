# 01_menu_walkthrough

Recorded and replayed on 2026-09-19. The human confirmed that all clicks landed
correctly and the replay returned to the main menu. Duration: 29.367 s;
289 input events; 1920x1080 screen, LFS window rect [0, 0, 1920, 1080].

## Scope and preconditions

Start and end at the LFS main menu, with InSim on port 29999. Keep the recorded
resolution, window geometry and menu layout. This scenario visits Single Player,
Multiplayer and the options pages clicked in the recording. It does not enter a
track, join a server or deliberately open a confirmation dialog.

Markers denote the action about to happen, not confirmation that its target
screen has been reached. There are exactly five markers; there is no final
back_at_menu marker. Return from Options is the click at 25.04 s. The final
4.33 seconds allow the menu to settle before recording ends.

## Recorded timeline

Times below are seconds from replay start. Subtract the trace's scenario_start
from trace timestamps to compare. Actual state transitions can lag clicks.

| Time (s) | Recorded input | Expected meaning / evidence |
|---:|---|---|
| 2.5071 | **MARKER `menu_single_player`** | Next click opens Single Player. |
| 2.7789 | left click @ (1766, 505) | Open Single Player. |
| 7.5859 | **MARKER `back_from_single_player`** | Next click returns from Single Player to main menu. |
| 7.8263 | left click @ (64, 1041) | Return to main menu. |
| 11.3048 | **MARKER `menu_multiplayer`** | Next click opens Multiplayer; no server is joined. |
| 11.5387 | left click @ (1798, 431) | Open Multiplayer. |
| 14.5267 | **MARKER `back_from_multiplayer`** | Next click returns from Multiplayer to main menu. |
| 14.8161 | left click @ (170, 1027) | Return to main menu. |
| 18.2686 | **MARKER `menu_options`** | Next click opens Options; expect CIM mode OPTIONS. |
| 18.4985 | left click @ (1677, 702) | Open Options; expect CIM mode OPTIONS. |
| 19.8857 | left click @ (244, 166) | Select recorded options page 1; exact page label is not captured by InSim. |
| 20.3958 | left click @ (237, 224) | Select recorded options page 2; exact page label is not captured by InSim. |
| 20.8908 | left click @ (236, 290) | Select recorded options page 3; exact page label is not captured by InSim. |
| 21.3708 | left click @ (230, 353) | Select recorded options page 4; exact page label is not captured by InSim. |
| 21.8507 | left click @ (230, 415) | Select recorded options page 5; exact page label is not captured by InSim. |
| 22.4058 | left click @ (234, 484) | Select recorded options page 6; exact page label is not captured by InSim. |
| 22.9532 | left click @ (243, 554) | Select recorded options page 7; exact page label is not captured by InSim. |
| 23.4632 | left click @ (249, 615) | Select recorded options page 8; exact page label is not captured by InSim. |
| 23.9807 | left click @ (242, 701) | Select recorded options page 9; exact page label is not captured by InSim. |
| 25.0382 | left click @ (136, 1073) | Leave Options; expect CIM mode NORMAL and return to initial menu state. |

| 29.3670 | Recording ends | Main menu; no keys or mouse buttons held. |

## Validated baseline and evaluation limits

Baseline run: `01_menu_walkthrough_20260919-093027`. All 289 events were applied,
with no replay abort, zero dropped trace records and maximum reported dispatch
lateness 0.0001 s. Human visual confirmation covers the successful menu sequence.

Measured STA flags: initial menu 19904; Single Player 19648; return 19904;
Multiplayer 3264; return 19904; Options 3264; final menu 19904. CIM changed to
OPTIONS at replay-relative 18.625 s and NORMAL at 26.609 s. These observed flag
values conflict with the existing screen map in reference/ui.md; do not use that
map or an assertion of FRONT_END throughout as an oracle for this recording.

The options-page clicks produced no individual CIM transitions. Their exact
labels and rendering cannot be asserted from this trace. This is a validated
navigation stimulus, not proof of add-on HUD or sound correctness. The add-on
functional verdict remains not evaluated.

Marker names were corrected after the baseline run using the human's reported
sequence; all recorded input actions and timestamps are unchanged. Historical
traces retain the old names. Their mapping to the current names is:

| Baseline marker | Current marker |
|---|---|
| menu_single_player | menu_single_player |
| menu_multiplayer | back_from_single_player |
| menu_options | menu_multiplayer |
| options_sweep | back_from_multiplayer |
| back_at_menu | menu_options |

## Run and inspect

```powershell
python simulation_tests/run_scenario.py 01_menu_walkthrough --countdown 20
python simulation_tests/analyze_trace.py runs/01_menu_walkthrough_<stamp>/ --timeline
```

For re-recording, press Scroll Lock immediately before each of the five marked
actions above, in order. After leaving Options, wait at the main menu, release
all inputs, then press Pause. No additional final marker is required.
