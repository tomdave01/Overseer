"""FastAPI service, live feed processor, and replay development adapter."""

from __future__ import annotations

import asyncio
import copy
import json
import logging
import os
import threading
from collections import deque
import time
from contextlib import asynccontextmanager
from datetime import datetime, timedelta, timezone
from math import isfinite
from pathlib import Path
from typing import Any, Literal
from uuid import uuid4

from fastapi import FastAPI, HTTPException, Query, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, StreamingResponse
from pydantic import BaseModel, Field

from agents.orchestrator import Orchestrator
from api.commentary import CommentaryHistory
from api.overlays import (
    Overlay,
    OverlayHistory,
    OverlaySink,
    WebPubSubOverlaySink,
    match_timecode,
    stable_fact_id,
)
from engine.replay import ReplayDataset, ReplayEngine, default_datasets
from engine.delay import DelayBuffer
from engine.stats import StatsAggregator
from engine.state import MatchState
from feed.clients import create_feed_consumer
from feed.models import FeedMessage
from feed.processor import FeedProcessor
from telemetry import initialize_telemetry


ROOT = Path(__file__).resolve().parent.parent
WEB_DIR = ROOT / "web"
logger = logging.getLogger(__name__)


def _configured_seconds(name: str, default: float, *, allow_zero: bool = False) -> float:
    raw = os.getenv(name)
    if raw is None:
        return default
    try:
        value = float(raw)
    except ValueError as error:
        raise ValueError(f"{name} must be a positive number") from error
    if not isfinite(value) or value < 0 or (value == 0 and not allow_zero):
        requirement = "non-negative" if allow_zero else "positive"
        raise ValueError(f"{name} must be {requirement}")
    return value


class ControlRequest(BaseModel):
    action: Literal["start", "stop"] | None = None
    speed: float | None = Field(default=None, gt=0, le=1000)
    mode: Literal["Broadcast", "Analyst", "Fan"] | None = None
    language: Literal["English", "Spanish", "Arabic", "French"] | None = None
    favourite_team: Literal["Rovers", "United", "Neutral"] | None = None
    favourite_player: str | None = Field(default=None, max_length=80)


class RecapRequest(BaseModel):
    mode: Literal["Broadcast", "Analyst", "Fan"] | None = None
    language: Literal["English", "Spanish", "Arabic", "French"] | None = None
    favourite_team: Literal["Rovers", "United", "Neutral"] | None = None


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
        self.display_clock = 0.0
        self.stream_delay_seconds = _configured_seconds(
            "STREAM_DELAY_SECONDS", 8.0, allow_zero=True
        )
        self.analysis_tick_seconds = _configured_seconds(
            "ANALYSIS_TICK_SECONDS", 3.0
        )
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
        self._agent_latencies: deque[float] = deque(maxlen=500)
        self.agent_answers: dict[str, str] = {}
        self.agent_status: dict[str, dict[str, Any]] = {
            name: {
                "status": "idle",
                "latency_ms": None,
                "output_id": None,
                "task": None,
            }
            for name in (
                "ShapeAnalyst",
                "PatternSpotter",
                "ImpactAnalyst",
                "MomentDetector",
            )
        }
        self._agent_active_tasks: dict[str, dict[str, dict[str, Any]]] = {
            name: {} for name in self.agent_status
        }
        self.delay_buffer = DelayBuffer()
        self.commentary_history = CommentaryHistory(max_items=500)
        self._analysis_queue: asyncio.Queue[dict[str, Any]] | None = None
        self._fact_consumer_task: asyncio.Task[None] | None = None
        self._narration_queue: asyncio.Queue[dict[str, Any]] | None = None
        self._narration_consumer_task: asyncio.Task[None] | None = None
        self._last_analysis_clock = -self.analysis_tick_seconds
        self._display_snapshot: dict[str, Any] | None = None
        self._pending_display_snapshot: dict[str, Any] | None = None
        self._snapshot_publish_handle: asyncio.TimerHandle | None = None
        self._last_snapshot_publish_at = 0.0
        self.overlays = OverlayHistory()
        self._last_record_wall_time = datetime.now(timezone.utc)
        self.overlay_latency_ms: float | None = None
        self._scheduled_recap_phases: set[str] = set()
        self.recap_results: dict[str, dict[str, Any]] = {}
        self.webpubsub_sink: OverlaySink | None = WebPubSubOverlaySink.from_environment()
        self._display_snapshot = self.compact_snapshot()

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
                "ingest_clock": self.match_clock,
                "display_clock": self.display_clock,
                "stream_delay_seconds": self.stream_delay_seconds,
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
                "agents": copy.deepcopy(self.agent_status),
                "agent_latency_avg_ms": (
                    sum(self._agent_latencies) / len(self._agent_latencies)
                    if self._agent_latencies
                    else None
                ),
                "agent_latency_max_ms": (
                    max(self._agent_latencies) if self._agent_latencies else None
                ),
                "events": [dict(event) for event in state.events[-80:]],
                "frame": dict(state.frames[-1]) if state.frames else None,
                "shape": dict(state.shape),
                "patterns": dict(state.patterns),
                "impact": list(state.impact[:10]),
                "momentum_series": list(state.momentum_series),
                "moments": list(state.moments[-30:]),
                "narration": dict(state.narration_log[-1]) if state.narration_log else None,
                "agent_answers": dict(self.agent_answers),
                "tick": tick,
                "error": self.error,
            }

    def public_snapshot(self) -> dict[str, Any]:
        with self._lock:
            if self._display_snapshot is None:
                return self.compact_snapshot()
            snapshot = copy.deepcopy(self._display_snapshot)
            snapshot.update({
                "status": self.status,
                "speed": self.speed,
                "mode": self.mode,
                "language": self.language,
                "favourite_team": self.favourite_team,
                "favourite_player": self.favourite_player,
                "stream_delay_seconds": self.stream_delay_seconds,
                "agents": copy.deepcopy(self.agent_status),
                "agent_answers": dict(self.agent_answers),
                "agent_latency_avg_ms": (
                    sum(self._agent_latencies) / len(self._agent_latencies)
                    if self._agent_latencies
                    else None
                ),
                "agent_latency_max_ms": (
                    max(self._agent_latencies) if self._agent_latencies else None
                ),
                "error": self.error,
            })
            return snapshot

    def publish_from_worker(self, tick: dict[str, Any]) -> None:
        if self._loop is not None and not self._loop.is_closed():
            narration = tick.get("narration")
            if isinstance(narration, dict):
                self.publish_narration_from_worker(narration)
            snapshot = self.compact_snapshot(tick)
            self._queue_stream_snapshot(snapshot, self.match_clock)

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

        if self._loop is not None and not self._loop.is_closed():
            state_snapshot = state.analysis_snapshot()
            self._loop.call_soon_threadsafe(
                self._schedule_analysis_tasks,
                state_snapshot,
                settings,
                current_match_clock,
                emitted_from,
                copy.deepcopy(fact_moments),
            )

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
            start_clock = max(
                0.0,
                float(moment.get("minute", state.minute)) * 60.0,
            )
            overlay = self.overlays.create({
                "type": "fact",
                "match_id": match_id,
                "match_clock": start_clock,
                "start_tc": match_timecode(start_clock),
                "ingest_tc": match_timecode(current_match_clock),
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

    def _schedule_analysis_tasks(
        self,
        state: MatchState,
        settings: dict[str, Any],
        match_clock: float,
        event_wall_time: datetime,
        flagged_moments: list[dict[str, Any]],
    ) -> None:
        self._ensure_fact_consumer()
        task = asyncio.create_task(
            self._run_analysis_tick(
                state,
                settings,
                match_clock,
                event_wall_time,
                flagged_moments,
            )
        )
        self._agent_tasks.add(task)
        task.add_done_callback(self._finish_narration_task)

    def _ensure_fact_consumer(self) -> None:
        if self._analysis_queue is None:
            self._analysis_queue = asyncio.Queue(maxsize=64)
        if self._narration_queue is None:
            self._narration_queue = asyncio.Queue(maxsize=16)
        if self._fact_consumer_task is None or self._fact_consumer_task.done():
            self._fact_consumer_task = asyncio.create_task(
                self._consume_analysis_facts()
            )
        if (
            self._narration_consumer_task is None
            or self._narration_consumer_task.done()
        ):
            self._narration_consumer_task = asyncio.create_task(
                self._consume_narrations()
            )

    def _mark_agent_working(self, task: dict[str, Any]) -> None:
        with self._lock:
            agent = task["agent"]
            self._agent_active_tasks[agent][task["task_id"]] = copy.deepcopy(task)
            self.agent_status[agent].update({
                "status": "working",
                "task": copy.deepcopy(task),
                "error": None,
            })
        self._fanout(self.public_snapshot())

    def _mark_agent_complete(
        self,
        agent: str,
        task: dict[str, Any],
        latency_ms: float,
        output_id: str,
    ) -> None:
        with self._lock:
            active = self._agent_active_tasks[agent]
            active.pop(task["task_id"], None)
            self.agent_status[agent].update({
                "status": "working" if active else "idle",
                "latency_ms": latency_ms,
                "output_id": output_id,
                "task": copy.deepcopy(next(reversed(list(active.values())), task)),
                "error": None,
            })
            self._agent_latencies.append(latency_ms)
        self._fanout(self.public_snapshot())

    async def _run_analysis_tick(
        self,
        state: MatchState,
        settings: dict[str, Any],
        match_clock: float,
        event_wall_time: datetime,
        flagged_moments: list[dict[str, Any]],
    ) -> None:
        model_tasks: dict[str, dict[str, Any]] = {}
        model_task: asyncio.Task[dict[str, str]] | None = None
        model_started = time.perf_counter()

        def on_model_agent_complete(
            agent_name: str,
            answer: str,
            latency_ms: float,
        ) -> None:
            task = model_tasks[agent_name]
            self._mark_agent_complete(
                agent_name,
                task,
                latency_ms,
                stable_fact_id("agent-answer", answer or task["task_id"]),
            )
            if answer:
                with self._lock:
                    self.agent_answers[agent_name] = answer
                self._fanout(self.public_snapshot())

        try:
            if (
                flagged_moments
                and getattr(self.orchestrator, "framework_agents_enabled", False)
            ):
                for agent_name in self.orchestrator.agents_for_mode(settings["mode"]):
                    created_at = datetime.now(timezone.utc)
                    task = {
                        "task_id": str(uuid4()),
                        "agent": agent_name,
                        "window": {
                            "start_minute": max(0.0, state.minute - 10.0),
                            "end_minute": state.minute,
                        },
                        "deadline": (
                            created_at
                            + timedelta(seconds=max(6.0, self.stream_delay_seconds))
                        ).isoformat(),
                        "created_at": created_at.isoformat(),
                    }
                    model_tasks[agent_name] = task
                    self._mark_agent_working(task)
                model_task = asyncio.create_task(
                    self.orchestrator.run_routed_agents(
                        state,
                        mode=settings["mode"],
                        on_agent_complete=on_model_agent_complete,
                    )
                )
            async for result in self.orchestrator.iter_analysis_tasks(
                state,
                flagged_moments=flagged_moments,
                deadline_seconds=max(6.0, self.stream_delay_seconds),
                on_task_created=self._mark_agent_working,
            ):
                task = result["task"]
                agent_name = task["agent"]
                latency_ms = result["latency_ms"]
                self._mark_agent_complete(
                    agent_name,
                    task,
                    latency_ms,
                    result["output_id"],
                )
                self._enqueue_analysis_fact({
                    "agent": agent_name,
                    "facts": result["facts"],
                    "flagged_moments": flagged_moments,
                    "state": (
                        copy.deepcopy(result["state"])
                        if agent_name == "MomentDetector" and flagged_moments
                        else None
                    ),
                    "settings": settings,
                    "match_clock": match_clock,
                    "event_wall_time": event_wall_time,
                    "task": task,
                    "priority": (
                        100
                        if any(
                            moment.get("type") == "goal"
                            for moment in flagged_moments
                        )
                        else 30
                        if flagged_moments
                        else 10
                    ),
                })
            if model_task is not None:
                answers = await model_task
                for agent_name, task in model_tasks.items():
                    with self._lock:
                        still_active = (
                            task["task_id"]
                            in self._agent_active_tasks[agent_name]
                        )
                    if still_active:
                        self._mark_agent_complete(
                            agent_name,
                            task,
                            (time.perf_counter() - model_started) * 1000,
                            stable_fact_id(
                                "agent-answer",
                                answers.get(agent_name) or task["task_id"],
                            ),
                        )
        except Exception as error:
            if model_task is not None and not model_task.done():
                model_task.cancel()
                await asyncio.gather(model_task, return_exceptions=True)
            with self._lock:
                for agent_name, task in model_tasks.items():
                    self._agent_active_tasks[agent_name].pop(
                        task["task_id"],
                        None,
                    )
                for agent_name, agent in self.agent_status.items():
                    if agent["status"] == "working":
                        self._agent_active_tasks[agent_name].clear()
                        agent.update(
                            {
                                "status": "idle",
                                "error": f"{error.__class__.__name__}: {error}",
                            }
                        )
            logger.error(
                "Concurrent analyst task failed (%s)",
                error.__class__.__name__,
                exc_info=(type(error), error, error.__traceback__),
            )

    def _enqueue_analysis_fact(self, item: dict[str, Any]) -> None:
        self._enqueue_bounded(self._analysis_queue, item, "analysis facts")

    def _enqueue_narration(self, item: dict[str, Any]) -> None:
        self._enqueue_bounded(self._narration_queue, item, "narration")

    @staticmethod
    def _enqueue_bounded(
        queue: asyncio.Queue[dict[str, Any]] | None,
        item: dict[str, Any],
        queue_name: str,
    ) -> None:
        if queue is None:
            raise RuntimeError(f"{queue_name} consumer was not initialized")
        if queue.full():
            pending: list[dict[str, Any]] = []
            while not queue.empty():
                queued = queue.get_nowait()
                queue.task_done()
                if queue_name == "analysis facts":
                    keep_item = queued.get("agent") != item.get("agent")
                else:
                    queued_ids = {
                        moment.get("id")
                        for moment in queued.get("flagged_moments", [])
                    }
                    item_ids = {
                        moment.get("id")
                        for moment in item.get("flagged_moments", [])
                    }
                    keep_item = not queued_ids.intersection(item_ids)
                if keep_item:
                    pending.append(queued)
            pending.append(item)
            if len(pending) > queue.maxsize:
                lowest_priority = min(queued["priority"] for queued in pending)
                drop_index = next(
                    index
                    for index, queued in enumerate(pending)
                    if queued["priority"] == lowest_priority
                )
                pending.pop(drop_index)
                logger.warning(
                    "Dropped low-priority %s under queue pressure",
                    queue_name,
                )
            for queued in pending:
                queue.put_nowait(queued)
            return
        queue.put_nowait(item)

    async def _consume_analysis_facts(self) -> None:
        queue = self._analysis_queue
        if queue is None:
            raise RuntimeError("Analysis fact queue was not initialized")
        while True:
            item = await queue.get()
            try:
                moments = item["flagged_moments"]
                if item["agent"] != "MomentDetector" or not moments:
                    continue
                self._enqueue_narration(item)
            finally:
                queue.task_done()

    async def _consume_narrations(self) -> None:
        queue = self._narration_queue
        if queue is None:
            raise RuntimeError("Narration queue was not initialized")
        while True:
            item = await queue.get()
            try:
                await self._narrate_live_update(
                    item["state"],
                    item["settings"],
                    item["match_clock"],
                    item["event_wall_time"],
                    min(
                        (
                            float(moment.get("minute", item["state"].minute))
                            * 60.0
                            for moment in item["flagged_moments"]
                        ),
                        default=item["match_clock"],
                    ),
                )
            except Exception as error:
                logger.error(
                    "Queued narration failed (%s)",
                    error.__class__.__name__,
                    exc_info=(type(error), error, error.__traceback__),
                )
            finally:
                queue.task_done()

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
        display_match_clock: float | None = None,
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
        display_start = (
            current_match_clock
            if display_match_clock is None
            else max(0.0, display_match_clock)
        )
        overlay = self.overlays.create({
            "type": "narration",
            "match_id": (
                str(state.events[-1].get("match_id") or "")
                if state.events
                else ""
            ) or "overseer-match",
            "match_clock": display_start,
            "start_tc": match_timecode(display_start),
            "ingest_tc": match_timecode(current_match_clock),
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
        self._ensure_fact_consumer()
        self._queue_stream_snapshot(self.compact_snapshot(), message.match_clock)
        if (
            message.match_clock >= 45 * 60
            and "halftime" not in self._scheduled_recap_phases
        ):
            self._scheduled_recap_phases.add("halftime")
            self._schedule_automatic_recap("halftime")
        is_goal = (
            message.kind == "event"
            and record.get("type") == "shot"
            and record.get("outcome") == "goal"
        )
        if (
            message.match_clock - self._last_analysis_clock
            >= self.analysis_tick_seconds
            or is_goal
        ):
            self._last_analysis_clock = message.match_clock
            facts = await asyncio.to_thread(self.orchestrator.analyze_facts, state)
            self.publish_facts_from_worker(
                facts,
                state=state,
                match_clock=message.match_clock,
                event_wall_time=event_wall_time,
            )

    async def _narrate_live_update(
        self,
        state: MatchState,
        settings: dict[str, Any],
        match_clock: float,
        event_wall_time: datetime,
        display_match_clock: float,
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
            display_match_clock=display_match_clock,
            event_wall_time=event_wall_time,
        )

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
        if overlay.match_clock < self.display_clock:
            overlay = self.overlays.mark_late(overlay)
            self._emit_overlay_now(overlay)
            return
        if overlay.match_clock == self.display_clock:
            self._emit_overlay_now(overlay)
            return
        self.delay_buffer.push(overlay.match_clock, "overlay", overlay)

    def _emit_overlay_now(self, overlay: Overlay) -> None:
        self.overlays.publish(overlay)
        if overlay.type == "narration":
            self.commentary_history.add(overlay.model_dump(mode="json"))
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

    def _queue_stream_snapshot(
        self,
        snapshot: dict[str, Any],
        ingest_clock: float,
    ) -> None:
        self.delay_buffer.push(ingest_clock, "snapshot", snapshot)
        self._advance_display_clock(
            max(0.0, ingest_clock - self.stream_delay_seconds)
        )

    def _advance_display_clock(self, display_clock: float) -> None:
        for item in self.delay_buffer.release_until(display_clock):
            if item.kind == "overlay":
                self._emit_overlay_now(item.payload)
                continue
            snapshot = item.payload
            with self._lock:
                snapshot["minute"] = round(item.match_clock / 60.0, 2)
                snapshot["match_clock"] = item.match_clock
                snapshot["display_clock"] = item.match_clock
                snapshot["ingest_clock"] = self.match_clock
                snapshot["stream_delay_seconds"] = self.stream_delay_seconds
                snapshot["agents"] = copy.deepcopy(self.agent_status)
                snapshot["agent_answers"] = dict(self.agent_answers)
                snapshot["agent_latency_avg_ms"] = (
                    sum(self._agent_latencies) / len(self._agent_latencies)
                    if self._agent_latencies
                    else None
                )
                snapshot["agent_latency_max_ms"] = (
                    max(self._agent_latencies) if self._agent_latencies else None
                )
                self.display_clock = max(self.display_clock, item.match_clock)
                self._display_snapshot = snapshot
            if self._loop is not None and not self._loop.is_closed():
                self._pending_display_snapshot = snapshot
                self._loop.call_soon_threadsafe(self._schedule_snapshot_fanout)

    def _schedule_snapshot_fanout(self) -> None:
        if self._pending_display_snapshot is None:
            return
        if self._snapshot_publish_handle is not None:
            return
        wait = max(
            0.0,
            self._last_snapshot_publish_at + 1 / 30 - time.monotonic(),
        )
        if wait:
            self._snapshot_publish_handle = asyncio.get_running_loop().call_later(
                wait,
                self._flush_snapshot_fanout,
            )
        else:
            self._flush_snapshot_fanout()

    def _flush_snapshot_fanout(self) -> None:
        self._snapshot_publish_handle = None
        snapshot = self._pending_display_snapshot
        self._pending_display_snapshot = None
        if snapshot is None:
            return
        self._last_snapshot_publish_at = time.monotonic()
        self._fanout(snapshot)

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
        previous_tasks = tuple(self._agent_tasks)
        for task in previous_tasks:
            task.cancel()
        if previous_tasks:
            await asyncio.gather(*previous_tasks, return_exceptions=True)
        if self._narration_consumer_task is not None:
            self._narration_consumer_task.cancel()
            await asyncio.gather(
                self._narration_consumer_task,
                return_exceptions=True,
            )
            self._narration_consumer_task = None
        for queue in (self._analysis_queue, self._narration_queue):
            if queue is not None:
                while not queue.empty():
                    queue.get_nowait()
                    queue.task_done()
        with self._lock:
            for agent_name, status in self.agent_status.items():
                self._agent_active_tasks[agent_name].clear()
                status.update({"status": "idle", "task": None, "error": None})
        self.state = MatchState()
        self.stats = StatsAggregator()
        self.error = None
        self.match_clock = 0.0
        self.display_clock = 0.0
        self._last_analysis_clock = -self.analysis_tick_seconds
        self.agent_answers.clear()
        self._scheduled_recap_phases.clear()
        self.recap_results.clear()
        self.delay_buffer.clear()
        self._display_snapshot = None
        self._pending_display_snapshot = None
        if self._snapshot_publish_handle is not None:
            self._snapshot_publish_handle.cancel()
            self._snapshot_publish_handle = None
        self._last_snapshot_publish_at = 0.0
        self.status = "starting"
        self.overlays.clear()
        self.overlay_latency_ms = None
        self._display_snapshot = self.compact_snapshot()
        self._loop = asyncio.get_running_loop()
        self._ensure_fact_consumer()
        self._stop_event = threading.Event()
        self._engine = ReplayEngine(
            self.state,
            speed=self.speed,
            on_fact=self.publish_facts_from_worker,
            defer_model_calls=True,
            stop_event=self._stop_event,
            config_provider=self.settings,
            on_record=self._on_replay_record,
            tick_interval=self.analysis_tick_seconds,
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

        self._queue_stream_snapshot(self.compact_snapshot(), self.match_clock)
        if (
            self.match_clock >= 45 * 60
            and "halftime" not in self._scheduled_recap_phases
            and self._loop is not None
            and not self._loop.is_closed()
        ):
            self._scheduled_recap_phases.add("halftime")
            self._loop.call_soon_threadsafe(
                self._schedule_automatic_recap,
                "halftime",
            )

    def _build_recap_facts(self, recap_type: str) -> dict[str, Any]:
        state = self.state
        moments = copy.deepcopy(state.moments)
        goals_seen = {
            (
                moment.get("team"),
                moment.get("player"),
                round(float(moment.get("minute", 0.0)), 3),
            )
            for moment in moments
            if moment.get("type") == "goal"
        }
        for event in state.events:
            if event.get("type") != "shot" or event.get("outcome") != "goal":
                continue
            identity = (
                event.get("team"),
                event.get("player"),
                round(float(event.get("minute", 0.0)), 3),
            )
            if identity not in goals_seen:
                moments.append({
                    "id": f"goal-{identity[0]}-{identity[2]:.3f}",
                    "type": "goal",
                    "team": identity[0],
                    "player": identity[1],
                    "minute": identity[2],
                    "why": (
                        f"{identity[1]} scored for {identity[0]}."
                        if identity[1]
                        else f"A goal was scored for {identity[0]}."
                    ),
                })
                goals_seen.add(identity)
        moments.sort(key=lambda moment: float(moment.get("minute", 0.0)))
        players = self.stats.player_stats(
            force_gravity_refresh=recap_type == "full_time"
        )
        top_influencers = sorted(
            players,
            key=lambda player: (
                -float(player.get("impact_score", 0.0)),
                player.get("team", ""),
                player.get("name", ""),
            ),
        )[:5]
        findings = copy.deepcopy(state.patterns.get("findings", {}))
        block_changes = [
            copy.deepcopy(change)
            for change in state.shape.get("changes", [])
            if change.get("id") == "block_change"
        ]
        block_change_keys = {
            (
                change.get("team"),
                round(float(change.get("minute", 0.0)), 2),
                change.get("to"),
            )
            for change in block_changes
        }
        for moment in moments:
            if moment.get("type") != "block_change":
                continue
            identity = (
                moment.get("team"),
                round(float(moment.get("minute", 0.0)), 2),
                moment.get("to"),
            )
            if identity not in block_change_keys:
                block_changes.append(copy.deepcopy(moment))
                block_change_keys.add(identity)
        momentum_swings = [
            copy.deepcopy(moment)
            for moment in moments
            if moment.get("type") == "momentum_swing"
        ]
        return {
            "recap_type": recap_type,
            "minute": round(state.minute, 2),
            "score": dict(state.score),
            "current_score": dict(state.score),
            "final_score": (
                dict(state.score) if recap_type == "full_time" else None
            ),
            "team_stats": self.stats.team_stats(),
            "key_moments": [
                {
                    "id": moment.get("id"),
                    "type": moment.get("type"),
                    "minute": moment.get("minute"),
                    "team": moment.get("team"),
                    "player": moment.get("player"),
                    "why": moment.get("why", ""),
                }
                for moment in moments[-20:]
            ],
            "top_influencers": [
                {
                    "team": player["team"],
                    "player": player["name"],
                    "score": player["impact_score"],
                    "components": player["impact_components"],
                }
                for player in top_influencers
            ],
            "notable_patterns": findings,
            "block_changes": block_changes[-10:],
            "momentum_swings": momentum_swings[-10:],
        }

    async def generate_recap(
        self,
        recap_type: str = "current",
        *,
        settings: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        selected = settings or self.settings()
        facts = await asyncio.to_thread(self._build_recap_facts, recap_type)
        recap = await self.orchestrator.narrator.narrate_recap(
            facts,
            recap_type=recap_type,
            mode=selected["mode"],
            language=selected["language"],
            favourite_team=selected["favourite_team"],
        )
        with self._lock:
            self.recap_results[recap_type] = copy.deepcopy(recap)
        return recap

    def _schedule_automatic_recap(self, recap_type: str) -> None:
        task = asyncio.create_task(self.generate_recap(recap_type))
        self._agent_tasks.add(task)
        task.add_done_callback(self._finish_narration_task)

    async def _run_worker(self) -> None:
        try:
            datasets = await asyncio.to_thread(default_datasets)
            engine = self._engine
            if engine is None:
                raise RuntimeError("Replay engine was not initialized")
            await asyncio.to_thread(engine.run, datasets, **self.settings())
            with self._lock:
                self.status = "stopped" if self._stop_event and self._stop_event.is_set() else "complete"
            if self.status == "complete":
                try:
                    await self.generate_recap("full_time")
                except Exception as error:
                    logger.error(
                        "Full-time recap generation failed (%s)",
                        error.__class__.__name__,
                        exc_info=(type(error), error, error.__traceback__),
                    )
            if self.status == "complete" and self.stream_delay_seconds:
                await asyncio.sleep(self.stream_delay_seconds / self.speed)
            if self.status == "complete":
                self._advance_display_clock(self.match_clock)
        except Exception as error:
            with self._lock:
                self.error = f"{error.__class__.__name__}: {error}"
                self.status = "error"
        finally:
            snapshot = copy.deepcopy(
                self._display_snapshot or self.compact_snapshot()
            )
            snapshot["status"] = self.status
            snapshot["error"] = self.error
            self._fanout(snapshot)

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
        self._fanout(self.public_snapshot())

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
            self._fanout(self.public_snapshot())
        return self.public_snapshot()

    def full_snapshot(self) -> dict[str, Any]:
        if self.status in ("idle", "complete"):
            with self._lock:
                snapshot = self.state.snapshot()
                snapshot.update({
                    "status": self.status,
                    "match_clock": self.match_clock,
                    "ingest_clock": self.match_clock,
                    "display_clock": self.display_clock,
                    "stream_delay_seconds": self.stream_delay_seconds,
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
                    "agents": copy.deepcopy(self.agent_status),
                    "agent_answers": dict(self.agent_answers),
                    "agent_latency_avg_ms": (
                        sum(self._agent_latencies) / len(self._agent_latencies)
                        if self._agent_latencies
                        else None
                    ),
                    "agent_latency_max_ms": (
                        max(self._agent_latencies)
                        if self._agent_latencies
                        else None
                    ),
                })
                return snapshot
        snapshot = self.public_snapshot()
        snapshot["display_clock"] = self.display_clock
        snapshot["ingest_clock"] = self.match_clock
        snapshot["event_count"] = (
            self._display_snapshot.get("event_count", 0)
            if self._display_snapshot
            else 0
        )
        snapshot["frame_count"] = (
            self._display_snapshot.get("frame_count", 0)
            if self._display_snapshot
            else 0
        )
        snapshot["overlay_latency_ms"] = self.overlay_latency_ms
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
        if manager._fact_consumer_task is not None:
            manager._fact_consumer_task.cancel()
            await asyncio.gather(
                manager._fact_consumer_task,
                return_exceptions=True,
            )
            manager._fact_consumer_task = None
        if manager._narration_consumer_task is not None:
            manager._narration_consumer_task.cancel()
            await asyncio.gather(
                manager._narration_consumer_task,
                return_exceptions=True,
            )
            manager._narration_consumer_task = None
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


@app.get("/agents/status")
async def get_agent_status() -> dict[str, Any]:
    return {
        "agents": copy.deepcopy(manager.agent_status),
        "average_latency_ms": (
            sum(manager._agent_latencies) / len(manager._agent_latencies)
            if manager._agent_latencies
            else None
        ),
        "max_latency_ms": (
            max(manager._agent_latencies) if manager._agent_latencies else None
        ),
        "stream_delay_seconds": manager.stream_delay_seconds,
    }


@app.get("/commentary")
async def get_commentary(
    limit: int = Query(default=100, ge=1, le=500),
    since: int | None = Query(default=None, ge=0),
) -> list[dict[str, Any]]:
    return manager.commentary_history.recent(limit=limit, since=since)


@app.get("/recap")
async def get_recap(
    recap_type: Literal["current", "halftime", "full_time"] = "current",
) -> dict[str, Any]:
    with manager._lock:
        existing = manager.recap_results.get(recap_type)
        if existing is not None:
            return copy.deepcopy(existing)
    return await manager.generate_recap(recap_type)


@app.post("/recap/generate")
async def generate_recap(data: RecapRequest | None = None) -> dict[str, Any]:
    selected = manager.settings()
    if data is not None:
        for field in ("mode", "language", "favourite_team"):
            value = getattr(data, field)
            if value is not None:
                selected[field] = value
    return await manager.generate_recap("current", settings=selected)


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
    if manager.status not in ("idle", "complete"):
        frames = [
            frame
            for frame in frames
            if float(frame.get("minute", 0.0)) * 60.0 <= manager.display_clock
        ]
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
    await queue.put(json.dumps(manager.public_snapshot(), ensure_ascii=False))

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
