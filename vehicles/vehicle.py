import time
from dataclasses import dataclass
from typing import Optional
import math


@dataclass
class VehicleData:
    """Container für Fahrzeugdaten"""
    player_id: int = 0
    x: float = 0.0
    y: float = 0.0
    z: float = 0.0
    speed: float = 0.0
    heading: float = 0.0
    direction: float = 0.0
    distance_to_player: float = 0.0
    acceleration: float = 0.0


class Vehicle:
    """Repräsentiert ein Fahrzeug auf der Strecke"""

    def __init__(self, player_id: int):
        self.data = VehicleData(player_id=player_id)
        self.last_update = 0
        self.previous_speed = 0.0

    def update_position(self, x: float, y: float, z: float, heading: float,
                        direction: float, speed: float):
        """Aktualisiert Position und Bewegungsdaten"""
        self.data.x = x
        self.data.y = y
        self.data.z = z
        self.data.heading = heading
        self.data.direction = direction

        # Berechne Beschleunigung
        self.data.speed = speed
        self.data.acceleration = (speed - self.previous_speed) * 2.778  # Umrechnung von km/h auf m/s²
        self.previous_speed = self.data.speed

    def update_distance_to_player(self, player_x: float, player_y: float, player_z: float):
        """Berechnet Distanz zum Spieler"""
        dx = self.data.x - player_x
        dy = self.data.y - player_y
        dz = self.data.z - player_z
        dx = dx / 65536
        dy = dy / 65536
        dz = dz / 65536
        self.data.distance_to_player = math.sqrt(dx * dx + dy * dy + dz * dz)