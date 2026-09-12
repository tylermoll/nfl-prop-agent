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
        return self.data[dataset].copy()


def models(feature="previous_game_value"):
    return SimpleNamespace(scorers={market: SimpleNamespace(artifact={"features": [feature]}) for market in MARKETS})


def test_only_completed_prior_games_contribute_and_future_values_are_excluded(tmp_path):
    rows, report = build_current_features(tmp_path / "out.parquet", dry_run=True, now=NOW,
                                          client=Client(), models=models())
    passing = rows[rows.canonical_market == "player_pass_yds"]
    assert passing.previous_game_value.tolist() == [200]
    assert report.upcoming_games_found == 1
    assert any(item["reason"] == "missing stable player ID" for item in report.excluded_players)
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
