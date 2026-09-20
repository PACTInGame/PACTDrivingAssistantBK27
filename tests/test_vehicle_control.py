"""The controller between a ControlDemand and three input devices.

Everything here runs against recording stubs, so it says nothing about whether
a key really reaches LFS -- that is what the in-game scenarios are for. What it
does pin is the arithmetic and the three rules that make the manoeuvre safe:
a gear is only changed at a standstill, every exit releases every input, and
the steering model is learned rather than assumed.
"""

import math

import pytest

from assistance.parking.path_follower import ControlDemand
from Controls.vehicle_control import (CurvatureModel, DEFAULT_CURVATURE_GAIN,
                                      GEAR_FIRST, GEAR_NEUTRAL, GEAR_REVERSE,
                                      VehicleController, VehicleState)


class FakeSteering:
    def __init__(self, works=True):
        self.works = works
        self.values = []
        self.released = 0

    def set(self, value):
        self.values.append(value)
        return self.works

    def release(self):
        self.released += 1

    def unavailable_reason(self):
        return None if self.works else 'steering_broken'


class FakePedals:
    def __init__(self, works=True):
        self.works = works
        self.calls = []
        self.released = 0

    def set(self, throttle, brake):
        self.calls.append((throttle, brake))
        return self.works

    def release(self):
        self.released += 1

    def unavailable_reason(self):
        return None


class FakeGears:
    def __init__(self):
        self.requests = []
        self.released = 0

    def select(self, gear):
        self.requests.append(gear)
        return True

    def release(self):
        self.released += 1

    def unavailable_reason(self):
        return None


class Clock:
    def __init__(self):
        self.now = 0.0

    def __call__(self):
        return self.now

    def tick(self, dt=0.1):
        self.now += dt
        return self.now


def controller(**kwargs):
    clock = kwargs.pop('clock', Clock())
    outputs = (kwargs.pop('steering', FakeSteering()),
               kwargs.pop('pedals', FakePedals()),
               kwargs.pop('gears', FakeGears()))
    return VehicleController(*outputs, clock=clock, **kwargs), outputs, clock


def demand(speed=1.0, direction=1, curvature=0.0, **kwargs):
    return ControlDemand(speed=speed, direction=direction,
                         curvature=curvature, **kwargs)


def state(speed=1.0, yaw_rate=0.0, gear=GEAR_FIRST, reversing=False):
    return VehicleState(speed_mps=speed, yaw_rate=yaw_rate, gear=gear,
                        reversing=reversing)


class TestCurvatureModel:
    def test_it_starts_deliberately_weak(self):
        """An under-estimated gain over-steers and corrects; the reverse does not."""
        model = CurvatureModel()
        assert model.gain == DEFAULT_CURVATURE_GAIN
        # A full command is assumed to give only a 20 m radius.
        assert model.curvature_for(1.0) == pytest.approx(0.05)

    def test_it_learns_from_what_the_car_did(self):
        model = CurvatureModel()
        # Command 0.5 produced a 1/6 per metre turn -> the real gain is 1/3.
        for _ in range(60):
            model.observe(0.5, state(speed=1.2, yaw_rate=1.2 / 6.0))
        assert model.gain == pytest.approx(1.0 / 3.0, rel=0.05)

    def test_it_ignores_samples_that_teach_nothing(self):
        model = CurvatureModel()
        before = model.gain
        model.observe(0.5, state(speed=0.05, yaw_rate=0.5))     # barely moving
        model.observe(0.01, state(speed=1.5, yaw_rate=0.5))     # wheel straight
        assert model.gain == before
        assert model.samples == 0

    def test_reversing_does_not_flip_what_it_learns(self):
        """Path curvature is the same arc whichever way the car drives it."""
        forward = CurvatureModel()
        backward = CurvatureModel()
        for _ in range(60):
            forward.observe(0.5, state(speed=1.2, yaw_rate=1.2 / 6.0))
            backward.observe(0.5, state(speed=1.2, yaw_rate=-1.2 / 6.0,
                                        reversing=True))
        assert forward.gain == pytest.approx(backward.gain, rel=1e-6)

    def test_command_for_is_bounded(self):
        model = CurvatureModel()
        assert model.command_for(100.0) == 1.0
        assert model.command_for(-100.0) == -1.0


class TestGearChanges:
    def test_nothing_is_requested_while_the_car_rolls(self):
        control, (_, _, gears), clock = controller()
        control.apply(demand(direction=-1), state(speed=1.0, gear=GEAR_FIRST))
        assert gears.requests == []

    def test_reverse_is_requested_once_stopped(self):
        control, (_, _, gears), clock = controller()
        control.apply(demand(direction=-1), state(speed=0.0, gear=GEAR_FIRST))
        assert gears.requests == [GEAR_REVERSE]

    def test_a_request_is_not_repeated_every_cycle(self):
        control, (_, _, gears), clock = controller()
        for _ in range(4):
            control.apply(demand(direction=-1), state(speed=0.0, gear=GEAR_FIRST))
            clock.tick(0.1)
        assert len(gears.requests) == 1

    def test_it_asks_again_after_the_interval(self):
        control, (_, _, gears), clock = controller()
        control.apply(demand(direction=-1), state(speed=0.0, gear=GEAR_FIRST))
        clock.tick(1.0)
        control.apply(demand(direction=-1), state(speed=0.0, gear=GEAR_FIRST))
        assert len(gears.requests) == 2

    def test_the_right_gear_stops_the_asking(self):
        control, (_, _, gears), clock = controller()
        control.apply(demand(direction=-1), state(speed=0.0, gear=GEAR_REVERSE))
        assert gears.requests == []

    def test_a_gear_that_never_arrives_is_a_fault(self):
        control, _, clock = controller()
        status = None
        for _ in range(60):
            status = control.apply(demand(direction=-1),
                                   state(speed=0.0, gear=GEAR_NEUTRAL))
            clock.tick(0.2)
        assert status.fault == 'gear_change_failed'


class TestPedals:
    def test_it_brakes_while_the_gear_is_wrong(self):
        control, (_, pedals, _), _ = controller()
        control.apply(demand(speed=1.0, direction=-1),
                      state(speed=1.0, gear=GEAR_FIRST))
        throttle, brake = pedals.calls[-1]
        assert throttle == 0.0 and brake > 0.0

    def test_zero_demand_brakes(self):
        control, (_, pedals, _), _ = controller()
        control.apply(demand(speed=0.0), state(speed=0.8))
        assert pedals.calls[-1][1] > 0.0

    def test_under_speed_gives_throttle(self):
        control, (_, pedals, _), _ = controller()
        control.apply(demand(speed=1.2), state(speed=0.5))
        throttle, brake = pedals.calls[-1]
        assert 0.0 < throttle <= 1.0 and brake == 0.0

    def test_the_throttle_is_proportional_rather_than_on_or_off(self):
        """The whole reason the bang-bang loop was replaced.

        A key has no travel, but the *demand* on it can, and
        ``Controls/pulse_modulator.py`` turns a fraction into a duty cycle. A
        controller that answers a 0.2 m/s shortfall and a 1.0 m/s shortfall
        with the same full pedal is the one that sawed between full throttle
        and full brake in the game.
        """
        control, (_, pedals, _), _ = controller()
        control.apply(demand(speed=1.2), state(speed=1.0))
        small = pedals.calls[-1][0]
        control.reset()
        control.apply(demand(speed=1.2), state(speed=0.2))
        large = pedals.calls[-1][0]
        assert 0.0 < small < large

    def test_on_speed_coasts(self):
        control, (_, pedals, _), _ = controller()
        control.apply(demand(speed=1.2), state(speed=1.19))
        throttle, brake = pedals.calls[-1]
        assert throttle == pytest.approx(0.0, abs=1e-6)
        assert brake == pytest.approx(0.0, abs=1e-6)

    def test_well_over_speed_brakes(self):
        control, (_, pedals, _), _ = controller()
        control.apply(demand(speed=1.0), state(speed=2.0))
        assert pedals.calls[-1][1] > 0.0

    def test_a_small_overspeed_brakes_less_than_a_large_one(self):
        control, (_, pedals, _), _ = controller()
        control.apply(demand(speed=1.0), state(speed=1.2))
        gentle = pedals.calls[-1][1]
        control.reset()
        control.apply(demand(speed=1.0), state(speed=3.0))
        hard = pedals.calls[-1][1]
        assert 0.0 < gentle < hard

    def test_the_integral_takes_out_a_standing_shortfall(self):
        """What makes the car's own creep a non-problem rather than a bias."""
        control, (_, pedals, _), clock = controller()
        first = None
        for _ in range(12):
            control.apply(demand(speed=1.2), state(speed=1.0))
            clock.tick(0.1)
            if first is None:
                first = pedals.calls[-1][0]
        assert pedals.calls[-1][0] > first

    def test_the_integral_does_not_wind_up_against_a_car_that_cannot_move(self):
        """A car against a kerb must not bank a pedal it spends later."""
        control, (_, pedals, _), clock = controller()
        for _ in range(60):
            control.apply(demand(speed=1.2), state(speed=0.0))
            clock.tick(0.1)
        from Controls.vehicle_control import MAX_SPEED_INTEGRAL
        assert control._speed_integral <= MAX_SPEED_INTEGRAL

    def test_a_stop_clears_the_integral(self):
        """The next stroke is in the other gear, where the creep differs."""
        control, (_, pedals, _), clock = controller()
        for _ in range(10):
            control.apply(demand(speed=1.2), state(speed=1.0))
            clock.tick(0.1)
        assert control._speed_integral > 0.0
        control.apply(demand(speed=0.0), state(speed=0.0))
        assert control._speed_integral == 0.0


class TestSteering:
    def test_it_commands_the_feedforward(self):
        control, (steering, _, _), _ = controller()
        control.apply(demand(curvature=0.025), state(speed=0.0))
        # Default gain 0.05, so half a command for half that curvature.
        assert steering.values[-1] == pytest.approx(0.5, abs=0.01)

    def test_the_trim_corrects_what_the_model_has_not_learned(self):
        control, (steering, _, _), clock = controller()
        # A curvature the default gain answers with 40 % of full lock, so
        # there is room left for the trim to show up in the command.
        for _ in range(20):
            control.apply(demand(curvature=0.02), state(speed=1.0, yaw_rate=0.0))
            clock.tick(0.1)
        assert control._trim > 0.0
        assert steering.values[-1] > steering.values[0]

    def test_the_trim_decays_at_a_standstill(self):
        """One stroke's correction must not be carried into the next."""
        control, (steering, _, _), clock = controller()
        for _ in range(20):
            control.apply(demand(curvature=0.02), state(speed=1.0, yaw_rate=0.0))
            clock.tick(0.1)
        wound_up = control._trim
        assert wound_up > 0.0
        for _ in range(40):
            control.apply(demand(curvature=0.0), state(speed=0.0))
            clock.tick(0.1)
        assert abs(control._trim) < abs(wound_up) * 0.2

    def test_a_steering_failure_is_reported_and_zeroed(self):
        control, (steering, _, _), _ = controller(steering=FakeSteering(works=False))
        status = control.apply(demand(curvature=0.1), state())
        assert status.fault == 'steering_failed'
        assert status.steer == 0.0


class TestLifecycle:
    def test_unavailable_reason_comes_from_the_outputs(self):
        control, _, _ = controller(steering=FakeSteering(works=False))
        assert control.unavailable_reason() == 'steering_broken'
        control, _, _ = controller()
        assert control.unavailable_reason() is None

    def test_release_gives_every_input_back(self):
        control, (steering, pedals, gears), _ = controller()
        control.apply(demand(curvature=0.1), state())
        control.release()
        assert steering.released == 1
        assert pedals.released == 1
        assert gears.released == 1

    def test_release_survives_an_output_that_throws(self):
        class Angry(FakeSteering):
            def release(self):
                raise RuntimeError("nope")

        control, (_, pedals, gears), _ = controller(steering=Angry())
        control.release()          # must not raise
        assert pedals.released == 1
        assert gears.released == 1

    def test_reset_keeps_what_was_learned(self):
        control, _, clock = controller()
        for _ in range(60):
            control.model.observe(0.5, state(speed=1.2, yaw_rate=1.2 / 6.0))
        learned = control.model.gain
        control.reset()
        assert control.model.gain == learned
        assert control._trim == 0.0
