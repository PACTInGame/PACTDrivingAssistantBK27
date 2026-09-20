"""
lfs_link.py — Anbindung des Reglers an die Fahrsimulation *Live for Speed*.

╔══════════════════════════════════════════════════════════════════════════════╗
║  DIESE DATEI GEHOERT ZUR ANLAGE UND DARF NICHT VERAENDERT WERDEN.            ║
║  Sie ist hier, damit die Datentypen und die Einbettung der Reglerfunktion    ║
║  nachlesbar sind.                                                            ║
╚══════════════════════════════════════════════════════════════════════════════╝

Signalfluss
-----------
::

    Pfeiltasten ─► virtuelle Achsen ─► Fahrereingabe ─┐
                                                      ├─► berechne_pedalwerte
    LFS ──OutGauge/InSim──► Fahrzeugzustand ──────────┘            │
                                                                   ▼
                                       virtuelle Laengsachse ─► Mausposition ─► LFS

Die Steuerung laeuft ueber die **Maus-Achsen** von LFS: die X-Achse ist die
Lenkung, die Y-Achse ist die gemeinsame Gas-/Bremsachse. Der Pruefstand setzt
dazu die Cursorposition. Eine zusaetzliche Achsenhardware (vJoy o. ae.) wird
damit nicht gebraucht, aber es folgt eine Einschraenkung daraus, die fuer die
Auslegung des Reglers wichtig ist: **Gas und Bremse liegen auf derselben Achse
und koennen nicht gleichzeitig anliegen.** Die Bremse hat Vorrang.

Der Fahrer bedient den Pruefstand mit den **Pfeiltasten**; sie sind in LFS
absichtlich auf nichts gebunden. Der Pruefstand verrampt sie zu analogen
Achswerten — eine gehaltene Pfeiltaste ist also ein zunehmend durchgetretenes
Pedal, kein Schalter.

Telemetriequellen
-----------------
Es gibt genau zwei, und der Unterschied zwischen ihnen ist der Kern der Aufgabe:

* **InSim IS_MCI** — Geschwindigkeit **ueber Grund**, Position, Kurswinkel,
  Bewegungsrichtung und Gierrate. Diese Geschwindigkeit ist von den Raedern
  unabhaengig; sie bleibt richtig, auch wenn die Raeder stehen.
* **OutGauge** — die Anzeigewerte des Fahrzeugs. Die dort gemeldete
  Geschwindigkeit ist die **Radgeschwindigkeit der Hinterachse** (so liefert LFS
  sie), dazu Gang, Drehzahl und die Rueckmeldung, was von Gas, Bremse und
  Kupplung tatsaechlich am Fahrzeug angekommen ist.

Beide Werte werden **roh** durchgereicht. Der Pruefstand rechnet nichts aus
ihnen aus — kein Schlupf, keine Beschleunigung, keine Filter. Das ist Aufgabe
des Reglers.

Was es bewusst *nicht* gibt: einzelne Raddrehzahlen, radindividuellen
Bremsdruck, Lenkmoment. Eine Ansteuerung gibt es nur global.
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
from dataclasses import dataclass, asdict
from typing import Any, Dict, List, Optional, Tuple

# ``abs_regelung`` wird erst in Regelkreis.__init__ importiert: das Modul
# importiert seinerseits das Datenmodell aus dieser Datei, ein Import auf
# Modulebene waere also zirkulaer.

HIER = os.path.dirname(os.path.abspath(__file__))
KALIBRIERUNGSDATEI = os.path.join(HIER, "kalibrierung.json")


# ─── Konfiguration ────────────────────────────────────────────────────────────

@dataclass
class Konfiguration:
    """Betriebsparameter der Anlage. Einheiten stehen im Feldnamen."""

    # --- Netzwerk ---
    insim_host: str = "127.0.0.1"
    insim_port: int = 29999
    outgauge_port: int = 30000
    mci_intervall_ms: int = 50            # so schnell LFS IS_MCI liefert

    # --- Takt ---
    takt_ms: int = 50                     # Aufrufrate der Reglerfunktion
    max_paketalter_s: float = 0.30        # aeltere Telemetrie gilt als ungueltig

    # --- Fahrermodell: gehaltene Pfeiltaste -> Achswert ---
    pedal_anstieg_ms: int = 250           # 0 -> 100 % Gas/Bremse
    pedal_abfall_ms: int = 250            # 100 -> 0 % beim Loslassen
    lenkung_anstieg_ms: int = 900         # 0 -> Anschlag
    lenkung_ruecklauf_ms: int = 700       # zurueck zur Mitte

    # --- Maus-Achsen ---
    maus_rechteck: Optional[Tuple[int, int, int, int]] = None   # None = ganzer Bildschirm
    maus_rand_px: int = 40                # Sicherheitsrand, damit der Cursor nie klebt

    # --- Aufzeichnung ---
    puffer_takte: int = 120000            # ~100 min bei 50 ms


KONFIG = Konfiguration()


# ─── Datenmodell: genau das sieht die Reglerfunktion ──────────────────────────

@dataclass(frozen=True)
class Fahrereingabe:
    """Der Fahrerwunsch, aus den Pfeiltasten verrampt."""

    gas_prozent: float          # 0 .. 100   (Pfeil hoch)
    bremse_prozent: float       # 0 .. 100   (Pfeil runter)
    lenkung_prozent: float      # -100 (links) .. +100 (rechts), nur Information:
                                # die Lenkung wird unveraendert durchgereicht


@dataclass(frozen=True)
class Fahrzeugzustand:
    """Rohe Messwerte zum Aufrufzeitpunkt — ohne jede Vorverarbeitung.

    Die Felder sind nach ihrer Quelle benannt, weil die Quelle die Bedeutung
    entscheidet: ``v_ueber_grund_mps`` kommt aus IS_MCI und ist von den Raedern
    unabhaengig, ``v_rad_hinterachse_mps`` kommt aus OutGauge und ist die
    Radgeschwindigkeit der Hinterachse. Nur aus dem Unterschied der beiden ist
    Bremsschlupf ueberhaupt erkennbar.

    Es gibt keine Vorderrad-Drehzahl. Das Fahrzeug ist hinterradgetrieben; die
    Hinterachse ist damit die einzige Achse, deren Drehzahl gemeldet wird.
    """

    # --- Takt ---------------------------------------------------------------
    zeit_s: float                       # monotone Zeit seit Start des Regelkreises
    dt_s: float                         # tatsaechlicher Abstand zum letzten Aufruf

    # --- IS_MCI: Bewegung ueber Grund (Quelle: InSim, roh) -------------------
    v_ueber_grund_mps: float            # Betrag der Fahrzeuggeschwindigkeit
    kurswinkel_rad: float               # wohin das Fahrzeug zeigt; 0 = Norden (+Y),
                                        # gegen den Uhrzeigersinn wachsend
    bewegungsrichtung_rad: float        # wohin es sich bewegt, gleiche Kodierung.
                                        # Nur aussagekraeftig bei v > 0
    gierrate_rad_s: float               # positiv = Drehung nach links
    position_m: Tuple[float, float, float]      # (x, y, z) in Weltkoordinaten
    mci_alter_s: float                  # Alter des juengsten IS_MCI-Pakets

    # --- OutGauge: Anzeigewerte des Fahrzeugs (Quelle: OutGauge, roh) --------
    v_rad_hinterachse_mps: float        # OutGauge.Speed = Radgeschwindigkeit hinten
    motordrehzahl_rpm: float            # OutGauge.RPM
    gang: int                           # 0 = R, 1 = N, 2 = 1. Gang, ...
    ladedruck_bar: float                # OutGauge.Turbo
    motortemperatur_c: float
    kraftstoff_norm: float              # 0 .. 1
    oeldruck_bar: float
    oeltemperatur_c: float
    gas_ist_norm: float                 # 0 .. 1, was bei LFS ankam
    bremse_ist_norm: float              # 0 .. 1, was bei LFS ankam
    kupplung_ist_norm: float            # 0 .. 1
    handbremse_an: bool                 # Kontrollleuchte Handbremse, leuchtet = gezogen
    leuchten_verfuegbar: int            # Rohbitfeld OutGauge.DashLights: welche
                                        # Kontrollleuchten dieses Fahrzeug ueberhaupt hat
    leuchten_an: int                    # Rohbitfeld OutGauge.ShowLights: welche gerade
                                        # leuchten. Vorsicht bei DL_ABS und DL_TC — die
                                        # Leuchte steht fuer "aktiv ODER abgeschaltet"
                                        # und ist damit kein Zustandssignal
    fahrzeug: str                       # z. B. "XRG"
    outgauge_alter_s: float             # Alter des juengsten OutGauge-Pakets

    # --- Guete --------------------------------------------------------------
    daten_gueltig: bool                 # frisches IS_MCI *und* OutGauge vorhanden.
                                        # False = alle Messwerte oben sind 0
    mci_gueltig: bool                   # IS_MCI allein ist frisch
    outgauge_gueltig: bool              # OutGauge allein ist frisch

    # --- Rueckmeldung der Anlage --------------------------------------------
    letzte_ausgabe_gas_prozent: float   # eigene Ausgabe des letzten Takts
    letzte_ausgabe_bremse_prozent: float


@dataclass
class Pedalstellung:
    """Rueckgabewert der Reglerfunktion — die virtuellen Pedale."""

    gas_prozent: float          # 0 .. 100
    bremse_prozent: float       # 0 .. 100


# ─── OutGauge ─────────────────────────────────────────────────────────────────

# OutGauge-Anzeigeleuchten (LFS OutGaugePack.txt, DL_*)
DL_SHIFT, DL_FULLBEAM, DL_HANDBRAKE, DL_PITSPEED = 1 << 0, 1 << 1, 1 << 2, 1 << 3
DL_TC, DL_SIGNAL_L, DL_SIGNAL_R, DL_SIGNAL_ANY = 1 << 4, 1 << 5, 1 << 6, 1 << 7
DL_OILWARN, DL_BATTERY, DL_ABS = 1 << 8, 1 << 9, 1 << 10


class OutGaugePaket:
    """Ein OutGauge-Datagramm, 92 oder 96 Byte."""

    _s = struct.Struct('<I3sxH2B7f2I3f15sx15sx')   # 92 Byte, ohne optionale ID

    #: ``DashLights`` sind die Kontrollleuchten, die das Fahrzeug *hat*,
    #: ``ShowLights`` die, die gerade *leuchten* — in dieser Reihenfolge im Paket.
    __slots__ = ("zeit_ms", "fahrzeug", "flags", "gang", "plid", "geschwindigkeit_mps",
                 "drehzahl", "turbo", "motortemperatur", "kraftstoff", "oeldruck",
                 "oeltemperatur", "leuchten_verfuegbar", "leuchten_an", "gas", "bremse",
                 "kupplung")

    @classmethod
    def entpacke(cls, daten: bytes) -> Optional["OutGaugePaket"]:
        if len(daten) not in (92, 96):
            return None
        p = cls()
        (p.zeit_ms, fahrzeug, p.flags, p.gang, p.plid, p.geschwindigkeit_mps,
         p.drehzahl, p.turbo, p.motortemperatur, p.kraftstoff, p.oeldruck,
         p.oeltemperatur, p.leuchten_verfuegbar, p.leuchten_an, p.gas, p.bremse,
         p.kupplung, _d1, _d2) = cls._s.unpack(daten[:92])
        p.fahrzeug = fahrzeug.decode("latin-1", "ignore").strip("\x00")
        return p


class OutGaugeEmpfaenger:
    """Haelt das juengste OutGauge-Paket. Ein eigener Thread, kein Puffer.

    Der Regelkreis *tastet ab*, er verarbeitet keine Paketfolge — damit sind
    Paketrate und Regeltakt entkoppelt.
    """

    def __init__(self, konfig: Konfiguration = KONFIG):
        self.konfig = konfig
        self._sperre = threading.Lock()
        self._paket: Optional[OutGaugePaket] = None
        self._zeit = 0.0
        self._stop = threading.Event()
        self._thread: Optional[threading.Thread] = None
        self.pakete = 0
        self.fehlerhafte_pakete = 0
        self.bindefehler: Optional[str] = None

    def starte(self) -> None:
        self._stop.clear()
        self._thread = threading.Thread(target=self._empfange, daemon=True, name="outgauge")
        self._thread.start()

    def stoppe(self) -> None:
        self._stop.set()
        if self._thread:
            self._thread.join(timeout=1.5)

    def _empfange(self) -> None:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        s.settimeout(0.5)
        try:
            s.bind(("127.0.0.1", self.konfig.outgauge_port))
        except OSError as e:
            self.bindefehler = str(e)
            print(f"[lfs_link] FEHLER: UDP-Port {self.konfig.outgauge_port} nicht belegbar: {e}")
            print("           Laeuft noch ein anderer Empfaenger (Add-on, alter Lauf)?")
            return
        while not self._stop.is_set():
            try:
                daten, _ = s.recvfrom(1024)
            except socket.timeout:
                continue
            except OSError:
                break
            paket = OutGaugePaket.entpacke(daten)
            if paket is None:
                self.fehlerhafte_pakete += 1
                continue
            with self._sperre:
                self._paket, self._zeit = paket, time.monotonic()
                self.pakete += 1
        s.close()

    def lies(self) -> Tuple[Optional[OutGaugePaket], float]:
        with self._sperre:
            return self._paket, self._zeit


# ─── InSim ────────────────────────────────────────────────────────────────────

ISP_ISI, ISP_VER, ISP_TINY, ISP_SMALL, ISP_STA, ISP_MST, ISP_MCI = 1, 2, 3, 4, 5, 13, 38
TINY_NONE, TINY_PING, TINY_REPLY, TINY_SST = 0, 3, 4, 7
INSIM_VERSION = 9
ISF_LOCAL, ISF_MCI = 4, 32
ISS_GAME, ISS_REPLAY, ISS_PAUSED, ISS_DIALOG = 1, 2, 4, 16
ISS_FRONT_END, ISS_VISIBLE, ISS_TEXT_ENTRY = 256, 16384, 32768


@dataclass
class LfsZustand:
    """Ausschnitt aus IS_STA — reicht, um zu wissen, ob gefahren werden darf."""

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
        """Nur dann darf die Maus als Fahrzeugachse benutzt werden.

        Solange noch kein IS_STA angekommen ist, gilt bewusst *nicht*
        fahrbereit: waehrend der Menuefuehrung eines Szenarios gehoert die Maus
        dem Abspieler, und ein Cursor, den zwei Stellen gleichzeitig setzen,
        trifft keinen Menuepunkt mehr.
        """
        if not self.sta_empfangen:
            return False
        return (self.verbunden and self.im_spiel
                and not self.dialog_offen and not self.pausiert)


class InSimVerbindung:
    """Minimaler InSim-Client: Handshake, Keepalive, Kommandos, IS_STA, IS_MCI.

    Bewusst ohne Fremdbibliothek, damit der Arbeitsbereich aus zwei Dateien
    besteht und nichts nachinstalliert werden muss ausser ``pynput``.
    """

    _isi = struct.Struct('<4B2HBcH15sx15sx')    # 44 Byte
    _tiny = struct.Struct('<4B')                # 4 Byte
    _mst = struct.Struct('<4B63sx')             # 68 Byte
    _sta = struct.Struct('<4BfH10B5sx2B')       # 28 Byte
    _compcar = struct.Struct('<2H4B3i3Hh')      # 28 Byte je Fahrzeug in IS_MCI

    def __init__(self, konfig: Konfiguration = KONFIG):
        self.konfig = konfig
        self.zustand = LfsZustand()
        self.mci: Optional[Dict[str, Any]] = None
        self.mci_zeit = 0.0
        self.mci_plid = 0                      # 0 = erstes Fahrzeug im Paket
        self.mci_pakete = 0
        self._sock: Optional[socket.socket] = None
        self._stop = threading.Event()
        self._thread: Optional[threading.Thread] = None
        self._sendesperre = threading.Lock()

    def verbinde(self, zeitlimit_s: float = 5.0) -> bool:
        try:
            self._sock = socket.create_connection(
                (self.konfig.insim_host, self.konfig.insim_port), timeout=zeitlimit_s)
            self._sock.settimeout(0.5)
        except OSError as e:
            print(f"[lfs_link] InSim nicht erreichbar ({self.konfig.insim_port}): {e}")
            print("           In LFS '/insim 29999' eingeben oder in autoexec.lfs eintragen.")
            return False

        paket = self._isi.pack(self._isi.size // 4, ISP_ISI, 1, 0, 0,
                               ISF_LOCAL | ISF_MCI, INSIM_VERSION, b'!',
                               max(0, self.konfig.mci_intervall_ms), b'', b'ABS-Bench')
        self._sock.sendall(paket)
        self.zustand.verbunden = True
        self._stop.clear()
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
        """Sendet einen LFS-Befehl, z. B. '/restart'."""
        text = kommando.encode("latin-1", "ignore")[:63]
        self._sende(self._mst.pack(self._mst.size // 4, ISP_MST, 0, 0, text))

    def frage_zustand_ab(self) -> None:
        self._sende(self._tiny.pack(1, ISP_TINY, 255, TINY_SST))

    def _lies_schleife(self) -> None:
        puffer = b""
        while not self._stop.is_set():
            try:
                teil = self._sock.recv(8192)
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
            # Keepalive: ohne Antwort schliesst LFS die Verbindung.
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
        """Liest den CompCar-Eintrag des eigenen Fahrzeugs.

        Einheiten laut InSim.txt:
          Speed     word,  32768 = 100 m/s
          AngVel    short, 16384 = 360 Grad/s, gegen den Uhrzeigersinn
          Heading   word,  0 = Norden (+Y), gegen den Uhrzeigersinn
          Direction word,  gleiche Kodierung, Bewegungsrichtung
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
            self.mci_pakete += 1
            return


# ─── Virtuelle Achsen ─────────────────────────────────────────────────────────

class VirtuelleAchsen:
    """Die beiden Achsen, mit denen das Fahrzeug gestellt wird.

    ============  =========  ==========================================
    Achse         Bereich    Wirkung in LFS
    ============  =========  ==========================================
    ``laengs``    -1 .. +1   +1 = Vollgas, -1 = Vollbremsung; Maus-Y
    ``quer``      -1 .. +1   -1 = voll links, +1 = voll rechts; Maus-X
    ============  =========  ==========================================

    Beide Achsen werden als **Cursorposition** ausgegeben — LFS liest die Maus
    als analoge Achse. Daraus folgt die harte Einschraenkung der Anlage: Gas und
    Bremse teilen sich eine Achse. ``aus_pedalen`` bildet eine Pedalstellung auf
    ``laengs`` ab und gibt der Bremse dabei Vorrang.

    Die Zuordnung Position -> Achswert wird nicht angenommen, sondern mit
    ``kalibriere()`` gemessen und in ``kalibrierung.json`` abgelegt.
    """

    def __init__(self, konfig: Konfiguration = KONFIG):
        self.konfig = konfig
        self.benutzer32 = ctypes.windll.user32 if sys.platform == "win32" else None
        x0, y0, x1, y1 = self._rechteck()
        self.x_mitte = (x0 + x1) / 2.0
        self.y_mitte = (y0 + y1) / 2.0
        self.x_spanne = (x1 - x0) / 2.0
        self.y_spanne = (y1 - y0) / 2.0
        self.x_richtung = 1.0
        self.y_richtung = -1.0      # kleineres y = Gas (Standardannahme)
        self.aktiv = False          # erst wenn LFS fahrbereit meldet
        self.laengs = 0.0
        self.quer = 0.0
        self.kalibriert = self.lade_kalibrierung()

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

    # -- Stellen ------------------------------------------------------------

    @staticmethod
    def aus_pedalen(gas_prozent: float, bremse_prozent: float) -> float:
        """Pedalstellung -> Laengsachse. Die Bremse hat Vorrang."""
        gas = max(0.0, min(1.0, gas_prozent / 100.0))
        bremse = max(0.0, min(1.0, bremse_prozent / 100.0))
        return -bremse if bremse > 0.0 else gas

    def setze(self, laengs: float, quer: float) -> None:
        self.laengs = max(-1.0, min(1.0, laengs))
        self.quer = max(-1.0, min(1.0, quer))
        if not self.aktiv or self.benutzer32 is None:
            return
        x = self.x_mitte + self.x_richtung * self.quer * self.x_spanne
        y = self.y_mitte + self.y_richtung * self.laengs * self.y_spanne
        self.benutzer32.SetCursorPos(int(round(x)), int(round(y)))

    def neutral(self, erzwingen: bool = False) -> None:
        """Pedale los, Lenkung gerade — auf jedem Ausstiegspfad."""
        self.laengs = 0.0
        self.quer = 0.0
        if self.benutzer32 is None or (not self.aktiv and not erzwingen):
            return
        self.benutzer32.SetCursorPos(int(round(self.x_mitte)), int(round(self.y_mitte)))

    def kalibriere(self, outgauge: OutGaugeEmpfaenger, schritte: int = 21,
                   wartezeit_s: float = 0.20) -> bool:
        """Faehrt beide Achsen ab und misst, was LFS daraus macht.

        Voraussetzung: Fahrzeug steht fahrbereit auf der Strecke, LFS im
        Vordergrund, Fenster- oder randloser Vollbildmodus.
        """
        if self.benutzer32 is None:
            print("[lfs_link] Kalibrierung nur unter Windows moeglich.")
            return False

        war_aktiv, self.aktiv = self.aktiv, True
        try:
            x0, y0, x1, y1 = self._rechteck()
            messwerte: List[Tuple[float, float, float]] = []
            print("[lfs_link] Kalibriere Laengsachse (Maus Y, Gas/Bremse) ...")
            for i in range(schritte):
                y = y0 + (y1 - y0) * i / (schritte - 1)
                self.benutzer32.SetCursorPos(int(round(self.x_mitte)), int(round(y)))
                time.sleep(wartezeit_s)
                og, og_zeit = outgauge.lies()
                if og is None or time.monotonic() - og_zeit > 0.5:
                    print("[lfs_link] Keine frischen OutGauge-Daten — abgebrochen.")
                    return False
                messwerte.append((y, og.gas, og.bremse))

            gas_werte = [(y, g) for y, g, _ in messwerte if g > 0.9]
            bremse_werte = [(y, b) for y, _, b in messwerte if b > 0.9]
            neutral_werte = [y for y, g, b in messwerte if g < 0.02 and b < 0.02]
            if not gas_werte or not bremse_werte or not neutral_werte:
                print("[lfs_link] LFS hat auf die Mausbewegung nicht reagiert.")
                print("           Pruefen: Steuerung = Maus, Y-Achse = Gas/Bremse,")
                print("           LFS im Vordergrund, kein exklusives Vollbild.")
                return False

            y_gas = sum(y for y, _ in gas_werte) / len(gas_werte)
            y_bremse = sum(y for y, _ in bremse_werte) / len(bremse_werte)
            self.y_mitte = sum(neutral_werte) / len(neutral_werte)
            self.y_spanne = abs(y_bremse - y_gas) / 2.0
            self.y_richtung = -1.0 if y_bremse > y_gas else 1.0

            print("[lfs_link] Kalibriere Querachse (Maus X, Lenkung) ...")
            # Die Lenkrueckmeldung steht nicht in OutGauge. Die X-Achse wird
            # deshalb geometrisch aus dem Rechteck bestimmt und nur auf
            # Plausibilitaet geprueft: LFS spiegelt die Bildschirmmitte.
            self.x_mitte = (x0 + x1) / 2.0
            self.x_spanne = (x1 - x0) / 2.0
            self.x_richtung = 1.0

            self.neutral(erzwingen=True)
            self.speichere_kalibrierung()
            self.kalibriert = True
            print(f"[lfs_link] Kalibrierung gespeichert: {KALIBRIERUNGSDATEI}")
            print(f"           Laengs: Mitte {self.y_mitte:.0f}, Spanne {self.y_spanne:.0f}, "
                  f"Richtung {self.y_richtung:+.0f}")
            print(f"           Quer:   Mitte {self.x_mitte:.0f}, Spanne {self.x_spanne:.0f}")
            return True
        finally:
            self.aktiv = war_aktiv


# ─── Fahrereingabe ────────────────────────────────────────────────────────────

class Pfeiltasteneingabe:
    """Wandelt gehaltene Pfeiltasten in verrampte Achswerte 0..100 %.

    ================  ====================================
    Taste             Wirkung
    ================  ====================================
    ``Pfeil hoch``    Gas — Maus faehrt nach oben
    ``Pfeil runter``  Bremse — Maus faehrt nach unten
    ``Pfeil links``   Lenkung links — Maus faehrt nach links
    ``Pfeil rechts``  Lenkung rechts — Maus faehrt nach rechts
    ================  ====================================

    Die Rampen bilden nach, was ein Fahrer mit einem echten Pedal macht: eine
    gehaltene Taste ist ein Pedal, das in ``pedal_anstieg_ms`` durchgetreten
    wird. Die Reglerfunktion sieht ausschliesslich das Ergebnis, nie einen
    Tastenzustand.

    Die Pfeiltasten sind in LFS auf nichts gebunden. Sie werden global
    abgegriffen, gehen also an LFS vorbei — das ist der Grund, warum ein
    aufgezeichnetes Szenario waehrend der Fahrt Tasten und nicht die Maus
    abspielen darf: die Maus ist waehrenddessen die Fahrzeugachse.
    """

    TASTEN = ("up", "down", "left", "right")

    def __init__(self, konfig: Konfiguration = KONFIG):
        self.konfig = konfig
        self._gedrueckt: Dict[str, bool] = {t: False for t in self.TASTEN}
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

        namen = {keyboard.Key.up: "up", keyboard.Key.down: "down",
                 keyboard.Key.left: "left", keyboard.Key.right: "right"}

        def bei_druck(taste):
            name = namen.get(taste)
            if name:
                with self._sperre:
                    self._gedrueckt[name] = True

        def bei_loslassen(taste):
            name = namen.get(taste)
            if name:
                with self._sperre:
                    self._gedrueckt[name] = False

        self._listener = keyboard.Listener(on_press=bei_druck, on_release=bei_loslassen)
        self._listener.daemon = True
        self._listener.start()

    def stoppe(self) -> None:
        if self._listener:
            self._listener.stop()
            self._listener = None

    @staticmethod
    def _rampe(ist: float, ziel: float, dt_s: float, anstieg_ms: int, abfall_ms: int) -> float:
        """Bewegt ``ist`` mit begrenzter Rate in Richtung ``ziel`` (Bereich 100)."""
        dauer_ms = anstieg_ms if abs(ziel) > abs(ist) else abfall_ms
        schritt = 100.0 * dt_s * 1000.0 / max(1, dauer_ms)
        if ziel > ist:
            return min(ziel, ist + schritt)
        return max(ziel, ist - schritt)

    def aktualisiere(self, dt_s: float) -> Fahrereingabe:
        with self._sperre:
            hoch = self._gedrueckt["up"]
            runter = self._gedrueckt["down"]
            links = self._gedrueckt["left"]
            rechts = self._gedrueckt["right"]
        # Beide Pedaltasten gleichzeitig: die Bremse gewinnt (eine Achse).
        gas_ziel = 100.0 if (hoch and not runter) else 0.0
        bremse_ziel = 100.0 if runter else 0.0
        # Beide Lenktasten gleichzeitig = geradeaus.
        lenk_ziel = 0.0 if links == rechts else (-100.0 if links else 100.0)

        self.gas = self._rampe(self.gas, gas_ziel, dt_s,
                               self.konfig.pedal_anstieg_ms, self.konfig.pedal_abfall_ms)
        self.bremse = self._rampe(self.bremse, bremse_ziel, dt_s,
                                  self.konfig.pedal_anstieg_ms, self.konfig.pedal_abfall_ms)
        self.lenkung = self._rampe(self.lenkung, lenk_ziel, dt_s,
                                   self.konfig.lenkung_anstieg_ms,
                                   self.konfig.lenkung_ruecklauf_ms)
        return Fahrereingabe(gas_prozent=self.gas, bremse_prozent=self.bremse,
                             lenkung_prozent=self.lenkung)


# ─── Regelkreis ───────────────────────────────────────────────────────────────

@dataclass
class Takt:
    """Eine Zeile der Aufzeichnung — ein Aufruf der Reglerfunktion."""

    t_mono: float
    zustand: Dict[str, Any]
    fahrer_gas: float
    fahrer_bremse: float
    fahrer_lenkung: float
    ausgabe_gas: float
    ausgabe_bremse: float
    achse_laengs: float
    achse_quer: float
    laufzeit_ms: float
    fehler: Optional[str] = None


class Regelkreis:
    """Der 50-ms-Takt: Messen -> Regeln -> Stellen -> Aufzeichnen."""

    def __init__(self, insim: InSimVerbindung, outgauge: OutGaugeEmpfaenger,
                 achsen: VirtuelleAchsen, eingabe: Pfeiltasteneingabe,
                 konfig: Konfiguration = KONFIG):
        self.insim = insim
        self.outgauge = outgauge
        self.achsen = achsen
        self.eingabe = eingabe
        self.konfig = konfig

        # Die Kandidatenfunktion wird beim Aufbau geladen, nicht im ersten
        # Regeltakt: ein Syntaxfehler soll den Start abbrechen, nicht die Fahrt.
        import abs_regelung
        self.reglerfunktion = abs_regelung.berechne_pedalwerte

        self.takte: List[Takt] = []
        self._taktsperre = threading.Lock()
        self._stop = threading.Event()
        self._thread: Optional[threading.Thread] = None
        self._reglerzustand: Dict[str, Any] = {}
        self._t0 = 0.0
        self._t_letzt = 0.0
        self._letzte_ausgabe = (0.0, 0.0)
        self.zyklen = 0
        self.ueberlaeufe = 0
        self.reglerfehler = 0
        self.laufzeit_max_ms = 0.0
        self._fehler_gemeldet = False
        self._achsen_gemeldet = False
        self._achsen_verdacht_s = 0.0

    # -- Lebenszyklus -------------------------------------------------------

    def starte(self) -> None:
        _erhoehe_timeraufloesung()
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
        _setze_timeraufloesung_zurueck()

    def setze_regler_zurueck(self) -> None:
        """Vor jedem Fahrversuch: der Regler startet ohne Vorgeschichte."""
        self._reglerzustand = {}

    def hole_takte(self) -> List[Takt]:
        with self._taktsperre:
            return list(self.takte)

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
            except Exception:       # Der Regelkreis darf niemals still sterben.
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

        # --- Reglerfunktion: die einzige Datei, die veraendert werden darf ---
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
                print("[lfs_link] Ausnahme in berechne_pedalwerte — "
                      "Rueckfall auf Durchreichen:")
                traceback.print_exc()
        laufzeit_ms = (time.perf_counter() - beginn) * 1000.0
        self.laufzeit_max_ms = max(self.laufzeit_max_ms, laufzeit_ms)

        # --- Stellen ---
        self.achsen.aktiv = self.insim.zustand.fahrbereit
        if self.achsen.aktiv:
            self.achsen.setze(VirtuelleAchsen.aus_pedalen(gas, bremse),
                              fahrer.lenkung_prozent / 100.0)
        self._letzte_ausgabe = (gas, bremse)
        self._pruefe_stellweg(zustand, bremse, dt)

        # --- Aufzeichnen (im Speicher; geschrieben wird erst am Ende) ---
        with self._taktsperre:
            if len(self.takte) < self.konfig.puffer_takte:
                self.takte.append(Takt(
                    t_mono=jetzt, zustand=asdict(zustand),
                    fahrer_gas=fahrer.gas_prozent, fahrer_bremse=fahrer.bremse_prozent,
                    fahrer_lenkung=fahrer.lenkung_prozent,
                    ausgabe_gas=gas, ausgabe_bremse=bremse,
                    achse_laengs=self.achsen.laengs, achse_quer=self.achsen.quer,
                    laufzeit_ms=laufzeit_ms, fehler=fehler))

    @staticmethod
    def _pruefe_ausgabe(ausgabe: Any) -> Tuple[float, float]:
        try:
            gas = float(ausgabe.gas_prozent)
            bremse = float(ausgabe.bremse_prozent)
        except (AttributeError, TypeError, ValueError):
            raise TypeError("berechne_pedalwerte muss eine Pedalstellung mit den Feldern "
                            f"gas_prozent und bremse_prozent liefern, geliefert wurde "
                            f"{ausgabe!r}")
        if not (math.isfinite(gas) and math.isfinite(bremse)):
            raise ValueError("Pedalwert ist NaN oder unendlich")
        return max(0.0, min(100.0, gas)), max(0.0, min(100.0, bremse))

    def _pruefe_stellweg(self, zustand: Fahrzeugzustand, bremse_soll: float,
                         dt: float) -> None:
        """Meldet, wenn LFS die Maus-Achse offensichtlich nicht annimmt."""
        if bremse_soll > 50.0 and zustand.daten_gueltig and zustand.bremse_ist_norm < 0.05:
            self._achsen_verdacht_s += dt
        else:
            self._achsen_verdacht_s = 0.0
        if self._achsen_verdacht_s > 0.5 and not self._achsen_gemeldet:
            self._achsen_gemeldet = True
            print("[lfs_link] WARNUNG: Bremse angefordert, LFS meldet aber keine Bremse. "
                  "Achsen pruefen (--kalibrieren), LFS in den Vordergrund holen.")

    # -- Messgroessen -------------------------------------------------------

    def _baue_zustand(self, jetzt: float, dt: float) -> Fahrzeugzustand:
        """Traegt die Rohwerte beider Quellen zusammen. Es wird nichts gerechnet."""
        og, og_zeit = self.outgauge.lies()
        mci, mci_zeit = self.insim.mci, self.insim.mci_zeit

        og_alter = jetzt - og_zeit if og is not None else 1e9
        mci_alter = jetzt - mci_zeit if mci is not None else 1e9
        og_gueltig = og is not None and og_alter <= self.konfig.max_paketalter_s
        mci_gueltig = mci is not None and mci_alter <= self.konfig.max_paketalter_s

        if not (og_gueltig and mci_gueltig):
            # Ungueltige Daten werden nicht kaschiert: Nullen plus daten_gueltig=False.
            return Fahrzeugzustand(
                zeit_s=jetzt - self._t0, dt_s=dt,
                v_ueber_grund_mps=0.0, kurswinkel_rad=0.0, bewegungsrichtung_rad=0.0,
                gierrate_rad_s=0.0, position_m=(0.0, 0.0, 0.0),
                mci_alter_s=min(mci_alter, 999.0),
                v_rad_hinterachse_mps=0.0, motordrehzahl_rpm=0.0, gang=0,
                ladedruck_bar=0.0, motortemperatur_c=0.0, kraftstoff_norm=0.0,
                oeldruck_bar=0.0, oeltemperatur_c=0.0,
                gas_ist_norm=0.0, bremse_ist_norm=0.0, kupplung_ist_norm=0.0,
                handbremse_an=False, leuchten_verfuegbar=0, leuchten_an=0,
                fahrzeug=og.fahrzeug if og is not None else "",
                outgauge_alter_s=min(og_alter, 999.0),
                daten_gueltig=False, mci_gueltig=mci_gueltig,
                outgauge_gueltig=og_gueltig,
                letzte_ausgabe_gas_prozent=self._letzte_ausgabe[0],
                letzte_ausgabe_bremse_prozent=self._letzte_ausgabe[1])

        return Fahrzeugzustand(
            zeit_s=jetzt - self._t0, dt_s=dt,
            # IS_MCI, roh
            v_ueber_grund_mps=mci["v_mps"],
            kurswinkel_rad=mci["kurs_rad"],
            bewegungsrichtung_rad=mci["richtung_rad"],
            gierrate_rad_s=mci["gierrate_rad_s"],
            position_m=mci["position_m"],
            mci_alter_s=mci_alter,
            # OutGauge, roh
            v_rad_hinterachse_mps=og.geschwindigkeit_mps,
            motordrehzahl_rpm=og.drehzahl,
            gang=og.gang,
            ladedruck_bar=og.turbo,
            motortemperatur_c=og.motortemperatur,
            kraftstoff_norm=og.kraftstoff,
            oeldruck_bar=og.oeldruck,
            oeltemperatur_c=og.oeltemperatur,
            gas_ist_norm=og.gas,
            bremse_ist_norm=og.bremse,
            kupplung_ist_norm=og.kupplung,
            handbremse_an=bool(og.leuchten_an & DL_HANDBRAKE),
            leuchten_verfuegbar=og.leuchten_verfuegbar,
            leuchten_an=og.leuchten_an,
            fahrzeug=og.fahrzeug,
            outgauge_alter_s=og_alter,
            daten_gueltig=True, mci_gueltig=True, outgauge_gueltig=True,
            letzte_ausgabe_gas_prozent=self._letzte_ausgabe[0],
            letzte_ausgabe_bremse_prozent=self._letzte_ausgabe[1])


# ─── Windows-Timeraufloesung ──────────────────────────────────────────────────

def _erhoehe_timeraufloesung() -> None:
    """``time.sleep()`` ist unter Windows sonst auf ~15,6 ms gerastert.

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


# ─── Fassade ──────────────────────────────────────────────────────────────────

class LfsAnbindung:
    """Startet und stoppt alle Teile in der richtigen Reihenfolge."""

    def __init__(self, konfig: Konfiguration = KONFIG):
        self.konfig = konfig
        self.insim = InSimVerbindung(konfig)
        self.outgauge = OutGaugeEmpfaenger(konfig)
        self.achsen = VirtuelleAchsen(konfig)
        self.eingabe = Pfeiltasteneingabe(konfig)
        self.regelkreis: Optional[Regelkreis] = None

    def starte(self, mit_regelkreis: bool = True) -> bool:
        self.outgauge.starte()
        if not self.insim.verbinde():
            self.outgauge.stoppe()
            return False
        self.eingabe.starte()
        if mit_regelkreis:
            self.regelkreis = Regelkreis(self.insim, self.outgauge, self.achsen,
                                         self.eingabe, self.konfig)
            self.regelkreis.starte()
        return True

    def stoppe(self) -> None:
        if self.regelkreis:
            self.regelkreis.stoppe()
        self.eingabe.stoppe()
        self.insim.trenne()
        self.outgauge.stoppe()
        self.achsen.neutral(erzwingen=True)

    def __enter__(self) -> "LfsAnbindung":
        if not self.starte():
            raise RuntimeError("LFS-Anbindung konnte nicht gestartet werden.")
        return self

    def __exit__(self, *_) -> None:
        self.stoppe()

    def warte_auf_daten(self, zeitlimit_s: float = 10.0) -> bool:
        ende = time.monotonic() + zeitlimit_s
        while time.monotonic() < ende:
            og, og_zeit = self.outgauge.lies()
            if og is not None and self.insim.mci is not None:
                return True
            time.sleep(0.1)
        return False


# ─── Werkzeuge auf der Kommandozeile ──────────────────────────────────────────

def _pruefen() -> int:
    """Meldet, ob InSim, OutGauge und IS_MCI ankommen."""
    anbindung = LfsAnbindung()
    if not anbindung.starte(mit_regelkreis=False):
        return 1
    try:
        print("[lfs_link] Warte auf Telemetrie (Fahrzeug muss auf der Strecke stehen) ...")
        ok = anbindung.warte_auf_daten(10.0)
        og, _ = anbindung.outgauge.lies()
        mci = anbindung.insim.mci
        print(f"  InSim      : {'verbunden' if anbindung.insim.zustand.verbunden else 'FEHLT'}"
              f"  (Strecke {anbindung.insim.zustand.strecke or '?'})")
        print(f"  OutGauge   : {'ok' if og else 'FEHLT'}"
              f"  ({anbindung.outgauge.pakete} Pakete"
              f"{', Fahrzeug ' + og.fahrzeug if og else ''})")
        print(f"  IS_MCI     : {'ok' if mci else 'FEHLT'}"
              f"  ({anbindung.insim.mci_pakete} Pakete)")
        print(f"  Achsen     : {'kalibriert' if anbindung.achsen.kalibriert else 'NICHT kalibriert'}")
        if og and mci:
            print(f"  v Grund    : {mci['v_mps']:6.2f} m/s   (IS_MCI)")
            print(f"  v Rad hinten: {og.geschwindigkeit_mps:6.2f} m/s   (OutGauge)")
        if not ok:
            print("\n  Keine vollstaendige Telemetrie. Pruefen:")
            print("   * cfg.txt: OutGauge Mode 2, Delay 1, IP 127.0.0.1, Port 30000")
            print("   * LFS: Fahrzeug auf der Strecke (OutGauge steht in der Box still)")
            print("   * InSim-UDP: ohne UDP-Socket auf 29999 kommt kein IS_MCI an")
        return 0 if ok else 1
    finally:
        anbindung.stoppe()


def _kalibrieren() -> int:
    anbindung = LfsAnbindung()
    if not anbindung.starte(mit_regelkreis=False):
        return 1
    try:
        if not anbindung.warte_auf_daten(10.0):
            print("[lfs_link] Keine Telemetrie — Kalibrierung nicht moeglich.")
            return 1
        print("[lfs_link] LFS jetzt in den Vordergrund holen. Start in 5 s ...")
        time.sleep(5.0)
        return 0 if anbindung.achsen.kalibriere(anbindung.outgauge) else 1
    finally:
        anbindung.stoppe()


def _quellen(dauer_s: float = 40.0) -> int:
    """Belegt, dass OutGauge.Speed die Radgeschwindigkeit ist, nicht die ueber Grund.

    Beschleunigen, dann Vollbremsung bis zum Blockieren (LFS-Fahrhilfen aus).
    Nur dort trennen sich Rad- und Grundgeschwindigkeit; im Rollen sind sie
    gleich und beweisen nichts.
    """
    anbindung = LfsAnbindung()
    if not anbindung.starte(mit_regelkreis=False):
        return 1
    try:
        if not anbindung.warte_auf_daten(10.0):
            print("[lfs_link] Keine Telemetrie.")
            return 1
        print(f"[lfs_link] Zeichne {dauer_s:.0f} s auf. Beschleunigen, dann Vollbremsung "
              "bis zum Blockieren.")
        zeilen: List[Tuple[float, float, float, float]] = []
        ende = time.monotonic() + dauer_s
        while time.monotonic() < ende:
            og, og_zeit = anbindung.outgauge.lies()
            mci = anbindung.insim.mci
            if og and mci:
                zeilen.append((time.monotonic(), mci["v_mps"],
                               og.geschwindigkeit_mps, og.bremse))
            time.sleep(0.05)

        bremsend = [z for z in zeilen if z[3] > 0.5 and z[1] > 5.0]
        if not bremsend:
            print("[lfs_link] Keine Bremsung mit Tempo aufgezeichnet — nichts zu vergleichen.")
            return 1
        max_abweichung = max((z[1] - z[2]) / z[1] for z in bremsend)
        rollend = [z for z in zeilen if z[3] < 0.02 and z[1] > 10.0]
        roll_abweichung = (max(abs(z[1] - z[2]) / z[1] for z in rollend)
                           if rollend else float("nan"))
        print(f"\n  Abtastpunkte gesamt        : {len(zeilen)}")
        print(f"  davon bremsend ueber 5 m/s : {len(bremsend)}")
        print(f"  max. (v_Grund-v_Rad)/v_Grund beim Bremsen : {max_abweichung:+.3f}")
        print(f"  max. Abweichung im Rollen                 : {roll_abweichung:+.3f}")
        if max_abweichung > 0.15 and (math.isnan(roll_abweichung) or roll_abweichung < 0.05):
            print("\n  -> OutGauge.Speed ist eine RADgeschwindigkeit: im Rollen gleich der "
                  "Grundgeschwindigkeit,\n     beim Blockieren deutlich kleiner. So ist der "
                  "Benchmark ausgelegt.")
        else:
            print("\n  -> Kein klarer Unterschied. Entweder wurde nicht bis zum Blockieren "
                  "gebremst,\n     oder eine LFS-Fahrhilfe (ABS/Bremshilfe) ist noch an.")
        return 0
    finally:
        anbindung.stoppe()


def _fahren(dauer_s: float) -> int:
    """Faehrt den Regelkreis frei, ohne Szenario — zum Ausprobieren von Hand."""
    anbindung = LfsAnbindung()
    if not anbindung.starte(mit_regelkreis=True):
        return 1
    try:
        print(f"[lfs_link] Regelkreis laeuft ({dauer_s:.0f} s). Pfeiltasten steuern. "
              "Abbruch mit Strg+C.")
        ende = time.monotonic() + dauer_s
        while time.monotonic() < ende:
            time.sleep(0.5)
    except KeyboardInterrupt:
        pass
    finally:
        rk = anbindung.regelkreis
        anbindung.stoppe()
        if rk:
            print(f"[lfs_link] {rk.zyklen} Takte, {rk.ueberlaeufe} verpasst, "
                  f"{rk.reglerfehler} Reglerfehler, "
                  f"max. Reglerlaufzeit {rk.laufzeit_max_ms:.2f} ms")
    return 0


def main() -> int:
    p = argparse.ArgumentParser(description="Anbindung des ABS-Reglers an Live for Speed")
    g = p.add_mutually_exclusive_group(required=True)
    g.add_argument("--pruefen", action="store_true", help="Verbindung und Telemetrie pruefen")
    g.add_argument("--kalibrieren", action="store_true", help="Maus-Achsen einmessen")
    g.add_argument("--quellen", action="store_true",
                   help="belegen, dass OutGauge.Speed die Radgeschwindigkeit ist")
    g.add_argument("--fahren", type=float, nargs="?", const=120.0, metavar="SEKUNDEN",
                   help="Regelkreis frei laufen lassen")
    a = p.parse_args()
    if a.pruefen:
        return _pruefen()
    if a.kalibrieren:
        return _kalibrieren()
    if a.quellen:
        return _quellen()
    return _fahren(a.fahren)


if __name__ == "__main__":
    raise SystemExit(main())
