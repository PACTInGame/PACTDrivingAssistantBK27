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

---

## Forward Collision Warning — `collision_warning.py`

Detects cars in a forward corridor and computes the deceleration required to avoid them.

- **Detection:** a straight corridor `CORRIDOR_LENGTH_M` (85 m) long along the car's
  heading. Its half-width is **not a tuned number**: it is the lateral-overlap
  condition, `(own_width + other_width) / 2`, because that is exactly when the two
  bodies claim the same lane width. One cheap gate runs first, on a value
  `VehicleManager` already computes per frame (`distance_to_player`); what passes it
  gets two dot products — forward and lateral offset in the ego frame.
  Only cars *ahead* count: a centre behind ours is a blind-spot case, not a rear-end one.
  Suppressed below 10 km/h and while reversing — reverse detection is
  `misc.helpers.is_reversing`, a **signed modular** heading/direction difference.
  A plain subtraction disabled the system in one heading sector.

  This replaced a wedge built from angles (±20° near, ±1° at 85 m), whose half-width
  was 1.03–1.48 m over its whole length — narrower than a car. Measured in game on
  2026-09-20: the targets that were hit sat up to **1.68 m and 1.77 m** off the ego
  axis and dropped out of the wedge in the very second the intervention had to start
  (level and demand fell to 0); the close overtake that must *not* fire passed at
  **2.74 m**. The overlap condition (≈1.9 m for an FZ5 beside an RB4) separates the
  two cleanly. Widths come from the same table as the lengths, with the conservative
  fallback for mods.
- **Physics:** `_calculate_needed_braking` returns the required deceleration in m/s²
  using closed-form constant-acceleration kinematics. **Only the lead car's braking is
  extrapolated**: `a_lead` is clamped to ≤ 0. Its speed is a measurement and is used in
  full, but the speed it has not gained yet is a claim about the future, and a car
  cannot accelerate for ever — the one pulling away now is the one standing still a
  second later. Measured (scenario 32, sub-case 4, 2026-09-20): a lead accelerating
  away at 5 m/s² made `a_req` positive, so the demand was **0.0 for 1.8 s** while the
  gap closed from 55 m to 44 m at 82 km/h; the warning then arrived 1.15 s before
  contact. Same one-sided rule as the yaw extrapolation in `path_conflict.py`: a model
  may bring an answer forward, never postpone it.
  It picks between two cases:
  - *dynamic* — we catch them while they still move: `a_req = a_lead − Δv²/(2d)`
  - *static* — they stop first: treat the stopping point as a wall,
    `a_req = −v² / (2·(d + d_lead_stop))`
  `d` subtracts the mean car length, a 0.5 m `SAFETY_BUFFER`, and a 0.2 s reaction-time
  term (the last one only while actually closing). `d ≤ 0.01` returns 20 m/s² (panic),
  and the computed value is **capped at the same 20 m/s²** — beyond it the division is
  rounding error, not information (traces of 2026-09-20 carried 198, 1354 and
  4758 m/s² into the logs, which makes any average over the demand useless).
  The result is a **non-negative required deceleration**: 0 means "no braking needed".
  It used to be `abs(req_accel)`, so a situation that allowed us to accelerate came back
  as a large braking demand.
  Car sizes come from `park_distance_control.get_vehicle_size`, but only for the ~15
  car codes that table really knows; an unknown `CName` (every vehicle mod) gets the
  largest standard car, 5.0 × 2.1 m, so the warning is early rather than late
  (`conventions.md` §4).
- **Levels:** required deceleration is compared against a three-element threshold list
  selected by `collision_warning_distance` (0 early / 1 normal / 2 late) —
  `[7.5, 3.0, 2.0]`, `[7.5, 5.0, 2.5]`, `[7.5, 6.5, 5.5]` (level 3, 2, 1). Level 1 also
  requires that we are not already braking hard enough by ourselves.
  **Hysteresis, not a latch:** a level rises at its threshold and falls again once the
  demand drops below `HYSTERESIS_RELEASE` (0.8) × that threshold, one step at a time.
  Before WP7 any demand above 0 held level 3 indefinitely.
- **Output:** `collision_warning_changed` on change; `needed_deceleration_update` every
  cycle, carrying the demand **from level 3 upwards** and 0 below it. The gate is on the
  *level*, not on a number, which also keeps the order display → sound → brake true in
  every distance setting: `EmergencyBrake` can never see a demand the driver has not
  already been told about.

  **This gate, not `ENGAGE_DECELERATION_MS2`, is what decides when the car brakes.**
  Level 3 starts above 7.5 m/s² in every setting, so the brake's own 6.0 m/s² floor is
  never the binding constraint. Publishing from level 2 instead was tried and measured
  in game on 2026-09-20: every remaining rear-end collision disappeared (scenario 32
  went from two impacts to none), **and the car came to rest 4 m, 6 m and 11 m short**
  in scenarios 06, 07 and 12. That is not an emergency stop, and a driver reads it as
  the assistant panicking.

  The cause is the actuator, not the threshold: the key output is digital, so an
  intervention that starts at a demand of 6 m/s² still brakes with the ~9.7 m/s² the
  tyres give and overshoots by roughly a quarter of the braking distance. With the gate
  back at level 3 the same scenarios stop **0.7–1.6 m** behind the lead car.
  **The right way to have both is modulation** — duty-cycling the key, or the analog
  path, which already runs feed-forward plus a correction on the achieved deceleration
  (`control-intervention.md` §3). Until the digital path modulates, the later gate is
  the honest setting. What it still costs is measured: scenario 13 taps at 18 km/h
  instead of 8, and scenario 32's sub-case 4 — a lead that accelerates away and then
  brakes at 10 m/s² — hits at 44 km/h instead of not at all (baseline 85 km/h).
- **This system never actuates anything.** It warns and it publishes a demand; whether
  that becomes braking is `EmergencyBrake`'s decision alone, and only at
  `automatic_emergency_brake == 2` (default 1, warn only). The trailing
  `# TODO no automatic braking for now` that used to sit at the end of the module was
  left over from before that split and described the opposite of what ships.

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
  as the corrected FCW corner order described above.
- The other car's outline used `abs((heading − 16384) / 182.05)`. Above 16384 that is
  a 180° rotation, which this centrally symmetric box does not notice; below it, it is
  a **mirror** — a car pointing north-west got an outline pointing north-east.
- **Cost:** per vehicle two float comparisons (`distance_to_player`,
  `angle_to_player`) and one modular heading test. A shapely polygon and two
  `intersects` are paid only for cars that pass all of them — normally none to two,
  instead of one polygon per car per cycle.
- **Three levels per side** (`left_level` / `right_level` in the event):
  1. **Display** — the corridor geometry above. Somebody is where no mirror looks.
  2. **Acute** — blinking and audible: the two outlines are predicted to touch within
     `ACUTE_TTC_S` (2.5 s), on the straight line or on the arc the yaw rate describes.
     That one criterion covers both cases the driver feels — "I am getting too close"
     and "I am driving into their path".
  3. **Braking** — level 2, plus own speed below `BRAKE_SPEED_KMH` (30), the other car
     behind us and at least `MIN_APPROACH_DELTA_KMH` (10) faster, and a stop still able
     to keep us out of its corridor. This is turning into flowing traffic. The demand
     goes out as `needed_deceleration_update` with `source='blind_spot'`.
- **Levels 2 and 3 do not use the corridor.** They ask "do these two rectangles meet",
  through `assistance/path_conflict.py`. The corridor is fixed to *our* heading, so it
  turns **away** from the car we are steering towards — the warning would go quiet at
  the exact moment it is needed.
- **The yaw rate is what makes a lane change visible in time.** With the heading
  alone, scenario 22 still read "never crosses their path" 0.7 s after the driver
  started turning — two degrees of heading across a 1.8 m gap is four seconds of
  travel, while `AngVel` already said 20 °/s. Both `free_distance` and
  `contact_window` therefore also walk the arc. That moved the intervention 0.25 s
  earlier and turned scenario 22's contact into a 2.9 m miss.
- **`_MIN_RELATIVE_YAW_RATE` (12 °/s) is measured, not chosen.** Over scenario 24
  — two cars side by side through a long bend — the *relative* yaw rate reaches
  10.6 °/s from line choice and steering corrections alone (median 2.8). A real
  turn-in is above 20 °/s when it matters. At the original 1 °/s the arc turned
  corner noise into a predicted collision.
- **The prediction horizon scales with what supports it** (`STEADY_TTC_S`, 1.5 s,
  against `ACUTE_TTC_S`, 2.5 s), and this is what keeps the warning out of corners.
  Two cars taking the same bend side by side hold a persistent couple of degrees of
  heading between them — the rest of the corner one has already taken. Over 2.5 s at
  110 km/h that is 70 m of travel, and a degree of error is 1.2 m of lateral offset:
  scenario 24 predicted contact in 1.9 s between two cars that simply drove on like
  that. So the full warning time is given only when the relative yaw rate shows
  somebody actually steering into somebody; otherwise the system looks 1.5 s ahead,
  where the error is under half a metre. The cost is one second of warning for a car
  closing on a fixed shallow angle — and a second is still left.
- **In a shared corner nothing is extrapolated at all.** 1.5 s is still too long when
  the two cars are close: in one of three runs of scenario 24 they were 3.4 m apart
  instead of 5.5, and the same persistent angle predicted contact in 1.0 s. So when
  **both** cars are cornering (`MIN_CORNERING_YAW`, 2 °/s — measured, a straight never
  exceeds 1.7) and neither is steering into the other, the angle between them *is* the
  bend, and only a real overlap warns. Racing alongside somebody must not blink and
  beep, or the driver switches the system off and it protects nothing.
- **Making the model cleverer does not fix that, and it was tried.** Giving each car
  its own arc instead of using the difference reproduces a steady corner correctly in
  principle, but the measured radii differ (196 m against 164 m in scenario 24), so
  the two arcs meet as well — just later. No extrapolation is reliable over 70 m;
  bounding the horizon is.
- Scenario 24 needs **three runs** before a quiet result means anything: where the
  two cars meet decides whether the situation exists at all
  (`simulation_tests/README.md` §3). Two of three runs showed the false positive; the
  third could not have produced it.
- **Two true statements about different moments are not one true statement.** Level 2
  also used to fire when "we enter their corridor within 1.5 s" *and* "they are within
  2.5 s behind us". Both held in scenario 25 — turning in slowly behind traffic that
  was drawing level at 93 km/h: corridor entry in 1.4 s, headway 0.06 s. They were
  27 m down the road by the time we got there. The contact prediction asks about one
  moment at a time and gets it right, so the second criterion was removed. The cost is
  a known limitation: a car held at a fixed angle across a lane with **no** yaw rate
  reads as passing through, not merging (`test_blind_spot.py` pins it).
- **Why two acute criteria and not one.** The contact prediction assumes a constant
  heading. For a fast lane change that is wrong in the dangerous direction: at 60 km/h
  and 20° the prediction has us crossing the next lane and leaving it again before the
  other car arrives, so the *sharper* manoeuvre produced *no* warning. The lane-entry
  criterion asks the question a driver would.
- **Four exclusions, all cheap and all load-bearing:**
  - `_is_plain_following` — same lane *and* parallel is a tailgater, not a lane-change
    conflict, and the side would be decided by noise. Either car turning ends it.
  - `_is_longitudinal_traffic` — ahead of our front bumper *and* in our lane is the
    car in front, and that stays true while we are running into it. This is the one
    the rule above does not cover: the moment of impact rotates both cars past the
    2° parallel test, so the lead car became "interesting" again, `contact_window`
    correctly found the outlines touching, and the side came out of a cross product
    that is essentially zero straight ahead — ±5 cm of lateral offset flips it, and
    the hold time then lit **both** sides (`known-issues.md` #52). Both halves are
    needed: "ahead" alone drops the car drawing level with us in the next lane,
    which is the case the acute stage exists for.
  - `MIN_ACUTE_OWN_SPEED_KMH` (1 km/h) — a blind spot warning says *do not go there
    now*, and a car that stands is not going anywhere. Below the floor our outline
    does not move over the horizon, so every predicted contact comes from the other
    car alone, and neither warning nor braking answers that. Standing at a light
    used to beep for everything that drove past (`known-issues.md` #53). **Level 1
    is deliberately unaffected** — that somebody sits in the mirror's blind spot is
    worth knowing exactly when the driver is about to pull out — and the level-3
    merge survives, because pulling into traffic means moving while you do it.
  - a contact window that starts at `-inf` — "overlapping since forever" is degenerate
    data, and a warning with no beginning could never end.
- **Braking is refused once we are already in their corridor** (`free_distance == 0`).
  They are behind us and faster: braking cannot take us out of their way, it only
  lengthens their approach and raises the speed they arrive with. This is the exact
  **opposite** of the cross traffic case, where we are the ones running into somebody.
- Output: `blind_spot_warning_changed` `{left, right, left_level, right_level}` on
  change; `needed_deceleration_update` every cycle.
- **Cost, measured with 40 cars:** 3 µs spread over a track, 402 µs in the
  (unrealistic) case where all 40 sit beside or behind us, against a 100 ms budget.
- **Open product question:** the corridor is 85 m long, which is lane-change-assist
  geometry rather than a blind spot. The relevance rule keeps far-away same-speed
  traffic quiet, but a fast approacher 80 m back does raise a blind-spot warning.
  Shortening `_CORRIDOR_MULTIPLIERS` is a decision for the author, not a bug fix.

## Cross Traffic Warning — `cross_traffic_warning.py`

Rectangle-vs-rectangle conflict prediction between our path and each other car's
(`assistance/path_conflict.py`), and — since WP12 — a braking demand.

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
- **Both vehicles are rectangles.** The ray intersection plus a size-derived arrival
  window it replaced was good enough for a warning but not for an intervention: an
  intervention has to know **where** the conflict starts, not only **when**. The TTC is
  now the first-contact time from `contact_window()` (a separating-axis test over both
  outlines, exact for rectangles at constant velocity), and the braking distance is
  `free_distance()` — how far our centre may still travel before our outline touches
  their travel corridor. Sizes still come from `park_distance_control.get_vehicle_size`.
- Thresholds on TTC by `cross_traffic_warning_distance`: early `3.5/3.0`,
  medium `2.5/1.5`, late `1.5/1.0` s (visual / acoustic). They are now times to
  **contact**, not to the meeting point of the two centres — about 0.3 s earlier for
  two normal cars, in the driver's favour.
- **The warning level has two sources, and the higher wins**: the contact time
  against the thresholds above, *and* the braking demand against
  `VISUAL_DEMAND_FRACTION` / `ACOUSTIC_DEMAND_FRACTION` of the engage threshold
  (0.4 / 0.75, i.e. 2.4 and 4.5 m/s²). The second one exists because the time
  thresholds cannot guarantee that a warning precedes an intervention: the
  stopping distance grows with v² and the contact time only with v. Measured in
  `simulation_tests` scenario 08 — accelerating hard towards a junction, the
  demand was already 6.25 m/s² while the contact was still 3.7 s away, so the
  driver got the brake with no warning in front of it. Tying the display to the
  same number that triggers the brake makes the order display → tone → brake
  true by construction.
- **No braking for a car overtaking us from behind** (`_overtaking_us`). 20° is
  the lower end of "crossing", and a car drawing level at that angle looks like
  cross traffic from here. Warning is fine; braking is not, for the reason in
  `control-intervention.md`'s table of sources — that geometry belongs to the
  blind spot warning.
- **The braking demand** is `v² / 2s` over the free distance, less `SAFETY_BUFFER_M`
  (1.0) and `v · REACTION_TIME_S` (0.2). Published every cycle with
  `source='cross_traffic'`; `EmergencyBrake` owns the 6.0 m/s² threshold and the
  actuation. Three properties that make it engage at the right moment and only then:
  - it is **self-limiting**: at 36 km/h it is 3.5 m/s² thirty metres out and 7.9 m/s²
    at twelve, so it crosses the threshold at the last point where a stop short of the
    other car's path is still possible;
  - it is **self-consistent under braking**: brake at the demanded rate and `v²/s` stays
    put, so the intervention does not collapse the moment it starts working;
  - it **needs no latch**: as our speed falls the predicted conflict resolves by itself,
    the demand goes to zero, and the brake is handed back — that is exactly the state
    "we braked enough to let them through".
- **No braking when there is nothing to brake for:** if we clear the junction first, if
  their path crosses ours behind us, or if they are more than `BRAKE_HORIZON_S` (4 s)
  away, the demand is 0.
- `_compute_side` uses the 2D cross product; the code is right for LFS's
  right-handed CCW system and the docstrings now say so (they used to claim Y grows
  south, which is what `known-issues.md` #16 was about). See `conventions.md` §1.
- Output: `cross_traffic_warning_changed` `{level, side}` on change;
  `needed_deceleration_update` every cycle.
- **Cost, measured with 40 cars:** 14 µs spread over a track, 61 µs with all 40 packed
  around us, against a 100 ms budget.

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
- **The tone stops after 1 s at a standstill** (`PDC_STANDSTILL_KMH` 0.1,
  `PDC_SILENCE_AFTER_S` 1.0) and comes back the moment the car moves. A parking aid
  reports an *approach*; once the car stands, the manoeuvre is over and the gap is
  the one the driver chose, so a continuous tone is only loud. Standing close behind
  somebody used to beep for as long as you sat there.
  - The **display stays** — that something is still there has not changed by
    stopping. Only the tone is dropped.
  - The second of debounce is for the direction changes in a parking manoeuvre: the
    tone must not break off at every zero crossing.
  - It travels as its own event, `pdc_beep_allowed`, not as an extra key in
    `pdc_changed` — that payload is a pure sensor dict and is read positionally
    (`events.md`).
- `get_vehicle_size` is also imported by FCW for car-length maths — keep it here.

## Auto Hold — `auto_hold.py`

Applies the handbrake when the car is stopped with the brake pressed.

- Trigger: `speed < 0.05 km/h and brake > 0.05` and the handbrake dash light is off.
- Actuation is a **global `pyautogui` keypress** of `user_handbrake_key`, read from
  the settings at press time, and only after `InputGuard.may_inject()` agrees —
  on track, no dialog/text entry, no Shift or Ctrl held, LFS in the foreground and
  OutGauge really describing our car (`ui.md` §1.4). Never press a key here without it.
- One key attempt per stopped/braking phase. Success and `auto_hold_active` require
  the handbrake dashboard light, not merely a queued key. After 1 s without that
  confirmation, one diagnostic asks the driver to check the handbrake key/axis.
- Releasing the brake, moving, leaving track, changing car/mode/key, or disabling
  resets the attempt. No repeated toggles while waiting or after a manual release.
- Wheel users with a handbrake axis are not converted to keys. This feature only
  supports an effective key binding; disk files may be stale until LFS exits.

## Adaptive Lights / Cop Mode — `adaptive_lights.py`

Three unrelated features share this system:

1. **Adaptive brake lights** — flashes the hazards at ~150 ms while decelerating
   harder than 8 m/s² (or brake > 0.85 above 10 km/h), not while reversing.
   "Reversing" is `misc.helpers.is_reversing`, i.e. a *signed modular* heading
   difference — the plain subtraction it used before switched the brake light off in
   one heading sector.
2. **High beam assist** — high beam unless a car is visible ahead (`distance < 250 m`,
   `speed > 1`, within ±15° cone). Gated by `high_beam_assist`. Two rules that make it
   a driver aid instead of an override:
   - **Lights off means hands off.** With neither low nor full beam on, the assist does
     nothing. It used to force the low beam on every cycle, so driving without lights
     was impossible.
   - **One command per state change.** It compares the desired beam against the one
     OutGauge reports *and* against what it last asked for, so an unchanged decision
     sends nothing, and a driver who dips again by hand is not overruled until the
     situation itself changes. Before, this was one `send_light_command` per cycle,
     ~10 InSim packets per second.
3. **Siren / strobe (cop roleplay)** — enabled only when the player name contains
   `[cop]`, `[tow]` or `[res]` **and** `cop_assistance` is on. The strobe is a 14-step
   light pattern advanced **every `STROBE_STEP_S` (0.1 s)**, not once per cycle, so its
   speed no longer follows `assistance_refresh_rate` (the cycle time is still the
   ceiling). Siren uses `SMALL_LCS`; the strobe uses `send_light_command`. Toggled by
   buttons 62/63 or the `$siren` / `$strobe` chat commands — **all four paths go through
   the one owner here** and are published as `siren_state_changed` /
   `strobe_state_changed`; the UI only draws (`ui.md` §2).
   `disable_siren()` switches the strobe's extra lights off and **nothing else** — it
   used to switch the low beam *on* as a side effect, for every player, on every track
   entry and every name change.

Everything data-driven here (brake light, high beam) is skipped while
`own_vehicle.is_local_driver` is false: OutGauge follows the camera but `SMALL_LCL`
always switches *our* car (`conventions.md` §5.2).

It is the only system that should be driving lights.

## Automatic Gearbox — `gearbox.py`

Shifts by injecting keypresses through the shared `KeyTapper` (`ui.md` §1.6) and
`InputGuard.may_inject()` (`ui.md` §1.4). The sequence is clutch down at 0 ms, gear key
at 100 ms, gear key up at 200 ms, clutch up at 300 ms — `CLUTCH_LEAD_S` / `SHIFT_HOLD_S` /
`CLUTCH_HOLD_S`. Those timings ran on the assistance thread until WP11, where they were
`pyautogui.PAUSE` and cost ~440 ms of a 100 ms budget per gear change; they are unchanged,
they simply run on the tapper's thread now. A refused shift is a shift that did **not**
happen: the cooldown does not start, so the next allowed cycle still shifts. The keys are
read from the settings at press time, so a rebind in the menu works without a restart.

- **Three sources for idle rpm, redline and gear count, in this order** (`_apply_known_values`):
  1. **The driver's own calibration**, `data/gearbox_calibrations.json`, keyed by car
     name. An explicit statement: once it exists it is used unchanged, and a later
     measurement does not move it.
  2. **`STOCK_PROFILES`** in `vehicles/car_profiles.py` — measured values for the LFS
     standard cars, so a car nobody has calibrated shifts on the first lap.
  3. **The learned profile**, measured from the OutGauge stream while the game runs
     (`vehicles/car_profiles.py`). The only source for mods, and it improves with every
     lap, so 2 and 3 are re-resolved **every cycle** rather than on car change — freezing
     them at car change would leave the first, worst estimate in place for the session.

  If nothing yields a rev range of at least `MIN_RPM_RANGE`, the gearbox stays inactive
  rather than shifting on invented numbers. A car with no entry never inherits the
  previous car's values.
- **Two cars-by-name policies, both in `vehicles/car_profiles.py`.** This is the one
  place where naming cars is deliberate rather than a mistake (`conventions.md` §4), and
  the failure direction is stated: a *mod* is not recognised by either list.
  - `NO_AUTOMATIC_BY_DEFAULT` — the single-seaters (`FBM`, `FOX`, `FO8`, `BF1`) are
    absent from the table and a *learned* profile does not arm them. On a formula car the
    driver wants the gears. Anyone who disagrees calibrates it, and that explicit
    calibration is honoured.
  - `NEVER_AUTOMATIC` — the `MRT` has a motorbike gearbox, which this shift logic does
    not describe at all. Checked before anything else, so even an existing calibration
    cannot arm it, and a calibration request is **refused out loud** ("Automatic Gearbox
    not available") rather than accepted and then ignored.
- **Every step is measured over its whole 12 s, never in the cycle it ends in.** This
  is the part that kept breaking in the field, and each variant looked like a different
  bug:
  - *idle* = the **median** of the samples at or above `ENGINE_RUNNING_MIN_RPM` (300).
    Not the minimum: LFS shuts a standing engine down and then reports `rpm 0`, which a
    minimum cannot defend itself against — it stored an idle speed of 0. Not the mean
    either: a blip of throttle would move it. The median is "the rpm that was there most
    of the time", and both the start-up transient and a blip are minorities among ~120
    samples. No sample with the engine running at all aborts the calibration.
  - *redline* = the **highest** rpm of the step. Letting off a moment early used to store
    idle speed as the redline (`step 1 ended - rpm 962` while the step's peak was 7481).
    A redline less than `MIN_RPM_RANGE` (1000) above idle is rejected rather than stored:
    it leaves an automatic that never shifts, with nothing to explain why.
  - *top gear* = the **highest gear engaged** during the step. LFS drops a standing car
    back into neutral, so reading the gear at the end produced `step 2 ended - gear 1`
    from a driver who had shifted all the way up.
- **Calibration UX**: the prompt, the remaining seconds and the value measured *so far*
  are published every cycle as `gearbox_calibration_state` and drawn in the calibration's
  own slot (`ui.md` §1.7) — deliberately not through the notification queue, which put the
  driver a whole step behind. It aborts on its own if the car moves or the camera leaves
  the own car; the same menu entry pressed again **cancels**. Finishing in neutral or
  reverse is rejected instead of storing "0 gears" and silently never shifting again.
- **`forward_gears` is the number of forward gears**, not the raw OutGauge gear index —
  one representation, displayed as it is stored. Files written by older builds carry
  the raw index under `max_gears` and are converted on load.
- **It stands down when LFS shifts by itself.** `PIF_AUTOGEARS` in the driver's help
  flags (`insim.md` §7, reached as `own_vehicle.data.lfs_auto_gears`) means LFS's own
  automatic gearbox is on, and two automatics on one crankshaft shift against each
  other. It blocks automatic shifts only; passive calibration remains available,
  including when the add-on toggle is off. The system stays scheduled to update
  availability and service calibration requests; the
  reason goes to the menu as `gearbox_availability` = `lfs_auto_gears` and the driver
  gets one notification if the add-on toggle is on. It follows the flag in both directions — SHIFT+G on track
  hands control back within a cycle. This is not an exotic case: SHIFT+G is a two-key
  shortcut and the recorded test scenarios were all driven with it on.
- **Every executed shift logs one line**, like every other actuator in this project:

  ```
  Gearbox shift up: gear 4, 6800 min-1, 90.0 km/h, throttle 1.00, brake 0.00.
  ```

  The gear is the one being left. It is not logging on `process()`'s success path
  (`AGENTS.md` §3) — a shift is a discrete intervention that takes the car out of
  the driver's hands, and AEB engage/release, the throttle cut and the brake-axis
  handover all log theirs. Without it the system was unverifiable in game: the
  `simulation_tests` tracer gets no OutGauge while the add-on holds port 30000,
  and MCI carries no gear, so a full live run on 2026-09-20 produced no evidence
  at all. The fields are the ones the two suspected faults are recognised by —
  several gears at constant speed, and an upshift under braking.

  **Verified live 2026-09-20** (FZ5, LA1, mouse/keyboard with SHIFT+G on manual,
  three stints, `15_gearbox_tests`): 26 shifts, no stand-down. 0 of 14 upshifts
  had any brake applied; no same-direction pair at near-constant speed; smallest
  gap between any two shifts 2 s, so the drivetrain gate held. Upshifts at
  6144–7502 min⁻¹ against a calibrated redline of 7478, downshifts at
  1848–3407 min⁻¹ of which 6 were under hard braking, which is the right way
  round.

- **The shift decision needs a closed, settled drivetrain.** `omega_engine =
  omega_wheel · i_gear · i_final` only holds with the clutch engaged; with it open the
  engine revs free against the throttle and sits on the limiter no matter which gear is
  in. So `_drivetrain_is_settled()` refuses to decide while `clutch > 0.05` and for
  `RPM_SETTLE_S` (0.25 s) after it closes. Without this the gearbox read *its own*
  clutch as "still too high a gear" and walked up the whole box. The clutch value comes from OutGauge, so it covers the driver's
  clutch and LFS's autoclutch as well as our own.
- **No upshift while braking** (`MAX_BRAKE_FOR_UPSHIFT`, 0.20). Upshifting under braking
  removes the engine braking and leaves the car in the wrong gear for the handback;
  during an emergency-brake intervention it did exactly that, 3 → 4 → 5 → 6.
- **Anti-hunting design** — read `_process_shifting`'s docstring before changing it:
  throttle-dependent shift points create a wide dead zone between the upshift threshold
  (`idle + range·(0.50 + 0.42·throttle)`) and the downshift threshold
  (`idle + range·(0.15 + 0.20·throttle)`), plus direction-dependent cooldowns
  (1.5 s before reversing an upshift, 0.8 s the other way, 0.4 s same direction) and a
  5-sample throttle average. The cooldowns are a *floor*, not the protection: at 0.4 s
  same-direction they are shorter than the clutch is open, which is why the settle gate
  above exists.
- Gear numbering follows OutGauge: `0` = reverse, `1` = neutral, `2` = 1st gear.

## Navigation — removed in WP10

There was a `navigation.py` with a `NavigationSystem`: Dijkstra route guidance over the
`junctions` graph in `track_data/*.json`, turn-by-turn maneuver detection from the
cross/dot product of the incoming and outgoing road vectors, and a notification 150 m
before a junction. It never ran — no `sat_nav` settings key existed, so `is_enabled()`
was always false — and WP6 stopped constructing it. WP10 deleted the file.

**If sat-nav is wanted, it is a new design, not a resurrection.** What the deleted
version got wrong, and what a new one has to do differently:

- dozens of `print()` per 100 ms cycle — use `logging`, and nothing per cycle;
- `_get_closest_road` walked every point of every road every cycle, for one car; the
  windowed search `AIDriver` now uses (`ai-traffic.md` §3) is the pattern to copy;
- it needed a settings key (`SettingsManager.known_keys`) before it could ever be
  enabled, and a menu entry before a user could find it.

The shipped route data still carries the `junctions` the graph was built from, and the
translated turn instructions are still in `misc/language.py`. `git log -- assistance/navigation.py`
has the original if it is ever wanted as a starting point.

## Chat commands — `chat_commands.py`

Not a `process()` system; it reacts to `message_received`. Only handles `IS_MSO`
packets with `UserType == MSO_PREFIX` (the InSim `Prefix` is `$`) whose sender matches
the local player name after stripping LFS colour/encoding markers.

Commands: `$help`, `$siren`, `$strobe`, `$fcw`, `$ctw`, `$autoh`, `$light`, `$highbeam`.
Add new ones to the `self._commands` dict and to `_cmd_help`'s text.

`check_tooltip()` is called from `AssistanceManager.process_all_systems` (outside the
`on_track` gate) and pushes a random translated tooltip every 360 s.

## AI Driver — `AI_Driver.py`

See `reference/ai-traffic.md`. Two things belong in every reader's head before touching
it: cars are adopted by `IS_NPL.PType` bit 1, never by name, and the nearest-point
search is windowed around the previous index — hand it `previous_index=` or it falls
back to scanning the whole route.

## Automatic Emergency Braking — `emergency_brake.py`

**Read `reference/control-intervention.md` before touching this or any other feature
that actuates the car.** It covers arbitration, handback, fail-safe behaviour, what LFS
actually accepts in each control mode, and the key-release trap. What follows is only
the shape of the system.

Turns a `needed_deceleration_update` into real braking. The warning systems stay pure
warning systems; everything that takes control away from the driver lives here, behind
`automatic_emergency_brake == 2` (0 off, 1 warn only, 2 warn and brake).

- **Three systems ask now** — forward collision, cross traffic and blind spot — and the
  demands are kept apart by `source` and reduced with `max`. See `events.md` for the
  ordering contract this creates (`aeb` runs last) and for why the collection is cleared
  on every pass.
- **The 10 km/h floor is about rear-ending, not about being hit.** A crossing or
  blind-spot demand engages from `MIN_SPEED_CROSSING_KMH` (3) upwards: a car creeping
  into a junction at 8 km/h is not performing a parking manoeuvre, and the energy in
  that crash belongs to the other vehicle.

- **Path selection:** by `own_vehicle.data.control_mode`, and the two paths are not
  interchangeable. Mouse and keyboard (`mouse_kb`) get `Controls/brake_key.py`, which
  injects the driver's own LFS brake key; wheel/joystick (`wheel_js`) gets
  `Controls/brake_axis.py`, a vJoy axis swapped in with `/axis` for the duration of the
  intervention, because LFS ignores keys for brake in that mode.
- **Arbitration:** we only ever *add* braking. The driver's input reaches LFS on its
  own path and is never reduced. `misc/physical_keys.py` separates what the hardware
  holds from what LFS believes, using `LLKHF_INJECTED`; without it running, the key
  path refuses to arm rather than risk releasing a key the driver is holding.
- **Binding:** `/key <user_brake_key> brake` is pushed once per session, after IS_NPL
  identifies the local driver and only in `mouse_kb`. LFS holds exactly one key per
  function, so this replaces whatever the driver had — it is their brake key we are
  writing, from the value they set in the menu.
- **Output value:** the key path is digital, so it presses or it does not. The axis path
  runs a feed-forward (`demand / 10 m/s²`, i.e. what a road car reaches on dry tarmac
  with the pedal down) plus a proportional correction on the deceleration actually
  achieved, floored at 0.2 so a flattering reading cannot lift the pedal mid-intervention.
- **Engagement:** demand ≥ 6.0 m/s² engages, < 3.0 m/s² for two consecutive cycles
  releases, plus floors at 10 km/h (do not engage) and 3 km/h (release), and a 10 s
  runaway cap. The key output is digital, so this is full braking or none; modulation
  waits for the analog path.
  **6.0 is this system's own floor, but a publisher can raise it, and FCW does:** it
  withholds its demand below level 3, so the rear-end engage point is really 7.5 m/s².
  That is deliberate and measured — see the FCW section — and it means the effective
  trigger is `max(6.0, the publisher's own announce threshold)`. Do not read the 6.0
  here as "the car brakes at 6.0".
  **The digital output is why that matters.** It brakes fully or not at all, so an
  intervention overshoots by about `1 − demand/achieved` of its braking distance —
  a quarter of it when a 7.5 m/s² demand is answered with ~9.7 m/s². Modulation
  (duty cycling, or the analog path) is what would let the engage point move earlier
  without the car stopping metres short.
- **Release paths:** every one of them, because a stranded press is the worst failure
  here — demand gone, guard refusal, control mode change, feature switched off
  mid-intervention (`is_enabled()` deliberately stays True while a press is
  outstanding), `state_data` reporting off-track, and `AssistanceManager.shutdown()`
  from `main.shutdown()`.
