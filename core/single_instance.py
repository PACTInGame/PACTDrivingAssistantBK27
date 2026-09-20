"""One add-on per machine, enforced at startup.

Two copies of this app can be started, and until now both ran. What that looks
like from the driver's seat was measured on 2026-09-20:

* the second copy cannot bind UDP 30000, so it is **blind** -- `WinError 10048`,
  every assistance system refused with ``no_outgauge`` (`lfs-setup.md` §2.1);
* but its InSim connection is fine, so it draws the same buttons over the first
  copy's, and both write to the same log file. The log then reads as one process
  flip-flopping between "armed" and "cannot be armed", which is a diagnosis that
  costs an hour and does not exist;
* and both arm the *same* actuators. Two `EmergencyBrake` instances press and
  release the same brake key against each other: instance 2 releasing takes
  instance 1's brake away mid-intervention. That is the one failure mode
  `control-intervention.md` §1 exists to prevent.

So the second copy does not start. ``AGENTS.md`` §5: fail loudly at startup
rather than silently in the loop.

**Why a socket and not a PID file.** A lock file survives a crash and then locks
the user out of their own add-on; detecting that needs a liveness check on a PID
that may have been reused. A listening TCP socket on the loopback interface is
released by the OS the moment the process dies, whatever killed it, and the bind
is atomic -- there is no window in which two processes both think they won. We
never set ``SO_REUSEADDR``: that option exists to defeat exactly this.

The socket carries no traffic. It is bound, never accepted on, and held for the
lifetime of the process.
"""

import errno
import logging
import os
import socket
from typing import Optional

logger = logging.getLogger(__name__)

# Loopback only -- nothing outside this machine has any business here, and
# binding to 127.0.0.1 keeps Windows from asking about the firewall.
LOCK_HOST = '127.0.0.1'

# Deliberately next to the ports this project already owns, and clear of every
# one of them: InSim 29999, OutSim 29998, OutGauge 30000, and the
# ``simulation_tests`` relay on 30010/30011.
LOCK_PORT = 29997

# Escape hatch for a developer who really does want two processes (and knows
# that the second one will be blind). Not something a driver ever sets.
OVERRIDE_ENV = 'PACT_ALLOW_MULTIPLE'


class AlreadyRunning(RuntimeError):
    """Another copy of the add-on holds the lock."""


class SingleInstance:
    """Holds the startup lock for as long as this process lives."""

    def __init__(self, host: str = LOCK_HOST, port: int = LOCK_PORT):
        self.host = host
        self.port = port
        self._socket: Optional[socket.socket] = None

    def acquire(self) -> bool:
        """Take the lock. ``False`` when somebody else already has it.

        Never raises for the "taken" case -- that is an expected outcome, not a
        fault. Any *other* error (a firewall policy, a broken stack) is logged
        and treated as "lock unavailable, carry on": refusing to start because
        we could not ask would be worse than the problem it guards against.
        """
        if self._socket is not None:
            return True
        if os.environ.get(OVERRIDE_ENV):
            logger.warning("%s is set - the single-instance lock is skipped. "
                           "A second copy cannot bind OutGauge and will run "
                           "blind.", OVERRIDE_ENV)
            return True
        lock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        try:
            # No SO_REUSEADDR, on purpose: it would let the second copy bind.
            lock.bind((self.host, self.port))
            lock.listen(1)
        except OSError as exc:
            lock.close()
            if _is_address_in_use(exc):
                return False
            logger.warning("The single-instance lock could not be taken on "
                           "%s:%d (%s: %s) - starting anyway.", self.host,
                           self.port, type(exc).__name__, exc)
            return True
        self._socket = lock
        return True

    def release(self):
        """Give the lock back. Idempotent, and never raises."""
        lock, self._socket = self._socket, None
        if lock is None:
            return
        try:
            lock.close()
        except OSError as exc:
            logger.debug("Releasing the single-instance lock failed: %s: %s",
                         type(exc).__name__, exc)

    def __enter__(self):
        return self

    def __exit__(self, *_exc):
        self.release()
        return False


def _is_address_in_use(exc: OSError) -> bool:
    """WSAEADDRINUSE (10048) on Windows, EADDRINUSE elsewhere.

    Both are checked because Python reports the Winsock code in ``winerror``
    and maps it onto ``errno`` separately; which one is set has varied.
    """
    return (exc.errno == errno.EADDRINUSE
            or getattr(exc, 'winerror', None) == 10048)
