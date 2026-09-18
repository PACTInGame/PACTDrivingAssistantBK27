"""Passive, separately connected collector. No application components or events."""
import time
import pyinsim
from .protocol import IS_CON


class Collector:
    def __init__(self, trace, port=29999, udp=30001, request_streams=True):
        self.trace = trace
        self.state = None
        self.menu_state = None
        self.menu_request_id = 1
        self.error = None
        self.packets = {}
        self.connection = pyinsim.insim(
            "127.0.0.1", port, ReqI=1, UDPPort=udp, IName=b"Scenario observer", Interval=50,
            Flags=pyinsim.ISF_MCI | pyinsim.ISF_CON | pyinsim.ISF_OBH |
                  pyinsim.ISF_HLV | pyinsim.ISF_AXM_LOAD | pyinsim.ISF_AXM_EDIT)
        # Bind only packet classes supported by this fork: EVT_ALL would crash
        # on newly introduced packet IDs missing from pyinsim's decoder map.
        from pyinsim.core import _PACKET_MAP
        self.packet_types = _PACKET_MAP
        self.original_handler = self.connection._handle_insim_packet
        self.connection._handle_insim_packet = self.receive
        for packet_type in _PACKET_MAP:
            self.connection.bind(packet_type, self.packet)
        self.connection.bind(pyinsim.EVT_OUTGAUGE, self.packet)
        self.connection.bind(pyinsim.EVT_OUTSIM, self.packet)
        for event in (pyinsim.EVT_CLOSE, pyinsim.EVT_ERROR, pyinsim.EVT_TIMEOUT):
            self.connection.bind(event, self.failed)
        for subtype in (pyinsim.TINY_SST, pyinsim.TINY_NPL, pyinsim.TINY_NCN):
            self.connection.send(pyinsim.ISP_TINY, ReqI=1, SubT=subtype)
        if request_streams:
            for subtype in (pyinsim.SMALL_SSG, pyinsim.SMALL_SSP):
                self.connection.send(pyinsim.ISP_SMALL, SubT=subtype, UVal=50)

    def receive(self, data):
        try:
            if data[1] == pyinsim.ISP_CON:
                self.packet(self.connection, IS_CON(data))
            elif data[1] not in self.packet_types:
                self.trace.write("unsupported_packet", {"type": data[1], "raw_hex": data.hex()})
            else:
                self.original_handler(data)
        except Exception as exc:
            self.trace.write("decode_or_callback_error", {"raw_hex": data.hex(), "error": repr(exc)})
            raise

    def failed(self, connection):
        self.error = "InSim connection closed or decoder/transport failed"

    def packet(self, connection, packet):
        name = type(packet).__name__
        self.trace.write(name, packet)
        self.packets[name] = self.packets.get(name, 0) + 1
        if name == "IS_STA":
            self.state = packet
            if packet.ReqI == self.menu_request_id:
                self.menu_state = packet
        self.observe(name, packet)

    def observe(self, name, packet):
        """Override in a _temp monitor; write derived signals to self.trace."""

    def check(self):
        if self.error:
            raise RuntimeError(self.error)

    def menu(self, pump, timeout=5):
        self.menu_state = None
        self.menu_request_id = self.menu_request_id % 254 + 1
        self.connection.send(pyinsim.ISP_TINY, ReqI=self.menu_request_id, SubT=pyinsim.TINY_SST)
        deadline = time.perf_counter() + timeout
        while self.menu_state is None and time.perf_counter() < deadline:
            pump(0.01)
            self.check()
        if self.menu_state is None:
            raise RuntimeError("No fresh IS_STA response")
        forbidden = (pyinsim.ISS_GAME | pyinsim.ISS_FRONT_END | pyinsim.ISS_REPLAY |
                     pyinsim.ISS_DIALOG | pyinsim.ISS_TEXT_ENTRY)
        if self.menu_state.Flags & forbidden:
            raise RuntimeError("LFS must be in the main menu")

    def close(self):
        self.connection.close()
