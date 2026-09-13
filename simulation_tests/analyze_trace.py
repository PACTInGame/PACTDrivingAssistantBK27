#!/usr/bin/env python
"""Read a trace: summary, event timeline, signal extraction, CSV export.

    python simulation_tests/analyze_trace.py runs/04_.../trace.jsonl
    python simulation_tests/analyze_trace.py trace.jsonl --timeline
    python simulation_tests/analyze_trace.py trace.jsonl \
        --signal OutGauge.speed_kmh --signal OutGauge.Brake --csv brake.csv
    python simulation_tests/analyze_trace.py trace.jsonl --events CON,OBH --json

Signal paths are ``<event>.<field>`` into a record's ``d``. Lists are indexed by
position (``MCI.cars.0.speed_kmh``) or by a field match
(``MCI.cars[PLID=0].speed_kmh``), which is what you want for MCI because the car
order in a packet is not stable.

The summary calls out the two failure modes that make a run look like a bug in
the add-on when it is really a bad capture: **OutGauge stalls** (it stops dead
outside an internal view or in the pits -- conventions.md §5.3) and **dropped
records**.
"""

from __future__ import annotations

import argparse
import csv
import json
import os
import re
import sys
from typing import Any, Dict, Iterable, List, Optional, Sequence, Tuple

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from simulation_tests.trace_format import STREAM_EVENTS, load_trace  # noqa: E402

#: An OutGauge gap longer than this multiple of the requested interval is a stall.
STALL_FACTOR = 4.0
#: ... but never flag anything shorter than this.
MIN_STALL_S = 0.5

_TOKEN = re.compile(r"^(?P<name>[^.\[\]]+)(?:\[(?P<key>[^=\]]+)=(?P<value>[^\]]*)\])?$")


# ── path resolution ──────────────────────────────────────────────────────────
def resolve_path(data: Any, path: str) -> Any:
    """Follow a dotted path into a record payload. Returns None when absent."""
    current = data
    for token in path.split("."):
        match = _TOKEN.match(token)
        if match is None or current is None:
            return None
        name = match.group("name")
        if isinstance(current, list):
            try:
                current = current[int(name)]
            except (ValueError, IndexError):
                return None
        elif isinstance(current, dict):
            current = current.get(name)
        else:
            return None
        key, value = match.group("key"), match.group("value")
        if key is not None:
            current = _select(current, key, value)
    return current


def _select(items: Any, key: str, value: str) -> Any:
    """``[PLID=0]`` -- first list entry whose ``key`` equals ``value``."""
    if not isinstance(items, list):
        return None
    for item in items:
        if not isinstance(item, dict):
            continue
        candidate = item.get(key)
        if str(candidate) == value:
            return item
    return None


def split_signal(spec: str) -> Tuple[str, str]:
    """``"OutGauge.speed_kmh"`` -> ``("OutGauge", "speed_kmh")``."""
    if "." not in spec:
        raise ValueError(f"signal must be <event>.<field>, got {spec!r}")
    event, _, path = spec.partition(".")
    return event, path


def extract_signal(records: Sequence[Dict[str, Any]], spec: str) -> List[Tuple[float, Any]]:
    """``[(t, value), ...]`` for one signal, skipping records where it is absent."""
    event, path = split_signal(spec)
    out: List[Tuple[float, Any]] = []
    for record in records:
        if record.get("ev") != event:
            continue
        value = resolve_path(record.get("d", {}), path)
        if value is not None:
            out.append((record["t"], value))
    return out


# ── windows ──────────────────────────────────────────────────────────────────
def marker_times(records: Iterable[Dict[str, Any]]) -> List[Tuple[float, str]]:
    return [(r["t"], r["ev"]) for r in records if r.get("src") == "marker"]


def window(records: Sequence[Dict[str, Any]], start_marker: Optional[str],
           end_marker: Optional[str]) -> List[Dict[str, Any]]:
    """Records between two markers (inclusive of the markers themselves)."""
    start = 0.0
    end = float("inf")
    for t, name in marker_times(records):
        if start_marker and name == start_marker:
            start = t
        if end_marker and name == end_marker and t >= start:
            end = t
            break
    return [r for r in records if start <= r["t"] <= end]


def on_track_phases(records: Sequence[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """Windows during which IS_STA reported the car on track."""
    phases: List[Dict[str, Any]] = []
    current: Optional[Dict[str, Any]] = None
    for record in records:
        if record.get("ev") != "STA":
            continue
        data = record.get("d", {})
        on_track = bool(data.get("on_track"))
        if on_track and current is None:
            current = {"start": record["t"], "end": None,
                       "track": data.get("Track"), "cam": data.get("cam")}
        elif not on_track and current is not None:
            current["end"] = record["t"]
            phases.append(current)
            current = None
    if current is not None:
        current["end"] = records[-1]["t"] if records else current["start"]
        phases.append(current)
    return phases


def stream_gaps(records: Sequence[Dict[str, Any]], event: str,
                expected_interval_s: float) -> List[Dict[str, float]]:
    """Gaps in a fixed-rate stream that are long enough to mean it stalled."""
    threshold = max(MIN_STALL_S, expected_interval_s * STALL_FACTOR)
    times = [r["t"] for r in records if r.get("ev") == event]
    gaps = []
    for previous, current in zip(times, times[1:]):
        if current - previous > threshold:
            gaps.append({"from": round(previous, 3), "to": round(current, 3),
                         "gap_s": round(current - previous, 3)})
    return gaps


# ── summary ──────────────────────────────────────────────────────────────────
def summarise(path: str) -> Dict[str, Any]:
    """Everything worth knowing about a trace, as plain data."""
    records = load_trace(path)
    if not records:
        return {"trace": path, "error": "trace is empty"}

    meta = next((r["d"] for r in records if r.get("ev") == "meta"), {})
    end = next((r["d"] for r in reversed(records) if r.get("ev") == "end"), None)

    counts: Dict[str, int] = {}
    for record in records:
        counts[record["ev"]] = counts.get(record["ev"], 0) + 1

    og_interval = float(meta.get("outgauge_interval_ms") or 0) / 1000.0
    mci_interval = float(meta.get("mci_interval_ms") or 0) / 1000.0

    players: Dict[str, Dict[str, Any]] = {}
    for record in records:
        if record.get("ev") != "NPL":
            continue
        data = record.get("d", {})
        players[str(data.get("PLID"))] = {
            "plid": data.get("PLID"), "ucid": data.get("UCID"),
            "name": data.get("PName"), "car": data.get("CName"),
            "ptype": data.get("ptype_flags", []),
            "t": record["t"],
        }

    contacts = [
        {"t": r["t"],
         "plid_a": (r["d"].get("A") or {}).get("PLID"),
         "plid_b": (r["d"].get("B") or {}).get("PLID"),
         "closing_speed_kmh": r["d"].get("closing_speed_kmh")}
        for r in records if r.get("ev") == "CON"
    ]
    object_hits = [
        {"t": r["t"], "plid": r["d"].get("PLID"), "index": r["d"].get("Index"),
         "flags": r["d"].get("obhflags_flags", [])}
        for r in records if r.get("ev") == "OBH"
    ]

    warnings: List[str] = []
    if end is None:
        warnings.append("no 'end' record: the tracer was killed, the trace may be truncated")
    elif end.get("dropped"):
        warnings.append(f"{end['dropped']} records were dropped -- the trace has holes")
    if og_interval > 0 and not counts.get("OutGauge"):
        warnings.append("OutGauge was requested but never arrived "
                        "(camera not in an internal view, or in the pits?)")
    og_gaps = stream_gaps(records, "OutGauge", og_interval) if og_interval > 0 else []
    if og_gaps:
        worst = max(g["gap_s"] for g in og_gaps)
        warnings.append(f"OutGauge stalled {len(og_gaps)}x, worst {worst:.2f} s -- "
                        "assistance systems see nothing while that happens")
    mci_gaps = stream_gaps(records, "MCI", mci_interval) if mci_interval > 0 else []
    if mci_gaps:
        warnings.append(f"MCI stalled {len(mci_gaps)}x, worst "
                        f"{max(g['gap_s'] for g in mci_gaps):.2f} s")

    markers = [{"t": round(t, 3), "name": name} for t, name in marker_times(records)]
    phases = on_track_phases(records)
    speeds = [v for _, v in extract_signal(records, "OutGauge.speed_kmh")]

    return {
        "trace": path,
        "meta": meta,
        "records": len(records),
        "duration_s": round(records[-1]["t"], 3),
        "counts": counts,
        "markers": markers,
        "on_track_phases": [
            {"start": round(p["start"], 3), "end": round(p["end"], 3),
             "duration_s": round(p["end"] - p["start"], 3),
             "track": p["track"], "cam": p["cam"]} for p in phases],
        "players": list(players.values()),
        "contacts": contacts,
        "object_hits": object_hits,
        "outgauge_gaps": og_gaps,
        "mci_gaps": mci_gaps,
        "speed_kmh": {
            "max": round(max(speeds), 2) if speeds else None,
            "min": round(min(speeds), 2) if speeds else None,
            "samples": len(speeds),
        },
        "end": end,
        "warnings": warnings,
    }


def format_summary(summary: Dict[str, Any]) -> str:
    if summary.get("error"):
        return f"{summary['trace']}: {summary['error']}"
    lines: List[str] = []
    meta = summary.get("meta", {})
    lines.append(f"trace     : {summary['trace']}")
    lines.append(f"scenario  : {meta.get('scenario') or '-'}  "
                 f"(tracer {meta.get('tracer')}, {meta.get('started_utc')})")
    lines.append(f"duration  : {summary['duration_s']:.2f} s, "
                 f"{summary['records']} records")
    counts = summary.get("counts", {})
    lines.append("packets   : " + ", ".join(
        f"{name}={count}" for name, count in sorted(counts.items())))
    if summary.get("markers"):
        lines.append("markers   :")
        for marker in summary["markers"]:
            lines.append(f"  {marker['t']:8.2f}  {marker['name']}")
    if summary.get("on_track_phases"):
        lines.append("on track  :")
        for phase in summary["on_track_phases"]:
            lines.append(f"  {phase['start']:8.2f} -> {phase['end']:.2f} "
                         f"({phase['duration_s']:.2f} s) {phase['track']} cam={phase['cam']}")
    if summary.get("players"):
        lines.append("players   :")
        for player in summary["players"]:
            lines.append(f"  PLID {player['plid']}  {player['name']!r} "
                         f"car={player['car']} {','.join(player['ptype']) or '-'}")
    speed = summary.get("speed_kmh", {})
    if speed.get("samples"):
        lines.append(f"speed     : {speed['min']:.1f} .. {speed['max']:.1f} km/h "
                     f"({speed['samples']} OutGauge samples)")
    if summary.get("contacts"):
        lines.append("contacts  :")
        for contact in summary["contacts"]:
            lines.append(f"  {contact['t']:8.2f}  PLID {contact['plid_a']} <-> "
                         f"{contact['plid_b']}  closing {contact['closing_speed_kmh']} km/h")
    if summary.get("object_hits"):
        lines.append(f"object hits: {len(summary['object_hits'])}")
    for warning in summary.get("warnings", []):
        lines.append(f"WARNING   : {warning}")
    return "\n".join(lines)


# ── timeline ─────────────────────────────────────────────────────────────────
def describe_record(record: Dict[str, Any]) -> str:
    event, data = record["ev"], record.get("d", {})
    if record.get("src") == "marker":
        return f"MARKER {event} {json.dumps(data, ensure_ascii=False) if data else ''}".strip()
    if event == "STA":
        return (f"STA {'|'.join(data.get('flags_flags', []))} cam={data.get('cam')} "
                f"track={data.get('Track')} players={data.get('NumP')}")
    if event == "CIM":
        return f"CIM screen={data.get('mode_name')}/{data.get('submode_name')}"
    if event == "NPL":
        return (f"NPL PLID={data.get('PLID')} {data.get('PName')!r} car={data.get('CName')} "
                f"[{','.join(data.get('ptype_flags', []))}]")
    if event == "PLL":
        return f"PLL PLID={data.get('PLID')} left"
    if event == "PLP":
        return f"PLP PLID={data.get('PLID')} went to the garage"
    if event == "CON":
        return (f"CON PLID {(data.get('A') or {}).get('PLID')} <-> "
                f"{(data.get('B') or {}).get('PLID')} "
                f"closing {data.get('closing_speed_kmh')} km/h")
    if event == "OBH":
        return (f"OBH PLID={data.get('PLID')} index={data.get('Index')} "
                f"[{','.join(data.get('obhflags_flags', []))}]")
    if event == "MSO":
        return f"MSO [{data.get('user_type')}] {data.get('Msg')!r}"
    if event == "BTC":
        return f"BTC ClickID={data.get('ClickID')}"
    if event in ("meta", "end", "note", "warning", "stopping"):
        return f"{event} {json.dumps(data, ensure_ascii=False)}"
    return f"{event} {json.dumps(data, ensure_ascii=False)[:160]}"


def format_timeline(records: Sequence[Dict[str, Any]], include_streams: bool = False) -> str:
    lines = []
    for record in records:
        if not include_streams and record["ev"] in STREAM_EVENTS:
            continue
        lines.append(f"{record['t']:9.3f}  {describe_record(record)}")
    return "\n".join(lines)


# ── CLI ──────────────────────────────────────────────────────────────────────
def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("trace", help="path to trace.jsonl (or a run directory)")
    parser.add_argument("--timeline", action="store_true",
                        help="print every discrete event in time order")
    parser.add_argument("--all", action="store_true",
                        help="with --timeline: include the high-rate streams too")
    parser.add_argument("--signal", action="append", default=[],
                        help="extract <event>.<field>; repeatable")
    parser.add_argument("--csv", default="", help="write the extracted signals to a CSV file")
    parser.add_argument("--events", default="",
                        help="dump full records of these comma-separated events as JSON")
    parser.add_argument("--from-marker", default="", help="start at this marker")
    parser.add_argument("--to-marker", default="", help="stop at this marker")
    parser.add_argument("--json", action="store_true", help="print the summary as JSON")
    return parser


def _resolve_trace(spec: str) -> str:
    if os.path.isdir(spec):
        return os.path.join(spec, "trace.jsonl")
    return spec


def main(argv: Optional[List[str]] = None) -> int:
    args = build_parser().parse_args(argv)
    trace_path = _resolve_trace(args.trace)
    if not os.path.isfile(trace_path):
        print(f"no such trace: {trace_path}", file=sys.stderr)
        return 2

    records = load_trace(trace_path)
    if args.from_marker or args.to_marker:
        records = window(records, args.from_marker or None, args.to_marker or None)

    did_something = False
    if args.signal:
        did_something = True
        series = {spec: extract_signal(records, spec) for spec in args.signal}
        if args.csv:
            _write_csv(args.csv, series)
            print(f"wrote {args.csv}")
        else:
            for spec, values in series.items():
                print(f"# {spec}  ({len(values)} samples)")
                for t, value in values:
                    print(f"{t:9.3f}\t{value}")
    if args.events:
        did_something = True
        wanted = {name.strip().upper() for name in args.events.split(",") if name.strip()}
        for record in records:
            if record["ev"].upper() in wanted:
                print(json.dumps(record, ensure_ascii=False))
    if args.timeline:
        did_something = True
        print(format_timeline(records, include_streams=args.all))

    if not did_something or args.json:
        summary = summarise(trace_path)
        if args.json:
            print(json.dumps(summary, indent=2, ensure_ascii=False))
        else:
            print(format_summary(summary))
    return 0


def _write_csv(path: str, series: Dict[str, List[Tuple[float, Any]]]) -> None:
    """Merge the signals onto one time column; blank where a signal has no sample."""
    columns = list(series)
    rows: Dict[float, Dict[str, Any]] = {}
    for spec in columns:
        for t, value in series[spec]:
            rows.setdefault(t, {})[spec] = value
    directory = os.path.dirname(os.path.abspath(path))
    if directory:
        os.makedirs(directory, exist_ok=True)
    with open(path, "w", encoding="utf-8", newline="") as fh:
        writer = csv.writer(fh)
        writer.writerow(["t"] + columns)
        for t in sorted(rows):
            writer.writerow([f"{t:.3f}"] + [rows[t].get(spec, "") for spec in columns])


if __name__ == "__main__":
    raise SystemExit(main())
