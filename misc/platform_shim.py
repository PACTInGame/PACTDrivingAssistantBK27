"""Lazy, platform-tolerant access to OS- and hardware-bound modules.

The add-on itself only ever runs on Windows next to LFS, but the code must be
*importable* everywhere, otherwise none of its logic can be tested without
Windows and without the game (``reference/testing.md``).  Every module that is
Windows-only (``winsound``, the vJoy ctypes bindings), that needs a display
server (``tkinter``, ``pynput``, ``pyautogui``) or a sound device (``pygame``)
is therefore imported *here*, on first use, instead of at module level in the
call sites.

Behaviour on Windows is unchanged: the first accessor call imports the real
module, every later call returns the very same module object, so a call site
that used to write ``pyautogui.keyDown(k)`` and now writes
``get_keyboard().keyDown(k)`` does exactly the same thing.

Where the module cannot be imported the accessor returns a *null module*: an
object that absorbs every attribute access and every call, records the call and
returns another null module.  Nothing raises and nothing happens.  Tests can
read back what would have been done via :func:`recorded_calls`, which is how the
key-injection guards are asserted without a keyboard.

A null module is falsy, so a call site that must degrade gracefully can simply
ask ``if not get_tkinter(): ...``.

Rule for new code: never import ``pyautogui``, ``winsound``, ``pynput``,
``pygame``, ``tkinter`` or ``misc.vjoy`` at module level -- go through this
module.
"""

import importlib
import logging
import sys
import threading
from typing import Any, Dict, List, Optional, Tuple

_logger = logging.getLogger(__name__)

# Import cache. Reads are plain dict lookups (atomic under the GIL); the lock is
# only taken while an import is actually running, so the accessors are cheap
# enough to be called from a keypress path.
_modules: Dict[str, Any] = {}
_import_lock = threading.Lock()

# Ring-buffered record of everything a null module swallowed. Bounded, because
# a Windows machine with a missing dependency would otherwise grow this forever.
_MAX_RECORDED_CALLS = 500
_recorded: List[Tuple[str, tuple, dict]] = []
_record_lock = threading.Lock()


def _record(path: str, args: tuple, kwargs: dict):
    with _record_lock:
        _recorded.append((path, args, kwargs))
        if len(_recorded) > _MAX_RECORDED_CALLS:
            del _recorded[:-_MAX_RECORDED_CALLS]


def recorded_calls() -> List[Tuple[str, tuple, dict]]:
    """Calls swallowed by null modules, as ``(dotted_path, args, kwargs)``."""
    with _record_lock:
        return list(_recorded)


def reset_recorded_calls():
    """Drop the recorded calls (tests call this between cases)."""
    with _record_lock:
        _recorded.clear()


class NullModule:
    """Stand-in for a module that is unavailable on this platform."""

    def __init__(self, path: str):
        self._path = path

    def __getattr__(self, name: str) -> "NullModule":
        # Dunder lookups must keep failing, or copy/pickle/inspect misbehave.
        if name.startswith('__') and name.endswith('__'):
            raise AttributeError(name)
        return NullModule(f"{self._path}.{name}")

    def __call__(self, *args, **kwargs) -> "NullModule":
        _record(self._path, args, kwargs)
        return NullModule(f"{self._path}()")

    def __bool__(self) -> bool:
        return False

    def __iter__(self):
        return iter(())

    def __repr__(self) -> str:
        return f"<NullModule {self._path!r}>"


def _load(name: str, submodules: Tuple[str, ...] = ()) -> Any:
    """Import *name* once and cache it; return a :class:`NullModule` on failure.

    *submodules* are imported as well so that ``pynput.keyboard`` and friends
    are bound as attributes of the returned package.
    """
    cached = _modules.get(name)
    if cached is not None:
        return cached

    with _import_lock:
        cached = _modules.get(name)
        if cached is not None:
            return cached
        try:
            module = importlib.import_module(name)
            for sub in submodules:
                importlib.import_module(f"{name}.{sub}")
        except Exception as exc:  # ImportError, but pynput/pygame raise others
            _logger.warning(
                "%s is not available on this platform (%s) - "
                "every call to it will be ignored.", name, exc)
            module = NullModule(name)
        _modules[name] = module
        return module


# ─── Accessors ───────────────────────────────────────────────────────────────

def get_keyboard() -> Any:
    """``pyautogui`` -- global key injection (auto-hold, automatic gearbox).

    These are OS-wide keystrokes; see ``reference/ui.md`` §1.4 for when they
    may be sent at all.
    """
    return _load('pyautogui')


def get_sound() -> Any:
    """``winsound`` -- the PDC beep. Windows-only, blocking; never call in ``process()``."""
    return _load('winsound')


def get_input_listener() -> Any:
    """``pynput`` (with ``.keyboard`` / ``.mouse``) -- key capture for rebinding."""
    return _load('pynput', ('keyboard', 'mouse'))


def get_audio() -> Any:
    """``pygame`` (with ``.mixer``) -- warning sound playback."""
    return _load('pygame', ('mixer',))


def get_tkinter() -> Any:
    """``tkinter`` (with ``.filedialog`` / ``.messagebox``) -- the setup wizard."""
    return _load('tkinter', ('filedialog', 'messagebox'))


def get_vjoy() -> Any:
    """``misc.vjoy`` -- ctypes bindings to the vJoy driver DLL."""
    return _load('misc.vjoy')


_ACCESSORS = {
    'pyautogui': get_keyboard,
    'winsound': get_sound,
    'pynput': get_input_listener,
    'pygame': get_audio,
    'tkinter': get_tkinter,
    'vjoy': get_vjoy,
}


def is_windows() -> bool:
    return sys.platform.startswith('win')


def is_available(name: str) -> bool:
    """True if the real module behind *name* could be imported.

    *name* is one of ``pyautogui``, ``winsound``, ``pynput``, ``pygame``,
    ``tkinter``, ``vjoy``.
    """
    accessor = _ACCESSORS.get(name)
    if accessor is None:
        raise KeyError(f"unknown platform module {name!r}")
    return not isinstance(accessor(), NullModule)
