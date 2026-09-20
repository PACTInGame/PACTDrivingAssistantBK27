"""Asking for half a pedal when the pedal is a key.

A mouse/keyboard driver's throttle and brake are keys. A key has two
positions, and a control loop that wants 30 % of one has no way to say so --
so it says 100 %, for a whole control cycle, and then has to undo it. That is
not a tuning problem, it is a resolution problem, and this module is the
resolution.

### What goes wrong without it

The parking manoeuvre runs at 1.1 m/s. LFS has auto-clutch and the car
**creeps in gear with no throttle at all**, which at that speed is most of
the demand already. A full 100 ms of throttle on top of the creep is far more
than the loop asked for, the car overshoots, the only answer available is a
full brake, and the next cycle is under the demand again. A live run recorded
exactly that: ``thr 1.00`` on nearly every line, ``brk 0.50`` on the rest, and
the speed sawing between 0.3 and 2.3 m/s against a 1.1 m/s demand. The car
parked, jerkily and crooked.

### Duty cycle, with the remainder carried

A key held for 30 ms out of every 100 ms is 30 % of a pedal, averaged over a
period the car's inertia is far slower than. So a demand of 0.3 becomes a
30 ms tap, once per cycle, on :mod:`misc.key_tap`'s own thread -- never on the
assistance thread, which is the rule that module exists for.

The complication is small demands. LFS samples input per physics tick, so a
tap shorter than :data:`MIN_PULSE_S` is a keystroke that may simply never be
seen; rounding those down to nothing would make every demand under about 15 %
silently zero, and the loop would wind up its integral against a pedal that
does not exist. Rounding them *up* to the minimum would do the opposite and
give 15 % whenever 1 % was asked for.

So the remainder is carried instead. Each call adds ``duty * period`` to a
budget and spends it only once there is enough for a pulse LFS can see. A
demand of 0.05 at a 100 ms cycle therefore produces one 40 ms pulse every
eight cycles -- 5 % on average, which is what was asked for, delivered in
lumps the game can actually read. It is a sigma-delta modulator, and the one
property that matters is that the *average* is right with no steady-state
error.

### Reusable on purpose

Nothing here knows about parking, pedals or LFS. It converts a continuous
0..1 demand into timed presses of one named key, which is what every digital
actuator in this project needs -- the handbrake, a horn, an indicator stalk
held for a while. The key tapper is injected, so the whole thing tests
without Windows.
"""

import logging
from typing import Optional

logger = logging.getLogger(__name__)

# The shortest press worth sending. LFS reads the keyboard once per physics
# tick (10 ms) and once per rendered frame; 40 ms is four ticks and at least
# two frames at 50 fps, which is the shortest press that is reliably seen
# rather than sometimes seen. Anything smaller is not a gentler pedal, it is a
# pedal that intermittently does nothing.
MIN_PULSE_S = 0.04
# Above this the key simply stays down. Splitting a 95 % duty into a press and
# a 5 ms gap would make LFS see a key *repeat*, and a released-and-repressed
# throttle is worse than a held one.
CONTINUOUS_DUTY = 0.90
# And below this nothing is sent at all, budget included: a demand this small
# is the loop idling, not asking for anything.
DEADBAND_DUTY = 0.02
# How long a continuous hold is armed for, in periods. Longer than one, so
# that consecutive cycles overlap and the key never blips up between them;
# short enough that a caller which stops calling -- a crashed worker, an
# aborted manoeuvre -- leaves the key down for a fifth of a second and no
# more.
HOLD_PERIODS = 2.0
# The budget may never grow past one period's worth. Without the cap a long
# stretch of small demands would bank enough time to hold the key down
# continuously once the demand finally rose -- an integrator nobody asked for.
MAX_BUDGET_PERIODS = 1.0


class PulseModulator:
    """One digital output, driven by a continuous 0..1 demand.

    *tapper* is a :class:`misc.key_tap.KeyTapper`; *period* is how often
    :meth:`apply` is called, i.e. the control cycle it is modulating over.
    """

    def __init__(self, tapper, period_s: float, name: str = 'pedal',
                 min_pulse_s: float = MIN_PULSE_S):
        self.tapper = tapper
        self.period_s = max(1e-3, float(period_s))
        self.name = name
        self.min_pulse_s = max(0.0, float(min_pulse_s))
        self._budget = 0.0
        self._holding = False

    # ─── One cycle ────────────────────────────────────────────────────

    def apply(self, key: Optional[str], duty: float) -> bool:
        """Command *duty* (0..1) on *key* for one period.

        Returns whether the output is in a sane state -- ``False`` only when
        a keystroke that should have gone out could not, which the caller
        treats exactly like any other actuator refusing.

        Costs a handful of float operations plus, at most, one enqueue on the
        tapper. It never sleeps and never touches pyautogui.
        """
        if key is None:
            self.reset()
            return False
        duty = 0.0 if duty <= 0.0 else (1.0 if duty >= 1.0 else float(duty))

        if duty >= CONTINUOUS_DUTY:
            # Held rather than pulsed, by re-arming the hold every cycle. A
            # later tap of the same key owns it and invalidates the earlier
            # release (``misc/key_tap.py``), so the key never comes up between
            # two of these -- and if this stops being called, the key is
            # released by itself within two periods rather than sticking.
            self._budget = 0.0
            self._holding = True
            return self.tapper.tap(key, hold_s=self.period_s * HOLD_PERIODS)

        if duty <= DEADBAND_DUTY:
            # Idling, not asking for anything.
            self._budget = 0.0
            return self._end_hold(key)

        self._budget = min(self._budget + duty * self.period_s,
                           self.period_s * MAX_BUDGET_PERIODS)
        if self._budget < self.min_pulse_s:
            # Not enough banked for a press the game would see. Carry it, and
            # make sure nothing is still held from a previous continuous one.
            return self._end_hold(key)
        hold = min(self._budget, self.period_s)
        self._budget -= hold
        # This tap supersedes any outstanding hold, so it ends one too.
        self._holding = False
        return self.tapper.tap(key, hold_s=hold)

    # ─── Lifecycle ────────────────────────────────────────────────────

    def _end_hold(self, key: str) -> bool:
        """Cut a continuous hold short, if one is outstanding.

        Without this a demand that falls from "held" to nothing would leave
        the key down until the last hold expired -- two whole control periods
        of throttle nobody asked for. A zero-length tap is the way to say it:
        the key is already down, so the tapper skips the press and executes
        only the release, which it still withholds if the driver is holding
        that key on the hardware.
        """
        if not self._holding:
            return True
        self._holding = False
        return self.tapper.tap(key, hold_s=0.0)

    def release(self, key: Optional[str] = None):
        """Stop driving this output and forget the carried remainder.

        Always allowed, from any state and any thread: it can only ever take
        back a press of ours.
        """
        if key is not None:
            self._end_hold(key)
        self._budget = 0.0
        self._holding = False

    def reset(self):
        """Forget the carried remainder without touching the key.

        For a caller that has no key to name -- a rebind, a torn-down
        manoeuvre. Anything still held expires within two periods by itself.
        """
        self._budget = 0.0
        self._holding = False

    @property
    def budget_s(self) -> float:
        """The carried remainder, in seconds. Tests and diagnostics only."""
        return self._budget
