# AI traffic, route data and MapBuilder

Drives LFS's built-in AI cars along recorded city routes so that maps like South City
feel like a living town. Implemented in `assistance/AI_Driver.py` on top of
`AI_Control.py` (the `IS_AIC` wrapper) with map data in `track_data/*.json`.

## 1. Controlling an LFS AI car — `IS_AIC` / `AI_Control.py`

`IS_AIC` injects raw driver inputs into an AI car identified by `PLID`. Up to
`AIC_MAX_INPUTS = 20` `AIInputVal(Input, Time, Value)` entries per packet.

`AICarController` (`AI_Control.py`) wraps this:

```python
controller.control_ai(plid, AIControlState(throttle=60, brake=0, steer=-15))
controller.stop_ai_control(plid)          # hand the car back to the built-in AI
controller.reset_ai_controls(plid)
controller.request_ai_info(plid, repeat_interval=100)   # → IS_AII every 100 ms
controller.bind_ai_info_handler(plid, callback)         # callback(aii)
```

- `AIControlState` fields are `Optional`; **only non-`None` fields are sent**, so you
  can send partial updates. Analog values accept 0–100 % (steer: −100…+100) and are
  normalised to 0–65535 (steer centre 32768) by `_normalize_analog`.
- `IS_AII` returns physics for one AI car: `OSData` (angular velocity, heading/pitch/
  roll, acceleration, velocity, position), `Flags`, `Gear`, `RPM`, `ShowLights`.
- **Once you send `IS_AIC` to a car, you own it** — it will not steer, shift or restart
  its engine by itself. `AIDriver` therefore also handles gear shifting and stall
  recovery from `IS_AII`.
- `AI_Cheatsheet.py` is a stale near-copy of `AI_Control.py` (its `_normalize_analog`
  lacks the clamping). **Do not edit or import it.**

## 2. `AIDriver` state machine

`STATE_INACTIVE → STATE_ACTIVE → STATE_STOPPING → STATE_INACTIVE`

- **Start** (`ai_traffic_start`, from the menu) validates that
  `track_data/track_data_XX.json` exists and that the current track is in
  `ALLOWED_TRACKS = {b'BL1X', b'SO7', b'KY1X'}`. On mismatch it emits a translated
  error plus a track-specific hint from `TRACK_LAYOUT_HINTS` (e.g. *"Select City"* for
  SO). On success it sends `/axload AI_Traffic` and `/restart` to LFS, loads routes and
  markers, and goes active.
- **Stop** brakes every controlled car at 100 % for `STOP_BRAKE_CYCLES = 20` (2 s),
  then calls `stop_ai_control` on each and clears all per-vehicle state.
- A track change (`state_data`) forces a stop and drops the loaded routes.

Cars are adopted only if their player name contains `AI` (`_is_local_ai_vehicle`) — LFS
names its local AI drivers `AI 1`, `AI 2`, …. The player's own car is included as a
candidate because the camera may be attached to an AI car.

Because `IS_AII` requests are lost when the map reloads, `AIDriver` re-issues
`request_ai_info` for any car silent for more than `AI_INFO_TIMEOUT = 2.0` s.

## 3. Control law (feedforward, per car per cycle)

```
closest_index      = nearest route point to the car
lookahead_dist     = clamp(15 + (speed − 10)·0.5, 15, 40)   metres
upcoming_points    = points covering lookahead_dist (≥5 points)
curvature, target  = analyze_upcoming_track(upcoming_points)
                     target = weighted average of points 1,2,3 (25/50/25 %)
long_straight      = curvature over the next 120 m < CURVATURE_THRESHOLD

target_speed       = (STRAIGHT_SPEED if long_straight else BASE_SPEED)
                     − max(0, (curvature − CURVATURE_THRESHOLD)·1500), floored at MIN_SPEED
target_speed       = min(target_speed, collision-avoidance limit)

steer              = clamp(target_angle, ±45°) / 45° · 100
throttle/brake     = proportional on (target_speed − speed), gain 3.0, throttle capped at 60 %
```

Tuning constants (all class attributes on `AIDriver`, all commented with units):
`BASE_SPEED 70`, `STRAIGHT_SPEED 107`, `MIN_SPEED 22` km/h,
`STRAIGHT_LOOKAHEAD_DIST 120` m, `CURVATURE_THRESHOLD 0.004`,
`MAX_STEERING_ANGLE 45°`, `SPEED_GAIN 3.0`, `MAX_THROTTLE 60`.

**Smoothing:** throttle and brake are first-order filtered (`1/10` and `1/2` per cycle).
**Steering is intentionally not smoothed** — smoothing caused overshoot and oscillation.

**Collision avoidance:** a ±12° forward cone (`CA_CONE_HALF_ANGLE`) out to
`CA_DETECTION_DISTANCE = 50 m`, checked against the player's car and all other
controlled AI cars. Allowed speed interpolates linearly from
`CA_MAX_SPEED_AT_LIMIT = 70` km/h at 50 m down to 0 at
`CA_EMERGENCY_DISTANCE = 10 m`, below which it full-brakes.
`_calculate_following_speed` is deliberately simple and is the intended place to drop
in a TTC-based model.

**Gear shifting and stalls** happen in `monitor_ai(aii)`, driven by `IS_AII`, not by
`process()`. It shifts up above 3600 rpm (below 6th) and down below 1700 rpm (above
2nd), and turns the ignition on below 300 rpm. A shift command must be released before
it can be sent again, so `_shift_pending` forces a `False` cycle between every `True` —
this both prevents stuck inputs and guarantees a failed shift is retried. Net effect: a
shift can occur at most every other cycle.

**Markers** (`_process_markers`) trigger within `MARKER_TRIGGER_DISTANCE = 3 m` and are
locked out until the car is `MARKER_COOLDOWN_DISTANCE = 8 m` away again:
- `stop_line` → state machine `idle → braking (50 %) → stopped (hold 5 cycles) → idle`
- `arrow_left` / `arrow_right` → indicator on for `INDICATOR_DURATION_CYCLES = 50` (5 s),
  then `IndicatorMode.CANCEL`

**Performance note:** `get_closest_index_on_route` scans the *entire* route path for
every controlled car every cycle, and `analyze_upcoming_track` is called twice per car.
With many cars on a long route this is the heaviest loop in the project. Caching the
previous index and searching a local window would fix it — see `known-issues.md` #9.

## 4. `track_data/*.json` format

One file per track prefix: `track_data_BL.json`, `track_data_KY.json`,
`track_data_SO.json`. **All positions are in metres**, `[x, y, z]`.

```jsonc
{
  "metadata": { ... },
  "roads": [
    { "road_id": 32,          // == the LFS layout object index used to draw this road
      "point_count": 148,
      "closed_loop": true,
      "inverted": false,      // if true, AIDriver reverses the path on load
      "path": [[x, y, z], ...] }   // ordered centreline points
  ],
  "junctions": [
    { "location": [x, y, z], "connected_roads": [32, 20] }
  ],
  "markers": [
    { "type": "stop_line" | "arrow_left" | "arrow_right", "position": [x, y, z] }
  ]
}
```

`roads` and `markers` are consumed by `AIDriver`; `roads` and `junctions` are consumed
by `NavigationSystem` (which builds the Dijkstra graph from them).

## 5. Generating map data — `MapBuilder.py` + `test.py`

The map is authored **inside LFS's layout editor**: you place objects along each road,
using a *different object type per road* — the object's `Index` becomes the `road_id`.
Marker object indices are fixed: **4 = stop line, 8 = arrow left, 11 = arrow right**.

Capture and build:

1. Load the layout in LFS, then run `test.py` (a capture script, **not** a unit test).
   It opens InSim, requests `TINY_AXM`, converts objects to metres
   (`X/16`, `Y/16`, `Zbyte/4`), skips index 184, and once ≥ 318 objects have arrived
   runs `MapGenerator`.
2. `MapGenerator.process()`:
   - `_extract_markers` pulls out indices 4/8/11.
   - `_build_ordered_roads` groups the remaining points by object index and orders each
     group with a greedy nearest-neighbour walk, then corrects the classic greedy
     failure by cutting the path at its largest internal gap.
   - `_handle_closed_loops` closes a road into a loop if the start–end gap is under
     `loop_threshold = 50 m`.
   - `_find_junctions` uses a KDTree to find points of *different* roads within
     `junction_radius = 3 m` and records one junction per road pair.
3. `save_to_json()` writes the file; `debug_plot()` renders it with matplotlib.

The `.lyt` files in `layouts/` (`BL1X_AI_Traffic.lyt`, `KY1X_AI_Traffic.lyt`,
`SO7_AI_Traffic.lyt`) are the authored layouts; the setup wizard copies them into
`<LFS>/data/layout/`. Adding a new track means: author a layout → capture with
`test.py` → commit `track_data_XX.json` and the `.lyt` → add the track code to
`AIDriver.ALLOWED_TRACKS` and a hint to `TRACK_LAYOUT_HINTS`.

`MapBuilder`/`test.py` are **offline tools**; they import `numpy`, `scipy` and
`matplotlib`, which the runtime app does not need in its hot path.
