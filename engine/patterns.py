"""Deterministic football match analysis helpers."""

from __future__ import annotations

from collections import Counter, defaultdict
from math import hypot, isfinite
from typing import Any, Iterable


HIGH_PRESS_THRESHOLD = 52.0
MID_BLOCK_THRESHOLD = 40.0
FLANK_SHARE_THRESHOLD = 0.45
RELIANCE_SHARE_THRESHOLD = 0.15
PRESS_DISTANCE_THRESHOLD = 10.0
MARKING_CONSISTENCY_THRESHOLD = 0.65
MARKING_DISTANCE_THRESHOLD = 9.0
MOMENTUM_WINDOW_MINUTES = 5.0
MOMENTUM_SWING_THRESHOLD = 0.4


def _team_x(team: str, x: float) -> float:
    return float(x) if team == "Rovers" else 100.0 - float(x)


def _team_y(team: str, y: float) -> float:
    return float(y) if team == "Rovers" else 100.0 - float(y)


def _event_time(event: dict[str, Any]) -> float:
    if event.get("t") is not None:
        return float(event["t"])
    return float(event.get("minute", 0.0)) * 60.0


def _source_time(record: dict[str, Any]) -> float:
    return float(record.get("minute", 0.0)) * 60.0


def _frames_for(frames: Iterable[dict[str, Any]], team: str, in_poss: bool) -> list[dict[str, Any]]:
    return [frame for frame in frames if (frame.get("poss") == team) == in_poss]


def _kmeans_1d(values: list[float], k: int = 3) -> list[float]:
    if not values:
        return []
    ordered = sorted(values)
    if len(set(ordered)) < k:
        return sorted(set(ordered))
    centers = [ordered[round((len(ordered) - 1) * index / (k - 1))] for index in range(k)]
    for _ in range(30):
        groups = [[] for _ in centers]
        for value in values:
            groups[min(range(len(centers)), key=lambda idx: abs(value - centers[idx]))].append(value)
        updated = [
            sum(group) / len(group) if group else centers[index]
            for index, group in enumerate(groups)
        ]
        if all(abs(left - right) < 1e-6 for left, right in zip(centers, updated)):
            centers = updated
            break
        centers = updated
    return sorted(centers)


def shape(frames: Iterable[dict[str, Any]], team: str, in_poss: bool) -> dict[str, Any]:
    """Summarize line height, depth, width, and three-line formation."""
    selected = _frames_for(frames, team, in_poss)
    if not selected:
        return {
            "team": team,
            "in_possession": in_poss,
            "line_height": 0.0,
            "depth": 0.0,
            "width": 0.0,
            "formation": "",
            "sample_frames": 0,
        }

    frame_heights: list[float] = []
    frame_depths: list[float] = []
    frame_widths: list[float] = []
    player_positions: dict[str, list[float]] = defaultdict(list)
    for frame in selected:
        outfield = [
            player for player in frame.get("players", [])
            if player.get("team") == team and player.get("role") != "GK"
        ]
        if not outfield:
            continue
        xs = [_team_x(team, player["x"]) for player in outfield]
        ys = [float(player["y"]) for player in outfield]
        frame_heights.append(sum(xs) / len(xs))
        frame_depths.append(max(xs) - min(xs))
        frame_widths.append(max(ys) - min(ys))
        for player in outfield:
            player_positions[player["id"]].append(_team_x(team, player["x"]))

    average_x = [
        sum(positions) / len(positions)
        for positions in player_positions.values()
        if positions
    ]
    centers = _kmeans_1d(average_x)
    counts = [0] * len(centers)
    for value in average_x:
        counts[min(range(len(centers)), key=lambda idx: abs(value - centers[idx]))] += 1
    return {
        "team": team,
        "in_possession": in_poss,
        "line_height": round(sum(frame_heights) / len(frame_heights), 2) if frame_heights else 0.0,
        "depth": round(sum(frame_depths) / len(frame_depths), 2) if frame_depths else 0.0,
        "width": round(sum(frame_widths) / len(frame_widths), 2) if frame_widths else 0.0,
        "formation": "-".join(str(count) for count in counts) if len(centers) == 3 else "",
        "sample_frames": len(frame_heights),
    }


def block(frames: Iterable[dict[str, Any]], team: str) -> dict[str, Any]:
    """Classify a team's out-of-possession block from its line height."""
    summary = shape(frames, team, in_poss=False)
    height = summary["line_height"]
    label = "high press" if height > HIGH_PRESS_THRESHOLD else "mid block" if height > MID_BLOCK_THRESHOLD else "low block"
    return {"team": team, "line_height": height, "block": label}


def flank_use(events: Iterable[dict[str, Any]], team: str) -> dict[str, Any]:
    """Measure a team's pass and shot distribution across pitch-width bands."""
    counts = Counter({"left": 0, "centre": 0, "right": 0})
    for event in events:
        if event.get("team") != team or event.get("type") not in ("pass", "shot"):
            continue
        y = _team_y(team, float(event.get("y", 50.0)))
        counts["left" if y < 33.0 else "right" if y > 66.0 else "centre"] += 1
    total = sum(counts.values())
    shares = {name: round(count / total, 4) if total else 0.0 for name, count in counts.items()}
    top = max(shares, key=shares.get) if total else None
    return {
        "team": team,
        "counts": dict(counts),
        "shares": shares,
        "top_flank": top,
        "top_share": shares[top] if top else 0.0,
        "flagged": bool(top and shares[top] > FLANK_SHARE_THRESHOLD),
    }


def reliance(events: Iterable[dict[str, Any]], team: str) -> dict[str, Any]:
    """Find the most involved player among passes, shots, and tackles."""
    counts = Counter(
        event.get("player", "")
        for event in events
        if event.get("team") == team and event.get("type") in ("pass", "shot", "tackle")
    )
    total = sum(counts.values())
    top_player, top_count = counts.most_common(1)[0] if counts else (None, 0)
    share = top_count / total if total else 0.0
    return {
        "team": team,
        "top_player": top_player,
        "top_count": top_count,
        "total_involvements": total,
        "share": round(share, 4),
        "flagged": share > RELIANCE_SHARE_THRESHOLD,
    }


def turnover_zones(events: Iterable[dict[str, Any]], team: str) -> dict[str, Any]:
    """Count lost passes and tackles on a team-relative 3-by-3 grid."""
    counts: Counter[str] = Counter()
    for event in events:
        if (
            event.get("team") != team
            or event.get("type") not in ("pass", "tackle")
            or event.get("outcome") != "lost"
        ):
            continue
        x = _team_x(team, float(event.get("x", 50.0)))
        y = _team_y(team, float(event.get("y", 50.0)))
        third = "defensive" if x < 33.0 else "middle" if x < 66.0 else "attacking"
        flank = "left" if y < 33.0 else "right" if y > 66.0 else "centre"
        counts[f"{third}_{flank}"] += 1
    total = sum(counts.values())
    zone, zone_count = counts.most_common(1)[0] if counts else (None, 0)
    return {
        "team": team,
        "zone": zone,
        "count": zone_count,
        "total_turnovers": total,
        "share": round(zone_count / total, 4) if total else 0.0,
        "counts": dict(sorted(counts.items())),
    }


def _nearest_frame(
    frames: list[dict[str, Any]], timestamp: float, source: str | None = None
) -> dict[str, Any] | None:
    candidates = (
        [frame for frame in frames if frame.get("source") == source]
        if source is not None and any(frame.get("source") is not None for frame in frames)
        else frames
    )
    if not candidates:
        return None
    return min(
        candidates,
        key=lambda frame: abs(
            _source_time(frame) - timestamp
        ),
    )


def _press_success(frame: dict[str, Any] | None, defending_team: str) -> bool:
    if frame is None:
        return False
    ball = frame.get("ball")
    if not isinstance(ball, dict) or ball.get("x") is None or ball.get("y") is None:
        return False
    ball_x = float(ball["x"])
    ball_y = float(ball["y"])
    if not isfinite(ball_x) or not isfinite(ball_y):
        return False
    nearby = sum(
        1
        for player in frame.get("players", [])
        if player.get("team") == defending_team
        and player.get("role") != "GK"
        and hypot(float(player.get("x", 0.0)) - ball_x,
                  float(player.get("y", 0.0)) - ball_y)
        < PRESS_DISTANCE_THRESHOLD
    )
    return nearby >= 2


def _classify_lost_pass(event: dict[str, Any]) -> str:
    team = str(event.get("team", ""))
    start_x = _team_x(team, float(event.get("x", 50.0)))
    end_x = _team_x(team, float(event.get("end_x", event.get("x", 50.0))))
    end_y = float(event.get("end_y", event.get("y", 50.0)))
    if end_x < start_x - 4.0:
        return "back_pass"
    if end_y < 15.0 or end_y > 85.0:
        return "touchline"
    return "other"


def press_triggers(
    frames: Iterable[dict[str, Any]],
    defending_team: str,
    events: Iterable[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    """Rank press triggers by success rate; events optionally sharpen trigger classification."""
    frame_list = list(frames)
    samples: dict[str, list[bool]] = {name: [] for name in ("back_pass", "touchline", "other")}

    if events is not None:
        for event in events:
            if (
                event.get("team") == defending_team
                or event.get("type") != "pass"
                or event.get("outcome") != "lost"
            ):
                continue
            source = str(event.get("source", "synthetic"))
            timestamp = _source_time(event)
            target_frame = _nearest_frame(frame_list, timestamp + 2.0, source)
            trigger = _classify_lost_pass(event)
            samples[trigger].append(_press_success(target_frame, defending_team))
    else:
        for index in range(1, len(frame_list)):
            previous, current = frame_list[index - 1], frame_list[index]
            if previous.get("source") != current.get("source"):
                continue
            if previous.get("poss") == current.get("poss") or current.get("poss") != defending_team:
                continue
            source = str(current.get("source", "synthetic"))
            timestamp = _source_time(current)
            prior_frame = _nearest_frame(frame_list, timestamp - 2.0, source)
            ball = (prior_frame or previous).get("ball", {})
            later_ball = previous.get("ball", {})
            attacking_team = str(previous.get("poss", ""))
            dx = _team_x(attacking_team, float(later_ball.get("x", 50.0))) - _team_x(
                attacking_team, float(ball.get("x", 50.0))
            )
            y = float(later_ball.get("y", 50.0))
            trigger = "back_pass" if dx < -2.0 else "touchline" if y < 15.0 or y > 85.0 else "other"
            target_frame = _nearest_frame(frame_list, timestamp + 2.0, source)
            samples[trigger].append(_press_success(target_frame, defending_team))

    results = {
        name: {
            "samples": len(outcomes),
            "presses": sum(outcomes),
            "rate": round(sum(outcomes) / len(outcomes), 4) if outcomes else 0.0,
        }
        for name, outcomes in samples.items()
    }
    eligible = [
        (name, data["rate"])
        for name, data in results.items()
        if data["samples"] >= 3
    ]
    best_trigger = max(eligible, key=lambda item: item[1])[0] if eligible else None
    return {
        "defending_team": defending_team,
        "triggers": results,
        "best_trigger": best_trigger,
    }


def marking(
    frames: Iterable[dict[str, Any]], defending: str, attacking: str
) -> dict[str, Any]:
    """Measure how consistently each defender tracks the same nearest opponent."""
    seen: dict[str, list[tuple[str, float]]] = defaultdict(list)
    for frame in frames:
        if frame.get("poss") == defending:
            continue
        defenders = [
            player for player in frame.get("players", [])
            if player.get("team") == defending and player.get("role") == "DEF"
        ]
        attackers = [
            player for player in frame.get("players", [])
            if player.get("team") == attacking and player.get("role") != "GK"
        ]
        if not attackers:
            continue
        for defender in defenders:
            nearest, distance = min(
                ((
                    opponent,
                    hypot(float(defender["x"]) - float(opponent["x"]),
                          float(defender["y"]) - float(opponent["y"])),
                ) for opponent in attackers),
                key=lambda item: item[1],
            )
            seen[str(defender["id"])].append((str(nearest["id"]), distance))

    defender_results: list[dict[str, Any]] = []
    for defender_id, observations in sorted(seen.items()):
        targets = Counter(target for target, _ in observations)
        consistent = targets.most_common(1)[0][1] / len(observations)
        mean_distance = sum(distance for _, distance in observations) / len(observations)
        defender_results.append(
            {
                "defender": defender_id,
                "nearest_opponent": targets.most_common(1)[0][0],
                "consistency": round(consistent, 4),
                "mean_distance": round(mean_distance, 2),
                "samples": len(observations),
            }
        )
    consistency = (
        sum(item["consistency"] for item in defender_results) / len(defender_results)
        if defender_results else 0.0
    )
    mean_distance = (
        sum(item["mean_distance"] for item in defender_results) / len(defender_results)
        if defender_results else 0.0
    )
    return {
        "defending_team": defending,
        "attacking_team": attacking,
        "consistency": round(consistency, 4),
        "mean_distance": round(mean_distance, 2),
        "style": "man-oriented"
        if consistency > MARKING_CONSISTENCY_THRESHOLD and mean_distance < MARKING_DISTANCE_THRESHOLD
        else "zonal",
        "defenders": defender_results,
    }


def _event_weight(event: dict[str, Any]) -> float:
    event_type = event.get("type")
    outcome = event.get("outcome")
    if event_type == "shot":
        return 3.0 if outcome == "goal" else 1.5 if outcome == "on_target" else 1.0
    if event_type == "tackle" and outcome == "won":
        return 0.6
    if event_type == "pressure":
        return 0.2
    if event_type == "pass" and outcome == "complete":
        return 0.05
    return 0.0


def momentum(
    events: Iterable[dict[str, Any]], window: float = MOMENTUM_WINDOW_MINUTES
) -> list[dict[str, Any]]:
    """Return a rolling team momentum score per window, positive for Rovers."""
    if window <= 0:
        raise ValueError("window must be greater than zero")
    event_list = list(events)
    max_minute = max((float(event.get("minute", 0.0)) for event in event_list), default=0.0)
    bucket_count = max(1, int(max_minute // window) + 1)
    buckets = [{"rovers": 0.0, "united": 0.0} for _ in range(bucket_count)]
    for event in event_list:
        index = min(bucket_count - 1, int(float(event.get("minute", 0.0)) // window))
        key = "rovers" if event.get("team") == "Rovers" else "united"
        buckets[index][key] += _event_weight(event)

    series: list[dict[str, Any]] = []
    for index, scores in enumerate(buckets):
        total = scores["rovers"] + scores["united"]
        value = (scores["rovers"] - scores["united"]) / total if total else 0.0
        series.append(
            {
                "minute": round(index * window, 2),
                "rovers": round(scores["rovers"], 3),
                "united": round(scores["united"], 3),
                "value": round(value, 4),
            }
        )
    return series


def influence(events: Iterable[dict[str, Any]]) -> list[dict[str, Any]]:
    """Score each player's involvement, chance creation, defending, and momentum."""
    event_list = list(events)
    involvement: Counter[tuple[str, str]] = Counter()
    created: Counter[tuple[str, str]] = Counter()
    defensive: Counter[tuple[str, str]] = Counter()
    player_momentum: dict[tuple[str, str], list[float]] = defaultdict(list)
    series = momentum(event_list)

    for index, event in enumerate(event_list):
        key = (str(event.get("team", "")), str(event.get("player", "")))
        event_type = event.get("type")
        if event_type in ("pass", "shot", "tackle", "pressure"):
            involvement[key] += 1
        if event_type in ("tackle", "pressure") and event.get("outcome") in ("won", ""):
            defensive[key] += 1
        if event_type == "shot":
            preceding = next(
                (
                    candidate for candidate in reversed(event_list[max(0, index - 3):index])
                    if candidate.get("team") == event.get("team")
                    and candidate.get("type") == "pass"
                    and _event_time(event) - _event_time(candidate) <= 12.0
                ),
                None,
            )
            if preceding:
                created[(str(preceding.get("team", "")), str(preceding.get("player", "")))] += 1
        bucket_index = min(
            len(series) - 1,
            int(float(event.get("minute", 0.0)) // MOMENTUM_WINDOW_MINUTES),
        )
        signed = series[bucket_index]["value"]
        team_momentum = signed if key[0] == "Rovers" else -signed
        player_momentum[key].append((team_momentum + 1.0) / 2.0)

    keys = set(involvement) | set(created) | set(defensive) | set(player_momentum)
    max_involvement = max(involvement.values(), default=0)
    max_created = max(created.values(), default=0)
    max_defensive = max(defensive.values(), default=0)
    results: list[dict[str, Any]] = []
    for team, player in keys:
        key = (team, player)
        involvement_part = involvement[key] / max_involvement if max_involvement else 0.0
        created_part = created[key] / max_created if max_created else 0.0
        defensive_part = defensive[key] / max_defensive if max_defensive else 0.0
        momentum_part = (
            sum(player_momentum[key]) / len(player_momentum[key])
            if player_momentum[key] else 0.0
        )
        score = 100.0 * (
            0.3 * involvement_part
            + 0.3 * created_part
            + 0.2 * defensive_part
            + 0.2 * momentum_part
        )
        results.append(
            {
                "team": team,
                "player": player,
                "score": round(score, 2),
                "involvement": round(involvement_part, 3),
                "shots_created": round(created_part, 3),
                "defensive_actions": round(defensive_part, 3),
                "team_momentum": round(momentum_part, 3),
            }
        )
    return sorted(results, key=lambda item: (-item["score"], item["team"], item["player"]))
