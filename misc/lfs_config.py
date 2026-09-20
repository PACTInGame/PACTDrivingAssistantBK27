"""Reading LFS's own controller configuration off disk.

LFS keeps the whole controller setup -- which axis carries which function, and
whether it is inverted -- in ``<lfs>\\data\\misc\\<Device>.csf``. The format is
undocumented; it was reverse engineered here and the details live in
``reference/lfs-config-files.md``. Only the axis table is read; buttons, keys
and the wheel angle are skipped.

**Why this is worth reading at all.** The throttle cut has to name the axis it
gives the throttle back to, and naming the wrong one destroys whatever that axis
was doing (``reference/control-intervention.md`` §2.2). Nothing in LFS can be
*asked* for that number -- ``/axis`` writes, it does not read -- and probing for
it is exactly the destructive sweep that must never be written. This file is the
only non-destructive source, and reading it touches nothing.

Two things make it trustworthy rather than a guess:

**The right file selects itself.** Each device gets its own file and each file
is a complete snapshot, so "which device is the driver using" would normally be
a question. It is not, because we already know one number for certain: the
brake axis, proven every time an intervention hands the brake back and the
driver's pedal works again. The file whose brake entry matches that number is
the driver's; a file that disagrees is another device's copy.

**The stored number is not the number ``/axis`` takes.** It is one lower --
``brake`` reads 11 on this install where ``/axis 12 brake`` is what LFS wants.
Rather than assume that offset is always 1, it is *derived* from the known
brake number and then applied to the rest, so whatever the offset is on a given
machine, the same one is used throughout.

Cost: one file read of a few hundred bytes, done once when the throttle cut
wants to arm. Never in a hot path.
"""

import glob
import logging
import os
import struct
from typing import Dict, NamedTuple, Optional

logger = logging.getLogger(__name__)

# The container, from reference/lfs-config-files.md §3.
_MAGIC = b'LFSCON'
_VERSION_OFFSET = 7
_BUTTON_COUNT_OFFSET = 0x2c
_BUTTON_ENTRY = 4
_AXIS_ENTRY_V1 = 4
_AXIS_ENTRY = 8

# The axis table is in the order Commands.txt lists the /axis function names.
AXIS_FUNCTIONS = ('steer', 'combined', 'throttle', 'brake', 'lookh', 'lookp',
                  'lookr', 'clutch', 'handbrake', 'shiftx', 'shifty')

# What LFS writes for "this function has no axis" -- the same thing
# ``/axis -1 <function>`` produces.
UNASSIGNED = 0xFFFF


class AxisAssignment(NamedTuple):
    """One function's axis, as the number ``/axis`` takes, plus its polarity."""

    axis: int
    invert: int


def _read_axis_table(path: str) -> Optional[Dict[str, AxisAssignment]]:
    """The raw table of one file, with the *stored* (not ``/axis``) numbers.

    ``None`` for anything that is not a controller file we understand. Never
    raises: these are files a user can replace, truncate or corrupt.
    """
    try:
        with open(path, 'rb') as handle:
            data = handle.read()
    except OSError as exc:
        logger.debug("Cannot read %s: %s: %s", path, type(exc).__name__, exc)
        return None
    if len(data) < _BUTTON_COUNT_OFFSET + 4 or not data.startswith(_MAGIC):
        return None

    version = data[_VERSION_OFFSET]
    if version not in (1, 6, 7):
        return None
    width = _AXIS_ENTRY_V1 if version == 1 else _AXIS_ENTRY
    try:
        buttons = struct.unpack_from('<I', data, _BUTTON_COUNT_OFFSET)[0]
        offset = _BUTTON_COUNT_OFFSET + 4 + buttons * _BUTTON_ENTRY
        count = struct.unpack_from('<I', data, offset)[0]
        offset += 4
        if count != len(AXIS_FUNCTIONS):
            # Every version seen has exactly eleven. A different number means
            # the layout changed and nothing below can be believed.
            logger.info("%s has %d axis functions, not %d - ignoring it.",
                        os.path.basename(path), count, len(AXIS_FUNCTIONS))
            return None
        if len(data) < offset + count * width:
            return None
        table = {}
        for index, function in enumerate(AXIS_FUNCTIONS):
            axis, invert = struct.unpack_from('<HH', data,
                                              offset + index * width)
            table[function] = AxisAssignment(axis, invert)
    except struct.error:
        return None
    return table


def controller_files(lfs_directory: str):
    """Every controller configuration file, newest layout first."""
    misc = os.path.join(lfs_directory, 'data', 'misc')
    return sorted(glob.glob(os.path.join(misc, '*.csf'))) + \
        sorted(glob.glob(os.path.join(misc, '*.con')))


def axis_assignments(lfs_directory: str,
                     known_brake_axis: int) -> Optional[Dict[str, AxisAssignment]]:
    """The driver's axis assignments, as the numbers ``/axis`` takes.

    *known_brake_axis* is the anchor: the one live number we are sure of. It
    picks the device's own file out of the folder and fixes the offset between
    what is stored and what ``/axis`` wants, in one step.

    ``None`` when no file agrees -- which is the honest answer, and leaves the
    caller refusing rather than acting on a number nobody vouched for.
    """
    for path in controller_files(lfs_directory):
        table = _read_axis_table(path)
        if table is None:
            continue
        stored_brake = table['brake'].axis
        if not 0 <= stored_brake <= 31 or table['brake'].invert not in (0, 1):
            continue
        offset = known_brake_axis - stored_brake
        if not 0 <= offset <= 1:
            # The stored value is a small index and ``/axis`` numbers start one
            # higher. Anything else means this file describes another device
            # whose brake happens to sit elsewhere, not a different offset.
            continue
        resolved = {
            function: AxisAssignment(assignment.axis + offset,
                                     assignment.invert)
            for function, assignment in table.items()
            # 0xFFFD/0xFFFE also occur in saved LFS configs. Their meaning
            # is unverified; neither is a physical axis to restore with /axis.
            if 0 <= assignment.axis + offset <= 31
            and assignment.invert in (0, 1)
        }
        logger.info("Read the controller configuration from %s (offset %+d): "
                    "%s", os.path.basename(path), offset,
                    {name: value.axis for name, value in resolved.items()})
        if 'throttle' not in resolved:
            logger.warning(
                "Selected controller file %s has no usable saved throttle axis "
                "(raw axis %d, invert %d). A working pedal in LFS can differ "
                "from this saved snapshot. Check Options - Controls - Axes; "
                "stop the add-on before exiting LFS to save the current setup, "
                "then restart LFS and the add-on. No legacy binding is substituted.",
                path, table['throttle'].axis, table['throttle'].invert)
        return resolved
    logger.info("No controller file in %s names brake axis %d - the driver's "
                "other axis numbers stay unknown.",
                os.path.join(lfs_directory, 'data', 'misc'), known_brake_axis)
    return None
