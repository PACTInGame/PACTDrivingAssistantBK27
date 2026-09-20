# Self-parking — where it stands, and what to do next

Written at the end of the second live-test session. Delete this file once the
open items below are done and the feature has had a clean in-game run.

## What changed in this session

The feature went from *never having moved a car* to **completing a five-stroke
parallel manoeuvre in game**. It ends up in the space but visibly crooked, and
it drives far too aggressively. Eight defects were found, all of them by live
runs, all of them now fixed and covered by tests (1531 green).

| # | Defect | Fix |
|---|---|---|
| 1 | Every manoeuvre aborted on its own brake within a cycle | `OutGauge.Brake` is the *merged* pedal, and the brake is a **key** — 0.35 and 1.0 are the same keystroke. The reading is only believed while the manoeuvre is not braking (`DRIVER_BRAKE_OVERRIDE`, `BRAKE_SETTLE_S`). |
| 2 | Only ever the first space was offered | `_rank` pinned the held space absolutely. Stickiness is now beatable by a clearly better candidate (`_clearly_better`, `STICKY_MARGIN_M`). |
| 3 | The offer blinked on and off at 2 Hz | `_plan_best_of` offers the best *drivable* space, which need not be the best-ranked one, so the undrivable one came straight back to the top. Refusals are remembered (`_unplannable`, `UNPLANNABLE_TTL_S`). |
| 4 | Spaces were offered that could never be driven | The detector wanted 1.2 m more than the car; the planner needs 2.5 m. Measured over a grid and pinned by the new `tests/test_parking_agreement.py`. |
| 5 | A space driven past was forgotten and never offered | The "driven past" flag was dropped the moment the space left one scan, and open-ended spaces leave the scan constantly. It now expires by time (`PASSED_TTL_S`). |
| 6 | Driving past the space *in front of* a car marked the one *behind* it | A single parked car bounds two spaces and both had the same `slot_id`. `ParkingSlot.open_side` now distinguishes them. |
| 7 | **Steering did nothing at all** | `park_assist_mouse_span` was 0.25, so full lock moved the cursor 240 px of a 1920 px window and the car drove a measured 500 m radius. LFS uses nearly the whole window width. Now 0.95, with a settings migration (v1 to v2) for existing files. |
| 8 | The car swung hard one way, hit the parked car, then hard back | Two causes: the plan was made at the offer and driven seconds later from a different pose (now re-planned on accept, `_replan_from_here`), and pure pursuit wound up to a 1.3 m radius demand on a 6 m plan (now capped, `MAX_CORRECTION_FACTOR`). |

Also added, and worth keeping: `PACT_LOG_LEVEL=DEBUG` (`misc/logging_setup.py`),
a one-shot failed-scene dump that replays a live plan failure offline
(`assistance/parking/scene_dump.py`), per-second manoeuvre telemetry and scan
tracing in `assistance/park_assist.py`, and a cursor diagnostic in
`Controls/manoeuvre_outputs.py`. All are DEBUG-only and cost nothing at the
default level. Every defect above was found with one of them; do not remove
them.

## How to run a live test

The driver stays in LFS; the agent starts and stops the add-on.

```bash
PACT_LOG_LEVEL=DEBUG python main.py
```

Then: drive past a space at walking pace, stop beside it, click the offer
twice (the first click installs the key hooks and is refused by design), and
do not touch the mouse — the manoeuvre is steering with it. Read
`pact_assistant.log` afterwards; `grep -E "Parking:|Steering:|manoeuvre"`.

## Open, in priority order

### 1. It is grossly over-controlled — the biggest remaining problem

From the last run: `thr 1.00` on almost every line, then `brk 0.50`, and speed
spiking to 2.3 m/s against a 1.1 m/s demand at every direction change. The
driver's description is the right diagnosis and the right fix: **tap the
throttle once, then coast and steer.**

`VehicleController._pedal_commands` is bang-bang with a ±15 % deadband, which
assumes the pedal has authority proportional to how long it is held. It does
not: LFS has auto-clutch and the car *creeps in gear with no throttle at all*,
which at these speeds is most of the demand already. Full throttle for a whole
100 ms cycle then overshoots, the answer is a full brake, and that is the
oscillation.

Suggested direction: treat the throttle as an impulse rather than a level — a
short tap through `misc/key_tap.py` when the speed is below the demand *and
falling*, nothing otherwise, and let creep carry the car. Measure the creep
speed per gear first; it is probably around 1 m/s in reverse, i.e. the
manoeuvre may need almost no throttle at all.

### 2. The car ends up crooked

Same telemetry: the demanded curvature sits **pinned at the cap**
(`-0.233 1/m` = 1.4/6) for most of the manoeuvre. A correction that is
saturated for ten seconds is not correcting — it means the car left the
planned path early and the follower spent the rest of the manoeuvre chasing
it. Fixing (1) will remove most of the cause, because the overshoot at each
direction change is what puts the car off the path. Re-measure before tuning
anything else.

The offline closed-loop simulation shows a milder version of the same thing:
the tightest space the detector offers ends 10.1 degrees crooked
(`tests/test_parking_follower.py`, the `squareness_deg` parameter), where a
roomier one ends within 4. That test is the fast feedback loop for this — use
it before going back in game.

### 3. The offer comes too late

Reported at about 11 m past the space, and the trace agrees (`offer moves ...
at -3.7m`, with the manoeuvre starting 6 s later). A space has to be seen
ahead (`PASSED_MARGIN_M`), then survive `OFFER_SETTLE_S`, then be planned —
and for an open-ended space the geometry only settles once enough of the row
is in range. Measure where the time actually goes before changing any of the
three.

### 4. Smaller things

* After an abort the car keeps rolling in reverse; nothing holds it. Decide
  what handback means here (`reference/control-intervention.md`) — probably
  brake to a stop and then release, rather than dropping every input at speed.
* `LFS' own automatic gearbox is active (PIF_AUTOGEARS)` on the test car. The
  manoeuvre shifts with the driver's own keys and that works, but it has never
  been tried with the automatic off.
* `_plan_shuffle` is a greedy search and is phase-brittle. It now sweeps the
  same radii the closed form does, which closes the isolated holes a clean
  two-car sweep found, but one documented corner remains: the **largest** car
  (5.2 m) with the driver **closest** to the row (2.6 m centre-to-centre)
  needs up to 4.3 m of slack. `tests/test_parking_agreement.py` skips that
  combination deliberately and says why.

## Not built, deliberately

* **Wheel/joystick steering.** Would need a second vJoy axis and
  `/axis <n> steer`, with everything `control-intervention.md` §3.2 says about
  it. `MouseSteeringOutput` is the only steering output that exists — and it
  now works.
* **Keyboard steering** (`/key <k> steer_left|steer_right`) — possible in
  `mouse_kb`, not written.
* **Learning the turning radius per car.** `park_assist_turn_radius` is a
  setting with a conservative 6 m default; `CurvatureModel` already measures
  the closely related steering gain and learns it within about a second
  (0.05 to 0.15 over the last run).
