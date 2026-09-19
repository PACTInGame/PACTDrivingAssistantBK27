# Known issues and technical debt

Observations from a full read of the codebase. Ordered roughly by risk. **Not a task
list — do not fix these opportunistically while working on something else.** Mention
them when relevant, fix them when asked.

Keep this file current: remove entries when they are fixed, add systemic defects you
discover. Do not log one-off bugs that were fixed in the same session.

---

## Robustness

**#41 — Reported controller reset when installing vJoy; verification pending.**
The existing report says LFS treats
a device it has not seen before as new hardware and discards every existing controller
assignment. Under those reported conditions a wheel driver has to rebuild their
whole control setup by hand, which is the actual reason users call the feature
unfriendly. It happens once, at install, so a backup/restore of `cfg.txt` and
`data\misc\*.csf`/`*.con` around it should be able to fix it. The format is no longer
undocumented -- see `lfs-config-files.md` -- and the numbers in it are device-local, so a
restore is not obviously invalidated by the new device. Still unverified: whether LFS
honours a restored file, and whether the numeric suffix in the filename shifts when a
device is added. `control-intervention.md` §3.2, experiment in `lfs-config-files.md` §7.

**#45 — Something emits a notification at cycle rate, and nobody knows what.**
The reproducible AutoHold flood path is fixed: one attempt per stop/brake phase,
notification only after dashboard confirmation, one timeout diagnostic. This does
not identify the historical bursts conclusively; keep this observation open until
a fresh log names the emitter or a live reproduction matches it.

Two bursts were seen live (14:15:33 and 14:15:47) at roughly **ten dropped notifications
per second**, i.e. one per assistance cycle. The queue holds 8 and shows each entry for
3 s, so while that runs the driver reads a message up to 24 s old — which is what made
the gearbox calibration unusable (`ui.md` §1.7). Not reproduced since, and the emitter is
still unidentified: the old warning printed one anonymous line per drop. It now names the
dropped and the incoming text and is rate-limited to one line per 5 s, so the next
occurrence identifies itself. Note that the queue is *structurally* at its limit — see
§1.7 for why anything periodic must not use it at all.

## LFS integration, screen context and car data

**#24 — OutGauge misconfiguration is undetectable and fatal.** If `cfg.txt` has
OutGauge off or on the wrong port, InSim still connects, the startup connection test
passes, and buttons still draw — but `own_vehicle_updated` never fires, so
`AssistanceManager.own_vehicle` stays `None` and `process_all_systems()` returns
immediately forever. Every assistance system silently does nothing, with no diagnostic.
(`own_vehicle.data.player_id` itself now comes from `IS_NPL` and survives this,
but without OutGauge there is no speed, rpm or pedal data at all.) An LFS update or reinstall can reset `cfg.txt`, and the setup
wizard never re-runs because of the `.setup_done` flag. Needs a startup validation of
`cfg.txt` — or, better, dropping the `cfg.txt` dependency entirely via `SMALL_SSG`
(`lfs-setup.md` §5).

**The "no OutGauge data" half of this is done** (#51): `InputGuard` refuses every
actuation with `no_outgauge` after three silent seconds, and the emergency brake
reports it as its availability reason instead of claiming to be armed. What is still
missing is the *warning-only* half — FCW, blind spot and cross traffic keep working
off MCI, so a driver whose `cfg.txt` is wrong still gets a HUD that looks healthy.

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

**#46 — FIXED 2026-09-19. The throttle cut did not remove a throttle the
driver was already holding.** Kept because the measurement is the reference for
the next change to that path, and because the fix rests on an asymmetry that is
easy to get backwards.

*The defect.* `/key -1 throttle` stops LFS reading **new** presses and does
nothing about an input that is already down: LFS latches the held state and
does not re-evaluate it until the input is released. Measured three times —
`05_fcw_rear_end_keyboard` and `06_fcw_rear_end_keyboard` (throttle back to
1.00 for 23 % and 18 % of the braking phase), and then, with the drivetrain
closed after #47, `08_cross_traffic_warning_traffic_from_left_keyboard` and
`26_foward_collision_warning_hard_scenario`, where OutGauge reported
`Throttle = 1.00` for the **entire** braking phase. The car still stopped — the
brake beats the engine — but over roughly a third more distance than it had to,
and `throttle_cut_availability` reported `None`, i.e. "armed", throughout.

*The fix.* `KeyThrottleCut` now also **un-presses the input**, and re-presses it
on handback only if the driver never let go (`Controls/throttle_cut.py`). That
is the exact manoeuvre the brake path may never perform — §3.1's key-release
trap — and the reason it is allowed here is that its sign is inverted: an
injected release takes away *braking the driver commanded* on one path and
*throttle the driver commanded* on the other, and only the second is the
feature. `misc/physical_keys.py` separates the two states, so the release is
issued against LFS's belief and never against the hardware. Without those hooks
the key path now reports `no_physical_key_tracking` instead of an armed cut it
cannot deliver.

*Still open:* the axis path (`wheel_js`) has not been re-measured against a held
pedal; there is no equivalent of an injected release for an axis, and
`/axis -1 throttle` may have the same latching problem. The original text
follows.

---

**#46 (original) — The throttle cut does not stay effective in `mouse_kb` mode.**
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

**Re-measured 2026-09-19 with the drivetrain closed** (#47 fixed), in
`08_cross_traffic_warning_traffic_from_left_keyboard`: the cut went out at
t = 47.20 and OutGauge reported `Throttle = 1.00` for the whole braking phase,
right up to the handback. So the leak is not conditional on the open clutch —
it is the full-throttle case the design assumes it prevents. The intervention
still stopped the car (41 -> 26 km/h in 1.4 s, collision avoided), so brake
beats throttle in LFS, but a cut that never takes effect should not be
reported as available. The `mouse_kb` + mouse-button path is the one to fix;
whether a keyboard key behaves the same has not been measured.

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

**#51 — FIXED 2026-09-19. An OutGauge socket that never bound disarmed every
actuator, silently, under a log line that said "armed".**

Reported as *"the collision warning only warns, it never brakes"*, with
`Automatic emergency braking is armed.` in the log and correct key bindings
everywhere. The chain, from the session log of 2026-09-19 18:02:

1. A leftover `simulation_tests/_temp/fcw35_addon.py` from an earlier run still
   held UDP 30000. `pyinsim.outgauge` therefore raised `WinError 10048`.
2. `start_outgauge()` logged one line and carried on. `connect()` then emitted
   `lfs_connected` and the next line in the log was `Connected to LFS.`
3. Without OutGauge, `own_vehicle.viewed_plid` is never filled — so
   `is_local_driver` compares `0 == local_plid` and is **False for a driver
   sitting in their own car**.
4. `InputGuard` refused every injection with `not_local_driver`, at *debug*
   level. `EmergencyBrake` returned `{'active': False, 'refused': ...}` and said
   nothing. The warnings kept working, because they run on MCI alone.

So this is #24 with the socket open: the same blindness, arriving by a route
the startup validation of `cfg.txt` would not catch.

Three changes, all in the "fail loudly" direction (`AGENTS.md` §5):

* `start_outgauge()` **closes the old socket before binding**. `StateHandler`
  re-opens it on track entry, so half of the 10048s in that log were
  self-inflicted, and the message pointed at the wrong culprit.
* A failed bind names the likely cause — a second receiver on 30000 — and
  publishes `outgauge_status` (`events.md`).
* `InputGuard` gained `REASON_NO_OUTGAUGE`, checked **before**
  `is_local_driver` so the refusal describes the world rather than a variable,
  and `EmergencyBrake` reports it as its availability reason instead of
  arming. The menu says *"OutGauge port 30000 is taken"*.

The guard fails *closed* here, unlike the Shift reading beside it: a stale
modifier disables a feature, a stale stream means nothing knows whose car it
is looking at. It does **not** guess when nobody has told it whether the socket
is open — same rule as `lfs_has_focus`.

Note the operational lesson as well: a `_temp` harness that owns 30000 outlives
the run it was written for. `simulation_tests/README.md` §2 already says never
to start a second receiver on 30000 alongside the add-on; what it did not say
is that the add-on cannot tell you when one is there. Now it can.

**One deliberate consequence, and it is the open half of this.** OutGauge also
stops for an *external camera* (`conventions.md` §5.3), so a driver in chase
view now loses the emergency brake, auto-hold and the automatic gearbox after
three seconds — with a red menu line and a log warning, not silently. Before,
they kept actuating on a `viewed_plid` frozen at whatever it last was, which is
worse in the case that matters (spectating somebody else in an external view)
and better in the common one (own car, chase cam). The proper fix is
`IS_STA.ViewPLID`: it carries the viewed PLID over InSim, camera-independently,
pyinsim already unpacks it, and nothing in this project reads it
(`conventions.md` §5.2). Feeding `own_vehicle.viewed_plid` from it would make
`is_local_driver` answerable without OutGauge and let the key path — which
needs no gauges at all — keep working in chase view. Not done here: it is a
separate change to `StateHandler`, the `state_data` payload and `OwnVehicle`,
and this session was scoped to the silent-failure bug.

**#52 — FIXED 2026-09-19. The car you were running into raised an acute blind
spot warning, on whichever side the noise picked — sometimes both.**

Reproducible by rear-ending the car in front, with that car the only other one
on track, and seen in `simulation_tests` recordings where the add-on was not
even running.

`BlindSpotWarning`'s **level 1** geometry is a corridor from 90° to 180° — it
cannot see anything in front. The **acute** stages (2 and 3) never had that
bound; their only protection against longitudinal traffic was
`_is_plain_following`, which requires the two headings to be parallel within
2°. A collision breaks that in the first frame: both cars rotate, the pair
becomes "interesting" again, `contact_window` finds an overlap (the outlines
really are touching), and `_is_on_left` decides the side from a cross product
that is essentially zero straight ahead. Measured on the rebuilt scene, a
lateral offset of ±5 cm flips the side; the 0.5–2 s hold time then leaves both
sides lit.

`_is_longitudinal_traffic` now rejects a vehicle that is **both** ahead of our
front bumper (longitudinal offset beyond half our own length) **and** in our
lane (the same tolerance `_is_plain_following` uses). Both halves are needed:
"ahead" alone would drop the car drawing level with us in the next lane, which
is the case the acute stage exists for; "in our lane" alone is the condition
that falls apart on impact.

**#53 — FIXED 2026-09-19. The acute blind spot warning beeped at a standstill
for anything that drove past.**

Reported from a red light; replay `BL1_RB4`. The acute stages had no lower
speed bound of their own. Standing still, our outline does not move over the
prediction horizon, so every contact `contact_window` finds comes from the
other car's motion alone — and neither warning nor braking is an answer to
that when we are already stopped. `MIN_ACUTE_OWN_SPEED_KMH` (1.0 km/h) gates
the whole acute branch. **Level 1 is deliberately unaffected**: that somebody
is sitting in the mirror's blind spot is worth knowing precisely when the
driver is about to pull out.

The level-3 merge case survives, because a driver pulling into traffic is
moving while they do it — `tests/test_blind_spot.py` holds both halves.

**#54 — FIXED 2026-09-19. Warning sounds stacked on top of each other and
clipped.**

Reported as *"do - do - do - rauschen und knacken - do - do - do"* when several
cars were in the acute blind spot stages at once. Four separate faults in
`misc/audio_player.py` and its callers, all of them the same mistake — treating
warning tones as a mixdown rather than as one voice:

* `_update_collision_warning_display` emitted `play_audio` **three times in the
  same line** to beep three times. pygame played three simultaneous copies of
  one waveform: three times the amplitude, ~9.5 dB up, and into clipping.
  There is now a `repeat` key, played back to back with `Channel.queue`.
* Every call took whatever channel was free. A flapping acute level — several
  cars, hold times expiring out of step — put a dozen copies of a 0.88 s sample
  on top of each other within a second, until pygame ran out of channels and
  began cutting running tones off. One **reserved channel** now carries every
  warning and a new one replaces the old.
* `UIManager._blind_spot_beep(force=True)` bypassed its own repeat interval on
  every rising edge, and an edge is not rare when two hold times interleave.
  The edge keeps what it needs (it sounds immediately instead of waiting for
  the next UI pass) and loses what it should never have had.
* `mixer.Sound(file)` re-read the WAV from disk on **every** beep — blocking
  I/O inside a 50 ms cycle (`AGENTS.md` §1), which is itself a source of buffer
  underruns. All files are loaded once at startup.

The mixer is also initialised explicitly now: 48 kHz to match the files (no
resampling) and a 1024-sample buffer instead of pygame's 512, which is ~12 ms
and too tight next to a running LFS. A machine with no audio device logs one
line and loses its warning tones instead of taking the app down at startup.

**#55 — The add-on is completely inert during a replay, and nothing says so.**

Not a regression and arguably not a bug — but it costs a whole verification
route, so it is worth knowing before someone tries the same thing. A replay
sets `ISS_REPLAY` and **not** `ISS_GAME` (measured, `ui.md` §1.1), so
`on_track` is False and `AssistanceManager` skips every system. No warning, no
PDC, no HUD. Anyone checking a fix by loading the replay of the incident gets
silence and cannot tell it apart from the fix working.

Blocking *actuation* there is correct and must stay. Whether warnings and the
HUD should run is an open product decision; it would be a `replay` flag in
`state_data`, with `InputGuard` refusing on it, and `AssistanceManager` gated
on `on_track or replay`.

Until then, the way to check behaviour against a replay is offline: capture
`IS_MCI` with a second InSim client (a replay streams it normally at 10 Hz) and
feed the frames through the systems in a script. Note that `IS_NPL` does **not**
arrive in a replay, so `CName` is unavailable and `IS_STA.ViewPLID` is the only
pointer to the player's own car.

## Deliberately disabled — leave alone unless asked

- *(nothing at present)* Automatic emergency braking is no longer inert: it lives
  in `assistance/emergency_brake.py`, is armed by `automatic_emergency_brake == 2`,
  and was measured preventing a rear-end collision end to end —
  `simulation_tests/README.md` §13. `Controls/wheel.py` and
  `assistance/controller_emulator.py` are still dead code; see #8.
