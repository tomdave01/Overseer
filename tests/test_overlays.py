from __future__ import annotations

import asyncio

import pytest
from pydantic import ValidationError

from api.overlays import Overlay, OverlayHistory, WebPubSubOverlaySink, match_timecode


def _overlay_data() -> dict:
    return {
        "type": "fact",
        "match_id": "match-1",
        "match_clock": 125.25,
        "start_tc": "00:02:05.250",
        "ingest_tc": "00:02:13.250",
        "duration_ms": 8_000,
        "priority": 80,
        "mode": "Broadcast",
        "language": "English",
        "headline": "GOAL · Rico Vale",
        "body": "Rico Vale scores for Rovers.",
        "stat": {"label": "Score", "value": "1 — 0"},
        "player": "Rico Vale",
        "team": "Rovers",
        "markings": [{
            "kind": "spotlight",
            "points": [{"x": 74.0, "y": 42.0}],
        }],
        "source_agent": "MomentDetector",
        "fact_ids": ["goal-rovers-2.0"],
    }


def test_overlay_schema_validates_machine_readable_output():
    overlay = Overlay.model_validate({**_overlay_data(), "seq": 0})

    assert overlay.type == "fact"
    assert overlay.stat is not None
    assert overlay.stat.value == "1 — 0"
    assert overlay.model_dump(mode="json")["markings"][0]["kind"] == "spotlight"


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("priority", 101),
        ("duration_ms", 0),
        ("start_tc", "125.25"),
        ("start_tc", "00:60:00.000"),
        ("ingest_tc", "00:60:00.000"),
        ("type", "unknown"),
        ("mode", "Local"),
    ],
)
def test_overlay_schema_rejects_invalid_values(field: str, value: object):
    with pytest.raises(ValidationError):
        Overlay.model_validate({**_overlay_data(), "seq": 0, field: value})


def test_overlay_history_assigns_monotonic_sequence_and_bounds_recent_items():
    history = OverlayHistory(max_items=2)
    first = history.add(_overlay_data())
    second = history.add(_overlay_data())
    third = history.add(_overlay_data())

    assert (first.seq, second.seq, third.seq) == (0, 1, 2)
    assert [item.seq for item in history.recent(2)] == [1, 2]


def test_match_timecode_formats_milliseconds():
    assert match_timecode(3661.789) == "01:01:01.789"


def test_web_pubsub_sink_broadcasts_the_validated_overlay():
    class FakeClient:
        def __init__(self) -> None:
            self.message = None
            self.content_type = None

        def send_to_all(self, message: object, *, content_type: str) -> None:
            self.message = message
            self.content_type = content_type

        def close(self) -> None:
            pass

    client = FakeClient()
    sink = WebPubSubOverlaySink(client)
    overlay = Overlay.model_validate({**_overlay_data(), "seq": 0})

    asyncio.run(sink.publish(overlay))

    assert client.message == overlay.model_dump(mode="json")
    assert client.content_type == "application/json"
