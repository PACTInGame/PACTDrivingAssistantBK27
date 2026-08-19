"""WP2 -- startup probe and shutdown.

Two things the app never did: it never came back from a failed connection
attempt (``core/connection_test.py`` reused a closed dispatcher and blocked in
``pyinsim.run()`` forever), and it never cleaned up (``shutdown()`` was a
``pass``, so LFS kept the buttons and the socket stayed open).
"""

import logging
import types

import pytest

import core.connection_test as connection_test
from core.connection_test import LfsConnectionTest


class FakeTcp:
    def __init__(self):
        self.closed = False

    def close(self):
        self.closed = True


class FakeInsimConnection:
    """Just enough of ``_InSim`` for the connection test."""

    instances = []

    def __init__(self, answer_after=None):
        self.bindings = {}
        self.sent = []
        self.closed = False
        self.answer_after = answer_after
        self.polls = 0
        FakeInsimConnection.instances.append(self)

    def bind(self, packet_type, handler):
        self.bindings[packet_type] = handler

    def send(self, packet_type, **fields):
        self.sent.append((packet_type, fields))

    def close(self):
        self.closed = True


@pytest.fixture
def fake_pyinsim(monkeypatch):
    """Replaces the pyinsim module and asyncore inside connection_test."""
    FakeInsimConnection.instances = []
    socket_map = {'tcp': object()}
    state = {'answer_after': None, 'raise_on_connect': None, 'closed_all': 0,
             'socket_map': socket_map}

    def insim(host, port, **kwargs):
        if state['raise_on_connect']:
            raise state['raise_on_connect']
        return FakeInsimConnection(answer_after=state['answer_after'])

    def closeall():
        state['closed_all'] += 1
        socket_map.clear()

    fake = types.SimpleNamespace(
        insim=insim, closeall=closeall,
        ISP_STA=5, ISP_TINY=3, TINY_SST=8)
    monkeypatch.setattr(connection_test, 'pyinsim', fake)

    polls = {'n': 0}

    def loop(timeout=0.0, count=None):
        polls['n'] += 1
        current = FakeInsimConnection.instances[-1]
        answer_after = current.answer_after
        if answer_after is not None and polls['n'] >= answer_after:
            handler = current.bindings.get(fake.ISP_STA)
            if handler:
                handler(current, object())

    fake_asyncore = types.SimpleNamespace(socket_map=socket_map, loop=loop)
    monkeypatch.setattr(connection_test, 'asyncore', fake_asyncore)

    state['polls'] = polls
    state['module'] = fake
    return state


def test_a_silent_lfs_makes_the_test_time_out_instead_of_blocking(fake_pyinsim):
    fake_pyinsim['answer_after'] = None
    test = LfsConnectionTest(timeout=0.2)

    assert test.run_test() is False
    assert fake_pyinsim['polls']['n'] > 0        # it really polled, it did not hang


def test_an_answering_lfs_makes_the_test_succeed(fake_pyinsim):
    fake_pyinsim['answer_after'] = 2
    test = LfsConnectionTest(timeout=1.0)

    assert test.run_test() is True


def test_every_attempt_builds_a_fresh_insim_object(fake_pyinsim):
    """The old code reused the dispatcher closed by the first attempt."""
    fake_pyinsim['answer_after'] = None
    test = LfsConnectionTest(timeout=0.05)

    test.run_test()
    fake_pyinsim['socket_map']['tcp'] = object()      # LFS came back
    test.run_test()

    assert len(FakeInsimConnection.instances) == 2
    assert FakeInsimConnection.instances[0] is not FakeInsimConnection.instances[1]


def test_a_retry_after_a_failure_can_still_succeed(fake_pyinsim):
    """This is the case the app could never reach before."""
    fake_pyinsim['answer_after'] = None
    test = LfsConnectionTest(timeout=0.05)
    assert test.run_test() is False

    fake_pyinsim['answer_after'] = 1
    fake_pyinsim['polls']['n'] = 0
    fake_pyinsim['socket_map']['tcp'] = object()

    assert test.run_test() is True


def test_a_refused_connection_is_reported_not_raised(fake_pyinsim, caplog):
    fake_pyinsim['raise_on_connect'] = ConnectionRefusedError("nothing listening")
    test = LfsConnectionTest(timeout=0.05)

    with caplog.at_level(logging.WARNING):
        assert test.run_test() is False

    assert any('ConnectionRefusedError' in record.getMessage()
               for record in caplog.records)


def test_every_attempt_closes_what_it_opened(fake_pyinsim):
    fake_pyinsim['answer_after'] = 1
    LfsConnectionTest(timeout=0.2).run_test()

    assert fake_pyinsim['closed_all'] == 1


# ─── Shutdown ────────────────────────────────────────────────────────────────

class ShutdownProbe:
    """Records the order of the shutdown steps."""

    def __init__(self):
        self.order = []

    def make_thread_manager(self):
        probe = self

        class TM:
            def stop(self, timeout=2.0):
                probe.order.append('threads_stopped')
                return True
        return TM()

    def make_message_sender(self):
        probe = self

        class MS:
            def remove_all(self):
                probe.order.append('buttons_removed')
                return True
        return MS()

    def make_connector(self):
        probe = self

        class C:
            def disconnect(self):
                probe.order.append('disconnected')
        return C()


def _app_with(probe):
    from main import LFSAssistantApp

    app = LFSAssistantApp.__new__(LFSAssistantApp)
    app._shutdown_done = False
    app.thread_manager = probe.make_thread_manager()
    app.message_sender = probe.make_message_sender()
    app.lfs_connector = probe.make_connector()
    return app


def test_shutdown_stops_threads_removes_buttons_and_disconnects():
    probe = ShutdownProbe()
    app = _app_with(probe)

    app.shutdown()

    assert probe.order == ['threads_stopped', 'buttons_removed', 'disconnected']


def test_shutdown_runs_only_once():
    probe = ShutdownProbe()
    app = _app_with(probe)

    app.shutdown()
    app.shutdown()

    assert probe.order == ['threads_stopped', 'buttons_removed', 'disconnected']


def test_a_failing_step_does_not_stop_the_remaining_cleanup():
    probe = ShutdownProbe()
    app = _app_with(probe)

    class BrokenSender:
        def remove_all(self):
            raise RuntimeError("socket already gone")

    app.message_sender = BrokenSender()

    app.shutdown()

    assert probe.order == ['threads_stopped', 'disconnected']


def test_the_signal_handler_interrupts_the_asyncore_loop():
    """asyncore re-raises KeyboardInterrupt; closeall() would not let us
    send the goodbye packets any more."""
    probe = ShutdownProbe()
    app = _app_with(probe)

    with pytest.raises(KeyboardInterrupt):
        app._on_signal(2, None)
