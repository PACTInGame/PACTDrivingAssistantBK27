"""Read-only startup validation of the running LFS installation's OutGauge setup."""
import logging
from pathlib import Path

import psutil

from core.setup_wizard import REQUIRED_CFG_SETTINGS

logger = logging.getLogger(__name__)


def show_startup_warning(report):
    """Visible setup guidance before connection; never called from a cycle."""
    if not report.get('issues'):
        return
    from misc.platform_shim import get_tkinter
    tk = get_tkinter()
    if not tk:
        return
    root = None
    try:
        root = tk.Tk()
        root.withdraw()
        tk.messagebox.showwarning(
            'LFS OutGauge configuration',
            'The saved LFS configuration needs attention:\n\n'
            + str(report.get('path') or 'Unknown LFS installation') + '\n'
            + '\n'.join(report['issues'])
            + '\n\nClose LFS before changing cfg.txt, otherwise LFS overwrites '
              'the changes on exit. Run PACTDrivingAssistant.exe --setup '
              'to repair it, then restart LFS.\n\n'
              'If you intentionally use a telemetry relay, check its forwarding '
              'to UDP 30000 instead.', parent=root)
    except Exception:
        logger.exception('Could not display the OutGauge setup warning')
    finally:
        if root is not None:
            root.destroy()


def inspect_cfg(path):
    """Return setup differences; disk contents do not prove live stream health."""
    path = Path(path)
    try:
        # Only ASCII setting names/values matter; LFS files can contain local text.
        lines = path.read_bytes().decode('utf-8-sig', errors='replace').splitlines()
    except OSError as exc:
        return [f"Cannot read {path}: {exc}"]
    expected = {key: value for key, value in REQUIRED_CFG_SETTINGS.items()
                if key.startswith('OutGauge ')}
    values = {key: [] for key in expected}
    for line in lines:
        fields = line.split()
        key = ' '.join(fields[:2])
        if key in values:
            values[key].append(' '.join(fields[2:]))
    issues = []
    for key, wanted in expected.items():
        found = values[key]
        if len(found) != 1 or found[0] != wanted:
            actual = ', '.join(repr(value) for value in found) if found else 'missing'
            issues.append(f"{key}: {actual}; expected {wanted}")
    return issues


def validate_startup(event_bus, configured_directory):
    """One process scan and one file read per startup; never on a cycle thread.

    Prefer the running executable over a potentially stale saved install path.
    Multiple or inaccessible installations are inconclusive, not permission to
    validate a different copy of LFS. Never edit cfg.txt while LFS is running.
    """
    paths = set()
    inaccessible = False
    try:
        for proc in psutil.process_iter(['name', 'exe']):
            try:
                if (proc.info.get('name') or '').lower() != 'lfs.exe':
                    continue
                executable = proc.info.get('exe')
                if executable:
                    paths.add(Path(executable).parent / 'cfg.txt')
                else:
                    inaccessible = True
            except (psutil.AccessDenied, psutil.NoSuchProcess):
                inaccessible = True
    except psutil.Error as exc:
        logger.warning("Cannot locate the running LFS installation: %s", exc)
        inaccessible = True

    path = None
    if inaccessible or len(paths) > 1:
        issues = ['Cannot uniquely identify the running LFS installation; '
                  'check its cfg.txt OutGauge settings manually.']
    else:
        path = next(iter(paths)) if paths else Path(configured_directory) / 'cfg.txt'
        issues = inspect_cfg(path)

    report = {'path': str(path) if path else None, 'issues': issues}
    if issues:
        logger.warning(
            "OutGauge startup configuration check (%s): %s. "
            "Close LFS before correcting cfg.txt, then restart LFS and the add-on. "
            "This checks saved settings only; live telemetry is checked separately.",
            path or 'unknown installation', '; '.join(issues))
    else:
        logger.info("OutGauge startup configuration verified: %s", path)
    event_bus.emit('outgauge_config_checked', report)
    return report
