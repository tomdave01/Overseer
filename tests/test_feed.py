from __future__ import annotations

import asyncio
from datetime import datetime, timezone
from typing import Any

from engine.state import MatchState
from feed.clients import create_feed_consumer, create_feed_producer
from feed.models import FeedMessage
from feed.persistence import InMemoryNarrationCache, narration_cache_key
from feed.processor import FeedProcessor
from feed.simulator import simulate


class _UnusedConsumer:
    async def receive(self) -> FeedMessage:
        raise AssertionError("This test supplies messages directly")

    async def close(self) -> None:
        return None


class _MemorySnapshotStore:
    def __init__(self) -> None:
        self.snapshots: dict[str, dict[str, Any]] = {}

    async def load_snapshot(self, match_id: str) -> dict[str, Any] | None:
        return self.snapshots.get(match_id)

    async def save_snapshot(
        self, match_id: str, snapshot: dict[str, Any]
    ) -> None:
        self.snapshots[match_id] = snapshot


class _CollectingProducer:
    def __init__(self) -> None:
        self.messages: list[FeedMessage] = []

    async def publish(self, message: FeedMessage) -> None:
        self.messages.append(message)

    async def close(self) -> None:
        return None


def _message(seq: int, *, goal: bool = False) -> FeedMessage:
    event = {
        "match_id": "match-1",
        "source": "synthetic",
        "minute": float(seq + 1),
        "t": None,
        "type": "shot",
        "team": "Rovers",
        "player": "Rico Vale",
        "outcome": "goal" if goal else "on_target",
        "x": 80.0,
        "y": 75.0,
        "end_x": None,
        "end_y": None,
    }
    return FeedMessage(
        match_id="match-1",
        seq=seq,
        match_clock=float(seq + 1),
        wall_time_utc=datetime.now(timezone.utc).isoformat(),
        source="synthetic",
        payload={"kind": "event", "record": event},
    )


def test_processor_reorders_messages_and_ignores_duplicates():
    async def run() -> tuple[MatchState, int]:
        processor = FeedProcessor(
            _UnusedConsumer(),
            snapshot_store=_MemorySnapshotStore(),
            reorder_wait_seconds=10,
            max_reorder_buffer=4,
        )
        await processor.process_message(_message(1))
        assert processor.states["match-1"].events == []
        await processor.process_message(_message(0, goal=True))
        await processor.process_message(_message(2))
        await processor.process_message(_message(1))
        return processor.states["match-1"], processor.duplicate_count

    state, duplicate_count = asyncio.run(run())
    assert [event["minute"] for event in state.events] == [1.0, 2.0, 3.0]
    assert state.score == {"Rovers": 1, "United": 0}
    assert duplicate_count == 1


def test_processor_skips_a_persistent_gap_and_drops_late_messages():
    async def run() -> tuple[list[dict[str, Any]], int]:
        processor = FeedProcessor(
            _UnusedConsumer(),
            snapshot_store=_MemorySnapshotStore(),
            reorder_wait_seconds=10,
            max_reorder_buffer=2,
        )
        await processor.process_message(_message(2))
        await processor.process_message(_message(3))
        await processor.process_message(_message(1))
        return processor.states["match-1"].events, processor.duplicate_count

    events, duplicate_count = asyncio.run(run())
    assert [event["minute"] for event in events] == [3.0, 4.0]
    assert duplicate_count == 1


def test_processor_restores_last_sequence_from_snapshot():
    async def run() -> MatchState:
        store = _MemorySnapshotStore()
        store.snapshots["match-1"] = {
            "last_seq": 0,
            "state": MatchState(events=[_message(0).record]).snapshot(),
        }
        processor = FeedProcessor(_UnusedConsumer(), snapshot_store=store)
        await processor.process_message(_message(0))
        await processor.process_message(_message(1))
        return processor.states["match-1"]

    state = asyncio.run(run())
    assert [event["minute"] for event in state.events] == [1.0, 2.0]


def test_key_moment_is_snapshotted_with_applied_sequence():
    async def run() -> dict[str, Any]:
        store = _MemorySnapshotStore()
        processor = FeedProcessor(_UnusedConsumer(), snapshot_store=store)
        await processor.process_message(_message(0, goal=True))
        return store.snapshots["match-1"]

    snapshot = asyncio.run(run())
    assert snapshot["last_seq"] == 0
    assert snapshot["state"]["score"] == {"Rovers": 1, "United": 0}


def test_processor_calls_live_update_hook_after_ordered_ingest():
    async def run() -> list[tuple[int, int]]:
        updates: list[tuple[int, int]] = []

        async def on_update(message: FeedMessage, state: MatchState) -> None:
            updates.append((message.seq, len(state.events)))

        processor = FeedProcessor(_UnusedConsumer(), on_update=on_update)
        await processor.process_message(_message(1))
        await processor.process_message(_message(0))
        return updates

    assert asyncio.run(run()) == [(0, 1), (1, 2)]


def test_unconfigured_event_hubs_uses_shared_in_process_queue(monkeypatch, caplog):
    async def run() -> None:
        monkeypatch.delenv("EVENTHUB_NAMESPACE", raising=False)
        producer = create_feed_producer()
        consumer = create_feed_consumer()
        assert producer is consumer
        message = _message(0)
        await producer.publish(message)
        assert await consumer.receive() == message
        await producer.close()
        await consumer.close()

    asyncio.run(run())
    assert "not a distributed transport" in caplog.text


def test_simulator_publishes_only_synthetic_records(monkeypatch):
    def fake_generate_match(
        seed: int,
    ) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
        assert seed == 42
        return (
            [{"match_id": "synthetic-42", "minute": 0.008, "t": 0.5, "type": "pass"}],
            [{"minute": 0.004, "t": 0.25}],
        )

    monkeypatch.setattr("feed.simulator.generate_match", fake_generate_match)
    producer = _CollectingProducer()

    async def run() -> int:
        return await simulate(
            producer,
            speed=1_000_000,
            match_id="shared-match",
        )

    assert asyncio.run(run()) == 2
    assert [message.source for message in producer.messages] == ["synthetic", "synthetic"]
    assert [message.seq for message in producer.messages] == [0, 1]
    assert all(message.match_id == "shared-match" for message in producer.messages)
    assert [message.match_clock for message in producer.messages] == sorted(
        message.match_clock for message in producer.messages
    )


def test_narration_cache_key_includes_fact_mode_language_and_favourite():
    base = narration_cache_key("fact-1", "Broadcast", "English", "Rovers")
    assert base != narration_cache_key("fact-2", "Broadcast", "English", "Rovers")
    assert base != narration_cache_key("fact-1", "Analyst", "English", "Rovers")
    assert base != narration_cache_key("fact-1", "Broadcast", "French", "Rovers")
    assert base != narration_cache_key("fact-1", "Broadcast", "English", "United")
    neutral = narration_cache_key("fact-1", "Fan", "English", "Neutral")
    assert neutral != narration_cache_key("fact-1", "Fan", "English", "Rovers")
    assert neutral != narration_cache_key("fact-1", "Fan", "English", "United")

    async def run() -> None:
        cache = InMemoryNarrationCache()
        await cache.put("fact-1", "Broadcast", "English", "Rovers", "Rovers lead.")
        assert await cache.get("fact-1", "Broadcast", "English", "Rovers") == "Rovers lead."
        assert await cache.get("fact-1", "Broadcast", "English", "United") is None

    asyncio.run(run())
