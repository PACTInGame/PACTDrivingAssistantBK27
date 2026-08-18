# Testing concept

**Status: not implemented.** There are currently zero automated tests. `test.py` in the
project root is a live LFS capture script, not a test (`known-issues.md` #20).

This file is the proposed target design. Update it as tests actually land.

## Guiding constraint

Tests must run **without LFS, without a GUI, and without a network** — otherwise they
will never be run. That rules out end-to-end testing of the real thing, so the strategy
is to push as much logic as possible behind pure functions and event boundaries, and to
replay recorded packet data for everything else.

Tooling: `pytest`. Layout:

```
tests/
  conftest.py            fixtures: EventBus, fake settings, vehicle factories
  fixtures/
    mci_*.bin            recorded raw InSim packets
    outgauge_*.bin
    session_*.jsonl      recorded (timestamp, event, payload) traces
  test_geometry.py
  test_packets.py
  test_systems_*.py
  test_replay.py
  test_performance.py
```

## Layer 1 — pure functions (highest value, start here)

No mocks needed, no LFS. These are already pure or nearly so:

| Target | What to assert |
|---|---|
| `misc/helpers.py` — `calc_polygon_points`, `point_in_rectangle` | known points in/out of rotated rectangles; degenerate rectangles |
| `vehicles/vehicle.py` — `update_distance_to_player`, `update_angle_to_player` | metre conversion, angle 0 = straight ahead, wraparound at 0/360 |
| `assistance/AI_Driver.py` — `calculate_angle`, `calculate_angle_meters`, `analyze_upcoming_track`, `calculate_feedforward_steering`, `calculate_feedforward_throttle_brake`, `get_next_points_for_distance` | straight line → curvature 0; known arc → known curvature; clamping at ±45°; wraparound on closed loops |
| `assistance/cross_traffic_warning.py` — `_direction_vector`, `_find_intersection`, `_compute_side` | perpendicular paths intersect at the expected point; parallel → `None`; intersection behind → `None`; **left/right sign convention** (this is the one the wrong comment threatens) |
| `assistance/collision_warning.py` — `_calculate_needed_braking` | closing on a stationary car at known distance/speed → the physically correct deceleration; slower-and-not-braking lead → 0; inside the buffer → panic value |
| `misc/spacial_hash_grid.py` — `point_in_polygon`, `polygon_overlap`, insert/query/remove | overlapping and touching polygons; objects spanning several cells |
| `misc/language.py` | every key has all 8 languages; unknown key falls back to English |
| `core/settings_manager.py` | unknown key → default; corrupted JSON → defaults; round-trip save/load |

A cheap high-value test: assert that **every** `AssistanceSystem` name passed to
`super().__init__` exists in `SettingsManager._defaults`. That single test would have
caught `sat_nav` (`known-issues.md` #18).

## Layer 2 — systems against a real EventBus

`process(own_vehicle, vehicles)` takes plain objects and emits events. Build fake
`OwnVehicle` / `Vehicle` instances, subscribe a recorder to the bus, call `process()`,
assert the emitted events.

```python
def test_fcw_warns_on_stationary_car_ahead(bus, settings):
    seen = []
    bus.subscribe('collision_warning_changed', seen.append)
    fcw = ForwardCollisionWarning(bus, settings)
    own   = make_own_vehicle(speed=80, x=0, y=0, heading=0)
    other = make_vehicle(plid=2, speed=0, x=0, y=30*65536)
    fcw.process(own, {2: other})
    assert seen[-1]['level'] >= 2
```

Cover per system: the disabled path, the below-threshold path, each warning level, and
the **emit-only-on-change** contract (calling `process` twice with identical input must
emit once).

Systems that inject keys (`AutoHold`, `Gearbox`) need `pyautogui` patched — and the
tests should assert the guards: no keypress while `dialog` or `text_entry` is active.

## Layer 3 — packet round-trips (`pyinsim/`)

Byte-exact tests against `C:\LFS\docs\InSim.txt`:

- `pack()` output length equals `Size * 4` for every outgoing packet type.
- `IS_AIC` with n inputs → `Size == 1 + n`; more than `AIC_MAX_INPUTS` raises.
- `unpack()` of a recorded `IS_MCI` yields the expected `NumC` and per-car fields.
- `IS_MCI` with more than 16 cars arrives as several packets — assert the reassembly
  logic in `VehicleManager` handles the split (this is `known-issues.md` #6).
- `OutGaugePack` / `OutSimPack` accept both documented packet sizes.

## Layer 4 — replay harness (the big win)

Add an opt-in recorder to `LFSConnector` that appends every inbound packet (with a
timestamp) to a file. A replay driver then feeds a recorded session through the real
`VehicleManager` → `AssistanceManager` pipeline at accelerated speed, with the
`ThreadManager` replaced by a deterministic clock.

This gives regression tests over real driving situations — "in this recorded overtake,
no false blind-spot warning is emitted" — which no synthetic fixture can express. It is
also the only realistic way to test the AI traffic controller.

Record at minimum: one city lap on SO with AI traffic, one parking manoeuvre, one
motorway approach to a stopped car, one junction crossing.

## Layer 5 — performance regression

The real-time budget is the actual product requirement, so guard it:

```python
def test_assistance_cycle_within_budget(benchmark_scene_40_cars):
    t = timeit(lambda: manager.process_all_systems(), number=100) / 100
    assert t < 0.030   # 30 ms of a 100 ms budget on CI hardware
```

Run per system as well, so a regression names its culprit. Note CI hardware differs
from the target laptop — treat the number as a relative regression guard, not an
absolute guarantee.

## Not worth automating

- The Tkinter setup wizard, `MapBuilder.debug_plot`, `winsound`/`pygame` playback,
  vJoy — verify manually.
- Anything requiring LFS to actually render. Manual test checklist instead: fresh
  install → wizard → join track → each menu → each assistance system → leave track →
  rejoin (checks button cleanup and state reset).
