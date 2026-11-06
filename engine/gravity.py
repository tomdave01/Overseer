"""Pressure and convergence metrics derived from player tracking frames."""

from __future__ import annotations

from bisect import bisect_left
from collections import defaultdict
from math import hypot
from typing import Any


RADIUS = 10.0
CONVERGENCE_DISTANCE_THRESHOLD = 0.5
TEAM_SHAPE_MOVEMENT_THRESHOLD = 0.5
PREVIOUS_FRAME_SECONDS = 1.0
FRAME_TIME_TOLERANCE_SECONDS = 0.25

_TEAMS = ("Rovers", "United")


def _distance(left: dict[str, Any], right: dict[str, Any]) -> float:
    return hypot(float(left["x"]) - float(right["x"]), float(left["y"]) - float(right["y"]))


def _centroid(players: list[dict[str, Any]]) -> dict[str, float] | None:
    outfield = [player for player in players if player.get("role") != "GK"]
    if not outfield:
        return None
    return {
        "x": sum(float(player["x"]) for player in outfield) / len(outfield),
        "y": sum(float(player["y"]) for player in outfield) / len(outfield),
    }


def _time(frame: dict[str, Any]) -> float:
    if frame.get("t") is not None:
        return float(frame["t"])
    return float(frame.get("minute", 0.0)) * 60.0


def _player_with_ball(frame: dict[str, Any]) -> str | None:
    possession = frame.get("poss")
    ball = frame.get("ball")
    if possession not in _TEAMS or not isinstance(ball, dict):
        return None
    candidates = [
        player
        for player in frame.get("players", [])
        if player.get("team") == possession
    ]
    if not candidates:
        return None
    nearest = min(
        candidates,
        key=lambda player: hypot(
            float(player["x"]) - float(ball["x"]),
            float(player["y"]) - float(ball["y"]),
        ),
    )
    return str(nearest.get("id") or "")


def player_gravity(frames: list[dict[str, Any]]) -> dict[tuple[str, str], dict[str, float]]:
    """Return each player's average opponent pressure, pull, and normalized index."""
    ordered = sorted(frames, key=_time)
    accumulators: dict[tuple[str, str], dict[str, float]] = defaultdict(
        lambda: {
            "with_count": 0.0,
            "with_frames": 0.0,
            "without_count": 0.0,
            "without_frames": 0.0,
            "with_pull": 0.0,
            "without_pull": 0.0,
        }
    )
    indexes = [_time(frame) for frame in ordered]

    for frame_index, frame in enumerate(ordered):
        now = indexes[frame_index]
        target = now - PREVIOUS_FRAME_SECONDS
        insertion = bisect_left(indexes, target)
        candidates = [
            index
            for index in (insertion - 1, insertion)
            if 0 <= index < frame_index
            and abs(indexes[index] - target) <= FRAME_TIME_TOLERANCE_SECONDS
        ]
        previous_index = (
            min(candidates, key=lambda index: abs(indexes[index] - target))
            if candidates
            else None
        )
        previous = ordered[previous_index] if previous_index is not None else None
        previous_by_id = {
            str(player.get("id")): player
            for player in (previous or {}).get("players", [])
        }
        teams_now = {
            team: [
                player
                for player in frame.get("players", [])
                if player.get("team") == team
            ]
            for team in _TEAMS
        }
        teams_previous = {
            team: [
                player
                for player in (previous or {}).get("players", [])
                if player.get("team") == team
            ]
            for team in _TEAMS
        }
        centroids_now = {team: _centroid(players) for team, players in teams_now.items()}
        centroids_previous = {
            team: _centroid(players) for team, players in teams_previous.items()
        }
        with_ball_id = _player_with_ball(frame)
        players_by_id = {
            str(player.get("id")): player
            for player in frame.get("players", [])
        }

        for player_id, player in players_by_id.items():
            team = player.get("team")
            if team not in _TEAMS:
                continue
            player_name = str(player.get("name") or player_id)
            opponent_team = "United" if team == "Rovers" else "Rovers"
            opponents = [
                opponent
                for opponent in teams_now[opponent_team]
                if opponent.get("role") != "GK"
            ]
            nearby = [
                opponent for opponent in opponents
                if _distance(player, opponent) <= RADIUS
            ]
            category = "with" if player_id == with_ball_id else "without"
            values = accumulators[(team, player_name)]
            values[f"{category}_count"] += len(nearby)
            values[f"{category}_frames"] += 1

            if previous is None:
                continue
            previous_player = previous_by_id.get(player_id)
            old_centroid = centroids_previous[team]
            new_centroid = centroids_now[team]
            if previous_player is None or old_centroid is None or new_centroid is None:
                continue
            converging = 0
            for opponent in nearby:
                previous_opponent = previous_by_id.get(str(opponent.get("id")))
                if previous_opponent is None:
                    continue
                player_closing = (
                    _distance(previous_player, previous_opponent)
                    - _distance(player, opponent)
                ) > CONVERGENCE_DISTANCE_THRESHOLD
                team_shape_closing = (
                    _distance(old_centroid, previous_opponent)
                    - _distance(new_centroid, opponent)
                ) > TEAM_SHAPE_MOVEMENT_THRESHOLD
                if player_closing and not team_shape_closing:
                    converging += 1
            values[f"{category}_pull"] += converging

    result: dict[tuple[str, str], dict[str, float]] = {}
    raw_indexes: dict[tuple[str, str], float] = {}
    for key, values in accumulators.items():
        with_frames = values["with_frames"]
        without_frames = values["without_frames"]
        gravity_with = (
            values["with_count"] / with_frames if with_frames else 0.0
        )
        gravity_without = (
            values["without_count"] / without_frames if without_frames else 0.0
        )
        pull_with = values["with_pull"] / with_frames if with_frames else 0.0
        pull_without = (
            values["without_pull"] / without_frames if without_frames else 0.0
        )
        count_frames = with_frames + without_frames
        raw_index = (
            (values["with_count"] + values["without_count"]) / count_frames
            if count_frames
            else 0.0
        )
        raw_indexes[key] = raw_index
        result[key] = {
            "gravity_with_ball": round(gravity_with, 3),
            "gravity_without_ball": round(gravity_without, 3),
            "gravity_pull_with_ball": round(pull_with, 3),
            "gravity_pull_without_ball": round(pull_without, 3),
            "gravity_index": raw_index,
        }

    maximum = max(raw_indexes.values(), default=0.0)
    for key, values in result.items():
        values["gravity_index"] = round(
            raw_indexes[key] * 100.0 / maximum if maximum else 0.0,
            2,
        )
    return result
