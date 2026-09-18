"""Tracer-local corrections to pyinsim's decoder.

``pyinsim/insim.py`` is the add-on's shared protocol library and is not touched
from here: this module installs corrected packet classes into the *tracer
process's own* ``pyinsim.core._PACKET_MAP``. The tracer runs in a separate
process from the add-on (``run_scenario.py`` starts it with ``subprocess``), so
a patch applied here can never reach a running add-on session.

Currently one packet needs it -- ``IS_CON``:

* **Size.** ``pyinsim`` expects the 40-byte layout (``word Time``). The InSim
  version LFS advertises today sends 44 bytes, with an extra ``word SpW`` and a
  32-bit ``Time``. ``CarContact(data[24:])`` then gets 20 bytes and
  ``struct.error`` escapes into the asyncore loop, which drops the connection.
  Both layouts are decoded here, chosen by ``Size``, and the trace records which
  one arrived (``con_layout``) so a capture says what it measured.
* **Signedness.** ``pyinsim``'s ``CarContact`` unpacks ``ThrBrk``/``CluHan``/
  ``GearSp``/``Speed``/``Direction``/``Heading`` as *signed* bytes and
  ``AccelF``/``AccelR`` as *unsigned*. It is the other way round: the pedal
  nibbles, speed and the two angle bytes are unsigned 0..255, while the
  accelerations are signed (``AccelF`` negative = braking). Speeds above
  127 m/s, headings past 180 deg and every deceleration decode wrong otherwise.

Reference: https://www.lfs.net/programmer/insim (``IS_CON`` / ``CarContact``).
``C:\\LFS\\docs\\InSim.txt`` is now only a link to that page, so the layout is
not verifiable from disk -- which is why this decodes by ``Size`` rather than
assuming a version (reference/insim.md §6).
"""

from __future__ import annotations

import struct
from typing import Any

#: ``PLID Info Sp2 Steer | ThrBrk CluHan GearSp Speed | Direction Heading
#: AccelF AccelR | X Y``  -- 16 bytes, signedness per InSim.
_CAR_CONTACT = struct.Struct("<3Bb6B2b2h")
_CAR_CONTACT_FIELDS = ("PLID", "Info", "Sp2", "Steer", "ThrBrk", "CluHan",
                       "GearSp", "Speed", "Direction", "Heading",
                       "AccelF", "AccelR", "X", "Y")

#: ``Size Type ReqI Zero | SpClose Time`` -- the pre-v10 header, 8 bytes.
_HEADER_40 = struct.Struct("<4B2H")
#: ``Size Type ReqI Zero | SpClose SpW | Time`` -- 12 bytes, 32-bit Time.
_HEADER_44 = struct.Struct("<4B2HI")


class CarContact(object):
    """One car's state at the moment of a contact. 16 bytes, both layouts.

    Plain attributes, no ``__slots__``: ``packet_dump.decode_object`` walks
    ``vars(obj)`` and a slots-only object would trace as an empty dict.
    """

    def __init__(self, data: bytes):
        for name, value in zip(_CAR_CONTACT_FIELDS, _CAR_CONTACT.unpack(data)):
            setattr(self, name, value)


class IS_CON(object):
    """CONtact between two cars. ``A`` and ``B`` are sorted by PLID."""

    def unpack(self, data: bytes) -> "IS_CON":
        size = data[0] * 4 if data else 0
        if size == 44 and len(data) >= 44:
            (self.Size, self.Type, self.ReqI, self.Zero,
             self.SpClose, self.SpW, self.Time) = _HEADER_44.unpack(data[:12])
            body = 12
            self.con_layout = 44
        elif size == 40 and len(data) >= 40:
            (self.Size, self.Type, self.ReqI, self.Zero,
             self.SpClose, self.Time) = _HEADER_40.unpack(data[:8])
            self.SpW = 0
            body = 8
            self.con_layout = 40
        else:
            raise ValueError(
                f"IS_CON: unexpected size {size} (got {len(data)} bytes) -- "
                "neither the 40-byte nor the 44-byte layout")
        self.A = CarContact(data[body:body + 16])
        self.B = CarContact(data[body + 16:body + 32])
        return self


def apply(pyinsim_module: Any) -> None:
    """Install the corrected classes into this process's pyinsim.

    Idempotent, and a no-op if pyinsim's internals ever move -- a tracer that
    cannot patch still traces everything else.
    """
    try:
        packet_map = pyinsim_module.core._PACKET_MAP
    except AttributeError:  # pragma: no cover - pyinsim layout changed
        return
    packet_map[pyinsim_module.ISP_CON] = IS_CON
