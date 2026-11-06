"""Deterministic analysis tools used by the Agent Framework roles."""

from __future__ import annotations

from typing import Any

from engine.patterns import (
    block,
    flank_use,
    influence,
    marking,
    momentum,
    press_triggers,
    reliance,
    shape,
    turnover_zones,
)
from engine.gravity import player_gravity
from engine.state import MatchState
from generator.match import player_metadata


ANALYSIS_WINDOW_MINUTES = 10.0
TURNOVER_FLAG_SHARE = 0.35
PRESS_FLAG_RATE = 0.6


def _window_events(state: MatchState) -> list[dict[str, Any]]:
    lower_bound = state.minute - ANALYSIS_WINDOW_MINUTES
    return [event for event in state.events if float(event.get("minute", 0.0)) >= lower_bound]


def _window_frames(state: MatchState) -> list[dict[str, Any]]:
    lower_bound = state.minute - ANALYSIS_WINDOW_MINUTES
    return [frame for frame in state.frames if float(frame.get("minute", 0.0)) >= lower_bound]


class ShapeAnalysisTool:
    """Compute team shapes, blocks, marking styles, and detected changes."""

    def __init__(self) -> None:
        self._previous: dict[str, dict[str, str]] = {}

    def analyze(self, state: MatchState) -> dict[str, Any]:
        frames = _window_frames(state)
        result: dict[str, Any] = {}
        changes: list[dict[str, Any]] = []
        for team, opponent in (("Rovers", "United"), ("United", "Rovers")):
            in_possession = shape(frames, team, in_poss=True)
            out_of_possession = shape(frames, team, in_poss=False)
            block_result = block(frames, team)
            marking_result = marking(frames, team, opponent)
            signature = {
                "formation": out_of_possession["formation"],
                "block": block_result["block"],
            }
            previous = self._previous.get(team)
            if previous:
                for key, label in (("formation", "formation_change"), ("block", "block_change")):
                    if previous[key] != signature[key]:
                        changes.append(
                            {
                                "id": label,
                                "team": team,
                                "from": previous[key],
                                "to": signature[key],
                                "minute": round(state.minute, 2),
                            }
                        )
            self._previous[team] = signature
            result[team] = {
                "in_possession": in_possession,
                "out_of_possession": out_of_possession,
                "block": block_result,
                "marking": marking_result,
            }
        state.shape = {"minute": round(state.minute, 2), "teams": result, "changes": changes}
        return state.shape


class PatternAnalysisTool:
    """Compute only patterns that pass their explicit significance thresholds."""

    def analyze(self, state: MatchState) -> dict[str, Any]:
        events = _window_events(state)
        frames = _window_frames(state)
        findings: dict[str, dict[str, Any]] = {}
        for team in ("Rovers", "United"):
            flank = flank_use(events, team)
            if flank["flagged"]:
                findings[f"{team.lower()}_flank_use"] = flank

            player_reliance = reliance(events, team)
            if player_reliance["flagged"]:
                findings[f"{team.lower()}_reliance"] = player_reliance

            turnovers = turnover_zones(events, team)
            if turnovers["count"] >= 3 and turnovers["share"] >= TURNOVER_FLAG_SHARE:
                findings[f"{team.lower()}_turnover_zone"] = turnovers

            triggers = press_triggers(frames, team, events)
            trigger = triggers["best_trigger"]
            if trigger and triggers["triggers"][trigger]["rate"] >= PRESS_FLAG_RATE:
                findings[f"{team.lower()}_press_trigger"] = triggers

        state.patterns = {"minute": round(state.minute, 2), "findings": findings}
        return state.patterns


class ImpactAnalysisTool:
    """Compute influence rankings and the current rolling momentum series."""

    def analyze(self, state: MatchState) -> dict[str, Any]:
        events = _window_events(state)
        impact = influence(events)
        known = {(item["team"], item["player"]) for item in impact}
        roster: dict[tuple[str, str], dict[str, Any]] = {
            (metadata["team"], metadata["name"]): metadata
            for metadata in player_metadata().values()
        }
        for frame in state.frames:
            for player in frame.get("players", []):
                key = (str(player.get("team") or ""), str(player.get("name") or player.get("id") or ""))
                if key[0] in {"Rovers", "United"} and key[1]:
                    roster[key] = player
        for (team, name), player in roster.items():
            if (team, name) in known:
                continue
            impact.append({
                "team": team,
                "player": name,
                "score": 0.0,
                "involvement": 0.0,
                "shots_created": 0.0,
                "defensive_actions": 0.0,
                "team_momentum": 0.0,
            })
        gravity = player_gravity(state.frames)
        for player in impact:
            player["momentum_while_on_ball"] = player.pop("team_momentum")
            player.update(
                gravity.get(
                    (player["team"], player["player"]),
                    {
                        "gravity_with_ball": 0.0,
                        "gravity_without_ball": 0.0,
                        "gravity_pull_with_ball": 0.0,
                        "gravity_pull_without_ball": 0.0,
                        "gravity_index": 0.0,
                    },
                )
            )
        state.impact = sorted(
            impact,
            key=lambda item: (-item["score"], item["team"], item["player"]),
        )
        state.momentum_series = momentum(events)
        return {
            "impact": state.impact,
            "momentum_series": state.momentum_series,
        }


class MomentDetectionTool:
    """Add deduplicated goals, momentum swings, and shape changes to state."""

    def analyze(self, state: MatchState) -> list[dict[str, Any]]:
        existing_ids = {moment["id"] for moment in state.moments}
        recent_events = _window_events(state)
        additions: list[dict[str, Any]] = []

        for event in recent_events:
            if event.get("type") == "shot" and event.get("outcome") == "goal":
                event_minute = float(event.get("minute", 0.0))
                moment_id = f"goal-{event.get('team')}-{event_minute:.3f}"
                if moment_id not in existing_ids:
                    additions.append(
                        {
                            "id": moment_id,
                            "type": "goal",
                            "team": event.get("team"),
                            "player": event.get("player"),
                            "minute": event_minute,
                            "why": f"{event.get('player')} scored for {event.get('team')}.",
                        }
                    )
                    existing_ids.add(moment_id)

        series = state.momentum_series
        for previous, current in zip(series, series[1:]):
            change = float(current["value"]) - float(previous["value"])
            if abs(change) < 0.4:
                continue
            swing_id = f"momentum-{current['minute']:.2f}"
            if swing_id in existing_ids:
                continue
            beneficiary = "Rovers" if change > 0 else "United"
            additions.append(
                {
                    "id": swing_id,
                    "type": "momentum_swing",
                    "team": beneficiary,
                    "from": previous["value"],
                    "to": current["value"],
                    "minute": current["minute"],
                    "why": (
                        f"Momentum swung {change:+.2f} toward {beneficiary} "
                        f"between {previous['minute']:.0f} and {current['minute']:.0f}."
                    ),
                }
            )
            existing_ids.add(swing_id)

        for change in state.shape.get("changes", []):
            change_id = f"{change['id']}-{change['team']}-{change['minute']:.2f}"
            if change_id in existing_ids:
                continue
            if change["id"] == "block_change":
                why = (
                    f"{change['team']} changed from {change['from']} to {change['to']} "
                    f"at minute {change['minute']:.0f}."
                )
            else:
                why = (
                    f"{change['team']}'s formation changed from {change['from']} "
                    f"to {change['to']}."
                )
            additions.append(
                {
                    "id": change_id,
                    "type": change["id"],
                    "team": change["team"],
                    "from": change["from"],
                    "to": change["to"],
                    "minute": change["minute"],
                    "why": why,
                }
            )
            existing_ids.add(change_id)

        state.moments.extend(additions)
        return additions
