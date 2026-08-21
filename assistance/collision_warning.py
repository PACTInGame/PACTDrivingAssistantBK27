import math
from typing import Any, Dict

from assistance import park_distance_control
from assistance.base_system import AssistanceSystem
from core.event_bus import EventBus
from core.settings_manager import SettingsManager
from misc.helpers import calc_polygon_points, is_reversing, point_in_rectangle
from vehicles.own_vehicle import OwnVehicle
from vehicles.vehicle import Vehicle, VehicleData

# Die Autos, deren Laenge das Projekt wirklich kennt. ``get_vehicle_size``
# liefert fuer jedes unbekannte CName - also fuer jeden Fahrzeug-Mod - den
# Mittelklassewert 4.5 m (known-issues #28). Zu kurz geschaetzt heisst hier:
# die Warnung kommt zu spaet, und genau diesen Fehler darf ein Warnsystem
# nicht machen. Fuer unbekannte Autos wird deshalb die groesste Serienlaenge
# angesetzt (siehe FALLBACK_VEHICLE_LENGTH_M).
_KNOWN_CAR_NAMES = frozenset(getattr(park_distance_control, '_CAR_SIZES', {}))


class ForwardCollisionWarning(AssistanceSystem):
    """Kollisionswarnung für Fahrzeuge voraus

    Physikalisches Modell (reference/conventions.md §7):

    * Geschlossene Form bei konstanter Beschleunigung. Es wird *keine*
      Reibungsgrenze angenommen - berechnet wird die **noetige** Verzoegerung,
      nicht die erreichbare. Der Vergleich mit den Schwellen unten uebernimmt
      die Bewertung: 7.5 m/s² liegt am oberen Ende dessen, was ein
      Strassenreifen auf trockener Strecke geradeaus hergibt (~8–11 m/s²,
      in der Kurve weniger, weil der Kammsche Kreis schon zur Seite arbeitet).
    * ``SAFETY_BUFFER_M`` (0.5 m) Restabstand, den wir nicht aufbrauchen wollen.
    * ``REACTION_TIME_S`` (0.2 s) Reaktionszeit, aber nur, solange wir wirklich
      auflaufen - beim Entfernen waere sie ein Geschenk in die falsche Richtung.
    * Fahrzeuglaengen aus ``park_distance_control.get_vehicle_size``, mit
      konservativem Rueckfallwert fuer Mods.
    """

    # ─── Erfassungsbereich ────────────────────────────────────────────
    # Ein gedrehtes Viereck: nah ±20° breit, ab ~85 m nur noch ±1°.
    WEDGE_LENGTH_M = 85.0
    WEDGE_NEAR_RADIUS_M = 3.0
    WEDGE_HALF_ANGLE_DEG = 20.0
    WEDGE_FAR_HALF_ANGLE_DEG = 1.0
    # Etwas weiter als das Polygon, damit die Vorauswahl nie etwas verwirft,
    # das der Polygontest angenommen haette.
    ANGLE_GATE_DEG = WEDGE_HALF_ANGLE_DEG + 1.0
    RANGE_GATE_M = WEDGE_LENGTH_M + 1.0

    METRE = 65536.0     # MCI-Positionseinheiten pro Meter

    # ─── Physik ───────────────────────────────────────────────────────
    MIN_SPEED_KMH = 10.0
    SAFETY_BUFFER_M = 0.5
    REACTION_TIME_S = 0.2
    # Abstand aufgebraucht: es gibt keine sinnvolle Rechnung mehr, also der
    # Panikwert. Deutlich ueber jeder erreichbaren Verzoegerung, damit er die
    # oberste Schwelle sicher reisst.
    PANIC_DECELERATION_MS2 = 20.0
    # Untere Schranke fuer "das Auto vor uns bremst wirklich" - darunter ist
    # das Messrauschen groesser als der Wert.
    LEAD_BRAKING_EPS_MS2 = 0.001
    # Groesste Serienlaenge in LFS (FXR/XRR/FZR). Fuer ein unbekanntes CName
    # kommt die Warnung damit hoechstens 0.25 m frueher als noetig, statt bis
    # zu 0.65 m zu spaet.
    FALLBACK_VEHICLE_LENGTH_M = 5.0

    # Schwellen der noetigen Verzoegerung in m/s², je nach Einstellung
    # ``collision_warning_distance``: [Stufe 3, Stufe 2, Stufe 1].
    WARNING_THRESHOLDS = {
        0: (7.5, 3.0, 2.0),     # Early
        1: (7.5, 5.0, 2.5),     # Normal
        2: (7.5, 6.5, 5.5),     # Late
    }
    # Hysterese: eine erreichte Stufe faellt erst, wenn die noetige
    # Verzoegerung unter dieses Vielfache ihrer Schwelle sinkt. Sie faellt
    # aber *wirklich* - frueher hielt jede Verzoegerung > 0 die Stufe 3 fest,
    # bis der Bedarf exakt 0 wurde.
    HYSTERESIS_RELEASE = 0.8

    def __init__(self, event_bus: EventBus, settings: SettingsManager):
        super().__init__("forward_collision_warning", event_bus, settings)
        self.current_warning_level = 0
        self.own_rectangle = None
        # Laengen je CName werden einmal aufgeloest und gemerkt - im Zyklus
        # bleibt ein dict-Zugriff statt zweier Tabellen-Lookups.
        self._length_cache: Dict[str, float] = {}

    # ─── Hauptschleife ────────────────────────────────────────────────

    def process(self, own_vehicle: OwnVehicle, vehicles: Dict[int, Vehicle]) -> Dict[str, Any]:
        """Prüft auf Kollisionsgefahr voraus

        Kosten pro Zyklus: pro Fahrzeug zwei Zahlenvergleiche (Abstand und
        Winkel, beide vom VehicleManager ohnehin pro Frame ausgerechnet); nur
        was beide besteht, geht in den Polygontest. Gemessen mit 40 Autos:
        ~8 µs, wenn sie ueber die Strecke verteilt sind, ~93 µs im
        (unrealistischen) Fall, dass alle 40 im Keil stehen - bei 100 ms Budget.
        """
        # Einmal binden: OutGauge schreibt nebenlaeufig in own_vehicle
        # (known-issues #12).
        own = own_vehicle.data

        # Rueckwaertsfahrt: Heading und Direction sind Winkel auf einem Kreis,
        # die Differenz muss modular gerechnet werden. Die alte Subtraktion
        # schaltete FCW in einem ganzen Heading-Sektor ab.
        reversing = is_reversing(own.heading, own.direction)
        if not self.is_enabled() or own.speed < self.MIN_SPEED_KMH or reversing:
            self._publish(0, 0.0, always_emit_deceleration=False)
            return {'level': 0}

        self.own_rectangle = self._build_wedge(own)
        thresholds = self.WARNING_THRESHOLDS.get(
            self.settings.get('collision_warning_distance'),
            self.WARNING_THRESHOLDS[1])

        max_needed_deceleration = 0.0
        for vehicle in vehicles.values():
            other = vehicle.data
            if not self._is_vehicle_ahead(other):
                continue
            needed_braking = self._calculate_needed_braking(own, other)
            if needed_braking > max_needed_deceleration:
                max_needed_deceleration = needed_braking

        warning_level = self._warning_level(max_needed_deceleration,
                                            own.acceleration, thresholds)
        # Der Bremseingriff ist abgeschaltet (siehe unten); der Vertrag des
        # Events bleibt trotzdem: jeden Zyklus, 0 ausser bei Stufe 3.
        self._publish(warning_level,
                      max_needed_deceleration if warning_level > 2 else 0.0)

        return {
            'level': warning_level,
        }

    def _publish(self, warning_level: int, deceleration: float,
                 always_emit_deceleration: bool = True):
        """Veroeffentlicht Warnstufe (nur bei Aenderung) und Sollverzoegerung"""
        level_changed = warning_level != self.current_warning_level
        if always_emit_deceleration or level_changed:
            self.event_bus.emit('needed_deceleration_update', {
                'deceleration': deceleration,
            })
        if level_changed:
            self.current_warning_level = warning_level
            self.event_bus.emit('collision_warning_changed', {
                'level': warning_level,
            })

    # ─── Geometrie ────────────────────────────────────────────────────

    def _build_wedge(self, own: VehicleData):
        """Baut das gedrehte Viereck vor dem eigenen Auto (MCI-Einheiten)"""
        # (heading + 16384) / 182.05: LFS-Heading (0 = +Y, gegen den
        # Uhrzeigersinn) in Mathe-Grad (0 = +X) - reference/conventions.md §2.
        angle_of_car = (own.heading + 16384) / 182.05
        near = self.WEDGE_NEAR_RADIUS_M * self.METRE
        far = self.WEDGE_LENGTH_M * self.METRE
        return [
            calc_polygon_points(own.x, own.y, far,
                                angle_of_car + self.WEDGE_FAR_HALF_ANGLE_DEG),
            calc_polygon_points(own.x, own.y, near,
                                angle_of_car - self.WEDGE_HALF_ANGLE_DEG),
            calc_polygon_points(own.x, own.y, near,
                                angle_of_car + self.WEDGE_HALF_ANGLE_DEG),
            calc_polygon_points(own.x, own.y, far,
                                angle_of_car - self.WEDGE_FAR_HALF_ANGLE_DEG),
        ]

    def _is_vehicle_ahead(self, other: VehicleData) -> bool:
        """Prüft ob Fahrzeug vor uns ist

        Zwei billige Vorauswahlen vor dem Polygontest. Beide Groessen rechnet
        der ``VehicleManager`` ohnehin einmal pro Frame aus, hier kostet die
        Abfrage also nur je einen Vergleich - und keine Wurzel:

        * Abstand: alles hinter der Keillaenge kann nicht drin liegen.
        * Winkel: ``angle_to_player`` ist 0/360 = genau voraus, der Keil ist
          ±20° breit (reference/conventions.md §2).
        """
        if other.distance_to_player > self.RANGE_GATE_M:
            return False
        angle = other.angle_to_player
        if self.ANGLE_GATE_DEG < angle < 360.0 - self.ANGLE_GATE_DEG:
            return False
        return point_in_rectangle(other.x, other.y, self.own_rectangle)

    # ─── Warnstufe ────────────────────────────────────────────────────

    def _warning_level(self, needed_braking: float, own_acceleration: float,
                       thresholds) -> int:
        """Bildet die noetige Verzoegerung auf eine Warnstufe ab

        Steigend: die reine Schwelle. Fallend: erst unter
        ``HYSTERESIS_RELEASE`` x Schwelle, damit die Warnung am Schwellwert
        nicht flackert.
        """
        if needed_braking > thresholds[0]:
            raw = 3
        elif needed_braking > thresholds[1]:
            raw = 2
        elif needed_braking > thresholds[2] and own_acceleration > -needed_braking:
            # Stufe 1 nur, solange wir *nicht* schon stark genug bremsen.
            raw = 1
        else:
            raw = 0

        level = self.current_warning_level
        if raw >= level:
            return raw
        while level > raw and needed_braking <= thresholds[3 - level] * self.HYSTERESIS_RELEASE:
            level -= 1
        return level

    # ─── Physik ───────────────────────────────────────────────────────

    def _vehicle_length(self, cname) -> float:
        """Fahrzeuglaenge in Metern, mit konservativem Rueckfall fuer Mods"""
        cached = self._length_cache.get(cname)
        if cached is not None:
            return cached
        if cname in _KNOWN_CAR_NAMES:
            length = park_distance_control.get_vehicle_size(cname)[0]
        else:
            length = self.FALLBACK_VEHICLE_LENGTH_M
        self._length_cache[cname] = length
        return length

    def _calculate_needed_braking(self, own: VehicleData,
                                  other: VehicleData) -> float:
        """
        Calculates the deceleration we need in order to avoid a collision.

        Returns a **non-negative** value in m/s²: 0 means "no braking
        required". Previously this returned ``abs(req_accel)``, so a situation
        that allowed us to *accelerate* came back as a large braking demand and
        could raise a warning level.
        """

        # --- 1. SETUP & CONVERSION ---
        v_own = own.speed * 0.277778  # km/h to m/s
        v_other = other.speed * 0.277778  # km/h to m/s
        # Ensure a_other is treated as signed (negative for braking)
        relative_speed = v_own - v_other
        a_other = other.acceleration

        # --- 2. GEOMETRY & DISTANCE ---
        # Average length is used to find center-to-center offset,
        # assuming data.distance_to_player is center-to-center.
        length_of_both_vehicles = (self._vehicle_length(own.cname)
                                   + self._vehicle_length(other.cname)) / 2

        d = other.distance_to_player - length_of_both_vehicles - self.SAFETY_BUFFER_M
        if relative_speed > 0:
            d = d - relative_speed * self.REACTION_TIME_S

        # --- 3. PANIC & TRIVIAL CHECKS ---

        # If we have already hit the buffer (or the car), brake maximally immediately
        if d <= 0.01:
            return self.PANIC_DECELERATION_MS2

        # If we are slower than them and they are not braking (or accelerating away),
        # we don't need to do anything.
        if v_own <= v_other and a_other >= 0:
            return 0.0

        # --- 4. CALCULATE TIME HORIZONS ---

        # Time until the lead car comes to a complete stop
        # If a_other is 0 (constant speed) or > 0 (accelerating), it never stops.
        if a_other >= -self.LEAD_BRAKING_EPS_MS2:
            t_stop = float('inf')
        else:
            t_stop = -v_other / a_other

        # Time until we would crash/match speed if we used dynamic braking logic
        # If v_own <= v_other here, we are slower but they are braking.
        # The time to match is theoretically infinite/undefined in this specific
        # math context until they slow down below our speed, so we treat it as
        # 'never catch dynamically'
        if v_own <= v_other:
            t_match = float('inf')
        else:
            t_match = (2 * d) / (v_own - v_other)

        # --- 5. THE LOGIC SWITCH ---
        if t_match < t_stop:
            # === DYNAMIC CASE ===
            # We will catch them while they are still moving.
            # We need to match their acceleration plus a term to close the gap.
            # Formula: a_req = a_lead - (delta_v^2 / 2d)
            req_accel = a_other - ((v_own - v_other) ** 2 / (2 * d))

        else:
            # === STATIC CASE ===
            # They will stop before we catch them.
            # Treat them as a stationary wall located at their stopping point.

            # 1. Calculate distance lead car travels before stopping
            d_lead_stop = -(v_other ** 2) / (2 * a_other)

            # 2. Total distance we have available to stop
            d_total = d + d_lead_stop

            # 3. Calculate braking to stop in that distance
            # Formula: v^2 = 2*a*d  ->  a = -v^2 / 2d
            req_accel = -(v_own ** 2) / (2 * d_total)

        if not math.isfinite(req_accel):
            # Kann mit den Schranken oben nicht auftreten - aber LFS-Daten sind
            # nicht vertrauenswuerdig, und ein NaN wuerde jeden Vergleich unten
            # still zu False machen.
            return 0.0
        # Ein positives req_accel heisst: wir duerften sogar beschleunigen.
        return max(0.0, -req_accel)

# TODO no automatic braking for now
