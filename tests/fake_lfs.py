"""A minimal fake LFS for the simulation-test harness.

It speaks just enough InSim to exercise simulation_tests/insim_trace.py without
the game: accepts the ``IS_ISI`` handshake, answers the keep-alive, and sends the
packets a scenario relies on. ``SMALL_SSG`` starts an OutGauge stream to the UDP
port the handshake asked for, exactly as LFS does.

Only used by tests; nothing in simulation_tests/ imports it.
"""

from __future__ import annotations

import socket
import struct
import threading
import time
from typing import List, Optional, Tuple

ISP_ISI, ISP_VER, ISP_TINY, ISP_SMALL = 1, 2, 3, 4
ISP_STA, ISP_MSO, ISP_NPL, ISP_PLL, ISP_MCI, ISP_CON, ISP_CIM = 5, 11, 21, 23, 38, 50, 64
TINY_NONE, TINY_SST, TINY_NPL, TINY_CLOSE = 0, 7, 14, 2
SMALL_SSG, SMALL_SSP = 2, 1

_STA = struct.Struct("4BfH10B5sx2B")
_MCI_HEAD = struct.Struct("4B")
_COMPCAR = struct.Struct("2H4B3i3Hh")
_NPL = struct.Struct("6BH23sx8s3sx15sx8Bi4B")
_CIM = struct.Struct("8B")
_CON = struct.Struct("4B2H")
_CARCONTACT = struct.Struct("3Bb6b2B2h")
_OUTGAUGE = struct.Struct("I3sxH2B7f2I3f15sx15sx")


def mso(message: bytes, user_type=0, code_page=0) -> bytes:
    body = message + b'\0'
    body += bytes((-len(body)) % 4)
    return struct.pack('<8B', (8 + len(body)) // 4, ISP_MSO, 0, code_page,
                       0, 0, user_type, 0) + body


def sta(flags: int, track: bytes = b"BL1", cam: int = 3, num_p: int = 1,
        view_plid: int = 0) -> bytes:
    return _STA.pack(_STA.size // 4, ISP_STA, 0, 0, 1.0, flags, cam, view_plid,
                     num_p, 1, 0, 0, 0, 0, 0, 0, track, 0, 0)


def compcar(plid: int, x_m: float, y_m: float, speed_kmh: float,
            heading_deg: float = 0.0, info: int = 0) -> bytes:
    return _COMPCAR.pack(
        0, 1, plid, 1, info, 0,
        int(x_m * 65536), int(y_m * 65536), 0,
        int(speed_kmh * 91.02), int((heading_deg / 360.0) * 65536) & 0xFFFF,
        int((heading_deg / 360.0) * 65536) & 0xFFFF, 0)


def mci(cars: List[bytes]) -> bytes:
    body = b"".join(cars)
    size = (4 + len(body)) // 4
    return _MCI_HEAD.pack(size, ISP_MCI, 0, len(cars)) + body


def npl(plid: int, ucid: int, ptype: int, pname: bytes, cname: bytes) -> bytes:
    return _NPL.pack(_NPL.size // 4, ISP_NPL, 0, plid, ucid, ptype, 0,
                     pname, b"ABC12345", cname, b"XF GTI", 1, 1, 1, 1, 0, 0,
                     0, 0, 0, 0, 1, 0, 0)


def cim(mode: int, submode: int = 0) -> bytes:
    return _CIM.pack(_CIM.size // 4, ISP_CIM, 0, 0, mode, submode, 0, 0)


def _carcontact(plid: int) -> bytes:
    return _CARCONTACT.pack(plid, 0, 0, 0, 0, 0, 0, 12, 0, 0, 0, 0, 160, 320)


#: 44-byte IS_CON header: Size Type ReqI Zero | SpClose SpW | Time (32-bit ms).
_CON_44 = struct.Struct("<4B2HI")


def con(plid_a: int, plid_b: int, sp_close: int = 55, layout: int = 40) -> bytes:
    """One car-to-car contact, in either the 40- or the 44-byte layout.

    LFS has shipped both; ``pyinsim.IS_CON`` decodes by ``Size``
    rather than assuming a version, so both are worth sending from here.
    """
    if layout == 44:
        return (_CON_44.pack(11, ISP_CON, 0, 0, sp_close, 0, 1234000)
                + _carcontact(plid_a) + _carcontact(plid_b))
    if layout != 40:
        raise ValueError("IS_CON is either 40 or 44 bytes")
    size = (_CON.size + 32) // 4
    return (_CON.pack(size, ISP_CON, 0, 0, sp_close, 1234)
            + _carcontact(plid_a) + _carcontact(plid_b))


def outgauge(time_ms: int, speed_ms: float, rpm: float, gear: int,
             throttle: float, brake: float, plid: int = 0,
             car: bytes = b"XFG", show_lights: int = 0) -> bytes:
    return _OUTGAUGE.pack(time_ms, car, 16384, gear, plid, speed_ms, rpm, 0.0,
                          90.0, 30.0, 2.0, 95.0, 0, show_lights, throttle, brake,
                          0.0, b"", b"")


class FakeLFS:
    """TCP InSim listener plus a UDP sender for OutGauge."""

    def __init__(self, host: str = "127.0.0.1"):
        self._server = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self._server.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self._server.bind((host, 0))
        self._server.listen(1)
        self._server.settimeout(0.25)
        self.host = host
        self.port = self._server.getsockname()[1]
        self.udp = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        self.udp_port: Optional[int] = None
        self.isi: Optional[Tuple] = None
        self.received: List[Tuple[int, int]] = []   # (type, subtype)
        self.connected = threading.Event()
        self.ssg_interval: Optional[int] = None
        self._client: Optional[socket.socket] = None
        self._running = threading.Event()
        self._running.set()
        self._thread = threading.Thread(target=self._serve, daemon=True)
        self._thread.start()

    # -- lifecycle ----------------------------------------------------------
    def close(self) -> None:
        self._running.clear()
        self._thread.join(2.0)
        for sock in (self._client, self._server, self.udp):
            try:
                if sock is not None:
                    sock.close()
            except OSError:
                pass

    def send(self, packet: bytes) -> None:
        if self._client is not None:
            try:
                self._client.sendall(packet)
            except OSError:
                pass

    def send_outgauge(self, packet: bytes) -> None:
        if self.udp_port:
            self.udp.sendto(packet, (self.host, self.udp_port))

    def wait_for_ssg(self, timeout: float = 5.0) -> bool:
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if self.ssg_interval is not None:
                return True
            time.sleep(0.02)
        return False

    # -- server -------------------------------------------------------------
    def _serve(self) -> None:
        while self._running.is_set():
            try:
                client, _ = self._server.accept()
            except socket.timeout:
                continue
            except OSError:
                return
            client.settimeout(0.25)
            self._client = client
            self.connected.set()
            buffer = b""
            while self._running.is_set():
                try:
                    chunk = client.recv(4096)
                except socket.timeout:
                    continue
                except OSError:
                    break
                if not chunk:
                    break
                buffer += chunk
                while len(buffer) >= 4:
                    size = buffer[0] * 4
                    if size == 0 or len(buffer) < size:
                        break
                    self._handle(buffer[:size])
                    buffer = buffer[size:]
            self.connected.clear()

    def _handle(self, data: bytes) -> None:
        ptype = data[1]
        subtype = data[3] if len(data) > 3 else 0
        self.received.append((ptype, subtype))
        if ptype == ISP_ISI:
            # IS_ISI: Size Type ReqI Zero UDPPort(H) Flags(H) InSimVer Prefix Interval(H) ...
            udp_port, flags = struct.unpack_from("<HH", data, 4)
            self.isi = (udp_port, flags)
            self.udp_port = udp_port or None
            self.send(struct.pack("4B7sx5sxBB", 5, ISP_VER, data[2], 0,
                                  b"0.7F", b"S3", 10, 0))
        elif ptype == ISP_SMALL:
            uval = struct.unpack_from("<I", data, 4)[0]
            if subtype == SMALL_SSG:
                self.ssg_interval = uval
        elif ptype == ISP_TINY and subtype == TINY_NONE:
            self.send(data)  # keep-alive echo, as LFS expects the client to do
