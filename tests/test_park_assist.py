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
                                    STATE_SCANNING)
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
    """Two parked cars on the right of a car at x = 14, with 10 m between."""
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
    """Run cycles for *seconds*, returning the last published payload."""
    own = own or own_vehicle()
    payload = None
    for _ in range(int(seconds / 0.1)):
        payload = system.process(own, vehicles)
        clock.tick(0.1)
    return payload


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
        fast = own_vehicle(speed_kmh=60.0)
        payload = scan_until(system, clock, a_parallel_space(), own=fast)
        assert payload['state'] == STATE_SCANNING
        assert payload['kind'] is None

    def test_an_empty_road_offers_nothing(self, tmp_path):
        system, _, clock = build(tmp_path)
        payload = scan_until(system, clock, {})
        assert payload['state'] == STATE_SCANNING

    def test_a_space_is_found_and_offered(self, tmp_path):
        system, _, clock = build(tmp_path)
        payload = scan_until(system, clock, a_parallel_space())
        assert payload['state'] == STATE_OFFERED
        assert payload['kind'] == 'parallel'
        assert payload['side'] == 'right'
        assert payload['length'] > CAR_L
        assert payload['strokes'] >= 1

    def test_a_space_is_not_offered_before_it_has_settled(self, tmp_path):
        system, _, clock = build(tmp_path)
        payload = scan_until(system, clock, a_parallel_space(),
                             seconds=SCAN_INTERVAL_S + 0.05)
        assert payload['state'] == STATE_SCANNING

    def test_a_moving_car_is_not_a_boundary(self, tmp_path):
        system, _, clock = build(tmp_path)
        rolling = a_parallel_space()
        for vehicle in rolling.values():
            vehicle.data.speed = 20.0
        payload = scan_until(system, clock, rolling)
        assert payload['state'] == STATE_SCANNING


class TestConsent:
    def test_it_never_parks_without_a_click(self, tmp_path):
        """The rule the whole feature is built around."""
        controller = RecordingController()
        system, _, clock = build(tmp_path, controller=controller)
        scan_until(system, clock, a_parallel_space(), seconds=20.0)
        assert system.state == STATE_OFFERED
        assert controller.applied == []
        assert controller.resets == 0

    def test_clicking_the_offer_starts_it(self, tmp_path):
        controller = RecordingController()
        system, bus, clock = build(tmp_path, controller=controller)
        scan_until(system, clock, a_parallel_space())
        bus.emit('park_assist_accept', {})
        system.process(own_vehicle(), a_parallel_space())
        assert system.state == STATE_PARKING
        assert controller.resets == 1

    def test_the_offer_button_is_the_same_thing(self, tmp_path):
        system, bus, clock = build(tmp_path)
        scan_until(system, clock, a_parallel_space())
        bus.emit('button_clicked', type('Click', (), {'ClickID': BTN_PARK_OFFER}))
        system.process(own_vehicle(), a_parallel_space())
        assert system.state == STATE_PARKING

    def test_a_click_on_something_else_does_nothing(self, tmp_path):
        system, bus, clock = build(tmp_path)
        scan_until(system, clock, a_parallel_space())
        bus.emit('button_clicked', type('Click', (), {'ClickID': 22}))
        system.process(own_vehicle(), a_parallel_space())
        assert system.state == STATE_OFFERED

    def test_auto_accept_starts_it_for_a_recording(self, tmp_path):
        """The one exception, off by default, for replays that cannot click."""
        controller = RecordingController()
        system, _, clock = build(tmp_path, controller=controller,
                                 park_assist_auto_accept=True)
        scan_until(system, clock, a_parallel_space())
        assert system.state == STATE_PARKING

    def test_a_controller_that_cannot_run_refuses_out_loud(self, tmp_path):
        controller = RecordingController(reason='pyautogui_missing')
        system, bus, clock = build(tmp_path, controller=controller)
        scan_until(system, clock, a_parallel_space())
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
        scan_until(system, clock, a_parallel_space())
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

    def test_a_manoeuvre_stands_other_actuators_down(self, tmp_path):
        seen = []
        controller = RecordingController()
        system, bus, clock = build(tmp_path, controller=controller)
        bus.subscribe('manoeuvre_active', seen.append)
        scan_until(system, clock, a_parallel_space())
        bus.emit('park_assist_accept', {})
        system.process(own_vehicle(), a_parallel_space())
        assert any(event['active'] for event in seen)
        system._finish(STATE_DONE, None)
        assert not seen[-1]['active']

    def test_the_driver_braking_ends_it(self, tmp_path):
        system, _, _, controller = self._started(tmp_path)
        system.process(own_vehicle(brake=0.9), a_parallel_space())
        assert system.state == STATE_ABORTED
        assert system.reason == 'driver_brake'
        assert controller.releases == 1

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
        scan_until(system, clock, a_parallel_space(), seconds=5.0)
        # 50 cycles, a handful of state changes.
        assert 0 < len(seen) <= 8

    def test_the_payload_describes_the_space(self, tmp_path):
        system, _, clock = build(tmp_path)
        payload = scan_until(system, clock, a_parallel_space())
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
        for _ in range(40):                # 4 s at the 100 ms cycle
            system.process(own_vehicle(), vehicles)
            clock.tick(0.1)
        assert len(calls) <= int(4.0 / SCAN_INTERVAL_S) + 1
