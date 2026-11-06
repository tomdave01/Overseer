from __future__ import annotations

from engine.gravity import player_gravity


def _frame(t: float, players: list[dict], ball_x: float = 41.0) -> dict:
    return {
        "t": t,
        "minute": t / 60.0,
        "poss": "Rovers",
        "ball": {"x": ball_x, "y": 50.0},
        "players": players,
    }


def _player(
    player_id: str,
    team: str,
    x: float,
    y: float = 50.0,
    role: str = "MID",
) -> dict:
    return {
        "id": player_id,
        "name": player_id,
        "team": team,
        "role": role,
        "x": x,
        "y": y,
    }


def test_gravity_classifies_ball_proximity_and_normalizes_player_pressure():
    first = _frame(0.0, [
        _player("R-P", "Rovers", 40.0),
        _player("R-Q", "Rovers", 39.0),
        _player("R-R", "Rovers", 20.0),
        _player("U-E", "United", 45.0),
        _player("U-GK", "United", 43.0, role="GK"),
    ])
    second = _frame(1.0, [
        _player("R-P", "Rovers", 41.0),
        _player("R-Q", "Rovers", 38.0),
        _player("R-R", "Rovers", 20.0),
        _player("U-E", "United", 45.0),
        _player("U-GK", "United", 43.0, role="GK"),
    ])

    gravity = player_gravity([first, second])

    with_ball = gravity[("Rovers", "R-P")]
    without_ball = gravity[("Rovers", "R-Q")]
    unaffected = gravity[("Rovers", "R-R")]
    assert with_ball["gravity_with_ball"] == 1.0
    assert with_ball["gravity_without_ball"] == 0.0
    assert without_ball["gravity_without_ball"] == 1.0
    assert with_ball["gravity_index"] == 50.0
    assert without_ball["gravity_index"] == 50.0
    assert unaffected["gravity_index"] == 0.0
    assert with_ball["gravity_pull_with_ball"] == 0.5
    assert without_ball["gravity_pull_without_ball"] == 0.0


def test_gravity_excludes_opponent_movement_caused_by_team_shape():
    first = _frame(0.0, [
        _player("R-P", "Rovers", 40.0),
        _player("R-Q", "Rovers", 39.0),
        _player("U-E", "United", 46.0),
    ], ball_x=40.0)
    second = _frame(1.0, [
        _player("R-P", "Rovers", 40.0),
        _player("R-Q", "Rovers", 39.0),
        _player("U-E", "United", 45.0),
    ], ball_x=40.0)

    gravity = player_gravity([first, second])

    assert gravity[("Rovers", "R-P")]["gravity_with_ball"] == 1.0
    assert gravity[("Rovers", "R-P")]["gravity_pull_with_ball"] == 0.0
