"""Self-parking: find a space, offer it, and drive into it when asked.

The assistance system that ties the parking package to the game. Everything
below it is pure geometry and control; everything above it is the screen. What
lives *here* is the part that is neither: the decisions.

### It never parks the car on its own

A space being found is not a reason to do anything. The manoeuvre starts when
the driver clicks, and nothing else -- no timeout, no "the driver seemed to
want to", no automatic anything. That is not caution for its own sake: an
intervention that takes the car over without being asked is exactly the hazard
``control-intervention.md`` is written about, and parking is the one assistance
feature where the assistant holds *all three* inputs at once.

The single exception is :data:`SETTING_AUTO_ACCEPT`, which exists for the
recorded scenarios in ``simulation_tests/`` -- a replay cannot click a button.
It is off by default, it is not in the menu, and it says so in the log every
time it fires.

### Coupled to the PDC, as the driver sees it

The feature only looks while the car is going slowly enough for the park
distance control to be live, so "a space was found" always arrives in the same
state the driver already associates with parking. It is not *implemented* on
top of the PDC -- that system's sensor cones answer a different question, "is
something within 2.8 m of the bumper", and a slot is measured from the obstacle
geometry directly. Sharing the speed gate is the honest amount of coupling.

### Giving up is a first-class outcome

Six things end a manoeuvre before it finishes, and all six end it the same way:
release every input, tell the driver, go back to scanning. They are the driver
braking, the driver cancelling, the car leaving the planned path, an actuator
reporting a fault, the input guard refusing (a menu opened, LFS lost focus,
the camera moved to another car), and a manoeuvre simply taking too long. None
of them is a special case in the code, because the handling is the same and
the differences only matter to the line the driver reads.
"""

import logging
import math
import time
from typing import Any, Dict, List, Optional

import pyinsim

from assistance.base_system import AssistanceSystem
from assistance.park_distance_control import (axm_object_id,
                                              conservative_vehicle_size,
                                              create_rectangle_for_object)
from assistance.parking.geometry import (MCI_TO_M, OrientedBox, Pose,
                                         VehicleShape, box_from_corners,
                                         heading_to_rad, pose_from_mci)
from assistance.parking.path_follower import PathFollower
from assistance.parking.slot_detection import (Obstacle, ParkingSlot,
                                               ParkingSlotDetector,
                                               SCAN_MAX_SPEED_KMH)
from assistance.parking.trajectory import (DEFAULT_MIN_TURN_RADIUS_M,
                                           ParkingPlanner)
from Controls.manoeuvre_outputs import (GearSelector, KeyPedalOutput,
                                        MouseSteeringOutput)
from Controls.vehicle_control import VehicleController, VehicleState
from core.event_bus import EventBus
from core.settings_manager import SettingsManager
from misc.input_guard import InputGuard
from misc.physical_keys import PhysicalKeyState
from misc.spacial_hash_grid import SpatialHashGrid
from vehicles.own_vehicle import OwnVehicle
from vehicles.vehicle import Vehicle

logger = logging.getLogger(__name__)

# ─── Settings ─────────────────────────────────────────────────────────────

SETTING_ENABLED = 'park_assist'
SETTING_AUTO_ACCEPT = 'park_assist_auto_accept'
SETTING_TURN_RADIUS = 'park_assist_turn_radius'

# ─── States ───────────────────────────────────────────────────────────────

STATE_OFF = 'off'                  # switched off, or nothing to do here
STATE_SCANNING = 'scanning'        # crawling along, looking
STATE_OFFERED = 'offered'          # a space is on screen, waiting for a click
STATE_PARKING = 'parking'          # driving the manoeuvre
STATE_DONE = 'done'                # parked
STATE_ABORTED = 'aborted'          # gave up, driver has the car back

# ─── Rates ────────────────────────────────────────────────────────────────

# How often the obstacles are searched for spaces. Four times a second is far
# more often than a space appears at walking pace, and it keeps the cost off
# most cycles: the scan itself is under 0.2 ms but the obstacle list it needs
# is rebuilt with it (``AGENTS.md`` §1).
SCAN_INTERVAL_S = 0.25
# How often a space already on screen is re-planned. Planning is the expensive
# half -- tens of milliseconds for a shuffle -- so it happens when the space
# changes and otherwise at this interval, to notice a space that has since
# been blocked.
PLAN_INTERVAL_S = 2.0
# A space has to survive this long before it is offered. Stops a space
# flickering onto the screen because one MCI frame put a car half a metre
# further on.
OFFER_SETTLE_S = 0.6

# ─── Limits ───────────────────────────────────────────────────────────────

# Obstacles further than this from the car are not in the scan at all.
OBSTACLE_RANGE_M = 30.0
# How many candidate spaces are planned before giving up for this scan. Each
# one costs a closed-form attempt (about 2 ms) and, if that does not fit, a
# shuffle (tens of milliseconds), so this is a bound on the worst cycle the
# system can produce.
MAX_PLAN_CANDIDATES = 3
# A manoeuvre that has not finished by now is not going to.
MAX_MANOEUVRE_S = 120.0
# The driver pressing the brake pedal this hard ends the manoeuvre. It is their
# car; a deliberate brake application is the clearest "stop" there is, and the
# controller never commands more than 0.5 itself.
DRIVER_BRAKE_OVERRIDE = 0.65
# And the car going this fast means something is wrong -- the manoeuvre is a
# crawl and never asks for more than 2.2 m/s.
RUNAWAY_SPEED_MPS = 4.0

# Why a manoeuvre ended, in the words the screen uses.
ABORT_REASONS = {
    'driver_brake': "Parking cancelled - you braked",
    'driver_cancel': "Parking cancelled",
    'off_track': "Parking stopped - car left the path",
    'runaway': "Parking stopped - too fast",
    'timeout': "Parking stopped - took too long",
}

# Button IDs. 64-66 sit above the siren pair (62-63) and below the debug slots
# (100-101), which is the only free run in ``ui/ui_manager.py``'s map.
BTN_PARK_OFFER = 64
BTN_PARK_CANCEL = 65
BTN_PARK_STATUS = 66
PARK_BUTTON_RANGE = (64, 66)


class ParkAssist(AssistanceSystem):
    """Finds parking spaces, offers them, and drives the accepted one."""

    def __init__(self, event_bus: EventBus, settings: SettingsManager,
                 physical_keys: Optional[PhysicalKeyState] = None,
                 guard: Optional[InputGuard] = None,
                 controller: Optional[VehicleController] = None,
                 clock=time.monotonic):
        super().__init__(SETTING_ENABLED, event_bus, settings)
        self.clock = clock
        self.physical_keys = physical_keys or PhysicalKeyState()
        self.guard = guard or InputGuard(event_bus)

        # Layout obstacles. Its own grid rather than a reference to the PDC's:
        # a subsystem may not hold another one (``AGENTS.md`` §3), and the cost
        # is one insert per object per layout load, not per cycle.
        self.layout = SpatialHashGrid(cell_size=15.0 * 65536)
        self.event_bus.subscribe('layout_received', self._on_layout)
        self.event_bus.subscribe('button_clicked', self._on_button)
        self.event_bus.subscribe('park_assist_accept', self._on_accept)
        self.event_bus.subscribe('park_assist_cancel', self._on_cancel)
        self.event_bus.subscribe('state_data', self._on_state_data)

        self.state = STATE_OFF
        self.slot: Optional[ParkingSlot] = None
        self.trajectory = None
        self.follower: Optional[PathFollower] = None
        self.reason: Optional[str] = None

        self._controller = controller
        self._shape: Optional[VehicleShape] = None
        self._detector: Optional[ParkingSlotDetector] = None
        self._planner: Optional[ParkingPlanner] = None
        self._shape_cname = None

        # ``None`` rather than 0.0: the first cycle has to scan, and a clock
        # that starts at zero makes "0.0 seconds ago" look like "just now".
        self._last_scan = None
        self._last_plan = 0.0
        self._slot_seen_since = None
        self._planned_slot_id = None
        self._started_at = 0.0
        self._accept_requested = False
        self._cancel_requested = False
        self._on_track = False
        self._published: Optional[tuple] = None

    # ─── Events ───────────────────────────────────────────────────────

    def _on_layout(self, axm):
        """Keep the static obstacle grid in step with the layout.

        Runs on the packet thread over raw data, so no field is assumed -- a
        packet without ``PMOAction`` or ``Info`` falls through without effect
        rather than killing the handler.
        """
        action = getattr(axm, 'PMOAction', None)
        if action in (pyinsim.PMO_ADD_OBJECTS, pyinsim.PMO_TINY_AXM):
            for info in getattr(axm, 'Info', ()) or ():
                rectangle = create_rectangle_for_object(
                    getattr(info, 'X', 0), getattr(info, 'Y', 0),
                    getattr(info, 'Index', -1), getattr(info, 'Heading', 0))
                if rectangle and rectangle[0] != -1:
                    self.layout.insert_object(axm_object_id(info), rectangle,
                                              is_static=True)
        elif action == pyinsim.PMO_DEL_OBJECTS:
            for info in getattr(axm, 'Info', ()) or ():
                self.layout.remove_object(axm_object_id(info))
        elif action == pyinsim.PMO_CLEAR_ALL:
            self.layout.clear()

    def _on_button(self, data):
        try:
            button_id = int(getattr(data, 'ClickID'))
        except (AttributeError, TypeError, ValueError):
            return
        if button_id == BTN_PARK_OFFER:
            self._accept_requested = True
        elif button_id == BTN_PARK_CANCEL:
            self._cancel_requested = True

    def _on_accept(self, data=None):
        self._accept_requested = True

    def _on_cancel(self, data=None):
        self._cancel_requested = True

    def _on_state_data(self, data):
        on_track = bool(data.get('on_track', False)) if isinstance(data, dict) else False
        if self._on_track and not on_track and self.state == STATE_PARKING:
            self._finish(STATE_ABORTED, 'off_track')
        self._on_track = on_track

    # ─── Per-car objects ──────────────────────────────────────────────

    def _ensure_shape(self, cname) -> VehicleShape:
        """Build the detector and planner for this car, once per car.

        The dimensions are the conservative ones: an unknown car -- which is
        every vehicle mod -- is treated as the largest standard one, because
        every error here points at "parked it somewhere it does not fit"
        (``conventions.md`` §4).
        """
        if self._shape is not None and self._shape_cname == cname:
            return self._shape
        length, width = conservative_vehicle_size(cname)
        shape = VehicleShape(length, width,
                             wheelbase=length * 0.58,
                             rear_axle_offset=length * 0.28)
        radius = self.settings.get(SETTING_TURN_RADIUS) or DEFAULT_MIN_TURN_RADIUS_M
        self._shape = shape
        self._shape_cname = cname
        self._detector = ParkingSlotDetector(shape)
        self._planner = ParkingPlanner(shape, min_turn_radius=float(radius))
        return shape

    @property
    def controller(self) -> VehicleController:
        if self._controller is None:
            self._controller = VehicleController(
                MouseSteeringOutput(self.settings),
                KeyPedalOutput(self.event_bus, self.settings, self.physical_keys),
                GearSelector(self.settings))
        return self._controller

    # ─── The cycle ────────────────────────────────────────────────────

    def process(self, own_vehicle: OwnVehicle,
                vehicles: Dict[int, Vehicle]) -> Dict[str, Any]:
        """One assistance pass.

        Cost, by state: **off** one comparison; **scanning** an obstacle
        rebuild and a scan four times a second, a plan at most every two
        seconds; **parking** one follower update and one controller pass, both
        tens of microseconds. The expensive thing -- planning a shuffle -- can
        never land on a cycle that is also driving the car, because a manoeuvre
        is planned before it is offered.
        """
        own = own_vehicle.data
        if not self._may_run(own_vehicle):
            if self.state == STATE_PARKING:
                self._finish(STATE_ABORTED, self._refusal(own_vehicle))
            else:
                self._set_state(STATE_OFF)
            return self._publish()

        if self.state == STATE_PARKING:
            self._drive(own_vehicle, own)
            return self._publish()

        if self._cancel_requested:
            self._cancel_requested = False
            self._accept_requested = False

        if self.state in (STATE_DONE, STATE_ABORTED) and own.speed > 1.0:
            # The driver has moved off; go back to looking.
            self._set_state(STATE_SCANNING)

        self._scan(own_vehicle, own, vehicles)

        if self._accept_requested:
            self._accept_requested = False
            self._begin(own_vehicle)
        return self._publish()

    # ─── Gates ────────────────────────────────────────────────────────

    def _may_run(self, own_vehicle: OwnVehicle) -> bool:
        """Is this feature allowed to do anything at all right now?"""
        if not self.settings.get(SETTING_ENABLED):
            return False
        if not self._on_track:
            return False
        # Nothing here may act on a car the camera is merely watching
        # (``conventions.md`` §5.2).
        return own_vehicle.is_local_driver

    def _refusal(self, own_vehicle: OwnVehicle) -> str:
        return self.guard.may_inject(own_vehicle) or 'unavailable'

    # ─── Scanning ─────────────────────────────────────────────────────

    def _scan(self, own_vehicle: OwnVehicle, own, vehicles):
        """Look for a space, and plan the best one. Rate-limited; see above."""
        if own.speed > SCAN_MAX_SPEED_KMH:
            self._forget_slot()
            self._set_state(STATE_SCANNING)
            return
        now = self.clock()
        if self._last_scan is not None and now - self._last_scan < SCAN_INTERVAL_S:
            return
        self._last_scan = now

        shape = self._ensure_shape(own.cname)
        ego = pose_from_mci(own.x, own.y, own.heading)
        obstacles = self._obstacles(ego, own, vehicles)
        slots = self._detector.scan(ego, obstacles)
        if not slots:
            self._forget_slot()
            self._set_state(STATE_SCANNING)
            return

        ranked = self._rank(slots)
        best = ranked[0]
        if self.slot is None or best.slot_id != self.slot.slot_id:
            self.slot = best
            self._slot_seen_since = now
            self._planned_slot_id = None
            self._set_state(STATE_SCANNING)
            return
        self.slot = best
        # ``is None``, not ``or now``: the clock legitimately reads 0.0 at the
        # start of a session, and a falsy zero here made "seen 0.0 s ago" mean
        # "seen just now" on every single cycle -- the space settled forever
        # and was never offered.
        if (self._slot_seen_since is not None
                and now - self._slot_seen_since < OFFER_SETTLE_S):
            return

        if (self._planned_slot_id != best.slot_id
                or now - self._last_plan > PLAN_INTERVAL_S):
            self._last_plan = now
            self._planned_slot_id = best.slot_id
            self._plan_best_of(ego, ranked, obstacles)
        if self.trajectory is not None:
            self._set_state(STATE_OFFERED)
            if self.settings.get(SETTING_AUTO_ACCEPT):
                logger.info("park_assist_auto_accept is on - starting the "
                            "manoeuvre without a driver click. This setting "
                            "exists for recorded scenarios only.")
                self._accept_requested = True
        else:
            self._set_state(STATE_SCANNING)

    def _rank(self, slots: List[ParkingSlot]) -> List[ParkingSlot]:
        """Best candidate first, with the one already on screen kept on top.

        Two orderings, and the first matters more than it looks. A space
        **bounded at both ends** is a parking space; a space that merely runs
        off past the last parked car is a guess about where the row ends, and
        it is a guess that reliably comes out *nearer* than the real space
        between two cars, because its open end is measured from the car the
        driver is beside. Ranked by distance alone the assistant therefore
        offered the strip of road beyond the last car in preference to the gap
        it was standing next to. Closed spaces first, then the nearest.

        Keeping the space already on screen on top is the other half: without
        it the offer would swap between two equally good spaces as the car
        rolls between them, and a driver cannot click something that keeps
        changing under the cursor.
        """
        ranked = sorted(slots, key=lambda slot: (slot.open_ended,
                                                 abs(slot.ahead_of_ego)))
        if self.slot is not None:
            for index, slot in enumerate(ranked):
                if slot.slot_id == self.slot.slot_id:
                    ranked.insert(0, ranked.pop(index))
                    break
        return ranked

    def _plan_best_of(self, ego: Pose, ranked: List[ParkingSlot],
                      obstacles: List[Obstacle]):
        """Plan candidates in order and keep the first that is drivable.

        A space can be measurable and still not be reachable -- the driver may
        be square beside it, or something may be in the way of the manoeuvre
        rather than of the space. Rather than offering it and failing on the
        click, the next candidate is tried. Bounded at
        :data:`MAX_PLAN_CANDIDATES`, because planning a shuffle is the
        expensive thing this system does (``AGENTS.md`` §1) and this runs on
        the assistance thread.
        """
        boxes = [obstacle.box for obstacle in obstacles]
        self.trajectory = None
        for slot in ranked[:MAX_PLAN_CANDIDATES]:
            result = self._planner.plan(ego, slot, boxes)
            self.reason = result.reason
            if result.ok:
                self.slot = slot
                self.trajectory = result.trajectory
                self._planned_slot_id = slot.slot_id
                return

    def _obstacles(self, ego: Pose, own, vehicles) -> List[Obstacle]:
        """Everything standing near the car, from both sources LFS offers.

        Live cars come from MCI with their own speed, so a car that is still
        rolling can be told from one that is parked. Layout objects come from
        the AXM grid as the corner lists it stores, which
        :func:`box_from_corners` reads back -- that keeps the object size table
        in one place (``assistance/park_distance_control.py``).
        """
        obstacles: List[Obstacle] = []
        for vehicle in list(vehicles.values()):
            data = vehicle.data
            if data.player_id == own.player_id:
                continue
            if data.distance_to_player > OBSTACLE_RANGE_M:
                continue
            length, width = conservative_vehicle_size(data.cname)
            obstacles.append(Obstacle(
                OrientedBox(data.x * MCI_TO_M, data.y * MCI_TO_M,
                            heading_to_rad(data.heading), length, width),
                data.player_id, data.speed))

        for entry in self.layout.query_area(own.x, own.y,
                                            OBSTACLE_RANGE_M * 65536):
            points = entry.get('points')
            box = box_from_corners([(x * MCI_TO_M, y * MCI_TO_M)
                                    for x, y in points] if points else None)
            if box is None or box.length < 0.05 or box.width < 0.05:
                continue
            obstacles.append(Obstacle(box, entry.get('id')))
        return obstacles

    def _forget_slot(self):
        self.slot = None
        self.trajectory = None
        self.follower = None
        self._slot_seen_since = None
        self._planned_slot_id = None

    # ─── Driving ──────────────────────────────────────────────────────

    def _begin(self, own_vehicle: OwnVehicle):
        """Take the car over. Refuses out loud rather than half-starting."""
        if self.state != STATE_OFFERED or self.trajectory is None:
            return
        reason = self.controller.unavailable_reason()
        if reason is not None:
            logger.warning("Parking manoeuvre refused: %s.", reason)
            self._finish(STATE_ABORTED, reason)
            return
        refusal = self.guard.may_inject(own_vehicle)
        if refusal is not None:
            logger.warning("Parking manoeuvre refused by the input guard: %s.",
                           refusal)
            self._finish(STATE_ABORTED, refusal)
            return
        pedals = getattr(self.controller, 'pedals', None)
        if hasattr(pedals, 'push_bindings'):
            pedals.push_bindings()
        self.follower = PathFollower(self.trajectory)
        self.controller.reset()
        self._started_at = self.clock()
        self._set_state(STATE_PARKING)
        logger.info("Parking manoeuvre started: %s on the %s, %.1f m of path, "
                    "%d stroke(s), planned at a %.1f m radius.",
                    self.slot.kind, self.slot.side, self.trajectory.length,
                    len(self.trajectory.segments), self.trajectory.radius)

    def _drive(self, own_vehicle: OwnVehicle, own):
        """One control cycle of an active manoeuvre."""
        if self._cancel_requested:
            self._cancel_requested = False
            self._finish(STATE_ABORTED, 'driver_cancel')
            return
        refusal = self.guard.may_inject(own_vehicle)
        if refusal is not None:
            self._finish(STATE_ABORTED, refusal)
            return
        if own_vehicle.brake >= DRIVER_BRAKE_OVERRIDE:
            self._finish(STATE_ABORTED, 'driver_brake')
            return
        now = self.clock()
        if now - self._started_at > MAX_MANOEUVRE_S:
            self._finish(STATE_ABORTED, 'timeout')
            return

        speed_mps = own.speed / 3.6
        if speed_mps > RUNAWAY_SPEED_MPS:
            self._finish(STATE_ABORTED, 'runaway')
            return

        pose = pose_from_mci(own.x, own.y, own.heading)
        demand = self.follower.update(pose, speed_mps)
        if demand.off_track:
            self._finish(STATE_ABORTED, 'off_track')
            return
        if demand.finished:
            self._finish(STATE_DONE, None)
            return

        state = VehicleState(speed_mps=speed_mps, yaw_rate=own.yaw_rate,
                             gear=own_vehicle.gear,
                             reversing=demand.direction < 0)
        status = self.controller.apply(demand, state)
        self._last_demand = demand
        if status.fault is not None:
            self._finish(STATE_ABORTED, status.fault)

    def _finish(self, state: str, reason: Optional[str]):
        """End a manoeuvre, whichever way it ended. Always releases first."""
        was_parking = self.state == STATE_PARKING
        if was_parking or self._controller is not None:
            self.controller.release()
        self.follower = None
        self._accept_requested = False
        self._cancel_requested = False
        self.reason = reason
        self._set_state(state)
        if not was_parking:
            return
        if state == STATE_DONE:
            logger.info("Parking manoeuvre finished.")
        else:
            logger.info("Parking manoeuvre ended: %s.", reason)
        # Emit an audible marker for the recorded scenarios and the log, and a
        # line for the driver. The screen already shows the state; this is the
        # one-off event.
        self.event_bus.emit('notification', {'notification':
                            "^2Parked." if state == STATE_DONE
                            else "^1" + ABORT_REASONS.get(reason,
                                                          "Parking stopped")})

    # ─── Publishing ───────────────────────────────────────────────────

    def _set_state(self, state: str):
        if state != self.state:
            self.state = state
            if state in (STATE_SCANNING, STATE_OFF):
                self.reason = None
            # The manoeuvre holds all three inputs, so everything else that
            # presses a key has to stand down for the duration. Auto-hold would
            # put the handbrake on at every stroke's stop; the automatic
            # gearbox would fight the gear selection.
            self.event_bus.emit('manoeuvre_active',
                                {'active': state == STATE_PARKING,
                                 'source': 'park_assist'})

    def _publish(self) -> Dict[str, Any]:
        """Tell the screen what to draw, on change only."""
        demand = getattr(self, '_last_demand', None)
        payload = {
            'state': self.state,
            'kind': self.slot.kind if self.slot else None,
            'side': self.slot.side if self.slot else None,
            'length': round(self.slot.length, 1) if self.slot else 0.0,
            'distance': round(abs(self.slot.ahead_of_ego), 1) if self.slot else 0.0,
            'strokes': len(self.trajectory.segments) if self.trajectory else 0,
            'stroke': demand.stroke if demand and self.state == STATE_PARKING else 0,
            'progress': (round(demand.progress, 2)
                         if demand and self.state == STATE_PARKING else 0.0),
            'reason': self.reason,
        }
        signature = tuple(sorted(payload.items(), key=lambda item: item[0]))
        if signature != self._published:
            self._published = signature
            self.event_bus.emit('park_assist_changed', payload)
        return payload

    # ─── Lifecycle ────────────────────────────────────────────────────

    def is_enabled(self) -> bool:
        # Keep running while a manoeuvre is live even if the switch is turned
        # off underneath it: something has to give the car back.
        return bool(self.settings.get(SETTING_ENABLED)) or self.state == STATE_PARKING

    def shutdown(self):
        """Give every input back before the process ends."""
        if self._controller is not None:
            self.controller.release()
