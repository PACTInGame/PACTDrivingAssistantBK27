"""Die Warntoene - eine Stimme, kein Mischpult.

Warntoene sind keine Musik. Wenn zwei davon gleichzeitig etwas zu sagen haben,
ist das Richtige, den neueren zu spielen, und nicht beide uebereinander. Genau
das hat die alte Fassung getan, und es hat sich angehoert wie der Fehlerbericht
es beschreibt: *"do - do - do - rauschen und knacken - do - do - do"*.

Drei Ursachen, alle drei hier behoben (``known-issues.md`` #54):

1. **Dieselbe Datei mehrfach gleichzeitig.** Die Kollisionswarnung hat
   ``play_audio`` dreimal in derselben Zeile abgesetzt, um dreimal zu piepen.
   pygame hat daraus drei *gleichzeitige* Kopien derselben Welle gemacht -
   dreifache Amplitude, also rund 9.5 dB darueber und sicher im Clipping. Ein
   ``repeat`` im Payload spielt sie jetzt **nacheinander** (``Channel.queue``).
2. **Unbegrenzte Ueberlagerung.** Jeder Aufruf nahm sich einen freien Kanal.
   Flatterte eine Warnstufe im 50-ms-UI-Takt, lagen Sekundenbruchteile spaeter
   ein Dutzend Kopien eines 0.88-s-Samples uebereinander, bis pygame die
   Kanaele ausgingen und anfing, laufende Toene abzuschneiden. Jetzt gibt es
   **einen reservierten Kanal**; ein neuer Warnton loest den alten ab.
3. **Plattenzugriff im Ausgabepfad.** ``mixer.Sound(datei)`` wurde bei *jedem*
   Ton neu von der Platte geladen - blockierendes I/O in einem 50-ms-Zyklus
   (``AGENTS.md`` §1), und ein verspaeteter Puffer ist genau das Knacken.
   Die Dateien werden einmal beim Start geladen und behalten.

Dazu die Puffergroesse: pygames Vorgabe sind 512 Samples (~12 ms). Auf einem
Rechner, der nebenher LFS rendert, ist das zu knapp - ein Underrun ist hoerbar.
``MIXER_BUFFER`` gibt dem Mixer rund 21 ms, und die Abtastrate ist die der
Dateien, damit nichts resampelt werden muss.

Kosten: ein dict-Zugriff und ein ``Channel.play`` je Ton. Kein I/O, keine
Allokation, kein Thread.
"""

import logging
import os
import time

from core.event_bus import EventBus
from core.settings_manager import SettingsManager
from misc.helpers import resolve_path
# pygame mixer, imported lazily so this module loads without a sound device
from misc.platform_shim import get_audio

logger = logging.getLogger(__name__)


class AudioPlayer:
    """Spielt die Warntoene des Add-ons ab."""

    # Die WAVs liegen in 48 kHz Stereo vor; dieselbe Rate im Mixer spart das
    # Resampling beim Laden.
    MIXER_FREQUENCY = 48000
    MIXER_SIZE = -16
    MIXER_CHANNELS = 2
    # ~21 ms. Siehe Modulkopf.
    MIXER_BUFFER = 1024
    # Der eine Kanal, auf dem alle Warnungen liegen. Reserviert, damit
    # ``Sound.play()`` von irgendwo sonst ihn nicht wegnimmt.
    WARNING_CHANNEL = 0

    # Toene, die sich nicht selbst wiederholen duerfen, und wie lange nicht.
    # ``fcw`` ist der einmalige Gong der Kollisionswarnung: er meldet ein
    # *Ereignis*, und ein Ereignis, das dreimal in zwei Sekunden gemeldet wird,
    # ist keine Information mehr.
    REPEAT_SUPPRESSION_S = {'fcw': 3.0}
    # Untergrenze fuer *jeden* Ton. Kuerzer als das kann kein Mensch zwei
    # Warnungen auseinanderhalten, und es ist die Schranke, die verhindert,
    # dass eine flatternde Warnstufe den Kanal im UI-Takt neu anschlaegt.
    MIN_GAP_S = 0.25
    # Wie oft ein Ton hoechstens hintereinander gespielt wird. Eine Zahl aus
    # einem Payload ist nicht vertrauenswuerdig (``AGENTS.md`` §3).
    MAX_REPEAT = 5

    def __init__(self, event_bus: EventBus, settings: SettingsManager,
                 clock=time.monotonic):
        self.event_bus = event_bus
        self.settings = settings
        self.clock = clock
        # Wann welcher Ton zuletzt angeschlagen wurde. Ersetzt die alte Liste
        # samt Aufraeumschleife durch einen dict-Zugriff.
        self._last_played = {}
        self._sounds = {}
        self._mixer_ready = self._init_mixer()
        if self._mixer_ready:
            self._preload()
        self.event_bus.subscribe('play_audio', self._on_play_audio)

    # ─── Aufbau ───────────────────────────────────────────────────────

    def _init_mixer(self) -> bool:
        """Mixer starten. Ohne Audiogeraet laeuft alles Uebrige weiter.

        Frueher stand hier ein ungeschuetztes ``mixer.init()``: ein Rechner
        ohne Soundgeraet hat damit beim Start eine Exception geworfen und die
        ganze App mitgenommen, obwohl nur die Warntoene betroffen sind.
        """
        try:
            get_audio().mixer.init(frequency=self.MIXER_FREQUENCY,
                                   size=self.MIXER_SIZE,
                                   channels=self.MIXER_CHANNELS,
                                   buffer=self.MIXER_BUFFER)
            # Kanal 0 gehoert ab hier den Warnungen allein.
            get_audio().mixer.set_reserved(self.WARNING_CHANNEL + 1)
        except Exception as e:
            logger.warning("No audio output (%s: %s) - warning sounds are off.",
                           type(e).__name__, e)
            return False
        return True

    def _preload(self):
        """Alle WAVs einmal laden, damit im Zyklus kein I/O mehr liegt."""
        folder = resolve_path("audio")
        try:
            names = [f[:-4] for f in os.listdir(folder) if f.endswith('.wav')]
        except OSError as e:
            logger.warning("The audio folder could not be read (%s: %s).",
                           type(e).__name__, e)
            return
        for name in names:
            self._sound(name)

    def _sound(self, audio_file: str):
        """Der geladene Ton, oder ``None``, wenn er nicht zu laden war."""
        if audio_file in self._sounds:
            return self._sounds[audio_file]
        sound = None
        try:
            sound = get_audio().mixer.Sound(
                resolve_path("audio", f"{audio_file}.wav"))
        except Exception as e:
            logger.warning("Audio file %r could not be loaded: %s: %s",
                           audio_file, type(e).__name__, e)
        self._sounds[audio_file] = sound
        return sound

    # ─── Ausgabe ──────────────────────────────────────────────────────

    def _on_play_audio(self, event):
        """``play_audio``: ``audio_file`` und optional ``repeat``.

        ``repeat`` spielt denselben Ton *nacheinander* - siehe Modulkopf. Der
        Payload kommt vom Bus, also wird nichts angenommen.
        """
        if not isinstance(event, dict):
            return
        audio_file = event.get('audio_file')
        if not audio_file or not self._mixer_ready:
            return
        try:
            repeat = int(event.get('repeat', 1) or 1)
        except (TypeError, ValueError):
            repeat = 1
        repeat = max(1, min(self.MAX_REPEAT, repeat))

        if not self._may_play(audio_file):
            return
        self._last_played[audio_file] = self.clock()
        self._play(audio_file, repeat)

    def _may_play(self, audio_file: str) -> bool:
        """Darf dieser Ton jetzt kommen?

        Zwei Schranken: die allgemeine Mindestpause und, fuer die Toene in
        ``REPEAT_SUPPRESSION_S``, eine laengere eigene.
        """
        last = self._last_played.get(audio_file)
        if last is None:
            return True
        gap = max(self.MIN_GAP_S,
                  self.REPEAT_SUPPRESSION_S.get(audio_file, 0.0))
        return self.clock() - last >= gap

    def _play(self, audio_file: str, repeat: int):
        sound = self._sound(audio_file)
        if sound is None:
            return
        try:
            channel = get_audio().mixer.Channel(self.WARNING_CHANNEL)
            # Ohne ``stop`` wuerde ``queue`` an einen noch laufenden Ton
            # anhaengen, statt ihn abzuloesen - und genau das Stapeln ist das,
            # was dieser Kanal verhindern soll.
            channel.stop()
            channel.play(sound)
            for _ in range(repeat - 1):
                channel.queue(sound)
        except Exception as e:
            logger.warning("Playing %r failed: %s: %s",
                           audio_file, type(e).__name__, e)
