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
import threading
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
from assistance.parking.scene_dump import dump_scene
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
from misc.physical_keys import PhysicalKeyState, get_physical_keys
from misc.platform_shim import get_keyboard
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

# ─── Driving past is part of finding it ───────────────────────────────────
#
# A real slot scanner measures a space *while the car goes by it*, and that is
# what a driver expects to have happened before being offered one. The scan
# here is pure current-frame geometry, so without this it offers a space the
# car is merely standing next to -- which the first live test did the instant
# the driver joined the track, several car lengths short of the space.
#
# So a space is only offered once it has been seen ahead of the car and is
# then no longer ahead of it. One flag per space, dropped with the space.
# "No longer ahead" is measured to the car's middle rather than its nose,
# which is where the driver is sitting and where a space stops being something
# to drive towards and starts being something to reverse into.
PASSED_MARGIN_M = 2.0

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
# How often installing the physical key hooks is retried after a failure.
HOOK_RETRY_S = 10.0
# One telemetry line per this long while a manoeuvre runs; see _log_progress.
PROGRESS_LOG_INTERVAL_S = 1.0
# And one DEBUG line per this long while scanning; see _trace.
TRACE_INTERVAL_S = 1.0

# Refusals that clear themselves within a second or two, and must therefore not
# be remembered against the space that happened to be on screen at the time.
# The hooks are the whole reason this set exists: they are installed on the
# first click, so the first click is always refused, and remembering that
# refusal meant the space was never offered again -- the car stood beside it
# doing nothing, which is exactly what a live run showed.
TRANSIENT_REFUSALS = frozenset(('no_physical_key_tracking', 'vjoy_loading',
                                'binding_not_pushed',
                                'throttle_binding_not_pushed'))
# At most one line per distinct refusal per this long.
REFUSAL_LOG_INTERVAL_S = 30.0
# The driver pressing the brake pedal this hard ends the manoeuvre. It is their
# car; a deliberate brake application is the clearest "stop" there is.
#
# ``OutGauge.Brake`` is the **merged** pedal -- it contains what the manoeuvre
# is commanding as well -- and the first thing a manoeuvre does is brake,
# because the gear is not in yet. Read as the driver's, it ended every
# manoeuvre the first three live runs started, within a cycle.
#
# Subtracting what the controller asked for is not enough either, and that is
# worth spelling out because it cost a live run to find. The brake is a *key*
# (``Controls/manoeuvre_outputs.KeyPedalOutput``): a demand of 0.35 and a
# demand of 1.0 are the same keystroke, so LFS reports 1.0 for both. There is
# no fractional headroom to measure a driver against.
#
# So the rule is the only one the signal supports: the reading is the driver's
# **only while the manoeuvre is not braking at all**, and only once the key
# has been released long enough for LFS to have noticed
# (:data:`BRAKE_SETTLE_S`). A manoeuvre brakes at a standstill and into one,
# so this leaves the driver's brake visible for most of every stroke; for the
# rest, the cancel button and the off-track check are what stops it.
DRIVER_BRAKE_OVERRIDE = 0.5
# How long our own brake key has to have been up before the reading is
# believed. Two assistance cycles: the key is released during ``apply()``, so
# the packet that still shows it arrives after the cycle that released it.
BRAKE_SETTLE_S = 0.25
# How long a space that would not plan stays at the back of the ranking.
# Long enough that the ranking does not pick it up again on the next scan --
# which is what made the offer flicker at 2 Hz in a live run -- and short
# enough that a space blocked by a car that then drives away is reconsidered
# within a couple of seconds.
UNPLANNABLE_TTL_S = 5.0
# How much nearer a rival space has to be before it takes the offer away from
# the one already on screen. Half a car length: enough that rolling forward
# past a space does not hand the offer back and forth, small enough that the
# space the driver has actually stopped beside wins.
STICKY_MARGIN_M = 2.0
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
        # Shared with the emergency brake, so the low-level hooks are
        # installed once for the process (``misc/physical_keys.py``).
        self.physical_keys = physical_keys or get_physical_keys()
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
        # A space whose manoeuvre was refused. Without it, an offer that
        # cannot be started is re-offered and re-refused on every scan, which
        # in a live run produced four log lines a second for the whole time
        # the driver was beside the space.
        self._refused_slot_id = None
        self._refusal_logged = {}
        # slot_id -> True once the car has driven past that space.
        self._passed: Dict[Any, bool] = {}
        # slot_id -> when planning last failed for it. See UNPLANNABLE_TTL_S.
        # Bounded the same way ``_passed`` is: pruned with the spaces in range.
        self._unplannable: Dict[Any, float] = {}
        self._hooks_tried_at = None
        self._hooks_pending = False
        # When the manoeuvre's own brake was last released, or ``None`` while
        # it is applied. See DRIVER_BRAKE_OVERRIDE.
        self._brake_free_since = None
        self._last_progress_log = 0.0
        self._last_trace = 0.0

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
        if self._on_track and not on_track:
            if self.state == STATE_PARKING:
                self._finish(STATE_ABORTED, 'off_track')
            # Leaving the track is the one thing that really does invalidate
            # "we drove past this": the car is put back somewhere else.
            self._passed.clear()
            self._refused_slot_id = None
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
            self._trace('too fast: %.1f km/h', own.speed)
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
            self._trace('%d obstacle(s), no space', len(obstacles))
            self._forget_slot()
            self._set_state(STATE_SCANNING)
            return

        self._note_passing(slots)
        passed = [slot for slot in slots
                  if self._passed.get(slot.slot_id)
                  and slot.ahead_of_ego < PASSED_MARGIN_M]
        if not passed:
            self._trace('%d obstacle(s), %d space(s), none driven past yet: %s',
                        len(obstacles), len(slots),
                        ', '.join('%s %s %.1fm %+.1fm ahead%s'
                                  % (slot.kind, slot.side, slot.length,
                                     slot.ahead_of_ego,
                                     ' open' if slot.open_ended else '')
                                  for slot in slots[:4]))
            self._forget_slot()
            self._set_state(STATE_SCANNING)
            return

        ranked = self._rank(passed)
        best = ranked[0]
        if self.slot is None or best.slot_id != self.slot.slot_id:
            # Not rate-limited: the offered space changing is an event, not a
            # per-cycle condition, and when it is *not* an event -- the 2 Hz
            # churn two live runs showed -- that is the thing being diagnosed.
            if logger.isEnabledFor(logging.DEBUG):
                logger.debug("Parking scan: offer moves from %s to %s %s "
                             "%.1fm at %+.1fm%s, %d candidate(s)",
                             self.slot.slot_id if self.slot else None,
                             best.kind, best.side, best.length,
                             best.ahead_of_ego,
                             ' open' if best.open_ended else '', len(passed))
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
        if self.trajectory is None:
            self._trace('space %s %s %.1fm at %+.1fm is not drivable: %s',
                        best.kind, best.side, best.length, best.ahead_of_ego,
                        self.reason)
            # Once per run, at DEBUG only, off-thread: see scene_dump.
            dump_scene(ego, best, [obstacle.box for obstacle in obstacles],
                       self.reason)
        if self.trajectory is not None:
            self._set_state(STATE_OFFERED)
            if (self.settings.get(SETTING_AUTO_ACCEPT)
                    and best.slot_id != self._refused_slot_id):
                logger.info("park_assist_auto_accept is on - starting the "
                            "manoeuvre without a driver click. This setting "
                            "exists for recorded scenarios only.")
                self._accept_requested = True
        else:
            self._set_state(STATE_SCANNING)

    def _trace(self, message: str, *args):
        """Why the scan did not offer anything, at DEBUG, at most once a second.

        The scanning path is otherwise silent, which is right at the default
        level -- nothing has happened -- but it made "no space was offered"
        undiagnosable in a live run: the only two states the log can tell
        apart are "nothing found" and "manoeuvre started". This closes that
        gap without costing anything when DEBUG is off, which is the point of
        the ``isEnabledFor`` guard: the argument tuple is never even built.

        Run the add-on with ``PACT_LOG_LEVEL=DEBUG`` to see it.
        """
        if not logger.isEnabledFor(logging.DEBUG):
            return
        now = self.clock()
        if now - self._last_trace < TRACE_INTERVAL_S:
            return
        self._last_trace = now
        logger.debug("Parking scan: " + message, *args)

    def _note_passing(self, slots: List[ParkingSlot]):
        """Remember which spaces the car has driven past. See PASSED_MARGIN_M.

        Bounded by construction: only spaces currently in range can be added,
        and the record is dropped when a space leaves range, so this cannot
        grow over a session.
        """
        visible = set()
        for slot in slots:
            visible.add(slot.slot_id)
            if slot.ahead_of_ego > PASSED_MARGIN_M:
                self._passed[slot.slot_id] = True
        for known in list(self._passed):
            if known not in visible:
                del self._passed[known]
        for known in list(self._unplannable):
            if known not in visible:
                del self._unplannable[known]

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

        That stickiness is **beatable**, and it has to be. Held absolutely it
        pinned the first space found -- always the open-ended strip behind the
        last parked car, because that is what the car reaches first -- and the
        real space between two cars, found a moment later, could never take
        its place. A live run spent a whole pass along the row being offered
        the same piece of road. A challenger wins when it is closed where the
        held space is open-ended, or when it is :data:`STICKY_MARGIN_M` nearer;
        anything less and the held space keeps the screen.
        """
        now = self.clock()
        ranked = sorted(slots, key=lambda slot: (self._unplannable_now(slot, now),
                                                 slot.open_ended,
                                                 abs(slot.ahead_of_ego)))
        if self.slot is None:
            return ranked
        held_index = next((index for index, slot in enumerate(ranked)
                           if slot.slot_id == self.slot.slot_id), None)
        if held_index is None or held_index == 0:
            return ranked
        held, best = ranked[held_index], ranked[0]
        if self._unplannable_now(held, now):
            # The held space has since failed to plan. Whatever is on top now
            # is the one the driver can actually be offered.
            return ranked
        if _clearly_better(best, held):
            return ranked
        ranked.insert(0, ranked.pop(held_index))
        return ranked

    def _unplannable_now(self, slot: ParkingSlot, now: float) -> bool:
        """Did planning for this space fail recently? See UNPLANNABLE_TTL_S."""
        failed_at = self._unplannable.get(slot.slot_id)
        return failed_at is not None and now - failed_at < UNPLANNABLE_TTL_S

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
        now = self.clock()
        self.trajectory = None
        for slot in ranked[:MAX_PLAN_CANDIDATES]:
            result = self._planner.plan(ego, slot, boxes)
            self.reason = result.reason
            if result.ok:
                self._unplannable.pop(slot.slot_id, None)
                self.slot = slot
                self.trajectory = result.trajectory
                self._planned_slot_id = slot.slot_id
                return
            # Remembered, so the next scan's ranking does not put this space
            # back on top and undo the offer that was just made below it.
            self._unplannable[slot.slot_id] = now

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
        # Push the key bindings *first*. ``KeyBrakeOutput`` refuses with
        # ``binding_not_pushed`` until ``/key <key> brake`` has been sent for
        # the key it is about to inject -- and this manoeuvre owns its own
        # output objects, so the emergency brake having pushed its bindings
        # says nothing about ours. Asked in the other order, the first live
        # test refused every click with ``binding_not_pushed``.
        pedals = getattr(self.controller, 'pedals', None)
        if hasattr(pedals, 'push_bindings'):
            pedals.push_bindings()
        reason = self.controller.unavailable_reason()
        if reason == 'no_physical_key_tracking':
            # The hooks are a system-wide side effect and are installed on
            # demand, not at startup, so a driver who never uses this feature
            # never gets them. Installing them takes over 100 ms, which is a
            # whole assistance cycle, so it happens on its own thread and this
            # click is refused -- the next one, a moment later, arms.
            self._start_hooks()
            reason = self.controller.unavailable_reason()
        if reason is not None:
            if self.slot is not None and reason not in TRANSIENT_REFUSALS:
                self._refused_slot_id = self.slot.slot_id
            self._report_refusal(reason)
            self._finish(STATE_ABORTED, reason)
            return
        refusal = self.guard.may_inject(own_vehicle)
        if refusal is not None:
            logger.warning("Parking manoeuvre refused by the input guard: %s.",
                           refusal)
            self._finish(STATE_ABORTED, refusal)
            return
        self.follower = PathFollower(self.trajectory)
        self.controller.reset()
        self._brake_free_since = None
        self._last_progress_log = 0.0
        self._started_at = self.clock()
        self._set_state(STATE_PARKING)
        logger.info("Parking manoeuvre started: %s on the %s, %.1f m of path, "
                    "%d stroke(s), planned at a %.1f m radius.",
                    self.slot.kind, self.slot.side, self.trajectory.length,
                    len(self.trajectory.segments), self.trajectory.radius)

    def _report_refusal(self, reason: str):
        """One line per distinct reason per window, not one per attempt.

        A transient refusal is retried on every scan, and the first live run
        of this feature produced four identical warnings a second for as long
        as the driver stood beside the space. The reason still has to be said
        out loud -- a feature that is on and does nothing is the failure mode
        this project keeps hitting -- just not on a loop.
        """
        now = self.clock()
        last = self._refusal_logged.get(reason)
        if last is not None and now - last < REFUSAL_LOG_INTERVAL_S:
            return
        self._refusal_logged[reason] = now
        logger.warning("Parking manoeuvre refused: %s.", reason)

    def _start_hooks(self):
        """Install the physical key hooks off the assistance thread.

        Same shape as ``EmergencyBrake._try_start_hooks`` and for the same
        measured reason: the two ``pynput`` listeners take over 100 ms to
        install, and doing it inline overran the cycle budget on the very pass
        that arms the feature. Nothing waits for the result.
        """
        if self._hooks_pending:
            return
        now = self.clock()
        if (self._hooks_tried_at is not None
                and now - self._hooks_tried_at < HOOK_RETRY_S):
            return
        self._hooks_tried_at = now
        self._hooks_pending = True
        threading.Thread(target=self._start_hooks_off_thread,
                         name='park-hooks', daemon=True).start()

    def _start_hooks_off_thread(self):
        try:
            # Warm pyautogui here too: the first import costs ~255 ms and the
            # steering output's availability check would otherwise pay for it
            # on the assistance thread.
            get_keyboard()
            self.physical_keys.start()
        except Exception as exc:
            logger.error("Starting the physical key hooks raised: %s: %s",
                         type(exc).__name__, exc)
        finally:
            self._hooks_pending = False

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
        now = self.clock()
        # Only believed while our own brake is off and has been for a moment;
        # see DRIVER_BRAKE_OVERRIDE.
        if (self._brake_free_since is not None
                and now - self._brake_free_since >= BRAKE_SETTLE_S
                and own_vehicle.brake >= DRIVER_BRAKE_OVERRIDE):
            self._finish(STATE_ABORTED, 'driver_brake')
            return
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
        self._note_commanded_brake(status.brake, now)
        self._last_demand = demand
        self._log_progress(demand, state, status)
        if status.fault is not None:
            self._finish(STATE_ABORTED, status.fault)

    def _note_commanded_brake(self, brake: float, now: float):
        """Track when the manoeuvre's own brake was last off. See above."""
        if brake > 0.0:
            self._brake_free_since = None
        elif self._brake_free_since is None:
            self._brake_free_since = now

    def _log_progress(self, demand, state: VehicleState, status):
        """One telemetry line per second while a manoeuvre runs.

        A manoeuvre is a ten-second event that either works or does not, and
        when it does not the question is always the same: was the demand
        wrong, or did the car not follow it? Both halves are on the line. One
        second is slow enough not to be spam (ten lines for a typical
        manoeuvre) and fast enough to see a stroke go wrong. Cost: one clock
        comparison on nine cycles out of ten.
        """
        now = self.clock()
        if now - self._last_progress_log < PROGRESS_LOG_INTERVAL_S:
            return
        self._last_progress_log = now
        measured = (state.yaw_rate / (-state.speed_mps if state.reversing
                                      else state.speed_mps)
                    if state.speed_mps > 0.2 else 0.0)
        logger.info("Parking: stroke %d/%d %s %.0f%%, v %.2f/%.2f m/s, "
                    "gear %d/%d%s, kappa %+.3f/%+.3f 1/m, steer %+.2f "
                    "(gain %.3f), thr %.0f brk %.0f",
                    demand.stroke + 1, len(self.trajectory.segments),
                    'rev' if demand.direction < 0 else 'fwd',
                    100.0 * demand.progress, state.speed_mps, demand.speed,
                    state.gear, status.gear_wanted,
                    '' if status.gear_ready else ' (waiting)',
                    demand.curvature, measured, status.steer,
                    getattr(getattr(self.controller, 'model', None),
                            'gain', float('nan')),
                    status.throttle, status.brake)

    def _finish(self, state: str, reason: Optional[str]):
        """End a manoeuvre, whichever way it ended. Always releases first."""
        was_parking = self.state == STATE_PARKING
        if was_parking or self._controller is not None:
            self.controller.release()
        self.follower = None
        self._brake_free_since = None
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
            logger.debug("Parking state: %s -> %s (%s)", self.state, state,
                         self.reason)
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
            # Whole metres: it is read off a button by a moving driver, and
            # publishing tenths meant a fresh event, and a fresh button, on
            # every single scan.
            'distance': round(abs(self.slot.ahead_of_ego)) if self.slot else 0,
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


def _clearly_better(challenger: ParkingSlot, held: ParkingSlot) -> bool:
    """Is *challenger* enough better than *held* to take the offer over?"""
    if held.open_ended and not challenger.open_ended:
        return True
    if challenger.open_ended and not held.open_ended:
        return False
    return (abs(held.ahead_of_ego) - abs(challenger.ahead_of_ego)
            > STICKY_MARGIN_M)
