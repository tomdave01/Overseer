"""Incremental player and team statistics from synthetic events and frames."""

from __future__ import annotations

from collections import Counter, deque
from math import hypot
from threading import RLock
from typing import Any

from engine.patterns import block, flank_use, momentum, shape


PITCH_LENGTH_METRES = 105.0
PITCH_WIDTH_METRES = 68.0
MAX_SPEED_KMH = 36.0
POSITION_SMOOTHING_FRAMES = 3


def _new_player(team: str, name: str) -> dict[str, Any]:
    return {
        "team": team,
        "role": None,
        "shirt": None,
        "name": name,
        "short_name": name,
        "touches": 0,
        "passes_attempted": 0,
        "passes_completed": 0,
        "pass_accuracy_pct": 0.0,
        "shots": 0,
        "shots_on_target": 0,
        "goals": 0,
        "tackles_won": 0,
        "tackles_lost": 0,
        "pressures": 0,
        "fouls": 0,
        "turnovers_lost": 0,
        "distance_covered_metres": 0.0,
        "top_speed_kmh": 0.0,
        "minutes_on_pitch": 0.0,
    }


class StatsAggregator:
    """Accumulate event totals and frame-derived physical/team statistics."""

    def __init__(self) -> None:
        self._players: dict[tuple[str, str], dict[str, Any]] = {}
        self._team_events: dict[str, Counter[str]] = {
            "Rovers": Counter(),
            "United": Counter(),
        }
        self._events: list[dict[str, Any]] = []
        self._frames: list[dict[str, Any]] = []
        self._possession_seconds: Counter[str] = Counter()
        self._possession_total_seconds = 0.0
        self._last_frame: dict[str, Any] | None = None
        self._positions: dict[str, deque[tuple[float, float]]] = {}
        self._last_smoothed: dict[str, tuple[float, float, float]] = {}
        self._lock = RLock()

    def add_event(self, event: dict[str, Any]) -> None:
        team = str(event.get("team") or "")
        name = str(event.get("player") or "")
        if team not in self._team_events or not name:
            return
        with self._lock:
            player = self._players.setdefault((team, name), _new_player(team, name))
            player["touches"] += 1
            kind = event.get("type")
            outcome = event.get("outcome")
            totals = self._team_events[team]
            if kind == "pass":
                player["passes_attempted"] += 1
                totals["passes_attempted"] += 1
                if outcome == "complete":
                    player["passes_completed"] += 1
                    totals["passes_completed"] += 1
            elif kind == "shot":
                player["shots"] += 1
                totals["shots"] += 1
                if outcome in {"on_target", "goal"}:
                    player["shots_on_target"] += 1
                    totals["shots_on_target"] += 1
                if outcome == "goal":
                    player["goals"] += 1
                    totals["goals"] += 1
            elif kind == "tackle":
                if outcome == "won":
                    player["tackles_won"] += 1
                    totals["tackles_won"] += 1
                elif outcome == "lost":
                    player["tackles_lost"] += 1
            elif kind == "pressure":
                player["pressures"] += 1
                totals["pressures"] += 1
            elif kind == "foul":
                player["fouls"] += 1
                totals["fouls"] += 1

            if outcome == "lost":
                player["turnovers_lost"] += 1
                totals["turnovers"] += 1
            attempts = player["passes_attempted"]
            player["pass_accuracy_pct"] = (
                round(player["passes_completed"] * 100.0 / attempts, 1)
                if attempts
                else 0.0
            )
            self._events.append(dict(event))

    def add_frame(self, frame: dict[str, Any]) -> None:
        with self._lock:
            timestamp = float(
                frame.get("t")
                if frame.get("t") is not None
                else float(frame.get("minute", 0.0)) * 60.0
            )
            previous = self._last_frame
            previous_time = (
                float(previous.get("t"))
                if previous is not None and previous.get("t") is not None
                else float(previous.get("minute", 0.0)) * 60.0
                if previous is not None
                else timestamp
            )
            elapsed = timestamp - previous_time
            if elapsed > 0 and previous is not None:
                previous_possession = previous.get("poss")
                if previous_possession in self._team_events:
                    self._possession_seconds[previous_possession] += elapsed
                    self._possession_total_seconds += elapsed
                for old_player in previous.get("players", []):
                    team = str(old_player.get("team") or "")
                    name = str(old_player.get("name") or old_player.get("id") or "")
                    player = self._players.setdefault(
                        (team, name), _new_player(team, name)
                    )
                    player["minutes_on_pitch"] += elapsed / 60.0

            for item in frame.get("players", []):
                team = str(item.get("team") or "")
                name = str(item.get("name") or item.get("id") or "")
                if team not in self._team_events or not name:
                    continue
                player = self._players.setdefault((team, name), _new_player(team, name))
                player["role"] = item.get("role")
                player["shirt"] = item.get("shirt")
                player["name"] = item.get("name") or name
                player["short_name"] = item.get("short_name") or player["name"]
                position = (
                    float(item["x"]) * PITCH_LENGTH_METRES / 100.0,
                    float(item["y"]) * PITCH_WIDTH_METRES / 100.0,
                )
                history = self._positions.setdefault(
                    str(item["id"]), deque(maxlen=POSITION_SMOOTHING_FRAMES)
                )
                history.append(position)
                smoothed = (
                    sum(point[0] for point in history) / len(history),
                    sum(point[1] for point in history) / len(history),
                )
                previous_position = self._last_smoothed.get(str(item["id"]))
                if elapsed > 0 and previous_position is not None:
                    previous_x, previous_y, previous_timestamp = previous_position
                    delta = timestamp - previous_timestamp
                    if delta > 0:
                        distance = hypot(
                            smoothed[0] - previous_x,
                            smoothed[1] - previous_y,
                        )
                        speed = min(distance / delta * 3.6, MAX_SPEED_KMH)
                        distance = min(distance, MAX_SPEED_KMH / 3.6 * delta)
                        player["distance_covered_metres"] += distance
                        player["top_speed_kmh"] = max(
                            player["top_speed_kmh"], speed
                        )
                self._last_smoothed[str(item["id"])] = (
                    smoothed[0],
                    smoothed[1],
                    timestamp,
                )

            self._last_frame = dict(frame)
            self._frames.append(dict(frame))

    def player_stats(self, team: str | None = None) -> list[dict[str, Any]]:
        with self._lock:
            players = [
                {
                    **player,
                    "distance_covered_metres": round(
                        player["distance_covered_metres"], 2
                    ),
                    "top_speed_kmh": round(player["top_speed_kmh"], 2),
                    "minutes_on_pitch": round(player["minutes_on_pitch"], 2),
                }
                for (player_team, _), player in sorted(self._players.items())
                if team is None or player_team == team
            ]
            return players

    def team_stats(self, team: str | None = None) -> dict[str, dict[str, Any]]:
        with self._lock:
            results: dict[str, dict[str, Any]] = {}
            for team_name in ("Rovers", "United"):
                if team is not None and team_name != team:
                    continue
                totals = self._team_events[team_name]
                attempts = totals["passes_attempted"]
                possession_pct = (
                    self._possession_seconds[team_name] * 100.0
                    / self._possession_total_seconds
                    if self._possession_total_seconds
                    else self._frame_possession_pct(team_name)
                )
                in_shape = shape(self._frames, team_name, in_poss=True)
                out_shape = shape(self._frames, team_name, in_poss=False)
                block_result = block(self._frames, team_name)
                flank = flank_use(self._events, team_name)
                series = momentum(self._events)
                current_momentum = (
                    series[-1]["value"] if series else 0.0
                )
                if team_name == "United":
                    current_momentum = -current_momentum
                results[team_name] = {
                    "team": team_name,
                    "possession_pct": round(possession_pct, 1),
                    "passes_attempted": attempts,
                    "passes_completed": totals["passes_completed"],
                    "pass_accuracy_pct": round(
                        totals["passes_completed"] * 100.0 / attempts, 1
                    ) if attempts else 0.0,
                    "shots": totals["shots"],
                    "shots_on_target": totals["shots_on_target"],
                    "goals": totals["goals"],
                    "tackles_won": totals["tackles_won"],
                    "pressures": totals["pressures"],
                    "fouls": totals["fouls"],
                    "turnovers": totals["turnovers"],
                    "average_line_height_in_possession": in_shape["line_height"],
                    "average_line_height_out_of_possession": out_shape["line_height"],
                    "current_block": block_result["block"] if out_shape["sample_frames"] else None,
                    "formation": out_shape["formation"],
                    "width": out_shape["width"],
                    "depth": out_shape["depth"],
                    "flank_share": flank["shares"],
                    "momentum": round(current_momentum, 4),
                }
            return results

    def _frame_possession_pct(self, team: str) -> float:
        known = [frame.get("poss") for frame in self._frames if frame.get("poss")]
        if not known:
            return 0.0
        return known.count(team) * 100.0 / len(known)
