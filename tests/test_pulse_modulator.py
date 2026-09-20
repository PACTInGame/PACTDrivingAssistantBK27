"""Turning a fractional pedal demand into presses of a key that has no travel.

The property that matters is the **average**: over enough cycles, the key has
to be down for the fraction of the time that was asked for. Everything else
here is about the two ways that can go wrong in the game -- a pulse too short
for LFS to see, and a hold that outlives the demand.
"""

import pytest

from Controls.pulse_modulator import (CONTINUOUS_DUTY, DEADBAND_DUTY,
                                      HOLD_PERIODS, MIN_PULSE_S,
                                      PulseModulator)

PERIOD = 0.1


class FakeTapper:
    """Records taps. ``works=False`` is an output that cannot inject."""

    def __init__(self, works=True):
        self.works = works
        self.taps = []

    def tap(self, key, hold_s=0.1, delay_s=0.0):
        self.taps.append((key, round(hold_s, 6)))
        return self.works

    @property
    def held_time(self):
        return sum(hold for _, hold in self.taps)


def modulator(works=True):
    tapper = FakeTapper(works)
    return PulseModulator(tapper, PERIOD, name='test'), tapper


class TestAverage:
    @pytest.mark.parametrize('duty', [0.05, 0.1, 0.25, 0.5, 0.75, 0.85])
    def test_the_key_is_down_for_the_fraction_asked_for(self, duty):
        """The one property the control loop above depends on.

        Tolerated to within one carried remainder, which is what the budget
        is: at the end of a run up to one pulse's worth may still be banked.
        """
        pulse, tapper = modulator()
        cycles = 200
        for _ in range(cycles):
            assert pulse.apply('b', duty)
        wanted = duty * PERIOD * cycles
        assert tapper.held_time == pytest.approx(wanted, abs=PERIOD)

    def test_a_small_demand_still_produces_pulses_the_game_can_see(self):
        """Rounded down it would be silently zero; rounded up, far too much."""
        pulse, tapper = modulator()
        for _ in range(100):
            pulse.apply('b', 0.05)
        assert tapper.taps, "a 5 % demand produced no keystroke at all"
        assert all(hold >= MIN_PULSE_S for _, hold in tapper.taps)
        assert tapper.held_time == pytest.approx(0.05 * PERIOD * 100, abs=PERIOD)

    def test_nothing_is_sent_inside_the_deadband(self):
        pulse, tapper = modulator()
        for _ in range(100):
            pulse.apply('b', DEADBAND_DUTY * 0.5)
        assert tapper.taps == []

    def test_no_pulse_is_longer_than_one_period(self):
        """A longer one would overlap the next cycle's decision."""
        pulse, tapper = modulator()
        for duty in (0.1, 0.9 * CONTINUOUS_DUTY, 0.3, 0.8, 0.05):
            for _ in range(30):
                pulse.apply('b', duty)
        assert all(hold <= PERIOD + 1e-9 for _, hold in tapper.taps)


class TestContinuous:
    def test_a_full_demand_holds_the_key_rather_than_pulsing_it(self):
        pulse, tapper = modulator()
        for _ in range(5):
            pulse.apply('b', 1.0)
        assert tapper.taps == [('b', PERIOD * HOLD_PERIODS)] * 5

    def test_dropping_to_nothing_ends_the_hold_at_once(self):
        """Two more periods of throttle is not a handback."""
        pulse, tapper = modulator()
        pulse.apply('b', 1.0)
        tapper.taps.clear()
        pulse.apply('b', 0.0)
        assert tapper.taps == [('b', 0.0)]

    def test_the_hold_is_only_ended_once(self):
        pulse, tapper = modulator()
        pulse.apply('b', 1.0)
        pulse.apply('b', 0.0)
        tapper.taps.clear()
        pulse.apply('b', 0.0)
        assert tapper.taps == []

    def test_releasing_ends_the_hold(self):
        pulse, tapper = modulator()
        pulse.apply('b', 1.0)
        tapper.taps.clear()
        pulse.release('b')
        assert tapper.taps == [('b', 0.0)]

    def test_releasing_when_nothing_is_held_sends_nothing(self):
        """A stray press-then-release blip would be a pedal nobody asked for."""
        pulse, tapper = modulator()
        pulse.apply('b', 0.2)
        tapper.taps.clear()
        pulse.release('b')
        assert tapper.taps == []


class TestBudget:
    def test_the_carried_remainder_is_bounded(self):
        """Or a long quiet stretch would bank a pedal and spend it at once."""
        pulse, _ = modulator()
        for _ in range(500):
            pulse.apply('b', 0.03)
        assert pulse.budget_s <= PERIOD + 1e-9

    def test_the_budget_is_dropped_when_the_demand_goes_away(self):
        pulse, _ = modulator()
        for _ in range(3):
            pulse.apply('b', 0.03)
        assert pulse.budget_s > 0.0
        pulse.apply('b', 0.0)
        assert pulse.budget_s == 0.0


class TestRefusal:
    def test_no_key_is_a_refusal_rather_than_a_crash(self):
        pulse, tapper = modulator()
        assert pulse.apply(None, 0.5) is False
        assert tapper.taps == []

    def test_a_tapper_that_cannot_inject_is_reported(self):
        pulse, _ = modulator(works=False)
        assert pulse.apply('b', 1.0) is False


class TestPeriod:
    def test_the_duty_follows_a_changed_control_period(self):
        """``assistance_refresh_rate`` is adjustable; the duty must track it."""
        pulse, tapper = modulator()
        pulse.period_s = 0.2
        for _ in range(50):
            pulse.apply('b', 0.5)
        assert tapper.held_time == pytest.approx(0.5 * 0.2 * 50, abs=0.2)
