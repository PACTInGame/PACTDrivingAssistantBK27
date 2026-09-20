"""Turns a recorded input stream into a draft ``timeline.md``.

The timestamps in a scenario's timeline are what makes a trace checkable: "at
t=18.4 s the brake key goes down" is the only thing that tells the analyser where
in the trace to look for a brake pressure. Deriving that table by hand from the
gaps between events is exactly the manual work this harness exists to remove, so
the recorder writes a draft and the human only edits the *meaning* column.

Mouse movement is left out on purpose: it is thousands of samples that say
nothing about what the scenario did. Clicks carry their own coordinates.
"""

from __future__ import annotations

from typing import Any, Dict, List, Optional, Tuple

from . import input_model

#: Gap between two actions that is worth calling out as a pause, in seconds.
IDLE_THRESHOLD_S = 0.5


def _key_label(event: Dict[str, Any]) -> str:
    name = event.get("name") or input_model.vk_label(event.get("vk"))
    return name


def build_actions(events: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """Human-meaningful actions, in time order, mouse movement removed.

    Key presses are collapsed into one row carrying the hold duration; a key that
    is never released (recording stopped while held) gets ``hold`` of None.
    """
    open_keys: Dict[Any, Dict[str, Any]] = {}
    actions: List[Dict[str, Any]] = []
    for event in events:
        kind = event.get("kind")
        if kind == input_model.KIND_MOVE:
            continue
        if kind == input_model.KIND_KEY:
            identity = event.get("vk") if event.get("vk") is not None else event.get("name")
            if event.get("action") == "down":
                if identity in open_keys:
                    continue  # auto-repeat while held
                action = {"t": event["t"], "type": "key", "label": _key_label(event),
                          "hold": None}
                open_keys[identity] = action
                actions.append(action)
            else:
                action = open_keys.pop(identity, None)
                if action is not None:
                    action["hold"] = round(event["t"] - action["t"], 3)
        elif kind == input_model.KIND_CLICK:
            if event.get("action") != "down":
                continue
            actions.append({"t": event["t"], "type": "click",
                            "label": f"{event.get('button', 'left')} click",
                            "pos": (event.get("x"), event.get("y"))})
        elif kind == input_model.KIND_SCROLL:
            actions.append({"t": event["t"], "type": "scroll",
                            "label": f"wheel {event.get('dy', 0):+d}",
                            "pos": (event.get("x"), event.get("y"))})
        elif kind == input_model.KIND_MARKER:
            actions.append({"t": event["t"], "type": "marker",
                            "label": event.get("name", "marker")})
    actions.sort(key=lambda a: a["t"])
    return actions


def describe(action: Dict[str, Any]) -> str:
    """One cell of text for the action column."""
    if action["type"] == "key":
        hold = action.get("hold")
        if hold is None:
            return f"key `{action['label']}` down (never released)"
        return f"key `{action['label']}` ({hold:.2f} s)"
    if action["type"] == "click":
        x, y = action.get("pos", (None, None))
        return f"{action['label']} @ ({x}, {y})"
    if action["type"] == "scroll":
        x, y = action.get("pos", (None, None))
        return f"{action['label']} @ ({x}, {y})"
    if action["type"] == "marker":
        return f"**MARKER `{action['label']}`**"
    return action.get("label", "")


def render(meta: Dict[str, Any], events: List[Dict[str, Any]],
           scenario: str = "", idle_threshold: float = IDLE_THRESHOLD_S) -> str:
    """The full markdown draft."""
    actions = build_actions(events)
    moves = sum(1 for e in events if e.get("kind") == input_model.KIND_MOVE)
    total = input_model.duration(events)
    name = scenario or meta.get("scenario") or "unnamed scenario"

    lines: List[str] = []
    lines.append(f"# {name} — timeline (draft)")
    lines.append("")
    lines.append("> Generated from `input.jsonl` by the recorder. **Edit the "
                 "\"expected\" column**, then rename this file to `timeline.md`. "
                 "Times are seconds from replay start and line up with the trace's "
                 "`t` once `scenario_start` is subtracted.")
    lines.append("")
    lines.append("## Recording")
    lines.append("")
    lines.append(f"- recorded: {meta.get('recorded_utc', '?')}")
    lines.append(f"- duration: {total:.2f} s")
    lines.append(f"- screen: {meta.get('screen_size')}")
    lines.append(f"- events: {len(events)} ({moves} mouse samples, "
                 f"{len(actions)} actions)")
    lines.append(f"- sample interval: {meta.get('sample_interval_ms')} ms")
    lines.append("")
    lines.append("## Timeline")
    lines.append("")
    lines.append("| t [s] | Δt [s] | input | expected in the trace |")
    lines.append("|------:|-------:|-------|-----------------------|")

    previous: Optional[float] = None
    for action in actions:
        gap = 0.0 if previous is None else action["t"] - previous
        if previous is not None and gap >= idle_threshold:
            lines.append(f"| | {gap:.2f} | *— idle {gap:.2f} s —* | |")
        lines.append(f"| {action['t']:.2f} | {gap:.2f} | {describe(action)} | |")
        previous = action["t"]
    if previous is not None and total - previous >= idle_threshold:
        lines.append(f"| | {total - previous:.2f} | *— idle "
                     f"{total - previous:.2f} s —* | |")
    lines.append(f"| {total:.2f} | | *end of recording* | back at the main menu |")
    lines.append("")
    lines.append("## Notes")
    lines.append("")
    lines.append("- ")
    lines.append("")
    return "\n".join(lines)
