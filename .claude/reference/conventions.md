# Units, coordinates and code conventions

**Read this before writing any geometry, distance, speed or angle code.** Mixing up
these units is the single most common bug class in this project. Authoritative source:
`C:\LFS\docs\InSim.txt` (`struct CompCar`, `struct ObjectInfo`) and `OutSimPack.txt`.

---

## 1. Coordinate system

LFS world axes: **X = east, Y = north, Z = up.** Right-handed, headings measured
**anticlockwise** from the +Y axis.

| Source | Field | Unit | Convert to metres |
|---|---|---|---|
| `IS_MCI` / `CompCar` | `X`, `Y`, `Z` | 1/65536 m (int) | `v / 65536` |
| `IS_AXM` / `ObjectInfo` | `X`, `Y` | 1/16 m (short) | `v / 16` |
| `IS_AXM` / `ObjectInfo` | `Zbyte` | 1/4 m (byte) | `v / 4` |
| OutSim `Pos` | 3 ints | 1/65536 m | `v / 65536` |
| `track_data/*.json` | all positions | **metres** | — |

So AXM→MCI scale conversion is `* 4096` (65536/16). You will see this literal in
`assistance/park_distance_control.py:create_rectangle_for_object`.

**Never compare a raw MCI coordinate with a `track_data` coordinate.** Convert first.
`AI_Driver` does this consistently as `vehicle.data.x / 65536`.

## 2. Headings and angles

| Source | Field | Encoding |
|---|---|---|
| `CompCar` | `Heading` | word, 0 = +Y (north), 32768 = 180°, **anticlockwise** |
| `CompCar` | `Direction` | word, same encoding — direction of *motion*, valid only if Speed > 0 |
| `CompCar` | `AngVel` | signed short, 16384 = 360 °/s anticlockwise |
| `ObjectInfo` | `Heading` | byte, 0…255 = 0…360° |

The idiom used everywhere in this codebase:

```python
angle_of_car = (heading + 16384) / 182.05     # → standard math degrees, 0 = +X axis, CCW
```
- `182.05 ≈ 65536/360` converts LFS units to degrees.
- `+16384` (= +90°) rotates from "0 = +Y" to "0 = +X", so the result feeds straight
  into `math.cos` / `math.sin`. This is what `misc.helpers.calc_polygon_points` expects.

For objects: `angle_of_obj = (heading * 360 / 256 + 90) % 360` — same target frame.

**Reverse detection** (used by FCW and adaptive lights):
```python
reversing = abs(heading - direction) > 10000
```

**`Vehicle.data.angle_to_player`** (from `update_angle_to_player`): 0…360°, where
**0/360 = directly ahead** of the observer. `AI_Driver.calculate_angle` /
`calculate_angle_meters` return the same quantity remapped to **−180…+180**, 0 = ahead,
which is more convenient for steering.

Note `calculate_angle` divides `own_x/own_y` by 65536 but expects the *target* already
in metres; `calculate_angle_meters` expects both in metres. Picking the wrong one is a
silent 65536× error.

## 3. Speed, distance, acceleration

| Quantity | Source | Unit as received | Stored as |
|---|---|---|---|
| `CompCar.Speed` | MCI | word, 32768 = 100 m/s | km/h via `Speed / 91.02` |
| OutGauge `Speed` | UDP | m/s (float) | km/h via `* 3.6` |
| `Vehicle.data.speed` | — | — | **km/h** (always) |
| `Vehicle.data.distance_to_player` | computed | — | **metres** |
| `Vehicle.data.acceleration` | computed | — | **m/s²** (signed; negative = braking) |
| OutGauge pedals (`Throttle`, `Brake`, `Clutch`) | UDP | **0.0 … 1.0**, not percent | as-is |
| `IS_AIC` analog inputs | sent | 0 … 65535 (steer: 1 left, 32768 centre, 65535 right) | `AICarController` accepts 0–100 % / −100…+100 and normalises |

Conversions: `km/h → m/s` is `* 0.277778`; `m/s → km/h` is `* 3.6`.

**Acceleration has a hidden timing assumption.** `Vehicle.update_position` computes
`acceleration = (speed_kmh - previous_speed_kmh) * 2.778`, where `2.778 = 1/(3.6 · 0.1)`.
It is only correct while the MCI interval is exactly **100 ms**. That interval comes
from `settings['assistance_refresh_rate']` (`LFSConnector.connect` passes it as
`Interval`), so changing the refresh rate to 50 or 200 ms silently scales every
acceleration value by 2×. Fix this by measuring real Δt if you touch it.

## 4. Performance rules for the hot path

The hot path is: every InSim packet handler, `AssistanceSystem.process()`,
`UIManager.update_hud()`, and everything they emit into. Budget per assistance cycle is
the refresh rate (default **100 ms**) minus headroom, on a mid-range laptop, with up to
~40 vehicles.

Do:
- Precompute constants in `__init__`; hoist `settings.get()` out of inner loops.
- Compare **squared** distances (`d_sq < r*r`) instead of calling `math.sqrt`.
- Reject early: a cheap distance/bounding check before any polygon or trig work.
- Reuse containers; prefer tuples and plain floats over object graphs.
- Use `misc.spacial_hash_grid.SpatialHashGrid` for broad-phase against layout objects.

Do not:
- `print()` inside `process()` or a packet handler. (`navigation.py` violates this
  heavily — dozens of lines per cycle.)
- Allocate polygons per vehicle per cycle without a distance pre-filter
  (`blind_spot_warning.py` currently does).
- Do file I/O, `winsound`, `time.sleep`, or anything blocking.
- Scan an entire route/layout dataset per cycle per vehicle without an index.

If a change adds measurable per-cycle cost, state the cost and the worst-case vehicle
count in the code comment.

## 5. Physics expectations

LFS is a full simulation: tyre load sensitivity, Kamm circle, per-wheel slip, brake
balance, aero. Assistance logic must be defensible in those terms.

- Braking calculations must state their assumed deceleration limit and where it comes
  from. `collision_warning.py` uses closed-form constant-deceleration kinematics with
  an explicit `SAFETY_BUFFER` and a 0.2 s reaction-time term — follow that pattern.
- Distinguish *reachable* deceleration (grip-limited, ~8–11 m/s² on road tyres, less
  in a corner because lateral grip is already consuming the friction circle) from
  *requested* deceleration.
- Warning thresholds should be expressed as required deceleration or time-to-collision,
  not as raw distances. FCW uses required deceleration; CTW uses TTC.
- OutSim (`OutSimPack`) can supply per-wheel data and G-forces if a system needs real
  slip/friction information — it is connected but currently unused.

## 6. Code style

- Follow the surrounding file: German docstrings/comments in the older modules,
  English in the newer ones. Do not mass-translate.
- Type hints on public methods; `Dict`/`Optional` from `typing` (matching existing code).
- Tuning constants go in `UPPER_CASE` class attributes with a comment giving units and
  rationale — see `AI_Driver` and `Gearbox` for the established pattern.
- Section headers use `# ─── Name ─────` box-drawing separators in the newer modules.
- Settings keys are `snake_case` strings and must be registered in
  `SettingsManager._defaults`.
- User-facing strings go through `LanguageManager.get(key, lang)`; never hardcode
  German or English into the UI.
- LFS colour codes (`^0`–`^7`) prefix strings, not separate arguments. See `ui.md`.
