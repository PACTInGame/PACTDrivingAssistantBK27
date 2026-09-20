# Self-parking — where it stands, and what to do next

Written at the end of the session that built it, so the next one does not have
to re-derive any of it. Delete this file once the two open defects below are
fixed and the feature has had a clean in-game run.

## What exists

| Layer | File | State |
|---|---|---|
| Geometry, boxes, SAT overlap | `assistance/parking/geometry.py` | done, tested |
| Slot detection (parallel + perpendicular) | `assistance/parking/slot_detection.py` | done, tested |
| Manoeuvre planning | `assistance/parking/trajectory.py` | done, tested |
| Path following → `ControlDemand` | `assistance/parking/path_follower.py` | done, closed-loop tested |
| Demand → steering/pedals/gear | `Controls/vehicle_control.py` | done, tested |
| Mouse steering, key pedals, shift gears | `Controls/manoeuvre_outputs.py` | done, **never yet driven a car** |
| State machine, offer, consent, aborts | `assistance/park_assist.py` | done, tested |
| Screen (offer/cancel/status buttons) | `ui/ui_manager.py` 64–66 | done |
| Menu switch | `ui/menu_system.py`, Parking menu | done |

Settings: `park_assist`, `park_assist_auto_accept` (scenarios only),
`park_assist_turn_radius`, `park_assist_mouse_span`.

## Verified in game (2026-09-20)

* Spaces are detected live on AU4X with the `parking` layout.
* A space is offered only after the car has driven past it.
* The offer and cancel buttons draw and the click reaches `ParkAssist`.
* The physical key hooks install on the first click and the manoeuvre arms.

## Two open defects, both reported from a live run

### 1. "Parking cancelled - you braked" immediately, without the driver touching anything

`ParkAssist._drive` aborts when `own_vehicle.brake >= DRIVER_BRAKE_OVERRIDE`.
`OutGaugePack.Brake` is the **merged** brake — it includes what *we* are
commanding, and the first thing a manoeuvre does is brake, because the gear is
not in yet (`VehicleController._pedal_commands`). So the manoeuvre aborts on
its own brake application.

The fix has to compare against what we asked for, not against the total:
carry the last commanded brake (it is already in `ControlStatus.brake`) and
treat only the *excess* over it as the driver. A margin is needed because LFS
merges the two and the reading is not a sum.

### 2. Only the first space is ever offered

`ParkAssist._rank` moves the currently offered space to the top of the ranking
whenever it is still visible, to stop the offer flickering between two equally
good spaces. It has no upper bound, so once the open-ended space behind the
first parked car is offered it stays offered for as long as it is in range,
and the better closed space between two cars that appears afterwards can never
win.

The stickiness is still wanted; it needs to be beaten by a clearly better
candidate — a closed space where the held one is open-ended, or one
substantially nearer — rather than being absolute.

## What to test next, in this order

1. Fix both defects, then re-run the manual test: drive past, click, watch.
2. `simulation_tests/run_scenario.py 29_parking_test` with
   `park_assist_auto_accept` on (parallel), then `31_perpendicular_parking_test`.
   Note that the tracer gets no OutGauge while the add-on holds port 30000 —
   read `pact_assistant.log` for the manoeuvre and the trace for the motion.
3. The actuation has never moved a car. Expect the curvature model
   (`Controls/vehicle_control.CurvatureModel`) to need a real measurement of
   its starting gain, and expect the mouse span setting to matter.

## Not built, deliberately

* **Wheel/joystick drivers.** Steering would need a second vJoy axis and
  `/axis <n> steer`, with everything `control-intervention.md` §3.2 says about
  that. `MouseSteeringOutput` is the only steering output that exists.
* **Keyboard steering** (`/key <k> steer_left|steer_right`) — possible in
  `mouse_kb`, not written.
* **Learning the turning radius per car.** `park_assist_turn_radius` is a
  setting with a conservative default; `conventions.md` §4 argues for measuring
  it instead, and `CurvatureModel` already measures the closely related
  steering gain.
