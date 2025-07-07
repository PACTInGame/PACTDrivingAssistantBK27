from typing import Dict, Optional

from assistance.base_system import AssistanceSystem
from assistance.blind_spot_warning import BlindSpotWarning
from assistance.collision_warning import ForwardCollisionWarning
from core.event_bus import EventBus
from core.settings_manager import SettingsManager
from vehicles.own_vehicle import OwnVehicle
from vehicles.vehicle import Vehicle


class AssistanceManager:
    """Verwaltet alle Fahrerassistenzsysteme"""

    def __init__(self, event_bus: EventBus, settings: SettingsManager):
        self.event_bus = event_bus
        self.settings = settings
        self.systems: Dict[str, AssistanceSystem] = {}
        self.own_vehicle: Optional[OwnVehicle] = None
        self.vehicles: Dict[int, Vehicle] = {}

        # Event-Handler
        self.event_bus.subscribe('own_vehicle_updated', self._on_own_vehicle_updated)
        self.event_bus.subscribe('vehicles_updated', self._on_vehicles_updated)

        # Systeme initialisieren
        self._init_systems()

    def _init_systems(self):
        """Initialisiert alle Assistenzsysteme"""
        self.systems['fcw'] = ForwardCollisionWarning(self.event_bus, self.settings)
        self.systems['bsw'] = BlindSpotWarning(self.event_bus, self.settings)
        # Weitere Systeme hier hinzufügen

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
        for name, system in self.systems.items():
            if system.is_enabled():
                #try:
                    result = system.process(self.own_vehicle, self.vehicles)
                    results[name] = result
                #except Exception as e:
                   # print(f"Error in assistance system {name}: {e}")

        self.event_bus.emit('assistance_results', results)
        return results

    def get_system(self, name: str) -> Optional[AssistanceSystem]:
        """Gibt ein spezifisches Assistenzsystem zurück"""
        return self.systems.get(name)

    def enable_system(self, name: str, enabled: bool):
        """Aktiviert/deaktiviert ein System"""
        if name in self.systems:
            self.systems[name].enabled = enabled