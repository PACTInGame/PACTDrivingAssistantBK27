"""Directory layout of the harness and the sys.path bootstrap.

Every entry point calls :func:`ensure_repo_on_path` before importing ``pyinsim``,
so the scripts work both as ``python simulation_tests/insim_trace.py`` and as
``python -m simulation_tests.insim_trace``.
"""

from __future__ import annotations

import os
import re
import sys
from datetime import datetime

PACKAGE_DIR = os.path.dirname(os.path.abspath(__file__))
REPO_ROOT = os.path.dirname(PACKAGE_DIR)

SCENARIOS_DIR = os.path.join(PACKAGE_DIR, "scenarios")
RUNS_DIR = os.path.join(PACKAGE_DIR, "runs")
TEMP_DIR = os.path.join(PACKAGE_DIR, "_temp")

#: Files that make up a scenario.
INPUT_FILE = "input.jsonl"
SCENARIO_FILE = "scenario.json"
TIMELINE_FILE = "timeline.md"
TIMELINE_DRAFT_FILE = "timeline.draft.md"

_SAFE_NAME = re.compile(r"^[A-Za-z0-9._-]+$")


def ensure_repo_on_path() -> None:
    """Make ``import pyinsim`` work regardless of how the script was started."""
    if REPO_ROOT not in sys.path:
        sys.path.insert(0, REPO_ROOT)


def scenario_dir(name: str) -> str:
    """Absolute path of a scenario, accepting either a name or a path.

    Raises:
        ValueError: the name is not a plain directory name.
        FileNotFoundError: no such scenario.
    """
    if os.path.sep in name or (os.path.altsep and os.path.altsep in name):
        path = os.path.abspath(name)
    else:
        if not _SAFE_NAME.match(name):
            raise ValueError(f"invalid scenario name: {name!r}")
        path = os.path.join(SCENARIOS_DIR, name)
    if not os.path.isdir(path):
        raise FileNotFoundError(f"scenario not found: {path}")
    return path


def list_scenarios() -> list:
    """Names of every directory under ``scenarios/`` that carries a scenario.json."""
    if not os.path.isdir(SCENARIOS_DIR):
        return []
    out = []
    for entry in sorted(os.listdir(SCENARIOS_DIR)):
        path = os.path.join(SCENARIOS_DIR, entry)
        if os.path.isfile(os.path.join(path, SCENARIO_FILE)):
            out.append(entry)
    return out


def new_run_dir(scenario_name: str, now: datetime = None) -> str:
    """Create and return ``runs/<scenario>_<YYYYmmdd-HHMMSS>/``."""
    stamp = (now or datetime.now()).strftime("%Y%m%d-%H%M%S")
    safe = re.sub(r"[^A-Za-z0-9._-]", "_", scenario_name)
    path = os.path.join(RUNS_DIR, f"{safe}_{stamp}")
    os.makedirs(path, exist_ok=True)
    return path
