"""FastAPI service, live feed processor, and replay development adapter."""

from __future__ import annotations

import asyncio
import copy
import json
import logging
import os
import threading
from contextlib import asynccontextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Literal

from fastapi import FastAPI, HTTPException, Query, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, StreamingResponse
from pydantic import BaseModel, Field

from agents.orchestrator import Orchestrator
from api.overlays import (
    Overlay,
    OverlayHistory,
    OverlaySink,
    WebPubSubOverlaySink,
    match_timecode,
    stable_fact_id,
)
from engine.replay import ReplayDataset, ReplayEngine, default_datasets
from engine.stats import StatsAggregator
from engine.state import MatchState
from feed.clients import create_feed_consumer
from feed.models import FeedMessage
from feed.processor import FeedProcessor
from telemetry import initialize_telemetry


ROOT = Path(__file__).resolve().parent.parent
WEB_DIR = ROOT / "web"
logger = logging.getLogger(__name__)


class ControlRequest(BaseModel):
    action: Literal["start", "stop"] | None = None
    speed: float | None = Field(default=None, gt=0, le=1000)
    mode: Literal["Broadcast", "Analyst", "Fan"] | None = None
    language: Literal["English", "Spanish", "Arabic", "French"] | None = None
    favourite_team: Literal["Rovers", "United", "Neutral"] | None = None
    favourite_player: str | None = Field(default=None, max_length=80)


class ReplayManager:
    def __init__(self) -> None:
        self.state = MatchState()
        self.stats = StatsAggregator()
        self.speed = 50.0
        self.mode = "Broadcast"
        self.language = "English"
        self.favourite_team = "Neutral"
        self.favourite_player: str | None = None
        self.status = "idle"
        self.error: str | None = None
        self.match_clock = 0.0
        self._lock = threading.RLock()
        self._stop_event: threading.Event | None = None
        self._engine: ReplayEngine | None = None
        self._task: asyncio.Task[None] | None = None
        self._loop: asyncio.AbstractEventLoop | None = None
        self._subscribers: set[asyncio.Queue[str]] = set()
        self.orchestrator = Orchestrator(framework_agents_enabled=True)
        self.stats = StatsAggregator()
        self.feed_processor: FeedProcessor | None = None
        self._agent_tasks: set[asyncio.Task[Any]] = set()
        self.overlays = OverlayHistory()
        self._last_record_wall_time = datetime.now(timezone.utc)
        self.overlay_latency_ms: float | None = None
        self.webpubsub_sink: OverlaySink | None = WebPubSubOverlaySink.from_environment()

    def settings(self) -> dict[str, Any]:
        with self._lock:
            return {
                "mode": self.mode,
                "language": self.language,
                "favourite_team": self.favourite_team,
                "favourite_player": self.favourite_player,
            }

    def compact_snapshot(self, tick: dict[str, Any] | None = None) -> dict[str, Any]:
        with self._lock:
            state = self.state
            return {
                "minute": round(state.minute, 2),
                "match_clock": self.match_clock,
                "score": dict(state.score),
                "source": state.source,
                "status": self.status,
                "speed": self.speed,
                "mode": self.mode,
                "language": self.language,
                "favourite_team": self.favourite_team,
                "favourite_player": self.favourite_player,
                "event_count": len(state.events),
                "frame_count": len(state.frames),
                "overlay_latency_ms": self.overlay_latency_ms,
                "events": [dict(event) for event in state.events[-80:]],
                "frame": dict(state.frames[-1]) if state.frames else None,
                "shape": dict(state.shape),
                "patterns": dict(state.patterns),
                "impact": list(state.impact[:10]),
                "momentum_series": list(state.momentum_series),
                "moments": list(state.moments[-30:]),
                "narration": dict(state.narration_log[-1]) if state.narration_log else None,
                "tick": tick,
                "error": self.error,
            }

    def publish_from_worker(self, tick: dict[str, Any]) -> None:
        if self._loop is None or self._loop.is_closed():
            return
        narration = tick.get("narration")
        if isinstance(narration, dict):
            self.publish_narration_from_worker(narration)
        snapshot = self.compact_snapshot(tick)
        self._loop.call_soon_threadsafe(self._fanout, snapshot)

    def publish_facts_from_worker(
        self,
        facts: dict[str, Any],
        *,
        state: MatchState | None = None,
        match_clock: float | None = None,
        event_wall_time: datetime | None = None,
    ) -> None:
        settings = self.settings()
        state = state or self.state
        emitted_from = event_wall_time or self._last_record_wall_time
        match_id = (
            str(state.events[-1].get("match_id") or "")
            if state.events
            else ""
        ) or "overseer-match"
        current_match_clock = (
            self.match_clock if match_clock is None else max(0.0, match_clock)
        )
        moments = facts.get("moments")
        fact_moments = moments if isinstance(moments, list) else []
        if not fact_moments:
            if self._loop is not None and not self._loop.is_closed():
                snapshot = self.compact_snapshot({"facts": facts})
                self._loop.call_soon_threadsafe(self._fanout, snapshot)
            return

        for moment in fact_moments[:3]:
            fact_id = str(moment.get("id") or stable_fact_id("fact", moment))
            headline, body, agent_name = self._fact_copy(moment, state)
            latency_ms = max(
                0.0,
                (datetime.now(timezone.utc) - emitted_from).total_seconds() * 1000,
            )
            markings = []
            event = next(
                (
                    item
                    for item in reversed(state.events)
                    if item.get("player") == moment.get("player")
                    and item.get("x") is not None
                    and item.get("y") is not None
                ),
                None,
            )
            if event is not None:
                markings = [{
                    "kind": "spotlight",
                    "points": [{"x": event["x"], "y": event["y"]}],
                }]
            overlay = self.overlays.add({
                "type": "fact",
                "match_id": match_id,
                "match_clock": current_match_clock,
                "start_tc": match_timecode(current_match_clock),
                "duration_ms": 8_000,
                "priority": 80 if moment.get("type") == "goal" else 50,
                "latency_ms": latency_ms,
                "mode": settings["mode"],
                "language": settings["language"],
                "headline": headline,
                "body": body,
                "stat": {
                    "label": "Score",
                    "value": (
                        f"{state.score.get('Rovers', 0)} — "
                        f"{state.score.get('United', 0)}"
                    ),
                },
                "player": moment.get("player"),
                "team": moment.get("team"),
                "markings": markings,
                "source_agent": agent_name,
                "fact_ids": [fact_id],
            })
            self.overlay_latency_ms = latency_ms
            self._publish_overlay(overlay)

        if self._loop is not None and not self._loop.is_closed():
            snapshot = self.compact_snapshot({"facts": facts})
            self._loop.call_soon_threadsafe(self._fanout, snapshot)
            state_snapshot = copy.deepcopy(state)
            self._loop.call_soon_threadsafe(
                self._schedule_flagged_agent_tasks,
                state_snapshot,
                settings,
                current_match_clock,
                emitted_from,
            )

    def _schedule_flagged_agent_tasks(
        self,
        state: MatchState,
        settings: dict[str, Any],
        match_clock: float,
        event_wall_time: datetime,
    ) -> None:
        analysis_task = asyncio.create_task(
            self._run_live_agent_analysis(state, settings["mode"])
        )
        self._agent_tasks.add(analysis_task)
        analysis_task.add_done_callback(self._finish_narration_task)
        narration_task = asyncio.create_task(
            self._narrate_live_update(
                state,
                settings,
                match_clock,
                event_wall_time,
            )
        )
        self._agent_tasks.add(narration_task)
        narration_task.add_done_callback(self._finish_narration_task)

    @staticmethod
    def _fact_copy(moment: dict[str, Any], state: MatchState) -> tuple[str, str, str]:
        moment_type = str(moment.get("type", ""))
        team = str(moment.get("team") or "")
        player = str(moment.get("player") or "")
        why = str(moment.get("why") or "")
        if moment_type == "goal":
            club = team or "their side"
            name = player or (f"A {club} player" if team else "A player")
            return f"GOAL · {name}", f"{name} scores for {club}.", "MomentDetector"
        if moment_type == "momentum_swing":
            return "Momentum shift", why, "ImpactAnalyst"
        if moment_type in {"formation_change", "block_change"}:
            return "Tactical shape change", why, "ShapeAnalyst"
        return (
            f"Match update · {state.minute:.1f}'",
            why or f"Rovers {state.score.get('Rovers', 0)} — {state.score.get('United', 0)} United",
            "Orchestrator",
        )

    def publish_narration_from_worker(
        self,
        narration: dict[str, Any],
        *,
        state: MatchState | None = None,
        match_clock: float | None = None,
        event_wall_time: datetime | None = None,
    ) -> None:
        state = state or self.state
        emitted_from = event_wall_time or self._last_record_wall_time
        current_match_clock = (
            self.match_clock if match_clock is None else max(0.0, match_clock)
        )
        latency_ms = max(
            0.0,
            (datetime.now(timezone.utc) - emitted_from).total_seconds() * 1000,
        )
        self.overlay_latency_ms = latency_ms
        overlay = self.overlays.add({
            "type": "narration",
            "match_id": (
                str(state.events[-1].get("match_id") or "")
                if state.events
                else ""
            ) or "overseer-match",
            "match_clock": current_match_clock,
            "start_tc": match_timecode(current_match_clock),
            "duration_ms": 9_000,
            "priority": 40,
            "latency_ms": latency_ms,
            "mode": narration.get("mode", self.mode),
            "language": narration.get("language", self.language),
            "headline": (
                "Template fallback"
                if narration.get("fallback")
                else "Match commentary"
            ),
            "body": str(narration.get("text") or ""),
            "player": narration.get("facts", {}).get("selected_player"),
            "team": narration.get("favourite_team"),
            "source_agent": "Narrator",
            "fact_ids": [str(narration.get("fact_id") or "narration")],
        })
        self._publish_overlay(overlay)

    async def on_feed_update(
        self,
        message: FeedMessage,
        state: MatchState,
    ) -> None:
        self.state = state
        self.status = "live"
        self.match_clock = message.match_clock
        record = {**message.record, "source": message.source}
        if message.kind == "event":
            self.stats.add_event(record)
        else:
            self.stats.add_frame(record)
        self._last_record_wall_time = datetime.fromisoformat(
            message.wall_time_utc.replace("Z", "+00:00")
        ).astimezone(timezone.utc)
        event_wall_time = self._last_record_wall_time
        self._fanout(self.compact_snapshot())
        if message.kind != "event":
            return

        facts = await asyncio.to_thread(self.orchestrator.analyze_facts, state)
        self.publish_facts_from_worker(
            facts,
            state=state,
            match_clock=message.match_clock,
            event_wall_time=event_wall_time,
        )

    async def _run_live_agent_analysis(
        self,
        state: MatchState,
        mode: str,
    ) -> None:
        answers = await self.orchestrator.run_routed_agents(state, mode=mode)
        if answers:
            snapshot = self.compact_snapshot()
            snapshot["agent_answers"] = answers
            self._fanout(snapshot)

    async def _narrate_live_update(
        self,
        state: MatchState,
        settings: dict[str, Any],
        match_clock: float,
        event_wall_time: datetime,
    ) -> None:
        narration = await self.orchestrator.narrator.narrate(
            state,
            mode=settings["mode"],
            language=settings["language"],
            favourite_team=settings["favourite_team"],
            favourite_player=settings["favourite_player"],
        )
        self.publish_narration_from_worker(
            narration,
            state=state,
            match_clock=match_clock,
            event_wall_time=event_wall_time,
        )
        self._fanout(self.compact_snapshot())

    def _finish_narration_task(self, task: asyncio.Task[Any]) -> None:
        self._agent_tasks.discard(task)
        if task.cancelled():
            return
        error = task.exception()
        if error is not None:
            logger.error(
                "Live background agent task failed (%s)",
                error.__class__.__name__,
                exc_info=(type(error), error, error.__traceback__),
            )

    def _publish_overlay(self, overlay: Overlay) -> None:
        if self._loop is None or self._loop.is_closed():
            return
        payload = json.dumps(
            overlay.model_dump(mode="json"),
            ensure_ascii=False,
            separators=(",", ":"),
        )
        self._loop.call_soon_threadsafe(self._fanout_message, f"event: overlay\ndata: {payload}\n\n")
        if self.webpubsub_sink is not None:
            self._loop.call_soon_threadsafe(self._schedule_webpubsub_publish, overlay)

    def _schedule_webpubsub_publish(self, overlay: Overlay) -> None:
        sink = self.webpubsub_sink
        if sink is None:
            return
        task = asyncio.create_task(sink.publish(overlay))

        def log_publish_failure(completed: asyncio.Task[None]) -> None:
            if completed.cancelled():
                return
            error = completed.exception()
            if error is not None:
                logger.error(
                    "Web PubSub overlay publish failed (%s)",
                    error.__class__.__name__,
                    exc_info=(type(error), error, error.__traceback__),
                )

        task.add_done_callback(log_publish_failure)

    async def close_overlay_sink(self) -> None:
        if self.webpubsub_sink is not None:
            await self.webpubsub_sink.close()
            self.webpubsub_sink = None

    def _fanout_message(self, message: str) -> None:
        for queue in tuple(self._subscribers):
            if queue.full():
                try:
                    queue.get_nowait()
                except asyncio.QueueEmpty:
                    pass
            try:
                queue.put_nowait(message)
            except asyncio.QueueFull:
                pass

    def _fanout(self, snapshot: dict[str, Any]) -> None:
        payload = json.dumps(snapshot, ensure_ascii=False, separators=(",", ":"))
        for queue in tuple(self._subscribers):
            if queue.full():
                try:
                    queue.get_nowait()
                except asyncio.QueueEmpty:
                    pass
            try:
                queue.put_nowait(payload)
            except asyncio.QueueFull:
                pass

    async def start(self) -> None:
        if self.feed_processor is not None:
            raise HTTPException(
                status_code=409,
                detail="Replay controls are unavailable while the live feed is active",
            )
        if self._task and not self._task.done():
            raise HTTPException(status_code=409, detail="Replay is already running")
        self.state = MatchState()
        self.error = None
        self.match_clock = 0.0
        self.status = "starting"
        self.overlays.clear()
        self.overlay_latency_ms = None
        self._loop = asyncio.get_running_loop()
        self._stop_event = threading.Event()
        self._engine = ReplayEngine(
            self.state,
            speed=self.speed,
            on_tick=self.publish_from_worker,
            on_fact=self.publish_facts_from_worker,
            defer_model_calls=True,
            stop_event=self._stop_event,
            config_provider=self.settings,
            on_record=self._on_replay_record,
        )
        self.status = "running"
        self._task = asyncio.create_task(self._run_worker())
        self._fanout(self.compact_snapshot())

    def _on_replay_record(self, kind: str, record: dict[str, Any]) -> None:
        if kind == "event":
            self.stats.add_event(record)
        else:
            self.stats.add_frame(record)
        wall_time = record.get("wall_time_utc")
        if wall_time is None:
            self._last_record_wall_time = datetime.now(timezone.utc)
        elif isinstance(wall_time, str):
            parsed_wall_time = datetime.fromisoformat(wall_time.replace("Z", "+00:00"))
            if (
                parsed_wall_time.tzinfo is None
                or parsed_wall_time.utcoffset() != timedelta(0)
            ):
                raise ValueError("record wall_time_utc must include a UTC offset")
            self._last_record_wall_time = parsed_wall_time.astimezone(timezone.utc)
        else:
            raise ValueError("record wall_time_utc must be an ISO-8601 string")
        self.match_clock = max(0.0, float(self.state.minute) * 60.0)
    async def _run_worker(self) -> None:
        try:
            datasets = await asyncio.to_thread(default_datasets)
            engine = self._engine
            if engine is None:
                raise RuntimeError("Replay engine was not initialized")
            await asyncio.to_thread(engine.run, datasets, **self.settings())
            with self._lock:
                self.status = "stopped" if self._stop_event and self._stop_event.is_set() else "complete"
        except Exception as error:
            with self._lock:
                self.error = f"{error.__class__.__name__}: {error}"
                self.status = "error"
        finally:
            self._fanout(self.compact_snapshot())

    async def stop(self) -> None:
        if self.feed_processor is not None:
            raise HTTPException(
                status_code=409,
                detail="The live feed is managed by the service lifecycle",
            )
        if self._stop_event:
            self._stop_event.set()
        if self._task and not self._task.done():
            await self._task
        elif self.status in ("idle", "running", "starting"):
            self.status = "stopped"
        self._fanout(self.compact_snapshot())

    async def update(self, data: ControlRequest) -> dict[str, Any]:
        if data.speed is not None:
            self.speed = data.speed
            if self._engine:
                self._engine.speed = data.speed
        if data.mode is not None:
            self.mode = data.mode
        if data.language is not None:
            self.language = data.language
        if data.favourite_team is not None:
            self.favourite_team = data.favourite_team
        if "favourite_player" in data.model_fields_set:
            self.favourite_player = (
                data.favourite_player.strip() if data.favourite_player else None
            )

        if data.action == "stop":
            await self.stop()
        elif data.action == "start":
            await self.start()
        else:
            self._fanout(self.compact_snapshot())
        return self.compact_snapshot()

    def full_snapshot(self) -> dict[str, Any]:
        with self._lock:
            snapshot = self.state.snapshot()
            snapshot.update(
                {
                    "status": self.status,
                    "match_clock": self.match_clock,
                    "speed": self.speed,
                    "mode": self.mode,
                    "language": self.language,
                    "favourite_team": self.favourite_team,
                    "favourite_player": self.favourite_player,
                    "error": self.error,
                    "frame": self.state.frames[-1] if self.state.frames else None,
                    "event_count": len(self.state.events),
                    "frame_count": len(self.state.frames),
                    "overlay_latency_ms": self.overlay_latency_ms,
                }
            )
            return snapshot


manager = ReplayManager()


@asynccontextmanager
async def lifespan(_: FastAPI):
    initialize_telemetry()
    manager._loop = asyncio.get_running_loop()
    feed_stop: asyncio.Event | None = None
    feed_task: asyncio.Task[None] | None = None
    processor: FeedProcessor | None = None
    if os.getenv("EVENTHUB_NAMESPACE", "").strip():
        processor = FeedProcessor(
            create_feed_consumer(),
            on_update=manager.on_feed_update,
        )
        manager.feed_processor = processor
        manager.status = "live"
        feed_stop = asyncio.Event()
        feed_task = asyncio.create_task(processor.run(feed_stop))

        def report_feed_error(task: asyncio.Task[None]) -> None:
            if task.cancelled():
                return
            error = task.exception()
            if error is not None:
                manager.status = "error"
                manager.error = f"{error.__class__.__name__}: {error}"
                logger.error(
                    "Live feed processor stopped unexpectedly (%s)",
                    error.__class__.__name__,
                    exc_info=(type(error), error, error.__traceback__),
                )

        feed_task.add_done_callback(report_feed_error)
    try:
        yield
    finally:
        if manager._task and not manager._task.done():
            await manager.stop()
        if feed_stop is not None:
            feed_stop.set()
        if feed_task is not None:
            try:
                await feed_task
            except Exception as error:
                logger.error(
                    "Live feed processor raised during shutdown (%s)",
                    error.__class__.__name__,
                    exc_info=(type(error), error, error.__traceback__),
                )
        if processor is not None:
            await processor.close()
        manager.feed_processor = None
        for task in tuple(manager._agent_tasks):
            task.cancel()
        if manager._agent_tasks:
            await asyncio.gather(*manager._agent_tasks, return_exceptions=True)
        await manager.close_overlay_sink()


app = FastAPI(title="Overseer", version="0.1.0", lifespan=lifespan)
_cors_origins = [
    origin.strip()
    for origin in os.getenv("CORS_ALLOWED_ORIGINS", "").split(",")
    if origin.strip()
]
if "*" in _cors_origins:
    raise RuntimeError("CORS_ALLOWED_ORIGINS must not contain a wildcard")
app.add_middleware(
    CORSMiddleware,
    allow_origins=_cors_origins,
    allow_methods=["*"],
    allow_headers=["*"],
)


@app.get("/", include_in_schema=False)
async def index() -> FileResponse:
    return FileResponse(WEB_DIR / "index.html")

@app.get("/overlay", include_in_schema=False)
@app.get("/overlay.html", include_in_schema=False)
async def overlay_page() -> FileResponse:
    return FileResponse(WEB_DIR / "overlay.html")

@app.get("/overlay.js", include_in_schema=False)
async def overlay_script() -> FileResponse:
    return FileResponse(WEB_DIR / "overlay.js", media_type="text/javascript")


@app.get("/app.js", include_in_schema=False)
async def app_script() -> FileResponse:
    return FileResponse(WEB_DIR / "app.js", media_type="text/javascript")


@app.get("/styles.css", include_in_schema=False)
async def styles() -> FileResponse:
    return FileResponse(WEB_DIR / "styles.css", media_type="text/css")


@app.get("/config.js", include_in_schema=False)
async def frontend_config() -> FileResponse:
    return FileResponse(WEB_DIR / "config.js", media_type="text/javascript")

@app.get("/overlays/recent", response_model=list[Overlay])
async def get_recent_overlays(
    limit: int = Query(default=100, ge=1, le=500),
) -> list[Overlay]:
    return manager.overlays.recent(limit)


@app.get("/state")
async def get_state() -> dict[str, Any]:
    return manager.full_snapshot()


@app.get("/stats/players")
async def get_player_stats(
    team: Literal["Rovers", "United"] | None = None,
) -> dict[str, Any]:
    return {
        "minute": round(manager.state.minute, 2),
        "players": manager.stats.player_stats(team),
    }


@app.get("/stats/teams")
async def get_team_stats(
    team: Literal["Rovers", "United"] | None = None,
) -> dict[str, Any]:
    return {
        "minute": round(manager.state.minute, 2),
        "teams": manager.stats.team_stats(team),
    }


@app.get("/frame")
async def get_frame(
    minute: float = Query(ge=0),
    t: float | None = Query(default=None, ge=0),
    source: Literal["synthetic"] | None = None,
) -> dict[str, Any]:
    frames = manager.state.frames
    if not frames:
        raise HTTPException(status_code=404, detail="No replay frames are available yet")
    if source is not None:
        frames = [frame for frame in frames if frame.get("source") == source]
        if not frames:
            raise HTTPException(
                status_code=404,
                detail=f"No {source} replay frames are available",
            )
    if t is not None:
        target_time = t

        def frame_time(item: dict[str, Any]) -> float:
            return float(item.get("t", float(item.get("minute", 0.0)) * 60.0))
    else:
        target_time = minute * 60.0

        def frame_time(item: dict[str, Any]) -> float:
            return float(item.get("minute", 0.0)) * 60.0

    frame = min(
        frames,
        key=lambda item: abs(frame_time(item) - target_time),
    )
    return {"frame": frame}


@app.post("/control")
async def control(data: ControlRequest) -> dict[str, Any]:
    return await manager.update(data)


@app.get("/stream")
async def stream(request: Request) -> StreamingResponse:
    queue: asyncio.Queue[str] = asyncio.Queue(maxsize=64)
    manager._subscribers.add(queue)
    await queue.put(json.dumps(manager.compact_snapshot(), ensure_ascii=False))

    async def events():
        try:
            while not await request.is_disconnected():
                try:
                    message = await asyncio.wait_for(queue.get(), timeout=15.0)
                    if message.startswith("event: overlay"):
                        yield message
                    else:
                        yield f"data: {message}\n\n"
                except asyncio.TimeoutError:
                    yield ": keep-alive\n\n"
        finally:
            manager._subscribers.discard(queue)

    return StreamingResponse(
        events(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "X-Accel-Buffering": "no",
            "Connection": "keep-alive",
        },
    )
