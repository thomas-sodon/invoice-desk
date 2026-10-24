"""In-memory realtime publisher (tests, offline demo)."""

from __future__ import annotations

import threading
from collections.abc import Callable

from invoicedesk.contracts.infra import BoardEvent

__all__ = ["InMemoryEventPublisher"]


class InMemoryEventPublisher:
    def __init__(self) -> None:
        self.events: list[BoardEvent] = []
        self._subs: list[Callable[[BoardEvent], None]] = []
        self._lock = threading.Lock()

    def publish(self, event: BoardEvent) -> None:
        with self._lock:
            self.events.append(event)
            subs = list(self._subs)
        for s in subs:
            s(event)

    def subscribe(self, fn: Callable[[BoardEvent], None]) -> None:
        with self._lock:
            self._subs.append(fn)
