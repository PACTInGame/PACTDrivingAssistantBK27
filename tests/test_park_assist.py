"""The parking state machine: what it offers, what it refuses, when it lets go.

The geometry is tested elsewhere; these are about the decisions. The one that
matters most has a test of its own and is worth stating here too: **nothing
starts the manoeuvre except a click.** A space being found, the car stopping,
the driver waiting -- none of them is consent.
"""

import math

import pytest

from assistance.park_assist import (BTN_PARK_CANCEL, BTN_PARK_OFFER,
                                    OFFER_SETTLE_S, ParkAssist, SCAN_INTERVAL_S,
                                    STATE_ABORTED, STATE_DONE, STATE_OFF,
                                    STATE_OFFERED, STATE_PARKING,
                                    STATE_SCANNING, UNPLANNABLE_TTL_S,
                                    BRAKE_SETTLE_S, PASSED_TTL_S)
from assistance.parking.slot_detection import ParkingSlot
from core.event_bus import EventBus
from core.settings_manager import SettingsManager
from vehicles.own_vehicle import OwnVehicle
from vehicles.vehicle import Vehicle

CAR_L, CAR_W = 5.0, 2.1          # the conservative size an unknown car gets
METRE = 65536


class Clock:
    def __init__(self):
        self.now = 0.0

    def __call__(self):
        return self.now

    def tick(self, dt):
        self.now += dt
        return self.now


class PermissiveGuard:
    """An input guard that always allows, or always refuses with a reason."""

    def __init__(self, refusal=None):
        self.refusal = refusal

    def may_inject(self, own_vehicle=None):
        return self.refusal


class RecordingController:
    def __init__(self, reason=None, fault=None):
        self.reason = reason
        self.fault = fault
        self.applied = []
        self.releases = 0
        self.resets = 0
        self.pedals = None

    def unavailable_reason(self):
        return self.reason

    def reset(self):
        self.resets += 1

    def release(self):
        self.releases += 1

    def apply(self, demand, state):
        from Controls.vehicle_control import ControlStatus
        self.applied.append((demand, state))
        return ControlStatus(fault=self.fault)


class BrakingController(RecordingController):
    """A controller that reports commanding a brake, as the real one does."""

    def __init__(self, brake=1.0, **kwargs):
        super().__init__(**kwargs)
        self.brake = brake

    def apply(self, demand, state):
        from Controls.vehicle_control import ControlStatus
        self.applied.append((demand, state))
        return ControlStatus(brake=self.brake, fault=self.fault)


def settings(tmp_path, **overrides):
    manager = SettingsManager(str(tmp_path / 'settings.json'))
    manager.set('park_assist', True)
    manager.set('park_distance_control_mode', 1)
    for key, value in overrides.items():
        manager.set(key, value)
    return manager


# LFS heading word for a car pointing along +X in the maths frame. The
# conversion is ``(heading + 16384) * 2pi / 65536`` (``conventions.md`` §2), so
# +X is 49152, not 16384 -- which is the other way down the same road.
HEADING_EAST = 49152


def own_vehicle(x=14.0, y=0.0, heading=HEADING_EAST, speed_kmh=4.0, gear=2,
                brake=0.0, plid=1):
    """An OwnVehicle at *x*, *y* metres, pointing along +X.

    So "ahead" is +x and "left" is +y, and these scenes read the same way as
    the ones in the geometry tests.
    """
    vehicle = OwnVehicle()
    vehicle.set_local_driver(plid)
    vehicle.viewed_plid = plid
    vehicle.gear = gear
    vehicle.brake = brake
    vehicle.data.x = x * METRE
    vehicle.data.y = y * METRE
    vehicle.data.heading = heading
    vehicle.data.speed = speed_kmh
    vehicle.data.cname = 'XRG'
    vehicle.data.yaw_rate = 0.0
    return vehicle


def parked_car(plid, x, y, heading=HEADING_EAST, speed=0.0):
    vehicle = Vehicle(plid)
    vehicle.data.x = x * METRE
    vehicle.data.y = y * METRE
    vehicle.data.heading = heading
    vehicle.data.speed = speed
    vehicle.data.cname = 'XRG'
    vehicle.data.distance_to_player = 10.0
    return vehicle


def a_parallel_space():
    """Two parked cars on the right of the road, with 10 m of kerb between.

    The gap runs from x = 2.25 to x = 12.75, so its centre is at 7.5 and a car
    driving +X passes it somewhere around x = 9.5.
    """
    return {2: parked_car(2, 0.0, -3.2), 3: parked_car(3, CAR_L + 10.0, -3.2)}


def build(tmp_path, controller=None, guard=None, **overrides):
    bus = EventBus()
    clock = Clock()
    system = ParkAssist(bus, settings(tmp_path, **overrides),
                        guard=guard or PermissiveGuard(),
                        controller=controller or RecordingController(),
                        clock=clock)
    system._on_track = True
    return system, bus, clock


def scan_until(system, clock, vehicles, own=None, seconds=3.0):
    """Run cycles for *seconds* with the car parked at x = 14.

    No approach: the car has not driven past anything, so nothing is offered.
    Used by the tests that check exactly that, and by the ones where the state
    machine is already past the offer.
    """
    own = own or own_vehicle()
    payload = None
    for _ in range(int(seconds / 0.1)):
        payload = system.process(own, vehicles)
        clock.tick(0.1)
    return payload


def drive_past(system, clock, vehicles, factory=None, seconds=3.0):
    """Drive the car up the road past the space, then sit beside it.

    A space is only offered once it has been *driven past* -- the same thing a
    real slot scanner measures -- so every test that expects an offer has to
    produce that, not just put the car next to a gap
    (``park_assist.PASSED_MARGIN_M``).
    """
    factory = factory or (lambda x: own_vehicle(x=x))
    for step in range(13):
        x = -10.0 + (24.0 * step / 12.0)          # -10 m up to +14 m
        system.process(factory(x), vehicles)
        clock.tick(0.25)
    return scan_until(system, clock, vehicles, own=factory(14.0),
                      seconds=seconds)


class TestScanning:
    def test_nothing_happens_while_it_is_switched_off(self, tmp_path):
        system, _, clock = build(tmp_path, park_assist=False)
        payload = scan_until(system, clock, a_parallel_space())
        assert payload['state'] == STATE_OFF

    def test_nothing_happens_off_track(self, tmp_path):
        system, _, clock = build(tmp_path)
        system._on_track = False
        payload = scan_until(system, clock, a_parallel_space())
        assert payload['state'] == STATE_OFF

    def test_nothing_happens_while_watching_another_car(self, tmp_path):
        """OutGauge follows the camera; actuation never may (conventions §5.2)."""
        system, _, clock = build(tmp_path)
        watching = own_vehicle()
        watching.viewed_plid = 99            # TAB moved the camera
        payload = scan_until(system, clock, a_parallel_space(), own=watching)
        assert payload['state'] == STATE_OFF

    def test_it_does_not_look_at_road_speed(self, tmp_path):
        system, _, clock = build(tmp_path)
        payload = drive_past(system, clock, a_parallel_space(),
                             factory=lambda x: own_vehicle(x=x, speed_kmh=60.0))
        assert payload['state'] == STATE_SCANNING
        assert payload['kind'] is None

    def test_an_empty_road_offers_nothing(self, tmp_path):
        system, _, clock = build(tmp_path)
        payload = scan_until(system, clock, {})
        assert payload['state'] == STATE_SCANNING

    def test_a_space_is_found_and_offered(self, tmp_path):
        system, _, clock = build(tmp_path)
        payload = drive_past(system, clock, a_parallel_space())
        assert payload['state'] == STATE_OFFERED
        assert payload['kind'] == 'parallel'
        assert payload['side'] == 'right'
        assert payload['length'] > CAR_L
        assert payload['strokes'] >= 1

    def test_a_space_that_blinks_out_of_one_scan_is_still_offered(self, tmp_path):
        """Forgetting having driven past a space cost a whole live run.

        An open-ended space is measured from whichever cars are in range, so
        it leaves the scan and comes back as a matter of course. The record
        used to be dropped the moment it did, and the space was then never
        offered again however long the driver waited beside it.
        """
        system, _, clock = build(tmp_path)
        vehicles = a_parallel_space()
        drive_past(system, clock, vehicles, seconds=0.0)
        # One scan in which the space is not visible at all.
        system.process(own_vehicle(x=14.0), {})
        clock.tick(SCAN_INTERVAL_S + 0.05)
        payload = scan_until(system, clock, vehicles, own=own_vehicle(x=14.0))
        assert payload['state'] == STATE_OFFERED

    def test_the_record_does_not_outlive_its_welcome(self, tmp_path):
        system, _, clock = build(tmp_path)
        vehicles = a_parallel_space()
        drive_past(system, clock, vehicles, seconds=0.0)
        assert system._passed
        clock.tick(PASSED_TTL_S + 1.0)
        system.process(own_vehicle(x=14.0), {})
        assert system._passed == {}

    def test_a_space_the_car_has_not_driven_past_is_not_offered(self, tmp_path):
        """Joining the track beside a gap is not the same as finding one.

        The first live test offered a space the instant the driver appeared on
        track, several car lengths before reaching it.
        """
        system, _, clock = build(tmp_path)
        payload = scan_until(system, clock, a_parallel_space(), seconds=10.0)
        assert payload['state'] == STATE_SCANNING

    def test_a_space_is_not_offered_before_it_has_settled(self, tmp_path):
        system, _, clock = build(tmp_path)
        payload = drive_past(system, clock, a_parallel_space(),
                             seconds=SCAN_INTERVAL_S + 0.05)
        assert payload['state'] == STATE_OFFERED or payload['kind'] is not None

    def test_a_moving_car_is_not_a_boundary(self, tmp_path):
        system, _, clock = build(tmp_path)
        rolling = a_parallel_space()
        for vehicle in rolling.values():
            vehicle.data.speed = 20.0
        payload = drive_past(system, clock, rolling)
        assert payload['state'] == STATE_SCANNING


class TestRanking:
    """``_rank`` decides which of several spaces the driver is offered."""

    @staticmethod
    def _slot(bounds, open_ended, ahead):
        from assistance.parking.geometry import Pose
        return ParkingSlot(kind='parallel', side='right',
                           entry=Pose(0.0, 0.0, 0.0),
                           target=Pose(0.0, 0.0, 0.0),
                           length=6.5, depth=2.5, bounded_by=bounds,
                           open_ended=open_ended, ahead_of_ego=ahead)

    def test_a_closed_space_beats_a_nearer_open_one(self, tmp_path):
        system, _, _ = build(tmp_path)
        open_one = self._slot(('a',), True, -2.0)
        closed = self._slot(('a', 'b'), False, -6.0)
        assert system._rank([open_one, closed])[0] is closed

    def test_the_space_on_screen_is_not_swapped_out_for_a_marginal_one(self, tmp_path):
        system, _, _ = build(tmp_path)
        held = self._slot(('a', 'b'), False, -5.0)
        rival = self._slot(('c', 'd'), False, -4.0)
        system.slot = held
        assert system._rank([rival, held])[0] is held

    def test_a_clearly_better_space_takes_the_offer_over(self, tmp_path):
        """The defect a live run found: the first, open-ended space stuck.

        ``_rank`` used to pin whatever was already offered, so the strip of
        road behind the last parked car -- always found first, because it is
        nearest -- held the screen for the whole drive down the row and the
        real space between two cars could never win.
        """
        system, _, _ = build(tmp_path)
        held = self._slot(('a',), True, -3.0)
        better = self._slot(('a', 'b'), False, -8.0)
        system.slot = held
        assert system._rank([held, better])[0] is better

    def test_a_space_that_would_not_plan_drops_to_the_back(self, tmp_path):
        """The 2 Hz flicker a live run showed, in one assertion.

        ``_plan_best_of`` offers the best *drivable* candidate, which need not
        be the best-ranked one. Ranked purely on geometry the undrivable space
        came back to the top on the very next scan, the offered slot changed
        under the driver, and the button blinked on and off twice a second.
        """
        system, _, clock = build(tmp_path)
        undrivable = self._slot(('a', 'b'), False, -3.0)
        drivable = self._slot(('c', 'd'), False, -9.0)
        system._unplannable[undrivable.slot_id] = clock()
        assert system._rank([undrivable, drivable])[0] is drivable

    def test_a_space_is_reconsidered_once_the_memory_expires(self, tmp_path):
        system, _, clock = build(tmp_path)
        undrivable = self._slot(('a', 'b'), False, -3.0)
        drivable = self._slot(('c', 'd'), False, -9.0)
        system._unplannable[undrivable.slot_id] = clock()
        clock.tick(UNPLANNABLE_TTL_S + 0.1)
        assert system._rank([undrivable, drivable])[0] is undrivable

    def test_a_much_nearer_space_of_the_same_kind_also_wins(self, tmp_path):
        system, _, _ = build(tmp_path)
        held = self._slot(('a', 'b'), False, -12.0)
        nearer = self._slot(('c', 'd'), False, -3.0)
        system.slot = held
        assert system._rank([nearer, held])[0] is nearer


class TestConsent:
    def test_it_never_parks_without_a_click(self, tmp_path):
        """The rule the whole feature is built around."""
        controller = RecordingController()
        system, _, clock = build(tmp_path, controller=controller)
        drive_past(system, clock, a_parallel_space(), seconds=20.0)
        assert system.state == STATE_OFFERED
        assert controller.applied == []
        assert controller.resets == 0

    def test_clicking_the_offer_starts_it(self, tmp_path):
        controller = RecordingController()
        system, bus, clock = build(tmp_path, controller=controller)
        drive_past(system, clock, a_parallel_space())
        bus.emit('park_assist_accept', {})
        system.process(own_vehicle(), a_parallel_space())
        assert system.state == STATE_PARKING
        assert controller.resets == 1

    def test_the_offer_button_is_the_same_thing(self, tmp_path):
        system, bus, clock = build(tmp_path)
        drive_past(system, clock, a_parallel_space())
        bus.emit('button_clicked', type('Click', (), {'ClickID': BTN_PARK_OFFER}))
        system.process(own_vehicle(), a_parallel_space())
        assert system.state == STATE_PARKING

    def test_a_click_on_something_else_does_nothing(self, tmp_path):
        system, bus, clock = build(tmp_path)
        drive_past(system, clock, a_parallel_space())
        bus.emit('button_clicked', type('Click', (), {'ClickID': 22}))
        system.process(own_vehicle(), a_parallel_space())
        assert system.state == STATE_OFFERED

    def test_auto_accept_starts_it_for_a_recording(self, tmp_path):
        """The one exception, off by default, for replays that cannot click."""
        controller = RecordingController()
        system, _, clock = build(tmp_path, controller=controller,
                                 park_assist_auto_accept=True)
        drive_past(system, clock, a_parallel_space())
        assert system.state == STATE_PARKING

    def test_a_controller_that_cannot_run_refuses_out_loud(self, tmp_path):
        controller = RecordingController(reason='pyautogui_missing')
        system, bus, clock = build(tmp_path, controller=controller)
        drive_past(system, clock, a_parallel_space())
        bus.emit('park_assist_accept', {})
        system.process(own_vehicle(), a_parallel_space())
        assert system.state == STATE_ABORTED
        assert system.reason == 'pyautogui_missing'
        assert controller.applied == []


class TestDriving:
    @staticmethod
    def _started(tmp_path, controller=None, guard=None):
        controller = controller or RecordingController()
        system, bus, clock = build(tmp_path, controller=controller, guard=guard)
        drive_past(system, clock, a_parallel_space())
        bus.emit('park_assist_accept', {})
        system.process(own_vehicle(), a_parallel_space())
        assert system.state == STATE_PARKING
        return system, bus, clock, controller

    def test_it_drives_the_controller(self, tmp_path):
        system, _, clock, controller = self._started(tmp_path)
        system.process(own_vehicle(), a_parallel_space())
        assert controller.applied
        demand, state = controller.applied[-1]
        assert demand.total > 0.0
        assert state.gear == 2

    def test_the_plan_driven_is_made_from_where_the_car_is_now(self, tmp_path):
        """The offer is planned when the space is found, the click comes later.

        A live run clicked several seconds after the offer, by which time the
        car had rolled on; the follower started off its own path, pure pursuit
        wound up, and the car swung into the parked car it was avoiding.
        """
        controller = RecordingController()
        system, bus, clock = build(tmp_path, controller=controller)
        vehicles = a_parallel_space()
        drive_past(system, clock, vehicles)
        offered = system.trajectory
        assert offered is not None
        # The driver rolls on a metre and then clicks.
        moved = own_vehicle(x=15.0)
        bus.emit('park_assist_accept', {})
        system.process(moved, vehicles)
        assert system.state == STATE_PARKING
        assert system.trajectory is not offered, "drove a stale plan"
        assert math.hypot(system.trajectory.start.x - moved.data.x / METRE,
                          system.trajectory.start.y - moved.data.y / METRE) < 1.0

    def test_a_space_that_went_away_between_offer_and_click_is_refused(self, tmp_path):
        controller = RecordingController()
        system, bus, clock = build(tmp_path, controller=controller)
        vehicles = a_parallel_space()
        drive_past(system, clock, vehicles)
        assert system.state == STATE_OFFERED
        # Somebody parks in it.
        vehicles[4] = parked_car(4, 7.5, -3.2)
        bus.emit('park_assist_accept', {})
        system.process(own_vehicle(), vehicles)
        assert system.state != STATE_PARKING
        assert controller.applied == [], 'took the car over with no plan'

    def test_a_manoeuvre_stands_other_actuators_down(self, tmp_path):
        seen = []
        controller = RecordingController()
        system, bus, clock = build(tmp_path, controller=controller)
        bus.subscribe('manoeuvre_active', seen.append)
        drive_past(system, clock, a_parallel_space())
        bus.emit('park_assist_accept', {})
        system.process(own_vehicle(), a_parallel_space())
        assert any(event['active'] for event in seen)
        system._finish(STATE_DONE, None)
        assert not seen[-1]['active']

    def test_the_driver_braking_ends_it(self, tmp_path):
        system, _, clock, controller = self._started(tmp_path)
        # One cycle with our own brake off, so the reading is believed.
        system.process(own_vehicle(), a_parallel_space())
        clock.tick(BRAKE_SETTLE_S + 0.05)
        system.process(own_vehicle(brake=0.9), a_parallel_space())
        assert system.state == STATE_ABORTED
        assert system.reason == 'driver_brake'
        assert controller.releases == 1

    def test_our_own_brake_does_not_count_as_the_driver_braking(self, tmp_path):
        """OutGauge.Brake is the merged pedal, and the manoeuvre brakes first.

        The gear is not in on the opening cycles, so the controller commands a
        full brake; read as the driver's, it ended every manoeuvre the first
        live run started.
        """
        controller = BrakingController(brake=1.0)
        system, _, clock, _ = self._started(tmp_path, controller=controller)
        # One cycle to command the brake, then the merged reading comes back.
        system.process(own_vehicle(brake=0.0), a_parallel_space())
        clock.tick(BRAKE_SETTLE_S + 0.05)
        system.process(own_vehicle(brake=1.0), a_parallel_space())
        assert system.state == STATE_PARKING
        assert system.reason != 'driver_brake'

    def test_the_driver_braking_on_top_of_ours_still_ends_it(self, tmp_path):
        controller = BrakingController(brake=0.35)
        system, _, clock, _ = self._started(tmp_path, controller=controller)
        # A full reading while the manoeuvre's own brake key is down says
        # nothing -- the key is the same keystroke at 0.35 and at 1.0.
        system.process(own_vehicle(brake=1.0), a_parallel_space())
        clock.tick(BRAKE_SETTLE_S + 0.05)
        system.process(own_vehicle(brake=1.0), a_parallel_space())
        assert system.state == STATE_PARKING
        # It only becomes the driver's once we stop asking for it.
        controller.brake = 0.0
        system.process(own_vehicle(brake=1.0), a_parallel_space())
        clock.tick(BRAKE_SETTLE_S + 0.05)
        system.process(own_vehicle(brake=1.0), a_parallel_space())
        assert system.state == STATE_ABORTED
        assert system.reason == 'driver_brake'

    def test_cancelling_ends_it(self, tmp_path):
        system, bus, _, controller = self._started(tmp_path)
        bus.emit('button_clicked', type('Click', (), {'ClickID': BTN_PARK_CANCEL}))
        system.process(own_vehicle(), a_parallel_space())
        assert system.state == STATE_ABORTED
        assert system.reason == 'driver_cancel'
        assert controller.releases == 1

    def test_the_input_guard_refusing_ends_it(self, tmp_path):
        guard = PermissiveGuard()
        system, _, _, controller = self._started(tmp_path, guard=guard)
        guard.refusal = 'lfs_not_focused'
        system.process(own_vehicle(), a_parallel_space())
        assert system.state == STATE_ABORTED
        assert system.reason == 'lfs_not_focused'
        assert controller.releases == 1

    def test_an_actuator_fault_ends_it(self, tmp_path):
        controller = RecordingController()
        system, _, _, _ = self._started(tmp_path, controller=controller)
        controller.fault = 'steering_failed'
        system.process(own_vehicle(), a_parallel_space())
        assert system.state == STATE_ABORTED
        assert system.reason == 'steering_failed'

    def test_leaving_the_track_ends_it(self, tmp_path):
        system, bus, _, controller = self._started(tmp_path)
        bus.emit('state_data', {'on_track': False})
        assert system.state == STATE_ABORTED
        assert controller.releases == 1

    def test_a_runaway_ends_it(self, tmp_path):
        system, _, _, controller = self._started(tmp_path)
        system.process(own_vehicle(speed_kmh=40.0), a_parallel_space())
        assert system.state == STATE_ABORTED
        assert system.reason == 'runaway'

    def test_a_manoeuvre_that_never_ends_is_given_up_on(self, tmp_path):
        system, _, clock, controller = self._started(tmp_path)
        clock.tick(500.0)
        system.process(own_vehicle(), a_parallel_space())
        assert system.state == STATE_ABORTED
        assert system.reason == 'timeout'

    def test_shutdown_releases_the_car(self, tmp_path):
        system, _, _, controller = self._started(tmp_path)
        system.shutdown()
        assert controller.releases == 1

    def test_it_keeps_running_when_switched_off_mid_manoeuvre(self, tmp_path):
        """Something has to give the car back."""
        system, _, _, _ = self._started(tmp_path)
        system.settings.set('park_assist', False)
        assert system.is_enabled()


class TestPublishing:
    def test_the_screen_is_only_told_about_changes(self, tmp_path):
        seen = []
        system, bus, clock = build(tmp_path)
        bus.subscribe('park_assist_changed', seen.append)
        drive_past(system, clock, a_parallel_space(), seconds=5.0)
        # Roughly 60 assistance cycles and 25 scans; the screen hears about a
        # state change and about the distance in whole metres, nothing else.
        assert 0 < len(seen) <= 15
        payloads = [tuple(sorted(event.items())) for event in seen]
        assert all(a != b for a, b in zip(payloads, payloads[1:]))

    def test_the_payload_describes_the_space(self, tmp_path):
        system, _, clock = build(tmp_path)
        payload = drive_past(system, clock, a_parallel_space())
        assert set(payload) >= {'state', 'kind', 'side', 'length', 'distance',
                                'strokes', 'stroke', 'progress', 'reason'}


class TestCost:
    def test_scanning_is_rate_limited(self, tmp_path):
        """The scan and the obstacle rebuild must not land on every cycle."""
        system, _, clock = build(tmp_path)
        calls = []
        vehicles = a_parallel_space()
        original = system._obstacles

        def counted(*args, **kwargs):
            calls.append(1)
            return original(*args, **kwargs)

        system._obstacles = counted
        for step in range(40):             # 4 s at the 100 ms cycle
            system.process(own_vehicle(x=-10.0 + step * 0.6), vehicles)
            clock.tick(0.1)
        assert len(calls) <= int(4.0 / SCAN_INTERVAL_S) + 1


class TestHooks:
    """The physical key hooks are installed on demand, not at startup.

    A low-level Windows hook sees every keystroke on the machine, so a driver
    who never uses self-parking never gets one. That means the *first* click
    can legitimately be refused while the hooks come up, and the refusal must
    not turn into a retry loop -- a live run produced four log lines a second
    for as long as the car stood beside the space before this was fixed.
    """

    def test_the_first_refusal_installs_the_hooks(self, tmp_path, monkeypatch):
        controller = RecordingController(reason='no_physical_key_tracking')
        system, bus, clock = build(tmp_path, controller=controller)
        started = []
        monkeypatch.setattr(system, '_start_hooks',
                            lambda: started.append(clock.now))
        drive_past(system, clock, a_parallel_space())
        bus.emit('park_assist_accept', {})
        system.process(own_vehicle(), a_parallel_space())
        assert started
        assert system.state == STATE_ABORTED

    def test_a_permanently_refused_space_is_not_offered_again(self, tmp_path):
        controller = RecordingController(reason='lfs_window_not_found')
        system, _, clock = build(tmp_path, controller=controller,
                                 park_assist_auto_accept=True)
        drive_past(system, clock, a_parallel_space(), seconds=20.0)
        assert system._refused_slot_id is not None
        assert controller.applied == []

    def test_a_transient_refusal_is_retried(self, tmp_path, monkeypatch):
        """The hooks are installed on the first click, so it always fails.

        Remembering that refusal against the space left the car standing
        beside it doing nothing -- which is what the first live run did.
        """
        controller = RecordingController(reason='no_physical_key_tracking')
        system, _, clock = build(tmp_path, controller=controller,
                                 park_assist_auto_accept=True)
        monkeypatch.setattr(system, '_start_hooks', lambda: None)
        drive_past(system, clock, a_parallel_space(), seconds=5.0)
        assert system._refused_slot_id is None
        # Now the hooks are up: the very next offer arms.
        controller.reason = None
        scan_until(system, clock, a_parallel_space(), seconds=2.0)
        assert system.state == STATE_PARKING

    def test_a_repeated_refusal_is_logged_once_per_window(self, tmp_path,
                                                          monkeypatch, caplog):
        controller = RecordingController(reason='no_physical_key_tracking')
        system, _, clock = build(tmp_path, controller=controller,
                                 park_assist_auto_accept=True)
        monkeypatch.setattr(system, '_start_hooks', lambda: None)
        with caplog.at_level('WARNING'):
            drive_past(system, clock, a_parallel_space(), seconds=20.0)
        refusals = [r for r in caplog.records if 'refused' in r.message]
        assert len(refusals) == 1

    def test_hooks_are_only_installed_once_per_retry_window(self, tmp_path,
                                                            monkeypatch):
        system, _, clock = build(tmp_path)
        threads = []
        monkeypatch.setattr('assistance.park_assist.threading.Thread',
                            lambda **kwargs: _FakeThread(threads, **kwargs))
        for _ in range(5):
            system._start_hooks()
            clock.tick(0.5)
        assert len(threads) == 1


class _FakeThread:
    def __init__(self, sink, target=None, name=None, daemon=None):
        sink.append(name)
        self.target = target

    def start(self):
        self.target()
