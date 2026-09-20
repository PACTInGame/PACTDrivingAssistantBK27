"""Identifying the driver's own pedals by correlating them against OutGauge.

The point of ``misc/pedal_watch.py`` is that nobody types an axis number: while
LFS is still reading the driver's pedals, ``OutGauge`` reports exactly what they
are pressing, and one axis out of the two dozen a wheel reports follows it. All
of that is arithmetic, so it is tested with numbers -- no joystick, no LFS, and
no thread.
"""

import logging

import pytest

from misc.pedal_watch import (CONFIRM_SAMPLES, FIT_ERROR_CYCLES, MIN_SAMPLES,
                              REFUSAL_LOG_INTERVAL_S, PedalFit, PedalLearner,
                              PedalWatch, _correlate, _endpoints)


# Three devices' worth of axes, of which exactly one is the pedal.
LABELS = {index: (0, 'FANATEC Wheel', index) for index in range(6)}


def samples(pedal_column, positions, noise=None):
    """Build (axes, reference) pairs where *pedal_column* carries the pedal.

    Every other axis gets a value that does not track the pedal: a constant, a
    slow ramp of its own, and a square wave. Those are the realistic
    distractors -- a wheel reports steering, clutch and two unused axes
    alongside the brake.
    """
    built = []
    for step, (raw, reference) in enumerate(positions):
        axes = [0.0] * 6
        axes[0] = -1.0                          # unused, parked at its stop
        axes[1] = 0.5                           # centred, never moves
        axes[2] = (step % 7) / 7.0 - 0.5        # something else entirely
        axes[3] = 1.0 if step % 2 else -1.0     # a switch-like axis
        axes[4] = 0.25
        axes[5] = 0.0
        axes[pedal_column] = raw
        if noise is not None:
            axes[pedal_column] += noise(step)
        built.append((tuple(axes), reference))
    return built


def linear_pedal(count=MIN_SAMPLES + 20, raw_zero=-1.0, raw_full=1.0):
    """A pedal swept from released to fully pressed and back."""
    positions = []
    for step in range(count):
        # A triangle wave, so the same pedal value is visited twice.
        phase = step / (count - 1)
        pressed = 2 * phase if phase <= 0.5 else 2 * (1 - phase)
        positions.append((raw_zero + (raw_full - raw_zero) * pressed, pressed))
    return positions


# ─── The arithmetic ──────────────────────────────────────────────────────────

def test_correlation_of_a_signal_with_itself_is_one():
    values = [0.1, 0.4, 0.2, 0.9, 0.5]
    assert _correlate(values, values) == pytest.approx(1.0)


def test_correlation_of_an_inverted_signal_is_minus_one():
    values = [0.1, 0.4, 0.2, 0.9, 0.5]
    assert _correlate(values, [-v for v in values]) == pytest.approx(-1.0)


def test_a_signal_that_never_moves_correlates_with_nothing():
    """Not a division by zero, and not a spurious 1.0."""
    assert _correlate([0.5] * 6, [0.1, 0.2, 0.3, 0.4, 0.5, 0.6]) == 0.0


def test_endpoints_extrapolate_past_the_travel_that_was_actually_used():
    """The driver rarely reaches either stop, so the ends have to be computed.

    Here the pedal was only ever pushed between 20 % and 60 %, on an axis that
    runs -1..+1. The endpoints must still come out as -1 and +1.
    """
    raws = [-1.0 + 2.0 * p for p in (0.2, 0.3, 0.4, 0.5, 0.6)]
    refs = [0.2, 0.3, 0.4, 0.5, 0.6]

    zero, full = _endpoints(raws, refs)

    assert zero == pytest.approx(-1.0)
    assert full == pytest.approx(1.0)


def test_endpoints_of_an_axis_that_never_moved_are_refused():
    assert _endpoints([0.5] * 5, [0.1, 0.2, 0.3, 0.4, 0.5]) is None


# ─── PedalFit ────────────────────────────────────────────────────────────────

def test_a_fit_maps_raw_readings_onto_the_pedal_range():
    fit = PedalFit(0, 'Wheel', 3, raw_zero=-1.0, raw_full=1.0)

    assert fit.value_for(-1.0) == pytest.approx(0.0)
    assert fit.value_for(0.0) == pytest.approx(0.5)
    assert fit.value_for(1.0) == pytest.approx(1.0)


def test_an_inverted_pedal_needs_no_special_case():
    """Raw +1 released, raw -1 pressed. Polarity falls out of the endpoints."""
    fit = PedalFit(0, 'Wheel', 3, raw_zero=1.0, raw_full=-1.0)

    assert fit.value_for(1.0) == pytest.approx(0.0)
    assert fit.value_for(-1.0) == pytest.approx(1.0)


def test_readings_beyond_the_measured_ends_are_clamped():
    fit = PedalFit(0, 'Wheel', 3, raw_zero=-1.0, raw_full=1.0)

    assert fit.value_for(-2.0) == 0.0
    assert fit.value_for(2.0) == 1.0


def test_a_degenerate_fit_is_not_valid():
    """Two endpoints that are the same value would make the mapping explode."""
    assert PedalFit(0, 'Wheel', 3, raw_zero=0.5, raw_full=0.5).valid is False


# ─── Identification ──────────────────────────────────────────────────────────

@pytest.mark.parametrize('column', [0, 2, 5])
def test_the_axis_that_tracks_the_pedal_is_the_one_identified(column):
    learner = PedalLearner('brake')
    for axes, reference in samples(column, linear_pedal()):
        learner.add(axes, reference)

    fit = learner.attempt(LABELS)

    assert fit is not None
    assert fit.axis == column
    assert fit.raw_zero == pytest.approx(-1.0, abs=0.02)
    assert fit.raw_full == pytest.approx(1.0, abs=0.02)


def test_an_inverted_axis_is_identified_and_calibrated():
    learner = PedalLearner('brake')
    for axes, reference in samples(4, linear_pedal(raw_zero=1.0, raw_full=-1.0)):
        learner.add(axes, reference)

    fit = learner.attempt(LABELS)

    assert fit is not None
    assert fit.raw_zero > fit.raw_full        # pressed is the *lower* raw value
    assert fit.value_for(-1.0) == pytest.approx(1.0, abs=0.02)


def test_a_pedal_that_was_barely_touched_identifies_nothing():
    """Correlating against a reference that hardly moved would find noise."""
    learner = PedalLearner('brake')
    barely = [(-1.0 + 0.2 * p, 0.1 * p) for p in
              [i / (MIN_SAMPLES + 19) for i in range(MIN_SAMPLES + 20)]]
    for axes, reference in samples(3, barely):
        learner.add(axes, reference)

    assert learner.attempt(LABELS) is None


def test_too_few_samples_identify_nothing():
    learner = PedalLearner('brake')
    for axes, reference in samples(3, linear_pedal(count=MIN_SAMPLES - 5)):
        learner.add(axes, reference)

    assert learner.attempt(LABELS) is None


def test_two_axes_that_move_together_are_refused_rather_than_guessed():
    """A pedal set can report the same travel on two axes (a load cell and its
    raw counterpart). Picking one at random would be worse than waiting."""
    learner = PedalLearner('brake')
    for axes, reference in samples(2, linear_pedal()):
        twinned = list(axes)
        twinned[5] = twinned[2]
        learner.add(tuple(twinned), reference)

    assert learner.attempt(LABELS) is None


def test_only_the_linear_middle_is_fitted():
    """LFS clips at both ends, so samples there would flatten the slope.

    The pedal here saturates: past 80 % of travel LFS keeps reporting 1.0. If
    those samples went into the fit, the computed full-brake endpoint would land
    well short of the real one.
    """
    learner = PedalLearner('brake')
    positions = []
    for step in range(MIN_SAMPLES + 40):
        phase = step / (MIN_SAMPLES + 39)
        raw = -1.0 + 2.0 * phase
        positions.append((raw, min(1.0, phase / 0.8)))
    for axes, reference in samples(1, positions):
        learner.add(axes, reference)

    fit = learner.attempt(LABELS)

    assert fit is not None
    assert fit.raw_full == pytest.approx(-1.0 + 2.0 * 0.8, abs=0.05)


# ─── The object as a whole ───────────────────────────────────────────────────

def test_an_unidentified_pedal_reads_as_unknown_not_as_zero(bus, settings):
    """The difference decides whether an intervention modulates or brakes fully."""
    watch = PedalWatch(bus, settings)

    assert watch.driver_brake() is None
    assert watch.driver_throttle() is None


def test_a_stored_calibration_is_used_again_next_session(bus, make_settings):
    settings = make_settings(pedal_brake_device='FANATEC Wheel',
                             pedal_brake_device_index=0,
                             pedal_brake_axis=2,
                             pedal_brake_raw_zero=-1.0,
                             pedal_brake_raw_full=1.0)
    watch = PedalWatch(bus, settings)
    watch._labels = LABELS
    watch._axes = (0.0, 0.0, 0.0, 0.0, 0.0, 0.0)

    assert watch.driver_brake() == pytest.approx(0.5)


def test_a_stored_calibration_with_identical_endpoints_is_discarded(
        bus, make_settings):
    """A half-written calibration must not produce a division by zero at
    120 km/h."""
    settings = make_settings(pedal_brake_device='Wheel',
                             pedal_brake_axis=2,
                             pedal_brake_raw_zero=0.5,
                             pedal_brake_raw_full=0.5)

    assert PedalWatch(bus, settings).driver_brake() is None


def test_a_calibration_for_a_device_that_is_gone_reads_as_unknown(
        bus, make_settings):
    """Unplugging the wheel must not silently point the fit at another axis."""
    settings = make_settings(pedal_brake_device='FANATEC Wheel',
                             pedal_brake_device_index=0,
                             pedal_brake_axis=2,
                             pedal_brake_raw_zero=-1.0,
                             pedal_brake_raw_full=1.0)
    watch = PedalWatch(bus, settings)
    watch._labels = {0: (0, 'Some Other Stick', 0)}
    watch._axes = (0.3,)

    assert watch.driver_brake() is None


def test_recalibrating_forgets_what_was_stored(bus, make_settings):
    settings = make_settings(pedal_brake_device='FANATEC Wheel',
                             pedal_brake_device_index=0,
                             pedal_brake_axis=2,
                             pedal_brake_raw_zero=-1.0,
                             pedal_brake_raw_full=1.0)
    watch = PedalWatch(bus, settings)
    watch._labels = LABELS
    watch._axes = (0.0,) * 6
    assert watch.driver_brake() is not None

    bus.emit('pedals_calibrate', {})

    assert watch.driver_brake() is None
    assert settings.get('pedal_brake_axis') == -1


def test_samples_are_not_taken_while_we_are_the_ones_writing_the_brake(
        bus, settings, make_own_vehicle):
    """During an intervention OutGauge reports our own command back, so a
    sample taken then would identify our vJoy axis as the driver's pedal."""
    watch = PedalWatch(bus, settings)
    watch._devices_ready = True
    watch._axes = (0.1, 0.2, 0.3)

    watch.observe(make_own_vehicle(local_plid=1, plid=1, brake=1.0),
                  trustworthy=False)

    assert len(watch._learners['brake'].samples) == 0


def test_samples_are_not_taken_from_somebody_elses_car(
        bus, settings, make_own_vehicle):
    """OutGauge follows the camera, not the driver (conventions.md §5)."""
    watch = PedalWatch(bus, settings)
    watch._devices_ready = True
    watch._axes = (0.1, 0.2, 0.3)

    watch.observe(make_own_vehicle(local_plid=1, plid=1, viewed_plid=2,
                                   brake=1.0))

    assert len(watch._learners['brake'].samples) == 0


def calibrated_watch(bus, make_settings):
    """A watch whose brake pedal is axis 2, running -1 (released) to +1."""
    settings = make_settings(pedal_brake_device='FANATEC Wheel',
                             pedal_brake_device_index=0,
                             pedal_brake_axis=2,
                             pedal_brake_raw_zero=-1.0,
                             pedal_brake_raw_full=1.0)
    watch = PedalWatch(bus, settings)
    watch._labels = LABELS
    watch._devices_ready = True
    return watch, settings


def test_a_fit_that_agrees_with_lfs_is_left_alone(bus, make_settings,
                                                  make_own_vehicle):
    watch, _settings = calibrated_watch(bus, make_settings)
    watch._axes = (0.0,) * 6                      # axis 2 at 0.0 -> pedal 0.5

    for _ in range(200):
        watch.observe(make_own_vehicle(local_plid=1, plid=1, brake=0.5))

    assert watch.driver_brake() == pytest.approx(0.5)


def test_a_brief_disagreement_does_not_throw_the_calibration_away(
        bus, make_settings, make_own_vehicle):
    """The axes and OutGauge are sampled up to ~100 ms apart, so a fast stab at
    the pedal disagrees instantly through no fault of the calibration."""
    watch, _settings = calibrated_watch(bus, make_settings)
    watch._axes = (0.0,) * 6

    for _ in range(5):
        watch.observe(make_own_vehicle(local_plid=1, plid=1, brake=1.0))

    assert watch.driver_brake() is not None


def test_a_calibration_that_keeps_disagreeing_is_measured_again(
        bus, make_settings, make_own_vehicle):
    """Somebody recalibrated the pedals, or the devices came back in a
    different order. Nothing announces that; the value would just drift.

    The axis reads +0.6 -- i.e. 0.8 on this fit -- while LFS reports a pedal
    that is not pressed. Deliberately not 0.0: that is the "SDL has not
    delivered anything for this device yet" signature and is ignored rather
    than counted against the fit (see the dead-axis tests below).
    """
    watch, settings = calibrated_watch(bus, make_settings)
    watch._axes = (0.0, 0.0, 0.6, 0.0, 0.0, 0.0)   # axis 2 -> pedal 0.8

    for _ in range(FIT_ERROR_CYCLES + 1):
        watch.observe(make_own_vehicle(local_plid=1, plid=1, brake=0.0))

    assert watch.driver_brake() is None
    assert settings.get('pedal_brake_axis') == -1


# ─── A device that has not delivered anything yet ────────────────────────────
#
# Measured live on 2026-09-20. Three seconds after the joysticks were opened,
# both stored calibrations were discarded with "axis says 0.50, LFS says 0.00"
# and "axis says 0.52, LFS says 0.05". Neither number was a reading: 0.4994 is
# what value_for(0.0) returns for a brake fitted at +0.9077/-0.9100, and 0.5177
# is the same arithmetic for a throttle at +1.0704/-0.9971. The axes were dead
# -- SDL delivers per device and the pedals sat on the one that came up second
# -- and 3 s is exactly FIT_ERROR_CYCLES. The driver had to re-brake the
# calibration in every session before the axis throttle cut could arm.


def test_an_axis_that_has_never_reported_anything_is_not_evidence(
        bus, make_settings, make_own_vehicle):
    watch, settings = calibrated_watch(bus, make_settings)
    watch._axes = (0.0,) * 6        # nothing delivered for this device yet

    for _ in range(FIT_ERROR_CYCLES * 3):
        watch.observe(make_own_vehicle(local_plid=1, plid=1, brake=0.0))

    assert settings.get('pedal_brake_axis') == 2      # kept, not discarded


def test_a_dead_axis_does_not_earn_confidence_either(
        bus, make_settings, make_own_vehicle):
    """The guard must not turn "no data" into "agrees with LFS" -- that would
    confirm an axis nobody has read and let the throttle cut arm on it."""
    watch, _settings = calibrated_watch(bus, make_settings)
    watch._axes = (0.0,) * 6

    for _ in range(CONFIRM_SAMPLES * 2):
        watch.observe(make_own_vehicle(local_plid=1, plid=1, brake=0.0))

    assert watch.confidence('brake') == 0.0


def test_once_the_axis_comes_alive_a_real_disagreement_still_counts(
        bus, make_settings, make_own_vehicle):
    """The guard is about "never delivered", not about the value 0.0. One live
    sample is enough to arm the check for good."""
    watch, settings = calibrated_watch(bus, make_settings)

    # The device starts delivering: the pedal is at rest, one end of its travel.
    watch._axes = (0.0, 0.0, -1.0, 0.0, 0.0, 0.0)
    watch.observe(make_own_vehicle(local_plid=1, plid=1, brake=0.0))

    # Now it reads mid-travel while LFS says nothing is pressed.
    watch._axes = (0.0,) * 6
    for _ in range(FIT_ERROR_CYCLES + 1):
        watch.observe(make_own_vehicle(local_plid=1, plid=1, brake=0.0))

    assert settings.get('pedal_brake_axis') == -1


def test_a_normal_pass_feeds_both_learners(bus, settings, make_own_vehicle):
    watch = PedalWatch(bus, settings)
    watch._devices_ready = True
    watch._axes = (0.1, 0.2, 0.3)

    watch.observe(make_own_vehicle(local_plid=1, plid=1,
                                   brake=0.4, throttle=0.6))

    assert watch._learners['brake'].samples[-1] == ((0.1, 0.2, 0.3), 0.4)
    assert watch._learners['throttle'].samples[-1] == ((0.1, 0.2, 0.3), 0.6)


# ─── known-issues #56: opening the devices is spread over several pumps ──────
#
# Measured: ``pygame.joystick.init()`` and every ``Joystick(i)`` hold the GIL
# for their whole duration (168 / 106 / 258 / 54 ms here). Doing all of it in
# one pump kept the main loop out of ``asyncore`` for ~590 ms and stalled the
# 100 ms assistance thread for up to 258 ms.


class _FakeJoystick:
    def __init__(self, name, axes):
        self._name = name
        self._axes = axes

    def init(self):
        pass

    def get_name(self):
        return self._name

    def get_numaxes(self):
        return self._axes

    def get_axis(self, index):
        return 0.0


class _FakeJoystickModule:
    def __init__(self, outer):
        self._outer = outer

    def init(self):
        pass

    def get_count(self):
        return len(self._outer._devices)

    def Joystick(self, index):
        self._outer.opened.append(index)
        name, axes = self._outer._devices[index]
        return _FakeJoystick(name, axes)


class _FakeDisplayModule:
    def __init__(self, outer):
        self._outer = outer

    def init(self):
        self._outer.subsystem_inits += 1


class _FakeEventModule:
    @staticmethod
    def pump():
        pass


class _FakePygame:
    """Counts the SDL calls, so "one device per call" is assertable."""

    def __init__(self, devices):
        self._devices = devices
        self.opened = []
        self.subsystem_inits = 0
        self.display = _FakeDisplayModule(self)
        self.joystick = _FakeJoystickModule(self)
        self.event = _FakeEventModule


def _watch_with(monkeypatch, bus, settings, devices):
    fake = _FakePygame(devices)
    monkeypatch.setattr('misc.pedal_watch.get_joystick', lambda: fake)
    monkeypatch.setattr('misc.pedal_watch.is_available', lambda _name: True)
    watch = PedalWatch(bus, settings)
    watch.request_start()
    watch._warming = False          # the pygame import is what that thread does
    return watch, fake


def test_only_one_device_is_opened_per_call(monkeypatch, bus, settings):
    watch, fake = _watch_with(monkeypatch, bus, settings,
                              [('FANATEC Wheel', 12), ('FANATEC Wheel', 8),
                               ('vJoy Device', 8)])

    assert watch.start() is False           # the SDL subsystem, no device yet
    assert fake.opened == []
    assert watch.start() is False
    assert fake.opened == [0]
    assert watch.start() is False
    assert fake.opened == [0, 1]
    assert watch.start() is True            # last device, and it finishes
    assert fake.opened == [0, 1, 2]
    assert watch.is_running() is True


def test_nothing_reads_a_half_built_axis_list(monkeypatch, bus, settings):
    """``is_running`` stays False until every device is open -- a partial list
    would make ``_flat_index`` resolve a stored fit onto the wrong axis."""
    watch, _fake = _watch_with(monkeypatch, bus, settings,
                               [('FANATEC Wheel', 12), ('FANATEC Wheel', 8)])

    watch.start()
    assert watch.is_running() is False
    watch.start()
    assert watch.is_running() is False
    watch.start()
    assert watch.is_running() is True


def test_our_own_virtual_device_is_still_skipped(monkeypatch, bus, settings):
    watch, _fake = _watch_with(monkeypatch, bus, settings,
                               [('vJoy Device', 8), ('FANATEC Wheel', 3)])

    while not watch.start():
        pass

    # Only the wheel's three axes are watched; the vJoy device is ours.
    assert len(watch._joysticks) == 3
    assert {label[1] for label in watch._labels.values()} == {'FANATEC Wheel'}


def test_a_machine_with_no_joystick_at_all_finishes_in_one_step(
        monkeypatch, bus, settings):
    watch, fake = _watch_with(monkeypatch, bus, settings, [])

    assert watch.start() is False           # nothing found -> a failure
    assert fake.subsystem_inits == 1
    assert watch.unavailable_reason() == 'no_joystick_found'
    assert watch.start() is False           # remembered, not retried
    assert fake.subsystem_inits == 1


def test_a_stop_does_not_resume_a_half_finished_enumeration(
        monkeypatch, bus, settings):
    watch, fake = _watch_with(monkeypatch, bus, settings,
                              [('FANATEC Wheel', 12), ('FANATEC Wheel', 8)])
    watch.start()
    watch.start()
    assert fake.opened == [0]

    watch.stop()
    watch.request_start()
    watch._warming = False

    watch.start()
    assert fake.opened == [0]               # back at the subsystem step
    watch.start()
    assert fake.opened == [0, 0]            # device 0 again, not device 1


# ─── A saturated pedal teaches nothing, and used to say nothing ──────────────
#
# Measured live on 2026-09-20: the driver braked to a full stop four times from
# ~50 km/h and the brake did not calibrate, while the throttle calibrated in the
# same window. Nothing in the log, the menu or the chat said why -- every gate in
# PedalLearner.attempt returned a bare None. The cause is that only the linear
# middle counts (FIT_LOW..FIT_HIGH) and a pedal held at 1.00 is outside it, so a
# full application contributes about 4 usable samples out of 36.


def _application(rise_s, hold_s, fall_s, peak, rate_hz=10):
    """One pedal application, sampled at the assistance rate."""
    count = lambda seconds: max(1, int(round(seconds * rate_hz)))
    out = [peak * (i + 1) / count(rise_s) for i in range(count(rise_s))]
    out += [peak] * count(hold_s)
    out += [peak * (1 - (i + 1) / count(fall_s)) for i in range(count(fall_s))]
    return out


def _drive(learner, applications, rest_samples=20):
    """Feed the learner: axis 0 is the pedal, at rest +0.91, full -0.91."""
    for application in applications:
        for ref in application:
            learner.add((0.91 - 1.82 * ref, 0.0), ref)
        for _ in range(rest_samples):
            learner.add((0.91, 0.0), 0.0)


_PEDAL_LABELS = {0: (1, 'FANATEC Wheel', 4), 1: (1, 'FANATEC Wheel', 1)}


def test_four_full_stops_do_not_identify_the_brake():
    """The live case, reproduced: 16 of 224 samples land in the usable band."""
    learner = PedalLearner('brake')
    _drive(learner, [_application(0.3, 3.0, 0.3, 1.00)] * 4)

    assert learner.attempt(_PEDAL_LABELS) is None


def test_the_same_four_applications_at_85_percent_do_identify_it():
    """Same manoeuvre, same count, only not flat out -- and the endpoints come
    back at the pedal's real travel."""
    learner = PedalLearner('brake')
    _drive(learner, [_application(0.4, 2.5, 0.4, 0.85)] * 4)

    fit = learner.attempt(_PEDAL_LABELS)

    assert fit is not None
    assert fit.axis == 4
    assert fit.raw_zero == pytest.approx(0.91, abs=0.02)
    assert fit.raw_full == pytest.approx(-0.91, abs=0.02)


def test_a_refusal_names_the_gate_that_stopped_it():
    learner = PedalLearner('brake')
    _drive(learner, [_application(0.3, 3.0, 0.3, 1.00)] * 4)

    learner.attempt(_PEDAL_LABELS)

    assert learner.refusal is not None
    assert 'usable range' in learner.refusal
    assert str(MIN_SAMPLES) in learner.refusal


def test_a_pedal_that_barely_moved_says_so_rather_than_blaming_the_samples():
    learner = PedalLearner('brake')
    _drive(learner, [_application(1.0, 4.0, 1.0, 0.30)] * 4)

    learner.attempt(_PEDAL_LABELS)

    assert learner.refusal is not None
    assert 'travel' in learner.refusal


def test_two_axes_that_move_together_say_which_ones():
    """A wheel with a combined axis, a load cell or a clutch that follows the
    brake lands here, and it never clears by itself."""
    learner = PedalLearner('brake')
    for ref in _application(0.5, 0.5, 0.5, 0.9) * 12:
        learner.add((0.91 - 1.82 * ref, 0.91 - 1.82 * ref), ref)

    assert learner.attempt(_PEDAL_LABELS) is None
    assert 'equally well' in (learner.refusal or '')


def test_a_successful_fit_clears_the_refusal():
    learner = PedalLearner('brake')
    _drive(learner, [_application(0.3, 3.0, 0.3, 1.00)] * 4)
    learner.attempt(_PEDAL_LABELS)
    assert learner.refusal is not None

    _drive(learner, [_application(0.4, 2.5, 0.4, 0.85)] * 4)

    assert learner.attempt(_PEDAL_LABELS) is not None
    assert learner.refusal is None


def test_the_reason_is_logged_once_and_then_rate_limited(
        bus, settings, make_own_vehicle, caplog):
    """A standing condition, not an event: say it, then leave the driver
    alone."""
    watch = PedalWatch(bus, settings)
    watch._devices_ready = True
    watch._labels = _PEDAL_LABELS
    clock = {'t': 0.0}
    watch.clock = lambda: clock['t']
    _drive(watch._learners['brake'], [_application(0.3, 3.0, 0.3, 1.00)] * 4)

    with caplog.at_level(logging.INFO, logger='misc.pedal_watch'):
        watch._try_fits()
        watch._try_fits()                       # same reason, same moment
        clock['t'] = REFUSAL_LOG_INTERVAL_S + 1.0
        watch._try_fits()                       # timer expired

    said = [r.getMessage() for r in caplog.records
            if 'not identified yet' in r.getMessage()]
    assert len(said) == 2


def test_nothing_is_said_before_the_driver_has_touched_the_pedal(
        bus, settings, caplog):
    """"No samples yet" is not a problem and must not read like one."""
    watch = PedalWatch(bus, settings)
    watch._devices_ready = True
    watch._labels = _PEDAL_LABELS

    with caplog.at_level(logging.INFO, logger='misc.pedal_watch'):
        watch._try_fits()

    assert [r for r in caplog.records
            if 'not identified yet' in r.getMessage()] == []


def test_changing_sample_counts_cannot_bypass_log_throttle(bus, settings, caplog):
    watch = PedalWatch(bus, settings)
    watch.clock = lambda: 5.0
    learner = watch._learners['brake']
    learner.add((0.0,), 0.5)
    with caplog.at_level(logging.INFO, logger='misc.pedal_watch'):
        for count in range(40):
            learner.refusal = f'only {count} usable samples'
            watch._report_refusal('brake', learner)
    assert sum('not identified yet' in r.getMessage() for r in caplog.records) == 1
