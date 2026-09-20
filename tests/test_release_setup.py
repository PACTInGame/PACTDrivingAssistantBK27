"""Fresh install/update regressions without LFS, controllers or GUI windows."""
import json
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

from core import setup_wizard as setup
from core.settings_manager import SettingsManager
from misc import helpers


@pytest.fixture
def closed_lfs(monkeypatch):
    monkeypatch.setattr(setup, '_is_lfs_running', lambda: False)


def test_cfg_preserves_local_bytes_removes_duplicates_and_keeps_backup(tmp_path, closed_lfs):
    path = tmp_path / 'cfg.txt'
    original = b'Player M\xfcller\r\nOutGauge Mode 0\r\nOutGauge Mode 1\r\n'
    path.write_bytes(original)
    setup.apply_cfg_settings(str(path))
    from core.outgauge_config import inspect_cfg
    assert inspect_cfg(path) == []
    assert b'Player M\xfcller\r\n' in path.read_bytes()
    assert Path(str(path) + '.pact-backup').read_bytes() == original
    setup.apply_cfg_settings(str(path))
    assert Path(str(path) + '.pact-backup').read_bytes() == original


def test_lfs_started_after_selection_prevents_cfg_write(tmp_path, monkeypatch):
    path = tmp_path / 'cfg.txt'
    path.write_bytes(b'OutGauge Mode 0\n')
    monkeypatch.setattr(setup, '_is_lfs_running', lambda: True)
    with pytest.raises(RuntimeError, match='Close LFS'):
        setup.apply_cfg_settings(str(path))
    assert path.read_bytes() == b'OutGauge Mode 0\n'


def test_autoexec_ignores_comments_and_removes_conflicting_ports(tmp_path, closed_lfs):
    path = tmp_path / 'data/script/autoexec.lfs'
    path.parent.mkdir(parents=True)
    path.write_text('// /insim 29999\n/insim 299990\n/insim 0\n/echo hello', encoding='utf-8')
    setup.add_insim_autoexec(str(tmp_path))
    setup.add_insim_autoexec(str(tmp_path))
    commands = [s for s in path.read_text().splitlines() if s.startswith('/insim')]
    assert commands == ['/insim 29999']
    assert '/echo hello' in path.read_text()


def test_frozen_updates_keep_settings_outside_two_resource_directories(tmp_path, monkeypatch):
    monkeypatch.setattr(sys, 'frozen', True, raising=False)
    monkeypatch.setenv('LOCALAPPDATA', str(tmp_path / 'profile'))
    monkeypatch.setattr(sys, '_MEIPASS', str(tmp_path / 'version1/_internal'), raising=False)
    first = SettingsManager()
    first.set('language', 'de')
    first.flush()
    monkeypatch.setattr(sys, '_MEIPASS', str(tmp_path / 'version2/_internal'))
    second = SettingsManager()
    assert second.get('language') == 'de'
    assert first.settings_file == second.settings_file
    assert helpers.resolve_path('audio').endswith(str(Path('version2/_internal/audio')))
    assert str(tmp_path / 'profile') in setup._get_flag_file_path()


def test_frozen_guardian_is_a_separate_mode_with_persistent_paths(tmp_path, monkeypatch):
    from Controls import brake_axis
    calls = []
    monkeypatch.setattr(sys, 'frozen', True, raising=False)
    monkeypatch.setenv('LOCALAPPDATA', str(tmp_path))
    monkeypatch.setattr(brake_axis.subprocess, 'Popen',
                        lambda args, **kwargs: calls.append(args) or SimpleNamespace(pid=456))
    assert brake_axis._spawn_guardian(123).pid == 456
    assert calls[0][:3] == [sys.executable, '--guardian', '123']
    assert calls[0][3:] == [helpers.resolve_data_path('settings.json'),
                           helpers.resolve_data_path('brake_axis_held.marker')]


def test_guardian_dispatch_never_constructs_the_app(monkeypatch):
    import guardian
    import release_main
    seen = []
    monkeypatch.setattr(guardian, 'main', lambda args: seen.append(args) or 0)
    assert release_main.main(['--guardian', '123', 'settings', 'marker']) == 0
    assert seen == [['123', 'settings', 'marker']]


def test_cancelling_setup_never_continues_to_connection(monkeypatch):
    monkeypatch.setattr(setup, 'is_first_run', lambda: True)
    monkeypatch.setattr(setup, 'get_tkinter', lambda: True)
    monkeypatch.setattr(setup, 'SetupWizard',
                        lambda settings: SimpleNamespace(run=lambda: None, completed=False))
    with pytest.raises(SystemExit, match='cancelled'):
        setup.run_setup_if_needed()


def test_failed_settings_write_remains_pending_for_retry(tmp_path, monkeypatch):
    settings = SettingsManager(str(tmp_path / 'settings.json'))
    settings.set('language', 'de')
    with monkeypatch.context() as patch:
        patch.setattr(json, 'dump', lambda *a, **k: (_ for _ in ()).throw(OSError('full')))
        settings.flush()
    assert settings._dirty
    settings.flush()
    assert json.loads(Path(settings.settings_file).read_text())['language'] == 'de'
    assert not settings._dirty


def test_downgrade_does_not_rewrite_future_settings(tmp_path):
    path = tmp_path / 'settings.json'
    content = '{"_version": 999, "future_option": "preserve"}'
    path.write_text(content)
    with pytest.raises(RuntimeError, match='newer PACT version'):
        SettingsManager(str(path))
    assert path.read_text() == content


def test_outgauge_warning_explains_lfs_must_be_closed(monkeypatch):
    from core.outgauge_config import show_startup_warning
    from misc import platform_shim
    shown = []
    root = SimpleNamespace(withdraw=lambda: None, destroy=lambda: None)
    tk = SimpleNamespace(Tk=lambda: root, messagebox=SimpleNamespace(
        showwarning=lambda title, message, **kwargs: shown.append(message)))
    monkeypatch.setattr(platform_shim, 'get_tkinter', lambda: tk)
    show_startup_warning({'path': 'D:/LFS/cfg.txt', 'issues': ['OutGauge Mode: missing']})
    assert 'Close LFS' in shown[0]
    assert 'D:/LFS/cfg.txt' in shown[0]
    assert '--setup' in shown[0]
