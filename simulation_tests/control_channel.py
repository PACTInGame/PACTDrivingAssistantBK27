"""Loopback UDP control channel between the runner/replay and the tracer.

The replay and the tracer are separate processes on purpose (the tracer must
keep its own InSim connection and asyncore loop). They still need one shared
timeline, so the replay pushes **markers** into the tracer, which stamps them
with its own trace clock. That is what makes "at 12.4 s the replay pressed the
brake" and "at 12.5 s OutGauge reported brake 1.0" comparable.

Protocol: one JSON object per datagram, one JSON object back.
Request  ``{"cmd": "marker", "name": "brake_applied", "data": {...}}``
Reply    ``{"ok": true, ...}`` or ``{"ok": false, "error": "..."}``

Loopback only, no authentication -- it must never be bound to anything but
127.0.0.1.
"""

from __future__ import annotations

import json
import socket
import threading
from typing import Any, Callable, Dict, Optional

_MAX_DATAGRAM = 8192
Handler = Callable[[Dict[str, Any]], Dict[str, Any]]


class ControlServer:
    """Serves control commands on a daemon thread until :meth:`stop`."""

    def __init__(self, port: int, handlers: Dict[str, Handler], host: str = "127.0.0.1"):
        self._handlers = handlers
        self._sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        self._sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self._sock.bind((host, port))
        self._sock.settimeout(0.25)
        self.port = self._sock.getsockname()[1]
        self._running = threading.Event()
        self._running.set()
        self._thread = threading.Thread(target=self._run, name="control-server", daemon=True)
        self._thread.start()

    def stop(self, timeout: float = 2.0) -> None:
        self._running.clear()
        self._thread.join(timeout)
        try:
            self._sock.close()
        except OSError:
            pass

    def _run(self) -> None:
        while self._running.is_set():
            try:
                raw, addr = self._sock.recvfrom(_MAX_DATAGRAM)
            except socket.timeout:
                continue
            except OSError:
                return
            reply = self._dispatch(raw)
            try:
                self._sock.sendto(json.dumps(reply).encode("utf-8"), addr)
            except OSError:
                pass

    def _dispatch(self, raw: bytes) -> Dict[str, Any]:
        try:
            message = json.loads(raw.decode("utf-8"))
        except (ValueError, UnicodeDecodeError):
            return {"ok": False, "error": "malformed request"}
        if not isinstance(message, dict):
            return {"ok": False, "error": "request must be an object"}
        handler = self._handlers.get(message.get("cmd"))
        if handler is None:
            return {"ok": False, "error": f"unknown command {message.get('cmd')!r}"}
        try:
            result = handler(message) or {}
        except Exception as exc:  # a broken handler must not kill the channel
            return {"ok": False, "error": f"{type(exc).__name__}: {exc}"}
        result.setdefault("ok", True)
        return result


class ControlClient:
    """Fire-and-forget or request/reply client for :class:`ControlServer`."""

    def __init__(self, port: int, host: str = "127.0.0.1", timeout: float = 1.0):
        self._addr = (host, port)
        self._sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        self._sock.settimeout(timeout)
        self._lock = threading.Lock()

    def close(self) -> None:
        try:
            self._sock.close()
        except OSError:
            pass

    def request(self, cmd: str, wait: bool = True, **kwargs: Any) -> Optional[Dict[str, Any]]:
        """Send one command. Returns the reply, or None on timeout/error.

        A failed marker must never break a running scenario, so nothing here
        raises -- callers check for None.
        """
        payload = dict(kwargs)
        payload["cmd"] = cmd
        try:
            with self._lock:
                self._sock.sendto(json.dumps(payload).encode("utf-8"), self._addr)
                if not wait:
                    return None
                raw, _ = self._sock.recvfrom(_MAX_DATAGRAM)
            return json.loads(raw.decode("utf-8"))
        except (OSError, ValueError):
            return None

    # -- convenience --------------------------------------------------------
    def ping(self) -> bool:
        reply = self.request("ping")
        return bool(reply and reply.get("ok"))

    def marker(self, name: str, **data: Any) -> bool:
        reply = self.request("marker", name=name, data=data)
        return bool(reply and reply.get("ok"))

    def state(self) -> Optional[Dict[str, Any]]:
        reply = self.request("state")
        if reply and reply.get("ok"):
            return reply
        return None

    def stop_tracer(self) -> bool:
        reply = self.request("stop")
        return bool(reply and reply.get("ok"))
