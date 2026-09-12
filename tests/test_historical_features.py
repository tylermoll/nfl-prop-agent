from __future__ import annotations

import pandas as pd
import pytest

from app.historical.features import build_modeling_table


def fixtures(values=(100, 200, 300, 400)):
    weekly, schedules = [], []
    dates = pd.date_range("2023-09-01", periods=4, freq="7D")
    for week, (value, date) in enumerate(zip(values, dates), 1):
        weekly.extend([
            {"season": 2023, "week": week, "player_id": "qb1", "player_name": "Q One", "team": "KC", "attempts": 20 + week, "passing_yards": value, "targets": 0, "receptions": 0, "receiving_yards": 0},
            {"season": 2023, "week": week, "player_id": "wr1", "player_name": "W One", "team": "KC", "attempts": 0, "passing_yards": 0, "targets": week + 2, "receptions": week, "receiving_yards": value / 2},
            {"season": 2023, "week": week, "player_id": "oppqb", "player_name": "Opponent", "team": "BUF", "attempts": 30, "passing_yards": value + 10, "targets": 0, "receptions": 0, "receiving_yards": 0},
        ])
        schedules.append({"season": 2023, "week": week, "game_id": f"g{week}", "home_team": "KC" if week % 2 else "BUF", "away_team": "BUF" if week % 2 else "KC", "gameday": date.date(), "gametime": "18:00"})
    return pd.DataFrame(weekly), pd.DataFrame(schedules)


def select(table, player="qb1", market="player_pass_yds"):
    return table[(table.player_id == player) & (table.canonical_market == market)].sort_values("week")


def test_current_and_future_values_never_enter_player_windows():
    weekly, schedules = fixtures()
    baseline = select(build_modeling_table(weekly, schedules))
    changed, _ = fixtures(values=(100, 200, 9999, 8888))
    modified = select(build_modeling_table(changed, schedules))
    # Week 3's own value and every future value are invisible at week 3.
    columns = ["previous_game_value", "rolling_3_mean", "season_to_date_mean", "pregame_attempts"]
    pd.testing.assert_series_equal(baseline.iloc[2][columns], modified.iloc[2][columns])
    assert baseline.iloc[2].rolling_3_mean == pytest.approx(150)
    assert baseline.iloc[2].season_to_date_mean == pytest.approx(150)


def test_opponent_allowed_uses_only_prior_games():
    weekly, schedules = fixtures()
    baseline = select(build_modeling_table(weekly, schedules))
    changed, _ = fixtures(values=(100, 200, 9999, 400))
    modified = select(build_modeling_table(changed, schedules))
    assert baseline.iloc[2].opponent_recent_passing_allowed == modified.iloc[2].opponent_recent_passing_allowed
    assert pd.isna(baseline.iloc[0].opponent_recent_passing_allowed)


def test_injury_and_depth_are_strictly_asof_and_not_backfilled():
    weekly, schedules = fixtures()
    # nflverse's 18:00 is an Eastern wall-clock value (EDT on this date).
    kickoff3 = pd.Timestamp("2023-09-15 22:00", tz="UTC")
    injuries = pd.DataFrame([
        {"player_id": "qb1", "observed_at": kickoff3 - pd.Timedelta("1h"), "injury_status": "questionable"},
        {"player_id": "qb1", "observed_at": kickoff3 + pd.Timedelta("1h"), "injury_status": "out"},
    ])
    depths = pd.DataFrame([
        {"player_id": "qb1", "observed_at": kickoff3 - pd.Timedelta("1h"), "depth_rank": 1},
        {"player_id": "qb1", "observed_at": kickoff3 + pd.Timedelta("1h"), "depth_rank": 2},
    ])
    rows = select(build_modeling_table(weekly, schedules, injuries=injuries, depth_charts=depths))
    assert pd.isna(rows.iloc[0].injury_status) and pd.isna(rows.iloc[1].injury_status)
    assert rows.iloc[2].injury_status == "questionable"
    assert rows.iloc[2].depth_rank == 1


def test_role_change_uses_prior_usage_not_target_game():
    weekly, schedules = fixtures()
    snaps = pd.DataFrame({"season": [2023] * 4, "week": [1, 2, 3, 4], "player_id": ["wr1"] * 4, "snap_share": [.2, .4, .99, .8]})
    rows = select(build_modeling_table(weekly, schedules, snaps=snaps), "wr1", "player_receptions")
    assert rows.iloc[2].pregame_snap_share == pytest.approx(.4)
    assert rows.iloc[2].snap_share_delta_prior == pytest.approx(.2)
    assert rows.iloc[2].pregame_targets == 4


def test_output_has_three_realized_statistic_targets():
    weekly, schedules = fixtures()
    rows = build_modeling_table(weekly, schedules)
    wr = rows[(rows.player_id == "wr1") & (rows.week == 2)].set_index("canonical_market")
    assert wr.loc["player_reception_yds", "actual_value"] == 100
    assert wr.loc["player_receptions", "actual_value"] == 2
    assert not any("odds" in column or "kalshi" in column for column in rows)


def test_cross_season_windows_continue_but_season_to_date_and_rest_reset():
    weekly = pd.DataFrame([
        {"season": 2025, "week": 17, "player_id": "qb1", "player_name": "Q", "team": "KC", "attempts": 20, "passing_yards": 100},
        {"season": 2025, "week": 18, "player_id": "qb1", "player_name": "Q", "team": "KC", "attempts": 30, "passing_yards": 200},
        {"season": 2026, "week": 1, "player_id": "qb1", "player_name": "Q", "team": "KC", "attempts": 40, "passing_yards": 300},
        {"season": 2026, "week": 2, "player_id": "qb1", "player_name": "Q", "team": "KC", "attempts": 50, "passing_yards": 400},
    ])
    schedules = pd.DataFrame([
        {"season": 2025, "week": 17, "game_id": "a", "home_team": "KC", "away_team": "BUF", "gameday": "2025-12-28"},
        {"season": 2025, "week": 18, "game_id": "b", "home_team": "KC", "away_team": "BUF", "gameday": "2026-01-04"},
        {"season": 2026, "week": 1, "game_id": "c", "home_team": "KC", "away_team": "BUF", "gameday": "2026-09-06"},
        {"season": 2026, "week": 2, "game_id": "d", "home_team": "KC", "away_team": "BUF", "gameday": "2026-09-13"},
    ])
    rows = select(build_modeling_table(weekly, schedules))
    week1 = rows[(rows.season == 2026) & (rows.week == 1)].iloc[0]
    week2 = rows[(rows.season == 2026) & (rows.week == 2)].iloc[0]
    assert week1.previous_game_value == 200
    assert week1.rolling_3_mean == pytest.approx(150)
    assert pd.isna(week1.season_to_date_mean)
    assert pd.isna(week1.days_rest)
    assert week1.pregame_attempts == 30
    assert week2.season_to_date_mean == 300
    assert week2.days_rest == pytest.approx(7)
