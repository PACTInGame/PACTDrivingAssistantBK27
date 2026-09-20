"""Writing a failed parking scene to disk, so it can be replayed offline.

A manoeuvre that will not plan is the hardest thing about this feature to
diagnose: the inputs are a pose, a slot and a dozen boxes, the failure is one
word (``blocked``), and the only place the combination exists is a moving car
in a running game. Asking the driver to reproduce it is a minute per attempt;
replaying it as a unit test is a second.

So when planning fails and ``PACT_LOG_LEVEL=DEBUG`` is set, the scene is
written out once. Three rules keep this from becoming a liability:

* **Off by default.** Nothing is written at the normal log level.
* **Off the assistance thread.** File I/O in ``process()`` is exactly what
  ``AGENTS.md`` rule 1 forbids; the dump is handed to a daemon thread and the
  cycle returns.
* **Once per run.** One file, not a stream -- the first failure is the
  interesting one and an unbounded dump would fill the disk from a loop that
  runs four times a second.

``tests/test_parking_scene.py`` reads the file back, which is the only reason
the format is stable enough to describe: a dict with ``ego``, ``slot`` and
``obstacles``, all in metres and radians in the maths frame
(``conventions.md`` §2).
"""

import json
import logging
import threading
from dataclasses import asdict, is_dataclass
from typing import Any, Dict, Optional, Sequence

logger = logging.getLogger(__name__)

DEFAULT_PATH = 'park_scene_debug.json'

_written = False
_lock = threading.Lock()


def reset():
    """Allow one more dump. For the tests, and for a second live attempt."""
    global _written
    with _lock:
        _written = False


def dump_scene(ego, slot, obstacles: Sequence, reason: Optional[str],
               path: str = DEFAULT_PATH) -> bool:
    """Write the scene, once per run, on a thread. Returns whether it will.

    The caller does not wait and does not learn whether the write succeeded --
    a diagnostic that can fail a manoeuvre is worse than no diagnostic.
    """
    global _written
    if not logger.isEnabledFor(logging.DEBUG):
        return False
    with _lock:
        if _written:
            return False
        _written = True
    scene = {
        'reason': reason,
        'ego': _pose(ego),
        'slot': _slot(slot),
        'obstacles': [_box(box) for box in obstacles],
    }
    threading.Thread(target=_write, args=(scene, path),
                     name='park-scene-dump', daemon=True).start()
    return True


def _write(scene: Dict[str, Any], path: str):
    try:
        with open(path, 'w', encoding='utf-8') as handle:
            json.dump(scene, handle, indent=1, sort_keys=True)
        logger.debug("Wrote the failed parking scene to %s.", path)
    except OSError as exc:
        logger.debug("Could not write the parking scene: %s", exc)


def _pose(pose) -> Dict[str, float]:
    return {'x': pose.x, 'y': pose.y, 'yaw': pose.yaw}


def _box(box) -> Dict[str, float]:
    return {'x': box.x, 'y': box.y, 'yaw': box.yaw,
            'length': box.length, 'width': box.width}


def _slot(slot) -> Dict[str, Any]:
    data = asdict(slot) if is_dataclass(slot) else dict(slot)
    data['entry'] = _pose(slot.entry)
    data['target'] = _pose(slot.target)
    data['bounded_by'] = [repr(item) for item in slot.bounded_by]
    return data
