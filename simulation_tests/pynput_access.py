"""Lazy access to pynput, the one Windows-only dependency of this harness.

Same idea as ``misc/platform_shim.py`` in the add-on: every module must *import*
on any platform, or none of it can be tested, so the Windows-only package is
reached through an accessor that fails with a useful message instead of an
ImportError halfway through a run.
"""

from __future__ import annotations

from typing import Any, Tuple


class ReplayUnavailable(RuntimeError):
    """Input capture or replay cannot run here (no pynput, wrong platform)."""


def import_pynput() -> Tuple[Any, Any]:
    """``(keyboard, mouse)``, or a :class:`ReplayUnavailable` saying what to do."""
    try:
        from pynput import keyboard, mouse  # noqa: WPS433 -- deliberately lazy
    except Exception as exc:  # ImportError, or a display error on Linux
        raise ReplayUnavailable(
            f"pynput is not usable here ({type(exc).__name__}: {exc}). "
            "Recording and replaying input needs Windows and "
            "`pip install -r simulation_tests/requirements.txt`.") from exc
    return keyboard, mouse
