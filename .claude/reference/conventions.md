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

**Reverse detection** (used by FCW and adaptive lights) — use
`misc.helpers.is_reversing(heading, direction)`:
```python
def heading_difference(heading, direction):   # signed, -32768…+32768
    return ((heading - direction + 32768) % 65536) - 32768
reversing = abs(heading_difference(heading, direction)) > 10000   # ≈ 55°
```
**Never subtract two heading words directly.** They are angles on a circle: `heading
100` and `direction 65500` are 0.75° apart, but the subtraction gives −65400. That read
as "reversing" and switched FCW off in one whole heading sector.
`Direction` is only meaningful while the car is moving, so gate on a minimum speed.

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

**Acceleration is measured, not assumed.** `Vehicle.update_position` derives
`acceleration = (Δ km/h / 3.6) / Δt` from the **real** time between two packets
(`time.monotonic`, or a `timestamp` the caller passes), and smooths it with an
exponential low-pass whose coefficient is `1 − exp(−Δt/τ)`, `τ = ACCEL_SMOOTHING_TAU_S`
(0.15 s) — so the filter behaves the same at any packet rate and lags reality by at most
that τ. Δt outside `MIN_SAMPLE_DT_S…MAX_SAMPLE_DT_S` (0.02…0.5 s) is a gap, not a
measurement: the filter resets and the value is 0 for that packet.

It used to be `(Δ km/h) * 2.778`, i.e. a hardcoded 100 ms step. The interval actually
comes from `settings['assistance_refresh_rate']` (`LFSConnector.connect` passes it as
the MCI `Interval`), so at 50 ms every acceleration was 2× too large and at 200 ms half
the truth — and acceleration feeds FCW's braking maths and the adaptive brake light.

## 4. Car identification — and why mods break lookup tables

Cars are identified by `CName`, a 4-byte field in `IS_NPL` (and `IS_SLC`). For the ~20
built-in cars it holds a fixed code: `XFG`, `XRG`, `UF1`, `FZ5`, `FXR`, …

**`CName`, `PName` and `IS_STA.Track` are decoded exactly once, at ingress.**
`VehicleManager._handle_player_joined` decodes `CName` with `latin-1` (byte-exact and
reversible, so it stays a safe dict key) and `PName` with
`lfs.text_encoding.decode_button_text` (player names carry LFS code-page escapes and
colour codes); `StateHandler` decodes `Track`. `Vehicle.data.cname` / `.pname` are
therefore **`str`**, with the raw bytes kept alongside in `.cname_bytes` / `.pname_bytes`.
Nothing downstream should do `str(cname)[2:-1]` any more — that repr arithmetic was the
source of the `"b'[COP] Name'"` cop-tag bug.

**But LFS also has vehicle mods**, selectable in the garage alongside the standard cars.
There are unlimited of them, they are downloaded per user, and the set changes over
time. For a mod, `CName` carries the mod's compressed skin/mod ID instead of a known
code — an arbitrary value this project has never seen.

Consequence: **any hardcoded table keyed on `CName` silently falls through to a default
for every modded car.** Two exist today:

| Table | Location | Fallback | Effect on a mod |
|---|---|---|---|
| `get_vehicle_size(cname)` → `(length, width)` | `assistance/park_distance_control.py` | `(4.5, 1.8)` | PDC sensor geometry is wrong for anything that is not a mid-size saloon — a bus or a kart gets saloon dimensions. **FCW no longer trusts it**: `ForwardCollisionWarning._vehicle_length` checks whether the `CName` is one the table really knows and otherwise uses `FALLBACK_VEHICLE_LENGTH_M` (5.0 m, the longest standard car), because for a warning threshold "too short" means "too late" |
| gearbox calibration | `data/gearbox_calibrations.json` | none | handled correctly: the file is keyed by `CName` and the user calibrates each car once, so mods work by construction |

The gearbox is the model to follow: **derive car-specific parameters at runtime instead
of tabulating them.**

When you need a vehicle parameter, prefer, in this order:

1. **Measure it at runtime** from data LFS already sends (OutGauge rpm/gear, OutSim
   per-wheel positions and forces, MCI motion) — works for every car including mods.
2. **Ask the user once and persist it**, keyed by `CName`, like the gearbox calibration.
3. **Table with an explicit, conservative fallback** — acceptable only when a wrong
   value is harmless. If the value feeds a warning threshold or a braking calculation,
   a wrong default is *not* harmless.

Never assume `CName` is one of the built-in codes, never index a dict with it without a
default, and never let an unknown `CName` raise.

`IS_MAL` / `TINY_MAL` list the mods a host allows, and `IS_SLC` reports when a
connection selects a car — both are unused here but are the hooks if mod handling ever
needs to be explicit.

## 5. Player identification (PLID) — whose car is this actually?

`PLID` (player ID) is the key almost everything in this project is indexed by: the
vehicle dict, route assignments, AI control, light commands. Getting "which PLID is
*ours*" wrong is subtle, because the wrong answer is usually still a **useful** answer.

### 5.1 Where PLIDs come from

| Source | Field | Meaning |
|---|---|---|
| `IS_MCI` / `CompCar` | `PLID` | one entry per car on track |
| `IS_NPL` | `PLID` + `UCID` + `PType` | a player joined the race **or left the pits** |
| `IS_PLL` | `PLID` | a player left |
| `IS_STA` | `ViewPLID` | **unique ID of the player currently being viewed** (0 = none) |
| **OutGauge** | `PLID` | **unique ID of the *viewed* player** — see below |

### 5.2 OutGauge reports the viewed car, not necessarily your car

`InSim.txt` defines `OutGaugePack.PLID` as *"Unique ID of viewed player"*. LFS sends
the dashboard data of **whatever car the camera is on**, which TAB cycles through
(offline: own car and the AI drivers; online: all players).

Consequences:

- **On track, driving your own car, OutGauge `PLID` is your PLID.** It is stored as
  `own_vehicle.viewed_plid` and is the fallback for `data.player_id` until `IS_NPL`
  has identified the local driver.
- **While spectating, or after pressing TAB, it is somebody else's PLID.** Since WP4
  that no longer repoints the whole `OwnVehicle`: `data.player_id` stays on
  `local_plid` and `own_vehicle.is_local_driver` goes False.
- The *gauge* fields (rpm, gear, pedals, dash lights, fuel) still follow the camera —
  that is **the desired behaviour** for the HUD. Do not "fix" that reflexively.
- Anything that **actuates** (auto-hold, gearbox, light commands, siren) acts on
  **your** car via `SMALL_LCL`/`SMALL_LCS`/keypresses. Gate it on
  `own_vehicle.is_local_driver`; shifting or braking on a spectated car's rpm is a
  real hazard.

`IS_STA.ViewPLID` carries the same information over InSim and is unpacked by pyinsim,
but is not read anywhere in this project.

### 5.3 Two additional conditions on OutGauge

Per `InSim.txt`: *"The user's car in multiplayer or the viewed car in single player or
single player replay can output information to a dashboard system **while viewed from
an internal view**."* And `OutGauge Mode` is `0 = off / 1 = driving / 2 = driving+replay`.

So OutGauge stops streaming entirely when:

- the camera is **not** an internal view — `IS_STA.InGameCam` other than `VIEW_DRIVER`
  (3) or `VIEW_CUSTOM` (4); chase (`VIEW_FOLLOW` 0), heli (1) and TV (2) produce nothing;
- you are **in the pits / garage**, not on track — which is why the own PLID cannot be
  established there;
- `OutGauge Mode` is 0 in `cfg.txt`.

Because `own_vehicle` is only ever updated from OutGauge, **every one of these silently
freezes all assistance** (`known-issues.md` #24, #29). The existing workaround is
`StateHandler.start_game_insim()`, which re-opens the OutGauge socket on track entry if
more than 30 s have passed; `AIDriver` sidesteps it with the notification *"Camera needs
to be on own vehicle."*

### 5.4 Determining your own PLID robustly

The camera-independent way is `IS_NPL`, which carries both:

- `UCID` — connection id, **0 = local**;
- `PType` — bit 0 female, **bit 1 = AI**, **bit 2 = remote**.

Your own player is the `IS_NPL` that is **neither AI (`PType & 2`) nor remote
(`PType & 4`)**; `UCID == 0` corroborates it. Note `UCID == 0` alone is *not* enough:
InSim.txt defines `UCID` 0 as **the host**, which is you in single player but somebody
else when you join a multiplayer host as a guest. `PType` covers both cases, so
`VehicleManager._consider_local_driver` scores candidates: not-AI-and-not-remote wins,
`UCID == 0` wins harder, and the first candidate keeps the title at equal score.

This works in the garage, in menus, and regardless of camera, and it distinguishes "the
car I drive" from "the car I am watching" — which OutGauge alone cannot.

### 5.4.1 What the code exposes

| Field | Where | Meaning |
|---|---|---|
| `own_vehicle.local_plid` | `OwnVehicle` | PLID of the local driver, from `IS_NPL`. `0` = not yet known |
| `own_vehicle.viewed_plid` | `OwnVehicle` | PLID OutGauge is currently describing — follows TAB |
| `own_vehicle.is_local_driver` | `OwnVehicle` | **gate every actuation on this.** True when the OutGauge data really is our car. Returns `True` while `local_plid` is still `0`, i.e. an unknown identity keeps the old behaviour rather than disabling features |
| `vehicle.data.ucid` / `.ptype` | `VehicleData` | raw `IS_NPL` identity |
| `vehicle.data.is_ai` | `VehicleData` | `PType` bit 1 — the authoritative AI flag |
| `vehicle.data.is_remote` | `VehicleData` | `PType` bit 2 |

`own_vehicle.data.player_id` follows `local_plid` once it is known, so pressing TAB no
longer repoints the whole object. `data.speed` is only taken from OutGauge while
`is_local_driver` holds — otherwise it stays the MCI value of our own car. The *gauge*
fields (`rpm`, `gear`, pedals, dash lights) deliberately keep following the camera,
because that is what the HUD is supposed to show.

Note `IS_NPL` is also sent when a player *leaves the pits*, not only on joining — do not
treat it as a one-shot "player created" event.

### 5.5 Do not detect AI drivers by name

`PType` bit 1 is the authoritative AI flag, exposed as `vehicle.data.is_ai`.
`AIDriver._is_local_ai_vehicle` reads it. Do not go back to a name substring test — the
old `b'AI' in pname` matched any human called MAIK, RAID or CAIN and handed their car to
the traffic controller.

## 6. Performance rules for the hot path

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
- `print()` inside `process()` or a packet handler. Every system logs through
  `logging.getLogger(__name__)`; the last offenders (`AI_Driver`, `navigation.py`) went
  in WP10.
- Allocate polygons per vehicle per cycle without a distance pre-filter
  (`blind_spot_warning.py` currently does).
- Do file I/O, `winsound`, `time.sleep`, or anything blocking.
- Scan an entire route/layout dataset per cycle per vehicle without an index.

If a change adds measurable per-cycle cost, state the cost and the worst-case vehicle
count in the code comment.

**The `vehicles_updated` payload is a snapshot, not the live dict.** `VehicleManager`
builds a fresh dict per MCI frame and swaps each `VehicleData` object rather than
mutating it, so iterating it on a worker thread is safe. Cost: one `copy.copy` of a flat
dataclass per vehicle per cycle (<40 µs at 40 cars). `own_vehicle` has no such
guarantee — bind `data = own_vehicle.data` once instead of re-reading it per line.

## 7. Physics expectations

LFS is a full simulation: tyre load sensitivity, Kamm circle, per-wheel slip, brake
balance, aero. Assistance logic must be defensible in those terms.

- Braking calculations must state their assumed deceleration limit and where it comes
  from. `collision_warning.py` uses closed-form constant-deceleration kinematics with
  an explicit `SAFETY_BUFFER` and a 0.2 s reaction-time term — follow that pattern. It
  computes the **required** deceleration and never assumes a grip limit; the warning
  thresholds are where the comparison against what a road tyre can actually do happens.
- A "required deceleration" is a magnitude: return 0 when no braking is needed rather
  than the absolute value of a positive (accelerate-away) result.
- Distinguish *reachable* deceleration (grip-limited, ~8–11 m/s² on road tyres, less
  in a corner because lateral grip is already consuming the friction circle) from
  *requested* deceleration.
- Warning thresholds should be expressed as required deceleration or time-to-collision,
  not as raw distances. FCW uses required deceleration; CTW uses TTC.
- OutSim (`OutSimPack`) can supply per-wheel data and G-forces if a system needs real
  slip/friction information — it is connected but currently unused.

## 8. Code style

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
