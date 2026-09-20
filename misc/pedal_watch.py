"""The driver's *physical* pedals, read from the device instead of from LFS.

Why this exists at all
======================

While :class:`Controls.brake_axis.AxisBrakeOutput` holds LFS's brake axis, LFS
does not read the driver's brake pedal any more, and ``OutGaugePack.Brake``
reports **our own** command back to us. For the whole duration of an
intervention there is therefore no way, through LFS, to answer the one question
the arbitration rule depends on (``reference/control-intervention.md`` §1):

    *is the driver braking harder than we are?*

Without an answer the analog path can silently **reduce** braking -- the driver
stands on the pedal, we command 50 %, and LFS obeys us. That is the exact
inversion of what an assistant may do, and it is what this module exists to
prevent. So the pedals are read where LFS cannot take them away: from the
joystick itself, over SDL/DirectInput.

Which axis is the brake is never asked
======================================

The user does not type an axis number here, and nothing is probed against LFS.
While LFS *is* reading the driver's pedals -- that is, whenever we are not
intervening -- ``OutGauge`` publishes exactly the pedal positions the driver is
producing. So every axis of every attached device is correlated against them:

* the brake axis is the one whose value tracks ``OutGaugePack.Brake``,
* the throttle axis is the one that tracks ``OutGaugePack.Throttle``.

One clean braking manoeuvre is enough. The fit also yields the two raw
endpoints, so the axis is *calibrated*, not merely identified -- polarity,
range and LFS's own dead zones all fall out of it, and a pedal wired backwards
needs no special case. This is the same "measure it rather than ask" approach
``vehicles/car_profiles.py`` uses for the gearbox.

Everything is refused rather than guessed. ``driver_brake()`` returns ``None``
until an axis has actually been identified, and the caller has to decide what
to do without it -- see :meth:`Controls.brake_axis.AxisBrakeOutput.apply`,
which then commands full braking because anything less might be less than the
driver.

Threading: SDL only works on the main thread
============================================

**Measured on Windows 11 / SDL 2.28.4, and it decides the whole shape of this
module.** Initialising SDL's joystick subsystem *and* pumping it from a worker
thread is accepted without an error and then reports a flat ``0.0`` on every
axis of every device, forever. The identical code on the main thread reports
real values. There is no exception, no log line and no failure mode to detect:
``get_axis`` simply returns a plausible number that never changes.

A second, independent trap sits next to it: SDL ignores joystick input entirely
while its process is not the foreground window, unless
``SDL_JOYSTICK_ALLOW_BACKGROUND_EVENTS`` is set before the subsystem starts.
The driver is always looking at LFS, so without that hint the readings are the
same flat zeros.

So this class does not own a thread. :meth:`pump` is called from the main loop
in ``main.py`` -- the thread that is otherwise inside ``asyncore`` -- and does
nothing but ``pygame.event.pump()`` plus one ``get_axis`` per axis, publishing
the result as a single immutable tuple. Two other threads only *read* that
tuple:

* the assistance pass calls :meth:`observe` once per cycle with the own vehicle;
  it reads the tuple (one attribute read) and, while a fit is still outstanding,
  appends one sample;
* an intervention calls :meth:`driver_brake` while it is engaged.

The fit is the only expensive part, and it runs at most every
``FIT_INTERVAL_S`` -- never in ``process()`` and never per pump.
"""

import logging
import os
import threading
import time
from collections import deque
from typing import Dict, List, Optional, Sequence, Tuple

from misc.platform_shim import get_joystick, is_available

logger = logging.getLogger(__name__)

# The fit walks every axis over the whole sample window, so it is the one part
# of this module that costs real time (a few milliseconds). It runs on the main
# loop, so it is rate-limited rather than run whenever new samples arrive.
FIT_INTERVAL_S = 1.0

# ─── Learning ────────────────────────────────────────────────────────────────
# Samples are taken at the assistance rate (10 Hz by default), so 120 of them
# are about twelve seconds of driving.
SAMPLE_LIMIT = 300
MIN_SAMPLES = 40
# The pedal has to have been *used* in the window: correlating against a signal
# that never moved would identify noise.
MIN_REFERENCE_SPAN = 0.5
# Only the linear middle is fitted. Below and above, LFS's own dead zones make
# the axis move while the reported pedal value does not, which would drag the
# regression towards a shallower slope and put the endpoints in the wrong place.
FIT_LOW = 0.02
FIT_HIGH = 0.98
# How well the winner has to track the reference, and how clearly it has to
# beat everything else. A wheel reports a dozen axes and several of them move
# together with the pedals (load cell, clutch, a combined axis), so "best" is
# not enough on its own.
MIN_CORRELATION = 0.97
CORRELATION_MARGIN = 0.015
# The axis must actually travel. Without this a nearly constant axis whose
# noise happens to line up would win on correlation alone.
MIN_AXIS_SPAN = 0.15
# How often an unidentified pedal may explain itself. Long, because it is a
# standing condition and not an event: a driver who has not braked yet should
# not be nagged, and a driver whose wheel genuinely cannot be resolved needs to
# read it once, not forty times.
REFUSAL_LOG_INTERVAL_S = 30.0

# A calibrated axis has to span something; below this the two endpoints are
# indistinguishable and the mapping would explode.
MIN_ENDPOINT_SPAN = 0.05

# ─── Keeping an identified pedal honest ──────────────────────────────────────
# A fit that was right once can stop being right: the driver recalibrates the
# pedals in the wheel software, changes LFS's own axis calibration, swaps a load
# cell for a potentiometer, or plugs the devices back in a different order. None
# of that announces itself, and the value would just quietly drift -- in an
# arbitration that decides how hard the car brakes.
#
# So the fit keeps being checked against the reference it was derived from,
# whenever LFS is reading the pedal normally. The tolerance is deliberately
# wide and the run deliberately long, because a *transient* disagreement is
# expected and harmless: the axes are sampled by the main loop and the pedal
# value by OutGauge, up to ~100 ms apart, so a fast stab at the pedal shows a
# big instantaneous error through no fault of the calibration. Only a
# disagreement that survives three seconds of driving is a broken fit.
FIT_ERROR_TOLERANCE = 0.25
FIT_ERROR_CYCLES = 30

# ─── Confidence ──────────────────────────────────────────────────────────────
# The same agreement, counted upwards instead of downwards. It answers a
# different question from the one above: not "has the calibration gone stale"
# but "is LFS demonstrably reading the pedal I think it is, right now" -- which
# is what has to be true before the throttle cut may take that input away and
# give it back (``Controls/throttle_cut.py``).
#
# 150 samples is 15 s at the default assistance rate. The span requirement is
# what stops a driver coasting down a straight from confirming anything: a
# pedal that never moved agrees with a prediction of "not pressed" no matter
# which axis it was fitted to.
CONFIRM_SAMPLES = 150
CONFIRM_SPAN = 0.5

# vJoy is *our* device. It never carries a driver pedal, and during an
# intervention it carries our own brake command -- which correlates perfectly
# with what LFS reports. Excluded by name rather than by hoping the learning
# window never overlaps an intervention.
_EXCLUDED_DEVICES = ('vjoy',)

# Measured, and the whole feature depends on it: **SDL ignores joystick input
# while its process is not the foreground window** unless this hint is set.
# Without it every axis of every device reads a flat 0.0 for as long as LFS has
# focus -- which is always, because that is where the driver is -- and nothing
# says so: ``get_axis`` returns a perfectly plausible number, the correlation
# never finds a pedal, and the arbitration silently falls back to full braking
# forever. Set before the joystick subsystem is initialised, because SDL reads
# its hints once, at init.
_BACKGROUND_EVENTS_HINT = 'SDL_JOYSTICK_ALLOW_BACKGROUND_EVENTS'


class PedalFit:
    """One identified pedal axis: where it lives and what its ends mean."""

    __slots__ = ('device_index', 'device_name', 'axis', 'raw_zero', 'raw_full')

    def __init__(self, device_index: int, device_name: str, axis: int,
                 raw_zero: float, raw_full: float):
        self.device_index = device_index
        self.device_name = device_name
        self.axis = axis
        self.raw_zero = raw_zero
        self.raw_full = raw_full

    @property
    def valid(self) -> bool:
        return abs(self.raw_full - self.raw_zero) >= MIN_ENDPOINT_SPAN

    def value_for(self, raw: float) -> float:
        """Map a raw axis reading to 0..1, the way LFS reports the pedal."""
        span = self.raw_full - self.raw_zero
        return max(0.0, min(1.0, (raw - self.raw_zero) / span))

    def __repr__(self) -> str:
        return (f"<PedalFit {self.device_name!r} axis {self.axis}: "
                f"{self.raw_zero:+.3f} -> {self.raw_full:+.3f}>")


def _correlate(xs: Sequence[float], ys: Sequence[float]) -> float:
    """Pearson correlation, or 0.0 when either side never moves."""
    n = len(xs)
    if n < 2:
        return 0.0
    mean_x = sum(xs) / n
    mean_y = sum(ys) / n
    sxy = sxx = syy = 0.0
    for x, y in zip(xs, ys):
        dx = x - mean_x
        dy = y - mean_y
        sxy += dx * dy
        sxx += dx * dx
        syy += dy * dy
    if sxx <= 0.0 or syy <= 0.0:
        return 0.0
    return sxy / (sxx * syy) ** 0.5


def _endpoints(raws: Sequence[float],
               refs: Sequence[float]) -> Optional[Tuple[float, float]]:
    """Raw axis values that correspond to pedal 0 and pedal 1.

    A least-squares fit of ``ref = m * raw + c`` inverted at the two ends, not
    the smallest and largest raw values seen: the driver rarely reaches either
    stop, and the endpoints have to be the *pedal's*, not the manoeuvre's.
    """
    n = len(raws)
    mean_r = sum(raws) / n
    mean_f = sum(refs) / n
    srf = srr = 0.0
    for raw, ref in zip(raws, refs):
        dr = raw - mean_r
        srf += dr * (ref - mean_f)
        srr += dr * dr
    if srr <= 0.0:
        return None
    slope = srf / srr
    if abs(slope) < 1e-6:
        return None
    intercept = mean_f - slope * mean_r
    return (-intercept / slope, (1.0 - intercept) / slope)


class PedalLearner:
    """Collects samples for one pedal and identifies its axis.

    Kept separate from :class:`PedalWatch` so the whole identification can be
    tested by feeding it numbers, with no joystick and no LFS.
    """

    def __init__(self, name: str):
        self.name = name
        self.samples: deque = deque(maxlen=SAMPLE_LIMIT)
        # Why the last attempt gave nothing, as a ready-made sentence. Every
        # gate below used to fail in silence, so a pedal that never calibrated
        # gave the driver nothing to act on -- and the one thing they are told
        # to do is the thing that helps least (see ``_usable``).
        self.refusal: Optional[str] = None

    def add(self, axes: Tuple[float, ...], reference: float):
        self.samples.append((axes, reference))

    def reset(self):
        self.samples.clear()
        self.refusal = None

    def attempt(self, labels: Dict[int, Tuple[int, str, int]]) -> Optional[PedalFit]:
        """Try to identify the axis. ``None`` while the evidence is not there.

        *labels* maps a flat axis index to ``(device_index, device_name,
        axis_within_device)`` -- the poller owns that mapping, this class only
        needs it to describe the winner.

        Every ``None`` also sets :attr:`refusal` to a sentence naming the gate
        that stopped it. ``PedalWatch`` prints that, rate-limited, because the
        alternative is what actually happened on 2026-09-20: a driver braked
        hard four times, the brake did not calibrate, and nothing anywhere said
        why.
        """
        usable = [(axes, ref) for axes, ref in self.samples
                  if FIT_LOW < ref < FIT_HIGH]
        if len(usable) < MIN_SAMPLES:
            # Measured, and it is the opposite of what anyone expects: a full
            # application is nearly worthless here. Only the linear middle
            # counts (FIT_LOW..FIT_HIGH), and a pedal held at 1.00 for three
            # seconds contributes about 4 usable samples out of 36, because
            # everything in the hold is saturated. Braking to 0.85 instead
            # contributes 32 of 33. So "brake hard a few times" needs ten
            # applications and "slow down smoothly a few times" needs one or
            # two -- which is why the wording below says the latter.
            self.refusal = (
                f"only {len(usable)} of {len(self.samples)} samples are in the "
                f"usable range {FIT_LOW:.2f}-{FIT_HIGH:.2f} (need "
                f"{MIN_SAMPLES}); a fully pressed pedal is saturated and "
                f"teaches nothing - press it part way and release it slowly")
            return None
        refs = [ref for _axes, ref in usable]
        if max(refs) - min(refs) < MIN_REFERENCE_SPAN:
            self.refusal = (
                f"the pedal only moved over {max(refs) - min(refs):.2f} of its "
                f"travel (need {MIN_REFERENCE_SPAN:.2f})")
            return None

        width = min(len(axes) for axes, _ref in usable)
        best_index, best_score, runner_up = -1, 0.0, 0.0
        for index in range(width):
            if index not in labels:
                continue
            column = [axes[index] for axes, _ref in usable]
            if max(column) - min(column) < MIN_AXIS_SPAN:
                continue
            score = abs(_correlate(column, refs))
            if score > best_score:
                best_index, runner_up, best_score = index, best_score, score
            elif score > runner_up:
                runner_up = score

        if best_index < 0:
            self.refusal = (
                f"no axis moved more than {MIN_AXIS_SPAN:.2f} while the pedal "
                f"did")
            return None
        if best_score < MIN_CORRELATION:
            self.refusal = (
                f"the best axis ({best_index}) tracks the pedal at only "
                f"{best_score:.3f} (need {MIN_CORRELATION:.2f})")
            return None
        if best_score - runner_up < CORRELATION_MARGIN:
            # A wheel with a combined axis, a load cell or a clutch that moves
            # with the brake lands here. It is not a fault, but it never clears
            # by itself either, so it is said out loud rather than at DEBUG.
            self.refusal = (
                f"axis {best_index} and another one track the pedal equally "
                f"well ({best_score:.3f} vs {runner_up:.3f}) - a manoeuvre "
                f"that moves only this pedal would separate them")
            return None

        column = [axes[best_index] for axes, _ref in usable]
        ends = _endpoints(column, refs)
        if ends is None:
            self.refusal = (f"axis {best_index} tracks the pedal but its "
                            f"travel could not be fitted")
            return None
        device_index, device_name, axis = labels[best_index]
        fit = PedalFit(device_index, device_name, axis, ends[0], ends[1])
        if not fit.valid:
            self.refusal = (f"axis {best_index} produced a degenerate "
                            f"calibration ({fit!r})")
            return None
        self.refusal = None
        return fit


class PedalWatch:
    """Reads the driver's own pedals, and learns where they are.

    ``event_bus`` is used for one notification when a pedal is identified;
    everything else is pull-based, because the callers need the value *now*,
    inside a decision, not whenever an event happens to arrive.
    """

    SETTING_KEYS = {
        'brake': ('pedal_brake_device', 'pedal_brake_device_index',
                  'pedal_brake_axis', 'pedal_brake_raw_zero',
                  'pedal_brake_raw_full'),
        'throttle': ('pedal_throttle_device', 'pedal_throttle_device_index',
                     'pedal_throttle_axis', 'pedal_throttle_raw_zero',
                     'pedal_throttle_raw_full'),
    }

    def __init__(self, event_bus, settings, clock=None):
        self.event_bus = event_bus
        self.settings = settings
        self.clock = clock or time.monotonic

        self._joysticks: list = []
        self._started = False
        self._failed = False
        self._wanted = False
        self._warming = False
        self._next_fit_at = 0.0
        # Erster gelesener Achsensatz, und ob sich seither je etwas bewegt hat.
        # Siehe _note_movement: der Ausfallmodus dieses Moduls ist stumm.
        self._first_axes: Optional[Tuple[float, ...]] = None
        self._movement_seen = False
        # Aufeinanderfolgende Zyklen, in denen ein Fit der Referenz
        # widersprochen hat - siehe FIT_ERROR_TOLERANCE.
        self._fit_errors: Dict[str, int] = {'brake': 0, 'throttle': 0}
        # Aufeinanderfolgende Zyklen, in denen der Fit gestimmt hat, und wie
        # weit das Pedal dabei ueberhaupt bewegt wurde - siehe confidence().
        self._agreeing: Dict[str, int] = {'brake': 0, 'throttle': 0}
        self._seen_low: Dict[str, float] = {}
        self._seen_high: Dict[str, float] = {}
        # Flat axis indices that have been seen at something other than exactly
        # 0.0 -- see ``_check_fit``. A set rather than a flag per pedal because
        # the two pedals can sit on different devices, which come up at
        # different times.
        self._axes_alive: set = set()
        # When each pedal last explained why it is still unidentified.
        self._refusal_logged_at: Dict[str, float] = {}
        self._last_refusal: Dict[str, str] = {}

        # Published by pump(), read by everyone else. Rebound as a whole
        # tuple, never mutated, so a reader can never see half an update.
        self._axes: Tuple[float, ...] = ()
        self._labels: Dict[int, Tuple[int, str, int]] = {}
        self._devices_ready = False
        self._reason: Optional[str] = None
        # Opening the devices is spread over several pumps -- see ``start``.
        # ``None`` means "not begun"; otherwise it is the next device index to
        # open, and ``_opening`` collects the flat axis list as it grows.
        self._open_index: Optional[int] = None
        self._open_count = 0
        self._opening: List[Tuple[object, int]] = []
        self._opening_labels: Dict[int, Tuple[int, str, int]] = {}

        self._fits: Dict[str, Optional[PedalFit]] = {'brake': None,
                                                     'throttle': None}
        self._learners = {'brake': PedalLearner('brake'),
                          'throttle': PedalLearner('throttle')}
        # Set by observe(), consumed by pump(): a fit is worth attempting only
        # when new samples have arrived since the last one.
        self._pending_fit = False

        self._load_saved_fits()
        self.event_bus.subscribe('pedals_calibrate', self._on_recalibrate)

    # ─── Lifecycle ────────────────────────────────────────────────────

    def request_start(self):
        """Ask for the pedals to be read. Callable from any thread.

        Split from :meth:`start` because of the two constraints that do not fit
        together: SDL has to be initialised on the main thread, and importing
        pygame costs ~300 ms -- three whole assistance cycles, and a main loop
        that is not answering LFS for that long drops packets. So the import is
        warmed on a thread of its own here, and the cheap SDL init happens in
        the next :meth:`pump`.

        Called from the assistance pass the moment the axis path arms, so a
        driver who never uses automatic braking never pays for pygame at all.
        """
        if self._wanted:
            return
        self._wanted = True
        if self._warming:
            return
        self._warming = True
        threading.Thread(target=self._warm_import, name='pedal-warm',
                         daemon=True).start()

    def _warm_import(self):
        """Import pygame off the main loop. Nothing else -- SDL init is not
        thread-safe here (see the module docstring)."""
        try:
            get_joystick()
        except Exception as exc:      # pragma: no cover - defensive
            logger.warning("Importing pygame failed: %s: %s",
                           type(exc).__name__, exc)
        finally:
            self._warming = False

    def start(self) -> bool:
        """Open the devices, **one step per call**. Main thread only.

        Measured on this machine (Windows 11, SDL 2.28.4, two FANATEC devices
        plus vJoy), and it is why this is a step machine rather than one call::

            pygame.display.init()        2.3 ms
            pygame.joystick.init()     168.1 ms
            Joystick(0)                106.2 ms
            Joystick(1)                257.9 ms
            Joystick(2)                 54.2 ms

        **Every one of those holds the GIL for its whole duration** -- a ticker
        thread sleeping 1 ms saw stalls exactly as long. So opening everything
        in one pump did two things at once: it kept the main loop out of
        ``asyncore`` for ~590 ms, long enough to drop InSim packets, and it
        stalled the 100 ms assistance thread for up to 258 ms. That is where
        "gearbox 553.5 ms" and "aeb 362.9 ms" came from -- neither system was
        doing any work, both were waiting for the GIL (known-issues #56).

        A thread does not help: the GIL follows. Spreading does -- the worst
        single block drops from ~590 ms to one device, and the main loop
        answers LFS in between. ``is_running`` stays False until every device
        is open, so nothing reads a half-built axis list.

        Returns True once the devices are ready. A failure is remembered as a
        failure: retrying an SDL init that has already gone wrong once only
        produces more log lines.
        """
        if self._started or self._failed:
            return self._started
        if not is_available('pygame'):
            self._failed = True
            self._reason = 'pygame_missing'
            logger.warning("pygame is not available - the driver's own pedals "
                           "cannot be read, so an intervention has to assume "
                           "the worst and brake fully.")
            return False
        try:
            done = self._open_step()
        except Exception as exc:
            self._failed = True
            self._reason = 'joystick_open_failed'
            logger.error("Opening the joysticks failed: %s: %s",
                         type(exc).__name__, exc)
            return False
        if not done:
            return False
        if not self._joysticks:
            self._failed = True
            self._reason = 'no_joystick_found'
            logger.info("No joystick with axes found - a wheel driver's own "
                        "pedals cannot be read.")
            return False
        self._started = True
        self._devices_ready = True
        return True

    def is_running(self) -> bool:
        return self._started

    def stop(self):
        self._started = False
        self._wanted = False
        self._devices_ready = False
        self._axes = ()
        self._joysticks = []
        # The flat indices mean nothing once the devices are closed.
        self._axes_alive = set()
        # A half-finished enumeration must not be resumed after a stop.
        self._open_index = None
        self._open_count = 0
        self._opening = []
        self._opening_labels = {}

    # ─── Reading ──────────────────────────────────────────────────────

    def driver_brake(self) -> Optional[float]:
        """The driver's brake pedal, 0..1, or ``None`` if it is not known."""
        return self._pedal('brake')

    def driver_throttle(self) -> Optional[float]:
        """The driver's throttle pedal, 0..1, or ``None`` if it is not known."""
        return self._pedal('throttle')

    def _pedal(self, which: str) -> Optional[float]:
        fit = self._fits.get(which)
        if fit is None:
            return None
        axes = self._axes
        index = self._flat_index(fit)
        if index is None or index >= len(axes):
            return None
        return fit.value_for(axes[index])

    def is_calibrated(self, which: str) -> bool:
        return self._fits.get(which) is not None

    def confidence(self, which: str) -> float:
        """How sure we are that LFS reads the pedal we identified, 0..1.

        Two factors, multiplied, because either one alone is worthless:

        * how long the identified axis and ``OutGauge`` have agreed without
          interruption -- one lucky sample proves nothing, and a disagreement
          resets it to zero;
        * how far the pedal was actually moved while they agreed -- agreeing at
          "not pressed" for a minute says nothing about which axis was fitted.

        Rises over roughly fifteen seconds of ordinary driving and needs no
        procedure from the driver, which is the whole point: an axis check that
        asks the user to hold a pedal on command is a step nobody should have
        to take.
        """
        if self._fits.get(which) is None:
            return 0.0
        low = self._seen_low.get(which)
        high = self._seen_high.get(which)
        if low is None or high is None:
            return 0.0
        span = min(1.0, (high - low) / CONFIRM_SPAN)
        return min(1.0, self._agreeing.get(which, 0) / CONFIRM_SAMPLES) * span

    def unavailable_reason(self) -> Optional[str]:
        """Why no pedal can be read at all, or ``None`` if one can."""
        if self._reason is not None:
            return self._reason
        if not self._devices_ready:
            return 'no_joystick_found'
        return None

    # ─── Feeding the learner ──────────────────────────────────────────

    def observe(self, own_vehicle, trustworthy: bool = True):
        """One sample, from the assistance pass.

        *trustworthy* is False whenever LFS's pedal readings are not the
        driver's -- during an intervention we are the ones writing the brake
        axis, so ``OutGaugePack.Brake`` is our own command coming back and
        learning from it would identify our own vJoy axis as the brake pedal.

        Cost: one attribute read, and one ``deque.append`` per pedal that is
        still unidentified. Nothing at all once both are known.
        """
        if not trustworthy or not self._devices_ready:
            return
        if not getattr(own_vehicle, 'is_local_driver', False):
            # OutGauge is describing somebody else's car (conventions.md §5).
            return
        axes = self._axes
        if not axes:
            return
        added = False
        for which, reference in (('brake', own_vehicle.brake),
                                 ('throttle', own_vehicle.throttle)):
            if self._fits[which] is not None:
                self._check_fit(which, float(reference))
                continue
            self._learners[which].add(axes, float(reference))
            added = True
        if added:
            self._pending_fit = True

    def _check_fit(self, which: str, reference: float):
        """Does the identified axis still predict what LFS reports?

        One subtraction and one counter per pedal per cycle. See
        ``FIT_ERROR_TOLERANCE`` for why a single disagreement means nothing and
        a persistent one means everything.

        **An axis that has never reported anything but exactly 0.0 is not
        evidence, in either direction.** Measured on 2026-09-20: three seconds
        after the devices were opened, a perfectly good calibration was thrown
        away with "axis says 0.50, LFS says 0.00". 0.50 was not a reading --
        it is what ``value_for(0.0)`` returns for a pedal whose endpoints sit
        at +0.91 and -0.91, and 0.52 is the same arithmetic for the throttle's
        +1.07 / -1.00. Both axes were simply still dead: SDL delivers per
        device, the pedals sat on the second one, and it came up later than the
        first. Exactly 30 cycles of that is ``FIT_ERROR_CYCLES``, so the
        calibration was discarded every single session and the driver had to
        brake it back in before the axis throttle cut could arm.

        ``_note_movement`` does not catch this: it compares the whole tuple, so
        the steering axis of a device that *is* live satisfies it while the
        pedals on another device are still zero.

        A physical pedal never rests at exactly 0.0 -- its rest position is one
        end of its travel -- so this costs nothing once the device is alive.
        """
        index = self._axis_index(which)
        if index is not None and index not in self._axes_alive:
            if self._axes[index] == 0.0:
                return
            self._axes_alive.add(index)
        predicted = self._pedal(which)
        if predicted is None:
            return
        if abs(predicted - reference) <= FIT_ERROR_TOLERANCE:
            self._fit_errors[which] = 0
            self._agreeing[which] = self._agreeing.get(which, 0) + 1
            self._seen_low[which] = min(self._seen_low.get(which, reference),
                                        reference)
            self._seen_high[which] = max(self._seen_high.get(which, reference),
                                         reference)
            return
        self._fit_errors[which] += 1
        # A disagreement is not proof that the fit is wrong, but it is proof
        # that we are not entitled to act on it yet.
        self._agreeing[which] = 0
        if self._fit_errors[which] < FIT_ERROR_CYCLES:
            return
        logger.warning("The %s pedal calibration no longer matches what LFS "
                       "reports (axis says %.2f, LFS says %.2f) - discarding "
                       "it and measuring again.", which, predicted, reference)
        self._forget(which)

    def _on_recalibrate(self, data=None):
        """Forget both pedals and learn them again (menu, or a device change)."""
        which = None
        if isinstance(data, dict):
            which = data.get('pedal')
        for name in (which,) if which else ('brake', 'throttle'):
            if name not in self._fits:
                continue
            self._forget(name)
        # Not "brake once": measured, one full stop contributes about four
        # usable samples out of forty, because a saturated pedal is discarded
        # (PedalLearner.attempt). Smooth, partial applications are what teach
        # it, and they do so in one or two goes.
        logger.info("Pedal calibration cleared - drive normally and slow down "
                    "smoothly a few times, without flooring the pedals.")

    # ─── The pump ─────────────────────────────────────────────────────

    def pump(self):
        """Sample every axis once. **Main thread only.**

        Called from the main loop next to ``asyncore``, so it must be fast and
        must never raise: an exception here would take the packet loop with it.

        A read that fails clears the published reading rather than leaving the
        last one in place. Stale is worse than absent -- an intervention would
        arbitrate against a pedal position from before the driver lifted, while
        "absent" makes it brake fully, which is the safe direction
        (``AGENTS.md`` §3).
        """
        if not self._started:
            # The first pump after the axis path armed is where SDL is
            # initialised: it is the first moment we are on the main thread and
            # know the pedals are actually wanted.
            if not self._wanted or self._failed or self._warming:
                return
            if not self.start():
                return
        pygame = get_joystick()
        try:
            pygame.event.pump()
            self._axes = tuple(joystick.get_axis(axis)
                               for joystick, axis in self._joysticks)
        except Exception as exc:
            self._axes = ()
            self._devices_ready = False
            self._started = False
            self._failed = True
            self._reason = 'joystick_read_failed'
            logger.error("Reading the joysticks failed, the driver's pedals "
                         "are no longer visible: %s: %s",
                         type(exc).__name__, exc)
            return
        self._note_movement()
        if not self._pending_fit:
            return
        now = self.clock()
        if now < self._next_fit_at:
            return
        self._next_fit_at = now + FIT_INTERVAL_S
        self._pending_fit = False
        try:
            self._try_fits()
        except Exception as exc:
            logger.error("Fitting the pedal axes failed: %s: %s",
                         type(exc).__name__, exc)

    def _note_movement(self):
        """Log once, the first time an axis actually moves.

        The failure mode of this module is silence: SDL on the wrong thread, or
        without the background-events hint, returns a perfectly plausible 0.0
        for every axis and never changes it. "20 axes are being watched" is
        therefore not evidence of anything. This line is, and it is the first
        thing to look for when a wheel driver reports that automatic braking
        always goes to full pedal.
        """
        if self._movement_seen or not self._axes:
            return
        if self._first_axes is None:
            self._first_axes = self._axes
            return
        if self._axes == self._first_axes:
            return
        self._movement_seen = True
        logger.info("The driver's input devices are being read (%d axes). "
                    "Brake and throttle are identified from ordinary driving; "
                    "partial pedal travel teaches more than full.",
                    len(self._axes))

    def _open_step(self) -> bool:
        """One step of the enumeration. True when there is nothing left to do.

        Step 0 is the SDL subsystem, every later step is exactly one device.
        ``pygame.display.init()`` rather than ``pygame.init()``: the event queue
        that ``pump()`` drives lives in SDL's *video* subsystem (without it
        ``pump()`` raises "video system not initialized"), but the rest of
        ``pygame.init()`` would also take the mixer, which
        :mod:`misc.audio_player` owns. No window is ever created.
        """
        pygame = get_joystick()
        if self._open_index is None:
            os.environ.setdefault(_BACKGROUND_EVENTS_HINT, '1')
            pygame.display.init()
            pygame.joystick.init()
            self._open_count = pygame.joystick.get_count()
            self._open_index = 0
            self._opening = []
            self._opening_labels = {}
            # "No device at all" still finishes here rather than costing
            # another pump.
            if self._open_count > 0:
                return False

        while self._open_index < self._open_count:
            device_index = self._open_index
            self._open_index += 1
            joystick = pygame.joystick.Joystick(device_index)
            joystick.init()
            name = joystick.get_name()
            if any(marker in name.lower() for marker in _EXCLUDED_DEVICES):
                logger.info("Skipping %r - that is our own virtual device.",
                            name)
            else:
                for axis in range(joystick.get_numaxes()):
                    self._opening_labels[len(self._opening)] = (device_index,
                                                                name, axis)
                    self._opening.append((joystick, axis))
            if self._open_index < self._open_count:
                # One device per call: the next one waits for the next pump.
                return False

        self._labels = self._opening_labels
        self._joysticks = self._opening
        self._opening = []
        self._opening_labels = {}
        if self._joysticks:
            logger.info("Watching %d axes on %d device(s) for the driver's "
                        "pedals.", len(self._joysticks),
                        len({label[0] for label in self._labels.values()}))
        return True

    def _try_fits(self):
        for which, learner in self._learners.items():
            if self._fits[which] is not None:
                continue
            fit = learner.attempt(self._labels)
            if fit is None:
                self._report_refusal(which, learner)
                continue
            self._fits[which] = fit
            self._fit_errors[which] = 0
            self._agreeing[which] = 0
            learner.reset()
            self._save_fit(which, fit)
            logger.info("Identified the %s pedal: %r", which, fit)
            self._refusal_logged_at.pop(which, None)
            self._last_refusal.pop(which, None)
            self.event_bus.emit('pedal_identified',
                                {'pedal': which,
                                 'device': fit.device_name,
                                 'axis': fit.axis})

    def _report_refusal(self, which: str, learner: 'PedalLearner'):
        """Say why a pedal is still unidentified. Once, then on a slow timer.

        The failure this closes was measured on 2026-09-20: four full-stop
        brakings produced no brake calibration, the throttle calibrated fine in
        the same window, and there was nothing in the log, the menu or the chat
        to tell the driver what to do differently. Every gate in
        ``PedalLearner.attempt`` returned a bare ``None``.

        A new reason is printed immediately -- it means something changed --
        and a repeated one at most every ``REFUSAL_LOG_INTERVAL_S``. Nothing is
        printed before the driver has touched the pedal at all, because "no
        samples yet" is not a problem.
        """
        reason = learner.refusal
        if reason is None or not learner.samples:
            return
        now = self.clock()
        last_at = self._refusal_logged_at.get(which)
        # Counts in the diagnostic change with every sample. They must not
        # bypass the throttle simply because the formatted text changed.
        if last_at is not None and now - last_at < REFUSAL_LOG_INTERVAL_S:
            return
        self._last_refusal[which] = reason
        self._refusal_logged_at[which] = now
        logger.info("The %s pedal is not identified yet: %s. "
                    "This is physical-pedal learning, not a failed emergency "
                    "brake; without a brake fit, axis interventions use full "
                    "braking to avoid undercutting the driver.", which, reason)

    def _axis_index(self, which: str) -> Optional[int]:
        """The flat index this pedal's fit currently resolves to, if any."""
        fit = self._fits.get(which)
        if fit is None:
            return None
        index = self._flat_index(fit)
        if index is None or index >= len(self._axes):
            return None
        return index

    def _flat_index(self, fit: PedalFit) -> Optional[int]:
        """Where this fit's axis sits in the current flat list.

        Resolved every read rather than stored: devices can be unplugged and
        the enumeration can change between sessions, and an index that silently
        points at a different axis is exactly the failure this module cannot
        afford.
        """
        for index, (device_index, name, axis) in self._labels.items():
            if axis == fit.axis and name == fit.device_name and \
                    device_index == fit.device_index:
                return index
        return None

    # ─── Persistence ──────────────────────────────────────────────────

    def _load_saved_fits(self):
        for which, keys in self.SETTING_KEYS.items():
            device_key, index_key, axis_key, zero_key, full_key = keys
            axis = self.settings.get(axis_key)
            if axis is None or axis < 0:
                continue
            fit = PedalFit(self.settings.get(index_key),
                           self.settings.get(device_key),
                           axis,
                           self.settings.get(zero_key),
                           self.settings.get(full_key))
            if not fit.valid:
                logger.warning("Stored %s pedal calibration is degenerate "
                               "(%r) - relearning it.", which, fit)
                continue
            self._fits[which] = fit
            logger.info("Loaded the %s pedal calibration: %r", which, fit)

    def _save_fit(self, which: str, fit: PedalFit):
        device_key, index_key, axis_key, zero_key, full_key = self.SETTING_KEYS[which]
        self.settings.set(device_key, fit.device_name)
        self.settings.set(index_key, fit.device_index)
        self.settings.set(axis_key, fit.axis)
        self.settings.set(zero_key, fit.raw_zero)
        self.settings.set(full_key, fit.raw_full)

    def _forget(self, which: str):
        """Drop everything known about one pedal, so it is measured again."""
        self._fits[which] = None
        self._fit_errors[which] = 0
        self._agreeing[which] = 0
        self._seen_low.pop(which, None)
        self._seen_high.pop(which, None)
        self._learners[which].reset()
        self.settings.set(self.SETTING_KEYS[which][2], -1)
