import logging
import time
from typing import Dict, Optional

from assistance.AI_Driver import AIDriver
from assistance.adaptive_lights import LightAssists
from assistance.auto_hold import AutoHold
from assistance.base_system import AssistanceSystem
from assistance.blind_spot_warning import BlindSpotWarning
from assistance.chat_commands import ChatCommandHandler
from assistance.collision_warning import ForwardCollisionWarning
from assistance.cross_traffic_warning import CrossTrafficWarning
from assistance.emergency_brake import EmergencyBrake
from assistance.gearbox import Gearbox
from assistance.park_distance_control import ParkDistanceControl
from core.event_bus import EventBus
from core.settings_manager import SettingsManager
from misc.logging_setup import ErrorThrottle
from vehicles.own_vehicle import OwnVehicle
from vehicles.vehicle import Vehicle

logger = logging.getLogger(__name__)

# Ein System, das k Zyklen hintereinander wirft, schaltet sich selbst ab.
# Vorher riss die erste Exception den 100-ms-Thread mit - alle folgenden
# Systeme liefen danach nie wieder, ohne jede Meldung.
MAX_CONSECUTIVE_FAILURES = 5

# Ein Durchlauf, der laenger als dieser Anteil seines Intervalls braucht, nennt
# beim naechsten Mal die teuersten Systeme. ThreadManager meldet *dass* das
# Budget gerissen wurde, aber nicht von wem - und ohne diese Zuordnung ist ein
# Overrun eine Ratesession (die 203 ms aus known-issues #43 waren AutoHold,
# gefunden erst durch Nachmessen von Hand).
BUDGET_WARN_FRACTION = 1.0
# Hoechstens eine Aufschluesselung pro so vielen Sekunden.
SLOW_PASS_LOG_INTERVAL_S = 10.0
# So viele der teuersten Systeme werden genannt.
SLOW_PASS_TOP_N = 3


class AssistanceManager:
    """Verwaltet alle Fahrerassistenzsysteme"""

    def __init__(self, event_bus: EventBus, settings: SettingsManager,
                 error_throttle: ErrorThrottle = None, car_profiles=None,
                 pedals=None):
        self.event_bus = event_bus
        self.settings = settings
        # Durchgereicht an die Systeme, die fahrzeugspezifische Werte brauchen
        # (bisher nur der Gearbox). Optional, damit Tests ohne auskommen.
        self.car_profiles = car_profiles
        # Die Pedalerkennung gehoert ``main.py``, weil sie auf dem Mainthread
        # gepumpt werden muss (misc/pedal_watch.py). Hier nur durchgereicht.
        self.pedals = pedals
        self._errors = error_throttle or ErrorThrottle(logger)
        # Systeme, die sich nach wiederholten Fehlern selbst deaktiviert haben.
        self.failed_systems = set()
        self.systems: Dict[str, AssistanceSystem] = {}
        self.own_vehicle: Optional[OwnVehicle] = None
        self.vehicles: Dict[int, Vehicle] = {}
        self.on_track = False
        # Laufzeit des letzten Durchlaufs je System, fuer _report_slow_pass.
        self._durations: Dict[str, float] = {}
        self._last_slow_log: Optional[float] = None

        # Event-Handler
        self.event_bus.subscribe('own_vehicle_updated', self._on_own_vehicle_updated)
        self.event_bus.subscribe('vehicles_updated', self._on_vehicles_updated)
        self.event_bus.subscribe('state_data', self._update_state_data)

        # Systeme initialisieren
        self._init_systems()

    def _init_systems(self):
        """Initialisiert alle Assistenzsysteme"""
        # ─── Reihenfolge ist Vertrag, nicht Geschmack ─────────────────
        # ``systems`` ist ein dict und wird in Einfuegereihenfolge
        # durchlaufen. Drei Systeme veroeffentlichen
        # ``needed_deceleration_update``: FCW, BSW und CTW. ``EmergencyBrake``
        # sammelt sie und leert die Sammlung am Ende jedes eigenen
        # Durchlaufs - genau daran erkennt es eine Quelle, die *nichts* mehr
        # sendet (abgeschaltet, selbst deaktiviert, Strecke verlassen) und
        # unterscheidet sie von einer, die 0 sendet.
        #
        # Deshalb muss der Bremseingriff **hinter allen dreien** stehen.
        # Vorher stand er direkt hinter FCW, damit dessen Anforderung im
        # selben Zyklus wirkt; genau dieselbe Begruendung verlangt jetzt den
        # letzten Platz unter den warnenden Systemen, sonst waeren Quer- und
        # Toter-Winkel-Anforderung einen Zyklus (100 ms, bei 50 km/h 1.4 m)
        # alt.
        self.systems['fcw'] = ForwardCollisionWarning(self.event_bus, self.settings)
        self.systems['bsw'] = BlindSpotWarning(self.event_bus, self.settings)
        self.systems['ctw'] = CrossTrafficWarning(self.event_bus, self.settings)
        self.systems['aeb'] = EmergencyBrake(self.event_bus, self.settings,
                                            pedals=self.pedals)
        self.systems['pdc'] = ParkDistanceControl(self.event_bus, self.settings)
        self.systems['autoh'] = AutoHold(self.event_bus, self.settings)
        self.systems['lighta'] = LightAssists(self.event_bus, self.settings)
        self.systems['gearbox'] = Gearbox(self.event_bus, self.settings,
                                          car_profiles=self.car_profiles)
        self.systems['ai_traffic'] = AIDriver(self.event_bus, self.settings)
        # NavigationSystem gibt es nicht mehr: es hatte nie einen
        # Einstellungsschluessel, lief also nie, und war in der vorliegenden
        # Form ohnehin nicht einsetzbar. Was ein neuer Entwurf anders machen
        # muss, steht in reference/systems.md; der alte Code steht in der
        # Git-Historie.

        # Chat-Command Handler (event-basiert, kein process()-System)
        self.chat_commands = ChatCommandHandler(self.event_bus, self.settings)

        # Weitere Systeme hier hinzufügen

    def _update_state_data(self, data):
        self.on_track = data.get('on_track', False)

    def _on_own_vehicle_updated(self, own_vehicle: OwnVehicle):
        """Updates own vehicle data"""
        self.own_vehicle = own_vehicle

    def _on_vehicles_updated(self, vehicles: Dict[int, Vehicle]):
        """Updates vehicle data"""
        self.vehicles = vehicles

    def process_all_systems(self):
        """Verarbeitet alle Assistenzsysteme"""
        if not self.own_vehicle:
            return

        results = {}
        # Einen veroeffentlichten Stand fuer den ganzen Durchlauf festhalten.
        own_vehicle, vehicles = self.own_vehicle, self.vehicles
        if self.on_track:
            # Ein perf_counter-Paar pro System, ~100 ns - unter dem Rauschen
            # eines 100-ms-Budgets, und die einzige Moeglichkeit, einen
            # Overrun einem Verursacher zuzuordnen.
            started = time.perf_counter()
            self._durations.clear()
            for name, system in self.systems.items():
                if name in self.failed_systems:
                    continue
                if not system.is_enabled():
                    continue
                # Fehler-Isolation pro System: ein defektes System deaktiviert
                # sich selbst, statt den gemeinsamen 100-ms-Thread zu killen.
                # Kosten im Gutfall: null.
                system_started = time.perf_counter()
                try:
                    result = system.process(own_vehicle, vehicles)
                except Exception as e:
                    self._handle_system_failure(name, e)
                else:
                    results[name] = result
                    self._errors.succeeded(name)
                finally:
                    self._durations[name] = time.perf_counter() - system_started
            self._report_slow_pass(time.perf_counter() - started)

        # Check for periodic tooltip messages
        try:
            self.chat_commands.check_tooltip()
        except Exception as e:
            self._errors.report('chat_commands.check_tooltip', e)
        return results

    def _report_slow_pass(self, elapsed_s: float):
        """Nennt die teuersten Systeme, wenn der Durchlauf sein Budget riss

        Ratenbegrenzt: ein dauerhaft zu langsames System soll eine Zeile pro
        10 s erzeugen, nicht eine pro Zyklus.
        """
        budget_ms = self.settings.get('assistance_refresh_rate')
        elapsed_ms = elapsed_s * 1000.0
        if elapsed_ms <= budget_ms * BUDGET_WARN_FRACTION:
            return

        now = time.monotonic()
        if self._last_slow_log is not None and                 now - self._last_slow_log < SLOW_PASS_LOG_INTERVAL_S:
            return
        self._last_slow_log = now

        worst = sorted(self._durations.items(), key=lambda item: -item[1])
        breakdown = ", ".join(f"{name} {seconds * 1000:.1f} ms"
                              for name, seconds in worst[:SLOW_PASS_TOP_N])
        logger.warning("Assistance pass took %.1f ms of a %d ms budget - "
                       "slowest: %s", elapsed_ms, budget_ms, breakdown)

    def _handle_system_failure(self, name: str, exc: Exception):
        """Meldet einen Systemfehler ratenbegrenzt und deaktiviert Dauerfehler"""
        consecutive = self._errors.report(name, exc, context='in process()')
        if consecutive < MAX_CONSECUTIVE_FAILURES:
            return
        self.failed_systems.add(name)
        logger.error("Assistance system '%s' disabled after %d consecutive failures.",
                     name, consecutive)
        self.event_bus.emit('notification',
                            {'notification': f"^1{name.upper()} disabled - see log"})

    def shutdown(self):
        """Gibt jedem System die Gelegenheit, seine Aussenwirkung zurueckzunehmen

        Aufzurufen, *nachdem* die Worker-Threads stehen: ein System, das noch
        eine Taste haelt oder eine LFS-Achse umgebogen hat, muss das rueckgaengig
        machen koennen, bevor der Prozess endet (reference/control-intervention.md
        §1, Fail-Safe). Ein Fehler in einem System darf die uebrigen nicht
        ueberspringen.
        """
        for name, system in self.systems.items():
            handler = getattr(system, 'shutdown', None)
            if handler is None:
                continue
            try:
                handler()
            except Exception as e:
                logger.warning("Shutdown of '%s' failed: %s: %s",
                               name, type(e).__name__, e)

    def get_system(self, name: str) -> Optional[AssistanceSystem]:
        """Gibt ein spezifisches Assistenzsystem zurück"""
        return self.systems.get(name)

    def enable_system(self, name: str, enabled: bool):
        """Aktiviert/deaktiviert ein System"""
        if name in self.systems:
            self.systems[name].enabled = enabled
            if enabled:
                # Manuelles Wiedereinschalten hebt eine Selbst-Deaktivierung auf.
                self.failed_systems.discard(name)
                self._errors.forget(name)
