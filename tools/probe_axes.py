"""Which API can read the wheel, and from which thread?

Runs for 15 seconds. Move the steering wheel from lock to lock and press the
throttle and the brake fully a couple of times while it runs.

Prints, for every axis of every device, the range it was seen to travel:
an axis that moved has a range well above 0, one that was never read at all
stays at exactly 0.
"""

import ctypes
import ctypes.wintypes as wt
import os
import threading
import time

DURATION = 15.0

os.environ.setdefault('SDL_JOYSTICK_ALLOW_BACKGROUND_EVENTS', '1')


# ─── SDL, on whichever thread we are called from ─────────────────────────────

def sdl_sample(label, results):
    import pygame
    pygame.display.init()
    pygame.joystick.init()
    sticks = []
    for index in range(pygame.joystick.get_count()):
        stick = pygame.joystick.Joystick(index)
        stick.init()
        sticks.append(stick)
    names = [(stick.get_name(), stick.get_numaxes()) for stick in sticks]
    lows, highs = {}, {}
    end = time.time() + DURATION
    while time.time() < end:
        pygame.event.pump()
        flat = 0
        for stick in sticks:
            for axis in range(stick.get_numaxes()):
                value = stick.get_axis(axis)
                lows[flat] = min(lows.get(flat, value), value)
                highs[flat] = max(highs.get(flat, value), value)
                flat += 1
        time.sleep(0.02)
    results[label] = (names, lows, highs)


# ─── winmm, which has no thread rules at all ─────────────────────────────────

class JOYCAPS(ctypes.Structure):
    _fields_ = [("wMid", wt.WORD), ("wPid", wt.WORD),
                ("szPname", ctypes.c_wchar * 32),
                ("wXmin", wt.UINT), ("wXmax", wt.UINT),
                ("wYmin", wt.UINT), ("wYmax", wt.UINT),
                ("wZmin", wt.UINT), ("wZmax", wt.UINT),
                ("wNumButtons", wt.UINT),
                ("wPeriodMin", wt.UINT), ("wPeriodMax", wt.UINT),
                ("wRmin", wt.UINT), ("wRmax", wt.UINT),
                ("wUmin", wt.UINT), ("wUmax", wt.UINT),
                ("wVmin", wt.UINT), ("wVmax", wt.UINT),
                ("wCaps", wt.UINT), ("wMaxAxes", wt.UINT), ("wNumAxes", wt.UINT),
                ("wMaxButtons", wt.UINT), ("szRegKey", ctypes.c_wchar * 32),
                ("szOEMVxD", ctypes.c_wchar * 260)]


class JOYINFOEX(ctypes.Structure):
    _fields_ = [("dwSize", wt.DWORD), ("dwFlags", wt.DWORD),
                ("dwXpos", wt.DWORD), ("dwYpos", wt.DWORD), ("dwZpos", wt.DWORD),
                ("dwRpos", wt.DWORD), ("dwUpos", wt.DWORD), ("dwVpos", wt.DWORD),
                ("dwButtons", wt.DWORD), ("dwButtonNumber", wt.DWORD),
                ("dwPOV", wt.DWORD), ("dwReserved1", wt.DWORD),
                ("dwReserved2", wt.DWORD)]


def winmm_sample(results):
    winmm = ctypes.WinDLL('winmm')
    names = []
    for device in range(winmm.joyGetNumDevs()):
        caps = JOYCAPS()
        if winmm.joyGetDevCapsW(device, ctypes.byref(caps),
                                ctypes.sizeof(caps)) == 0:
            names.append((device, caps.szPname, caps.wNumAxes))
    lows, highs = {}, {}
    end = time.time() + DURATION
    while time.time() < end:
        for device, _name, _axes in names:
            info = JOYINFOEX()
            info.dwSize = ctypes.sizeof(info)
            info.dwFlags = 0x000000FF
            if winmm.joyGetPosEx(device, ctypes.byref(info)) != 0:
                continue
            for index, value in enumerate([info.dwXpos, info.dwYpos, info.dwZpos,
                                           info.dwRpos, info.dwUpos, info.dwVpos]):
                key = (device, index)
                lows[key] = min(lows.get(key, value), value)
                highs[key] = max(highs.get(key, value), value)
        time.sleep(0.02)
    results['winmm'] = (names, lows, highs)


def report(label, names, lows, highs, scale=1.0):
    print(f"\n=== {label} ===")
    print("  devices:", names)
    moved = 0
    for key in sorted(lows):
        span = (highs[key] - lows[key]) / scale
        flag = "MOVED" if span > 0.05 else "     "
        if span > 0.05:
            moved += 1
        print(f"  {flag} axis {key}: {lows[key]/scale:+.3f} .. "
              f"{highs[key]/scale:+.3f}   span {span:.3f}")
    print(f"  -> {moved} axes moved")


if __name__ == '__main__':
    print(__doc__)
    print("Starting in 3 seconds - then move the wheel and both pedals.")
    time.sleep(3.0)
    print("GO - 15 seconds.")

    # SDL is process-global, so it is only ever initialised once here, on the
    # main thread. winmm has no such rule and runs alongside on its own thread,
    # which is exactly the question being asked of it.
    results = {}
    winmm_thread = threading.Thread(target=winmm_sample, args=(results,))
    winmm_thread.start()
    sdl_sample('SDL on the main thread', results)
    winmm_thread.join()

    if 'SDL on the main thread' in results:
        report('SDL on the main thread', *results['SDL on the main thread'])
    if 'winmm' in results:
        report('winmm joyGetPosEx (any thread)', *results['winmm'], scale=65535.0)
