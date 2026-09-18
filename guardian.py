"""Watchdog process: give the brake back if PACT dies holding it.

Runs as a separate process next to the main app, because that is the only thing
that can help here. While automatic emergency braking is engaged for a
wheel/joystick driver, LFS's brake axis points at a vJoy axis we are feeding
(``Controls/brake_axis.py``). **vJoy holds the last value it was fed, forever** —
neither resetting nor relinquishing the device moves the axis, measured against
LFS. So a main process that is killed mid-intervention leaves the car with the
brake nailed down and the driver's own pedal not assigned to brake at all. A
dead process cannot send ``/axis``; a live one next to it can.

    main app  --spawns-->  guardian  --waits on PID-->  replays /axis commands

What it does, and deliberately nothing else:

1. wait for the main process to exit, by PID;
2. look for the handover marker the main app writes while it holds anything;
3. if it is there, open an InSim connection and send the commands it names;
4. exit.

An intervention can hold more than the brake: the throttle is unassigned for
its duration too (``Controls/throttle_cut.py``), and a process killed in that
state leaves a driver who can neither accelerate nor stop the car braking. So
the marker carries a **list of commands to replay**, not an axis number, and
this process does not need to know what any of them mean. Older markers held a
bare axis number and are still understood.

Three design notes worth keeping.

**The marker decides whether to act at all.** ``AxisBrakeOutput`` creates it the
moment LFS's brake is pointed at the virtual axis and deletes it the moment the
brake is handed back, so its presence after the main process is gone means
exactly "died while holding". Without that check every clean shutdown would
force the brake onto whatever axis number our settings happened to hold -- and
if that number were wrong we would break a working configuration on every exit,
which is the opposite of the job.

**The numbers come from the marker, with settings as the fallback.** They
differ per user and the driver may recalibrate; the marker carries what was in
force when the swap happened, which is the only thing that restores what we
took away.

**Only commands this file recognises are sent.** The marker is a file on disk,
so it can be corrupt, truncated or edited; it is matched against a pattern that
allows exactly ``/axis``, ``/key`` and ``/invert`` for the two functions an
intervention ever takes, and anything else is dropped. A watchdog that would type whatever it
found in a file into the game is not a safety device.

**No IPC beyond that file.** A channel that has to survive the other end being
killed is exactly the thing that will not work when it matters.

Started by the main app; can also be run by hand for testing::

    python guardian.py <pid-to-watch>
"""

import json
import logging
import os
import re
import socket
import struct
import sys
import time

logger = logging.getLogger('guardian')

# The main app may already be gone when we get here, so nothing in this file
# imports from it. Reading one integer out of a JSON file and speaking enough
# InSim to send one command is less code than the import would be, and it
# cannot be broken by a half-torn-down package.
SETTINGS_FILE = 'settings.json'
MARKER_FILE = 'brake_axis_held.marker'
DEFAULT_BRAKE_AXIS = 12

# What a marker is allowed to ask for. ``/axis -1 <fn>`` and ``/key -1 <fn>``
# unassign, which is never a *restore*, so they are not in here: a marker that
# asked for one would leave the driver with the function still gone.
_ALLOWED_COMMAND = re.compile(
    r'^/(axis|key) [A-Za-z0-9]{1,6} (brake|throttle)$'
    r'|^/invert [01] (brake|throttle)$')

INSIM_HOST = '127.0.0.1'
INSIM_PORT = 29999
INSIM_VERSION = 10

ISP_ISI = 1
ISP_MST = 13
ISF_LOCAL = 4

# How often the PID is checked while the main app is alive. Two seconds is
# invisible in CPU terms and bounds how long a stuck brake can last.
POLL_INTERVAL_S = 2.0
# If LFS is gone too there is nothing to hand back to.
CONNECT_TIMEOUT_S = 3.0


def _valid_axis(value):
    try:
        axis = int(value)
    except (TypeError, ValueError):
        return None
    return axis if 0 <= axis <= 31 else None


def read_marker(marker_path: str):
    """The commands recorded in the handover marker.

    ``None``  -- no marker: the main app was not holding anything, which is the
    normal case after a clean exit, and nothing must be sent.
    ``[]``    -- marker present but unusable: something *was* held, so act
    anyway, with the brake handback rebuilt from the settings file.

    Three shapes are accepted, in order of age: the JSON object written today,
    a bare axis number from the version that only ever held the brake, and an
    empty or corrupt file.
    """
    try:
        with open(marker_path, encoding='utf-8') as handle:
            content = handle.read().strip()
    except FileNotFoundError:
        return None
    except OSError as exc:
        logger.warning("Handover marker unreadable (%s: %s) - assuming "
                       "something was held.", type(exc).__name__, exc)
        return []
    if not content:
        return []

    axis = _valid_axis(content)
    if axis is not None:
        # Pre-JSON marker: the brake, and only the brake.
        return [f"/axis {axis} brake"]

    try:
        payload = json.loads(content)
        commands = [str(command) for command in payload['commands']]
    except Exception as exc:
        logger.warning("Handover marker is not readable JSON (%s: %s) - "
                       "falling back to the configured brake axis.",
                       type(exc).__name__, exc)
        return []

    allowed = [command for command in commands
               if _ALLOWED_COMMAND.match(command)]
    for command in commands:
        if command not in allowed:
            logger.warning("Ignoring an unexpected marker command: %r", command)
    return allowed


def read_brake_axis(settings_path: str) -> int:
    """The driver's own brake axis, as configured right now."""
    try:
        with open(settings_path, encoding='utf-8') as handle:
            value = json.load(handle).get('user_axis_brake')
        axis = int(value)
    except Exception as exc:
        logger.warning("Could not read %s (%s: %s) - falling back to axis %d.",
                       settings_path, type(exc).__name__, exc,
                       DEFAULT_BRAKE_AXIS)
        return DEFAULT_BRAKE_AXIS
    if not 0 <= axis <= 31:
        logger.warning("user_axis_brake=%r is out of range - falling back to "
                       "axis %d.", value, DEFAULT_BRAKE_AXIS)
        return DEFAULT_BRAKE_AXIS
    return axis


def process_is_alive(pid: int) -> bool:
    """Is that PID still running?

    ``psutil`` is a dependency of the project, but this process must survive
    the main app being torn down mid-import, so there is a stdlib fallback.
    """
    try:
        import psutil
        return psutil.pid_exists(pid)
    except ImportError:
        pass
    try:
        os.kill(pid, 0)
    except OSError:
        return False
    return True


# The two packet layouts, copied from ``pyinsim.insim`` rather than imported:
# this process has to work while the main app is being torn down.
#
# Note the ``Size`` field: since InSim v9 it is the byte count **divided by
# four**, not the byte count. IS_ISI is 44 bytes on the wire and carries 11;
# IS_MST is 68 bytes and carries 17. Sending the byte count makes LFS drop the
# connection without a word.
_ISI_STRUCT = struct.Struct('4B2HBcH15sx15sx')     # 44 bytes
_MST_STRUCT = struct.Struct('4B63sx')              # 68 bytes


def _isi_packet() -> bytes:
    """IS_ISI: the InSim handshake. No admin password, no packet stream."""
    return _ISI_STRUCT.pack(
        44 // 4, ISP_ISI, 0, 0,
        0,                  # UDPPort - TCP only
        ISF_LOCAL,
        INSIM_VERSION,
        b' ',               # Prefix - we never send chat
        0,                  # Interval - we want no packets at all
        b'',                # Admin
        b'guardian')


def _mst_packet(command: str) -> bytes:
    """IS_MST: a command as if typed into LFS."""
    return _MST_STRUCT.pack(68 // 4, ISP_MST, 0, 0,
                            command.encode('latin-1')[:63])


def hand_back(commands) -> bool:
    """Send every *command* to LFS. False if LFS could not be reached.

    One connection for all of them: the handshake pause is the expensive part,
    and the driver is sitting in a car that is braking by itself while this
    runs.
    """
    if not commands:
        return False
    try:
        with socket.create_connection((INSIM_HOST, INSIM_PORT),
                                      timeout=CONNECT_TIMEOUT_S) as sock:
            sock.sendall(_isi_packet())
            # LFS needs the handshake processed before it will act on a
            # command; a short pause is cheaper than parsing the version reply.
            time.sleep(0.3)
            for command in commands:
                sock.sendall(_mst_packet(command))
                time.sleep(0.05)
            time.sleep(0.3)
    except OSError as exc:
        logger.info("LFS is not reachable (%s: %s) - nothing to hand back to.",
                    type(exc).__name__, exc)
        return False
    logger.info("Handed control back: %s", ', '.join(commands))
    return True


def watch(pid: int, settings_path: str, marker_path: str,
          poll_interval: float = POLL_INTERVAL_S, sleep=time.sleep) -> bool:
    """Wait for *pid* to disappear, then replay the marker if there is one.

    Returns whether a handback was sent and reached LFS. Split from
    :func:`main` so the whole sequence is testable without spawning anything.
    """
    logger.info("Guarding PID %d.", pid)
    while process_is_alive(pid):
        sleep(poll_interval)
    logger.info("PID %d is gone.", pid)

    commands = read_marker(marker_path)
    if commands is None:
        logger.info("No handover marker - nothing was ours, so there is "
                    "nothing to restore.")
        return False

    if not commands:
        commands = [f"/axis {read_brake_axis(settings_path)} brake"]
    logger.warning("The main app died mid-intervention. Restoring: %s",
                   ', '.join(commands))
    handed_back = hand_back(commands)
    if handed_back:
        try:
            os.remove(marker_path)
        except OSError:
            pass
    return handed_back


def main(argv=None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    if not argv:
        print(__doc__)
        return 2
    logging.basicConfig(
        level=logging.INFO,
        format='%(asctime)s guardian: %(message)s',
        datefmt='%H:%M:%S')

    here = os.path.dirname(os.path.abspath(__file__))
    pid = int(argv[0])
    settings_path = argv[1] if len(argv) > 1 else os.path.join(here, SETTINGS_FILE)
    marker_path = argv[2] if len(argv) > 2 else os.path.join(here, MARKER_FILE)
    watch(pid, settings_path, marker_path)
    return 0


if __name__ == '__main__':
    sys.exit(main())
