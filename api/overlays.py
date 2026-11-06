"""Validated broadcast overlay messages and bounded in-memory history."""

from __future__ import annotations

import asyncio
import json
import os
import threading
import time
from collections import deque
from hashlib import sha256
from typing import Any, Protocol, Literal
from uuid import uuid4

from pydantic import BaseModel, ConfigDict, Field, field_validator


OverlayType = Literal[
    "fact",
    "narration",
    "lower_third",
    "momentum",
    "pitch_marking",
]
MarkingKind = Literal["spotlight", "run_path", "line", "zone"]


class OverlayPoint(BaseModel):
    model_config = ConfigDict(extra="forbid")

    x: float = Field(ge=0, le=100, strict=True)
    y: float = Field(ge=0, le=100, strict=True)


class OverlayMarking(BaseModel):
    model_config = ConfigDict(extra="forbid")

    kind: MarkingKind
    points: list[OverlayPoint] = Field(min_length=1, max_length=64)


class OverlayStat(BaseModel):
    model_config = ConfigDict(extra="forbid")

    label: str = Field(min_length=1, max_length=80)
    value: str = Field(min_length=1, max_length=80)


class Overlay(BaseModel):
    model_config = ConfigDict(extra="forbid")

    overlay_id: str = Field(default_factory=lambda: str(uuid4()))
    type: OverlayType
    match_id: str = Field(min_length=1, max_length=120)
    seq: int = Field(ge=0, strict=True)
    match_clock: float = Field(ge=0, allow_inf_nan=False, strict=True)
    start_tc: str = Field(pattern=r"^\d{2,}:\d{2}:\d{2}\.\d{3}$")
    ingest_tc: str = Field(pattern=r"^\d{2,}:\d{2}:\d{2}\.\d{3}$")
    late: bool = False
    duration_ms: int = Field(ge=250, le=120_000, strict=True)
    priority: int = Field(ge=0, le=100, strict=True)
    latency_ms: float | None = Field(default=None, ge=0, allow_inf_nan=False)
    mode: Literal["Broadcast", "Analyst", "Fan"]
    language: Literal["English", "Spanish", "Arabic", "French"]
    headline: str = Field(min_length=1, max_length=160)
    body: str = Field(default="", max_length=1200)
    stat: OverlayStat | None = None
    player: str | None = Field(default=None, max_length=80)
    team: str | None = Field(default=None, max_length=80)
    markings: list[OverlayMarking] = Field(default_factory=list, max_length=16)
    source_agent: str = Field(min_length=1, max_length=80)
    fact_ids: list[str] = Field(default_factory=list, max_length=32)

    @field_validator("match_id", "headline", "source_agent")
    @classmethod
    def reject_blank_strings(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("must not be blank")
        return value

    @field_validator("start_tc", "ingest_tc")
    @classmethod
    def validate_timecode_components(cls, value: str) -> str:
        _, minutes, seconds_milliseconds = value.split(":")
        seconds = seconds_milliseconds.split(".")[0]
        if int(minutes) > 59 or int(seconds) > 59:
            raise ValueError("timecode minutes and seconds must be less than 60")
        return value


def match_timecode(match_clock: float) -> str:
    if match_clock < 0:
        raise ValueError("match_clock must not be negative")
    milliseconds = round(match_clock * 1000)
    hours, remainder = divmod(milliseconds, 3_600_000)
    minutes, remainder = divmod(remainder, 60_000)
    seconds, milliseconds = divmod(remainder, 1000)
    return f"{hours:02}:{minutes:02}:{seconds:02}.{milliseconds:03}"


def stable_fact_id(kind: str, data: Any) -> str:
    encoded = json.dumps(
        data,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        default=str,
    ).encode("utf-8")
    return f"{kind}:{sha256(encoded).hexdigest()}"


class OverlayHistory:
    def __init__(self, max_items: int = 500) -> None:
        if max_items < 1:
            raise ValueError("max_items must be at least one")
        self._items: deque[tuple[Overlay, float]] = deque(maxlen=max_items)
        self._lock = threading.Lock()
        self._seq = 0

    def add(self, data: dict[str, Any]) -> Overlay:
        overlay = self.create(data)
        self.publish(overlay)
        return overlay

    def create(self, data: dict[str, Any]) -> Overlay:
        with self._lock:
            overlay = Overlay.model_validate({**data, "seq": self._seq})
            self._seq += 1
            return overlay

    def publish(self, overlay: Overlay) -> None:
        with self._lock:
            self._items.append(
                (overlay, time.monotonic() + overlay.duration_ms / 1000)
            )

    def mark_late(self, overlay: Overlay) -> Overlay:
        late_overlay = overlay.model_copy(update={"late": True})
        with self._lock:
            self._items = deque(
                (
                    (late_overlay if item.overlay_id == overlay.overlay_id else item,
                     expires_at)
                    for item, expires_at in self._items
                ),
                maxlen=self._items.maxlen,
            )
        return late_overlay

    def recent(self, limit: int = 100) -> list[Overlay]:
        if not 1 <= limit <= 500:
            raise ValueError("limit must be between 1 and 500")
        with self._lock:
            now = time.monotonic()
            return [
                overlay
                for overlay, expires_at in list(self._items)[-limit:]
                if expires_at > now
            ]

    def clear(self) -> None:
        with self._lock:
            self._items.clear()
            self._seq = 0


class OverlaySink(Protocol):
    async def publish(self, overlay: Overlay) -> None:
        ...

    async def close(self) -> None:
        ...


class WebPubSubOverlaySink(OverlaySink):
    def __init__(self, client: Any) -> None:
        self._client = client

    @classmethod
    def from_environment(cls) -> WebPubSubOverlaySink | None:
        connection = os.getenv("WEB_PUBSUB_CONNECTION", "").strip()
        if not connection:
            return None
        hub = os.getenv("WEB_PUBSUB_HUB", "overseer").strip()
        if not hub:
            raise ValueError("WEB_PUBSUB_HUB must not be blank")
        from azure.messaging.webpubsubservice import WebPubSubServiceClient

        return cls(
            WebPubSubServiceClient.from_connection_string(
                connection_string=connection,
                hub=hub,
            )
        )

    async def publish(self, overlay: Overlay) -> None:
        await asyncio.to_thread(
            self._client.send_to_all,
            overlay.model_dump(mode="json"),
            content_type="application/json",
        )

    async def close(self) -> None:
        await asyncio.to_thread(self._client.close)
