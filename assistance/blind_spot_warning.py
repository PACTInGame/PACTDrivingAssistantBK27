import time
from typing import Any, Dict, Optional, Tuple

from assistance.base_system import AssistanceSystem
from core.event_bus import EventBus
from core.settings_manager import SettingsManager
from misc.helpers import calc_polygon_points
from vehicles.own_vehicle import OwnVehicle
from vehicles.vehicle import Vehicle
from shapely import Polygon

METRE = 65536.0             # MCI-Positionseinheiten pro Meter
KMH_TO_MS = 1.0 / 3.6
HEADING_OFFSET = 16384      # +90 Grad: von "0 = +Y" auf "0 = +X" (conventions.md §2)
HEADING_DIVISOR = 182.05    # LFS-Heading-Einheiten pro Grad

# Erlaubte Heading-Abweichung, damit ein Auto ueberhaupt als "faehrt in unsere
# Richtung" gilt. 5000 Einheiten ~ 27.5 Grad - der Wert dieses Projekts.
HEADING_THRESHOLD_UNITS = 5000

# Umriss eines fremden Autos: ein zentralsymmetrisches Viereck mit 2.3 m
# Radius, also ~4.3 m lang und ~1.7 m breit.
OTHER_CAR_RADIUS_M = 2.3
_OTHER_CAR_ANGLE_OFFSETS = (22, 158, 202, 338)

# Korridor neben dem Auto. Die Multiplikatoren sind Meter, die Winkel Grad
# relativ zur Fahrzeugachse (0 = voraus, gegen den Uhrzeigersinn).
_CORRIDOR_MULTIPLIERS = (4.0, 85.0, 85.0, 1.0)
# Reihenfolge der Ecken: aussen-nah -> aussen-fern -> innen-fern -> innen-nah.
# Vorher standen die beiden fernen Ecken vertauscht (178 vor 177 bzw. 182 vor
# 183). Damit kreuzten sich die Kanten, shapely bekam ein ungueltiges Polygon
# und der ueberdeckte Bereich war eine Schleife von 64 m² statt der
# beabsichtigten 190 m².
_CORRIDOR_ANGLES_LEFT = (90, 177, 178, 90)
_CORRIDOR_ANGLES_RIGHT = (270, 183, 182, 270)


def _is_within_threshold(own_heading, other_heading):
    # Checks if the heading of another car is within a threshold
    lower_bound = (other_heading - HEADING_THRESHOLD_UNITS) % 65536
    upper_bound = (other_heading + HEADING_THRESHOLD_UNITS) % 65536

    if lower_bound > upper_bound:
        return own_heading > lower_bound or own_heading < upper_bound
    return lower_bound < own_heading < upper_bound


def _polygon_intersect(p1, p2):
    return p1.intersects(p2)


def _normalize_angle(angle):
    """Bringt einen Winkel nach 0...360 Grad.

    Vorher stand hier ``abs(angle)``. Das spiegelt negative Winkel an der
    X-Achse, statt sie zu normalisieren: -30 Grad wurde zu +30 Grad.
    """
    return angle % 360.0


def car_angle_degrees(heading) -> float:
    """LFS-Heading -> Mathe-Grad (0 = +X, gegen den Uhrzeigersinn)."""
    return _normalize_angle((heading + HEADING_OFFSET) / HEADING_DIVISOR)


def create_rectangle_for_car(x: float, y: float, heading: float) -> Polygon:
    """Umriss eines fremden Autos in MCI-Einheiten.

    Der Winkel kam vorher aus ``abs((heading - 16384) / 182.05)``. Fuer
    Headings ab 16384 ist das eine Drehung um 180 Grad - bei diesem
    zentralsymmetrischen Viereck folgenlos. Darunter ist es aber eine
    Spiegelung: ein nach Nordost zeigendes Auto bekam einen nach Nordwest
    ausgerichteten Umriss.
    """
    angle_of_car = car_angle_degrees(heading)
    factor = OTHER_CAR_RADIUS_M * METRE
    return Polygon([calc_polygon_points(x, y, factor, angle_of_car + offset)
                    for offset in _OTHER_CAR_ANGLE_OFFSETS])


def _create_blindspot_rectangle(x: float, y: float, angle_of_car: float,
                                angles: Tuple[float, ...]) -> Polygon:
    # Creates blind spot rectangle using provided angles
    points = [calc_polygon_points(x, y, multiplier * METRE, angle_of_car + angle)
              for multiplier, angle in zip(_CORRIDOR_MULTIPLIERS, angles)]
    return Polygon(points)


class BlindSpotWarning(AssistanceSystem):
    """Toter-Winkel-Warner

    Geometrie: zwei lange, schmale Korridore links und rechts, die von der
    Fahrzeugmitte bis 85 m nach hinten reichen und seitlich etwa 1 bis 4.5 m
    von der eigenen Achse entfernt liegen - also die Nachbarspur.

    Ausloesekriterium (reference/systems.md):

    1. **Geometrie** - der Umriss des anderen Autos schneidet den Korridor.
    2. **Relevanz** - im eigentlichen toten Winkel (bis ``BLIND_SPOT_ZONE_M``
       hinter der eigenen Fahrzeugmitte) immer, denn dorthin sieht kein
       Spiegel, unabhaengig von der Relativgeschwindigkeit. Weiter hinten nur,
       solange das Auto auflaeuft und uns in hoechstens ``APPROACH_TIME_S``
       erreicht (Spurwechsel-Assistent, ISO 17387 arbeitet mit ~3.5 s).
    3. **Haltezeit** - eine gesetzte Warnung bleibt noch so lange stehen, wie
       das andere Auto braucht, um sich relativ zu uns um eine Fahrzeuglaenge
       zu verschieben. Solange ueberlappen die beiden Autos laengs noch, und
       die Warnung darf im 100-ms-Raster nicht flackern.

    Vorher lautete die Bedingung
    ``distance < (other_kmh - own_kmh + 5) * 1.2`` - links Meter, rechts km/h.
    Fuer jedes Auto, das nicht schneller war als wir, war die rechte Seite
    <= 0. Genau der haeufigste Fall, ein Auto das mit gleicher Geschwindigkeit
    im toten Winkel mitfaehrt, konnte damit nie warnen.

    Kosten pro Zyklus: zwei Float-Vergleiche und ein Modulo-Test pro Fahrzeug
    (~40), danach ein shapely-Polygon plus zwei ``intersects`` nur fuer die
    Fahrzeuge, die alle Vorfilter ueberstanden haben - im Normalfall keines
    bis zwei. Vorher war es ein Polygon pro Fahrzeug pro Zyklus, ohne jede
    Vorauswahl (known-issues #7).
    """

    # ─── Erfassungsbereich ────────────────────────────────────────────
    CORRIDOR_LENGTH_M = 85.0
    # Etwas weiter als der Korridor, damit die Vorauswahl nie ein Auto
    # verwirft, dessen Umriss den Korridor noch beruehrt haette.
    RANGE_GATE_M = CORRIDOR_LENGTH_M + OTHER_CAR_RADIUS_M + 1.0
    # ``angle_to_player``: 0 = genau voraus, im Uhrzeigersinn (conventions.md
    # §2). Der Korridor beginnt seitlich (90/270) und reicht nach hinten
    # (180). 30 Grad Zugabe decken den 2.3-m-Umriss ab allen Abstaenden ueber
    # NEAR_BYPASS_M ab (asin(2.3/5) = 27.4 Grad).
    SIDE_GATE_MIN_DEG = 60.0
    SIDE_GATE_MAX_DEG = 300.0
    NEAR_BYPASS_M = 5.0

    # ─── Relevanz ─────────────────────────────────────────────────────
    # Toter Winkel im engeren Sinn: bis hierher sieht der Spiegel nicht,
    # gemessen ab Fahrzeugmitte (ISO 17387: Heck plus 3 m, bei ~4.5 m
    # Fahrzeuglaenge also rund 7 m ab Mitte).
    BLIND_SPOT_ZONE_M = 7.0
    APPROACH_TIME_S = 3.5
    # Darunter ist die Differenz zweier km/h-Werte Rauschen, kein Auflaufen.
    MIN_CLOSING_MS = 0.5

    # ─── Haltezeit ────────────────────────────────────────────────────
    MEAN_VEHICLE_LENGTH_M = 4.5
    HOLD_MIN_S = 0.5
    HOLD_MAX_S = 2.0

    def __init__(self, event_bus: EventBus, settings: SettingsManager):
        super().__init__("blind_spot_warning", event_bus, settings)
        self.left_warning = False
        self.right_warning = False
        # Ablaufzeitpunkte der Haltezeit, siehe Klassendoku.
        self._left_until = 0.0
        self._right_until = 0.0
        # Zaehler fuer den Test des Vorfilters: wie viele shapely-Polygone
        # der letzte Zyklus gebaut hat.
        self.polygons_built = 0
        # Ueberschreibbar im Test, damit die Haltezeit ohne echte Uhr
        # geprueft werden kann.
        self.clock = time.monotonic

    def process(self, own_vehicle: OwnVehicle, vehicles: Dict[int, Vehicle]) -> Dict[str, Any]:
        """Prüft auf Fahrzeuge im toten Winkel"""
        now = self.clock()
        # Einmal binden: OutGauge schreibt nebenlaeufig in own_vehicle.data
        # (known-issues #12).
        own = own_vehicle.data
        own_speed_ms = own.speed * KMH_TO_MS

        self.polygons_built = 0
        rectangle_left: Optional[Polygon] = None
        rectangle_right: Optional[Polygon] = None
        hit_left = hit_right = False

        for vehicle in vehicles.values():
            data = vehicle.data
            distance = data.distance_to_player

            # ─── Vorfilter: nur Vergleiche, keine Allokation ──────────
            if distance > self.RANGE_GATE_M:
                continue
            if distance > self.NEAR_BYPASS_M and not (
                    self.SIDE_GATE_MIN_DEG <= data.angle_to_player <= self.SIDE_GATE_MAX_DEG):
                continue
            if not _is_within_threshold(own.heading, data.heading):
                continue

            other_speed_ms = data.speed * KMH_TO_MS
            if not self._is_relevant(distance, own_speed_ms, other_speed_ms):
                continue

            # ─── Erst jetzt Geometrie ─────────────────────────────────
            if rectangle_left is None:
                angle_of_car = car_angle_degrees(own.heading)
                rectangle_left = _create_blindspot_rectangle(
                    own.x, own.y, angle_of_car, _CORRIDOR_ANGLES_LEFT)
                rectangle_right = _create_blindspot_rectangle(
                    own.x, own.y, angle_of_car, _CORRIDOR_ANGLES_RIGHT)
                self.polygons_built += 2

            other_rectangle = create_rectangle_for_car(data.x, data.y, data.heading)
            self.polygons_built += 1

            hold_until = now + self._hold_time(own_speed_ms, other_speed_ms)
            if _polygon_intersect(other_rectangle, rectangle_left):
                hit_left = True
                self._left_until = max(self._left_until, hold_until)
            if _polygon_intersect(other_rectangle, rectangle_right):
                hit_right = True
                self._right_until = max(self._right_until, hold_until)
            if hit_left and hit_right:
                break

        blindspot_l = hit_left or now < self._left_until
        blindspot_r = hit_right or now < self._right_until

        if blindspot_l != self.left_warning or blindspot_r != self.right_warning:
            self.left_warning = blindspot_l
            self.right_warning = blindspot_r

            self.event_bus.emit('blind_spot_warning_changed', {
                'left': self.left_warning,
                'right': self.right_warning
            })
        return {
            'left_warning': self.left_warning,
            'right_warning': self.right_warning
        }

    # ─── Relevanz und Haltezeit ───────────────────────────────────────

    def _is_relevant(self, distance_m: float, own_speed_ms: float,
                     other_speed_ms: float) -> bool:
        """Ist ein Auto im Korridor ueberhaupt eine Warnung wert?"""
        if distance_m <= self.BLIND_SPOT_ZONE_M:
            # Toter Winkel: dort steht die Warnung immer, auch bei exakt
            # gleicher Geschwindigkeit.
            return True
        closing_ms = other_speed_ms - own_speed_ms
        if closing_ms <= self.MIN_CLOSING_MS:
            return False
        return (distance_m - self.BLIND_SPOT_ZONE_M) / closing_ms <= self.APPROACH_TIME_S

    def _hold_time(self, own_speed_ms: float, other_speed_ms: float) -> float:
        """Wie lange eine gesetzte Warnung mindestens stehen bleibt.

        Bezugsgroesse ist die Zeit, die das andere Auto braucht, um sich
        relativ zu uns um eine Fahrzeuglaenge zu verschieben: solange
        ueberlappen wir laengs noch, und ein einzelner ausgefallener
        Erfassungszyklus darf die Warnung nicht loeschen.
        """
        relative_ms = abs(other_speed_ms - own_speed_ms)
        hold = self.MEAN_VEHICLE_LENGTH_M / max(relative_ms, self.MIN_CLOSING_MS)
        return min(self.HOLD_MAX_S, max(self.HOLD_MIN_S, hold))
