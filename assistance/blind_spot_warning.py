import time
from typing import Any, Dict, Optional, Tuple

from assistance.base_system import AssistanceSystem
from assistance.path_conflict import (
    BRAKE_DEMAND_MS2, INF, MIN_CORNERING_YAW, MIN_MANOEUVRE_YAW, Body,
    body_from, contact_window, free_distance, stopping_deceleration)
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
# Fuer die Akutstufen ist derselbe Wert zu eng. Wer in fliessenden Verkehr
# abbiegt, steht im entscheidenden Moment schraeg zur Zielspur - 27 Grad
# schliessen genau die Situation aus, um die es geht. 12000 Einheiten ~ 66
# Grad lassen das Abbiegen zu und halten Querverkehr (90 Grad, Sache der
# Querverkehrswarnung) und Gegenverkehr (180 Grad) weiter draussen.
ACUTE_HEADING_THRESHOLD_UNITS = 12000

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


def _is_within_threshold(own_heading, other_heading,
                        threshold=HEADING_THRESHOLD_UNITS):
    # Checks if the heading of another car is within a threshold
    lower_bound = (other_heading - threshold) % 65536
    upper_bound = (other_heading + threshold) % 65536

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
    """Toter-Winkel-Warner - drei Stufen je Seite.

    ===== ================================================================
    Stufe Bedeutung
    ===== ================================================================
    0     nichts
    1     **Anzeige**: da ist jemand im toten Winkel. Reine Geometrie, kein
          Konflikt - der Fahrer soll es wissen, bevor er den Blinker setzt.
    2     **Akutwarnung**, blinkend und akustisch: die beiden Umrisse
          beruehren sich innerhalb von ``ACUTE_TTC_S`` - auf der Geraden oder
          auf dem Bogen, den die Gierrate beschreibt. Das deckt beide Faelle
          ab, die der Fahrer als gefaehrlich erlebt: "ich komme ihm zu nahe"
          und "ich fahre in seinen Pfad".
    3     **Bremseingriff**: Stufe 2, wir fahren langsam
          (``BRAKE_SPEED_KMH``), das andere Auto kommt von hinten und ist
          deutlich schneller (``MIN_APPROACH_DELTA_KMH``) - das Abbiegen in
          fliessenden Verkehr. Die Sollverzoegerung geht als
          ``needed_deceleration_update`` mit ``source='blind_spot'`` an
          ``EmergencyBrake``, der allein entscheidet, ob daraus ein Eingriff
          wird (reference/control-intervention.md).
    ===== ================================================================

    **Stufe 1 und Stufe 2/3 beantworten verschiedene Fragen und benutzen
    deshalb verschiedene Geometrie.** Stufe 1 fragt "steht jemand dort, wo
    kein Spiegel hinsieht" - das ist ein fester Korridor neben *uns*, und der
    dreht sich mit unserem Heading mit. Stufe 2/3 fragt "treffen sich die
    beiden Umrisse" - und genau waehrend des Einlenkens dreht sich der
    Korridor von dem Auto **weg**, auf das wir zufahren. Haetten die Akutstufen
    am Korridortreffer gehangen, waere die Warnung in dem Moment verstummt, in
    dem sie gebraucht wird. Die Physik dafuer steht in
    ``assistance/path_conflict.py``.

    Geometrie der Stufe 1: zwei lange, schmale Korridore links und rechts, die
    von der Fahrzeugmitte bis 85 m nach hinten reichen und seitlich etwa 1 bis
    4.5 m von der eigenen Achse entfernt liegen - also die Nachbarspur.

    Ausloesekriterium Stufe 1 (reference/systems.md):

    1. **Geometrie** - der Umriss des anderen Autos schneidet den Korridor.
    2. **Bewegung** - das andere Auto faehrt (``MIN_OTHER_SPEED_KMH``) und
       faellt nicht zurueck (hoechstens ``MAX_TRAILING_SPEED_KMH`` langsamer
       als wir). Ohne diesen Filter warnte jede parkende Autoreihe am
       Strassenrand, an der wir vorbeifuhren.
    3. **Relevanz** - im eigentlichen toten Winkel (bis ``BLIND_SPOT_ZONE_M``
       hinter der eigenen Fahrzeugmitte) immer, denn dorthin sieht kein
       Spiegel, unabhaengig von der Relativgeschwindigkeit. Weiter hinten nur,
       solange das Auto auflaeuft und uns in hoechstens ``APPROACH_TIME_S``
       erreicht (Spurwechsel-Assistent, ISO 17387 arbeitet mit ~3.5 s).
    4. **Haltezeit** - eine gesetzte Warnung bleibt noch so lange stehen, wie
       das andere Auto braucht, um sich relativ zu uns um eine Fahrzeuglaenge
       zu verschieben. Solange ueberlappen die beiden Autos laengs noch, und
       die Warnung darf im 100-ms-Raster nicht flackern.

    Vorher lautete die Bedingung
    ``distance < (other_kmh - own_kmh + 5) * 1.2`` - links Meter, rechts km/h.
    Fuer jedes Auto, das nicht schneller war als wir, war die rechte Seite
    <= 0. Genau der haeufigste Fall, ein Auto das mit gleicher Geschwindigkeit
    im toten Winkel mitfaehrt, konnte damit nie warnen.

    Kosten pro Zyklus: zwei Float-Vergleiche und ein Abstandsvergleich pro
    Fahrzeug (~40). Fuer die wenigen, die den Naeherungsfilter ueberstehen,
    kommt ein ``Body`` und ein Kontaktfenster dazu (vier Achsen, keine
    Allokation ausser dem ``Body``); ein shapely-Polygon plus zwei
    ``intersects`` kostet nur, wer zusaetzlich alle Stufe-1-Filter besteht -
    im Normalfall keines bis zwei. Vorher war es ein Polygon pro Fahrzeug pro
    Zyklus, ohne jede Vorauswahl (known-issues #7).
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

    # ─── Relevanz (Stufe 1) ───────────────────────────────────────────
    # Toter Winkel im engeren Sinn: bis hierher sieht der Spiegel nicht,
    # gemessen ab Fahrzeugmitte (ISO 17387: Heck plus 3 m, bei ~4.5 m
    # Fahrzeuglaenge also rund 7 m ab Mitte).
    BLIND_SPOT_ZONE_M = 7.0
    APPROACH_TIME_S = 3.5
    # Darunter ist die Differenz zweier km/h-Werte Rauschen, kein Auflaufen.
    MIN_CLOSING_MS = 0.5
    # ─── Bewegungsfilter ──────────────────────────────────────────────
    # Ein Toter-Winkel-Warner warnt vor einem *Spurwechselkonflikt*. Beides
    # hier ist die Bedingung dafuer, dass es ueberhaupt einen geben kann:
    #
    # 1. Das andere Auto muss fahren. Eine parkende Reihe am Strassenrand
    #    liegt sekundenlang im Korridor und hat die Warnung frueher dauerhaft
    #    gesetzt - genau der Fall, den ISO 17387 als "stationary object"
    #    ausdruecklich ausschliesst.
    # 2. Es darf nicht zurueckfallen. Wer langsamer ist, ist beim Spurwechsel
    #    kein Konflikt, sondern verschwindet nach hinten. Die 2 km/h Zugabe
    #    decken das Rauschen der km/h-Differenz und den haeufigsten Fall ab:
    #    ein Auto, das mit praktisch gleicher Geschwindigkeit mitfaehrt.
    MIN_OTHER_SPEED_KMH = 5.0
    MAX_TRAILING_SPEED_KMH = 2.0

    # ─── Akutstufen (2 und 3) ─────────────────────────────────────────
    # Vorwarnzeit bis zur Beruehrung. ISO 17387 gibt fuer den
    # Spurwechselassistenten 3.5 s als Erfassungskriterium vor; eine Warnung,
    # die blinkt und piept, soll aber erst kommen, wenn wirklich etwas
    # passiert - sonst schaltet der Fahrer sie ab. 2.5 s ist eine
    # Lenkbewegung und eine Reaktionszeit.
    ACUTE_TTC_S = 2.5
    # ...aber nur, solange die Vorhersage so weit traegt.
    #
    # Eine Beruehrung in 2.5 s bei 110 km/h liegt ueber 70 m voraus, und ueber
    # 70 m macht ein Grad Winkelfehler 1.2 m Querversatz. Zwei Autos, die
    # nebeneinander dieselbe Kurve fahren, haben dauerhaft ein paar Grad
    # Headingunterschied - den Rest der Kurve, den der eine schon genommen hat
    # und der andere noch vor sich - und *jede* Fortschreibung laesst sie
    # daraus ineinanderlaufen. Gemessen in ``simulation_tests`` Szenario 24:
    # 5.5 m Abstand, 2.4 Grad Unterschied, vorhergesagte Beruehrung in 1.9 s -
    # und in Wirklichkeit fuhren sie so weiter. ``path_conflict.contact_window``
    # sagt, warum auch ein Bogen das nicht rettet.
    #
    # Also wird der Horizont an das gebunden, was ihn traegt: die volle
    # Vorwarnzeit gibt es, wenn die *relative* Gierrate ein echtes Manoever
    # zeigt - jemand lenkt in jemanden hinein, dieselbe Schwelle, die
    # ``path_conflict`` fuer den Bogen benutzt. Ohne das, also zwischen zwei
    # Autos in unveraenderter Formation, wird nur bis STEADY_TTC_S
    # vorausgeschaut, wo der Querversatzfehler unter einem halben Meter
    # bleibt.
    #
    # Das kostet in genau einem Fall Vorwarnzeit: jemand faehrt mit
    # unveraendertem Lenkrad schraeg auf uns zu. Dann kommt die Warnung bei
    # 1.5 s statt 2.5 s - immer noch eine Sekunde vor dem Kontakt, und immer
    # noch mit dem Bremseingriff dahinter.
    #
    # Und auch 1.5 s sind noch zu viel, wenn **beide** eine Kurve fahren: im
    # zweiten von drei Durchlaeufen von Szenario 24 standen die Autos 3.4 m
    # statt 5.5 m auseinander, und derselbe Dauerwinkel sagte dann Kontakt in
    # 1.0 s voraus. Dort wird deshalb gar nicht mehr vorausgeschaut - siehe
    # ``_prediction_horizon`` und ``MIN_CORNERING_YAW``. Nebeneinander durch
    # eine Kurve zu fahren ist kein Spurwechsel, und ein Warner, der beim
    # Nebeneinanderfahren blinkt und piept, wird abgeschaltet.
    STEADY_TTC_S = 1.5
    # Darueber wird nicht gebremst: ein Spurwechsel bei Tempo ist mit dem
    # Lenkrad zu korrigieren, und eine Vollbremsung im fliessenden Verkehr
    # schafft das naechste Problem hinter uns. Der Fall, den Stufe 3 abdeckt,
    # ist das Abbiegen/Einfaedeln aus dem Stand heraus.
    BRAKE_SPEED_KMH = 30.0
    # "Deutlich schneller". Darunter reicht Lenken oder Gaswegnehmen, und ein
    # Eingriff waere eine Bevormundung; darueber ist die Zeit dafuer weg.
    MIN_APPROACH_DELTA_KMH = 10.0
    # Restabstand zum Fahrschlauch des anderen und Wirkzeit des Eingriffs -
    # dieselben Groessen und dieselbe Begruendung wie in der
    # Querverkehrswarnung.
    SAFETY_BUFFER_M = 1.0
    REACTION_TIME_S = 0.2
    # Laengenzugabe fuer den Naeherungsfilter der Akutstufen: zwei Fahrzeuge
    # von je 5 m koennen sich schon beruehren, wenn ihre Mittelpunkte noch
    # 10 m auseinander sind.
    PAIR_EXTENT_M = 10.0
    # ─── Was die Akutstufen *nicht* sind ──────────────────────────────
    # Ein Auto, das in unserer eigenen Spur hinter uns auflaeuft, ist kein
    # Spurwechselkonflikt, sondern ein Auffahrender - und auf welcher Seite es
    # angezeigt wuerde, entscheidet bei einem mittig folgenden Auto das
    # Rauschen. Solche Paare werden aussortiert: gleiche Spur *und* parallele
    # Fahrtrichtung. Sobald einer von beiden einlenkt, ist es wieder ein Fall
    # fuer dieses System - genau der Moment, um den es geht.
    #
    # Toleranz: eine halbe Fahrzeugbreite ueber die Summe der beiden halben
    # Breiten hinaus, damit ein leicht versetzt folgendes Auto noch als
    # "gleiche Spur" zaehlt.
    SAME_LANE_TOLERANCE_M = 0.5
    # 2 Grad. Deutlich ueber der Aufloesung des Headings (1/182 Grad) und
    # deutlich unter dem Winkel, den ein beginnender Spurwechsel erzeugt.
    MIN_CONVERGENCE_SIN = 0.035
    # ...und das Auto *vor* uns ist es genauso wenig.
    #
    # ``_is_plain_following`` sortiert das hintere Auto in der eigenen Spur
    # aus, solange beide parallel stehen. Genau das hoert im Moment eines
    # Auffahrunfalls auf zu gelten: beide Autos drehen sich, der Winkel
    # zwischen ihnen reisst die 2-Grad-Schwelle, und das Auto, in das wir
    # gerade hineinfahren, wird zur Akutwarnung des toten Winkels - auf einer
    # Seite, die das Vorzeichen eines fast verschwindenden Kreuzprodukts
    # entscheidet. Mal links, mal rechts, und wegen der Haltezeit oft beides
    # gleichzeitig (known-issues #52).
    #
    # Der tote Winkel ist per Definition neben und hinter uns - der
    # Stufe-1-Korridor deckt genau das ab (90 Grad bis 180 Grad). Die
    # Akutstufen hatten diese Schranke nie. Sie bekommen sie in
    # ``_is_longitudinal_traffic``, und zwar aus **zwei** Teilen, weil keiner
    # allein reicht:
    #
    # * **vor uns** - der Mittelpunkt des anderen Autos liegt vor unserer
    #   Frontstossstange (halbe eigene Laenge). Allein genommen kostet das den
    #   Fall, um den es hier geht: wer uns ueberholt, schiebt seinen
    #   Mittelpunkt an unserem vorbei, lange bevor der Konflikt vorbei ist.
    # * **in unserer Spur** - derselbe Querversatz wie in
    #   ``_is_plain_following``. Allein genommen ist das die Bedingung, die
    #   beim Aufprall zerfaellt.
    #
    # Zusammen beschreiben sie genau den Vordermann: vor uns, in unserer
    # Spur, egal wie verdreht die beiden im Moment des Aufpralls stehen. Der
    # gehoert der Kollisionswarnung, die ihn mit der richtigen Physik
    # behandelt. Ein Auto vor uns in der *Nachbarspur* bleibt ein Fall fuer
    # die Akutstufe - dorthin koennen wir lenken.

    # ─── Und im Stillstand gibt es keine Akutstufe ────────────────────
    #
    # Eine Toter-Winkel-Warnung sagt "fahr da jetzt nicht hin". Wer steht,
    # faehrt nirgendwo hin: die Kontaktvorhersage schreibt unseren Umriss
    # ueber den ganzen Horizont an dieselbe Stelle fort, jede vorhergesagte
    # Beruehrung kommt also allein aus der Bewegung des anderen. Daraus wurde
    # an der Ampel ein Piepen fuer jedes Auto, das vorbeikam
    # (known-issues #53) - und weder Warnen noch Bremsen ist die richtige
    # Antwort, wenn wir bereits stehen.
    #
    # 1 km/h = 0.28 m/s liegt klar unter allem, was Anfahren oder Rangieren
    # erzeugt, und klar ueber der Aufloesung der MCI-Geschwindigkeit. Der
    # Einfaedelfall der Stufe 3 bleibt erhalten: wer wirklich in den Verkehr
    # hinausfaehrt, bewegt sich dabei.
    MIN_ACUTE_OWN_SPEED_KMH = 1.0

    # ─── Haltezeit ────────────────────────────────────────────────────
    MEAN_VEHICLE_LENGTH_M = 4.5
    HOLD_MIN_S = 0.5
    HOLD_MAX_S = 2.0

    def __init__(self, event_bus: EventBus, settings: SettingsManager):
        super().__init__("blind_spot_warning", event_bus, settings)
        self.left_warning = False
        self.right_warning = False
        self.left_level = 0
        self.right_level = 0
        # Ablaufzeitpunkte der Haltezeit, siehe Klassendoku. Je Seite eine
        # fuer die Anzeige (Stufe 1) und eine fuer die Akutwarnung (Stufe 2).
        # Stufe 3 bekommt keine: sie fordert eine Bremsung an, und die darf
        # keine Sekunde laenger stehen als die Rechnung sie traegt -
        # ``EmergencyBrake`` bringt seine eigene Hysterese mit.
        self._left_until = 0.0
        self._right_until = 0.0
        self._left_acute_until = 0.0
        self._right_acute_until = 0.0
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
        own_body: Optional[Body] = None
        hit_left = hit_right = False
        acute_left = acute_right = False
        brake_left = brake_right = False
        deceleration = 0.0
        # Einmal fuer den ganzen Durchlauf, nicht je Fahrzeug: siehe
        # MIN_ACUTE_OWN_SPEED_KMH. Stufe 1 bleibt davon unberuehrt - dass
        # jemand im toten Winkel *steht*, darf der Fahrer auch im Stand
        # wissen; es blinkt und piept nur nicht.
        acute_possible = own.speed >= self.MIN_ACUTE_OWN_SPEED_KMH

        for vehicle in vehicles.values():
            data = vehicle.data
            distance = data.distance_to_player

            # ─── Vorfilter: nur Vergleiche, keine Allokation ──────────
            if distance > self.RANGE_GATE_M:
                continue
            if distance > self.NEAR_BYPASS_M and not (
                    self.SIDE_GATE_MIN_DEG <= data.angle_to_player <= self.SIDE_GATE_MAX_DEG):
                continue

            if not self._is_moving_relevantly(own.speed, data.speed):
                continue

            other_speed_ms = data.speed * KMH_TO_MS

            # ─── Stufen 2 und 3: Kontaktvorhersage ────────────────────
            # Naeherungsfilter zuerst: die Relativgeschwindigkeit ist nie
            # groesser als die Summe der beiden Betraege, also kann es
            # innerhalb von ACUTE_TTC_S nicht knallen, wenn wir weiter
            # auseinander sind als das.
            acute_level = 0
            if acute_possible and self._acute_prefilter(
                    distance, own_speed_ms, other_speed_ms,
                    own.heading, data.heading):
                if own_body is None:
                    own_body = body_from(own, own_speed_ms)
                other_body = body_from(data, other_speed_ms)
                acute_level, demand = self._acute_state(own, own_body,
                                                        other_body, data)
                if demand > deceleration:
                    deceleration = demand
                if acute_level:
                    on_left = self._is_on_left(own_body, other_body)
                    hold_until = now + self._hold_time(own_speed_ms,
                                                       other_speed_ms)
                    if on_left:
                        hit_left = True
                        acute_left = True
                        brake_left = brake_left or acute_level >= 3
                        self._left_until = max(self._left_until, hold_until)
                        self._left_acute_until = max(self._left_acute_until,
                                                     hold_until)
                    else:
                        hit_right = True
                        acute_right = True
                        brake_right = brake_right or acute_level >= 3
                        self._right_until = max(self._right_until, hold_until)
                        self._right_acute_until = max(self._right_acute_until,
                                                      hold_until)

            # ─── Stufe 1: der Korridor ────────────────────────────────
            if hit_left and hit_right:
                continue
            if not _is_within_threshold(own.heading, data.heading):
                continue
            if not self._is_relevant(distance, own_speed_ms, other_speed_ms):
                continue

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

        left_level = self._level_for(brake_left, acute_left, hit_left,
                                     self._left_acute_until, self._left_until,
                                     now)
        right_level = self._level_for(brake_right, acute_right, hit_right,
                                      self._right_acute_until,
                                      self._right_until, now)

        self._publish(left_level, right_level, deceleration)
        return {
            'left_warning': self.left_warning,
            'right_warning': self.right_warning,
            'left_level': self.left_level,
            'right_level': self.right_level,
            'deceleration': deceleration,
        }

    # ─── Vorfilter der Akutstufen ─────────────────────────────────────

    def _acute_prefilter(self, distance_m: float, own_speed_ms: float,
                         other_speed_ms: float, own_heading: float,
                         other_heading: float) -> bool:
        """Lohnt sich fuer dieses Fahrzeug ueberhaupt eine Kontaktrechnung?

        Zwei Vergleiche, vor jedem ``Body``:

        * **Naeherung** - die Relativgeschwindigkeit ist nie groesser als die
          Summe der beiden Betraege, also kann es innerhalb von ``ACUTE_TTC_S``
          nicht knallen, wenn wir weiter auseinander sind als das (plus der
          Laenge beider Autos, ``PAIR_EXTENT_M``).
        * **Richtung** - Querverkehr (90 Grad) und Gegenverkehr (180 Grad)
          haben eigene Systeme; hier geht es um jemanden, der ungefaehr in
          unsere Richtung faehrt.
        """
        reach = self.ACUTE_TTC_S * (own_speed_ms + other_speed_ms) \
            + self.PAIR_EXTENT_M
        if distance_m > reach:
            return False
        return _is_within_threshold(own_heading, other_heading,
                                    ACUTE_HEADING_THRESHOLD_UNITS)

    # ─── Ausgabe ──────────────────────────────────────────────────────

    def _level_for(self, braking: bool, acute: bool, present: bool,
                   acute_until: float, present_until: float,
                   now: float) -> int:
        if braking:
            return 3
        if acute or now < acute_until:
            return 2
        if present or now < present_until:
            return 1
        return 0

    def _publish(self, left_level: int, right_level: int,
                 deceleration: float):
        """Warnzustand nur bei Änderung, Sollverzögerung jeden Zyklus.

        ``left``/``right`` bleiben im Payload: sie sind die Frage "ist da
        ueberhaupt jemand", die es seit jeher gibt, und ein Abonnent, der die
        Stufen nicht kennt, soll weiter funktionieren (reference/events.md:
        Schluessel duerfen dazukommen, nie verschwinden).
        """
        self.event_bus.emit('needed_deceleration_update', {
            'deceleration': deceleration,
            'source': 'blind_spot',
        })
        if left_level == self.left_level and right_level == self.right_level:
            return
        self.left_level = left_level
        self.right_level = right_level
        self.left_warning = left_level > 0
        self.right_warning = right_level > 0
        self.event_bus.emit('blind_spot_warning_changed', {
            'left': self.left_warning,
            'right': self.right_warning,
            'left_level': left_level,
            'right_level': right_level,
        })

    # ─── Akutstufen ───────────────────────────────────────────────────

    @staticmethod
    def _is_on_left(own_body: Body, other_body: Body) -> bool:
        """Liegt das andere Auto links von unserer Fahrtrichtung?

        Dasselbe Kreuzprodukt wie in der Querverkehrswarnung: im
        rechtshaendigen LFS-System (conventions.md §1) heisst positiv links.
        Bewusst nicht ueber den Korridortreffer bestimmt - die Akutstufen
        haengen nicht am Korridor (siehe Klassendoku).
        """
        return (own_body.dx * (other_body.y - own_body.y)
                - own_body.dy * (other_body.x - own_body.x)) > 0.0

    def _acute_state(self, own, own_body: Body, other_body: Body,
                     data) -> Tuple[int, float]:
        """(Stufe, Sollverzoegerung) fuer genau ein Fahrzeug.

        Stufe ist 0, 2 oder 3 - Stufe 1 entsteht aus dem Korridor und nicht
        hier. Die Sollverzoegerung wird auch unterhalb von Stufe 3
        zurueckgegeben, sobald Bremsen ueberhaupt helfen wuerde: die Schwelle
        gehoert ``EmergencyBrake``, hier wird nur gerechnet.
        """
        if self._is_longitudinal_traffic(own_body, other_body):
            return 0, 0.0
        if self._is_plain_following(own_body, other_body):
            return 0, 0.0
        if not self._contact_is_imminent(own_body, other_body):
            return 0, 0.0
        free = free_distance(own_body, other_body)

        # ─── Stufe 3: Einfaedeln in fliessenden Verkehr ───────────────
        if own.speed >= self.BRAKE_SPEED_KMH:
            return 2, 0.0
        if data.speed - own.speed < self.MIN_APPROACH_DELTA_KMH:
            return 2, 0.0
        # "Von hinten": vor uns ist es kein Toter-Winkel-Fall, sondern
        # Laengsverkehr - und der gehoert der Kollisionswarnung.
        if (own_body.dx * (other_body.x - own_body.x)
                + own_body.dy * (other_body.y - own_body.y)) >= 0.0:
            return 2, 0.0

        if free == INF or free <= 0.0:
            # ``inf``: wir geraten gar nicht in seinen Fahrschlauch, er kommt
            # zu uns - dagegen hilft Bremsen nicht.
            # ``0``: wir stehen schon darin. Hier ist Bremsen sogar falsch -
            # er kommt von hinten und ist schneller, also verlaengert jede
            # Verzoegerung seine Annaeherung und haelt uns laenger in seinem
            # Weg. Das ist der Unterschied zur Querverkehrswarnung, wo wir
            # diejenigen sind, die auffahren.
            return 2, 0.0

        demand = stopping_deceleration(free, own_body.speed,
                                       self.SAFETY_BUFFER_M,
                                       self.REACTION_TIME_S)
        return (3 if demand >= BRAKE_DEMAND_MS2 else 2), demand

    def _contact_is_imminent(self, own_body: Body, other_body: Body) -> bool:
        """Beruehren sich die beiden Umrisse innerhalb von ``ACUTE_TTC_S``?

        Das ist die *ganze* Bedingung fuer Stufe 2, und es deckt beide Faelle
        ab, die der Fahrer als gefaehrlich erlebt: "ich komme ihm zu nahe"
        (Kontakt praktisch sofort) und "ich fahre in seinen Pfad" (Kontakt in
        ein bis zwei Sekunden, weil *wir* uns quer bewegen). Der zweite Fall
        haengt daran, dass :func:`contact_window` beim Lenken den Bogen
        mitrechnet - ohne das sieht ein unveraenderliches Heading den
        Spurwechsel erst, wenn er passiert ist.

        Hier stand vorher zusaetzlich ein Kriterium aus zwei Zeiten: "wir sind
        gleich in seinem Fahrschlauch" und "er ist uns dicht auf". Beide
        stimmten - aber sie beschrieben **verschiedene Augenblicke** und
        wurden trotzdem verglichen. In ``simulation_tests`` Szenario 25 (nach
        vorbeifahrendem Verkehr langsam rechts abbiegen) waren wir in 1.4 s in
        seinem Fahrschlauch und er 0.06 s hinter uns - und genau deshalb war
        er laengst 27 m weiter, wenn wir dort ankamen. Die Warnung blinkte und
        piepte fuer einen Konflikt, der nie einer war. Die Kontaktvorhersage
        beantwortet dieselbe Frage richtig, weil sie beide zur selben Zeit
        betrachtet.
        """
        window = contact_window(own_body, other_body)
        if window is None or window[1] < 0.0:
            # Kein Kontakt, oder er liegt hinter uns.
            return False
        if window[0] == -INF:
            # Die beiden ueberlappen "seit immer und fuer immer": gleiche
            # Geschwindigkeit, gleiche Richtung, Umrisse ineinander. Das ist
            # kein Ereignis, sondern ein Zustand, den LFS so gar nicht liefern
            # kann - und eine Warnung ohne Anfang koennte nie wieder aufhoeren.
            return False
        return window[0] <= self._prediction_horizon(own_body, other_body)

    def _prediction_horizon(self, own_body: Body, other_body: Body) -> float:
        """Wie weit voraus eine Beruehrung noch eine Warnung wert ist.

        Siehe ``STEADY_TTC_S``: die volle Vorwarnzeit nur, wenn die relative
        Gierrate ein Manoever zeigt.
        """
        if abs(own_body.yaw_rate - other_body.yaw_rate) >= MIN_MANOEUVRE_YAW:
            return self.ACUTE_TTC_S
        if min(abs(own_body.yaw_rate),
               abs(other_body.yaw_rate)) >= MIN_CORNERING_YAW:
            # Beide fahren eine Kurve, und keiner lenkt in den anderen. Dann
            # *ist* der Winkel zwischen ihnen die Kurve, und vorausgeschaut
            # wird gar nicht mehr: gewarnt wird nur noch, wenn sich die
            # Umrisse wirklich beruehren. Siehe STEADY_TTC_S.
            return 0.0
        return self.STEADY_TTC_S

    def _is_longitudinal_traffic(self, own_body: Body,
                                 other_body: Body) -> bool:
        """Faehrt das andere Auto schlicht vor uns in unserer Spur?

        Der Vordermann, und zwar auch dann noch, wenn wir gerade in ihn
        hineinfahren - siehe den Block bei ``MIN_CONVERGENCE_SIN``. Ohne diese
        Frage wurde ausgerechnet der Auffahrunfall zur Akutwarnung des toten
        Winkels, auf einer Seite, die das Rauschen entschied.

        Bewusst *hier* statt im Vorfilter: ``_is_on_left``, die
        Kontaktvorhersage und ``_is_plain_following`` brauchen ohnehin alle
        denselben ``Body``.
        """
        to_other_x = other_body.x - own_body.x
        to_other_y = other_body.y - own_body.y
        longitudinal = own_body.dx * to_other_x + own_body.dy * to_other_y
        if longitudinal <= own_body.length / 2.0:
            return False
        lateral = abs(own_body.dx * to_other_y - own_body.dy * to_other_x)
        return lateral <= (own_body.width + other_body.width) / 2.0 \
            + self.SAME_LANE_TOLERANCE_M

    def _is_plain_following(self, own_body: Body, other_body: Body) -> bool:
        """Faehrt das andere Auto schlicht in unserer Spur hinter uns her?

        Gleiche Spur (Querversatz kleiner als die beiden halben Breiten plus
        Toleranz) **und** parallele Fahrtrichtung. Siehe
        ``SAME_LANE_TOLERANCE_M`` - beides muss zutreffen, damit ein
        beginnendes Einlenken das Paar sofort wieder interessant macht.
        """
        to_other_x = other_body.x - own_body.x
        to_other_y = other_body.y - own_body.y
        lateral = abs(own_body.dx * to_other_y - own_body.dy * to_other_x)
        if lateral > (own_body.width + other_body.width) / 2.0                 + self.SAME_LANE_TOLERANCE_M:
            return False
        converging = abs(own_body.dx * other_body.dy
                         - own_body.dy * other_body.dx)
        return converging <= self.MIN_CONVERGENCE_SIN

    # ─── Relevanz und Haltezeit ───────────────────────────────────────

    def _is_moving_relevantly(self, own_speed_kmh: float,
                              other_speed_kmh: float) -> bool:
        """Kann dieses Auto ueberhaupt ein Spurwechselkonflikt sein?

        Zwei Vergleiche, vor jeder Geometrie - siehe MIN_OTHER_SPEED_KMH und
        MAX_TRAILING_SPEED_KMH. Absichtlich in km/h: beide Schwellen sind so
        formuliert, wie der Fahrer sie beschreibt, und die Umrechnung nach m/s
        wuerde nur eine Multiplikation pro Fahrzeug hinzufuegen.
        """
        if other_speed_kmh < self.MIN_OTHER_SPEED_KMH:
            return False
        return other_speed_kmh >= own_speed_kmh - self.MAX_TRAILING_SPEED_KMH

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
