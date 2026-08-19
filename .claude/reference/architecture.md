# Architecture

## 1. Startup sequence (`main.py`)

```
run_setup_if_needed()          # blocking Tkinter wizard on first run only (.setup_done flag)
EventBus()                     # created first, everything else receives it
SettingsManager()              # loads settings.json, falls back to hardcoded defaults
ThreadManager(event_bus)
wait for LFS.exe process       # exponential backoff, sys.exit after ~60 s
LfsConnectionTest().run_test() # opens a throwaway InSim conn, waits for IS_STA, closes all
LFSConnector(bus, settings)    # the real connection: InSim + OutGauge + OutSim
MessageSender(connector)
VehicleManager(bus)
AssistanceManager(bus, settings)   # constructs all assistance systems
UIManager(bus, message_sender, settings)
MenuSystem(ui_manager, settings)
AudioPlayer(bus, settings)
ScheduledTask("assistance_processing", assistance_manager.process_all_systems, 100 ms)
ScheduledTask("ui_updates",            ui_manager.update_hud,                   50 ms)
thread_manager.start()
pyinsim.run()                  # BLOCKS the main thread in the asyncore loop
```

Component construction order matters: every subscriber must exist before the events it
cares about are first emitted. Because subscription happens in `__init__`, adding a
component late in `main.py` can silently miss early events (e.g. the first `IS_STA`).

## 2. Threading model

| Thread | Runs | Started by |
|---|---|---|
| main | `asyncore` loop → **all InSim/OutGauge/OutSim packet handlers**, and therefore every `event_bus.emit` that originates from a packet | `pyinsim.run()` |
| worker (100 ms) | `AssistanceManager.process_all_systems` | `ThreadManager` |
| worker (50 ms) | `UIManager.update_hud` | `ThreadManager` |
| ad-hoc | `PDCBeepController` spawns a thread per beep; `Keybinder` spawns a listener thread | — |

`ThreadManager` groups tasks by interval: **one thread per distinct `interval_ms`**,
tasks in that group run sequentially. Adding a task with a new interval creates a new
thread. `_run_cycle` measures elapsed time and sleeps the remainder — it does not
catch up on overruns, it just runs late.

**Concurrency reality:** packet handlers (main thread) mutate `VehicleManager.vehicles`
and `OwnVehicle` while assistance systems (worker thread) read them. There is no lock
around vehicle state. `EventBus` only locks its subscriber dict, not the payloads.
Several systems defensively copy dicts (`vehicles.copy()` in PDC) — this is why.
When you add state that both sides touch, assume it can change mid-iteration.

## 3. EventBus (`core/event_bus.py`)

```python
bus.subscribe('event_name', callback)   # callback(data)
bus.emit('event_name', data)            # synchronous, in the caller's thread
```

- **Emission is synchronous.** `emit` returns only after every subscriber has run.
  A slow subscriber directly delays the packet handler or the worker cycle that
  emitted the event. A subscriber that raises propagates into the emitter.
- Subscribers are copied under a lock, then invoked outside the lock.
- There is no unsubscribe-on-shutdown, no priority, no async queue. Handlers must be
  fast and total.
- Event names are plain strings — typos fail silently (the event simply has no
  subscribers). Always cross-check `reference/events.md` when adding one.

The `try/except` around handler invocation is **commented out** in `emit`, in
`ThreadManager._run_cycle` and in `AssistanceManager.process_all_systems`. This was
done to surface bugs during development. It means one bad packet can kill a worker
thread permanently and silently. See `known-issues.md` #1.

## 4. Module map

```
main.py                    Application composition root and lifecycle
kontext_prompt             Original hand-written project briefing (superseded, kept for context)

core/
  event_bus.py             Publish/subscribe hub — the only inter-component interface
  settings_manager.py      settings.json persistence + the authoritative default table
  thread_manager.py        ScheduledTask + one thread per interval
  setup_wizard.py          First-run Tkinter wizard: patches LFS cfg.txt, autoexec.lfs, copies layouts
  connection_test.py       Throwaway InSim connection used to probe that LFS is reachable

lfs/
  connector.py             Owns InSim/OutGauge/OutSim; binds packets → emits events; sends buttons, lights, commands
  lfs_state.py             StateHandler: parses IS_STA flags into the `state_data` event (on_track, dialog, text_entry, track, cam)
  message_sender.py        Thin wrapper over connector for buttons/messages; tracks which button IDs are live

vehicles/
  vehicle.py               Vehicle + VehicleData dataclass; position, heading, distance/angle to player
  own_vehicle.py           OwnVehicle(Vehicle): adds OutGauge data (rpm, gear, pedals, dash lights)
  vehicle_manager.py       Consumes MCI/NPL/PLL/OutGauge → maintains the vehicle dict → emits vehicles_updated / own_vehicle_updated
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
        updates Vehicle positions, computes distance/angle to player
        when it believes all cars for this frame arrived → emit 'vehicles_updated'
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
   exactly match a key in `SettingsManager._defaults`** — `is_enabled()` does
   `settings.get(self.name.lower(), False)`. A missing key means the system never
   runs (this is how `sat_nav` ended up permanently disabled).
3. Add the default to `core/settings_manager.py`.
4. Register it in `AssistanceManager._init_systems`.
5. Emit results as events; never call the UI directly. Document the event in
   `reference/events.md`.
6. Add a menu entry in `ui/menu_system.py` and translations in `misc/language.py`.
7. Respect the cycle budget — see `conventions.md` §6.
