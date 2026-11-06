from __future__ import annotations

from api.commentary import CommentaryHistory
from engine.delay import DelayBuffer


def test_delay_buffer_releases_in_match_clock_order_after_threshold():
    buffer = DelayBuffer()
    buffer.push(8.0, "frame", {"id": "frame-8"})
    buffer.push(5.0, "event", {"id": "event-5"})
    buffer.push(5.0, "overlay", {"id": "overlay-5"})

    assert buffer.release_until(4.99) == []
    assert [
        (item.match_clock, item.kind)
        for item in buffer.release_until(5.0)
    ] == [(5.0, "event"), (5.0, "overlay")]
    assert [
        item.payload for item in buffer.release_until(8.0)
    ] == [{"id": "frame-8"}]


def test_commentary_history_is_bounded_ordered_and_keeps_language():
    history = CommentaryHistory(max_items=2)
    first = history.add({
        "type": "narration",
        "language": "English",
        "body": "A goal.",
    })
    history.add({
        "type": "narration",
        "language": "French",
        "body": "Un but.",
    })
    history.add({
        "type": "narration",
        "language": "Arabic",
        "body": "هدف.",
    })

    entries = history.recent(limit=2)
    assert [entry["history_seq"] for entry in entries] == [1, 2]
    assert [entry["language"] for entry in entries] == ["French", "Arabic"]
    assert history.recent(since=first["history_seq"]) == entries
