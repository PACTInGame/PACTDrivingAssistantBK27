# Testing

**Status: offline test harness in place; standalone live scenario tooling is in
`simulations-tests/` (record/replay/monitor). Real scenarios must be recorded and
validated manually before use.** See `../simulations-tests/README.md` for commands,
trace timing, port separation and the distinction between completed execution and
a functional pass. Baseline scenarios are immutable during AI testing; adapt only
monitor copies under `simulations-tests/_temp/`. The tools share only pyinsim and
the lazy platform loader, not running application components. Offline harness
regressions live in `tests/test_simulation_harness.py` and perform no input/network I/O.
When cfg.txt UDP streams cannot be duplicated through InSim, the optional standalone
`simulations-tests/udp_relay.py` forwards unchanged packets to both consumers; run
the observer with `--cfg-streams`. See its README for the explicit one-time LFS
configuration and restore procedure. Never bind a second listener to the add-on's
30000/29998 ports and assume both processes will receive every packet.
`python -m pytest` from the project root runs the suite on
Linux, Windows or macOS with only `requirements-dev.txt` installed (`pytest`, `psutil`,
`shapely`, `numpy`). No LFS, no display, no sound device, no socket.

The live LFS capture script that used to sit in the project root as `test.py` is now
`tools/capture_layout.py`; `pytest.ini` sets `testpaths = tests`, so neither it nor
`MapBuilder.py` is ever collected.

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
  test_settings.py         SettingsManager storage, defaults merge, migration, schema
                           validation, debounced/atomic writes; every system name has
                           a settings key
  test_fixtures.py         the factories themselves, incl. a VehicleManager/StateHandler round trip
  test_error_isolation.py  EventBus / ThreadManager / AssistanceManager error isolation,
                           the ErrorThrottle rate limiter and the task watchdog
  test_lifecycle.py        connection-test timeout and retry, shutdown order, signal path
  test_insim_output.py     thread-safe send buffer, button registry, LFS text encoding
  test_vehicle_model.py    MCI frame reassembly, snapshot immutability, PIF_* control
                           modes, own-PLID identity and the PType AI flag
  test_screen_context.py   IS_STA/IS_CIM screen contexts, main-menu suppression, SHIFT+B
                           repaint, HUD clamp, warning blink, notification queue
  test_menu.py             every menu's button list checked against its action table,
                           the settings the menu writes, live language, PDC mode
  test_collision_warning.py  FCW physics with hand-computed expectations, the detection
                           gates, warning-level hysteresis, and Vehicle acceleration
                           from the real packet interval
  test_blind_spot.py       BSW corridor geometry and validity, the geometric trigger,
                           relative-speed relevance and hold time, and the polygon
                           budget of the pre-filter
  test_cross_traffic.py    CTW direction/side conventions, the speed+reverse gate that
                           replaced the gear gate, and the size-aware arrival window
  test_pdc.py              AXM object identity and the AXM→MCI scale, the -1/0 sensor
                           contract, and the single-threaded beeper
  test_actuation.py        the input guard's refusal table, AutoHold/Gearbox key
                           injection and its guards, live key rebinding, gearbox
                           calibration (countdown, cancel, gear storage, legacy file),
                           high-beam dedupe, strobe timing, and the single owner of
                           the siren/strobe state
  test_car_profiles.py     CarProfiles: the redline that only counts once it stops
                           rising, idle sampled only at a standstill, a stopped
                           engine never becoming an idle speed, and the camera
                           trap — packets whose Car field changes mid-stream
  test_key_tap.py          KeyTapper: that ``tap()`` returns without waiting for the
                           hold, that the hold happens anyway on the tapper's thread,
                           the gearbox's clutch/gear ordering, re-tap extension, the
                           stored->pyautogui name translation, and release_all()
  test_emergency_brake.py  the key-name table (stored/VK/LFS/pyautogui spellings),
                           the two low-level hook filters called directly, the
                           brake-key arbitration incl. the key-release trap, and
                           EmergencyBrake's hysteresis, guards and lifecycle
  test_brake_axis.py       the wheel_js path: the measured vJoy mapping in both
                           polarities, the engage/release orderings, the handover
                           marker, guardian.py's marker/settings fallbacks, its
                           packets against pyinsim's, and VJoyDevice without a DLL
  test_ai_traffic.py       the windowed route search against a full scan over the real
                           track_data files, the teleport resync, route-file validation,
                           PType-based adoption, the routes/worker race, the AI traffic
                           start confirmation, and the per-cycle budget with 20 cars
```

### Fixtures (`tests/conftest.py`)

| Fixture | Gives you |
|---|---|
| `bus` | a real `EventBus` |
| `recorder` | `recorder('a', 'b')` → `EventRecorder` with `.payloads(e)`, `.last(e)`, `.count(e)` |
| `settings` / `make_settings(**overrides)` | `SettingsManager` on a `tmp_path` file |
| `make_vehicle(...)` / `make_own_vehicle(...)` | `Vehicle` / `OwnVehicle` from **metres, degrees, km/h, m/s²** |
| `relate_to_own(own, *vehicles)` | fills in `distance_to_player` / `angle_to_player`, as `VehicleManager` does per MCI frame — needed by anything reading those two fields (FCW's gates, BSW, CTW) |
| `make_compcar`, `make_mci_packet`, `make_npl_packet`, `make_pll_packet`, `make_sta_packet`, `make_outgauge_packet`, `make_cim_packet`, `make_bfn_packet` | namespace packets with the real field names |
| `make_mci_frame(cars, chunk=16, mark=True)` | splits cars into MCI packets and sets `CCI_FIRST`/`CCI_LAST` the way LFS does; `mark=False` reproduces a stream that sets neither |
| `fake_insim`, `fake_connector` | recording stand-ins for the InSim socket and `LFSConnector`. `fake_connector` also records the button path: `.buttons`, `.deletes`, `.drawn_ids()`, `.last_button(id)`, `.reset()` |
| `message_sender` | a real `MessageSender` on top of `fake_connector` — enough to drive `UIManager` and `MenuSystem` without a socket |

Module-level helpers `metres()`, `lfs_heading()`, `mci_speed()` and `show_lights()` do the
unit conversions; import them from `conftest` when a test needs raw LFS units.

Angle convention for every factory argument is the LFS one: **0° = +Y (north),
anticlockwise** — the same encoding as `CompCar.Heading` (`reference/conventions.md` §2).
`cname`/`pname` are passed in as **bytes**, as IS_NPL delivers them; what comes back out
of `vehicle.data.cname` / `.pname` is a decoded `str` (`conventions.md` §4).

Two traps in the packet factories:

- `make_npl_packet()` defaults to `ucid=0, ptype=0`, which describes **the local
  driver**. `VehicleManager` adopts that player as the own car, so it will not show up
  in `manager.vehicles`. Build foreign cars with `ptype=PTYPE_AI` or
  `ucid=1, ptype=PTYPE_REMOTE`.
- `make_sta_packet(base_flags=…)` is the only way to build the main-menu / server-list
  signature, which sets neither `ISS_GAME` nor `ISS_FRONT_END`.
- The vehicle factories build **one car in isolation**: `distance_to_player` and
  `angle_to_player` stay 0 until `relate_to_own` is called. A distance of 0 reads as
  "already touching" to FCW.

Anything driven by a wall clock (`UIManager`'s blink phase, the notification timer)
needs a fake clock — `tests/test_screen_context.py` monkeypatches
`ui.ui_manager.time.perf_counter`. `Gearbox` and `LightAssists` instead expose an
overridable `self.clock` (default `time.perf_counter`); replace it and **re-base the
timestamps `__init__` already took from the real clock** — `test_actuation.py`'s
`with_clock()` shows the pattern, and forgetting it makes every timer look like it is
in the future.

`make_outgauge_packet(flags=pyinsim.OG_SHIFT)` is how the held-Shift state reaches
`misc/input_guard.py`; `InputGuard(bus, foreground_check=…)` is how the Win32
foreground check is driven from both sides without a window manager.

### Tests that are expected to fail

A few tests are marked `xfail(strict=False)` because they describe a defect that is real
but belongs to someone else. When it is fixed the test turns green on its own; remove the
marker then.

| Test | Waiting for |
|---|---|
| `test_helpers.py` degenerate-rectangle cases | `point_in_rectangle` judges by cross-product sign only, so a zero-area rectangle swallows its whole line |

### Testing anything that injects a real key

Windows test-suite caveats: the existing PDC test
`test_the_beeper_actually_sounds_while_requested` expects the shim's recorded
`winsound.Beep` calls, but native Windows winsound does not record them. This can
fail despite a working sound path. The invalid-DLL probe
`test_brake_axis.py::test_a_dll_that_will_not_load_is_reported_as_missing` can also
stall on this Windows host while loading a deliberately corrupt DLL; isolate or
explicitly deselect that test when diagnosing an otherwise stalled suite. Neither
is exercised by the standalone simulation harness's offline tests.

`test_actuation.py` reads the keystrokes back through `platform_shim.recorded_calls()`,
which only works where the real `pyautogui` is missing — hence its module-level `skipif`.
`test_emergency_brake.py` takes the other route and patches a recorder over
`Controls.brake_key.get_keyboard` (and `misc.platform_shim.get_keyboard`), so it runs on
Windows too. Prefer that for new modules.

**Never call `PhysicalKeyState.start()` from a test** — it installs a machine-wide pynput
hook. Call `_keyboard_filter` / `_mouse_filter` directly with a namespace object carrying
`vkCode` and `flags`, and inject a stand-in with the `is_running` / `physically_down` /
`down_for_lfs` / `forget` surface everywhere above it. A faithful stand-in also has to
feed our *own* injected presses back into `down_for_lfs` (never into `physically_down`),
because that is what the real hook does — without it `KeyBrakeOutput.apply(True)`
re-presses on every cycle.

### Comparing against pyinsim

`pyinsim/__init__.py` does `from pyinsim.core import *`, and `core` defines a function
called `insim` — so `pyinsim.insim` is that **function**, not the submodule, and
`from pyinsim import insim` silently gives you the wrong object. Reach packet classes
through the package itself (`pyinsim.IS_ISI`, `pyinsim.INSIM_VERSION`), as
`test_brake_axis.py` does when it checks `guardian.py`'s hand-written packets.

## Guiding constraint for the offline suite

The default `tests/` suite must run **without LFS, without a GUI, and without a
network**. Push logic behind pure functions and event boundaries, and use recorded
packet replay for offline regression coverage. Real-game end-to-end scenarios are
a separate, explicitly launched test layer; they must not enter default pytest
collection or make the offline suite depend on a running game.

## Layer 1 — pure functions (highest value, start here)

No mocks needed, no LFS. These are already pure or nearly so:

| Target | What to assert |
|---|---|
| `misc/helpers.py` — `calc_polygon_points`, `point_in_rectangle` | known points in/out of rotated rectangles; degenerate rectangles |
| `vehicles/vehicle.py` — `update_distance_to_player`, `update_angle_to_player` | metre conversion, angle 0 = straight ahead, wraparound at 0/360 |
| `assistance/AI_Driver.py` — `get_closest_index_on_route`, `load_routes_from_file` | *(done — `test_ai_traffic.py`)* |
| `assistance/AI_Driver.py` — `calculate_angle`, `calculate_angle_meters`, `analyze_upcoming_track`, `calculate_feedforward_steering`, `calculate_feedforward_throttle_brake`, `get_next_points_for_distance` | straight line → curvature 0; known arc → known curvature; clamping at ±45°; wraparound on closed loops |
| `assistance/cross_traffic_warning.py` — `_direction_vector`, `_find_intersection`, `_compute_side` | *(done — `test_cross_traffic.py`)* |
| `assistance/blind_spot_warning.py` | *(done — `test_blind_spot.py`)* |
| `assistance/park_distance_control.py`, `misc/pdc_beep.py` | *(done — `test_pdc.py`)* |
| `assistance/collision_warning.py` — `_calculate_needed_braking` | *(done — `test_collision_warning.py`)* |
| `misc/spacial_hash_grid.py` — `point_in_polygon`, `polygon_overlap`, insert/query/remove | overlapping and touching polygons; objects spanning several cells |
| `misc/language.py` | *(done — `test_language.py`)* |
| `core/settings_manager.py` | *(done — `test_settings.py`)* |
| `misc/helpers.py` | *(done — `test_helpers.py`)* |

A cheap high-value test — every `AssistanceSystem` name resolves to a
`SettingsManager.known_keys` entry — is
`test_settings.py::test_every_system_name_is_a_settings_key`. It caught `sat_nav`, which
is no longer registered; keep it green when adding a system.

## Layer 2 — systems against a real EventBus

`process(own_vehicle, vehicles)` takes plain objects and emits events. Build fake
`OwnVehicle` / `Vehicle` instances, subscribe a recorder to the bus, call `process()`,
assert the emitted events.

```python
def test_fcw_warns_on_stationary_car_ahead(bus, recorder, settings, relate_to_own,
                                           make_own_vehicle, make_vehicle):
    seen = recorder('collision_warning_changed')
    fcw = ForwardCollisionWarning(bus, settings)
    own   = make_own_vehicle(speed=80, x=0, y=0, heading=0)   # km/h, metres, degrees
    other = make_vehicle(plid=2, speed=0, x=0, y=30)
    relate_to_own(own, other)                                 # distance and bearing
    fcw.process(own, {2: other})
    assert seen.last('collision_warning_changed')['level'] >= 2
```

Cover per system: the disabled path, the below-threshold path, each warning level, and
the **emit-only-on-change** contract (calling `process` twice with identical input must
emit once).

Systems that inject keys (`AutoHold`, `Gearbox`) need no patching: off Windows the shim
already swallows the keystrokes, and `platform_shim.recorded_calls()` reads them back.
Assert the guards that way — no `pyautogui.keyDown` while `dialog` or `text_entry` is
active (`test_actuation.py`). Call `reset_recorded_calls()` first, since the log is
process-wide, and skip those tests where the real `pyautogui` exists
(`platform_shim.is_available('pyautogui')`), because then nothing is recorded.

## Layer 3 — packet round-trips (`pyinsim/`)

Byte-exact tests against `C:\LFS\docs\InSim.txt`:

- `pack()` output length equals `Size * 4` for every outgoing packet type.
- `IS_AIC` with n inputs → `Size == 1 + n`; more than `AIC_MAX_INPUTS` raises.
- `unpack()` of a recorded `IS_MCI` yields the expected `NumC` and per-car fields.
- `IS_MCI` with more than 16 cars arrives as several packets — the reassembly logic in
  `VehicleManager` is covered by `test_vehicle_model.py`.
- `OutGaugePack` / `OutSimPack` accept both documented packet sizes.

## Layer 4 — offline packet replay (planned)

Add an opt-in recorder to `LFSConnector` that appends every inbound packet (with a
timestamp) to a file. A replay driver then feeds a recorded session through the real
`VehicleManager` → `AssistanceManager` pipeline at accelerated speed, with the
`ThreadManager` replaced by a deterministic clock.

This gives regression tests over real driving situations — "in this recorded overtake,
no false blind-spot warning is emitted" — which no synthetic fixture can express. It is
also useful alongside synthetic tests of the AI traffic controller.

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

`test_ai_traffic.py` shows the two shapes that survive a noisy container, and the first
is worth preferring wherever it fits:

* **count the operations**, not the milliseconds — after the adoption cycle, no
  steady-state cycle may call `_scan_whole_path` at all, asserted by monkeypatching it;
* **compare against a baseline measured in the same test run** — the same scene with the
  caches dropped each cycle is the pre-optimisation behaviour, and the assertion is on
  the ratio. Any absolute number stays as a loose ceiling with a comment saying why.

## Standalone live LFS scenarios (planned)

The intended live runner is a separate program, independent of PACT's modules and
EventBus. It replays recorded mouse/keyboard input while an independent telemetry
observer records the game. Each scenario starts and ends at the main menu. Initial
coverage: menu navigation, garage settings, standing still, driving then stopping,
and encounters for collision, blind-spot and cross-traffic warnings.

Reusable ABS benchmark code is present in the locally inspected remote-tracking
branches `origin/ki-benchmark` and
`origin/claude/lfs-abs-benchmark-setup-ft10oq`: `harness/input_recorder.py`,
`harness/orchestrator.py` and `lfs_link.py`. It is not a ready-to-run PACT scenario
suite in `refactoring`: the inspected traces/baselines contain only placeholders,
and the harness drives its own ABS controller. Check the branch contents before
reusing it; do not switch a working tree containing unrelated changes to obtain it.

Before claiming autonomous live coverage, provide:

- Recorded scenarios, timestamped semantic markers, explicit expected outcomes
  and measurement windows on a shared time base.
- State checks between replay phases, repeatable vehicle/track/setup conditions,
  and verified main-menu start/end states instead of timing alone.
- Independent telemetry delivery alongside PACT. Both the existing benchmark and
  PACT bind OutGauge port 30000 and OutSim port 29998; resolve port ownership and
  delivery before running them together.
- A way to observe the actual PACT warning/output as well as the driving situation;
  a dangerous encounter or contact alone does not prove a warning was shown.
- Prompt cancellation during idle gaps, release of replay-owned inputs on every
  exit path, foreground checks and explicit failure reporting for invalid runs.
  The inspected ABS recorder sleeps until the next event before noticing an abort.

Keep raw traces and machine-readable results so agents can inspect the evidence.
Validate the runner and repeatability in LFS before calling any scenario ready.

## Manual checks until covered by a validated live scenario

- The Tkinter setup wizard, `MapBuilder.debug_plot`, `winsound`/`pygame` playback,
  vJoy — verify manually.
- Anything requiring LFS to actually render. Manual test checklist instead: fresh
  install → wizard → join track → each menu → each assistance system → leave track →
  rejoin (checks button cleanup and state reset).
