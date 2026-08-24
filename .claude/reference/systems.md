# Assistance systems

All live in `assistance/`, subclass `AssistanceSystem` (`base_system.py`) and implement
`process(own_vehicle, vehicles) -> dict`. `AssistanceManager.process_all_systems()`
calls every enabled system once per cycle (default 100 ms) — but **only while
`on_track` is true and `own_vehicle` exists**.

`is_enabled()` is `self.enabled and settings.get(self.name.lower(), False)`, so the
constructor's `name` argument **must match a key the settings know** —
`SettingsManager.known_keys`, i.e. `_defaults` plus the derived keys. The explicit
`False` there only takes effect for a name the settings do *not* know; for a known key
`get()` always returns the stored value or the schema default (`ui.md` §5).

| Key in `manager.py` | Class | `name` / settings key |
|---|---|---|
| `fcw` | `ForwardCollisionWarning` | `forward_collision_warning` |
| `bsw` | `BlindSpotWarning` | `blind_spot_warning` |
| `ctw` | `CrossTrafficWarning` | `cross_traffic_warning` |
| `pdc` | `ParkDistanceControl` | `park_distance_control` — derived from `park_distance_control_mode` |
| `autoh` | `AutoHold` | `auto_hold` |
| `lighta` | `LightAssists` | `adaptive_lights` |
| `gearbox` | `Gearbox` | `automatic_gearbox` |
| `ai_traffic` | `AIDriver` | `ai_traffic` |
| — | `ChatCommandHandler` | event-driven, no `process()` |
| *(commented out)* | `ControllerEmulator` | `controller_emulator` |
| *(not registered)* | `NavigationSystem` | would need a `sat_nav` key — see below |

---

## Forward Collision Warning — `collision_warning.py`

Detects cars in a forward wedge and computes the deceleration required to avoid them.

- **Detection:** builds a rotated quad ~85 m long, ±20° wide near the car and ±1° at
  its far end, from the car's heading (`calc_polygon_points` + `point_in_rectangle`).
  Two cheap gates run first, both on values `VehicleManager` already computed per
  frame, so nothing pays for a polygon test it cannot pass: `distance_to_player` beyond
  the wedge length, and `angle_to_player` outside ±21°.
  Suppressed below 10 km/h and while reversing — reverse detection is
  `misc.helpers.is_reversing`, a **signed modular** heading/direction difference.
  A plain subtraction disabled the system in one heading sector.
- **Physics:** `_calculate_needed_braking` returns the required deceleration in m/s²
  using closed-form constant-acceleration kinematics, accounting for the lead car's own
  acceleration. It picks between two cases:
  - *dynamic* — we catch them while they still move: `a_req = a_lead − Δv²/(2d)`
  - *static* — they stop first: treat the stopping point as a wall,
    `a_req = −v² / (2·(d + d_lead_stop))`
  `d` subtracts the mean car length, a 0.5 m `SAFETY_BUFFER`, and a 0.2 s reaction-time
  term (the last one only while actually closing). `d ≤ 0.01` returns 20 m/s² (panic).
  The result is a **non-negative required deceleration**: 0 means "no braking needed".
  It used to be `abs(req_accel)`, so a situation that allowed us to accelerate came back
  as a large braking demand.
  Car lengths come from `park_distance_control.get_vehicle_size`, but only for the ~15
  car codes that table really knows; an unknown `CName` (every vehicle mod) gets the
  longest standard car, 5.0 m, so the warning is early rather than late
  (`conventions.md` §4).
- **Levels:** required deceleration is compared against a three-element threshold list
  selected by `collision_warning_distance` (0 early / 1 normal / 2 late) —
  `[7.5, 3.0, 2.0]`, `[7.5, 5.0, 2.5]`, `[7.5, 6.5, 5.5]` (level 3, 2, 1). Level 1 also
  requires that we are not already braking hard enough by ourselves.
  **Hysteresis, not a latch:** a level rises at its threshold and falls again once the
  demand drops below `HYSTERESIS_RELEASE` (0.8) × that threshold, one step at a time.
  Before WP7 any demand above 0 held level 3 indefinitely.
- **Output:** `collision_warning_changed` on change; `needed_deceleration_update` every
  cycle (0 unless level 3).
- **Automatic braking is deliberately disabled** (`# TODO no automatic braking for now`)
  and must not be re-enabled unasked.

## Blind Spot Warning — `blind_spot_warning.py`

Two long, narrow corridors beside the car — from the car's centre to 85 m behind it,
laterally 1…4.5 m off the axis, i.e. the adjacent lane. Each is tested against a
2.3 m-radius quad built around the other car with `shapely.Polygon.intersects`.

- **Trigger** = geometry **and** relevance:
  1. the other car's outline intersects the corridor;
  2. within `BLIND_SPOT_ZONE_M` (7 m from our centre — the mirror blind spot proper,
     ISO 17387 uses "rear bumper + 3 m") it is always relevant, whatever its speed;
     further back only while it is closing and reaches us inside `APPROACH_TIME_S`
     (3.5 s, the lane-change-assist criterion);
  3. `_is_within_threshold`: its heading must be within ±5000 LFS units (~±27°) of
     ours, so oncoming traffic is not blind-spot traffic.
  The condition used to be `distance < (other_kmh − own_kmh + 5) · 1.2` — metres
  compared against km/h. For any car not faster than us the right-hand side was ≤ 0,
  so a car sitting in the blind spot at our speed could **never** warn.
- **Hold time:** a set warning stays for the time the other car needs to move one
  vehicle length relative to us, clamped to 0.5…2.0 s, so one missed 100 ms sample
  cannot blank it.
- **Corner order matters.** Both corridor quads were `[near-outer, far-inner,
  far-outer, near-inner]`, which crosses two edges: shapely got an invalid polygon
  covering a 64 m² bow-tie instead of the intended 190 m² corridor. Same defect class
  as `known-issues.md` #35 in FCW.
- The other car's outline used `abs((heading − 16384) / 182.05)`. Above 16384 that is
  a 180° rotation, which this centrally symmetric box does not notice; below it, it is
  a **mirror** — a car pointing north-west got an outline pointing north-east.
- **Cost:** per vehicle two float comparisons (`distance_to_player`,
  `angle_to_player`) and one modular heading test. A shapely polygon and two
  `intersects` are paid only for cars that pass all of them — normally none to two,
  instead of one polygon per car per cycle.
- Output: `blind_spot_warning_changed` `{left, right}` on change.
- **Open product question:** the corridor is 85 m long, which is lane-change-assist
  geometry rather than a blind spot. The relevance rule keeps far-away same-speed
  traffic quiet, but a fast approacher 80 m back does raise a blind-spot warning.
  Shortening `_CORRIDOR_MULTIPLIERS` is a decision for the author, not a bug fix.

## Cross Traffic Warning — `cross_traffic_warning.py`

Ray-ray intersection between our path and each other car's path, then compares arrival
times.

- Skips: own speed < `MIN_OWN_SPEED_KMH` (5), **reversing**, other car <
  `MIN_OTHER_SPEED_KMH` (3), crossing angle < `MIN_CROSSING_ANGLE_DEG` (20°),
  intersection farther than `MAX_INTERSECTION_DISTANCE` (100 m), arrival-time
  difference outside `_arrival_window()`.
- The gate used to be `own_vehicle.gear <= 1`, i.e. the raw OutGauge gear. That
  silenced the system in neutral, in reverse, and for any car whose gear is not
  reported (0). What matters is the motion, so it is now speed plus
  `misc.helpers.is_reversing(heading, direction)` — reversing has to stay excluded
  because the direction vector is derived from `heading` and would point the wrong
  way.
- **`_arrival_window()` is size-aware.** Vehicles are bodies, not points: we occupy
  the conflict area for `(own_length + other_width) / own_speed`, they for
  `(other_length + own_width) / other_speed`. The window is the sum of the two
  half-occupancies plus `ARRIVAL_TIME_TOLERANCE` (0.5 s) for noise. A 5 m car
  crossing at 10 km/h blocks the junction for ~2.4 s and was simply missed by the
  old fixed ±0.5 s. Lengths come from `park_distance_control.get_vehicle_size`.
- Thresholds on TTC by `cross_traffic_warning_distance`: early `3.5/3.0`,
  medium `2.5/1.5`, late `1.5/1.0` s (visual / acoustic).
- `_compute_side` uses the 2D cross product; the code is right for LFS's
  right-handed CCW system and the docstrings now say so (they used to claim Y grows
  south, which is what `known-issues.md` #16 was about). See `conventions.md` §1.
- Output: `cross_traffic_warning_changed` `{level, side}` on change.

## Park Distance Control — `park_distance_control.py`

Six virtual ultrasonic sensors (3 front, 3 rear) against layout objects *and* cars.

- Only active below `PDC_MAX_SPEED_KMH` (10); otherwise all six report
  `PDC_INACTIVE` (`-1`), which `UIManager._update_pdc` reads as "remove the display".
  `PDC_CLEAR` (`0`) means "active, nothing in range" and keeps the empty column on
  screen — the two must not be swapped.
- Sensor geometry: from the car's four corners plus front/rear midpoints, three nested
  triangular cones per position at 0.1 / 1.4 / 2.8 m, half-angle 25°.
- `get_vehicle_size` / `get_object_size` are hardcoded tables. For **vehicle mods**
  `CName` is an unknown value and the size silently falls back to `(4.5, 1.8)` —
  see `conventions.md` §4.
- Obstacles come from two sources:
  - **Static** — `IS_AXM` layout objects, converted to rectangles by
    `create_rectangle_for_object` (`AXM_TO_MCI` = 65536/16 = 4096, verified against
    `conventions.md` §1) and inserted into a `SpatialHashGrid` (cell size 15 m).
    Objects in `NO_HITBOX_OBJECTS` are skipped. Sizes come from the `get_object_size`
    index table.
    Grid keys are `axm_object_id(info)` = `(Index, X, Y, Zbyte)` **tuples**. They used
    to be `int(str(Index) + str(abs(X)) + str(abs(Y)) + str(abs(Zbyte)))`, which is
    not injective — `X=1, Y=23` and `X=12, Y=3` produce the same number, and `abs()`
    threw the sign away — so a `PMO_DEL_OBJECTS` could evict a different object and
    leave an invisible obstacle behind until the next layout reload.
  - **Dynamic** — other cars within `PDC_VEHICLE_RANGE_M` (15 m), re-inserted every
    cycle after `clear_dynamic_objects()`; `get_vehicle_size` holds the per-model
    dimension table.
- Result is `{sensor: 0..3}` where 3 is closest; only emitted on change.
- **The beeper is one long-lived daemon thread** (`misc/pdc_beep.py`), started on the
  first `beep()` call and fed by `pdc_changed`. `UIManager._show_pdc_display` calls
  `beep()` every UI cycle while `park_distance_control_mode == 2`; that call only
  *permits* sound for `REQUEST_TIMEOUT_S`, the pattern timing happens in the thread.
  Before, every single beep was a fresh thread running a blocking `winsound.Beep`.
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

**Not wired up:** since WP6 `NavigationSystem` is no longer constructed in
`AssistanceManager._init_systems`. It had no `sat_nav` settings key, so it could never
be enabled, and every cycle still paid for its `is_enabled()` call and its two event
subscriptions. Before it can come back it needs the `print()` calls out of the hot path
and an index for the nearest-road scan (`known-issues.md` #5); adding the settings key
alone would only make a broken system reachable.

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

**Read `reference/control-intervention.md` before touching this or any other feature
that actuates the car.** It covers arbitration, handback, fail-safe behaviour, the axis
configuration requirement, and the keyboard key-release trap.
