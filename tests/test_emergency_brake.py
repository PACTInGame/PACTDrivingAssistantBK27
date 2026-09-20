"""WP11 -- automatic emergency braking: the key spellings, the hardware key
state, the brake output and the system that owns it.

Everything here runs without LFS, without Windows and -- the point of the
fakes below -- **without installing a global input hook**. A real
``PhysicalKeyState.start()`` puts two ``pynput`` listeners on the whole
machine; a test suite must never do that, so the two filter callbacks are
called directly and everything above them gets a stand-in with the same query
surface.

The key injection is not observed through ``platform_shim.recorded_calls()``
here (as ``test_actuation.py`` does) but through a recorder patched over
``get_keyboard``: the brake output has to be asserted on Windows too, where the
real ``pyautogui`` exists and nothing would be recorded.

The behaviour pinned for ``Controls/brake_key.py`` is
``reference/control-intervention.md`` §3.1 -- above all the **key-release
trap**: our ``keyUp`` and the driver's are the same event to LFS, so a release
issued while the driver is physically holding the key takes away braking they
commanded.
"""

import contextlib

import pytest

from Controls.brake_key import KeyBrakeOutput
from assistance.emergency_brake import EmergencyBrake
from misc.key_names import (is_mouse_button, lfs_name_for, spelling_for,
                            vk_for)
from misc.physical_keys import (LLKHF_INJECTED, LLMHF_INJECTED, WM_KEYDOWN,
                                WM_KEYUP, WM_SYSKEYDOWN, WM_SYSKEYUP,
                                PhysicalKeyState)

from conftest import FakePacket


# ─── Fakes ───────────────────────────────────────────────────────────────────

class FakeClock:
    """A monotonic clock a test drives by hand."""

    def __init__(self, now: float = 1000.0):
        self.now = now

    def __call__(self) -> float:
        return self.now

    def advance(self, seconds: float):
        self.now += seconds
        return self.now


class FakePhysicalKeys:
    """``PhysicalKeyState``'s query surface, without a hook.

    Keyed on the virtual key code exactly like the real one, so ``'B'`` and
    ``'b'`` are the same key here too. The test drives it through the
    ``driver_*`` helpers, the injector through :class:`FakeKeyboard` below.
    """

    def __init__(self, running: bool = True):
        self.running = running
        self._physical = {}
        self._for_lfs = {}
        self.forgotten = []

    # --- the surface Controls/brake_key.py uses ------------------------
    def is_running(self) -> bool:
        return self.running

    def physically_down(self, stored_name) -> bool:
        vk = vk_for(stored_name)
        return False if vk is None else self._physical.get(vk, False)

    def down_for_lfs(self, stored_name) -> bool:
        vk = vk_for(stored_name)
        return False if vk is None else self._for_lfs.get(vk, False)

    def forget(self, stored_name):
        self.forgotten.append(stored_name)
        vk = vk_for(stored_name)
        if vk is not None:
            self._physical.pop(vk, None)
            self._for_lfs.pop(vk, None)

    # --- driven by the test -------------------------------------------
    def driver_presses(self, stored_name):
        """Genuine hardware press: both states go down."""
        vk = vk_for(stored_name)
        self._physical[vk] = True
        self._for_lfs[vk] = True

    def driver_releases(self, stored_name):
        """Genuine hardware release: both states go up."""
        vk = vk_for(stored_name)
        self._physical[vk] = False
        self._for_lfs[vk] = False

    # --- driven by the injector ---------------------------------------
    def injected(self, stored_name, pressed: bool):
        """What the hook would see for one of our own events: LFS only."""
        vk = vk_for(stored_name)
        if vk is not None:
            self._for_lfs[vk] = pressed


class FakeKeyboard:
    """Recording stand-in for ``pyautogui``.

    Optionally wired to a :class:`FakePhysicalKeys`, because that is what
    really happens on Windows: our injected event comes back through the same
    low-level hook and updates ``down_for_lfs`` (but never
    ``physically_down``). Without that feedback the arbitration in
    ``KeyBrakeOutput.apply`` would re-press on every cycle.
    """

    _MOUSE_KEY = {'left': 'mousel', 'right': 'mouser', 'middle': 'mousem'}

    def __init__(self, hook: FakePhysicalKeys = None):
        self.calls = []
        self._hook = hook

    def _record(self, name, key, pressed):
        self.calls.append((name, key))
        if self._hook is not None:
            self._hook.injected(key, pressed)

    def keyDown(self, key):
        self._record('keyDown', key, True)

    def keyUp(self, key):
        self._record('keyUp', key, False)

    def mouseDown(self, button=None):
        self._record('mouseDown', self._MOUSE_KEY.get(button, button), True)

    def mouseUp(self, button=None):
        self._record('mouseUp', self._MOUSE_KEY.get(button, button), False)


class FakeGuard:
    """``InputGuard.may_inject``: ``None`` when allowed, else the reason."""

    def __init__(self, refusal=None, outgauge=None):
        self.refusal = refusal
        # ``InputGuard.outgauge_reason``: ``None`` while the stream is alive.
        self.outgauge = outgauge
        self.asked = 0

    def may_inject(self, own_vehicle):
        self.asked += 1
        return self.refusal

    def outgauge_reason(self):
        return self.outgauge


def key_event(vk: int, flags: int = 0x00) -> FakePacket:
    """A ``KBDLLHOOKSTRUCT`` as the low-level keyboard hook delivers it."""
    return FakePacket(vkCode=vk, scanCode=0, flags=flags, time=0, dwExtraInfo=0)


def mouse_event(flags: int = 0x00) -> FakePacket:
    """An ``MSLLHOOKSTRUCT`` as the low-level mouse hook delivers it."""
    return FakePacket(flags=flags, mouseData=0, time=0, dwExtraInfo=0)


# ─── misc/key_names.py ───────────────────────────────────────────────────────

@pytest.mark.parametrize("stored, vk, lfs, pyautogui", [
    ('b',         0x42, 'B',      'b'),
    ('s',         0x53, 'S',      's'),
    ('z',         0x5A, 'Z',      'z'),
    ('0',         0x30, '0',      '0'),
    ('5',         0x35, '5',      '5'),
    ('space',     0x20, 'space',  'space'),
    ('up',        0x26, 'up',     'up'),
    ('down',      0x28, 'down',   'down'),
    ('left',      0x25, 'left',   'left'),
    ('right',     0x27, 'right',  'right'),
    ('page_up',   0x21, 'pgup',   'pageup'),
    ('page_down', 0x22, 'pgdn',   'pagedown'),
])
def test_every_bindable_key_has_the_right_spelling_in_all_three_worlds(
        stored, vk, lfs, pyautogui):
    """One key, three names -- ``page_up`` / ``pgup`` / ``pageup`` is the trap."""
    spelling = spelling_for(stored)

    assert spelling is not None, f"{stored!r} is not in the table"
    assert (spelling.vk, spelling.lfs, spelling.pyautogui) == (vk, lfs, pyautogui)
    assert vk_for(stored) == vk
    assert lfs_name_for(stored) == lfs
    assert is_mouse_button(stored) is False


@pytest.mark.parametrize("stored, vk, lfs", [
    ('mousel', 0x01, 'mousel'),
    ('mouser', 0x02, 'mouser'),
    ('mousem', 0x04, 'mousem'),
])
def test_a_mouse_button_is_a_key_to_lfs_but_not_to_pyautogui(stored, vk, lfs):
    """LFS binds mouse buttons with ``/key``; pyautogui needs ``mouseDown``."""
    spelling = spelling_for(stored)

    assert is_mouse_button(stored) is True
    assert (spelling.vk, spelling.lfs) == (vk, lfs)
    assert spelling.pyautogui is None


@pytest.mark.parametrize("stored", ['enter', 'esc', 'tab', 'f1', 'ctrl', ''])
def test_a_key_lfs_cannot_bind_is_reported_as_unknown_instead_of_raising(stored):
    """``/key`` takes A-Z, 0-9 and a short list of named keys -- nothing else."""
    assert spelling_for(stored) is None
    assert vk_for(stored) is None
    assert lfs_name_for(stored) is None
    assert is_mouse_button(stored) is False


@pytest.mark.parametrize("value", [None, 42, 3.5, True, ['b'], {'key': 'b'}, b'b'])
def test_a_non_string_from_a_hand_edited_settings_file_is_not_an_exception(value):
    """The name comes out of ``settings.json``; a user may have broken it."""
    assert spelling_for(value) is None
    assert vk_for(value) is None
    assert lfs_name_for(value) is None
    assert is_mouse_button(value) is False


@pytest.mark.parametrize("written, canonical", [
    ('B', 'b'),
    ('PAGE_UP', 'page_up'),
    ('  Down  ', 'down'),
    ('MouseL', 'mousel'),
])
def test_a_differently_cased_or_padded_name_finds_the_same_key(written, canonical):
    assert spelling_for(written) == spelling_for(canonical)


# ─── misc/physical_keys.py -- the two hook filters ───────────────────────────
#
# Called directly. ``start()`` is never invoked anywhere in this module: it
# installs a machine-wide pynput hook, which a test suite must not do.

@pytest.fixture
def hooked() -> PhysicalKeyState:
    """A real ``PhysicalKeyState`` with no listeners installed."""
    return PhysicalKeyState()


@pytest.mark.parametrize("message", [WM_KEYDOWN, WM_SYSKEYDOWN])
def test_an_injected_keydown_is_seen_by_lfs_but_is_not_the_driver(hooked, message):
    """LLKHF_INJECTED (0x10) is the whole reason this module exists."""
    hooked._keyboard_filter(message, key_event(vk_for('b'), flags=LLKHF_INJECTED))

    assert hooked.down_for_lfs('b') is True
    assert hooked.physically_down('b') is False


@pytest.mark.parametrize("message", [WM_KEYDOWN, WM_SYSKEYDOWN])
def test_a_physical_keydown_sets_both_states(hooked, message):
    hooked._keyboard_filter(message, key_event(vk_for('b'), flags=0x00))

    assert hooked.down_for_lfs('b') is True
    assert hooked.physically_down('b') is True


@pytest.mark.parametrize("message", [WM_KEYUP, WM_SYSKEYUP])
def test_an_injected_keyup_clears_only_what_lfs_believes(hooked, message):
    hooked._keyboard_filter(WM_KEYDOWN, key_event(vk_for('b'), flags=0x00))
    hooked._keyboard_filter(message, key_event(vk_for('b'), flags=0x90))

    assert hooked.down_for_lfs('b') is False
    assert hooked.physically_down('b') is True     # still on the keyboard


def test_a_physical_keyup_after_an_injected_keydown_clears_both(hooked):
    """The driver taps the key we are holding: LFS and the hardware agree again."""
    hooked._keyboard_filter(WM_KEYDOWN, key_event(vk_for('b'), flags=LLKHF_INJECTED))
    hooked._keyboard_filter(WM_KEYUP, key_event(vk_for('b'), flags=0x80))

    assert hooked.down_for_lfs('b') is False
    assert hooked.physically_down('b') is False


def test_a_key_nobody_touched_is_down_nowhere(hooked):
    assert hooked.down_for_lfs('b') is False
    assert hooked.physically_down('b') is False
    assert hooked.down_for_lfs('enter') is False   # not even a known key


@pytest.mark.parametrize("down_msg, up_msg, stored", [
    (0x0201, 0x0202, 'mousel'),
    (0x0204, 0x0205, 'mouser'),
    (0x0207, 0x0208, 'mousem'),
])
def test_the_mouse_filter_separates_injection_the_same_way(
        hooked, down_msg, up_msg, stored):
    """A mouse driver brakes with ``mousel``; LLMHF_INJECTED is bit 0, not 4."""
    hooked._mouse_filter(down_msg, mouse_event(flags=LLMHF_INJECTED))
    assert (hooked.down_for_lfs(stored), hooked.physically_down(stored)) == (True, False)

    hooked._mouse_filter(down_msg, mouse_event(flags=0x00))
    assert (hooked.down_for_lfs(stored), hooked.physically_down(stored)) == (True, True)

    hooked._mouse_filter(up_msg, mouse_event(flags=0x00))
    assert (hooked.down_for_lfs(stored), hooked.physically_down(stored)) == (False, False)


def test_a_mouse_move_changes_nothing(hooked):
    """Only the button messages are in the table; WM_MOUSEMOVE must be ignored."""
    hooked._mouse_filter(0x0201, mouse_event())
    hooked._mouse_filter(0x0200, mouse_event())          # WM_MOUSEMOVE

    assert hooked.down_for_lfs('mousel') is True


def test_forgetting_a_key_drops_both_of_its_states(hooked):
    """After a rebind the old key's state must not decide anything."""
    hooked._keyboard_filter(WM_KEYDOWN, key_event(vk_for('b'), flags=0x00))
    hooked._keyboard_filter(WM_KEYDOWN, key_event(vk_for('k'), flags=0x00))

    hooked.forget('b')

    assert hooked.down_for_lfs('b') is False
    assert hooked.physically_down('b') is False
    assert hooked.down_for_lfs('k') is True       # the other key is untouched


def test_the_filters_never_suppress_the_event_for_other_applications(hooked):
    """``False`` skips pynput's own decoding; it does not swallow the key."""
    assert hooked._keyboard_filter(WM_KEYDOWN, key_event(vk_for('b'))) is False
    assert hooked._mouse_filter(0x0201, mouse_event()) is False


def test_hooks_that_were_never_started_do_not_claim_to_be_running(hooked):
    """``is_running`` gates every actuation -- it must not be optimistic."""
    assert hooked.is_running() is False


# ─── Controls/brake_key.py ───────────────────────────────────────────────────

@pytest.fixture
def physical() -> FakePhysicalKeys:
    return FakePhysicalKeys(running=True)


@pytest.fixture
def keyboard(physical, monkeypatch) -> FakeKeyboard:
    """A recording keyboard, patched over every name the injection reaches it by.

    ``KeyBrakeOutput`` injects through ``platform_shim.instant_input()`` -- the
    context manager that turns ``pyautogui.PAUSE`` off for the call -- so that
    is what has to be replaced, not ``get_keyboard``.
    """
    recorder = FakeKeyboard(hook=physical)
    monkeypatch.setattr('misc.platform_shim.get_keyboard', lambda: recorder)
    monkeypatch.setattr('Controls.brake_key.instant_input',
                        lambda: contextlib.nullcontext(recorder))
    # The throttle cut un-presses the driver's input the same way
    # (``Controls/throttle_cut.py``), so its door has to be replaced as well.
    monkeypatch.setattr('Controls.throttle_cut.instant_input',
                        lambda: contextlib.nullcontext(recorder))
    # Off Windows there is no pyautogui at all, and unavailable_reason() would
    # stop at the first line. The rest of the table is what these tests are about.
    monkeypatch.setattr('Controls.brake_key.is_available',
                        lambda name: True if name == 'pyautogui' else False)
    return recorder


@pytest.fixture
def brake_output(bus, make_settings, physical, keyboard) -> KeyBrakeOutput:
    """A brake output with its binding already pushed, on key ``b``."""
    settings = make_settings(user_brake_key='b', language='en')
    output = KeyBrakeOutput(bus, settings, physical)
    output.push_binding()
    return output


def test_wanting_the_brake_presses_the_key_lfs_does_not_have_down(
        brake_output, keyboard):
    assert brake_output.apply(True) is True

    assert keyboard.calls == [('keyDown', 'b')]
    assert brake_output.holds_press() is True


def test_wanting_the_brake_presses_nothing_while_the_driver_is_already_braking(
        brake_output, keyboard, physical):
    """LFS already sees the key down -- adding a press adds no braking."""
    physical.driver_presses('b')

    assert brake_output.apply(True) is True

    assert keyboard.calls == []
    assert brake_output.holds_press() is False   # nothing of ours to release


def test_holding_the_press_is_idempotent_across_cycles(brake_output, keyboard):
    """The output is touched when the demand changes, not once per cycle."""
    for _ in range(5):
        brake_output.apply(True)

    assert keyboard.calls == [('keyDown', 'b')]


def test_our_release_is_suppressed_while_the_driver_holds_the_key(
        brake_output, keyboard, physical):
    """The key-release trap (control-intervention.md §3.1).

    We press, then the driver puts their own finger on the same key. Our
    ``keyUp`` is indistinguishable from theirs, so sending it would tell LFS
    the brake is released while the driver is still commanding it.
    """
    brake_output.apply(True)
    physical.driver_presses('b')
    keyboard.calls.clear()

    assert brake_output.apply(False) is False

    assert keyboard.calls == []
    assert brake_output.holds_press() is False   # we are no longer the holder
    assert physical.down_for_lfs('b') is True    # and LFS still brakes


def test_our_release_is_sent_when_the_driver_is_not_holding_the_key(
        brake_output, keyboard):
    brake_output.apply(True)
    keyboard.calls.clear()

    assert brake_output.apply(False) is False

    assert keyboard.calls == [('keyUp', 'b')]
    assert brake_output.holds_press() is False


def test_the_key_is_pressed_again_when_the_driver_lets_go_mid_intervention(
        brake_output, keyboard, physical):
    """The inverse trap: LFS gets the genuine keyup, so we must re-press.

    Continuing to brake is correct for an emergency stop; the gap is at most
    one assistance cycle (~100 ms).
    """
    physical.driver_presses('b')
    brake_output.apply(True)                     # nothing pressed, LFS has it
    assert keyboard.calls == []

    physical.driver_releases('b')                # the driver lifts off
    brake_output.apply(True)

    assert keyboard.calls == [('keyDown', 'b')]
    assert brake_output.holds_press() is True


def test_releasing_without_ever_having_pressed_does_nothing(brake_output, keyboard):
    brake_output.release()
    brake_output.release()

    assert keyboard.calls == []


def test_releasing_twice_sends_one_keyup(brake_output, keyboard):
    brake_output.apply(True)
    keyboard.calls.clear()

    brake_output.release()
    brake_output.release()

    assert keyboard.calls == [('keyUp', 'b')]


def test_a_mouse_driver_is_braked_with_the_mouse_button(
        bus, make_settings, physical, keyboard):
    settings = make_settings(user_brake_key='mousel', language='en')
    output = KeyBrakeOutput(bus, settings, physical)
    output.push_binding()

    output.apply(True)
    output.apply(False)

    assert keyboard.calls == [('mouseDown', 'mousel'), ('mouseUp', 'mousel')]


# --- unavailable_reason: one distinct reason per cause -----------------------

def test_everything_in_place_is_the_only_case_without_a_reason(brake_output):
    assert brake_output.unavailable_reason() is None


def test_a_missing_pyautogui_is_reported_before_anything_else(
        brake_output, monkeypatch):
    monkeypatch.setattr('Controls.brake_key.is_available', lambda name: False)

    assert brake_output.unavailable_reason() == 'pyautogui_missing'


def test_without_hardware_key_tracking_the_output_refuses_to_arm(
        brake_output, physical):
    """No physical state means the key-release trap is unguarded."""
    physical.running = False

    assert brake_output.unavailable_reason() == 'no_physical_key_tracking'


def test_a_key_lfs_cannot_bind_refuses_to_arm(brake_output):
    brake_output.settings.set('user_brake_key', 'enter')

    assert brake_output.unavailable_reason() == 'key_not_bindable_in_lfs'


def test_an_unpushed_binding_refuses_to_arm(bus, make_settings, physical, keyboard):
    """Injecting into an LFS whose bindings we never wrote is a coin flip."""
    output = KeyBrakeOutput(bus, make_settings(user_brake_key='b'), physical)

    assert output.unavailable_reason() == 'binding_not_pushed'


def test_a_rebind_unarms_the_output_until_the_new_binding_is_pushed(brake_output):
    brake_output.settings.set('user_brake_key', 'k')

    assert brake_output.unavailable_reason() == 'binding_not_pushed'


def test_a_lost_connection_unarms_the_output(brake_output):
    brake_output.binding_lost()

    assert brake_output.unavailable_reason() == 'binding_not_pushed'


# --- push_binding ------------------------------------------------------------

def test_pushing_the_binding_sends_exactly_one_key_command(
        bus, make_settings, physical, keyboard, recorder):
    seen = recorder('send_command_to_lfs')
    output = KeyBrakeOutput(bus, make_settings(user_brake_key='b'), physical)

    assert output.push_binding() is True

    assert seen.payloads('send_command_to_lfs') == ['/key B brake']


def test_pushing_a_binding_lfs_cannot_accept_sends_nothing(
        bus, make_settings, physical, keyboard, recorder):
    """``/key`` has no name for Enter, so there is nothing truthful to send."""
    seen = recorder('send_command_to_lfs')
    output = KeyBrakeOutput(bus, make_settings(user_brake_key='enter'), physical)

    assert output.push_binding() is False

    assert seen.count('send_command_to_lfs') == 0
    assert output.unavailable_reason() == 'key_not_bindable_in_lfs'


def test_a_rebind_forgets_the_old_keys_tracked_state(
        brake_output, physical, recorder):
    """A stale "physically down" on the old key would suppress our release."""
    seen = recorder('send_command_to_lfs')
    brake_output.settings.set('user_brake_key', 'k')

    assert brake_output.push_binding() is True

    assert physical.forgotten == ['b']
    assert seen.payloads('send_command_to_lfs') == ['/key K brake']


def test_pushing_the_same_binding_again_forgets_nothing(brake_output, physical):
    """A reconnect re-pushes the binding; that is not a rebind."""
    brake_output.push_binding()

    assert physical.forgotten == []


# ─── assistance/emergency_brake.py ───────────────────────────────────────────

DECELERATION_EVENT = 'needed_deceleration_update'


@pytest.fixture
def aeb_factory(bus, make_settings, physical, keyboard):
    """An ``EmergencyBrake`` on injected hooks, guard and clock.

    ``physical_keys`` is passed in, so the system does not own the hooks and
    ``start()`` / ``shutdown()`` never touch a real listener.

    The binding is pushed by hand because ``process()`` cannot get there on its
    own -- see ``test_the_first_cycle_pushes_the_binding_so_the_system_can_arm``.
    """
    def _make(mode: int = 2, refusal=None, clock=None, push=True,
              outgauge=None, **overrides):
        settings = make_settings(automatic_emergency_brake=mode,
                                 user_brake_key='b', language='en', **overrides)
        guard = FakeGuard(refusal, outgauge)
        system = EmergencyBrake(bus, settings, physical_keys=physical,
                                guard=guard, clock=clock or FakeClock())
        if push:
            system.key_output.push_binding()
        return system

    return _make


@pytest.fixture
def braking_car(make_own_vehicle):
    """A mouse driver at 80 km/h -- fast enough for AEB to be relevant."""
    return make_own_vehicle(speed=80, local_plid=1, plid=1, control_mode=0)


def demand(bus, deceleration: float, source: str = 'forward_collision'):
    """What a warning system publishes every cycle.

    ``EmergencyBrake`` collects the demands of one assistance pass and clears
    the collection when it consumes them, so a *missing* demand is not the
    same as a demand of zero (reference/events.md). A test that wants the
    brake to stay on for several passes therefore has to publish for each of
    them, exactly as the real systems do.
    """
    bus.emit(DECELERATION_EVENT,
             {'deceleration': deceleration, 'source': source})


def test_a_silent_outgauge_stream_refuses_to_arm(
        bus, aeb_factory, braking_car, keyboard, recorder):
    """The whole of known-issues #51, in one pass.

    Port 30000 was held by a leftover process, so OutGauge never bound.
    Everything else looked healthy -- the binding was pushed, the hooks were
    up, the log said "Automatic emergency braking is armed." -- and not one
    intervention happened all evening, because ``is_local_driver`` needs
    ``viewed_plid`` and ``viewed_plid`` comes from OutGauge.
    """
    events = recorder('emergency_brake_availability')
    system = aeb_factory(outgauge='port_in_use')
    demand(bus, 9.0)

    result = system.process(braking_car, {})

    assert result['active'] is False
    assert result['refused'] == 'no_outgauge'
    assert keyboard.calls == []
    assert events.last('emergency_brake_availability')['reason']         == 'no_outgauge:port_in_use'


def test_the_stream_coming_back_arms_the_system(
        bus, aeb_factory, braking_car, keyboard):
    """And it has to recover by itself, without a restart."""
    system = aeb_factory(outgauge='no_packets')
    demand(bus, 9.0)
    system.process(braking_car, {})

    system.guard.outgauge = None
    demand(bus, 9.0)
    result = system.process(braking_car, {})

    assert result['active'] is True


def test_mode_zero_does_not_arm_at_all(bus, aeb_factory, braking_car, keyboard):
    system = aeb_factory(mode=0)
    demand(bus, 9.0)

    result = system.process(braking_car, {})

    assert result == {'active': False}
    assert keyboard.calls == []


def test_mode_one_warns_but_never_brakes(bus, aeb_factory, braking_car, keyboard):
    """Mode 1 is FCW's warning alone; taking control needs mode 2."""
    system = aeb_factory(mode=1)
    demand(bus, 9.0)

    result = system.process(braking_car, {})

    assert result == {'active': False}
    assert keyboard.calls == []
    assert system.is_enabled() is False


def test_a_wheel_driver_never_gets_the_key_path(
        bus, aeb_factory, make_own_vehicle, keyboard, recorder):
    """``wheel_js`` ignores keys for brake entirely (control-intervention §2.1).

    An uncalibrated wheel driver must be told once and then left alone --
    silence here is the old bug, and a keystroke here would be the new one.
    """
    seen = recorder('notification')
    system = aeb_factory()
    system.settings.set('vjoy_brake_calibrated', False)
    wheel_driver = make_own_vehicle(speed=80, local_plid=1, plid=1, control_mode=2)
    demand(bus, 9.0)

    for _ in range(10):
        assert system.process(wheel_driver, {}) == {'active': False}

    assert keyboard.calls == []
    assert seen.count('notification') == 1
    assert 'vjoy_not_calibrated' in seen.last('notification')['notification']


# ─── Arbitration: never less braking than the driver commands ────────────────

class FakePedals:
    """Stands in for ``PedalWatch``: the driver's pedal, read at the device."""

    def __init__(self, brake=None):
        self.brake = brake
        self.started = False
        self.samples = []

    def driver_brake(self):
        return self.brake

    def driver_throttle(self):
        return None

    def unavailable_reason(self):
        return None if self.brake is not None else 'not identified yet'

    def confidence(self, _which):
        return 0.0

    def request_start(self):
        self.started = True

    def stop(self):
        pass

    def observe(self, own_vehicle, trustworthy=True):
        self.samples.append(trustworthy)


@pytest.fixture
def wheel_aeb(bus, make_settings, physical, keyboard):
    """An AEB whose output is the analog path, with a fake vJoy underneath."""
    def _make(pedals, **overrides):
        settings = make_settings(automatic_emergency_brake=2, language='en',
                                 vjoy_brake_calibrated=True, vjoy_axis_1=15,
                                 user_axis_brake=12, **overrides)
        system = EmergencyBrake(bus, settings, physical_keys=physical,
                                guard=FakeGuard(None), clock=FakeClock(),
                                pedals=pedals)
        system.axis_output.device = _AlwaysThereDevice()
        system.axis_output.start_guardian = lambda: None
        return system

    return _make


class _AlwaysThereDevice:
    def __init__(self):
        self.raw = None

    def prepare(self):
        # Nothing to load: the real device warms its DLL on a thread.
        return True

    def loading(self):
        return False

    def unavailable_reason(self):
        return None

    def acquire(self):
        return True

    def set_raw(self, value):
        self.raw = value

    def relinquish(self):
        pass


def wheel_at(make_own_vehicle, speed=80.0, **kwargs):
    return make_own_vehicle(speed=speed, local_plid=1, plid=1, control_mode=2,
                            **kwargs)


def test_the_axis_path_never_commands_less_than_the_driver_is_braking(
        bus, wheel_aeb, make_own_vehicle):
    """The regression this exists for.

    While the vJoy axis carries ``brake``, LFS has stopped reading the driver's
    pedal, so a modulated 50 % really is 50 % even with their foot on the
    floor. Anything that reduces braking is the exact inversion of what an
    assistant may do (control-intervention.md section 1).
    """
    pedals = FakePedals(brake=1.0)
    system = wheel_aeb(pedals)
    demand(bus, 6.5)                       # would modulate to well under 1.0

    result = system.process(wheel_at(make_own_vehicle), {})

    assert result['active'] is True
    assert result['brake'] == pytest.approx(1.0)


def test_the_axis_path_still_modulates_above_the_drivers_pedal(
        bus, wheel_aeb, make_own_vehicle):
    """Arbitration is a floor, not a replacement: a driver barely on the pedal
    must not stop the assistant from braking properly."""
    pedals = FakePedals(brake=0.1)
    system = wheel_aeb(pedals)
    demand(bus, 6.5)

    result = system.process(wheel_at(make_own_vehicle), {})

    assert 0.1 < result['brake'] < 1.0


def test_an_unreadable_pedal_makes_the_axis_path_brake_fully(
        bus, wheel_aeb, make_own_vehicle):
    """No joystick, no pygame, or the pedal not identified yet: we cannot tell
    whether we would be braking less than the driver, and the only value that
    is certainly not less is all of it."""
    system = wheel_aeb(FakePedals(brake=None))
    demand(bus, 6.5)

    result = system.process(wheel_at(make_own_vehicle), {})

    assert result['brake'] == pytest.approx(1.0)


def test_the_key_path_does_not_arbitrate(bus, aeb_factory, braking_car):
    """LFS merges our keystroke with the driver's own input, so the harder one
    already wins -- and a key has no travel to arbitrate with anyway."""
    system = aeb_factory()
    demand(bus, 6.5)

    result = system.process(braking_car, {})

    assert result['brake'] < 1.0


def test_pedal_samples_stop_while_we_hold_the_brake_axis(
        bus, wheel_aeb, make_own_vehicle):
    """OutGauge reports our own command back during an intervention; learning
    from it would identify our vJoy axis as the driver's brake pedal."""
    pedals = FakePedals(brake=0.0)
    system = wheel_aeb(pedals)

    demand(bus, 0.0)
    system.process(wheel_at(make_own_vehicle), {})
    demand(bus, 9.0)
    system.process(wheel_at(make_own_vehicle), {})
    system.process(wheel_at(make_own_vehicle), {})

    assert pedals.samples == [True, True, False]


def test_the_pedal_watch_is_started_with_the_axis_path(
        bus, wheel_aeb, make_own_vehicle):
    """It needs a normal braking manoeuvre to identify the pedal, so it has to
    be running well before the first intervention."""
    pedals = FakePedals(brake=0.0)
    system = wheel_aeb(pedals)

    demand(bus, 0.0)
    system.process(wheel_at(make_own_vehicle), {})

    assert pedals.started is True


def test_an_uncalibrated_axis_output_refuses_before_it_touches_vjoy():
    """Polarity is measured, never assumed: raw 0 is full brake on one machine
    and no brake on another, so an unmeasured axis must not be driven."""
    from Controls.brake_axis import AxisBrakeOutput

    class _ExplodingDevice:
        def unavailable_reason(self):
            raise AssertionError("the device must not be consulted at all")

    settings = _Settings({'vjoy_brake_calibrated': False,
                          'vjoy_axis_1': 15, 'user_axis_brake': 12})
    output = AxisBrakeOutput(None, settings, device=_ExplodingDevice())

    assert output.unavailable_reason() == 'vjoy_not_calibrated'


def test_an_axis_output_refuses_when_both_axes_are_the_same_number():
    """Handing back to ourselves is not a handback."""
    from Controls.brake_axis import AxisBrakeOutput

    settings = _Settings({'vjoy_brake_calibrated': True,
                          'vjoy_axis_1': 12, 'user_axis_brake': 12})
    output = AxisBrakeOutput(None, settings, device=None)

    assert output.unavailable_reason() == 'vjoy_and_driver_axis_identical'


class _Settings:
    """Just enough settings object for the two checks above."""

    def __init__(self, values):
        self._values = values

    def get(self, key, default=None):
        return self._values.get(key, default)


def test_the_brake_engages_at_the_deceleration_threshold(
        bus, aeb_factory, braking_car, keyboard, recorder):
    seen = recorder('emergency_brake_changed')
    system = aeb_factory()
    demand(bus, EmergencyBrake.ENGAGE_DECELERATION_MS2)

    result = system.process(braking_car, {})

    assert result['active'] is True
    assert keyboard.calls == [('keyDown', 'b')]
    assert seen.last('emergency_brake_changed') == {
        'active': True, 'source': 'forward_collision'}


def test_a_demand_below_the_threshold_does_not_engage(
        bus, aeb_factory, braking_car, keyboard):
    system = aeb_factory()
    demand(bus, EmergencyBrake.ENGAGE_DECELERATION_MS2 - 0.1)

    assert system.process(braking_car, {})['active'] is False
    assert keyboard.calls == []


def test_one_quiet_cycle_does_not_drop_the_brake_but_two_do(
        bus, aeb_factory, braking_car, keyboard):
    """Release hysteresis: a single noisy cycle mid-intervention is debounced."""
    system = aeb_factory()
    demand(bus, 9.0)
    assert system.process(braking_car, {})['active'] is True

    demand(bus, EmergencyBrake.RELEASE_DECELERATION_MS2 - 0.1)
    assert system.process(braking_car, {})['active'] is True     # debounced
    assert keyboard.calls == [('keyDown', 'b')]

    assert system.process(braking_car, {})['active'] is False    # second cycle
    assert keyboard.calls == [('keyDown', 'b'), ('keyUp', 'b')]


def test_a_demand_between_release_and_engage_keeps_the_brake_on(
        bus, aeb_factory, braking_car):
    """Hysteresis, not a single threshold: 4 m/s² neither engages nor releases."""
    system = aeb_factory()
    demand(bus, 9.0)
    system.process(braking_car, {})

    for _ in range(5):
        demand(bus, 4.0)
        assert system.process(braking_car, {})['active'] is True


def test_a_running_intervention_brakes_all_the_way_to_standstill(
        bus, aeb_factory, make_own_vehicle, keyboard):
    """The regression: FCW goes blind below its own floor, so we must not
    read its silence as "the hazard is gone".

    Below ``MIN_SPEED_KMH`` FCW publishes a demand of 0 whatever is in front
    of the car. An intervention that let go there handed back at walking speed
    a metre short of the obstacle it had just braked for.
    """
    clock = FakeClock()
    system = aeb_factory(clock=clock)
    demand(bus, 9.0)
    system.process(make_own_vehicle(speed=80, local_plid=1, plid=1), {})
    assert system._engaged is True

    # FCW stops publishing: from here the demand says nothing at all.
    demand(bus, 0.0)
    for speed in (9.0, 6.0, 3.0, 1.0):
        crawling = make_own_vehicle(speed=speed, local_plid=1, plid=1)
        assert system.process(crawling, {})['active'] is True, speed
    assert keyboard.calls == [('keyDown', 'b')]


def test_the_brake_is_held_briefly_after_standstill_then_given_back(
        bus, aeb_factory, make_own_vehicle, keyboard):
    """AutoHold needs standstill and brake pressure in the same pass."""
    clock = FakeClock()
    system = aeb_factory(clock=clock)
    demand(bus, 9.0)
    system.process(make_own_vehicle(speed=80, local_plid=1, plid=1), {})
    demand(bus, 0.0)

    stopped = make_own_vehicle(speed=0.0, local_plid=1, plid=1)
    assert system.process(stopped, {})['active'] is True

    clock.advance(EmergencyBrake.STANDSTILL_HOLD_S + 0.1)
    assert system.process(stopped, {})['active'] is False
    assert keyboard.calls == [('keyDown', 'b'), ('keyUp', 'b')]


def test_a_car_settling_on_its_suspension_does_not_restart_the_hold(
        bus, aeb_factory, make_own_vehicle, keyboard):
    """Measured live on 2026-09-20: "standstill reached" was logged twice in the
    same second. The body rocking after a full stop crossed STANDSTILL_KMH
    again, which cleared ``_standstill_since`` and started the 1 s handover
    hold from the beginning -- so the brake stayed in longer than
    STANDSTILL_HOLD_S, in the limit indefinitely.
    """
    clock = FakeClock()
    system = aeb_factory(clock=clock)
    demand(bus, 9.0)
    system.process(make_own_vehicle(speed=80, local_plid=1, plid=1), {})
    demand(bus, 0.0)

    stopped = make_own_vehicle(speed=0.0, local_plid=1, plid=1)
    assert system.process(stopped, {})['active'] is True
    started_at = system._standstill_since

    # Rebound: above the entry threshold, far below the exit one.
    clock.advance(0.3)
    rocking = make_own_vehicle(speed=EmergencyBrake.STANDSTILL_KMH + 0.4,
                               local_plid=1, plid=1)
    assert system.process(rocking, {})['active'] is True
    assert system._standstill_since == started_at      # clock not restarted

    clock.advance(EmergencyBrake.STANDSTILL_HOLD_S - 0.2)
    assert system.process(stopped, {})['active'] is False
    assert keyboard.calls == [('keyDown', 'b'), ('keyUp', 'b')]


def test_a_car_that_really_rolls_away_is_braked_again(
        bus, aeb_factory, make_own_vehicle, keyboard):
    """The other half of the hysteresis: standstill is not latched for good.
    A car rolling off a slope passes STANDSTILL_EXIT_KMH within the hold and
    must get the brake back, not a handover."""
    clock = FakeClock()
    system = aeb_factory(clock=clock)
    demand(bus, 9.0)
    system.process(make_own_vehicle(speed=80, local_plid=1, plid=1), {})
    demand(bus, 0.0)

    stopped = make_own_vehicle(speed=0.0, local_plid=1, plid=1)
    assert system.process(stopped, {})['active'] is True

    clock.advance(0.5)
    rolling = make_own_vehicle(speed=EmergencyBrake.STANDSTILL_EXIT_KMH + 0.5,
                               local_plid=1, plid=1)
    assert system.process(rolling, {})['active'] is True
    assert system._standstill_since is None

    # And the hold starts afresh from the next standstill, not from the old one.
    clock.advance(EmergencyBrake.STANDSTILL_HOLD_S + 0.1)
    assert system.process(stopped, {})['active'] is True


def test_the_driver_takes_the_car_back_with_the_throttle_below_the_floor(
        bus, aeb_factory, make_own_vehicle, keyboard):
    """Brake and throttle used to fight each other for as long as both were on.

    The committed stop deliberately ignores FCW, so nothing else in the loop
    could end it early -- and a driver who saw the hazard clear at 8 km/h and
    accelerated got a car with both pedals applied.
    """
    system = aeb_factory()
    demand(bus, 9.0)
    system.process(make_own_vehicle(speed=80, local_plid=1, plid=1), {})
    demand(bus, 0.0)

    crawling = make_own_vehicle(speed=8.0, local_plid=1, plid=1)
    assert system.process(crawling, {})['active'] is True

    accelerating = make_own_vehicle(speed=8.0, local_plid=1, plid=1,
                                    throttle=0.6)
    assert system.process(accelerating, {})['active'] is False
    assert keyboard.calls == [('keyDown', 'b'), ('keyUp', 'b')]


def _hand_back_on_the_throttle(bus, system, make_own_vehicle, clock):
    """Drive the exact sequence that ended known-issues #49's stutter loop.

    Engage at speed, let the car fall below the commit speed, then have the
    driver press the throttle: the intervention hands the car back.
    """
    demand(bus, 9.0)
    system.process(make_own_vehicle(speed=80, local_plid=1, plid=1), {})
    assert system._engaged is True
    demand(bus, 0.0)
    accelerating = make_own_vehicle(speed=8.0, local_plid=1, plid=1,
                                    throttle=0.6)
    assert system.process(accelerating, {})['active'] is False
    return accelerating


def test_a_handback_on_the_throttle_blocks_the_next_engagement(
        bus, aeb_factory, make_own_vehicle):
    """Eight engagements in 4.5 s -- the other end of known-issues #49.

    The ghost vehicle that produced the demand is gone, but the loop it ran
    around is not: hand back because the driver is on the throttle, let the
    car accelerate past FCW's 10 km/h floor, and the same demand re-engages on
    the very next cycle. Nothing damped that.
    """
    clock = FakeClock()
    system = aeb_factory(clock=clock)
    _hand_back_on_the_throttle(bus, system, make_own_vehicle, clock)

    # The demand comes straight back and the driver is still accelerating.
    demand(bus, 9.0)
    still_on_it = make_own_vehicle(speed=15.0, local_plid=1, plid=1,
                                   throttle=0.6)
    for _ in range(5):
        clock.advance(0.1)
        demand(bus, 9.0)
        assert system.process(still_on_it, {})['active'] is False


def test_the_lockout_ends_when_the_driver_comes_off_the_throttle(
        bus, aeb_factory, make_own_vehicle):
    """The handback is the driver's decision only while they keep making it."""
    clock = FakeClock()
    system = aeb_factory(clock=clock)
    _hand_back_on_the_throttle(bus, system, make_own_vehicle, clock)

    clock.advance(0.1)
    demand(bus, 9.0)
    coasting = make_own_vehicle(speed=15.0, local_plid=1, plid=1, throttle=0.0)
    assert system.process(coasting, {})['active'] is True


def test_the_lockout_expires_so_panic_throttle_cannot_disable_the_aeb(
        bus, aeb_factory, make_own_vehicle):
    """A brake that a held throttle switches off for good is not an AEB."""
    clock = FakeClock()
    system = aeb_factory(clock=clock)
    _hand_back_on_the_throttle(bus, system, make_own_vehicle, clock)

    clock.advance(EmergencyBrake.THROTTLE_HANDBACK_LOCKOUT_S + 0.01)
    demand(bus, 9.0)
    still_on_it = make_own_vehicle(speed=15.0, local_plid=1, plid=1,
                                   throttle=0.6)
    assert system.process(still_on_it, {})['active'] is True


def test_throttle_does_not_abort_the_emergency_itself(
        bus, aeb_factory, make_own_vehicle):
    """Panic throttle at speed is what an AEB exists to override."""
    system = aeb_factory()
    demand(bus, 9.0)
    system.process(make_own_vehicle(speed=80, local_plid=1, plid=1), {})

    flooring_it = make_own_vehicle(speed=75.0, local_plid=1, plid=1,
                                   throttle=1.0)
    assert system.process(flooring_it, {})['active'] is True


def test_a_resting_pedal_does_not_count_as_an_override(
        bus, aeb_factory, make_own_vehicle):
    """A calibrated pedal at rest reports a few per cent, not zero."""
    system = aeb_factory()
    demand(bus, 9.0)
    system.process(make_own_vehicle(speed=80, local_plid=1, plid=1), {})
    demand(bus, 0.0)

    resting = make_own_vehicle(speed=8.0, local_plid=1, plid=1,
                               throttle=EmergencyBrake.THROTTLE_OVERRIDE - 0.05)
    assert system.process(resting, {})['active'] is True


def test_the_committed_stop_keeps_braking_firmly(
        bus, aeb_factory, make_own_vehicle):
    """The demand FCW stops publishing must not become a 20 % pedal.

    ``_brake_fraction`` fed on a demand of 0 fell straight to
    ``MIN_BRAKE_FRACTION``, which is why the car still rolled into the one in
    front over the last few metres.
    """
    system = aeb_factory()
    demand(bus, 9.0)
    system.process(make_own_vehicle(speed=80, local_plid=1, plid=1), {})

    demand(bus, 0.0)
    crawling = make_own_vehicle(speed=8.0, local_plid=1, plid=1,
                                acceleration=-1.0)
    result = system.process(crawling, {})

    assert result['active'] is True
    assert result['brake'] > 0.5


def test_an_intervention_that_clears_well_above_the_floor_still_releases(
        bus, aeb_factory, make_own_vehicle, keyboard):
    """Committing to a stop must not swallow the normal release."""
    system = aeb_factory()
    demand(bus, 9.0)
    system.process(make_own_vehicle(speed=80, local_plid=1, plid=1), {})

    demand(bus, 0.0)
    rolling = make_own_vehicle(speed=60.0, local_plid=1, plid=1)
    for _ in range(EmergencyBrake.RELEASE_DEBOUNCE_CYCLES):
        system.process(rolling, {})
    assert system.process(rolling, {})['active'] is False


def test_a_parking_manoeuvre_is_below_the_floor_and_belongs_to_pdc(
        bus, aeb_factory, make_own_vehicle, keyboard):
    system = aeb_factory()
    demand(bus, 9.0)
    slow = make_own_vehicle(speed=EmergencyBrake.MIN_SPEED_KMH - 0.5,
                            local_plid=1, plid=1)

    assert system.process(slow, {})['active'] is False
    assert keyboard.calls == []


def test_an_intervention_that_never_ends_is_released_by_the_runaway_guard(
        bus, aeb_factory, braking_car, keyboard):
    """Nothing legitimate needs a ten-second emergency stop."""
    clock = FakeClock()
    system = aeb_factory(clock=clock)
    demand(bus, 9.0)
    assert system.process(braking_car, {})['active'] is True

    clock.advance(EmergencyBrake.MAX_ENGAGE_S + 0.1)

    assert system.process(braking_car, {})['active'] is False
    assert keyboard.calls == [('keyDown', 'b'), ('keyUp', 'b')]


def test_a_refusal_from_the_input_guard_prevents_engagement(
        bus, aeb_factory, braking_car, keyboard):
    """Chat line open, modifier held, LFS in the background, wrong car."""
    system = aeb_factory(refusal='text_entry')
    demand(bus, 9.0)

    result = system.process(braking_car, {})

    assert result == {'active': False, 'refused': 'text_entry'}
    assert keyboard.calls == []


def test_leaving_the_track_releases_a_running_intervention(
        bus, aeb_factory, braking_car, keyboard, recorder):
    """Shift+P mid-intervention: the manager stops calling process(), so the
    state event is the only thing left that can let go."""
    seen = recorder('emergency_brake_changed')
    system = aeb_factory()
    demand(bus, 9.0)
    assert system.process(braking_car, {})['active'] is True

    bus.emit('state_data', {'on_track': False, 'screen': 'entry'})

    assert system._engaged is False
    assert keyboard.calls == [('keyDown', 'b'), ('keyUp', 'b')]
    assert seen.last('emergency_brake_changed') == {
        'active': False, 'source': None}


def test_shutdown_gives_the_brake_back(bus, aeb_factory, braking_car, keyboard):
    """An intervention that outlives the process leaves a pressed brake key."""
    system = aeb_factory()
    demand(bus, 9.0)
    system.process(braking_car, {})

    system.shutdown()

    assert keyboard.calls == [('keyDown', 'b'), ('keyUp', 'b')]
    assert system._engaged is False


def brake_commands(seen):
    """Only the commands about the brake binding.

    An intervention now also pushes the throttle binding and unassigns the
    throttle for its duration, so the raw command stream carries four things
    where it used to carry one.
    """
    return [command for command in seen.payloads('send_command_to_lfs')
            if command.endswith(' brake')]


def test_a_rebind_of_the_brake_key_pushes_the_new_binding_before_using_it(
        bus, aeb_factory, braking_car, keyboard, recorder):
    """A rebind must reach LFS before the new key is ever injected.

    It does not cost an intervention: the push happens at the top of the same
    pass, so the driver is not left unarmed for a cycle just because they
    changed a binding.
    """
    seen = recorder('send_command_to_lfs')
    system = aeb_factory()
    system.settings.set('user_brake_key', 'k')
    bus.emit('new_keybinding', {'button': 'k', 'setting': 'user_brake_key'})
    demand(bus, 9.0)

    assert system.process(braking_car, {})['active'] is True
    # The fixture pushed the original binding; the rebind pushes the new one.
    # Filtered to the brake: the same pass also pushes the throttle binding and
    # cuts the throttle, and neither is what this test is about.
    assert brake_commands(seen)[-1] == '/key K brake'
    assert keyboard.calls == [('keyDown', 'k')]


def test_a_reconnect_pushes_the_binding_into_the_new_session_before_using_it(
        bus, aeb_factory, braking_car, keyboard, recorder):
    """LFS may have restarted; a binding from the old session proves nothing."""
    seen = recorder('send_command_to_lfs')
    system = aeb_factory()
    bus.emit('lfs_connected', {})
    demand(bus, 9.0)

    assert system.process(braking_car, {})['active'] is True
    # Pushed twice: once by the fixture, once again for the new session.
    assert brake_commands(seen) == ['/key B brake', '/key B brake']
    assert keyboard.calls == [('keyDown', 'b')]


def test_the_disabled_path_releases_a_running_intervention(
        bus, aeb_factory, braking_car, keyboard):
    system = aeb_factory()
    demand(bus, 9.0)
    assert system.process(braking_car, {})['active'] is True

    system.settings.set('automatic_emergency_brake', 1)

    assert system.process(braking_car, {}) == {'active': False}
    assert keyboard.calls == [('keyDown', 'b'), ('keyUp', 'b')]


def test_the_first_cycle_pushes_the_binding_so_the_system_can_arm(
        bus, aeb_factory, braking_car, keyboard, recorder):
    seen = recorder('send_command_to_lfs')
    system = aeb_factory(push=False)
    demand(bus, 9.0)

    system.process(braking_car, {})
    result = system.process(braking_car, {})

    assert brake_commands(seen) == ['/key B brake']
    assert result['active'] is True
    assert keyboard.calls == [('keyDown', 'b')]


def test_the_system_stays_enabled_while_a_press_of_ours_is_outstanding(
        bus, aeb_factory, braking_car, keyboard):
    system = aeb_factory()
    demand(bus, 9.0)
    assert system.process(braking_car, {})['active'] is True
    assert system.key_output.holds_press() is True

    system.settings.set('automatic_emergency_brake', 0)

    # The manager skips a system whose is_enabled() is False, so saying False
    # here strands the pressed key.
    assert system.is_enabled() is True


# --- Several systems ask for braking; the largest wins ----------------------
#
# Forward collision, cross traffic and blind spot all publish
# ``needed_deceleration_update`` every cycle. Before they were kept apart by
# ``source``, the last emitter of an assistance pass simply overwrote the
# others, so which hazard got braked for was decided by the iteration order of
# ``AssistanceManager.systems``.


def test_the_largest_demand_of_the_pass_is_the_one_acted_on(
        bus, aeb_factory, braking_car, keyboard):
    system = aeb_factory()
    demand(bus, 9.0, source='cross_traffic')
    demand(bus, 0.0, source='forward_collision')

    assert system.process(braking_car, {})['active'] is True
    assert keyboard.calls == [('keyDown', 'b')]


def test_a_quiet_source_cannot_overwrite_a_loud_one(
        bus, aeb_factory, braking_car):
    """Order within the pass must not matter - either way round."""
    system = aeb_factory()
    demand(bus, 0.0, source='blind_spot')
    demand(bus, 8.0, source='forward_collision')
    assert system.process(braking_car, {})['active'] is True


def test_a_source_that_stops_publishing_releases_the_brake(
        bus, aeb_factory, braking_car, keyboard):
    """A system switched off, self-disabled after repeated failures, or simply
    not called any more publishes nothing at all. A value left over from the
    last time it ran would hold the brake down with nobody left to lift it.
    """
    system = aeb_factory()
    for _ in range(2):
        demand(bus, 9.0, source='cross_traffic')
        assert system.process(braking_car, {})['active'] is True

    # Cross traffic warning switched off mid-intervention: silence, not zero.
    for _ in range(EmergencyBrake.RELEASE_DEBOUNCE_CYCLES):
        system.process(braking_car, {})
    assert system._engaged is False
    assert keyboard.calls == [('keyDown', 'b'), ('keyUp', 'b')]


def test_the_engaged_event_names_the_source(bus, aeb_factory, braking_car,
                                            recorder):
    """Only the bus says *why* a car braked; a trace without it reads as "the
    car stopped" (``simulation_tests/README.md`` §13)."""
    seen = recorder('emergency_brake_changed')
    system = aeb_factory()
    demand(bus, 9.0, source='blind_spot')
    system.process(braking_car, {})

    assert seen.last('emergency_brake_changed') == {
        'active': True, 'source': 'blind_spot'}


# --- The speed floor is about rear-ending, not about being hit --------------


def test_a_crossing_demand_engages_below_the_forward_collision_floor(
        bus, aeb_factory, make_own_vehicle, keyboard):
    """Creeping into a junction at 8 km/h is not a parking manoeuvre: the
    energy belongs to the car crossing in front of us."""
    system = aeb_factory()
    creeping = make_own_vehicle(speed=8.0, local_plid=1, plid=1,
                                control_mode=0)
    demand(bus, 9.0, source='cross_traffic')

    assert system.process(creeping, {})['active'] is True
    assert keyboard.calls == [('keyDown', 'b')]


def test_the_forward_collision_floor_is_unchanged(
        bus, aeb_factory, make_own_vehicle, keyboard):
    system = aeb_factory()
    creeping = make_own_vehicle(speed=8.0, local_plid=1, plid=1,
                                control_mode=0)
    demand(bus, 9.0, source='forward_collision')

    assert system.process(creeping, {})['active'] is False
    assert keyboard.calls == []


def test_even_a_crossing_demand_has_a_floor(
        bus, aeb_factory, make_own_vehicle, keyboard):
    """Below 3 km/h the speed reading is noise and the car is parking."""
    system = aeb_factory()
    stopped = make_own_vehicle(speed=1.0, local_plid=1, plid=1,
                               control_mode=0)
    demand(bus, 9.0, source='cross_traffic')

    assert system.process(stopped, {})['active'] is False
    assert keyboard.calls == []


# --- End to end over the bus ------------------------------------------------


def test_cross_traffic_braking_end_to_end(bus, make_settings, physical,
                                          keyboard, make_own_vehicle,
                                          make_vehicle):
    """CTW -> needed_deceleration_update -> AEB, with nothing else in between.

    The two systems never see each other; the whole coupling is the event
    (``AGENTS.md`` §5). They are called by hand in the order
    ``AssistanceManager`` uses - every demand publisher first, the
    intervention last.
    """
    from assistance.cross_traffic_warning import CrossTrafficWarning

    settings = make_settings(automatic_emergency_brake=2, user_brake_key='b',
                             language='en')
    ctw = CrossTrafficWarning(bus, settings)
    aeb = EmergencyBrake(bus, settings, physical_keys=physical,
                         guard=FakeGuard(None), clock=FakeClock())
    aeb.key_output.push_binding()

    # 12 m from the junction at 36 km/h, an identical car 12 m from the other
    # side: stopping short needs ~7.9 m/s2, which is past the engage threshold.
    own = make_own_vehicle(plid=1, local_plid=1, x=0.0, y=-12.0, heading=0.0,
                           speed=36.0, control_mode=0)
    other = make_vehicle(plid=2, x=12.0, y=0.0, heading=90.0, speed=36.0)
    other.update_distance_to_player(own.data.x, own.data.y, own.data.z)
    other.update_angle_to_player(own.data.x, own.data.y, own.data.heading)

    ctw.process(own, {2: other})
    assert aeb.process(own, {2: other})['active'] is True
    assert keyboard.calls == [('keyDown', 'b')]


def test_a_junction_that_is_still_far_away_does_not_engage(
        bus, make_settings, physical, keyboard, make_own_vehicle,
        make_vehicle):
    """The same geometry 30 m out: the warning is on, the brake is not."""
    from assistance.cross_traffic_warning import CrossTrafficWarning

    settings = make_settings(automatic_emergency_brake=2, user_brake_key='b',
                             language='en')
    ctw = CrossTrafficWarning(bus, settings)
    aeb = EmergencyBrake(bus, settings, physical_keys=physical,
                         guard=FakeGuard(None), clock=FakeClock())
    aeb.key_output.push_binding()

    own = make_own_vehicle(plid=1, local_plid=1, x=0.0, y=-30.0, heading=0.0,
                           speed=36.0, control_mode=0)
    other = make_vehicle(plid=2, x=30.0, y=0.0, heading=90.0, speed=36.0)
    other.update_distance_to_player(own.data.x, own.data.y, own.data.z)
    other.update_angle_to_player(own.data.x, own.data.y, own.data.heading)

    ctw.process(own, {2: other})
    assert aeb.process(own, {2: other})['active'] is False
    assert keyboard.calls == []


def test_an_intervention_takes_a_held_throttle_away(
        bus, aeb_factory, braking_car, physical, keyboard):
    """known-issues #46, end to end.

    The driver is flat out when the hazard appears. The invalid ``/key -1 throttle``
    left LFS reading the held input and reporting full throttle for the whole
    braking phase - measured twice in `simulation_tests`, scenarios 08 and 26.
    """
    system = aeb_factory()
    system.key_throttle.push_binding()
    physical.driver_presses('up')       # the configured throttle key
    demand(bus, 9.0)

    assert system.process(braking_car, {})['active'] is True

    assert ('keyUp', 'up') in keyboard.calls
    assert physical.down_for_lfs('up') is False
    assert physical.physically_down('up') is True


def test_the_throttle_comes_back_with_the_brake(
        bus, aeb_factory, braking_car, physical, keyboard):
    system = aeb_factory()
    system.key_throttle.push_binding()
    physical.driver_presses('up')
    demand(bus, 9.0)
    system.process(braking_car, {})

    system._disengage()

    assert keyboard.calls[-1] == ('keyDown', 'up')
    assert physical.down_for_lfs('up') is True


def test_first_idle_cycle_clears_startup_throttle_binding_warning(aeb_factory, braking_car, recorder):
    system = aeb_factory(push=False, user_throttle_key='mousel')
    seen = recorder('throttle_cut_availability')
    system._on_player_changed({'control_mode': 0})
    assert seen.last('throttle_cut_availability')['reason'] == 'throttle_binding_not_pushed'
    system.process(braking_car, {})
    assert system.key_throttle.unavailable_reason() is None
    assert seen.last('throttle_cut_availability')['reason'] is None
    count = seen.count('throttle_cut_availability')
    system.process(braking_car, {})
    assert seen.count('throttle_cut_availability') == count
