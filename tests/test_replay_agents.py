from __future__ import annotations

import asyncio
import json
import threading
import time
from types import SimpleNamespace

from agents import Orchestrator
from agents.framework_agents import build_agent_suite, build_analysis_tools
from agents.narrator import Narrator
from engine.replay import ReplayDataset, ReplayEngine
from engine.state import MatchState, ingest
from feed.persistence import InMemoryNarrationCache
from llm.foundry import ask
import pytest


def _event(
    team: str,
    player: str,
    outcome: str,
    *,
    minute: float = 0.0,
) -> dict:
    return {
        "match_id": "test",
        "source": "synthetic",
        "minute": minute,
        "t": None,
        "type": "shot",
        "team": team,
        "player": player,
        "outcome": outcome,
        "x": 80.0,
        "y": 75.0,
        "end_x": None,
        "end_y": None,
    }


class _FakeAgent:
    def __init__(self, text: str, *, delay: float = 0) -> None:
        self.text = text
        self.delay = delay
        self.calls = 0

    async def run(self, _prompt: str) -> SimpleNamespace:
        self.calls += 1
        assert _prompt
        if self.delay:
            await asyncio.sleep(self.delay)
        return SimpleNamespace(text=self.text)


def test_ingest_tracks_goals_minute_and_source():
    state = MatchState()
    ingest(state, event=_event("Rovers", "Rico Vale", "goal", minute=2))
    assert state.score == {"Rovers": 1, "United": 0}
    assert state.minute == 2
    assert state.source == "synthetic"


def test_analysis_snapshot_is_detached_and_windowed():
    state = MatchState(
        minute=20.0,
        events=[
            {"minute": 1.0, "player": "Old"},
            {"minute": 19.0, "player": "Recent"},
        ],
        frames=[
            {"minute": 2.0, "ball": {"x": 1}},
            {"minute": 18.0, "ball": {"x": 2}},
        ],
    )

    snapshot = state.analysis_snapshot(window_minutes=5.0)
    snapshot.events[0]["player"] = "Changed"
    snapshot.frames[0]["ball"]["x"] = 99

    assert [event["player"] for event in state.events] == ["Old", "Recent"]
    assert [event["player"] for event in snapshot.events] == ["Changed"]
    assert state.frames[1]["ball"]["x"] == 2
    assert len(snapshot.frames) == 1


def test_replay_processes_only_synthetic_records(tmp_path):
    events_path = tmp_path / "events.json"
    frames_path = tmp_path / "frames.json"
    events_path.write_text(
        json.dumps([{**_event("Rovers", "Rico Vale", "goal"), "clip_id": None}]),
        encoding="utf-8",
    )
    frames_path.write_text(
        json.dumps([{
            "t": 0.0,
            "minute": 0.0,
            "poss": "Rovers",
            "ball": {"x": 50, "y": 50},
            "players": [{
                "id": "R-MF-01",
                "team": "Rovers",
                "role": "MID",
                "x": 50,
                "y": 50,
            }],
        }]),
        encoding="utf-8",
    )
    dataset = ReplayDataset.from_files(
        events_path,
        frames_path,
        name="generated",
    )
    state = MatchState()
    engine = ReplayEngine(state, speed=1_000_000, tick_interval=2.5)
    engine.run([dataset])
    assert [event["source"] for event in state.events] == ["synthetic"]
    assert "clip_id" not in state.events[0]
    assert state.score == {"Rovers": 1, "United": 0}
    assert len(state.frames) == 1
    assert state.frames[0]["players"][0]["shirt"] == 6
    assert state.frames[0]["players"][0]["name"] == "Rico Vale"


def test_narrator_uses_templated_fallback_after_timeout():
    state = MatchState()
    agent = _FakeAgent("too late", delay=0.05)

    result = asyncio.run(
        Narrator(agent=agent, timeout_seconds=0.001).narrate(
            state,
            mode="Broadcast",
            language="English",
        )
    )

    assert agent.calls == 1
    assert result["fallback"] is True
    assert result["fact_id"]
    assert result["text"]


def test_ask_returns_fallback_on_timeout_and_agent_exception():
    class BrokenAgent:
        async def run(self, _prompt: str) -> str:
            raise RuntimeError("request failed")

    timeout_result = asyncio.run(
        ask(
            _FakeAgent("too late", delay=0.05),
            "prompt",
            timeout=0.001,
            fallback="timeout fallback",
        )
    )
    error_result = asyncio.run(
        ask(BrokenAgent(), "prompt", fallback="error fallback")
    )

    assert timeout_result == "timeout fallback"
    assert error_result == "error fallback"


def test_ask_respects_configured_concurrency_limit(monkeypatch):
    monkeypatch.setenv("LLM_MAX_CONCURRENCY", "2")

    class TrackedAgent:
        active = 0
        maximum = 0
        lock = threading.Lock()

        async def run(self, _prompt: str) -> str:
            with self.lock:
                self.active += 1
                self.maximum = max(self.maximum, self.active)
            await asyncio.sleep(0.03)
            with self.lock:
                self.active -= 1
            return "ok"

    async def run() -> list[str]:
        agent = TrackedAgent()
        results = await asyncio.gather(
            *(ask(agent, f"prompt-{index}") for index in range(6))
        )
        assert agent.maximum == 2
        return results

    assert asyncio.run(run()) == ["ok"] * 6


def test_ask_retries_http_429_once(monkeypatch):
    monkeypatch.setenv("LLM_MAX_CONCURRENCY", "2")

    class RateLimitError(Exception):
        status_code = 429

    class RateLimitedAgent:
        def __init__(self) -> None:
            self.calls = 0

        async def run(self, _prompt: str) -> str:
            self.calls += 1
            if self.calls == 1:
                raise RateLimitError("rate limited")
            return "recovered"

    agent = RateLimitedAgent()
    assert asyncio.run(ask(agent, "prompt")) == "recovered"
    assert agent.calls == 2


def test_analyst_tasks_run_concurrently_on_independent_snapshots(monkeypatch):
    orchestrator = Orchestrator()
    lock = threading.Lock()
    active = 0
    maximum = 0
    snapshot_ids: set[int] = set()

    def analyze(agent: str, section: str, snapshot: dict, flagged: list):
        nonlocal active, maximum
        with lock:
            active += 1
            maximum = max(maximum, active)
            snapshot_ids.add(id(snapshot))
        time.sleep(0.04)
        with lock:
            active -= 1
        if section == "impact":
            result = {"impact": [], "momentum_series": []}
        elif section == "moments":
            result = list(flagged)
        else:
            result = {"agent": agent}
        return result, result

    monkeypatch.setattr(orchestrator, "_analyze_task_section", analyze)
    state = MatchState(minute=3.0)
    flagged = [{"id": "goal-1", "type": "goal"}]

    async def run() -> list[dict]:
        return [
            result
            async for result in orchestrator.iter_analysis_tasks(
                state,
                flagged_moments=flagged,
            )
        ]

    results = asyncio.run(run())
    assert len(results) == 4
    assert maximum > 1
    assert len(snapshot_ids) == 4
    assert all(
        {"task_id", "agent", "window", "deadline", "created_at"}
        <= result["task"].keys()
        for result in results
    )
    assert state.shape == {"agent": "ShapeAnalyst"}
    assert state.patterns == {"agent": "PatternSpotter"}
    assert state.impact == []
    assert state.moments == flagged


def test_traced_agent_span_records_name_facts_and_latency(monkeypatch):
    import telemetry

    class FakeSpan:
        def __init__(self) -> None:
            self.attributes: dict[str, object] = {}

        def __enter__(self) -> "FakeSpan":
            return self

        def __exit__(self, *_args: object) -> None:
            return None

        def set_attribute(self, name: str, value: object) -> None:
            self.attributes[name] = value

        def record_exception(self, _error: Exception) -> None:
            pass

        def set_status(self, _status: object) -> None:
            pass

    class FakeTracer:
        def __init__(self, span: FakeSpan) -> None:
            self.span = span
            self.name = ""

        def start_as_current_span(self, name: str) -> FakeSpan:
            self.name = name
            return self.span

    span = FakeSpan()
    tracer = FakeTracer(span)
    monkeypatch.setattr(telemetry.trace, "get_tracer", lambda _name: tracer)

    result = asyncio.run(
        telemetry.traced(
            "ShapeAnalyst",
            ("fact-1",),
            asyncio.sleep(0, result="grounded"),
        )
    )

    assert result == "grounded"
    assert tracer.name == "agent.ShapeAnalyst"
    assert span.attributes["agent.name"] == "ShapeAnalyst"
    assert span.attributes["fact.ids"] == ["fact-1"]
    assert span.attributes["agent.latency_ms"] >= 0


def test_narrator_content_safety_replaces_rejected_output(monkeypatch):
    monkeypatch.setenv("AZURE_CONTENT_SAFETY_ENABLED", "1")
    monkeypatch.setattr("agents.narrator.narrator_output_is_safe", lambda text: not text)

    result = asyncio.run(
        Narrator(agent=_FakeAgent("Rejected generated narration.")).narrate(
            MatchState(),
            language="English",
        )
    )

    assert result["fallback"] is True
    assert result["reason"] == "ContentSafetyRejected"
    assert "Rejected generated narration" not in result["text"]


def test_optional_translator_only_translates_template_fallback(monkeypatch):
    monkeypatch.setenv("AZURE_TRANSLATOR_ENABLED", "1")
    translated_input: list[str] = []

    def fake_translate(text: str, language: str, names: list[str]) -> str:
        translated_input.append(text)
        assert language == "Spanish"
        assert "Rico Vale" in names
        assert "Rovers" in names and "United" in names
        return f"{language}:{text}"

    monkeypatch.setattr("agents.narrator.translate_narration", fake_translate)
    state = MatchState(events=[_event("Rovers", "Rico Vale", "goal")])
    narrator = Narrator(
        agent=_FakeAgent("late response", delay=0.2),
        timeout_seconds=0.05,
    )

    result = asyncio.run(narrator.narrate(state, language="Spanish"))

    assert result["fallback"] is True
    assert result["text"].startswith("Spanish:")
    assert "Rovers" in translated_input[0]


def test_narrator_cache_separates_mode_language_and_favourite():
    state = MatchState()
    agent = _FakeAgent("Grounded generated narration.")
    cache = InMemoryNarrationCache()
    narrator = Narrator(agent=agent, cache=cache)

    async def run() -> list[dict]:
        first = await narrator.narrate(state, favourite_team="Rovers")
        repeated = await narrator.narrate(state, favourite_team="Rovers")
        analyst = await narrator.narrate(
            state,
            mode="Analyst",
            favourite_team="Rovers",
        )
        spanish = await narrator.narrate(
            state,
            language="Spanish",
            favourite_team="Rovers",
        )
        rival = await narrator.narrate(state, favourite_team="United")
        neutral = await narrator.narrate(state)
        neutral_again = await narrator.narrate(state, favourite_team="Neutral")
        return [first, repeated, analyst, spanish, rival, neutral, neutral_again]

    results = asyncio.run(run())
    assert agent.calls == 5
    assert results[0]["text"] == results[1]["text"]
    assert results[0]["fact_id"] == results[1]["fact_id"]
    assert len({result["mode"] for result in (results[0], results[2])}) == 2
    assert results[3]["language"] == "Spanish"
    assert results[4]["favourite_team"] == "United"
    assert results[5]["favourite_team"] == "Neutral"
    assert results[5]["text"] == results[6]["text"]
    facts = Narrator._facts(state, "Neutral", None)
    assert facts["team_focus"] == "Neutral"
    neutral_template = Narrator._template(facts, "Fan", "English")
    assert "come on" not in neutral_template.casefold()


def test_orchestrator_keeps_deterministic_analysis_and_template_fallback(monkeypatch):
    async def use_fallback(_agent, _prompt, *, fallback, **_kwargs):
        return fallback

    monkeypatch.setattr("agents.narrator.ask", use_fallback)
    state = MatchState()
    state.events = [_event("Rovers", "Rico Vale", "goal", minute=1)]
    state.frames = [
        {
            "t": 60.0,
            "minute": 1.0,
            "poss": "Rovers",
            "ball": {"x": 70.0, "y": 50.0},
            "players": [],
        }
    ]

    output = Orchestrator().tick(state, mode="Broadcast", language="English")

    assert output["narration"]["fallback"] is True
    assert "Rovers" in output["narration"]["text"]
    assert state.moments[0]["type"] == "goal"


def test_orchestrator_skips_narration_for_unflagged_state():
    agent = _FakeAgent("must not be called")
    orchestrator = Orchestrator(narrator=Narrator(agent=agent))

    result = asyncio.run(orchestrator.tick_async(MatchState()))

    assert result["narration"] is None
    assert agent.calls == 0


def test_orchestrator_yields_facts_before_starting_narrator():
    state = MatchState(events=[_event("Rovers", "Rico Vale", "goal", minute=1)])
    agent = _FakeAgent("Grounded generated narration.")
    orchestrator = Orchestrator(narrator=Narrator(agent=agent))

    async def collect_events() -> list[dict]:
        events = orchestrator.iter_tick_events(state)
        first = await anext(events)
        assert first["type"] == "fact"
        assert agent.calls == 0
        second = await anext(events)
        return [first, second]

    events = asyncio.run(collect_events())

    assert events[1]["type"] == "narration"
    assert agent.calls == 1


def test_replay_engine_emits_facts_before_narration():
    agent = _FakeAgent("Grounded generated narration.")
    orchestrator = Orchestrator(narrator=Narrator(agent=agent))
    callbacks: list[str] = []

    def on_fact(_facts: dict) -> None:
        assert isinstance(_facts, dict)
        assert agent.calls == 0
        callbacks.append("fact")

    def on_tick(_tick: dict) -> None:
        assert isinstance(_tick, dict)
        assert agent.calls == 1
        callbacks.append("narration")

    replay = ReplayEngine(
        orchestrator=orchestrator,
        speed=1_000_000,
        on_fact=on_fact,
        on_tick=on_tick,
    )
    replay.run([
        ReplayDataset(
            name="single",
            events=[_event("Rovers", "Rico Vale", "goal")],
            frames=[{
                "t": 0.0,
                "minute": 0.0,
                "poss": "Rovers",
                "ball": {"x": 50.0, "y": 50.0},
                "players": [],
            }],
        )
    ])

    assert callbacks == ["fact", "narration"]


def test_agent_framework_suite_uses_tool_backed_agents(monkeypatch):
    import agents.framework_agents as framework_agents

    made_agents: dict[str, object] = {}

    def fake_make_agent(name: str, instructions: str, tools: list) -> object:
        agent = SimpleNamespace(name=name, instructions=instructions, tools=tools)
        made_agents[name] = agent
        return agent

    monkeypatch.setattr(framework_agents, "make_agent", fake_make_agent)
    suite = build_agent_suite(state=MatchState())

    assert set(made_agents) == {
        "ShapeAnalyst",
        "PatternSpotter",
        "ImpactAnalyst",
        "MomentDetector",
    }
    assert suite.shape_analyst.tools[0].name == "get_team_shape"
    assert suite.pattern_spotter.tools[0].name == "get_team_patterns"
    assert suite.impact_analyst.tools[0].name == "get_player_impact"
    assert suite.moment_detector.tools[0].name == "detect_recent_moments"
    for agent in made_agents.values():
        assert "Always call your available tool" in agent.instructions
        assert "Never invent numbers" in agent.instructions


def test_analysis_tools_close_over_state_and_write_owned_sections():
    state = MatchState(
        minute=1.0,
        frames=[
            {
                "minute": 1.0,
                "poss": "United",
                "ball": {"x": 50.0, "y": 50.0},
                "players": [],
            }
        ],
    )
    tools = build_analysis_tools(state=state)

    shape_result = tools.shape.func(team="United")
    pattern_result = tools.patterns.func(team="United")
    impact_result = tools.impact.func()
    moment_result = tools.moments.func(since_minute=0.0)

    assert '"team": "United"' in shape_result
    assert state.shape["teams"]["United"]
    assert '"team": "United"' in pattern_result
    assert state.patterns["findings"] == {}
    assert '"impact":' in impact_result
    assert state.impact is not None
    assert '"moments":' in moment_result


def test_orchestrator_routes_agents_by_mode():
    assert Orchestrator.agents_for_mode("Broadcast") == (
        "ShapeAnalyst",
        "PatternSpotter",
        "MomentDetector",
    )
    assert Orchestrator.agents_for_mode("Analyst") == (
        "ShapeAnalyst",
        "PatternSpotter",
        "ImpactAnalyst",
        "MomentDetector",
    )
    assert Orchestrator.agents_for_mode("Fan") == (
        "ImpactAnalyst",
        "MomentDetector",
    )
    with pytest.raises(ValueError, match="Unsupported match mode"):
        Orchestrator.agents_for_mode("Unknown")


def test_orchestrator_calls_only_the_selected_agents(monkeypatch):
    class RoutedAgent:
        def __init__(self, name: str) -> None:
            self.name = name
            self.prompts: list[str] = []

        async def run(self, prompt: str) -> str:
            self.prompts.append(prompt)
            return f"{self.name} grounded answer"

    suite = SimpleNamespace(
        shape_analyst=RoutedAgent("ShapeAnalyst"),
        pattern_spotter=RoutedAgent("PatternSpotter"),
        impact_analyst=RoutedAgent("ImpactAnalyst"),
        moment_detector=RoutedAgent("MomentDetector"),
    )
    orchestrator = Orchestrator(framework_agents_enabled=True)
    monkeypatch.setattr(orchestrator, "_agents_for_state", lambda _state: suite)

    answers = asyncio.run(
        orchestrator.run_routed_agents(MatchState(), mode="Broadcast")
    )

    assert set(answers) == {"ShapeAnalyst", "PatternSpotter", "MomentDetector"}
    assert suite.shape_analyst.prompts
    assert suite.pattern_spotter.prompts
    assert suite.moment_detector.prompts
    assert suite.impact_analyst.prompts == []


def test_translator_uses_sdk_text_list_and_restores_names(monkeypatch):
    from types import SimpleNamespace

    import azure.ai.translation.text as translation_sdk
    import llm.translator as translator_module
    from llm.translator import translate_narration

    monkeypatch.setenv("AZURE_TEXT_TRANSLATION_ENDPOINT", "https://translator.test")
    monkeypatch.setenv("AZURE_TEXT_TRANSLATION_REGION", "eastus")
    captured: tuple[list[str], list[str], str] | None = None

    class FakeCredential:
        def close(self) -> None:
            pass

    class FakeTranslator:
        def __init__(self, **_: object) -> None:
            pass

        def translate(
            self,
            *,
            body: list[str],
            to_language: list[str],
            from_language: str,
        ) -> list[object]:
            nonlocal captured
            captured = (body, to_language, from_language)
            text = body[0]
            translated = text.replace("scores for", "anota para")
            return [
                SimpleNamespace(
                    translations=[SimpleNamespace(text=translated)]
                )
            ]

        def close(self) -> None:
            pass

    monkeypatch.setattr(translator_module, "DefaultAzureCredential", FakeCredential)
    monkeypatch.setattr(translation_sdk, "TextTranslationClient", FakeTranslator)
    result = translate_narration(
        "Rico Vale scores for Rovers", "Spanish", ["Rico Vale", "Rovers"]
    )
    assert captured == (
        ["OVERSEERNAME0TOKEN scores for OVERSEERNAME1TOKEN"],
        ["es"],
        "en",
    )
    assert result == "Rico Vale anota para Rovers"


def test_foundry_client_is_lazy_singleton_with_default_credential(monkeypatch):
    import llm.foundry as foundry_module

    captured: dict[str, object] = {}
    fake_credential = object()

    def fake_foundry_client(**kwargs: object) -> object:
        captured.update(kwargs)
        return object()

    monkeypatch.setenv(
        "FOUNDRY_PROJECT_ENDPOINT",
        "https://example.services.ai.azure.com/api/projects/test",
    )
    monkeypatch.setenv("FOUNDRY_MODEL", "test-deployment")
    monkeypatch.setattr(foundry_module, "_client", None)
    monkeypatch.setattr(
        foundry_module,
        "DefaultAzureCredential",
        lambda: fake_credential,
    )
    monkeypatch.setattr(foundry_module, "FoundryChatClient", fake_foundry_client)

    first_client = foundry_module.get_client()
    second_client = foundry_module.get_client()

    assert first_client is second_client
    assert captured["project_endpoint"] == (
        "https://example.services.ai.azure.com/api/projects/test"
    )
    assert captured["model"] == "test-deployment"
    assert captured["credential"] is fake_credential
    assert "default_headers" not in captured


def test_foundry_rejects_non_https_endpoint(monkeypatch):
    import llm.foundry as foundry_module

    monkeypatch.setattr(foundry_module, "_client", None)
    monkeypatch.setenv("FOUNDRY_PROJECT_ENDPOINT", "http://foundry.example")
    monkeypatch.setenv("FOUNDRY_MODEL", "test-deployment")

    with pytest.raises(ValueError, match="must start with 'https://'"):
        foundry_module.get_client()
