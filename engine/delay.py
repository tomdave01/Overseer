"""Thread-safe, match-clock ordered delivery buffer."""

from __future__ import annotations

import heapq
import threading
from dataclasses import dataclass
from typing import Any


@dataclass(frozen=True)
class DelayedItem:
    match_clock: float
    kind: str
    payload: Any


class DelayBuffer:
    def __init__(self) -> None:
        self._items: list[tuple[float, int, DelayedItem]] = []
        self._sequence = 0
        self._lock = threading.Lock()

    def push(self, match_clock: float, kind: str, payload: Any) -> None:
        if match_clock < 0:
            raise ValueError("match_clock must not be negative")
        with self._lock:
            item = DelayedItem(match_clock, kind, payload)
            heapq.heappush(self._items, (match_clock, self._sequence, item))
            self._sequence += 1

    def release_until(self, display_clock: float) -> list[DelayedItem]:
        if display_clock < 0:
            raise ValueError("display_clock must not be negative")
        released: list[DelayedItem] = []
        with self._lock:
            while self._items and self._items[0][0] <= display_clock:
                released.append(heapq.heappop(self._items)[2])
        return released

    def clear(self) -> None:
        with self._lock:
            self._items.clear()
