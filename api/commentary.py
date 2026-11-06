"""Bounded, ordered narration history."""

from __future__ import annotations

import threading
from collections import deque
from typing import Any


class CommentaryHistory:
    def __init__(self, max_items: int = 500) -> None:
        if max_items < 1:
            raise ValueError("max_items must be at least one")
        self._items: deque[dict[str, Any]] = deque(maxlen=max_items)
        self._lock = threading.Lock()
        self._sequence = 0

    def add(self, overlay: dict[str, Any]) -> dict[str, Any]:
        if overlay.get("type") != "narration":
            raise ValueError("commentary history accepts narration overlays only")
        with self._lock:
            item = {**overlay, "history_seq": self._sequence}
            self._sequence += 1
            self._items.append(item)
            return dict(item)

    def recent(
        self,
        limit: int = 100,
        since: int | None = None,
    ) -> list[dict[str, Any]]:
        if limit < 1 or limit > 500:
            raise ValueError("limit must be between 1 and 500")
        if since is not None and since < 0:
            raise ValueError("since must not be negative")
        with self._lock:
            items = [
                item
                for item in self._items
                if since is None or item["history_seq"] > since
            ]
            return [dict(item) for item in items[-limit:]]
