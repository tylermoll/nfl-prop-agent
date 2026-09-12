from datetime import datetime, timezone
from types import SimpleNamespace

import pandas as pd
import pytest

from app.current_features import atomic_write, build_current_features, validate_current_rows
from app.modeling.benchmark import MARKETS

NOW = datetime(2026, 9, 12, 12, tzinfo=timezone.utc)


class Client:
    def __init__(self):
        self.calls = []
        self.data = {
            "schedules": pd.DataFrame([
                {"season": 2026, "week": 1, "game_id": "g1", "home_team": "BUF", "away_team": "MIA", "gameday": "2026-09-01", "gametime": "20:00"},
                {"season": 2026, "week": 2, "game_id": "g2", "home_team": "BUF", "away_team": "MIA", "gameday": "2026-09-13", "gametime": "20:00"},
                {"season": 2026, "week": 3, "game_id": "g3", "home_team": "BUF", "away_team": "MIA", "gameday": "2026-09-20", "gametime": "20:00"},
            ]),
            "weekly_stats": pd.DataFrame([
                {"season": 2026, "week": 1, "player_id": "p1", "player_name": "Quarter Back", "team": "BUF", "attempts": 20, "passing_yards": 200},
                # A released/partial future row must never affect the target.
                {"season": 2026, "week": 2, "player_id": "p1", "player_name": "Quarter Back", "team": "BUF", "attempts": 99, "passing_yards": 999},
            ]),
            "rosters": pd.DataFrame([
                {"gsis_id": "p1", "full_name": "Quarter Back", "team": "BUF", "position": "QB"},
                {"gsis_id": None, "full_name": "No Identity", "team": "BUF", "position": "QB"},
            ]),
        }

    def fetch(self, dataset, season=None, refresh=False):
        self.calls.append((dataset, season, refresh))
        result = self.data[dataset].copy()
        if dataset == "weekly_stats":
            result = result[result.season == season]
        return result


def models(feature="previous_game_value"):
    return SimpleNamespace(scorers={market: SimpleNamespace(artifact={"features": [feature]}) for market in MARKETS})


def test_only_completed_prior_games_contribute_and_future_values_are_excluded(tmp_path):
    rows, report = build_current_features(tmp_path / "out.parquet", dry_run=True, now=NOW,
                                          client=Client(), models=models())
    passing = rows[rows.canonical_market == "player_pass_yds"]
    assert passing.previous_game_value.tolist() == [200]
    assert report.upcoming_games_found == 1
    assert any("missing stable player ID" in item["reason"] for item in report.excluded_players)
    assert (pd.to_datetime(rows.feature_data_as_of_utc, utc=True) < pd.to_datetime(rows.kickoff, utc=True)).all()
    assert (pd.to_datetime(rows.feature_built_at_utc, utc=True) < pd.to_datetime(rows.kickoff, utc=True)).all()


def test_exact_artifact_schema_and_stable_identity_enforced():
    frame = pd.DataFrame([{c: "x" for c in ["player_id", "player_name", "home_team", "away_team",
        "kickoff", "canonical_market", "feature_built_at_utc", "feature_data_as_of_utc", "previous_game_value"]}])
    frame.loc[0, "canonical_market"] = "player_pass_yds"
    frame.loc[0, "kickoff"] = "2026-09-13T20:00:00Z"
    frame.loc[0, "feature_built_at_utc"] = frame.loc[0, "feature_data_as_of_utc"] = "2026-09-12T12:00:00Z"
    validate_current_rows(frame, models())
    with pytest.raises(ValueError, match="stable"):
        validate_current_rows(frame.assign(player_id=""), models())
    with pytest.raises(ValueError, match="schema"):
        validate_current_rows(frame.assign(extra=1), models())


def test_atomic_replacement_and_failed_write_preserves_old_file(tmp_path, monkeypatch):
    output = tmp_path / "features.parquet"
    rows, _ = build_current_features(output, dry_run=True, now=NOW, client=Client(), models=models())
    pd.DataFrame({"old": [1]}).to_parquet(output)
    atomic_write(rows, output, models())
    assert pd.read_parquet(output).player_id.tolist() == ["p1"]
    original = output.read_bytes()
    monkeypatch.setattr(pd.DataFrame, "to_parquet", lambda *a, **k: (_ for _ in ()).throw(RuntimeError("boom")))
    with pytest.raises(RuntimeError, match="boom"):
        atomic_write(rows, output, models())
    assert output.read_bytes() == original


def test_dry_run_does_not_modify_destination_and_uses_only_nflverse_client(tmp_path):
    output = tmp_path / "features.parquet"
    output.write_bytes(b"sentinel")
    client = Client()
    build_current_features(output, dry_run=True, now=NOW, client=client, models=models())
    assert output.read_bytes() == b"sentinel"
    assert {call[0] for call in client.calls} == {"schedules", "weekly_stats", "rosters"}
    assert [call[1] for call in client.calls if call[0] == "weekly_stats"] == [2024, 2025, 2026]


class CrossSeasonClient(Client):
    def __init__(self, *, week_one_complete=False):
        super().__init__()
        target_week = 2 if week_one_complete else 1
        target_day = "2026-09-13" if week_one_complete else "2026-09-06"
        schedule_rows = [
            {"season": 2025, "week": 17, "game_id": "old1", "home_team": "BUF", "away_team": "MIA", "gameday": "2025-12-28", "gametime": "20:00"},
            {"season": 2025, "week": 18, "game_id": "old2", "home_team": "MIA", "away_team": "BUF", "gameday": "2026-01-04", "gametime": "20:00"},
            {"season": 2026, "week": target_week, "game_id": "target", "home_team": "BUF", "away_team": "MIA", "gameday": target_day, "gametime": "20:00"},
        ]
        if week_one_complete:
            schedule_rows.insert(2, {"season": 2026, "week": 1, "game_id": "new1", "home_team": "BUF", "away_team": "MIA", "gameday": "2026-09-06", "gametime": "20:00"})
        self.data["schedules"] = pd.DataFrame(schedule_rows)
        stats = [
            {"season": 2025, "week": 17, "player_id": "stable", "player_name": "Old Name", "team": "BUF", "attempts": 20, "passing_yards": 100},
            {"season": 2025, "week": 18, "player_id": "stable", "player_name": "Old Name", "team": "BUF", "attempts": 25, "passing_yards": 200},
        ]
        if week_one_complete:
            stats.append({"season": 2026, "week": 1, "player_id": "stable", "player_name": "New Name", "team": "BUF", "attempts": 30, "passing_yards": 300})
        # This published target-game row must be excluded by the kickoff cutoff.
        stats.append({"season": 2026, "week": target_week, "player_id": "stable", "player_name": "New Name", "team": "BUF", "attempts": 99, "passing_yards": 999})
        self.data["weekly_stats"] = pd.DataFrame(stats)
        self.data["rosters"] = pd.DataFrame([
            {"gsis_id": "stable", "full_name": "New Name", "team": "BUF", "position": "QB"},
            {"gsis_id": "rookie", "full_name": "Rookie", "team": "MIA", "position": "QB"},
            {"gsis_id": "lineman", "full_name": "Lineman", "team": "BUF", "position": "OL"},
        ])


def test_week_one_uses_stable_id_prior_season_history_and_excludes_rookie(tmp_path):
    now = datetime(2026, 9, 5, 12, tzinfo=timezone.utc)
    rows, report = build_current_features(tmp_path / "week1.parquet", dry_run=True, now=now,
                                          client=CrossSeasonClient(), models=models("rolling_3_mean"))
    passing = rows[rows.canonical_market == "player_pass_yds"]
    assert passing.player_id.tolist() == ["stable"]
    assert passing.rolling_3_mean.iloc[0] == pytest.approx(150)
    assert report.history_seasons_requested == [2024, 2025, 2026]
    assert report.history_seasons_used == [2025]
    assert report.history_rows_used == 2
    assert report.player_history_diagnostics[0]["status"] == "no current-season history; prior-season history successfully used"
    assert any(item["reason"] == "no historical NFL participation" for item in report.excluded_players)
    assert any(item["reason"] == "unsupported position" for item in report.excluded_players)


def test_week_two_combines_week_one_and_prior_season_without_target_leakage(tmp_path):
    rows, report = build_current_features(tmp_path / "week2.parquet", dry_run=True, now=NOW,
        client=CrossSeasonClient(week_one_complete=True), models=models("rolling_3_mean"))
    passing = rows[rows.canonical_market == "player_pass_yds"]
    assert passing.rolling_3_mean.iloc[0] == pytest.approx(200)  # 100, 200, 300; never 999
    assert report.history_seasons_used == [2025, 2026]
    stable = next(item for item in report.player_history_diagnostics if item["player_id"] == "stable")
    assert stable["status"] == "current and prior-season history successfully used"
    assert stable["history_seasons"] == [2025, 2026]
