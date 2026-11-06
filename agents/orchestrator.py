"""Coordinates deterministic analysis and Microsoft Agent Framework agents."""

from __future__ import annotations

import asyncio
import copy
import hashlib
import json
import logging
import threading
import time
from collections.abc import AsyncIterator, Callable
from datetime import datetime, timedelta, timezone
from typing import Any
from uuid import uuid4

from agents.analysis_tools import (
    ImpactAnalysisTool,
    MomentDetectionTool,
    PatternAnalysisTool,
    ShapeAnalysisTool,
)
from agents.framework_agents import AgentSuite, build_agent_suite
from agents.narrator import Narrator
from engine.state import MatchState
from feed.persistence import NarrationCache
from llm.foundry import ask
from telemetry import traced_agent_call, traced_tool_call


logger = logging.getLogger(__name__)


def _fact_id(section: str, data: Any) -> str:
    serialized = json.dumps(
        data,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        default=str,
    )
    return f"{section}:{hashlib.sha256(serialized.encode('utf-8')).hexdigest()}"


class Orchestrator:
    def __init__(
        self,
        narrator: Narrator | None = None,
        *,
        narration_cache: NarrationCache | None = None,
        framework_agents_enabled: bool = False,
    ) -> None:
        self.shape_tool = ShapeAnalysisTool()
        self.pattern_tool = PatternAnalysisTool()
        self.impact_tool = ImpactAnalysisTool()
        self.moment_tool = MomentDetectionTool()
        self.narrator = narrator or Narrator(cache=narration_cache)
        self.framework_agents_enabled = framework_agents_enabled
        self._section_lock = threading.Lock()

    @staticmethod
    def agents_for_mode(mode: str) -> tuple[str, ...]:
        """Return the analyst roles relevant to each viewer mode."""
        routes = {
            "Broadcast": ("ShapeAnalyst", "PatternSpotter", "MomentDetector"),
            "Analyst": (
                "ShapeAnalyst",
                "PatternSpotter",
                "ImpactAnalyst",
                "MomentDetector",
            ),
            "Fan": ("ImpactAnalyst", "MomentDetector"),
        }
        try:
            return routes[mode]
        except KeyError as error:
            raise ValueError(f"Unsupported match mode: {mode}") from error

    def _agents_for_state(self, state: MatchState) -> AgentSuite:
        return build_agent_suite(state=state)

    async def run_routed_agents(
        self,
        state: MatchState,
        *,
        mode: str = "Broadcast",
        on_agent_complete: Callable[[str, str, float], None] | None = None,
    ) -> dict[str, str]:
        """Call only the mode-selected analyst agents, returning grounded replies."""
        selected = self.agents_for_mode(mode)
        if not self.framework_agents_enabled:
            return {}

        try:
            snapshot = state.analysis_snapshot()
            suite = self._agents_for_state(snapshot)
        except Exception as error:
            logger.warning(
                "Agent Framework analysts unavailable (%s)",
                error.__class__.__name__,
            )
            return {}

        prompts = {
            "ShapeAnalyst": "What block is United playing? Use team United.",
            "PatternSpotter": "Summarize significant patterns for both teams.",
            "ImpactAnalyst": "Summarize measured player impact and recent momentum.",
            "MomentDetector": "List the detected moments from the current match.",
        }
        agents = {
            "ShapeAnalyst": suite.shape_analyst,
            "PatternSpotter": suite.pattern_spotter,
            "ImpactAnalyst": suite.impact_analyst,
            "MomentDetector": suite.moment_detector,
        }
        answers: dict[str, str] = {}
        fact_ids = [
            _fact_id("input", record)
            for record in snapshot.events[-50:] + snapshot.frames[-50:]
        ]

        async def run_one(agent_name: str) -> tuple[str, str]:
            started = time.perf_counter()
            answer = await ask(
                agents[agent_name],
                prompts[agent_name],
                fallback="",
                agent_name=agent_name,
                fact_ids=fact_ids,
            )
            if on_agent_complete is not None:
                on_agent_complete(
                    agent_name,
                    answer,
                    (time.perf_counter() - started) * 1000,
                )
            return agent_name, answer

        results = await asyncio.gather(
            *(run_one(agent_name) for agent_name in selected)
        )
        answers = {
            agent_name: answer
            for agent_name, answer in results
            if answer
        }
        return answers

    def analyze_facts(self, state: MatchState) -> dict[str, Any]:
        """Run the deterministic tools without waiting for any model call."""
        input_ids = [
            _fact_id("input", record)
            for record in state.events[-50:] + state.frames[-50:]
        ]
        results: dict[str, Any] = {}
        analyses = (
            ("ShapeAnalyst", self.shape_tool.analyze, "shape"),
            ("PatternSpotter", self.pattern_tool.analyze, "patterns"),
            ("ImpactAnalyst", self.impact_tool.analyze, "impact"),
            ("MomentDetector", self.moment_tool.analyze, "moments"),
        )
        for agent_name, analyze, result_name in analyses:
            with traced_agent_call(agent_name, state, fact_ids=input_ids) as span:
                with traced_tool_call(
                    result_name,
                    fact_ids=input_ids,
                ) as tool_span:
                    result = analyze(state)
                    output_id = _fact_id(result_name, result)
                    tool_span.set_attribute("overseer.fact.output_ids", [output_id])
                    span.set_attribute("overseer.fact.output_ids", [output_id])
                    results[result_name] = result
        return {
            "minute": round(state.minute, 2),
            **results,
            "momentum_series": state.momentum_series,
        }

    async def iter_analysis_tasks(
        self,
        state: MatchState,
        *,
        flagged_moments: list[dict[str, Any]] | None = None,
        deadline_seconds: float = 8.0,
        on_task_created: Callable[[dict[str, Any]], None] | None = None,
    ) -> AsyncIterator[dict[str, Any]]:
        """Run independent analysts against copies and yield results as they finish."""
        if deadline_seconds <= 0:
            raise ValueError("deadline_seconds must be greater than zero")
        base = state.analysis_snapshot().snapshot()
        now = datetime.now(timezone.utc)
        task_deadline = (now + timedelta(seconds=deadline_seconds)).isoformat()
        created_at = now.isoformat()
        window_start = max(0.0, float(base["minute"]) - 10.0)

        flagged = copy.deepcopy(flagged_moments or [])
        task_specs = (
            ("ShapeAnalyst", "shape"),
            ("PatternSpotter", "patterns"),
            ("ImpactAnalyst", "impact"),
            ("MomentDetector", "moments"),
        )

        async def run_one(agent_name: str, section: str) -> dict[str, Any]:
            task = {
                "task_id": str(uuid4()),
                "agent": agent_name,
                "window": {
                    "start_minute": window_start,
                    "end_minute": float(base["minute"]),
                },
                "deadline": task_deadline,
                "created_at": created_at,
            }
            if on_task_created is not None:
                on_task_created(task)
            snapshot = copy.deepcopy(base)
            started = datetime.now(timezone.utc)
            result, facts = await asyncio.to_thread(
                self._analyze_task_section,
                agent_name,
                section,
                snapshot,
                flagged,
            )
            with self._section_lock:
                if section == "shape":
                    state.shape = result
                elif section == "patterns":
                    state.patterns = result
                elif section == "impact":
                    state.impact = result["impact"]
                    state.momentum_series = result["momentum_series"]
                else:
                    known = {moment.get("id") for moment in state.moments}
                    state.moments.extend(
                        copy.deepcopy(moment)
                        for moment in facts
                        if moment.get("id") not in known
                    )
            output_id = _fact_id(section, facts)
            return {
                "task": task,
                "facts": facts,
                "output_id": output_id,
                "latency_ms": max(
                    0.0,
                    (datetime.now(timezone.utc) - started).total_seconds() * 1000,
                ),
                "state": state,
            }

        tasks = [
            asyncio.create_task(run_one(agent_name, section))
            for agent_name, section in task_specs
        ]
        try:
            for completed in asyncio.as_completed(tasks):
                yield await completed
        finally:
            for task in tasks:
                if not task.done():
                    task.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)

    def _analyze_task_section(
        self,
        agent_name: str,
        section: str,
        snapshot: dict[str, Any],
        flagged_moments: list[dict[str, Any]],
    ) -> tuple[Any, Any]:
        state = MatchState(**snapshot)
        if agent_name == "ShapeAnalyst":
            tool = ShapeAnalysisTool()
            tool._previous = {
                team: {
                    "formation": details.get("out_of_possession", {}).get("formation", ""),
                    "block": details.get("block", {}).get("block", ""),
                }
                for team, details in state.shape.get("teams", {}).items()
            }
            result = tool.analyze(state)
            return result, result
        if agent_name == "PatternSpotter":
            result = PatternAnalysisTool().analyze(state)
            return result, result
        if agent_name == "ImpactAnalyst":
            result = ImpactAnalysisTool().analyze(state)
            return result, result

        shape_tool = ShapeAnalysisTool()
        shape_tool._previous = {
            team: {
                "formation": details.get("out_of_possession", {}).get("formation", ""),
                "block": details.get("block", {}).get("block", ""),
            }
            for team, details in state.shape.get("teams", {}).items()
        }
        state.shape = shape_tool.analyze(state)
        impact = ImpactAnalysisTool().analyze(state)
        state.impact = impact["impact"]
        state.momentum_series = impact["momentum_series"]
        additions = MomentDetectionTool().analyze(state)
        by_id = {moment.get("id"): moment for moment in additions}
        by_id.update({moment.get("id"): moment for moment in flagged_moments})
        facts = list(by_id.values())
        state.moments = facts
        return facts, facts

    async def iter_tick_events(
        self,
        state: MatchState,
        *,
        mode: str = "Broadcast",
        language: str = "English",
        favourite_team: str | None = None,
        favourite_player: str | None = None,
    ) -> AsyncIterator[dict[str, Any]]:
        """Yield deterministic facts before awaiting optional model narration."""
        facts = self.analyze_facts(state)
        yield {"type": "fact", "facts": facts}

        if not facts.get("moments"):
            return
        agent_answers = await self.run_routed_agents(state, mode=mode)
        narration = await self.narrator.narrate(
            state,
            mode=mode,
            language=language,
            favourite_team=favourite_team,
            favourite_player=favourite_player,
        )
        yield {
            "type": "narration",
            "narration": narration,
            "agent_answers": agent_answers,
        }

    async def tick_async(
        self,
        state: MatchState,
        *,
        mode: str = "Broadcast",
        language: str = "English",
        favourite_team: str | None = None,
        favourite_player: str | None = None,
    ) -> dict[str, Any]:
        facts = self.analyze_facts(state)
        agent_answers: dict[str, str] = {}
        narration = None
        if facts.get("moments"):
            agent_answers = await self.run_routed_agents(state, mode=mode)
            narration = await self.narrator.narrate(
                state,
                mode=mode,
                language=language,
                favourite_team=favourite_team,
                favourite_player=favourite_player,
            )
        return {
            **facts,
            "impact": state.impact,
            "momentum_series": state.momentum_series,
            "moments": state.moments,
            "narration": narration,
            "agent_answers": agent_answers,
        }

    def tick(
        self,
        state: MatchState,
        *,
        mode: str = "Broadcast",
        language: str = "English",
        favourite_team: str | None = None,
        favourite_player: str | None = None,
    ) -> dict[str, Any]:
        """Synchronous adapter used by the existing replay runner."""
        try:
            asyncio.get_running_loop()
        except RuntimeError:
            return asyncio.run(
                self.tick_async(
                    state,
                    mode=mode,
                    language=language,
                    favourite_team=favourite_team,
                    favourite_player=favourite_player,
                )
            )
        raise RuntimeError("Orchestrator.tick cannot run inside an event loop; await tick_async")
