"""The startup lock that keeps a second copy of the add-on from running.

Measured on 2026-09-20, and the reason this exists: two copies were started by
accident. The second could not bind UDP 30000, so it ran blind and refused every
actuation with ``no_outgauge`` -- but its InSim connection was fine, so it drew
buttons over the first one's and both wrote to the same log file. The result read
as one process alternating between "armed" and "cannot be armed".

The tests use a port of their own rather than the real one, so running the suite
never collides with an add-on the developer has open.
"""

import errno
import socket

import pytest

from core import single_instance
from core.single_instance import OVERRIDE_ENV, SingleInstance


@pytest.fixture
def lock_port():
    """A free port, found by letting the OS pick one and giving it straight back.

    There is a race here in principle -- something else could take the port in
    between -- but it is the standard trick and it beats hard-coding a number
    that a developer's own add-on may be sitting on.
    """
    probe = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    probe.bind(('127.0.0.1', 0))
    port = probe.getsockname()[1]
    probe.close()
    return port


def test_the_first_copy_takes_the_lock_and_the_second_does_not(lock_port):
    first = SingleInstance(port=lock_port)
    second = SingleInstance(port=lock_port)

    assert first.acquire() is True
    assert second.acquire() is False

    first.release()


def test_the_lock_is_free_again_once_the_holder_lets_go(lock_port):
    """The whole point of a socket over a PID file: no stale lock survives."""
    first = SingleInstance(port=lock_port)
    second = SingleInstance(port=lock_port)
    first.acquire()

    first.release()

    assert second.acquire() is True
    second.release()


def test_acquiring_twice_from_the_same_holder_is_not_a_conflict(lock_port):
    holder = SingleInstance(port=lock_port)

    assert holder.acquire() is True
    assert holder.acquire() is True

    holder.release()


def test_releasing_a_lock_that_was_never_taken_does_nothing(lock_port):
    SingleInstance(port=lock_port).release()      # must not raise


def test_releasing_twice_does_nothing_the_second_time(lock_port):
    holder = SingleInstance(port=lock_port)
    holder.acquire()

    holder.release()
    holder.release()                              # must not raise


def test_the_context_manager_gives_the_lock_back(lock_port):
    with SingleInstance(port=lock_port) as holder:
        assert holder.acquire() is True

    assert SingleInstance(port=lock_port).acquire() is True


def test_an_error_that_is_not_a_taken_port_still_lets_the_app_start(
        monkeypatch, lock_port):
    """Refusing to start because we could not *ask* would be worse than the
    problem the lock guards against (``AGENTS.md`` §3, fail in the safe
    direction)."""
    class Refusing(socket.socket):
        def bind(self, _address):
            raise OSError(13, "policy says no")

    monkeypatch.setattr(single_instance.socket, 'socket',
                        lambda *a, **k: Refusing())

    assert SingleInstance(port=lock_port).acquire() is True


def test_the_override_skips_the_lock_entirely(monkeypatch, lock_port):
    monkeypatch.setenv(OVERRIDE_ENV, '1')
    first = SingleInstance(port=lock_port)
    second = SingleInstance(port=lock_port)

    assert first.acquire() is True
    assert second.acquire() is True

    first.release()
    second.release()


def test_a_taken_port_is_recognised_however_windows_reports_it():
    """Python puts the Winsock code in ``winerror`` and maps it onto ``errno``
    separately; which one is set has varied, so both are checked."""
    by_errno = OSError(errno.EADDRINUSE, "Address already in use")

    windows = OSError(errno.EINVAL, "")
    windows.winerror = 10048

    unrelated = OSError(errno.EACCES, "permission denied")

    assert single_instance._is_address_in_use(by_errno) is True
    assert single_instance._is_address_in_use(windows) is True
    assert single_instance._is_address_in_use(unrelated) is False
