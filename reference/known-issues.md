# Known issues and technical debt

Observations from a full read of the codebase. Ordered roughly by risk. **Not a task
list — do not fix these opportunistically while working on something else.** Mention
them when relevant, fix them when asked.

Keep this file current: remove entries when they are fixed, add systemic defects you
discover. Do not log one-off bugs that were fixed in the same session.

---

## Robustness

**IS_CON decoder is still pre-v10.** `pyinsim/insim.py:IS_CON` expects a 40-byte
packet with a 16-bit time, while newer InSim sends 44 bytes with `SpW` and a
32-bit millisecond timestamp — the 44-byte case raises `struct.error` *inside the
asyncore loop*, which drops the connection. `CarContact` also has the signedness
inverted: the pedal nibbles, speed and the two angle bytes are unsigned, the two
accelerations signed. The test harness carries a corrected decoder in
`simulation_tests/insim_patch.py`, which picks the layout by `Size` and is applied
to the **tracer's own process only**. The add-on's shared decoder remains unfixed;
it currently does not subscribe to CON, so nothing is broken today — but anything
that starts consuming CON must port that decoder first. Other legacy layouts such
as IS_RIP should be checked before enabling new consumers.
Source: https://www.lfs.net/programmer/insim. `C:\LFS\docs\InSim.txt` is now only
a link to that page, so the layout cannot be confirmed from disk.

**#3 — Dead events.** `assistance_results`, `outsim_data` and `player_data_updated` are
emitted every cycle (or every packet) with no subscribers. `outsim_data` in particular
means the whole OutSim pipeline runs for nothing.

**#4 — Two nearly identical command events.** `send_command_to_lfs` (payload: plain
`str`, subscriber `MessageSender`) versus `send_lfs_command` (payload:
`{'command': str}`, subscriber `UIManager`). Different shapes, different routes, same
purpose. Should be unified into one event with one payload shape.

**#8 — `Controls/wheel.py` and `assistance/controller_emulator.py` are dead.** Both
are superseded by `assistance/emergency_brake.py` + `Controls/brake_key.py` and are no
longer imported anywhere. `wheel.py` never worked: its `try` block raises `ImportError`
unconditionally *after* the import, so `vj` and `setJoy` were never bound. Delete both
once the vJoy axis path is rebuilt in their place.

**#41 — Installing vJoy destroys the user's LFS controller configuration.** LFS treats
a device it has not seen before as new hardware and discards every existing controller
assignment. A wheel driver who installs vJoy for automatic braking has to rebuild their
whole control setup by hand, which is the actual reason users call the feature
unfriendly. It happens once, at install, so a backup/restore of `cfg.txt` and
`data\misc\*.csf`/`*.con` around it should be able to fix it. The format is no longer
undocumented -- see `lfs-config-files.md` -- and the numbers in it are device-local, so a
restore is not obviously invalidated by the new device. Still unverified: whether LFS
honours a restored file, and whether the numeric suffix in the filename shifts when a
device is added. `control-intervention.md` §3.2, experiment in `lfs-config-files.md` §7.

**#42 — `auto_hold` may be pressing a handbrake key a wheel driver does not have.**
In `wheel_js` mode LFS lets the driver choose *per function* whether clutch and
handbrake come from an axis or a key. `InputGuard`'s docstring still claims a wheel
user always has a keyboard handbrake binding; for anyone who set handbrake to an axis,
`AutoHold`'s injected key does nothing and reports success. Same class of silent
failure as the one emergency braking just had. Which of the two the driver chose is
readable off disk without touching LFS: the `handbrake` entry of the axis table in
`data\misc\<Device>.csf` is `0xFFFF` exactly when the function is not on an axis
(`lfs-config-files.md` §4).

**#45 — Something emits a notification at cycle rate, and nobody knows what.**
Two bursts were seen live (14:15:33 and 14:15:47) at roughly **ten dropped notifications
per second**, i.e. one per assistance cycle. The queue holds 8 and shows each entry for
3 s, so while that runs the driver reads a message up to 24 s old — which is what made
the gearbox calibration unusable (`ui.md` §1.7). Not reproduced since, and the emitter is
still unidentified: the old warning printed one anonymous line per drop. It now names the
dropped and the incoming text and is rate-limited to one line per 5 s, so the next
occurrence identifies itself. Note that the queue is *structurally* at its limit — see
§1.7 for why anything periodic must not use it at all.

**#12 — `own_vehicle` is still mutated while workers read it.** The vehicle
dict is safe now: `VehicleManager` publishes a fresh snapshot dict per MCI frame
and swaps each `VehicleData` object instead of mutating it (`Vehicle.begin_frame`
/ `commit_frame`), so `vehicles_updated` payloads never change under an iterating
worker. `own_vehicle` is different: `own_vehicle_updated` hands out the live
`OwnVehicle` object and OutGauge writes into it at ~30 Hz on the packet thread.
A system that reads `own_vehicle.data.x` and `own_vehicle.data.speed` on
separate lines can still straddle a packet. Bind `data = own_vehicle.data` once
per `process()` call, or give `OwnVehicle` the same swap treatment.

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

**#29 — OutGauge stops in any external camera view, freezing the gauge half of
`own_vehicle`.** LFS only streams OutGauge from an internal view while on track.
Switching to chase/heli/TV camera, or entering the garage, stops the OutGauge side of
`own_vehicle_updated`, so rpm, gear, pedals and the dash lights stand still. Same
silent-freeze mechanism as #24, but triggered by ordinary user actions rather than
misconfiguration. The 30-second `start_outgauge()` re-init in
`StateHandler._start_game_insim` is a partial workaround. `conventions.md` §5.3.

**Half of this is fixed (2026-09-19).** `VehicleManager` now also publishes
`own_vehicle_updated` from every MCI frame, which arrives in every camera view. So the
position/heading/speed half keeps moving, and — the part that actually broke things —
`AssistanceManager.process_all_systems()` no longer returns immediately because
`self.own_vehicle` is still `None`. Before, an app started while LFS already sat in a
chase camera ran **no** assistance system at all, including the AI traffic, which needs
nothing from OutGauge. What is left of #29 is genuinely OutGauge-only data.

**#46 — The throttle cut does not stay effective in `mouse_kb` mode.**
Measured in `simulation_tests` run `05_fcw_rear_end_keyboard_20260919-104215`
(mouse buttons as pedals, `/key mousel throttle`): `KeyThrottleCut` sent
`/key -1 throttle` at the start of the intervention and OutGauge's `Throttle`
dropped to 0.04 as expected — and then climbed back to **1.00 for 23 % of the
braking phase** while the driver (the replay) kept the left mouse button held.
Nothing in the add-on re-bound it; no `send_command_to_lfs` went out between the
cut and the handback. So unassigning the *key* does not reliably stop LFS from
reading a held mouse button as throttle, and the guarantee in
`control-intervention.md` §3 ("engaging needs no knowledge") does not hold for
this path. It cost no deceleration in that run only because the clutch happened
to be open for most of it — 8.86 m/s² with the throttle on versus 8.73 m/s² with
it off. Do not rely on the cut until it is re-measured with the clutch engaged.
Reproduced in scenario `06_fcw_rear_end_keyboard` with a different car (RB4):
throttle back to 1.00 for 18 % of the braking phase.

**#47 — FIXED 2026-09-19. The automatic gearbox fought LFS's own, and walked
up the whole box against the rev limiter.** Kept here because the measurement
is the reference for the next gearbox change, and because one half of the fix
has not been exercised in game yet.

*Two independent defects, and the first one hid the second.*

**(a) It did not know LFS was also shifting.** `PIF_AUTOGEARS` says LFS's own
automatic gearbox is on. The add-on never read it — worse, it could not have:
it bound `ISP_NPL` only, and `IS_NPL` carries the flags as they were on
joining. SHIFT+G on track sends `IS_PFL` and nothing else (`insim.md` §7). Two
automatics on one crankshaft shift against each other. `Gearbox` now stands
down on that flag, in both directions, and says so through
`gearbox_availability`.

**(b) The shift point was read off a free-revving engine.** A shift holds the
clutch for `CLUTCH_HOLD_S` (0.30 s) and LFS ramps it back over a further
~0.18 s, while the next same-direction shift is allowed after 0.40 s. The
clutch therefore never closed between shifts, the engine sat on the limiter
the whole time — `omega_engine` is not coupled to the wheels with the clutch
open — and that satisfied the upshift condition again, and again. Measured in
`15_gearbox_tests` at t=42.0 s (FZ5): key at 41.96, clutch open at 42.00, gear
2→3 at 42.05 with the rpm *rising* 6641 → 6983, then 3→4 at 42.45 and 4→5 at
42.84, all at a constant 55 km/h. `_drivetrain_is_settled()` now refuses to
decide while `clutch > 0.05` and for 0.25 s after it closes. A second face of
the same loop — upshifting 3 → 4 → 5 → 6 under full emergency braking — is
additionally blocked by `MAX_BRAKE_FOR_UPSHIFT`.

Before / after, `15_gearbox_tests` (FZ5, RB4, UF1, 274 s), moving phase:

| | baseline | before | after |
|---|---:|---:|---:|
| gear changes | 46 | **111** | 49 |
| bursts of 3-4 shifts inside 0.6 s | 0 | **5** | 0 |
| clutch open, share of moving time | 6.2 % | **18.0 %** | 6.0 % |
| top speed | 214.0 km/h | **196.0 km/h** | 211.4 km/h |

And what it cost the rest of the test set — mean divergence of the ego speed
profile from the baseline *before the add-on acted*, which is the number that
decides whether a result may be attributed to the add-on at all:

| scenario | before | after |
|---|---:|---:|
| `07_fcw_rear_end_keyboard` | 3.9 km/h | **0.6** |
| `10_false_positive…` | 7.8 km/h | **0.1** |
| `12_high_speed_collision_warning` | 7.8 km/h | **0.2** |

**Not yet verified in game: (b).** All eleven scenarios of the 2026-09-19
add-on run were driven with LFS's automatic gearbox on, so the gearbox stood
down for the whole session and never took a shift decision — `gearbox_events`
recorded exactly one `gearbox_availability` = `lfs_auto_gears` and no other.
The drivetrain gate is covered by `tests/test_actuation.py` and by the physics,
not by a live run. **Re-run `15_gearbox_tests` with SHIFT+G set to manual to
close that gap.**

**#49 — FIXED 2026-09-19. A car that vanished from MCI stayed in the vehicle
list for ever, and the emergency brake braked for it.**

`VehicleManager` only ever *added* to `self.vehicles`. Removal was wired to
`IS_PLL` — and **LFS does not send `IS_PLL` when a race ends.** Measured over
nine consecutive scenarios (`simulation_tests`, 2026-09-19): **zero** `IS_PLL`,
while every car disappeared between races and came back under a different
PLID. The AI is PLID 3 in scenarios 05–11 and PLID 2 in 13.

The ghost is worse than dead weight, because `_apply_frame` recomputes
`distance_to_player` **only for the cars in the frame**. A vehicle nobody
touches therefore keeps the distance it had when it was last seen, for ever,
while the player drives away. Every consumer that iterates `vehicles` — FCW,
BSW, CTW — treats it as a real car at that frozen distance.

What it cost, scenario `13_car_in_front_brakes_collision_warning_test`, add-on
run `20260919-131719`: a ghost PLID 3, left over from scenario 11 and frozen at
**6.06 m**, sat inside FCW's detection wedge. At the race start FCW demanded
10.97 / 15.94 / 12.05 m/s² against it (live: 12.71 / 16.43 / 10.28), the
emergency brake engaged at 12.5 km/h and the car never got away: peak speed
16.9 km/h against the baseline's 82.8.

**The stutter is the same bug seen from the other end.** Each engage dropped
the car below `COMMIT_TO_STOP_SPEED_KMH`, `_wants_brake_while_stopping()` then
handed back because the driver was on the throttle, the car accelerated past
FCW's 10 km/h floor, the ghost demand returned — **eight engage/release cycles
in 4.5 s**. There is no lockout after a release, so nothing damps that loop.
With the ghost gone it cannot start, but the loop itself is still reachable by
any genuine demand that survives a handback; see the open question below.

The fix: `_apply_frame` now drops vehicles that are not in the frame. A frame
that LFS marked complete (`CCI_LAST`, or a short packet) is authoritative —
"not in it" means "gone". The timeout path (known-issues #6) deliberately
publishes a fragment, so there only age counts, `STALE_VEHICLE_S` = 1.0 s,
at least five frames even at the slowest allowed rate. `Vehicle.last_seen`
carries the stamp.

**Two things this changes for everyone:** `vehicles` now shrinks as well as
grows, and a car in a fragment-only frame survives up to a second longer than
it exists. Any code that assumed a PLID stays in the dict once seen has to be
re-read — `tests/test_vehicle_model.py` had two tests encoding exactly that
assumption.

**Still open, deliberately not changed here.** `EmergencyBrake` has no
re-engage lockout and no minimum hold: an intervention that hands back because
the driver is on the throttle can re-engage on the very next cycle. That is
correct for a real hazard that persists, and a stutter for anything else.
Decide it together with #48, and with `control-intervention.md` §3 open —
taking the brake and giving it back repeatedly is worse than either choice.

**#50 — FIXED 2026-09-19. AI traffic kept talking to cars that no longer existed,
and to the wrong car when the camera had moved.**

Three separate defects, all reported from the same session
(`simulation_tests/scenarios/20_at_traffic_test`, app log 2026-09-19 14:21).

*One chat line per car when the race ends.* `IS_AIC` for a PLID LFS does not know
answers with *"IS_AIC - no driver to control"*. `_process_active` re-requested
`IS_AII` for every assigned PLID **before** it checked which cars had departed, so
the single pass in which a race ended sent one packet per car; the stop sequence
did the same for two seconds if it was triggered afterwards. Every send now goes
through `AIDriver._control`, which drops anything outside `_live_plids`, the
departed check runs first, and leaving the race or changing track *abandons*
control instead of stopping gracefully — the cars are already gone, so there is
nothing to brake. `ai-traffic.md` §2.0, §2.1.

*The own PLID could be a foreign car.* `OwnVehicle.data.player_id` falls back to
the PLID OutGauge reports while no `IS_NPL` for the local driver has been seen,
and TAB points that at any car on track. `_apply_frame` used it directly, so the
watched car was merged into `OwnVehicle` **and dropped out of `vehicles`** — the
one dict AI traffic reads. `VehicleManager._own_plid()` now refuses a guess that
`players` calls an AI or a remote player, and `AIDriver` excludes the local driver
by `local_plid` rather than by `data.player_id`. `conventions.md` §5.4.

*The local PLID never followed a race restart.* "The first candidate keeps the
title" was unconditional, and LFS sends no `IS_PLL` at a race start, so after a
`/restart` — which is exactly what starting AI traffic does — `local_plid` could
point at a car that now belongs to somebody else. `IS_RST` is now bound, re-opens
the election, and triggers a `TINY_NPL`; a stale own car (missing from MCI for
`STALE_VEHICLE_S`) does the same.

*Route assignments were permanent.* A car adopted in the moment before `/restart`
teleported it kept steering towards a road on the other side of town at full
throttle. It is now re-assigned after a second spent more than 25 m from its road.
**The measure matters more than the threshold**: judged by the nearest stored route
*point*, a car driving correctly down SO road 33 reads 20.8 m off, because the
points there are 41.3 m apart and the closed loop's seam puts the nearest one two
segments away. Judged against the road over a window of segments it reads 3.0 m,
on every shipped road. `ai-traffic.md` §2.

**Two more things changed with them**, because the same session showed a contact
the AI should have avoided. Collision avoidance now tests a forward **corridor**
instead of a ±12° cone — a cone is ±1.06 m wide at 5 m, so a stopped car half a
width off centre was invisible where braking mattered most — and the allowed
following speed comes from Gipps' safe-speed law with the lead car's speed in it,
instead of a straight line that ignored it. Both are in `ai-traffic.md` §3. The
combination is untested in the game; `21_ai_traffic_started_from_another_car` is
the scenario that will say whether it holds.

## Deliberately disabled — leave alone unless asked

- *(nothing at present)* Automatic emergency braking is no longer inert: it lives
  in `assistance/emergency_brake.py`, is armed by `automatic_emergency_brake == 2`,
  and was measured preventing a rear-end collision end to end —
  `simulation_tests/README.md` §13. `Controls/wheel.py` and
  `assistance/controller_emulator.py` are still dead code; see #8.
