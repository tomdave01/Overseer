"""Coordinates deterministic analysis and Microsoft Agent Framework agents."""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
from collections.abc import AsyncIterator
from typing import Any

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
        self._agent_state: MatchState | None = None
        self._agent_suite: AgentSuite | None = None

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
        if self._agent_suite is None or self._agent_state is not state:
            self._agent_suite = build_agent_suite(
                state=state,
                shape_analyst=self.shape_tool,
                pattern_spotter=self.pattern_tool,
                impact_analyst=self.impact_tool,
                moment_detector=self.moment_tool,
            )
            self._agent_state = state
        return self._agent_suite

    async def run_routed_agents(
        self,
        state: MatchState,
        *,
        mode: str = "Broadcast",
    ) -> dict[str, str]:
        """Call only the mode-selected analyst agents, returning grounded replies."""
        selected = self.agents_for_mode(mode)
        if not self.framework_agents_enabled:
            return {}

        try:
            suite = self._agents_for_state(state)
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
            for record in state.events[-50:] + state.frames[-50:]
        ]
        for agent_name in selected:
            agent = agents[agent_name]
            answer = await ask(
                agent,
                prompts[agent_name],
                fallback="",
                agent_name=agent_name,
                fact_ids=fact_ids,
            )
            if answer:
                answers[agent_name] = answer
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
