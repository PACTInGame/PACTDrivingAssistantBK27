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

**#11 — Global keypress injection is under-guarded.** `AutoHold` and `Gearbox` press
real OS keys with `pyautogui`. `AutoHold` guards against `dialog`/`text_entry`;
**`Gearbox` guards against nothing**. Neither checks whether the user is holding
**Shift** (LFS binds many SHIFT+key commands, so an injected key becomes a command) nor
whether LFS is the foreground window (so the app can type into the user's browser).
Shift state is available as `OutGaugePack.Flags & OG_SHIFT` and is simply not read.
Full rule set in `ui.md` §1.4.

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

**#34 — `point_in_rectangle` judges by cross-product sign only.** For a proper rectangle
it is correct, but a **degenerate** one (zero width or all four corners equal) contains
the entire line it lies on, or the whole plane. Every caller currently builds its
rectangle from a non-zero vehicle or object size, so nothing is broken today — it is a
trap for the next caller that computes a size from packet data.
`tests/test_helpers.py` carries two `xfail` cases for it.

## LFS integration, screen context and car data

**#24 — OutGauge misconfiguration is undetectable and fatal.** If `cfg.txt` has
OutGauge off or on the wrong port, InSim still connects, the startup connection test
passes, and buttons still draw — but `own_vehicle_updated` never fires, so
`AssistanceManager.own_vehicle` stays `None` and `process_all_systems()` returns
immediately forever. Every assistance system silently does nothing, with no diagnostic.
`own_vehicle.data.player_id` also comes only from OutGauge, so the app cannot even
identify the player's car. An LFS update or reinstall can reset `cfg.txt`, and the setup
wizard never re-runs because of the `.setup_done` flag. Needs a startup validation of
`cfg.txt` plus a "no OutGauge data after N seconds" warning — or, better, dropping the
`cfg.txt` dependency entirely via `SMALL_SSG` (`lfs-setup.md` §5).

**#25 — Screen context is detected far too crudely.** `StateHandler` derives only
`on_track = ISS_GAME and not ISS_FRONT_END`, plus `text_entry` and `dialog`. It ignores
`ISS_VISIBLE` (the authoritative "are our buttons on screen" flag), `ISS_SHIFTU` and
`ISS_MULTI`, and it never binds `ISP_CIM` — so the app cannot distinguish the main menu,
the entry screen, the garage and its nine submodes, options, or car/track select. Its
own `in_game_interface` / `submode_interface` fields are initialised to 0 and never
written; pyinsim already has `IS_CIM` and every constant. See `ui.md` §1.

**#26 — The user can clear our buttons and they never come back.** `BFN_USER_CLEAR`
(SHIFT+B) and `BFN_REQUEST` (SHIFT+B / SHIFT+I) arrive as inbound `IS_BFN` packets and
neither is bound. The HUD recovers because it repaints every 50 ms, but the menu, PDC
and notification buttons stay gone until something else redraws them. `ui.md` §1.5.

**#27 — HUD position can break the LFS entry screen.** Buttons inside
`L 0…110, T 30…170` make LFS keep that rectangle clear of its own UI. `hud_width` /
`hud_height` are user-adjustable in 2-unit steps with no constraint, so a user can move
the HUD into that area and make LFS's own entry-screen menus vanish, with no way to
understand why. Either clamp the HUD out of the reserved area or warn. `ui.md` §1.3.

**#28 — Vehicle mods fall through hardcoded car tables.** `get_vehicle_size()` returns
`(4.5, 1.8)` for any `CName` it does not know, and LFS mods produce arbitrary `CName`
values. PDC sensor geometry and FCW's car-length term are then wrong for every modded
car. `conventions.md` §4 has the preferred alternatives.

**#29 — OutGauge stops in any external camera view, freezing all assistance.** LFS only
streams OutGauge from an internal view while on track. Switching to chase/heli/TV camera,
or entering the garage, stops `own_vehicle_updated` — and `process_all_systems()` then
returns immediately every cycle. Same silent-freeze mechanism as #24, but triggered by
ordinary user actions rather than misconfiguration. The 30-second
`start_outgauge()` re-init in `StateHandler.start_game_insim` is a partial workaround.
`conventions.md` §5.3.

**#30 — Own PLID is derived only from OutGauge, so it follows the camera.**
`OwnVehicle.update_outgauge_data` sets `data.player_id = packet.PLID`, but that field is
the **viewed** player. Pressing TAB or spectating silently repoints the entire
`OwnVehicle` object at somebody else's car. Harmless (arguably correct) for the HUD and
warnings; actively wrong for anything that actuates — auto-hold, gearbox, light and
siren commands act on the local car while the data describes another one.
`IS_NPL` carries `UCID` and `PType`, which identify the local human driver
camera-independently, but `VehicleManager._handle_player_joined` discards both.
`conventions.md` §5.

**#31 — AI drivers are detected by substring match on the player name.**
`AIDriver._is_local_ai_vehicle` returns true for `b'AI' in pname`, so a human called
MAIK, RAID or CAIN is adopted by the traffic controller and driven by it. `IS_NPL.PType`
bit 1 is the authoritative AI flag and is not read. `conventions.md` §5.5.

**#32 — Key bindings are guessed, never asserted.** The `user_*_key` settings are the
app's assumption about what the user bound in LFS; nothing verifies them, so a mismatch
makes auto-hold and the gearbox silently do nothing or press the wrong control. LFS
accepts `/key <key> <function>`, which would let the app *set* the binding it is going to
inject. Same class of problem for `user_axis_*` / `vjoy_axis_1`, where an unset value
means the brake-intervention `/axis` switch points at the wrong device.
`control-intervention.md` §2, §3.1.

**#33 — Keyboard intervention has an unsolved key-release trap.** If an intervention
injects a key the user is physically holding and then releases it, LFS sees the release
and stops braking while the driver is still pressing the pedal key. Any future automatic
braking on keyboard/mouse must track real hardware key state and suppress its own
release. `control-intervention.md` §3.2.

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

## Deliberately disabled — leave alone unless asked

- **Automatic emergency braking.** `collision_warning.py` ends with
  `# TODO no automatic braking for now`; `ControllerEmulator` (the actuator) is
  commented out in `AssistanceManager._init_systems`; the
  `automatic_emergency_brake` setting is inert. The whole intervention path needs
  redesign before it comes back.
- **`NavigationSystem`** — see #5 / #18.
