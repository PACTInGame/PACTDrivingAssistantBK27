# Known issues and technical debt

Only unresolved defects and outstanding live verification belong here. Remove
fixed or withdrawn findings; keep durable behaviour and design details in the
relevant reference documents. IDs remain stable when entries are removed.

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

## LFS integration and car data

**#28 — Vehicle mod dimensions are unknown.**
Unknown `CName` values still use assumed dimensions. PDC and path-conflict geometry
use a conservative 5.0 × 2.1 m fallback; other uses of `get_vehicle_size()` retain
4.5 × 1.8 m. Neither measures the mod, and mods can exceed the conservative fallback.
A verified runtime source for actual dimensions is still needed (`conventions.md`
§4); warning tests alone cannot establish the dimensions.
Deferred for now: manual per-mod profiles do not scale to the changing mod
catalogue. A future investigation may examine locally downloaded mod geometry
and invalidate cached dimensions when files change; no usable parser is verified.

**#57 — The digital brake cannot be aimed, so the engage threshold has to absorb it.**
`Controls/brake_key.py` brakes fully or not at all, so an intervention that starts at a
demand of `x` still decelerates at whatever the tyres give (~9.7 m/s² measured) and
stops about `1 − x/9.7` of its braking distance early. Both ends of that trade were
measured in game on 2026-09-20 over the recorded rear-end scenarios:

| FCW publishes from | remaining collisions | gap at standstill |
|---|---|---|
| level 2 (engage ≈ 6.0 m/s²) | none | 4 m, 6 m, 11 m short (06, 07, 12) |
| level 3 (engage ≈ 7.5 m/s²) | 13 taps at 18 km/h; 32 sub-case 4 hits at 44 km/h | 0.7–1.6 m |

Level 3 ships, because stopping metres short reads as the assistant panicking and is
what a driver notices every time, while the two remaining impacts are heavily reduced (baseline
63 and 85 km/h). **Neither column is the right answer.** The fix is to modulate the
digital output — duty-cycling the key against `OutGaugePack.Brake`, the way
`control-intervention.md` §3.1 already describes — so the achieved deceleration follows
the demand instead of saturating. Then the engage point can move back to
`EmergencyBrake`'s own 6.0 m/s² floor without the car stopping short. The analog
(`wheel_js`) path already closes that loop and should be measured the same way, to see
whether it lands in the 1–2 m band on its own.

Sub-case 4 of scenario 32 is the hard end of this: the lead accelerates away at 5 m/s²
and then brakes at 10 m/s² to a standstill, so the braking distance is taken away after
the geometry is already committed. A light tap there is acceptable; 44 km/h is not.

## Outstanding live verification

**#58 — Release acceptance on a clean Windows installation is pending.**
The PyInstaller package has an offline import/asset smoke test, but clean-user
wizard interaction, no-vJoy operation in game, update retention across released
packages, audio and crash handback must be exercised on the packaged executable.
See `RELEASE.md`. Development-machine tests do not establish this.

**#59 — Guided axis commissioning and native mouse-joystick intervention are incomplete.**
The setup wizard configures LFS telemetry and explains manual controls. It does
not safely determine arbitrary global LFS axis assignments or verify an unused
virtual axis and polarity. `vjoy_brake_calibrated` defaults false, so new axis
users remain warning-only until manually commissioned. Native mouse-axis
sentinels in controller files cannot be restored as ordinary `/axis` indices;
full support needs live verification and a dedicated restoration path. Do not
advertise automatic intervention for all controller configurations yet.

**#60 — Recovery-marker persistence is synchronous during intervention transitions.**
The marker is now atomic and takeover fails closed when it cannot be written,
but its file write/fsync still runs on the assistance thread on engage/release.
Slow storage can exceed the cycle budget. An acknowledged out-of-process or
pre-recorded recovery protocol is needed to remove this I/O without opening a
crash window. No steady-state marker write is performed.

**#50 — Two AI-traffic sub-cases still unexercised.**
The combined live check is done (`ai-traffic.md` §3, 2026-09-20): all 23 AI cars
adopted with the camera on another car, local player excluded through TAB and
`/restart`, zero `no driver to control`, nothing dropped during a 21-TAB camera
walk, and an AI stopping 6.1 m behind the standing player at 4.2 m/s² with no
contact. Two things that run did **not** exercise:

* **a stopped car offset from the lane centre.** The measured lateral offset was
  0.1 m, so the easy case. The forward corridor is what decides whether an
  off-centre obstacle is seen at all.
* **route re-assignment after a teleport.** `was off its route - re-assigned`
  never appeared, so that path is still unproven in game.

Both need a recording that puts the player off-centre and resets an AI car out of
its route. `21_ai_traffic_started_from_another_car` does neither.

**#61 — Self-parking: the car ends up offset to one side of the space.**
Third live session, 2026-09-20. The manoeuvre completes and the car ends
correctly *aligned* — same heading as the car in front — but too far to one
side. Not a follower defect: the telemetry through that run shows demanded
and measured curvature matching to three decimals (`kappa -0.167/-0.167`), so
the car drove the plan it was given. Suspect `ParkingSlot.target` for a space
bounded on **one** side only (the run was `parallel on the right … open`),
and `_replan_from_here` inheriting how far out from the row the driver
stopped. Reproduce in `tests/test_parking_endtoend.py` with a one-sided scene
before changing anything. Full context: `reference/park-assist-handover.md`.

**#62 — Self-parking needs two clicks to start.**
The first click installs the `pynput` hooks — over 100 ms, so it happens off
the assistance thread and that click is refused with
`no_physical_key_tracking`. Correct mechanically, bad product behaviour. Fix
by installing the hooks when a space is first *offered* rather than when it is
accepted: the offer has already survived `OFFER_SETTLE_S` and is a strong
enough signal, and a second or more passes before any click arrives.

**#63 — A completed self-park does not end on the brake.**
`_finish(STATE_DONE, …)` releases every input and the car creeps on out of
the space. The `STATE_STOPPING` machinery that fixes exactly this already
exists and is tested, but is only wired to *aborts*. Decide whether a
completed park should hold the brake until the driver acts, or stop once and
release.

**#64 — Do not learn the parking turn radius from `CurvatureModel.gain`.**
Tried and reverted within one session, recorded here so it is not re-added.
`1 / gain` looks like the car's full-lock radius, but adopting it is a
positive feedback loop: a larger radius makes the next plan flatter, a flatter
plan needs less steering, the gain is then only measured at part lock where it
reads lower, and the radius grows again. Measured: two manoeuvres planned at
6.9 m and both finished, the fit adopted 11.0 m, and every manoeuvre after
that planned at 12.7 m, never exceeded 0.76 of lock, needed 25–38 m of path
and ended `off_track`. A calibration *sweep* at full lock — the way
`Controls/throttle_axis_check.py` treats the pedal — would be sound; an
estimate taken from the manoeuvre it then changes is not.
