# Assistance systems

All live in `assistance/`, subclass `AssistanceSystem` (`base_system.py`) and implement
`process(own_vehicle, vehicles) -> dict`. `AssistanceManager.process_all_systems()`
calls every enabled system once per cycle (default 100 ms) — but **only while
`on_track` is true and `own_vehicle` exists**.

`is_enabled()` is `self.enabled and settings.get(self.name.lower(), False)`, so the
constructor's `name` argument **must match a key in `SettingsManager._defaults`**.

| Key in `manager.py` | Class | `name` / settings key |
|---|---|---|
| `fcw` | `ForwardCollisionWarning` | `forward_collision_warning` |
| `bsw` | `BlindSpotWarning` | `blind_spot_warning` |
| `ctw` | `CrossTrafficWarning` | `cross_traffic_warning` |
| `pdc` | `ParkDistanceControl` | `park_distance_control` |
| `autoh` | `AutoHold` | `auto_hold` |
| `lighta` | `LightAssists` | `adaptive_lights` |
| `gearbox` | `Gearbox` | `automatic_gearbox` |
| `sat_nav` | `NavigationSystem` | `sat_nav` — **no such settings key → never runs** |
| `ai_traffic` | `AIDriver` | `ai_traffic` |
| — | `ChatCommandHandler` | event-driven, no `process()` |
| *(commented out)* | `ControllerEmulator` | `controller_emulator` |

---

## Forward Collision Warning — `collision_warning.py`

Detects cars in a forward wedge and computes the deceleration required to avoid them.

- **Detection:** builds a rotated quad ~85 m long, ±20° wide, from the car's heading
  (`calc_polygon_points` + `point_in_rectangle`). Suppressed below 10 km/h and while
  reversing.
- **Physics:** `_calculate_needed_braking` returns the required deceleration in m/s²
  using closed-form constant-acceleration kinematics, accounting for the lead car's own
  acceleration. It picks between two cases:
  - *dynamic* — we catch them while they still move: `a_req = a_lead − Δv²/(2d)`
  - *static* — they stop first: treat the stopping point as a wall,
    `a_req = −v² / (2·(d + d_lead_stop))`
  `d` subtracts both car lengths, a 0.5 m `SAFETY_BUFFER`, and a 0.2 s reaction-time
  term. `d ≤ 0.01` returns 20 m/s² (panic).
- **Levels:** required deceleration is compared against a three-element threshold list
  selected by `collision_warning_distance` (0 early / 1 normal / 2 late) —
  `[7.5, 3.0, 2.0]`, `[7.5, 5.0, 2.5]`, `[7.5, 6.5, 5.5]`. Levels are sticky: once at
  level 2/3, any positive requirement holds it there.
- **Output:** `collision_warning_changed` on change; `needed_deceleration_update` every
  cycle (0 unless level 3).
- **Automatic braking is deliberately disabled** (`# TODO no automatic braking for now`)
  and must not be re-enabled unasked.
- Emits `dist_debug` per detected vehicle per cycle — hot-path noise.

## Blind Spot Warning — `blind_spot_warning.py`

Two rotated quads beside the car (left `[90,178,177,90]`, right `[270,182,183,270]`
degree offsets) tested against a quad built around every other car, using
`shapely.Polygon.intersects`.

- Only warns if the other car's heading is within ±5000 LFS units (~±27°) of ours
  (`_is_within_threshold`) and it is approaching (distance gated by the speed delta).
- Output: `blind_spot_warning_changed` `{left, right}` on change.
- **Cost:** allocates one shapely polygon per vehicle per cycle with no distance
  pre-filter. This is the most expensive system per vehicle — see `known-issues.md` #7.

## Cross Traffic Warning — `cross_traffic_warning.py`

Ray-ray intersection between our path and each other car's path, then compares arrival
times.

- Skips: own speed < 5 km/h, gear ≤ 1 (reverse/neutral), other car < 3 km/h,
  crossing angle < `MIN_CROSSING_ANGLE_DEG` (20°), intersection farther than
  `MAX_INTERSECTION_DISTANCE` (100 m), arrival-time difference >
  `ARRIVAL_TIME_TOLERANCE` (0.5 s).
- Thresholds on TTC by `cross_traffic_warning_distance`: early `3.5/3.0`,
  medium `2.5/1.5`, late `1.5/1.0` s (visual / acoustic).
- `_compute_side` uses the 2D cross product. **The comment in that function describes
  the coordinate system incorrectly (it claims Y grows south / clockwise); the code
  itself is right for LFS's right-handed CCW system.** See `conventions.md` §1.
- Output: `cross_traffic_warning_changed` `{level, side}` on change.

## Park Distance Control — `park_distance_control.py`

Six virtual ultrasonic sensors (3 front, 3 rear) against layout objects *and* cars.

- Only active below 10 km/h; otherwise all six report `-1` (display removed).
- Sensor geometry: from the car's four corners plus front/rear midpoints, three nested
  triangular cones per position at 0.1 / 1.4 / 2.8 m, half-angle 25°.
- `get_vehicle_size` / `get_object_size` are hardcoded tables. For **vehicle mods**
  `CName` is an unknown value and the size silently falls back to `(4.5, 1.8)` —
  see `conventions.md` §4.
- Obstacles come from two sources:
  - **Static** — `IS_AXM` layout objects, converted to rectangles by
    `create_rectangle_for_object` (note the `* 4096` AXM→MCI unit conversion) and
    inserted into a `SpatialHashGrid` (cell size 15 m). Objects in `no_hitbox_objects`
    are skipped. Sizes come from the `get_object_size` index table.
  - **Dynamic** — other cars within 15 m, re-inserted every cycle;
    `get_vehicle_size` holds the per-model dimension table.
- Result is `{sensor: 0..3}` where 3 is closest; only emitted on change.
- `get_vehicle_size` is also imported by FCW for car-length maths — keep it here.

## Auto Hold — `auto_hold.py`

Applies the handbrake when the car is stopped with the brake pressed.

- Trigger: `speed < 0.05 km/h and brake > 0.05` and the handbrake dash light is off.
- Actuation is a **global `pyautogui` keypress** of `user_handbrake_key`. It is
  suppressed while `dialog` or `text_entry` is active — this guard is essential and
  must not be removed. It does **not** check whether the user is holding Shift, nor
  whether LFS has focus. Both are required; see `ui.md` §1.4 for the full rule set.

## Adaptive Lights / Cop Mode — `adaptive_lights.py`

Three unrelated features share this system:

1. **Adaptive brake lights** — flashes the hazards at ~150 ms while decelerating
   harder than 8 m/s² (or brake > 0.85 above 10 km/h), not while reversing.
2. **High beam assist** — high beam on unless a car is visible ahead
   (`distance < 250 m`, `speed > 1`, within ±15° cone). Gated by `high_beam_assist`.
3. **Siren / strobe (cop roleplay)** — enabled only when the player name contains
   `[cop]`, `[tow]` or `[res]` **and** `cop_assistance` is on. The strobe is a 14-step
   light pattern advanced one step per cycle. Siren uses `SMALL_LCS`; the strobe uses
   `send_light_command`. Toggled by buttons 62/63 or the `$siren` / `$strobe` chat
   commands.

Emits 12 of the project's `send_light_command` calls — it is the only system that
should be driving lights.

## Automatic Gearbox — `gearbox.py` (incomplete)

Shifts by injecting `pyautogui` keypresses (clutch down, shift key, release).
**It applies none of the input-injection guards** — no `text_entry`, no `dialog`, no
Shift, no focus check — so an automatic shift while the user is typing in chat types
the clutch and shift keys into the chat line. See `ui.md` §1.4 and
`known-issues.md` #11.

- **Requires per-car calibration**: idle rpm, redline, max gear. This is the pattern to
  copy for any car-specific parameter — it works for vehicle mods by construction
  (`conventions.md` §4). Started from the menu
  (`gearbox_calibrate`), three 12-second steps, persisted to
  `data/gearbox_calibrations.json` keyed by car name. Without calibration the system
  does nothing.
- **Anti-hunting design** — read `_process_shifting`'s docstring before changing it:
  throttle-dependent shift points create a wide dead zone between the upshift threshold
  (`idle + range·(0.50 + 0.42·throttle)`) and the downshift threshold
  (`idle + range·(0.15 + 0.20·throttle)`), plus direction-dependent cooldowns
  (1.5 s before reversing an upshift, 0.8 s the other way, 0.4 s same direction) and a
  5-sample throttle average.
- Gear numbering follows OutGauge: `0` = reverse, `1` = neutral, `2` = 1st gear.

## Navigation — `navigation.py` (dormant)

Dijkstra route guidance over the junction graph in `track_data/*.json`, with
turn-by-turn maneuver detection via cross/dot product of the incoming and outgoing road
vectors, notifying 150 m before a junction.

**Currently dead:** `sat_nav` is not in the settings defaults, and `sat_nav_active`
defaults to `False`. It also prints dozens of debug lines per cycle and does an
O(all road segments) nearest-road scan every cycle — it needs both cleanups before it
can be enabled. See `known-issues.md` #5.

## Chat commands — `chat_commands.py`

Not a `process()` system; it reacts to `message_received`. Only handles `IS_MSO`
packets with `UserType == MSO_PREFIX` (the InSim `Prefix` is `$`) whose sender matches
the local player name after stripping LFS colour/encoding markers.

Commands: `$help`, `$siren`, `$strobe`, `$fcw`, `$ctw`, `$autoh`, `$light`, `$highbeam`.
Add new ones to the `self._commands` dict and to `_cmd_help`'s text.

`check_tooltip()` is called from `AssistanceManager.process_all_systems` (outside the
`on_track` gate) and pushes a random translated tooltip every 360 s.

## AI Driver — `AI_Driver.py`

See `reference/ai-traffic.md`.

## Controller Emulator — `controller_emulator.py` (disabled)

Would convert `needed_deceleration_update` into a vJoy brake axis for wheel users,
switching LFS's brake axis via `/axis` commands. Commented out in `manager.py`; its
`Controls/wheel.py` dependency is also broken (`known-issues.md` #8).
