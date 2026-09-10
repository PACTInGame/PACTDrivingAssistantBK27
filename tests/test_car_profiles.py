"""Learning idle, redline and gear count from the OutGauge stream.

`vehicles/car_profiles.py` exists so that a car the driver has never calibrated
still shifts, and so that the HUD knows where the redline is — LFS's own shift
light turned out to be active only in the race cars, so it could not be the
source (see the module docstring).

The property worth pinning hardest is the one that is easy to get wrong:
**OutGauge follows the camera**. Pressing TAB puts another car's rpm and gear in
the stream, and a car name tracked through a *different* channel would not turn
over in the same packet. These tests therefore feed packets whose `Car` field
changes mid-stream and check that nothing lands under the wrong name.
"""

import json

import pytest

from vehicles.car_profiles import (ENGINE_RUNNING_MIN_RPM, IDLE_SAMPLE_LIMIT,
                                   REDLINE_SETTLE_S, CarProfiles, car_key,
                                   stock_profile)


class FakeClock:
    """A monotonic clock a test drives by hand."""

    def __init__(self, now: float = 1000.0):
        self.now = now

    def __call__(self) -> float:
        return self.now

    def advance(self, seconds: float):
        self.now += seconds
        return self.now


class OutGauge:
    """One OutGauge packet, with the fields this module reads."""

    def __init__(self, car=b'XRT', rpm=1000.0, gear=1, throttle=0.0,
                 speed_kmh=0.0):
        self.Car = car
        self.RPM = rpm
        self.Gear = gear
        self.Throttle = throttle
        self.Speed = speed_kmh / 3.6      # OutGauge speed is m/s


@pytest.fixture
def clock():
    return FakeClock()


@pytest.fixture
def profiles(tmp_path, clock):
    """A profile store on a hand-driven clock.

    The clock matters: a redline only counts once it has stopped rising for
    ``REDLINE_SETTLE_S``, so a test that wants an answer has to let time pass.
    """
    return CarProfiles(path=str(tmp_path / 'car_profiles.json'), clock=clock)


def settle(clock):
    """Let the highest rpm stand long enough to count as the rev limit."""
    clock.advance(REDLINE_SETTLE_S + 0.1)


PACKET_INTERVAL_S = 0.03      # OutGauge runs at about 30 Hz

# The three ``Car`` bytes one of the tested mods really sent.
MOD_ID_BYTES = bytes((0x06, 0xC8, 0xD3))


def feed(profiles, clock, packets):
    """Feed packets at the real OutGauge rate.

    The clock has to move: an idle sample only counts while the engine speed is
    *holding still*, which cannot be judged between two packets that arrived at
    the same instant.
    """
    for packet in packets:
        clock.advance(PACKET_INTERVAL_S)
        profiles.record(packet)


def idle_for(profiles, clock, count, rpm=950.0, car=b'XRT'):
    feed(profiles, clock, [idling(car=car, rpm=rpm) for _ in range(count)])


def idling(car=b'XRT', rpm=950.0):
    return OutGauge(car=car, rpm=rpm, gear=1, throttle=0.0, speed_kmh=0.0)


def driving(car=b'XRT', rpm=6000.0, gear=4):
    return OutGauge(car=car, rpm=rpm, gear=gear, throttle=1.0, speed_kmh=120.0)


# ─── Learning ────────────────────────────────────────────────────────────────

def test_the_redline_is_the_highest_rpm_ever_seen(profiles, clock):
    """It only ever moves up, so it converges on the real limit over time."""
    profiles.record(driving(rpm=5000.0))
    profiles.record(driving(rpm=7481.0))
    profiles.record(driving(rpm=6200.0))
    settle(clock)

    assert profiles.redline('XRT') == 7481.0


def test_a_rising_rpm_is_not_a_redline_yet(profiles, clock):
    """During the first climb the maximum *is* the current rpm.

    Answering with it would mean "you are at the rev limit" at every rpm --
    the readout would be red for the whole climb and the gearbox would upshift
    straight away.
    """
    for rpm in (2000.0, 3000.0, 4000.0, 7000.0):
        clock.advance(0.03)
        profiles.record(driving(rpm=rpm))

    assert profiles.redline('XRT') is None
    assert profiles.highest_rpm_seen('XRT') == 7000.0

    settle(clock)
    assert profiles.redline('XRT') == 7000.0


def test_the_gear_count_is_the_highest_gear_ever_engaged(profiles):
    """OutGauge counts 0 = reverse, 1 = neutral, 2 = 1st gear."""
    profiles.record(driving(gear=2))
    profiles.record(driving(gear=6))
    profiles.record(driving(gear=3))

    assert profiles.forward_gears('XRT') == 5


def test_idle_is_only_sampled_while_standing_still_off_the_throttle(profiles, clock):
    idle_for(profiles, clock, 20)
    # Revving on the spot, and driving: neither is idle.
    feed(profiles, clock, [OutGauge(rpm=6000.0, throttle=1.0, speed_kmh=0.0),
                           driving(rpm=4000.0)] * 20)

    assert profiles.idle('XRT') == 950.0


def test_an_engine_being_shut_down_is_not_sampled_as_idle(profiles, clock):
    """The field failure: a shutdown dragged a learned idle from 627 to 446.

    LFS stops a standing engine on its own. On the way down the engine passes
    through every speed below idle while stationary and off the throttle, so
    every other gate lets those samples through. An idling engine is governed
    and holds its speed; a dying one does not.
    """
    idle_for(profiles, clock, 60, rpm=950.0)
    assert profiles.idle('XRT') == 950.0

    # ~600 min-1 per second, which is what a shutdown looks like.
    feed(profiles, clock,
         [idling(rpm=rpm) for rpm in range(940, 300, -18)])

    assert profiles.idle('XRT') == 950.0


def test_idle_hunting_around_its_target_is_still_sampled(profiles, clock):
    """The gate must not reject the normal wobble of an idle governor."""
    feed(profiles, clock,
         [idling(rpm=rpm) for rpm in (940, 955, 948, 962, 950, 944) * 10])

    assert profiles.idle('XRT') == pytest.approx(950, abs=8)


def test_a_stopped_engine_is_never_learned_as_idle(profiles, clock):
    """LFS shuts a standing engine down and then reports rpm 0."""
    idle_for(profiles, clock, 30, rpm=0.0)

    assert profiles.idle('XRT') is None
    assert profiles.redline('XRT') is None


def test_a_corrupt_rpm_cannot_raise_the_redline(profiles, clock):
    profiles.record(driving(rpm=7000.0))
    profiles.record(driving(rpm=999999.0))
    settle(clock)

    assert profiles.redline('XRT') == 7000.0


def test_an_absurd_gear_index_is_ignored(profiles):
    profiles.record(driving(gear=5))
    profiles.record(driving(gear=99))

    assert profiles.forward_gears('XRT') == 4


def test_a_packet_without_a_car_name_teaches_nothing(profiles):
    profiles.record(OutGauge(car=b'', rpm=7000.0, gear=6))
    profiles.record(OutGauge(car=None, rpm=7000.0, gear=6))

    assert profiles.known_cars() == []


def test_a_broken_packet_does_not_kill_the_packet_handler(profiles):
    """This runs on the InSim thread; an exception there stops everything."""
    broken = OutGauge()
    broken.RPM = 'not a number'

    profiles.record(broken)         # must not raise

    assert profiles.redline('XRT') is None


# ─── The camera trap ─────────────────────────────────────────────────────────

def test_switching_the_camera_attributes_values_to_the_car_in_that_packet(
        profiles, clock):
    """TAB moves OutGauge to another car — mid-stream, packet by packet.

    Each packet carries its own `Car`, so the values can only ever land under
    the car they came from. Nothing here consults a car name from elsewhere.
    """
    profiles.record(driving(car=b'XRT', rpm=7481.0, gear=6))
    # Camera jumps to a BF1 being driven by someone else.
    profiles.record(driving(car=b'BF1', rpm=18500.0, gear=8))
    profiles.record(driving(car=b'XRT', rpm=7000.0, gear=5))
    settle(clock)

    assert profiles.redline('XRT') == 7481.0
    assert profiles.forward_gears('XRT') == 5
    assert profiles.redline('BF1') == 18500.0
    assert profiles.forward_gears('BF1') == 7


def test_the_car_name_is_normalised_the_same_way_everywhere(profiles, clock):
    profiles.record(driving(car=b'XRT\x00', rpm=7000.0))
    settle(clock)

    assert profiles.known_cars() == ['XRT']
    assert profiles.redline('xrt') == 7000.0
    assert profiles.redline(b'XRT\x00') == 7000.0
    assert car_key(b'FZ5\x00') == 'FZ5'
    assert car_key(None) == ''


def test_a_mod_gets_a_profile_like_anything_else(profiles, clock):
    """Unknown names are the normal case, not an error (`conventions.md` §4)."""
    profiles.record(driving(car=b'A1B', rpm=9000.0, gear=7))
    settle(clock)

    assert profiles.redline('A1B') == 9000.0
    assert stock_profile('A1B') is None


def test_a_mods_id_bytes_are_shown_as_its_hex_id(profiles, clock):
    """A mod's ``Car`` field is a number, not a name.

    Measured live: two mods sent ``06 C8 D3`` and ``AC C6 55``. Read as
    characters those became unreadable keys -- correct as dict keys, but
    meaningless in the profile file and in the log.
    """
    profiles.record(driving(car=MOD_ID_BYTES, rpm=7466.0, gear=6))
    settle(clock)

    assert profiles.known_cars() == ['D3C806']
    assert profiles.redline('D3C806') == 7466.0


def test_a_key_stored_by_an_older_build_still_finds_its_profile(tmp_path):
    """Older files hold the mod id decoded as characters. Same car, same bytes."""
    path = tmp_path / 'car_profiles.json'
    stored_the_old_way = MOD_ID_BYTES.decode('latin-1')
    path.write_text(json.dumps({
        stored_the_old_way: {'max_rpm': 7466.2, 'max_gear': 6,
                             'idle_rpm': 857.5},
    }), encoding='utf-8')

    profiles = CarProfiles(path=str(path))

    assert profiles.known_cars() == ['D3C806']
    assert profiles.redline('D3C806') == 7466.2
    assert profiles.forward_gears(MOD_ID_BYTES) == 5


def test_a_car_that_taught_us_nothing_is_not_written_to_the_file(tmp_path):
    """Sitting in the pits with the engine off is not a measurement."""
    path = tmp_path / 'car_profiles.json'
    clock = FakeClock()
    profiles = CarProfiles(path=str(path), clock=clock)
    idle_for(profiles, clock, 10, rpm=0.0, car=b'RB4')
    feed(profiles, clock, [driving(car=b'XRT', rpm=7000.0, gear=6)])

    profiles.maybe_save(force=True)

    stored = json.loads(path.read_text(encoding='utf-8'))
    assert list(stored) == ['XRT']


# ─── Bounds and caching ──────────────────────────────────────────────────────

def test_the_idle_sample_window_is_bounded(profiles, clock):
    feed(profiles, clock, [idling(rpm=900.0 + (n % 10))
                           for n in range(IDLE_SAMPLE_LIMIT * 2)])

    profile = profiles.profile('XRT')
    assert len(profile.idle_samples) == IDLE_SAMPLE_LIMIT


def test_the_idle_median_is_not_recomputed_for_an_unchanged_profile(
        profiles, clock):
    """The gearbox asks once per assistance cycle; sorting each time is waste."""
    idle_for(profiles, clock, 50)

    first = profiles.profile('XRT')._cached_version
    profiles.idle('XRT')
    after_read = profiles.profile('XRT')._cached_version
    profiles.idle('XRT')

    assert after_read != first          # the first read filled the cache
    assert profiles.profile('XRT')._cached_version == after_read


# ─── Persistence ─────────────────────────────────────────────────────────────

def test_what_was_learned_survives_a_restart(tmp_path):
    """And a value from the file needs no settling time - it settled last time."""
    path = str(tmp_path / 'car_profiles.json')
    clock = FakeClock()
    first = CarProfiles(path=path, clock=clock)
    idle_for(first, clock, 20, rpm=959.0)
    first.record(driving(rpm=7481.0, gear=6))
    first.maybe_save(force=True)

    second = CarProfiles(path=path)

    assert second.redline('XRT') == 7481.0
    assert second.forward_gears('XRT') == 5
    assert second.idle('XRT') == 959.0


def test_nothing_is_written_while_nothing_changed(tmp_path):
    path = tmp_path / 'car_profiles.json'
    profiles = CarProfiles(path=str(path))

    profiles.maybe_save(force=True)

    assert not path.exists()


def test_an_unreadable_profile_file_is_ignored_rather_than_fatal(tmp_path):
    path = tmp_path / 'car_profiles.json'
    path.write_text("{ this is not json", encoding='utf-8')

    profiles = CarProfiles(path=str(path))

    assert profiles.known_cars() == []


def test_garbage_inside_the_file_does_not_become_a_car_value(tmp_path):
    path = tmp_path / 'car_profiles.json'
    path.write_text(json.dumps({
        'XRT': {'max_rpm': 'lots', 'max_gear': None, 'idle_rpm': 12},
        'FZ5': 'not an object',
    }), encoding='utf-8')

    profiles = CarProfiles(path=str(path))

    assert profiles.redline('XRT') is None
    assert profiles.forward_gears('XRT') == 0
    # 12 min-1 is below ENGINE_RUNNING_MIN_RPM, so it is not an idle speed.
    assert profiles.idle('XRT') is None
    assert ENGINE_RUNNING_MIN_RPM > 12
    assert 'FZ5' not in profiles.known_cars()
