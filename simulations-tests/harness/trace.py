"""One receive-time axis for asynchronous packets, inputs and markers."""
import json
import threading
import time
from collections import Counter
from datetime import datetime, timezone


def plain(value):
    if isinstance(value, bytes):
        return {"hex": value.hex(), "text": value.decode("latin-1")}
    if isinstance(value, (str, int, float, bool)) or value is None:
        return value
    if isinstance(value, (list, tuple)):
        return [plain(v) for v in value]
    if isinstance(value, dict):
        return {str(k): plain(v) for k, v in value.items()}
    return {k: plain(v) for k, v in vars(value).items() if not k.startswith("_")}


class Trace:
    def __init__(self, path, clock=time.perf_counter_ns):
        self.clock = clock
        self.origin = clock()
        self.lock = threading.Lock()
        self.counts = Counter()
        self.sequence = 0
        self.file = path.open("x", encoding="utf-8")
        self.write("session", {"utc": datetime.now(timezone.utc).isoformat(),
                               "schema": 1, "origin_ns": self.origin})

    def now(self):
        return (self.clock() - self.origin) / 1e9

    def write(self, kind, data, **extra):
        with self.lock:
            row = dict(seq=self.sequence, t=self.now(), kind=kind,
                       data=plain(data), **extra)
            self.file.write(json.dumps(row, ensure_ascii=True, allow_nan=False) + "\n")
            self.counts[kind] += 1
            self.sequence += 1
            return row["t"]

    def close(self):
        self.file.flush()
        self.file.close()
