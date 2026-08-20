"""
orchestrator.py — Ablauf und Bewertung des ABS-Benchmarks.

Der Orchestrator ist der *Pruefstand*. Er wird nach der Kandidatenaenderung genau
einmal ausgefuehrt und macht der Reihe nach:

  1. LFS starten und warten, bis InSim, OutGauge und OutSim liefern
  2. den Regelkreis aus ``lfs_link`` starten (ruft die Kandidatenfunktion mit 20 Hz)
  3. eine Sitzung in LFS aufsetzen (aufgezeichneter Menue-Trace + LFS-Kommandos)
  4. drei Szenarien abfahren und je Szenario ein Messfenster aufzeichnen
        1) Vollbremsung auf Asphalt
        2) Vollbremsung auf niedrigem Reibwert
        3) Vollbremsung in der Kurve auf Asphalt
  5. die Messschriebe gegen zwei Basislaeufe vergleichen und punkten
  6. pruefen, ob wirklich nur die eine Funktion veraendert wurde

Alle Stellen, die von der konkreten LFS-Installation und den selbst
aufgenommenen Traces abhaengen, sind mit PLATZHALTER gekennzeichnet und stehen
gesammelt im Abschnitt "Konfiguration".

Aufrufe
-------
    python harness/orchestrator.py --lauf
        Kompletter Benchmark-Lauf mit Bewertung.

    python harness/orchestrator.py --basiswerte ohne_abs
    python harness/orchestrator.py --basiswerte referenz
        Basislaeufe aufzeichnen (einmalig, siehe README.md):
        "ohne_abs"  = unveraenderte Durchreich-Funktion, LFS-Fahrhilfen aus
        "referenz"  = Fahrzeug/Einstellung *mit* funktionierendem ABS

    python harness/orchestrator.py --nur-codepruefung
        Nur pruefen, ob ausschliesslich die erlaubte Funktion geaendert wurde.

    python harness/orchestrator.py --szenario 2 --lauf
        Einzelnes Szenario fahren (Fehlersuche).
"""

from __future__ import annotations

import argparse
import ast
import json
import math
import os
import subprocess
import sys
import time
from dataclasses import dataclass, field, asdict
from typing import Any, Dict, List, Optional, Tuple

HARNESS_VERZEICHNIS = os.path.dirname(os.path.abspath(__file__))
PROJEKTWURZEL = os.path.dirname(HARNESS_VERZEICHNIS)
for _p in (PROJEKTWURZEL, HARNESS_VERZEICHNIS):
    if _p not in sys.path:
        sys.path.insert(0, _p)

from lfs_link import LfsAnbindung, KONFIG                # noqa: E402


def _recorder():
    """Laedt input_recorder erst bei Bedarf.

    Der Recorder braucht pynput; Auswertung und Codepruefung sollen auch ohne
    Eingabebibliothek laufen (z. B. auf einem Auswerterechner ohne LFS).
    """
    import input_recorder
    return input_recorder


# ══════════════════════════════════════════════════════════════════════════════
# Konfiguration — hier stehen alle PLATZHALTER
# ══════════════════════════════════════════════════════════════════════════════

# --- LFS-Installation ---------------------------------------------------------
LFS_PFAD = r"C:\LFS\LFS.exe"                      # PLATZHALTER
LFS_CFG = r"C:\LFS\cfg.txt"                       # PLATZHALTER
LFS_STARTZEIT_S = 20.0                            # PLATZHALTER: Ladezeit bis zum Hauptmenue
LFS_SELBST_STARTEN = True                         # False = LFS laeuft bereits
LFS_NACH_LAUF_BEENDEN = False

# --- Traces (mit harness/input_recorder.py aufnehmen) -------------------------
# Menue-Trace: vom LFS-Hauptmenue bis "Fahrzeug steht fahrbereit auf der Strecke".
TRACE_SITZUNG = os.path.join(HARNESS_VERZEICHNIS, "traces", "sitzung_start.json")   # PLATZHALTER

# --- Auswertung ---------------------------------------------------------------
SCHWELLE_BREMSBEGINN_PROZENT = 20.0   # ab hier gilt der Bremsvorgang als begonnen
V_STILLSTAND_MPS = 1.0
V_REF_HOCH_MPS = 25.0                 # PLATZHALTER: Bremsweg wird zwischen diesen
V_REF_NIEDRIG_MPS = 5.0               # PLATZHALTER: beiden Geschwindigkeiten gemessen
SCHLUPF_BLOCKIERT = 0.40              # ab diesem Bremsschlupf gilt das Rad als blockiert
SCHWIMMWINKEL_DREHER_GRAD = 45.0      # darueber gilt der Lauf als Dreher

# Gewichte der Teilkriterien je Szenariotyp (Summe jeweils 1.0)
GEWICHTE_GERADEAUS = {"bremsweg": 0.60, "blockieranteil": 0.25, "stabilitaet": 0.15}
GEWICHTE_KURVE = {"bremsweg": 0.35, "lenkbarkeit": 0.40, "stabilitaet": 0.25}

# Anteil der Fahrleistung an der Gesamtnote; der Rest kommt aus der Codebewertung,
# die ausserhalb dieses Skripts vergeben wird (siehe AUFGABE.md / README.md).
ANTEIL_FAHRLEISTUNG = 0.70
ANTEIL_CODEBEWERTUNG = 0.30

ABZUG_REGLERFEHLER = 0.20             # Ausnahme in der Kandidatenfunktion
ABZUG_TAKTVERLETZUNG = 0.10           # Reglerlaufzeit ueber dem Takt
MAX_REGLERLAUFZEIT_MS = 10.0          # 20 % des 50-ms-Takts


@dataclass
class Szenario:
    """Ein Fahrversuch. Zeiten in ms, immer relativ zum Bezugspunkt."""

    schluessel: str
    name: str
    trace: str                                  # aufgezeichneter Fahr-Trace
    messfenster_ms: Tuple[int, int]             # PLATZHALTER: (von, bis)
    messfenster_bezug: str = "start"            # "start" oder Name einer Marke im Trace
    lfs_kommandos: List[str] = field(default_factory=list)
    maus_bis_marke: Optional[str] = "fahrt_start"   # ab hier gehoert die Maus dem Regelkreis
    vorlauf_s: float = 2.0                      # Ruhe vor dem Abspielen
    nachlauf_s: float = 2.0                     # Auslaufen nach dem Abspielen
    kurvenszenario: bool = False
    gewicht: float = 1.0
    hinweis: str = ""


# PLATZHALTER: Traces aufnehmen, Messfenster eintragen, LFS-Kommandos pruefen.
# Die Kommandos gehen ueber InSim an LFS; wenn eine LFS-Version einen Befehl nicht
# kennt, die Liste leeren und die Streckenwahl stattdessen im Menue-Trace aufnehmen.
SZENARIEN: List[Szenario] = [
    Szenario(
        schluessel="1_asphalt_gerade",
        name="Vollbremsung auf Asphalt",
        trace=os.path.join(HARNESS_VERZEICHNIS, "traces", "szenario_1_asphalt.json"),
        messfenster_ms=(0, 20000),                       # PLATZHALTER
        messfenster_bezug="start",                       # PLATZHALTER: oder "fahrt_start"
        lfs_kommandos=["/track BL1", "/car XRG", "/restart"],   # PLATZHALTER
        kurvenszenario=False,
        gewicht=0.35,
        hinweis="Beschleunigen bis Zielgeschwindigkeit, dann Vollbremsung geradeaus.",
    ),
    Szenario(
        schluessel="2_niedriger_reibwert",
        name="Vollbremsung auf niedrigem Reibwert",
        trace=os.path.join(HARNESS_VERZEICHNIS, "traces", "szenario_2_niedrig_mue.json"),
        messfenster_ms=(0, 25000),                       # PLATZHALTER
        messfenster_bezug="start",                       # PLATZHALTER
        lfs_kommandos=["/track BL1", "/car XRG", "/restart"],   # PLATZHALTER
        kurvenszenario=False,
        gewicht=0.35,
        hinweis="Niedriger Reibwert: Gras-/Schotterflaeche oder Rallycross-Konfiguration. "
                "Die Flaeche muss lang genug fuer eine vollstaendige Bremsung sein.",
    ),
    Szenario(
        schluessel="3_kurve_asphalt",
        name="Vollbremsung in der Kurve auf Asphalt",
        trace=os.path.join(HARNESS_VERZEICHNIS, "traces", "szenario_3_kurve.json"),
        messfenster_ms=(0, 20000),                       # PLATZHALTER
        messfenster_bezug="start",                       # PLATZHALTER
        lfs_kommandos=["/track BL1", "/car XRG", "/restart"],   # PLATZHALTER
        kurvenszenario=True,
        gewicht=0.30,
        hinweis="Konstante Kurvenfahrt, dann Vollbremsung bei gehaltenem Lenkwinkel. "
                "Bewertet wird, ob das Fahrzeug lenkbar bleibt.",
    ),
]

VERZEICHNIS_BASISWERTE = os.path.join(HARNESS_VERZEICHNIS, "basiswerte")
VERZEICHNIS_ERGEBNISSE = os.path.join(HARNESS_VERZEICHNIS, "ergebnisse")

# Datei und Funktion, die der Kandidat veraendern darf.
KANDIDATENDATEI = "abs_regelung.py"
KANDIDATENFUNKTION = "berechne_pedalwerte"


# ══════════════════════════════════════════════════════════════════════════════
# LFS starten und vorbereiten
# ══════════════════════════════════════════════════════════════════════════════

ERWARTETE_CFG = {
    "OutGauge Mode": "2", "OutGauge Port": "30000", "OutGauge Delay": "1",
    "OutSim Mode": "2", "OutSim Port": "29998", "OutSim Delay": "1",
    "OutSim Opts": "1ff",
}


def pruefe_cfg(pfad: str = LFS_CFG) -> List[str]:
    """Liest cfg.txt und meldet fehlende Eintraege.

    Ohne 'OutSim Opts 1ff' fehlt der Rad-Block und damit die
    Vorderachsgeschwindigkeit — der haeufigste stille Fehler.
    """
    maengel: List[str] = []
    try:
        with open(pfad, "r", encoding="latin-1") as f:
            zeilen = f.read().splitlines()
    except OSError as e:
        return [f"cfg.txt nicht lesbar ({pfad}): {e}"]
    werte = {}
    for zeile in zeilen:
        teile = zeile.strip().rsplit(" ", 1)
        if len(teile) == 2:
            werte[teile[0].strip()] = teile[1].strip()
    for schluessel, erwartet in ERWARTETE_CFG.items():
        ist = werte.get(schluessel)
        if ist is None or ist.lower() != erwartet.lower():
            maengel.append(f"cfg.txt: '{schluessel}' ist '{ist}', erwartet '{erwartet}'")
    return maengel


def starte_lfs() -> Optional[subprocess.Popen]:
    if not LFS_SELBST_STARTEN:
        print("[orchestrator] LFS wird nicht selbst gestartet (LFS_SELBST_STARTEN=False).")
        return None
    if not os.path.exists(LFS_PFAD):
        print(f"[orchestrator] LFS nicht gefunden: {LFS_PFAD}")
        return None
    print(f"[orchestrator] Starte LFS: {LFS_PFAD}")
    prozess = subprocess.Popen([LFS_PFAD], cwd=os.path.dirname(LFS_PFAD))
    time.sleep(LFS_STARTZEIT_S)
    return prozess


def verbinde_mit_lfs(versuche: int = 12, pause_s: float = 5.0) -> LfsAnbindung:
    """Baut die Anbindung auf. LFS braucht nach dem Start Zeit fuer autoexec.lfs."""
    for versuch in range(1, versuche + 1):
        anbindung = LfsAnbindung(KONFIG)
        if anbindung.starte(mit_regelkreis=True):
            if anbindung.warte_auf_daten(zeitlimit_s=10.0):
                print("[orchestrator] Verbindung steht, Telemetrie laeuft.")
                return anbindung
            print("[orchestrator] InSim ok, aber keine OutGauge-/OutSim-Pakete "
                  "(Fahrzeug auf der Strecke? Innenansicht? cfg.txt?).")
            return anbindung
        anbindung.stoppe()
        print(f"[orchestrator] Verbindungsversuch {versuch}/{versuche} fehlgeschlagen, "
              f"warte {pause_s:.0f} s ...")
        time.sleep(pause_s)
    raise RuntimeError("Keine Verbindung zu LFS — laeuft '/insim 29999'?")


def sitzung_aufsetzen(anbindung: LfsAnbindung) -> None:
    """Spielt den Menue-Trace ab, bis das Fahrzeug fahrbereit auf der Strecke steht."""
    if not os.path.exists(TRACE_SITZUNG):
        print(f"[orchestrator] PLATZHALTER: Menue-Trace fehlt ({TRACE_SITZUNG}). "
              f"Mit 'python harness/input_recorder.py aufnehmen ...' aufzeichnen.")
        return
    print("[orchestrator] Setze Sitzung auf ...")
    anbindung.regelkreis.achsen.aktiv = False          # Maus gehoert dem Menue-Replay
    try:
        _recorder().spiele_ab(TRACE_SITZUNG)
    finally:
        anbindung.regelkreis.achsen.aktiv = True
    time.sleep(2.0)


# ══════════════════════════════════════════════════════════════════════════════
# Szenario fahren
# ══════════════════════════════════════════════════════════════════════════════

def fahre_szenario(anbindung: LfsAnbindung, szenario: Szenario) -> Dict[str, Any]:
    """Faehrt ein Szenario und liefert den Messschrieb des Messfensters."""
    print(f"\n[orchestrator] === {szenario.name} ===")
    if szenario.hinweis:
        print(f"               {szenario.hinweis}")

    for kommando in szenario.lfs_kommandos:
        anbindung.insim.sende_kommando(kommando)
        time.sleep(1.0)
    anbindung.insim.frage_zustand_ab()
    time.sleep(1.0)

    if not os.path.exists(szenario.trace):
        raise FileNotFoundError(
            f"Trace fehlt: {szenario.trace}\n"
            f"  -> python harness/input_recorder.py aufnehmen {szenario.trace}")

    recorder = _recorder()
    trace = recorder.lade_trace(szenario.trace)
    markenzeiten: Dict[str, float] = {}

    # Maus-Eigentum: bis zur Marke spielt der Trace Mausereignisse ab (Menue),
    # ab der Marke ist die Maus die Fahrzeugachse. Enthaelt der Trace die Marke
    # nicht, bleiben die Achsen durchgehend aktiv.
    achsen = anbindung.regelkreis.achsen
    nutze_marke = bool(szenario.maus_bis_marke
                       and szenario.maus_bis_marke in recorder.marken(trace))
    if szenario.maus_bis_marke and not nutze_marke:
        print(f"[orchestrator] WARNUNG: Marke '{szenario.maus_bis_marke}' fehlt im Trace — "
              f"Replay und Regelkreis teilen sich die Maus.")
    achsen.aktiv = not nutze_marke

    def marke_gesehen(name: str, t: float) -> None:
        markenzeiten[name] = t
        if nutze_marke and name == szenario.maus_bis_marke:
            achsen.aktiv = True

    anbindung.regelkreis.setze_regler_zurueck()
    anbindung.regelkreis.leere_abtastungen()
    time.sleep(szenario.vorlauf_s)

    t0 = time.monotonic()
    try:
        recorder.Abspieler().spiele_ab(
            trace, maus_bis_marke=szenario.maus_bis_marke,
            bei_marke=marke_gesehen, t0=t0)
    finally:
        achsen.aktiv = True
    time.sleep(szenario.nachlauf_s)

    # Bezugspunkt des Messfensters bestimmen.
    if szenario.messfenster_bezug == "start":
        bezug = t0
    else:
        bezug = markenzeiten.get(szenario.messfenster_bezug)
        if bezug is None:
            print(f"[orchestrator] WARNUNG: Marke '{szenario.messfenster_bezug}' nicht "
                  f"gefunden — Messfenster bezieht sich auf den Trace-Start.")
            bezug = t0

    von = bezug + szenario.messfenster_ms[0] / 1000.0
    bis = bezug + szenario.messfenster_ms[1] / 1000.0
    abtastungen = anbindung.regelkreis.hole_abtastungen(von, bis)
    print(f"[orchestrator] Messfenster {szenario.messfenster_ms[0]}–"
          f"{szenario.messfenster_ms[1]} ms: {len(abtastungen)} Abtastungen")

    return {
        "szenario": szenario.schluessel,
        "name": szenario.name,
        "aufgenommen_am": time.strftime("%Y-%m-%d %H:%M:%S"),
        "messfenster_ms": list(szenario.messfenster_ms),
        "messfenster_bezug": szenario.messfenster_bezug,
        "takt_ms": KONFIG.takt_ms,
        "abtastungen": [_abtastung_zu_json(a, bezug) for a in abtastungen],
    }


def _abtastung_zu_json(abtastung, bezug: float) -> Dict[str, Any]:
    z = abtastung.zustand
    return {
        "t_ms": round((abtastung.t_mono - bezug) * 1000.0, 1),
        "fahrer_gas": round(abtastung.fahrer_gas, 2),
        "fahrer_bremse": round(abtastung.fahrer_bremse, 2),
        "fahrer_lenkung": round(abtastung.fahrer_lenkung, 2),
        "ausgabe_gas": round(abtastung.ausgabe_gas, 2),
        "ausgabe_bremse": round(abtastung.ausgabe_bremse, 2),
        "regler_laufzeit_ms": round(abtastung.regler_laufzeit_ms, 3),
        "regler_fehler": abtastung.regler_fehler,
        "v_ueber_grund_mps": round(z["v_ueber_grund_mps"], 4),
        "v_vorderachse_mps": round(z["v_vorderachse_mps"], 4),
        "v_laengs_mps": round(z["v_laengs_mps"], 4),
        "v_quer_mps": round(z["v_quer_mps"], 4),
        "schwimmwinkel_rad": round(z["schwimmwinkel_rad"], 5),
        "gierrate_rad_s": round(z["gierrate_rad_s"], 5),
        "laengsbeschleunigung_mps2": round(z["laengsbeschleunigung_mps2"], 4),
        "querbeschleunigung_mps2": round(z["querbeschleunigung_mps2"], 4),
        "kurswinkel_rad": round(z["kurswinkel_rad"], 5),
        "lenkung_ist_norm": round(z["lenkung_ist_norm"], 4),
        "bremse_ist_norm": round(z["bremse_ist_norm"], 4),
        "gas_ist_norm": round(z["gas_ist_norm"], 4),
        "gang": z["gang"],
        "position_m": [round(v, 3) for v in z["position_m"]],
        "daten_gueltig": z["daten_gueltig"],
        "vorderachse_gueltig": z["vorderachse_gueltig"],
    }


# ══════════════════════════════════════════════════════════════════════════════
# Auswertung
# ══════════════════════════════════════════════════════════════════════════════

def berechne_metriken(messschrieb: Dict[str, Any]) -> Dict[str, Any]:
    """Verdichtet einen Messschrieb zu den Kennwerten der Bewertung."""
    proben = [p for p in messschrieb["abtastungen"] if p["daten_gueltig"]]
    if len(proben) < 5:
        return {"gueltig": False, "grund": "zu wenige gueltige Abtastungen"}

    t = [p["t_ms"] / 1000.0 for p in proben]
    v = [p["v_ueber_grund_mps"] for p in proben]
    v_va = [p["v_vorderachse_mps"] for p in proben]

    # Bremsbeginn = erste Fahreranforderung ueber der Schwelle.
    beginn = next((i for i, p in enumerate(proben)
                   if p["fahrer_bremse"] >= SCHWELLE_BREMSBEGINN_PROZENT), None)
    if beginn is None:
        return {"gueltig": False, "grund": "kein Bremsvorgang im Messfenster"}

    # Weg durch Integration der Geschwindigkeit (Trapez) — unempfindlicher gegen
    # einzelne Positionsspruenge als die Differenz der Koordinaten.
    weg = [0.0] * len(proben)
    for i in range(beginn + 1, len(proben)):
        weg[i] = weg[i - 1] + 0.5 * (v[i] + v[i - 1]) * (t[i] - t[i - 1])

    def weg_bei_v(v_ziel: float) -> Optional[float]:
        """Zurueckgelegter Weg, wenn die Geschwindigkeit v_ziel unterschritten wird."""
        for i in range(beginn + 1, len(proben)):
            if v[i] <= v_ziel < v[i - 1]:
                anteil = (v[i - 1] - v_ziel) / max(1e-6, v[i - 1] - v[i])
                return weg[i - 1] + anteil * (weg[i] - weg[i - 1])
        return None

    s_hoch = weg_bei_v(V_REF_HOCH_MPS)
    s_niedrig = weg_bei_v(V_REF_NIEDRIG_MPS)
    s_still = weg_bei_v(V_STILLSTAND_MPS)

    bremsweg_referenz = (s_niedrig - s_hoch) if (s_hoch is not None and s_niedrig is not None) else None
    mittlere_verzoegerung = None
    if bremsweg_referenz and bremsweg_referenz > 0.1:
        mittlere_verzoegerung = ((V_REF_HOCH_MPS ** 2 - V_REF_NIEDRIG_MPS ** 2)
                                 / (2.0 * bremsweg_referenz))

    # Bremsphase: ab Bremsbeginn bis Stillstand bzw. Fensterende.
    ende = next((i for i in range(beginn, len(proben)) if v[i] <= V_STILLSTAND_MPS),
                len(proben) - 1)
    phase = range(beginn, max(beginn + 1, ende + 1))

    # Schlupf nur dort, wo die Vorderachsgeschwindigkeit gueltig ist.
    schlupfwerte: List[Tuple[float, float]] = []
    for i in phase:
        if proben[i]["vorderachse_gueltig"] and v[i] > 2.0:
            schlupf = (v[i] - v_va[i]) / v[i]
            dt = t[i] - t[i - 1] if i > 0 else 0.05
            schlupfwerte.append((max(0.0, min(1.0, schlupf)), dt))
    zeit_gesamt = sum(dt for _, dt in schlupfwerte)
    zeit_blockiert = sum(dt for s, dt in schlupfwerte if s >= SCHLUPF_BLOCKIERT)
    blockieranteil = zeit_blockiert / zeit_gesamt if zeit_gesamt > 0 else None
    mittlerer_schlupf = (sum(s * dt for s, dt in schlupfwerte) / zeit_gesamt
                         if zeit_gesamt > 0 else None)

    # Stabilitaet und Lenkbarkeit.
    schwimm = [abs(math.degrees(proben[i]["schwimmwinkel_rad"])) for i in phase]
    gier = [abs(proben[i]["gierrate_rad_s"]) for i in phase]
    kurswinkelaenderung = math.degrees(abs(_winkeldifferenz(
        proben[phase.stop - 1]["kurswinkel_rad"], proben[beginn]["kurswinkel_rad"])))

    # Modulation: wie oft wechselt die Bremsanforderung die Richtung?
    wechsel = 0
    richtung = 0
    for i in range(beginn + 1, phase.stop):
        d = proben[i]["ausgabe_bremse"] - proben[i - 1]["ausgabe_bremse"]
        neu = 1 if d > 1.0 else (-1 if d < -1.0 else richtung)
        if neu != richtung and richtung != 0:
            wechsel += 1
        richtung = neu
    dauer_phase = max(1e-3, t[phase.stop - 1] - t[beginn])

    laufzeiten = [p["regler_laufzeit_ms"] for p in proben]
    fehler = sum(1 for p in proben if p["regler_fehler"])

    return {
        "gueltig": True,
        "v_start_mps": v[beginn],
        "bremsbeginn_s": t[beginn],
        "bremsdauer_s": dauer_phase,
        "bremsweg_referenz_m": bremsweg_referenz,
        "bremsweg_bis_stillstand_m": s_still,
        "stillstand_erreicht": s_still is not None,
        "mittlere_verzoegerung_mps2": mittlere_verzoegerung,
        "blockieranteil": blockieranteil,
        "mittlerer_schlupf": mittlerer_schlupf,
        "schwimmwinkel_max_grad": max(schwimm) if schwimm else None,
        "gierrate_mittel_rad_s": sum(gier) / len(gier) if gier else None,
        "gierrate_max_rad_s": max(gier) if gier else None,
        "kurswinkelaenderung_grad": kurswinkelaenderung,
        "dreher": bool(schwimm and max(schwimm) > SCHWIMMWINKEL_DREHER_GRAD),
        "bremsmodulationen_pro_s": wechsel / dauer_phase,
        "regler_laufzeit_max_ms": max(laufzeiten) if laufzeiten else 0.0,
        "regler_laufzeit_mittel_ms": sum(laufzeiten) / len(laufzeiten) if laufzeiten else 0.0,
        "regler_fehler": fehler,
        "abtastungen": len(proben),
    }


def _winkeldifferenz(a: float, b: float) -> float:
    """Differenz zweier Winkel in rad, auf -pi .. +pi normiert."""
    d = (a - b + math.pi) % (2.0 * math.pi) - math.pi
    return d


def _normiere(wert: Optional[float], ohne_abs: Optional[float],
              referenz: Optional[float]) -> Optional[float]:
    """0 = so gut wie ohne ABS, 1 = so gut wie die Referenz.

    Die Richtung (kleiner/groesser ist besser) ergibt sich aus den beiden
    Basiswerten selbst, es muss nichts zusaetzlich angegeben werden.
    """
    if wert is None or ohne_abs is None or referenz is None:
        return None
    if abs(referenz - ohne_abs) < 1e-9:
        return 1.0 if abs(wert - referenz) < 1e-9 else 0.0
    x = (wert - ohne_abs) / (referenz - ohne_abs)
    return max(-0.5, min(1.25, x))


def bewerte_szenario(szenario: Szenario, metriken: Dict[str, Any],
                     basis_ohne: Dict[str, Any], basis_referenz: Dict[str, Any]) -> Dict[str, Any]:
    if not metriken.get("gueltig"):
        return {"punkte": 0.0, "gueltig": False, "grund": metriken.get("grund"),
                "teilnoten": {}}

    gewichte = GEWICHTE_KURVE if szenario.kurvenszenario else GEWICHTE_GERADEAUS
    teil: Dict[str, Optional[float]] = {}
    teil["bremsweg"] = _normiere(metriken["bremsweg_referenz_m"],
                                 basis_ohne.get("bremsweg_referenz_m"),
                                 basis_referenz.get("bremsweg_referenz_m"))
    teil["blockieranteil"] = _normiere(metriken["blockieranteil"],
                                       basis_ohne.get("blockieranteil"),
                                       basis_referenz.get("blockieranteil"))
    teil["stabilitaet"] = _normiere(metriken["schwimmwinkel_max_grad"],
                                    basis_ohne.get("schwimmwinkel_max_grad"),
                                    basis_referenz.get("schwimmwinkel_max_grad"))
    teil["lenkbarkeit"] = _normiere(metriken["kurswinkelaenderung_grad"],
                                    basis_ohne.get("kurswinkelaenderung_grad"),
                                    basis_referenz.get("kurswinkelaenderung_grad"))

    summe = 0.0
    gewicht_summe = 0.0
    for name, gewicht in gewichte.items():
        if teil.get(name) is not None:
            summe += gewicht * teil[name]
            gewicht_summe += gewicht
    punkte = summe / gewicht_summe if gewicht_summe > 0 else 0.0

    anmerkungen: List[str] = []
    if metriken.get("dreher"):
        punkte *= 0.2
        anmerkungen.append("Dreher: Schwimmwinkel ueber "
                           f"{SCHWIMMWINKEL_DREHER_GRAD:.0f} Grad")
    if not metriken.get("stillstand_erreicht"):
        anmerkungen.append("Fahrzeug kam im Messfenster nicht zum Stehen")
    if metriken.get("regler_fehler"):
        punkte *= (1.0 - ABZUG_REGLERFEHLER)
        anmerkungen.append(f"{metriken['regler_fehler']} Ausnahmen in der Reglerfunktion")
    if metriken.get("regler_laufzeit_max_ms", 0.0) > MAX_REGLERLAUFZEIT_MS:
        punkte *= (1.0 - ABZUG_TAKTVERLETZUNG)
        anmerkungen.append(f"Reglerlaufzeit bis {metriken['regler_laufzeit_max_ms']:.1f} ms "
                           f"(Grenze {MAX_REGLERLAUFZEIT_MS:.0f} ms)")
    if gewicht_summe < 0.999:
        anmerkungen.append("Teilkriterien ohne Basiswert wurden uebersprungen")

    return {"punkte": max(0.0, punkte), "gueltig": True,
            "teilnoten": teil, "anmerkungen": anmerkungen}


# ══════════════════════════════════════════════════════════════════════════════
# Codepruefung: wurde wirklich nur die eine Funktion veraendert?
# ══════════════════════════════════════════════════════════════════════════════

def _git(*argumente: str) -> Optional[str]:
    try:
        ergebnis = subprocess.run(["git", *argumente], cwd=PROJEKTWURZEL,
                                  capture_output=True, text=True, timeout=30)
    except (OSError, subprocess.SubprocessError):
        return None
    return ergebnis.stdout if ergebnis.returncode == 0 else None


def pruefe_kandidatendatei() -> Dict[str, Any]:
    """Vergleicht die Kandidatendatei mit dem Original aus der Git-Historie.

    Geprueft wird zweierlei:
      1. Es wurde keine andere Datei angefasst.
      2. In der Kandidatendatei ist ausser dem Rumpf der erlaubten Funktion
         nichts veraendert (AST-Vergleich, unempfindlich gegen Formatierung).
    """
    bericht: Dict[str, Any] = {"verstoesse": [], "hinweise": [], "pruefbar": True}

    status = _git("status", "--porcelain")
    if status is None:
        bericht["pruefbar"] = False
        bericht["hinweise"].append("Kein Git-Repository — Dateipruefung uebersprungen.")
    else:
        erlaubt_unversioniert = ("harness/ergebnisse/", "harness/traces/", "harness/basiswerte/",
                                 "kalibrierung.json", "rollradius.json", "__pycache__")
        for zeile in status.splitlines():
            pfad = zeile[3:].strip().strip('"')
            if any(pfad.startswith(p) or p in pfad for p in erlaubt_unversioniert):
                continue
            if pfad != KANDIDATENDATEI:
                bericht["verstoesse"].append(f"Fremde Aenderung: {zeile.strip()}")

    original = _git("show", f"HEAD:{KANDIDATENDATEI}")
    if original is None:
        bericht["pruefbar"] = False
        bericht["hinweise"].append("Original der Kandidatendatei nicht aus Git lesbar.")
        return bericht

    try:
        with open(os.path.join(PROJEKTWURZEL, KANDIDATENDATEI), "r", encoding="utf-8") as f:
            aktuell = f.read()
    except OSError as e:
        bericht["verstoesse"].append(f"Kandidatendatei nicht lesbar: {e}")
        return bericht

    vergleich = vergleiche_ast(original, aktuell)
    bericht["verstoesse"].extend(vergleich["verstoesse"])
    bericht["hinweise"].extend(vergleich["hinweise"])
    return bericht


def vergleiche_ast(original: str, aktuell: str) -> Dict[str, List[str]]:
    """Vergleicht zwei Fassungen der Kandidatendatei auf Modulebene.

    Erlaubt ist ausschliesslich ein anderer Rumpf der Kandidatenfunktion. Der
    Vergleich laeuft ueber den Syntaxbaum und ist damit unempfindlich gegen
    Formatierung, Leerzeilen und Kommentare ausserhalb von Docstrings.
    """
    bericht: Dict[str, List[str]] = {"verstoesse": [], "hinweise": []}
    try:
        baum_alt = ast.parse(original)
        baum_neu = ast.parse(aktuell)
    except SyntaxError as e:
        bericht["verstoesse"].append(f"Kandidatendatei ist nicht parsebar: {e}")
        return bericht

    knoten_alt = {_knotenname(k): k for k in baum_alt.body}
    knoten_neu = {_knotenname(k): k for k in baum_neu.body}

    for name in knoten_neu.keys() - knoten_alt.keys():
        bericht["verstoesse"].append(f"Neues Element auf Modulebene: {name}")
    for name in knoten_alt.keys() - knoten_neu.keys():
        bericht["verstoesse"].append(f"Entferntes Element auf Modulebene: {name}")

    for name in knoten_alt.keys() & knoten_neu.keys():
        alt, neu = knoten_alt[name], knoten_neu[name]
        if name == f"funktion:{KANDIDATENFUNKTION}":
            # Signatur und Dekoratoren muessen bleiben, der Rumpf darf sich aendern.
            if ast.dump(alt.args) != ast.dump(neu.args):
                bericht["verstoesse"].append("Signatur von "
                                             f"{KANDIDATENFUNKTION} wurde veraendert")
            if ast.dump(ast.Module(body=alt.decorator_list, type_ignores=[])) != \
               ast.dump(ast.Module(body=neu.decorator_list, type_ignores=[])):
                bericht["verstoesse"].append("Dekoratoren von "
                                             f"{KANDIDATENFUNKTION} wurden veraendert")
            if ast.dump(alt) == ast.dump(neu):
                bericht["hinweise"].append(f"{KANDIDATENFUNKTION} ist unveraendert — "
                                           "es wurde keine Regelung implementiert.")
            continue
        if ast.dump(alt) != ast.dump(neu):
            bericht["verstoesse"].append(f"Veraendert, obwohl gesperrt: {name}")

    return bericht


def _knotenname(knoten: ast.AST) -> str:
    if isinstance(knoten, (ast.FunctionDef, ast.AsyncFunctionDef)):
        return f"funktion:{knoten.name}"
    if isinstance(knoten, ast.ClassDef):
        return f"klasse:{knoten.name}"
    if isinstance(knoten, (ast.Import, ast.ImportFrom)):
        return f"import:{ast.dump(knoten)}"
    if isinstance(knoten, ast.Expr) and isinstance(knoten.value, ast.Constant):
        return "moduldocstring"
    if isinstance(knoten, (ast.Assign, ast.AnnAssign)):
        ziele = getattr(knoten, "targets", [getattr(knoten, "target", None)])
        namen = ",".join(getattr(z, "id", "?") for z in ziele if z is not None)
        return f"zuweisung:{namen}"
    return f"sonstiges:{type(knoten).__name__}:{getattr(knoten, 'lineno', 0)}"


# ══════════════════════════════════════════════════════════════════════════════
# Dateien
# ══════════════════════════════════════════════════════════════════════════════

def speichere(pfad: str, inhalt: Dict[str, Any]) -> None:
    os.makedirs(os.path.dirname(os.path.abspath(pfad)), exist_ok=True)
    with open(pfad, "w", encoding="utf-8") as f:
        json.dump(inhalt, f, indent=1)


def lade(pfad: str) -> Optional[Dict[str, Any]]:
    try:
        with open(pfad, "r", encoding="utf-8") as f:
            return json.load(f)
    except (OSError, ValueError):
        return None


def basiswertpfad(szenario: Szenario, art: str) -> str:
    return os.path.join(VERZEICHNIS_BASISWERTE, f"{szenario.schluessel}_{art}.json")


# ══════════════════════════════════════════════════════════════════════════════
# Ablaeufe
# ══════════════════════════════════════════════════════════════════════════════

def _gewaehlte_szenarien(nummer: Optional[int]) -> List[Szenario]:
    if nummer is None:
        return SZENARIEN
    if not 1 <= nummer <= len(SZENARIEN):
        raise SystemExit(f"Szenario {nummer} gibt es nicht (1..{len(SZENARIEN)}).")
    return [SZENARIEN[nummer - 1]]


def fahre_alle(szenarien: List[Szenario]) -> Dict[str, Dict[str, Any]]:
    maengel = pruefe_cfg()
    for m in maengel:
        print(f"[orchestrator] WARNUNG: {m}")

    prozess = starte_lfs()
    anbindung = verbinde_mit_lfs()
    messschriebe: Dict[str, Dict[str, Any]] = {}
    try:
        sitzung_aufsetzen(anbindung)
        for szenario in szenarien:
            messschriebe[szenario.schluessel] = fahre_szenario(anbindung, szenario)
    finally:
        anbindung.stoppe()
        if prozess and LFS_NACH_LAUF_BEENDEN:
            prozess.terminate()
    return messschriebe


def lauf_basiswerte(art: str, nummer: Optional[int]) -> int:
    szenarien = _gewaehlte_szenarien(nummer)
    print(f"[orchestrator] Basislauf '{art}'.")
    if art == "ohne_abs":
        print("               Voraussetzung: unveraendertes abs_regelung.py, "
              "alle LFS-Fahrhilfen aus.")
    else:
        print("               Voraussetzung: unveraendertes abs_regelung.py, "
              "Fahrzeug/Einstellung MIT funktionierendem ABS.")
    messschriebe = fahre_alle(szenarien)
    for szenario in szenarien:
        schrieb = messschriebe[szenario.schluessel]
        metriken = berechne_metriken(schrieb)
        speichere(basiswertpfad(szenario, art),
                  {"art": art, "szenario": szenario.schluessel,
                   "metriken": metriken, "messschrieb": schrieb})
        print(f"[orchestrator] {szenario.name}: " + _kurzfassung(metriken))
    return 0


def lauf_benchmark(nummer: Optional[int]) -> int:
    szenarien = _gewaehlte_szenarien(nummer)
    codebericht = pruefe_kandidatendatei()
    messschriebe = fahre_alle(szenarien)

    ergebnis: Dict[str, Any] = {
        "erstellt": time.strftime("%Y-%m-%d %H:%M:%S"),
        "codepruefung": codebericht,
        "szenarien": {},
    }
    gesamt = 0.0
    gewichtssumme = 0.0

    for szenario in szenarien:
        schrieb = messschriebe[szenario.schluessel]
        metriken = berechne_metriken(schrieb)
        basis_ohne = (lade(basiswertpfad(szenario, "ohne_abs")) or {}).get("metriken", {})
        basis_ref = (lade(basiswertpfad(szenario, "referenz")) or {}).get("metriken", {})
        if not basis_ohne or not basis_ref:
            print(f"[orchestrator] WARNUNG: Basiswerte fuer {szenario.schluessel} fehlen "
                  f"— Szenario wird nur gemessen, nicht bewertet.")
        note = bewerte_szenario(szenario, metriken, basis_ohne, basis_ref)
        ergebnis["szenarien"][szenario.schluessel] = {
            "name": szenario.name, "metriken": metriken, "bewertung": note,
            "messschrieb": schrieb,
        }
        if note["gueltig"] and basis_ohne and basis_ref:
            gesamt += szenario.gewicht * note["punkte"]
            gewichtssumme += szenario.gewicht

    fahrleistung = (gesamt / gewichtssumme) if gewichtssumme > 0 else 0.0
    ergebnis["fahrleistung_0_1"] = round(fahrleistung, 4)
    ergebnis["fahrleistung_punkte"] = round(100.0 * ANTEIL_FAHRLEISTUNG * fahrleistung, 1)
    ergebnis["hinweis_gesamtnote"] = (
        f"Fahrleistung zaehlt {ANTEIL_FAHRLEISTUNG:.0%}, die Codebewertung "
        f"{ANTEIL_CODEBEWERTUNG:.0%}. Die Codebewertung wird ausserhalb dieses "
        f"Skripts vergeben (siehe README.md).")

    pfad = os.path.join(VERZEICHNIS_ERGEBNISSE,
                        f"ergebnis_{time.strftime('%Y%m%d_%H%M%S')}.json")
    speichere(pfad, ergebnis)
    _bericht(ergebnis, pfad)
    return 0


def _kurzfassung(metriken: Dict[str, Any]) -> str:
    if not metriken.get("gueltig"):
        return f"ungueltig ({metriken.get('grund')})"
    def z(x, n=1):
        return "—" if x is None else f"{x:.{n}f}"
    return (f"v0 {z(metriken['v_start_mps'])} m/s, "
            f"Bremsweg {z(metriken['bremsweg_referenz_m'])} m, "
            f"a {z(metriken['mittlere_verzoegerung_mps2'], 2)} m/s2, "
            f"blockiert {z((metriken['blockieranteil'] or 0) * 100)} %, "
            f"Schwimmwinkel max {z(metriken['schwimmwinkel_max_grad'])} Grad")


def _bericht(ergebnis: Dict[str, Any], pfad: str) -> None:
    print("\n" + "=" * 78)
    print("ABS-BENCHMARK — ERGEBNIS")
    print("=" * 78)
    for schluessel, eintrag in ergebnis["szenarien"].items():
        print(f"\n{eintrag['name']}  [{schluessel}]")
        print("  " + _kurzfassung(eintrag["metriken"]))
        note = eintrag["bewertung"]
        if note["gueltig"]:
            teile = ", ".join(f"{k} {v:+.2f}" for k, v in note["teilnoten"].items()
                              if v is not None)
            print(f"  Teilnoten: {teile}")
            print(f"  Punkte   : {note['punkte']:.3f}  (0 = wie ohne ABS, 1 = wie Referenz)")
        for a in note.get("anmerkungen", []):
            print(f"  ! {a}")

    print("\nCodepruefung:")
    bericht = ergebnis["codepruefung"]
    if bericht["verstoesse"]:
        for v in bericht["verstoesse"]:
            print(f"  VERSTOSS: {v}")
    else:
        print("  keine Verstoesse — es wurde nur die erlaubte Funktion geaendert.")
    for h in bericht.get("hinweise", []):
        print(f"  Hinweis: {h}")

    print(f"\nFahrleistung: {ergebnis['fahrleistung_0_1']:.3f} "
          f"-> {ergebnis['fahrleistung_punkte']:.1f} von "
          f"{100 * ANTEIL_FAHRLEISTUNG:.0f} Punkten")
    print(ergebnis["hinweis_gesamtnote"])
    print(f"\nVollstaendiges Ergebnis: {pfad}")


def main() -> int:
    p = argparse.ArgumentParser(description="ABS-Benchmark fuer Live for Speed")
    g = p.add_mutually_exclusive_group(required=True)
    g.add_argument("--lauf", action="store_true", help="Benchmark fahren und bewerten")
    g.add_argument("--basiswerte", choices=["ohne_abs", "referenz"],
                   help="Basislauf aufzeichnen")
    g.add_argument("--nur-codepruefung", action="store_true",
                   help="Nur pruefen, ob ausschliesslich die erlaubte Funktion geaendert wurde")
    g.add_argument("--nur-cfg", action="store_true", help="Nur cfg.txt pruefen")
    p.add_argument("--szenario", type=int, default=None, help="nur Szenario N (1..3)")
    args = p.parse_args()

    if args.nur_cfg:
        maengel = pruefe_cfg()
        print("\n".join(maengel) if maengel else "cfg.txt ist vollstaendig.")
        return 1 if maengel else 0
    if args.nur_codepruefung:
        bericht = pruefe_kandidatendatei()
        print(json.dumps(bericht, indent=2, ensure_ascii=False))
        return 1 if bericht["verstoesse"] else 0
    if args.basiswerte:
        return lauf_basiswerte(args.basiswerte, args.szenario)
    return lauf_benchmark(args.szenario)


if __name__ == "__main__":
    sys.exit(main())
