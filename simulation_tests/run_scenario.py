#!/usr/bin/env python
"""Run one scenario end to end: tracer + replay + trace + summary.

    python simulation_tests/run_scenario.py 04_drive_and_stop

Sequence:

1. start the tracer as a **separate process** (it needs its own InSim connection
   and its own asyncore loop);
2. wait until it answers on the control channel, and -- unless the scenario says
   otherwise -- until LFS reports the main menu, because that is where every
   scenario starts;
3. mark ``scenario_start`` in the trace, replay the recorded input, mark
   ``scenario_end``;
4. keep tracing for ``tail_s`` more seconds so a late reaction is still caught;
5. stop the tracer, write ``run.json``, print the analyser's summary.

Everything lands in ``runs/<scenario>_<timestamp>/``.

To measure something the base tracer does not log, copy ``insim_trace.py`` into
``_temp/``, edit the copy, and pass ``--tracer _temp/my_tracer.py``. The scenario
itself stays untouched.
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import sys
import time
from typing import Any, Dict, List, Optional

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from simulation_tests import analyze_trace, config, input_model, paths  # noqa: E402
from simulation_tests import player as player_mod, scenario as scenario_mod  # noqa: E402
from simulation_tests import pynput_access  # noqa: E402
from simulation_tests.control_channel import ControlClient  # noqa: E402

TRACER_STARTUP_TIMEOUT_S = 15.0
TRACER_SHUTDOWN_TIMEOUT_S = 15.0


def resolve_tracer(spec: str) -> str:
    """Absolute path of the tracer script, accepting a bare name or a path."""
    if os.path.isabs(spec):
        return spec
    for base in (os.getcwd(), paths.PACKAGE_DIR):
        candidate = os.path.join(base, spec)
        if os.path.isfile(candidate):
            return os.path.abspath(candidate)
    return os.path.abspath(spec)


def wait_for_tracer(control: ControlClient, timeout: float) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if control.ping():
            return True
        time.sleep(0.25)
    return False


def wait_for_main_menu(control: ControlClient, timeout: float) -> Dict[str, Any]:
    """Poll IS_STA until LFS is on the entry screen. Returns the last state seen."""
    deadline = time.monotonic() + timeout
    state: Dict[str, Any] = {}
    while time.monotonic() < deadline:
        state = control.state() or {}
        if state.get("have_state") and "FRONT_END" in (state.get("flags") or []):
            state["at_main_menu"] = True
            return state
        time.sleep(0.3)
    state["at_main_menu"] = bool(state.get("have_state")) and \
        "FRONT_END" in (state.get("flags") or [])
    return state


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("scenario", nargs="?", help="scenario name or path")
    parser.add_argument("--list", action="store_true",
                        help="list the available scenarios and exit")
    parser.add_argument("--tracer", default="insim_trace.py",
                        help="tracer script to run (use a copy in _temp/ to measure more)")
    parser.add_argument("--speed", type=float, default=1.0)
    parser.add_argument("--countdown", type=int, default=5,
                        help="seconds before the replay starts (alt-tab into LFS)")
    parser.add_argument("--out-dir", default="",
                        help="run directory (default: runs/<scenario>_<timestamp>)")
    parser.add_argument("--control-port", type=int, default=config.CONTROL_PORT)
    parser.add_argument("--host", default=config.LFS_HOST)
    parser.add_argument("--insim-port", type=int, default=config.INSIM_PORT)
    parser.add_argument("--udp-port", type=int, default=config.TRACER_UDP_PORT,
                        help="UDP port LFS streams MCI/OutGauge to for the tracer")
    parser.add_argument("--abort-key", default=config.DEFAULT_ABORT_KEY)
    parser.add_argument("--on-focus-loss", choices=("abort", "pause", "ignore"),
                        default="abort")
    parser.add_argument("--no-focus-check", action="store_true",
                        help="do not raise LFS and do not watch the foreground "
                             "at all (unsafe: input can land in another window)")
    parser.add_argument("--strict-timing", action="store_true",
                        help=f"abort if an event is dispatched more than "
                             f"{player_mod.Player.LATE_BUDGET_S:g} s past its deadline")
    parser.add_argument("--no-wait-menu", action="store_true",
                        help="do not wait for the LFS main menu before replaying")
    parser.add_argument("--force", action="store_true",
                        help="run even though pre-flight found problems")
    parser.add_argument("--tail", type=float, default=None,
                        help="override the scenario's post-replay trace tail, in seconds")
    parser.add_argument("--require", action="append", default=[], metavar="EVENT",
                        help="fail the run if this trace event never arrived, e.g. "
                             "--require OutGauge --require MCI. Repeatable.")
    parser.add_argument("--no-summary", action="store_true",
                        help="do not print the trace summary at the end")
    return parser


def main(argv: Optional[List[str]] = None) -> int:  # noqa: C901 - a linear script
    args = build_parser().parse_args(argv)
    if args.list:
        for name in paths.list_scenarios():
            data = scenario_mod.load(paths.scenario_dir(name))
            recorded = "recorded" if scenario_mod.has_recording(
                paths.scenario_dir(name)) else "NOT RECORDED"
            why = scenario_mod.disabled_reason(data)
            if why:
                recorded = "DISABLED"
            print(f"{name:<32} [{recorded}]  {data['description']}")
            if why:
                print(f"{'':<32}   disabled: {why}")
        return 0
    if not args.scenario:
        print("give a scenario name, or --list to see them", file=sys.stderr)
        return 2
    try:
        scenario_path = paths.scenario_dir(args.scenario)
    except (ValueError, FileNotFoundError) as exc:
        print(exc, file=sys.stderr)
        return 2
    scenario = scenario_mod.load(scenario_path)
    why_disabled = scenario_mod.disabled_reason(scenario)
    if why_disabled and not args.force:
        # Before the tracer starts and before anything is injected: a disabled
        # scenario costs nothing here, and running it anyway can leave LFS
        # somewhere that breaks the next scenario in a batch.
        print(f"{scenario['name']} is disabled: {why_disabled}", file=sys.stderr)
        print("re-enable it in scenario.json, or pass --force to run it anyway",
              file=sys.stderr)
        return 8
    if why_disabled:
        print(f"  note: running a disabled scenario ({why_disabled})", file=sys.stderr)
    input_path = os.path.join(scenario_path, paths.INPUT_FILE)
    if not os.path.isfile(input_path):
        print(f"no recording at {input_path} -- record the scenario first", file=sys.stderr)
        return 2
    meta, events = input_model.read_recording(input_path)

    run_dir = args.out_dir or paths.new_run_dir(scenario["name"])
    os.makedirs(run_dir, exist_ok=True)
    trace_path = os.path.join(run_dir, "trace.jsonl")
    tracer_log_path = os.path.join(run_dir, "tracer.log")
    tracer_path = resolve_tracer(args.tracer or scenario["tracer"].get("script", "insim_trace.py"))
    if not os.path.isfile(tracer_path):
        print(f"tracer script not found: {tracer_path}", file=sys.stderr)
        return 2

    run_id = os.path.basename(run_dir)
    tracer_argv = [sys.executable, tracer_path] + scenario_mod.tracer_argv(
        scenario, trace_path, args.control_port, run_id) + [
        "--host", args.host,
        "--insim-port", str(args.insim_port),
        "--udp-port", str(args.udp_port),
    ]

    print(f"Scenario : {scenario['name']}")
    print(f"Tracer   : {os.path.relpath(tracer_path, paths.REPO_ROOT)}")
    print(f"Run dir  : {run_dir}")

    result: Dict[str, Any] = {
        "scenario": scenario["name"],
        "scenario_path": scenario_path,
        "run_dir": run_dir,
        "run_id": run_id,
        "tracer": tracer_path,
        "tracer_argv": tracer_argv[1:],
        "started_utc": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "speed": args.speed,
        "input_events": len(events),
        "input_duration_s": round(input_model.duration(events), 3),
        "required_events": list(args.require),
        # A run that finished is not a run that passed. Nothing in this harness
        # can decide whether the *add-on* behaved: that is the timeline's job and
        # a human's or an agent's reading of the trace.
        "functional_verdict": "not_evaluated",
    }
    exit_code = 0
    control = ControlClient(args.control_port)
    tracer_log = open(tracer_log_path, "w", encoding="utf-8")
    process = subprocess.Popen(tracer_argv, stdout=tracer_log, stderr=subprocess.STDOUT,
                               cwd=paths.REPO_ROOT)
    try:
        if not wait_for_tracer(control, TRACER_STARTUP_TIMEOUT_S):
            print(f"tracer did not come up -- see {tracer_log_path}", file=sys.stderr)
            result["error"] = "tracer did not start"
            return _finish(process, control, tracer_log, run_dir, result, 4)

        wait_menu = scenario["run"]["require_main_menu"] and not args.no_wait_menu
        if wait_menu:
            timeout = float(scenario["run"]["menu_timeout_s"])
            state = wait_for_main_menu(control, timeout)
            result["state_before"] = state
            if not state.get("at_main_menu"):
                print("LFS is not at the main menu "
                      f"(state: {state.get('flags')})", file=sys.stderr)
                if not args.force:
                    result["error"] = "not at the main menu"
                    return _finish(process, control, tracer_log, run_dir, result, 5,
                                   trace_path=trace_path, scenario_path=scenario_path)
        else:
            result["state_before"] = control.state() or {}

        play = player_mod.Player(
            meta, events,
            speed=args.speed,
            require_focus=not args.no_focus_check,
            on_focus_loss=args.on_focus_loss,
            abort_key=args.abort_key,
            control=control,
            strict_timing=args.strict_timing,
        )
        problems = play.preflight()
        result["preflight"] = problems
        result["preflight_warnings"] = list(play.warnings)
        for warning in play.warnings:
            print(f"  note: {warning}", file=sys.stderr)
        for problem in problems:
            print(f"  pre-flight: {problem}", file=sys.stderr)
        if problems and not args.force:
            print("refusing to replay; fix the above or pass --force", file=sys.stderr)
            result["error"] = "pre-flight failed"
            return _finish(process, control, tracer_log, run_dir, result, 3,
                           trace_path=trace_path, scenario_path=scenario_path)

        for remaining in range(args.countdown, 0, -1):
            print(f"  starting in {remaining}...", end="\r", flush=True)
            time.sleep(1.0)
        print("  replaying -- press the abort key to stop.        ")

        control.marker("scenario_start", scenario=scenario["name"], speed=args.speed)
        try:
            replay_result = play.play()
        except pynput_access.ReplayUnavailable as exc:
            print(exc, file=sys.stderr)
            result["error"] = str(exc)
            return _finish(process, control, tracer_log, run_dir, result, 3,
                           trace_path=trace_path, scenario_path=scenario_path)
        control.marker("scenario_end", aborted=replay_result["aborted"])
        result["replay"] = replay_result
        if replay_result["lateness_over_budget"]:
            # The input landed later in the game than the timeline says. Say so
            # before anyone reads the trace as a behaviour change.
            print(f"warning: {replay_result['lateness_over_budget']} event(s) dispatched "
                  f"more than {replay_result['lateness_budget_s']:g} s late "
                  f"(worst {replay_result['lateness_max_s']:.3f} s)", file=sys.stderr)
        if replay_result["aborted"]:
            print(f"replay aborted: {replay_result['abort_reason']}", file=sys.stderr)
            exit_code = 6

        tail = args.tail if args.tail is not None else float(scenario["run"]["tail_s"])
        if tail > 0:
            time.sleep(tail)
        result["tail_s"] = tail
        result["state_after"] = control.state() or {}
        result["tracer_stats"] = control.request("stats") or {}
        counts = (result["tracer_stats"].get("counts") or {})
        missing = [name for name in args.require if not counts.get(name)]
        if missing:
            # Missing telemetry is not a zero reading. Say so with an exit code,
            # or an agent reads "the brake never moved" out of an empty capture.
            print(f"required telemetry missing: {', '.join(missing)}", file=sys.stderr)
            result["missing_required"] = missing
            exit_code = exit_code or 7
        return _finish(process, control, tracer_log, run_dir, result, exit_code,
                       trace_path=trace_path, scenario_path=scenario_path,
                       print_summary=not args.no_summary)
    except KeyboardInterrupt:
        print("\ninterrupted", file=sys.stderr)
        result["error"] = "interrupted"
        return _finish(process, control, tracer_log, run_dir, result, 130,
                       trace_path=trace_path, scenario_path=scenario_path)


def _finish(process: subprocess.Popen, control: ControlClient, tracer_log: Any,
            run_dir: str, result: Dict[str, Any], exit_code: int,
            trace_path: str = "", scenario_path: str = "",
            print_summary: bool = False) -> int:
    """Stop the tracer, write run.json, optionally print the summary."""
    control.stop_tracer()
    try:
        process.wait(timeout=TRACER_SHUTDOWN_TIMEOUT_S)
    except subprocess.TimeoutExpired:
        process.terminate()
        try:
            process.wait(timeout=5)
        except subprocess.TimeoutExpired:
            process.kill()
        result["tracer_killed"] = True
    control.close()
    try:
        tracer_log.close()
    except OSError:
        pass
    result["tracer_returncode"] = process.returncode
    result["finished_utc"] = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())

    if scenario_path:
        for name in (paths.TIMELINE_FILE, paths.TIMELINE_DRAFT_FILE):
            source = os.path.join(scenario_path, name)
            if os.path.isfile(source):
                shutil.copy2(source, os.path.join(run_dir, name))

    if trace_path and os.path.isfile(trace_path):
        try:
            summary = analyze_trace.summarise(trace_path)
            result["summary"] = summary
            if print_summary:
                print()
                print(analyze_trace.format_summary(summary))
        except Exception as exc:
            result["summary_error"] = f"{type(exc).__name__}: {exc}"

    with open(os.path.join(run_dir, "run.json"), "w", encoding="utf-8", newline="\n") as fh:
        json.dump(result, fh, indent=2, ensure_ascii=False)
        fh.write("\n")
    print(f"\nrun.json : {os.path.join(run_dir, 'run.json')}")
    if trace_path:
        print(f"trace    : {trace_path}")
    return exit_code


if __name__ == "__main__":
    raise SystemExit(main())
