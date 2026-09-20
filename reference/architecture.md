# Architecture

## 1. Startup sequence (`main.py`)

```
setup_logging()                # console + rotating file handler, once, from __main__
SingleInstance().acquire()     # TCP 127.0.0.1:29997; a second copy exits 1 here
SettingsManager()              # loaded before setup so the chosen LFS folder is persisted
run_setup_if_needed(settings)  # blocking Tkinter wizard; cancellation aborts startup
EventBus()                     # created first, everything else receives it
ThreadManager(event_bus)
wait for LFS.exe process       # exponential backoff, sys.exit after ~60 s
LfsConnectionTest().run_test() # fresh InSim conn per attempt, 5 s timeout, then closes all
LFSConnector(bus, settings)    # the real connection: InSim + OutGauge; OutSim opt-in
MessageSender(connector)
CarProfiles(bus)               # learns per-car rpm/gear from OutGauge; before its readers
VehicleManager(bus)
AssistanceManager(bus, settings, car_profiles)   # constructs all assistance systems
UIManager(bus, message_sender, settings, car_profiles)
MenuSystem(ui_manager, settings)
AudioPlayer(bus, settings)
ScheduledTask("assistance_processing", assistance_manager.process_all_systems, 100 ms)
ScheduledTask("ui_updates",            ui_manager.update_hud,                   50 ms)
ScheduledTask("car_profiles_save",     car_profiles.maybe_save,              30000 ms)
thread_manager.start()         # one thread per interval + a watchdog thread
install_signal_handlers()      # SIGINT/SIGTERM/SIGBREAK -> KeyboardInterrupt
pyinsim.run()                  # BLOCKS the main thread in the asyncore loop
shutdown()                     # threads -> BFN_CLEAR -> TINY_CLOSE -> flush -> closeall
```

**Shutdown order matters.** The worker threads stop first, or they keep painting buttons
while the cleanup runs. Then `MessageSender.remove_all()` (one `BFN_CLEAR`) and
`LFSConnector.disconnect()` (`TINY_CLOSE`, a bounded `flush()` straight to the socket,
then `close()` and `pyinsim.closeall()`). The flush is not optional: after
`pyinsim.run()` has returned, nobody calls `handle_write()`, so the goodbye packets would
sit in the send buffer and LFS would keep the buttons on screen. This is also why the
signal handler raises `KeyboardInterrupt` instead of calling `closeall()` — asyncore
re-raises exactly that exception, and it leaves the socket open long enough to say
goodbye.

Component construction order matters: every subscriber must exist before the events it
cares about are first emitted. Because subscription happens in `__init__`, adding a
component late in `main.py` can silently miss early events (e.g. the first `IS_STA`).

Frozen builds enter through `release_main.py`. Its `--guardian` dispatch occurs
before app construction, setup or the single-instance lock; a frozen executable
cannot run `guardian.py` by treating itself as a Python interpreter.
`--smoke-test` validates bundled imports/assets without connecting or injecting input.
`--setup` reruns setup, guarded by the same instance lock.

`misc.helpers.resolve_path` is for read-only resources and uses `sys._MEIPASS`
when frozen. `resolve_data_path` is for mutable state: frozen builds use
`%LOCALAPPDATA%/PACTDrivingAssistant`, source runs use the repository. Settings,
logs, setup state, car/gearbox profiles and guardian markers follow this rule.
Guardian receives explicit settings and marker paths. Never ship local state
as PyInstaller data; the spec includes only audio, layouts, routes and instructions.

**Services may be passed by reference; subsystems may not.** The rule "the EventBus is
the only interface between components" is about *subsystems* — one assistance system must
never reach into another. A **service** is different: read-mostly, owned by `main.py`,
answering questions rather than doing things. `SettingsManager` and `MessageSender` were
always passed this way, and `CarProfiles` is the same kind of object. Pushing per-car
values through events would mean every reader keeping its own copy of the same table,
which is more coupling, not less. The test for "is this a service": it has no `process()`,
it does not act on the game, and two readers asking it the same question must get the
same answer.

`CarProfiles` learns on the **packet thread** and is read from the assistance and UI
threads, so it holds a lock for the handful of comparisons it makes, and it never writes
its file there — `maybe_save` runs from its own slow scheduled task and from `shutdown()`.

## A second process: `guardian.py`

The app is not alone any more. When automatic emergency braking arms its analog path,
`Controls/brake_axis.py` spawns `guardian.py` as a **detached** child that waits on the
main process's PID and restores LFS's brake axis if the main process dies while holding
it. It exists because `/axis` can only be sent by something that is alive, and vJoy holds
its last value forever — see `control-intervention.md` §3.2.

Rules for it, because a watchdog that fails is worse than none:

- **It imports nothing from this codebase.** It reads one integer from `settings.json`
  and speaks the two InSim packets it needs by hand. A watchdog that dies with the thing
  it watches, or that breaks because a package was half-imported, is decoration.
- **It acts only on evidence**, the `brake_axis_held.marker` file, never on a timer or an
  assumption.
- It is detached (`DETACHED_PROCESS`, no console) so a Ctrl+C or a kill of the main
  process does not take it with them.
- It exits on its own once it has acted.

If it ever grows a UI — the standalone status window that is wanted eventually — that
belongs on top of this process, not inside the main one.

## 2. Threading model

| Thread | Runs | Started by |
|---|---|---|
| main | `asyncore` loop → **all InSim/OutGauge/OutSim packet handlers**, and therefore every `event_bus.emit` that originates from a packet | `pyinsim.run()` |
| worker (100 ms) | `AssistanceManager.process_all_systems` | `ThreadManager` |
| worker (50 ms) | `UIManager.update_hud` | `ThreadManager` |
| watchdog | checks every task's `last_execution` once a second | `ThreadManager` |
| ad-hoc | `PDCBeepController` spawns a thread per beep; `Keybinder` spawns a listener thread | — |

`ThreadManager` groups tasks by interval: **one thread per distinct `interval_ms`**,
tasks in that group run sequentially. Adding a task with a new interval creates a new
thread. `_run_cycle` measures elapsed time and waits the remainder on a stop event — it
does not catch up on overruns, it just runs late, and it logs an overrun at most once
per 30 s per interval group. The watchdog thread reports a task that has not run for
more than 5× its interval (once, until it runs again).

**Concurrency reality:** packet handlers run on the main thread while assistance
systems read the same state on worker threads. `EventBus` only locks its subscriber
dict, not the payloads.

- **Vehicle events publish detached read-only snapshots.** Both the Vehicle wrapper
  and its VehicleData are copied before publication. OwnVehicle's scalar gauge
  fields and local/viewed identity are copied together on the packet thread.
  Later MCI, OutGauge, NPL or PLL handling cannot alter a published reading.
  A fresh dict alone is insufficient: it would still contain live Vehicle wrappers.
- AssistanceManager binds the published own/foreign snapshots once per pass.
  These streams have different packet times; this prevents mutation during a pass,
  but does not imply simultaneous telemetry. Consumers must not mutate snapshots.
- Cost: two shallow object copies per published vehicle (O(n), at most about 40
  per MCI frame); two for each OutGauge update. No route deep copies or worker I/O.

When you add state that both sides touch, assume it can change mid-iteration.

**A thread does not escape the GIL.** "Move it off the assistance thread" only
helps when the work releases the GIL — I/O, `subprocess`, most `ctypes` calls.
Some C extensions hold it for their whole duration, and then the *other* threads
pay the cost with none of the blame. Measured here: `pygame.joystick.init()`
(168 ms) and each `pygame.joystick.Joystick(i)` (54–258 ms) hold it throughout,
which is where two mis-attributed budget warnings came from —
`gearbox 553.5 ms` and `aeb 362.9 ms`, neither of which was doing any work.

So before trusting `AssistanceManager`'s per-system timing, check what the *main*
thread was doing at the same timestamp. If the answer is "something in a C
extension", the slow system is a bystander. Such work is split into steps across
main-loop pumps instead (`PedalWatch.start`), never handed to a thread and
forgotten.

## 3. EventBus (`core/event_bus.py`)

```python
bus.subscribe('event_name', callback)   # callback(data)
bus.emit('event_name', data)            # synchronous, in the caller's thread
```

- **Emission is synchronous.** `emit` returns only after every subscriber has run.
  A slow subscriber directly delays the packet handler or the worker cycle that
  emitted the event.
- **A subscriber that raises is isolated.** The other subscribers still run, the
  emitting thread survives, and the failure is logged through a shared
  `ErrorThrottle` — first few in full, then at most one line per source per 30 s.
  Nothing propagates into the asyncore loop.
- Subscribers are copied under a lock, then invoked outside the lock.
- There is no unsubscribe-on-shutdown, no priority, no async queue. Handlers must be
  fast and total.
- Event names are plain strings — typos fail silently (the event simply has no
  subscribers). Always cross-check `reference/events.md` when adding one.

## 3a. Failure policy (`misc/logging_setup.py`)

All three loops — `EventBus.emit`, `ThreadManager._run_task`,
`AssistanceManager.process_all_systems` — share one policy, implemented by
`ErrorThrottle`:

- log the first 3 failures of a *source* in full, with traceback;
- afterwards at most one line per source per 30 s, carrying how many were suppressed;
- 5 **consecutive** failures disable that task / assistance system, log it and emit a
  `notification` so the driver sees something happened. `AssistanceManager.enable_system`
  clears that state again.

A source is a stable string: the subscriber's `Class.method`, the task name, the
assistance system key. The cost on the success path is zero — nothing is called unless
an exception was raised.

Consequence for the rest of the app: **an event handler that raises is not a crash any
more, it is a silent degradation with a log line.** Do not rely on exceptions
propagating out of `emit()`.

## 4. Module map

```
main.py                    Application composition root and lifecycle
kontext_prompt             Original hand-written project briefing (superseded, kept for context)

core/
  single_instance.py       Startup lock (TCP 127.0.0.1:29997) — a second copy of the add-on exits instead of running blind
  outgauge_config.py        Read-only startup check of saved OutGauge settings; running installation takes precedence
  event_bus.py             Publish/subscribe hub — the only inter-component interface
  settings_manager.py      settings.json persistence + the authoritative default table
  thread_manager.py        ScheduledTask + one thread per interval
  setup_wizard.py          First-run Tkinter wizard: patches LFS cfg.txt, autoexec.lfs, copies layouts
  connection_test.py       Throwaway InSim connection used to probe that LFS is reachable (per-attempt, with timeout)

lfs/
  connector.py             Owns InSim/OutGauge and opt-in OutSim; binds packets → emits events; sends buttons, lights, commands
  lfs_state.py             StateHandler: IS_STA + IS_CIM → the `state_data` event (on_track, dialog, text_entry, track, cam, screen context, buttons_allowed)
  message_sender.py        Button registry (sends only on change) + chat/command wrapper over the connector
  text_encoding.py         LFS code-page encoding of button and chat text (^L/^T/…), truncation

vehicles/
  vehicle.py               Vehicle + VehicleData dataclass; position, heading, distance/angle to player, decoded names, IS_NPL identity; frame staging (begin_frame/commit_frame)
  own_vehicle.py           OwnVehicle(Vehicle): OutGauge data (rpm, gear, pedals, dash lights) + local_plid / viewed_plid / is_local_driver
  vehicle_manager.py       Consumes MCI/NPL/PFL/PLL/OutGauge → reassembles MCI frames on CCI_FIRST/CCI_LAST → drops cars the frame no longer carries → emits an immutable vehicles_updated snapshot / own_vehicle_updated
  car_profiles.py          CarProfiles: idle rpm / rev limit / gear count per car *model*, learned from OutGauge and persisted to data/car_profiles.json

assistance/
  base_system.py           AssistanceSystem ABC: process(), is_enabled()
  manager.py               Constructs and drives every system each cycle
  collision_warning.py     Forward collision warning (FCW)
  path_conflict.py         Shared geometry: do two moving rectangles meet, and how far may we still go (used by BSW and CTW)
  blind_spot_warning.py    Blind spot warning (BSW), 3 levels; shapely corridor for level 1, path_conflict for 2 and 3
  cross_traffic_warning.py Cross traffic warning (CTW), path_conflict + braking demand
  park_distance_control.py PDC: 6 sensors vs. layout objects and cars via spatial hash grid
  auto_hold.py             Automatic handbrake when stopped (injects a keypress)
  adaptive_lights.py       Adaptive brake lights, high beam assist, cop siren/strobe
  gearbox.py               Automatic gearbox with per-car calibration (injects keypresses)
  AI_Driver.py             AI traffic controller: drives LFS AI cars along recorded routes
  chat_commands.py         `$`-prefixed in-game chat commands + periodic tooltips (event-driven, no process())

ui/
  ui_manager.py            HUD, warnings, PDC display, notifications, siren buttons; owns the button ID map
  menu_system.py           In-game menu tree built from InSim buttons

misc/
  platform_shim.py         Lazy accessors for pyautogui / winsound / pynput / pygame / tkinter / vjoy
  logging_setup.py         setup_logging() (console + rotating file) and the ErrorThrottle rate limiter
  helpers.py               resolve_path, is_lfs_running, geometry helpers (calc_polygon_points, point_in_rectangle, is_reversing)
  input_guard.py           InputGuard: may a key be injected right now? — every pyautogui call site asks it (ui.md §1.4)
  key_tap.py               KeyTapper: timed key press on its own thread — the hold never runs on an assistance cycle (ui.md §1.6)
  key_names.py             One key, four spellings: settings.json / VK code / LFS /key / pyautogui
  physical_keys.py         PhysicalKeyState: is the *hardware* holding this key, and what does LFS believe? (control-intervention.md §3.1)
  language.py              LanguageManager: 8-language translation table
  key_binder.py            pynput listener to capture a key/mouse button for rebinding
  audio_player.py          pygame.mixer playback of audio/*.wav - ONE reserved channel
  pdc_beep.py              winsound beep patterns for PDC
  spacial_hash_grid.py     SpatialHashGrid: broad-phase + polygon overlap for PDC
  vjoy.py                  Raw vJoy ctypes binding
  vjoy_device.py           VJoyDevice: one vJoy device for one axis — acquire/feed, driver touched only on acquire

tests/                     pytest suite + shared fixtures — see reference/testing.md
pyinsim/                   Forked & extended pyinsim 2.1.0 — see reference/insim.md

AI_Control.py              AICarController: high-level wrapper over IS_AIC (AI car control)
MapBuilder.py              Offline tool: turns a captured LFS layout into track_data/*.json (roads, junctions, markers)
tools/capture_layout.py    NOT a test — a capture script that feeds a live layout into MapBuilder

track_data/*.json          Generated route/junction/marker maps per track (BL, KY, SO)
layouts/*.lyt              LFS layout files installed into LFS by the setup wizard
audio/*.wav                Warning sounds
```

## 5. Data flow, end to end

```
IS_MCI (all car positions, every `Interval` ms, possibly split over several packets)
  → LFSConnector._handle_mci → emit 'vehicle_data_received'
  → VehicleManager._handle_vehicle_data
        accumulates cars until CCI_LAST (or a 0.5 s timeout) closes the frame
        updates staged Vehicle positions, computes distance/angle to player,
        commits every touched Vehicle → drops the cars the frame did not
        contain (#49) → emit 'vehicles_updated' (fresh dict)
  → AssistanceManager caches the dict

OutGauge packet (high rate, own car only: speed, rpm, gear, pedals, dash lights)
  → emit 'outgauge_data'
  → VehicleManager updates OwnVehicle → emit 'own_vehicle_updated'
  → UIManager also consumes 'outgauge_data' directly for the HUD

every 100 ms (worker thread)
  → AssistanceManager.process_all_systems()
        for each enabled system: process(own_vehicle, vehicles)
        systems emit their own result events (collision_warning_changed, pdc_changed, …)
  → UIManager / AudioPlayer / LFSConnector react

every 50 ms (worker thread)
  → UIManager.update_hud() renders buttons via MessageSender → LFSConnector → IS_BTN
```

`process_all_systems` returns early if `own_vehicle` is not set yet, and only runs the
systems when `on_track` is true.

## 6. Adding a new assistance system

1. Create `assistance/your_system.py`, subclass `AssistanceSystem`.
2. Call `super().__init__("your_key", event_bus, settings)` where **`"your_key"` must
   exactly match one of `SettingsManager.known_keys`** — `is_enabled()` does
   `settings.get(self.name.lower(), False)`, and that explicit `False` is what an
   unknown key gets. A missing key means the system never runs (this is how `sat_nav`
   ended up permanently disabled; it is no longer registered at all).
3. Add a `Setting(...)` entry to `_SCHEMA` in `core/settings_manager.py`.
   `tests/test_settings.py` fails if a registered system has no key.
4. Register it in `AssistanceManager._init_systems`.
5. Emit results as events; never call the UI directly. Document the event in
   `reference/events.md`.
6. Add a menu entry in `ui/menu_system.py` and translations in `misc/language.py`.
7. Respect the cycle budget — see `conventions.md` §6.
