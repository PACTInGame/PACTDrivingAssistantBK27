import time

from core.event_bus import EventBus
from core.settings_manager import SettingsManager
# init pygame mixer stuff
import pygame


class AudioPlayer():
    """Audio player component for handling audio playback."""

    def __init__(self, event_bus: EventBus, settings: SettingsManager):
        self.event_bus = event_bus
        self.settings = settings
        self.event_bus.subscribe('play_audio', self._update_audio_queue)
        self.last_played_audio = [] # list that stores sounds played withing the last 3 seconds
        self.no_multiple_playback_audios = ["fcw"]
        pygame.mixer.init()

    def _update_audio_queue(self, event):
        """Updates the audio playback queue based on events."""
        audio_file = event.get('audio_file')
        print(f"AudioPlayer: Received request to play audio file: {audio_file}")
        if audio_file:
            # Clean up last_played_audio list
            self.last_played_audio = [item for item in self.last_played_audio if item[1] + 3 > time.perf_counter()]

            can_be_played = audio_file in self.no_multiple_playback_audios and audio_file not in self.last_played_audio
            if can_be_played or audio_file not in self.no_multiple_playback_audios:
                if audio_file in self.no_multiple_playback_audios:
                    self.last_played_audio.append([audio_file, time.perf_counter()])

                self._play_audio(audio_file)


    def _play_audio(self, audio_file):
        # Play audio using pygame
        try:
            sound = pygame.mixer.Sound(f"audio/{audio_file}.wav")
            sound.play()
        except Exception as e:
            print(f"Error playing audio file {audio_file}: {e}")
