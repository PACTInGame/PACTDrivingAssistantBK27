#!/usr/bin/env python
"""Standalone InSim/OutGauge tracer -- the base script for every scenario.

Run it next to a live add-on session and it records what LFS reports, into one
time-ordered JSONL trace:

    python simulation_tests/insim_trace.py --out trace.jsonl --duration 120

It touches nothing in the game. The only packets it sends are the InSim
handshake, ``TINY_SST`` / ``TINY_NPL`` requests, and ``SMALL_SSG`` / ``SMALL_SSP``
to start its **own** OutGauge/OutSim stream on its own UDP port -- a per-InSim
connection setting, so the add-on's streams on 30000/29998 keep running
untouched (reference/insim.md §1).

Copy this file into ``_temp/`` and edit the copy when a test needs different
measurements; see simulation_tests/README.md. Scenarios themselves are not to be
edited.
"""

from __future__ import annotations

import argparse
import os
import signal
import sys
import threading
import time
from typing import Any, Dict, List, Optional

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from simulation_tests import config, insim_patch, packet_dump, paths  # noqa: E402
from simulation_tests.control_channel import ControlServer  # noqa: E402
from simulation_tests.trace_format import (SRC_INSIM, SRC_MARKER, SRC_OUTGAUGE,  # noqa: E402
                                           SRC_OUTSIM, SRC_TRACER, TraceWriter)

paths.ensure_repo_on_path()
import pyinsim  # noqa: E402

# Correct pyinsim's decoder *in this process only* -- the tracer runs beside the
# add-on, not inside it. Must happen before _known_packets() reads the map.
insim_patch.apply(pyinsim)


# ── packet registry ──────────────────────────────────────────────────────────
def _known_packets() -> Dict[str, int]:
    """``{"MCI": ISP_MCI, ...}`` for every packet pyinsim can actually decode."""
    try:
        decodable = set(pyinsim.core._PACKET_MAP)
    except AttributeError:  # pragma: no cover - pyinsim layout changed
        decodable = None
    out: Dict[str, int] = {}
    for name, value in vars(pyinsim).items():
        if not name.startswith("ISP_") or not isinstance(value, int):
            continue
        if decodable is not None and value not in decodable:
            # Binding a packet pyinsim has no class for would raise inside the
            # asyncore loop the first time LFS sends it.
            continue
        out[name[4:]] = value
    return out


KNOWN_PACKETS = _known_packets()

#: Packets that only arrive when the handshake asks for them.
PACKET_FLAGS = {
    "MCI": "ISF_MCI",
    "NLP": "ISF_NLP",
    "CON": "ISF_CON",
    "OBH": "ISF_OBH",
    "HLV": "ISF_HLV",
    "AXM": "ISF_AXM_LOAD|ISF_AXM_EDIT",
}


def resolve_flags(packet_names: List[str]) -> int:
    """InSim handshake flags needed to receive ``packet_names``."""
    flags = pyinsim.ISF_LOCAL
    for name in packet_names:
        for part in PACKET_FLAGS.get(name, "").split("|"):
            if part:
                flags |= getattr(pyinsim, part)
    return flags


# ── tracer ───────────────────────────────────────────────────────────────────
class InSimTracer:
    """Owns the InSim connection, the trace file and the control channel."""

    def __init__(self, args: argparse.Namespace):
        self.args = args
        self.packets: List[str] = args.packets
        self.writer = TraceWriter(args.out)
        self.insim = None
        self.control: Optional[ControlServer] = None
        self._stopping = threading.Event()
        self._started_wall = time.time()
        # Last known game state, for the `state` control command.
        self.last_state: Dict[str, Any] = {}
        self.last_cim: Dict[str, Any] = {}
        self.packets_seen: Dict[str, int] = {}
        self.markers: List[Dict[str, Any]] = []

    # -- lifecycle ----------------------------------------------------------
    def start(self) -> None:
        args = self.args
        flags = resolve_flags(self.packets)
        self.insim = pyinsim.insim(
            args.host.encode("ascii"), args.insim_port,
            Admin=b"",
            Prefix=b"!",
            UDPPort=args.udp_port,
            Flags=flags,
            Interval=args.mci_interval,
            IName=b"PACTTRACE",
        )
        for name in self.packets:
            self.insim.bind(KNOWN_PACKETS[name], self._make_handler(name))
        if args.outgauge_interval > 0:
            self.insim.bind(pyinsim.EVT_OUTGAUGE, self._on_outgauge)
        if args.outsim_interval > 0:
            self.insim.bind(pyinsim.EVT_OUTSIM, self._on_outsim)
        self.insim.bind(pyinsim.EVT_CLOSE, self._on_closed)
        self.insim.bind(pyinsim.EVT_ERROR, self._on_error)

        self._write_meta(flags)
        for name, interval in (("MCI", args.mci_interval), ("NLP", args.mci_interval)):
            if name in self.packets and interval <= 0:
                self._warn(f"{name} is in the packet list but the interval is 0 -- "
                           f"LFS will never send it")

        # Own OutGauge/OutSim stream on our UDP port -- independent of cfg.txt
        # and of whatever the add-on has open on 30000/29998.
        if args.outgauge_interval > 0:
            self.insim.send(pyinsim.ISP_SMALL, SubT=pyinsim.SMALL_SSG,
                            UVal=args.outgauge_interval)
        if args.outsim_interval > 0:
            self.insim.send(pyinsim.ISP_SMALL, SubT=pyinsim.SMALL_SSP,
                            UVal=args.outsim_interval)
        self.insim.send(pyinsim.ISP_TINY, ReqI=255, SubT=pyinsim.TINY_SST)
        self.insim.send(pyinsim.ISP_TINY, ReqI=255, SubT=pyinsim.TINY_NPL)

        self.control = ControlServer(self.args.control_port, {
            "ping": self._cmd_ping,
            "state": self._cmd_state,
            "marker": self._cmd_marker,
            "note": self._cmd_note,
            "stats": self._cmd_stats,
            "stop": self._cmd_stop,
        })
        self._log(f"tracing to {self.args.out}")
        self._log(f"packets: {','.join(self.packets)}")
        self._log(f"control port {self.control.port}, udp port {self.args.udp_port}")

        if self.args.duration > 0:
            timer = threading.Timer(self.args.duration, self.request_stop, ["duration reached"])
            timer.daemon = True
            timer.start()

    def run(self) -> None:
        """Block in the asyncore loop until stopped."""
        try:
            pyinsim.run()
        except KeyboardInterrupt:
            self.request_stop("interrupted")
        finally:
            self.finish()

    def request_stop(self, reason: str = "stop requested") -> None:
        """Stop from any thread: quiesce the streams, then empty the asyncore map."""
        if self._stopping.is_set():
            return
        self._stopping.set()
        self.writer.write(SRC_TRACER, "stopping", {"reason": reason})

        def _shutdown() -> None:
            try:
                if self.insim is not None:
                    # Stop our own streams so LFS is not left talking to a dead port.
                    if self.args.outgauge_interval > 0:
                        self.insim.send(pyinsim.ISP_SMALL, SubT=pyinsim.SMALL_SSG, UVal=0)
                    if self.args.outsim_interval > 0:
                        self.insim.send(pyinsim.ISP_SMALL, SubT=pyinsim.SMALL_SSP, UVal=0)
                    self.insim.send(pyinsim.ISP_TINY, ReqI=0, SubT=pyinsim.TINY_CLOSE)
            except Exception as exc:
                self.writer.write(SRC_TRACER, "warning",
                                  {"where": "shutdown", "error": f"{type(exc).__name__}: {exc}"})
            # Give the asyncore loop a moment to drain the send buffer, then
            # empty its socket map so run() returns.
            time.sleep(0.3)
            pyinsim.closeall()

        threading.Thread(target=_shutdown, name="tracer-shutdown", daemon=True).start()

    def finish(self) -> None:
        """Write the closing record and release everything. Idempotent."""
        if self.control is not None:
            self.control.stop()
            self.control = None
        if self.writer is None:
            return
        self.writer.write(SRC_TRACER, "end", {
            "wall_duration_s": round(time.time() - self._started_wall, 3),
            "trace_duration_s": round(self.writer.now(), 3),
            "records": self.writer.written,
            "dropped": self.writer.dropped,
            "counts": dict(self.writer.counts),
            "markers": [m["name"] for m in self.markers],
        })
        self.writer.close()
        self._log(f"done: {self.writer.written} records, {self.writer.dropped} dropped")
        self.writer = None

    # -- packet handlers ----------------------------------------------------
    def _make_handler(self, name: str):
        def handler(_insim: Any, packet: Any) -> None:
            # Timestamp first: everything after this is decoding cost.
            t = self.writer.now() if self.writer else 0.0
            try:
                data = packet_dump.packet_to_dict(name, packet)
            except Exception as exc:
                data = {"decode_error": f"{type(exc).__name__}: {exc}"}
            if name == "STA":
                self.last_state = data
            elif name == "CIM":
                self.last_cim = data
            self.packets_seen[name] = self.packets_seen.get(name, 0) + 1
            if self.writer is not None:
                self.writer.write(SRC_INSIM, name, data, t=t)
            if self.args.print_state and name in ("STA", "CIM"):
                self._log(f"{name}: {data.get('mode_name') or data.get('flags_flags')}")
        return handler

    def _on_outgauge(self, _insim: Any, packet: Any) -> None:
        t = self.writer.now() if self.writer else 0.0
        data = packet_dump.packet_to_dict("OutGauge", packet)
        if self.writer is not None:
            self.writer.write(SRC_OUTGAUGE, "OutGauge", data, t=t)

    def _on_outsim(self, _insim: Any, packet: Any) -> None:
        t = self.writer.now() if self.writer else 0.0
        data = packet_dump.packet_to_dict("OutSim", packet)
        if self.writer is not None:
            self.writer.write(SRC_OUTSIM, "OutSim", data, t=t)

    def _on_closed(self, _insim: Any) -> None:
        if self.writer is not None:
            self.writer.write(SRC_TRACER, "warning", {"where": "insim", "error": "connection closed"})
        self.request_stop("insim connection closed")

    def _on_error(self, _insim: Any) -> None:
        if self.writer is not None:
            self.writer.write(SRC_TRACER, "warning", {"where": "insim", "error": "socket error"})

    # -- control commands ---------------------------------------------------
    def _cmd_ping(self, _message: Dict[str, Any]) -> Dict[str, Any]:
        return {"trace": self.args.out, "t": round(self.writer.now(), 3) if self.writer else None}

    def _cmd_state(self, _message: Dict[str, Any]) -> Dict[str, Any]:
        state = self.last_state
        return {
            "t": round(self.writer.now(), 3) if self.writer else None,
            "have_state": bool(state),
            "on_track": state.get("on_track"),
            "flags": state.get("flags_flags", []),
            "cam": state.get("cam"),
            "track": state.get("Track"),
            "num_players": state.get("NumP"),
            "screen": self.last_cim.get("mode_name"),
            "submode": self.last_cim.get("submode_name"),
        }

    def _cmd_marker(self, message: Dict[str, Any]) -> Dict[str, Any]:
        name = str(message.get("name", "marker"))
        data = message.get("data") if isinstance(message.get("data"), dict) else {}
        t = self.writer.now() if self.writer else 0.0
        entry = {"name": name, "t": round(t, 3)}
        self.markers.append(entry)
        if self.writer is not None:
            self.writer.write(SRC_MARKER, name, dict(data), t=t)
        return entry

    def _cmd_note(self, message: Dict[str, Any]) -> Dict[str, Any]:
        if self.writer is not None:
            self.writer.write(SRC_TRACER, "note", {"text": str(message.get("text", ""))})
        return {}

    def _cmd_stats(self, _message: Dict[str, Any]) -> Dict[str, Any]:
        return {
            "records": self.writer.written if self.writer else 0,
            "dropped": self.writer.dropped if self.writer else 0,
            "counts": dict(self.writer.counts) if self.writer else {},
            "markers": [m["name"] for m in self.markers],
        }

    def _cmd_stop(self, message: Dict[str, Any]) -> Dict[str, Any]:
        self.request_stop(str(message.get("reason", "stop requested")))
        return {}

    # -- helpers ------------------------------------------------------------
    def _write_meta(self, flags: int) -> None:
        self.writer.write(SRC_TRACER, "meta", {
            "format": config.TRACE_FORMAT,
            "tracer": os.path.basename(os.path.abspath(__file__)),
            "scenario": self.args.scenario,
            "run_id": self.args.run_id,
            "started_utc": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
            "packets": list(self.packets),
            "insim_flags": flags,
            "mci_interval_ms": self.args.mci_interval,
            "outgauge_interval_ms": self.args.outgauge_interval,
            "outsim_interval_ms": self.args.outsim_interval,
            "insim_port": self.args.insim_port,
            "udp_port": self.args.udp_port,
            "control_port": self.args.control_port,
            "insim_version": getattr(pyinsim, "INSIM_VERSION", None),
            "python": sys.version.split()[0],
        }, t=0.0)

    def _warn(self, text: str) -> None:
        if self.writer is not None:
            self.writer.write(SRC_TRACER, "warning", {"where": "config", "error": text})
        self._log(f"warning: {text}")

    def _log(self, text: str) -> None:
        if not self.args.quiet:
            print(f"[tracer] {text}", file=sys.stderr, flush=True)


# ── CLI ──────────────────────────────────────────────────────────────────────
def parse_packet_list(spec: str) -> List[str]:
    names = [part.strip().upper() for part in spec.split(",") if part.strip()]
    unknown = [n for n in names if n not in KNOWN_PACKETS]
    if unknown:
        raise argparse.ArgumentTypeError(
            f"unknown packet(s): {', '.join(unknown)}. "
            f"Known: {', '.join(sorted(KNOWN_PACKETS))}")
    return names


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--out", help="path of the JSONL trace to write (required unless --list-packets)")
    parser.add_argument("--scenario", default="", help="scenario name, recorded in the meta record")
    parser.add_argument("--run-id", default="", help="free-form run identifier")
    parser.add_argument("--duration", type=float, default=0.0,
                        help="stop after N seconds (0 = until told to stop)")
    parser.add_argument("--packets", type=parse_packet_list, default=list(config.DEFAULT_PACKETS),
                        help="comma-separated packet names to log")
    parser.add_argument("--add-packets", type=parse_packet_list, default=[],
                        help="packets to add to the default set")
    parser.add_argument("--drop-packets", type=parse_packet_list, default=[],
                        help="packets to remove from the set")
    parser.add_argument("--mci-interval", type=int, default=config.MCI_INTERVAL_MS,
                        help="IS_MCI interval in ms")
    parser.add_argument("--outgauge-interval", type=int, default=config.OUTGAUGE_INTERVAL_MS,
                        help="OutGauge interval in ms (0 disables OutGauge)")
    parser.add_argument("--outsim-interval", type=int, default=0,
                        help="OutSim interval in ms (0 disables OutSim; try 100)")
    parser.add_argument("--host", default=config.LFS_HOST)
    parser.add_argument("--insim-port", type=int, default=config.INSIM_PORT)
    parser.add_argument("--udp-port", type=int, default=config.TRACER_UDP_PORT,
                        help="UDP port LFS streams MCI/OutGauge to for this connection")
    parser.add_argument("--control-port", type=int, default=config.CONTROL_PORT)
    parser.add_argument("--print-state", action="store_true",
                        help="echo IS_STA/IS_CIM changes to stderr while tracing")
    parser.add_argument("--quiet", action="store_true")
    parser.add_argument("--list-packets", action="store_true",
                        help="print every loggable packet name and exit")
    return parser


def normalise_packets(args: argparse.Namespace) -> None:
    selected = list(args.packets)
    for name in args.add_packets:
        if name not in selected:
            selected.append(name)
    for name in args.drop_packets:
        if name in selected:
            selected.remove(name)
    args.packets = selected


def main(argv: Optional[List[str]] = None) -> int:
    args = build_parser().parse_args(argv)
    if args.list_packets:
        print(", ".join(sorted(KNOWN_PACKETS)))
        return 0
    if not args.out:
        print("--out is required", file=sys.stderr)
        return 2
    normalise_packets(args)
    if not args.packets and args.outgauge_interval <= 0 and args.outsim_interval <= 0:
        print("nothing to trace: no packets and no OutGauge/OutSim", file=sys.stderr)
        return 2

    tracer = InSimTracer(args)

    def _on_signal(_signum, _frame):
        raise KeyboardInterrupt("signal")

    for signame in ("SIGINT", "SIGTERM", "SIGBREAK"):
        sig = getattr(signal, signame, None)
        if sig is None:
            continue
        try:
            signal.signal(sig, _on_signal)
        except (ValueError, OSError):
            pass

    try:
        tracer.start()
    except Exception as exc:
        print(f"[tracer] could not connect to LFS on {args.host}:{args.insim_port}: "
              f"{type(exc).__name__}: {exc}", file=sys.stderr)
        print("[tracer] is LFS running with InSim enabled? (/insim 29999)", file=sys.stderr)
        tracer.finish()
        return 1
    tracer.run()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
