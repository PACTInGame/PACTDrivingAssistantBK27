"""Windows input adapter; hooks exist only during explicit record/replay runs."""
import ctypes
import sys
import threading
from ctypes import wintypes


class Desktop:
    def __init__(self, clock):
        if sys.platform != "win32":
            raise RuntimeError("Recording and replay require Windows")
        # Shared lazy dependency loader only; no running add-on is required.
        from misc.platform_shim import get_input_listener
        self.api = get_input_listener()
        if not self.api:
            raise RuntimeError("Install pynput; input cannot use a no-op backend")
        self.user = ctypes.WinDLL("user32", use_last_error=True)
        self.kernel = ctypes.WinDLL("kernel32", use_last_error=True)
        self.user.GetForegroundWindow.restype = wintypes.HWND
        self.user.GetKeyboardLayout.restype = wintypes.HANDLE
        self.user.GetWindowThreadProcessId.argtypes = (wintypes.HWND, ctypes.POINTER(wintypes.DWORD))
        self.user.GetClientRect.argtypes = (wintypes.HWND, ctypes.POINTER(wintypes.RECT))
        self.user.ClientToScreen.argtypes = (wintypes.HWND, ctypes.POINTER(wintypes.POINT))
        self.kernel.OpenProcess.argtypes = (wintypes.DWORD, wintypes.BOOL, wintypes.DWORD)
        self.kernel.OpenProcess.restype = wintypes.HANDLE
        self.kernel.QueryFullProcessImageNameW.argtypes = (wintypes.HANDLE, wintypes.DWORD,
                                                          wintypes.LPWSTR, ctypes.POINTER(wintypes.DWORD))
        self.kernel.CloseHandle.argtypes = (wintypes.HANDLE,)
        self.user.SetProcessDPIAware()
        self.clock = clock
        self.lock = threading.Lock()
        self.pending = []
        self.physical = set()
        self.held = set()
        self.abort = False
        self.stop = False
        self.keyboard = self.api.keyboard.Controller()
        self.special_keys = {key.value.vk: key for key in self.api.keyboard.Key}
        self.mouse = self.api.mouse.Controller()
        self.listeners = []

    def environment(self):
        hwnd = self.user.GetForegroundWindow()
        pid = wintypes.DWORD()
        thread_id = self.user.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
        handle = self.kernel.OpenProcess(0x1000, False, pid.value)
        if not handle:
            raise RuntimeError("Cannot identify foreground process")
        try:
            length = wintypes.DWORD(32768)
            name = ctypes.create_unicode_buffer(length.value)
            if not self.kernel.QueryFullProcessImageNameW(handle, 0, name, ctypes.byref(length)):
                raise RuntimeError("Cannot identify foreground executable")
            if name.value.replace("\\", "/").rsplit("/", 1)[-1].lower() != "lfs.exe":
                raise RuntimeError("LFS.exe must remain in the foreground")
        finally:
            self.kernel.CloseHandle(handle)
        rect, point = wintypes.RECT(), wintypes.POINT()
        if not self.user.GetClientRect(hwnd, ctypes.byref(rect)) or not self.user.ClientToScreen(hwnd, ctypes.byref(point)):
            raise RuntimeError("Cannot read LFS window geometry")
        return {"client": [point.x, point.y, rect.right, rect.bottom],
                "screen": [self.user.GetSystemMetrics(0), self.user.GetSystemMetrics(1)],
                "keyboard_layout": self.user.GetKeyboardLayout(thread_id)}

    def enqueue(self, kind, **data):
        with self.lock:
            self.pending.append(dict(t=self.clock(), kind=kind, **data))

    def key(self, key, down):
        vk = getattr(key, "vk", None)
        if vk is None:
            vk = getattr(getattr(key, "value", None), "vk", None)
        if vk is None:
            self.abort = True  # An unrepresentable key cannot silently disappear.
            return
        if vk in (121, 122, 123):
            if down:
                if vk == 121:
                    self.enqueue("marker", label="manual marker")
                elif vk == 122:
                    self.stop = True
                else:
                    self.abort = True
            return
        self.transition("key", vk, down)

    def transition(self, kind, code, down):
        token = (kind, code)
        with self.lock:
            if down == (token in self.physical):
                return  # Ignore OS auto-repeat; replay holds the original key.
            if down:
                self.physical.add(token)
            else:
                self.physical.discard(token)
            self.pending.append(dict(t=self.clock(), kind=kind, code=code, down=down))

    def click(self, x, y, button, down):
        self.enqueue("move", x=int(x), y=int(y))
        self.transition("button", button.name, down)

    def scroll(self, x, y, dx, dy):
        self.enqueue("move", x=int(x), y=int(y))
        self.enqueue("scroll", x=int(dx), y=int(dy))

    def start(self):
        # Filter injected events out of callbacks, including the add-on's inputs.
        # Returning False here does NOT suppress the event in other applications.
        self.listeners = [
            self.api.keyboard.Listener(on_press=lambda k: self.key(k, True),
                                       on_release=lambda k: self.key(k, False),
                                       win32_event_filter=lambda msg, data: not data.flags & 0x10),
            self.api.mouse.Listener(on_click=self.click, on_scroll=self.scroll,
                                    win32_event_filter=lambda msg, data: not data.flags & 1)]
        for listener in self.listeners:
            listener.start()
            listener.wait()

    def neutral(self):
        if any(self.user.GetAsyncKeyState(vk) & 0x8000 for vk in range(1, 256)):
            raise RuntimeError("Release all keys and mouse buttons before the countdown ends")
        with self.lock:
            self.pending.clear()
            self.physical.clear()

    def guard(self, expected, replay=False):
        if self.abort or self.environment() != expected:
            raise RuntimeError("Aborted, focus lost, or LFS window geometry changed")
        if any(not listener.running for listener in self.listeners):
            raise RuntimeError("Input listener stopped unexpectedly")
        if replay:
            with self.lock:
                if self.physical or any(e["kind"] in ("key", "button", "scroll") for e in self.pending):
                    raise RuntimeError("Physical input interrupted replay")
            if self.stop:
                raise RuntimeError("Replay stopped by F11")

    def drain(self):
        with self.lock:
            events, self.pending = self.pending, []
        return events

    def apply(self, event):
        kind = event["kind"]
        if kind == "move":
            x, y, width, height = self.environment()["client"]
            if not x <= event["x"] < x + width or not y <= event["y"] < y + height:
                raise RuntimeError("Recorded mouse position is outside the LFS client")
            self.mouse.position = (event["x"], event["y"])
        elif kind == "scroll":
            self.mouse.scroll(event["x"], event["y"])
        else:
            token = (kind, event["code"])
            control, value = self.target(token)
            if event["down"]:
                self.held.add(token)  # Track even if injection raises halfway through.
                control.press(value)
            else:
                control.release(value)
                self.held.discard(token)

    def target(self, token):
        kind, code = token
        if kind == "key":
            # Preserve extended-key flags (arrows, right Ctrl, etc.). A bare VK
            # would turn some keys into their non-extended keypad counterparts.
            return self.keyboard, self.special_keys.get(code, self.api.keyboard.KeyCode.from_vk(code))
        return self.mouse, getattr(self.api.mouse.Button, code)

    def release(self):
        errors = []
        for token in list(self.held):
            try:
                # Do not release a key that the human has taken over physically.
                if token not in self.physical:
                    control, value = self.target(token)
                    control.release(value)
                self.held.discard(token)
            except Exception as exc:
                errors.append(str(exc))
        if errors:
            raise RuntimeError("Input cleanup failed: " + "; ".join(errors))

    def close(self):
        try:
            self.release()
        finally:
            for listener in self.listeners:
                listener.stop()
            for listener in self.listeners:
                listener.join(timeout=1)
