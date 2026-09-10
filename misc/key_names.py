"""One key, three spellings -- the translation table between them.

A single bound key has to be named three different ways in this project:

===============  =========================  ===================================
Spelling         Example                    Who needs it
===============  =========================  ===================================
stored           ``"page_up"``, ``"s"``     ``settings.json``; written by
                 ``"mousel"``               ``key_binder`` from ``pynput``
virtual key       ``0x21``                  the low-level hook in
                                            :mod:`misc.physical_keys`, which
                                            only ever sees VK codes
LFS              ``"pgup"``                 ``/key <key> <function>``
                                            (``Commands.txt``)
pyautogui        ``"pageup"``               injection through
                                            :func:`misc.platform_shim.get_keyboard`
===============  =========================  ===================================

They disagree in exactly the places that hurt: ``page_up`` / ``pgup`` /
``pageup`` are three different strings for one key, and a mouse button is a
"key" to LFS and to ``settings.json`` but not to ``pyautogui``. Every lookup
goes through this module so the disagreement is written down once.

Unknown names return ``None`` rather than raising. A key we cannot name in
LFS's spelling cannot be pushed with ``/key``, and the caller has to say so
out loud instead of guessing -- see ``reference/control-intervention.md`` §3.1.
"""

from typing import NamedTuple, Optional


class KeySpelling(NamedTuple):
    """How one key is written in each of the four worlds above."""

    stored: str
    vk: int
    lfs: Optional[str]          # None: LFS has no /key name for it
    pyautogui: Optional[str]    # None: not a keyboard key (mouse button)
    is_mouse: bool = False


# Named keys, in the spelling ``pynput`` produces and ``key_binder`` stores.
# The LFS column comes from Commands.txt "KEYS for the /key command"; keys LFS
# does not accept there (enter, esc, tab, the function keys) are deliberately
# absent -- LFS only accepts those for ``/press``, which cannot hold a key and
# is therefore useless for actuation.
_SPECIAL = (
    KeySpelling('up',        0x26, 'up',     'up'),
    KeySpelling('down',      0x28, 'down',   'down'),
    KeySpelling('left',      0x25, 'left',   'left'),
    KeySpelling('right',     0x27, 'right',  'right'),
    KeySpelling('space',     0x20, 'space',  'space'),
    KeySpelling('page_up',   0x21, 'pgup',   'pageup'),
    KeySpelling('page_down', 0x22, 'pgdn',   'pagedown'),
    # Mouse buttons. LFS treats them as keys; pyautogui does not, so the
    # injector has to branch on ``is_mouse`` (VK_LBUTTON/RBUTTON/MBUTTON).
    KeySpelling('mousel',    0x01, 'mousel', None, is_mouse=True),
    KeySpelling('mouser',    0x02, 'mouser', None, is_mouse=True),
    KeySpelling('mousem',    0x04, 'mousem', None, is_mouse=True),
)

_BY_STORED = {spelling.stored: spelling for spelling in _SPECIAL}

# Letters and digits: VK codes equal the ASCII code of the uppercase form, and
# LFS and pyautogui both take the character itself.
for _char in 'abcdefghijklmnopqrstuvwxyz0123456789':
    _BY_STORED[_char] = KeySpelling(_char, ord(_char.upper()), _char.upper(), _char)


def spelling_for(stored_name) -> Optional[KeySpelling]:
    """Look up a key by the name ``settings.json`` holds. ``None`` if unknown.

    Case-insensitive and tolerant of a non-string, because the value comes out
    of a settings file a user may have edited.
    """
    if not isinstance(stored_name, str):
        return None
    return _BY_STORED.get(stored_name.strip().lower())


def vk_for(stored_name) -> Optional[int]:
    """The Windows virtual key code, for comparing against a hook event."""
    spelling = spelling_for(stored_name)
    return spelling.vk if spelling else None


def lfs_name_for(stored_name) -> Optional[str]:
    """The spelling ``/key`` expects, or ``None`` if LFS cannot bind this key."""
    spelling = spelling_for(stored_name)
    return spelling.lfs if spelling else None


def is_mouse_button(stored_name) -> bool:
    """Is this a mouse button rather than a keyboard key?"""
    spelling = spelling_for(stored_name)
    return bool(spelling and spelling.is_mouse)
