"""Minimal ctypes binding to one vJoy device axis.

Replaces the axis half of ``misc/vjoy.py``, which packed a 24-field joystick
struct by hand for every update, printed at import time, pulled in ``numpy``
for a demo function, and read the DLL path out of a ``controls.txt`` that does
not exist in this project. All we need from vJoy is "put this axis at this
value", and ``SetAxis`` does exactly that.

The DLL is loaded lazily, so this module imports on any platform and the tests
run without vJoy. Everything degrades to "unavailable" rather than raising.

**vJoy holds the last value it was fed, forever.** Measured against LFS::

    vjoy raw 0, axis assigned to brake   -> LFS brake 1.000
    after ResetVJD                       -> LFS brake 1.000   (no effect)
    after RelinquishVJD, feeder gone     -> LFS brake 1.000   (no effect)
    2 s later                            -> LFS brake 1.000

Neither resetting the device nor giving it back moves the axis. That single
fact shapes the whole design of ``Controls/brake_axis.py``: whatever we last
wrote is what LFS keeps seeing after we are gone, so the axis must be parked at
"no brake" the moment an intervention ends, and a process that dies *during* an
intervention leaves the car braking. See ``reference/control-intervention.md``
§3.2.
"""

import ctypes
import logging
import os
from typing import Optional

logger = logging.getLogger(__name__)

# vJoy installs to a fixed location; the 64-bit DLL is the one that matches a
# 64-bit Python. The environment variable is an escape hatch for a portable or
# relocated install, not something a user is expected to set.
_DEFAULT_DLL_PATHS = (
    r"C:\Program Files\vJoy\x64\vJoyInterface.dll",
    r"C:\Program Files\vJoy\x86\vJoyInterface.dll",
)

# HID usage IDs, as vJoy numbers its axes.
AXIS_X = 0x30
AXIS_Y = 0x31
AXIS_Z = 0x32
AXIS_RX = 0x33
AXIS_RY = 0x34
AXIS_RZ = 0x35

# GetVJDStatus
VJD_STATUS_OWN = 0      # already ours
VJD_STATUS_FREE = 1     # free to acquire
VJD_STATUS_BUSY = 2     # owned by another application
VJD_STATUS_MISS = 3     # not configured in vJoyConf
_ACQUIRABLE = (VJD_STATUS_OWN, VJD_STATUS_FREE)


def _find_dll() -> Optional[str]:
    override = os.environ.get('PACT_VJOY_DLL')
    candidates = (override,) + _DEFAULT_DLL_PATHS if override else _DEFAULT_DLL_PATHS
    for path in candidates:
        if path and os.path.isfile(path):
            return path
    return None


class VJoyDevice:
    """One vJoy device, used for a single axis.

    Nothing happens in ``__init__`` beyond remembering the ids: the driver is
    only touched when someone actually tries to :meth:`acquire` it.
    """

    def __init__(self, device_id: int = 1, axis: int = AXIS_X):
        self.device_id = device_id
        self.axis = axis
        self._dll = None
        self._loaded = False
        self.acquired = False
        self.raw_min = 0
        self.raw_max = 32767

    # ─── Availability ─────────────────────────────────────────────────

    def _load(self):
        """Load the DLL once. Never raises; ``self._dll`` stays None on failure."""
        if self._loaded:
            return
        self._loaded = True
        path = _find_dll()
        if path is None:
            logger.info("vJoy is not installed - no virtual brake axis.")
            return
        try:
            dll = ctypes.CDLL(path)
            for name in ('GetVJDAxisMin', 'GetVJDAxisMax'):
                getattr(dll, name).argtypes = [
                    ctypes.c_uint, ctypes.c_uint, ctypes.POINTER(ctypes.c_long)]
            dll.SetAxis.argtypes = [ctypes.c_long, ctypes.c_uint, ctypes.c_uint]
            dll.GetvJoyVersion.restype = ctypes.c_short
        except Exception as exc:
            logger.warning("vJoy DLL at %s could not be loaded: %s: %s",
                           path, type(exc).__name__, exc)
            return
        self._dll = dll

    def unavailable_reason(self) -> Optional[str]:
        """Why this device cannot be driven, or ``None`` if it can.

        Asked before arming, and asked again later: a device can be taken away
        by another application between one lap and the next.
        """
        self._load()
        if self._dll is None:
            return 'vjoy_not_installed'
        try:
            if not self._dll.vJoyEnabled():
                return 'vjoy_driver_disabled'
            status = self._dll.GetVJDStatus(self.device_id)
        except Exception as exc:
            logger.warning("vJoy status query failed: %s: %s",
                           type(exc).__name__, exc)
            return 'vjoy_query_failed'
        if status == VJD_STATUS_MISS:
            return 'vjoy_device_not_configured'
        if status not in _ACQUIRABLE:
            return 'vjoy_device_busy'
        if not self._dll.GetVJDAxisExist(self.device_id, self.axis):
            return 'vjoy_axis_not_enabled'
        return None

    # ─── Lifecycle ────────────────────────────────────────────────────

    def acquire(self) -> bool:
        """Take ownership of the device and learn its raw axis range."""
        if self.acquired:
            return True
        if self.unavailable_reason() is not None:
            return False
        try:
            if not self._dll.AcquireVJD(self.device_id):
                return False
            low, high = ctypes.c_long(), ctypes.c_long()
            self._dll.GetVJDAxisMin(self.device_id, self.axis, ctypes.byref(low))
            self._dll.GetVJDAxisMax(self.device_id, self.axis, ctypes.byref(high))
            self.raw_min, self.raw_max = low.value, high.value
        except Exception as exc:
            logger.error("Acquiring vJoy device %d failed: %s: %s",
                         self.device_id, type(exc).__name__, exc)
            return False
        self.acquired = True
        logger.info("vJoy device %d acquired, axis raw range %d..%d",
                    self.device_id, self.raw_min, self.raw_max)
        return True

    def relinquish(self):
        """Give the device back. Does **not** move the axis -- nothing does.

        Park the axis at a safe value *before* calling this; after it, the
        value we leave behind is the value LFS keeps reading.
        """
        if not self.acquired:
            return
        self.acquired = False
        try:
            self._dll.RelinquishVJD(self.device_id)
        except Exception as exc:
            logger.warning("Relinquishing vJoy device %d failed: %s: %s",
                           self.device_id, type(exc).__name__, exc)

    # ─── Output ───────────────────────────────────────────────────────

    def set_raw(self, value: int) -> bool:
        """Write a raw axis value. Clamped to the device's range."""
        if not self.acquired:
            return False
        value = max(self.raw_min, min(self.raw_max, int(value)))
        try:
            return bool(self._dll.SetAxis(value, self.device_id, self.axis))
        except Exception as exc:
            logger.error("vJoy SetAxis failed: %s: %s", type(exc).__name__, exc)
            return False

    def version(self) -> Optional[int]:
        """Driver version, for the log line that says what we are talking to."""
        self._load()
        if self._dll is None:
            return None
        try:
            return self._dll.GetvJoyVersion()
        except Exception:
            return None
