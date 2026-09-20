"""Wann beruehren sich zwei fahrende Rechtecke - und wie weit duerfen wir noch?

Gemeinsame Geometrie der Querverkehrswarnung (``cross_traffic_warning.py``)
und der Toter-Winkel-Warnung (``blind_spot_warning.py``). Beide beantworten im
Kern dieselben zwei Fragen, nur mit anderen Winkeln:

1. **Kracht es, und wann?** - :func:`contact_window`. Fahrzeuge sind Koerper,
   keine Punkte: ein 4.5 m langes Auto belegt eine Kreuzung ueber seine ganze
   Laenge, und ein Punktmodell uebersieht genau das.
2. **Wie weit duerfen wir noch?** - :func:`free_distance`. Der Weg, den unser
   Mittelpunkt entlang der eigenen Fahrtrichtung noch zuruecklegen darf, bevor
   unser Umriss den *Fahrschlauch* des anderen beruehrt. Daraus wird mit
   :func:`stopping_deceleration` die Sollverzoegerung fuer den Notbremseingriff.

Modell und Annahmen - sie gehoeren hierher, nicht in den Kopf des Lesers
(``AGENTS.md`` §1.2):

* **Konstante Geschwindigkeit** ueber den Vorhersagehorizont von ein bis drei
  Sekunden. Bei 100-ms-Takt ist das die uebliche und belastbare Naeherung.
* **Feste Ausrichtung, plus der Bogen.** Beide Funktionen rechnen zuerst mit
  unveraendertem Heading und pruefen zusaetzlich den Kreisbogen aus der
  **relativen** Gierrate (``CompCar.AngVel``, siehe ``Body.yaw_rate``). Der
  Grund: ein beginnender Spurwechsel ist am Heading erst abzulesen, wenn er
  stattgefunden hat. Drei Regeln halten das ehrlich:

  - Der Bogen darf eine Antwort nur **vorziehen**, nie verschieben. Ein Modell
    mit konstanter Gierrate ist eine Behauptung, keine Vorhersage.
  - Er wird hoechstens ``_YAW_HORIZON_S`` weit fortgeschrieben.
  - Gerechnet wird mit der **relativen** Gierrate, nicht mit zwei eigenen. In
    einer Kurve, die beide zusammen nehmen, hebt sie sich auf - und nur so
    unterscheidet die Rechnung "er lenkt in mich hinein" von "wir fahren
    dieselbe Kurve".
* **Fahrtrichtung = Heading.** Der Schraeglaufwinkel zwischen "wohin das Auto
  zeigt" und "wohin es faehrt" ist in den Situationen, um die es hier geht
  (Kreuzung, Einfaedeln), klein, und ``Direction`` ist laut InSim.txt nur bei
  Bewegung ueberhaupt gueltig. Wer rueckwaerts rollt, muss deshalb vorher
  aussortiert werden (``misc.helpers.is_reversing``) - sonst zeigt der Vektor
  in die falsche Richtung.
* **Separating Axis Theorem.** Zwei Rechtecke ueberlappen genau dann *nicht*,
  wenn sich ihre Projektionen auf mindestens einer der vier Kantennormalen
  trennen. Bei konstanter Geschwindigkeit liefert jede dieser Achsen ein
  Zeitintervall der Ueberlappung; der Schnitt der vier ist das Kontaktfenster.
  Das ist exakt fuer Rechtecke, keine Naeherung - und es ist der Grund, warum
  hier nichts mit Radien oder Mittelpunktsabstaenden gerechnet wird.

Kosten: vier Achsen mit je rund zehn Gleitkommaoperationen, also etwa 60
Multiplikationen pro Fahrzeugpaar, plus ein ``Body`` pro Fahrzeug. Bezahlt wird
das nur fuer Fahrzeuge, die die Vorfilter der beiden Systeme ueberstanden haben
- im Normalfall keines bis zwei von ~40.
"""

import math
from dataclasses import dataclass
from typing import Optional, Tuple

from assistance.park_distance_control import conservative_vehicle_size

METRE = 65536.0             # MCI-Positionseinheiten pro Meter
KMH_TO_MS = 1.0 / 3.6
HEADING_OFFSET = 16384      # +90 Grad: von "0 = +Y" auf "0 = +X"
HEADING_DIVISOR = 182.05    # LFS-Heading-Einheiten pro Grad

INF = float('inf')

# Unterhalb dieser Rate ist eine Achse praktisch parallel - die Division waere
# nur noch Rauschen mal einer grossen Zahl.
_EPS = 1e-9
# Restweg, unter dem "anhalten" keine sinnvolle Rechnung mehr ist.
_MIN_USABLE_M = 0.01

# ─── Kurvenfahrt ──────────────────────────────────────────────────────────
# Unterhalb dieser *relativen* Gierrate wird geradeaus gerechnet.
#
# **Gemessen, nicht geschaetzt** (``simulation_tests``, 2026-09-19, relative
# Gierrate aller Paare naeher als 40 m):
#
# ====================================  ======  ======  ======
# Szenario                              Median  p90     Max
# ====================================  ======  ======  ======
# 24 nebeneinander durch die Kurve         2.8     5.1    10.6
# 21 normaler toter Winkel                 0.1     0.7     1.8
# 22 Abbiegen in fliessenden Verkehr       0.8    26.3    33.8
# 23 Ausweichen bei 60 km/h                2.3    24.6    52.4
# ====================================  ======  ======  ======
#
# Zwei Autos, die dieselbe Kurve nebeneinander nehmen, unterscheiden sich
# allein durch Linienwahl und Lenkkorrekturen um bis zu 10 Grad/s - das ist
# kein Manoever, das ist die Kurve. Ein echtes Einlenken liegt in dem Moment,
# in dem es zaehlt, bei 20 Grad/s und darueber. Die Schwelle liegt dazwischen.
#
# Der Wert stand zuerst bei 1 Grad/s. Damit wurde in Szenario 24 aus
# Kurvenrauschen eine vorhergesagte Kollision (``AGENTS.md`` §3: ein Warner,
# der in jeder Kurve warnt, wird abgeschaltet).
_MIN_RELATIVE_YAW_RATE = 0.21     # rad/s, rund 12 Grad/s
# Derselbe Wert unter dem Namen, unter dem ihn ein Aufrufer braucht: "hier
# lenkt jemand wirklich". ``BlindSpotWarning`` bindet seinen Vorhersagehorizont
# daran.
MIN_MANOEUVRE_YAW = _MIN_RELATIVE_YAW_RATE

# Und die zweite Schwelle, auf der *absoluten* Gierrate eines einzelnen Autos:
# "faehrt dieses hier gerade eine Kurve". Gemessen ueber dieselben Laeufe, je
# Fahrzeug:
#
#   geradeaus (08, 09, 21)      Median 0.1-0.2, Maximum 1.7 Grad/s
#   in der Kurve (24)           Median 5.3-5.9, p90 12-14 Grad/s
#
# 2 Grad/s liegt ueber allem, was eine Gerade erzeugt, und unter allem, was
# eine Kurve erzeugt. Die Kurve ist damit klar *erkennbar* - sie laesst sich
# nur nicht **fortschreiben**, siehe :func:`contact_window`. Genau dafuer wird
# sie erkannt: ``BlindSpotWarning`` schaut in einer gemeinsamen Kurve gar
# nicht voraus.
MIN_CORNERING_YAW = 0.035    # rad/s, rund 2 Grad/s

# Rein numerisch: darunter ist die Division durch die Gierrate im Bogen kein
# Kreis mehr, sondern Rundungsfehler. Keine Aussage ueber Fahrverhalten -
# dafuer sind die beiden Schwellen darueber da.
_YAW_EPS = 1e-6
# So weit wird eine Gierrate hoechstens fortgeschrieben. Ein Fahrer lenkt
# nicht eine Sekunde lang mit unveraenderter Rate, und 1.5 s bei 40 Grad/s
# waeren bereits 60 Grad Drehung - mehr darf ein Modell mit konstanter
# Gierrate nicht behaupten.
_YAW_HORIZON_S = 1.5
# Aufloesung der Abtastung und Zahl der Halbierungsschritte danach. 12
# Schritte ueber 1.5 s sind 0.125 s, vier Halbierungen bringen das auf 8 ms -
# bei 7 m/s also 6 cm, weit unter allem, was die Rechnung sonst an
# Genauigkeit hat.
_YAW_STEPS = 12
_YAW_REFINE = 4

# Sollverzoegerung, ab der ``EmergencyBrake`` eingreift. Die Autoritaet dafuer
# ist ``EmergencyBrake.ENGAGE_DECELERATION_MS2``; hier steht eine Kopie, damit
# ein warnendes System seine eigene Stufe "ab hier wird gebremst" benennen
# kann, ohne den Bremseingriff zu importieren (``AGENTS.md`` §5: kein System
# haelt eine Referenz auf ein anderes). ``tests/test_path_conflict.py`` haelt
# die beiden Werte aneinander fest.
BRAKE_DEMAND_MS2 = 6.0
# Kein sinnvoller Bremsweg mehr - derselbe Panikwert wie in der
# Kollisionswarnung, deutlich ueber allem, was Reifen hergeben.
PANIC_DECELERATION_MS2 = 20.0


def direction_vector(heading) -> Tuple[float, float]:
    """Normierter Fahrtrichtungsvektor aus einem LFS-Heading-Wort.

    Im LFS-Koordinatensystem (reference/conventions.md §1) waechst X nach
    Osten, Y nach Norden; Headings zaehlen **gegen** den Uhrzeigersinn ab der
    +Y-Achse. Der Summand ``HEADING_OFFSET`` dreht von "0 = +Y" auf "0 = +X",
    damit das Ergebnis direkt in cos/sin passt.
    """
    rad = math.radians((heading + HEADING_OFFSET) / HEADING_DIVISOR)
    return math.cos(rad), math.sin(rad)


@dataclass(frozen=True)
class Body:
    """Ein Fahrzeug als Rechteck mit konstanter Geschwindigkeit.

    Alles in SI: Position in Metern, ``speed`` in m/s, ``length``/``width`` in
    Metern. ``dx``/``dy`` ist der **normierte** Fahrtrichtungsvektor; die
    Ausrichtung des Rechtecks ist dieselbe (siehe Modulkopf).
    """

    x: float
    y: float
    dx: float
    dy: float
    speed: float
    length: float
    width: float
    # Gierrate in rad/s, positiv gegen den Uhrzeigersinn. 0 heisst geradeaus.
    yaw_rate: float = 0.0

    @property
    def forward(self) -> Tuple[float, float]:
        return self.dx, self.dy

    @property
    def left(self) -> Tuple[float, float]:
        """Die Quer-Achse. Rechtshaendiges System: +90 Grad ist links."""
        return -self.dy, self.dx

    def half_extent(self, ax: float, ay: float) -> float:
        """Halbe Ausdehnung des Rechtecks entlang der Einheitsachse (ax, ay).

        Die Stuetzfunktion eines achsenparallelen Rechtecks, in das eigene
        Koordinatensystem gedreht: laengs mal |cos|, quer mal |sin|. Fuer ein
        Auto quer zur Blickrichtung ist das seine halbe *Laenge*, laengs seine
        halbe *Breite* - der Unterschied, der ein Punktmodell falsch macht.
        """
        return _half_extent(self.length, self.width, self.dx, self.dy, ax, ay)

    def at(self, seconds: float) -> Tuple[float, float, float, float]:
        """Position und Blickrichtung nach ``seconds`` Fahrt.

        Konstante Geschwindigkeit, konstante Gierrate: der Wagen faehrt einen
        Kreisbogen. Fuer ``yaw_rate == 0`` faellt es auf die Gerade zurueck.
        Zurueck kommt ``(x, y, dx, dy)`` - bewusst ein Tupel und kein neuer
        ``Body``, damit eine Abtastung mit einem Dutzend Schritten nichts
        allokiert.
        """
        turn = self.yaw_rate * seconds
        if abs(self.yaw_rate) < _YAW_EPS:
            return (self.x + self.dx * self.speed * seconds,
                    self.y + self.dy * self.speed * seconds,
                    self.dx, self.dy)
        sin_turn = math.sin(turn)
        cos_turn = math.cos(turn)
        # Bogen: die Integrale von cos/sin der sich drehenden Richtung.
        along = self.speed * sin_turn / self.yaw_rate
        across = self.speed * (1.0 - cos_turn) / self.yaw_rate
        left_x, left_y = self.left
        return (self.x + self.dx * along + left_x * across,
                self.y + self.dy * along + left_y * across,
                self.dx * cos_turn - self.dy * sin_turn,
                self.dx * sin_turn + self.dy * cos_turn)


def _half_extent(length: float, width: float, dx: float, dy: float,
                 ax: float, ay: float) -> float:
    return ((length / 2.0) * abs(ax * dx + ay * dy)
            + (width / 2.0) * abs(-ax * dy + ay * dx))


def body_from(data, speed_ms: Optional[float] = None) -> Body:
    """``Body`` aus einem ``VehicleData``-Schnappschuss.

    ``data`` ist bereits gebunden zu uebergeben (``data = vehicle.data``) -
    OutGauge und der MCI-Thread schreiben nebenlaeufig (known-issues #12).
    ``speed_ms`` erlaubt es, eine bereits umgerechnete Geschwindigkeit
    weiterzureichen, statt sie ein zweites Mal zu teilen.
    """
    dx, dy = direction_vector(data.heading)
    # Konservativ, nicht Mittelklasse: aus diesen Massen entstehen die
    # Kontaktfenster von Quer- und Toter-Winkel-Warnung, und ein zu klein
    # angesetztes Auto findet den Kontakt nicht (known-issues #28).
    length, width = conservative_vehicle_size(data.cname)
    return Body(data.x / METRE, data.y / METRE, dx, dy,
                data.speed * KMH_TO_MS if speed_ms is None else speed_ms,
                length, width, getattr(data, 'yaw_rate', 0.0))


# ─── Eindimensionale Fenster ──────────────────────────────────────────────

def _overlap_window(offset: float, rate: float,
                    reach: float) -> Optional[Tuple[float, float]]:
    """Parameterintervall, in dem ``|offset + rate * p| <= reach`` gilt.

    ``p`` ist je nach Aufrufer eine Zeit oder ein Weg - die Form ist dieselbe.
    ``None`` heisst "nie".
    """
    if abs(rate) < _EPS:
        return (-INF, INF) if abs(offset) <= reach else None
    a = (-reach - offset) / rate
    b = (reach - offset) / rate
    return (a, b) if a <= b else (b, a)


def _halfspace_window(offset: float, rate: float,
                      minimum: float) -> Optional[Tuple[float, float]]:
    """Parameterintervall, in dem ``offset + rate * p >= minimum`` gilt."""
    if abs(rate) < _EPS:
        return (-INF, INF) if offset >= minimum else None
    bound = (minimum - offset) / rate
    return (bound, INF) if rate > 0 else (-INF, bound)


def _bodies_overlap(own: Body, other: Body, seconds: float) -> bool:
    """Beruehren sich die beiden Umrisse zum Zeitpunkt ``seconds``?

    Dasselbe Separating Axis Theorem wie in :func:`contact_window`, nur
    statisch fuer einen Augenblick auf den beiden Bahnen - deshalb gilt es
    auch dann, wenn eine davon ein Bogen ist.
    """
    ox, oy, odx, ody = own.at(seconds)
    px, py, pdx, pdy = other.at(seconds)
    gap_x = px - ox
    gap_y = py - oy
    for ax, ay in ((odx, ody), (-ody, odx), (pdx, pdy), (-pdy, pdx)):
        reach = (_half_extent(own.length, own.width, odx, ody, ax, ay)
                 + _half_extent(other.length, other.width, pdx, pdy, ax, ay))
        if abs(ax * gap_x + ay * gap_y) > reach:
            return False
    return True


def _first_arc_contact(own: Body, other: Body, limit_s: float) -> float:
    """Erste Beruehrung auf dem Bogen, oder ``inf`` innerhalb ``limit_s``."""
    if limit_s <= 0.0:
        return INF
    step = limit_s / _YAW_STEPS
    previous = 0.0
    for index in range(1, _YAW_STEPS + 1):
        moment = index * step
        if _bodies_overlap(own, other, moment):
            low, high = previous, moment
            for _ in range(_YAW_REFINE):
                middle = (low + high) / 2.0
                if _bodies_overlap(own, other, middle):
                    high = middle
                else:
                    low = middle
            return high
        previous = moment
    return INF


def _turning_copy(own: Body, relative_yaw: float) -> Body:
    """Wir auf dem Bogen, der andere geradeaus.

    Gerechnet wird mit der **relativen** Gierrate, nicht mit zwei eigenen:
    in einer Kurve, die beide zusammen nehmen, hebt sie sich auf, und es
    bleibt nur eine verrauschte Groesse statt zweier.
    """
    return Body(own.x, own.y, own.dx, own.dy, own.speed,
                own.length, own.width, relative_yaw)


def _intersect(a, b):
    if a is None or b is None:
        return None
    low = a[0] if a[0] > b[0] else b[0]
    high = a[1] if a[1] < b[1] else b[1]
    return (low, high) if low <= high else None


# ─── Die beiden Fragen ────────────────────────────────────────────────────

def contact_window(own: Body, other: Body) -> Optional[Tuple[float, float]]:
    """(erste, letzte) Beruehrung in Sekunden, oder ``None``.

    Negative Werte liegen in der Vergangenheit: ``(-0.4, 1.2)`` heisst "die
    beiden ueberlappen seit 0.4 s und noch 1.2 s lang", also *jetzt*. Ein
    Fenster, dessen oberes Ende negativ ist, ist vorbei - der Aufrufer muss
    das pruefen, weil "vorbei" und "nie" verschiedene Antworten sind.

    Exakt fuer zwei Rechtecke bei konstanter Geschwindigkeit, siehe Modulkopf.
    Lenkt einer merklich in den anderen hinein (``_MIN_RELATIVE_YAW_RATE``),
    wird zusaetzlich der Bogen abgetastet - mit derselben Regel wie in
    :func:`free_distance`: er darf die erste Beruehrung nur **vorziehen**, nie
    verschieben, und er darf eine finden, wo die Gerade keine sieht. Genau das
    ist der Fall "ich lenke in ihn hinein", den ein unveraenderliches Heading
    erst bemerkt, wenn es passiert ist.

    **Der umgekehrte Fall - zwei Autos, die dieselbe Kurve fahren - wird hier
    nicht geloest, sondern beim Aufrufer.** Ein Versuch dazu steht in der
    Git-Historie: jedem Auto seinen eigenen Bogen zu geben, statt nur der
    Differenz. Es hilft nicht. In ``simulation_tests`` Szenario 24 fahren zwei
    Autos mit 107 km/h und 5.5 m Abstand nebeneinander durch eine lange Kurve;
    gemessen werden 8.7 und 10.4 Grad/s, das sind Radien von 196 und 164 m.
    Auf 2.5 s fortgeschrieben laufen diese beiden Kreise ineinander - nur
    etwas spaeter als die Geraden. Bei 110 km/h sind 2.5 s ueber 70 m Fahrweg,
    und ueber 70 m macht jede Messungenauigkeit im Winkel mehrere Meter
    Querversatz. **Kein Fortschreiben ist auf diesem Horizont belastbar**,
    weder gerade noch gekruemmt; was hilft, ist den Horizont an die
    Belastbarkeit zu binden - siehe ``BlindSpotWarning.STEADY_TTC_S``.
    """
    straight = _straight_contact_window(own, other)

    relative_yaw = own.yaw_rate - other.yaw_rate
    if abs(relative_yaw) < _MIN_RELATIVE_YAW_RATE or own.speed <= 0.0:
        return straight

    limit_s = _YAW_HORIZON_S
    if straight is not None and straight[0] > 0.0:
        limit_s = min(limit_s, straight[0])
    arc = _first_arc_contact(_turning_copy(own, relative_yaw), other, limit_s)
    if arc == INF:
        return straight
    if straight is None:
        # Die Gerade sieht nichts, der Bogen schon. Das Ende des Fensters ist
        # damit nicht bekannt; der Horizont ist die ehrliche Untergrenze, und
        # die Aufrufer fragen ohnehin nur "liegt es noch vor uns".
        return arc, _YAW_HORIZON_S
    return min(arc, straight[0]), max(straight[1], arc)


def _straight_contact_window(own: Body,
                             other: Body) -> Optional[Tuple[float, float]]:
    gap_x = other.x - own.x
    gap_y = other.y - own.y
    vel_x = other.dx * other.speed - own.dx * own.speed
    vel_y = other.dy * other.speed - own.dy * own.speed

    window = (-INF, INF)
    for ax, ay in (own.forward, own.left, other.forward, other.left):
        window = _intersect(window, _overlap_window(
            ax * gap_x + ay * gap_y,
            ax * vel_x + ay * vel_y,
            own.half_extent(ax, ay) + other.half_extent(ax, ay)))
        if window is None:
            return None
    return window


def _inside_corridor(other: Body, x: float, y: float,
                     dx: float, dy: float, length: float,
                     width: float) -> bool:
    """Beruehrt ein Umriss an (x, y) mit Richtung (dx, dy) den Fahrschlauch?"""
    lat_x, lat_y = other.left
    gap_x = x - other.x
    gap_y = y - other.y
    if abs(lat_x * gap_x + lat_y * gap_y) > other.width / 2.0 + _half_extent(
            length, width, dx, dy, lat_x, lat_y):
        return False
    fwd_x, fwd_y = other.forward
    return (fwd_x * gap_x + fwd_y * gap_y) >= -(
        other.length / 2.0 + _half_extent(length, width, dx, dy, fwd_x, fwd_y))


def _turning_free_distance(own: Body, other: Body, limit_s: float) -> float:
    """Dasselbe wie :func:`free_distance`, aber auf dem Kreisbogen.

    Abgetastet statt gerechnet: die Bedingung ist eine Ungleichung in
    ``sin``/``cos`` der Zeit und hat keine geschlossene Loesung. Der erste
    Treffer wird danach durch Halbierung eingegabelt, ``inf`` heisst "innerhalb
    von ``limit_s`` nicht".
    """
    step = limit_s / _YAW_STEPS
    previous = 0.0
    for index in range(1, _YAW_STEPS + 1):
        moment = index * step
        if _inside_corridor(other, *own.at(moment), own.length, own.width):
            low, high = previous, moment
            for _ in range(_YAW_REFINE):
                middle = (low + high) / 2.0
                if _inside_corridor(other, *own.at(middle),
                                    own.length, own.width):
                    high = middle
                else:
                    low = middle
            return own.speed * high
        previous = moment
    return INF


def free_distance(own: Body, other: Body) -> float:
    """Restweg bis zum Fahrschlauch des anderen, entlang unserer Fahrtrichtung.

    Der Fahrschlauch ist der Bereich, den der andere bei gleichbleibender Fahrt
    noch belegen wird: quer ``width`` breit, laengs von seinem Heck nach vorn
    offen. Zurueckgegeben wird der Weg unseres **Mittelpunkts**, bis unser
    Umriss diesen Bereich beruehrt.

    Drei Antworten, die auseinandergehalten werden muessen:

    * ``inf`` - wir geraten nie hinein, oder wir sind schon hindurch. In beiden
      Faellen hilft Bremsen nichts mehr und darf nicht angefordert werden.
    * ``0.0`` - wir stehen bereits darin. Bremsen haelt uns *im* Konflikt; ob
      das richtig ist, entscheidet der Aufrufer und nicht diese Funktion
      (Querverkehr: ja, Toter Winkel: nein - siehe die beiden Systeme).
    * ein positiver Wert - so weit duerfen wir noch.

    Die Geschwindigkeit des anderen geht bewusst **nicht** ein: hier zaehlt
    allein die Geometrie. Ob er zur selben Zeit da ist, beantwortet
    :func:`contact_window`.

    **Lenkt einer von beiden, wird zusaetzlich der Bogen geprueft.** Ohne das
    haengt die Antwort allein am *momentanen* Heading, und das hinkt der
    Absicht des Fahrers um den ganzen Einlenkvorgang hinterher: gemessen in
    ``simulation_tests`` Szenario 22 stand das Auto 0.7 s nach Lenkbeginn
    immer noch auf "kreuzt nie", weil 2 Grad Schraeglage bei 1.8 m Restspalt
    rechnerisch 4 s bis zur Nachbarspur bedeuten - waehrend die Gierrate
    bereits 20 Grad/s war. Die relative Gierrate (``own`` minus ``other``) ist
    dabei die richtige Groesse: in einer Kurve drehen sich beide mit, und dann
    aendert sich zwischen ihnen nichts.

    Der Bogen darf die Antwort nur **vorziehen**, nie verzoegern - ein Modell
    mit konstanter Gierrate ueber ``_YAW_HORIZON_S`` hinaus ist eine
    Behauptung, keine Vorhersage.
    """
    gap_x = own.x - other.x
    gap_y = own.y - other.y

    lat_x, lat_y = other.left
    lateral = _overlap_window(
        lat_x * gap_x + lat_y * gap_y,
        lat_x * own.dx + lat_y * own.dy,
        other.width / 2.0 + own.half_extent(lat_x, lat_y))

    fwd_x, fwd_y = other.forward
    ahead = _halfspace_window(
        fwd_x * gap_x + fwd_y * gap_y,
        fwd_x * own.dx + fwd_y * own.dy,
        -(other.length / 2.0 + own.half_extent(fwd_x, fwd_y)))

    window = _intersect(lateral, ahead)
    straight = INF
    if window is not None and window[1] >= 0.0:
        straight = window[0] if window[0] > 0.0 else 0.0
    if straight == 0.0:
        return 0.0

    relative_yaw = own.yaw_rate - other.yaw_rate
    if abs(relative_yaw) < _MIN_RELATIVE_YAW_RATE or own.speed <= 0.0:
        return straight
    limit_s = _YAW_HORIZON_S
    if straight != INF:
        limit_s = min(limit_s, straight / own.speed)
    return min(straight, _turning_free_distance(
        _turning_copy(own, relative_yaw), other, limit_s))


def stopping_deceleration(free_m: float, speed_ms: float, buffer_m: float,
                          reaction_s: float) -> float:
    """Verzoegerung, die noetig ist, um vor ``free_m`` zum Stehen zu kommen.

    ``v² = 2 a s`` mit zwei Abzuegen vom Weg: ``buffer_m`` Restabstand, den wir
    nicht aufbrauchen wollen, und ``speed_ms * reaction_s`` fuer den Weg, den
    das Auto zurueklegt, bevor der Eingriff ueberhaupt wirkt - ein
    100-ms-Zyklus, eine Tastatureingabe und LFS' eigener Bremsanstieg.

    ``inf`` an Restweg ergibt 0 - kein Bedarf. Ist der Weg aufgebraucht, gibt
    es keine sinnvolle Rechnung mehr, und es kommt der Panikwert.
    """
    if free_m == INF:
        return 0.0
    usable = free_m - buffer_m - speed_ms * reaction_s
    if usable <= _MIN_USABLE_M:
        return PANIC_DECELERATION_MS2
    # Gedeckelt: ``PANIC_DECELERATION_MS2`` ist bereits das Doppelte dessen,
    # was Reifen hergeben. Was darueber liegt, ist keine Information mehr,
    # sondern eine Division durch fast Null - und es steht in Logs und Traces,
    # wo eine Zahl wie 53 m/s² nur verwirrt.
    return min(PANIC_DECELERATION_MS2,
               speed_ms * speed_ms / (2.0 * usable))
