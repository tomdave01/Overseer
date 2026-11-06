from __future__ import annotations

import asyncio

from fastapi.testclient import TestClient

from agents.narrator import Narrator
from api.main import app, manager
from engine.state import MatchState
from engine.stats import StatsAggregator


def test_recap_generate_passes_complete_facts_and_selected_settings(monkeypatch):
    original_state = manager.state
    original_stats = manager.stats
    original_results = manager.recap_results
    event = {
        "minute": 24.0,
        "type": "shot",
        "team": "Rovers",
        "player": "Rico Vale",
        "outcome": "goal",
        "x": 80.0,
        "y": 45.0,
    }
    manager.state = MatchState(
        minute=50.0,
        score={"Rovers": 1, "United": 0},
        events=[event],
        moments=[{
            "id": "goal-rovers-24",
            "type": "goal",
            "minute": 24.0,
            "team": "Rovers",
            "player": "Rico Vale",
            "why": "A clean finish put Rovers ahead.",
        }, {
            "id": "momentum-swing-30",
            "type": "momentum_swing",
            "minute": 30.0,
            "team": "Rovers",
            "why": "Momentum shifted toward Rovers.",
        }, {
            "id": "block-change-united-40",
            "type": "block_change",
            "minute": 40.0,
            "team": "United",
            "from": "mid",
            "to": "low",
            "why": "United dropped into a low block.",
        }],
        patterns={"findings": {"rovers_reliance": {"top_player": "Rico Vale"}}},
        shape={"changes": [{
            "id": "block_change",
            "team": "United",
            "from": "mid",
            "to": "low",
            "minute": 40.0,
        }]},
    )
    manager.stats = StatsAggregator()
    manager.stats.add_event(event)

    calls: list[tuple[dict, dict]] = []

    async def fake_narrate_recap(facts, **settings):
        calls.append((facts, settings))
        return {
            "recap_type": facts["recap_type"],
            "text": "Test recap",
            "facts": facts,
            "fallback": True,
            **settings,
        }

    monkeypatch.setattr(
        manager.orchestrator.narrator,
        "narrate_recap",
        fake_narrate_recap,
    )
    try:
        with TestClient(app) as client:
            response = client.post(
                "/recap/generate",
                json={
                    "mode": "Analyst",
                    "language": "French",
                    "favourite_team": "United",
                },
            )
        assert response.status_code == 200
        result = response.json()
        facts, settings = calls[0]
        assert settings == {
            "recap_type": "current",
            "mode": "Analyst",
            "language": "French",
            "favourite_team": "United",
        }
        assert facts["current_score"] == {"Rovers": 1, "United": 0}
        assert facts["final_score"] is None
        assert facts["team_stats"]["Rovers"]["goals"] == 1
        assert any(
            moment["type"] == "goal"
            and moment["why"] == "A clean finish put Rovers ahead."
            for moment in facts["key_moments"]
        )
        assert facts["top_influencers"][0]["player"] == "Rico Vale"
        assert facts["notable_patterns"]["rovers_reliance"]["top_player"] == "Rico Vale"
        assert len(facts["block_changes"]) == 1
        assert facts["block_changes"][0]["to"] == "low"
        assert facts["momentum_swings"][0]["team"] == "Rovers"
        assert result["text"] == "Test recap"
    finally:
        manager.state = original_state
        manager.stats = original_stats
        manager.recap_results = original_results


def test_recap_fallback_is_mode_language_and_favourite_aware(monkeypatch):
    async def use_fallback(_agent, _prompt, *, fallback, **_kwargs):
        return fallback

    monkeypatch.setattr("agents.narrator.ask", use_fallback)
    facts = {
        "minute": 45.0,
        "score": {"Rovers": 1, "United": 0},
        "top_influencers": [{"player": "Rico Vale", "score": 67.5}],
        "key_moments": [],
    }
    narrator = Narrator(agent=object())

    analyst = asyncio.run(narrator.narrate_recap(
        facts,
        recap_type="halftime",
        mode="Analyst",
        language="English",
        favourite_team="Neutral",
    ))
    fan = asyncio.run(narrator.narrate_recap(
        facts,
        recap_type="halftime",
        mode="Fan",
        language="Spanish",
        favourite_team="Rovers",
    ))

    assert analyst["fallback"] is True
    assert "Rico Vale" in analyst["text"] and "67.5" in analyst["text"]
    assert fan["fallback"] is True
    assert fan["text"].startswith("Resumen del descanso:")
    assert "Rovers" in fan["text"]
    assert "perspectiva de Rovers" in fan["text"]
