# 05_fcw_rear_end_keyboard

## Validated recording

The directory retains its original name; this is **mouse steering in LFS
mouse/keyboard mode**, not a joystick scenario. Recorded 2026-09-19 at 07:55:45
UTC, duration 46.901 s, 328 events. Screen 1920x1080; LFS rect [0,0,1920,1080].
The input recording was not edited. Its original note still says keyboard.

Setup/menu clicks are included; see timeline.draft.md for every recorded action.
The existing AU4_collisionwarning layout and matching settings must be available.

| Recording time (s) | Action | Interpretation |
|---:|---|---|
| 0-17.49 | Recorded menu/setup clicks | Prepare session and enter track |
| 18.7722 | on_track | Human marker after entering track |
| 21.05-21.90 | Left mouse, s, right mouse | Initial control sequence; baseline remains near standstill |
| 30.94 | Left mouse down | Main acceleration; confirm Throttle rises in OutGauge |
| 34.9289 | collision_reference | Original collision reference, not a required crash in future braking tests |
| 37.36 | Right mouse down | Brake input after reference collision |
| 42.40-46.78 | Escape and menu clicks | Leave track and return to main menu |
| 46.901 | Recording ends | All recorded inputs released |

## Run

With the add-on stopped and UDP 30000 free:

```powershell
python simulation_tests/run_scenario.py 05_fcw_rear_end_keyboard --countdown 20 --udp-port 30000 --require OutGauge --require MCI
```

Live baseline 05_fcw_rear_end_keyboard_20260919-095822: all 328 events applied,
2684 OutGauge samples, 268 MCI packets, two CON events, zero dropped records.
OutGauge covered the on-track phase with at most 16 ms between received samples;
all gauge PLIDs were 1, matching the local FZ5. AI RB4 was PLID 3.
Contact arrivals were at replay-relative 35.109 s and 35.906 s. Final menu state
matched the initial state. This validates input replay and telemetry, not HUD,
sound or emergency-brake functionality of the add-on.

The configured LFS OutGauge stream goes to 30000; the default tracer port 30011
received no gauges on this installation. Never start a competing receiver on
30000. Parallel add-on/tracer operation needs a validated relay arrangement;
this direct-port baseline does not validate parallel operation. See README.md.

OutGauge is dashboard speed; MCI is a separate motion measurement. This baseline
has maxima 77.09 and 70.63 km/h respectively. Preserve both signals when comparing
runs instead of treating them as identical. Brake is a normalized input (0..1),
not a hydraulic pressure measurement.
