import pyinsim
from vehicles.vehicle import PTYPE_AI, PTYPE_REMOTE, Vehicle


class OwnVehicle(Vehicle):
    """Repräsentiert das eigene Fahrzeug mit erweiterten Daten

    Zwei PLID-Quellen, die frueher zu einer verschmolzen wurden
    (reference/conventions.md §5.2, known-issues #30):

    * ``local_plid``  - aus IS_NPL, also die PLID des *lokalen Fahrers*.
      Kameraunabhaengig und damit die einzige Quelle, auf die man aktuieren
      darf.
    * ``viewed_plid`` - aus OutGauge, also die PLID des *betrachteten* Autos.
      TAB aendert sie. Frueher landete genau dieser Wert in
      ``data.player_id`` und hat das ganze OwnVehicle-Objekt auf ein fremdes
      Auto umgebogen.

    ``data.player_id`` folgt jetzt ``local_plid``, sobald ein IS_NPL fuer den
    lokalen Fahrer gesehen wurde. Solange das nicht der Fall ist (LFS hat noch
    kein IS_NPL geschickt), gilt weiter der OutGauge-Wert - sonst wuerde die
    App gar kein eigenes Auto kennen.
    """

    def __init__(self):
        super().__init__(0)  # Player ID wird später gesetzt

        # PLID-Herkunft
        self.local_plid: int = 0
        self.viewed_plid: int = 0

        # OutGauge-Daten
        self.fuel: float = 0.0
        self.rpm: int = 0
        self.gear: int = 0
        self.brake: float = 0.0
        self.throttle: float = 0.0
        self.clutch: float = 0.0
        self.turbo: float = 0.0

        # Lichter und Indikatoren
        self.indicator_left: bool = False
        self.indicator_right: bool = False
        self.hazard_lights: bool = False
        self.full_beam_light: bool = False
        self.low_beam_light: bool = False

        # Warnleuchten
        self.tc_light: bool = False
        self.abs_light: bool = False
        self.handbrake_light: bool = False
        self.battery_light: bool = False
        self.oil_light: bool = False
        self.eng_light: bool = False

    # ─── Identität ────────────────────────────────────────────────────

    def set_local_driver(self, plid: int, ucid: int = 0, ptype: int = 0):
        """Uebernimmt die per IS_NPL bestaetigte PLID des lokalen Fahrers"""
        self.local_plid = plid
        target = self._target
        target.player_id = plid
        target.ucid = ucid
        target.ptype = ptype
        target.is_ai = bool(ptype & PTYPE_AI)
        target.is_remote = bool(ptype & PTYPE_REMOTE)

    def clear_local_driver(self):
        """Der lokale Fahrer hat die Strecke verlassen (IS_PLL)"""
        self.local_plid = 0
        target = self._target
        target.ucid = -1
        target.ptype = 0
        target.is_ai = False
        target.is_remote = False

    @property
    def is_local_driver(self) -> bool:
        """Beschreiben die OutGauge-Daten gerade wirklich das eigene Auto?

        Das ist die Bedingung, unter der aktuiert werden darf - Gangwechsel,
        Auto-Hold, Licht- und Sirenenbefehle wirken auf das *eigene* Auto,
        auch wenn die Daten von einem fremden kommen
        (reference/conventions.md §5.2).

        Solange kein IS_NPL fuer den lokalen Fahrer gesehen wurde, kann die
        Frage nicht beantwortet werden. Dann gilt der bisherige Zustand
        (OutGauge ist die einzige Quelle) und die Antwort ist True - sonst
        wuerde diese Aenderung Funktionen abschalten statt sie abzusichern.
        """
        if not self.local_plid:
            return True
        return self.viewed_plid == self.local_plid

    # ─── OutGauge ─────────────────────────────────────────────────────

    def update_outgauge_data(self, packet):
        """Aktualisiert Daten aus OutGauge-Paket

        Jedes Feld einzeln abgesichert: ein aelteres oder gemoddetes LFS darf
        hier keine AttributeError werfen, sonst stirbt der Paket-Handler.
        """
        target = self._target

        self.viewed_plid = _as_int(getattr(packet, 'PLID', 0))
        if self.local_plid:
            target.player_id = self.local_plid
        elif self.viewed_plid:
            # Notbehelf, solange IS_NPL noch nicht da war.
            target.player_id = self.viewed_plid

        self.fuel = _as_float(getattr(packet, 'Fuel', 0.0))
        self.rpm = _as_float(getattr(packet, 'RPM', 0.0))
        self.gear = _as_int(getattr(packet, 'Gear', 0))
        self.brake = _as_float(getattr(packet, 'Brake', 0.0))
        self.throttle = _as_float(getattr(packet, 'Throttle', 0.0))
        self.clutch = _as_float(getattr(packet, 'Clutch', 0.0))
        self.turbo = _as_float(getattr(packet, 'Turbo', 0.0))

        # Geschwindigkeit ist Bewegungsdatum, kein Anzeigewert: sie geht in
        # jede Abstands- und Bremsrechnung ein. Beim Zuschauen liefert
        # OutGauge die eines fremden Autos - dann bleibt der MCI-Wert des
        # eigenen Autos stehen.
        if self.is_local_driver:
            target.speed = _as_float(getattr(packet, 'Speed', 0.0)) * 3.6  # Convert to km/h

        # Lichter auswerten
        lights = _as_int(getattr(packet, 'ShowLights', 0))
        self.indicator_left = bool(pyinsim.DL_SIGNAL_L & lights)
        self.indicator_right = bool(pyinsim.DL_SIGNAL_R & lights)
        self.hazard_lights = self.indicator_left and self.indicator_right
        self.full_beam_light = bool(pyinsim.DL_FULLBEAM & lights)
        self.low_beam_light = bool(pyinsim.DL_DIPPED & lights)
        self.tc_light = bool(pyinsim.DL_TC & lights)
        self.abs_light = bool(pyinsim.DL_ABS & lights)
        self.handbrake_light = bool(pyinsim.DL_HANDBRAKE & lights)
        self.battery_light = bool(pyinsim.DL_BATTERY & lights)
        self.oil_light = bool(pyinsim.DL_OILWARN & lights)
        self.eng_light = bool(pyinsim.DL_ENGINE & lights)


def _as_int(value, default: int = 0) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def _as_float(value, default: float = 0.0) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return default
