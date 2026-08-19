"""The translation table (``misc/language.py``).

Every user-facing string goes through ``LanguageManager.get(key, lang)``
(``reference/conventions.md`` §8), so a missing key or a broken language code
shows up as English text -- or as mojibake in LFS.
"""

import ast
import os

import pytest

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


@pytest.mark.xfail(strict=False, reason=(
    "LFS button text is latin-1; 52 Turkish strings and one Swedish string are "
    "not encodable and render as mojibake. WP3 replaces the encoder with LFS "
    "code-page escapes (^E, ^T, ...) -- this test passes once it does."))
def test_every_string_is_encodable_for_lfs_buttons(translator, languages):
    unencodable = []
    for key, entry in translator.translations.items():
        for lang in languages:
            try:
                entry[lang].encode('latin-1')
            except (UnicodeEncodeError, AttributeError):
                unencodable.append((key, lang, entry[lang]))

    assert unencodable == []
