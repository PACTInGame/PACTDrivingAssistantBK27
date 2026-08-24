# Known issues and technical debt

Observations from a full read of the codebase. Ordered roughly by risk. **Not a task
list — do not fix these opportunistically while working on something else.** Mention
them when relevant, fix them when asked.

Keep this file current: remove entries when they are fixed, add systemic defects you
discover. Do not log one-off bugs that were fixed in the same session.

---

## Robustness

**#3 — Dead events.** `assistance_results`, `outsim_data` and `player_data_updated` are
emitted every cycle (or every packet) with no subscribers. `outsim_data` in particular
means the whole OutSim pipeline runs for nothing.

**#4 — Two nearly identical command events.** `send_command_to_lfs` (payload: plain
`str`, subscriber `MessageSender`) versus `send_lfs_command` (payload:
`{'command': str}`, subscriber `UIManager`). Different shapes, different routes, same
purpose. Should be unified into one event with one payload shape.

**#8 — `Controls/wheel.py` cannot work.** Its `try` block raises `ImportError`
unconditionally *after* the import, so `vj` and `setJoy` are never bound and
`press_wheel_brake` would raise `NameError`. Currently unreachable only because
`ControllerEmulator` is commented out in `AssistanceManager`.

**#12 — `own_vehicle` is still mutated while workers read it.** The vehicle
dict is safe now: `VehicleManager` publishes a fresh snapshot dict per MCI frame
and swaps each `VehicleData` object instead of mutating it (`Vehicle.begin_frame`
/ `commit_frame`), so `vehicles_updated` payloads never change under an iterating
worker. `own_vehicle` is different: `own_vehicle_updated` hands out the live
`OwnVehicle` object and OutGauge writes into it at ~30 Hz on the packet thread.
A system that reads `own_vehicle.data.x` and `own_vehicle.data.speed` on
separate lines can still straddle a packet. Bind `data = own_vehicle.data` once
per `process()` call, or give `OwnVehicle` the same swap treatment.

## Performance

**#5 — `navigation.py` is unusable as written.** Dozens of `print()` calls per 100 ms
cycle (the only `print()` calls left in a hot path), and `_get_closest_road` walks every segment of every road each cycle. It is
currently inert because the system is not registered in `AssistanceManager._init_systems`
(and `sat_nav_active` is `False`) — both the logging and the nearest-road lookup
(spatial index or last-road-first search) must be fixed before it can be switched on.

**#9 — AI traffic route search is O(path length) per car per cycle.**
`get_closest_index_on_route` scans the full route for each controlled car, and
`analyze_upcoming_track` runs twice per car (normal + 120 m long-straight lookahead).
Cache the previous index and search a local window around it.

## Correctness

**#35 — FCW's detection quad is self-intersecting, so it is skewed.** The corner order
`[far+1°, near−20°, near+20°, far−1°]` makes edges `p1p2` and `p3p4` cross, and
`point_in_rectangle` covers the union of triangles `(p1,p2,p3)` and `(p1,p3,p4)` —
not the intended wedge. Measured at 50 m with the car pointing north, the covered
sector is about −0.5°…+1.5° around the axis instead of ±1°: a car half a metre to the
right at that range is missed. Only the ordering is wrong, the four points are right.
Changing it changes which cars FCW detects, so it is a product decision, not a silent
fix. `tests/test_collision_warning.py` pins the current behaviour by placing test
vehicles on the axis. The same defect existed in the blind-spot corridors and was
fixed there (WP8) — there the quad was pure geometry with no tuning attached to it,
so reordering the corners was a fix rather than a product decision.

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
(`own_vehicle.data.player_id` itself now comes from `IS_NPL` and survives this,
but without OutGauge there is no speed, rpm or pedal data at all.) An LFS update or reinstall can reset `cfg.txt`, and the setup
wizard never re-runs because of the `.setup_done` flag. Needs a startup validation of
`cfg.txt` plus a "no OutGauge data after N seconds" warning — or, better, dropping the
`cfg.txt` dependency entirely via `SMALL_SSG` (`lfs-setup.md` §5).

**#27 — The HUD may still sit in the area LFS reserves for its own UI.**
`clamp_hud_position()` (`ui/ui_manager.py`) now keeps the whole block — HUD, PDC
column, siren buttons and notification line — inside `0…200` in both axes, so
the arrows can no longer push anything off screen. The second half is
deliberately *not* enforced: the shipped default (`hud_width` 90,
`hud_height` 119) sits inside `L 0…110, T 30…170`, so clamping the HUD out of
that rectangle would relocate every existing user's HUD. Instead the system
menu's "HUD Position" label turns `^1` red while the block overlaps it, and the
move is logged. Whether the default should move out of the rectangle is a
product decision, not a bug fix. `ui.md` §1.3.

**#28 — Vehicle mods fall through hardcoded car tables.** `get_vehicle_size()` returns
`(4.5, 1.8)` for any `CName` it does not know, and LFS mods produce arbitrary `CName`
values. (`CName` is a decoded `str` since WP4 and the lookup accepts both, so the
fall-through is now the only remaining half of this.) PDC sensor geometry is then wrong
for every modded car, and since WP8 the cross-traffic arrival window uses the same table.
FCW no longer relies on it — it detects the unknown name and uses
a conservative 5.0 m length instead (WP7) — but it has to reach into the table's private
`_CAR_SIZES` to do so; a public `is_known_car(cname)` next to `get_vehicle_size()` would
be the cleaner home for that. `conventions.md` §4 has the preferred alternatives.

**#29 — OutGauge stops in any external camera view, freezing all assistance.** LFS only
streams OutGauge from an internal view while on track. Switching to chase/heli/TV camera,
or entering the garage, stops `own_vehicle_updated` — and `process_all_systems()` then
returns immediately every cycle. Same silent-freeze mechanism as #24, but triggered by
ordinary user actions rather than misconfiguration. The 30-second
`start_outgauge()` re-init in `StateHandler.start_game_insim` is a partial workaround.
`conventions.md` §5.3.

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

## Deliberately disabled — leave alone unless asked

- **Automatic emergency braking.** `collision_warning.py` ends with
  `# TODO no automatic braking for now`; `ControllerEmulator` (the actuator) is
  commented out in `AssistanceManager._init_systems`; the
  `automatic_emergency_brake` setting is inert. The whole intervention path needs
  redesign before it comes back.
- **`NavigationSystem`** — not registered in `AssistanceManager._init_systems` since
  WP6. Needs the `print()` calls and the per-cycle nearest-road scan fixed (#5) before
  it can be wired up again.
