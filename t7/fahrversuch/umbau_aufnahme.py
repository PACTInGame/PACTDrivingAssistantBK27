#!/usr/bin/env python
"""Baut eine mit der Maus gefahrene Aufnahme auf Pfeiltasten um.

Warum
-----
Die Referenzaufnahme ``90_abs_tests`` wurde mit den **Maustasten** gefahren:
linke Taste = Gas, rechte Taste = Bremse, und die Lenkung ueber die Mausposition.
Der Pruefstand stellt das Fahrzeug aber selbst ueber die **Mausposition** — die
Maus ist waehrend der Fahrt die Fahrzeugachse. Ein Abspieler, der in derselben
Zeit Mausereignisse einspielt, streitet mit dem Regelkreis um den Cursor, und
beide verlieren.

Die Loesung ist die Fahrereingabe des Pruefstands: die **Pfeiltasten**. Sie sind
in LFS auf nichts gebunden, gehen also am Spiel vorbei und nur an den
Pruefstand. Tastatur laesst sich gefahrlos abspielen, waehrend die Maus eine
Achse ist.

Was umgebaut wird
-----------------
Nur die Fahrphase — vom Startmarker (Vorgabe ``ontrack``) bis zu der Taste, mit
der das Szenario die Strecke wieder verlaesst (Vorgabe ``esc``). Die
Menuefuehrung davor und danach bleibt Byte fuer Byte erhalten, denn dort gehoert
die Maus dem Abspieler.

============================  ==========================================
In der Fahrphase              wird zu
============================  ==========================================
linke Maustaste gedrueckt     Pfeil hoch gedrueckt   (Gas)
rechte Maustaste gedrueckt    Pfeil runter gedrueckt (Bremse)
Mausbewegung in X             Pfeil links / rechts   (Lenkung, siehe unten)
Mausbewegung in Y             entfaellt (das war die Pedalachse)
Tasten (SHIFT+R, S, ...)      bleiben unveraendert
============================  ==========================================

Die Lenkung
-----------
Sie steckte in der X-Position des Cursors und ist nicht nebensaechlich: in der
Referenzaufnahme wird in zwei der fuenf Bremsungen deutlich eingelenkt — das ist
der Lenkbarkeitsteil des Szenarios. Er geht verloren, wenn man die Bewegung
einfach wegwirft.

Umgesetzt wird sie deshalb ueber eine **Ruecksimulation der Rampe**: der
Pruefstand macht aus einer gehaltenen Pfeiltaste einen Lenkwinkel, der mit
``lenkung_anstieg_ms`` aufbaut und mit ``lenkung_ruecklauf_ms`` zurueckfaellt.
Das Werkzeug rechnet aus der aufgezeichneten Cursorbahn den gewuenschten
Lenkwinkel, simuliert dieselbe Rampe auf einem 50-ms-Raster und waehlt in jedem
Schritt die Taste (links, rechts oder keine), die am dichtesten an den
gewuenschten Winkel fuehrt. Am Ende steht ein Tastendrehbuch, das der Cursorbahn
folgt, so gut zwei Tasten das koennen.

Der verbleibende Fehler wird gemessen und ausgegeben. Er ist dort klein, wo
langsam eingelenkt wurde, und gross bei einem Lenkstoss, der schneller ist als
die Rampe. Eine Aufnahme, die von vornherein mit den Pfeiltasten gefahren wurde,
ist immer besser — das hier ist die Bruecke zu einer Aufnahme, die es schon gibt.

Aufruf
------
::

    python umbau_aufnahme.py                       # 90_abs_tests -> 90_abs_pfeiltasten
    python umbau_aufnahme.py --quelle X --ziel Y
    python umbau_aufnahme.py --ohne-lenkung        # Lenkung verwerfen
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import sys
import time
from typing import Any, Dict, List, Optional, Tuple

HIER = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HIER)
sys.path.insert(0, os.path.join(os.path.dirname(HIER), "workspace"))

from simulation_tests import input_model, paths   # noqa: E402
from lfs_link import KONFIG, Pfeiltasteneingabe   # noqa: E402

#: Windows-Virtual-Key-Codes der Pfeiltasten.
VK_LINKS, VK_HOCH, VK_RECHTS, VK_RUNTER = 0x25, 0x26, 0x27, 0x28

#: Welche Maustaste welchem Pedal entspricht — so wurde die Referenz gefahren.
TASTE_JE_KNOPF = {
    "left":  ("up", VK_HOCH),
    "right": ("down", VK_RUNTER),
}

#: Raster, auf dem das Lenk-Drehbuch geschrieben wird. Gleich dem Regeltakt.
LENKRASTER_S = 0.05


def _fahrphase(events: List[Dict[str, Any]], startmarke: str,
               endetaste: str) -> Tuple[float, float]:
    """``(von, bis)`` der Fahrphase in Sekunden der Aufnahme."""
    von: Optional[float] = None
    for e in events:
        if e.get("kind") == input_model.KIND_MARKER and e.get("name") == startmarke:
            von = e["t"]
            break
    if von is None:
        raise SystemExit(f"Marke {startmarke!r} kommt in der Aufnahme nicht vor. "
                         f"Vorhanden: {[n for _, n in input_model.markers(events)]}")
    bis = input_model.duration(events) + 1.0
    for e in events:
        if (e.get("kind") == input_model.KIND_KEY and e.get("action") == "down"
                and e["t"] > von and e.get("name") == endetaste):
            bis = e["t"]
            break
    return von, bis


def _lenkbahn(events: List[Dict[str, Any]], von: float, bis: float,
              mitte: float, spanne: float) -> List[Tuple[float, float]]:
    """Gewuenschter Lenkwinkel in Prozent, auf dem Raster ``LENKRASTER_S``.

    Die Cursorbahn ist eine Treppe: zwischen zwei Bewegungen stand der Zeiger
    still, also wird der letzte Wert gehalten.
    """
    bewegungen = [(e["t"], float(e["x"])) for e in events
                  if e.get("kind") == input_model.KIND_MOVE and von <= e["t"] < bis]
    bewegungen.sort()
    bahn: List[Tuple[float, float]] = []
    i, x = 0, mitte
    t = von
    while t < bis:
        while i < len(bewegungen) and bewegungen[i][0] <= t:
            x = bewegungen[i][1]
            i += 1
        soll = max(-100.0, min(100.0, (x - mitte) / spanne * 100.0))
        bahn.append((t, soll))
        t += LENKRASTER_S
    return bahn


def _lenkdrehbuch(bahn: List[Tuple[float, float]], toleranz: Optional[float] = None
                  ) -> Tuple[List[Dict[str, Any]], Dict[str, float]]:
    """Macht aus dem gewuenschten Lenkwinkel ein Drehbuch fuer zwei Tasten.

    In jedem Rasterschritt wird die Rampe des Pruefstands fuer alle drei
    moeglichen Tastenzustaende vorausgerechnet und der genommen, der dem Sollwert
    am naechsten kommt. Ein Wechsel kostet ``toleranz`` Prozentpunkte, sonst
    klappert die Taste im Raster hin und her, ohne dass es dem Winkel hilft.

    ``toleranz`` haengt an der Rampe und wird deshalb aus ihr abgeleitet: ein
    Rasterschritt kann den Winkel um hoechstens ``schritt`` Prozentpunkte
    bewegen. Eine Toleranz darueber wuerde *jeden* Wechsel verbieten und die
    Lenkung stillschweigend ganz abschalten.
    """
    schritt = 100.0 * LENKRASTER_S * 1000.0 / max(1, KONFIG.lenkung_anstieg_ms)
    if toleranz is None:
        toleranz = 0.35 * schritt
    toleranz = min(toleranz, 0.5 * schritt)
    kandidaten = {"none": 0.0, "left": -100.0, "right": 100.0}
    schritt = {"left": (VK_LINKS, "left"), "right": (VK_RECHTS, "right")}

    def naechster(ist: float, taste: str) -> float:
        return Pfeiltasteneingabe._rampe(ist, kandidaten[taste], LENKRASTER_S,
                                         KONFIG.lenkung_anstieg_ms,
                                         KONFIG.lenkung_ruecklauf_ms)

    ereignisse: List[Dict[str, Any]] = []
    ist, gehalten = 0.0, "none"
    fehler_summe, fehler_max, n = 0.0, 0.0, 0
    for t, soll in bahn:
        bester, bester_fehler = gehalten, abs(naechster(ist, gehalten) - soll)
        for taste in kandidaten:
            if taste == gehalten:
                continue
            fehler = abs(naechster(ist, taste) - soll)
            if fehler < bester_fehler - toleranz:
                bester, bester_fehler = taste, fehler
        if bester != gehalten:
            if gehalten != "none":
                vk, name = schritt[gehalten]
                ereignisse.append({"t": round(t, 4), "kind": input_model.KIND_KEY,
                                   "action": "up", "name": name, "vk": vk})
            if bester != "none":
                vk, name = schritt[bester]
                ereignisse.append({"t": round(t, 4), "kind": input_model.KIND_KEY,
                                   "action": "down", "name": name, "vk": vk})
            gehalten = bester
        ist = naechster(ist, gehalten)
        fehler_summe += (ist - soll) ** 2
        fehler_max = max(fehler_max, abs(ist - soll))
        n += 1

    if gehalten != "none" and bahn:
        vk, name = schritt[gehalten]
        ereignisse.append({"t": round(bahn[-1][0] + LENKRASTER_S, 4),
                           "kind": input_model.KIND_KEY, "action": "up",
                           "name": name, "vk": vk})
    guete = {"lenkfehler_rms_prozent": round((fehler_summe / n) ** 0.5, 1) if n else 0.0,
             "lenkfehler_max_prozent": round(fehler_max, 1),
             "lenkereignisse": len(ereignisse)}
    return ereignisse, guete


def baue_um(events: List[Dict[str, Any]], von: float, bis: float,
            mitte: float, spanne: float, mit_lenkung: bool = True
            ) -> Tuple[List[Dict[str, Any]], Dict[str, Any]]:
    """Ersetzt Maustasten durch Pfeiltasten und die Cursorbahn durch Lenktasten.

    Gibt die neuen Ereignisse und einen Bericht zurueck.
    """
    neu: List[Dict[str, Any]] = []
    gehalten: Dict[str, Tuple[str, int]] = {}      # Knopf -> Pfeiltaste
    bericht = {"ersetzte_klicks": 0, "entfernte_bewegungen": 0,
               "unbekannte_knoepfe": [], "lenkweg_px": 0.0,
               "fahrphase_s": [round(von, 3), round(bis, 3)],
               "lenkung_uebernommen": bool(mit_lenkung)}

    for e in events:
        t = e["t"]
        in_fahrt = von <= t < bis
        kind = e.get("kind")

        if not in_fahrt:
            neu.append(e)
            continue

        if kind == input_model.KIND_MOVE:
            # Die Maus ist waehrend der Fahrt die Fahrzeugachse; jede
            # abgespielte Bewegung waere ein zweiter Fahrer am selben Lenkrad.
            # Die Lenkinformation daraus wird unten als Tastendrehbuch ersetzt.
            bericht["entfernte_bewegungen"] += 1
            bericht["lenkweg_px"] = max(bericht["lenkweg_px"],
                                        abs(float(e["x"]) - mitte))
            continue

        if kind == input_model.KIND_CLICK:
            knopf = str(e.get("button", ""))
            zuordnung = TASTE_JE_KNOPF.get(knopf)
            if zuordnung is None:
                if knopf not in bericht["unbekannte_knoepfe"]:
                    bericht["unbekannte_knoepfe"].append(knopf)
                neu.append(e)
                continue
            name, vk = zuordnung
            if e.get("action") == "down":
                gehalten[knopf] = zuordnung
            else:
                gehalten.pop(knopf, None)
            neu.append({"t": t, "kind": input_model.KIND_KEY,
                        "action": e.get("action"), "name": name, "vk": vk})
            bericht["ersetzte_klicks"] += 1
            continue

        neu.append(e)

    # Eine Maustaste, die beim Verlassen der Strecke noch gehalten wird, muss
    # als Pfeiltaste losgelassen werden — sonst bleibt ein Pedal getreten.
    for knopf, (name, vk) in sorted(gehalten.items()):
        neu.append({"t": max(0.0, bis - 0.05), "kind": input_model.KIND_KEY,
                    "action": "up", "name": name, "vk": vk})
        bericht.setdefault("nachgezogene_freigaben", []).append(name)

    if mit_lenkung:
        lenkereignisse, guete = _lenkdrehbuch(_lenkbahn(events, von, bis, mitte, spanne))
        neu.extend(lenkereignisse)
        bericht.update(guete)

    neu.sort(key=lambda e: e["t"])
    bericht["lenkweg_px"] = round(bericht["lenkweg_px"], 1)
    return neu, bericht


def main() -> int:
    p = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    p.add_argument("--quelle", default="90_abs_tests", help="Name des Quellszenarios")
    p.add_argument("--ziel", default="90_abs_pfeiltasten", help="Name des Zielszenarios")
    p.add_argument("--start-marke", default="ontrack",
                   help="Marke, ab der die Fahrphase beginnt")
    p.add_argument("--ende-taste", default="esc",
                   help="Taste, mit der die Strecke wieder verlassen wird")
    p.add_argument("--ohne-lenkung", action="store_true",
                   help="Cursorbahn verwerfen statt sie auf Lenktasten abzubilden")
    p.add_argument("--ueberschreiben", action="store_true")
    a = p.parse_args()

    quelle = paths.scenario_dir(a.quelle)
    ziel = os.path.join(paths.SCENARIOS_DIR, a.ziel)
    if os.path.isdir(ziel) and not a.ueberschreiben:
        print(f"{ziel} gibt es schon. --ueberschreiben verwenden.")
        return 2
    os.makedirs(ziel, exist_ok=True)

    meta, events = input_model.read_recording(os.path.join(quelle, paths.INPUT_FILE))
    von, bis = _fahrphase(events, a.start_marke, a.ende_taste)
    # Dieselbe Abbildung Cursorposition <-> Achswert, die der Pruefstand stellt:
    # sonst zeigt das Drehbuch auf einen Lenkwinkel, den es nie gegeben hat.
    breite = float((meta.get("screen_size") or [1920, 1080])[0])
    mitte = breite / 2.0
    spanne = (breite - 2 * KONFIG.maus_rand_px) / 2.0
    neu, bericht = baue_um(events, von, bis, mitte, spanne,
                           mit_lenkung=not a.ohne_lenkung)

    probleme = input_model.check_recording(neu)
    if probleme:
        print("Die umgebaute Aufnahme ist nicht abspielbar:")
        for x in probleme:
            print("  -", x)
        return 1

    meta = dict(meta)
    meta["scenario"] = a.ziel
    meta["event_count"] = len(neu)
    meta["duration_s"] = input_model.duration(neu)
    meta["abgeleitet_von"] = a.quelle
    meta["umbau"] = bericht
    meta["umgebaut_utc"] = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
    input_model.write_recording(os.path.join(ziel, paths.INPUT_FILE), meta, neu)

    # scenario.json mitnehmen und auf den Pruefstand anpassen.
    with open(os.path.join(quelle, paths.SCENARIO_FILE), "r", encoding="utf-8") as f:
        szenario = json.load(f)
    szenario["name"] = a.ziel
    szenario["description"] = (szenario.get("description", "").strip()
                               + f" Aus {a.quelle} auf Pfeiltasten umgebaut.").strip()
    szenario["markers"] = [n for _, n in input_model.markers(neu)]
    szenario.setdefault("tracer", {})
    # Der Pruefstand besitzt OutGauge auf 30000. Der Tracer fragt es deshalb
    # nicht an und beschraenkt sich auf IS_MCI und die Ereignispakete.
    szenario["tracer"]["outgauge_interval_ms"] = 0
    szenario["tracer"]["outsim_interval_ms"] = 0
    szenario["tracer"]["mci_interval_ms"] = 50
    # IS_NPL traegt die Fahrhilfen nur so, wie sie beim Beitritt waren. SHIFT+G
    # auf der Strecke meldet sich ausschliesslich ueber IS_PFL — und ob eine
    # LFS-Bremshilfe mitbremst, entscheidet ueber die Gueltigkeit des Laufs.
    pakete = list(szenario["tracer"].get("packets", []))
    if "PFL" not in pakete:
        pakete.append("PFL")
    szenario["tracer"]["packets"] = pakete
    szenario["duration_s"] = meta["duration_s"]
    with open(os.path.join(ziel, paths.SCENARIO_FILE), "w", encoding="utf-8") as f:
        json.dump(szenario, f, indent=2, ensure_ascii=False)
        f.write("\n")

    for name in ("timeline.md", "timeline.draft.md"):
        quell_datei = os.path.join(quelle, name)
        if os.path.isfile(quell_datei):
            shutil.copy2(quell_datei, os.path.join(ziel, name))

    print(f"{a.quelle} -> {a.ziel}")
    print(f"  Fahrphase              : {von:.2f} s .. {bis:.2f} s")
    print(f"  ersetzte Klicks        : {bericht['ersetzte_klicks']}")
    print(f"  entfernte Mausbewegungen: {bericht['entfernte_bewegungen']}")
    print(f"  Lenkweg des Cursors    : {bericht['lenkweg_px']:.0f} px "
          f"(= {bericht['lenkweg_px'] / spanne * 100:.0f} % Lenkwinkel)")
    if bericht.get("lenkung_uebernommen"):
        print(f"  Lenk-Tastenereignisse  : {bericht['lenkereignisse']}")
        print(f"  Lenkfehler (RMS / max) : {bericht['lenkfehler_rms_prozent']:.1f} % / "
              f"{bericht['lenkfehler_max_prozent']:.1f} %")
        if bericht["lenkfehler_max_prozent"] > 25:
            print("  ACHTUNG: Die Rampe des Pruefstands kommt der aufgezeichneten")
            print("           Lenkung nicht hinterher. Kurvenszenario besser mit den")
            print("           Pfeiltasten neu aufnehmen.")
    elif bericht["lenkweg_px"] > 60:
        print("  ACHTUNG: In der Fahrphase wurde gelenkt, die Lenkung wurde aber")
        print("           verworfen (--ohne-lenkung).")
    if bericht["unbekannte_knoepfe"]:
        print(f"  nicht zugeordnete Knoepfe: {bericht['unbekannte_knoepfe']}")
    if bericht.get("nachgezogene_freigaben"):
        print(f"  nachgezogene Freigaben : {bericht['nachgezogene_freigaben']}")
    print(f"  Ereignisse             : {len(events)} -> {len(neu)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
