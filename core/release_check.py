"""Offline frozen-package check: no LFS, input hooks or device acquisition."""
import importlib
import json
from pathlib import Path

from misc.helpers import resolve_path


def smoke_test():
    for module in ('main', 'guardian', 'tkinter.filedialog', 'tkinter.messagebox',
                   'pygame.mixer', 'pygame.joystick', 'pyautogui',
                   'pynput.keyboard._win32', 'pynput.mouse._win32', 'shapely'):
        importlib.import_module(module)
    for folder, suffix in (('audio', '.wav'), ('layouts', '.lyt'), ('track_data', '.json')):
        files = list(Path(resolve_path(folder)).glob('*' + suffix))
        if not files:
            raise RuntimeError(f'Missing bundled {folder}')
        if suffix == '.json':
            for path in files:
                json.loads(path.read_text(encoding='utf-8'))
    if not Path(resolve_path('RELEASE.md')).is_file():
        raise RuntimeError('Missing release instructions')
    print('PASS: runtime imports, Tk dialogs, input backends, audio, layouts and routes')
    return 0
