import math
from typing import Dict, Any, Optional, Tuple

from assistance.base_system import AssistanceSystem
from core.event_bus import EventBus
from core.settings_manager import SettingsManager
from vehicles.own_vehicle import OwnVehicle
from vehicles.vehicle import Vehicle


def _direction_vector(heading: float) -> Tuple[float, float]:
    """Gibt den normierten Richtungsvektor basierend auf dem LFS-Heading zurück.

    Nutzt die bewährte Konvertierung aus der Codebase:
      angle_deg = (heading + 16384) / 182.05

    Im LFS-Koordinatensystem: X wächst nach Osten, Y wächst nach Süden.
    Heading 0 = Süd, 16384 = West, 32768 = Nord, 49152 = Ost (clockwise).
    """
    angle_deg = (heading + 16384) / 182.05
    rad = math.radians(angle_deg)
    return math.cos(rad), math.sin(rad)


def _find_intersection(
    x1: float, y1: float, dx1: float, dy1: float,
    x2: float, y2: float, dx2: float, dy2: float
) -> Optional[Tuple[float, float, float, float]]:
    """Findet den Schnittpunkt zweier Strahlen (Fahrwege).

    Strahl 1: P1 + t1 * D1
    Strahl 2: P2 + t2 * D2

    Returns:
        (t1, t2, ix, iy) wobei t1/t2 die Parameter sind und ix/iy der Schnittpunkt,
        oder None wenn die Strahlen parallel sind oder der Schnittpunkt hinter den Fahrzeugen liegt.
    """
    # Kreuzprodukt D1 x D2 (2D: dx1*dy2 - dy1*dx2)
    cross = dx1 * dy2 - dy1 * dx2

    if abs(cross) < 1e-9:
        # Strahlen sind (nahezu) parallel – kein Querverkehr
        return None

    # Differenzvektor P2 - P1
    diffx = x2 - x1
    diffy = y2 - y1

    t1 = (diffx * dy2 - diffy * dx2) / cross
    t2 = (diffx * dy1 - diffy * dx1) / cross

    # Schnittpunkt muss VOR beiden Fahrzeugen liegen (t > 0)
    if t1 <= 0 or t2 <= 0:
        return None

    ix = x1 + t1 * dx1
    iy = y1 + t1 * dy1

    return t1, t2, ix, iy


def _compute_side(own_dx: float, own_dy: float, own_x: float, own_y: float,
                  other_x: float, other_y: float) -> str:
    """Bestimmt, ob das andere Fahrzeug von links oder rechts kommt.

    Nutzt das Kreuzprodukt des eigenen Richtungsvektors mit dem Vektor
    vom eigenen Fahrzeug zum anderen Fahrzeug.

    ACHTUNG: Im LFS-Koordinatensystem wächst Y nach Süden (linkshändig).
    Dadurch ist das Vorzeichen des 2D-Kreuzprodukts gegenüber dem
    Standard-Mathe-System invertiert.

    Returns:
        'left' oder 'right'
    """
    # Vektor zum anderen Fahrzeug
    to_other_x = other_x - own_x
    to_other_y = other_y - own_y

    # 2D Kreuzprodukt: own_dir × to_other
    cross = own_dx * to_other_y - own_dy * to_other_x

    # Im LFS-KS (Y nach unten/Süden): Negativ = links, Positiv = rechts
    return 'right' if cross < 0 else 'left'


class CrossTrafficWarning(AssistanceSystem):
    """Querverkehrswarnung – warnt vor kreuzenden Fahrzeugen"""

    # Maximale Distanz zum Schnittpunkt (in Metern) für Berücksichtigung
    MAX_INTERSECTION_DISTANCE = 100.0
    # Toleranz für gleichzeitige Ankunft am Schnittpunkt (Sekunden)
    ARRIVAL_TIME_TOLERANCE = 0.5
    # Minimaler Kreuzungswinkel (Grad) um nahezu parallele Fahrzeuge auszuschließen
    MIN_CROSSING_ANGLE_DEG = 20.0

    def __init__(self, event_bus: EventBus, settings: SettingsManager):
        super().__init__("cross_traffic_warning", event_bus, settings)
        self.current_warning_level = 0
        self.current_side = None  # 'left' oder 'right'

    def process(self, own_vehicle: OwnVehicle, vehicles: Dict[int, Vehicle]) -> Dict[str, Any]:
        """Prüft auf Querverkehr-Kollisionsgefahr"""
        warning_level = 0
        warning_side = None
        min_ttc = float('inf')

        if not self.is_enabled() or own_vehicle.data.speed < 5 or own_vehicle.gear <= 1:
            self._emit_if_changed(warning_level, warning_side)
            return {'level': 0, 'side': None, 'ttc': float('inf')}

        # Eigene Position in Metern (LFS nutzt 1/65536 Meter)
        own_x = own_vehicle.data.x / 65536.0
        own_y = own_vehicle.data.y / 65536.0
        own_speed_ms = own_vehicle.data.speed * 0.277778  # km/h -> m/s

        # Eigener Richtungsvektor
        own_dx, own_dy = _direction_vector(own_vehicle.data.heading)

        for vehicle in vehicles.values():
            if vehicle.data.speed < 3:
                # Stehendes/sehr langsames Fahrzeug ignorieren
                continue

            other_x = vehicle.data.x / 65536.0
            other_y = vehicle.data.y / 65536.0
            other_speed_ms = vehicle.data.speed * 0.277778
            other_dx, other_dy = _direction_vector(vehicle.data.heading)

            # Kreuzungswinkel prüfen (parallele Fahrzeuge ausschließen)
            dot = own_dx * other_dx + own_dy * other_dy
            dot = max(-1.0, min(1.0, dot))  # Clamp für acos
            crossing_angle_deg = math.degrees(math.acos(abs(dot)))
            if crossing_angle_deg < self.MIN_CROSSING_ANGLE_DEG:
                # Fast parallel/gleiche Richtung – kein Querverkehr
                continue

            # Schnittpunkt der beiden Fahrwege berechnen
            result = _find_intersection(
                own_x, own_y, own_dx, own_dy,
                other_x, other_y, other_dx, other_dy
            )
            if result is None:
                continue

            t1, t2, ix, iy = result

            # t1 und t2 sind die Distanzen zum Schnittpunkt (da Richtungsvektoren normiert sind)
            dist_own = t1  # Meter
            dist_other = t2  # Meter

            # Nur Schnittpunkte innerhalb von MAX_INTERSECTION_DISTANCE berücksichtigen
            if dist_own > self.MAX_INTERSECTION_DISTANCE or dist_other > self.MAX_INTERSECTION_DISTANCE:
                continue

            # Zeit bis zum Schnittpunkt
            time_own = dist_own / own_speed_ms if own_speed_ms > 0.1 else float('inf')
            time_other = dist_other / other_speed_ms if other_speed_ms > 0.1 else float('inf')

            # Prüfe ob beide Fahrzeuge ungefähr gleichzeitig ankommen
            time_diff = abs(time_own - time_other)
            if time_diff > self.ARRIVAL_TIME_TOLERANCE:
                continue

            # Time-to-collision aus Sicht des eigenen Fahrzeugs
            ttc = time_own

            if ttc < min_ttc:
                min_ttc = ttc
                side = _compute_side(own_dx, own_dy, own_x, own_y, other_x, other_y)

                if ttc < 1.0:
                    warning_level = 2  # Akustisch + blinkend
                    warning_side = side
                elif ttc < 2.0:
                    warning_level = max(warning_level, 1)  # Visuell
                    if warning_level == 1:
                        warning_side = side

        self._emit_if_changed(warning_level, warning_side)

        return {
            'level': warning_level,
            'side': warning_side,
            'ttc': min_ttc,
        }

    def _emit_if_changed(self, warning_level: int, warning_side: Optional[str]):
        """Emittiert Events nur bei Änderung des Warnzustands."""
        if warning_level != self.current_warning_level or warning_side != self.current_side:
            self.current_warning_level = warning_level
            self.current_side = warning_side
            self.event_bus.emit('cross_traffic_warning_changed', {
                'level': warning_level,
                'side': warning_side,
            })

