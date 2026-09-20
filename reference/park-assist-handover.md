# Self-parking — where it stands, and what to do next

Written at the end of the **third** live-test session. Delete this file once
the open items below are done and the feature has had a clean in-game run.

Read this before touching `assistance/park_assist.py`,
`assistance/parking/*` or `Controls/{vehicle_control,manoeuvre_outputs,
pulse_modulator}.py`.

---

## Where it got to

The driver's verdict after this session: *"massive Verbesserungen … das
Fahrzeug parkte zum ersten Mal einigermaßen vernünftig."*

In game, in one session: a five-stroke **parallel** manoeuvre completed, and
— by accident, it was never aimed for — a three-stroke **perpendicular** one
completed too. Both ended in the space. The jerking is gone. What is left is
placement accuracy and a set of specific, individually understood defects.

The two numbers that say the control loops now work, straight from the good
run's telemetry (`16:43:47`–`16:44:03`):

```
kappa -0.167/-0.167 1/m      demanded / measured — they are the same number
v 1.10/1.10 m/s              speed / demand
thr 0.00–0.23  brk 0.00–0.25 the pedals, as fractions
```

Against the previous session, where the same lines read `kappa -0.233/-0.048`
(demand pinned at its cap, measurement a third of it), `v 2.33/1.10` and
`thr 1.00` on nearly every line.

---

## What changed in this session

### 1. The steering demanded half again as much curvature as the plan

**The single biggest defect, and it was there from the first line of code.**
The follower fed the path's own arc forward *and* added a pure-pursuit term
on top. Pure pursuit is not an error term: aimed at a point on the path it
asks for the arc that reaches that point, which on a curved path is the
path's own arc. A car sitting **exactly** on a 6 m arc therefore demanded
0.233 1/m where the plan said 0.167.

Only `MAX_CORRECTION_FACTOR` kept it finite, which is why the last session saw
the demand pinned at that cap for ten seconds and read it as "the follower is
chasing the car". It was not chasing anything; it was asking for that by
construction.

Replaced by feedforward plus an error feedback that is zero on the path:

```
kappa = kappa_path - CROSS_TRACK_GAIN * e - HEADING_GAIN * direction * theta
```

The heading term carries the sign of travel and the cross-track term does
not — `path_follower.py`'s docstring derives why from the closed loop's trace
and determinant. Getting that one sign wrong gives a controller that
converges forwards and diverges reversing.

Offline, on the same scenes: worst squareness 10.1° → **2.1°**, worst
cross-track 0.45 m → **0.02 m**. Pinned by
`tests/test_parking_follower.py::TestSteeringLaw::
test_a_car_on_a_curved_path_asks_for_exactly_that_curve`.

### 2. The plan was tighter than the car could steer

`park_assist_turn_radius` (6.0 m) was handed straight to the planner, whose
tightest `RADIUS_FACTORS` entry is 1.0 — so the plan's arcs *were* the car's
steering lock, and there was no authority left to correct with. The planner
now plans at `planning_radius_for(lock)` = lock × `PLAN_RADIUS_MARGIN` (1.15).

That costs room: `PARALLEL_LENGTH_MARGIN_M` went 2.5 → **2.75 m**, re-measured
over the same grid (`tests/test_parking_agreement.py`). The grid's closest
lateral offset (2.6 m centre-to-centre) is now out of reach at any length and
`LATERAL_OFFSETS` starts at 3.0; the planner refuses it and `ParkAssist`
offers the next space, which is the designed behaviour, not a hole.

### 3. Full throttle, full brake, all the way in

`_pedal_commands` was bang-bang with a ±15 % band, which assumes the pedal's
authority is proportional to how long it is held. It is not: LFS has
auto-clutch and **the car creeps in gear with no throttle at all**, which at
the 1.1 m/s a manoeuvre runs at is most of the demand already.

Two changes, and they belong together:

* **`Controls/pulse_modulator.py`** (new) — a fractional demand on a key is a
  **duty cycle**. 30 % is a 30 ms press in a 100 ms cycle, held by
  `misc/key_tap.py` on its own thread. Small demands are carried in a budget
  and spent as pulses of at least `MIN_PULSE_S` (40 ms), because a shorter
  press is one LFS may never sample — a sigma-delta modulator, so the
  *average* is right with no steady-state error. It knows nothing about
  parking or pedals and is reusable for any digital actuator.
* **A PI speed loop** in `VehicleController._pedal_commands`, with
  anti-windup. The integral is what makes the creep a non-problem: whatever
  the car does by itself becomes the operating point the loop trims around,
  per gear, without anybody measuring it.

Also: the follower's speed demand is now acceleration-limited
(`ACCELERATION_MPS2`), so a stroke no longer begins with a step from
standstill to the crawl. A step is a demand no controller can meet, and the
answer to it was the overshoot.

`misc/key_tap.py`'s shared tapper is now wired to the shared
`PhysicalKeyState`. That became necessary the moment the brake was *pulsed*
rather than held: a pulse ending while the driver is on the brake would take
their brake away, ten times a second (`control-intervention.md` §3.1).

### 4. An abort left the car rolling

Every abort dropped all three inputs, at speed, in gear. There is now a
`STATE_STOPPING` phase: steering and gears go back immediately, the brake is
held until the car is at rest. Bounded three ways — standstill,
`STOPPING_TIMEOUT_S` (4 s), and the driver touching the throttle — and never
entered when the input guard has refused, because then the keystroke is not
ours to send. The screen says `Stopping the car...` while it runs.

### 5. Smaller

* The telemetry line printed `stroke 6/5`; `ControlDemand.stroke` is already
  1-based and was being incremented again.
* Steering integral trim gained anti-windup. It saturates on every tight
  stroke, and an error integrated against a wheel already on the stop is a
  correction applied later, pointing the wrong way.

### 6. Reverted: learning the turn radius

Added during this session and **taken out again the same session**, because
the live run showed it is a positive feedback loop. `1 / CurvatureModel.gain`
looks like the car's lock radius; adopting it makes the next plan flatter; a
flatter plan needs less steering; the gain is then only measured at part lock,
where it reads lower; so the radius grows again.

Measured: the first two manoeuvres planned at 6.9 m and **both finished**, the
fit then adopted 11.0 m, and every manoeuvre after that planned at 12.7 m,
never commanded more than 0.76 of lock, needed 25–38 m of path, and ended
`off_track`. The reasoning is kept as a comment in `park_assist.py` so nobody
re-adds it; what is missing is a **calibration sweep** at full lock, like
`Controls/throttle_axis_check.py` does for the pedal, rather than an estimate
taken from the manoeuvre it then changes.

---

## Tests

1572 pass. The ones that matter here:

| File | What it holds down |
|---|---|
| `tests/test_parking_endtoend.py` | **New, and the one to use.** Plan → follow → control → pulse keys → a car that *creeps in gear*, brakes far harder than it accelerates, and samples its keys every 10 ms. The offline replica of the live test. |
| `tests/test_parking_follower.py` | The steering law, including the exactly-on-the-arc case, and closed-loop parking against a car whose lock is genuinely tighter than its plan. |
| `tests/test_pulse_modulator.py` | That the key is down for the fraction asked for, that a 5 % demand still produces pulses the game can see, and that a hold ends when the demand does. |
| `tests/test_parking_agreement.py` | Detector and planner agree, now at the planning radius production actually uses. |
| `tests/test_vehicle_control.py` | The PI loop: proportional, both anti-windups, integral cleared at a stop. |

---

## Open, in priority order

### 1. It parks offset to one side (driver's #1 and #2)

The clearest remaining defect and the one to start on. In the attached
scenario the car ended up correctly aligned — same heading as the car in
front, which is the steering law working — but **too far to the right**,
i.e. further from the kerb-side neighbour than it should be. The driver's
own reading is that it *"lenkte zu lange nach rechts rein beim
Rückwärtsfahren"*.

Note what this is **not**: the telemetry shows demand and measurement
matching to three decimals through that manoeuvre, so the car drove the plan
it was given. Look at the **plan and the slot target**, not the follower:

* `ParkingSlot.target` for a space bounded on only one side. The good run was
  `parallel on the right … open` — an open-ended slot, whose lateral target
  is derived from the one neighbour that exists. Suspect first.
* `_replan_from_here` plans from the pose at the click. If the driver stops
  further out from the row than the plan assumes, the final lateral position
  inherits that.

Reproduce offline in `tests/test_parking_endtoend.py` with a one-sided scene
before changing anything.

### 2. `off_track` is still common (driver's #7)

Every instance in this session's log is from a manoeuvre planned at the
runaway 12.7 m radius, so **re-measure before doing anything** — it may
already be gone with the learning removed. If it is not, the suspects are
`MAX_CROSS_TRACK_M` (0.9 m) being too tight for a long approach, and the
progress index on a path that doubles back.

### 3. Two clicks are needed to start (driver's #3)

Known and deliberate-ish: the first click installs the `pynput` hooks, which
takes over 100 ms, so it is refused (`no_physical_key_tracking`) and the
second one arms. It is documented in the code and it is still bad product
behaviour. Fix by installing the hooks **when a space is first offered**
rather than on the click — the offer is already a strong enough signal, and
there is a second or more of settle time before any click arrives.

### 4. The car does not finish on the brake (driver's #4)

`_finish(STATE_DONE, ...)` releases everything, and the car creeps on. The
`STATE_STOPPING` machinery from §4 above already exists and is tested — it is
simply not wired to the *successful* finish, only to aborts. Wiring it there
is a small change; decide whether a completed park should also hold the brake
until the driver does something, or just stop once.

### 5. Which space is offered is not obvious (driver's #5)

Perpendicular spaces were only triggered twice by accident. The ranking
prefers closed spaces then nearest (`_rank`), which is defensible, but the
driver cannot see *why* a space won and cannot ask for a different one. Worth
considering: show the runner-up, or let the driver cycle candidates.

### 6. Was a 9-stroke shuffle right?

One perpendicular attempt planned **nine** strokes and aborted after five.
`_plan_shuffle` is a greedy search; nine strokes is a sign it barely fit, and
offering a manoeuvre that marginal is probably wrong. Consider a stroke-count
ceiling above which the space is simply not offered.

### 7. The offer still comes late

Carried over from the last session and not measured this time. A space has to
be seen ahead (`PASSED_MARGIN_M`), survive `OFFER_SETTLE_S`, then be planned.
Measure where the time actually goes before changing any of the three.

---

## How to run a live test

The driver stays in LFS; the agent starts and stops the add-on. **Stop any
add-on left running from a previous session first** — it holds
`pact_assistant.log` open and will fight the new one for InSim.

```bash
PACT_LOG_LEVEL=DEBUG python main.py
```

Then: drive past a space at walking pace, stop beside it, click the offer
twice (see open item 3), and do not touch the mouse — the manoeuvre is
steering with it. Afterwards:

```bash
grep -E "manoeuvre (started|finished|ended)|Parking: stroke" pact_assistant.log
```

The telemetry line is the whole diagnosis in one place: `kappa a/b` is
demanded against measured curvature, `v a/b` speed against demand, `steer` the
command in −1..1 and `thr`/`brk` the pedal fractions. If `kappa`'s two numbers
agree, the follower is fine and the problem is in the plan.

---

## Not built, deliberately

* **Wheel/joystick steering.** Needs a second vJoy axis and `/axis <n> steer`,
  with everything `control-intervention.md` §3.2 says about it.
  `MouseSteeringOutput` is the only steering output that exists.
* **Keyboard steering** (`/key <k> steer_left|steer_right`) — possible in
  `mouse_kb`, not written.
* **Learning the turning radius** — tried, reverted, see §6 above.
