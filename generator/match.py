"""Reproducible synthetic match data for Overseer."""

from __future__ import annotations

import json
import math
import random
from pathlib import Path
from typing import Any


MATCH_ID = "synthetic-42"
MATCH_MINUTES = 90
FRAME_INTERVAL_SECONDS = 0.2
ROVERS = "Rovers"
UNITED = "United"
GOAL_TIMES = ((24.0, ROVERS), (58.0, UNITED), (72.0, ROVERS))
KEY_ROVERS_PLAYER = "Rico Vale"


def _roster() -> dict[str, list[dict[str, Any]]]:
    return {
        ROVERS: [
            {"id": "R-GK-01", "shirt": 1, "name": "Milo Hart", "short_name": "M. Hart", "role": "GK"},
            {"id": "R-DF-01", "shirt": 2, "name": "Tomas Reed", "short_name": "T. Reed", "role": "DEF"},
            {"id": "R-DF-02", "shirt": 3, "name": "Eli Ward", "short_name": "E. Ward", "role": "DEF"},
            {"id": "R-DF-03", "shirt": 4, "name": "Noah Pike", "short_name": "N. Pike", "role": "DEF"},
            {"id": "R-DF-04", "shirt": 5, "name": "Sam Rowan", "short_name": "S. Rowan", "role": "DEF"},
            {"id": "R-MF-01", "shirt": 6, "name": KEY_ROVERS_PLAYER, "short_name": "R. Vale", "role": "MID"},
            {"id": "R-MF-02", "shirt": 8, "name": "Ari Stone", "short_name": "A. Stone", "role": "MID"},
            {"id": "R-MF-03", "shirt": 10, "name": "Kai Mercer", "short_name": "K. Mercer", "role": "MID"},
            {"id": "R-MF-04", "shirt": 11, "name": "Jude Ellis", "short_name": "J. Ellis", "role": "MID"},
            {"id": "R-FW-01", "shirt": 9, "name": "Leo Vale", "short_name": "L. Vale", "role": "FWD"},
            {"id": "R-FW-02", "shirt": 7, "name": "Finn Cole", "short_name": "F. Cole", "role": "FWD"},
        ],
        UNITED: [
            {"id": "U-GK-01", "shirt": 1, "name": "Owen Shaw", "short_name": "O. Shaw", "role": "GK"},
            {"id": "U-DF-01", "shirt": 2, "name": "Ben Frost", "short_name": "B. Frost", "role": "DEF"},
            {"id": "U-DF-02", "shirt": 3, "name": "Max Quinn", "short_name": "M. Quinn", "role": "DEF"},
            {"id": "U-DF-03", "shirt": 4, "name": "Jay Brooks", "short_name": "J. Brooks", "role": "DEF"},
            {"id": "U-DF-04", "shirt": 5, "name": "Cal Webb", "short_name": "C. Webb", "role": "DEF"},
            {"id": "U-MF-01", "shirt": 6, "name": "Nico Lane", "short_name": "N. Lane", "role": "MID"},
            {"id": "U-MF-02", "shirt": 8, "name": "Ash Bell", "short_name": "A. Bell", "role": "MID"},
            {"id": "U-MF-03", "shirt": 10, "name": "Ezra King", "short_name": "E. King", "role": "MID"},
            {"id": "U-FW-01", "shirt": 7, "name": "Theo Gray", "short_name": "T. Gray", "role": "FWD"},
            {"id": "U-FW-02", "shirt": 9, "name": "Ira West", "short_name": "I. West", "role": "FWD"},
            {"id": "U-FW-03", "shirt": 11, "name": "Remy Lake", "short_name": "R. Lake", "role": "FWD"},
        ],
    }


def player_metadata() -> dict[str, dict[str, Any]]:
    return {
        player["id"]: {
            "team": team,
            "role": player["role"],
            "shirt": player["shirt"],
            "name": player["name"],
            "short_name": player["short_name"],
        }
        for team, squad in _roster().items()
        for player in squad
    }


def _relative_x(team: str, x: float) -> float:
    return x if team == ROVERS else 100.0 - x


def _raw_x(team: str, x: float) -> float:
    return x if team == ROVERS else 100.0 - x


def _layout(roster: list[dict[str, Any]], team: str) -> list[tuple[float, float]]:
    if team == ROVERS:
        rows = {
            "GK": (5.0, (50.0,)),
            "DEF": (28.0, (9.0, 37.0, 63.0, 91.0)),
            "MID": (48.0, (10.0, 36.0, 64.0, 90.0)),
            "FWD": (68.0, (37.0, 63.0)),
        }
    else:
        rows = {
            "GK": (5.0, (50.0,)),
            "DEF": (29.0, (9.0, 36.0, 64.0, 91.0)),
            "MID": (48.0, (18.0, 50.0, 82.0)),
            "FWD": (68.0, (18.0, 50.0, 82.0)),
        }
    indexes: dict[str, int] = {}
    positions: list[tuple[float, float]] = []
    for player in roster:
        role = player["role"]
        row_x, row_y = rows[role]
        index = indexes.get(role, 0)
        positions.append((row_x, row_y[index]))
        indexes[role] = index + 1
    return positions


def _phase_line(team: str, minute: float, has_ball: bool) -> float:
    if team == UNITED and not has_ball:
        return 26.0 + 1.5 * math.sin(minute / 7.0)
    if team == ROVERS and not has_ball:
        return 59.0 + 1.5 * math.sin(minute / 8.0)
    if has_ball:
        return 58.0 if team == ROVERS else 48.0
    return 40.0


def _make_segments(rng: random.Random) -> list[dict[str, Any]]:
    segments: list[dict[str, Any]] = []
    now = 0.0
    current_team = ROVERS
    while now < MATCH_MINUTES * 60:
        duration = min(rng.uniform(42.0, 78.0), MATCH_MINUTES * 60 - now)
        segments.append(
            {
                "start": now,
                "end": now + duration,
                "team": current_team,
                "events": [],
            }
        )
        now += duration
        current_team = UNITED if current_team == ROVERS else ROVERS
    return segments


def _event_position(
    rng: random.Random,
    team: str,
    minute: float,
    event_type: str,
    *,
    final_pass: bool = False,
    trigger: str = "other",
) -> tuple[float, float, float | None, float | None]:
    if team == ROVERS:
        attack_x = rng.uniform(45.0, 88.0) if event_type in ("shot", "tackle") else rng.uniform(30.0, 82.0)
        y = rng.uniform(68.0, 96.0) if rng.random() < 0.78 else rng.uniform(35.0, 65.0)
    else:
        attack_x = rng.uniform(16.0, 52.0) if event_type in ("shot", "tackle") else rng.uniform(18.0, 65.0)
        y = rng.uniform(8.0, 34.0) if rng.random() < 0.72 else rng.uniform(36.0, 65.0)
    x = _raw_x(team, attack_x)

    if not final_pass:
        return x, y, None, None

    if team == UNITED and trigger == "back_pass":
        end_attack_x = max(4.0, attack_x - rng.uniform(9.0, 16.0))
        end_y = y + rng.uniform(-5.0, 5.0)
    elif team == UNITED and trigger == "touchline":
        end_attack_x = attack_x - rng.uniform(1.0, 5.0)
        end_y = rng.choice((rng.uniform(1.0, 9.0), rng.uniform(91.0, 99.0)))
    elif trigger == "back_pass":
        end_attack_x = max(4.0, attack_x - rng.uniform(9.0, 16.0))
        end_y = y + rng.uniform(-5.0, 5.0)
    elif trigger == "touchline":
        end_attack_x = attack_x - rng.uniform(1.0, 5.0)
        end_y = rng.choice((rng.uniform(1.0, 9.0), rng.uniform(91.0, 99.0)))
    else:
        end_attack_x = min(96.0, attack_x + rng.uniform(2.0, 9.0))
        end_y = min(95.0, max(5.0, y + rng.uniform(-10.0, 10.0)))
    return x, y, _raw_x(team, end_attack_x), end_y


def _build_events(
    rng: random.Random, segments: list[dict[str, Any]]
) -> list[dict[str, Any]]:
    all_events: list[dict[str, Any]] = []

    for segment_number, segment in enumerate(segments):
        team = segment["team"]
        start, end = segment["start"], segment["end"]
        duration = end - start
        minute = start / 60.0
        count = max(3, round(duration / 13.0))
        if team == ROVERS and minute >= 60:
            count += 1
        times = [
            start + duration * (index + 1) / (count + 1)
            for index in range(count)
        ]
        event_specs: list[tuple[float, str, str | None]] = []
        for index, event_time in enumerate(times):
            if index == count - 1:
                terminal_type = (
                    "tackle"
                    if team == UNITED and segment_number % 4 == 1
                    else "pass"
                )
                event_specs.append((event_time, terminal_type, "lost"))
                continue
            phase_minute = event_time / 60.0
            if 45.0 <= phase_minute < 60.0:
                shot_chance = 0.30 if team == UNITED else 0.055
            elif 60.0 <= phase_minute < 76.0:
                shot_chance = 0.30 if team == ROVERS else 0.055
            else:
                shot_chance = 0.13
            roll = rng.random()
            if roll < shot_chance:
                outcome = "on_target" if rng.random() < 0.68 else "off_target"
                event_specs.append((event_time, "shot", outcome))
            elif roll < shot_chance + 0.10:
                event_specs.append((event_time, "tackle", "won"))
            elif roll < shot_chance + 0.16:
                event_specs.append((event_time, "pressure", ""))
            elif roll < shot_chance + 0.20:
                event_specs.append((event_time, "foul", ""))
            else:
                event_specs.append((event_time, "pass", "complete"))

        ordered_specs = sorted(event_specs, key=lambda spec: spec[0])
        for index, (event_time, event_type, outcome) in enumerate(ordered_specs):
            is_lost_final = index == len(ordered_specs) - 1 and outcome == "lost"
            trigger = "other"
            if is_lost_final and team == UNITED and event_type == "pass":
                trigger = rng.choices(
                    ("back_pass", "touchline", "other"),
                    weights=(0.60, 0.28, 0.12),
                    k=1,
                )[0]
            elif is_lost_final:
                trigger = rng.choice(("back_pass", "touchline", "other"))
            x, y, end_x, end_y = _event_position(
                rng,
                team,
                event_time / 60.0,
                event_type,
                final_pass=is_lost_final,
                trigger=trigger,
            )
            if is_lost_final and team == UNITED:
                # United's losses are deliberately concentrated in its own left channel.
                x = _raw_x(UNITED, rng.uniform(12.0, 30.0))
                y = rng.uniform(8.0, 30.0)
                relative_x = 100.0 - x
                if event_type == "pass" and trigger == "back_pass":
                    end_x = _raw_x(UNITED, max(4.0, relative_x - rng.uniform(9.0, 16.0)))
                    end_y = min(95.0, max(5.0, y + rng.uniform(-5.0, 5.0)))
                elif event_type == "pass" and trigger == "touchline":
                    end_x = _raw_x(UNITED, relative_x - rng.uniform(1.0, 3.0))
                    end_y = rng.choice((rng.uniform(1.0, 9.0), rng.uniform(91.0, 99.0)))
                elif event_type == "pass":
                    end_x = _raw_x(UNITED, min(96.0, relative_x + rng.uniform(2.0, 9.0)))
                    end_y = min(45.0, max(5.0, y + rng.uniform(-10.0, 10.0)))
                else:
                    end_x = None
                    end_y = None
            roster = _roster()[team]
            if team == ROVERS and rng.random() < 0.27:
                player = KEY_ROVERS_PLAYER
            elif event_type == "shot":
                player = rng.choice([p["name"] for p in roster if p["role"] == "FWD"])
            elif event_type in ("tackle", "pressure"):
                player = rng.choice([p["name"] for p in roster if p["role"] in ("DEF", "MID")])
            else:
                player = rng.choice([p["name"] for p in roster if p["role"] != "GK"])

            if outcome == "lost":
                # Put the turnover at the segment boundary for frame synchronization.
                event_time = end - 0.1
            all_events.append(
                {
                    "match_id": MATCH_ID,
                    "source": "synthetic",
                    "minute": round(event_time / 60.0, 3),
                    "t": None,
                    "type": event_type,
                    "team": team,
                    "player": player,
                    "outcome": outcome or "",
                    "x": round(x, 2),
                    "y": round(y, 2),
                    "end_x": round(end_x, 2) if end_x is not None else None,
                    "end_y": round(end_y, 2) if end_y is not None else None,
                }
            )
            segment["events"].append(all_events[-1])

    for goal_minute, goal_team in GOAL_TIMES:
        goal_seconds = goal_minute * 60.0
        segment = next(
            item for item in segments
            if item["start"] <= goal_seconds < item["end"]
        )
        forwards = [player["name"] for player in _roster()[goal_team] if player["role"] == "FWD"]
        attacking_x = rng.uniform(86.0, 96.0)
        goal_event = {
            "match_id": MATCH_ID,
            "source": "synthetic",
            "minute": goal_minute,
            "t": None,
            "type": "shot",
            "team": goal_team,
            "player": rng.choice(forwards),
            "outcome": "goal",
            "x": round(_raw_x(goal_team, attacking_x), 2),
            "y": round(rng.uniform(68.0, 95.0) if goal_team == ROVERS else rng.uniform(5.0, 32.0), 2),
            "end_x": None,
            "end_y": None,
        }
        all_events.append(goal_event)
        segment["events"].append(goal_event)
        segment["events"].sort(key=lambda event: event["minute"])

    return sorted(all_events, key=lambda event: event["minute"])


def _ball_position(segment: dict[str, Any], timestamp: float) -> tuple[float, float]:
    events = segment["events"]
    if not events:
        return 50.0, 50.0
    seconds = timestamp
    event_seconds = [event["minute"] * 60.0 for event in events]
    next_index = next((i for i, value in enumerate(event_seconds) if value >= seconds), len(events) - 1)
    current = events[next_index]
    previous = events[max(0, next_index - 1)]
    previous_t = event_seconds[max(0, next_index - 1)]
    current_t = event_seconds[next_index]
    previous_x = previous["end_x"] if previous["end_x"] is not None else previous["x"]
    previous_y = previous["end_y"] if previous["end_y"] is not None else previous["y"]
    fraction = 0.0 if current_t <= previous_t else min(1.0, max(0.0, (seconds - previous_t) / (current_t - previous_t)))
    x = previous_x + (current["x"] - previous_x) * fraction
    y = previous_y + (current["y"] - previous_y) * fraction
    return min(100.0, max(0.0, x)), min(100.0, max(0.0, y))


def _make_frames(
    rng: random.Random, segments: list[dict[str, Any]]
) -> list[dict[str, Any]]:
    rovers = _roster()[ROVERS]
    united = _roster()[UNITED]
    layouts = {
        ROVERS: _layout(rovers, ROVERS),
        UNITED: _layout(united, UNITED),
    }
    all_frames: list[dict[str, Any]] = []
    segment_index = 0
    total_frames = round(MATCH_MINUTES * 60 / FRAME_INTERVAL_SECONDS)

    for frame_index in range(total_frames):
        timestamp = frame_index * FRAME_INTERVAL_SECONDS
        while segment_index + 1 < len(segments) and timestamp >= segments[segment_index]["end"]:
            segment_index += 1
        segment = segments[segment_index]
        poss = segment["team"]
        minute = timestamp / 60.0
        ball_x, ball_y = _ball_position(segment, timestamp)
        players_by_team: dict[str, list[dict[str, Any]]] = {ROVERS: [], UNITED: []}

        for team, roster in ((ROVERS, rovers), (UNITED, united)):
            has_ball = poss == team
            line = _phase_line(team, minute, has_ball)
            opponent_roster = rovers if team == UNITED else united
            opponent_layout = layouts[ROVERS if team == UNITED else UNITED]
            for player_index, player in enumerate(roster):
                base_x, base_y = layouts[team][player_index]
                if player["role"] == "GK":
                    relative_x = 4.0
                elif has_ball:
                    relative_x = base_x + (line - 48.0)
                else:
                    relative_x = line + (base_x - 48.0) * 0.18
                wobble = math.sin(timestamp / 6.0 + player_index * 1.7) * 1.1
                relative_x = min(94.0, max(5.0, relative_x + wobble))
                y = min(98.0, max(2.0, base_y + math.sin(timestamp / 5.0 + player_index) * 2.0))
                raw_x = _raw_x(team, relative_x)

                if team == UNITED and poss == ROVERS and player["role"] == "DEF":
                    defender_number = [p for p in roster if p["role"] == "DEF"].index(player)
                    marked = next(
                        p for p in opponent_roster
                        if p["role"] in ("FWD", "MID")
                        and p["id"] == (
                            "R-FW-01", "R-FW-02", "R-MF-01", "R-MF-02"
                        )[defender_number]
                    )
                    marked_index = next(i for i, p in enumerate(opponent_roster) if p["id"] == marked["id"])
                    marked_base_x, marked_base_y = opponent_layout[marked_index]
                    marked_relative = marked_base_x + (_phase_line(ROVERS, minute, True) - 48.0)
                    marked_x = _raw_x(ROVERS, marked_relative + math.sin(timestamp / 6.0 + marked_index * 1.7) * 1.1)
                    marked_y = marked_base_y + math.sin(timestamp / 5.0 + marked_index) * 2.0
                    raw_x = min(100.0, max(0.0, marked_x + rng.uniform(-1.8, 1.8)))
                    y = min(100.0, max(0.0, marked_y + rng.uniform(-1.8, 1.8)))

                if team == ROVERS and poss == ROVERS and segment_index > 0 and segments[segment_index - 1]["team"] == UNITED and player["role"] == "DEF":
                    raw_x = min(100.0, max(0.0, ball_x + rng.uniform(-5.0, 5.0)))
                    y = min(100.0, max(0.0, ball_y + rng.uniform(-7.0, 7.0)))

                players_by_team[team].append(
                    {
                        "id": player["id"],
                        "team": team,
                        "role": player["role"],
                        "shirt": player["shirt"],
                        "name": player["name"],
                        "short_name": player["short_name"],
                        "x": round(raw_x, 2),
                        "y": round(y, 2),
                    }
                )

        all_frames.append(
            {
                "t": round(timestamp, 1),
                "minute": round(minute, 3),
                "poss": poss,
                "ball": {"x": round(ball_x, 2), "y": round(ball_y, 2)},
                "players": players_by_team[ROVERS] + players_by_team[UNITED],
            }
        )
    return all_frames


def generate_match(seed: int = 42) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """Return event and frame records for a full synthetic match."""
    rng = random.Random(seed)
    segments = _make_segments(rng)
    events = _build_events(rng, segments)
    frames = _make_frames(rng, segments)
    return events, frames


def write_match(
    output_dir: str | Path | None = None, seed: int = 42
) -> tuple[Path, Path]:
    """Generate the match and write compact JSON arrays to the output directory."""
    destination = Path(output_dir) if output_dir is not None else Path(__file__).resolve().parent.parent / "outputs"
    destination.mkdir(parents=True, exist_ok=True)
    events, frames = generate_match(seed)
    events_path = destination / "synthetic_events.json"
    frames_path = destination / "synthetic_frames.json"
    for path, records in ((events_path, events), (frames_path, frames)):
        with path.open("w", encoding="utf-8") as file:
            json.dump(records, file, separators=(",", ":"), ensure_ascii=False)
    return events_path, frames_path


if __name__ == "__main__":
    event_file, frame_file = write_match()
    print(f"Wrote {event_file} and {frame_file}")
