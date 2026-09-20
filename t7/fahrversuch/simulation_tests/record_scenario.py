#!/usr/bin/env python
"""Record a new test scenario: mouse + keyboard, plus a draft timeline.

    python simulation_tests/record_scenario.py 04_drive_and_stop \
        --description "Start, drive off, stop again, back to the menu"

What happens:

1. a countdown gives you time to alt-tab into LFS (start at the **main menu**);
2. everything you do is recorded -- mouse position on a 50 ms grid, keys and
   clicks with their real timestamps;
3. press **Scroll Lock** whenever something noteworthy happens: that drops a
   named marker, which both the timeline and the trace get;
4. press **Pause** to stop (end at the **main menu** again).

Written into ``scenarios/<name>/``: ``input.jsonl``, ``timeline.draft.md`` and,
if it does not exist yet, a ``scenario.json`` you can edit.
"""

from __future__ import annotations

import argparse
import os
import sys
import time
from typing import List, Optional

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from simulation_tests import config, input_model, paths, recorder as recorder_mod  # noqa: E402
from simulation_tests import scenario as scenario_mod, timeline_draft, win_focus  # noqa: E402


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("name", help="scenario directory name, e.g. 04_drive_and_stop")
    parser.add_argument("--description", default="", help="one line describing the scenario")
    parser.add_argument("--countdown", type=int, default=5,
                        help="seconds before recording starts (alt-tab into LFS)")
    parser.add_argument("--sample-interval", type=int, default=config.INPUT_SAMPLE_INTERVAL_MS,
                        help="mouse sampling interval in ms")
    parser.add_argument("--marker-names", default="",
                        help="comma-separated names for the markers, in the order you press them")
    parser.add_argument("--marker-key", default=config.DEFAULT_MARKER_KEY)
    parser.add_argument("--stop-key", default=config.DEFAULT_STOP_KEY)
    parser.add_argument("--dense", action="store_true",
                        help="record every sample tick, not only mouse movement")
    parser.add_argument("--overwrite", action="store_true",
                        help="replace an existing input.jsonl")
    return parser


def main(argv: Optional[List[str]] = None) -> int:
    args = build_parser().parse_args(argv)

    if os.path.sep in args.name:
        print("scenario name must be a plain directory name", file=sys.stderr)
        return 2
    target = os.path.join(paths.SCENARIOS_DIR, args.name)
    input_path = os.path.join(target, paths.INPUT_FILE)
    if os.path.exists(input_path) and not args.overwrite:
        print(f"{input_path} already exists -- pass --overwrite to replace it", file=sys.stderr)
        return 2

    if not win_focus.IS_WINDOWS:
        print("recording needs Windows (global mouse/keyboard hooks + LFS)", file=sys.stderr)
        return 2
    if not win_focus.find_windows(config.LFS_WINDOW_MATCH):
        print("warning: no LFS window found -- recording anyway", file=sys.stderr)

    marker_names = [part.strip() for part in args.marker_names.split(",") if part.strip()]
    if not marker_names and os.path.isfile(os.path.join(target, paths.SCENARIO_FILE)):
        # A scenario stub lists the markers it wants, in order; pressing the
        # marker key then names them without anyone typing anything.
        marker_names = list(scenario_mod.load(target).get("markers") or [])
    rec = recorder_mod.Recorder(
        sample_interval_ms=args.sample_interval,
        marker_key=args.marker_key,
        stop_key=args.stop_key,
        marker_names=marker_names,
        dense=args.dense,
    )

    print(f"Recording '{args.name}'.")
    if marker_names:
        print(f"  markers    : {' -> '.join(marker_names)}")
    print(f"  marker key : {args.marker_key}")
    print(f"  stop key   : {args.stop_key}")
    print("  Start at the LFS main menu, and end there too.")
    for remaining in range(args.countdown, 0, -1):
        print(f"  starting in {remaining}...", end="\r", flush=True)
        time.sleep(1.0)
    print("  recording -- press the stop key when done.        ")

    def status(elapsed: float, events: int, markers: int) -> None:
        print(f"  {elapsed:7.1f} s | {events:6d} events | {markers} markers",
              end="\r", flush=True)

    events = rec.record(on_status=status)
    print()

    meta = recorder_mod.build_meta(rec, events, scenario=args.name,
                                   note=args.description)
    os.makedirs(target, exist_ok=True)
    written = input_model.write_recording(input_path, meta, events)

    draft_path = os.path.join(target, paths.TIMELINE_DRAFT_FILE)
    with open(draft_path, "w", encoding="utf-8", newline="\n") as fh:
        fh.write(timeline_draft.render(meta, events, scenario=args.name))

    scenario_path = os.path.join(target, paths.SCENARIO_FILE)
    if not os.path.exists(scenario_path):
        data = scenario_mod.default_scenario(args.name, args.description)
        data["duration_s"] = meta["duration_s"]
        scenario_mod.save(target, data)
    else:
        data = scenario_mod.load(target)
        data["duration_s"] = meta["duration_s"]
        scenario_mod.save(target, data)

    print(f"\nRecorded {written} events over {meta['duration_s']:.1f} s "
          f"({meta['marker_count']} markers).")
    print(f"  {input_path}")
    print(f"  {draft_path}")
    print(f"  {scenario_path}")
    print("\nNext: fill in the 'expected' column of the draft, rename it to "
          f"{paths.TIMELINE_FILE}, then run:")
    print(f"  python simulation_tests/run_scenario.py {args.name}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
