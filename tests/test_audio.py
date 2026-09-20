"""Die Warntoene - known-issues #54.

Der Mixer wird hier nicht benutzt: ``misc.audio_player.get_audio`` wird durch
ein Attrappen-pygame ersetzt, das mitschreibt, was auf dem Kanal passiert
waere. Das laeuft auf jedem Betriebssystem und ohne Soundgeraet
(``reference/testing.md``).
"""

import pytest

from misc import audio_player as audio_module
from misc.audio_player import AudioPlayer
from ui.ui_manager import BSW_ACUTE_AUDIO, BSW_ACUTE_BEEP_INTERVAL_S, FCW_BEEPS


class FakeChannel:
    """Ein pygame-Mixerkanal, so weit der AudioPlayer ihn benutzt."""

    def __init__(self):
        # ('stop',) / ('play', name) / ('queue', name), in der Reihenfolge.
        self.calls = []

    def stop(self):
        self.calls.append(('stop',))

    def play(self, sound):
        self.calls.append(('play', sound))

    def queue(self, sound):
        self.calls.append(('queue', sound))


class FakeMixer:
    def __init__(self):
        self.channel = FakeChannel()
        self.init_kwargs = None
        self.reserved = None
        self.loaded = []

    def init(self, **kwargs):
        self.init_kwargs = kwargs

    def set_reserved(self, count):
        self.reserved = count

    def Sound(self, path):                       # noqa: N802 - pygame's name
        self.loaded.append(path)
        return path

    def Channel(self, index):                    # noqa: N802 - pygame's name
        assert index == AudioPlayer.WARNING_CHANNEL
        return self.channel


class FakePygame:
    def __init__(self):
        self.mixer = FakeMixer()


class FakeClock:
    def __init__(self):
        self.now = 1000.0

    def __call__(self):
        return self.now

    def advance(self, seconds):
        self.now += seconds


@pytest.fixture
def mixer(monkeypatch):
    pygame = FakePygame()
    monkeypatch.setattr(audio_module, 'get_audio', lambda: pygame)
    return pygame.mixer


@pytest.fixture
def player(bus, settings, mixer):
    clock = FakeClock()
    player = AudioPlayer(bus, settings, clock=clock)
    mixer.channel.calls.clear()          # the preload is not under test here
    return player


def played(mixer):
    """Only the sounds that were started, in order."""
    return [name for kind, name in
            (c for c in mixer.channel.calls if c[0] in ('play', 'queue'))]


# ─── Setup ───────────────────────────────────────────────────────────────────

def test_the_mixer_gets_a_buffer_big_enough_for_a_game(player, mixer):
    """pygame's default is 512 samples (~12 ms) - an underrun is audible."""
    assert mixer.init_kwargs['buffer'] >= 1024
    assert mixer.init_kwargs['frequency'] == 48000     # the files' own rate


def test_the_warning_channel_is_reserved(player, mixer):
    assert mixer.reserved == AudioPlayer.WARNING_CHANNEL + 1


def test_every_file_is_loaded_once_and_kept(bus, settings, mixer):
    """Loading a Sound is disk I/O and used to happen on every single beep,
    inside a 50 ms UI cycle (AGENTS.md section 1)."""
    AudioPlayer(bus, settings, clock=FakeClock())
    before = len(mixer.loaded)
    assert before > 0

    bus.emit('play_audio', {'audio_file': 'fcw'})

    assert len(mixer.loaded) == before


def test_a_missing_audio_device_does_not_stop_the_app(bus, settings, monkeypatch):
    """Only the warning sounds are affected, so only they may fail."""
    class Broken:
        class mixer:
            @staticmethod
            def init(**kwargs):
                raise RuntimeError("no audio device")

    monkeypatch.setattr(audio_module, 'get_audio', lambda: Broken)
    player = AudioPlayer(bus, settings, clock=FakeClock())

    bus.emit('play_audio', {'audio_file': 'fcw'})      # must not raise


# ─── The three beeps ─────────────────────────────────────────────────────────

def test_a_repeat_plays_the_sound_one_after_another(player, mixer, bus):
    """known-issues #54: three simultaneous copies of one waveform is +9.5 dB
    and straight into clipping. They belong one after the other."""
    bus.emit('play_audio', {'audio_file': 'warning_3', 'repeat': 3})

    kinds = [call[0] for call in mixer.channel.calls if call[0] != 'stop']
    assert kinds == ['play', 'queue', 'queue']
    assert len(set(played(mixer))) == 1


def test_a_repeat_count_from_the_bus_is_not_trusted(player, mixer, bus):
    for payload in ({'audio_file': 'fcw', 'repeat': 9999},
                    {'audio_file': 'fcw', 'repeat': 'lots'},
                    {'audio_file': 'fcw', 'repeat': -4}):
        mixer.channel.calls.clear()
        player._last_played.clear()
        bus.emit('play_audio', payload)
        assert 1 <= len(played(mixer)) <= AudioPlayer.MAX_REPEAT


# ─── One voice ───────────────────────────────────────────────────────────────

def test_a_new_warning_replaces_the_one_that_is_still_sounding(player, mixer, bus):
    """The blind spot beep on top of the collision gong is not twice the
    information, it is twice the amplitude."""
    bus.emit('play_audio', {'audio_file': 'fcw'})
    player.clock.advance(AudioPlayer.MIN_GAP_S)
    mixer.channel.calls.clear()

    bus.emit('play_audio', {'audio_file': BSW_ACUTE_AUDIO})

    assert mixer.channel.calls[0] == ('stop',)


def test_a_flapping_warning_level_cannot_restack_the_channel(player, mixer, bus):
    """The reported failure: several cars in the acute stages, hold times
    expiring out of step, an edge every UI cycle."""
    for _ in range(20):
        bus.emit('play_audio', {'audio_file': BSW_ACUTE_AUDIO})
        player.clock.advance(0.05)          # one UI cycle

    assert len(played(mixer)) <= 5          # 1 s of cycles / MIN_GAP_S


def test_the_collision_gong_still_does_not_repeat_itself(player, mixer, bus):
    """It reports an event; an event reported three times in two seconds is
    not information any more."""
    bus.emit('play_audio', {'audio_file': 'fcw'})
    mixer.channel.calls.clear()
    player.clock.advance(AudioPlayer.REPEAT_SUPPRESSION_S['fcw'] - 0.1)
    bus.emit('play_audio', {'audio_file': 'fcw'})
    assert played(mixer) == []

    player.clock.advance(0.2)
    bus.emit('play_audio', {'audio_file': 'fcw'})
    assert len(played(mixer)) == 1


def test_a_payload_without_a_file_is_ignored(player, mixer, bus):
    bus.emit('play_audio', {})
    bus.emit('play_audio', None)
    bus.emit('play_audio', {'audio_file': ''})
    assert mixer.channel.calls == []


# ─── The UI side ─────────────────────────────────────────────────────────────

@pytest.fixture
def ui(bus, message_sender, settings):
    from ui.ui_manager import UIManager
    return UIManager(bus, message_sender, settings)


def test_the_collision_warning_asks_for_one_event_with_a_repeat(
        bus, ui, recorder):
    """Not three events - see the docstring of the module under test."""
    events = recorder('play_audio')

    bus.emit('collision_warning_changed', {'level': 3})

    payloads = events.payloads('play_audio')
    assert payloads == [{'audio_file': 'fcw', 'repeat': FCW_BEEPS}]


def test_the_blind_spot_edge_cannot_bypass_the_repeat_interval(
        bus, ui, recorder):
    """The edge stays immediate; it just no longer gets a free beep.

    Two cars, two hold times running out of step: the acute level drops to 0
    and comes straight back, over and over.
    """
    events = recorder('play_audio')

    for _ in range(10):
        bus.emit('blind_spot_warning_changed',
                 {'left': True, 'left_level': 2, 'right_level': 0})
        bus.emit('blind_spot_warning_changed',
                 {'left': False, 'left_level': 0, 'right_level': 0})

    assert events.count('play_audio') == 1
    assert BSW_ACUTE_BEEP_INTERVAL_S > 0


@pytest.mark.parametrize("repeat", [1, 2, 3, 9999])
def test_collision_gong_never_queues_a_second_copy(player, mixer, bus, repeat):
    bus.emit('play_audio', {'audio_file': 'fcw', 'repeat': repeat})
    assert len(played(mixer)) == 1
    assert not any(call[0] == 'queue' for call in mixer.channel.calls)


def test_collision_and_cross_traffic_flapping_share_one_gong(
        player, mixer, bus, ui):
    for level in [2, 3, 1, 2, 0, 3]:
        bus.emit('collision_warning_changed', {'level': level})
        bus.emit('cross_traffic_warning_changed', {'level': level, 'side': 'left'})
        player.clock.advance(0.05)
    assert len(played(mixer)) == 1
