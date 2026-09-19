import math
from typing import Dict, Any, Optional

from assistance.base_system import AssistanceSystem
from assistance.path_conflict import (
    BRAKE_DEMAND_MS2, INF, body_from, contact_window, direction_vector,
    free_distance, stopping_deceleration)
from core.event_bus import EventBus
from core.settings_manager import SettingsManager
from misc.helpers import is_reversing
from vehicles.own_vehicle import OwnVehicle
from vehicles.vehicle import Vehicle

KMH_TO_MS = 0.277778
METRE = 65536.0     # MCI-Positionseinheiten pro Meter

# Alias auf die gemeinsame Geometrie. Die Herleitung des Vektors steht dort;
# hier bleibt der Name, weil er die Historie dieses Moduls traegt
# (known-issues #16: der Kommentar behauptete jahrelang ein linkshaendiges
# Koordinatensystem, der Code war immer richtig).
_direction_vector = direction_vector


def _compute_side(own_dx: float, own_dy: float, own_x: float, own_y: float,
                  other_x: float, other_y: float) -> str:
    """Bestimmt, ob das andere Fahrzeug von links oder rechts kommt.

    Nutzt das Kreuzprodukt des eigenen Richtungsvektors mit dem Vektor
    vom eigenen Fahrzeug zum anderen Fahrzeug.

    Das LFS-System ist rechtshändig (X Ost, Y Nord), also gilt die
    Standard-Mathematik ohne Vorzeichenumkehr: ein positives 2D-Kreuzprodukt
    ``own_dir × to_other`` heißt, das andere Fahrzeug liegt gegen den
    Uhrzeigersinn von unserer Fahrtrichtung - und das ist **links**.
    Negativ heißt rechts.

    Beispiel: wir fahren nach Norden (0, 1), das andere Auto steht im Osten
    (+X). Dann ist cross = 0*0 - 1*10 = -10 < 0, also rechts - was stimmt.

    Der frühere Kommentar erklärte dasselbe Ergebnis mit einem linkshändigen
    Koordinatensystem, das es nicht gibt (known-issues #16). Das Verhalten
    bleibt unverändert, nur die Begründung stimmt jetzt.

    Returns:
        'left' oder 'right'
    """
    # Vektor zum anderen Fahrzeug
    to_other_x = other_x - own_x
    to_other_y = other_y - own_y

    # 2D Kreuzprodukt: own_dir × to_other
    cross = own_dx * to_other_y - own_dy * to_other_x

    # Rechtshändiges System: negativ = rechts, positiv = links
    return 'right' if cross < 0 else 'left'


class CrossTrafficWarning(AssistanceSystem):
    """Querverkehrswarnung - warnt vor kreuzenden Fahrzeugen und bremst.

    Zwei Ausgaben aus einer Rechnung (``assistance/path_conflict.py``):

    * ``cross_traffic_warning_changed`` - die Anzeige, Stufe 1 visuell,
      Stufe 2 zusaetzlich akustisch und blinkend.
    * ``needed_deceleration_update`` mit ``source='cross_traffic'`` - die
      Sollverzoegerung fuer ``EmergencyBrake``, jeden Zyklus, genau wie die
      Kollisionswarnung sie liefert. Ob daraus ein Eingriff wird, entscheidet
      allein ``EmergencyBrake`` und nur bei ``automatic_emergency_brake == 2``
      (reference/control-intervention.md).

    **Beide Fahrzeuge sind Rechtecke.** Die alte Rechnung verglich die
    Ankunftszeiten zweier *Punkte* am Schnittpunkt der beiden Fahrwege und
    erlaubte dafuer ein Zeitfenster, das aus den Fahrzeuggroessen geschaetzt
    wurde. Das genuegte fuer eine Warnung, nicht fuer einen Bremseingriff: ein
    Eingriff muss wissen, **wo** der Konflikt anfaengt, nicht nur **wann**. Die
    Kontaktzeit kommt jetzt aus dem Separating Axis Theorem ueber beide
    Umrisse, der Bremsweg aus dem Abstand bis zum Fahrschlauch des anderen.
    """

    # ─── Erfassungsbereich ────────────────────────────────────────────
    # Vorauswahl auf dem Abstand, den der VehicleManager ohnehin je Frame
    # rechnet - ein Vergleich statt einer Strahlenschnittrechnung pro
    # Fahrzeug. Weiter weg gibt es keinen Querverkehr, um den es sich zu
    # kuemmern lohnt.
    MAX_RANGE_M = 100.0
    # Minimaler Kreuzungswinkel (Grad) um nahezu parallele Fahrzeuge
    # auszuschliessen. Alles darunter ist Laengsverkehr und gehoert der
    # Kollisions- bzw. der Toter-Winkel-Warnung.
    MIN_CROSSING_ANGLE_DEG = 20.0
    # Unterhalb dieser Geschwindigkeit gibt es keine Querverkehrswarnung.
    MIN_OWN_SPEED_KMH = 5.0
    MIN_OTHER_SPEED_KMH = 3.0
    # Weiter als so voraus ist eine Vorhersage mit konstanter Geschwindigkeit
    # nichts wert - beide Fahrer lenken und bremsen in der Zwischenzeit.
    MAX_PREDICTION_S = 6.0

    # ─── Bremseingriff ────────────────────────────────────────────────
    # Restabstand zum Fahrschlauch des anderen, den wir nicht aufbrauchen
    # wollen. Groesser als die 0.5 m der Kollisionswarnung: dort haben wir es
    # mit einem Fahrzeug zu tun, das in dieselbe Richtung faehrt, hier mit
    # einem, das quer durch unsere Front will.
    SAFETY_BUFFER_M = 1.0
    # Zeit, bis ein Eingriff wirkt: ein 100-ms-Zyklus plus Tastendruck plus
    # LFS' eigener Bremsdruckaufbau. Dieselbe Groessenordnung wie in der
    # Kollisionswarnung.
    REACTION_TIME_S = 0.2
    # Weiter voraus wird nicht gebremst. Die Sollverzoegerung waere dort
    # ohnehin klein, aber ein Konflikt in 5 s ist eine Vorhersage und kein
    # Grund, jemandem die Kontrolle abzunehmen.
    BRAKE_HORIZON_S = 4.0
    # ─── Warnstufen aus der Sollverzoegerung ──────────────────────────
    # Eine Warnung muss vor dem Eingriff kommen, und die Zeitschwellen oben
    # koennen das nicht garantieren: der Bremsweg waechst mit v², die
    # Kontaktzeit nur mit v. Gemessen in ``simulation_tests`` Szenario 08 -
    # mit Vollgas auf die Kreuzung zu war der Bedarf bei 6.25 m/s², also
    # ueber der Eingriffsschwelle, waehrend die Kontaktzeit noch bei 3.7 s
    # lag und damit unter jeder Warnstufe. Der Fahrer bekam die Bremsung
    # ohne vorherige Warnung.
    #
    # Deshalb zweitens: dieselbe Zahl, die den Eingriff ausloest, traegt auch
    # die Anzeige. Als Anteil von ``BRAKE_DEMAND_MS2`` formuliert, damit die
    # Reihenfolge Anzeige -> Ton -> Bremse per Konstruktion stimmt und nicht
    # per Zufall.
    VISUAL_DEMAND_FRACTION = 0.4
    ACOUSTIC_DEMAND_FRACTION = 0.75

    def __init__(self, event_bus: EventBus, settings: SettingsManager):
        super().__init__("cross_traffic_warning", event_bus, settings)
        self.current_warning_level = 0
        self.current_side = None  # 'left' oder 'right'

    def process(self, own_vehicle: OwnVehicle, vehicles: Dict[int, Vehicle]) -> Dict[str, Any]:
        """Prüft auf Querverkehr-Kollisionsgefahr"""
        warning_level = 0
        warning_side = None
        min_ttc = INF
        deceleration = 0.0

        # Einmal binden: OutGauge schreibt nebenläufig in own_vehicle.data
        # (known-issues #12).
        own = own_vehicle.data

        # Vorher hing die Warnung an ``own_vehicle.gear <= 1``. Das ist der
        # rohe OutGauge-Gang (0 = Rückwärts, 1 = Leerlauf), also gab es weder
        # im Leerlauf noch beim Rückwärtsfahren eine Warnung - und für ein
        # Auto ohne gemeldeten Gang (kein OutGauge, Automatik im Leerlauf,
        # Rollen) überhaupt keine. Gefährlich ist aber die Bewegung, nicht der
        # Gang: geprüft wird jetzt die Geschwindigkeit und - über den
        # modularen Heading/Direction-Vergleich - ob wir rückwärts rollen.
        # Beim Rückwärtsfahren zeigt der Richtungsvektor aus dem Heading in
        # die falsche Richtung, dann wäre jeder Schnittpunkt falsch.
        if (not self.is_enabled() or own.speed < self.MIN_OWN_SPEED_KMH
                or is_reversing(own.heading, own.direction)):
            self._publish(0, None, 0.0)
            return {'level': 0, 'side': None, 'ttc': INF, 'deceleration': 0.0}

        # Warnschwellen basierend auf Einstellung (0=Early, 1=Medium, 2=Late).
        # Die Schwellen sind Zeiten bis zur **Berührung**, nicht mehr bis zum
        # Schnittpunkt der Mittelpunkte - für ein 4.5 m langes Auto sind das
        # rund 0.2 bis 0.4 s Unterschied, die dem Fahrer zugutekommen.
        ctw_dist = self.settings.get("cross_traffic_warning_distance")
        if ctw_dist == 0:
            visual_threshold = 3.5
            acoustic_threshold = 3.0
        elif ctw_dist == 2:
            visual_threshold = 1.5
            acoustic_threshold = 1.0
        else:  # 1 = Medium (default)
            visual_threshold = 2.5
            acoustic_threshold = 1.5

        own_speed_ms = own.speed * KMH_TO_MS
        own_body = body_from(own, own_speed_ms)
        visual_demand = self.VISUAL_DEMAND_FRACTION * BRAKE_DEMAND_MS2
        acoustic_demand = self.ACOUSTIC_DEMAND_FRACTION * BRAKE_DEMAND_MS2

        for vehicle in vehicles.values():
            data = vehicle.data
            if data.speed < self.MIN_OTHER_SPEED_KMH:
                # Stehendes/sehr langsames Fahrzeug ignorieren
                continue
            if data.distance_to_player > self.MAX_RANGE_M:
                continue

            other_body = body_from(data)

            # Kreuzungswinkel prüfen (parallele Fahrzeuge ausschließen)
            dot = own_body.dx * other_body.dx + own_body.dy * other_body.dy
            dot = max(-1.0, min(1.0, dot))  # Clamp für acos
            if math.degrees(math.acos(abs(dot))) < self.MIN_CROSSING_ANGLE_DEG:
                # Fast parallel/gleiche Richtung – kein Querverkehr
                continue

            # Wie weit dürfen wir noch, bevor wir in seinem Fahrschlauch
            # stehen? ``inf`` heißt "nie hinein oder schon hindurch" - dann
            # ist er kein Querverkehr für uns, egal was die Zeiten sagen.
            free = free_distance(own_body, other_body)
            if free == INF:
                continue

            window = contact_window(own_body, other_body)
            if window is None or window[1] < 0.0:
                # Kein Kontakt, oder er liegt hinter uns.
                continue

            ttc = window[0] if window[0] > 0.0 else 0.0
            if ttc > self.MAX_PREDICTION_S:
                continue

            demand = 0.0
            if ttc <= self.BRAKE_HORIZON_S and not self._overtaking_us(
                    own_body, other_body):
                demand = stopping_deceleration(free, own_speed_ms,
                                               self.SAFETY_BUFFER_M,
                                               self.REACTION_TIME_S)
                if demand > deceleration:
                    deceleration = demand

            # Zwei Wege zu einer Stufe, und die hoehere gewinnt: die
            # Kontaktzeit (wie nah ist es zeitlich) und die Sollverzoegerung
            # (wie nah ist es an dem Punkt, an dem nur noch Bremsen hilft).
            level = 0
            if ttc < acoustic_threshold or demand >= acoustic_demand:
                level = 2
            elif ttc < visual_threshold or demand >= visual_demand:
                level = 1

            # Die Seite gehoert dem Fahrzeug, das die Stufe traegt; bei
            # gleicher Stufe dem naeheren.
            if level > warning_level or (level == warning_level
                                         and level > 0 and ttc < min_ttc):
                warning_level = level
                warning_side = _compute_side(own_body.dx, own_body.dy,
                                             own_body.x, own_body.y,
                                             other_body.x, other_body.y)
            if ttc < min_ttc:
                min_ttc = ttc

        self._publish(warning_level, warning_side, deceleration)

        return {
            'level': warning_level,
            'side': warning_side,
            'ttc': min_ttc,
            'deceleration': deceleration,
        }

    @staticmethod
    def _overtaking_us(own_body, other_body) -> bool:
        """Kommt er von hinten und ist schneller als wir?

        Der Kreuzungswinkel geht bis 20 Grad hinunter, und darunter faengt der
        Bereich der Toter-Winkel-Warnung an - ein Auto, das uns mit 28 Grad
        Winkelunterschied dicht ueberholt, sieht von hier aus wie Querverkehr.
        Fuer die *Warnung* ist das in Ordnung. Fuer einen Bremseingriff nicht:
        er ist hinter uns und schneller, also nimmt Bremsen uns nicht aus
        seinem Weg, sondern verlaengert seine Annaeherung und erhoeht die
        Geschwindigkeit, mit der er ankommt
        (reference/control-intervention.md, Tabelle der Quellen). Diese
        Situation gehoert der Toter-Winkel-Warnung, die dort richtig
        entscheidet.

        Gemessen: in ``simulation_tests`` Szenario 22 forderte die
        Querverkehrswarnung in dem Zyklus, in dem der ueberholende Wagen
        seitlich an uns vorbeischrammte, 20 m/s2 an - genau die falsche
        Antwort, nur unauffaellig, weil der Toter-Winkel-Eingriff ohnehin
        schon lief.
        """
        behind = (own_body.dx * (other_body.x - own_body.x)
                  + own_body.dy * (other_body.y - own_body.y)) < 0.0
        return behind and other_body.speed > own_body.speed

    # ─── Ausgabe ──────────────────────────────────────────────────────

    def _publish(self, warning_level: int, warning_side: Optional[str],
                 deceleration: float):
        """Warnzustand nur bei Änderung, Sollverzögerung jeden Zyklus.

        Der Vertrag von ``needed_deceleration_update`` ist bewusst derselbe
        wie bei der Kollisionswarnung (reference/events.md): sein Abonnent
        greift in die Fahrzeugführung ein, und eine Anforderung, die
        *ausbleibt*, muss von einer Anforderung "0" unterscheidbar sein.
        """
        self.event_bus.emit('needed_deceleration_update', {
            'deceleration': deceleration,
            'source': 'cross_traffic',
        })
        if warning_level != self.current_warning_level or warning_side != self.current_side:
            self.current_warning_level = warning_level
            self.current_side = warning_side
            self.event_bus.emit('cross_traffic_warning_changed', {
                'level': warning_level,
                'side': warning_side,
            })
