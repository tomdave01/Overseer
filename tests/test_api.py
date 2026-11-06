from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

from fastapi.testclient import TestClient

from api.main import ReplayManager, app, manager
from api.overlays import OverlayHistory
from engine.state import MatchState, ingest
from feed.models import FeedMessage


def test_static_dashboard_and_state_route():
    with TestClient(app) as client:
        page = client.get("/")
        assert page.status_code == 200
        assert "Match pitch" in page.text
        assert client.get("/app.js").status_code == 200
        assert client.get("/styles.css").status_code == 200
        assert client.get("/overlay").status_code == 200
        assert "transparent" in client.get("/overlay.html").text
        assert client.get("/overlay.js").status_code == 200
        assert "OVERSEER_API_BASE_URL" in client.get("/config.js").text
        snapshot = client.get("/state")
        assert snapshot.status_code == 200
        assert snapshot.json()["status"] == "idle"
        assert "frames" in snapshot.json()
        assert snapshot.json()["stream_delay_seconds"] == manager.stream_delay_seconds
        assert snapshot.json()["agents"]["ShapeAnalyst"]["status"] == "idle"
        agent_status = client.get("/agents/status")
        assert agent_status.status_code == 200
        assert (
            agent_status.json()["stream_delay_seconds"]
            == manager.stream_delay_seconds
        )
        assert client.get("/commentary?limit=10").json() == []
        missing_frame = client.get("/frame?minute=1")
        assert missing_frame.status_code == 404
        assert client.get("/overlays/recent").json() == []
        assert "Synthetic data" in page.text or "SYNTHETIC DATA" in page.text
        assert client.get("/clip-video").status_code == 404


def test_favourite_club_defaults_to_neutral():
    assert ReplayManager().favourite_team == "Neutral"


def test_stream_snapshots_wait_for_display_clock_and_late_overlays_are_marked():
    async def run() -> tuple[ReplayManager, dict]:
        replay = ReplayManager()
        replay._loop = asyncio.get_running_loop()
        replay.stream_delay_seconds = 8.0
        replay._queue_stream_snapshot({"minute": 0.0, "frame": {"t": 5}}, 5.0)
        assert replay._display_snapshot["frame"] is None
        replay._queue_stream_snapshot({"minute": 0.2, "frame": {"t": 13}}, 13.0)
        assert replay.display_clock == 5.0
        assert replay._display_snapshot["frame"]["t"] == 5

        replay.display_clock = 20.0
        late = replay.overlays.create({
            "type": "fact",
            "match_id": "match-1",
            "match_clock": 12.0,
            "start_tc": "00:00:12.000",
            "ingest_tc": "00:00:20.000",
            "duration_ms": 8_000,
            "priority": 50,
            "mode": "Broadcast",
            "language": "English",
            "headline": "Momentum shift",
            "body": "The pressure is changing.",
            "source_agent": "ImpactAnalyst",
        })
        replay._publish_overlay(late)
        await asyncio.sleep(0)
        return replay, replay.overlays.recent()[0].model_dump(mode="json")

    replay, overlay = asyncio.run(run())
    assert replay.display_clock == 20.0
    assert overlay["late"] is True
    assert overlay["match_clock"] == 12.0


def test_narration_queue_preserves_high_priority_goals_under_pressure():
    replay = ReplayManager()
    replay._narration_queue = asyncio.Queue(maxsize=1)
    replay._enqueue_narration({
        "priority": 30,
        "flagged_moments": [{"id": "swing-1", "type": "momentum_swing"}],
    })
    replay._enqueue_narration({
        "priority": 100,
        "flagged_moments": [{"id": "goal-1", "type": "goal"}],
    })

    item = replay._narration_queue.get_nowait()
    assert item["flagged_moments"][0]["id"] == "goal-1"
    replay._narration_queue.task_done()


def test_frame_lookup_uses_t_and_minute_for_synthetic_frames():
    original_state = manager.state
    manager.state = MatchState(
        frames=[
            {
                "t": 5.0,
                "minute": 0.1,
                "source": "synthetic",
                "poss": "Rovers",
                "ball": {"x": 10, "y": 10},
                "players": [],
            },
            {
                "t": 5.0,
                "minute": 30.1,
                "source": "synthetic",
                "poss": "United",
                "ball": {"x": 90, "y": 90},
                "players": [],
            },
        ]
    )
    try:
        with TestClient(app) as client:
            timed = client.get("/frame?minute=0.1&t=5&source=synthetic")
            synthetic = client.get("/frame?minute=30.1&source=synthetic")
        assert timed.json()["frame"]["minute"] == 0.1
        assert synthetic.json()["frame"]["source"] == "synthetic"
    finally:
        manager.state = original_state


def test_fact_overlay_is_validated_and_available_to_late_joiners():
    original_state = manager.state
    original_history = manager.overlays
    original_clock = manager.display_clock
    original_delay = manager.stream_delay_seconds
    manager.state = MatchState(
        minute=2.0,
        score={"Rovers": 1, "United": 0},
        events=[{
            "match_id": "overlay-test",
            "player": "Rico Vale",
            "minute": 2.0,
            "team": "Rovers",
            "x": 80.0,
            "y": 45.0,
        }],
    )
    manager.overlays = OverlayHistory()
    try:
        manager.publish_facts_from_worker(
            {
                "moments": [{
                    "id": "goal-rovers-2",
                    "type": "goal",
                    "team": "Rovers",
                    "player": "Rico Vale",
                    "minute": 125.25 / 60,
                    "why": "Rico Vale scored for Rovers.",
                }]
            },
            match_clock=125.25,
        )
        assert manager.overlays.recent() == []
        manager._advance_display_clock(125.25)
        with TestClient(app) as client:
            response = client.get("/overlays/recent?limit=1")

        assert response.status_code == 200
        overlay = response.json()[0]
        assert overlay["type"] == "fact"
        assert overlay["match_id"] == "overlay-test"
        assert overlay["seq"] == 0
        assert overlay["match_clock"] == 125.25
        assert overlay["start_tc"] == "00:02:05.250"
        assert overlay["ingest_tc"] == "00:02:05.250"
        assert overlay["source_agent"] == "MomentDetector"
        assert overlay["markings"][0]["kind"] == "spotlight"
    finally:
        manager.state = original_state
        manager.overlays = original_history
        manager.display_clock = original_clock
        manager.stream_delay_seconds = original_delay


def test_flagged_fact_overlay_precedes_async_narration_and_includes_latency():
    async def run() -> list:
        replay = ReplayManager()
        replay._loop = asyncio.get_running_loop()
        replay.stream_delay_seconds = 0
        published = []
        replay._publish_overlay = published.append

        class DelayedNarrator:
            async def narrate(self, *_args, **_kwargs) -> dict:
                await asyncio.sleep(0.05)
                return {
                    "fact_id": "goal-rovers-1",
                    "mode": "Broadcast",
                    "language": "English",
                    "favourite_team": "Rovers",
                    "facts": {},
                    "text": "GOAL for Rovers.",
                    "fallback": False,
                }

        async def iter_analysis_tasks(
            state,
            *,
            flagged_moments,
            deadline_seconds,
            on_task_created,
        ):
            task = {
                "task_id": "moment-1",
                "agent": "MomentDetector",
                "window": {"start_minute": 0.0, "end_minute": state.minute},
                "deadline": "deadline",
                "created_at": "created",
            }
            on_task_created(task)
            await asyncio.sleep(0)
            yield {
                "task": task,
                "facts": flagged_moments,
                "output_id": "moment-output",
                "latency_ms": 10.0,
                "state": state,
            }

        replay.orchestrator = SimpleNamespace(
            analyze_facts=lambda _state: {
                "moments": [{
                    "id": "goal-rovers-1",
                    "type": "goal",
                    "team": "Rovers",
                    "player": "Rico Vale",
                    "minute": 1.0,
                    "why": "Rico Vale scored for Rovers.",
                }]
            },
            iter_analysis_tasks=iter_analysis_tasks,
            narrator=DelayedNarrator(),
        )
        state = MatchState(
            minute=1.0,
            events=[{
                "match_id": "match-1",
                "player": "Rico Vale",
                "team": "Rovers",
                "x": 80.0,
                "y": 45.0,
            }],
        )
        wall_time = datetime.now(timezone.utc) - timedelta(milliseconds=100)
        message = FeedMessage(
            match_id="match-1",
            seq=1,
            match_clock=60.0,
            wall_time_utc=wall_time.isoformat(),
            source="synthetic",
            payload={"kind": "event", "record": {"type": "shot"}},
        )

        await replay.on_feed_update(message, state)
        assert [overlay.type for overlay in published] == ["fact"]
        assert published[0].latency_ms is not None
        assert published[0].latency_ms >= 90

        async def wait_for_narration() -> None:
            while len(published) < 2:
                await asyncio.sleep(0.005)

        await asyncio.wait_for(wait_for_narration(), timeout=1)
        await asyncio.gather(*tuple(replay._agent_tasks))
        assert [overlay.type for overlay in published] == ["fact", "narration"]
        assert published[1].latency_ms >= published[0].latency_ms
        return published

    published = asyncio.run(run())
    assert published[0].headline.startswith("GOAL")
    assert published[1].body == "GOAL for Rovers."


def test_feed_update_survives_missing_foundry_configuration(monkeypatch):
    async def run() -> list:
        replay = ReplayManager()
        replay._loop = asyncio.get_running_loop()
        replay.stream_delay_seconds = 0
        state = MatchState()
        ingest(
            state,
            event={
                "match_id": "match-1",
                "source": "synthetic",
                "minute": 1.0,
                "t": None,
                "type": "shot",
                "team": "Rovers",
                "player": "Rico Vale",
                "outcome": "goal",
                "x": 80.0,
                "y": 45.0,
                "end_x": None,
                "end_y": None,
            },
        )
        wall_time = datetime.now(timezone.utc)
        message = FeedMessage(
            match_id="match-1",
            seq=1,
            match_clock=60.0,
            wall_time_utc=wall_time.isoformat(),
            source="synthetic",
            payload={
                "kind": "event",
                "record": {"type": "shot", "outcome": "goal"},
            },
        )

        await replay.on_feed_update(message, state)
        assert [overlay.type for overlay in replay.overlays.recent()] == ["fact"]
        await asyncio.wait_for(
            asyncio.gather(*tuple(replay._agent_tasks)),
            timeout=2,
        )
        async def wait_for_narration() -> None:
            while len(replay.overlays.recent()) < 2:
                await asyncio.sleep(0.005)

        await asyncio.wait_for(wait_for_narration(), timeout=2)
        return replay.overlays.recent()

    monkeypatch.delenv("FOUNDRY_PROJECT_ENDPOINT", raising=False)
    monkeypatch.delenv("FOUNDRY_MODEL", raising=False)
    monkeypatch.delenv("APPLICATIONINSIGHTS_CONNECTION_STRING", raising=False)
    import llm.foundry

    monkeypatch.setattr(llm.foundry, "_client", None)
    overlays = asyncio.run(run())
    assert [overlay.type for overlay in overlays] == ["fact", "narration"]
    assert "Rico Vale scores for Rovers" in overlays[1].body


def test_control_preferences_and_fan_query():
    original_state = manager.state
    original_settings = (
        manager.speed,
        manager.mode,
        manager.language,
        manager.favourite_team,
        manager.favourite_player,
    )
    manager.state = MatchState(
        events=[
            {
                "minute": 10.0,
                "team": "Rovers",
                "player": "Rico Vale",
                "type": "shot",
                "outcome": "on_target",
                "t": None,
            },
            {
                "minute": 11.0,
                "team": "United",
                "player": "Ira West",
                "type": "shot",
                "outcome": "off_target",
                "t": None,
            },
        ],
        frames=[
            {
                "t": 600.0,
                "minute": 10.0,
                "poss": "Rovers",
                "ball": {"x": 80.0, "y": 75.0},
                "players": [],
            }
        ],
    )
    try:
        with TestClient(app) as client:
            response = client.post(
                "/control",
                json={
                    "speed": 25,
                    "mode": "Analyst",
                    "language": "French",
                    "favourite_team": "United",
                },
            )
            assert response.status_code == 200
            assert response.json()["speed"] == 25
            assert response.json()["mode"] == "Analyst"

            selected = client.post(
                "/control",
                json={"favourite_player": "Rico Vale"},
            )
            assert selected.json()["favourite_player"] == "Rico Vale"
            cleared = client.post(
                "/control",
                json={"favourite_player": None},
            )
            assert cleared.json()["favourite_player"] is None

            frame = client.get("/frame?minute=9.9")
            assert frame.status_code == 200
            assert frame.json()["frame"]["minute"] == 10.0
    finally:
        manager.state = original_state
        (
            manager.speed,
            manager.mode,
            manager.language,
            manager.favourite_team,
            manager.favourite_player,
        ) = original_settings
