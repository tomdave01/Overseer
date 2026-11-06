"""Validated message envelope shared by feed producers and consumers."""

from __future__ import annotations

import json
import math
from dataclasses import asdict, dataclass
from datetime import datetime, timedelta
from typing import Any, Literal


Source = Literal["synthetic"]
RecordKind = Literal["event", "frame"]


@dataclass(frozen=True, slots=True)
class FeedMessage:
    match_id: str
    seq: int
    match_clock: float
    wall_time_utc: str
    source: Source
    payload: dict[str, Any]

    def __post_init__(self) -> None:
        if not isinstance(self.match_id, str) or not self.match_id.strip():
            raise ValueError("match_id must not be blank")
        if (
            isinstance(self.seq, bool)
            or not isinstance(self.seq, int)
            or self.seq < 0
        ):
            raise ValueError("seq must be a non-negative integer")
        if (
            isinstance(self.match_clock, bool)
            or not isinstance(self.match_clock, (int, float))
            or not math.isfinite(self.match_clock)
        ):
            raise ValueError("match_clock must be a finite number")
        if self.match_clock < 0:
            raise ValueError("match_clock must not be negative")
        if (
            not isinstance(self.source, str)
            or self.source != "synthetic"
        ):
            raise ValueError("source must be synthetic")
        if not isinstance(self.payload, dict):
            raise ValueError("payload must be an object")
        kind = self.payload.get("kind")
        record = self.payload.get("record")
        if (
            not isinstance(kind, str)
            or kind not in {"event", "frame"}
            or not isinstance(record, dict)
        ):
            raise ValueError("payload must contain an event or frame record")
        if not isinstance(self.wall_time_utc, str):
            raise ValueError("wall_time_utc must be an ISO-8601 UTC timestamp")
        try:
            timestamp = datetime.fromisoformat(
                self.wall_time_utc.replace("Z", "+00:00")
            )
        except ValueError as error:
            raise ValueError("wall_time_utc must be an ISO-8601 UTC timestamp") from error
        if timestamp.tzinfo is None or timestamp.utcoffset() != timedelta(0):
            raise ValueError("wall_time_utc must include the UTC offset")

    @property
    def kind(self) -> RecordKind:
        return self.payload["kind"]

    @property
    def record(self) -> dict[str, Any]:
        return self.payload["record"]

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    def to_json(self) -> str:
        return json.dumps(self.to_dict(), ensure_ascii=False, separators=(",", ":"))

    @classmethod
    def from_json(cls, value: str) -> FeedMessage:
        data = json.loads(value)
        if not isinstance(data, dict):
            raise ValueError("Feed message must be a JSON object")
        return cls(**data)
