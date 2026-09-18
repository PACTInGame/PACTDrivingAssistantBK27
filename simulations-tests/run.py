"""Entry point: record, replay, standalone monitor, and offline inspection."""
import argparse
import hashlib
import importlib.util
import json
import re
import shutil
import sys
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT.parent))

from harness.scenario import load, play
from harness.trace import Trace


def save(path, data):
    path.write_text(json.dumps(data, indent=2, ensure_ascii=True) + "\n", encoding="utf-8")


def digest(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def monitor_class(path):
    spec = importlib.util.spec_from_file_location("scenario_monitor", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module.Monitor


def collect_inputs(desktop, trace, pump, guard, max_seconds):
    start = trace.now()
    trace.write("record_start", {}, origin_t=start)
    events = []
    next_sample = start
    last_position = None
    while not desktop.stop:
        guard()
        now = trace.now()
        if now - start >= max_seconds:
            raise RuntimeError("Recording exceeded maximum duration")
        if now >= next_sample:
            position = tuple(desktop.mouse.position)
            if position != last_position:
                desktop.enqueue("move", x=int(position[0]), y=int(position[1]))
                last_position = position
            for event in desktop.drain():
                event["t"] = max(0, event["t"] - start)
                events.append(event)
                trace.write("recorded_input", event)
            next_sample += 0.05
            if now - next_sample > 0.25:
                raise RuntimeError("Recording scheduler fell behind by more than 250 ms")
        pump(0.005)
    for event in desktop.drain():
        event["t"] = max(0, event["t"] - start)
        events.append(event)
        trace.write("recorded_input", event)
    if desktop.physical:
        raise RuntimeError("Release all controls before ending recording with F11")
    events.sort(key=lambda event: event["t"])
    trace.write("record_end", {})
    return {"schema": 1, "sample_ms": 50, "duration": trace.now() - start, "events": events}


def timeline(folder, data):
    lines = ["# Scenario timeline", "", "Status: DRAFT — fill in expected outcomes before using as a regression test.", "",
             "Start and end: LFS main menu (visually verify; IS_STA also matches the server list).", "",
             "## Preconditions", "", "- LFS version, language, resolution/DPI: TODO",
             "- Vehicle/setup, track/layout, AI cars, controls, camera: TODO",
             "- Add-on settings / version and expected signals: TODO", "",
             "## Expected behaviour", "", "| Replay time (s) | Action / phase | Expected result / tolerance |",
             "|---:|---|---|", "| 0.000 | Main menu | TODO |"]
    for event in data["events"]:
        if event["kind"] == "marker":
            lines.append(f'| {event["t"]:.3f} | Marker | TODO |')
    lines += [f'| {data["duration"]:.3f} | Main menu | TODO |', "",
              "## Input reference", "", "Exact events (including sampled mouse positions) are in replay.json.", "",
              "| Time (s) | Input |", "|---:|---|"]
    for event in data["events"]:
        if event["kind"] != "move":
            description = json.dumps({k: v for k, v in event.items() if k != "t"})
            lines.append(f'| {event["t"]:.3f} | `{description.replace(chr(124), chr(47))}` |')
    (folder / "timeline.md").write_text("\n".join(lines) + "\n", encoding="utf-8")


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", choices=("record", "replay", "monitor", "inspect"))
    parser.add_argument("scenario", nargs="?", help="Scenario name, e.g. 04-drive-stop")
    parser.add_argument("--monitor", type=Path, help="Custom observer Python file (replay: under _temp only)")
    parser.add_argument("--port", type=int, default=29999)
    parser.add_argument("--udp-port", type=int, default=30001)
    parser.add_argument("--countdown", type=float, default=5)
    parser.add_argument("--seconds", type=float, default=600, help="Monitor duration / recording limit (max 3600)")
    parser.add_argument("--require", action="append", default=[], metavar="PACKET",
                        help="Fail if a packet type is absent, e.g. --require OutGaugePack")
    parser.add_argument("--cfg-streams", action="store_true",
                        help="Receive forwarded cfg.txt UDP streams; do not send SSG/SSP")
    args = parser.parse_args(argv)
    if not 0 < args.seconds <= 3600 or not 0 <= args.countdown <= 60:
        parser.error("Invalid duration or countdown")
    if not 1 <= args.port <= 65535 or not 1024 <= args.udp_port <= 65535 or args.udp_port in (29998, 30000):
        parser.error("Use valid ports; UDP 29998 and 30000 belong to the add-on")
    if args.command != "monitor" and (not args.scenario or not re.fullmatch(r"[a-zA-Z0-9][a-zA-Z0-9_-]{0,79}", args.scenario)):
        parser.error("Supply a scenario name (letters, digits, hyphens, underscores)")
    folder = ROOT / "scenarios" / (args.scenario or "monitor")
    if args.command == "inspect":
        data = load(folder / "replay.json")
        print(json.dumps({"duration": data["duration"], "events": len(data["events"]),
                          "environment": data.get("environment"), "sha256": digest(folder / "replay.json")}, indent=2))
        return 0
    if sys.version_info >= (3, 12):
        parser.error("Live tests need Python 3.11 or earlier (pyinsim uses asyncore)")
    import asyncore
    import pyinsim
    from harness.monitor import Collector

    data = load(folder / "replay.json") if args.command == "replay" else None
    profile = args.monitor.resolve() if args.monitor else (
        folder / "monitor.py" if data else ROOT / "monitor_template.py")
    if args.command == "replay" and args.monitor and not profile.is_relative_to(ROOT / "_temp"):
        parser.error("Custom replay observers must be placed under simulations-tests/_temp")
    factory = monitor_class(profile) if profile.is_file() else Collector
    if not profile.is_file():
        parser.error(f"Monitor not found: {profile}")
    if args.command == "record":
        folder.mkdir(parents=True, exist_ok=False)  # Never overwrite a recorded scenario.
        shutil.copyfile(ROOT / "monitor_template.py", folder / "monitor.py")
    run_dir = ROOT / "_temp" / "runs" / (datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S") + "-" + uuid.uuid4().hex[:8])
    run_dir.mkdir(parents=True)
    shutil.copyfile(profile, run_dir / "monitor.py")
    trace = Trace(run_dir / "trace.jsonl")
    result = {"schema": 1, "command": args.command, "scenario": args.scenario,
              "status": "incomplete", "functional_verdict": "not_evaluated",
              "monitor_sha256": digest(profile), "monitor": str(profile),
              "cfg_streams": args.cfg_streams, "required_packets": args.require}
    if data:
        result["replay_sha256"] = digest(folder / "replay.json")
    save(run_dir / "summary.json", result)
    desktop = observer = None
    def pump(seconds):
        asyncore.loop(timeout=max(0, seconds), count=1)
        if observer:
            observer.check()
    try:
        observer = factory(trace, port=args.port, udp=args.udp_port,
                           request_streams=not args.cfg_streams)
        if args.command == "monitor":
            deadline = time.perf_counter() + args.seconds
            while time.perf_counter() < deadline:
                pump(0.01)
            if not observer.packets:
                raise RuntimeError("No packets received")
        else:
            from harness.desktop import Desktop
            desktop = Desktop(trace.now)
            desktop.start()
            print(f"Focus LFS main menu; starting in {args.countdown:g}s. F10 marker, F11 finish, F12 abort.", flush=True)
            deadline = time.perf_counter() + args.countdown
            while time.perf_counter() < deadline:
                if desktop.abort:
                    raise RuntimeError("Aborted during countdown")
                pump(0.01)
            environment = desktop.environment()
            observer.menu(pump)
            desktop.neutral()
            if data and data.get("environment") != environment:
                raise RuntimeError("Window position / size or screen resolution differs from recording")
            trace.write("environment", environment)
            def guard():
                observer.check()
                desktop.guard(environment, replay=args.command == "replay")
            if args.command == "record":
                data = collect_inputs(desktop, trace, pump, guard, args.seconds)
                data["environment"] = environment
                # Preserve a candidate even if the final menu check fails.
                save(run_dir / "candidate-replay.json", data)
                load(run_dir / "candidate-replay.json")
            else:
                play(data, desktop, trace, pump, time.perf_counter, guard)
            observer.menu(pump)
            guard()
            if args.command == "record":
                save(folder / "replay.json", data)
                timeline(folder, data)
        missing = [name for name in args.require if not observer.packets.get(name)]
        if missing:
            raise RuntimeError("Required telemetry missing: " + ", ".join(missing))
        result["status"] = "completed"
    except (Exception, KeyboardInterrupt) as exc:
        result["status"] = "failed"
        result["error"] = str(exc) or type(exc).__name__
        trace.write("failure", result["error"])
    finally:
        for resource in (desktop, observer):
            if resource:
                try:
                    resource.close()
                except Exception as exc:
                    result["status"] = "failed"
                    result.setdefault("cleanup_errors", []).append(str(exc))
        pyinsim.closeall()
        result["packets"] = observer.packets if observer else {}
        result["trace_counts"] = dict(trace.counts)
        result["duration"] = trace.now()
        trace.close()
        save(run_dir / "summary.json", result)
    print(json.dumps(result, indent=2))
    print(f"Artifacts: {run_dir}")
    return 0 if result["status"] == "completed" else 1


if __name__ == "__main__":
    raise SystemExit(main())
