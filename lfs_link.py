"""
lfs_link.py — Anbindung an Live for Speed fuer den ABS-Benchmark.

Diese Datei ist die *Simulationsanbindung*. Sie enthaelt keine Regelungslogik.
Die Regelung liegt ausschliesslich in ``abs_regelung.berechne_pedalwerte``.

    ┌──────────────┐  Tastatur (W/A/S/D, global)   ┌───────────────────────┐
    │ Fahrer bzw.  │ ─────────────────────────────►│ Fahrereingabe         │
    │ Trace-Replay │                               │  Rampen 0..100 %      │
    └──────────────┘                               └──────────┬────────────┘
                                                              │ alle 50 ms
    ┌──────────────┐  OutGauge / OutSim (UDP)      ┌──────────▼────────────┐
    │     LFS      │ ─────────────────────────────►│ Fahrzeugzustand       │
    │              │                               └──────────┬────────────┘
    │              │                                          │
    │              │                        ┌─────────────────▼──────────────┐
    │              │                        │ abs_regelung.berechne_pedalwerte│
    │              │                        └─────────────────┬──────────────┘
    │              │  Maus X/Y (virtuelle Achsen)             │
    │              │◄─────────────────────────────────────────┘
    └──────────────┘

Ablauf pro 50-ms-Takt (Regelkreis._zyklus):
  1. Tastenzustand -> Fahrereingabe (zeitliche Rampen, siehe Konfiguration)
  2. juengste OutGauge-/OutSim-Pakete -> Fahrzeugzustand (Einheiten SI)
  3. Aufruf der Reglerfunktion aus abs_regelung.py
  4. Ergebnis -> Mausposition (X = Lenkung, Y = Gas/Bremse kombiniert)
  5. Abtastung in den Ringpuffer fuer die spaetere Auswertung

Voraussetzungen auf LFS-Seite (siehe README.md):
  * ``/insim 29999``
  * cfg.txt: ``OutGauge Mode 2``/``Port 30000``, ``OutSim Mode 2``/``Port 29998``,
    ``OutSim Opts 1ff``  (ohne 1ff fehlen die Rad-Daten -> keine Vorderachsdrehzahl)
  * Steuerung auf Maus, X-Achse = Lenkung, Y-Achse = Gas/Bremse
  * saemtliche Fahrhilfen von LFS aus (ABS/TC/Bremshilfe) — sonst misst der
    Benchmark die Fahrhilfe von LFS und nicht die Kandidatenregelung.

Aufrufe:
    python lfs_link.py --pruefen        Verbindung + Datenstrom pruefen
    python lfs_link.py --kalibrieren    Maus-Achsen einmessen (Ergebnis: kalibrierung.json)
    python lfs_link.py --fahren         Regelkreis starten (Abbruch mit Strg+C)
"""

from __future__ import annotations

import argparse
import ctypes
import json
import math
import os
import socket
import struct
import sys
import threading
import time
from dataclasses import dataclass, field, asdict
from typing import Any, Dict, List, Optional, Tuple

# abs_regelung wird bewusst erst in Regelkreis.__init__ importiert: abs_regelung
# importiert seinerseits das Datenmodell aus dieser Datei, ein Import auf Modulebene
# waere also zirkulaer.


# ─── Konfiguration ────────────────────────────────────────────────────────────

KALIBRIERUNGSDATEI = os.path.join(os.path.dirname(os.path.abspath(__file__)), "kalibrierung.json")


@dataclass
class Konfiguration:
    """Alle Betriebsparameter der Anbindung. Einheiten stehen im Feldnamen."""

    # --- Netzwerk ---
    insim_host: str = "127.0.0.1"
    insim_port: int = 29999
    outgauge_port: int = 30000
    outsim_port: int = 29998

    # --- Takt ---
    takt_ms: int = 50                      # Regeltakt = Aufrufrate der Reglerfunktion
    max_paketalter_s: float = 0.30         # aeltere Telemetrie gilt als ungueltig

    # --- Rampen der Fahrereingabe (Taste gehalten -> Achswert) ---
    pedal_anstieg_ms: int = 300            # 0 -> 100 % Gas/Bremse
    pedal_abfall_ms: int = 300             # 100 -> 0 % beim Loslassen
    lenkung_anstieg_ms: int = 1000         # 0 -> 100 % Lenkung (bis Anschlag)
    lenkung_ruecklauf_ms: int = 1000       # zurueck zur Mitte

    # --- Tastenbelegung der Fahrereingabe (nicht die LFS-Belegung!) ---
    taste_gas: str = "w"
    taste_bremse: str = "s"
    taste_links: str = "a"
    taste_rechts: str = "d"

    # --- Maus-Achsen ---
    maus_rechteck: Optional[Tuple[int, int, int, int]] = None   # (x0, y0, x1, y1); None = ganzer Bildschirm
    maus_rand_px: int = 40                 # Sicherheitsrand, damit der Cursor nie am Bildschirmrand klebt

    # --- Radmodell ---
    rollradius_standard_m: float = 0.31    # Ersatzwert, bis eingemessen wurde
    rollradius_min_m: float = 0.20
    rollradius_max_m: float = 0.45

    # --- Aufzeichnung ---
    puffer_abtastungen: int = 60000        # ca. 50 min bei 50 ms


KONFIG = Konfiguration()


# ─── Datenmodell: das sieht die Reglerfunktion ────────────────────────────────

@dataclass(frozen=True)
class Fahrereingabe:
    """Was der Fahrer will — bereits zeitlich verrampt, in Prozent."""

    gas_prozent: float          # 0 .. 100
    bremse_prozent: float       # 0 .. 100
    lenkung_prozent: float      # -100 (links) .. +100 (rechts); nur zur Information,
                                # die Lenkung wird vom Harness unveraendert durchgereicht


@dataclass(frozen=True)
class Fahrzeugzustand:
    """Messgroessen des Fahrzeugs zum Zeitpunkt des Reglerauftrufs.

    Bewusste Einschraenkung des Benchmarks: es gibt **keine einzelnen
    Raddrehzahlen**, sondern nur die gemittelte Vorderachsgeschwindigkeit.
    Genauso gibt es **keinen radindividuellen Bremsdruck**, sondern nur die
    globale Bremsanforderung.
    """

    # Zeit
    zeit_s: float                       # monotone Zeit seit Start des Regelkreises
    dt_s: float                         # tatsaechlicher Abstand zum letzten Aufruf (~0.05)

    # Geschwindigkeiten
    v_ueber_grund_mps: float            # Betrag der horizontalen Fahrzeuggeschwindigkeit
    v_vorderachse_mps: float            # Mittel der beiden Vorderrad-Umfangsgeschwindigkeiten
    v_laengs_mps: float                 # Laengsanteil im Fahrzeugkoordinatensystem
    v_quer_mps: float                   # Queranteil im Fahrzeugkoordinatensystem
    schwimmwinkel_rad: float            # atan2(v_quer, v_laengs)

    # Bewegungsgroessen
    gierrate_rad_s: float               # positiv = Drehung nach links (LFS: Z-Achse zeigt nach oben)
    laengsbeschleunigung_mps2: float    # +vorwaerts, beim Bremsen negativ
    querbeschleunigung_mps2: float
    vertikalbeschleunigung_mps2: float
    nickwinkel_rad: float
    rollwinkel_rad: float
    kurswinkel_rad: float

    # Rueckmeldung: was LFS tatsaechlich als Eingabe erhalten hat
    gas_ist_norm: float                 # 0 .. 1
    bremse_ist_norm: float              # 0 .. 1
    lenkung_ist_norm: float             # -1 .. +1
    kupplung_norm: float                # 0 .. 1
    handbremse_norm: float              # 0 .. 1

    # Antriebsstrang
    gang: int                           # 0 = R, 1 = N, 2 = 1. Gang, ...
    motordrehzahl_rpm: float

    # Position (Weltkoordinaten, Meter; X = Ost, Y = Nord, Z = oben)
    position_m: Tuple[float, float, float]

    # Guete der Messwerte — immer pruefen, bevor gerechnet wird!
    daten_gueltig: bool                 # frische OutGauge- *und* OutSim-Pakete vorhanden
    vorderachse_gueltig: bool           # Raddaten vorhanden und Rollradius eingemessen
    datenalter_s: float                 # Alter des juengsten Telemetriepakets
    rollradius_m: float                 # verwendeter Rollradius (eingemessen oder Ersatzwert)

    # Was im letzten Zyklus tatsaechlich ausgegeben wurde (0 .. 100)
    letzte_ausgabe_gas_prozent: float
    letzte_ausgabe_bremse_prozent: float


@dataclass
class Pedalstellung:
    """Rueckgabewert der Reglerfunktion — die virtuellen Pedale."""

    gas_prozent: float          # 0 .. 100
    bremse_prozent: float       # 0 .. 100


# ─── Telemetrie-Pakete ────────────────────────────────────────────────────────

class OutGaugePaket:
    """OutGauge, 92 bzw. 96 Byte (LFS 0.7)."""

    _s = struct.Struct('<I3sxH2B7f2I3f15sx15sx')   # 92 Byte

    __slots__ = ("zeit_ms", "auto", "flags", "gang", "plid", "geschwindigkeit_mps",
                 "drehzahl", "turbo", "motortemperatur", "kraftstoff", "oeldruck",
                 "oeltemperatur", "warnleuchten", "anzeigeleuchten", "gas", "bremse",
                 "kupplung")

    @classmethod
    def entpacke(cls, daten: bytes) -> Optional["OutGaugePaket"]:
        if len(daten) not in (92, 96):
            return None
        p = cls()
        (p.zeit_ms, auto, p.flags, p.gang, p.plid, p.geschwindigkeit_mps, p.drehzahl,
         p.turbo, p.motortemperatur, p.kraftstoff, p.oeldruck, p.oeltemperatur,
         p.warnleuchten, p.anzeigeleuchten, p.gas, p.bremse, p.kupplung,
         _d1, _d2) = cls._s.unpack(daten[:92])
        p.auto = auto.decode("latin-1", "ignore").strip("\x00")
        return p


class OutSimPaket:
    """OutSim mit ``OutSim Opts 1ff`` — 280 Byte, inklusive Rad-Block."""

    _kopf = struct.Struct('<4s')
    _id = struct.Struct('<i')
    _zeit = struct.Struct('<I')
    _haupt = struct.Struct('<3f3f3f3f3i')     # AngVel, Heading/Pitch/Roll, Accel, Vel, Pos
    _eingaben = struct.Struct('<5f')          # Gas, Bremse, Lenkung, Kupplung, Handbremse
    _antrieb = struct.Struct('<4B2f')         # Gang, 3 Reserve, Motorwinkelgeschw., MaxTorqueAtVel
    _distanz = struct.Struct('<2f')
    _rad = struct.Struct('<7f4B2f')           # 40 Byte je Rad
    _extra = struct.Struct('<2f')

    ERWARTETE_GROESSE = 280
    # Reihenfolge der Raeder in OutSim (OutSimPack.txt): hinten links, hinten rechts,
    # vorne links, vorne rechts.
    IDX_VORNE = (2, 3)

    __slots__ = ("kopf", "id", "zeit_ms", "winkelgeschwindigkeit", "kurswinkel",
                 "nickwinkel", "rollwinkel", "beschleunigung", "geschwindigkeit",
                 "position", "eingaben", "gang", "motorwinkelgeschwindigkeit",
                 "rad_winkelgeschwindigkeit_rad_s", "rad_bodenkontakt", "rad_lenkwinkel_rad")

    @classmethod
    def entpacke(cls, daten: bytes) -> Optional["OutSimPaket"]:
        if len(daten) < cls.ERWARTETE_GROESSE:
            return None
        p = cls()
        o = 0
        p.kopf = cls._kopf.unpack_from(daten, o)[0]; o += 4
        p.id = cls._id.unpack_from(daten, o)[0]; o += 4
        p.zeit_ms = cls._zeit.unpack_from(daten, o)[0]; o += 4
        h = cls._haupt.unpack_from(daten, o); o += cls._haupt.size
        p.winkelgeschwindigkeit = h[0:3]                       # rad/s (x, y, z)
        p.kurswinkel, p.nickwinkel, p.rollwinkel = h[3], h[4], h[5]
        p.beschleunigung = h[6:9]                              # m/s^2, Weltkoordinaten
        p.geschwindigkeit = h[9:12]                            # m/s,  Weltkoordinaten
        p.position = tuple(v / 65536.0 for v in h[12:15])      # Meter
        p.eingaben = cls._eingaben.unpack_from(daten, o); o += cls._eingaben.size
        a = cls._antrieb.unpack_from(daten, o); o += cls._antrieb.size
        p.gang, p.motorwinkelgeschwindigkeit = a[0], a[4]
        o += cls._distanz.size

        p.rad_winkelgeschwindigkeit_rad_s = []
        p.rad_bodenkontakt = []
        p.rad_lenkwinkel_rad = []
        for _ in range(4):
            r = cls._rad.unpack_from(daten, o); o += cls._rad.size
            p.rad_lenkwinkel_rad.append(r[1])
            p.rad_winkelgeschwindigkeit_rad_s.append(r[5])
            p.rad_bodenkontakt.append(bool(r[9]))
        return p


class TelemetrieEmpfaenger:
    """Empfaengt OutGauge und OutSim auf je einem eigenen Thread.

    Es wird immer nur das juengste Paket gehalten — der Regelkreis tastet ab,
    er verarbeitet keine Paketfolge. Das entkoppelt Regeltakt und Paketrate.
    """

    def __init__(self, konfig: Konfiguration = KONFIG):
        self.konfig = konfig
        self._sperre = threading.Lock()
        self._outgauge: Optional[OutGaugePaket] = None
        self._outgauge_zeit = 0.0
        self._outsim: Optional[OutSimPaket] = None
        self._outsim_zeit = 0.0
        self._stop = threading.Event()
        self._threads: List[threading.Thread] = []
        self.fehlerhafte_pakete = 0
        self._groesse_gemeldet = False

    def starte(self) -> None:
        self._threads = [
            threading.Thread(target=self._empfange, args=(self.konfig.outgauge_port, "outgauge"),
                             daemon=True, name="outgauge"),
            threading.Thread(target=self._empfange, args=(self.konfig.outsim_port, "outsim"),
                             daemon=True, name="outsim"),
        ]
        for t in self._threads:
            t.start()

    def stoppe(self) -> None:
        self._stop.set()
        for t in self._threads:
            t.join(timeout=1.5)

    def _empfange(self, port: int, art: str) -> None:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        s.settimeout(0.5)
        try:
            s.bind(("127.0.0.1", port))
        except OSError as e:
            print(f"[lfs_link] FEHLER: UDP-Port {port} ({art}) nicht belegbar: {e}")
            return
        while not self._stop.is_set():
            try:
                daten, _ = s.recvfrom(1024)
            except socket.timeout:
                continue
            except OSError:
                break
            jetzt = time.monotonic()
            if art == "outgauge":
                paket = OutGaugePaket.entpacke(daten)
                if paket is None:
                    self.fehlerhafte_pakete += 1
                    continue
                with self._sperre:
                    self._outgauge, self._outgauge_zeit = paket, jetzt
            else:
                if len(daten) != OutSimPaket.ERWARTETE_GROESSE and not self._groesse_gemeldet:
                    self._groesse_gemeldet = True
                    print(f"[lfs_link] WARNUNG: OutSim-Paket hat {len(daten)} statt "
                          f"{OutSimPaket.ERWARTETE_GROESSE} Byte. In cfg.txt fehlt vermutlich "
                          f"'OutSim Opts 1ff' — ohne Rad-Block gibt es keine Vorderachsdrehzahl.")
                paket = OutSimPaket.entpacke(daten)
                if paket is None:
                    self.fehlerhafte_pakete += 1
                    continue
                with self._sperre:
                    self._outsim, self._outsim_zeit = paket, jetzt
        s.close()

    def lies(self) -> Tuple[Optional[OutGaugePaket], float, Optional[OutSimPaket], float]:
        with self._sperre:
            return self._outgauge, self._outgauge_zeit, self._outsim, self._outsim_zeit


# ─── InSim (nur was der Benchmark braucht) ────────────────────────────────────

ISP_ISI, ISP_VER, ISP_TINY, ISP_SMALL, ISP_STA, ISP_MST, ISP_MCI = 1, 2, 3, 4, 5, 13, 38
TINY_NONE, TINY_PING, TINY_REPLY, TINY_SST = 0, 3, 4, 7
INSIM_VERSION = 9
ISF_LOCAL = 4
ISF_MCI = 32
ISS_GAME, ISS_REPLAY, ISS_PAUSED, ISS_DIALOG = 1, 2, 4, 16
ISS_FRONT_END, ISS_VISIBLE, ISS_TEXT_ENTRY = 256, 16384, 32768


@dataclass
class LfsZustand:
    """Ausschnitt aus IS_STA — reicht, um zu wissen ob gefahren werden darf."""

    flags: int = 0
    kamera: int = 0
    sicht_plid: int = 0
    strecke: str = ""
    verbunden: bool = False
    sta_empfangen: bool = False

    @property
    def im_spiel(self) -> bool:
        return bool(self.flags & ISS_GAME)

    @property
    def dialog_offen(self) -> bool:
        return bool(self.flags & (ISS_DIALOG | ISS_TEXT_ENTRY))

    @property
    def pausiert(self) -> bool:
        return bool(self.flags & ISS_PAUSED)

    @property
    def fahrbereit(self) -> bool:
        """Nur dann duerfen Achsen geschrieben werden.

        Solange noch kein IS_STA angekommen ist, gilt bewusst "fahrbereit":
        ein stiller Zustand duerfte sonst dazu fuehren, dass waehrend eines
        Messlaufs ueberhaupt nicht gebremst wird. LFS schickt IS_STA auf
        Anforderung (TINY_SST) und bei jeder Aenderung.
        """
        if not self.sta_empfangen:
            return True
        return self.verbunden and self.im_spiel and not self.dialog_offen and not self.pausiert


class InSimVerbindung:
    """Minimaler InSim-Client: Handshake, Keepalive, Kommandos, Zustand.

    Bewusst ohne pyinsim, damit das Benchmark-Projekt eigenstaendig bleibt.
    """

    _isi = struct.Struct('<4B2HBcH15sx15sx')    # 44 Byte
    _tiny = struct.Struct('<4B')                # 4 Byte
    _mst = struct.Struct('<4B63sx')             # 68 Byte
    _sta = struct.Struct('<4BfH10B5sx2B')       # 28 Byte
    _compcar = struct.Struct('<2H4B3i3Hh')      # 28 Byte je Fahrzeug in IS_MCI

    def __init__(self, konfig: Konfiguration = KONFIG):
        self.konfig = konfig
        self.zustand = LfsZustand()
        # IS_MCI wird nur auf Anforderung abonniert (siehe verbinde). Der
        # Benchmark braucht es nicht; das Diagnosewerkzeug --quellen schon.
        self.mci: Optional[Dict[str, float]] = None
        self.mci_zeit = 0.0
        self.mci_plid = 0                       # 0 = erstes Fahrzeug im Paket
        self._sock: Optional[socket.socket] = None
        self._stop = threading.Event()
        self._thread: Optional[threading.Thread] = None
        self._sendesperre = threading.Lock()

    def verbinde(self, zeitlimit_s: float = 5.0, mci_intervall_ms: int = 0) -> bool:
        """Baut die InSim-Verbindung auf.

        ``mci_intervall_ms`` > 0 abonniert zusaetzlich IS_MCI (Position,
        Geschwindigkeit ueber Grund, Kurs, Gierrate aller Fahrzeuge).
        """
        try:
            self._sock = socket.create_connection((self.konfig.insim_host, self.konfig.insim_port),
                                                  timeout=zeitlimit_s)
            self._sock.settimeout(0.5)
        except OSError as e:
            print(f"[lfs_link] InSim nicht erreichbar ({self.konfig.insim_port}): {e}")
            print("           In LFS '/insim 29999' eingeben oder in autoexec.lfs eintragen.")
            return False

        flags = ISF_LOCAL | (ISF_MCI if mci_intervall_ms > 0 else 0)
        paket = self._isi.pack(self._isi.size // 4, ISP_ISI, 1, 0, 0, flags,
                               INSIM_VERSION, b'!', max(0, mci_intervall_ms),
                               b'', b'ABS-Bench')
        self._sock.sendall(paket)
        self.zustand.verbunden = True
        self._thread = threading.Thread(target=self._lies_schleife, daemon=True, name="insim")
        self._thread.start()
        self.frage_zustand_ab()
        return True

    def trenne(self) -> None:
        self._stop.set()
        if self._thread:
            self._thread.join(timeout=1.5)
        if self._sock:
            try:
                self._sock.close()
            except OSError:
                pass
        self.zustand.verbunden = False

    def _sende(self, daten: bytes) -> None:
        if not self._sock:
            return
        with self._sendesperre:
            try:
                self._sock.sendall(daten)
            except OSError as e:
                print(f"[lfs_link] InSim-Sendefehler: {e}")
                self.zustand.verbunden = False

    def sende_kommando(self, kommando: str) -> None:
        """Sendet einen LFS-Befehl, z. B. '/restart' oder '/car XRG'."""
        text = kommando.encode("latin-1", "ignore")[:63]
        self._sende(self._mst.pack(self._mst.size // 4, ISP_MST, 0, 0, text))

    def frage_zustand_ab(self) -> None:
        self._sende(self._tiny.pack(1, ISP_TINY, 255, TINY_SST))

    def _lies_schleife(self) -> None:
        puffer = b""
        while not self._stop.is_set():
            try:
                teil = self._sock.recv(4096)
            except socket.timeout:
                continue
            except OSError:
                break
            if not teil:
                break
            puffer += teil
            # InSim ab Version 9: erstes Byte = Paketgroesse / 4.
            while len(puffer) >= 4:
                groesse = puffer[0] * 4
                if groesse == 0 or groesse > len(puffer):
                    break
                self._verarbeite(puffer[:groesse])
                puffer = puffer[groesse:]
        self.zustand.verbunden = False

    def _verarbeite(self, paket: bytes) -> None:
        typ = paket[1]
        if typ == ISP_TINY:
            unter = paket[3]
            # Keepalive: LFS erwartet die Antwort, sonst wird die Verbindung geschlossen.
            if unter == TINY_NONE:
                self._sende(self._tiny.pack(1, ISP_TINY, 0, TINY_NONE))
            elif unter == TINY_PING:
                self._sende(self._tiny.pack(1, ISP_TINY, paket[2], TINY_REPLY))
        elif typ == ISP_STA and len(paket) >= self._sta.size:
            f = self._sta.unpack(paket[:self._sta.size])
            self.zustand.sta_empfangen = True
            self.zustand.flags = f[5]
            self.zustand.kamera = f[6]
            self.zustand.sicht_plid = f[7]
            self.zustand.strecke = f[16].decode("latin-1", "ignore").strip("\x00")
        elif typ == ISP_MCI:
            self._verarbeite_mci(paket)

    def _verarbeite_mci(self, paket: bytes) -> None:
        """Liest den CompCar-Eintrag des eigenen Fahrzeugs aus IS_MCI.

        Einheiten laut InSim.txt:
          Speed     word,  32768 = 100 m/s
          AngVel    short, 16384 = 360 Grad/s (Drehung gegen den Uhrzeigersinn)
          Heading   word,  0 = Norden (+Y), gegen den Uhrzeigersinn
          Direction word,  gleiche Kodierung, Bewegungsrichtung (nur bei Speed > 0)
          X/Y/Z     int,   1/65536 m
        """
        anzahl = paket[3]
        for i in range(anzahl):
            versatz = 4 + i * self._compcar.size
            if versatz + self._compcar.size > len(paket):
                return
            (_node, _lap, plid, _pos, _info, _sp3, x, y, z,
             speed, richtung, kurs, gierrate) = self._compcar.unpack_from(paket, versatz)
            if self.mci_plid and plid != self.mci_plid:
                continue
            self.mci = {
                "plid": plid,
                "v_mps": speed * 100.0 / 32768.0,
                "gierrate_rad_s": math.radians(gierrate * 360.0 / 16384.0),
                "kurs_rad": math.radians(kurs * 360.0 / 65536.0),
                "richtung_rad": math.radians(richtung * 360.0 / 65536.0),
                "position_m": (x / 65536.0, y / 65536.0, z / 65536.0),
            }
            self.mci_zeit = time.monotonic()
            return


# ─── Maus-Achsen ──────────────────────────────────────────────────────────────

class MausAchsen:
    """Schreibt Lenkung und Gas/Bremse ueber die Mausposition.

    LFS ist fuer den Benchmark auf Maussteuerung konfiguriert:
        X-Achse  = Lenkung   (links .. rechts)
        Y-Achse  = Gas/Bremse kombiniert (oben = Gas, unten = Bremse)

    **Wichtige physikalische Einschraenkung:** Gas und Bremse teilen sich eine
    Achse und koennen daher nicht gleichzeitig anliegen. Die Bremse hat Vorrang;
    solange die Reglerausgabe Bremse > 0 fordert, wird Gas als 0 ausgegeben.
    Das ist fuer einen Bremsversuch unkritisch, muss aber bekannt sein.

    Die Zuordnung Position -> LFS-Achswert wird nicht angenommen, sondern mit
    ``kalibriere()`` gemessen (Rueckmeldung aus OutGauge/OutSim).
    """

    def __init__(self, konfig: Konfiguration = KONFIG):
        self.konfig = konfig
        self.benutzer32 = ctypes.windll.user32 if sys.platform == "win32" else None
        x0, y0, x1, y1 = self._rechteck()
        self.x_mitte = (x0 + x1) / 2.0
        self.y_mitte = (y0 + y1) / 2.0
        self.x_spanne = (x1 - x0) / 2.0
        self.y_spanne = (y1 - y0) / 2.0
        self.y_richtung = -1.0      # -1: kleineres y = Gas (Standardannahme, per Kalibrierung pruefen)
        self.x_richtung = 1.0
        self.aktiv = True
        self.lade_kalibrierung()

    def _rechteck(self) -> Tuple[int, int, int, int]:
        if self.konfig.maus_rechteck:
            return self.konfig.maus_rechteck
        if self.benutzer32:
            breite = self.benutzer32.GetSystemMetrics(0)
            hoehe = self.benutzer32.GetSystemMetrics(1)
        else:
            breite, hoehe = 1920, 1080
        r = self.konfig.maus_rand_px
        return (r, r, breite - r, hoehe - r)

    # -- Kalibrierung -------------------------------------------------------

    def lade_kalibrierung(self) -> bool:
        try:
            with open(KALIBRIERUNGSDATEI, "r", encoding="utf-8") as f:
                d = json.load(f)
        except (OSError, ValueError):
            return False
        self.x_mitte = d.get("x_mitte", self.x_mitte)
        self.y_mitte = d.get("y_mitte", self.y_mitte)
        self.x_spanne = d.get("x_spanne", self.x_spanne)
        self.y_spanne = d.get("y_spanne", self.y_spanne)
        self.x_richtung = d.get("x_richtung", self.x_richtung)
        self.y_richtung = d.get("y_richtung", self.y_richtung)
        return True

    def speichere_kalibrierung(self) -> None:
        with open(KALIBRIERUNGSDATEI, "w", encoding="utf-8") as f:
            json.dump({"x_mitte": self.x_mitte, "y_mitte": self.y_mitte,
                       "x_spanne": self.x_spanne, "y_spanne": self.y_spanne,
                       "x_richtung": self.x_richtung, "y_richtung": self.y_richtung,
                       "erstellt": time.strftime("%Y-%m-%d %H:%M:%S")}, f, indent=2)

    # -- Ausgabe ------------------------------------------------------------

    def setze(self, lenkung_prozent: float, gas_prozent: float, bremse_prozent: float) -> None:
        if not self.aktiv or self.benutzer32 is None:
            return
        lenkung = max(-1.0, min(1.0, lenkung_prozent / 100.0))
        gas = max(0.0, min(1.0, gas_prozent / 100.0))
        bremse = max(0.0, min(1.0, bremse_prozent / 100.0))
        laengs = -bremse if bremse > 0.0 else gas      # Bremse hat Vorrang
        x = self.x_mitte + self.x_richtung * lenkung * self.x_spanne
        y = self.y_mitte + self.y_richtung * laengs * self.y_spanne
        self.benutzer32.SetCursorPos(int(round(x)), int(round(y)))

    def neutral(self, erzwingen: bool = False) -> None:
        """Pedale los, Lenkung gerade — wird auf jedem Ausstiegspfad gerufen.

        Solange ``aktiv`` False ist (z. B. waehrend ein Menue-Trace die Maus
        braucht), wird der Zeiger nicht angefasst; ``erzwingen`` ist der
        Ausstiegspfad beim Herunterfahren.
        """
        if self.benutzer32 is None or (not self.aktiv and not erzwingen):
            return
        self.benutzer32.SetCursorPos(int(round(self.x_mitte)), int(round(self.y_mitte)))

    def kalibriere(self, telemetrie: "TelemetrieEmpfaenger", schritte: int = 21,
                   wartezeit_s: float = 0.20) -> bool:
        """Faehrt die Y-Achse ab und misst, was LFS daraus macht.

        Voraussetzung: Fahrzeug steht auf der Strecke, Innenansicht, Motor an.
        Ergebnis: Mitte, Spanne und Vorzeichen der Achse.
        """
        if self.benutzer32 is None:
            print("[lfs_link] Kalibrierung nur unter Windows moeglich.")
            return False

        x0, y0, x1, y1 = self._rechteck()
        messwerte: List[Tuple[float, float, float]] = []
        print("[lfs_link] Kalibriere Y-Achse (Gas/Bremse) ...")
        for i in range(schritte):
            y = y0 + (y1 - y0) * i / (schritte - 1)
            self.benutzer32.SetCursorPos(int(round(self.x_mitte)), int(round(y)))
            time.sleep(wartezeit_s)
            og, og_zeit, _, _ = telemetrie.lies()
            if og is None or time.monotonic() - og_zeit > 0.5:
                print("[lfs_link] Keine frischen OutGauge-Daten — Kalibrierung abgebrochen.")
                return False
            messwerte.append((y, og.gas, og.bremse))

        gas_werte = [(y, g) for y, g, _ in messwerte if g > 0.9]
        bremse_werte = [(y, b) for y, _, b in messwerte if b > 0.9]
        neutral_werte = [y for y, g, b in messwerte if g < 0.02 and b < 0.02]
        if not gas_werte or not bremse_werte or not neutral_werte:
            print("[lfs_link] LFS hat auf die Mausbewegung nicht reagiert.")
            print("           Pruefen: Steuerung = Maus, Y-Achse = Gas/Bremse, LFS im Vordergrund,")
            print("           Fenstermodus statt exklusivem Vollbild.")
            return False

        y_gas = sum(y for y, _ in gas_werte) / len(gas_werte)
        y_bremse = sum(y for y, _ in bremse_werte) / len(bremse_werte)
        self.y_mitte = sum(neutral_werte) / len(neutral_werte)
        self.y_spanne = abs(y_bremse - y_gas) / 2.0
        self.y_richtung = 1.0 if y_bremse > y_gas else -1.0

        print("[lfs_link] Kalibriere X-Achse (Lenkung) ...")
        lenk_messwerte: List[Tuple[float, float]] = []
        for i in range(schritte):
            x = x0 + (x1 - x0) * i / (schritte - 1)
            self.benutzer32.SetCursorPos(int(round(x)), int(round(self.y_mitte)))
            time.sleep(wartezeit_s)
            _, _, os_paket, os_zeit = telemetrie.lies()
            if os_paket is None or time.monotonic() - os_zeit > 0.5:
                print("[lfs_link] Keine frischen OutSim-Daten — X-Achse nicht kalibriert.")
                break
            lenk_messwerte.append((x, os_paket.eingaben[2]))

        if lenk_messwerte:
            links = [x for x, s in lenk_messwerte if s < -0.9]
            rechts = [x for x, s in lenk_messwerte if s > 0.9]
            mitte = [x for x, s in lenk_messwerte if abs(s) < 0.02]
            if links and rechts and mitte:
                self.x_mitte = sum(mitte) / len(mitte)
                self.x_spanne = abs(sum(rechts) / len(rechts) - sum(links) / len(links)) / 2.0
                self.x_richtung = 1.0 if sum(rechts) / len(rechts) > sum(links) / len(links) else -1.0

        self.neutral()
        self.speichere_kalibrierung()
        print(f"[lfs_link] Kalibrierung gespeichert: {KALIBRIERUNGSDATEI}")
        print(f"           X: Mitte {self.x_mitte:.0f}, Spanne {self.x_spanne:.0f}, "
              f"Richtung {self.x_richtung:+.0f}")
        print(f"           Y: Mitte {self.y_mitte:.0f}, Spanne {self.y_spanne:.0f}, "
              f"Richtung {self.y_richtung:+.0f}")
        return True


# ─── Fahrereingabe ────────────────────────────────────────────────────────────

class Tastatureingabe:
    """Wandelt gehaltene Tasten in verrampte Achswerte 0..100 %.

    Die Rampen bilden das nach, was ein Fahrer mit einem echten Pedal macht:
    ``taste_bremse`` gehalten -> nach ``pedal_anstieg_ms`` liegt Vollbremsung an.
    Die Reglerfunktion sieht ausschliesslich das Ergebnis, keine Tastenzustaende.
    """

    def __init__(self, konfig: Konfiguration = KONFIG):
        self.konfig = konfig
        self._gedrueckt: Dict[str, bool] = {konfig.taste_gas: False, konfig.taste_bremse: False,
                                            konfig.taste_links: False, konfig.taste_rechts: False}
        self._sperre = threading.Lock()
        self.gas = 0.0
        self.bremse = 0.0
        self.lenkung = 0.0
        self._listener = None

    def starte(self) -> None:
        try:
            from pynput import keyboard
        except ImportError:
            print("[lfs_link] pynput fehlt — Fahrereingabe deaktiviert (pip install pynput).")
            return

        def zeichen(taste) -> Optional[str]:
            try:
                return taste.char.lower() if taste.char else None
            except AttributeError:
                return None

        def bei_druck(taste):
            z = zeichen(taste)
            if z in self._gedrueckt:
                with self._sperre:
                    self._gedrueckt[z] = True

        def bei_loslassen(taste):
            z = zeichen(taste)
            if z in self._gedrueckt:
                with self._sperre:
                    self._gedrueckt[z] = False

        self._listener = keyboard.Listener(on_press=bei_druck, on_release=bei_loslassen)
        self._listener.daemon = True
        self._listener.start()

    def stoppe(self) -> None:
        if self._listener:
            self._listener.stop()

    @staticmethod
    def _rampe(ist: float, ziel: float, dt_s: float, anstieg_ms: int, abfall_ms: int) -> float:
        """Bewegt ``ist`` mit begrenzter Rate in Richtung ``ziel`` (Wertebereich 100)."""
        dauer_ms = anstieg_ms if abs(ziel) > abs(ist) else abfall_ms
        schritt = 100.0 * dt_s * 1000.0 / max(1, dauer_ms)
        if ziel > ist:
            return min(ziel, ist + schritt)
        return max(ziel, ist - schritt)

    def aktualisiere(self, dt_s: float) -> Fahrereingabe:
        with self._sperre:
            gas_ziel = 100.0 if self._gedrueckt[self.konfig.taste_gas] else 0.0
            bremse_ziel = 100.0 if self._gedrueckt[self.konfig.taste_bremse] else 0.0
            links = self._gedrueckt[self.konfig.taste_links]
            rechts = self._gedrueckt[self.konfig.taste_rechts]
        # Beide Lenktasten gleichzeitig = geradeaus.
        lenk_ziel = 0.0 if links == rechts else (-100.0 if links else 100.0)

        self.gas = self._rampe(self.gas, gas_ziel, dt_s,
                               self.konfig.pedal_anstieg_ms, self.konfig.pedal_abfall_ms)
        self.bremse = self._rampe(self.bremse, bremse_ziel, dt_s,
                                  self.konfig.pedal_anstieg_ms, self.konfig.pedal_abfall_ms)
        self.lenkung = self._rampe(self.lenkung, lenk_ziel, dt_s,
                                   self.konfig.lenkung_anstieg_ms, self.konfig.lenkung_ruecklauf_ms)
        return Fahrereingabe(gas_prozent=self.gas, bremse_prozent=self.bremse,
                             lenkung_prozent=self.lenkung)


# ─── Rollradius (Vorderachsgeschwindigkeit) ───────────────────────────────────

class Rollradiusschaetzer:
    """Schaetzt den wirksamen Rollradius der Vorderraeder online.

    Ohne Radradius laesst sich aus der Radwinkelgeschwindigkeit keine
    Umfangsgeschwindigkeit bilden. Statt einer Tabelle je Fahrzeug (die bei
    LFS-Mods immer falsch waere) wird der Radius im Rollen gemessen:
    schlupffrei gilt r = v_Grund / omega. Als schlupffrei gilt ein Zustand mit
    ausreichend Tempo, ohne Bremse, ohne nennenswertes Gas und ohne Querdynamik.
    Der Wert wird je Fahrzeugkuerzel persistiert.
    """

    DATEI = os.path.join(os.path.dirname(os.path.abspath(__file__)), "rollradius.json")

    def __init__(self, konfig: Konfiguration = KONFIG):
        self.konfig = konfig
        self.radius_m = konfig.rollradius_standard_m
        self.eingemessen = False
        self.auto = ""
        self._werte: Dict[str, float] = {}
        try:
            with open(self.DATEI, "r", encoding="utf-8") as f:
                self._werte = json.load(f)
        except (OSError, ValueError):
            self._werte = {}

    def setze_fahrzeug(self, auto: str) -> None:
        if auto == self.auto:
            return
        self.auto = auto
        gespeichert = self._werte.get(auto)
        if gespeichert and self.konfig.rollradius_min_m <= gespeichert <= self.konfig.rollradius_max_m:
            self.radius_m = gespeichert
            self.eingemessen = True
        else:
            self.radius_m = self.konfig.rollradius_standard_m
            self.eingemessen = False

    def aktualisiere(self, v_grund_mps: float, omega_vorne_rad_s: float,
                     gas_norm: float, bremse_norm: float, quer_mps2: float) -> None:
        if (v_grund_mps < 8.0 or omega_vorne_rad_s < 1.0 or bremse_norm > 0.02
                or gas_norm > 0.05 or abs(quer_mps2) > 2.0):
            return
        r = v_grund_mps / omega_vorne_rad_s
        if not (self.konfig.rollradius_min_m <= r <= self.konfig.rollradius_max_m):
            return
        # Exponentiell geglaettet; bewusst traege, ein Ausreisser darf nichts verschieben.
        self.radius_m = 0.95 * self.radius_m + 0.05 * r if self.eingemessen else r
        self.eingemessen = True

    def speichere(self) -> None:
        if not (self.eingemessen and self.auto):
            return
        self._werte[self.auto] = round(self.radius_m, 4)
        try:
            with open(self.DATEI, "w", encoding="utf-8") as f:
                json.dump(self._werte, f, indent=2)
        except OSError:
            pass


# ─── Regelkreis ───────────────────────────────────────────────────────────────

@dataclass
class Abtastung:
    """Eine Zeile der Messschrieb-Aufzeichnung (alle 50 ms)."""

    t_mono: float
    zustand: Dict[str, Any]
    fahrer_gas: float
    fahrer_bremse: float
    fahrer_lenkung: float
    ausgabe_gas: float
    ausgabe_bremse: float
    regler_laufzeit_ms: float
    regler_fehler: Optional[str] = None


class Regelkreis:
    """Der 50-ms-Takt: Messen -> Regeln -> Stellen -> Aufzeichnen."""

    def __init__(self, insim: InSimVerbindung, telemetrie: TelemetrieEmpfaenger,
                 achsen: MausAchsen, eingabe: Tastatureingabe, konfig: Konfiguration = KONFIG):
        self.insim = insim
        self.telemetrie = telemetrie
        self.achsen = achsen
        self.eingabe = eingabe
        self.konfig = konfig

        # Kandidatenfunktion laden. Ein Fehler hier soll den Start abbrechen und
        # nicht erst im ersten Regeltakt auffallen.
        import abs_regelung
        self.reglerfunktion = abs_regelung.berechne_pedalwerte

        self.radius = Rollradiusschaetzer(konfig)
        self.abtastungen: List[Abtastung] = []
        self._abtastsperre = threading.Lock()
        self._stop = threading.Event()
        self._thread: Optional[threading.Thread] = None
        self._reglerzustand: Dict[str, Any] = {}
        self._t0 = 0.0
        self._t_letzt = 0.0
        self._letzte_ausgabe = (0.0, 0.0)
        self._letzte_geschwindigkeit = None
        self.zyklen = 0
        self.ueberlaeufe = 0
        self.reglerfehler = 0
        self._fehler_gemeldet = False
        self._achsen_gemeldet = False
        self._achsen_verdacht_s = 0.0

    # -- Lebenszyklus -------------------------------------------------------

    def starte(self) -> None:
        _erhoehe_timerausfloesung()
        self._t0 = time.monotonic()
        self._t_letzt = self._t0
        self._stop.clear()
        self._thread = threading.Thread(target=self._schleife, daemon=True, name="regelkreis")
        self._thread.start()

    def stoppe(self) -> None:
        self._stop.set()
        if self._thread:
            self._thread.join(timeout=2.0)
        self.achsen.neutral(erzwingen=True)
        self.radius.speichere()
        _setze_timeraufloesung_zurueck()

    def setze_regler_zurueck(self) -> None:
        """Vor jedem Szenario aufrufen: der Regler startet ohne Vorgeschichte."""
        self._reglerzustand = {}

    def leere_abtastungen(self) -> None:
        with self._abtastsperre:
            self.abtastungen.clear()

    def hole_abtastungen(self, t_von: float, t_bis: float) -> List[Abtastung]:
        with self._abtastsperre:
            return [a for a in self.abtastungen if t_von <= a.t_mono <= t_bis]

    # -- Schleife -----------------------------------------------------------

    def _schleife(self) -> None:
        takt_s = self.konfig.takt_ms / 1000.0
        naechster = time.monotonic()
        while not self._stop.is_set():
            naechster += takt_s
            rest = naechster - time.monotonic()
            if rest > 0:
                time.sleep(rest)
            else:
                # Takt verpasst: nicht aufholen, sondern neu ausrichten.
                self.ueberlaeufe += 1
                naechster = time.monotonic()
            try:
                self._zyklus()
            except Exception as e:      # Der Regelkreis darf niemals still sterben.
                self.reglerfehler += 1
                if not self._fehler_gemeldet:
                    self._fehler_gemeldet = True
                    import traceback
                    print("[lfs_link] Ausnahme im Regeltakt:")
                    traceback.print_exc()

    def _zyklus(self) -> None:
        jetzt = time.monotonic()
        dt = max(1e-3, jetzt - self._t_letzt)
        self._t_letzt = jetzt
        self.zyklen += 1

        fahrer = self.eingabe.aktualisiere(dt)
        zustand = self._baue_zustand(jetzt, dt)

        # --- Reglerfunktion (die einzige Datei, die die KI aendern darf) ---
        beginn = time.perf_counter()
        fehler = None
        try:
            ausgabe = self.reglerfunktion(fahrer, zustand, self._reglerzustand)
            gas, bremse = self._pruefe_ausgabe(ausgabe)
        except Exception as e:
            # Rueckfallebene: Fahrereingabe 1:1. Ein Reglerfehler darf den
            # Versuch nicht in einen unkontrollierten Zustand bringen.
            fehler = f"{type(e).__name__}: {e}"
            self.reglerfehler += 1
            gas, bremse = fahrer.gas_prozent, fahrer.bremse_prozent
            if not self._fehler_gemeldet:
                self._fehler_gemeldet = True
                import traceback
                print("[lfs_link] Ausnahme in berechne_pedalwerte — Rueckfall auf Durchreichen:")
                traceback.print_exc()
        laufzeit_ms = (time.perf_counter() - beginn) * 1000.0

        # --- Stellen ---
        if self.insim.zustand.fahrbereit or not self.insim.zustand.verbunden:
            self.achsen.setze(fahrer.lenkung_prozent, gas, bremse)
        else:
            self.achsen.neutral()
        self._letzte_ausgabe = (gas, bremse)
        self._pruefe_stellweg(zustand, bremse, dt)

        # --- Aufzeichnen ---
        with self._abtastsperre:
            if len(self.abtastungen) < self.konfig.puffer_abtastungen:
                self.abtastungen.append(Abtastung(
                    t_mono=jetzt, zustand=asdict(zustand),
                    fahrer_gas=fahrer.gas_prozent, fahrer_bremse=fahrer.bremse_prozent,
                    fahrer_lenkung=fahrer.lenkung_prozent,
                    ausgabe_gas=gas, ausgabe_bremse=bremse,
                    regler_laufzeit_ms=laufzeit_ms, regler_fehler=fehler))

    @staticmethod
    def _pruefe_ausgabe(ausgabe: Any) -> Tuple[float, float]:
        """Ausgabe der Reglerfunktion validieren und begrenzen."""
        try:
            gas = float(ausgabe.gas_prozent)
            bremse = float(ausgabe.bremse_prozent)
        except (AttributeError, TypeError, ValueError):
            raise TypeError("berechne_pedalwerte muss eine Pedalstellung mit den Feldern "
                            f"gas_prozent und bremse_prozent liefern, geliefert wurde {ausgabe!r}")
        if math.isnan(gas) or math.isnan(bremse) or math.isinf(gas) or math.isinf(bremse):
            raise ValueError("Pedalwert ist NaN oder unendlich")
        return max(0.0, min(100.0, gas)), max(0.0, min(100.0, bremse))

    def _pruefe_stellweg(self, zustand: Fahrzeugzustand, bremse_soll: float, dt: float) -> None:
        """Meldet, wenn LFS die Maus-Achse offensichtlich nicht annimmt."""
        if bremse_soll > 50.0 and zustand.daten_gueltig and zustand.bremse_ist_norm < 0.05:
            self._achsen_verdacht_s += dt
        else:
            self._achsen_verdacht_s = 0.0
        if self._achsen_verdacht_s > 0.5 and not self._achsen_gemeldet:
            self._achsen_gemeldet = True
            print("[lfs_link] WARNUNG: Bremse angefordert, LFS meldet aber keine Bremse. "
                  "Maus-Achse pruefen (--kalibrieren), LFS in den Vordergrund holen.")

    # -- Messgroessen -------------------------------------------------------

    def _baue_zustand(self, jetzt: float, dt: float) -> Fahrzeugzustand:
        og, og_zeit, os_paket, os_zeit = self.telemetrie.lies()
        alter = min(jetzt - og_zeit if og else 1e9, jetzt - os_zeit if os_paket else 1e9)
        gueltig = (og is not None and os_paket is not None
                   and alter <= self.konfig.max_paketalter_s)

        if og is not None:
            self.radius.setze_fahrzeug(og.auto)

        if not gueltig:
            # Ungueltige Daten werden nicht kaschiert: Nullen plus daten_gueltig=False.
            return Fahrzeugzustand(
                zeit_s=jetzt - self._t0, dt_s=dt,
                v_ueber_grund_mps=0.0, v_vorderachse_mps=0.0, v_laengs_mps=0.0,
                v_quer_mps=0.0, schwimmwinkel_rad=0.0, gierrate_rad_s=0.0,
                laengsbeschleunigung_mps2=0.0, querbeschleunigung_mps2=0.0,
                vertikalbeschleunigung_mps2=0.0, nickwinkel_rad=0.0, rollwinkel_rad=0.0,
                kurswinkel_rad=0.0, gas_ist_norm=0.0, bremse_ist_norm=0.0,
                lenkung_ist_norm=0.0, kupplung_norm=0.0, handbremse_norm=0.0,
                gang=0, motordrehzahl_rpm=0.0, position_m=(0.0, 0.0, 0.0),
                daten_gueltig=False, vorderachse_gueltig=False,
                datenalter_s=min(alter, 999.0), rollradius_m=self.radius.radius_m,
                letzte_ausgabe_gas_prozent=self._letzte_ausgabe[0],
                letzte_ausgabe_bremse_prozent=self._letzte_ausgabe[1])

        # Geschwindigkeit: OutSim liefert den Vektor in Weltkoordinaten.
        vx, vy, vz = os_paket.geschwindigkeit
        v_grund = math.hypot(vx, vy)

        # Weltkoordinaten -> Fahrzeugkoordinaten. LFS: Kurswinkel 0 = +Y (Norden),
        # Drehung gegen den Uhrzeigersinn.
        kurs = os_paket.kurswinkel
        sin_k, cos_k = math.sin(kurs), math.cos(kurs)
        v_laengs = vy * cos_k + vx * sin_k
        v_quer = -vx * cos_k + vy * sin_k
        ax_welt, ay_welt, az_welt = os_paket.beschleunigung
        a_laengs = ay_welt * cos_k + ax_welt * sin_k
        a_quer = -ax_welt * cos_k + ay_welt * sin_k

        # Vorderachse: Mittel der beiden Vorderraeder. Einzelraeder werden
        # bewusst nicht nach aussen gegeben (Vorgabe des Benchmarks).
        vorderachse_gueltig = False
        v_vorderachse = 0.0
        if len(os_paket.rad_winkelgeschwindigkeit_rad_s) == 4:
            i_l, i_r = OutSimPaket.IDX_VORNE
            omega = (abs(os_paket.rad_winkelgeschwindigkeit_rad_s[i_l])
                     + abs(os_paket.rad_winkelgeschwindigkeit_rad_s[i_r])) / 2.0
            self.radius.aktualisiere(v_grund, omega, og.gas, og.bremse, a_quer)
            v_vorderachse = omega * self.radius.radius_m
            vorderachse_gueltig = self.radius.eingemessen

        return Fahrzeugzustand(
            zeit_s=jetzt - self._t0, dt_s=dt,
            v_ueber_grund_mps=v_grund,
            v_vorderachse_mps=v_vorderachse,
            v_laengs_mps=v_laengs, v_quer_mps=v_quer,
            schwimmwinkel_rad=math.atan2(v_quer, v_laengs) if v_grund > 0.5 else 0.0,
            gierrate_rad_s=os_paket.winkelgeschwindigkeit[2],
            laengsbeschleunigung_mps2=a_laengs,
            querbeschleunigung_mps2=a_quer,
            vertikalbeschleunigung_mps2=az_welt,
            nickwinkel_rad=os_paket.nickwinkel, rollwinkel_rad=os_paket.rollwinkel,
            kurswinkel_rad=kurs,
            gas_ist_norm=og.gas, bremse_ist_norm=og.bremse,
            lenkung_ist_norm=os_paket.eingaben[2],
            kupplung_norm=og.kupplung, handbremse_norm=os_paket.eingaben[4],
            gang=og.gang, motordrehzahl_rpm=og.drehzahl,
            position_m=os_paket.position,
            daten_gueltig=True, vorderachse_gueltig=vorderachse_gueltig,
            datenalter_s=alter, rollradius_m=self.radius.radius_m,
            letzte_ausgabe_gas_prozent=self._letzte_ausgabe[0],
            letzte_ausgabe_bremse_prozent=self._letzte_ausgabe[1])


# ─── Windows-Timeraufloesung ──────────────────────────────────────────────────

def _erhoehe_timerausfloesung() -> None:
    """time.sleep() ist unter Windows sonst auf ~15,6 ms gerastert.

    Ohne diesen Aufruf ist ein 50-ms-Takt nicht einzuhalten.
    """
    if sys.platform == "win32":
        try:
            ctypes.windll.winmm.timeBeginPeriod(1)
        except Exception:
            pass


def _setze_timeraufloesung_zurueck() -> None:
    if sys.platform == "win32":
        try:
            ctypes.windll.winmm.timeEndPeriod(1)
        except Exception:
            pass


# ─── Aufbau / Abbau ───────────────────────────────────────────────────────────

class LfsAnbindung:
    """Buendelt alle Teile und garantiert, dass nichts offen zurueckbleibt."""

    def __init__(self, konfig: Konfiguration = KONFIG):
        self.konfig = konfig
        self.telemetrie = TelemetrieEmpfaenger(konfig)
        self.insim = InSimVerbindung(konfig)
        self.achsen = MausAchsen(konfig)
        self.eingabe = Tastatureingabe(konfig)
        self.regelkreis = Regelkreis(self.insim, self.telemetrie, self.achsen,
                                     self.eingabe, konfig)
        self._gestartet = False

    def starte(self, mit_regelkreis: bool = True) -> bool:
        self.telemetrie.starte()
        if not self.insim.verbinde():
            self.telemetrie.stoppe()
            return False
        self.eingabe.starte()
        if mit_regelkreis:
            self.regelkreis.starte()
        self._gestartet = True
        return True

    def stoppe(self) -> None:
        if not self._gestartet:
            return
        self._gestartet = False
        # Reihenfolge ist wichtig: erst stellen beenden, dann Sensorik.
        self.regelkreis.stoppe()
        self.eingabe.stoppe()
        self.insim.trenne()
        self.telemetrie.stoppe()

    def __enter__(self) -> "LfsAnbindung":
        if not self.starte():
            raise RuntimeError("Verbindung zu LFS fehlgeschlagen")
        return self

    def __exit__(self, *_) -> None:
        self.stoppe()

    def warte_auf_daten(self, zeitlimit_s: float = 10.0) -> bool:
        ende = time.monotonic() + zeitlimit_s
        while time.monotonic() < ende:
            og, og_zeit, os_paket, os_zeit = self.telemetrie.lies()
            if og is not None and os_paket is not None:
                return True
            time.sleep(0.1)
        return False


# ─── Kommandozeile ────────────────────────────────────────────────────────────

def _pruefen() -> int:
    anbindung = LfsAnbindung()
    anbindung.telemetrie.starte()
    ok_insim = anbindung.insim.verbinde()
    print(f"InSim      : {'verbunden' if ok_insim else 'FEHLT'}")
    time.sleep(1.5)
    og, og_zeit, os_paket, os_zeit = anbindung.telemetrie.lies()
    print(f"OutGauge   : {'ok' if og else 'FEHLT (cfg.txt: OutGauge Mode 2, Port 30000)'}")
    if os_paket:
        raeder = len(os_paket.rad_winkelgeschwindigkeit_rad_s)
        print(f"OutSim     : ok, {raeder} Raeder"
              + ("" if raeder == 4 else "  -> cfg.txt: OutSim Opts 1ff fehlt!"))
    else:
        print("OutSim     : FEHLT (cfg.txt: OutSim Mode 2, Port 29998, Opts 1ff)")
    if og:
        print(f"Fahrzeug   : {og.auto}   Gang {og.gang}   {og.geschwindigkeit_mps * 3.6:5.1f} km/h")
    z = anbindung.insim.zustand
    if not z.sta_empfangen:
        print("LFS-Zustand: unbekannt (kein IS_STA empfangen)")
    else:
        print(f"LFS-Zustand: {'fahrbereit' if z.fahrbereit else 'nicht fahrbereit'}, "
              f"Strecke '{z.strecke}', Kamera {z.kamera}")
    print(f"Kalibrierung Maus: {'vorhanden' if os.path.exists(KALIBRIERUNGSDATEI) else 'FEHLT (--kalibrieren)'}")
    anbindung.insim.trenne()
    anbindung.telemetrie.stoppe()
    return 0 if (ok_insim and og and os_paket) else 1


def _kalibrieren() -> int:
    anbindung = LfsAnbindung()
    anbindung.telemetrie.starte()
    if not anbindung.insim.verbinde():
        anbindung.telemetrie.stoppe()
        return 1
    print("Fahrzeug auf die Strecke stellen, Innenansicht, LFS im Vordergrund lassen.")
    print("Die Kalibrierung startet in 5 Sekunden und bewegt die Maus.")
    time.sleep(5.0)
    if not anbindung.warte_auf_daten():
        print("Keine Telemetrie — zuerst 'python lfs_link.py --pruefen' ausfuehren.")
        anbindung.insim.trenne(); anbindung.telemetrie.stoppe()
        return 1
    ok = anbindung.achsen.kalibriere(anbindung.telemetrie)
    anbindung.insim.trenne()
    anbindung.telemetrie.stoppe()
    return 0 if ok else 1


def _vergleiche_quellen(zeilen: List[Dict[str, float]]) -> Dict[str, Any]:
    """Ordnet OutGauge.Speed einem der Kandidatensignale zu.

    Vorgehen: je Kandidat wird ein einzelner Skalenfaktor k kleinste-Quadrate
    durch den Ursprung angepasst (fuer Radsignale ist k der Rollradius, fuer
    Geschwindigkeitssignale muss k ~ 1 herauskommen). Entscheidend ist der
    Restfehler **waehrend der Bremsung**: dort laufen Rad- und
    Grundgeschwindigkeit auseinander, sonst nicht.
    """
    kandidaten = {
        "OutSim |v| (ueber Grund)": "v_outsim",
        "MCI.Speed (ueber Grund)": "v_mci",
        "OutSim Raeder vorne (omega)": "omega_v",
        "OutSim Raeder hinten (omega)": "omega_h",
    }
    roll = [z for z in zeilen if z["og_speed"] > 2.0]
    brems = [z for z in roll if z["bremse"] > 0.5]
    ergebnis: Dict[str, Any] = {"proben": len(roll), "bremsproben": len(brems),
                                "kandidaten": {}}
    if len(roll) < 20:
        return ergebnis

    for name, feld in kandidaten.items():
        gueltig = [z for z in roll if z[feld] > 0.01]
        if len(gueltig) < 20:
            continue
        zaehler = sum(z["og_speed"] * z[feld] for z in gueltig)
        nenner = sum(z[feld] ** 2 for z in gueltig)
        if nenner <= 0:
            continue
        k = zaehler / nenner

        def rest(menge):
            menge = [z for z in menge if z[feld] > 0.01]
            if not menge:
                return None
            quadrate = sum((z["og_speed"] - k * z[feld]) ** 2 for z in menge) / len(menge)
            bezug = sum(z["og_speed"] for z in menge) / len(menge)
            return 100.0 * math.sqrt(quadrate) / max(0.1, bezug)

        ergebnis["kandidaten"][name] = {"faktor": k, "rest_gesamt": rest(roll),
                                        "rest_bremsen": rest(brems)}

    bewertbar = {n: w for n, w in ergebnis["kandidaten"].items()
                 if w["rest_bremsen"] is not None}
    if bewertbar and brems:
        ergebnis["treffer"] = min(bewertbar, key=lambda n: bewertbar[n]["rest_bremsen"])
    return ergebnis


def _quellen(dauer_s: float = 40.0) -> int:
    """Diagnose: welches Signal ist die Vorderachs-, welches die Grundgeschwindigkeit?

    Ablauf: Fahrzeug auf die Strecke, beschleunigen, dann **mit blockierenden
    Raedern** voll bremsen (Fahrhilfen aus). Nur dann trennen sich Rad- und
    Grundgeschwindigkeit ueberhaupt.
    """
    anbindung = LfsAnbindung()
    anbindung.telemetrie.starte()
    if not anbindung.insim.verbinde(mci_intervall_ms=50):
        anbindung.telemetrie.stoppe()
        return 1
    if not anbindung.warte_auf_daten():
        print("Keine Telemetrie — zuerst 'python lfs_link.py --pruefen' ausfuehren.")
        anbindung.insim.trenne(); anbindung.telemetrie.stoppe()
        return 1

    print(f"Aufzeichnung laeuft {dauer_s:.0f} s. Beschleunigen, dann VOLLBREMSUNG bis "
          f"zum Blockieren. Strg+C beendet frueher.\n")
    print("   t     Bremse   OutGauge    OutSim|v|      MCI    omega_v    omega_h")
    zeilen: List[Dict[str, float]] = []
    t0 = time.monotonic()
    try:
        while time.monotonic() - t0 < dauer_s:
            time.sleep(0.05)
            og, og_zeit, osp, os_zeit = anbindung.telemetrie.lies()
            if og is None or osp is None or len(osp.rad_winkelgeschwindigkeit_rad_s) != 4:
                continue
            if og.plid and not anbindung.insim.mci_plid:
                anbindung.insim.mci_plid = og.plid
            i_l, i_r = OutSimPaket.IDX_VORNE
            omega_v = (abs(osp.rad_winkelgeschwindigkeit_rad_s[i_l])
                       + abs(osp.rad_winkelgeschwindigkeit_rad_s[i_r])) / 2.0
            omega_h = (abs(osp.rad_winkelgeschwindigkeit_rad_s[0])
                       + abs(osp.rad_winkelgeschwindigkeit_rad_s[1])) / 2.0
            mci = anbindung.insim.mci
            frisch = mci is not None and time.monotonic() - anbindung.insim.mci_zeit < 0.3
            zeile = {"t": time.monotonic() - t0, "bremse": og.bremse,
                     "og_speed": og.geschwindigkeit_mps,
                     "v_outsim": math.hypot(osp.geschwindigkeit[0], osp.geschwindigkeit[1]),
                     "v_mci": mci["v_mps"] if frisch else 0.0,
                     "omega_v": omega_v, "omega_h": omega_h}
            zeilen.append(zeile)
            print(f"\r{zeile['t']:6.2f}  {zeile['bremse']:6.2f}  {zeile['og_speed']:9.3f}  "
                  f"{zeile['v_outsim']:9.3f}  {zeile['v_mci']:9.3f}  "
                  f"{omega_v:8.2f}  {omega_h:8.2f}", end="")
    except KeyboardInterrupt:
        pass
    finally:
        print()
        anbindung.insim.trenne()
        anbindung.telemetrie.stoppe()

    ergebnis = _vergleiche_quellen(zeilen)
    print("\n" + "=" * 74)
    print(f"{ergebnis['proben']} Proben in Fahrt, davon {ergebnis['bremsproben']} unter Bremsung")
    if not ergebnis.get("kandidaten"):
        print("Zu wenige Daten — laenger fahren.")
        return 1
    if not ergebnis["bremsproben"]:
        print("Es wurde nicht gebremst. Ohne Bremsphase sind alle Signale identisch,")
        print("die Zuordnung ist dann nicht entscheidbar. Bitte wiederholen.")
        return 1

    print("\nWorauf laeuft OutGauge.Speed hinaus?\n")
    print(f"{'Kandidat':<30}{'Faktor k':>12}{'Rest gesamt':>14}{'Rest beim Bremsen':>19}")
    for name, w in ergebnis["kandidaten"].items():
        einheit = " m" if "omega" in name else ""
        rg = "—" if w["rest_gesamt"] is None else f"{w['rest_gesamt']:.2f} %"
        rb = "—" if w["rest_bremsen"] is None else f"{w['rest_bremsen']:.2f} %"
        print(f"{name:<30}{w['faktor']:>10.4f}{einheit:<2}{rg:>14}{rb:>19}")

    treffer = ergebnis.get("treffer")
    print(f"\nOutGauge.Speed folgt am ehesten: {treffer}")
    if treffer and "vorne" in treffer:
        print("  -> Die Annahme stimmt: OutGauge.Speed ist die Vorderachsgeschwindigkeit.")
        print("     Der Rollradius muss dann nicht mehr geschaetzt werden.")
    elif treffer and "Grund" in treffer:
        print("  -> OutGauge.Speed ist die Geschwindigkeit ueber Grund, nicht die")
        print("     Vorderachse. Die Vorderachse muss dann aus den OutSim-Raddaten")
        print("     kommen (so ist es aktuell implementiert).")
    print("=" * 74)
    return 0


def _fahren() -> int:
    anbindung = LfsAnbindung()
    if not anbindung.starte():
        return 1
    print("Regelkreis laeuft (50 ms). W = Gas, S = Bremse, A/D = Lenkung. Strg+C beendet.")
    try:
        while True:
            time.sleep(1.0)
            rk = anbindung.regelkreis
            og, _, _, _ = anbindung.telemetrie.lies()
            tempo = og.geschwindigkeit_mps * 3.6 if og else 0.0
            print(f"\r{tempo:6.1f} km/h | Zyklen {rk.zyklen:6d} | Taktueberlauf {rk.ueberlaeufe:4d} "
                  f"| Reglerfehler {rk.reglerfehler:3d}", end="")
    except KeyboardInterrupt:
        print()
    finally:
        anbindung.stoppe()
    return 0


def main() -> int:
    p = argparse.ArgumentParser(description="LFS-Anbindung fuer den ABS-Benchmark")
    g = p.add_mutually_exclusive_group(required=True)
    g.add_argument("--pruefen", action="store_true", help="Verbindung und Datenstrom pruefen")
    g.add_argument("--kalibrieren", action="store_true", help="Maus-Achsen einmessen")
    g.add_argument("--quellen", action="store_true",
                   help="Diagnose: welches Signal ist Vorderachs-, welches Grundgeschwindigkeit")
    g.add_argument("--fahren", action="store_true", help="Regelkreis starten")
    p.add_argument("--dauer", type=float, default=40.0,
                   help="Aufzeichnungsdauer fuer --quellen in Sekunden")
    args = p.parse_args()
    if args.pruefen:
        return _pruefen()
    if args.kalibrieren:
        return _kalibrieren()
    if args.quellen:
        return _quellen(args.dauer)
    return _fahren()


if __name__ == "__main__":
    # Beim Direktstart heisst dieses Modul "__main__". abs_regelung importiert aber
    # "lfs_link" — ohne diesen Alias wuerde die Datei ein zweites Mal geladen und es
    # gaebe zwei verschiedene Pedalstellung-Klassen.
    sys.modules.setdefault("lfs_link", sys.modules["__main__"])
    sys.exit(main())
