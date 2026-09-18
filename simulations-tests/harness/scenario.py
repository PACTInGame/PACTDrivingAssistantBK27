"""Validate artifacts before installing hooks or injecting any input."""
import json
import math


def load(path):
    data = json.loads(path.read_text(encoding="utf-8"))
    if data.get("schema") != 1 or data.get("sample_ms") != 50:
        raise ValueError("Unsupported replay schema / sample interval")
    duration = data.get("duration")
    if not isinstance(duration, (float, int)) or not math.isfinite(duration) or not 0 < duration <= 3600:
        raise ValueError("Replay duration must be in (0, 3600] seconds")
    previous = -1
    held = set()
    for event in data["events"]:
        t, kind = event["t"], event["kind"]
        if not isinstance(t, (int, float)) or not math.isfinite(t) or not previous <= t <= duration or t < 0:
            raise ValueError("Invalid or unordered event timestamp")
        previous = t
        if kind in ("key", "button"):
            code = event["code"]
            if kind == "key" and (type(code) is not int or not 1 <= code <= 255 or code in (121, 122, 123)):
                raise ValueError("Invalid / reserved key")
            if kind == "button" and code not in ("left", "right", "middle", "x1", "x2"):
                raise ValueError("Invalid mouse button")
            token = (kind, code)
            if type(event["down"]) is not bool or event["down"] == (token in held):
                raise ValueError("Unbalanced input transitions")
            if event["down"]:
                held.add(token)
            else:
                held.remove(token)
        elif kind in ("move", "scroll"):
            if any(type(event[k]) is not int for k in ("x", "y")):
                raise ValueError("Invalid mouse coordinates")
        elif kind == "marker":
            if not isinstance(event["label"], str):
                raise ValueError("Invalid marker")
        else:
            raise ValueError("Unknown input event")
    if held:
        raise ValueError("Replay leaves keys/buttons held")
    return data


def play(data, backend, trace, pump, clock, guard, max_lag=0.25):
    start = clock()
    trace.write("replay_start", {})
    try:
        for event in data["events"]:
            deadline = start + event["t"]
            while clock() < deadline:
                guard()
                pump(min(0.01, deadline - clock()))
            guard()
            lag = clock() - deadline
            if lag > max_lag:
                raise RuntimeError(f"Replay timing overrun: {lag:.3f}s")
            if event["kind"] != "marker":
                backend.apply(event)
            trace.write("input" if event["kind"] != "marker" else "marker", event,
                        planned_t=event["t"], lateness_s=lag)
        while clock() < start + data["duration"]:
            guard()
            pump(min(0.01, start + data["duration"] - clock()))
    finally:
        backend.release()
    trace.write("replay_end", {})
