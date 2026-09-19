"""Turn a pyinsim packet into a flat, self-explaining dict for the trace.

Two jobs:

1. **Generic decoding** — every public attribute of the packet object, with bytes
   made readable and sub-objects (``CompCar``, ``CarContact``, ``CarContOBJ``)
   expanded. Adding a packet to a trace therefore needs no code here.
2. **Derived SI values** — the raw LFS integers are useless to read by eye and
   are the project's most common bug source (reference/conventions.md §1-§3), so
   every packet that carries positions, speeds or angles also gets metres, km/h
   and degrees next to the raw fields. A reader never has to convert anything.

Derivation is table-driven: ``DERIVERS[<packet name>]`` and ``FLAG_FIELDS``. To
log something new, add an entry — nothing else in the harness needs to change.
"""

from __future__ import annotations

from typing import Any, Callable, Dict, List, Optional, Tuple

# ── unit conversions (reference/conventions.md §1-§3) ─────────────────────────
MCI_POS_TO_M = 1.0 / 65536.0        # CompCar X/Y/Z: 1/65536 m
AXM_POS_TO_M = 1.0 / 16.0           # ObjectInfo X/Y and CarContact X/Y: 1/16 m
AXM_Z_TO_M = 1.0 / 4.0              # ObjectInfo Zbyte: 1/4 m
MCI_SPEED_TO_KMH = 1.0 / 91.02      # CompCar Speed: 32768 = 100 m/s
LFS_ANGLE_TO_DEG = 360.0 / 65536.0  # Heading/Direction word: 65536 = 360 deg
BYTE_ANGLE_TO_DEG = 360.0 / 256.0   # ObjectInfo/CarContact heading byte
ANGVEL_TO_DEG_S = 360.0 / 16384.0   # CompCar AngVel: 16384 = 360 deg/s
MS_TO_KMH = 3.6

_SKIP_FIELDS = frozenset({"Size", "Type", "pack_s", "Zero", "Sp0", "Sp1", "Sp2", "Sp3"})


# ── flag tables ──────────────────────────────────────────────────────────────
ISS_FLAGS: Tuple[Tuple[int, str], ...] = (
    (1, "GAME"), (2, "REPLAY"), (4, "PAUSED"), (8, "SHIFTU"), (16, "DIALOG"),
    (32, "SHIFTU_FOLLOW"), (64, "SHIFTU_NO_OPT"), (128, "SHOW_2D"),
    (256, "FRONT_END"), (512, "MULTI"), (1024, "MPSPEEDUP"), (2048, "WINDOWED"),
    (4096, "SOUND_MUTE"), (8192, "VIEW_OVERRIDE"), (16384, "VISIBLE"),
    (32768, "TEXT_ENTRY"),
)

OG_FLAGS: Tuple[Tuple[int, str], ...] = (
    (1, "SHIFT"), (2, "CTRL"), (8192, "TURBO"), (16384, "KM"), (32768, "BAR"),
)

DL_FLAGS: Tuple[Tuple[int, str], ...] = (
    (1, "SHIFT"), (2, "FULLBEAM"), (4, "HANDBRAKE"), (8, "PITSPEED"), (16, "TC"),
    (32, "SIGNAL_L"), (64, "SIGNAL_R"), (128, "SIGNAL_ANY"), (256, "OILWARN"),
    (512, "BATTERY"), (1024, "ABS"), (2048, "ENGINE"), (4096, "FOG_REAR"),
    (8192, "FOG_FRONT"), (16384, "DIPPED"), (32768, "HANDBRAKE_2"),
)

CCI_FLAGS: Tuple[Tuple[int, str], ...] = (
    (1, "BLUE"), (2, "YELLOW"), (32, "LAG"), (64, "FIRST"), (128, "LAST"),
)

PTYPE_FLAGS: Tuple[Tuple[int, str], ...] = ((1, "FEMALE"), (2, "AI"), (4, "REMOTE"))

PIF_FLAGS: Tuple[Tuple[int, str], ...] = (
    (1, "SWAPSIDE"), (8, "AUTOGEARS"), (16, "SHIFTER"), (64, "HELP_B"),
    (128, "AXIS_CLUTCH"), (256, "INPITS"), (512, "AUTOCLUTCH"), (1024, "MOUSE"),
    (2048, "KB_NO_HELP"), (4096, "KB_STABILISED"), (8192, "CUSTOM_VIEW"),
)

OBH_FLAGS: Tuple[Tuple[int, str], ...] = (
    (1, "LAYOUT"), (2, "CAN_MOVE"), (4, "WAS_MOVING"), (8, "ON_SPOT"),
)

CAM_NAMES = {0: "FOLLOW", 1: "HELI", 2: "CAM", 3: "DRIVER", 4: "CUSTOM", 255: "ANOTHER"}
CIM_MODES = {0: "NORMAL", 1: "OPTIONS", 2: "HOST_OPTIONS", 3: "GARAGE",
             4: "CAR_SELECT", 5: "TRACK_SELECT", 6: "SHIFTU"}
CIM_SUBMODES = {
    0: {0: "NORMAL", 1: "WHEEL_TEMPS", 2: "WHEEL_DAMAGE", 3: "LIVE_SETTINGS",
        4: "PIT_INSTRUCTIONS"},
    3: {0: "INFO", 1: "COLOURS", 2: "BRAKE_TC", 3: "SUSP", 4: "STEER", 5: "DRIVE",
        6: "TYRES", 7: "AERO", 8: "PASS"},
    6: {0: "PLAIN", 1: "BUTTONS", 2: "EDIT"},
}
MSO_USERTYPES = {0: "SYSTEM", 1: "USER", 2: "PREFIX", 3: "O"}
PMO_ACTIONS = {0: "LOADING_FILE", 1: "ADD_OBJECTS", 2: "DEL_OBJECTS", 3: "CLEAR_ALL",
               4: "TINY_AXM", 5: "TTC_SEL", 6: "SELECTION", 7: "POSITION", 8: "GET_Z"}


def decode_flags(value: int, table: Tuple[Tuple[int, str], ...]) -> List[str]:
    """Bit names set in ``value``. Unknown bits appear as ``bit<N>``."""
    if not isinstance(value, int):
        return []
    names = []
    known = 0
    for bit, name in table:
        known |= bit
        if value & bit:
            names.append(name)
    rest = value & ~known
    if rest:
        for i in range(32):
            if rest & (1 << i):
                names.append(f"bit{i}")
    return names


#: Fields that get a ``<field>_flags`` list, keyed by packet name.
FLAG_FIELDS: Dict[str, Dict[str, Tuple[Tuple[int, str], ...]]] = {
    "STA": {"Flags": ISS_FLAGS},
    "NPL": {"PType": PTYPE_FLAGS, "Flags": PIF_FLAGS},
    # Same bitfield as NPL's. IS_PFL is the *change* notification, and the
    # only one a driver pressing SHIFT+G on track produces (README §5).
    "PFL": {"Flags": PIF_FLAGS},
    "OutGauge": {"Flags": OG_FLAGS, "DashLights": DL_FLAGS, "ShowLights": DL_FLAGS},
    "OBH": {"OBHFlags": OBH_FLAGS},
}


# ── generic decoding ─────────────────────────────────────────────────────────
def _printable(raw: bytes) -> bool:
    return all(32 <= b < 127 for b in raw)


def decode_value(value: Any) -> Any:
    """One packet field -> something json can carry and a human can read."""
    if isinstance(value, (bytes, bytearray)):
        raw = bytes(value).split(b"\x00", 1)[0]
        # latin-1 is a lossless byte<->char map, so the text stays reversible;
        # a vehicle mod puts three arbitrary bytes here (conventions.md §4).
        text = raw.decode("latin-1")
        return text if _printable(raw) else {"hex": raw.hex(), "text": text}
    if isinstance(value, (list, tuple)):
        return [decode_value(v) for v in value]
    if isinstance(value, (int, float, str, bool)) or value is None:
        return value
    if hasattr(value, "__dict__"):
        return decode_object(value)
    return repr(value)


def decode_object(obj: Any) -> Dict[str, Any]:
    """Every public attribute of a packet or sub-packet object."""
    out: Dict[str, Any] = {}
    for name, value in vars(obj).items():
        if name.startswith("_") or name in _SKIP_FIELDS:
            continue
        out[name] = decode_value(value)
    return out


# ── derivers ─────────────────────────────────────────────────────────────────
def _derive_compcar(car: Dict[str, Any]) -> None:
    car["x_m"] = round(car.get("X", 0) * MCI_POS_TO_M, 3)
    car["y_m"] = round(car.get("Y", 0) * MCI_POS_TO_M, 3)
    car["z_m"] = round(car.get("Z", 0) * MCI_POS_TO_M, 3)
    car["speed_kmh"] = round(car.get("Speed", 0) * MCI_SPEED_TO_KMH, 2)
    # 0 deg = +Y (north), anticlockwise -- LFS's own frame.
    car["heading_deg"] = round((car.get("Heading", 0) * LFS_ANGLE_TO_DEG) % 360.0, 2)
    car["direction_deg"] = round((car.get("Direction", 0) * LFS_ANGLE_TO_DEG) % 360.0, 2)
    # 0 deg = +X, anticlockwise -- the frame math/trig code in this repo uses.
    car["heading_math_deg"] = round((car["heading_deg"] + 90.0) % 360.0, 2)
    car["angvel_deg_s"] = round(car.get("AngVel", 0) * ANGVEL_TO_DEG_S, 2)
    car["info_flags"] = decode_flags(car.get("Info", 0), CCI_FLAGS)


def _derive_mci(data: Dict[str, Any]) -> None:
    cars = data.get("Info") or []
    for car in cars:
        if isinstance(car, dict):
            _derive_compcar(car)
    data["cars"] = cars
    data.pop("Info", None)


def _derive_outgauge(data: Dict[str, Any]) -> None:
    data["speed_kmh"] = round(data.get("Speed", 0.0) * MS_TO_KMH, 2)
    gear = data.get("Gear", 0)
    # OutGauge Gear: 0 = reverse, 1 = neutral, 2 = first.
    data["gear_label"] = {0: "R", 1: "N"}.get(gear, str(gear - 1))


def _derive_outsim(data: Dict[str, Any]) -> None:
    pos = data.get("Pos") or (0, 0, 0)
    if isinstance(pos, (list, tuple)) and len(pos) == 3:
        data["pos_m"] = [round(v * MCI_POS_TO_M, 3) for v in pos]
    vel = data.get("Vel") or (0.0, 0.0, 0.0)
    if isinstance(vel, (list, tuple)) and len(vel) == 3:
        data["speed_kmh"] = round((sum(v * v for v in vel) ** 0.5) * MS_TO_KMH, 2)


def _derive_sta(data: Dict[str, Any]) -> None:
    data["cam"] = CAM_NAMES.get(data.get("InGameCam"), str(data.get("InGameCam")))
    flags = data.get("Flags", 0)
    # The two questions everything else asks of IS_STA (reference/ui.md §1).
    data["on_track"] = bool(flags & 1) and not (flags & 256)
    data["buttons_visible"] = bool(flags & 16384)


def _derive_cim(data: Dict[str, Any]) -> None:
    mode = data.get("Mode", 0)
    data["mode_name"] = CIM_MODES.get(mode, str(mode))
    data["submode_name"] = CIM_SUBMODES.get(mode, {}).get(
        data.get("SubMode"), str(data.get("SubMode")))


def _derive_mso(data: Dict[str, Any]) -> None:
    data["user_type"] = MSO_USERTYPES.get(data.get("UserType"), str(data.get("UserType")))


def _derive_carcontact(contact: Dict[str, Any]) -> None:
    contact["x_m"] = round(contact.get("X", 0) * AXM_POS_TO_M, 3)
    contact["y_m"] = round(contact.get("Y", 0) * AXM_POS_TO_M, 3)
    contact["heading_deg"] = round((contact.get("Heading", 0) * BYTE_ANGLE_TO_DEG) % 360.0, 2)
    contact["direction_deg"] = round((contact.get("Direction", 0) * BYTE_ANGLE_TO_DEG) % 360.0, 2)
    contact["speed_kmh"] = round(contact.get("Speed", 0) * MS_TO_KMH, 2)  # Speed is m/s
    thr_brk = contact.get("ThrBrk", 0)
    clu_han = contact.get("CluHan", 0)
    gear_sp = contact.get("GearSp", 0)
    # Nibble-packed pedal/gear state, per InSim.txt's CarContact.
    contact["throttle"] = ((thr_brk >> 4) & 0x0F) / 15.0
    contact["brake"] = (thr_brk & 0x0F) / 15.0
    contact["clutch"] = ((clu_han >> 4) & 0x0F) / 15.0
    contact["handbrake"] = (clu_han & 0x0F) / 15.0
    contact["gear"] = (gear_sp >> 4) & 0x0F  # 15 = reverse
    # Official CarContact schema: signed m/s², forward/right positive.
    contact["accel_f_ms2"] = contact.get("AccelF", 0)
    contact["accel_r_ms2"] = contact.get("AccelR", 0)
    contact["accel_f_g"] = contact["accel_f_ms2"] / 9.80665
    contact["accel_r_g"] = contact["accel_r_ms2"] / 9.80665


def _derive_con(data: Dict[str, Any]) -> None:
    for side in ("A", "B"):
        contact = data.get(side)
        if isinstance(contact, dict):
            _derive_carcontact(contact)
    # SpClose: high 4 bits reserved, low 12 bits the closing speed at 10 = 1 m/s.
    # Not re-measured against this install -- treat the magnitude as indicative,
    # the *presence* of the packet as the hard fact.
    data["closing_speed_ms"] = round((data.get("SpClose", 0) & 0x0FFF) / 10.0, 2)
    data["closing_speed_kmh"] = round(data["closing_speed_ms"] * MS_TO_KMH, 2)


def _derive_obh(data: Dict[str, Any]) -> None:
    car = data.get("C")
    if isinstance(car, dict):
        car["x_m"] = round(car.get("X", 0) * AXM_POS_TO_M, 3)
        car["y_m"] = round(car.get("Y", 0) * AXM_POS_TO_M, 3)
        car["speed_kmh"] = round(car.get("Speed", 0) * MS_TO_KMH, 2)
        car["heading_deg"] = round((car.get("Heading", 0) * BYTE_ANGLE_TO_DEG) % 360.0, 2)
    data["object_x_m"] = round(data.get("X", 0) * AXM_POS_TO_M, 3)
    data["object_y_m"] = round(data.get("Y", 0) * AXM_POS_TO_M, 3)
    data["object_z_m"] = round(data.get("Zbyte", 0) * AXM_Z_TO_M, 3)
    data["closing_speed_ms"] = round(data.get("SpClose", 0) / 10.0, 2)


def _derive_axm(data: Dict[str, Any]) -> None:
    data["action"] = PMO_ACTIONS.get(data.get("PMOAction"), str(data.get("PMOAction")))
    objects = data.get("Info") or []
    for obj in objects:
        if isinstance(obj, dict):
            obj["x_m"] = round(obj.get("X", 0) * AXM_POS_TO_M, 3)
            obj["y_m"] = round(obj.get("Y", 0) * AXM_POS_TO_M, 3)
            obj["z_m"] = round(obj.get("Zbyte", 0) * AXM_Z_TO_M, 3)
            obj["heading_deg"] = round((obj.get("Heading", 0) * BYTE_ANGLE_TO_DEG) % 360.0, 2)
    data["objects"] = objects
    data.pop("Info", None)


#: packet name -> function mutating the decoded dict in place.
DERIVERS: Dict[str, Callable[[Dict[str, Any]], None]] = {
    "MCI": _derive_mci,
    "STA": _derive_sta,
    "CIM": _derive_cim,
    "MSO": _derive_mso,
    "CON": _derive_con,
    "OBH": _derive_obh,
    "AXM": _derive_axm,
    "OutGauge": _derive_outgauge,
    "OutSim": _derive_outsim,
}


def packet_to_dict(name: str, packet: Any) -> Dict[str, Any]:
    """Decode ``packet`` and add the derived SI fields registered for ``name``."""
    data = decode_object(packet)
    for field, table in FLAG_FIELDS.get(name, {}).items():
        if field in data:
            data[f"{field.lower()}_flags"] = decode_flags(data[field], table)
    deriver: Optional[Callable[[Dict[str, Any]], None]] = DERIVERS.get(name)
    if deriver is not None:
        deriver(data)
    return data
