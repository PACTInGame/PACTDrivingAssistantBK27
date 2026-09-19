#!/usr/bin/env python
"""Replay a recorded scenario without starting a tracer.

    python simulation_tests/replay_scenario.py 04_drive_and_stop

Use this to check a fresh recording plays back correctly. For an actual test run
use ``run_scenario.py``, which starts the tracer and writes the trace.

If a tracer is already running (started by hand), pass ``--markers`` and the
scenario's markers are pushed into its trace as well.

Safety: the replay puts LFS in the foreground itself before the first event and
takes it back if it is lost mid-run (only a raise that fails falls through to
``--on-focus-loss``), and it releases every key and mouse button it holds on any
exit path. **Pause** aborts it at any time.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
from typing import List, Optional

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from simulation_tests import config, input_model, paths, player as player_mod  # noqa: E402
from simulation_tests import scenario as scenario_mod  # noqa: E402
from simulation_tests import pynput_access  # noqa: E402
from simulation_tests.control_channel import ControlClient  # noqa: E402


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("scenario", help="scenario name or path")
    parser.add_argument("--speed", type=float, default=1.0,
                        help="replay speed multiplier (1.0 = as recorded)")
    parser.add_argument("--countdown", type=int, default=5,
                        help="seconds before the replay starts (alt-tab into LFS)")
    parser.add_argument("--abort-key", default=config.DEFAULT_ABORT_KEY)
    parser.add_argument("--on-focus-loss", choices=("abort", "pause", "ignore"),
                        default="abort")
    parser.add_argument("--no-focus-check", action="store_true",
                        help="do not raise LFS and do not watch the foreground "
                             "at all (unsafe: input can land in another window)")
    parser.add_argument("--force", action="store_true",
                        help="run even though pre-flight found problems")
    parser.add_argument("--markers", action="store_true",
                        help="push markers into an already running tracer")
    parser.add_argument("--control-port", type=int, default=config.CONTROL_PORT)
    return parser


def countdown(seconds: int) -> None:
    for remaining in range(seconds, 0, -1):
        print(f"  starting in {remaining}...", end="\r", flush=True)
        time.sleep(1.0)
    print("  replaying -- press the abort key to stop.        ")


def main(argv: Optional[List[str]] = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        scenario_path = paths.scenario_dir(args.scenario)
    except (ValueError, FileNotFoundError) as exc:
        print(exc, file=sys.stderr)
        return 2
    why_disabled = scenario_mod.disabled_reason(scenario_mod.load(scenario_path))
    if why_disabled and not args.force:
        print(f"{args.scenario} is disabled: {why_disabled}", file=sys.stderr)
        print("re-enable it in scenario.json, or pass --force to replay it anyway",
              file=sys.stderr)
        return 8
    input_path = os.path.join(scenario_path, paths.INPUT_FILE)
    if not os.path.isfile(input_path):
        print(f"no recording at {input_path} -- record the scenario first", file=sys.stderr)
        return 2

    meta, events = input_model.read_recording(input_path)
    control = ControlClient(args.control_port) if args.markers else None
    if control is not None and not control.ping():
        print(f"warning: no tracer answering on control port {args.control_port}; "
              "markers will be dropped", file=sys.stderr)

    play = player_mod.Player(
        meta, events,
        speed=args.speed,
        require_focus=not args.no_focus_check,
        on_focus_loss=args.on_focus_loss,
        abort_key=args.abort_key,
        control=control,
    )
    problems = play.preflight()
    for problem in problems:
        print(f"  pre-flight: {problem}", file=sys.stderr)
    if problems and not args.force:
        print("refusing to replay; fix the above or pass --force", file=sys.stderr)
        return 3

    print(f"Replaying '{meta.get('scenario') or args.scenario}': "
          f"{len(events)} events, {input_model.duration(events):.1f} s "
          f"at {args.speed}x")
    countdown(args.countdown)
    try:
        result = play.play()
    except pynput_access.ReplayUnavailable as exc:
        print(exc, file=sys.stderr)
        return 2
    print(json.dumps(result, indent=2))
    if control is not None:
        control.close()
    return 1 if result["aborted"] else 0


if __name__ == "__main__":
    raise SystemExit(main())
