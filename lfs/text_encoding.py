"""Encoding of button text for LFS (WP3).

LFS does not speak Unicode. A button string is a byte string interpreted in an
LFS *code page*, and the code page is switched inline with a caret escape:
``^L`` Latin (cp1252, the default), ``^E`` Central European (cp1250),
``^T`` Turkish (cp1254), ``^B`` Baltic (cp1257), ``^C`` Cyrillic (cp1251),
``^G`` Greek (cp1253), ``^J`` Japanese (cp932), ``^S`` simplified Chinese
(cp936), ``^H`` traditional Chinese (cp950), ``^K`` Korean (cp949).
The table lives in ``pyinsim/strmanip.py`` and is re-used here.

What this replaces: ``LFSConnector.send_button`` used to try ``latin-1`` and
fall back to ``str.encode()`` (UTF-8) on failure. LFS never reads UTF-8, so all
52 Turkish translation strings and one Swedish one rendered as mojibake
(``known-issues.md``, ``reference/insim.md``).

Two deliberate non-goals:

* ``^`` is **not** escaped. The UI puts LFS colour codes (``^1`` … ``^7``)
  straight into button text today, and escaping them would turn every coloured
  label into literal text. A caret in user-supplied text therefore still acts
  as a control character, exactly as before.
* the encoder does not reflow or wrap. LFS draws one line per button.
"""

import unicodedata

from pyinsim.strmanip import _ENCODING_MAP

__all__ = [
    'BUTTON_TEXT_MAX_BYTES',
    'MSL_TEXT_MAX_BYTES',
    'MST_TEXT_MAX_BYTES',
    'button_text_capacity',
    'decode_button_text',
    'encode_button_text',
    'encode_lfs_text',
]

# InSim.txt: "IS_BTN ... followed by 0 to 240 characters". pyinsim pads the
# text to a multiple of four, so 239 bytes guarantees at least one padding NUL
# and a total packet text block of exactly 240.
BUTTON_TEXT_MAX_BYTES = 239

# The same code pages apply to chat text. pyinsim's struct formats truncate
# silently ('63s' / '127s'), which would cut a multi-byte character or a
# code-page escape in half -- so the encoder gets the limit instead.
MST_TEXT_MAX_BYTES = 63         # IS_MST: a command typed into LFS
MSL_TEXT_MAX_BYTES = 127        # IS_MSL: a local-only chat line

DEFAULT_CODE_PAGE = 'L'

# Order in which code pages are tried for a character the current page cannot
# hold. Latin first, then the other single-byte European pages, then the CJK
# pages -- switching into a double-byte page for one accented character would
# cost bytes for nothing.
_CODE_PAGE_ORDER = ('L', 'E', 'T', 'B', 'C', 'G', 'J', 'S', 'H', 'K')

_CODECS = {letter: _ENCODING_MAP[letter][0] for letter in _CODE_PAGE_ORDER}

# Truncation marker: ASCII, so it needs no code page of its own.
_ELLIPSIS = b'..'

# --- width model -------------------------------------------------------------
#
# LFS renders button text in a proportional font scaled to the button height;
# InSim.txt gives no character metric, so a capacity has to be calibrated. The
# widest text the app itself ships is the off-track banner: 27 characters in
# W=25, H=5 (``ui/ui_manager.py``), which is legible in game. That is
# 27 / 25 = 1.08 characters per width unit at H=5, and character width scales
# with H. Taken as the *minimum* density LFS achieves, with a factor 2 margin
# on top so that only genuinely runaway text (a chat-driven notification, a
# mod car name) is ever cut:
_CHARS_PER_WIDTH_UNIT_AT_H5 = 1.08
_CAPACITY_MARGIN = 2.0
_REFERENCE_HEIGHT = 5.0


def button_text_capacity(width: int, height: int) -> int:
    """Characters that fit into a ``width`` x ``height`` button, generously.

    Conservative by design: a value that is too large only lets text overflow
    the way it does today, a value that is too small silently eats information
    the driver needs. See the calibration note above; the constant is the one
    thing here that wants checking in the running game.
    """
    if width <= 0 or height <= 0:
        return BUTTON_TEXT_MAX_BYTES
    chars = (_CHARS_PER_WIDTH_UNIT_AT_H5 * width
             * (_REFERENCE_HEIGHT / float(height)) * _CAPACITY_MARGIN)
    return max(1, min(BUTTON_TEXT_MAX_BYTES, int(chars)))


def _sanitise(text: str) -> str:
    """Drop what no LFS code page can carry, without changing visible text.

    * NFC composition, so ``a`` + combining diaeresis becomes ``ä``;
    * combining marks that survive composition are dropped -- they are stray
      diacritics on an already-accented base (one Swedish string has one) and
      no code page can encode them;
    * control characters are dropped: a NUL would terminate the button text
      early inside LFS, and newlines/tabs have no meaning on a button.
    """
    if text.isascii() and text.isprintable():
        # By far the common case (speedometer, gear, menu labels in English or
        # German ASCII). NFC is a no-op on ASCII and there is nothing to drop.
        return text
    text = unicodedata.normalize('NFC', text)
    out = []
    for ch in text:
        category = unicodedata.category(ch)
        if category == 'Mn' and not _find_code_page(ch, DEFAULT_CODE_PAGE):
            continue
        if category == 'Cc':
            continue
        out.append(ch)
    return ''.join(out)


def _encodes(ch: str, page: str) -> bool:
    try:
        ch.encode(_CODECS[page])
        return True
    except (UnicodeEncodeError, LookupError):
        return False


# How far ahead a code-page choice looks when several pages could hold the
# character. Purely an optimisation: fewer switches, fewer bytes.
_LOOKAHEAD = 32


def _find_code_page(ch: str, current: str, rest: str = ''):
    """The code page to use for *ch*, preferring the active one. None if any.

    When a switch is unavoidable and several pages qualify, the one that also
    covers the most of the following text wins -- otherwise a Turkish string
    ping-pongs between two pages that each hold half of its characters.
    """
    if _encodes(ch, current):
        return current

    candidates = [page for page in _CODE_PAGE_ORDER if _encodes(ch, page)]
    if not candidates:
        return None
    if len(candidates) == 1 or not rest:
        return candidates[0]

    lookahead = [c for c in rest[:_LOOKAHEAD] if ord(c) > 127 and c != '^']
    if not lookahead:
        return candidates[0]

    def coverage(page):
        return sum(1 for c in lookahead if _encodes(c, page))

    return max(candidates, key=lambda page: (coverage(page),
                                             -_CODE_PAGE_ORDER.index(page)))


def encode_button_text(text, max_chars: int = None) -> bytes:
    """Encode *text* for an ``IS_BTN`` packet.

    Args:
        text: the string to show. ``bytes`` are passed through (already
            encoded by a caller that knew what it was doing) and only length
            limited.
        max_chars: visible-character budget, e.g. from
            :func:`button_text_capacity`. Caret control sequences do not count
            against it. ``None`` applies only the protocol byte limit.

    Returns:
        Bytes ready for ``IS_BTN.Text``, at most
        :data:`BUTTON_TEXT_MAX_BYTES` long, never ending in a dangling ``^``
        and never cut inside a multi-byte character or a code-page escape.
    """
    return encode_lfs_text(text, max_chars=max_chars,
                           max_bytes=BUTTON_TEXT_MAX_BYTES)


def encode_lfs_text(text, max_chars: int = None,
                    max_bytes: int = BUTTON_TEXT_MAX_BYTES) -> bytes:
    """Encode *text* into LFS code pages, bounded by *max_bytes*.

    The shared primitive behind button text and chat text -- see
    :func:`encode_button_text` for the argument semantics.
    """
    if isinstance(text, (bytes, bytearray)):
        return bytes(text[:max_bytes])
    if text is None:
        return b''
    if not isinstance(text, str):
        text = str(text)

    text = _sanitise(text)
    if text.isascii():
        # ASCII is identical in every LFS code page, so no escape is ever
        # needed and the per-character code-page search can be skipped. This is
        # the path the 50 ms HUD repaint takes.
        return _encode_ascii(text, max_chars, max_bytes)

    out = bytearray()
    boundaries = [0]        # byte offsets at which a complete unit ended
    page = DEFAULT_CODE_PAGE
    visible = 0
    truncated = False
    index = 0
    length = len(text)

    while index < length:
        ch = text[index]

        # Caret sequences are the caller's control codes, passed through as-is.
        if ch == '^' and index + 1 < length:
            following = text[index + 1]
            chunk = ('^' + following).encode('ascii', 'replace')
            if len(out) + len(chunk) > max_bytes:
                truncated = True
                break
            out += chunk
            boundaries.append(len(out))
            if following in _CODECS:
                # The caller switched the code page itself - follow it, or the
                # next escape we emit would be computed against the wrong page.
                page = following
            index += 2
            continue

        if ch == '^':
            # Trailing lone caret: LFS would swallow the next character (there
            # is none) - drop it rather than emit a dangling control byte.
            break

        if max_chars is not None and visible >= max_chars:
            truncated = True
            break

        target = _find_code_page(ch, page, text[index + 1:])
        if target is None:
            chunk = b'?'
        else:
            chunk = b''
            if target != page:
                chunk += ('^' + target).encode('ascii')
            chunk += ch.encode(_CODECS[target])

        if len(out) + len(chunk) > max_bytes:
            truncated = True
            break

        out += chunk
        boundaries.append(len(out))
        if target is not None:
            page = target
        visible += 1
        index += 1

    if truncated:
        room = max_bytes - len(_ELLIPSIS)
        cut = 0
        for boundary in boundaries:
            if boundary > room:
                break
            cut = boundary
        out = out[:cut] + _ELLIPSIS

    return bytes(out)


def _drop_dangling_caret(text: str) -> str:
    """Cut a trailing ``^`` that has nothing to control.

    LFS reads a caret as "consume the next character", so a lone one at the end
    would eat whatever follows. ``^^`` is a *complete* sequence (a literal
    caret) and must survive, which is why this walks the string instead of
    looking at the last character.
    """
    index = 0
    while index < len(text):
        if text[index] == '^':
            if index + 1 >= len(text):
                return text[:index]
            index += 2
        else:
            index += 1
    return text


def _visible_cut(text: str, max_chars: int) -> int:
    """Index after ``max_chars`` visible characters; caret sequences are free."""
    visible = 0
    index = 0
    while index < len(text):
        if text[index] == '^' and index + 1 < len(text):
            index += 2
            continue
        if visible >= max_chars:
            break
        visible += 1
        index += 1
    return index


def _byte_cut(text: str, limit: int) -> int:
    """Longest prefix of at most *limit* ASCII bytes ending on a unit boundary."""
    index = 0
    boundary = 0
    while index < len(text):
        step = 2 if (text[index] == '^' and index + 1 < len(text)) else 1
        if index + step > limit:
            break
        index += step
        boundary = index
    return boundary


def _encode_ascii(text: str, max_chars, max_bytes: int) -> bytes:
    """Fast path of :func:`encode_lfs_text` for pure ASCII text.

    Same result as the general loop, without the per-character code-page
    search -- ASCII is identical in every LFS code page. This is the path the
    50 ms HUD repaint takes.
    """
    text = _drop_dangling_caret(text)

    truncated = False
    if max_chars is not None:
        cut = _visible_cut(text, max_chars)
        if cut < len(text):
            text = text[:cut]
            truncated = True
    if len(text) > max_bytes:
        truncated = True

    if not truncated:
        return text.encode('ascii')

    room = max(0, max_bytes - len(_ELLIPSIS))
    return text[:_byte_cut(text, room)].encode('ascii') + _ELLIPSIS


def decode_button_text(data: bytes) -> str:
    """Inverse of :func:`encode_button_text` -- what LFS puts on screen.

    Code-page escapes are consumed as state changes; every other caret
    sequence is returned verbatim, because that is what the encoder passed
    through. Used by the tests as the round-trip oracle.
    """
    if isinstance(data, str):
        return data
    out = []
    page = DEFAULT_CODE_PAGE
    index = 0
    length = len(data)
    while index < length:
        byte = data[index]
        if byte == 0:
            break                       # LFS stops at the NUL terminator
        if byte == 0x5E and index + 1 < length:     # '^'
            following = chr(data[index + 1])
            if following in _CODECS:
                page = following
                index += 2
                continue
            out.append('^' + following)
            index += 2
            continue
        # Consume the longest run that is not a caret and decode it in one go,
        # so multi-byte code pages survive.
        end = index
        while end < length and data[end] not in (0x5E, 0x00):
            end += 1
        out.append(bytes(data[index:end]).decode(_CODECS[page], errors='replace'))
        index = end
    return ''.join(out)
