"""Stream a JSONL trace into packet statistics or a flat per-packet CSV."""
import argparse
import csv
import json
from collections import Counter
from pathlib import Path


def rows(path):
    with path.open(encoding="utf-8") as handle:
        for number, line in enumerate(handle, 1):
            try:
                yield json.loads(line)
            except ValueError as exc:
                raise ValueError(f"Invalid JSON at line {number}; trace may be incomplete") from exc


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("trace", type=Path)
    parser.add_argument("--packet", default="OutGaugePack")
    parser.add_argument("--csv", type=Path)
    args = parser.parse_args()
    stats, latest, max_gap = Counter(), {}, {}
    fields = set()
    origin = None
    for row in rows(args.trace):
        kind, t = row["kind"], row["t"]
        stats[kind] += 1
        if kind in latest:
            max_gap[kind] = max(max_gap.get(kind, 0), t - latest[kind])
        latest[kind] = t
        if kind in ("replay_start", "record_start"):
            origin = row.get("origin_t", t)
        if kind == args.packet and isinstance(row["data"], dict):
            fields.update(row["data"])
    print(json.dumps({"counts": dict(stats), "max_receive_gap_seconds": max_gap,
                      "input_origin_t": origin, "functional_verdict": "not_evaluated",
                      "missing_driving_streams": [p for p in ("IS_MCI", "OutGaugePack", "OutSimPack") if not stats[p]]}, indent=2))
    if args.csv:
        if not fields:
            parser.error(f"No {args.packet} packets; cannot export a measurement")
        with args.csv.open("x", newline="", encoding="utf-8") as handle:
            writer = csv.DictWriter(handle, fieldnames=["seq", "receive_t", "scenario_t", *sorted(fields)])
            writer.writeheader()
            for row in rows(args.trace):
                if row["kind"] == args.packet:
                    writer.writerow({"seq": row["seq"], "receive_t": row["t"],
                                     "scenario_t": "" if origin is None else row["t"] - origin,
                                     **{k: json.dumps(v) if isinstance(v, (dict, list)) else v
                                        for k, v in row["data"].items()}})


if __name__ == "__main__":
    main()
