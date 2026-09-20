#!/usr/bin/env python
"""Deterministischer Pruefer fuer T7 — Antiblockiersystem.

Aufruf durch den Benchmark-Runner::

    python checks.py <workspace>

Gibt genau ein JSON-Objekt auf stdout aus.

Warum offline geprueft wird
---------------------------
Der eigentliche Nachweis ist der Fahrversuch in *Live for Speed* — der braucht
Windows, ein laufendes Spiel und rund drei Minuten Fahrzeit und passt deshalb
nicht in einen Benchmark-Lauf, der parallel mit anderen Modellen stattfindet.
Dieser Pruefer beantwortet die Fragen, die **ohne** Spiel deterministisch
beantwortbar sind:

* Wurde die Regel eingehalten, dass nur der Rumpf einer Funktion veraendert
  wurde? (AST-Vergleich gegen das Original)
* Ist die Funktion echtzeitfaehig, ausnahmefrei und robust gegen die Randfaelle,
  die die Anlage erzeugt?
* **Regelt sie ueberhaupt?** Dazu laeuft die Kandidatenfunktion in einem
  geschlossenen Regelkreis gegen ein Laengsdynamikmodell mit schlupfabhaengigem
  Reibbeiwert, das hier im Pruefer steckt und das der Kandidat nie sieht.

Das Modell ist **nicht** LFS. Es bildet den Zielkonflikt der Aufgabe ab —
Bremsweg gegen Blockieren — und bestraft die beiden Fehler, die ein
Antiblockiersystem disqualifizieren: gar nicht regeln (Rad steht, Weg wird lang)
und zu viel regeln (Bremse wird verschenkt). Die Fahrleistung in der Simulation
wird davon nicht vorweggenommen; sie wird getrennt gemessen
(``fahrversuch/auswertung.py``).

Punkte
------
Siehe ``rubric.md``. ``passed`` ab 60 von 100 Punkten.
"""

from __future__ import annotations

import ast
import importlib.util
import io
import json
import math
import os
import sys
import time
from contextlib import redirect_stdout
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

AUFGABEN_DIR = Path(__file__).resolve().parent
ORIGINAL_WS = AUFGABEN_DIR / "workspace"
ZU_AENDERNDE_FUNKTION = "berechne_pedalwerte"
#: Dateien, die Byte fuer Byte so bleiben muessen, wie sie ausgeliefert wurden.
ANLAGENDATEIEN = ("lfs_link.py", "README.md", "requirements.txt")


# ══════════════════════════════════════════════════════════════════════════════
# 1. Statische Pruefung: wurde nur der Rumpf der einen Funktion veraendert?
# ══════════════════════════════════════════════════════════════════════════════

class _RumpfEntferner(ast.NodeTransformer):
    """Ersetzt den Rumpf der Zielfunktion durch ``pass``.

    Danach sind zwei Dateien genau dann gleich, wenn sie sich ausserhalb dieses
    Rumpfes nicht unterscheiden — unabhaengig von Einrueckung, Kommentaren und
    Leerzeilen, die ein AST ohnehin nicht traegt.
    """

    def __init__(self) -> None:
        self.gefunden = 0

    def visit_FunctionDef(self, node: ast.FunctionDef) -> ast.AST:
        if node.name == ZU_AENDERNDE_FUNKTION:
            self.gefunden += 1
            doc = ast.get_docstring(node, clean=False)
            node.body = ([ast.Expr(value=ast.Constant(value=doc))] if doc else []) \
                + [ast.Pass()]
        return node


def _geruest(quelle: str) -> Tuple[Optional[str], int]:
    """AST-Abbild der Datei ohne den Rumpf der Zielfunktion."""
    baum = ast.parse(quelle)
    entferner = _RumpfEntferner()
    baum = entferner.visit(baum)
    ast.fix_missing_locations(baum)
    return ast.dump(baum, annotate_fields=True, include_attributes=False), entferner.gefunden


def _rumpf(quelle: str) -> Optional[ast.FunctionDef]:
    for knoten in ast.walk(ast.parse(quelle)):
        if isinstance(knoten, ast.FunctionDef) and knoten.name == ZU_AENDERNDE_FUNKTION:
            return knoten
    return None


#: Was im Rumpf nichts zu suchen hat, und warum.
VERBOTENE_NAMEN = {
    "open": "Dateizugriff im Regeltakt",
    "exec": "dynamische Ausfuehrung",
    "eval": "dynamische Ausfuehrung",
    "compile": "dynamische Ausfuehrung",
    "__import__": "Import im Regeltakt",
    "input": "blockiert den Takt",
    "print": "Konsolenausgabe im Regeltakt",
}
VERBOTENE_ATTRIBUTE = {
    "sleep": "haelt den Regeltakt an",
    "system": "Prozessaufruf im Regeltakt",
    "popen": "Prozessaufruf im Regeltakt",
}


def pruefe_statisch(ws: Path, befunde: Dict[str, Any]) -> Dict[str, bool]:
    ergebnis: Dict[str, bool] = {}
    kandidat_pfad = ws / "abs_regelung.py"
    original_pfad = ORIGINAL_WS / "abs_regelung.py"

    try:
        kandidat = kandidat_pfad.read_text(encoding="utf-8")
    except OSError as e:
        befunde["fehler"] = f"abs_regelung.py nicht lesbar: {e}"
        return {"nur_rumpf_geaendert": False, "keine_verbotenen_konstrukte": False,
                "anlage_unveraendert": False, "implementierung_vorhanden": False}
    original = original_pfad.read_text(encoding="utf-8")

    # -- 1a: der Rest der Datei ist unveraendert ---------------------------
    try:
        geruest_k, treffer_k = _geruest(kandidat)
        geruest_o, treffer_o = _geruest(original)
        ergebnis["nur_rumpf_geaendert"] = (treffer_k == treffer_o == 1
                                           and geruest_k == geruest_o)
        if not ergebnis["nur_rumpf_geaendert"]:
            if treffer_k != 1:
                befunde["regelverstoss"] = (
                    f"{ZU_AENDERNDE_FUNKTION} kommt {treffer_k}x auf Modulebene vor, "
                    "erwartet genau einmal")
            else:
                befunde["regelverstoss"] = ("Die Datei wurde ausserhalb des Rumpfes von "
                                            f"{ZU_AENDERNDE_FUNKTION} veraendert "
                                            "(Importe, Modulkopf, weitere Definitionen).")
    except SyntaxError as e:
        befunde["syntaxfehler"] = f"{e}"
        return {"nur_rumpf_geaendert": False, "keine_verbotenen_konstrukte": False,
                "anlage_unveraendert": False, "implementierung_vorhanden": False}

    # -- 1b: verbotene Konstrukte im Rumpf ---------------------------------
    funktion = _rumpf(kandidat)
    verstoesse: List[str] = []
    knoten_zahl = 0
    if funktion is not None:
        for knoten in ast.walk(funktion):
            knoten_zahl += 1
            if isinstance(knoten, (ast.Import, ast.ImportFrom)):
                verstoesse.append("Import im Funktionsrumpf")
            elif isinstance(knoten, (ast.Global, ast.Nonlocal)):
                verstoesse.append("global/nonlocal im Funktionsrumpf")
            elif isinstance(knoten, ast.Name) and knoten.id in VERBOTENE_NAMEN:
                verstoesse.append(f"{knoten.id}: {VERBOTENE_NAMEN[knoten.id]}")
            elif isinstance(knoten, ast.Attribute) and knoten.attr in VERBOTENE_ATTRIBUTE:
                verstoesse.append(f".{knoten.attr}: {VERBOTENE_ATTRIBUTE[knoten.attr]}")
    ergebnis["keine_verbotenen_konstrukte"] = not verstoesse
    if verstoesse:
        befunde["verbotene_konstrukte"] = sorted(set(verstoesse))

    # -- 1c: die Anlage selbst wurde nicht angefasst ------------------------
    # Bewusst eine feste Liste statt "alles ausser abs_regelung.py": in einen
    # Workspace kann auch Maschinenzubehoer geraten (eine eingemessene
    # kalibrierung.json etwa), und daran soll sich kein Regelverstoss
    # festmachen, den es nicht gibt.
    unterschiede: List[str] = []
    for name in ANLAGENDATEIEN:
        soll = (ORIGINAL_WS / name).read_bytes()
        pfad = ws / name
        if not pfad.is_file():
            unterschiede.append(f"{name} fehlt")
        elif pfad.read_bytes() != soll:
            unterschiede.append(f"{name} veraendert")
    ergebnis["anlage_unveraendert"] = not unterschiede
    if unterschiede:
        befunde["anlage"] = unterschiede

    # -- 1d: es steht ueberhaupt eine Implementierung da --------------------
    original_funktion = _rumpf(original)
    original_knoten = sum(1 for _ in ast.walk(original_funktion)) if original_funktion else 0
    ergebnis["implementierung_vorhanden"] = knoten_zahl > original_knoten + 15
    befunde["ast_knoten_im_rumpf"] = knoten_zahl
    return ergebnis


# ══════════════════════════════════════════════════════════════════════════════
# 2. Laengsdynamikmodell — der Pruefstand des Pruefers
# ══════════════════════════════════════════════════════════════════════════════

class Fahrbahn:
    """Reibbeiwert ueber Bremsschlupf nach Burckhardt.

    ``mu(s) = c1 (1 - e^(-c2 s)) - c3 s`` — ein Maximum bei kleinem Schlupf, ein
    flacher Abfall darueber. Genau dieser Abfall ist der Grund, warum ein
    blockiertes Rad schlechter bremst als ein rollendes, und damit der Grund,
    warum es die Aufgabe gibt.
    """

    def __init__(self, name: str, c1: float, c2: float, c3: float):
        self.name, self.c1, self.c2, self.c3 = name, c1, c2, c3

    def mu(self, s: float) -> float:
        s = max(0.0, min(1.0, s))
        return max(0.0, self.c1 * (1.0 - math.exp(-self.c2 * s)) - self.c3 * s)

    def optimum(self) -> Tuple[float, float]:
        beste = max((self.mu(i / 1000.0), i / 1000.0) for i in range(1001))
        return beste[1], beste[0]


ASPHALT = Fahrbahn("asphalt", 1.28, 23.99, 0.52)     # trocken, Maximum bei s ~ 0.17
NASS = Fahrbahn("nass", 0.86, 33.82, 0.35)           # nass
GLATT = Fahrbahn("glatt", 0.40, 33.71, 0.12)         # sehr niedriger Reibwert


class Fahrzeugmodell:
    """Laengsdynamik mit **einem** Radfreiheitsgrad.

    Warum ein Rad und nicht vier
    ----------------------------
    Die Anlage stellt eine globale Bremsanforderung und meldet eine einzige
    Radgeschwindigkeit. Ein Modell mit zwei Achsen und fester
    Bremskraftverteilung wuerde daraus eine Aufgabe machen, die anders ist als
    die gestellte: bei realistischer, vorderachsbetonter Verteilung erreicht die
    Vorderachse ihre Kraftgrenze zuerst, die gemessene Achse merkt davon nichts,
    und die beste Strategie waere, die gemessene Achse blockieren zu lassen. Das
    ist ein Artefakt der Verteilung, nicht die Aufgabe.

    Dieses Modell setzt deshalb eine **lastabhaengig ideale Verteilung** voraus:
    beide Achsen laufen dann per Konstruktion mit demselben Schlupf, die
    Radlastverlagerung hebt sich in der Summe auf, und das Ganze faellt auf ein
    aequivalentes Rad zusammen. Der gemessene Schlupf ist damit ein gueltiger
    Stellvertreter fuer den Zustand des Fahrzeugs, und das Modell prueft genau
    das, was die Aufgabe verlangt: den Schlupf ueber eine globale
    Bremsanforderung zu fuehren.

    Weitere Vereinfachungen, ausgeschrieben damit sie nicht fuer Physik gehalten
    werden:

    * kein Antriebsstrang — die Kupplung gilt beim Bremsen als offen, ein
      Schleppmoment des Motors gibt es nicht;
    * keine Querdynamik; der Pruefer misst geradeaus. Stabilitaet und
      Lenkbarkeit werden hier **nicht** bewertet, sondern erst im Fahrversuch;
    * Bremsmoment linear in der Anforderung, mit einer Zeitkonstante fuer die
      Hydraulik.
    """

    MASSE = 1100.0            # kg
    RADRADIUS = 0.30          # m
    TRAEGHEIT = 4.00          # kg m^2, alle vier Raeder zusammen
    BREMSMOMENT_MAX = 4800.0  # Nm bei 100 % Anforderung
    LUFTWIDERSTAND = 0.40     # 0.5 * rho * cw * A
    ROLLWIDERSTAND = 0.013
    G = 9.81
    #: Zeitkonstante der Bremshydraulik. Ohne sie waere jede noch so wilde
    #: Modulation kostenlos, und der Pruefer wuerde Regler belohnen, die es in
    #: einer echten Anlage nie geben kann.
    BREMSE_TAU_S = 0.045

    def __init__(self, fahrbahn: Fahrbahn, v0: float):
        self.fahrbahn = fahrbahn
        self.v = v0
        self.omega = v0 / self.RADRADIUS
        self.weg = 0.0
        self.a_x = 0.0
        self.bremse_ist = 0.0

    def schritt(self, bremse_prozent: float, dt: float) -> None:
        # Hydraulik: die Anforderung kommt verzoegert am Rad an.
        alpha = 1.0 - math.exp(-dt / self.BREMSE_TAU_S)
        self.bremse_ist += (max(0.0, min(100.0, bremse_prozent)) - self.bremse_ist) * alpha
        moment = self.BREMSMOMENT_MAX * self.bremse_ist / 100.0

        v_rad = self.omega * self.RADRADIUS
        schlupf = max(0.0, min(1.0, (self.v - v_rad) / max(self.v, 0.3)))
        kraft = self.fahrbahn.mu(schlupf) * self.MASSE * self.G

        # Radgleichung. Das Bremsmoment kann das Rad nicht rueckwaerts drehen:
        # unter Null wird es als stehend gefuehrt.
        self.omega = max(0.0, self.omega
                         + (kraft * self.RADRADIUS - moment) / self.TRAEGHEIT * dt)

        widerstand = (self.LUFTWIDERSTAND * self.v * self.v
                      + self.ROLLWIDERSTAND * self.MASSE * self.G)
        self.a_x = -(kraft + widerstand) / self.MASSE
        v_neu = max(0.0, self.v + self.a_x * dt)
        self.weg += 0.5 * (self.v + v_neu) * dt
        self.v = v_neu

    @property
    def v_rad_hinten(self) -> float:
        return self.omega * self.RADRADIUS


# ── Der Regelkreis des Pruefers ─────────────────────────────────────────────

#: dt-Streuung um den 50-ms-Takt. Fest verdrahtet statt zufaellig, damit zwei
#: Laeufe desselben Kandidaten bis auf die letzte Stelle dasselbe ergeben.
DT_MUSTER = (0.050, 0.052, 0.047, 0.051, 0.049, 0.055, 0.045, 0.050,
             0.048, 0.053, 0.050, 0.046, 0.054, 0.049, 0.051, 0.050)


class Bremsversuch:
    """Ein Bremsversuch: Vollbremsung aus ``v0`` bis Stillstand."""

    def __init__(self, lfs_link, fahrbahn: Fahrbahn, v0: float = 28.0,
                 max_s: float = 12.0):
        self.lfs_link = lfs_link
        self.fahrbahn = fahrbahn
        self.v0 = v0
        self.max_s = max_s

    def fahre(self, regler) -> Dict[str, Any]:
        lfs = self.lfs_link
        modell = Fahrzeugmodell(self.fahrbahn, self.v0)
        zustand: Dict[str, Any] = {}
        fahrer = lfs.Fahrereingabe(gas_prozent=0.0, bremse_prozent=100.0,
                                   lenkung_prozent=0.0)
        letzte = (0.0, 0.0)
        t = 0.0
        i = 0
        laufzeit_max = 0.0
        fehler = 0
        schlupfwerte: List[float] = []
        blockierzeit = 0.0
        ausgaben: List[float] = []
        weg_bei_25: Optional[float] = None
        weg_bei_5: Optional[float] = None

        while modell.v > 0.05 and t < self.max_s:
            dt = DT_MUSTER[i % len(DT_MUSTER)]
            fz = lfs.Fahrzeugzustand(
                zeit_s=t, dt_s=dt,
                v_ueber_grund_mps=modell.v,
                kurswinkel_rad=0.0, bewegungsrichtung_rad=0.0, gierrate_rad_s=0.0,
                position_m=(0.0, modell.weg, 0.0), mci_alter_s=0.02,
                v_rad_hinterachse_mps=modell.v_rad_hinten,
                motordrehzahl_rpm=1200.0, gang=1, ladedruck_bar=0.0,
                motortemperatur_c=88.0, kraftstoff_norm=0.5, oeldruck_bar=3.0,
                oeltemperatur_c=90.0,
                gas_ist_norm=0.0,
                bremse_ist_norm=modell.bremse_ist / 100.0,
                kupplung_ist_norm=1.0, handbremse_an=False,
                leuchten_verfuegbar=0, leuchten_an=0, fahrzeug="XRG",
                outgauge_alter_s=0.02,
                daten_gueltig=True, mci_gueltig=True, outgauge_gueltig=True,
                letzte_ausgabe_gas_prozent=letzte[0],
                letzte_ausgabe_bremse_prozent=letzte[1])

            beginn = time.perf_counter()
            try:
                with redirect_stdout(io.StringIO()):
                    ausgabe = regler(fahrer, fz, zustand)
                bremse = float(ausgabe.bremse_prozent)
                gas = float(ausgabe.gas_prozent)
                if not (math.isfinite(bremse) and math.isfinite(gas)):
                    raise ValueError("NaN")
                bremse = max(0.0, min(100.0, bremse))
                gas = max(0.0, min(100.0, gas))
            except Exception:
                fehler += 1
                gas, bremse = 0.0, 100.0
            laufzeit_max = max(laufzeit_max, (time.perf_counter() - beginn) * 1000.0)
            letzte = (gas, bremse)
            ausgaben.append(bremse)

            # Der Takt des Modells ist feiner als der des Reglers; die
            # Anforderung steht dazwischen still, wie in der Anlage auch.
            rest = dt
            while rest > 1e-9:
                h = min(0.001, rest)
                if modell.v > 3.0:
                    s = (modell.v - modell.v_rad_hinten) / modell.v
                    schlupfwerte.append(s)
                    if s >= 0.40:
                        blockierzeit += h
                if weg_bei_25 is None and modell.v <= 25.0:
                    weg_bei_25 = modell.weg
                if weg_bei_5 is None and modell.v <= 5.0:
                    weg_bei_5 = modell.weg
                modell.schritt(bremse, h)
                rest -= h
            t += dt
            i += 1

        if weg_bei_5 is None:
            weg_bei_5 = modell.weg
        schlupfwerte.sort()
        modulationen = sum(1 for a, b in zip(ausgaben, ausgaben[1:]) if abs(b - a) >= 5.0)
        return {
            "fahrbahn": self.fahrbahn.name,
            "bremsweg_m": round(modell.weg, 3),
            "referenzweg_m": (round(weg_bei_5 - weg_bei_25, 3)
                              if weg_bei_25 is not None else None),
            "dauer_s": round(t, 3),
            "schlupf_median": (round(schlupfwerte[len(schlupfwerte) // 2], 4)
                               if schlupfwerte else None),
            "blockierzeit_s": round(blockierzeit, 3),
            "modulationen": modulationen,
            "laufzeit_max_ms": round(laufzeit_max, 4),
            "fehler": fehler,
            "endgeschwindigkeit_mps": round(modell.v, 3),
        }


def _durchreichen(fahrer, fahrzeug, zustand):
    """Der Ausgangszustand: kein ABS."""
    return type("P", (), {"gas_prozent": fahrer.gas_prozent,
                          "bremse_prozent": fahrer.bremse_prozent})()


#: Ein- und Ausschaltschlupf des Referenzreglers. Der Bereich liegt um die
#: Reibwertmaxima der drei Fahrbahnen (0.13 .. 0.17) herum — wie ein echtes ABS,
#: das den Reibwert nicht kennt und deshalb einen Kompromiss faehrt.
REFERENZ_SCHLUPF_AUF, REFERENZ_SCHLUPF_AB = 0.20, 0.12


def _referenzregler(fahrer, fahrzeug, zustand):
    """Zweipunktregler auf den Schlupf — die Zielmarke des Pruefers.

    Ueber ``REFERENZ_SCHLUPF_AUF`` wird der Druck ganz abgebaut, unter
    ``REFERENZ_SCHLUPF_AB`` wieder ganz aufgebaut; dazwischen bleibt es beim
    zuletzt Entschiedenen. Das ist die einfachste Bauform, die es wirklich gibt,
    und sie hat dieselben Informationen wie der Kandidat: nur die beiden
    Geschwindigkeiten, kein Wissen ueber den Reibwert.

    Sie ist die Latte, nicht die Musterloesung. Eine Regelung, die den Druck
    dosiert statt ihn zu schalten, schlaegt sie auf griffiger Fahrbahn deutlich.
    """
    p = type("P", (), {"gas_prozent": 0.0, "bremse_prozent": fahrer.bremse_prozent})()
    if not fahrzeug.daten_gueltig or fahrer.bremse_prozent <= 0.0:
        zustand.pop("auf", None)
        return p
    if fahrzeug.v_ueber_grund_mps < 1.5:
        return p
    schlupf = ((fahrzeug.v_ueber_grund_mps - fahrzeug.v_rad_hinterachse_mps)
               / max(fahrzeug.v_ueber_grund_mps, 0.5))
    auf = zustand.get("auf", True)
    if auf and schlupf > REFERENZ_SCHLUPF_AUF:
        auf = False
    elif not auf and schlupf < REFERENZ_SCHLUPF_AB:
        auf = True
    zustand["auf"] = auf
    p.bremse_prozent = fahrer.bremse_prozent if auf else 0.0
    return p


# ══════════════════════════════════════════════════════════════════════════════
# 3. Randfaelle
# ══════════════════════════════════════════════════════════════════════════════

def _zustand(lfs, **felder):
    vorgabe = dict(
        zeit_s=1.0, dt_s=0.05, v_ueber_grund_mps=20.0, kurswinkel_rad=0.3,
        bewegungsrichtung_rad=0.3, gierrate_rad_s=0.0, position_m=(0.0, 0.0, 0.0),
        mci_alter_s=0.02, v_rad_hinterachse_mps=19.5, motordrehzahl_rpm=3000.0,
        gang=3, ladedruck_bar=0.0, motortemperatur_c=88.0, kraftstoff_norm=0.5,
        oeldruck_bar=3.0, oeltemperatur_c=90.0, gas_ist_norm=0.0,
        bremse_ist_norm=0.0, kupplung_ist_norm=0.0, handbremse_an=False,
        leuchten_verfuegbar=0, leuchten_an=0, fahrzeug="XRG",
        outgauge_alter_s=0.02, daten_gueltig=True, mci_gueltig=True,
        outgauge_gueltig=True, letzte_ausgabe_gas_prozent=0.0,
        letzte_ausgabe_bremse_prozent=0.0)
    vorgabe.update(felder)
    return lfs.Fahrzeugzustand(**vorgabe)


def randfaelle(lfs) -> List[Tuple[str, Any, Any]]:
    """``(name, fahrer, fahrzeug)`` — jeder einzelne muss ueberlebt werden."""
    F = lfs.Fahrereingabe
    voll = F(gas_prozent=0.0, bremse_prozent=100.0, lenkung_prozent=0.0)
    kein = F(gas_prozent=0.0, bremse_prozent=0.0, lenkung_prozent=0.0)
    gas = F(gas_prozent=80.0, bremse_prozent=0.0, lenkung_prozent=0.0)
    beides = F(gas_prozent=60.0, bremse_prozent=60.0, lenkung_prozent=-40.0)
    halb = F(gas_prozent=0.0, bremse_prozent=45.0, lenkung_prozent=0.0)

    faelle = [
        ("erster_aufruf", voll, _zustand(lfs, zeit_s=0.0)),
        ("telemetrie_fehlt", voll, _zustand(
            lfs, daten_gueltig=False, mci_gueltig=False, outgauge_gueltig=False,
            v_ueber_grund_mps=0.0, v_rad_hinterachse_mps=0.0, gang=0,
            mci_alter_s=999.0, outgauge_alter_s=999.0)),
        ("nur_mci", voll, _zustand(lfs, daten_gueltig=False, outgauge_gueltig=False,
                                   v_rad_hinterachse_mps=0.0, outgauge_alter_s=5.0)),
        ("stillstand", voll, _zustand(lfs, v_ueber_grund_mps=0.0,
                                      v_rad_hinterachse_mps=0.0)),
        ("kriechen", voll, _zustand(lfs, v_ueber_grund_mps=0.02,
                                    v_rad_hinterachse_mps=0.0)),
        ("rad_steht_fahrzeug_faehrt", voll, _zustand(lfs, v_rad_hinterachse_mps=0.0)),
        ("rad_schneller_als_fahrzeug", gas, _zustand(lfs, v_rad_hinterachse_mps=35.0)),
        ("kein_bremswunsch", kein, _zustand(lfs, v_rad_hinterachse_mps=12.0,
                                            v_ueber_grund_mps=20.0)),
        ("nur_gas", gas, _zustand(lfs)),
        ("gas_und_bremse", beides, _zustand(lfs)),
        ("teilbremsung", halb, _zustand(lfs, v_rad_hinterachse_mps=17.0)),
        ("rueckwaerts", voll, _zustand(lfs, gang=0, v_ueber_grund_mps=6.0,
                                       v_rad_hinterachse_mps=5.0)),
        ("leerlauf", voll, _zustand(lfs, gang=1)),
        ("handbremse", voll, _zustand(lfs, handbremse_an=True,
                                      v_rad_hinterachse_mps=0.0)),
        ("dt_sehr_klein", voll, _zustand(lfs, dt_s=1e-4)),
        ("dt_sehr_gross", voll, _zustand(lfs, dt_s=1.7)),
        ("hohe_geschwindigkeit", voll, _zustand(lfs, v_ueber_grund_mps=83.0,
                                                v_rad_hinterachse_mps=40.0)),
        ("altes_paket", voll, _zustand(lfs, mci_alter_s=0.9, outgauge_alter_s=0.9)),
        ("negativer_kurs", voll, _zustand(lfs, kurswinkel_rad=-3.1,
                                          bewegungsrichtung_rad=3.1)),
    ]
    return faelle


def pruefe_verhalten(lfs, regler, befunde: Dict[str, Any]) -> Dict[str, bool]:
    ergebnis: Dict[str, bool] = {}

    # -- Randfaelle: keine Ausnahme, kein NaN, Wertebereich ----------------
    kaputt: List[str] = []
    for name, fahrer, fahrzeug in randfaelle(lfs):
        try:
            with redirect_stdout(io.StringIO()):
                ausgabe = regler(fahrer, fahrzeug, {})
            gas = float(ausgabe.gas_prozent)
            bremse = float(ausgabe.bremse_prozent)
        except Exception as e:
            kaputt.append(f"{name}: {type(e).__name__}: {e}")
            continue
        if not (math.isfinite(gas) and math.isfinite(bremse)):
            kaputt.append(f"{name}: NaN oder unendlich")
        elif not (0.0 <= gas <= 100.0 and 0.0 <= bremse <= 100.0):
            kaputt.append(f"{name}: ausserhalb 0..100 (gas={gas}, bremse={bremse})")
    ergebnis["randfaelle"] = not kaputt
    if kaputt:
        befunde["randfaelle"] = kaputt[:10]

    # -- Ohne Bremswunsch darf nicht gebremst werden ------------------------
    ohne: List[str] = []
    zustand: Dict[str, Any] = {}
    for i in range(60):
        fahrer = lfs.Fahrereingabe(gas_prozent=70.0, bremse_prozent=0.0,
                                   lenkung_prozent=0.0)
        fz = _zustand(lfs, zeit_s=i * 0.05, v_ueber_grund_mps=18.0 + i * 0.1,
                      v_rad_hinterachse_mps=18.0 + i * 0.1 + 0.6)
        try:
            with redirect_stdout(io.StringIO()):
                ausgabe = regler(fahrer, fz, zustand)
            if float(ausgabe.bremse_prozent) > 2.0:
                ohne.append(f"Takt {i}: {float(ausgabe.bremse_prozent):.1f} % Bremse")
        except Exception as e:
            ohne.append(f"Takt {i}: {type(e).__name__}")
            break
    ergebnis["kein_eingriff_ohne_bremswunsch"] = not ohne
    if ohne:
        befunde["eingriff_ohne_bremswunsch"] = ohne[:5]

    # -- Der Zustandsspeicher darf nicht unbegrenzt wachsen -----------------
    zustand = {}
    versuch = Bremsversuch(lfs, ASPHALT, v0=30.0, max_s=20.0)
    try:
        with redirect_stdout(io.StringIO()):
            for i in range(4000):
                fz = _zustand(lfs, zeit_s=i * 0.05,
                              v_ueber_grund_mps=5.0 + 20.0 * abs(math.sin(i / 37.0)),
                              v_rad_hinterachse_mps=5.0 + 18.0 * abs(math.sin(i / 37.0)))
                regler(lfs.Fahrereingabe(gas_prozent=0.0, bremse_prozent=100.0,
                                         lenkung_prozent=0.0), fz, zustand)
        groesse = len(zustand) + sum(
            len(v) for v in zustand.values()
            if isinstance(v, (list, dict, tuple, set)))
        ergebnis["zustand_begrenzt"] = groesse <= 400
        befunde["zustandsgroesse_nach_4000_takten"] = groesse
    except Exception as e:
        ergebnis["zustand_begrenzt"] = False
        befunde["zustand_begrenzt"] = f"{type(e).__name__}: {e}"

    return ergebnis


# ══════════════════════════════════════════════════════════════════════════════
# 4. Geschlossener Regelkreis gegen das Modell
# ══════════════════════════════════════════════════════════════════════════════

def pruefe_regelung(lfs, regler, befunde: Dict[str, Any]) -> Dict[str, bool]:
    ergebnis: Dict[str, bool] = {}
    laeufe: Dict[str, Dict[str, Any]] = {}

    for fahrbahn in (ASPHALT, NASS, GLATT):
        versuch = Bremsversuch(lfs, fahrbahn, v0=30.0)
        ohne = versuch.fahre(_durchreichen)
        referenz = versuch.fahre(_referenzregler)
        kandidat = versuch.fahre(regler)
        s_opt, mu_opt = fahrbahn.optimum()
        laeufe[fahrbahn.name] = {
            "ohne_abs": ohne, "referenz": referenz, "kandidat": kandidat,
            "schlupf_optimum": round(s_opt, 3), "mu_max": round(mu_opt, 3),
        }
    # -- Bremsweg, gemessen als Anteil am erreichbaren Gewinn --------------
    # Absolute Meterzahlen waeren an dieses Modell gebunden. Der Anteil am
    # Abstand zwischen "gar nicht geregelt" und "Referenzregler" ist es nicht:
    # er sagt, wie viel von dem, was mit denselben Messgroessen zu holen ist,
    # tatsaechlich geholt wurde. 0 = wie ohne ABS, 1 = wie die Referenz.
    schlechter: List[str] = []
    for name, d in laeufe.items():
        spanne = d["ohne_abs"]["bremsweg_m"] - d["referenz"]["bremsweg_m"]
        gewinn = ((d["ohne_abs"]["bremsweg_m"] - d["kandidat"]["bremsweg_m"]) / spanne
                  if spanne > 1e-6 else 0.0)
        d["gewinnanteil"] = round(gewinn, 3)
        ergebnis[f"bremsweg_{name}"] = gewinn >= MINDESTGEWINN
        if gewinn < MINDESTWIRKUNG:
            schlechter.append(f"{name}: {d['kandidat']['bremsweg_m']:.1f} m gegen "
                              f"{d['ohne_abs']['bremsweg_m']:.1f} m ganz ohne Regelung")
    # Harte Untergrenze. Eine Regelung, die den Bremsweg gegenueber gar keiner
    # Regelung nicht verkuerzt, hat ihren Zweck verfehlt, egal wie sauber sie
    # sonst gebaut ist — deshalb steht das als eigener, schwer gewichteter Punkt
    # da und nicht als Abzug irgendwo. Das unveraenderte Durchreichen faellt
    # hier durch, obwohl sein Bremsweg per Definition genau der Vergleichswert
    # ist: gefordert ist eine Wirkung, nicht die Abwesenheit einer Verschlechterung.
    ergebnis["besser_als_ohne_regelung"] = not schlechter
    if schlechter:
        befunde["laenger_als_ohne_regelung"] = schlechter
    befunde["bremsversuche"] = laeufe

    # -- Regelt sie ueberhaupt? --------------------------------------------
    moduliert = [n for n, d in laeufe.items() if d["kandidat"]["modulationen"] >= 3]
    ergebnis["regelt"] = len(moduliert) >= 2

    # -- Blockieren wird tatsaechlich verhindert ---------------------------
    # Auch die Referenz blockiert auf glatter Fahrbahn zeitweise: dort laeuft
    # ein einmal stehendes Rad nur langsam wieder hoch, weil die Umfangskraft
    # klein ist. Ein fester Absolutwert wuerde das bestrafen, statt es zu
    # messen — die Schranke haengt deshalb an der Referenz.
    blockiert = {}
    ok = True
    for name, d in laeufe.items():
        grenze = max(1.0, 1.6 * d["referenz"]["blockierzeit_s"])
        blockiert[name] = {"kandidat_s": d["kandidat"]["blockierzeit_s"],
                           "referenz_s": d["referenz"]["blockierzeit_s"],
                           "grenze_s": round(grenze, 2)}
        ok = ok and d["kandidat"]["blockierzeit_s"] <= grenze
    ergebnis["kein_dauerblockieren"] = ok
    befunde["blockierzeit"] = blockiert

    # -- Das Rad laeuft ueberhaupt wieder hoch -----------------------------
    # Ein Median nahe 1 heisst: das Rad stand die halbe Bremsung. Das ist kein
    # Regelfehler mehr, sondern gar keine Regelung.
    ergebnis["schlupf_nicht_dauerhoch"] = all(
        d["kandidat"]["schlupf_median"] is not None
        and d["kandidat"]["schlupf_median"] < 0.60 for d in laeufe.values())

    # -- Echtzeit ----------------------------------------------------------
    langsamste = max(d["kandidat"]["laufzeit_max_ms"] for d in laeufe.values())
    ergebnis["echtzeit"] = langsamste < 2.0
    befunde["reglerlaufzeit_max_ms"] = round(langsamste, 3)

    # -- Keine Ausnahme im geschlossenen Kreis -----------------------------
    fehler = sum(d["kandidat"]["fehler"] for d in laeufe.values())
    ergebnis["ausnahmefrei_im_kreis"] = fehler == 0
    befunde["ausnahmen_im_kreis"] = fehler

    return ergebnis


# ══════════════════════════════════════════════════════════════════════════════
# 5. Zusammenbau
# ══════════════════════════════════════════════════════════════════════════════

#: Anteil am Gewinn der Referenz, der je Fahrbahn erreicht werden muss.
MINDESTGEWINN = 0.60
#: Anteil, unter dem von einer Wirkung keine Rede mehr sein kann.
MINDESTWIRKUNG = 0.05

PUNKTE = {
    # Handwerk: Regeln eingehalten, robust, echtzeitfaehig
    "nur_rumpf_geaendert": 8,
    "anlage_unveraendert": 4,
    "keine_verbotenen_konstrukte": 3,
    "implementierung_vorhanden": 5,
    "randfaelle": 8,
    "kein_eingriff_ohne_bremswunsch": 4,
    "zustand_begrenzt": 3,
    "echtzeit": 3,
    "ausnahmefrei_im_kreis": 2,
    # Regelgüte im geschlossenen Kreis
    "regelt": 5,
    "kein_dauerblockieren": 5,
    "schlupf_nicht_dauerhoch": 5,
    "besser_als_ohne_regelung": 15,
    "bremsweg_asphalt": 10,
    "bremsweg_nass": 10,
    "bremsweg_glatt": 10,
}
BESTEHENSGRENZE = 60

#: Ohne diese beiden gilt der Lauf als nicht bestanden, unabhaengig von der
#: Punktzahl. Die Aufgabe nennt die Beschraenkung auf eine Funktion
#: ausdruecklich; eine Loesung, die sie umgeht, hat eine andere Aufgabe geloest.
#: Die Punktzahl bleibt trotzdem stehen — sie sagt, wie gut der Regler war.
PFLICHT = ("nur_rumpf_geaendert", "anlage_unveraendert")


def lade_modul(ws: Path, name: str):
    """Importiert ein Modul **aus dem Workspace**, ohne den Suchpfad zu vererben."""
    pfad = ws / f"{name}.py"
    spezifikation = importlib.util.spec_from_file_location(name, pfad)
    modul = importlib.util.module_from_spec(spezifikation)
    sys.modules[name] = modul
    spezifikation.loader.exec_module(modul)
    return modul


def main() -> None:
    ws = Path(sys.argv[1]).resolve() if len(sys.argv) > 1 else Path(".").resolve()
    checks: Dict[str, bool] = {name: False for name in PUNKTE}
    befunde: Dict[str, Any] = {}

    checks.update(pruefe_statisch(ws, befunde))

    if str(ws) not in sys.path:
        sys.path.insert(0, str(ws))
    try:
        with redirect_stdout(io.StringIO()):
            lfs = lade_modul(ws, "lfs_link")
            regelung = lade_modul(ws, "abs_regelung")
        regler = getattr(regelung, ZU_AENDERNDE_FUNKTION)
        checks["importierbar"] = True
    except Exception as e:
        befunde["import"] = f"{type(e).__name__}: {e}"
        punkte = sum(PUNKTE[n] for n, ok in checks.items() if ok and n in PUNKTE)
        print(json.dumps({"passed": False, "checks": checks,
                          "deterministic_score": punkte, "befunde": befunde},
                         ensure_ascii=False))
        return

    checks.update(pruefe_verhalten(lfs, regler, befunde))
    checks.update(pruefe_regelung(lfs, regler, befunde))

    punkte = sum(PUNKTE[n] for n, ok in checks.items() if ok and n in PUNKTE)
    checks.pop("importierbar", None)
    verletzt = [n for n in PFLICHT if not checks.get(n)]
    if verletzt:
        befunde["pflichtverletzung"] = verletzt
    print(json.dumps({
        "passed": punkte >= BESTEHENSGRENZE and not verletzt,
        "checks": checks,
        "deterministic_score": punkte,
        "max_score": sum(PUNKTE.values()),
        "befunde": befunde,
    }, ensure_ascii=False))


if __name__ == "__main__":
    main()
