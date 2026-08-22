"""
selbsttest.py — prueft den Pruefstand selbst, ohne LFS und ohne Windows.

Sinn: ein Benchmark, dessen Messung oder Bewertung still kaputtgeht, ist
schlimmer als keiner. Dieser Test deckt alles ab, was ohne laufende Simulation
pruefbar ist:

  * Byte-Aufbau der OutGauge- und OutSim-Pakete
  * Umrechnung in den Fahrzeugzustand (Koordinaten, Vorderachse, Schlupf)
  * Rollradius-Schaetzer
  * Rampen der Fahrereingabe und Validierung der Reglerausgabe
  * kompletter Regelkreis gegen einen simulierten LFS-Datenstrom (UDP)
  * Kennwerte, Normierung und Punktevergabe der Auswertung
  * AST-Vergleich der Codepruefung
  * Abspiellogik des Recorders inklusive Maus-Sperre

Der Test veraendert keine Dateien des Projekts.

    python harness/selbsttest.py
"""

from __future__ import annotations

import math
import os
import socket
import struct
import sys
import threading
import time

HARNESS_VERZEICHNIS = os.path.dirname(os.path.abspath(__file__))
PROJEKTWURZEL = os.path.dirname(HARNESS_VERZEICHNIS)
for _p in (PROJEKTWURZEL, HARNESS_VERZEICHNIS):
    if _p not in sys.path:
        sys.path.insert(0, _p)

import lfs_link as L                                     # noqa: E402

FEHLER: list = []


def pruefe(bedingung: bool, text: str) -> None:
    print(("  ok   " if bedingung else "  FEHL ") + text)
    if not bedingung:
        FEHLER.append(text)


# ─── Hilfsmittel: Pakete bauen ────────────────────────────────────────────────

def baue_outgauge(auto=b'XRG', gang=3, v=27.5, gas=0.25, bremse=0.8, t_ms=0):
    return struct.Struct('<I3sxH2B7f2I3f15sx15sx').pack(
        t_ms, auto, 0, gang, 7, v, 3000.0, 0.0, 90.0, 30.0, 3.0, 95.0,
        0, 0, gas, bremse, 0.0, b'', b'')


def baue_outsim(vx=0.0, vy=30.0, kurs=0.0, omega=(96.0, 96.0, 80.0, 80.0),
                gierrate=0.0, ax=0.0, ay=0.0, bremse=0.9, t_ms=0):
    d = struct.pack('<4s', b'LFST') + struct.pack('<i', 0) + struct.pack('<I', t_ms)
    d += struct.Struct('<3f3f3f3f3i').pack(
        0.0, 0.0, gierrate, kurs, 0.02, -0.01, ax, ay, -9.81, vx, vy, 0.0,
        int(1.5 * 65536), int(2.5 * 65536), int(0.3 * 65536))
    d += struct.Struct('<5f').pack(0.1, bremse, -0.25, 0.0, 0.0)
    d += struct.Struct('<4B2f').pack(3, 0, 0, 0, 550.0, 30.0)
    d += struct.Struct('<2f').pack(120.0, 3.0)
    for w in omega:
        d += struct.Struct('<7f4B2f').pack(0.03, 0.05, 500.0, 100.0, 3500.0, w, 0.0,
                                           25, 10, 1, 0, 0.05, 0.02)
    return d + struct.Struct('<2f').pack(2.0, 0.0)


class _DummyTelemetrie:
    def __init__(self, alter_s=0.0):
        self.og = None
        self.osp = None
        self.alter = alter_s

    def lies(self):
        t = time.monotonic() - self.alter
        return self.og, t, self.osp, t


class _DummyInSim:
    zustand = L.LfsZustand(verbunden=True, flags=L.ISS_GAME, sta_empfangen=True)


# ─── 1. Telemetriepakete ──────────────────────────────────────────────────────

def test_pakete() -> None:
    print("Telemetriepakete:")
    roh = baue_outgauge()
    pruefe(len(roh) == 92, f"OutGauge 92 Byte (ist {len(roh)})")
    og = L.OutGaugePaket.entpacke(roh)
    pruefe(og.auto == 'XRG' and og.gang == 3, "OutGauge: Auto und Gang")
    pruefe(abs(og.geschwindigkeit_mps - 27.5) < 1e-6, "OutGauge: Geschwindigkeit")
    pruefe(abs(og.bremse - 0.8) < 1e-6 and abs(og.gas - 0.25) < 1e-6, "OutGauge: Pedale")
    pruefe(L.OutGaugePaket.entpacke(b'x' * 40) is None, "OutGauge: falsche Laenge abgelehnt")

    roh = baue_outsim()
    pruefe(len(roh) == 280, f"OutSim 280 Byte (ist {len(roh)})")
    osp = L.OutSimPaket.entpacke(roh)
    pruefe(osp.kopf == b'LFST', "OutSim: Kopfkennung")
    pruefe(len(osp.rad_winkelgeschwindigkeit_rad_s) == 4, "OutSim: 4 Raeder")
    pruefe(osp.rad_winkelgeschwindigkeit_rad_s[2] == 80.0, "OutSim: Vorderachse ab Index 2")
    pruefe(abs(osp.position[0] - 1.5) < 1e-3, "OutSim: Position in Metern")
    pruefe(osp.gang == 3 and abs(osp.eingaben[1] - 0.9) < 1e-6, "OutSim: Antrieb und Eingaben")
    pruefe(L.OutSimPaket.entpacke(b'x' * 100) is None, "OutSim: zu kurzes Paket abgelehnt")


# ─── 2. Fahrzeugzustand ───────────────────────────────────────────────────────

def _regelkreis(telemetrie) -> L.Regelkreis:
    rk = L.Regelkreis(_DummyInSim(), telemetrie, L.MausAchsen(), L.Tastatureingabe())
    rk._t0 = rk._t_letzt = time.monotonic()
    return rk


def test_fahrzeugzustand() -> None:
    print("Fahrzeugzustand:")
    tel = _DummyTelemetrie()
    tel.og = L.OutGaugePaket.entpacke(baue_outgauge())
    tel.osp = L.OutSimPaket.entpacke(baue_outsim(gierrate=0.4))
    rk = _regelkreis(tel)
    rk.radius.setze_fahrzeug('XRG')
    rk.radius.radius_m, rk.radius.eingemessen = 0.31, True

    z = rk._baue_zustand(time.monotonic(), 0.05)
    pruefe(z.daten_gueltig and z.vorderachse_gueltig, "Daten und Vorderachse gueltig")
    pruefe(abs(z.v_ueber_grund_mps - 30.0) < 1e-3, "Geschwindigkeit ueber Grund")
    pruefe(abs(z.v_laengs_mps - 30.0) < 1e-3 and abs(z.v_quer_mps) < 1e-3,
           "Kurs 0: laengs/quer korrekt zerlegt")
    pruefe(abs(z.v_vorderachse_mps - 80.0 * 0.31) < 1e-3, "Vorderachse = omega * r")
    pruefe(abs(z.gierrate_rad_s - 0.4) < 1e-6, "Gierrate aus AngVel[2]")
    schlupf = (z.v_ueber_grund_mps - z.v_vorderachse_mps) / z.v_ueber_grund_mps
    pruefe(abs(schlupf - 0.1733) < 1e-3, f"daraus berechneter Schlupf {schlupf:.4f}")

    # Kurs 90 Grad (Fahrt nach Osten): Weltgeschwindigkeit liegt jetzt auf X.
    tel.osp = L.OutSimPaket.entpacke(baue_outsim(vx=30.0, vy=0.0, kurs=math.pi / 2))
    z2 = rk._baue_zustand(time.monotonic(), 0.05)
    pruefe(abs(z2.v_laengs_mps - 30.0) < 1e-3 and abs(z2.v_quer_mps) < 1e-3,
           "Kurs 90 Grad: laengs/quer korrekt zerlegt")

    # Seitliches Rutschen: Kurs 0, Fahrzeug bewegt sich schraeg.
    tel.osp = L.OutSimPaket.entpacke(baue_outsim(vx=10.0, vy=30.0, kurs=0.0))
    z3 = rk._baue_zustand(time.monotonic(), 0.05)
    pruefe(abs(math.degrees(z3.schwimmwinkel_rad) + 18.43) < 0.1,
           f"Schwimmwinkel {math.degrees(z3.schwimmwinkel_rad):.2f} Grad")

    alt = _DummyTelemetrie(alter_s=5.0)
    alt.og, alt.osp = tel.og, tel.osp
    z4 = _regelkreis(alt)._baue_zustand(time.monotonic(), 0.05)
    pruefe(not z4.daten_gueltig and z4.v_ueber_grund_mps == 0.0,
           "veraltete Telemetrie -> ungueltig, Werte auf 0")

    leer = _DummyTelemetrie()
    z5 = _regelkreis(leer)._baue_zustand(time.monotonic(), 0.05)
    pruefe(not z5.daten_gueltig, "fehlende Telemetrie -> ungueltig")


def test_rollradius() -> None:
    print("Rollradiusschaetzer:")
    r = L.Rollradiusschaetzer()
    r.setze_fahrzeug('TESTAUTO_SELBSTTEST')
    pruefe(not r.eingemessen, "unbekanntes Fahrzeug -> nicht eingemessen")
    r.aktualisiere(30.0, 100.0, 0.5, 0.0, 0.0)
    r.aktualisiere(30.0, 100.0, 0.0, 0.9, 0.0)
    r.aktualisiere(3.0, 10.0, 0.0, 0.0, 0.0)
    r.aktualisiere(30.0, 100.0, 0.0, 0.0, 5.0)
    pruefe(not r.eingemessen, "unter Gas/Bremse/langsam/Querbeschleunigung wird nicht gelernt")
    r.aktualisiere(30.0, 100.0, 0.0, 0.0, 0.0)
    pruefe(r.eingemessen and abs(r.radius_m - 0.30) < 1e-6, f"gelernt: r = {r.radius_m:.4f} m")
    for _ in range(200):
        r.aktualisiere(31.0, 100.0, 0.0, 0.0, 0.0)
    pruefe(abs(r.radius_m - 0.31) < 1e-3, "folgt langsam nach")
    r.aktualisiere(30.0, 5.0, 0.0, 0.0, 0.0)
    pruefe(abs(r.radius_m - 0.31) < 1e-3, "unplausibler Wert wird verworfen")


# ─── 2b. IS_MCI und Quellen-Diagnose ──────────────────────────────────────────

def test_mci() -> None:
    print("IS_MCI:")
    compcar = struct.Struct('<2H4B3i3Hh')

    def auto(plid, speed, richtung=0, kurs=0, gierrate=0, x=1.5):
        return compcar.pack(0, 1, plid, 1, 0, 0,
                            int(x * 65536), int(2.5 * 65536), int(0.3 * 65536),
                            speed, richtung, kurs, gierrate)

    nutzlast = auto(3, 9830, kurs=16384, gierrate=455) + auto(7, 4915, x=99.0)
    paket = struct.pack('<4B', (4 + len(nutzlast)) // 4, L.ISP_MCI, 0, 2) + nutzlast

    v = L.InSimVerbindung()
    v._verarbeite(paket)
    pruefe(v.mci is not None and v.mci["plid"] == 3, "ohne Filter wird das erste Fahrzeug genommen")
    pruefe(abs(v.mci["v_mps"] - 30.0) < 0.01,
           f"Speed 32768 = 100 m/s -> {v.mci['v_mps']:.3f} m/s")
    pruefe(abs(math.degrees(v.mci["gierrate_rad_s"]) - 10.0) < 0.05,
           f"AngVel 16384 = 360 Grad/s -> {math.degrees(v.mci['gierrate_rad_s']):.2f} Grad/s")
    pruefe(abs(math.degrees(v.mci["kurs_rad"]) - 90.0) < 0.05, "Kurs 16384 = 90 Grad")
    pruefe(abs(v.mci["position_m"][0] - 1.5) < 1e-3, "Position 1/65536 m")

    v2 = L.InSimVerbindung()
    v2.mci_plid = 7
    v2._verarbeite(paket)
    pruefe(v2.mci["plid"] == 7 and abs(v2.mci["position_m"][0] - 99.0) < 1e-3,
           "Filter auf die eigene PLID greift")

    v3 = L.InSimVerbindung()
    v3._verarbeite(struct.pack('<4B', 8, L.ISP_MCI, 0, 5) + nutzlast)   # NumC luegt
    pruefe(v3.mci is not None, "zu kurzes MCI-Paket bricht nicht ab")


def test_quellenvergleich() -> None:
    import orchestrator  # noqa: F401  (nur um sicherzugehen, dass der Import klappt)
    print("Quellen-Diagnose:")

    def lauf(og_folgt: str, radius=0.31):
        """Erzeugt einen Rollvorgang mit anschliessender Bremsung mit Blockierern."""
        zeilen, v = [], 30.0
        for i in range(400):
            bremst = i > 200
            if bremst:
                v = max(0.5, v - 0.15)
            # Beim Bremsen blockieren die Vorderraeder zu 60 %, hinten zu 20 %.
            omega_v = v * (1.0 - (0.6 if bremst else 0.0)) / radius
            omega_h = v * (1.0 - (0.2 if bremst else 0.0)) / radius
            quellen = {"grund": v, "vorne": omega_v * radius, "hinten": omega_h * radius}
            zeilen.append({"t": i * 0.05, "bremse": 1.0 if bremst else 0.0,
                           "og_speed": quellen[og_folgt], "v_outsim": v, "v_mci": v,
                           "omega_v": omega_v, "omega_h": omega_h})
        return zeilen

    e = L._vergleiche_quellen(lauf("vorne"))
    pruefe(e.get("treffer") == "OutSim Raeder vorne (omega)",
           f"Vorderachs-Quelle erkannt ({e.get('treffer')})")
    pruefe(abs(e["kandidaten"]["OutSim Raeder vorne (omega)"]["faktor"] - 0.31) < 0.01,
           "Faktor entspricht dem Rollradius")
    pruefe(e["kandidaten"]["OutSim Raeder vorne (omega)"]["rest_bremsen"] < 1.0,
           "passende Quelle: kleiner Restfehler beim Bremsen")
    pruefe(e["kandidaten"]["OutSim |v| (ueber Grund)"]["rest_bremsen"] > 10.0,
           "unpassende Quelle: grosser Restfehler beim Bremsen")

    e = L._vergleiche_quellen(lauf("grund"))
    pruefe(e.get("treffer") in ("OutSim |v| (ueber Grund)", "MCI.Speed (ueber Grund)"),
           f"Grund-Quelle erkannt ({e.get('treffer')})")
    pruefe(abs(e["kandidaten"]["OutSim |v| (ueber Grund)"]["faktor"] - 1.0) < 0.01,
           "Faktor ~ 1 fuer eine Geschwindigkeitsquelle")

    e = L._vergleiche_quellen(lauf("hinten"))
    pruefe(e.get("treffer") == "OutSim Raeder hinten (omega)",
           f"Hinterachs-Quelle erkannt ({e.get('treffer')})")

    ohne_bremsung = [z for z in lauf("vorne") if z["bremse"] == 0.0]
    e = L._vergleiche_quellen(ohne_bremsung)
    pruefe(e["bremsproben"] == 0 and "treffer" not in e,
           "ohne Bremsphase wird bewusst kein Treffer gemeldet")
    pruefe(L._vergleiche_quellen([])["proben"] == 0, "leere Eingabe bricht nicht ab")


# ─── 3. Fahrereingabe und Ausgabepruefung ─────────────────────────────────────

def test_fahrereingabe() -> None:
    print("Fahrereingabe:")
    e = L.Tastatureingabe()
    e._gedrueckt[L.KONFIG.taste_bremse] = True
    for _ in range(6):
        e.aktualisiere(0.05)
    pruefe(abs(e.bremse - 100.0) < 1e-6, "Bremse nach 300 ms voll")
    e._gedrueckt[L.KONFIG.taste_bremse] = False
    for _ in range(6):
        e.aktualisiere(0.05)
    pruefe(abs(e.bremse) < 1e-6, "Bremse nach 300 ms wieder bei 0")

    e2 = L.Tastatureingabe()
    e2._gedrueckt[L.KONFIG.taste_rechts] = True
    for _ in range(20):
        e2.aktualisiere(0.05)
    pruefe(abs(e2.lenkung - 100.0) < 1e-6, "Lenkung nach 1000 ms am Anschlag")
    e2._gedrueckt[L.KONFIG.taste_links] = True
    fahrer = e2.aktualisiere(0.05)
    pruefe(fahrer.lenkung_prozent < 100.0, "beide Lenktasten -> zurueck zur Mitte")

    e3 = L.Tastatureingabe()
    e3._gedrueckt[L.KONFIG.taste_gas] = True
    f1 = e3.aktualisiere(0.15)
    f2 = e3.aktualisiere(0.15)
    pruefe(abs(f1.gas_prozent - 50.0) < 1e-6 and abs(f2.gas_prozent - 100.0) < 1e-6,
           "Rampe rechnet mit dem tatsaechlichen dt")


def test_ausgabepruefung() -> None:
    print("Ausgabepruefung:")
    rk = _regelkreis(_DummyTelemetrie())
    pruefe(rk._pruefe_ausgabe(L.Pedalstellung(150.0, -20.0)) == (100.0, 0.0),
           "Begrenzung auf 0..100")
    for wert, art in ((float('nan'), "NaN"), (float('inf'), "unendlich")):
        try:
            rk._pruefe_ausgabe(L.Pedalstellung(wert, 0.0))
            pruefe(False, f"{art} wird abgelehnt")
        except ValueError:
            pruefe(True, f"{art} wird abgelehnt")
    for falsch in ((50.0, 50.0), None, "50"):
        try:
            rk._pruefe_ausgabe(falsch)
            pruefe(False, f"{type(falsch).__name__} wird abgelehnt")
        except TypeError:
            pruefe(True, f"{type(falsch).__name__} wird abgelehnt")


# ─── 4. Regelkreis gegen simulierten Datenstrom ───────────────────────────────

def test_regelkreis() -> None:
    print("Regelkreis (simulierter LFS-Datenstrom, dauert ~5 s):")
    lauf = threading.Event()
    lauf.set()
    fahrzeug = {"v": 30.0, "bremse": 0.0}

    def sender():
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        t0 = time.monotonic()
        while lauf.is_set():
            t_ms = int((time.monotonic() - t0) * 1000)
            v = fahrzeug["v"]
            # Bremsschlupf 35 %, sobald gebremst wird.
            omega = v / 0.31 * (1.0 - 0.35 * fahrzeug["bremse"])
            s.sendto(baue_outgauge(v=v, gas=0.0, bremse=fahrzeug["bremse"], t_ms=t_ms),
                     ("127.0.0.1", L.KONFIG.outgauge_port))
            s.sendto(baue_outsim(vy=v, omega=(omega,) * 4, gierrate=0.05,
                                 ay=-9.0 * fahrzeug["bremse"], bremse=fahrzeug["bremse"],
                                 t_ms=t_ms), ("127.0.0.1", L.KONFIG.outsim_port))
            time.sleep(0.01)
        s.close()

    aufrufe = {"n": 0, "dt": [], "speicher": []}

    def testregler(fahrer, zustand, speicher):
        aufrufe["n"] += 1
        aufrufe["dt"].append(zustand.dt_s)
        speicher["zaehler"] = speicher.get("zaehler", 0) + 1
        aufrufe["speicher"].append(speicher["zaehler"])
        bremse = fahrer.bremse_prozent
        if zustand.vorderachse_gueltig and zustand.v_ueber_grund_mps > 4.0:
            schlupf = ((zustand.v_ueber_grund_mps - zustand.v_vorderachse_mps)
                       / zustand.v_ueber_grund_mps)
            if schlupf > 0.15:
                bremse *= 0.4
        return L.Pedalstellung(gas_prozent=fahrer.gas_prozent, bremse_prozent=bremse)

    telemetrie = L.TelemetrieEmpfaenger()
    telemetrie.starte()
    threading.Thread(target=sender, daemon=True).start()
    time.sleep(0.5)

    eingabe = L.Tastatureingabe()
    rk = L.Regelkreis(_DummyInSim(), telemetrie, L.MausAchsen(), eingabe)
    rk.reglerfunktion = testregler
    rk.starte()
    try:
        time.sleep(1.5)                                   # rollen: Rollradius lernen
        pruefe(rk.radius.eingemessen and abs(rk.radius.radius_m - 0.31) < 0.01,
               f"Rollradius im Rollen gelernt ({rk.radius.radius_m:.4f} m)")

        t_brems = time.monotonic()
        eingabe._gedrueckt[L.KONFIG.taste_bremse] = True
        fahrzeug["bremse"] = 1.0
        for _ in range(60):                               # 3 s bremsen
            time.sleep(0.05)
            fahrzeug["v"] = max(0.0, fahrzeug["v"] - 9.0 * 0.05)
        eingabe._gedrueckt[L.KONFIG.taste_bremse] = False
        time.sleep(0.3)
    finally:
        rk.stoppe()
        lauf.clear()
        telemetrie.stoppe()

    dts = aufrufe["dt"][5:]
    mittel = sum(dts) / len(dts)
    pruefe(aufrufe["n"] > 50, f"{aufrufe['n']} Reglerauftrufe")
    pruefe(abs(mittel - 0.05) < 0.008, f"mittlerer Takt {mittel * 1000:.1f} ms")
    pruefe(max(dts) < 0.15, f"groesster Takt {max(dts) * 1000:.1f} ms")
    pruefe(aufrufe["speicher"][-1] == aufrufe["n"], "Zustandsspeicher bleibt erhalten")
    pruefe(rk.reglerfehler == 0, "keine Reglerfehler")

    alle = rk.hole_abtastungen(0, time.monotonic())
    pruefe(len(alle) == aufrufe["n"], "jede Abtastung aufgezeichnet")
    pruefe(15 <= len(rk.hole_abtastungen(t_brems, t_brems + 1.0)) <= 25,
           "Messfenster von 1 s liefert ~20 Abtastungen")
    geregelt = [a for a in alle if a.fahrer_bremse > 50
                and a.zustand["v_ueber_grund_mps"] > 4.0]
    pruefe(geregelt and all(a.ausgabe_bremse < a.fahrer_bremse for a in geregelt),
           "Regler greift ein: Ausgabe kleiner als Fahrerwunsch")
    pruefe(alle[-1].zustand["v_ueber_grund_mps"] < 5.0, "Fahrzeug abgebremst")


def test_reglerfehler() -> None:
    print("Verhalten bei fehlerhafter Reglerfunktion:")
    tel = _DummyTelemetrie()
    tel.og = L.OutGaugePaket.entpacke(baue_outgauge())
    tel.osp = L.OutSimPaket.entpacke(baue_outsim())
    rk = _regelkreis(tel)
    rk._fehler_gemeldet = True          # unterdrueckt den Traceback auf der Konsole
    rk.eingabe._gedrueckt[L.KONFIG.taste_bremse] = True
    for _ in range(6):
        rk.eingabe.aktualisiere(0.05)

    rk.reglerfunktion = lambda f, z, s: (_ for _ in ()).throw(ValueError("Absicht"))
    rk._zyklus()
    pruefe(rk.reglerfehler == 1, "Ausnahme wird gezaehlt")
    pruefe(rk.abtastungen[-1].regler_fehler.startswith("ValueError"),
           "Fehler steht im Messschrieb")
    pruefe(abs(rk.abtastungen[-1].ausgabe_bremse - 100.0) < 1e-6,
           "Rueckfall auf den Fahrerwunsch (Bremse bleibt erhalten)")

    rk.reglerfunktion = lambda f, z, s: L.Pedalstellung(0.0, float('nan'))
    rk._zyklus()
    pruefe(rk.reglerfehler == 2, "NaN wird wie eine Ausnahme behandelt")


# ─── 5. Auswertung ────────────────────────────────────────────────────────────

def _messschrieb(v0=30.0, a=9.0, schlupf=0.12, gierrate=0.0, dauer_s=8.0,
                 takt=0.05, bremse_ab_s=1.0):
    proben, t, v, kurs = [], 0.0, v0, 0.0
    while t < dauer_s:
        bremst = t >= bremse_ab_s
        if bremst and v > 0.0:
            v = max(0.0, v - a * takt)
            kurs += gierrate * takt
        s = schlupf if (bremst and v > 2.0) else 0.0
        proben.append({
            "t_ms": t * 1000.0, "fahrer_gas": 0.0,
            "fahrer_bremse": 100.0 if bremst else 0.0, "fahrer_lenkung": 0.0,
            "ausgabe_gas": 0.0, "ausgabe_bremse": 100.0 if bremst else 0.0,
            "regler_laufzeit_ms": 0.2, "regler_fehler": None,
            "v_ueber_grund_mps": v, "v_vorderachse_mps": v * (1.0 - s),
            "v_laengs_mps": v, "v_quer_mps": 0.0, "schwimmwinkel_rad": 0.0,
            "gierrate_rad_s": gierrate if bremst else 0.0,
            "laengsbeschleunigung_mps2": -a if bremst else 0.0,
            "querbeschleunigung_mps2": 0.0, "kurswinkel_rad": kurs,
            "lenkung_ist_norm": 0.0, "bremse_ist_norm": 1.0 if bremst else 0.0,
            "gas_ist_norm": 0.0, "gang": 3, "position_m": [0.0, 0.0, 0.0],
            "daten_gueltig": True, "vorderachse_gueltig": True})
        t += takt
    return {"szenario": "selbsttest", "abtastungen": proben}


def test_auswertung() -> None:
    import orchestrator as O
    print("Kennwerte:")
    m = O.berechne_metriken(_messschrieb(a=9.0, schlupf=0.12))
    erwartet = (O.V_REF_HOCH_MPS ** 2 - O.V_REF_NIEDRIG_MPS ** 2) / (2 * 9.0)
    pruefe(m["gueltig"], "Messschrieb gueltig")
    pruefe(abs(m["bremsweg_referenz_m"] - erwartet) < 0.6,
           f"Bremsweg {m['bremsweg_referenz_m']:.2f} m (analytisch {erwartet:.2f} m)")
    pruefe(abs(m["mittlere_verzoegerung_mps2"] - 9.0) < 0.2, "mittlere Verzoegerung")
    pruefe(m["blockieranteil"] == 0.0, "kein Blockieren bei 12 % Schlupf")
    pruefe(m["stillstand_erreicht"], "Stillstand erkannt")

    m_blockiert = O.berechne_metriken(_messschrieb(a=6.0, schlupf=0.95))
    pruefe(m_blockiert["blockieranteil"] > 0.9, "Blockieren erkannt")
    pruefe(m_blockiert["bremsweg_referenz_m"] > m["bremsweg_referenz_m"],
           "blockiert -> laengerer Bremsweg")

    m_kurve = O.berechne_metriken(_messschrieb(a=8.0, gierrate=0.35))
    pruefe(m_kurve["kurswinkelaenderung_grad"] > 30.0, "Richtungsaenderung erkannt")
    pruefe(O.berechne_metriken(_messschrieb(a=8.0))["kurswinkelaenderung_grad"] < 1.0,
           "ohne Gierrate keine Richtungsaenderung")
    pruefe(not O.berechne_metriken(_messschrieb(bremse_ab_s=99.0))["gueltig"],
           "ohne Bremsung ungueltig")
    pruefe(not O.berechne_metriken({"abtastungen": []})["gueltig"], "leerer Schrieb ungueltig")

    print("Normierung:")
    pruefe(O._normiere(50.0, 50.0, 35.0) == 0.0, "wie ohne ABS -> 0")
    pruefe(O._normiere(35.0, 50.0, 35.0) == 1.0, "wie Referenz -> 1")
    pruefe(abs(O._normiere(42.5, 50.0, 35.0) - 0.5) < 1e-9, "dazwischen -> 0.5")
    pruefe(O._normiere(20.0, 50.0, 35.0) == 1.25, "besser als Referenz -> 1.25")
    pruefe(O._normiere(80.0, 50.0, 35.0) == -0.5, "schlechter als ohne ABS -> -0.5")
    pruefe(abs(O._normiere(30.0, 5.0, 45.0) - 0.625) < 1e-3,
           "Richtung 'groesser ist besser' wird erkannt")
    pruefe(O._normiere(None, 1.0, 2.0) is None, "fehlender Wert -> None")

    print("Punktevergabe:")
    basis_ohne = O.berechne_metriken(_messschrieb(a=6.0, schlupf=0.95))
    basis_ref = O.berechne_metriken(_messschrieb(a=9.5, schlupf=0.12))
    szenario = O.SZENARIEN[0]
    gut = O.bewerte_szenario(szenario, O.berechne_metriken(_messschrieb(a=9.4, schlupf=0.13)),
                             basis_ohne, basis_ref)
    schlecht = O.bewerte_szenario(szenario, O.berechne_metriken(_messschrieb(a=6.1, schlupf=0.9)),
                                  basis_ohne, basis_ref)
    pruefe(gut["punkte"] > 0.85, f"gute Regelung: {gut['punkte']:.3f}")
    pruefe(schlecht["punkte"] < 0.2, f"blockierende Regelung: {schlecht['punkte']:.3f}")

    schrieb = _messschrieb(a=9.4, schlupf=0.13)
    for p in schrieb["abtastungen"][40:60]:
        p["schwimmwinkel_rad"] = math.radians(70.0)
    m_dreher = O.berechne_metriken(schrieb)
    pruefe(m_dreher["dreher"], "Dreher erkannt")
    pruefe(O.bewerte_szenario(szenario, m_dreher, basis_ohne, basis_ref)["punkte"]
           < 0.3 * gut["punkte"], "Dreher wird abgestraft")

    schrieb = _messschrieb(a=9.4, schlupf=0.13)
    schrieb["abtastungen"][30]["regler_fehler"] = "ValueError: x"
    schrieb["abtastungen"][31]["regler_laufzeit_ms"] = 25.0
    note = O.bewerte_szenario(szenario, O.berechne_metriken(schrieb), basis_ohne, basis_ref)
    pruefe(len(note["anmerkungen"]) >= 2, "Reglerfehler und Taktverletzung gemeldet")
    pruefe(note["punkte"] < gut["punkte"], "Abzuege wirken")
    pruefe(O.bewerte_szenario(szenario, {"gueltig": False, "grund": "x"},
                              basis_ohne, basis_ref)["punkte"] == 0.0,
           "ungueltiges Szenario -> 0 Punkte")


# ─── 6. Codepruefung ──────────────────────────────────────────────────────────

ORIGINAL_QUELLTEXT = '''"""Kopf."""
from __future__ import annotations
from lfs_link import Fahrereingabe, Fahrzeugzustand, Pedalstellung
KONSTANTE = 1.0


def berechne_pedalwerte(fahrer, fahrzeug, zustand):
    """Text."""
    return Pedalstellung(fahrer.gas_prozent, fahrer.bremse_prozent)
'''


def test_codepruefung() -> None:
    import orchestrator as O
    print("Codepruefung (AST-Vergleich):")

    b = O.vergleiche_ast(ORIGINAL_QUELLTEXT, ORIGINAL_QUELLTEXT)
    pruefe(b["verstoesse"] == [], "unveraendert -> kein Verstoss")
    pruefe(any("unveraendert" in h for h in b["hinweise"]),
           "unveraenderte Funktion wird gemeldet")

    erlaubt = ORIGINAL_QUELLTEXT.replace(
        "    return Pedalstellung(fahrer.gas_prozent, fahrer.bremse_prozent)",
        "    # eigene Regelung\n"
        "    zustand['x'] = fahrzeug.v_ueber_grund_mps\n"
        "    return Pedalstellung(fahrer.gas_prozent, fahrer.bremse_prozent * 0.5)")
    b = O.vergleiche_ast(ORIGINAL_QUELLTEXT, erlaubt)
    pruefe(b["verstoesse"] == [], f"neuer Rumpf ist erlaubt ({b['verstoesse']})")
    pruefe(b["hinweise"] == [], "Aenderung wird erkannt")

    faelle = [
        ("zusaetzliche Funktion", erlaubt + "\n\ndef hilfe(x):\n    return x\n", "hilfe"),
        ("zusaetzlicher Import", erlaubt.replace("KONSTANTE = 1.0", "import math\nKONSTANTE = 1.0"), "import"),
        ("zusaetzliche Konstante", erlaubt.replace("KONSTANTE = 1.0", "KONSTANTE = 1.0\nZWEITE = 2.0"), "ZWEITE"),
        ("geaenderte Konstante", erlaubt.replace("KONSTANTE = 1.0", "KONSTANTE = 99.0"), "KONSTANTE"),
        ("geaenderter Modulkopf", erlaubt.replace('"""Kopf."""', '"""Anders."""'), "docstring"),
        ("entfernter Import", erlaubt.replace("from __future__ import annotations\n", ""), "import"),
        ("geaenderte Signatur", erlaubt.replace("def berechne_pedalwerte(fahrer, fahrzeug, zustand)",
                                                "def berechne_pedalwerte(fahrer, fahrzeug, zustand, extra=0)"),
         "Signatur"),
        ("Syntaxfehler", erlaubt + "\ndef kaputt(:\n", "parsebar"),
    ]
    for name, quelltext, erwartet in faelle:
        b = O.vergleiche_ast(ORIGINAL_QUELLTEXT, quelltext)
        treffer = any(erwartet.lower() in v.lower() for v in b["verstoesse"])
        pruefe(treffer, f"{name} wird erkannt" if treffer
               else f"{name} wird erkannt (gemeldet: {b['verstoesse']})")

    # Reine Formatierung darf keinen Verstoss ausloesen.
    formatiert = ORIGINAL_QUELLTEXT.replace("KONSTANTE = 1.0", "KONSTANTE = 1.0  # Kommentar")
    pruefe(O.vergleiche_ast(ORIGINAL_QUELLTEXT, formatiert)["verstoesse"] == [],
           "Kommentar ausserhalb von Docstrings ist kein Verstoss")


# ─── 7. Recorder ──────────────────────────────────────────────────────────────

TRACE = {"version": 1, "aufgenommen_am": "selbsttest", "bildschirm": {"breite": 0, "hoehe": 0},
         "dauer_ms": 300,
         "ereignisse": [
             {"t_ms": 0, "typ": "maus_bewegung", "x": 100, "y": 200},
             {"t_ms": 20, "typ": "maus_ab", "knopf": "left", "x": 100, "y": 200},
             {"t_ms": 40, "typ": "maus_auf", "knopf": "left", "x": 100, "y": 200},
             {"t_ms": 60, "typ": "maus_rad", "dx": 0, "dy": -2, "x": 100, "y": 200},
             {"t_ms": 100, "typ": "marke", "name": "fahrt_start"},
             {"t_ms": 120, "typ": "taste_ab", "taste": "w", "sonder": False},
             {"t_ms": 200, "typ": "maus_bewegung", "x": 800, "y": 600},
             {"t_ms": 240, "typ": "taste_auf", "taste": "w", "sonder": False},
             {"t_ms": 300, "typ": "taste_ab", "taste": "s", "sonder": False},
         ]}


class _AttrappeEingabe:
    """Ersetzt die echten pynput-Controller, damit der Test nichts fernsteuert."""

    def __init__(self):
        self.protokoll = []
        self._position = (0, 0)

    @property
    def position(self):
        return self._position

    @position.setter
    def position(self, p):
        self._position = p
        self.protokoll.append(("bewegung", p))

    def press(self, k):
        self.protokoll.append(("ab", repr(k)))

    def release(self, k):
        self.protokoll.append(("auf", repr(k)))

    def scroll(self, dx, dy):
        self.protokoll.append(("rad", (dx, dy)))


def _spiele(**kwargs):
    import input_recorder as R
    abspieler = R.Abspieler()
    tastatur, maus = _AttrappeEingabe(), _AttrappeEingabe()
    abspieler.tastatur, abspieler.maus = tastatur, maus
    abspieler.spiele_ab(TRACE, **kwargs)
    return tastatur.protokoll, maus.protokoll


def test_recorder() -> None:
    try:
        import input_recorder as R
    except Exception as e:
        print(f"Recorder: uebersprungen (pynput nicht verfuegbar: {e})")
        return
    print("Recorder:")
    pruefe(R.marken(TRACE) == {"fahrt_start": 100}, "Marken werden gefunden")

    marken_gesehen = []
    t0 = time.monotonic()
    try:
        tasten, maus = _spiele(bei_marke=lambda n, t: marken_gesehen.append(n))
    except Exception as e:
        print(f"Recorder: uebersprungen (kein Eingabesystem: {e})")
        return
    dauer = time.monotonic() - t0
    soll = TRACE["ereignisse"][-1]["t_ms"] / 1000.0
    pruefe(marken_gesehen == ["fahrt_start"], "Marken-Rueckruf ausgeloest")
    pruefe(soll - 0.02 < dauer < soll + 0.25,
           f"Abspieldauer {dauer * 1000:.0f} ms (Trace: {soll * 1000:.0f} ms)")
    pruefe(("bewegung", (800, 600)) in maus, "Mausbewegung nach der Marke wird abgespielt")
    pruefe(("rad", (0, -2)) in maus, "Mausrad wird abgespielt")
    pruefe(("ab", "'w'") in tasten and ("auf", "'w'") in tasten, "Tasten werden abgespielt")
    pruefe(tasten.count(("auf", "'s'")) == 1, "haengende Taste wird am Ende losgelassen")

    tasten, maus = _spiele(maus_bis_marke="fahrt_start")
    pruefe(("bewegung", (100, 200)) in maus, "vor der Marke laeuft die Maus")
    pruefe(("bewegung", (800, 600)) not in maus, "ab der Marke wird die Maus gesperrt")
    pruefe(("ab", "'w'") in tasten, "Tasten laufen trotz Maus-Sperre weiter")

    tasten, maus = _spiele(ohne_maus=True)
    pruefe(maus == [], "ohne_maus unterdrueckt alle Mausereignisse")

    tasten, maus = _spiele(maus_bis_marke="gibtsnicht")
    pruefe(("bewegung", (800, 600)) in maus, "unbekannte Marke -> vollstaendiges Abspielen")

    t0 = time.monotonic()
    _spiele(tempo=4.0)
    pruefe(time.monotonic() - t0 < 0.25, "Tempo 4x verkuerzt den Ablauf")


# ─── Ablauf ───────────────────────────────────────────────────────────────────

def main() -> int:
    beginn = time.monotonic()
    for test in (test_pakete, test_fahrzeugzustand, test_rollradius,
                 test_mci, test_quellenvergleich, test_fahrereingabe,
                 test_ausgabepruefung, test_regelkreis, test_reglerfehler,
                 test_auswertung, test_codepruefung, test_recorder):
        try:
            test()
        except Exception as e:
            import traceback
            FEHLER.append(f"{test.__name__}: {e}")
            traceback.print_exc()
    print(f"\n{time.monotonic() - beginn:.1f} s")
    if FEHLER:
        print(f"{len(FEHLER)} FEHLER:")
        for f in FEHLER:
            print("  -", f)
        return 1
    print("Alle Pruefungen bestanden.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
