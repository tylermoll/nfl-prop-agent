import pandas as pd
import pytest

from app.historical.features import build_modeling_table
from app.historical.normalize import normalize_schedules


def _schedules(rows):
    return pd.DataFrame([
        {"season": pd.Timestamp(kickoff).year, "week": week, "game_id": f"g{week}",
         "home_team": "PIT", "away_team": "ATL", "gameday": kickoff,
         "gametime": gametime}
        for week, kickoff, gametime in rows
    ])


def test_nflverse_eastern_wall_clock_kickoffs_are_true_utc():
    games = normalize_schedules(_schedules([
        (1, "2026-09-13", "13:00"),
        (2, "2026-09-13", "16:25"),
        (3, "2026-09-13", "20:20"),
        (4, "2026-12-13", "13:00"),
    ]))
    assert games.kickoff.tolist() == [
        pd.Timestamp("2026-09-13T17:00:00Z"),
        pd.Timestamp("2026-09-13T20:25:00Z"),
        pd.Timestamp("2026-09-14T00:20:00Z"),
        pd.Timestamp("2026-12-13T18:00:00Z"),
    ]


def test_historical_order_rest_and_strict_leakage_survive_timezone_conversion():
    schedules = _schedules([(1, "2023-10-29", "13:00"), (2, "2023-11-05", "13:00")])
    weekly = pd.DataFrame([
        {"season": 2023, "week": 1, "player_id": "qb", "player_name": "QB",
         "team": "ATL", "attempts": 20, "passing_yards": 200},
        {"season": 2023, "week": 2, "player_id": "qb", "player_name": "QB",
         "team": "ATL", "attempts": 21, "passing_yards": 210},
    ])
    rows = build_modeling_table(weekly, schedules)
    passing = rows[rows.canonical_market == "player_pass_yds"].sort_values("kickoff")
    assert passing.kickoff.tolist() == [pd.Timestamp("2023-10-29T17:00:00Z"),
                                        pd.Timestamp("2023-11-05T18:00:00Z")]
    assert passing.iloc[1].days_rest == pytest.approx(7 + 1 / 24)
    assert pd.isna(passing.iloc[0].previous_game_value)
    assert passing.iloc[1].previous_game_value == 200
