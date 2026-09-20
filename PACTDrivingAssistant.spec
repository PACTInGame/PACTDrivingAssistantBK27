# Build with Python 3.11: python -m PyInstaller --noconfirm PACTDrivingAssistant.spec
from pathlib import Path
import sys
import shutil

if sys.version_info[:2] != (3, 11):
    raise RuntimeError('Build with Python 3.11 (asyncore transport)')
root = Path(SPECPATH)
datas = [(str(root / name), name) for name in ('audio', 'layouts', 'track_data')]
datas += [(str(root / 'RELEASE.md'), '.')]
a = Analysis([str(root / 'release_main.py')], pathex=[str(root)], datas=datas,
             hiddenimports=['pyautogui', 'pynput.keyboard._win32', 'pynput.mouse._win32',
                            'pygame.mixer', 'pygame.joystick', 'pygame.display',
                            'tkinter.filedialog', 'tkinter.messagebox', 'winsound'],
             excludes=['scipy', 'matplotlib', 'pytest'])
pyz = PYZ(a.pure)
exe = EXE(pyz, a.scripts, [], exclude_binaries=True, name='PACTDrivingAssistant',
          console=True, upx=False)
coll = COLLECT(exe, a.binaries, a.datas, strip=False, upx=False,
               name='PACTDrivingAssistant')
shutil.copy2(root / 'RELEASE.md', Path(DISTPATH) / 'PACTDrivingAssistant' / 'RELEASE.md')
