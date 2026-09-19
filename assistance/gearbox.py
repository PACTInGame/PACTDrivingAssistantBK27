import logging
import time
from typing import Dict, Any
from assistance.base_system import AssistanceSystem
from core.event_bus import EventBus
from core.settings_manager import SettingsManager
from misc.input_guard import InputGuard
from misc.key_tap import get_key_tapper
from misc.language import LanguageManager
from vehicles.own_vehicle import OwnVehicle
from vehicles.vehicle import Vehicle
import json
from pathlib import Path
from misc.helpers import resolve_path
from vehicles.car_profiles import (automatic_gearbox_allowed,
                                   automatic_gearbox_by_default, car_key,
                                   stock_profile)

logger = logging.getLogger(__name__)


def _calibration_key(cname) -> str:
    """Schluessel fuer data/gearbox_calibrations.json

    CName kommt seit WP4 dekodiert als str aus dem VehicleManager. bytes
    werden weiter akzeptiert, damit ein direkter Aufruf mit Rohdaten nicht
    unter einem anderen Schluessel landet.
    """
    if isinstance(cname, (bytes, bytearray)):
        return bytes(cname).split(b'\x00', 1)[0].decode('latin-1', errors='replace')
    return "" if cname is None else str(cname)


def _as_int(value, default: int = 0) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


class Gearbox(AssistanceSystem):
    """Automatic Gearbox"""

    # ─── Shift Tuning Constants ───────────────────────────────────────
    # Upshift point = idle + rpm_range * (UPSHIFT_BASE + UPSHIFT_THROTTLE_SCALE * throttle)
    #   Low throttle:  ~50% of rpm range
    #   Full throttle: ~92% of rpm range
    UPSHIFT_BASE = 0.50
    UPSHIFT_THROTTLE_SCALE = 0.42

    # Downshift point = idle + rpm_range * (DOWNSHIFT_BASE + DOWNSHIFT_THROTTLE_SCALE * throttle)
    #   Low throttle:  ~15% of rpm range
    #   Full throttle: ~35% of rpm range
    DOWNSHIFT_BASE = 0.15
    DOWNSHIFT_THROTTLE_SCALE = 0.20

    # Cooldown times (seconds)
    COOLDOWN_AFTER_UPSHIFT = 1.5    # before a downshift is allowed
    COOLDOWN_AFTER_DOWNSHIFT = 0.8  # before an upshift is allowed
    COOLDOWN_SAME_DIRECTION = 0.4   # before another shift in the same direction

    # ─── Der Antriebsstrang muss geschlossen sein ─────────────────────
    #
    # Die Schaltpunkte vergleichen die Motordrehzahl mit Leerlauf und
    # Abregeldrehzahl. Diese Drehzahl sagt aber nur dann etwas ueber den
    # richtigen Gang aus, wenn Motor und Raeder ueberhaupt verbunden sind:
    #
    #     omega_motor = omega_rad * i_gang * i_achse      (Kupplung zu)
    #
    # Bei offener Kupplung faellt diese Gleichung weg. Der Motor dreht dann
    # gegen das Gaspedal frei hoch und steht binnen 100 ms am Begrenzer -
    # *unabhaengig* davon, welcher Gang eingelegt ist. Genau darauf hat die
    # alte Fassung reagiert (known-issues #47): sie hat ihre eigene Kupplung
    # als "zu hohe Drehzahl, noch ein Gang" gelesen.
    #
    # Gemessen in 15_gearbox_tests (FZ5, t = 42.0 s): Tastendruck bei 41.96,
    # Kupplung offen ab 42.00, Gang 2->3 bei 42.05, und die Drehzahl steigt
    # von 6641 auf den Begrenzer bei 6983 statt zu fallen. Der naechste
    # Schaltvorgang ist nach COOLDOWN_SAME_DIRECTION = 0.40 s erlaubt, die
    # Kupplung ist aber erst nach CLUTCH_HOLD_S (0.30 s) *plus* der Rampe,
    # mit der LFS sie kommen laesst (~0.18 s), wieder zu. Sie schliesst also
    # nie: 2->3->4->5 in 0.8 s bei konstant 55 km/h.
    #
    # Deshalb zwei Bedingungen vor jeder Entscheidung, beide physikalisch
    # und nicht getunt:
    CLUTCH_ENGAGED_MAX = 0.05   # darueber ist der Antriebsstrang offen
    # Nach dem Schliessen braucht die Drehzahl Zeit, den neuen Gang
    # anzunehmen - Motortraegheit und der Kupplungsschlupf, den LFS
    # modelliert. Im selben Trace dauert der Angleich 0.35-0.6 s; 0.25 s ist
    # der Punkt, ab dem die Drehzahl die Richtung sicher zeigt, und liegt
    # weit unter der Zeit, die ein Gang unter Vollgas bis zum Begrenzer
    # braucht (2-4 s). Die Automatik wird davon also nicht langsamer.
    RPM_SETTLE_S = 0.25

    # Hochschalten unter Bremsung nimmt die Motorbremse weg und laesst das
    # Auto im falschen Gang stehen - kein Automatikgetriebe tut das. Waehrend
    # eines Notbremseingriffs war genau das die zweite Haelfte von #47
    # (3 -> 4 -> 5 -> 6 bei voller Bremse).
    MAX_BRAKE_FOR_UPSHIFT = 0.20

    # Schaltablauf (Sekunden). LFS liest die Tastatur einmal pro gerendertem
    # Bild, eine Haltezeit unterhalb einer Bildperiode wird schlicht verpasst -
    # 0.1 s sind auch bei 30 fps mehrere Bilder. Die Werte sind die, die die
    # alte, blockierende Fassung ungewollt aus pyautogui.PAUSE bekam; sie sind
    # kuerzer als COOLDOWN_SAME_DIRECTION, ueberlappen sich also nie.
    CLUTCH_LEAD_S = 0.10   # Kupplung ist getrennt, bevor der Gang kommt
    SHIFT_HOLD_S = 0.10    # Haltezeit der Gangtaste
    CLUTCH_HOLD_S = 0.30   # Kupplung ueber den ganzen Vorgang

    # Throttle smoothing
    THROTTLE_HISTORY_SIZE = 5

    # Minimum throttle to consider upshifting
    MIN_THROTTLE_FOR_UPSHIFT = 0.05

    # ─── Kalibrierung ─────────────────────────────────────────────────
    # Dauer eines Kalibrierschritts. Unveraendert 12 s - lang genug, um die
    # Drehzahl zu stabilisieren; was fehlte, war die Rueckmeldung waehrend
    # der Wartezeit.
    CALIBRATION_STEP_S = 12.0
    # Ueber dieser Geschwindigkeit gilt das Auto als bewegt (km/h).
    CALIBRATION_MAX_SPEED_KMH = 1.0
    # ──────────────────────────────────────────────────────────────────

    # Rohes OutGauge-Gangindex-Schema: 0 = Rueckwaerts, 1 = Leerlauf,
    # 2 = 1. Gang. Der hoechste Gang hat also den Index forward_gears + 1.
    FIRST_FORWARD_GEAR = 2

    def __init__(self, event_bus: EventBus, settings: SettingsManager,
                 car_profiles=None):
        super().__init__("automatic_gearbox", event_bus, settings)
        self.translator = LanguageManager()
        # Ueberschreibbar, damit Tests die Kalibrierung und die Cooldowns
        # ohne Wartezeit durchfahren koennen.
        self.clock = time.perf_counter
        self.gearbox_active = False
        self.calibrating = False
        self.calibration_requested = False
        self.redline = 0
        self.idle = 0
        # Anzahl der Vorwaertsgaenge - nicht mehr der rohe Gangindex. Der
        # wurde frueher gespeichert und an manchen Stellen als max_gears, an
        # anderen als max_gears - 1 angezeigt.
        self.forward_gears = 0
        self.car = None
        self.calibration_step = 0
        self.time_in_step = self.clock()
        # Extremwerte des laufenden Kalibrierschritts (siehe _observe_step).
        self._reset_step_extremes()
        self.last_throttle_values = []
        self.time_since_last_gear_change = self.clock()
        self.last_shift_direction = None  # 'up', 'down', or None
        # Seit wann ist der Antriebsstrang geschlossen? None = offen, und
        # dann wird gar nicht entschieden (siehe CLUTCH_ENGAGED_MAX).
        self._clutch_closed_since = None

        # Tastendruck-Schutz (reference/ui.md §1.4). Vorher pruefte der
        # Gearbox ueberhaupt nichts: ein Schaltvorgang waehrend des Chats
        # tippte Kupplung und Gang in die Chatzeile (known-issues #11).
        self.guard = InputGuard(event_bus)
        # Gelernte Fahrzeugprofile (vehicles/car_profiles.py). Optional: ohne
        # sie verhaelt sich der Gearbox wie vorher und braucht die manuelle
        # Kalibrierung.
        self.car_profiles = car_profiles
        # Zuletzt gemeldeter Verfuegbarkeitsgrund (None = die Automatik
        # arbeitet). Nur bei *Wechsel* veroeffentlicht - das Event geht ins
        # Menue, eines pro Zyklus waere ein Button-Sturm.
        self._reported_reason = None
        # True, solange die Werte aus der Kalibrierdatei des Fahrers stammen.
        # Nur dann sind sie endgueltig; Tabelle und Messung werden pro Zyklus
        # nachgezogen, damit eine besser werdende Messung auch ankommt.
        self._from_calibration_file = False
        self._learned_logged_at = None
        # Schaltvorgang ueber den gemeinsamen KeyTapper: die Haltezeiten laufen
        # auf dessen Thread, der Assistenzzyklus zahlt nur vier Heap-Pushes.
        # Vorher hielt pyautogui.PAUSE die Tasten - mit time.sleep im
        # 100-ms-Thread, also ~440 ms Blockade pro Gangwechsel (#43).
        self.tapper = get_key_tapper()

        # Listen for calibration request from menu
        self.event_bus.subscribe('gearbox_calibrate', self._on_calibration_requested)

    def _on_calibration_requested(self, data=None):
        """Wird vom Menü über den Event-Bus ausgelöst

        Waehrend einer laufenden Kalibrierung ist derselbe Menuepunkt der
        Abbruch - vorher war Wegfahren die einzige Moeglichkeit, aus den drei
        blinden 12-Sekunden-Schritten wieder herauszukommen.
        """
        self.calibration_requested = True

    # ─── Persistenz ───────────────────────────────────────────────────

    def save_calibrations_for_cars(self, cname) -> bool:
        """Speichert Kalibrierungen pro Autos

        Dateizugriff im Zyklus - aber genau einmal, am Ende einer
        Kalibrierung und im Stand. Ein Schreibfehler darf die gerade
        ermittelten Werte nicht mitnehmen, deshalb wird er gemeldet statt
        geworfen: eine Exception aus process() heraus wuerde das System nach
        fuenf Zyklen abschalten (assistance/manager.py).
        """
        cname = _calibration_key(cname)
        calibration_file = Path(resolve_path("data", "gearbox_calibrations.json"))

        calibrations = {}
        if calibration_file.exists():
            try:
                with open(calibration_file, 'r', encoding='utf-8') as f:
                    calibrations = json.load(f)
            except (OSError, json.JSONDecodeError):
                # Kaputte Datei darf die frische Kalibrierung nicht verhindern.
                calibrations = {}
        if not isinstance(calibrations, dict):
            calibrations = {}

        calibrations[cname] = {
            'redline': self.redline,
            'idle': self.idle,
            'forward_gears': self.forward_gears
        }

        try:
            calibration_file.parent.mkdir(parents=True, exist_ok=True)
            with open(calibration_file, 'w', encoding='utf-8') as f:
                json.dump(calibrations, f, indent=4, ensure_ascii=False)
        except OSError as exc:
            logger.warning("Storing the gearbox calibration for %s failed: %s: %s",
                           cname, type(exc).__name__, exc)
            return False
        return True

    def load_calibrations_for_cars(self, cname):
        """Lädt Kalibrierungen pro Autos

        Aeltere Dateien speichern unter ``max_gears`` den rohen Gangindex des
        hoechsten Gangs (2 = 1. Gang). Sie werden beim Laden umgerechnet, so
        dass eine vorhandene Kalibrierung weiter gilt.
        """
        calibration_file = Path(resolve_path("data", "gearbox_calibrations.json"))
        cname = _calibration_key(cname)

        self.redline = 0
        self.idle = 0
        self.forward_gears = 0
        self._from_calibration_file = False

        if not calibration_file.exists():
            return

        try:
            with open(calibration_file, 'r', encoding='utf-8') as f:
                calibrations = json.load(f)
            car_data = calibrations.get(cname) if isinstance(calibrations, dict) else None
            if not isinstance(car_data, dict):
                return
            self.redline = _as_int(car_data.get('redline', 0))
            self.idle = _as_int(car_data.get('idle', 0))
            if 'forward_gears' in car_data:
                self.forward_gears = max(0, _as_int(car_data.get('forward_gears', 0)))
            else:
                legacy_index = _as_int(car_data.get('max_gears', 0))
                self.forward_gears = max(0, legacy_index - (self.FIRST_FORWARD_GEAR - 1))
            self._from_calibration_file = self.is_calibrated
        except (OSError, json.JSONDecodeError, KeyError, AttributeError):
            pass

    def _apply_known_values(self, cname):
        """Ergaenzt idle/redline/forward_gears aus Tabelle und Messung

        Rangfolge, absichtlich in dieser Reihenfolge:

        1. **Die Kalibrierung des Fahrers.** Eine ausdrueckliche Aussage; sie
           wird nicht angefasst, auch nicht von einer spaeter gemessenen
           hoeheren Drehzahl. Wer selbst kalibriert hat, bekommt genau das.
        2. **Die eingebaute Tabelle** der Serienautos (``STOCK_PROFILES``) -
           damit ein nie angefasstes Auto schon in der ersten Runde schaltet.
        3. **Das gelernte Profil** aus den OutGauge-Paketen. Fuer Mods und
           fuer alles, was die Tabelle nicht kennt, ist das die einzige
           Quelle - und sie wird mit jeder Runde besser.

        Wird pro Zyklus aufgerufen, solange die Werte *nicht* aus der
        Kalibrierdatei stammen: nur so kommt eine hoehere gemessene Drehzahl
        auch an. Kosten: zwei dict-Zugriffe, der Leerlauf-Median ist
        zwischengespeichert.
        """
        if self._from_calibration_file:
            return

        # Formelwagen und das MRT5 duerfen sich nicht selbst scharfschalten:
        # dort schaltet der Fahrer (vehicles/car_profiles.py). Wer es trotzdem
        # will, kalibriert - das ist dann eine ausdrueckliche Entscheidung.
        if not automatic_gearbox_by_default(cname):
            self.idle = self.redline = self.forward_gears = 0
            return

        stock = stock_profile(cname) or {}
        idle = stock.get('idle')
        redline = stock.get('redline')
        forward_gears = stock.get('forward_gears')

        profiles = self.car_profiles
        if profiles is not None:
            if idle is None:
                idle = profiles.idle(cname)
            if redline is None:
                redline = profiles.redline(cname)
            if not forward_gears:
                forward_gears = profiles.forward_gears(cname)

        idle = _as_int(idle)
        redline = _as_int(redline)
        forward_gears = max(0, _as_int(forward_gears))

        # Dieselbe Plausibilitaetsschwelle wie in der Kalibrierung: ohne
        # Spanne zwischen Leerlauf und Redline gibt es keine Schaltpunkte,
        # und ein Auto, das noch nie hochgedreht wurde, hat schlicht noch
        # keine brauchbare Redline.
        if redline - idle < self.MIN_RPM_RANGE:
            idle = redline = 0

        was = (self.idle, self.redline, self.forward_gears)
        self.idle, self.redline, self.forward_gears = idle, redline, forward_gears
        if was != (idle, redline, forward_gears) and self.is_calibrated:
            self._log_learned_values(cname)

    def _log_learned_values(self, cname):
        """Meldet die gemessenen Werte - hoechstens eine Zeile je Intervall

        Ohne Ratenbegrenzung ist das eine Logzeile *pro Zyklus*: der gemessene
        Leerlauf wandert um ein paar Umdrehungen, und jede Aenderung zaehlte als
        Ereignis. Live gesehen waren das zwanzig Zeilen in einer Sekunde -
        genau das, was AGENTS.md §1 verbietet.
        """
        now = self.clock()
        if (self._learned_logged_at is not None
                and now - self._learned_logged_at < self.LEARNED_LOG_INTERVAL_S):
            return
        self._learned_logged_at = now
        logger.info("Gearbox values for %s without a calibration: idle %s, "
                    "redline %s, forward gears %s.",
                    car_key(cname), self.idle, self.redline, self.forward_gears)

    @property
    def is_calibrated(self) -> bool:
        return self.redline > 0 and self.idle > 0 and self.forward_gears > 0

    # ─── Sprache ──────────────────────────────────────────────────────

    def _lang(self):
        return self.settings.get('language')

    def _t(self, key):
        return self.translator.get(key, self._lang())

    def _notify(self, text: str):
        self.event_bus.emit("notification", {'notification': text})

    # ─── Kalibrierung ─────────────────────────────────────────────────

    # Schritt -> (Aufforderung, Bestaetigung). Beide Texte existieren in
    # misc/language.py; der Countdown haengt nur die Restzeit an.
    _CALIBRATION_PROMPTS = {
        0: ('Keep the rpm at idle!', 'Recording idle rpm!'),
        1: ('Rev it to the redline!', 'Recording redline!'),
        2: ('Shift into the highest gear!', 'Recording highest gear!'),
    }

    def _start_calibration(self):
        """Startet die Kalibrierung"""
        self.calibrating = True
        self._notify(self._t('Gearbox Calibration Started'))
        self._enter_step(0)

    def _enter_step(self, step: int):
        self.calibration_step = step
        self.time_in_step = self.clock()
        self._reset_step_extremes()
        prompt, _ = self._CALIBRATION_PROMPTS[step]
        # Der Nutzer sieht nur die Anzeige, und die kann hinterherhaengen.
        # Was der Schritt wirklich tut, gehoert deshalb ins Log.
        logger.info("Gearbox calibration step %d (%.0f s): %s", step,
                    self.CALIBRATION_STEP_S, prompt)

    def _abort_calibration(self, reason=""):
        """Bricht die Kalibrierung ab"""
        logger.info("Gearbox calibration aborted in step %d: %s",
                    self.calibration_step, reason or "cancelled by the user")
        self._clear_calibration_state()
        self.calibrating = False
        self.calibration_step = 0
        self.calibration_requested = False
        msg = self._t('Gearbox Calibration Aborted')
        if reason:
            msg += f' - {self._t(reason)}'
        self._notify(f'^1{msg}')

    # Mindestabstand zwischen Leerlauf und Redline, damit die Schaltpunkte
    # ueberhaupt eine Spanne haben. Der kleinste Wert unter den LFS-Serienautos
    # ist die UF1 mit gut 1000 min-1 Leerlauf und rund 6500 min-1 Abregeldrehzahl;
    # 1000 min-1 Spanne ist damit sicher unterschritten nur von einem
    # Fehlversuch, nicht von einem echten Auto.
    MIN_RPM_RANGE = 1000.0

    # Unterhalb dieser Drehzahl laeuft der Motor nicht (aus, oder er wird
    # gerade angelassen). Kein Viertakter leerlauft unter 500 min-1, und die
    # niedrigste Leerlaufdrehzahl unter den LFS-Serienautos liegt bei rund
    # 900 min-1 - 300 liegt sicher unter jedem Leerlauf und ueber einem
    # stehenden Motor.
    ENGINE_RUNNING_MIN_RPM = 300.0
    # Obergrenze fuer die Messwertliste eines Schritts (12 s * 10 Hz = 120).
    MAX_STEP_SAMPLES = 600
    # Mindestabstand zwischen zwei Meldungen der gemessenen Werte (Sekunden).
    LEARNED_LOG_INTERVAL_S = 30.0

    def _reset_step_extremes(self):
        """Setzt die Messwerte fuer den naechsten Kalibrierschritt zurueck"""
        self._step_rpm_samples = []
        self._step_max_rpm = 0.0
        self._step_max_gear = 0

    def _observe_step(self, own_vehicle: OwnVehicle):
        """Fuehrt die Messwerte des laufenden Schritts nach

        Ein Vergleich, ein Anhaengen. Laeuft nur waehrend einer Kalibrierung,
        also hoechstens 12 s * 10 Hz = 120 Werte je Schritt.
        """
        rpm = own_vehicle.rpm
        if rpm > self._step_max_rpm:
            self._step_max_rpm = rpm
        # Nur Werte mit laufendem Motor. LFS stellt einen stehenden Motor nach
        # einer Weile selbst ab, und rpm ist dann 0 - ein Minimum haette dagegen
        # keine Abwehr und hat live tatsaechlich 0 als Leerlaufdrehzahl
        # gespeichert.
        if rpm >= self.ENGINE_RUNNING_MIN_RPM:
            if len(self._step_rpm_samples) < self.MAX_STEP_SAMPLES:
                self._step_rpm_samples.append(rpm)
        gear = _as_int(own_vehicle.gear)
        if gear > self._step_max_gear:
            self._step_max_gear = gear

    def _idle_rpm(self):
        """Die Drehzahl, die im Schritt die meiste Zeit anlag - oder None

        Der Median statt des Minimums, aus zwei Gruenden: die Leerlaufregelung
        pendelt um ihren Sollwert, und der Schritt enthaelt regelmaessig
        Werte, die kein Leerlauf sind - der Anlassvorgang am Anfang, ein
        Gasstoss zwischendurch. Beides sind Minderheiten unter 120 Messwerten
        und koennen den Median nicht verschieben. ``None`` heisst: der Motor
        lief in diesem Schritt nie.
        """
        samples = self._step_rpm_samples
        if not samples:
            return None
        ordered = sorted(samples)
        return ordered[len(ordered) // 2]

    def _redline_is_plausible(self) -> bool:
        """Wurde ueberhaupt hochgedreht?

        Ohne diese Pruefung speichert die Kalibrierung stillschweigend eine
        Redline auf Leerlaufhoehe. Die Automatik schaltet danach nie oder
        dauernd, und der Nutzer hat keinen Hinweis darauf, warum.
        """
        if self.redline - self.idle >= self.MIN_RPM_RANGE:
            return True
        logger.info("Gearbox calibration: redline %s is only %s min-1 above "
                    "idle %s - the engine was never revved.",
                    self.redline, self.redline - self.idle, self.idle)
        return False

    def _publish_calibration_state(self, remaining: float):
        """Speist das eigene Anzeigefeld der Kalibrierung

        **Nicht** ueber die Meldungsschlange: die zeigt jede Meldung 3 s lang
        nacheinander, ein Schritt erzeugte fuenf davon, und der Rueckstand
        wuchs pro Schritt um 3 s. Der Fahrer las dann die Aufforderung des
        vorigen Schritts und drehte hoch, waehrend laengst der Gang gemessen
        wurde - live beobachtet als "step 2 ended - gear 1" (reference/ui.md
        §1.7). Eine zeitkritische Prozedur braucht eine Anzeige, die den
        *jetzigen* Zustand zeigt, keine Warteschlange.

        Kosten: ein dict und ein emit pro Zyklus, nur waehrend einer
        Kalibrierung. Der Text aendert sich einmal pro Sekunde, die
        Button-Registry unterdrueckt den Rest.
        """
        step = self.calibration_step
        self.event_bus.emit('gearbox_calibration_state', {
            'active': True,
            'step': step,
            'prompt': self._t(self._CALIBRATION_PROMPTS[step][0]),
            'remaining': max(0.0, remaining),
            'reading': self._step_reading(step),
        })

    def _step_reading(self, step: int) -> str:
        """Was der laufende Schritt bisher gemessen hat - live mitlesbar

        Der Fahrer sieht damit, ob seine Handlung ueberhaupt ankommt, statt
        12 s blind zu warten und erst am Ende ein Ergebnis zu bekommen.
        """
        if step == 0:
            idle_rpm = self._idle_rpm()
            if idle_rpm is None:
                return '...'
            return f'{self._t("Idle RPM set to")} {round(idle_rpm)}'
        if step == 1:
            return f'{self._t("Redline RPM set to")} {round(self._step_max_rpm)}'
        return f'{self._t("Max gear set to")} {self._gear_label(self._step_max_gear)}'

    @staticmethod
    def _gear_label(gear_index: int) -> str:
        """Gangindex in LFS' eigener Schreibweise (0 = R, 1 = N, 2 = 1.)"""
        if gear_index <= 0:
            return 'R'
        if gear_index == 1:
            return 'N'
        return str(gear_index - 1)

    def _clear_calibration_state(self):
        self.event_bus.emit('gearbox_calibration_state', {'active': False})

    def _process_calibration(self, own_vehicle: OwnVehicle):
        """Ein Kalibrierschritt pro Zyklus - drei Vergleiche im Normalfall"""
        if own_vehicle.data.speed > self.CALIBRATION_MAX_SPEED_KMH:
            logger.info("Gearbox calibration: speed %.2f km/h exceeds the "
                        "%.1f km/h standstill limit.",
                        own_vehicle.data.speed, self.CALIBRATION_MAX_SPEED_KMH)
            self._abort_calibration("Vehicle moved during calibration!")
            return
        # OutGauge folgt der Kamera: was hier aufgezeichnet wuerde, waere
        # sonst die Drehzahl eines fremden Autos (conventions.md §5.2).
        if not own_vehicle.is_local_driver:
            self._abort_calibration("Camera needs to be on own vehicle.")
            return

        # Ueber den ganzen Schritt messen, nicht im Augenblick seines Endes.
        # Genau das war der Fehler: der Nutzer wird 12 s lang aufgefordert,
        # etwas zu tun, gemessen wurde aber nur der letzte Zyklus - wer eine
        # Sekunde zu frueh vom Gas ging, bekam Leerlauf als Redline
        # gespeichert, und wer den Gang nicht bis zum Schluss hielt, bekam
        # einen Abbruch. Kosten: drei Vergleiche pro Zyklus, nur waehrend
        # einer Kalibrierung.
        self._observe_step(own_vehicle)

        elapsed = self.clock() - self.time_in_step
        if elapsed <= self.CALIBRATION_STEP_S:
            self._publish_calibration_state(self.CALIBRATION_STEP_S - elapsed)
            return

        logger.info("Gearbox calibration: step %d ended - rpm %.0f, gear %s, "
                    "throttle %.2f, speed %.2f km/h", self.calibration_step,
                    own_vehicle.rpm, own_vehicle.gear, own_vehicle.throttle,
                    own_vehicle.data.speed)

        if self.calibration_step == 0:
            idle_rpm = self._idle_rpm()
            if idle_rpm is None:
                logger.info("Gearbox calibration: the engine never ran during "
                            "the idle step (no sample at or above %.0f min-1).",
                            self.ENGINE_RUNNING_MIN_RPM)
                self._abort_calibration('Keep the rpm at idle!')
                return
            self.idle = round(idle_rpm)
            logger.info("Gearbox calibration: idle rpm %d (median of %d samples "
                        "with the engine running).",
                        self.idle, len(self._step_rpm_samples))
            self._notify(f'{self._t("Idle RPM set to")} {self.idle}')
            self._enter_step(1)
        elif self.calibration_step == 1:
            # Die *hoechste* Drehzahl des Schritts - der Nutzer muss sie nicht
            # ausgerechnet in der letzten Zehntelsekunde anliegen haben.
            self.redline = round(self._step_max_rpm)
            logger.info("Gearbox calibration: redline rpm %d (highest of the "
                        "step).", self.redline)
            if not self._redline_is_plausible():
                self._abort_calibration('Rev it to the redline!')
                return
            self._notify(f'{self._t("Redline RPM set to")} {self.redline}')
            self._enter_step(2)
        else:
            self._finish_calibration(own_vehicle)

    def _finish_calibration(self, own_vehicle: OwnVehicle):
        # Der *hoechste* waehrend des Schritts eingelegte Gang. Am Ende des
        # Schritts steht das Auto oft wieder im Leerlauf - LFS legt bei
        # Stillstand von selbst aus, und gemessen wurde bisher genau dieser
        # Augenblick (live gesehen: "step 2 ended - gear 1", also Leerlauf,
        # obwohl durchgeschaltet wurde).
        gear_index = max(self._step_max_gear, _as_int(own_vehicle.gear))
        if gear_index < self.FIRST_FORWARD_GEAR:
            logger.info("Gearbox calibration: highest gear index seen was %s, "
                        "below the first forward gear (%d) - nothing to store.",
                        gear_index, self.FIRST_FORWARD_GEAR)
            # Leerlauf oder Rueckwaerts: mit 0 Vorwaertsgaengen wuerde die
            # Automatik danach schweigend nichts mehr tun.
            self._abort_calibration('Shift into the highest gear!')
            return

        self.forward_gears = gear_index - (self.FIRST_FORWARD_GEAR - 1)
        self._from_calibration_file = True
        self._clear_calibration_state()
        self.calibrating = False
        logger.info("Gearbox calibration finished for %s: idle %s, redline %s, "
                    "forward gears %s", own_vehicle.data.cname, self.idle,
                    self.redline, self.forward_gears)
        self._notify(f'{self._t("Max gear set to")} {self.forward_gears}')
        self.save_calibrations_for_cars(own_vehicle.data.cname)
        self._notify(self._t('Gearbox Calibration Completed'))
        self._notify(f'Idle: {self.idle}, Redline: {self.redline}, Gears: {self.forward_gears}')
        self._notify(self._t('Reset possible in menu!'))
        self.car = own_vehicle.data.cname

    # ─── Schalten ─────────────────────────────────────────────────────

    def _get_smoothed_throttle(self, raw_throttle: float) -> float:
        """Glättet den Gaspedalwert über die letzten N Werte"""
        self.last_throttle_values.append(raw_throttle)
        if len(self.last_throttle_values) > self.THROTTLE_HISTORY_SIZE:
            self.last_throttle_values.pop(0)
        return sum(self.last_throttle_values) / len(self.last_throttle_values)

    def _can_shift(self, direction: str) -> bool:
        """
        Prüft ob ein Schaltvorgang erlaubt ist, basierend auf
        richtungsabhängigen Cooldowns.

        Nach einem Hochschalten ist ein Runterschalten erst nach
        COOLDOWN_AFTER_UPSHIFT erlaubt (verhindert Gear Hunting).
        """
        elapsed = self.clock() - self.time_since_last_gear_change

        if self.last_shift_direction is None:
            return elapsed > self.COOLDOWN_SAME_DIRECTION

        # Gleiche Richtung wie letzter Schaltvorgang → kurzer Cooldown
        if direction == self.last_shift_direction:
            return elapsed > self.COOLDOWN_SAME_DIRECTION

        # Gegenrichtung → längerer Cooldown gegen Hunting
        if direction == 'down' and self.last_shift_direction == 'up':
            return elapsed > self.COOLDOWN_AFTER_UPSHIFT
        if direction == 'up' and self.last_shift_direction == 'down':
            return elapsed > self.COOLDOWN_AFTER_DOWNSHIFT

        return elapsed > self.COOLDOWN_SAME_DIRECTION

    def _execute_shift(self, direction: str, own_vehicle: OwnVehicle) -> bool:
        """Führt den Schaltvorgang aus und aktualisiert Tracking

        Gibt False zurueck, wenn der InputGuard den Tastendruck verweigert -
        dann hat kein Schaltvorgang stattgefunden, also darf auch der
        Cooldown nicht neu starten.
        """
        if self.guard.may_inject(own_vehicle) is not None:
            return False

        # Tasten live aus den Einstellungen: eine im Menue neu belegte Taste
        # wirkt sofort. Frueher wurden sie in __init__ zwischengespeichert,
        # eine Neubelegung also erst nach einem Neustart wirksam.
        shift_key = self.settings.get('user_shift_up_key' if direction == 'up'
                                      else 'user_shift_down_key')
        clutch_key = self.settings.get('user_clutch_key')

        # Zeitlicher Ablauf, unveraendert gegenueber der blockierenden
        # Fassung (dort kam er aus pyautogui.PAUSE zwischen den vier Aufrufen):
        #
        #   t = 0 ms    Kupplung runter
        #   t = 100 ms  Gangtaste runter   (Kupplung ist getrennt)
        #   t = 200 ms  Gangtaste hoch
        #   t = 300 ms  Kupplung hoch
        #
        # LFS liest die Tastatur einmal pro Bild; 100 ms Haltezeit sind auch
        # bei 30 fps mehrere Bilder. Der Aufruf kehrt sofort zurueck.
        if not self.tapper.tap(clutch_key, hold_s=self.CLUTCH_HOLD_S):
            return False
        if not self.tapper.tap(shift_key, hold_s=self.SHIFT_HOLD_S,
                               delay_s=self.CLUTCH_LEAD_S):
            # Kupplung faellt von selbst wieder hoch; ohne Gangtaste hat aber
            # kein Schaltvorgang stattgefunden.
            return False
        self.time_since_last_gear_change = self.clock()
        self.last_shift_direction = direction
        return True

    def _drivetrain_is_settled(self, own_vehicle: OwnVehicle) -> bool:
        """Sind Motor und Raeder verbunden, und hat die Drehzahl sich gefangen?

        Zwei Zustaende in einem Zaehler: ``_clutch_closed_since`` ist None,
        solange die Kupplung offen ist, und traegt sonst den Zeitpunkt des
        Schliessens. Kosten pro Zyklus: ein Vergleich, im Normalfall zwei.

        Rueckgabe False heisst ausdruecklich "jetzt nicht entscheiden", nicht
        "nicht schalten": der naechste Zyklus fragt neu.
        """
        clutch = own_vehicle.clutch
        if clutch > self.CLUTCH_ENGAGED_MAX:
            self._clutch_closed_since = None
            return False
        now = self.clock()
        if self._clutch_closed_since is None:
            self._clutch_closed_since = now
            return False
        return now - self._clutch_closed_since >= self.RPM_SETTLE_S

    def _process_shifting(self, own_vehicle: OwnVehicle):
        """
        Hauptlogik für das Schalten mit Hysterese.

        Die Upshift-Schwelle liegt deutlich höher als die Downshift-Schwelle.
        Dadurch entsteht eine "tote Zone" in der Mitte, in der kein
        Schaltvorgang ausgelöst wird. Das verhindert Gear Hunting:

            idle ─────[downshift]────────────[upshift]───── redline
                          ↑                      ↑
                     niedrig (15-35%)       hoch (50-92%)
                     je nach Throttle       je nach Throttle

        Zusätzlich sorgen richtungsabhängige Cooldowns dafür, dass nach
        einem Hochschalten nicht sofort zurückgeschaltet wird.
        """
        current_gear = own_vehicle.gear
        current_rpm = own_vehicle.rpm
        current_brake = own_vehicle.brake
        throttle = self._get_smoothed_throttle(own_vehicle.throttle)

        rpm_range = self.redline - self.idle
        if rpm_range <= 0:
            return

        # Ohne geschlossenen Antriebsstrang sagt die Drehzahl nichts ueber
        # den Gang aus (known-issues #47, Herleitung bei CLUTCH_ENGAGED_MAX).
        # Die Kupplung kommt aus OutGauge, umfasst also die eigene genauso
        # wie die des Fahrers und die von LFS' Autokupplung.
        if not self._drivetrain_is_settled(own_vehicle):
            return

        # Rohindex des hoechsten Gangs (forward_gears zaehlt Vorwaertsgaenge).
        top_gear_index = self.forward_gears + (self.FIRST_FORWARD_GEAR - 1)

        # ── Upshift-Schwelle (gaspedalabhängig) ──
        # Vollgas → schalte spät (nahe Redline)
        # Wenig Gas → schalte früh (Komfort-Modus)
        upshift_rpm = self.idle + rpm_range * (
            self.UPSHIFT_BASE + self.UPSHIFT_THROTTLE_SCALE * throttle
        )

        # ── Downshift-Schwelle (gaspedalabhängig, deutlich tiefer) ──
        # Die große Lücke zwischen Upshift und Downshift ist der
        # Kern der Anti-Hunting-Strategie
        downshift_rpm = self.idle + rpm_range * (
            self.DOWNSHIFT_BASE + self.DOWNSHIFT_THROTTLE_SCALE * throttle
        )

        # ── Hochschalten ──
        if (current_gear >= self.FIRST_FORWARD_GEAR   # mindestens im 1. Vorwärtsgang
                and current_gear < top_gear_index     # nicht über den höchsten Gang
                and throttle > self.MIN_THROTTLE_FOR_UPSHIFT
                and current_brake <= self.MAX_BRAKE_FOR_UPSHIFT
                and current_rpm > upshift_rpm
                and self._can_shift('up')):
            self._execute_shift('up', own_vehicle)

        # ── Runterschalten ──
        elif (current_gear > self.FIRST_FORWARD_GEAR  # nicht tiefer als 1. Gang
                and current_rpm < downshift_rpm
                and (throttle > 0.05 or current_brake > 0.05)
                and self._can_shift('down')):
            self._execute_shift('down', own_vehicle)

    # ─── Verfuegbarkeit ───────────────────────────────────────────────

    # Interne Gruende, die das Menue uebersetzt (ui/menu_system.py). Genau wie
    # beim Bremseingriff steht hier nur der Schluessel, nicht der Fahrertext.
    REASON_LFS_AUTO_GEARS = 'lfs_auto_gears'
    REASON_CAR_NOT_SUPPORTED = 'car_not_supported'
    REASON_NOT_CALIBRATED = 'not_calibrated'

    @staticmethod
    def lfs_shifts_by_itself(own_vehicle: OwnVehicle) -> bool:
        """Schaltet LFS' *eigenes* Automatikgetriebe gerade mit?

        ``PIF_AUTOGEARS`` aus IS_NPL/IS_PFL (``vehicles/vehicle.py``). Ist es
        gesetzt, schaltet LFS selbst - und zwei Automatiken auf derselben
        Kurbelwelle schalten gegeneinander: unsere Automatik tippt die
        Gangtaste, LFS schaltet im selben Augenblick noch einmal, und das
        Ergebnis ist ein Gang, den keiner der beiden wollte
        (``reference/systems.md``).

        Konservativ in beide Richtungen:

        * Das Flag beschreibt den *lokalen Fahrer*. Zeigt die Kamera gerade
          auf ein fremdes Auto, sagen die Daten nichts ueber uns aus - dann
          gilt weiter, was zuletzt fuer uns galt, und die Frage wird nicht
          neu beantwortet (``conventions.md`` §5.2). Das ist hier ohne
          Bedeutung, weil ``_execute_shift`` in dem Fall ohnehin nicht
          tippt, kostet aber nichts.
        * Kam noch kein IS_NPL, ist ``player_flags`` 0. Dann ist die Antwort
          "nein" - also das bisherige Verhalten. Eine unbekannte Antwort darf
          keine Funktion abschalten.
        """
        return bool(getattr(own_vehicle.data, 'lfs_auto_gears', False))

    def _publish_availability(self, reason):
        """Sagt, ob die Automatik wirklich schaltet - und wenn nicht, warum

        Ein Event je *Wechsel*. ``None`` geht mit raus, sonst bliebe ein
        behobener Grund fuer immer im Menue stehen.
        """
        if self._reported_reason == reason:
            return
        previous = self._reported_reason
        self._reported_reason = reason
        self.event_bus.emit('gearbox_availability', {'reason': reason})
        if reason is None:
            if previous is not None:
                logger.info("Automatic gearbox is shifting again.")
            return
        if reason == self.REASON_LFS_AUTO_GEARS:
            logger.info("Automatic gearbox switched itself off: LFS' own "
                        "automatic gearbox is active (PIF_AUTOGEARS). Two "
                        "gearboxes on one car shift against each other.")
            self._notify('^3' + self._t('LFS shifts by itself'))

    # ─── Zyklus ───────────────────────────────────────────────────────

    def process(self, own_vehicle: OwnVehicle, vehicles: Dict[int, Vehicle]) -> Dict[str, Any]:
        """Verarbeitet die Auto-Gearbox-Logik

        Kosten pro Zyklus: unveraendert eine Handvoll Vergleiche. Der
        InputGuard wird nur befragt, wenn wirklich geschaltet wuerde; der
        Kalibrier-Countdown laeuft nur waehrend der Kalibrierung.
        """
        if not self.is_enabled():
            # Abgeschaltet heisst abgeschaltet - kein Grund, den das Menue
            # als Stoerung anzeigen muesste.
            self._publish_availability(None)
            return {'auto_gearbox_active': False}

        # Fahrzeuge, deren Getriebe diese Logik nicht beschreibt. Vor allem
        # anderen geprueft, damit auch eine vorhandene Kalibrierung sie nicht
        # scharfschaltet (vehicles/car_profiles.py, NEVER_AUTOMATIC).
        if not automatic_gearbox_allowed(own_vehicle.data.cname):
            if self.calibration_requested:
                self.calibration_requested = False
                self._notify('^1' + self._t('Automatic Gearbox not available'))
            if self.calibrating:
                self._abort_calibration()
            self._publish_availability(self.REASON_CAR_NOT_SUPPORTED)
            return {'auto_gearbox_active': False}

        # LFS schaltet selbst. Dann haelt sich diese Automatik heraus - noch
        # vor allem Weiteren, damit weder eine Kalibrierung noch ein
        # Schaltvorgang beginnt. Gemessen (known-issues #47, Szenario
        # 15_gearbox_tests): zwei Automatiken auf einem Auto ergeben
        # Schaltsalven, eine dauernd offene Kupplung und weniger
        # Beschleunigung als jede der beiden allein.
        if self.lfs_shifts_by_itself(own_vehicle):
            if self.calibration_requested:
                self.calibration_requested = False
            if self.calibrating:
                self._abort_calibration('LFS shifts by itself')
            self._publish_availability(self.REASON_LFS_AUTO_GEARS)
            return {'auto_gearbox_active': False,
                    'suppressed_by': self.REASON_LFS_AUTO_GEARS}

        # Kalibrierung laden wenn das Fahrzeug wechselt
        if self.car != own_vehicle.data.cname:
            if not self.calibrating:
                self.load_calibrations_for_cars(own_vehicle.data.cname)
                self.car = own_vehicle.data.cname

        # Ohne eigene Kalibrierung: Tabelle und laufende Messung nachziehen.
        if not self.calibrating:
            self._apply_known_values(own_vehicle.data.cname)

        # Menuebefehl: startet die Kalibrierung - oder bricht sie ab.
        if self.calibration_requested:
            self.calibration_requested = False
            if self.calibrating:
                self._abort_calibration()
            elif own_vehicle.data.speed > self.CALIBRATION_MAX_SPEED_KMH:
                self._notify('^1' + self._t('Vehicle must be stationary to calibrate!'))
            else:
                self._start_calibration()

        if self.calibrating:
            self._process_calibration(own_vehicle)
            self._publish_availability(None)
        elif self.is_calibrated:
            self._process_shifting(own_vehicle)
            self._publish_availability(None)
        else:
            # Eingeschaltet, erlaubtes Auto - aber ohne Leerlauf, Redline und
            # Gangzahl gibt es keine Schaltpunkte. Bisher schwieg die
            # Automatik hier einfach.
            self._publish_availability(self.REASON_NOT_CALIBRATED)
            return {'auto_gearbox_active': False,
                    'suppressed_by': self.REASON_NOT_CALIBRATED}

        return {'auto_gearbox_active': True}

    def shutdown(self):
        """Kupplung oder Gangtaste duerfen den Prozess nicht ueberleben

        Der KeyTapper ist prozessweit geteilt und ``release_all()`` idempotent
        (reference/control-intervention.md §1, Fail-Safe).
        """
        self.tapper.release_all()
