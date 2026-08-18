# Known issues and technical debt

Observations from a full read of the codebase. Ordered roughly by risk. **Not a task
list — do not fix these opportunistically while working on something else.** Mention
them when relevant, fix them when asked.

Keep this file current: remove entries when they are fixed, add systemic defects you
discover. Do not log one-off bugs that were fixed in the same session.

---

## Robustness

**#1 — Exception handling is switched off in all three loops.** `EventBus.emit`,
`ThreadManager._run_cycle` and `AssistanceManager.process_all_systems` all have their
`try/except` commented out. Consequences: one malformed packet or one bad subscriber
raises out of a worker thread, that thread dies permanently, and the app keeps running
with a silently dead assistance or UI loop. In a packet handler the same exception
propagates into `asyncore` and can tear down the connection.
Fixing this properly means per-task isolation plus rate-limited error logging — not
just re-adding bare `except: pass`, which is what hid these bugs originally.

**#2 — Shutdown is a no-op.** `LFSAssistantApp.shutdown()` stops the thread manager but
its InSim cleanup branch is `pass`. Buttons stay on screen in LFS, the socket is not
closed, and `pyinsim.closeall()` is never called.

**#3 — Dead events.** `assistance_results`, `outsim_data` and `player_data_updated` are
emitted every cycle (or every packet) with no subscribers. `outsim_data` in particular
means the whole OutSim pipeline runs for nothing.

**#4 — Two nearly identical command events.** `send_command_to_lfs` (payload: plain
`str`, subscriber `MessageSender`) versus `send_lfs_command` (payload:
`{'command': str}`, subscriber `UIManager`). Different shapes, different routes, same
purpose. Should be unified into one event with one payload shape.

**#6 — MCI frame reassembly is fragile.** `VehicleManager._handle_vehicle_data` decides
a frame is complete when `received_cars_count == len(self.players)`. If the players
dict is stale or a player is missing, `vehicles_updated` never fires and every
assistance system freezes on old data. `CompCar.Info` carries `CCI_FIRST` / `CCI_LAST`
bits for exactly this purpose — use them (`insim.md` §2).

**#8 — `Controls/wheel.py` cannot work.** Its `try` block raises `ImportError`
unconditionally *after* the import, so `vj` and `setJoy` are never bound and
`press_wheel_brake` would raise `NameError`. Currently unreachable only because
`ControllerEmulator` is commented out in `AssistanceManager`.

**#11 — Global keypress injection has no focus check.** `AutoHold` and `Gearbox` use
`pyautogui` to press keys. `AutoHold` guards against `dialog`/`text_entry`; `Gearbox`
does not guard at all. Neither checks whether LFS is the foreground window, so the app
can type into whatever the user has focused.

**#12 — No shared-state locking.** Packet handlers (main thread) mutate the vehicle
dict while assistance systems (worker thread) iterate it. `ParkDistanceControl` works
around this with `vehicles.copy()`; others do not. `RuntimeError: dictionary changed
size during iteration` is reachable.

## Performance

**#5 — `navigation.py` is unusable as written.** Dozens of `print()` calls per 100 ms
cycle, and `_get_closest_road` walks every segment of every road each cycle. It is
currently inert because `sat_nav` has no settings key and `sat_nav_active` is `False` —
both the logging and the nearest-road lookup (spatial index or last-road-first search)
must be fixed before it can be switched on.

**#7 — Blind spot warning allocates per vehicle per cycle.** `_create_rectangles_for_blindspot_warning`
builds a `shapely.Polygon` for **every** car on track, with no distance pre-filter,
before any cheap rejection. Add a squared-distance gate first.

**#9 — AI traffic route search is O(path length) per car per cycle.**
`get_closest_index_on_route` scans the full route for each controlled car, and
`analyze_upcoming_track` runs twice per car (normal + 120 m long-straight lookahead).
Cache the previous index and search a local window around it.

**#10 — Off-track cleanup sends 239 button packets at once.**
`UIManager._state_change` calls `remove_button(0..238)` on every track exit.
`MessageSender` already tracks live button IDs — delete only those.

**#14 — PDC beep spawns a thread per beep** running a blocking `winsound.Beep`.

**#15 — FCW emits `dist_debug` per detected vehicle per cycle** even though its
subscriber is commented out.

## Correctness

**#13 — `Vehicle.acceleration` hardcodes a 100 ms timestep.** `(Δ km/h) * 2.778`
assumes exactly 0.1 s between MCI packets. Changing `assistance_refresh_rate` silently
scales every acceleration by the wrong factor — and acceleration feeds FCW's braking
maths and the adaptive brake lights. See `conventions.md` §3.

**#16 — Misleading comment in `cross_traffic_warning._compute_side`.** It claims LFS's
Y axis grows south and headings are clockwise. Per `InSim.txt` the system is
right-handed with anticlockwise headings. The *code* is correct; the comment is not,
and it will mislead the next person doing geometry work.

**#17 — Siren/strobe state is duplicated.** `UIManager` and `LightAssists` each keep
their own `siren_active` / `strobe_active` booleans, both driven by the same
`button_clicked` event. They can desynchronise (e.g. when the chat command toggles only
one of them). One owner should hold the state and publish it.

**#18 — `sat_nav` has no settings key**, so `NavigationSystem.is_enabled()` is always
`False`. Either add the key or remove the system from `_init_systems`.

## Housekeeping

**#19 — `AI_Cheatsheet.py` is a stale duplicate of `AI_Control.py`.** ~476 lines,
imported by nothing, differs only in that `_normalize_analog` lacks range clamping.
Delete it.

**#20 — `test.py` is not a test.** It is a live-capture script for `MapBuilder`. The
name will confuse any test runner and any reader. Rename to something like
`tools/capture_layout.py`.

**#21 — Unused module `vehicles/VehicleInfo.py`.** A parallel, richer vehicle data
container that nothing constructs. Either adopt it or delete it.

**#22 — Dead local variable** `adaptive_lights` in `LightAssists.process` — assigned in
some branches, never read; the method returns a literal.

**#23 — No automated tests at all.** See `reference/testing.md`.

## Deliberately disabled — leave alone unless asked

- **Automatic emergency braking.** `collision_warning.py` ends with
  `# TODO no automatic braking for now`; `ControllerEmulator` (the actuator) is
  commented out in `AssistanceManager._init_systems`; the
  `automatic_emergency_brake` setting is inert. The whole intervention path needs
  redesign before it comes back.
- **`NavigationSystem`** — see #5 / #18.
