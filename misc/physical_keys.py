"""Which keys is the *hardware* holding, and what does LFS currently believe?

Emergency braking through key injection is only safe if those two questions can
be answered separately (``reference/control-intervention.md`` §3.1). Windows
lets us: every event from ``SendInput``/``keybd_event`` carries
``LLKHF_INJECTED`` (``LLMHF_INJECTED`` for the mouse), genuine hardware never
does. Measured::

    injected keydown  flags=0x10   injected keyup  flags=0x90
    physical keydown  flags=0x00   physical keyup  flags=0x80

``pynput``'s high-level ``on_press``/``on_release`` throw that flag away, so
this module goes one level down and reads the raw ``KBDLLHOOKSTRUCT`` through
``keyboard.Listener(win32_event_filter=...)``.

Two states are kept per key, and the difference between them is the whole point:

``physically_down``
    the user is really holding the key. Our injected events never touch this.

``down_for_lfs``
    the last event of *any* origin was a press. LFS reads the same input stack,
    so this is what LFS believes -- including presses we made ourselves. It is
    observed rather than inferred: we see our own injected events come back
    through the hook like everybody else's.

With both, arbitration collapses to two lines (see ``Controls/brake_key.py``):
press when the brake is wanted and LFS does not have it, release only what we
pressed ourselves and only while the user is not holding the key.

Cost: the filters run inside the OS hook, on *every* keystroke and mouse click
on the machine, so they do two dict writes and nothing else -- no logging, no
locks, no allocation. They deliberately return ``False``, which tells pynput to
skip building its own key objects; it does **not** suppress the event for other
applications.
"""

import logging
import threading
from typing import Optional

from misc.key_names import vk_for
from misc.platform_shim import get_input_listener, is_available, is_windows

logger = logging.getLogger(__name__)

# KBDLLHOOKSTRUCT.flags / MSLLHOOKSTRUCT.flags
LLKHF_INJECTED = 0x10
LLMHF_INJECTED = 0x01

WM_KEYDOWN = 0x0100
WM_KEYUP = 0x0101
WM_SYSKEYDOWN = 0x0104
WM_SYSKEYUP = 0x0105

_KEY_MESSAGES = {
    WM_KEYDOWN: True,
    WM_SYSKEYDOWN: True,
    WM_KEYUP: False,
    WM_SYSKEYUP: False,
}

# WM_?BUTTONDOWN / UP -> (virtual key, pressed)
_MOUSE_MESSAGES = {
    0x0201: (0x01, True),   # WM_LBUTTONDOWN
    0x0202: (0x01, False),  # WM_LBUTTONUP
    0x0204: (0x02, True),   # WM_RBUTTONDOWN
    0x0205: (0x02, False),  # WM_RBUTTONUP
    0x0207: (0x04, True),   # WM_MBUTTONDOWN
    0x0208: (0x04, False),  # WM_MBUTTONUP
}


class PhysicalKeyState:
    """Hardware key state, tracked through the Windows low-level hooks.

    Not started automatically: a global hook is a system-wide side effect, so
    the owner starts it when a feature needs it and stops it on shutdown.
    """

    def __init__(self):
        # vk -> bool. Written on the hook thread, read on worker threads.
        # Plain dicts: single item assignment and ``get`` are atomic under the
        # GIL, and a lock in a low-level hook is exactly what must not happen.
        self._physical = {}
        self._for_lfs = {}
        self._keyboard_listener = None
        self._mouse_listener = None
        # start()/stop() only. Installing the listeners takes >100 ms, so it
        # runs off the assistance thread and two callers can collide.
        self._lifecycle_lock = threading.Lock()

    # ─── Lifecycle ────────────────────────────────────────────────────

    def start(self) -> bool:
        """Install the hooks. ``False`` if this platform cannot answer.

        A caller that cannot track physical state must **refuse to arm** any
        feature that injects keys, rather than fall back to injecting blindly:
        without this tracking the key-release trap is unavoidable.
        """
        with self._lifecycle_lock:
            return self._start_locked()

    def _start_locked(self) -> bool:
        if self.is_running():
            return True
        if not is_windows():
            logger.info("Physical key tracking needs Windows -- not started.")
            return False
        if not is_available('pynput'):
            logger.warning("pynput is not available -- physical key tracking "
                           "is off, key injection must stay disabled.")
            return False

        pynput = get_input_listener()
        try:
            self._keyboard_listener = pynput.keyboard.Listener(
                win32_event_filter=self._keyboard_filter)
            self._keyboard_listener.start()
            self._mouse_listener = pynput.mouse.Listener(
                win32_event_filter=self._mouse_filter)
            self._mouse_listener.start()
        except Exception as exc:
            logger.error("Could not install input hooks: %s: %s",
                         type(exc).__name__, exc)
            self._stop_locked()
            return False

        logger.info("Physical key tracking active.")
        return True

    def stop(self):
        """Remove the hooks. Safe to call twice, and from any thread."""
        with self._lifecycle_lock:
            self._stop_locked()

    def _stop_locked(self):
        for attribute in ('_keyboard_listener', '_mouse_listener'):
            listener = getattr(self, attribute)
            setattr(self, attribute, None)
            if listener is None:
                continue
            try:
                listener.stop()
            except Exception as exc:
                logger.debug("Stopping %s failed: %s", attribute, exc)
        self._physical.clear()
        self._for_lfs.clear()

    def is_running(self) -> bool:
        """Are both hooks alive?

        Asked before every actuation, not once at startup: a listener thread
        that died would otherwise leave us injecting keys we can no longer
        take back.
        """
        return bool(self._keyboard_listener is not None
                    and self._mouse_listener is not None
                    and getattr(self._keyboard_listener, 'running', False)
                    and getattr(self._mouse_listener, 'running', False))

    # ─── Queries ──────────────────────────────────────────────────────

    def physically_down(self, stored_name) -> bool:
        """Is the user really holding this key? Injected presses do not count."""
        vk = vk_for(stored_name)
        return False if vk is None else self._physical.get(vk, False)

    def down_for_lfs(self, stored_name) -> bool:
        """Does LFS currently see this key as pressed, whoever pressed it?"""
        vk = vk_for(stored_name)
        return False if vk is None else self._for_lfs.get(vk, False)

    def forget(self, stored_name):
        """Drop what we know about one key.

        Used after a rebind: the old key's state is stale and must not decide
        anything about the new one.
        """
        vk = vk_for(stored_name)
        if vk is not None:
            self._physical.pop(vk, None)
            self._for_lfs.pop(vk, None)

    # ─── Hook callbacks -- keep these trivial ─────────────────────────

    def _keyboard_filter(self, msg, data) -> bool:
        pressed = _KEY_MESSAGES.get(msg)
        if pressed is not None:
            vk = data.vkCode
            self._for_lfs[vk] = pressed
            if not data.flags & LLKHF_INJECTED:
                self._physical[vk] = pressed
        return False  # skip pynput's own decoding; does not suppress the event

    def _mouse_filter(self, msg, data) -> bool:
        button = _MOUSE_MESSAGES.get(msg)
        if button is not None:
            vk, pressed = button
            self._for_lfs[vk] = pressed
            if not data.flags & LLMHF_INJECTED:
                self._physical[vk] = pressed
        return False
