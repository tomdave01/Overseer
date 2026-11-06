"""Run a deterministic ten-minute replay using a test-only fake agent."""

from __future__ import annotations

import sys
from pathlib import Path
from types import SimpleNamespace

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from agents.narrator import Narrator
from agents.orchestrator import Orchestrator
from engine.replay import ReplayDataset, ReplayEngine
from engine.state import MatchState
from generator.match import generate_match


class _FakeAgent:
    async def run(self, _prompt: str) -> SimpleNamespace:
        assert _prompt
        return SimpleNamespace(text="Test narration grounded in supplied facts.")


def main() -> None:
    events, frames = generate_match(seed=42)
    first_ten_minutes = ReplayDataset(
        name="synthetic-first-ten-minutes",
        events=[event for event in events if event["minute"] <= 10],
        frames=[frame for frame in frames if frame["minute"] <= 10],
    )
    state = MatchState()
    orchestrator = Orchestrator(narrator=Narrator(agent=_FakeAgent()))
    ReplayEngine(state, orchestrator, speed=1000).run([first_ten_minutes])

    assert state.minute == 10.0, f"Expected ten simulated minutes, got {state.minute}"
    assert state.shape, "ShapeAnalyst did not update MatchState"
    assert state.patterns.get("findings"), "PatternSpotter found no flagged patterns"
    assert state.impact, "ImpactAnalyst produced no player rankings"
    assert state.moments, "MomentDetector produced no moments"
    assert state.narration_log, "Narrator wrote no narration"
    assert all(item["fallback"] is False for item in state.narration_log)
    print(
        "Test-agent smoke passed: 10.0 simulated minutes, "
        f"{len(state.events)} events, {len(state.frames)} frames, "
        f"{len(state.moments)} moments, {len(state.narration_log)} narrations."
    )


if __name__ == "__main__":
    main()
