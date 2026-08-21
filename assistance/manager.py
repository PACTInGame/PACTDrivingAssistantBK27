import logging
from typing import Dict, Optional

from assistance.AI_Driver import AIDriver
from assistance.adaptive_lights import LightAssists
from assistance.auto_hold import AutoHold
from assistance.base_system import AssistanceSystem
from assistance.blind_spot_warning import BlindSpotWarning
from assistance.chat_commands import ChatCommandHandler
from assistance.collision_warning import ForwardCollisionWarning
from assistance.cross_traffic_warning import CrossTrafficWarning
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


class AssistanceManager:
    """Verwaltet alle Fahrerassistenzsysteme"""

    def __init__(self, event_bus: EventBus, settings: SettingsManager,
                 error_throttle: ErrorThrottle = None):
        self.event_bus = event_bus
        self.settings = settings
        self._errors = error_throttle or ErrorThrottle(logger)
        # Systeme, die sich nach wiederholten Fehlern selbst deaktiviert haben.
        self.failed_systems = set()
        self.systems: Dict[str, AssistanceSystem] = {}
        self.own_vehicle: Optional[OwnVehicle] = None
        self.vehicles: Dict[int, Vehicle] = {}
        self.on_track = False

        # Event-Handler
        self.event_bus.subscribe('own_vehicle_updated', self._on_own_vehicle_updated)
        self.event_bus.subscribe('vehicles_updated', self._on_vehicles_updated)
        self.event_bus.subscribe('state_data', self._update_state_data)

        # Systeme initialisieren
        self._init_systems()

    def _init_systems(self):
        """Initialisiert alle Assistenzsysteme"""
        self.systems['fcw'] = ForwardCollisionWarning(self.event_bus, self.settings)
        self.systems['bsw'] = BlindSpotWarning(self.event_bus, self.settings)
        self.systems['pdc'] = ParkDistanceControl(self.event_bus, self.settings)
        self.systems['autoh'] = AutoHold(self.event_bus, self.settings)
        self.systems['lighta'] = LightAssists(self.event_bus, self.settings)
        # This will be used again once all control inputs are supported
        #self.systems['controller_emulator'] = ControllerEmulator(self.event_bus, self.settings)
        self.systems['gearbox'] = Gearbox(self.event_bus, self.settings)
        self.systems['ctw'] = CrossTrafficWarning(self.event_bus, self.settings)
        self.systems['ai_traffic'] = AIDriver(self.event_bus, self.settings)
        # NavigationSystem ist bewusst *nicht* registriert: es gibt keinen
        # Einstellungsschluessel 'sat_nav', also lief es nie - es kostete nur
        # zwei Event-Abonnements und einen is_enabled()-Aufruf pro Zyklus
        # (known-issues #18). Ueber seine Zukunft entscheidet WP10; bevor es
        # zurueckkommt, muessen die print()-Aufrufe und die O(n)-Suche pro
        # Zyklus raus (known-issues #5).

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
        if self.on_track:
            for name, system in self.systems.items():
                if name in self.failed_systems:
                    continue
                if not system.is_enabled():
                    continue
                # Fehler-Isolation pro System: ein defektes System deaktiviert
                # sich selbst, statt den gemeinsamen 100-ms-Thread zu killen.
                # Kosten im Gutfall: null.
                try:
                    result = system.process(self.own_vehicle, self.vehicles)
                except Exception as e:
                    self._handle_system_failure(name, e)
                else:
                    results[name] = result
                    self._errors.succeeded(name)

        self.event_bus.emit('assistance_results', results)
        # Check for periodic tooltip messages
        try:
            self.chat_commands.check_tooltip()
        except Exception as e:
            self._errors.report('chat_commands.check_tooltip', e)
        return results

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
