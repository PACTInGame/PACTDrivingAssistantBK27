import copy
import math
from dataclasses import dataclass

from lfs.text_encoding import decode_button_text

# IS_NPL PType-Bits (InSim.txt, reference/conventions.md §5.4).
# pyinsim kennt dafuer keine Konstanten.
PTYPE_FEMALE = 1
PTYPE_AI = 2
PTYPE_REMOTE = 4


def decode_car_name(value) -> str:
    """Dekodiert CName/Track byte-treu.

    Das sind reine ASCII-Codes (``XFG``) oder - bei Fahrzeug-Mods - eine
    beliebige Mod-ID (reference/conventions.md §4). ``latin-1`` ist hier
    richtig, weil es verlustfrei und umkehrbar ist: der Wert dient als
    Schluessel (z.B. in data/gearbox_calibrations.json).
    """
    if isinstance(value, (bytes, bytearray)):
        return bytes(value).split(b'\x00', 1)[0].decode('latin-1', errors='replace')
    if value is None:
        return ""
    return str(value)


def decode_player_name(value) -> str:
    """Dekodiert PName so, wie LFS ihn auf den Schirm bringt.

    Spielernamen tragen LFS-Codepage-Escapes (``^E``, ``^T`` …) und
    Farbcodes; ``decode_button_text`` ist die Umkehrung des Encoders aus
    lfs/text_encoding.py und liest genau diese Kodierung.
    """
    if isinstance(value, (bytes, bytearray)):
        return decode_button_text(bytes(value))
    if value is None:
        return ""
    return str(value)


def _encode_raw(value) -> bytes:
    """Behaelt die Rohbytes, falls doch einmal jemand sie braucht."""
    if isinstance(value, (bytes, bytearray)):
        return bytes(value)
    if value is None:
        return b""
    return str(value).encode('latin-1', errors='replace')


@dataclass
class VehicleData:
    """Container für Fahrzeugdaten - ein Schnappschuss *eines* MCI-Frames

    ``VehicleManager`` schreibt waehrend eines Frames in eine Kopie und tauscht
    das Objekt am Frame-Ende in einem Rutsch aus (``Vehicle.begin_frame`` /
    ``commit_frame``). Ein Assistenzsystem im Worker-Thread sieht deshalb nie
    ein halb aktualisiertes Fahrzeug (known-issues #12). Wer die Daten ueber
    mehrere Zeilen braucht, bindet ``data = vehicle.data`` einmal an eine
    lokale Variable - dann ist der Zustand garantiert konsistent.
    """
    player_id: int = 0
    x: float = 0.0
    y: float = 0.0
    z: float = 0.0
    speed: float = 0.0
    heading: float = 0.0
    direction: float = 0.0
    distance_to_player: float = 0.0
    angle_to_player: float = 0.0
    acceleration: float = 0.0
    # Namen kommen als bytes von LFS und werden genau einmal beim Eingang
    # dekodiert (reference/conventions.md §4). Die Rohbytes bleiben daneben
    # stehen, damit niemand sie zurueckrechnen muss.
    cname: str = "Unknown"
    pname: str = "Unknown"
    cname_bytes: bytes = b""
    pname_bytes: bytes = b""
    control_mode: int = 0
    # Identitaet aus IS_NPL - kameraunabhaengig (reference/conventions.md §5.4)
    ucid: int = -1          # -1 = unbekannt, 0 = lokale Verbindung / Host
    ptype: int = 0          # IS_NPL PType-Bitfeld
    is_ai: bool = False     # PType Bit 1 - die verlaessliche KI-Erkennung
    is_remote: bool = False  # PType Bit 2 - ein anderer Spieler im Netzwerk


class Vehicle:
    """Repräsentiert ein Fahrzeug auf der Strecke"""

    def __init__(self, player_id: int):
        self.data = VehicleData(player_id=player_id)
        # Waehrend eines MCI-Frames zeigt _staged auf die Arbeitskopie.
        self._staged = None
        self.last_update = 0
        self.previous_speed = 0.0
        self.current_route = None

    # ─── Frame-Handling ───────────────────────────────────────────────

    @property
    def _target(self) -> VehicleData:
        """Wohin die update_*-Methoden schreiben."""
        return self.data if self._staged is None else self._staged

    def begin_frame(self):
        """Oeffnet ein Frame: ab hier gehen alle Updates in eine Kopie.

        Kosten: ein ``copy.copy`` eines flachen Dataclass-Objekts pro Fahrzeug
        und Zyklus (<1 µs), also unter 40 µs bei 40 Autos.
        """
        if self._staged is None:
            self._staged = copy.copy(self.data)

    def commit_frame(self):
        """Veroeffentlicht das Frame - ein einzelner Attribut-Tausch."""
        if self._staged is not None:
            self.data = self._staged
            self._staged = None

    def abort_frame(self):
        """Verwirft ein offenes Frame, ohne es zu veroeffentlichen."""
        self._staged = None

    # ─── Updates ──────────────────────────────────────────────────────

    def update_position(self, x: float, y: float, z: float, heading: float,
                        direction: float, speed: float):
        """Aktualisiert Position und Bewegungsdaten"""
        target = self._target
        target.x = x
        target.y = y
        target.z = z
        target.heading = heading
        target.direction = direction

        # Berechne Beschleunigung
        target.speed = speed
        target.acceleration = (speed - self.previous_speed) * 2.778  # Umrechnung von km/h auf m/s²
        self.previous_speed = speed

    def update_distance_to_player(self, player_x: float, player_y: float, player_z: float):
        """Berechnet Distanz zum Spieler"""
        target = self._target
        dx = target.x - player_x
        dy = target.y - player_y
        dz = target.z - player_z
        dx = dx / 65536
        dy = dy / 65536
        dz = dz / 65536
        target.distance_to_player = math.sqrt(dx * dx + dy * dy + dz * dz)

    def update_angle_to_player(self, player_x: float, player_y: float, own_heading: float):
        """Berechnet Winkel zum Spieler"""
        target = self._target
        ang = (math.atan2((player_x / 65536 - target.x / 65536),
                          (player_y / 65536 - target.y / 65536)) * 180.0) / 3.1415926535897931
        if ang < 0.0:
            ang = 360.0 + ang
        consider_dir = ang + own_heading / 182
        if consider_dir > 360.0:
            consider_dir -= 360.0
        angle = (consider_dir + 180.0) % 360.0
        target.angle_to_player = angle

    def update_model_and_driver(self, cname, pname, control_mode: int,
                                ucid: int = None, ptype: int = None) -> bool:
        """Aktualisiert Modell-, Fahrer- und Identitätsdaten

        ``cname``/``pname`` duerfen bytes oder str sein; sie werden hier genau
        einmal dekodiert, damit kein Aufrufer mehr ``str(x)[2:-1]`` rechnen
        muss. ``ucid``/``ptype`` stammen aus IS_NPL und sind optional, weil
        aeltere Aufrufer sie nicht kennen.

        Rueckgabe: True, wenn sich Auto, Fahrername oder Eingabemodus geaendert
        haben - darauf haengt das ``player_name_changed``-Event.
        """
        target = self._target
        cname_str = decode_car_name(cname)
        pname_str = decode_player_name(pname)

        changed = (target.cname != cname_str
                   or target.pname != pname_str
                   or target.control_mode != control_mode)

        target.cname = cname_str
        target.cname_bytes = _encode_raw(cname)
        target.pname = pname_str
        target.pname_bytes = _encode_raw(pname)
        target.control_mode = control_mode

        if ucid is not None:
            target.ucid = ucid
        if ptype is not None:
            target.ptype = ptype
            target.is_ai = bool(ptype & PTYPE_AI)
            target.is_remote = bool(ptype & PTYPE_REMOTE)

        return changed
