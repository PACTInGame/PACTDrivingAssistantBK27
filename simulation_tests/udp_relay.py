#!/usr/bin/env python
"""Optional UDP fan-out, for when ``SMALL_SSG`` is not enough.

**Normally you do not need this.** The tracer asks LFS for its *own* OutGauge and
OutSim stream over its own InSim connection (``SMALL_SSG`` / ``SMALL_SSP``, see
``insim_trace.py``), on :data:`config.TRACER_UDP_PORT`. That is a per-connection
setting, so the add-on's cfg.txt streams on 30000/29998 keep running untouched
and two processes never compete for one datagram.

Use this only if that turns out not to hold on the installed LFS version -- if a
driving trace has no ``OutGauge`` records while the add-on's gauges are clearly
live. Then, **with LFS closed**, back up ``cfg.txt`` and change only::

    OutGauge Port 31000        (was 30000)
    OutSim Port   30998        (was 29998)

leaving both IPs at 127.0.0.1 and every mode/delay/option alone. Start this relay
**before** LFS; it forwards each datagram unchanged to the add-on's original port
*and* to the tracer, decoding nothing::

    python simulation_tests/udp_relay.py
    python simulation_tests/run_scenario.py 04_drive_and_stop   # in another shell

Then run the tracer with ``--outgauge-interval 0 --outsim-interval 0`` so it does
not also ask LFS for a second stream.

**The relay must stay running the whole time that cfg.txt is in place**, including
outside tests -- otherwise the add-on gets no OutGauge and every assistance system
silently does nothing (reference/lfs-setup.md). Restore the original ports, with
LFS closed, when you no longer want this setup. Nothing here edits cfg.txt; if the
setup wizard rewrites those ports, check the configuration again.

Never instead bind a second listener to 30000/29998 and hope both processes see
every packet: one of them will miss datagrams, unpredictably.
"""

from __future__ import annotations

import argparse
import os
import select
import socket
import sys
import time
from typing import Dict, List, Tuple

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from simulation_tests import config  # noqa: E402

#: ``(port LFS now sends to, port the add-on still listens on)``.
DEFAULT_ROUTES: Tuple[Tuple[int, int], ...] = (
    (31000, 30000),   # OutGauge
    (30998, 29998),   # OutSim
)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--tracer-port", type=int, default=config.TRACER_UDP_PORT,
                        help="second destination: the port the tracer listens on")
    parser.add_argument("--seconds", type=float, default=3600.0,
                        help="stop after N seconds (Ctrl+C also stops it)")
    parser.add_argument("--quiet", action="store_true")
    return parser


def relay(tracer_port: int, seconds: float,
          routes: Tuple[Tuple[int, int], ...] = DEFAULT_ROUTES,
          quiet: bool = False) -> Dict[int, int]:
    """Forward every datagram to the add-on's port and the tracer's. Returns counts."""
    counts: Dict[int, int] = {source: 0 for source, _ in routes}
    receivers: Dict[socket.socket, Tuple[int, int]] = {}
    sender = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        for source, addon_port in routes:
            receiver = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            if hasattr(socket, "SIO_UDP_CONNRESET"):
                # Windows: an ICMP "port unreachable" from a destination that is
                # not listening must not kill the receiving socket.
                receiver.ioctl(socket.SIO_UDP_CONNRESET, False)
            receiver.bind(("127.0.0.1", source))
            receivers[receiver] = (source, addon_port)
        if not quiet:
            for source, addon_port in routes:
                print(f"[relay] {source} -> {addon_port} + {tracer_port}", flush=True)
        deadline = time.monotonic() + seconds
        sockets: List[socket.socket] = list(receivers)
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                break
            ready, _, _ = select.select(sockets, [], [], min(0.2, remaining))
            for receiver in ready:
                source, addon_port = receivers[receiver]
                try:
                    data, _address = receiver.recvfrom(65535)
                except OSError:
                    continue
                for destination in (addon_port, tracer_port):
                    try:
                        sender.sendto(data, ("127.0.0.1", destination))
                    except OSError:
                        pass  # a consumer that is not up yet must not stop the relay
                counts[source] += 1
    except KeyboardInterrupt:
        pass
    finally:
        for receiver in receivers:
            receiver.close()
        sender.close()
    return counts


def main(argv=None) -> int:
    args = build_parser().parse_args(argv)
    reserved = {port for pair in DEFAULT_ROUTES for port in pair}
    if not 1024 <= args.tracer_port <= 65535 or args.tracer_port in reserved:
        print(f"--tracer-port must be free and none of {sorted(reserved)}", file=sys.stderr)
        return 2
    if not 0 < args.seconds <= 86400:
        print("--seconds must be in (0, 86400]", file=sys.stderr)
        return 2
    counts = relay(args.tracer_port, args.seconds, quiet=args.quiet)
    if not args.quiet:
        print(f"[relay] forwarded: {counts}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
