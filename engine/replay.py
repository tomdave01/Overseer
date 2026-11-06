"""Time-ordered replay of synthetic match records."""

from __future__ import annotations

import asyncio
import json
import threading
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Iterable

from agents.orchestrator import Orchestrator
from engine.state import MatchState, ingest
from generator.match import player_metadata


_PLAYER_METADATA = player_metadata()


def _synthetic_event(event: dict[str, Any]) -> dict[str, Any]:
    normalized = {
        key: value for key, value in event.items()
        if key != "clip_id"
    }
    normalized["source"] = "synthetic"
    return normalized


def _synthetic_frame(frame: dict[str, Any]) -> dict[str, Any]:
    normalized = dict(frame)
    normalized["source"] = "synthetic"
    normalized["players"] = [
        {
            **player,
            **{
                key: value
                for key, value in _PLAYER_METADATA.get(
                    str(player.get("id")), {}
                ).items()
                if key != "team" or player.get("team") == value
            },
        }
        for player in frame.get("players", [])
    ]
    return normalized


@dataclass
class ReplayDataset:
    name: str
    events: list[dict[str, Any]] = field(default_factory=list)
    frames: list[dict[str, Any]] = field(default_factory=list)

    @classmethod
    def from_files(
        cls,
        events_path: str | Path,
        frames_path: str | Path,
        *,
        name: str,
    ) -> "ReplayDataset":
        event_file = Path(events_path)
        frame_file = Path(frames_path)
        try:
            events = json.loads(event_file.read_text(encoding="utf-8"))
            frames = json.loads(frame_file.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as error:
            raise RuntimeError(f"Unable to load replay dataset {name}: {error}") from error
        if not isinstance(events, list) or not isinstance(frames, list):
            raise ValueError(f"Replay dataset {name} must contain JSON arrays")
        return cls(
            name=name,
            events=[_synthetic_event(event) for event in events],
            frames=[_synthetic_frame(frame) for frame in frames],
        )


TickCallback = Callable[[dict[str, Any]], None]
FactCallback = Callable[[dict[str, Any]], None]


class ReplayEngine:
    def __init__(
        self,
        state: MatchState | None = None,
        orchestrator: Orchestrator | None = None,
        *,
        speed: float = 1.0,
        tick_interval: float = 3.0,
        on_tick: TickCallback | None = None,
        on_fact: FactCallback | None = None,
        defer_model_calls: bool = False,
        stop_event: threading.Event | None = None,
        config_provider: Callable[[], dict[str, Any]] | None = None,
        on_record: Callable[[str, dict[str, Any]], None] | None = None,
    ) -> None:
        if speed <= 0:
            raise ValueError("speed must be greater than zero")
        if tick_interval <= 0:
            raise ValueError("tick_interval must be greater than zero")
        self.state = state or MatchState()
        self.orchestrator = orchestrator or Orchestrator()
        self.speed = speed
        self.tick_interval = tick_interval
        self.on_tick = on_tick
        self.on_fact = on_fact
        self.defer_model_calls = defer_model_calls
        self.stop_event = stop_event or threading.Event()
        self.config_provider = config_provider
        self.on_record = on_record
        self.tick_results: list[dict[str, Any]] = []

    def run(
        self,
        datasets: Iterable[ReplayDataset],
        *,
        mode: str = "Broadcast",
        language: str = "English",
        favourite_team: str | None = None,
        favourite_player: str | None = None,
    ) -> MatchState:
        """Ingest datasets in supplied order and periodically run all agents."""
        dataset_list = list(datasets)
        if not dataset_list:
            raise ValueError("at least one replay dataset is required")
        next_tick_second = self.state.minute * 60.0
        last_timestamp: float | None = None
        timeline_offset = self.state.minute * 60.0

        for dataset in dataset_list:
            records = self._ordered_records(dataset, timeline_offset)
            for timestamp, kind, value in records:
                if self.stop_event.is_set():
                    break
                if last_timestamp is not None and timestamp > last_timestamp:
                    delay = (timestamp - last_timestamp) / self.speed
                    if self.stop_event.wait(delay):
                        break
                last_timestamp = timestamp
                if kind == "event":
                    ingest(self.state, event=value)
                else:
                    ingest(self.state, frame=value)
                if self.on_record:
                    self.on_record(kind, value)

                if timestamp >= next_tick_second:
                    self._tick(
                        mode=mode,
                        language=language,
                        favourite_team=favourite_team,
                        favourite_player=favourite_player,
                    )
                    while next_tick_second <= timestamp:
                        next_tick_second += self.tick_interval

            if records and not self.stop_event.is_set():
                timeline_offset = max(timestamp for timestamp, _, _ in records)

        if (
            not self.stop_event.is_set()
            and (not self.tick_results or self.tick_results[-1]["minute"] < round(self.state.minute, 2))
        ):
            self._tick(
                mode=mode,
                language=language,
                favourite_team=favourite_team,
                favourite_player=favourite_player,
            )
        return self.state

    def _tick(
        self,
        *,
        mode: str,
        language: str,
        favourite_team: str | None,
        favourite_player: str | None,
    ) -> None:
        if self.config_provider:
            config = self.config_provider()
            mode = config.get("mode", mode)
            language = config.get("language", language)
            favourite_team = config.get("favourite_team", favourite_team)
            favourite_player = config.get("favourite_player", favourite_player)
        if self.on_fact:
            facts = self.orchestrator.analyze_facts(self.state)
            self.on_fact(facts)
            if self.defer_model_calls:
                agent_answers = {}
                narration = None
            else:
                agent_answers = (
                    asyncio.run(
                        self.orchestrator.run_routed_agents(
                            self.state,
                            mode=mode,
                        )
                    )
                    if facts.get("moments")
                    else {}
                )
                narration = (
                    asyncio.run(
                        self.orchestrator.narrator.narrate(
                            self.state,
                            mode=mode,
                            language=language,
                            favourite_team=favourite_team,
                            favourite_player=favourite_player,
                        )
                    )
                    if facts.get("moments")
                    else None
                )
            result = {
                **facts,
                "impact": self.state.impact,
                "momentum_series": self.state.momentum_series,
                "moments": self.state.moments,
                "narration": narration,
                "agent_answers": agent_answers,
            }
        else:
            result = self.orchestrator.tick(
                self.state,
                mode=mode,
                language=language,
                favourite_team=favourite_team,
                favourite_player=favourite_player,
            )
        self.tick_results.append(result)
        if self.on_tick:
            self.on_tick(result)

    @staticmethod
    def _ordered_records(
        dataset: ReplayDataset, timeline_offset: float
    ) -> list[tuple[float, str, dict[str, Any]]]:
        records: list[tuple[float, int, str, dict[str, Any]]] = []
        for event in dataset.events:
            local_time = (
                float(event["t"])
                if event.get("t") is not None
                else float(event.get("minute", 0.0)) * 60.0
            )
            absolute_time = timeline_offset + local_time
            normalized = _synthetic_event(event)
            normalized["minute"] = absolute_time / 60.0
            records.append((absolute_time, 0, "event", normalized))

        for frame in dataset.frames:
            local_time = (
                float(frame["t"])
                if frame.get("t") is not None
                else float(frame.get("minute", 0.0)) * 60.0
            )
            absolute_time = timeline_offset + local_time
            normalized = _synthetic_frame(frame)
            normalized["minute"] = absolute_time / 60.0
            records.append((absolute_time, 1, "frame", normalized))

        records.sort(key=lambda item: (item[0], item[1]))
        return [(timestamp, kind, value) for timestamp, _, kind, value in records]


def default_datasets(
    *,
    events_path: str | Path | None = None,
    frames_path: str | Path | None = None,
) -> list[ReplayDataset]:
    """Load the generated synthetic match."""
    project_root = Path(__file__).resolve().parent.parent
    return [
        ReplayDataset.from_files(
            events_path or project_root / "outputs" / "synthetic_events.json",
            frames_path or project_root / "outputs" / "synthetic_frames.json",
            name="synthetic",
        )
    ]
