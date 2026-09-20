"""What LFS has to be told if this process dies in the middle of an intervention.

An intervention on a wheel/joystick driver works by pointing an LFS *function*
at a different axis for a moment: ``brake`` at our vJoy axis, ``throttle`` at
nothing at all. Both are one-way -- LFS keeps whatever it was last told, and a
process that is killed between the two commands leaves the driver with a car
that brakes on its own and does not accelerate.

Nothing inside this process can fix that; a dead process sends no commands. So
the state is written to a file that outlives us, and ``guardian.py`` -- a
separate process waiting on our PID -- replays it (``reference/control-
intervention.md`` §3.2).

The file holds **the commands themselves**, not a description of what we were
doing::

    {"commands": ["/axis 12 brake", "/axis 9 throttle", "/invert 1 throttle"]}

That is deliberate. Every number in them (which axis the driver's brake is on,
which one carries the throttle) is known here and nowhere else -- the guardian
has no settings, no vehicle and no control mode. Giving it a list to replay
keeps it a dumb, never-changing watchdog while this side stays free to add
whatever a future intervention needs to undo.

**Presence is the whole signal.** The file exists only while something is
actually held. Gone means the intervention ended cleanly and the guardian must
keep its hands off -- otherwise every normal shutdown would force ``brake``
onto whatever number our settings happened to hold, and break a working
configuration to fix a problem that was not there.

Cost: one small synchronous write per claim and one delete per release, so two
file operations per intervention, not per cycle. Synchronous on purpose -- a
marker a background thread has not written yet does not exist at the only
moment it matters.
"""

import json
import logging
import os
import threading
from typing import Dict, Optional

from misc.helpers import resolve_data_path as resolve_path

logger = logging.getLogger(__name__)

MARKER_FILE = 'brake_axis_held.marker'


class HandoverMarker:
    """The set of restore commands that are currently outstanding.

    Owners are named ('brake', 'throttle', ...) so that two independent parts of
    one intervention can claim and release without stepping on each other, and
    so a repeated claim from the same owner just updates its commands.

    An owner claims a *list*, because undoing one thing can take more than one
    command: giving an axis back to the throttle also has to restore its
    polarity, and a guardian that sent only the first half would hand the
    driver an inverted pedal.
    """

    def __init__(self, path: Optional[str] = None):
        self.path = path or resolve_path(MARKER_FILE)
        self._lock = threading.Lock()
        self._claims: Dict[str, list] = {}

    # ─── State ────────────────────────────────────────────────────────

    def holds_anything(self) -> bool:
        return bool(self._claims)

    def commands(self) -> list:
        return [command for claim in self._claims.values() for command in claim]

    # ─── Transitions ──────────────────────────────────────────────────

    def claim(self, owner: str, restore_commands):
        """Record that *owner* has taken something, and how to give it back.

        *restore_commands* is a single command or a list of them, applied in
        order.
        """
        if isinstance(restore_commands, str):
            restore_commands = [restore_commands]
        else:
            restore_commands = list(restore_commands)
        with self._lock:
            if self._claims.get(owner) == restore_commands:
                return True
            previous = self._claims.get(owner)
            self._claims[owner] = restore_commands
            if self._write():
                return True
            if previous is None:
                self._claims.pop(owner, None)
            else:
                self._claims[owner] = previous
            return False

    def release(self, owner: str):
        """*owner* has given its part back; nothing to restore for it any more."""
        with self._lock:
            if self._claims.pop(owner, None) is None:
                return
            if self._claims:
                self._write()
            else:
                self._remove()

    def release_all(self):
        with self._lock:
            if not self._claims:
                return
            self._claims.clear()
            self._remove()

    # ─── Disk ─────────────────────────────────────────────────────────

    def _write(self):
        payload = json.dumps({'commands': self.commands()})
        try:
            with open(self.path + '.tmp', 'w', encoding='utf-8') as handle:
                handle.write(payload)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(self.path + '.tmp', self.path)
            return True
        except OSError as exc:
            # Callers must refuse takeover if recovery cannot be recorded.
            logger.warning("Could not write the handover marker: %s: %s",
                           type(exc).__name__, exc)
            return False

    def _remove(self):
        try:
            os.remove(self.path)
        except FileNotFoundError:
            pass
        except OSError as exc:
            logger.warning("Could not remove the handover marker: %s: %s",
                           type(exc).__name__, exc)
