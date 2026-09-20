"""Startup config diagnostics use temporary files and never modify LFS."""
from types import SimpleNamespace

import pytest

from core import outgauge_config
from core.setup_wizard import REQUIRED_CFG_SETTINGS


@pytest.fixture
def cfg(tmp_path):
    path = tmp_path / 'cfg.txt'
    path.write_text('\n'.join(f'{k} {v}' for k, v in REQUIRED_CFG_SETTINGS.items()))
    return path


def test_valid_cfg_is_accepted_without_changes(cfg):
    before = cfg.read_bytes()
    assert outgauge_config.inspect_cfg(cfg) == []
    assert cfg.read_bytes() == before


@pytest.mark.parametrize('key,value', [
    ('OutGauge Mode', '0'), ('OutGauge Port', '30011'),
    ('OutGauge IP', '192.168.1.2'), ('OutGauge Delay', '0'),
    ('OutGauge ID', 'garbage'),
])
def test_wrong_setting_names_the_exact_repair(cfg, key, value):
    cfg.write_text(cfg.read_text().replace(f'{key} {REQUIRED_CFG_SETTINGS[key]}',
                                           f'{key} {value}'))
    before = cfg.read_bytes()
    issues = outgauge_config.inspect_cfg(cfg)
    assert issues == [f'{key}: {value!r}; expected {REQUIRED_CFG_SETTINGS[key]}']
    assert cfg.read_bytes() == before


def test_missing_duplicate_and_empty_values_are_reported(cfg):
    cfg.write_text('OutGauge Mode 2\nOutGauge Mode 0\nOutGauge Port\n')
    issues = outgauge_config.inspect_cfg(cfg)
    assert len(issues) == 5
    assert any("'2', '0'" in issue for issue in issues)
    assert any("Port: ''" in issue for issue in issues)


def test_localized_unrelated_text_and_whitespace_do_not_break_validation(cfg):
    cfg.write_bytes(b'Player Name \xfc\n' + cfg.read_bytes().replace(b' ', b'\t'))
    assert outgauge_config.inspect_cfg(cfg) == []


def test_missing_file_is_explicit(tmp_path):
    issues = outgauge_config.inspect_cfg(tmp_path / 'missing.txt')
    assert len(issues) == 1
    assert 'Cannot read' in issues[0]


def test_running_installation_overrides_saved_directory(cfg, monkeypatch, bus, recorder):
    monkeypatch.setattr(outgauge_config.psutil, 'process_iter', lambda attrs: [
        SimpleNamespace(info={'name': 'LFS.exe', 'exe': str(cfg.parent / 'LFS.exe')})])
    seen = recorder('outgauge_config_checked')
    report = outgauge_config.validate_startup(bus, str(cfg.parent / 'other-install'))
    assert report == {'path': str(cfg), 'issues': []}
    assert seen.last('outgauge_config_checked') == report


@pytest.mark.parametrize('processes', [
    [{'name': 'LFS.exe', 'exe': None}],
    [{'name': 'LFS.exe', 'exe': '/one/LFS.exe'},
     {'name': 'LFS.exe', 'exe': '/two/LFS.exe'}],
])
def test_uncertain_installation_does_not_claim_saved_config_is_valid(
        cfg, monkeypatch, bus, processes):
    monkeypatch.setattr(outgauge_config.psutil, 'process_iter', lambda attrs: [
        SimpleNamespace(info=info) for info in processes])
    report = outgauge_config.validate_startup(bus, str(cfg.parent))
    assert report['path'] is None
    assert report['issues']


def test_read_error_is_reported(cfg, monkeypatch):
    def denied(self):
        raise PermissionError('denied')
    monkeypatch.setattr(type(cfg), 'read_bytes', denied)
    assert 'denied' in outgauge_config.inspect_cfg(cfg)[0]
