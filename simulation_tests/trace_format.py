"""The trace file format: one JSON object per line, ordered by capture time.

Why JSONL and not a binary/CSV format: the sources run at different rates and
carry different fields (OutGauge every 50 ms, MCI every 100 ms, IS_CON only when
two cars touch), so a fixed column layout would be mostly empty. One
self-describing record per line keeps the **temporal order** intact — that is the
one property the whole harness depends on — and stays greppable.

Record shape::

    {"t": 12.345, "src": "outgauge", "ev": "OutGauge", "d": {...}}

``t``   seconds since the tracer's epoch, monotonic, 3 decimals.
``src`` where it came from: ``insim`` | ``outgauge`` | ``outsim`` | ``marker`` | ``tracer``.
``ev``  event name: the InSim packet name (``MCI``, ``STA``, ``CON``), ``OutGauge``,
        ``OutSim``, a marker name, or one of ``meta`` / ``end`` / ``warning``.
``d``   the payload; for packets the decoded fields plus derived SI values.

The first record of every trace is ``ev="meta"``, the last is ``ev="end"``.
A trace that has no ``end`` record was cut off — treat it as suspect.
"""

from __future__ import annotations

import json
import os
import queue
import threading
import time
from typing import Any, Callable, Dict, Iterable, Iterator, Optional

SRC_INSIM = "insim"
SRC_OUTGAUGE = "outgauge"
SRC_OUTSIM = "outsim"
SRC_MARKER = "marker"
SRC_TRACER = "tracer"

#: Streamed at a fixed rate; the analyzer hides these from event timelines.
STREAM_EVENTS = frozenset({"OutGauge", "OutSim", "MCI", "NLP"})

_SENTINEL = object()


class TraceWriter:
    """Serialises records to a JSONL file from a single writer thread.

    Records are timestamped **by the caller at capture time** (or here, at
    :meth:`write`), never by the writer thread, so queueing can never reorder the
    trace relative to reality.

    The writer thread exists so a slow disk cannot stall pyinsim's asyncore loop:
    a blocked packet handler means missed keep-alives and a dropped InSim
    connection (see reference/insim.md §5).
    """

    def __init__(self, path: str, clock: Callable[[], float] = time.monotonic,
                 flush_interval: float = 0.5, max_queue: int = 20000):
        self._path = path
        self._clock = clock
        self._epoch = clock()
        self._flush_interval = flush_interval
        self._queue: "queue.Queue" = queue.Queue(maxsize=max_queue)
        self._closed = threading.Event()
        self.written = 0
        self.dropped = 0
        self.counts: Dict[str, int] = {}
        directory = os.path.dirname(os.path.abspath(path))
        if directory:
            os.makedirs(directory, exist_ok=True)
        self._fh = open(path, "w", encoding="utf-8", newline="\n")
        self._thread = threading.Thread(target=self._run, name="trace-writer", daemon=True)
        self._thread.start()

    # -- public API ---------------------------------------------------------
    @property
    def path(self) -> str:
        return self._path

    def now(self) -> float:
        """Seconds since the trace epoch."""
        return self._clock() - self._epoch

    def write(self, src: str, ev: str, data: Optional[Dict[str, Any]] = None,
              t: Optional[float] = None) -> None:
        """Queue one record. Never raises, never blocks on a full queue."""
        record = {
            "t": round(self.now() if t is None else t, 3),
            "src": src,
            "ev": ev,
            "d": data if data is not None else {},
        }
        self.counts[ev] = self.counts.get(ev, 0) + 1
        try:
            self._queue.put_nowait(record)
        except queue.Full:
            # Losing a record beats stalling the packet loop. Count it so the
            # run summary can say the trace is incomplete.
            self.dropped += 1

    def close(self, timeout: float = 5.0) -> None:
        """Drain the queue and close the file. Idempotent."""
        if self._closed.is_set():
            return
        self._closed.set()
        self._queue.put(_SENTINEL)
        self._thread.join(timeout)
        try:
            self._fh.close()
        except OSError:
            pass

    # -- writer thread ------------------------------------------------------
    def _run(self) -> None:
        last_flush = time.monotonic()
        while True:
            try:
                item = self._queue.get(timeout=0.25)
            except queue.Empty:
                item = None
            if item is _SENTINEL:
                self._drain()
                self._safe_flush()
                return
            if item is not None:
                self._emit(item)
            now = time.monotonic()
            if now - last_flush >= self._flush_interval:
                self._safe_flush()
                last_flush = now

    def _drain(self) -> None:
        while True:
            try:
                item = self._queue.get_nowait()
            except queue.Empty:
                return
            if item is _SENTINEL:
                continue
            self._emit(item)

    def _emit(self, record: Dict[str, Any]) -> None:
        try:
            self._fh.write(json.dumps(record, separators=(",", ":"), default=_fallback))
            self._fh.write("\n")
            self.written += 1
        except (OSError, ValueError):
            self.dropped += 1

    def _safe_flush(self) -> None:
        try:
            self._fh.flush()
        except OSError:
            pass


def _fallback(obj: Any) -> Any:
    """Last resort for anything json cannot encode — never fail a trace over it."""
    if isinstance(obj, (bytes, bytearray)):
        return obj.hex()
    return repr(obj)


def read_trace(path: str) -> Iterator[Dict[str, Any]]:
    """Yield every well-formed record of a trace, skipping a torn last line."""
    with open(path, "r", encoding="utf-8") as fh:
        for line in fh:
            line = line.strip()
            if not line:
                continue
            try:
                record = json.loads(line)
            except ValueError:
                continue  # a run killed mid-write leaves one partial line
            if isinstance(record, dict) and "t" in record and "ev" in record:
                yield record


def load_trace(path: str) -> list:
    return list(read_trace(path))


def meta_of(records: Iterable[Dict[str, Any]]) -> Dict[str, Any]:
    for record in records:
        if record.get("ev") == "meta":
            return record.get("d", {})
    return {}
