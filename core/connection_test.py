"""Startup probe: is LFS reachable on the InSim port?

Two defects made the retry loop in ``main.py`` unreachable (WP2):

* the InSim object was built **once, in ``__init__``**. ``insim_state`` calls
  ``pyinsim.closeall()``, and a second ``run_test()`` then reused an already
  closed dispatcher, so every retry after the first was guaranteed to fail;
* ``pyinsim.run()`` is ``asyncore.loop()``, which returns only when the socket
  map is empty. With LFS not answering it never returns, so the app hung on
  attempt one instead of retrying.

Both are fixed here: one fresh object per attempt, and the asyncore loop is
pumped in slices against a deadline instead of being entered open-endedly.
``asyncore`` is used directly for that -- ``pyinsim.run()`` has no bounded
form, and giving it one would change the transport every other module relies
on (``CLAUDE.md`` §2).
"""

import asyncore
import logging
import time

import pyinsim

logger = logging.getLogger(__name__)

DEFAULT_TIMEOUT_S = 5.0
_POLL_SLICE_S = 0.05


class LfsConnectionTest:
    """Opens a throwaway InSim connection and waits for one ``IS_STA``."""

    def __init__(self, host: bytes = b'127.0.0.1', port: int = 29999,
                 timeout: float = DEFAULT_TIMEOUT_S):
        self.host = host
        self.port = port
        self.timeout = timeout
        self.connection_successful = False
        self.insim = None

    def insim_state(self, insim, sta):
        """``ISP_STA`` handler -- LFS answered, so the connection works."""
        self.connection_successful = True

    def run_test(self) -> bool:
        """One attempt. Never blocks longer than ``timeout``.

        Returns True if LFS answered the state request within the timeout.
        Every attempt builds its own InSim object and closes it again, so
        calling this repeatedly is safe.
        """
        self.connection_successful = False
        self.insim = None
        try:
            self.insim = pyinsim.insim(self.host, self.port, Admin=b'')
            self.insim.bind(pyinsim.ISP_STA, self.insim_state)
            self.insim.send(pyinsim.ISP_TINY, ReqI=255, SubT=pyinsim.TINY_SST)
            self._pump_until_answered()
        except Exception as e:
            logger.warning("InSim connection attempt failed: %s: %s",
                           type(e).__name__, e)
        finally:
            # closeall() rather than insim.close(): a failed connect can leave
            # a half-open dispatcher in the global asyncore map, and the real
            # LFSConnector must start from an empty map.
            try:
                pyinsim.closeall()
            except Exception:                     # pragma: no cover - defensive
                logger.debug("closeall() after connection test failed", exc_info=True)

        return self.connection_successful

    def _pump_until_answered(self):
        deadline = time.monotonic() + self.timeout
        while not self.connection_successful and time.monotonic() < deadline:
            if not asyncore.socket_map:
                # Socket already gone (connection refused) - nothing left to poll.
                break
            asyncore.loop(timeout=_POLL_SLICE_S, count=1)


if __name__ == '__main__':
    logging.basicConfig(level=logging.INFO)
    test = LfsConnectionTest()
    print("LFS reachable:", test.run_test())
