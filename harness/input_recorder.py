"""
input_recorder.py — Aufzeichnen und Abspielen von Tastatur- und Mauseingaben.

Zweck im Benchmark: die drei Fahrszenarien werden einmal von Hand gefahren und
dabei aufgezeichnet. Der Orchestrator spielt sie danach identisch wieder ab, so
dass jede Kandidatenregelung exakt dieselbe Fahrereingabe bekommt.

Aufnahmeformat (JSON, Zeitstempel in Millisekunden ab Aufnahmebeginn)
---------------------------------------------------------------------
{
  "version": 1,
  "aufgenommen_am": "2026-08-20 14:03:11",
  "bildschirm": {"breite": 1920, "hoehe": 1080},
  "dauer_ms": 18240,
  "ereignisse": [
    {"t_ms":     0, "typ": "maus_bewegung", "x": 960, "y": 540},
    {"t_ms":   120, "typ": "maus_ab",       "knopf": "left", "x": 960, "y": 540},
    {"t_ms":   180, "typ": "maus_auf",      "knopf": "left", "x": 960, "y": 540},
    {"t_ms":   900, "typ": "maus_rad",      "dx": 0, "dy": -1, "x": 960, "y": 540},
    {"t_ms":  4000, "typ": "marke",         "name": "fahrt_start"},
    {"t_ms":  4050, "typ": "taste_ab",      "taste": "w",   "sonder": false},
    {"t_ms":  9000, "typ": "taste_auf",     "taste": "w",   "sonder": false},
    {"t_ms":  9000, "typ": "taste_ab",      "taste": "s",   "sonder": false},
    {"t_ms": 12000, "typ": "taste_auf",     "taste": "s",   "sonder": false}
  ]
}

Sondertasten stehen mit ``"sonder": true`` und dem pynput-Namen darin
(``esc``, ``space``, ``f1`` ...).

Bedienung
---------
    python harness/input_recorder.py aufnehmen harness/traces/szenario_1.json
        F7  setzt eine Marke (z. B. "fahrt_start"), F8 beendet die Aufnahme.

    python harness/input_recorder.py abspielen harness/traces/szenario_1.json
        F10 bricht sofort ab und laesst alle Tasten los.

    python harness/input_recorder.py zeigen harness/traces/szenario_1.json
        Zusammenfassung: Dauer, Marken, Ereigniszahl.

Wichtig fuer den Benchmark
--------------------------
Waehrend der Fahrt gehoert die Maus dem Regelkreis (sie ist die Gas-, Brems- und
Lenkachse). Ein Trace darf ab dem Fahrtbeginn deshalb **keine Mausereignisse**
mehr abspielen, sonst kaempfen Replay und Regelkreis um denselben Zeiger. Dafuer
beim Aufnehmen eine Marke setzen (F7) und beim Abspielen
``--maus-bis-marke fahrt_start`` verwenden.
"""

from __future__ import annotations

import argparse
import ctypes
import json
import os
import sys
import time
from typing import Any, Callable, Dict, List, Optional

try:
    from pynput import keyboard, mouse
except ImportError:                                     # pragma: no cover
    print("pynput fehlt:  pip install pynput")
    raise


# ─── Einstellungen ────────────────────────────────────────────────────────────

TASTE_MARKE = keyboard.Key.f7        # setzt waehrend der Aufnahme eine Marke
TASTE_STOP = keyboard.Key.f8         # beendet die Aufnahme
TASTE_ABBRUCH = keyboard.Key.f10     # bricht das Abspielen ab

# Mausbewegungen werden ausgeduennt, sonst wird die Datei riesig und das Abspielen
# ungenau. Beide Bedingungen muessen erfuellt sein, damit ein Punkt gespeichert wird.
MIN_ABSTAND_MS = 10
MIN_WEG_PX = 2

# Marken bekommen der Reihe nach diese Namen; danach marke_4, marke_5, ...
# Der Orchestrator bezieht seine Messfenster wahlweise auf eine dieser Marken.
STANDARD_MARKENNAMEN = ["fahrt_start", "bremsbeginn", "fahrt_ende"]

FORMATVERSION = 1


# ─── Hilfsfunktionen ──────────────────────────────────────────────────────────

def _bildschirmgroesse() -> Dict[str, int]:
    if sys.platform == "win32":
        u = ctypes.windll.user32
        return {"breite": u.GetSystemMetrics(0), "hoehe": u.GetSystemMetrics(1)}
    return {"breite": 0, "hoehe": 0}


def _feine_zeitgeber(ein: bool) -> None:
    """Windows-Timerraster von ~15,6 ms auf 1 ms setzen — sonst ruckelt das Replay."""
    if sys.platform != "win32":
        return
    try:
        if ein:
            ctypes.windll.winmm.timeBeginPeriod(1)
        else:
            ctypes.windll.winmm.timeEndPeriod(1)
    except Exception:
        pass


def _taste_zu_json(taste) -> Optional[Dict[str, Any]]:
    try:
        if taste.char is not None:
            return {"taste": taste.char, "sonder": False}
    except AttributeError:
        pass
    name = getattr(taste, "name", None)
    if name:
        return {"taste": name, "sonder": True}
    return None


def _json_zu_taste(eintrag: Dict[str, Any]):
    if eintrag.get("sonder"):
        return getattr(keyboard.Key, eintrag["taste"], None)
    return keyboard.KeyCode.from_char(eintrag["taste"])


_KNOPFNAMEN = {"left": mouse.Button.left, "right": mouse.Button.right,
               "middle": mouse.Button.middle}


def lade_trace(pfad: str) -> Dict[str, Any]:
    with open(pfad, "r", encoding="utf-8") as f:
        trace = json.load(f)
    if trace.get("version") != FORMATVERSION:
        print(f"[recorder] Hinweis: Trace hat Version {trace.get('version')}, "
              f"erwartet wird {FORMATVERSION}.")
    return trace


def marken(trace: Dict[str, Any]) -> Dict[str, int]:
    """Liefert {Markenname: t_ms}."""
    return {e["name"]: e["t_ms"] for e in trace["ereignisse"] if e["typ"] == "marke"}


# ─── Aufnahme ─────────────────────────────────────────────────────────────────

class Aufnahme:
    """Zeichnet Tastatur und Maus global auf (auch waehrend LFS im Vordergrund ist)."""

    def __init__(self) -> None:
        self.ereignisse: List[Dict[str, Any]] = []
        self._t0 = 0.0
        self._laeuft = False
        self._letzte_bewegung_ms = -10_000
        self._letzte_position = (0, 0)
        self._markenzaehler = 0

    def _t_ms(self) -> int:
        return int(round((time.monotonic() - self._t0) * 1000.0))

    def _anhaengen(self, ereignis: Dict[str, Any]) -> None:
        if self._laeuft:
            self.ereignisse.append(ereignis)

    # -- Rueckrufe --------------------------------------------------------

    def _taste_ab(self, taste) -> Optional[bool]:
        if taste == TASTE_STOP:
            self._laeuft = False
            return False                     # beendet den Tastatur-Listener
        if taste == TASTE_MARKE:
            name = (STANDARD_MARKENNAMEN[self._markenzaehler]
                    if self._markenzaehler < len(STANDARD_MARKENNAMEN)
                    else f"marke_{self._markenzaehler + 1}")
            self._markenzaehler += 1
            self._anhaengen({"t_ms": self._t_ms(), "typ": "marke", "name": name})
            print(f"  Marke gesetzt: {name} bei {self._t_ms()} ms")
            return None
        eintrag = _taste_zu_json(taste)
        if eintrag:
            self._anhaengen({"t_ms": self._t_ms(), "typ": "taste_ab", **eintrag})
        return None

    def _taste_auf(self, taste) -> None:
        if taste in (TASTE_STOP, TASTE_MARKE):
            return
        eintrag = _taste_zu_json(taste)
        if eintrag:
            self._anhaengen({"t_ms": self._t_ms(), "typ": "taste_auf", **eintrag})

    def _maus_bewegung(self, x: int, y: int) -> None:
        t = self._t_ms()
        weg = abs(x - self._letzte_position[0]) + abs(y - self._letzte_position[1])
        if t - self._letzte_bewegung_ms < MIN_ABSTAND_MS or weg < MIN_WEG_PX:
            return
        self._letzte_bewegung_ms = t
        self._letzte_position = (x, y)
        self._anhaengen({"t_ms": t, "typ": "maus_bewegung", "x": int(x), "y": int(y)})

    def _maus_klick(self, x: int, y: int, knopf, gedrueckt: bool) -> None:
        self._anhaengen({"t_ms": self._t_ms(),
                         "typ": "maus_ab" if gedrueckt else "maus_auf",
                         "knopf": getattr(knopf, "name", "left"),
                         "x": int(x), "y": int(y)})

    def _maus_rad(self, x: int, y: int, dx: int, dy: int) -> None:
        self._anhaengen({"t_ms": self._t_ms(), "typ": "maus_rad",
                         "dx": int(dx), "dy": int(dy), "x": int(x), "y": int(y)})

    # -- Ablauf -----------------------------------------------------------

    def starte(self, vorlauf_s: float = 3.0) -> Dict[str, Any]:
        print(f"Aufnahme startet in {vorlauf_s:.0f} s. "
              f"F7 = Marke setzen, F8 = Aufnahme beenden.")
        time.sleep(vorlauf_s)
        self._t0 = time.monotonic()
        self._laeuft = True
        print("Aufnahme laeuft ...")

        maus_listener = mouse.Listener(on_move=self._maus_bewegung,
                                       on_click=self._maus_klick,
                                       on_scroll=self._maus_rad)
        maus_listener.start()
        try:
            with keyboard.Listener(on_press=self._taste_ab,
                                   on_release=self._taste_auf) as tast_listener:
                tast_listener.join()
        finally:
            maus_listener.stop()

        dauer = self.ereignisse[-1]["t_ms"] if self.ereignisse else 0
        return {"version": FORMATVERSION,
                "aufgenommen_am": time.strftime("%Y-%m-%d %H:%M:%S"),
                "bildschirm": _bildschirmgroesse(),
                "dauer_ms": dauer,
                "ereignisse": self.ereignisse}


def nimm_auf(pfad: str, vorlauf_s: float = 3.0) -> Dict[str, Any]:
    trace = Aufnahme().starte(vorlauf_s)
    os.makedirs(os.path.dirname(os.path.abspath(pfad)), exist_ok=True)
    with open(pfad, "w", encoding="utf-8") as f:
        json.dump(trace, f, indent=1)
    print(f"Aufnahme gespeichert: {pfad}  "
          f"({len(trace['ereignisse'])} Ereignisse, {trace['dauer_ms']} ms)")
    for name, t in marken(trace).items():
        print(f"  Marke '{name}' bei {t} ms")
    return trace


# ─── Abspielen ────────────────────────────────────────────────────────────────

class Abspieler:
    """Spielt einen Trace zeitgetreu ab und laesst am Ende garantiert alles los."""

    def __init__(self) -> None:
        self.tastatur = keyboard.Controller()
        self.maus = mouse.Controller()
        self._gedrueckte_tasten: List[Any] = []
        self._gedrueckte_knoepfe: List[Any] = []
        self._abbruch = False

    def _alles_loslassen(self) -> None:
        """Muss auf jedem Ausstiegspfad laufen — eine haengende Taste 'w' faehrt weiter."""
        for taste in list(self._gedrueckte_tasten):
            try:
                self.tastatur.release(taste)
            except Exception:
                pass
        for knopf in list(self._gedrueckte_knoepfe):
            try:
                self.maus.release(knopf)
            except Exception:
                pass
        self._gedrueckte_tasten.clear()
        self._gedrueckte_knoepfe.clear()

    def spiele_ab(self, trace: Dict[str, Any], tempo: float = 1.0,
                  ohne_maus: bool = False, maus_bis_marke: Optional[str] = None,
                  bei_marke: Optional[Callable[[str, float], None]] = None,
                  t0: Optional[float] = None) -> float:
        """Spielt ``trace`` ab und liefert den Startzeitpunkt (time.monotonic()).

        ohne_maus        : Mausereignisse komplett ueberspringen
        maus_bis_marke   : ab dieser Marke keine Mausereignisse mehr abspielen
                           (der Regelkreis braucht die Maus dann als Achse)
        bei_marke        : Rueckruf(Markenname, monotone Zeit) — fuer Messfenster
        t0               : erzwungener Startzeitpunkt (fuer exakte Synchronisation)
        """
        ereignisse = trace["ereignisse"]
        maus_sperre_ms = None
        if maus_bis_marke:
            maus_sperre_ms = marken(trace).get(maus_bis_marke)
            if maus_sperre_ms is None:
                print(f"[recorder] WARNUNG: Marke '{maus_bis_marke}' nicht im Trace — "
                      f"Mausereignisse werden vollstaendig abgespielt.")

        bildschirm = trace.get("bildschirm") or {}
        jetzt_bildschirm = _bildschirmgroesse()
        if (bildschirm.get("breite") and jetzt_bildschirm.get("breite")
                and bildschirm["breite"] != jetzt_bildschirm["breite"]):
            print(f"[recorder] WARNUNG: Aufnahme bei {bildschirm['breite']}x{bildschirm['hoehe']}, "
                  f"jetzt {jetzt_bildschirm['breite']}x{jetzt_bildschirm['hoehe']}. "
                  f"Mausklicks treffen dann nicht.")

        self._abbruch = False
        wache = keyboard.Listener(on_press=self._pruefe_abbruch)
        wache.start()
        _feine_zeitgeber(True)
        start = t0 if t0 is not None else time.monotonic()
        try:
            for ereignis in ereignisse:
                if self._abbruch:
                    print("[recorder] Abbruch durch F10.")
                    break
                ziel = start + (ereignis["t_ms"] / 1000.0) / max(0.01, tempo)
                rest = ziel - time.monotonic()
                if rest > 0:
                    time.sleep(rest)
                self._fuehre_aus(ereignis, ohne_maus, maus_sperre_ms, bei_marke)
        finally:
            self._alles_loslassen()
            _feine_zeitgeber(False)
            wache.stop()
        return start

    def _pruefe_abbruch(self, taste) -> None:
        if taste == TASTE_ABBRUCH:
            self._abbruch = True

    def _fuehre_aus(self, ereignis: Dict[str, Any], ohne_maus: bool,
                    maus_sperre_ms: Optional[int],
                    bei_marke: Optional[Callable[[str, float], None]]) -> None:
        typ = ereignis["typ"]

        if typ == "marke":
            if bei_marke:
                bei_marke(ereignis["name"], time.monotonic())
            return

        if typ.startswith("maus"):
            if ohne_maus:
                return
            if maus_sperre_ms is not None and ereignis["t_ms"] >= maus_sperre_ms:
                return

        try:
            if typ == "taste_ab":
                taste = _json_zu_taste(ereignis)
                if taste is not None:
                    self.tastatur.press(taste)
                    if taste not in self._gedrueckte_tasten:
                        self._gedrueckte_tasten.append(taste)
            elif typ == "taste_auf":
                taste = _json_zu_taste(ereignis)
                if taste is not None:
                    self.tastatur.release(taste)
                    if taste in self._gedrueckte_tasten:
                        self._gedrueckte_tasten.remove(taste)
            elif typ == "maus_bewegung":
                self.maus.position = (ereignis["x"], ereignis["y"])
            elif typ == "maus_ab":
                self.maus.position = (ereignis["x"], ereignis["y"])
                knopf = _KNOPFNAMEN.get(ereignis.get("knopf", "left"), mouse.Button.left)
                self.maus.press(knopf)
                if knopf not in self._gedrueckte_knoepfe:
                    self._gedrueckte_knoepfe.append(knopf)
            elif typ == "maus_auf":
                self.maus.position = (ereignis["x"], ereignis["y"])
                knopf = _KNOPFNAMEN.get(ereignis.get("knopf", "left"), mouse.Button.left)
                self.maus.release(knopf)
                if knopf in self._gedrueckte_knoepfe:
                    self._gedrueckte_knoepfe.remove(knopf)
            elif typ == "maus_rad":
                self.maus.position = (ereignis["x"], ereignis["y"])
                self.maus.scroll(ereignis.get("dx", 0), ereignis.get("dy", 0))
        except Exception as e:
            # Ein einzelnes fehlgeschlagenes Ereignis darf den Lauf nicht kippen.
            print(f"[recorder] Ereignis {ereignis} nicht ausfuehrbar: {e}")


def spiele_ab(pfad: str, **kwargs) -> float:
    return Abspieler().spiele_ab(lade_trace(pfad), **kwargs)


# ─── Uebersicht ───────────────────────────────────────────────────────────────

def zeige(pfad: str) -> None:
    trace = lade_trace(pfad)
    zaehler: Dict[str, int] = {}
    for e in trace["ereignisse"]:
        zaehler[e["typ"]] = zaehler.get(e["typ"], 0) + 1
    print(f"Datei      : {pfad}")
    print(f"Aufgenommen: {trace.get('aufgenommen_am', '?')}")
    print(f"Bildschirm : {trace.get('bildschirm')}")
    print(f"Dauer      : {trace.get('dauer_ms', 0)} ms")
    print(f"Ereignisse : {sum(zaehler.values())}  {zaehler}")
    m = marken(trace)
    print(f"Marken     : {m if m else 'keine'}")
    tasten = sorted({e["taste"] for e in trace["ereignisse"] if e["typ"] == "taste_ab"})
    print(f"Tasten     : {tasten}")


# ─── Kommandozeile ────────────────────────────────────────────────────────────

def main() -> int:
    p = argparse.ArgumentParser(description="Aufzeichnen und Abspielen von Eingaben")
    unter = p.add_subparsers(dest="befehl", required=True)

    a = unter.add_parser("aufnehmen", help="Eingaben aufzeichnen (F7 Marke, F8 Ende)")
    a.add_argument("datei")
    a.add_argument("--vorlauf", type=float, default=3.0, help="Sekunden bis Aufnahmebeginn")

    b = unter.add_parser("abspielen", help="Trace abspielen (F10 bricht ab)")
    b.add_argument("datei")
    b.add_argument("--tempo", type=float, default=1.0)
    b.add_argument("--ohne-maus", action="store_true")
    b.add_argument("--maus-bis-marke", default=None)
    b.add_argument("--vorlauf", type=float, default=3.0)

    z = unter.add_parser("zeigen", help="Trace zusammenfassen")
    z.add_argument("datei")

    args = p.parse_args()
    if args.befehl == "aufnehmen":
        nimm_auf(args.datei, args.vorlauf)
    elif args.befehl == "abspielen":
        print(f"Abspielen startet in {args.vorlauf:.0f} s (F10 bricht ab) ...")
        time.sleep(args.vorlauf)
        spiele_ab(args.datei, tempo=args.tempo, ohne_maus=args.ohne_maus,
                  maus_bis_marke=args.maus_bis_marke)
        print("Fertig.")
    else:
        zeige(args.datei)
    return 0


if __name__ == "__main__":
    sys.exit(main())
