#!/usr/bin/env python
"""Faehrt einen Kandidaten in LFS und bewertet ihn.

Ein Lauf besteht aus drei Dingen, die gleichzeitig passieren:

1. **Der Pruefstand** (``lfs_link``) laeuft in *diesem* Prozess. Er haelt
   OutGauge auf 30000, eine eigene InSim-Verbindung fuer IS_MCI, den
   50-ms-Regeltakt mit der Kandidatenfunktion und die Maus als Fahrzeugachse.
2. **Der Abspieler** (``simulation_tests/run_scenario.py``) laeuft als eigener
   Prozess. Er spielt die Aufnahme ein — Menuefuehrung mit der Maus, die Fahrt
   mit den Pfeiltasten — und zeichnet nebenher einen unabhaengigen InSim-Trace
   auf.
3. **Die Auswertung** (``auswertung.py``) liest hinterher beides.

Warum die Maus nicht kollidiert: der Pruefstand fasst den Cursor nur an, solange
LFS ``fahrbereit`` meldet (im Spiel, kein Dialog, nicht pausiert). Am Hauptmenue
und in jedem Dialog gehoert die Maus dem Abspieler. Ab dem Moment, in dem das
Fahrzeug auf der Strecke steht, faehrt die Aufnahme nur noch mit Tasten.

Warum kein UDP-Relay: der Tracer fragt OutGauge gar nicht erst an
(``outgauge_interval_ms: 0``). IS_MCI bekommt er ueber seine eigene
InSim-Verbindung auf seinem eigenen UDP-Port; der Pruefstand laesst sich IS_MCI
ueber TCP schicken (``UDPPort = 0``). Die beiden treten sich nicht auf die Fuesse.

Aufruf
------
::

    python run_fahrversuch.py                          # Kandidat in ../workspace
    python run_fahrversuch.py --workspace <pfad>       # ein Benchmark-Workspace
    python run_fahrversuch.py --als-basiswert ohne_abs
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import sys
import time
from dataclasses import asdict
from datetime import datetime
from typing import Any, Dict, List, Optional, Tuple

HIER = os.path.dirname(os.path.abspath(__file__))
STANDARD_WORKSPACE = os.path.normpath(os.path.join(HIER, "..", "workspace"))
STANDARD_SZENARIO = "90_abs_pfeiltasten"
KALIBRIERUNG = os.path.join(HIER, "kalibrierung.json")

sys.path.insert(0, HIER)
from simulation_tests import paths   # noqa: E402


def _lade_pruefstand(workspace: str):
    """Importiert ``lfs_link`` und ``abs_regelung`` **aus dem Workspace**.

    Der Kandidat liegt je Lauf woanders; ein fest verdrahteter Import wuerde
    stillschweigend immer dieselbe Fassung fahren.
    """
    workspace = os.path.abspath(workspace)
    for name in ("lfs_link", "abs_regelung"):
        if not os.path.isfile(os.path.join(workspace, f"{name}.py")):
            raise SystemExit(f"{name}.py fehlt in {workspace}")
        sys.modules.pop(name, None)
    sys.path.insert(0, workspace)
    import lfs_link                                   # noqa: E402
    if os.path.dirname(os.path.abspath(lfs_link.__file__)) != workspace:
        raise SystemExit("Es wurde ein anderes lfs_link importiert als das aus dem "
                         f"Workspace: {lfs_link.__file__}")
    return lfs_link


def _schreibe_schrieb(pfad: str, meta: Dict[str, Any], takte: List[Any],
                      t0: float) -> int:
    """Schreibt den Reglerschrieb als JSONL. Zeiten relativ zum Laufbeginn."""
    with open(pfad, "w", encoding="utf-8", newline="\n") as f:
        f.write(json.dumps({"kind": "meta", "d": meta}, ensure_ascii=False) + "\n")
        for takt in takte:
            d = asdict(takt)
            satz = {
                "kind": "takt",
                "t": round(d["t_mono"] - t0, 4),
                "fahrer_gas": round(d["fahrer_gas"], 2),
                "fahrer_bremse": round(d["fahrer_bremse"], 2),
                "fahrer_lenkung": round(d["fahrer_lenkung"], 2),
                "ausgabe_gas": round(d["ausgabe_gas"], 2),
                "ausgabe_bremse": round(d["ausgabe_bremse"], 2),
                "achse_laengs": round(d["achse_laengs"], 4),
                "achse_quer": round(d["achse_quer"], 4),
                "laufzeit_ms": round(d["laufzeit_ms"], 4),
                "fehler": d["fehler"],
                "z": d["zustand"],
            }
            f.write(json.dumps(satz, ensure_ascii=False) + "\n")
    return len(takte)


def _vorpruefung(lfs_link, workspace: str, szenario: str
                 ) -> Tuple[List[str], List[str]]:
    """``(probleme, hinweise)`` — Probleme halten den Lauf an, Hinweise nicht."""
    probleme: List[str] = []
    hinweise: List[str] = []
    if sys.platform != "win32":
        probleme.append("Ein Fahrversuch braucht Windows (Maus-Achsen, globale Eingabe).")
    try:
        paths.scenario_dir(szenario)
    except (FileNotFoundError, ValueError) as e:
        probleme.append(str(e))
    try:
        import pynput                                  # noqa: F401
    except ImportError:
        probleme.append("pynput fehlt (pip install pynput).")
    if not os.path.isfile(os.path.join(workspace, "kalibrierung.json")):
        # Kein Abbruchgrund: die Achsen laufen dann auf die geometrische
        # Vorgabe (Bildschirmmitte, voller Weg). Die stimmt oft, aber niemand
        # hat sie gemessen — und ein Lauf mit falscher Achse sieht aus wie ein
        # Regler, der nicht bremst.
        hinweise.append("Keine kalibrierung.json — die Achsen laufen auf die "
                        "geometrische Vorgabe. Einmalig 'python lfs_link.py "
                        "--kalibrieren' fahren und die Datei nach "
                        f"{KALIBRIERUNG} kopieren.")
    return probleme, hinweise


def main() -> int:
    p = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    p.add_argument("--workspace", default=STANDARD_WORKSPACE,
                   help="Verzeichnis mit abs_regelung.py und lfs_link.py")
    p.add_argument("--szenario", default=STANDARD_SZENARIO)
    p.add_argument("--name", default="", help="Name des Laufs (Vorgabe: Zeitstempel)")
    p.add_argument("--countdown", type=int, default=5)
    p.add_argument("--als-basiswert", default="", choices=("", "ohne_abs", "referenz"))
    p.add_argument("--nur-vorpruefung", action="store_true")
    p.add_argument("--force", action="store_true",
                   help="trotz Beanstandungen der Vorpruefung fahren")
    a = p.parse_args()

    workspace = os.path.abspath(a.workspace)
    # Die eingemessene Maus-Achse gehoert zum Rechner, nicht zum Kandidaten: ein
    # frisch geklonter Benchmark-Workspace bringt sie nicht mit.
    if os.path.isfile(KALIBRIERUNG):
        shutil.copy2(KALIBRIERUNG, os.path.join(workspace, "kalibrierung.json"))

    lfs_link = _lade_pruefstand(workspace)
    probleme, hinweise = _vorpruefung(lfs_link, workspace, a.szenario)
    print("Vorpruefung:" if (probleme or hinweise) else "Vorpruefung: in Ordnung.")
    for x in probleme:
        print("  FEHLER:", x)
    for x in hinweise:
        print("  Hinweis:", x)
    if probleme and not a.force and not a.nur_vorpruefung:
        print("\nMit --force trotzdem fahren.")
        return 3
    if a.nur_vorpruefung:
        return 0 if not probleme else 3

    stempel = datetime.now().strftime("%Y%m%d-%H%M%S")
    lauf = os.path.join(paths.RUNS_DIR, f"{a.name or a.szenario}_{stempel}")
    os.makedirs(lauf, exist_ok=True)

    anbindung = lfs_link.LfsAnbindung()
    print("[fahrversuch] Starte Pruefstand ...")
    if not anbindung.starte(mit_regelkreis=True):
        print("[fahrversuch] Pruefstand konnte nicht starten.")
        return 4
    anbindung.regelkreis.setze_regler_zurueck()
    t0 = time.monotonic()

    rueckgabe = 0
    try:
        befehl = [sys.executable,
                  os.path.join(HIER, "simulation_tests", "run_scenario.py"),
                  a.szenario, "--out-dir", lauf,
                  "--countdown", str(a.countdown),
                  "--require", "MCI"]
        print(f"[fahrversuch] {' '.join(befehl[1:])}")
        rueckgabe = subprocess.call(befehl, cwd=HIER)
    except KeyboardInterrupt:
        print("\n[fahrversuch] Abbruch.")
        rueckgabe = 6
    finally:
        rk = anbindung.regelkreis
        takte = rk.hole_takte() if rk else []
        meta = {
            "workspace": workspace,
            "szenario": a.szenario,
            "aufgezeichnet_utc": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
            "takt_ms": lfs_link.KONFIG.takt_ms,
            "zyklen": rk.zyklen if rk else 0,
            "verpasste_takte": rk.ueberlaeufe if rk else 0,
            "reglerfehler": rk.reglerfehler if rk else 0,
            "reglerlaufzeit_max_ms": round(rk.laufzeit_max_ms, 3) if rk else 0.0,
            "outgauge_pakete": anbindung.outgauge.pakete,
            "mci_pakete": anbindung.insim.mci_pakete,
            "run_scenario_rueckgabe": rueckgabe,
        }
        anbindung.stoppe()
        schrieb = os.path.join(lauf, "regler_trace.jsonl")
        anzahl = _schreibe_schrieb(schrieb, meta, takte, t0)
        print(f"[fahrversuch] Reglerschrieb: {schrieb} ({anzahl} Takte)")

    if not takte:
        print("[fahrversuch] Kein einziger Regeltakt aufgezeichnet — nichts zu bewerten.")
        return 5

    import auswertung                                  # noqa: E402
    ergebnis = auswertung.werte_lauf_aus(lauf)
    auswertung.drucke(ergebnis)
    with open(os.path.join(lauf, "ergebnis.json"), "w", encoding="utf-8") as f:
        json.dump(ergebnis, f, indent=2, ensure_ascii=False)

    if a.als_basiswert:
        os.makedirs(auswertung.BASISWERTE_DIR, exist_ok=True)
        ziel = os.path.join(auswertung.BASISWERTE_DIR, f"{a.als_basiswert}.json")
        with open(ziel, "w", encoding="utf-8") as f:
            json.dump({"lauf": ergebnis["lauf"], "mittel": ergebnis["mittel"],
                       "bremsungen": ergebnis["bremsungen"]}, f, indent=2,
                      ensure_ascii=False)
        print(f"[fahrversuch] Basiswert gespeichert: {ziel}")

    return rueckgabe


if __name__ == "__main__":
    raise SystemExit(main())
