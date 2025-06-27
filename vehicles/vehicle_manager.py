from typing import Dict, List, Any, Optional

from core.event_bus import EventBus
from vehicles.own_vehicle import OwnVehicle
from vehicles.vehicle import Vehicle


class VehicleManager:
    """Verwaltet alle Fahrzeuge auf der Strecke"""

    def __init__(self, event_bus: EventBus):
        self.event_bus = event_bus
        self.vehicles: Dict[int, Vehicle] = {}
        self.own_vehicle = OwnVehicle()
        self.players: Dict[int, Any] = {}  # Player info from NPL packets

        # Event-Handler registrieren
        self.event_bus.subscribe('vehicle_data_received', self._handle_vehicle_data)
        self.event_bus.subscribe('player_joined', self._handle_player_joined)
        self.event_bus.subscribe('player_left', self._handle_player_left)
        self.event_bus.subscribe('outgauge_data', self._handle_outgauge_data)

    def _handle_vehicle_data(self, mci_packet):
        """Verarbeitet MCI-Pakete mit Fahrzeugdaten"""
        for data in mci_packet.Info:
            player_id = data.PLID

            # Erstelle Fahrzeug falls nicht vorhanden
            if player_id not in self.vehicles:
                self.vehicles[player_id] = Vehicle(player_id)

            # Aktualisiere Fahrzeugdaten
            vehicle = self.vehicles[player_id]
            vehicle.update_position(
                data.X, data.Y, data.Z,
                data.Heading, data.Direction,
                data.Speed / 91.02  # Convert to km/h
            )

            # Aktualisiere Distanz zum eigenen Fahrzeug
            if self.own_vehicle.data.player_id != 0:
                vehicle.update_distance_to_player(
                    self.own_vehicle.data.x,
                    self.own_vehicle.data.y,
                    self.own_vehicle.data.z
                )

        # Emit Event für andere Komponenten
        self.event_bus.emit('vehicles_updated', self.vehicles)

    def _handle_player_joined(self, npl_packet):
        """Verarbeitet neue Spieler"""
        self.players[npl_packet.PLID] = npl_packet
        self.event_bus.emit('player_data_updated', self.players)

    def _handle_player_left(self, pll_packet):
        """Entfernt Spieler"""
        player_id = pll_packet.PLID
        if player_id in self.players:
            del self.players[player_id]
        if player_id in self.vehicles:
            del self.vehicles[player_id]
        self.event_bus.emit('player_data_updated', self.players)

    def _handle_outgauge_data(self, outgauge_packet):
        """Verarbeitet OutGauge-Daten für eigenes Fahrzeug"""
        self.own_vehicle.update_outgauge_data(outgauge_packet)
        self.event_bus.emit('own_vehicle_updated', self.own_vehicle)

    def get_nearby_vehicles(self, max_distance: float = 100.0) -> List[Vehicle]:
        """Gibt nahegelegene Fahrzeuge zurück"""
        return [v for v in self.vehicles.values()
                if v.data.distance_to_player <= max_distance]

    def get_vehicle_by_id(self, player_id: int) -> Optional[Vehicle]:
        """Gibt Fahrzeug anhand der Player-ID zurück"""
        return self.vehicles.get(player_id)