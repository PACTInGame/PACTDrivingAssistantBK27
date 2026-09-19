"""Reading and writing ``scenario.json``.

A scenario is a directory holding four things:

``scenario.json``   what it is, what it needs, and how to trace it
``input.jsonl``     the recorded mouse/keyboard stream
``timeline.md``     what is supposed to happen when (hand-checked)
``timeline.draft.md`` the recorder's generated first version of that

The tracer settings live here rather than on the command line so a scenario
always runs with the measurements it was designed around.
"""

from __future__ import annotations

import json
import os
import time
from typing import Any, Dict, List

from . import config, paths

DEFAULT_TRACER: Dict[str, Any] = {
    "script": "insim_trace.py",
    "packets": list(config.DEFAULT_PACKETS),
    "mci_interval_ms": config.MCI_INTERVAL_MS,
    "outgauge_interval_ms": config.OUTGAUGE_INTERVAL_MS,
    "outsim_interval_ms": 0,
}

DEFAULT_RUN: Dict[str, Any] = {
    # Seconds of trace to keep after the replay ends, so a late reaction is
    # still captured.
    "tail_s": 2.0,
    # Refuse to start unless LFS reports the entry screen (every scenario starts
    # and ends at the main menu).
    "require_main_menu": True,
    # Seconds to wait for that; 0 = do not wait.
    "menu_timeout_s": 20.0,
}


def default_scenario(name: str, description: str = "") -> Dict[str, Any]:
    return {
        "name": name,
        "description": description,
        "created_utc": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "preconditions": [
            "LFS is running with InSim enabled on port 29999",
            "LFS is at the main menu",
        ],
        # Marker names in the order the recorder should hand them out, so the
        # person recording only has to press the marker key at the right moment.
        "markers": [],
        # A scenario whose recording no longer replays reliably. It stays in
        # the directory -- deleting it would lose the reference and the run
        # history that points at it -- but the runner refuses to start it, so
        # a batch does not waste time on it and, worse, does not leave LFS in
        # a state that breaks the *next* scenario.
        "disabled": False,
        "disabled_reason": "",
        "tracer": dict(DEFAULT_TRACER),
        "run": dict(DEFAULT_RUN),
    }


def load(scenario_path: str) -> Dict[str, Any]:
    """Read a scenario.json, filling in every default the file omits."""
    path = os.path.join(scenario_path, paths.SCENARIO_FILE)
    with open(path, "r", encoding="utf-8") as fh:
        data = json.load(fh)
    if not isinstance(data, dict):
        raise ValueError(f"{path}: expected an object")
    data.setdefault("name", os.path.basename(scenario_path.rstrip(os.sep)))
    data.setdefault("description", "")
    data.setdefault("preconditions", [])
    data.setdefault("markers", [])
    data["disabled"] = bool(data.get("disabled", False))
    data.setdefault("disabled_reason", "")
    tracer = dict(DEFAULT_TRACER)
    tracer.update(data.get("tracer") or {})
    data["tracer"] = tracer
    run = dict(DEFAULT_RUN)
    run.update(data.get("run") or {})
    data["run"] = run
    return data


def disabled_reason(data: Dict[str, Any]) -> str:
    """Why this scenario must not run, or ``""`` when it may.

    Returns a reason string rather than a bool so every caller is pushed into
    saying *why* it refused -- "scenario is disabled" on its own sends the
    reader to the JSON file.
    """
    if not data.get("disabled"):
        return ""
    return data.get("disabled_reason") or "no reason recorded"


def save(scenario_path: str, data: Dict[str, Any]) -> str:
    os.makedirs(scenario_path, exist_ok=True)
    path = os.path.join(scenario_path, paths.SCENARIO_FILE)
    with open(path, "w", encoding="utf-8", newline="\n") as fh:
        json.dump(data, fh, indent=2, ensure_ascii=False)
        fh.write("\n")
    return path


def tracer_argv(scenario: Dict[str, Any], out_path: str, control_port: int,
                run_id: str = "") -> List[str]:
    """Command-line arguments for the tracer, derived from the scenario."""
    tracer = scenario["tracer"]
    argv = [
        "--out", out_path,
        "--scenario", scenario.get("name", ""),
        "--run-id", run_id,
        "--control-port", str(control_port),
        "--mci-interval", str(int(tracer["mci_interval_ms"])),
        "--outgauge-interval", str(int(tracer["outgauge_interval_ms"])),
        "--outsim-interval", str(int(tracer["outsim_interval_ms"])),
    ]
    from simulation_tests.chat_review import PACKETS
    packets = list(dict.fromkeys([*(tracer.get("packets") or config.DEFAULT_PACKETS), *PACKETS]))
    if packets:
        argv += ["--packets", ",".join(packets)]
    return argv


def has_recording(scenario_path: str) -> bool:
    return os.path.isfile(os.path.join(scenario_path, paths.INPUT_FILE))
