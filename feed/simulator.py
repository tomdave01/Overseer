"""Publish a generated synthetic match to the feed."""

from __future__ import annotations

import argparse
import asyncio
import os
from datetime import datetime, timezone
from typing import Any, Sequence

from feed.clients import FeedProducer, create_feed_producer
from feed.models import FeedMessage, Source
from generator.match import MATCH_ID, generate_match


def _records(
    events: list[dict[str, Any]],
    frames: list[dict[str, Any]],
    fallback_match_id: str,
    match_id_override: str | None,
) -> list[tuple[float, str, str, dict[str, Any]]]:
    records: list[tuple[float, int, str, str, dict[str, Any]]] = []
    for kind, values, order in (
        ("event", events, 0),
        ("frame", frames, 1),
    ):
        for value in values:
            local_time = (
                float(value["t"])
                if value.get("t") is not None
                else float(value.get("minute", 0.0)) * 60.0
            )
            if local_time < 0:
                raise ValueError("Feed records must not have negative timestamps")
            if (
                kind == "event"
                and not value.get("match_id")
                and match_id_override is None
            ):
                raise ValueError("Processed event is missing its match_id")
            match_id = match_id_override or str(
                value.get("match_id") or fallback_match_id
            )
            records.append((local_time, order, kind, match_id, value))
    records.sort(key=lambda item: (item[0], item[1]))
    return [
        (timestamp, kind, match_id, record)
        for timestamp, _, kind, match_id, record in records
    ]


async def simulate(
    producer: FeedProducer,
    *,
    speed: float = 1.0,
    seed: int = 42,
    match_id: str | None = None,
) -> int:
    if speed <= 0:
        raise ValueError("speed must be greater than zero")
    synthetic_events, synthetic_frames = generate_match(seed=seed)
    synthetic_match_id = match_id or MATCH_ID
    datasets = [
        ("synthetic", synthetic_events, synthetic_frames, synthetic_match_id)
    ]

    sequences: dict[str, int] = {}
    clock_offsets: dict[str, float] = {}
    last_clocks: dict[str, float] = {}
    published = 0
    for source, events, frames, fallback_match_id in datasets:
        previous_local_time: float | None = None
        dataset_clocks: dict[str, float] = {}
        records = _records(events, frames, fallback_match_id, match_id)
        for local_time, kind, record_match_id, record in records:
            if previous_local_time is not None:
                delay = max(0.0, local_time - previous_local_time) / speed
                if delay:
                    await asyncio.sleep(delay)
            previous_local_time = local_time
            sequence = sequences.get(record_match_id, 0)
            match_clock = max(
                clock_offsets.get(record_match_id, 0.0) + local_time,
                last_clocks.get(record_match_id, 0.0),
            )
            message = FeedMessage(
                match_id=record_match_id,
                seq=sequence,
                match_clock=match_clock,
                wall_time_utc=datetime.now(timezone.utc).isoformat(),
                source=source,
                payload={
                    "kind": kind,
                    "record": (
                        {**record, "match_id": record_match_id}
                        if kind == "event"
                        else record
                    ),
                },
            )
            await producer.publish(message)
            sequences[record_match_id] = sequence + 1
            last_clocks[record_match_id] = match_clock
            dataset_clocks[record_match_id] = match_clock
            published += 1
        for record_match_id, final_clock in dataset_clocks.items():
            clock_offsets[record_match_id] = final_clock + 0.2
    return published


async def _run(arguments: argparse.Namespace) -> None:
    producer = create_feed_producer()
    try:
        count = await simulate(
            producer,
            speed=arguments.speed,
            seed=arguments.seed,
            match_id=arguments.match_id,
        )
        print(f"Published {count} feed messages.")
    finally:
        await producer.close()


def main(argv: Sequence[str] | None = None) -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--speed",
        type=float,
        default=float(os.getenv("SIMULATOR_SPEED", "1")),
        help="playback speed multiplier (default: 1)",
    )
    parser.add_argument("--seed", type=int, default=42)
    parser.add_argument("--match-id", help="optional match ID for synthetic records")
    arguments = parser.parse_args(argv)
    asyncio.run(_run(arguments))


if __name__ == "__main__":
    main()
