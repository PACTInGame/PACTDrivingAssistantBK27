"""Optional raw UDP fan-out when cfg.txt streams need multiple consumers.

Run before LFS. Configure OutGauge Port 31000 and OutSim Port 30998 once,
with LFS closed. The add-on still receives on its normal 30000 / 29998.
"""
import argparse
import select
import socket
import time


def forward(sender, payload, destinations):
    for port in destinations:
        sender.sendto(payload, ("127.0.0.1", port))


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--monitor-port", type=int, default=30001)
    parser.add_argument("--seconds", type=float, default=3600)
    args = parser.parse_args()
    if not 1024 <= args.monitor_port <= 65535 or args.monitor_port in (31000, 30998, 30000, 29998):
        parser.error("Monitor port must be distinct from relay and add-on ports")
    if not 0 < args.seconds <= 86400:
        parser.error("Duration must be in (0, 86400] seconds")
    receivers = {}
    counts = {31000: 0, 30998: 0}
    sender = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        if hasattr(socket, "SIO_UDP_CONNRESET"):
            sender.ioctl(socket.SIO_UDP_CONNRESET, False)
        for source, addon in ((31000, 30000), (30998, 29998)):
            receiver = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            receivers[receiver] = (source, addon)
            receiver.bind(("127.0.0.1", source))
        print("UDP relay active: 31000 -> 30000 + monitor; 30998 -> 29998 + monitor", flush=True)
        deadline = time.monotonic() + args.seconds
        while time.monotonic() < deadline:
            ready, _, _ = select.select(list(receivers), [], [], min(.2, max(0, deadline - time.monotonic())))
            for receiver in ready:
                data, address = receiver.recvfrom(65535)
                source, addon = receivers[receiver]
                forward(sender, data, (addon, args.monitor_port))
                counts[source] += 1
    except KeyboardInterrupt:
        pass
    finally:
        for receiver in receivers:
            receiver.close()
        sender.close()
        print(f"Forwarded packets: {counts}")


if __name__ == "__main__":
    main()
