# Architecture

## 1. Startup sequence (`main.py`)

```
setup_logging()                # console + rotating file handler, once, from __main__
run_setup_if_needed()          # blocking Tkinter wizard on first run only (.setup_done flag)
EventBus()                     # created first, everything else receives it
SettingsManager()              # loads settings.json, falls back to hardcoded defaults
ThreadManager(event_bus)
wait for LFS.exe process       # exponential backoff, sys.exit after ~60 s
LfsConnectionTest().run_test() # fresh InSim conn per attempt, 5 s timeout, then closes all
LFSConnector(bus, settings)    # the real connection: InSim + OutGauge + OutSim
MessageSender(connector)
VehicleManager(bus)
AssistanceManager(bus, settings)   # constructs all assistance systems
UIManager(bus, message_sender, settings)
MenuSystem(ui_manager, settings)
AudioPlayer(bus, settings)
ScheduledTask("assistance_processing", assistance_manager.process_all_systems, 100 ms)
ScheduledTask("ui_updates",            ui_manager.update_hud,                   50 ms)
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

- **Foreign vehicles are safe.** `vehicles_updated` carries a snapshot: a fresh dict per
  MCI frame, and each `VehicleData` object is *replaced* at the end of the frame rather
  than written to (`Vehicle.begin_frame` / `commit_frame`). Iterating it while packets
  keep arriving cannot raise and cannot see a half-updated car. `VehicleManager.vehicles`
  itself is the live dict and must not be handed out.
- **`own_vehicle` is not.** `own_vehicle_updated` passes the live `OwnVehicle`, and
  OutGauge writes into it at ~30 Hz. Bind `data = own_vehicle.data` once per `process()`
  call instead of re-reading it line by line (`known-issues.md` #12).

When you add state that both sides touch, assume it can change mid-iteration.

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
  event_bus.py             Publish/subscribe hub — the only inter-component interface
  settings_manager.py      settings.json persistence + the authoritative default table
  thread_manager.py        ScheduledTask + one thread per interval
  setup_wizard.py          First-run Tkinter wizard: patches LFS cfg.txt, autoexec.lfs, copies layouts
  connection_test.py       Throwaway InSim connection used to probe that LFS is reachable (per-attempt, with timeout)

lfs/
  connector.py             Owns InSim/OutGauge/OutSim; binds packets → emits events; sends buttons, lights, commands
  lfs_state.py             StateHandler: IS_STA + IS_CIM → the `state_data` event (on_track, dialog, text_entry, track, cam, screen context, buttons_allowed)
  message_sender.py        Button registry (sends only on change) + chat/command wrapper over the connector
  text_encoding.py         LFS code-page encoding of button and chat text (^L/^T/…), truncation

vehicles/
  vehicle.py               Vehicle + VehicleData dataclass; position, heading, distance/angle to player, decoded names, IS_NPL identity; frame staging (begin_frame/commit_frame)
  own_vehicle.py           OwnVehicle(Vehicle): OutGauge data (rpm, gear, pedals, dash lights) + local_plid / viewed_plid / is_local_driver
  vehicle_manager.py       Consumes MCI/NPL/PLL/OutGauge → reassembles MCI frames on CCI_FIRST/CCI_LAST → emits an immutable vehicles_updated snapshot / own_vehicle_updated
  VehicleInfo.py           Legacy/unused data container — not wired into anything

assistance/
  base_system.py           AssistanceSystem ABC: process(), is_enabled()
  manager.py               Constructs and drives every system each cycle
  collision_warning.py     Forward collision warning (FCW)
  blind_spot_warning.py    Blind spot warning (BSW), uses shapely polygons
  cross_traffic_warning.py Cross traffic warning (CTW), ray-intersection + TTC
  park_distance_control.py PDC: 6 sensors vs. layout objects and cars via spatial hash grid
  auto_hold.py             Automatic handbrake when stopped (injects a keypress)
  adaptive_lights.py       Adaptive brake lights, high beam assist, cop siren/strobe
  gearbox.py               Automatic gearbox with per-car calibration (injects keypresses)
  navigation.py            Dijkstra route guidance — currently never enabled, see known-issues
  AI_Driver.py             AI traffic controller: drives LFS AI cars along recorded routes
  chat_commands.py         `$`-prefixed in-game chat commands + periodic tooltips (event-driven, no process())
  controller_emulator.py   Emulated brake input via vJoy — disabled in manager.py

ui/
  ui_manager.py            HUD, warnings, PDC display, notifications, siren buttons; owns the button ID map
  menu_system.py           In-game menu tree built from InSim buttons

misc/
  platform_shim.py         Lazy accessors for pyautogui / winsound / pynput / pygame / tkinter / vjoy
  logging_setup.py         setup_logging() (console + rotating file) and the ErrorThrottle rate limiter
  helpers.py               resolve_path, is_lfs_running, geometry helpers (calc_polygon_points, point_in_rectangle)
  language.py              LanguageManager: 8-language translation table
  key_binder.py            pynput listener to capture a key/mouse button for rebinding
  audio_player.py          pygame.mixer playback of audio/*.wav with repeat suppression
  pdc_beep.py              winsound beep patterns for PDC
  spacial_hash_grid.py     SpatialHashGrid: broad-phase + polygon overlap for PDC
  vjoy.py                  Raw vJoy ctypes binding

tests/                     pytest suite + shared fixtures — see reference/testing.md
pyinsim/                   Forked & extended pyinsim 2.1.0 — see reference/insim.md
Controls/wheel.py          vJoy brake actuation (currently unreachable, see known-issues)

AI_Control.py              AICarController: high-level wrapper over IS_AIC (AI car control)
AI_Cheatsheet.py           Stale near-duplicate of AI_Control.py — dead file, do not edit
MapBuilder.py              Offline tool: turns a captured LFS layout into track_data/*.json (roads, junctions, markers)
test.py                    NOT a test — a capture script that feeds a live layout into MapBuilder

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
        commits every touched Vehicle → emit 'vehicles_updated' (fresh dict)
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
