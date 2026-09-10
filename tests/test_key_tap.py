"""The timed key press that must not block the assistance thread.

``misc/key_tap.py`` exists for one reason: a keystroke LFS can actually see has
to be *held*, and holding it with ``time.sleep`` on the shared 100 ms thread
cost 200-440 ms per actuation (``reference/known-issues.md`` #43). So the two
properties worth pinning are opposites of each other:

* ``tap()`` returns immediately -- it must not wait for the hold, and
* the hold really happens anyway, on the tapper's own thread.

Everything else here guards the traps: a re-tap must not restart the key for
LFS, a name from ``settings.json`` must reach pyautogui in pyautogui's spelling,
and nothing may stay pressed after ``release_all()``.

No keyboard is involved: off Windows ``misc/platform_shim`` hands out a null
module that records every call, so ``platform_shim.recorded_calls()`` answers
"which key went down, and when" exactly (``reference/testing.md``).
"""

import time

import pytest

from misc import platform_shim
from misc.key_tap import KeyTapper

# The recording only exists while the real pyautogui is absent -- which is the
# case everywhere the suite is meant to run (Linux CI, macOS).
pytestmark = pytest.mark.skipif(
    platform_shim.is_available('pyautogui'),
    reason="key injection is only observable through the null module")

# Real wall-clock timings, deliberately small: the shape of the sequence is what
# matters, not the production hold times. Windows timer jitter is ~15 ms, so
# every bound below is generous in the direction that could make it flaky.
SHORT_HOLD = 0.06


@pytest.fixture
def tapper():
    """A private tapper -- never the shared singleton, and never leaked."""
    platform_shim.reset_recorded_calls()
    instance = KeyTapper(name='key-tapper-test')
    yield instance
    instance.release_all()
    platform_shim.reset_recorded_calls()


def injections(*paths):
    """Recorded pyautogui calls as ``(call, first argument)`` pairs."""
    wanted = paths or ('pyautogui.keyDown', 'pyautogui.keyUp',
                       'pyautogui.mouseDown', 'pyautogui.mouseUp')
    return [(path.split('.')[-1], args[0] if args else kwargs.get('button'))
            for path, args, kwargs in platform_shim.recorded_calls()
            if path in wanted]


class Hardware:
    """Stand-in for ``PhysicalKeyState``: is the driver holding the key?"""

    def __init__(self, down=False, raises=False):
        self.down = down
        self.raises = raises

    def physically_down(self, key):
        if self.raises:
            raise RuntimeError("hook is not running")
        return self.down


# ─── The whole point: tap() does not wait ────────────────────────────────────

def test_tap_returns_without_waiting_for_the_hold(tapper):
    """The caller is an assistance cycle with a 100 ms budget.

    A 0.5 s hold must cost it nothing measurable. The bound is 20 ms rather
    than something tighter because the very first tap also starts the thread.
    """
    started = time.monotonic()
    assert tapper.tap('q', hold_s=0.5) is True
    elapsed = time.monotonic() - started

    assert elapsed < 0.02, f"tap() blocked for {elapsed * 1000:.1f} ms"


def test_the_key_goes_down_and_comes_back_up(tapper):
    tapper.tap('q', hold_s=SHORT_HOLD)

    assert tapper.wait_idle(timeout=3.0)
    assert injections() == [('keyDown', 'q'), ('keyUp', 'q')]
    assert tapper.holding() == []


def test_the_key_stays_down_for_the_requested_hold(tapper):
    """The hold is the reason this module exists -- LFS polls once per frame."""
    started = time.monotonic()
    tapper.tap('q', hold_s=0.15)
    assert tapper.wait_idle(timeout=3.0)
    released_after = time.monotonic() - started

    # Lower bound only: the scheduler may be late, it may never be early.
    assert released_after >= 0.14, f"released after {released_after * 1000:.1f} ms"


# ─── Sequences ───────────────────────────────────────────────────────────────

def test_delay_builds_the_gearbox_shift_sequence(tapper):
    """Clutch first, gear inside it, clutch last -- ``Gearbox._execute_shift``.

    Same shape as production (0.10 / 0.10 / 0.30), scaled down to keep the test
    quick. The gaps are several times the timer jitter, so the order is stable.
    """
    tapper.tap('c', hold_s=0.18)                    # clutch, over the whole shift
    tapper.tap('s', hold_s=0.06, delay_s=0.06)      # gear, inside the clutch

    assert tapper.wait_idle(timeout=3.0)
    assert injections() == [('keyDown', 'c'), ('keyDown', 's'),
                            ('keyUp', 's'), ('keyUp', 'c')]


def test_a_second_tap_extends_the_hold_instead_of_repeating_the_key(tapper):
    """Re-pressing a key LFS already sees as down would look like a key repeat.

    The second tap must therefore only move the release out, and the release
    must not happen when the *first* hold would have expired.
    """
    started = time.monotonic()
    tapper.tap('q', hold_s=0.06)
    time.sleep(0.02)
    tapper.tap('q', hold_s=0.15)

    assert tapper.wait_idle(timeout=3.0)
    assert injections() == [('keyDown', 'q'), ('keyUp', 'q')]
    # 0.02 + 0.15 = 0.17 s; the first tap alone would have released at 0.06 s.
    assert time.monotonic() - started >= 0.16


# ─── Key names ───────────────────────────────────────────────────────────────

def test_a_stored_key_name_reaches_pyautogui_in_pyautogui_spelling(tapper):
    """``page_up`` is what settings.json holds; pyautogui only knows ``pageup``.

    The old call sites handed the stored name straight to pyautogui, so a
    handbrake bound to Page Up simply did nothing.
    """
    tapper.tap('page_up', hold_s=SHORT_HOLD)

    assert tapper.wait_idle(timeout=3.0)
    assert injections() == [('keyDown', 'pageup'), ('keyUp', 'pageup')]


@pytest.mark.parametrize("key", ['f13', 'not a key', '', None, 123])
def test_an_unusable_key_name_is_refused_rather_than_injected(tapper, key):
    """The caller has to learn that no keystroke happened -- like a guard refusal."""
    assert tapper.tap(key, hold_s=SHORT_HOLD) is False

    assert tapper.wait_idle(timeout=1.0)
    assert injections() == []


def test_a_mouse_button_is_pressed_as_a_mouse_button(tapper):
    """LFS accepts mouse buttons as controls; pyautogui has no key called 'mousel'."""
    tapper.tap('mousel', hold_s=SHORT_HOLD)

    assert tapper.wait_idle(timeout=3.0)
    assert injections() == [('mouseDown', 'left'), ('mouseUp', 'left')]


# ─── Fail-safe ───────────────────────────────────────────────────────────────

def test_release_all_drops_a_key_that_is_still_held(tapper):
    """A clutch left down would outlive the process -- control-intervention §1."""
    tapper.tap('c', hold_s=5.0)
    _wait_until(lambda: tapper.holding() == ['c'])

    tapper.release_all()

    assert injections() == [('keyDown', 'c'), ('keyUp', 'c')]
    assert tapper.pending() == 0
    assert tapper.holding() == []


def test_release_all_is_safe_to_call_twice(tapper):
    """It runs from the shutdown path, which several systems reach."""
    tapper.tap('c', hold_s=5.0)
    _wait_until(lambda: tapper.holding() == ['c'])

    tapper.release_all()
    tapper.release_all()

    assert injections() == [('keyDown', 'c'), ('keyUp', 'c')]


def test_the_tapper_works_again_after_release_all(tapper):
    """Shutdown is not the only caller: a second run must still press keys."""
    tapper.tap('q', hold_s=5.0)
    _wait_until(lambda: tapper.holding() == ['q'])
    tapper.release_all()
    platform_shim.reset_recorded_calls()

    assert tapper.tap('q', hold_s=SHORT_HOLD) is True
    assert tapper.wait_idle(timeout=3.0)
    assert injections() == [('keyDown', 'q'), ('keyUp', 'q')]


# ─── Arbitration against the driver's own hand ───────────────────────────────

def test_a_key_the_driver_is_holding_is_not_released(tapper):
    """Our press has become their press; releasing it takes their input away."""
    held_by_driver = KeyTapper(name='key-tapper-hw', physical=Hardware(down=True))
    try:
        held_by_driver.tap('q', hold_s=SHORT_HOLD)
        assert held_by_driver.wait_idle(timeout=3.0)

        assert injections() == [('keyDown', 'q')]
    finally:
        held_by_driver.release_all()


def test_a_broken_physical_key_state_does_not_strand_the_key(tapper):
    """If we cannot tell, we release: a stuck key is the worse failure."""
    unreadable = KeyTapper(name='key-tapper-hw', physical=Hardware(raises=True))
    try:
        unreadable.tap('q', hold_s=SHORT_HOLD)
        assert unreadable.wait_idle(timeout=3.0)

        assert injections() == [('keyDown', 'q'), ('keyUp', 'q')]
    finally:
        unreadable.release_all()


def _wait_until(predicate, timeout: float = 2.0):
    """Poll *predicate* -- the press happens on the tapper's thread, not here."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return
        time.sleep(0.005)
    raise AssertionError("the tapper never reached the expected state")
