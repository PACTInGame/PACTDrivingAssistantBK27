import logging
import time
from typing import Any, Dict, List, Optional

import pyinsim
from core.event_bus import EventBus
from vehicles.own_vehicle import OwnVehicle
from vehicles.vehicle import (PTYPE_AI, PTYPE_REMOTE, Vehicle, decode_car_name,
                              decode_player_name)

logger = logging.getLogger(__name__)

# InSim.txt: "MCI_MAX_CARS 16" - mehr Autos verteilt LFS auf mehrere Pakete.
MCI_MAX_CARS = 16

# Wie lange auf das fehlende Reststueck eines MCI-Frames gewartet wird, bevor
# das Teilstueck trotzdem veroeffentlicht wird. Ein halbes Update ist besser
# als ein eingefrorenes: frueher blieben die Assistenzsysteme hier fuer immer
# auf alten Daten stehen (known-issues #6).
FRAME_TIMEOUT_S = 0.5

# Wie sicher die Erkennung des lokalen Fahrers ist. IS_NPL liefert zwei
# unabhaengige Signale (reference/conventions.md §5.4):
#   PType Bit 1 = KI, Bit 2 = fremder Spieler  -> beides aus = wir
#   UCID 0                                     -> lokale Verbindung
# UCID allein reicht nicht, weil im Netzwerk 0 der Host ist und nicht
# zwangslaeufig wir.
_LOCAL_SCORE_PTYPE = 1
_LOCAL_SCORE_UCID = 2


def _as_int(value, default: int = 0) -> int:
    """Paketfelder sind nicht vertrauenswuerdig - nie ungeprueft rechnen."""
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


class VehicleManager:
    """Verwaltet alle Fahrzeuge auf der Strecke"""

    def __init__(self, event_bus: EventBus):
        self.event_bus = event_bus
        self.vehicles: Dict[int, Vehicle] = {}
        self.own_vehicle = OwnVehicle()
        self.players: Dict[int, Any] = {}  # Player info from NPL packets

        # MCI-Frame-Reassembly (CCI_FIRST / CCI_LAST, reference/insim.md §2)
        self._frame: List[Any] = []
        self._frame_started = time.perf_counter()
        self._frame_open = False

        self._local_driver_score = 0

        # Event-Handler registrieren
        self.event_bus.subscribe('vehicle_data_received', self._handle_vehicle_data)
        self.event_bus.subscribe('player_joined', self._handle_player_joined)
        self.event_bus.subscribe('player_left', self._handle_player_left)
        self.event_bus.subscribe('outgauge_data', self._handle_outgauge_data)

    # ─── MCI: Frame-Reassembly ────────────────────────────────────────

    def _handle_vehicle_data(self, mci_packet):
        """Verarbeitet MCI-Pakete mit Fahrzeugdaten

        LFS schickt hoechstens ``MCI_MAX_CARS`` Autos pro Paket. Ein Frame
        beginnt mit einem CompCar, das ``CCI_FIRST`` traegt, und endet mit
        einem, das ``CCI_LAST`` traegt. Frueher wurde stattdessen gezaehlt,
        bis so viele Autos da waren wie Eintraege in ``players`` - war der
        Dict um einen Eintrag veraltet, feuerte ``vehicles_updated`` nie
        wieder (known-issues #6).
        """
        cars = getattr(mci_packet, 'Info', None)
        if not cars:
            return

        now = time.perf_counter()
        marks_first = False
        marks_last = False
        for car in cars:
            info = _as_int(getattr(car, 'Info', 0))
            if info & pyinsim.CCI_FIRST:
                marks_first = True
            if info & pyinsim.CCI_LAST:
                marks_last = True

        # Timeout-Fallback: der Rest des Frames ist nie gekommen.
        if self._frame and now - self._frame_started > FRAME_TIMEOUT_S:
            logger.debug("MCI frame still incomplete after %.0f ms - publishing "
                         "%d cars anyway.", FRAME_TIMEOUT_S * 1000, len(self._frame))
            self._flush_frame()

        if marks_first or not self._frame:
            self._frame = []
            self._frame_started = now
            self._frame_open = False
        self._frame.extend(cars)

        if marks_last:
            self._flush_frame()
        elif marks_first:
            self._frame_open = True
        elif not self._frame_open:
            # Kein CCI-Bit gesetzt: entweder eine LFS-Version, die sie nicht
            # schickt, oder ein Mod. Ein volles Paket heisst, dass noch eins
            # folgt - alles andere ist ein fertiger Frame.
            if len(cars) < MCI_MAX_CARS:
                self._flush_frame()
            else:
                self._frame_open = True

    def _flush_frame(self):
        frame = self._frame
        self._frame = []
        self._frame_open = False
        if frame:
            self._apply_frame(frame)

    def _apply_frame(self, cars):
        """Uebertraegt einen vollstaendigen MCI-Frame und veroeffentlicht ihn

        Kosten pro Zyklus: eine flache Kopie der ``VehicleData`` je Fahrzeug
        (``begin_frame``) plus ein frisches Snapshot-Dict - zusammen unter
        30 µs bei 40 Autos, bei einem Budget von 100 ms.
        """
        own = self.own_vehicle
        own_plid = own.data.player_id
        touched: List[Vehicle] = []
        seen = set()
        own_identity_changed = False

        for data in cars:
            player_id = _as_int(getattr(data, 'PLID', 0))
            if not player_id or player_id in seen:
                continue
            seen.add(player_id)

            if player_id == own_plid:
                vehicle = own
            else:
                vehicle = self.vehicles.get(player_id)
                if vehicle is None:
                    vehicle = Vehicle(player_id)
                    self.vehicles[player_id] = vehicle

            vehicle.begin_frame()
            touched.append(vehicle)

            vehicle.update_position(
                _as_int(getattr(data, 'X', 0)),
                _as_int(getattr(data, 'Y', 0)),
                _as_int(getattr(data, 'Z', 0)),
                _as_int(getattr(data, 'Heading', 0)),
                _as_int(getattr(data, 'Direction', 0)),
                _as_int(getattr(data, 'Speed', 0)) / 91.02  # Convert to km/h
            )

            player_info = self.players.get(player_id)
            if player_info:
                changed = vehicle.update_model_and_driver(
                    player_info.get("CName", "Unknown"),
                    player_info.get("PName", "Unknown"),
                    player_info.get("ControlMode", 0),
                    player_info.get("UCID"),
                    player_info.get("PType"),
                )
                if changed and vehicle is own:
                    own_identity_changed = True

        # Das eigene Auto gehoert nie in die Fremdfahrzeug-Liste.
        if own_plid:
            stray = self.vehicles.pop(own_plid, None)
            if stray is not None:
                stray.abort_frame()
                if stray in touched:
                    touched.remove(stray)

        # Erst das eigene Auto veroeffentlichen - die Abstaende der anderen
        # beziehen sich auf dessen neue Position.
        own.commit_frame()
        if own_plid:
            own_data = own.data
            for vehicle in touched:
                if vehicle is own:
                    continue
                vehicle.update_distance_to_player(own_data.x, own_data.y, own_data.z)
                vehicle.update_angle_to_player(own_data.x, own_data.y, own_data.heading)

        for vehicle in touched:
            vehicle.commit_frame()

        if own_identity_changed:
            self.event_bus.emit('player_name_changed',
                                {"player_name": own.data.pname,
                                 "control_mode": own.data.control_mode})

        # Frisches Dict: die Assistenzsysteme iterieren im Worker-Thread,
        # waehrend der Paket-Thread hier weiter einfuegt und loescht
        # (known-issues #12).
        self.event_bus.emit('vehicles_updated', dict(self.vehicles))

    # ─── IS_NPL / IS_PLL ──────────────────────────────────────────────

    def _get_control_mode(self, flags: int) -> int:
        """Leitet den Eingabemodus aus den IS_NPL-Flags ab

        0 = Maus, 1 = Tastatur, 2 = Lenkrad. Vorher wurde ``bin(flags)[2:]``
        in eine Liste zerlegt und mit negativen Indizes adressiert - dieselben
        Bits, nur teurer und ohne erkennbaren Bezug zu den dokumentierten
        Masken.
        """
        flags = _as_int(flags)
        if flags & pyinsim.PIF_MOUSE:
            return 0
        if flags & (pyinsim.PIF_KB_NO_HELP | pyinsim.PIF_KB_STABILISED):
            return 1
        return 2

    def _handle_player_joined(self, npl_packet):
        """Verarbeitet neue Spieler (IS_NPL - auch beim Verlassen der Box)"""
        player_id = _as_int(getattr(npl_packet, 'PLID', 0))
        if not player_id:
            return

        ucid = _as_int(getattr(npl_packet, 'UCID', -1), -1)
        ptype = _as_int(getattr(npl_packet, 'PType', 0))
        flags = _as_int(getattr(npl_packet, 'Flags', 0))
        raw_pname = getattr(npl_packet, 'PName', b'')
        raw_cname = getattr(npl_packet, 'CName', b'')

        player_info = {
            "PName": decode_player_name(raw_pname),
            "CName": decode_car_name(raw_cname),
            "PNameBytes": raw_pname,
            "CNameBytes": raw_cname,
            "UCID": ucid,
            "PType": ptype,
            "Flags": flags,
            "IsAI": bool(ptype & PTYPE_AI),
            "IsRemote": bool(ptype & PTYPE_REMOTE),
            "ControlMode": self._get_control_mode(flags),
        }
        self.players[player_id] = player_info
        self._consider_local_driver(player_id, ucid, ptype)
        self.event_bus.emit('player_data_updated', dict(self.players))

    def _consider_local_driver(self, player_id: int, ucid: int, ptype: int):
        """Merkt sich die PLID des lokalen Fahrers, kameraunabhaengig

        Ein KI-Fahrer (PType Bit 1) und ein fremder Spieler (Bit 2) scheiden
        aus. Von den verbleibenden Kandidaten gewinnt der mit UCID 0.
        """
        if ptype & (PTYPE_AI | PTYPE_REMOTE):
            return
        score = _LOCAL_SCORE_UCID if ucid == 0 else _LOCAL_SCORE_PTYPE
        current = self.own_vehicle.local_plid
        if current and current != player_id and score <= self._local_driver_score:
            # Schon ein mindestens gleich guter Kandidat bekannt - der erste
            # gewinnt, sonst wandert die eigene PLID bei jedem IS_NPL weiter.
            return

        self._local_driver_score = score
        self.own_vehicle.set_local_driver(player_id, ucid, ptype)
        # Falls das eigene Auto vorher als Fremdfahrzeug gefuehrt wurde.
        self.vehicles.pop(player_id, None)

    def _handle_player_left(self, pll_packet):
        """Entfernt Spieler"""
        player_id = _as_int(getattr(pll_packet, 'PLID', 0))
        if not player_id:
            return
        self.players.pop(player_id, None)
        self.vehicles.pop(player_id, None)

        if player_id == self.own_vehicle.local_plid:
            self.own_vehicle.clear_local_driver()
            self._local_driver_score = 0

        self.event_bus.emit('player_data_updated', dict(self.players))

    # ─── OutGauge ─────────────────────────────────────────────────────

    def _handle_outgauge_data(self, outgauge_packet):
        """Verarbeitet OutGauge-Daten für eigenes Fahrzeug"""
        self.own_vehicle.update_outgauge_data(outgauge_packet)
        self.event_bus.emit('own_vehicle_updated', self.own_vehicle)

    # ─── Abfragen ─────────────────────────────────────────────────────

    def get_nearby_vehicles(self, max_distance: float = 100.0) -> List[Vehicle]:
        """Gibt nahegelegene Fahrzeuge zurück"""
        return [v for v in self.vehicles.values()
                if v.data.distance_to_player <= max_distance]

    def get_vehicle_by_id(self, player_id: int) -> Optional[Vehicle]:
        """Gibt Fahrzeug anhand der Player-ID zurück"""
        return self.vehicles.get(player_id)
