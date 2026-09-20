#!/usr/bin/env python
"""Bewertet einen Fahrversuch aus dem Reglerschrieb.

Eingang ist ``regler_trace.jsonl`` — die Aufzeichnung des 50-ms-Regeltakts, die
``run_fahrversuch.py`` schreibt. Sie enthaelt beides: was das Fahrzeug gemeldet
hat *und* was der Regler daraus gemacht hat. Der parallel aufgezeichnete
InSim-Trace des Pruefstands ist der unabhaengige Zeuge (Kollisionen, LFS-Meldungen,
Fahrhilfen) und wird, wenn vorhanden, mitgelesen.

Was gemessen wird
-----------------
Der Schrieb wird nicht nach Marken zerschnitten, sondern nach dem, was der
Fahrer getan hat: jede zusammenhaengende Bremsanforderung ueber
``BREMSSCHWELLE`` aus ausreichendem Tempo ist eine **Bremsung**. Das ist
robuster als eine Zeitmarke — ein Lauf, der 300 ms spaeter startet, wird
trotzdem an derselben Stelle vermessen.

Je Bremsung::

    bremsweg_referenz_m        Weg zwischen V_HOCH und V_TIEF, aus der
                               Geschwindigkeit ueber Grund integriert. Unabhaengig
                               von kleinen Unterschieden im Anfangstempo.
    mittlere_verzoegerung_mps2 (v1^2 - v2^2) / (2 s) aus demselben Fenster
    blockieranteil             Zeitanteil mit Bremsschlupf >= SCHLUPF_BLOCKIERT
    schlupf_median             wo die Regelung den Schlupf im Mittel haelt
    schwimmwinkel_max_grad     Stabilitaet. Ueber DREHER_GRAD gilt der Lauf als Dreher
    kurswinkelaenderung_grad   Lenkbarkeit: blockierte Raeder fahren geradeaus weiter
    bremsmodulationen_pro_s    Regelaktivitaet der Bremsanforderung
    regler_laufzeit_max_ms     Echtzeitverhalten
    regler_fehler              Ausnahmen in der Reglerfunktion

Der Bremsschlupf ist hier ``(v_ueber_grund - v_rad_hinterachse) / v_ueber_grund``.
Beide Groessen stehen roh im Schrieb; die Anlage rechnet sie nicht aus, die
Auswertung schon — sie darf es, der Regler muss es selbst tun.

**Der Schlupf wird an der Hinterachse gemessen, und die ist angetrieben.** Bei
eingelegtem Gang haelt der Motor die Hinterraeder mit; ein Blockieranteil von
0 ist deshalb noch kein Beweis dafuer, dass auch vorne nichts blockiert hat. Der
Kennwert ist ein Vergleichsmass zwischen Laeufen, kein Absolutmass.

Bewertung
---------
Wenn unter ``basiswerte/`` zwei Referenzlaeufe liegen, wird jeder Kennwert
zwischen ihnen normiert: ``0`` = so gut wie **ohne** ABS, ``1`` = so gut wie der
Lauf **mit** ABS. Die Richtung (kleiner oder groesser ist besser) ergibt sich aus
den Basiswerten selbst, nicht aus einer Annahme. Ohne Basiswerte wird gegen die
absoluten Zielwerte in ``ZIELE`` bewertet; das ist der Notbehelf, nicht das Ziel.

Aufruf
------
::

    python auswertung.py <lauf-verzeichnis>
    python auswertung.py <lauf-verzeichnis> --json
"""

from __future__ import annotations

import argparse
import json
import math
import os
import statistics
import sys
from typing import Any, Dict, List, Optional, Tuple

HIER = os.path.dirname(os.path.abspath(__file__))
BASISWERTE_DIR = os.path.join(HIER, "basiswerte")

# ── Schwellen der Auswertung ────────────────────────────────────────────────
#: Ab dieser Bremsanforderung des Fahrers gilt eine Bremsung als begonnen.
BREMSSCHWELLE = 50.0
#: Darunter gilt sie als beendet.
BREMSENDE = 10.0
#: So lange darf die Anforderung unter BREMSENDE fallen, ohne die Bremsung zu teilen.
BREMSLUECKE_S = 0.5
#: Eine Bremsung zaehlt nur ab diesem Anfangstempo.
MIN_STARTTEMPO_MPS = 12.0
#: Referenzfenster fuer den Bremsweg.
V_HOCH_MPS, V_TIEF_MPS = 25.0, 5.0
#: Unterhalb dieses Tempos ist der Schlupf numerisch wertlos.
MIN_SCHLUPFTEMPO_MPS = 3.0
#: Ab hier gilt ein Rad als blockierend.
SCHLUPF_BLOCKIERT = 0.40
#: Ab hier gilt der Lauf als Dreher.
DREHER_GRAD = 45.0
#: Aenderung der Bremsanforderung, die als Modulation zaehlt (Prozentpunkte).
MODULATION_PROZENT = 5.0
#: Reglerlaufzeit, ab der der Takt als verletzt gilt.
LAUFZEIT_BUDGET_MS = 10.0

#: Notbehelf ohne Basiswerte: (schlecht, gut) je Kennwert.
ZIELE = {
    "bremsweg_referenz_m": (85.0, 55.0),
    "blockieranteil": (0.60, 0.02),
    "schwimmwinkel_max_grad": (25.0, 4.0),
    "kurswinkelaenderung_grad": (2.0, 25.0),
}

#: Gewichte je Kennwert, getrennt fuer geradeaus und mit Lenkeingriff.
GEWICHTE_GERADE = {"bremsweg_referenz_m": 0.60, "blockieranteil": 0.25,
                   "schwimmwinkel_max_grad": 0.15}
GEWICHTE_KURVE = {"bremsweg_referenz_m": 0.35, "kurswinkelaenderung_grad": 0.40,
                  "schwimmwinkel_max_grad": 0.25}
#: Ab diesem Lenkeingriff waehrend der Bremsung gilt sie als Kurvenbremsung.
KURVE_LENKUNG_PROZENT = 15.0


# ── Schrieb lesen ───────────────────────────────────────────────────────────

def lade_schrieb(pfad: str) -> Tuple[Dict[str, Any], List[Dict[str, Any]]]:
    """``(meta, takte)``. Der erste Satz ist meta, jeder weitere ein Regeltakt."""
    meta: Dict[str, Any] = {}
    takte: List[Dict[str, Any]] = []
    with open(pfad, "r", encoding="utf-8") as f:
        for zeile in f:
            zeile = zeile.strip()
            if not zeile:
                continue
            satz = json.loads(zeile)
            if satz.get("kind") == "meta":
                meta = satz.get("d", {})
            elif satz.get("kind") == "takt":
                takte.append(satz)
    takte.sort(key=lambda s: s["t"])
    return meta, takte


def _winkel_differenz(a: float, b: float) -> float:
    """``a - b`` auf -pi .. +pi gebracht."""
    return (a - b + math.pi) % (2.0 * math.pi) - math.pi


# ── Bremsungen finden ───────────────────────────────────────────────────────

def finde_bremsungen(takte: List[Dict[str, Any]]) -> List[Tuple[int, int]]:
    """Indexpaare ``(von, bis)`` je zusammenhaengender Bremsanforderung."""
    abschnitte: List[Tuple[int, int]] = []
    start: Optional[int] = None
    letzte_bremse_idx: Optional[int] = None
    for i, s in enumerate(takte):
        bremst = s["fahrer_bremse"] >= BREMSSCHWELLE
        haelt = s["fahrer_bremse"] >= BREMSENDE
        if bremst and start is None:
            start = i
            letzte_bremse_idx = i
        elif start is not None:
            if haelt:
                letzte_bremse_idx = i
            elif s["t"] - takte[letzte_bremse_idx]["t"] > BREMSLUECKE_S:
                abschnitte.append((start, letzte_bremse_idx))
                start, letzte_bremse_idx = None, None
    if start is not None and letzte_bremse_idx is not None:
        abschnitte.append((start, letzte_bremse_idx))

    brauchbar = []
    for von, bis in abschnitte:
        if takte[von]["z"]["v_ueber_grund_mps"] >= MIN_STARTTEMPO_MPS:
            brauchbar.append((von, bis))
    return brauchbar


def _referenzbremsweg(fenster: List[Dict[str, Any]]) -> Tuple[Optional[float], float, float]:
    """Weg zwischen V_HOCH und V_TIEF, integriert aus der Grundgeschwindigkeit.

    Gibt ``(weg_m, v_oben, v_unten)``. ``None``, wenn das Fenster nie durchfahren
    wurde — dann ist der Lauf fuer diesen Kennwert nicht vergleichbar, und das
    muss sichtbar bleiben statt durch ein ersatzweise verschobenes Fenster
    verdeckt zu werden.
    """
    i_oben = None
    for i, s in enumerate(fenster):
        if s["z"]["v_ueber_grund_mps"] <= V_HOCH_MPS:
            i_oben = i
            break
    if i_oben is None:
        return None, 0.0, 0.0
    i_unten = None
    for i in range(i_oben, len(fenster)):
        if fenster[i]["z"]["v_ueber_grund_mps"] <= V_TIEF_MPS:
            i_unten = i
            break
    if i_unten is None or i_unten <= i_oben:
        return None, 0.0, 0.0
    weg = 0.0
    for a, b in zip(fenster[i_oben:i_unten], fenster[i_oben + 1:i_unten + 1]):
        dt = b["t"] - a["t"]
        weg += 0.5 * (a["z"]["v_ueber_grund_mps"] + b["z"]["v_ueber_grund_mps"]) * dt
    return weg, fenster[i_oben]["z"]["v_ueber_grund_mps"], \
        fenster[i_unten]["z"]["v_ueber_grund_mps"]


def kennwerte(takte: List[Dict[str, Any]], von: int, bis: int) -> Dict[str, Any]:
    """Alle Kennwerte einer Bremsung."""
    fenster = takte[von:bis + 1]
    dauer = fenster[-1]["t"] - fenster[0]["t"]

    weg, v_oben, v_unten = _referenzbremsweg(fenster)
    if weg and weg > 0.1:
        verzoegerung = (v_oben ** 2 - v_unten ** 2) / (2.0 * weg)
    else:
        verzoegerung = None

    # Schlupf nur dort, wo er numerisch etwas bedeutet.
    schlupfwerte: List[float] = []
    blockierzeit = 0.0
    schlupfzeit = 0.0
    for a, b in zip(fenster, fenster[1:]):
        v = a["z"]["v_ueber_grund_mps"]
        if v < MIN_SCHLUPFTEMPO_MPS or not a["z"]["daten_gueltig"]:
            continue
        schlupf = (v - a["z"]["v_rad_hinterachse_mps"]) / v
        schlupfwerte.append(schlupf)
        dt = b["t"] - a["t"]
        schlupfzeit += dt
        if schlupf >= SCHLUPF_BLOCKIERT:
            blockierzeit += dt

    schwimm = []
    for s in fenster:
        z = s["z"]
        if z["v_ueber_grund_mps"] >= MIN_SCHLUPFTEMPO_MPS and z["daten_gueltig"]:
            schwimm.append(abs(math.degrees(
                _winkel_differenz(z["bewegungsrichtung_rad"], z["kurswinkel_rad"]))))

    gueltig = [s for s in fenster if s["z"]["daten_gueltig"]]
    if len(gueltig) >= 2:
        kursaenderung = abs(math.degrees(_winkel_differenz(
            gueltig[-1]["z"]["kurswinkel_rad"], gueltig[0]["z"]["kurswinkel_rad"])))
    else:
        kursaenderung = 0.0

    modulationen = 0
    richtung = 0
    for a, b in zip(fenster, fenster[1:]):
        d = b["ausgabe_bremse"] - a["ausgabe_bremse"]
        if abs(d) < MODULATION_PROZENT:
            continue
        neu = 1 if d > 0 else -1
        if neu != richtung:
            modulationen += 1
            richtung = neu

    lenkung_max = max(abs(s["fahrer_lenkung"]) for s in fenster)

    return {
        "t_start_s": round(fenster[0]["t"], 3),
        "dauer_s": round(dauer, 2),
        "v_start_mps": round(fenster[0]["z"]["v_ueber_grund_mps"], 2),
        "v_ende_mps": round(fenster[-1]["z"]["v_ueber_grund_mps"], 2),
        "art": "kurve" if lenkung_max >= KURVE_LENKUNG_PROZENT else "gerade",
        "lenkung_max_prozent": round(lenkung_max, 1),
        "bremsweg_referenz_m": round(weg, 2) if weg is not None else None,
        "mittlere_verzoegerung_mps2": round(verzoegerung, 2) if verzoegerung else None,
        "blockieranteil": round(blockierzeit / schlupfzeit, 3) if schlupfzeit > 0 else None,
        "schlupf_median": round(statistics.median(schlupfwerte), 3) if schlupfwerte else None,
        "schlupf_max": round(max(schlupfwerte), 3) if schlupfwerte else None,
        "schwimmwinkel_max_grad": round(max(schwimm), 1) if schwimm else None,
        "kurswinkelaenderung_grad": round(kursaenderung, 1),
        "bremsmodulationen_pro_s": round(modulationen / dauer, 2) if dauer > 0 else 0.0,
        "regler_laufzeit_max_ms": round(max(s["laufzeit_ms"] for s in fenster), 3),
        "regler_fehler": sum(1 for s in fenster if s.get("fehler")),
        "takte_ohne_daten": sum(1 for s in fenster if not s["z"]["daten_gueltig"]),
    }


# ── Normierung und Punkte ───────────────────────────────────────────────────

def _normiere(wert: Optional[float], schlecht: Optional[float],
              gut: Optional[float]) -> Optional[float]:
    """0 = wie ``schlecht``, 1 = wie ``gut``. Begrenzt auf -0.5 .. 1.25.

    Die Richtung steckt in den beiden Enden, nicht in einer Annahme darueber,
    ob gross oder klein besser ist.
    """
    if wert is None or schlecht is None or gut is None:
        return None
    if abs(gut - schlecht) < 1e-9:
        return 1.0 if abs(wert - gut) < 1e-9 else 0.0
    return max(-0.5, min(1.25, (wert - schlecht) / (gut - schlecht)))


def _mittel(werte: List[Optional[float]]) -> Optional[float]:
    echte = [w for w in werte if w is not None]
    return sum(echte) / len(echte) if echte else None


def lade_basiswerte() -> Dict[str, Dict[str, Any]]:
    """``{"ohne_abs": {...}, "referenz": {...}}``, soweit vorhanden."""
    aus: Dict[str, Dict[str, Any]] = {}
    for name in ("ohne_abs", "referenz"):
        pfad = os.path.join(BASISWERTE_DIR, f"{name}.json")
        if os.path.isfile(pfad):
            with open(pfad, "r", encoding="utf-8") as f:
                aus[name] = json.load(f)
    return aus


def bewerte(bremsungen: List[Dict[str, Any]],
            basis: Dict[str, Dict[str, Any]]) -> Dict[str, Any]:
    """Punkte 0..1 je Bremsung und gesamt."""
    hat_basis = "ohne_abs" in basis and "referenz" in basis
    quelle = "basiswerte" if hat_basis else "zielwerte"

    einzeln: List[Dict[str, Any]] = []
    for b in bremsungen:
        gewichte = GEWICHTE_KURVE if b["art"] == "kurve" else GEWICHTE_GERADE
        teile: Dict[str, Optional[float]] = {}
        for kennwert in gewichte:
            if hat_basis:
                schlecht = basis["ohne_abs"].get("mittel", {}).get(kennwert)
                gut = basis["referenz"].get("mittel", {}).get(kennwert)
            else:
                schlecht, gut = ZIELE.get(kennwert, (None, None))
            teile[kennwert] = _normiere(b.get(kennwert), schlecht, gut)
        vorhanden = {k: v for k, v in teile.items() if v is not None}
        if vorhanden:
            summe = sum(gewichte[k] for k in vorhanden)
            punkte = sum(gewichte[k] * v for k, v in vorhanden.items()) / summe
        else:
            punkte = None
        einzeln.append({"t_start_s": b["t_start_s"], "art": b["art"],
                        "teilpunkte": {k: (round(v, 3) if v is not None else None)
                                       for k, v in teile.items()},
                        "punkte": round(punkte, 3) if punkte is not None else None})

    roh = _mittel([e["punkte"] for e in einzeln])

    abzuege: List[str] = []
    faktor = 1.0
    dreher = [b for b in bremsungen
              if (b["schwimmwinkel_max_grad"] or 0.0) > DREHER_GRAD]
    if dreher:
        faktor *= 0.2
        abzuege.append(f"{len(dreher)} Dreher (Schwimmwinkel > {DREHER_GRAD:.0f} Grad)")
    fehler = sum(b["regler_fehler"] for b in bremsungen)
    if fehler:
        faktor *= 0.8
        abzuege.append(f"{fehler} Ausnahmen in der Reglerfunktion")
    langsam = max((b["regler_laufzeit_max_ms"] for b in bremsungen), default=0.0)
    if langsam > LAUFZEIT_BUDGET_MS:
        faktor *= 0.9
        abzuege.append(f"Reglerlaufzeit bis {langsam:.1f} ms "
                       f"(Budget {LAUFZEIT_BUDGET_MS:.0f} ms)")

    return {
        "normierung": quelle,
        "je_bremsung": einzeln,
        "punkte_roh": round(roh, 3) if roh is not None else None,
        "abzugsfaktor": round(faktor, 3),
        "abzuege": abzuege,
        "punkte": round(max(0.0, roh) * faktor, 3) if roh is not None else None,
    }


# ── InSim-Trace als unabhaengiger Zeuge ─────────────────────────────────────

def pruefe_insim_trace(lauf: str) -> Dict[str, Any]:
    """Liest den parallel aufgezeichneten InSim-Trace, soweit vorhanden."""
    bericht: Dict[str, Any] = {"vorhanden": False}
    pfad = os.path.join(lauf, "trace.jsonl")
    if not os.path.isfile(pfad):
        return bericht
    bericht["vorhanden"] = True
    kontakte, marken, vollstaendig = 0, [], False
    with open(pfad, "r", encoding="utf-8") as f:
        for zeile in f:
            try:
                satz = json.loads(zeile)
            except ValueError:
                continue
            ev = satz.get("ev")
            if ev in ("CON", "OBH"):
                kontakte += 1
            elif satz.get("src") == "marker":
                marken.append(ev)
            elif ev == "end":
                vollstaendig = True
    bericht["kontakte"] = kontakte
    bericht["marken"] = marken
    bericht["trace_vollstaendig"] = vollstaendig

    run_json = os.path.join(lauf, "run.json")
    if os.path.isfile(run_json):
        with open(run_json, "r", encoding="utf-8") as f:
            run = json.load(f)
        bericht["chat_check"] = (run.get("chat_check") or {}).get("verdict")
        bericht["replay_abgebrochen"] = bool(run.get("replay", {}).get("aborted"))
        bericht["lateness_max_s"] = run.get("replay", {}).get("lateness_max_s")
    return bericht


# ── Gesamtlauf ──────────────────────────────────────────────────────────────

def werte_lauf_aus(lauf: str) -> Dict[str, Any]:
    schrieb = os.path.join(lauf, "regler_trace.jsonl")
    if not os.path.isfile(schrieb):
        raise SystemExit(f"Kein Reglerschrieb in {lauf} (regler_trace.jsonl fehlt).")
    meta, takte = lade_schrieb(schrieb)
    if not takte:
        raise SystemExit(f"{schrieb} enthaelt keinen einzigen Regeltakt.")

    abschnitte = finde_bremsungen(takte)
    bremsungen = [kennwerte(takte, von, bis) for von, bis in abschnitte]
    punkte = bewerte(bremsungen, lade_basiswerte())

    mittel = {}
    for kennwert in ("bremsweg_referenz_m", "mittlere_verzoegerung_mps2",
                     "blockieranteil", "schwimmwinkel_max_grad",
                     "kurswinkelaenderung_grad", "bremsmodulationen_pro_s"):
        mittel[kennwert] = _mittel([b[kennwert] for b in bremsungen])
        if mittel[kennwert] is not None:
            mittel[kennwert] = round(mittel[kennwert], 3)

    warnungen: List[str] = []
    if not bremsungen:
        warnungen.append("Keine Bremsung gefunden. Wurde ueberhaupt gefahren, und "
                         "kamen die Pfeiltasten am Pruefstand an?")
    ungueltig = sum(1 for s in takte if not s["z"]["daten_gueltig"])
    if ungueltig > 0.2 * len(takte):
        warnungen.append(f"{ungueltig} von {len(takte)} Takten ohne gueltige "
                         "Telemetrie — Messung ist nur bedingt belastbar.")
    # Aus den Kontrollleuchten laesst sich *nicht* ablesen, ob eine LFS-Fahrhilfe
    # mitbremst: DL_ABS steht laut OutGaugePack.txt fuer "ABS aktiv ODER
    # abgeschaltet" und leuchtet bei abgeschalteten Hilfen dauerhaft. Ob die
    # Hilfen aus sind, sagt IS_PFL im InSim-Trace, nicht OutGauge.

    return {
        "lauf": os.path.basename(os.path.abspath(lauf)),
        "meta": meta,
        "takte": len(takte),
        "takte_ohne_daten": ungueltig,
        "bremsungen": bremsungen,
        "mittel": mittel,
        "bewertung": punkte,
        "insim_trace": pruefe_insim_trace(lauf),
        "warnungen": warnungen,
    }


def drucke(ergebnis: Dict[str, Any]) -> None:
    print(f"\nLauf: {ergebnis['lauf']}  ({ergebnis['takte']} Regeltakte, "
          f"{ergebnis['takte_ohne_daten']} ohne Daten)")
    if not ergebnis["bremsungen"]:
        print("  keine Bremsung gefunden")
    kopf = (f"  {'#':>2} {'art':<7} {'t[s]':>7} {'v0':>6} {'Weg':>7} {'a':>6} "
            f"{'Block':>6} {'Schwimm':>8} {'dKurs':>6} {'Mod/s':>6} {'ms':>5}")
    print(kopf)
    print("  " + "-" * (len(kopf) - 2))
    for i, b in enumerate(ergebnis["bremsungen"], 1):
        def z(v, f="{:.2f}"):
            return f.format(v) if v is not None else "  -"
        print(f"  {i:>2} {b['art']:<7} {b['t_start_s']:>7.1f} {b['v_start_mps']:>6.1f} "
              f"{z(b['bremsweg_referenz_m']):>7} {z(b['mittlere_verzoegerung_mps2']):>6} "
              f"{z(b['blockieranteil'], '{:.3f}'):>6} "
              f"{z(b['schwimmwinkel_max_grad'], '{:.1f}'):>8} "
              f"{b['kurswinkelaenderung_grad']:>6.1f} "
              f"{b['bremsmodulationen_pro_s']:>6.2f} "
              f"{b['regler_laufzeit_max_ms']:>5.2f}")
    bw = ergebnis["bewertung"]
    print(f"\n  Normierung : {bw['normierung']}")
    print(f"  Punkte roh : {bw['punkte_roh']}")
    if bw["abzuege"]:
        print(f"  Abzuege    : {', '.join(bw['abzuege'])} (Faktor {bw['abzugsfaktor']})")
    print(f"  PUNKTE     : {bw['punkte']}")
    it = ergebnis["insim_trace"]
    if it.get("vorhanden"):
        print(f"  InSim-Trace: {it.get('kontakte')} Kontakte, "
              f"chat_check={it.get('chat_check')}, "
              f"vollstaendig={it.get('trace_vollstaendig')}")
    for w in ergebnis["warnungen"]:
        print(f"  WARNUNG: {w}")


def main() -> int:
    p = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    p.add_argument("lauf", help="Laufverzeichnis mit regler_trace.jsonl")
    p.add_argument("--json", action="store_true", help="nur das JSON ausgeben")
    p.add_argument("--speichern", action="store_true",
                   help="ergebnis.json in das Laufverzeichnis schreiben")
    p.add_argument("--als-basiswert", metavar="NAME", default="",
                   help="Lauf als Basiswert ablegen: ohne_abs oder referenz")
    a = p.parse_args()

    ergebnis = werte_lauf_aus(a.lauf)
    if a.json:
        print(json.dumps(ergebnis, indent=2, ensure_ascii=False))
    else:
        drucke(ergebnis)
    if a.speichern:
        with open(os.path.join(a.lauf, "ergebnis.json"), "w", encoding="utf-8") as f:
            json.dump(ergebnis, f, indent=2, ensure_ascii=False)
    if a.als_basiswert:
        if a.als_basiswert not in ("ohne_abs", "referenz"):
            print("--als-basiswert erwartet 'ohne_abs' oder 'referenz'.")
            return 2
        os.makedirs(BASISWERTE_DIR, exist_ok=True)
        ziel = os.path.join(BASISWERTE_DIR, f"{a.als_basiswert}.json")
        with open(ziel, "w", encoding="utf-8") as f:
            json.dump({"lauf": ergebnis["lauf"], "mittel": ergebnis["mittel"],
                       "bremsungen": ergebnis["bremsungen"]}, f, indent=2,
                      ensure_ascii=False)
        print(f"Basiswert gespeichert: {ziel}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
