"""WP2 -- error isolation, rate limiting and the watchdog.

Acceptance for WP2 reads: a subscriber that raises does not stop the other
subscribers, does not kill the emitting thread, and is reported once rather
than once per cycle; an assistance system killed by an induced exception
leaves the rest of the app running.
"""

import logging
import threading

import pytest

from assistance.base_system import AssistanceSystem
from core.event_bus import EventBus
from core.thread_manager import (MAX_CONSECUTIVE_FAILURES, ScheduledTask,
                                 ThreadManager, WATCHDOG_FACTOR)
from misc.logging_setup import ErrorThrottle


class FakeClock:
    """Monotonic time under test control -- no sleeping in the rate-limiter tests."""

    def __init__(self, start=0.0):
        self.now = start

    def __call__(self):
        return self.now

    def advance(self, seconds):
        self.now += seconds
        return self.now


@pytest.fixture
def clock():
    return FakeClock()


@pytest.fixture
def throttle(clock):
    return ErrorThrottle(logging.getLogger('tests.throttle'), burst=2,
                         interval=30.0, time_source=clock)


def records_of(caplog, logger_name):
    """caplog installs a handler per logger *and* on the root; filter by name."""
    return [record for record in caplog.records if record.name == logger_name]


def boom(*_args, **_kwargs):
    raise ValueError("induced failure")


# ─── ErrorThrottle ───────────────────────────────────────────────────────────

def test_the_first_failures_are_logged_then_the_rate_limit_kicks_in(throttle, clock, caplog):
    with caplog.at_level(logging.ERROR):
        for _ in range(50):
            throttle.report('fcw', ValueError('boom'))

    # burst=2 plus nothing further inside the same 30 s window.
    assert len(records_of(caplog, 'tests.throttle')) == 2


def test_the_rate_limit_opens_again_after_the_interval(throttle, clock, caplog):
    with caplog.at_level(logging.ERROR):
        for _ in range(10):
            throttle.report('fcw', ValueError('boom'))
        clock.advance(31.0)
        throttle.report('fcw', ValueError('boom'))

    records = records_of(caplog, 'tests.throttle')
    assert len(records) == 3
    assert "further failures suppressed" in records[-1].getMessage()


def test_sources_are_rate_limited_independently(throttle, caplog):
    with caplog.at_level(logging.ERROR):
        for _ in range(10):
            throttle.report('fcw', ValueError('boom'))
            throttle.report('bsw', ValueError('boom'))

    assert len(records_of(caplog, 'tests.throttle')) == 4


def test_consecutive_counter_counts_up_and_resets(throttle):
    assert throttle.report('fcw', ValueError()) == 1
    assert throttle.report('fcw', ValueError()) == 2
    throttle.succeeded('fcw')
    assert throttle.consecutive('fcw') == 0
    assert throttle.report('fcw', ValueError()) == 1
    assert throttle.total('fcw') == 3


# ─── EventBus ────────────────────────────────────────────────────────────────

def test_a_raising_subscriber_does_not_stop_the_others(bus):
    seen = []
    bus.subscribe('state_data', boom)
    bus.subscribe('state_data', seen.append)

    bus.emit('state_data', {'on_track': True})

    assert seen == [{'on_track': True}]


def test_a_raising_subscriber_does_not_propagate_to_the_emitter(bus):
    bus.subscribe('outgauge_data', boom)

    bus.emit('outgauge_data', None)      # must not raise -- this is the packet handler


def test_a_raising_subscriber_is_reported_once_not_once_per_cycle(clock, caplog):
    logger = logging.getLogger('tests.bus')
    bus = EventBus(ErrorThrottle(logger, burst=1, interval=30.0, time_source=clock))
    bus.subscribe('vehicles_updated', boom)

    with caplog.at_level(logging.ERROR):
        for _ in range(100):             # ten seconds of 100 ms cycles
            bus.emit('vehicles_updated', {})

    assert len(records_of(caplog, 'tests.bus')) == 1


def test_a_raising_subscriber_does_not_kill_the_emitting_thread(bus):
    survived = []
    bus.subscribe('vehicles_updated', boom)

    def worker():
        for _ in range(5):
            bus.emit('vehicles_updated', {})
        survived.append(True)

    thread = threading.Thread(target=worker)
    thread.start()
    thread.join(timeout=2.0)

    assert survived == [True]
    assert not thread.is_alive()


def test_unsubscribing_an_unknown_callback_is_harmless(bus):
    bus.unsubscribe('vehicles_updated', boom)      # never subscribed
    assert bus.subscriber_count('vehicles_updated') == 0


# ─── ThreadManager ───────────────────────────────────────────────────────────

def test_a_raising_task_does_not_stop_the_other_tasks_of_the_same_interval(bus):
    manager = ThreadManager(bus)
    calls = []
    manager.add_task(ScheduledTask('broken', boom, 100))
    manager.add_task(ScheduledTask('healthy', lambda: calls.append(1), 100))

    manager._run_cycle_once(100)

    assert calls == [1]


def test_a_task_that_keeps_raising_disables_itself_and_notifies(bus, recorder):
    events = recorder('notification')
    manager = ThreadManager(bus)
    task = ScheduledTask('broken', boom, 100)
    manager.add_task(task)

    for _ in range(MAX_CONSECUTIVE_FAILURES):
        manager._run_cycle_once(100)

    assert task.enabled is False
    assert events.count('notification') == 1
    assert 'broken' in events.last('notification')['notification']


def test_an_occasional_failure_does_not_disable_a_task(bus):
    manager = ThreadManager(bus)
    state = {'n': 0}

    def flaky():
        state['n'] += 1
        if state['n'] % 2:
            raise ValueError('every other cycle')

    task = ScheduledTask('flaky', flaky, 100)
    manager.add_task(task)

    for _ in range(4 * MAX_CONSECUTIVE_FAILURES):
        manager._run_cycle_once(100)

    assert task.enabled is True


def test_a_task_that_ran_is_not_reported_as_stalled(bus, clock):
    manager = ThreadManager(bus)
    task = ScheduledTask('assistance_processing', lambda: None, 100)
    manager.add_task(task)
    task.last_execution = 10.0

    assert manager.check_stalled_tasks(now=10.0 + 0.1 * WATCHDOG_FACTOR) == []


def test_a_task_that_stopped_running_is_reported_once(bus, caplog):
    manager = ThreadManager(bus)
    task = ScheduledTask('ui_updates', lambda: None, 50)
    manager.add_task(task)
    task.last_execution = 10.0
    late = 10.0 + 5.0 * WATCHDOG_FACTOR * 0.05

    with caplog.at_level(logging.ERROR):
        assert manager.check_stalled_tasks(now=late) == [task]
        assert manager.check_stalled_tasks(now=late + 1.0) == [task]

    assert len(records_of(caplog, 'core.thread_manager')) == 1


def test_a_disabled_task_is_not_watched(bus):
    manager = ThreadManager(bus)
    task = ScheduledTask('off', lambda: None, 100, enabled=False)
    manager.add_task(task)
    task.last_execution = 0.0

    assert manager.check_stalled_tasks(now=1000.0) == []


def test_a_cycle_overrun_is_logged_but_rate_limited(bus, caplog):
    manager = ThreadManager(bus)
    with caplog.at_level(logging.WARNING):
        for _ in range(20):
            manager._check_budget(100, 250.0)

    records = records_of(caplog, 'core.thread_manager')
    assert len(records) == 1
    assert '250.0 ms' in records[0].getMessage()


def test_a_cycle_inside_its_budget_is_silent(bus, caplog):
    manager = ThreadManager(bus)
    with caplog.at_level(logging.WARNING):
        manager._check_budget(100, 12.0)

    assert records_of(caplog, 'core.thread_manager') == []


def test_start_and_stop_really_run_and_really_join(bus):
    manager = ThreadManager(bus)
    calls = []
    manager.add_task(ScheduledTask('tick', lambda: calls.append(1), 50))

    manager.start()
    try:
        deadline = threading.Event()
        deadline.wait(0.3)
    finally:
        stopped = manager.stop(timeout=2.0)

    assert stopped is True
    assert len(calls) >= 2
    before = len(calls)
    threading.Event().wait(0.2)
    assert len(calls) == before          # nothing runs after stop()


def test_add_task_rejects_a_zero_interval(bus):
    manager = ThreadManager(bus)
    with pytest.raises(ValueError):
        manager.add_task(ScheduledTask('bad', lambda: None, 0))


# ─── AssistanceManager ───────────────────────────────────────────────────────

class Exploding(AssistanceSystem):
    def __init__(self, event_bus, settings, name='forward_collision_warning'):
        super().__init__(name, event_bus, settings)
        self.calls = 0

    def process(self, own_vehicle, vehicles):
        self.calls += 1
        raise ValueError("induced failure")


class Counting(AssistanceSystem):
    def __init__(self, event_bus, settings, name='blind_spot_warning'):
        super().__init__(name, event_bus, settings)
        self.calls = 0

    def process(self, own_vehicle, vehicles):
        self.calls += 1
        return {'ok': True}


@pytest.fixture
def manager(bus, settings, monkeypatch):
    """An AssistanceManager with its real systems replaced by test doubles."""
    from assistance import manager as manager_module

    monkeypatch.setattr(manager_module.AssistanceManager, '_init_systems',
                        lambda self: None)
    instance = manager_module.AssistanceManager(bus, settings)
    instance.chat_commands = type('NoChat', (), {'check_tooltip': lambda self: None})()
    return instance


def test_a_broken_system_does_not_stop_the_others(manager, bus, settings, make_own_vehicle):
    broken = Exploding(bus, settings)
    healthy = Counting(bus, settings)
    manager.systems = {'fcw': broken, 'bsw': healthy}
    manager.own_vehicle = make_own_vehicle()
    manager.on_track = True

    results = manager.process_all_systems()

    assert healthy.calls == 1
    assert results == {'bsw': {'ok': True}}


def test_a_permanently_broken_system_disables_itself_and_notifies(
        manager, bus, settings, recorder, make_own_vehicle):
    events = recorder('notification')
    broken = Exploding(bus, settings)
    healthy = Counting(bus, settings)
    manager.systems = {'fcw': broken, 'bsw': healthy}
    manager.own_vehicle = make_own_vehicle()
    manager.on_track = True

    for _ in range(50):
        manager.process_all_systems()

    assert 'fcw' in manager.failed_systems
    assert broken.calls == 5             # MAX_CONSECUTIVE_FAILURES, then never again
    assert healthy.calls == 50           # the rest of the app keeps running
    assert events.count('notification') == 1


def test_re_enabling_a_system_clears_its_failure_state(manager, bus, settings):
    manager.systems = {'fcw': Exploding(bus, settings)}
    manager.failed_systems.add('fcw')

    manager.enable_system('fcw', True)

    assert 'fcw' not in manager.failed_systems


def test_a_broken_chat_tooltip_does_not_kill_the_cycle(manager, make_own_vehicle):
    manager.own_vehicle = make_own_vehicle()
    manager.chat_commands = type('Boom', (), {'check_tooltip': boom})()

    manager.process_all_systems()        # must not raise


def test_state_data_without_on_track_does_not_raise(manager):
    manager._update_state_data({})

    assert manager.on_track is False
