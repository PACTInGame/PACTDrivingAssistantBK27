import math
from typing import Any, Dict

from assistance import park_distance_control
from assistance.base_system import AssistanceSystem
from assistance.path_conflict import direction_vector
from core.event_bus import EventBus
from core.settings_manager import SettingsManager
from misc.helpers import is_reversing
from vehicles.own_vehicle import OwnVehicle
from vehicles.vehicle import Vehicle, VehicleData

# Fahrzeugmasse: ``park_distance_control.is_known_car`` sagt, ob die
# Tabelle dieses CName wirklich kennt. Fuer jedes unbekannte - also fuer
# jeden Fahrzeug-Mod - liefert ``get_vehicle_size`` die Mittelklasse
# (known-issues #28). Zu klein geschaetzt heisst hier: die Warnung kommt
# zu spaet, und genau diesen Fehler darf ein Warnsystem nicht machen. Fuer
# unbekannte Autos gelten deshalb die Masse des laengsten und breitesten
# Serienautos (FALLBACK_VEHICLE_LENGTH_M / _WIDTH_M).


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
    * Fahrzeugmasse aus ``park_distance_control``, mit konservativem
      Rueckfallwert fuer Mods.
    """

    # ─── Erfassungsbereich ────────────────────────────────────────────
    #
    # Ein gerader Korridor vor uns, und seine Breite ist **keine** frei
    # gewaehlte Zahl: zwei Autos treffen sich genau dann, wenn ihre
    # Mittelpunkte quer zur Fahrtrichtung naeher beieinander liegen als die
    # Summe ihrer halben Breiten. Genau das wird geprueft.
    #
    # Vorher stand hier ein Keil aus Winkeln - nah ±20°, ab 85 m nur noch ±1°
    # -, dessen halbe Breite ueber die ganze Laenge zwischen 1.03 m und 1.48 m
    # lag. Ein 1.8 m breites Auto in derselben Spur faellt daraus heraus,
    # sobald es anderthalb Meter neben unserer Achse liegt, und das ist auf
    # einer Geraden bei 90 km/h voellig normal. Gemessen
    # (``simulation_tests``, 2026-09-20, jeweils der Lauf mit Add-on):
    #
    # ==========================================  ==========  =============
    # Lauf                                        |quer| max  Ausgang
    # ==========================================  ==========  =============
    # 07 Auffahren auf stehendes Auto               1.68 m    Aufprall
    # 32 Stresstest, Teilfall 3                     1.77 m    Aufprall
    # 10 knappes Vorbeifahren (Fehlalarmtest)       2.74 m    kein Kontakt
    # ==========================================  ==========  =============
    #
    # In 07 und 32/3 verschwand das Ziel genau in der Sekunde aus dem Keil, in
    # der der Eingriff haette beginnen muessen; die Warnstufe fiel auf 0 und
    # der Bedarf auf 0. Die Ueberlappungsbedingung liegt bei rund 1.9 m (FZ5
    # 2.0 m neben RB4 1.8 m) sauber zwischen beiden Gruppen - sie sieht die
    # Aufprallfaelle und laesst das Vorbeifahren in Ruhe, ohne dass daran
    # etwas eingestellt worden waere.
    CORRIDOR_LENGTH_M = 85.0
    # Etwas weiter als der Korridor, damit die Vorauswahl auf dem
    # Mittelpunktsabstand nie etwas verwirft, das noch drin liegen koennte.
    RANGE_GATE_M = CORRIDOR_LENGTH_M + 1.0

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
    # Groesste Serienmasse in LFS (FXR/XRR/FZR). Fuer ein unbekanntes CName
    # kommt die Warnung damit hoechstens 0.25 m frueher als noetig, statt bis
    # zu 0.65 m zu spaet.
    FALLBACK_VEHICLE_LENGTH_M = 5.0
    FALLBACK_VEHICLE_WIDTH_M = 2.1

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
        # Masse je CName werden einmal aufgeloest und gemerkt - im Zyklus
        # bleibt ein dict-Zugriff statt zweier Tabellen-Lookups.
        self._length_cache: Dict[str, float] = {}
        self._width_cache: Dict[str, float] = {}

    # ─── Hauptschleife ────────────────────────────────────────────────

    def process(self, own_vehicle: OwnVehicle, vehicles: Dict[int, Vehicle]) -> Dict[str, Any]:
        """Prüft auf Kollisionsgefahr voraus

        Kosten pro Zyklus: pro Fahrzeug ein Vergleich auf dem Abstand, den
        der VehicleManager ohnehin je Frame ausrechnet; nur was den besteht,
        bekommt zwei Skalarprodukte. Das ist weniger als vorher - der Keil
        baute je Zyklus ein Polygon und prueste vier Kanten pro Fahrzeug.
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

        # Einmal je Zyklus, nicht je Fahrzeug. Bewusst eine lokale Groesse und
        # kein Feld: ein Feld waere vor dem ersten Durchlauf (0, 0), und damit
        # laege *jedes* Fahrzeug in Reichweite genau voraus.
        forward = direction_vector(own.heading)
        thresholds = self.WARNING_THRESHOLDS.get(
            self.settings.get('collision_warning_distance'),
            self.WARNING_THRESHOLDS[1])

        max_needed_deceleration = 0.0
        for vehicle in vehicles.values():
            other = vehicle.data
            if not self._is_vehicle_ahead(own, other, forward):
                continue
            needed_braking = self._calculate_needed_braking(own, other)
            if needed_braking > max_needed_deceleration:
                max_needed_deceleration = needed_braking

        warning_level = self._warning_level(max_needed_deceleration,
                                            own.acceleration, thresholds)
        # Die Sollverzoegerung geht erst ab Stufe 3 hinaus. Das ist bewusst
        # **nicht** die Schwelle, die ``EmergencyBrake`` fuer sich selbst
        # nennt: dessen ``ENGAGE_DECELERATION_MS2`` steht auf 6.0 m/s², und
        # Stufe 3 beginnt in jeder Einstellung erst ueber 7.5 m/s². Effektiv
        # wird also bei 7.5 gebremst.
        #
        # Beides wurde gemessen (2026-09-20, dieselben Aufnahmen, einmal so
        # und einmal ab Stufe 2 veroeffentlicht):
        #
        # * **ab Stufe 2** (Eingriff also ab 6.0): Szenario 32 wurde in allen
        #   fuenf Teilfaellen kollisionsfrei - aber der Wagen stand danach 6 m
        #   (07), 11 m (12) und 11 m (26) vor dem Hindernis. Das ist keine
        #   Notbremsung mehr, das ist Stehenbleiben auf Verdacht.
        # * **ab Stufe 3**: der Restabstand liegt im Bereich, den ein Fahrer
        #   erwartet.
        #
        # Der Grund fuer den Unterschied liegt nicht in der Schwelle, sondern
        # im Aktuator: die Tastenausgabe kennt nur ganz oder gar nicht. Wer bei
        # einem Bedarf von 6.0 m/s² eingreift und dann mit den ~9.7 m/s²
        # bremst, die der Reifen hergibt, steht zwangslaeufig rund ein Viertel
        # des Bremswegs zu frueh. Solange der Eingriff nicht moduliert
        # (Tastentakten oder die Achse, ``control-intervention.md`` §3), ist die
        # spaetere Schwelle die ehrlichere: sie laesst dem Fahrer mehr Weg und
        # dem Eingriff weniger Ueberschuss.
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
                # Mehrere Systeme fordern inzwischen Verzoegerung an (auch
                # Quer- und Toter-Winkel-Warnung). ``EmergencyBrake`` fuehrt
                # sie je Quelle und nimmt die groesste - ohne diesen
                # Schluessel wuerde die zuletzt gesendete gewinnen, also
                # ausgerechnet die Reihenfolge im AssistanceManager.
                'source': 'forward_collision',
            })
        if level_changed:
            self.current_warning_level = warning_level
            self.event_bus.emit('collision_warning_changed', {
                'level': warning_level,
            })

    # ─── Geometrie ────────────────────────────────────────────────────

    def _is_vehicle_ahead(self, own: VehicleData, other: VehicleData,
                          forward) -> bool:
        """Liegt dieses Fahrzeug in unserem Fahrschlauch?

        Zwei Fragen, in der Reihenfolge ihrer Kosten:

        * **Reichweite** - ``distance_to_player`` rechnet der
          ``VehicleManager`` ohnehin je Frame aus; alles jenseits der
          Korridorlaenge kann nicht drin liegen. Ein Vergleich, keine Wurzel.
        * **Ueberlappung** - der Abstand quer zu unserer Fahrtrichtung gegen
          die Summe der beiden halben Fahrzeugbreiten. Genau dann, wenn er
          kleiner ist, belegen die beiden Autos dieselbe Spurbreite; alles
          andere faehrt aneinander vorbei (siehe Klassendoku).

        Laengs wird nur "vor uns" verlangt: ein Fahrzeug, dessen Mittelpunkt
        hinter unserem liegt, ist kein Auffahrfall, sondern Sache der
        Toter-Winkel-Warnung.

        ``forward`` ist der normierte Fahrtrichtungsvektor, den ``process``
        einmal je Zyklus aus dem Heading rechnet.
        """
        if other.distance_to_player > self.RANGE_GATE_M:
            return False
        dx, dy = forward
        gap_x = (other.x - own.x) / self.METRE
        gap_y = (other.y - own.y) / self.METRE
        along = dx * gap_x + dy * gap_y
        if along < 0.0 or along > self.CORRIDOR_LENGTH_M:
            return False
        lateral = -dy * gap_x + dx * gap_y
        reach = (self._vehicle_width(own.cname)
                 + self._vehicle_width(other.cname)) / 2.0
        return abs(lateral) <= reach

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
        if park_distance_control.is_known_car(cname):
            length = park_distance_control.get_vehicle_size(cname)[0]
        else:
            length = self.FALLBACK_VEHICLE_LENGTH_M
        self._length_cache[cname] = length
        return length

    def _vehicle_width(self, cname) -> float:
        """Fahrzeugbreite in Metern, mit konservativem Rueckfall fuer Mods."""
        cached = self._width_cache.get(cname)
        if cached is not None:
            return cached
        if park_distance_control.is_known_car(cname):
            width = park_distance_control.get_vehicle_size(cname)[1]
        else:
            width = self.FALLBACK_VEHICLE_WIDTH_M
        self._width_cache[cname] = width
        return width

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
        relative_speed = v_own - v_other
        # Only the lead car's *braking* is extrapolated. Its speed is a
        # measurement and is used in full; the speed it has not gained yet is
        # a claim about the future, and a car cannot accelerate for ever - the
        # one pulling away now is the same one standing still a second later.
        #
        # Measured (``simulation_tests`` 32, sub-case 4, 2026-09-20): the lead
        # accelerated away at 4-6 m/s² from a standstill and then braked at
        # 10 m/s² back to zero. Crediting its acceleration made
        # ``req_accel`` positive - "we may even speed up" - so the demand was
        # **0.0 for 1.8 s** while we closed from 55 m to 44 m at 82 km/h. The
        # warning then arrived 1.15 s before contact, at which point no tyre
        # could have helped. With the acceleration clamped away the same frames
        # ask for 3.7 to 7.2 m/s², which is where an intervention belongs.
        #
        # The asymmetry is deliberate and is the same rule
        # ``assistance/path_conflict.py`` applies to yaw: an extrapolation may
        # only ever bring an answer *forward*, never postpone it. Braking is
        # kept because it only raises the demand, and because a car that is
        # braking demonstrably goes on braking.
        a_other = min(0.0, other.acceleration)

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

        # If we are slower than them and they are not braking, we don't need to
        # do anything. (``a_other`` is clamped at 0, so ``>= 0`` reads "not
        # braking" and nothing else.)
        if v_own <= v_other and a_other >= 0:
            return 0.0

        # --- 4. CALCULATE TIME HORIZONS ---

        # Time until the lead car comes to a complete stop. At constant speed
        # it never does.
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
        #
        # Gedeckelt auf denselben Panikwert wie oben: ``d`` geht gegen Null,
        # wenn der Abstand aufgebraucht ist, und die Division liefert dann
        # keine Information mehr, sondern Rundungsfehler mal einer grossen
        # Zahl. Gemessen in Traces vom 2026-09-20: 198, 1354 und 4758 m/s² -
        # Zahlen, die in Logs und Traces nur verwirren und die jede
        # Mittelung ueber den Bedarf unbrauchbar machen. Jenseits von 20 m/s²
        # ist die Antwort ohnehin dieselbe: alles, was die Reifen hergeben.
        # Dieselbe Deckelung wie in ``path_conflict.stopping_deceleration``.
        return min(self.PANIC_DECELERATION_MS2, max(0.0, -req_accel))
