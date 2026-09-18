"""Identifying the driver's own pedals by correlating them against OutGauge.

The point of ``misc/pedal_watch.py`` is that nobody types an axis number: while
LFS is still reading the driver's pedals, ``OutGauge`` reports exactly what they
are pressing, and one axis out of the two dozen a wheel reports follows it. All
of that is arithmetic, so it is tested with numbers -- no joystick, no LFS, and
no thread.
"""

import pytest

from misc.pedal_watch import (FIT_ERROR_CYCLES, MIN_SAMPLES, PedalFit,
                              PedalLearner, PedalWatch, _correlate, _endpoints)


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
    different order. Nothing announces that; the value would just drift."""
    watch, settings = calibrated_watch(bus, make_settings)
    watch._axes = (0.0,) * 6                      # says 0.5

    for _ in range(FIT_ERROR_CYCLES + 1):
        watch.observe(make_own_vehicle(local_plid=1, plid=1, brake=0.0))

    assert watch.driver_brake() is None
    assert settings.get('pedal_brake_axis') == -1


def test_a_normal_pass_feeds_both_learners(bus, settings, make_own_vehicle):
    watch = PedalWatch(bus, settings)
    watch._devices_ready = True
    watch._axes = (0.1, 0.2, 0.3)

    watch.observe(make_own_vehicle(local_plid=1, plid=1,
                                   brake=0.4, throttle=0.6))

    assert watch._learners['brake'].samples[-1] == ((0.1, 0.2, 0.3), 0.4)
    assert watch._learners['throttle'].samples[-1] == ((0.1, 0.2, 0.3), 0.6)
