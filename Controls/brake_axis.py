"""Braking for a wheel/joystick driver: a virtual analog axis.

The path for LFS's ``wheel_js`` control mode. LFS ignores keys for throttle and
brake there (``reference/control-intervention.md`` §2.1), so an analog axis is
not a nicer option, it is the only one.

The mechanism is a swap:

    engage    write the demanded brake to the vJoy axis, then
              ``/axis <vjoy> brake``   -- LFS now reads us
    release   ``/axis <user> brake``   -- LFS reads the driver's pedal again,
              then park our axis at "no brake"

The order in each line is deliberate. On engage the value is written *first*,
locally and instantly, while the ``/axis`` command still has a TCP round trip to
make -- so by the time LFS switches, the right value is already waiting. On
release LFS is handed back *first*, so if anything goes wrong afterwards the
driver already has their pedal.

### The fail-safe hole, stated plainly

**vJoy holds the last value it was fed, forever** -- see the measurements in
:mod:`misc.vjoy_device`. Neither resetting nor relinquishing the device moves the
axis. So if this process dies *while engaged*, LFS keeps reading a braking value
from a device nobody is feeding, and the driver's own pedal is not assigned to
brake any more. The car brakes until they fix it in the LFS options.

Nothing inside this process can close that hole -- a dead process cannot send
``/axis``. Four things together do close it:

* the swap lasts only as long as the intervention, a second or so, not the
  session;
* the axis is parked at "no brake" the instant we hand back, so the value frozen
  into the device outside an intervention is harmless;
* every exit path -- release, disable, off-track, control-mode change, exception,
  shutdown -- hands back;
* and ``guardian.py``, a separate process started as soon as this path is armed,
  waits on our PID and sends the handback itself if we never get to. It acts
  only when the handover marker written below says we really were holding the
  brake.

### Why the raw values are calibrated and never assumed

LFS maps an axis through its own calibration, and it can be inverted: on the
development machine raw 0 is *full brake* and raw 32767 is *no brake*, with a
linear response and a small dead band at each end::

    vjoy 0.00 -> LFS brake 1.000        vjoy 0.75 -> LFS brake 0.222
    vjoy 0.25 -> LFS brake 0.778        vjoy 1.00 -> LFS brake 0.000
    vjoy 0.50 -> LFS brake 0.500

Another machine can have that the other way round. Getting the sign wrong means
writing *full brake* where we meant *idle*, so the two raw endpoints are stored
as measured values (``vjoy_raw_no_brake`` / ``vjoy_raw_full_brake``) and this
class refuses to run until ``vjoy_brake_calibrated`` says someone measured them.
"""

import logging
import os
import subprocess
import sys
import threading
import time
from typing import Optional

from Controls.handover_marker import HandoverMarker
from misc.helpers import resolve_path, resolve_data_path
from misc.vjoy_device import VJoyDevice

logger = logging.getLogger(__name__)

# Our name in the shared handover marker. An intervention can hold more than
# one thing (the throttle is unassigned at the same time), so each part claims
# and releases under its own name -- see ``Controls/handover_marker.py``.
MARKER_OWNER = 'brake'

# How long before a failed spawn is tried again, and how often a running
# guardian is checked for having died. Both are the same number because both
# are "look at this again, but not from the 100 ms thread".
GUARDIAN_RETRY_S = 30.0

# Transient, not a fault: the vJoy DLL is still being loaded on its own thread
# (``misc/vjoy_device.py``). Callers treat it like "ask again next cycle", not
# like "this path is broken" -- see ``EmergencyBrake._output_for``.
REASON_LOADING = 'vjoy_loading'


class AxisBrakeOutput:
    """Analog brake actuation through a vJoy axis, with handback to the driver."""

    def __init__(self, event_bus, settings, device: Optional[VJoyDevice] = None,
                 marker_path: Optional[str] = None, spawn=None, clock=None,
                 marker: Optional[HandoverMarker] = None):
        self.event_bus = event_bus
        self.settings = settings
        self.device = device or VJoyDevice()
        # Shared with the throttle cut when there is one, so both halves of an
        # intervention end up in the same file for the guardian to replay.
        self.marker = marker or HandoverMarker(marker_path)
        self._spawn = spawn or _spawn_guardian
        self._clock = clock or time.monotonic
        # True while LFS's brake is pointing at our axis instead of the driver's.
        self._holds_axis = False
        self._guardian = None            # the Popen, once one is running
        self._guardian_pending = False   # a spawn attempt is in flight
        self._guardian_retry_at = 0.0

    # ─── Configuration ────────────────────────────────────────────────

    @property
    def lfs_axis(self) -> int:
        """The number LFS gives our vJoy axis."""
        return self.settings.get('vjoy_axis_1')

    @property
    def driver_axis(self) -> int:
        """The number LFS gives the driver's own brake pedal."""
        return self.settings.get('user_axis_brake')

    def holds_axis(self) -> bool:
        """Is LFS's brake currently pointing at us?"""
        return self._holds_axis

    def unavailable_reason(self) -> Optional[str]:
        """Why this output cannot be armed, or ``None`` if it can."""
        if not self.settings.get('vjoy_brake_calibrated'):
            # Never guess the polarity. See the class docstring.
            return 'vjoy_not_calibrated'
        if self.lfs_axis == self.driver_axis:
            # Handing back to ourselves is not a handback. Almost certainly a
            # half-finished calibration; refusing beats swapping to nothing.
            return 'vjoy_and_driver_axis_identical'
        if not self.device.prepare():
            # Loading the DLL costs ~72 ms with a warm file cache, which is
            # most of an assistance cycle, so it happens on its own thread and
            # this pass simply says "not yet" (known-issues #56).
            return REASON_LOADING
        return self.device.unavailable_reason()

    # ─── Actuation ────────────────────────────────────────────────────

    def start_guardian(self):
        """Make sure the watchdog process is up. Called every pass; cheap.

        Spawned as soon as this path is armed rather than when an intervention
        starts: at that moment it is already too late to pay ~30 ms of process
        creation, and the whole point is that it outlives us.

        Three states have to be told apart, and an earlier version told apart
        only two. A spawn that *failed* returns ``None``, which looked exactly
        like "never started" -- so a missing or unstartable ``guardian.py``
        produced a fresh thread, a fresh ``Popen`` and a fresh error line ten
        times a second for as long as the driver was on track. Failure is now
        remembered as failure and retried on a timer, the way the input hooks
        already are.

        The liveness check is on the same timer rather than per cycle: a
        guardian that died would otherwise cost a syscall every 100 ms to
        discover something that changes almost never.
        """
        if self._guardian_pending:
            return
        now = self._clock()
        if now < self._guardian_retry_at:
            return
        self._guardian_retry_at = now + GUARDIAN_RETRY_S

        if self._guardian is not None:
            if self._guardian.poll() is None:
                return
            logger.warning("The brake guardian exited on its own - the brake "
                           "axis was unguarded. Restarting it.")
            self._guardian = None

        self._guardian_pending = True
        threading.Thread(target=self._spawn_guardian_now,
                         name='brake-guardian', daemon=True).start()

    def _spawn_guardian_now(self):
        """Runs on its own thread; must never let an exception escape it.

        A thread that dies here would leave ``_guardian_pending`` set forever
        and block every later attempt, which is the quiet-failure mode
        ``AGENTS.md`` §3 exists to prevent.
        """
        try:
            self._guardian = self._spawn(os.getpid())
        except Exception as exc:
            logger.error("Starting the brake guardian raised: %s: %s",
                         type(exc).__name__, exc)
            self._guardian = None
        finally:
            self._guardian_pending = False

    def guardian_ready(self):
        """Nonblocking process-status check; no spawn or wait on the cycle thread."""
        return self._guardian is not None and self._guardian.poll() is None

    def apply(self, fraction: float) -> bool:
        """Command *fraction* (0..1) of full braking. Returns True while engaged."""
        fraction = max(0.0, min(1.0, float(fraction)))
        if not self.device.acquire():
            return False

        if not self.device.set_raw(self._raw_for(fraction)):
            # A disconnected device retains its last value. Never switch LFS
            # onto an axis we cannot feed; hand back an existing intervention.
            self.release()
            return False
        if not self._holds_axis:
            # Value first, then the swap: the local write is instant, the
            # command is a TCP round trip away.
            if not self.marker.claim(MARKER_OWNER, f"/axis {self.driver_axis} brake"):
                self.device.set_raw(self._raw_for(0.0))
                return False
            self.event_bus.emit('send_command_to_lfs',
                                f"/axis {self.lfs_axis} brake")
            self._holds_axis = True
            logger.info("Brake axis handed to vJoy (LFS axis %d).", self.lfs_axis)
        return True

    def release(self):
        """Give the brake back to the driver's pedal, then park our axis.

        Safe to call at any time and from any state; calling it twice does
        nothing the second time. This is the only method that must never be
        blocked by a guard -- it can only ever *return* control.
        """
        if not self._holds_axis:
            return
        self._holds_axis = False
        # Handback first. If anything below fails, the driver already has
        # their pedal back.
        self.event_bus.emit('send_command_to_lfs',
                            f"/axis {self.driver_axis} brake")
        # Then park, so the value frozen into the device between interventions
        # is "no brake" rather than whatever we were last commanding.
        self.device.set_raw(self._raw_for(0.0))
        self.marker.release(MARKER_OWNER)
        logger.info("Brake axis handed back to the driver (LFS axis %d).",
                    self.driver_axis)

    @property
    def marker_path(self) -> str:
        """Where the handover marker lives. Kept for the tests and the log."""
        return self.marker.path

    def shutdown(self):
        """Hand back and let the device go."""
        try:
            self.release()
        finally:
            self.device.relinquish()

    # ─── Mapping ──────────────────────────────────────────────────────

    def _raw_for(self, fraction: float) -> int:
        """Interpolate between the two measured endpoints.

        Linear, because LFS's own response measured linear between them. The
        dead band at each end is LFS's, and is left alone: compensating for it
        would mean a small brake demand produced no braking at all, which is
        the wrong direction to err in.
        """
        no_brake = self.settings.get('vjoy_raw_no_brake')
        full_brake = self.settings.get('vjoy_raw_full_brake')
        return int(round(no_brake + (full_brake - no_brake) * fraction))


def _spawn_guardian(watched_pid: int):
    """Start ``guardian.py`` as a detached process. Returns the Popen or None."""
    if getattr(sys, 'frozen', False):
        command = [sys.executable, '--guardian']
    else:
        script = resolve_path('guardian.py')
        if not os.path.isfile(script):
            logger.error("guardian.py is missing - the brake axis has no watchdog.")
            return None
        command = [sys.executable, script]
    command += [str(watched_pid), resolve_data_path('settings.json'),
                resolve_data_path('brake_axis_held.marker')]
    # No console window, and not part of our process group: it has to survive
    # a Ctrl+C or a kill that takes us down.
    flags = 0
    if sys.platform.startswith('win'):
        flags = getattr(subprocess, 'CREATE_NO_WINDOW', 0) |             getattr(subprocess, 'DETACHED_PROCESS', 0)
    try:
        process = subprocess.Popen(command,
                                   creationflags=flags,
                                   stdin=subprocess.DEVNULL,
                                   stdout=subprocess.DEVNULL,
                                   stderr=subprocess.DEVNULL)
    except Exception as exc:
        logger.error("Could not start the brake guardian: %s: %s",
                     type(exc).__name__, exc)
        return None
    logger.info("Brake guardian started (pid %d), watching %d.",
                process.pid, watched_pid)
    return process
