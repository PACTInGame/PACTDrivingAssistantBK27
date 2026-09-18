"""Observer-local fixes for InSim v10; do not patch the add-on's decoder map.

Reference: https://www.lfs.net/programmer/insim (IS_CON, CarContact).
"""
import struct
from types import SimpleNamespace


class IS_CON:
    def __init__(self, data):
        if len(data) != 44 or data[0] * 4 != len(data):
            raise ValueError("InSim v10 IS_CON must contain 44 bytes")
        (self.Size, self.Type, self.ReqI, self.Zero, self.SpClose,
         self.SpW, self.Time) = struct.unpack_from("<4B2HI", data)
        self.A = contact(data[12:28])
        self.B = contact(data[28:44])


def contact(data):
    # Steer and accelerations are signed; pedal nibbles, speed and angles unsigned.
    values = struct.unpack("<3Bb6B2b2h", data)
    names = ("PLID", "Info", "Sp2", "Steer", "ThrBrk", "CluHan", "GearSp",
             "Speed", "Direction", "Heading", "AccelF", "AccelR", "X", "Y")
    return SimpleNamespace(**dict(zip(names, values)))
