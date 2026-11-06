"""Shared, in-memory state for match ingestion and analysis agents."""

from __future__ import annotations

import copy
from dataclasses import asdict, dataclass, field
from typing import Any


@dataclass
class MatchState:
    minute: float = 0.0
    score: dict[str, int] = field(
        default_factory=lambda: {"Rovers": 0, "United": 0}
    )
    source: str = "synthetic"
    events: list[dict[str, Any]] = field(default_factory=list)
    frames: list[dict[str, Any]] = field(default_factory=list)
    shape: dict[str, Any] = field(default_factory=dict)
    patterns: dict[str, Any] = field(default_factory=dict)
    momentum_series: list[dict[str, Any]] = field(default_factory=list)
    impact: list[dict[str, Any]] = field(default_factory=list)
    moments: list[dict[str, Any]] = field(default_factory=list)
    narration_log: list[dict[str, Any]] = field(default_factory=list)

    def snapshot(self) -> dict[str, Any]:
        """Return a JSON-serializable snapshot detached from live state."""
        return asdict(self)

    def analysis_snapshot(self, window_minutes: float = 10.0) -> MatchState:
        """Copy the current analysis window without copying the full replay history."""
        if window_minutes <= 0:
            raise ValueError("window_minutes must be greater than zero")
        lower_bound = self.minute - window_minutes
        return MatchState(
            minute=self.minute,
            score=copy.deepcopy(self.score),
            source=self.source,
            events=copy.deepcopy([
                event
                for event in self.events
                if float(event.get("minute", 0.0)) >= lower_bound
            ]),
            frames=copy.deepcopy([
                frame
                for frame in self.frames
                if float(frame.get("minute", 0.0)) >= lower_bound
            ]),
            shape=copy.deepcopy(self.shape),
            patterns=copy.deepcopy(self.patterns),
            momentum_series=copy.deepcopy(self.momentum_series),
            impact=copy.deepcopy(self.impact),
            moments=copy.deepcopy(self.moments[-500:]),
            narration_log=copy.deepcopy(self.narration_log[-500:]),
        )


def ingest(
    state: MatchState,
    event: dict[str, Any] | None = None,
    frame: dict[str, Any] | None = None,
) -> MatchState:
    """Append one event and/or frame and update the live score and clock."""
    if event is None and frame is None:
        raise ValueError("ingest requires an event, a frame, or both")

    if event is not None:
        event_minute = float(event.get("minute", 0.0))
        state.minute = max(state.minute, event_minute)
        state.source = str(event.get("source", state.source))
        state.events.append(event)
        if event.get("type") == "shot" and event.get("outcome") == "goal":
            team = str(event.get("team", ""))
            if team not in state.score:
                state.score[team] = 0
            state.score[team] += 1

    if frame is not None:
        frame_minute = float(frame.get("minute", state.minute))
        state.minute = max(state.minute, frame_minute)
        state.frames.append(frame)
        state.source = str(frame.get("source", state.source))

    return state
