import logging
from typing import Dict, List, Callable, Any
from threading import Lock

from misc.logging_setup import ErrorThrottle

logger = logging.getLogger(__name__)


class EventBus:
    """Zentrale Event-Verteilung zwischen allen Komponenten"""

    def __init__(self, error_throttle: ErrorThrottle = None):
        self._subscribers: Dict[str, List[Callable]] = {}
        self._lock = Lock()
        # Fehler-Isolation: ein Subscriber, der wirft, darf weder die anderen
        # Subscriber noch den emittierenden Thread stoppen. Der emittierende
        # Thread ist bei Paket-Handlern der asyncore-Mainthread - eine
        # durchgereichte Exception reisst dort die Verbindung ab.
        self._errors = error_throttle or ErrorThrottle(logger)

    def subscribe(self, event_type: str, callback: Callable):
        """Registriert einen Event-Handler"""
        with self._lock:
            if event_type not in self._subscribers:
                self._subscribers[event_type] = []
            self._subscribers[event_type].append(callback)

    def unsubscribe(self, event_type: str, callback: Callable):
        """Entfernt einen Event-Handler"""
        with self._lock:
            if event_type in self._subscribers:
                subscribers = self._subscribers[event_type]
                if callback in subscribers:
                    subscribers.remove(callback)

    def emit(self, event_type: str, data: Any = None):
        """Sendet ein Event an alle registrierten Handler.

        Jeder Handler ist einzeln gekapselt: eine Exception wird
        ratenbegrenzt geloggt, die restlichen Handler laufen weiter.
        Kosten im Gutfall: null (try/except ohne Exception ist in
        Python 3.11 gratis).
        """
        subscribers = []
        with self._lock:
            if event_type in self._subscribers:
                subscribers = self._subscribers[event_type].copy()

        for callback in subscribers:
            try:
                callback(data)
            except Exception as e:
                self._errors.report(_describe(callback), e,
                                    context=f"handling '{event_type}'")

    def subscriber_count(self, event_type: str) -> int:
        """Anzahl registrierter Handler - fuer Diagnose und Tests"""
        with self._lock:
            return len(self._subscribers.get(event_type, ()))


def _describe(callback: Callable) -> str:
    """Stabiler Name eines Handlers fuer die Fehler-Ratenbegrenzung."""
    owner = getattr(callback, '__self__', None)
    name = getattr(callback, '__qualname__', None) or repr(callback)
    if owner is not None:
        return f"{type(owner).__name__}.{getattr(callback, '__name__', name)}"
    return name
