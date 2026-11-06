"""Runtime validation for the shared event and frame JSON shapes."""

from __future__ import annotations

import math
from typing import Any


EVENT_FIELDS = frozenset(
    {
        "match_id", "source", "minute", "t", "type", "team",
        "player", "outcome", "x", "y", "end_x", "end_y",
    }
)
FRAME_FIELDS = frozenset({"t", "minute", "poss", "ball", "players"})
FRAME_OPTIONAL_FIELDS = frozenset({"source"})
PLAYER_FIELDS = frozenset({"id", "team", "role", "x", "y"})
PLAYER_OPTIONAL_FIELDS = frozenset({"shirt", "name", "short_name"})

EVENT_OUTCOMES = {
    "pass": {"complete", "lost"},
    "shot": {"blocked", "off_target", "on_target", "goal"},
    "tackle": {"won", "lost"},
    "pressure": {""},
    "foul": {""},
}
TEAMS = {"Rovers", "United"}
ROLES = {"GK", "DEF", "MID", "FWD"}


def _number(value: Any, label: str, *, coordinate: bool = False) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError(f"{label} must be a number")
    result = float(value)
    if not math.isfinite(result):
        raise ValueError(f"{label} must be finite")
    if coordinate and not 0 <= result <= 100:
        raise ValueError(f"{label} must be normalized to 0..100")
    return result


def validate_event(event: Any) -> None:
    if not isinstance(event, dict) or set(event) != EVENT_FIELDS:
        raise ValueError("Event must contain exactly the shared Event schema fields")
    if not isinstance(event["match_id"], str) or not event["match_id"]:
        raise ValueError("Event match_id must be a non-empty string")
    if event["source"] != "synthetic":
        raise ValueError("Event source must be synthetic")
    minute = _number(event["minute"], "Event minute")
    if minute < 0:
        raise ValueError("Event minute must not be negative")
    if event["t"] is not None and _number(event["t"], "Event t") < 0:
        raise ValueError("Event t must not be negative")
    if event["type"] not in EVENT_OUTCOMES:
        raise ValueError("Event type is not supported")
    if event["team"] not in TEAMS or not isinstance(event["player"], str):
        raise ValueError("Event must have a supported team and player name")
    if event["outcome"] not in EVENT_OUTCOMES[event["type"]]:
        raise ValueError("Event outcome is not valid for its event type")
    for field in ("x", "y"):
        _number(event[field], f"Event {field}", coordinate=True)
    end_x, end_y = event["end_x"], event["end_y"]
    if (end_x is None) != (end_y is None):
        raise ValueError("Event end_x and end_y must both be numbers or both be null")
    if end_x is not None:
        _number(end_x, "Event end_x", coordinate=True)
        _number(end_y, "Event end_y", coordinate=True)


def validate_frame(frame: Any) -> None:
    allowed_fields = (FRAME_FIELDS, FRAME_FIELDS | FRAME_OPTIONAL_FIELDS)
    if not isinstance(frame, dict) or set(frame) not in allowed_fields:
        raise ValueError("Frame must contain exactly the shared Frame schema fields")
    if "source" in frame and frame["source"] != "synthetic":
        raise ValueError("Frame source must be synthetic")
    if _number(frame["minute"], "Frame minute") < 0:
        raise ValueError("Frame minute must not be negative")
    if _number(frame["t"], "Frame t") < 0:
        raise ValueError("Frame t must not be negative")
    if frame["poss"] is not None and frame["poss"] not in TEAMS:
        raise ValueError("Frame poss must be Rovers, United, or null")
    ball = frame["ball"]
    if not isinstance(ball, dict) or set(ball) != {"x", "y"}:
        raise ValueError("Frame ball must contain exactly x and y")
    _number(ball["x"], "Frame ball x", coordinate=True)
    _number(ball["y"], "Frame ball y", coordinate=True)
    players = frame["players"]
    if not isinstance(players, list):
        raise ValueError("Frame players must be a list")
    for player in players:
        if (
            not isinstance(player, dict)
            or not PLAYER_FIELDS <= set(player)
            or set(player) - PLAYER_FIELDS - PLAYER_OPTIONAL_FIELDS
        ):
            raise ValueError("Frame player contains unsupported or missing fields")
        if not isinstance(player["id"], str) or player["team"] not in TEAMS:
            raise ValueError("Frame player must have a string id and supported team")
        if player["role"] not in ROLES:
            raise ValueError("Frame player role is not supported")
        if "shirt" in player and (
            isinstance(player["shirt"], bool)
            or not isinstance(player["shirt"], int)
            or player["shirt"] < 1
        ):
            raise ValueError("Frame player shirt must be a positive integer")
        for field in ("name", "short_name"):
            if field in player and (
                not isinstance(player[field], str) or not player[field].strip()
            ):
                raise ValueError(f"Frame player {field} must be a non-empty string")
        _number(player["x"], "Frame player x", coordinate=True)
        _number(player["y"], "Frame player y", coordinate=True)
