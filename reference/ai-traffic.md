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

## 2. `AIDriver` state machine

`STATE_INACTIVE → STATE_ACTIVE → STATE_STOPPING → STATE_INACTIVE`

- **Start** (`ai_traffic_start`, from the menu) validates, in this order: that
  `track_data/track_data_XX.json` exists, that the current track (a decoded `str`) is in
  `ALLOWED_TRACKS = {'BL1X', 'SO7', 'KY1X'}`, and that the route file actually parses.
  On mismatch it emits a translated error plus a track-specific hint from
  `TRACK_LAYOUT_HINTS` (e.g. *"Select City"* for SO). **Only then** does it send
  `/axload AI_Traffic` and `/restart` to LFS and go active — those two commands throw
  away whatever layout the driver had loaded, so nothing is sent until the run is
  certain to work.
- Because that pair of commands cannot be undone, the menu asks first: the first click
  on the toggle only arms the start (`^3Confirm: restart race` plus a notification
  saying what will happen), the second executes it. Leaving the menu, reopening it or a
  SHIFT+B repaint cancels the confirmation (`MenuSystem.ai_traffic_confirm_pending`).
- **Stop** brakes every controlled car at 100 % for `STOP_BRAKE_CYCLES = 20` (2 s),
  then calls `stop_ai_control` on each and clears all per-vehicle state.
- A track change (`state_data`) forces a stop and drops the loaded routes. A packet
  with no usable track name is "unknown", not a change — otherwise one malformed
  `IS_STA` would stop running traffic. `self.routes` is *rebound*, never mutated, and
  `_process_active` binds it once per pass, because that handler runs on the packet
  thread while `process()` runs on a worker (`conventions.md` §6).

Cars are adopted only when LFS itself marks them as AI: `IS_NPL.PType` bit 1, exposed as
`vehicle.data.is_ai` (`_is_local_ai_vehicle`, `conventions.md` §5.5). The player's own
car is included as a candidate because the camera may be attached to an AI car.

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

**Route search (the hot part).** `get_closest_index_on_route(x, y, z, route)` still
scans the whole path when called with three coordinates and a route, which is what
`_find_closest_route` and any external caller does. `AIDriver` instead passes
`previous_index=` — the index that car had last cycle, kept in `_route_index` — and the
search then only looks at `ROUTE_SEARCH_WINDOW = 20` points either side of it. The
window wraps when the road is a `closed_loop`. It is thrown away and the whole path
scanned again when the car cannot be inside it: the best point found is further than
`ROUTE_RESYNC_DISTANCE_M = 25 m` away (teleport, respawn, `/restart`), or it sits on the
window's edge, where something nearer may lie just outside. So the answer equals the old
full scan whenever the car is on its route, and being wrong costs one extra scan, never
a wrong index. All comparisons are squared distances.

The 120 m long-straight analysis — the second `analyze_upcoming_track` call, over ~25
points — depends only on `(route_id, index)`, both fixed while the route is loaded, so
it is cached in `_straight_cache` and computed at most once per route point instead of
once per car per cycle. The cache is dropped when routes are loaded or dropped; it is
bounded by the number of points in the track (~2500 booleans).

Cost per controlled car per cycle: 41 squared-distance comparisons, one curvature
analysis over the 5–8 points of the speed-dependent lookahead, one dict lookup, the
marker scan, and one pass over the other controlled cars for collision avoidance.
Measured on a cloud container with 20 cars on a synthetic 2000-point route: 0.66 ms per
`_process_active` pass, against 5.3 ms for the pre-WP10 behaviour.
`tests/test_ai_traffic.py` guards both halves — no full scan in a steady-state cycle,
and the ratio against a full-scan baseline.

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

`roads` and `markers` are consumed by `AIDriver`. `junctions` is written by
`MapBuilder` and read by nothing since `NavigationSystem` was deleted (`systems.md`);
the shipped files carry 0–1 of them anyway.

**The file is treated as hostile input.** `load_routes_from_file` validates the whole
shape and raises `RouteDataError` — naming the file and the offending element — for a
missing or unreadable file, invalid JSON, a non-object top level, a `roads` entry
without an integer `road_id` or with a duplicate one, a `path` shorter than two points,
or a point that is not three numbers. Marker types it does not know are logged and
skipped, not fatal. Positions come back as tuples of floats, converted once. This
matters because the load happens inside the InSim packet handler that delivered the
menu click: `AIDriver._load_routes` catches the error, logs it and returns `False`, and
the start is refused with a notification instead of an exception in the packet loop.

## 5. Generating map data — `MapBuilder.py` + `tools/capture_layout.py`

The map is authored **inside LFS's layout editor**: you place objects along each road,
using a *different object type per road* — the object's `Index` becomes the `road_id`.
Marker object indices are fixed: **4 = stop line, 8 = arrow left, 11 = arrow right**.

Capture and build:

1. Load the layout in LFS, then run `python tools/capture_layout.py` from the project
   root (a capture script, **not** a unit test — it used to be `test.py`).
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
`tools/capture_layout.py` → commit `track_data_XX.json` and the `.lyt` → add the track
code to `AIDriver.ALLOWED_TRACKS` and a hint to `TRACK_LAYOUT_HINTS`.

`MapBuilder.py` and `tools/capture_layout.py` are **offline tools**; they import
`numpy`, `scipy` and `matplotlib`, which the runtime app does not need in its hot path.
Neither is imported by the app, and `pytest.ini`'s `testpaths = tests` keeps the capture
script out of the test run.
