"""Settings storage and the system-name ↔ settings-key contract.

``AssistanceSystem.is_enabled`` looks the system's own name up in the settings
(``assistance/base_system.py``), so a system whose name has no entry in
``SettingsManager._defaults`` is dead code that still costs a cycle.
"""

import json
import logging
import os

import pytest

from assistance.manager import AssistanceManager
from core.settings_manager import SETTINGS_VERSION, SettingsManager


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
    settings.flush()          # writes are debounced, see SAVE_DEBOUNCE_S

    assert json.loads(open(path).read())['language'] == 'fr'
    assert SettingsManager(settings_file=path).get('language') == 'fr'


def test_corrupt_file_falls_back_to_defaults_without_raising(tmp_path):
    path = tmp_path / 'settings.json'
    path.write_text('{ this is not json')

    settings = SettingsManager(settings_file=str(path))

    assert settings.get('language') == settings._defaults['language']


# ─── Merging, migration and unknown keys (WP6) ───────────────────────────────

def write_settings(tmp_path, payload) -> str:
    path = tmp_path / 'settings.json'
    path.write_text(json.dumps(payload))
    return str(path)


def test_an_older_file_gains_new_keys_and_keeps_the_user_values(tmp_path):
    """The defect this fixes: a key added in a later version was never merged
    in, and ``get(name, False)`` then returned the caller's False -- so every
    newly added assistance system was off forever for existing users."""
    path = write_settings(tmp_path, {'language': 'fr', 'auto_hold': False})

    settings = SettingsManager(settings_file=path)

    assert settings.get('language') == 'fr'          # user value survives
    assert settings.get('auto_hold') is False
    assert settings.get('ai_traffic') is True        # key the file never had
    assert settings.get('ai_traffic', False) is True  # and no shadowing default


def test_unknown_keys_from_a_newer_version_are_kept(tmp_path):
    path = write_settings(tmp_path, {'from_the_future': 'keep me'})

    settings = SettingsManager(settings_file=path)
    settings.flush()

    assert settings.get('from_the_future') == 'keep me'
    assert json.loads(open(path).read())['from_the_future'] == 'keep me'


def test_the_file_carries_a_version(tmp_path):
    path = str(tmp_path / 'settings.json')
    SettingsManager(settings_file=path).flush()

    assert json.loads(open(path).read())['_version'] == SETTINGS_VERSION


def test_a_version_zero_pdc_pair_migrates_to_the_mode_alone(tmp_path):
    """v0 stored a boolean *and* a mode, which could contradict each other --
    and the shipped default did: 'on' with mode 'off'."""
    path = write_settings(tmp_path, {'park_distance_control': True,
                                     'park_distance_control_mode': 0})

    settings = SettingsManager(settings_file=path)
    settings.flush()

    assert settings.get('park_distance_control_mode') != 0
    assert settings.get('park_distance_control') is True
    assert 'park_distance_control' not in json.loads(open(path).read())


def test_migration_keeps_a_deliberate_pdc_choice(tmp_path):
    path = write_settings(tmp_path, {'park_distance_control': True,
                                     'park_distance_control_mode': 2})

    assert SettingsManager(settings_file=path).get('park_distance_control_mode') == 2


def test_migration_keeps_pdc_switched_off(tmp_path):
    path = write_settings(tmp_path, {'park_distance_control': False,
                                     'park_distance_control_mode': 2})

    settings = SettingsManager(settings_file=path)

    assert settings.get('park_distance_control_mode') == 0
    assert settings.get('park_distance_control') is False


# ─── Validation (WP6) ────────────────────────────────────────────────────────

def test_a_hand_edited_zero_refresh_rate_is_clamped_and_logged(tmp_path, caplog):
    """0 ms turned the assistance thread into a busy loop and went to LFS as
    the MCI Interval."""
    path = write_settings(tmp_path, {'assistance_refresh_rate': 0})

    with caplog.at_level(logging.WARNING):
        settings = SettingsManager(settings_file=path)

    assert settings.get('assistance_refresh_rate') == 50
    assert 'assistance_refresh_rate' in caplog.text


def test_an_out_of_range_value_is_clamped_at_the_top(tmp_path):
    path = write_settings(tmp_path, {'assistance_refresh_rate': 100000})

    assert SettingsManager(settings_file=path).get('assistance_refresh_rate') == 200


def test_a_value_outside_the_allowed_choices_falls_back_to_the_default(tmp_path, caplog):
    path = write_settings(tmp_path, {'collision_warning_distance': 7,
                                     'language': 'klingon'})

    with caplog.at_level(logging.WARNING):
        settings = SettingsManager(settings_file=path)

    assert settings.get('collision_warning_distance') == 1
    assert settings.get('language') == 'de'
    assert 'klingon' in caplog.text


def test_a_wrongly_typed_value_falls_back_to_the_default(tmp_path):
    path = write_settings(tmp_path, {'auto_hold': "yes please",
                                     'hud_width': "far left"})

    settings = SettingsManager(settings_file=path)

    assert settings.get('auto_hold') is True
    assert settings.get('hud_width') == 90


def test_a_boolean_is_not_accepted_where_a_number_belongs(tmp_path):
    path = write_settings(tmp_path, {'collision_warning_distance': True})

    assert SettingsManager(settings_file=path).get('collision_warning_distance') == 1


def test_set_validates_too(settings):
    settings.set('assistance_refresh_rate', 5)

    assert settings.get('assistance_refresh_rate') == 50


def test_a_file_that_is_not_an_object_falls_back_to_defaults(tmp_path):
    path = tmp_path / 'settings.json'
    path.write_text('[1, 2, 3]')

    assert SettingsManager(settings_file=str(path)).get('language') == 'de'


def test_a_corrupt_file_is_kept_instead_of_being_overwritten(tmp_path):
    path = tmp_path / 'settings.json'
    path.write_text('{ this is not json')

    SettingsManager(settings_file=str(path))

    assert (tmp_path / 'settings.json.corrupt').read_text() == '{ this is not json'


# ─── Writing (WP6) ───────────────────────────────────────────────────────────

def test_writes_are_debounced_not_one_per_click(tmp_path):
    """Menu clicks run on the packet thread; one file write per click was
    blocking I/O in the hot path."""
    path = str(tmp_path / 'settings.json')
    settings = SettingsManager(settings_file=path)
    settings.flush()
    written = os.path.getmtime(path)

    for step in range(10):
        settings.set('hud_width', 100 + step)

    assert os.path.getmtime(path) == written      # nothing written yet
    settings.flush()
    assert json.loads(open(path).read())['hud_width'] == 109


def test_setting_a_value_that_is_already_stored_does_not_dirty_the_file(settings):
    settings.flush()
    settings.set('language', settings.get('language'))

    assert settings._dirty is False


def test_the_write_is_atomic(tmp_path, monkeypatch):
    """A failed write must not leave a truncated settings.json behind."""
    path = str(tmp_path / 'settings.json')
    settings = SettingsManager(settings_file=path)
    settings.set('language', 'it')
    settings.flush()

    def explode(*args, **kwargs):
        raise OSError("disk full")

    monkeypatch.setattr(json, 'dump', explode)
    settings.set('language', 'fr')
    settings.flush()

    assert json.loads(open(path).read())['language'] == 'it'
    assert not os.path.exists(path + '.tmp')


# ─── System names ────────────────────────────────────────────────────────────

@pytest.fixture
def manager(bus, settings):
    """A real AssistanceManager -- construction alone must stay side-effect free."""
    return AssistanceManager(bus, settings)


def test_every_registered_system_has_a_unique_name(manager):
    names = [system.name for system in manager.systems.values()]

    assert len(names) == len(set(names))


def test_every_system_name_is_a_settings_key(manager, settings):
    """A system whose name is not a known settings key can never be enabled.

    ``known_keys`` covers stored and derived keys -- ``park_distance_control``
    is derived from ``park_distance_control_mode`` and has no stored value.
    """
    without_key = sorted(system.name for system in manager.systems.values()
                         if system.name not in settings.known_keys)

    assert without_key == []


def test_a_system_with_a_settings_key_reports_the_settings_value(bus, make_settings):
    settings = make_settings(auto_hold=False)
    manager = AssistanceManager(bus, settings)

    assert manager.systems['autoh'].is_enabled() is False

    settings._settings['auto_hold'] = True
    assert manager.systems['autoh'].is_enabled() is True
