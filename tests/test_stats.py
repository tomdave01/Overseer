from __future__ import annotations

from fastapi.testclient import TestClient

from api.main import app, manager
from engine.state import MatchState
from engine.stats import StatsAggregator
from generator.match import _roster


def _event(kind: str, outcome: str) -> dict:
    return {
        "match_id": "stats-test",
        "source": "synthetic",
        "minute": 1.0,
        "t": None,
        "type": kind,
        "team": "Rovers",
        "player": "Rico Vale",
        "outcome": outcome,
        "x": 50.0,
        "y": 50.0,
        "end_x": None,
        "end_y": None,
    }


def _frame(t: float, possession: str, x: float) -> dict:
    return {
        "t": t,
        "minute": t / 60,
        "source": "synthetic",
        "poss": possession,
        "ball": {"x": 50.0, "y": 50.0},
        "players": [
            {
                "id": "R-MF-01",
                "team": "Rovers",
                "role": "MID",
                "shirt": 6,
                "name": "Rico Vale",
                "short_name": "R. Vale",
                "x": x,
                "y": 50.0,
            }
        ],
    }


def test_synthetic_squads_have_stable_identity_and_shirt_metadata():
    squads = _roster()
    assert set(squads) == {"Rovers", "United"}
    for squad in squads.values():
        assert len(squad) == 11
        assert len({player["id"] for player in squad}) == 11
        assert len({player["shirt"] for player in squad}) == 11
        assert all(player["name"] and player["short_name"] for player in squad)
        assert all("." in player["short_name"] for player in squad)


def test_stats_aggregate_handmade_events_and_smoothed_frame_metrics():
    aggregator = StatsAggregator()
    events = [
        _event("pass", "complete"),
        _event("pass", "lost"),
        _event("shot", "on_target"),
        _event("shot", "goal"),
        _event("tackle", "won"),
        _event("tackle", "lost"),
        _event("pressure", ""),
        _event("foul", ""),
    ]
    for event in events:
        aggregator.add_event(event)
    for frame in (
        _frame(0.0, "Rovers", 0.0),
        _frame(1.0, "United", 100.0),
        _frame(2.0, "United", 100.0),
    ):
        aggregator.add_frame(frame)

    player = next(
        player
        for player in aggregator.player_stats(team="Rovers")
        if player["name"] == "Rico Vale"
    )
    assert {
        key: player[key]
        for key in (
            "team",
            "role",
            "shirt",
            "name",
            "short_name",
            "touches",
            "passes_attempted",
            "passes_completed",
            "pass_accuracy_pct",
            "shots",
            "shots_on_target",
            "goals",
            "tackles_won",
            "tackles_lost",
            "pressures",
            "fouls",
            "turnovers_lost",
            "distance_covered_metres",
            "top_speed_kmh",
            "minutes_on_pitch",
        )
    } == {
        "team": "Rovers",
        "role": "MID",
        "shirt": 6,
        "name": "Rico Vale",
        "short_name": "R. Vale",
        "touches": 8,
        "passes_attempted": 2,
        "passes_completed": 1,
        "pass_accuracy_pct": 50.0,
        "shots": 2,
        "shots_on_target": 2,
        "goals": 1,
        "tackles_won": 1,
        "tackles_lost": 1,
        "pressures": 1,
        "fouls": 1,
        "turnovers_lost": 2,
        "distance_covered_metres": 20.0,
        "top_speed_kmh": 36.0,
        "minutes_on_pitch": 0.03,
    }
    assert player["impact_score"] >= 0.0
    assert set(player["impact_components"]) == {
        "involvement",
        "shots_created",
        "defensive_actions",
        "momentum_while_on_ball",
    }
    assert player["gravity_index"] == 0.0
    rovers = aggregator.team_stats()["Rovers"]
    assert rovers["possession_pct"] == 50.0
    assert rovers["passes_attempted"] == 2
    assert rovers["passes_completed"] == 1
    assert rovers["pass_accuracy_pct"] == 50.0
    assert rovers["shots"] == 2
    assert rovers["shots_on_target"] == 2
    assert rovers["goals"] == 1
    assert rovers["tackles_won"] == 1
    assert rovers["pressures"] == 1
    assert rovers["fouls"] == 1
    assert rovers["turnovers"] == 2
    assert rovers["current_block"]


def test_stats_endpoints_return_latest_snapshot_and_filter_by_team():
    original_state = manager.state
    original_stats = manager.stats
    manager.state = MatchState(minute=3.0)
    manager.stats = StatsAggregator()
    manager.stats.add_event(_event("pass", "complete"))
    try:
        with TestClient(app) as client:
            players = client.get("/stats/players?team=Rovers")
            teams = client.get("/stats/teams?team=United")
            invalid_team = client.get("/stats/players?team=Neutral")
        assert players.status_code == 200
        assert players.json()["minute"] == 3.0
        assert any(
            player["name"] == "Rico Vale"
            for player in players.json()["players"]
        )
        assert len(players.json()["players"]) == 11
        assert teams.status_code == 200
        assert list(teams.json()["teams"]) == ["United"]
        assert invalid_team.status_code == 422
    finally:
        manager.state = original_state
        manager.stats = original_stats


def test_stats_reports_impact_and_gravity_for_all_22_roster_players():
    aggregator = StatsAggregator()
    players = []
    for team, squad in _roster().items():
        for index, item in enumerate(squad):
            players.append({
                **item,
                "team": team,
                "x": 20.0 + index * 2.0,
                "y": 20.0 + index * 3.0,
            })
    aggregator.add_frame({
        "t": 0.0,
        "minute": 0.0,
        "poss": "Rovers",
        "ball": {"x": 50.0, "y": 50.0},
        "players": players,
    })

    stats = aggregator.player_stats()
    assert len(stats) == 22
    assert {player["team"] for player in stats} == {"Rovers", "United"}
    assert all("impact_components" in player for player in stats)
    assert all("gravity_index" in player for player in stats)
    assert all(0.0 <= player["gravity_index"] <= 100.0 for player in stats)
