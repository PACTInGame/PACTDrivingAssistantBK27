"""Settings storage and the system-name ↔ settings-key contract.

``AssistanceSystem.is_enabled`` looks the system's own name up in the settings
(``assistance/base_system.py``), so a system whose name has no entry in
``SettingsManager._defaults`` is dead code that still costs a cycle.
"""

import json

import pytest

from assistance.manager import AssistanceManager
from core.settings_manager import SettingsManager


# ─── Storage ─────────────────────────────────────────────────────────────────

def test_missing_file_yields_the_defaults(tmp_path):
    settings = SettingsManager(settings_file=str(tmp_path / 'absent.json'))

    assert settings.get('language') == settings._defaults['language']
    assert settings.get('assistance_refresh_rate') == 100


def test_unknown_key_without_a_default_is_none(settings):
    assert settings.get('no_such_setting') is None


def test_explicit_default_is_returned_for_an_unknown_key(settings):
    assert settings.get('no_such_setting', 42) == 42


def test_set_persists_and_reloads(tmp_path):
    path = str(tmp_path / 'settings.json')
    settings = SettingsManager(settings_file=path)
    settings.set('language', 'fr')

    assert json.loads(open(path).read())['language'] == 'fr'
    assert SettingsManager(settings_file=path).get('language') == 'fr'


def test_corrupt_file_falls_back_to_defaults_without_raising(tmp_path):
    path = tmp_path / 'settings.json'
    path.write_text('{ this is not json')

    settings = SettingsManager(settings_file=str(path))

    assert settings.get('language') == settings._defaults['language']


# ─── System names ────────────────────────────────────────────────────────────

@pytest.fixture
def manager(bus, settings):
    """A real AssistanceManager -- construction alone must stay side-effect free."""
    return AssistanceManager(bus, settings)


def test_every_registered_system_has_a_unique_name(manager):
    names = [system.name for system in manager.systems.values()]

    assert len(names) == len(set(names))


@pytest.mark.xfail(strict=False, reason=(
    "'sat_nav' (NavigationSystem) has no entry in SettingsManager._defaults, so "
    "is_enabled() is always False (known-issues.md #18). WP6 either adds the key "
    "or unregisters the system, and removes this xfail."))
def test_every_system_name_is_a_settings_key(manager, settings):
    without_key = sorted(system.name for system in manager.systems.values()
                         if system.name not in settings._defaults)

    assert without_key == []


def test_a_system_with_a_settings_key_reports_the_settings_value(bus, make_settings):
    settings = make_settings(auto_hold=False)
    manager = AssistanceManager(bus, settings)

    assert manager.systems['autoh'].is_enabled() is False

    settings._settings['auto_hold'] = True
    assert manager.systems['autoh'].is_enabled() is True
