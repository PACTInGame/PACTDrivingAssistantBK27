"""Lossless chat decoding and conservative post-run LFS message review.

MSO identifies the author, not severity. Known DE/EN diagnostics fail a run;
unrecognised system text needs review rather than silently passing. No word
filter is applied to user chat. This runs off the assistance thread.
"""

import codecs
import re

PACKETS = ('MSO', 'III', 'ACR')
# LID order and inline code-page switches: lfs.net/programmer/insim.
_PAGES = dict(zip('LETBJCGHSK',
                  ('cp1252', 'cp1250', 'cp1254', 'cp1257', 'cp932',
                   'cp1251', 'cp1253', 'cp950', 'cp936', 'cp949')))
_ERROR = re.compile(
    r'\b(?:ungültig\w*|ungueltig\w*|invalid|unknown|unbekannt\w*|'
    r'warnung\w*|warning\w*|fehler\w*|error\w*|failed|failure|'
    r'cannot|could not|not found|nicht gefunden|nicht möglich|'
    r'nicht erlaubt|abgelehnt|rejected)\b', re.IGNORECASE)
# Narrow informational forms observed in existing traces. Never whitelist an
# arbitrary system-message prefix: diagnostics can use that prefix too.
_INFO = re.compile(
    r'(?:Autocross: \d+ Kontrollpunkte?|BETA: \d+ checkpoints?|'
    r'Layout: [\w.-]+|.+ hat das Rennen gewonnen|.+ kommt als \d+\. ins Ziel)',
    re.IGNORECASE)


def message_text(value, mso_data=0):
    """Read both new packets and the reversible Msg representation in old traces."""
    if isinstance(value, dict):
        try:
            value = bytes.fromhex(value['hex'])
        except (KeyError, ValueError, TypeError):
            value = value.get('text', '')
    if isinstance(value, str):
        # Old trace strings are latin-1, not UTF-8. New Unicode text is supplied
        # separately by packet_dump and does not go through this conversion.
        value = value.encode('latin-1', errors='replace')
    if not isinstance(value, (bytes, bytearray)):
        return ''
    raw = bytes(value).split(b'\0', 1)[0]
    page = mso_data & 15
    codec = tuple(_PAGES.values())[page] if page < len(_PAGES) else 'cp1252'
    decoder = codecs.getincrementaldecoder(codec)(errors='replace')
    text = []
    i = 0
    while i < len(raw):
        # A DBCS trailing byte can equal '^'; only parse escapes between chars.
        if not decoder.getstate()[0] and raw[i:i + 1] == b'^' and i + 1 < len(raw):
            code = chr(raw[i + 1])
            if code in _PAGES:
                decoder = codecs.getincrementaldecoder(_PAGES[code])(errors='replace')
                i += 2
                continue
            if code in '0123456789':
                i += 2
                continue
        text.append(decoder.decode(raw[i:i + 1]))
        i += 1
    text.append(decoder.decode(b'', final=True))
    return ''.join(text)


def review(records, meta, end):
    """Return all chat, diagnostics, and unresolved messages with timestamps."""
    messages, diagnostics, unresolved = [], [], []
    for record in records:
        event, data = record.get('ev'), record.get('d', {})
        if event not in PACKETS:
            continue
        text = data.get('text')
        if not isinstance(text, str):
            text = message_text(data.get('Msg', data.get('Text', '')),
                                data.get('MSOData', 0))
        item = {'t': record['t'], 'event': event, 'text': text,
                'ucid': data.get('UCID'), 'plid': data.get('PLID'),
                'user_type': data.get('UserType'), 'result': data.get('Result')}
        messages.append(item)
        if data.get('decode_error') or not text:
            unresolved.append(item)
        elif event == 'ACR':
            if data.get('Result') in (2, 3):
                diagnostics.append(item)
            elif data.get('Result') != 1:
                unresolved.append(item)
        elif event == 'MSO' and data.get('UserType') == 0:
            if _ERROR.search(text):
                diagnostics.append(item)
            elif not _INFO.fullmatch(text):
                unresolved.append(item)
        elif event == 'MSO' and data.get('UserType') not in (1, 2, 3):
            unresolved.append(item)
    # Zero chat messages is valid. Prove subscription from metadata, not counts.
    complete = (set(PACKETS) <= set(meta.get('packets', [])) and end is not None
                and not end.get('dropped')
                and not any(r.get('ev') == 'warning' and
                            r.get('d', {}).get('where') == 'insim' for r in records))
    status = ('failed' if diagnostics else 'needs_review' if unresolved else
              'passed' if complete else 'incomplete')
    return {'status': status, 'capture_complete': complete, 'messages': messages,
            'diagnostics': diagnostics, 'unreviewed_system_messages': unresolved}
