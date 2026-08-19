# Testing

**Status: harness in place.** `python -m pytest` from the project root runs the suite on
Linux, Windows or macOS with only `requirements-dev.txt` installed (`pytest`, `psutil`,
`shapely`, `numpy`). No LFS, no display, no sound device, no socket.

`test.py` in the project root is still a live LFS capture script, not a test
(`known-issues.md` #20); `pytest.ini` sets `testpaths = tests`, so it is never collected.

## How to run

```
pip install -r requirements-dev.txt
python -m pytest                # whole suite
python -m pytest tests/test_language.py -k latin
```

`pytest.ini` also sets `pythonpath = .`, so tests import the app packages
(`assistance.…`, `core.…`) exactly the way `main.py` does.

## Portability: the platform shim

The app is Windows-only at runtime, but every module must *import* anywhere, otherwise
nothing below it can be tested. `misc/platform_shim.py` owns that: `pyautogui`,
`winsound`, `pynput`, `pygame`, `tkinter` and `misc/vjoy.py` are imported lazily through
`get_keyboard()`, `get_sound()`, `get_input_listener()`, `get_audio()`, `get_tkinter()`
and `get_vjoy()`.

- On Windows the accessor returns the **real** module, so behaviour is unchanged.
- Elsewhere it returns a `NullModule`: attribute access and calls are absorbed, recorded
  and never raise. `platform_shim.recorded_calls()` returns
  `[(dotted_path, args, kwargs), …]`, which is how "does this system inject a key here?"
  is asserted without a keyboard. `reset_recorded_calls()` clears it.
- A `NullModule` is falsy, so a call site that must degrade can ask `if not get_tkinter()`.

`tests/test_imports.py` enforces both halves: every app module imports, and no module
outside the shim imports one of those packages at module level.

## What exists today

```
tests/
  conftest.py              fixtures (below)
  test_imports.py          every module imports; no module-level Windows imports
  test_platform_shim.py    accessor caching, null-module recording, real-module passthrough
  test_helpers.py          calc_polygon_points / point_in_rectangle, rotated + degenerate
  test_language.py         8 languages complete, fallbacks, code literals vs. table, LFS encoding
  test_settings.py         SettingsManager storage; every system name has a settings key
  test_fixtures.py         the factories themselves, incl. a VehicleManager/StateHandler round trip
  test_error_isolation.py  EventBus / ThreadManager / AssistanceManager error isolation,
                           the ErrorThrottle rate limiter and the task watchdog
  test_lifecycle.py        connection-test timeout and retry, shutdown order, signal path
  test_insim_output.py     thread-safe send buffer, button registry, LFS text encoding
```

### Fixtures (`tests/conftest.py`)

| Fixture | Gives you |
|---|---|
| `bus` | a real `EventBus` |
| `recorder` | `recorder('a', 'b')` → `EventRecorder` with `.payloads(e)`, `.last(e)`, `.count(e)` |
| `settings` / `make_settings(**overrides)` | `SettingsManager` on a `tmp_path` file |
| `make_vehicle(...)` / `make_own_vehicle(...)` | `Vehicle` / `OwnVehicle` from **metres, degrees, km/h, m/s²** |
| `make_compcar`, `make_mci_packet`, `make_npl_packet`, `make_pll_packet`, `make_sta_packet`, `make_outgauge_packet` | namespace packets with the real field names |
| `fake_insim`, `fake_connector` | recording stand-ins for the InSim socket and `LFSConnector` |

Module-level helpers `metres()`, `lfs_heading()`, `mci_speed()` and `show_lights()` do the
unit conversions; import them from `conftest` when a test needs raw LFS units.

Angle convention for every factory argument is the LFS one: **0° = +Y (north),
anticlockwise** — the same encoding as `CompCar.Heading` (`reference/conventions.md` §2).
`cname`/`pname` are **bytes**, as IS_NPL delivers them.

### Tests that are expected to fail

A few tests are marked `xfail(strict=False)` because they describe a defect that is real
but belongs to someone else. When it is fixed the test turns green on its own; remove the
marker then.

| Test | Waiting for |
|---|---|
| `test_settings.py::test_every_system_name_is_a_settings_key` | `sat_nav` has no settings key (`known-issues.md` #18) |
| `test_helpers.py` degenerate-rectangle cases | `point_in_rectangle` judges by cross-product sign only, so a zero-area rectangle swallows its whole line |

## Guiding constraint

Tests must run **without LFS, without a GUI, and without a network** — otherwise they
will never be run. That rules out end-to-end testing of the real thing, so the strategy
is to push as much logic as possible behind pure functions and event boundaries, and to
replay recorded packet data for everything else.

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
| `misc/language.py` | *(done — `test_language.py`)* |
| `core/settings_manager.py` | *(done — `test_settings.py`)* |
| `misc/helpers.py` | *(done — `test_helpers.py`)* |

A cheap high-value test — every `AssistanceSystem` name resolves to a
`SettingsManager._defaults` key — is `test_settings.py::test_every_system_name_is_a_settings_key`.
It catches `sat_nav` (`known-issues.md` #18) and is `xfail` until that is fixed.

## Layer 2 — systems against a real EventBus

`process(own_vehicle, vehicles)` takes plain objects and emits events. Build fake
`OwnVehicle` / `Vehicle` instances, subscribe a recorder to the bus, call `process()`,
assert the emitted events.

```python
def test_fcw_warns_on_stationary_car_ahead(bus, recorder, settings,
                                           make_own_vehicle, make_vehicle):
    seen = recorder('collision_warning_changed')
    fcw = ForwardCollisionWarning(bus, settings)
    own   = make_own_vehicle(speed=80, x=0, y=0, heading=0)   # km/h, metres, degrees
    other = make_vehicle(plid=2, speed=0, x=0, y=30)
    fcw.process(own, {2: other})
    assert seen.last('collision_warning_changed')['level'] >= 2
```

Cover per system: the disabled path, the below-threshold path, each warning level, and
the **emit-only-on-change** contract (calling `process` twice with identical input must
emit once).

Systems that inject keys (`AutoHold`, `Gearbox`) need no patching: off Windows the shim
already swallows the keystrokes, and `platform_shim.recorded_calls()` reads them back.
Assert the guards that way — no `pyautogui.keyDown` while `dialog` or `text_entry` is
active. Call `reset_recorded_calls()` first, since the log is process-wide.

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
