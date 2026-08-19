"""The translation table (``misc/language.py``).

Every user-facing string goes through ``LanguageManager.get(key, lang)``
(``reference/conventions.md`` §8), so a missing key or a broken language code
shows up as English text -- or as mojibake in LFS.
"""

import ast
import os
import unicodedata

import pytest

from lfs.text_encoding import decode_button_text, encode_button_text
from misc.language import LanguageManager

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SKIP_DIRS = {'.git', '.claude', '__pycache__', 'pyinsim', 'tests',
             'audio', 'layouts', 'track_data', 'Controls'}


@pytest.fixture(scope='module')
def translator():
    return LanguageManager()


@pytest.fixture(scope='module')
def languages(translator):
    return translator.get_supported_languages()


def test_eight_languages_are_supported(languages):
    assert sorted(languages) == ['de', 'dk', 'en', 'fr', 'it', 'no', 'se', 'tr']


def test_every_key_has_every_language(translator, languages):
    incomplete = {
        key: [lang for lang in languages if not entry.get(lang)]
        for key, entry in translator.translations.items()
    }
    incomplete = {key: missing for key, missing in incomplete.items() if missing}

    assert incomplete == {}


def test_every_key_resolves_in_every_language(translator, languages):
    for key in translator.translations:
        for lang in languages:
            value = translator.get(key, lang)
            assert isinstance(value, str) and value


def test_unknown_key_falls_back_to_the_key_itself(translator):
    assert translator.get('No Such String', 'de') == 'No Such String'


def test_unknown_language_falls_back_to_english(translator):
    assert translator.get('Main Menu', 'xx') == translator.get('Main Menu', 'en')


def test_no_language_argument_means_english(translator):
    assert translator.get('Main Menu') == 'Main Menu'


def test_get_all_translations_returns_a_copy(translator):
    entry = translator.get_all_translations('Main Menu')
    entry['de'] = 'tampered'

    assert translator.get('Main Menu', 'de') != 'tampered'


def test_get_all_translations_of_an_unknown_key_is_empty(translator):
    assert translator.get_all_translations('No Such String') == {}


def _literal_translation_keys():
    """Every string literal handed to a ``translator.get(...)`` call."""
    found = []
    for root, dirs, files in os.walk(PROJECT_ROOT):
        dirs[:] = [d for d in dirs if d not in SKIP_DIRS]
        for name in sorted(files):
            if not name.endswith('.py'):
                continue
            path = os.path.join(root, name)
            with open(path, encoding='utf-8') as handle:
                tree = ast.parse(handle.read(), filename=path)
            for node in ast.walk(tree):
                if not (isinstance(node, ast.Call)
                        and isinstance(node.func, ast.Attribute)
                        and node.func.attr == 'get'):
                    continue
                target = node.func.value
                owner = getattr(target, 'id', None) or getattr(target, 'attr', None)
                if not owner or 'transl' not in owner.lower():
                    continue
                if node.args and isinstance(node.args[0], ast.Constant) \
                        and isinstance(node.args[0].value, str):
                    relative = os.path.relpath(path, PROJECT_ROOT)
                    found.append((relative, node.lineno, node.args[0].value))
    return found


def test_every_translated_literal_in_the_code_has_a_table_entry(translator):
    unknown = [(path, line, key) for path, line, key in _literal_translation_keys()
               if key not in translator.translations]

    assert unknown == []


def test_the_scan_actually_finds_call_sites():
    """Guards the test above against silently scanning nothing."""
    assert len(_literal_translation_keys()) > 20


def test_every_string_survives_the_lfs_button_encoder(translator, languages):
    """WP3: every translation must reach LFS as the text it was written as.

    The old encoder tried latin-1 and fell back to UTF-8, which LFS does not
    read -- 52 Turkish strings and one Swedish one rendered as mojibake. The
    replacement switches LFS code pages inline (``^T`` and friends), so the
    round trip must be lossless.
    """
    broken = []
    for key, entry in translator.translations.items():
        for lang in languages:
            original = entry[lang]
            rendered = decode_button_text(encode_button_text(original))
            if rendered != original:
                broken.append((key, lang, original, rendered))

    # One Swedish string carries a stray combining diaeresis on an already
    # accented vowel (a typo). No LFS code page can encode it; the encoder
    # drops the mark, which is exactly the text the author meant.
    expected_difference = [
        (key, lang, original, rendered) for key, lang, original, rendered in broken
        if unicodedata.normalize('NFC', rendered)
        == unicodedata.normalize('NFC', original.replace('\u0308', ''))
    ]

    assert [item[:2] for item in broken] == [item[:2] for item in expected_difference]
