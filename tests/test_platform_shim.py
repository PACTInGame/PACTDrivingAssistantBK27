"""The platform shim: import safety off Windows, transparency on Windows."""

import importlib

import pytest

from misc import platform_shim
from misc.platform_shim import NullModule


@pytest.fixture(autouse=True)
def _clean_recorder():
    platform_shim.reset_recorded_calls()
    yield
    platform_shim.reset_recorded_calls()


ACCESSORS = [
    platform_shim.get_keyboard,
    platform_shim.get_sound,
    platform_shim.get_input_listener,
    platform_shim.get_audio,
    platform_shim.get_tkinter,
    platform_shim.get_vjoy,
]


@pytest.mark.parametrize("accessor", ACCESSORS, ids=lambda a: a.__name__)
def test_accessor_never_raises(accessor):
    """Whatever the platform, an accessor returns something usable."""
    assert accessor() is not None


@pytest.mark.parametrize("accessor", ACCESSORS, ids=lambda a: a.__name__)
def test_accessor_is_cached(accessor):
    assert accessor() is accessor()


def test_null_module_swallows_attribute_chains_and_calls():
    null = NullModule('demo')
    result = null.mixer.Sound('beep.wav').play()

    assert isinstance(result, NullModule)
    assert ('demo.mixer.Sound', ('beep.wav',), {}) in platform_shim.recorded_calls()


def test_null_module_is_falsy_so_call_sites_can_degrade():
    assert not NullModule('demo')


def test_null_module_still_fails_on_dunders():
    """Dunder lookups must not be absorbed, or copy/inspect/pickle misbehave."""
    with pytest.raises(AttributeError):
        NullModule('demo').__wrapped__


def test_recorded_calls_can_be_reset():
    NullModule('demo').press('q')
    assert platform_shim.recorded_calls()
    platform_shim.reset_recorded_calls()
    assert platform_shim.recorded_calls() == []


def test_key_injection_is_recorded_when_pyautogui_is_missing():
    """WP9's guard tests read the keystrokes back through the shim."""
    if platform_shim.is_available('pyautogui'):
        pytest.skip("pyautogui is installed; the recorder is not used")

    keyboard = platform_shim.get_keyboard()
    keyboard.keyDown('q')
    keyboard.keyUp('q')

    assert [call[0] for call in platform_shim.recorded_calls()] == [
        'pyautogui.keyDown', 'pyautogui.keyUp']


def test_real_module_is_returned_unchanged_when_importable():
    """On Windows the accessor must hand out the real module, not a wrapper.

    Stood in for by a stdlib module with submodules, since the Windows-only
    ones are by definition absent here.
    """
    try:
        loaded = platform_shim._load('email', ('message',))
        assert loaded is importlib.import_module('email')
        assert loaded.message is importlib.import_module('email.message')
    finally:
        platform_shim._modules.pop('email', None)


def test_is_available_rejects_unknown_names():
    with pytest.raises(KeyError):
        platform_shim.is_available('directx')
