from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import joblib
import numpy as np
import pandas as pd
import pytest
from sklearn.dummy import DummyRegressor

from app.config import settings
from app.modeling.benchmark import MARKETS
from app.models import MarketSnapshot, MarketType, Side
from app.production_pipeline import CurrentFeatureCache, create_scheduler_pipeline
from app.scheduler import DueCapture

NOW = datetime(2026, 9, 12, 12, tzinfo=timezone.utc)
KICKOFF = NOW + timedelta(hours=1)


def _artifact(path, version, feature="x"):
    model = DummyRegressor(strategy="constant", constant=10).fit([[0], [1]], [10, 10])
    joblib.dump({"pipeline": model, "features": [feature], "artifact_id": version,
                 "calibration_predictions": np.array([10., 10.]),
                 "calibration_residuals": np.array([-1., 1.]),
                 "prediction_bin_edges": [-float("inf"), float("inf")]}, path)


def _configure(monkeypatch, tmp_path, *, feature="x"):
    versions = {}
    for market in MARKETS:
        path = tmp_path / f"{market}.joblib"
        versions[market] = f"version-{market}"
        _artifact(path, versions[market], feature)
        monkeypatch.setattr(settings, f"football_artifact_{market}", str(path))
    cache = tmp_path / "features.csv"
    pd.DataFrame([{"player_id": "00-123", "player_name": "Test Player", "team": "BUF",
        "opponent": "MIA", "home_team": "Buffalo Bills", "away_team": "Miami Dolphins",
        "canonical_market": market, "kickoff": KICKOFF.isoformat(),
        "feature_built_at_utc": (NOW-timedelta(minutes=2)).isoformat(),
        "feature_data_as_of_utc": (NOW-timedelta(minutes=3)).isoformat(), feature: 4.}
        for market in MARKETS]).to_csv(cache, index=False)
    monkeypatch.setattr(settings, "football_current_feature_path", str(cache))
    return versions, cache


def _row(source="hardrockbet", side=Side.OVER, market=MarketType.PASS_YDS, line=9.5,
         odds=-110):
    return MarketSnapshot(source=source, source_market_id=f"{source}-{side.value}", game_id="provider-g",
        event_name="Miami Dolphins at Buffalo Bills", home_team="Buffalo Bills", away_team="Miami Dolphins",
        player_name="Test Player", market_type=market, line=line, side=side, american_odds=odds,
        observed_at_utc=NOW-timedelta(seconds=5), source_updated_at_utc=NOW-timedelta(seconds=10))


def test_factory_importable_and_models_load_once_with_exact_selection(monkeypatch, tmp_path):
    versions, _ = _configure(monkeypatch, tmp_path)
    loader, builder = create_scheduler_pipeline()
    first = loader()
    assert loader() is first
    assert set(first.scorers) == set(MARKETS)
    assert {m: first.scorers[m].version for m in MARKETS} == versions
    assert builder is not None


def test_missing_artifact_fails_closed(monkeypatch, tmp_path):
    _configure(monkeypatch, tmp_path)
    monkeypatch.setattr(settings, "football_artifact_player_receptions", str(tmp_path / "absent"))
    loader, _ = create_scheduler_pipeline()
    with pytest.raises(FileNotFoundError, match="player_receptions"):
        loader()


def test_feature_schema_parity_and_no_current_game_leakage(monkeypatch, tmp_path):
    _, cache = _configure(monkeypatch, tmp_path, feature="rolling_3_mean")
    loaded = CurrentFeatureCache(cache)
    assert "rolling_3_mean" in loaded.rows
    unsafe = pd.read_csv(cache).assign(actual_value=99)
    unsafe.to_csv(cache, index=False)
    with pytest.raises(ValueError, match="result_columns"):
        CurrentFeatureCache(cache)


def test_missing_player_identity_fails_closed(monkeypatch, tmp_path):
    _configure(monkeypatch, tmp_path)
    loader, builder = create_scheduler_pipeline()
    capture = DueCapture("provider-g", KICKOFF, "90m", NOW)
    quote = _row(); quote.player_name = "Unknown Player"
    with pytest.raises(ValueError, match="identity"):
        builder(capture, [quote], [], loader(), NOW)


def test_valid_side_observations_provenance_and_partial_context(monkeypatch, tmp_path):
    versions, _ = _configure(monkeypatch, tmp_path)
    loader, builder = create_scheduler_pipeline()
    capture = DueCapture("provider-g", KICKOFF, "90m", NOW)
    hardrock = [_row(side=Side.OVER), _row(side=Side.UNDER, odds=-105)]
    observations = builder(capture, hardrock, [], loader(), NOW)
    assert {row["side"] for row in observations} == {"over", "under"}
    assert all(row["line"] == 9.5 and row["model_version"] == versions["player_pass_yds"] for row in observations)
    assert all(row["point_prediction"] == 10 and row["residual_bucket"] == 0 for row in observations)
    assert all(row["kalshi_context"]["kalshi_unavailable"] for row in observations)
    assert all(row["reference_context"]["insufficient_reference_coverage"] for row in observations)
    provenance = observations[0]["context"]["artifact_provenance"]
    assert provenance["artifact_sha256"] and provenance["market"] == "player_pass_yds"
    assert observations[0]["feature_built_at_utc"] < observations[0]["kickoff_utc"]


def test_reference_and_kalshi_context_are_attached(monkeypatch, tmp_path):
    _configure(monkeypatch, tmp_path)
    monkeypatch.setattr(settings, "consensus_min_reference_books", 1)
    loader, builder = create_scheduler_pipeline()
    reference = [_row("fanduel", Side.OVER, odds=-110), _row("fanduel", Side.UNDER, odds=-110)]
    kalshi = SimpleNamespace(away_team="Miami Dolphins", home_team="Buffalo Bills",
        player_name="Test Player", market_type=MarketType.PASS_YDS, line=9.5,
        source_market_id="KXTEST", contract_price=.55, yes_bid=.54, yes_ask=.56,
        no_bid=.44, no_ask=.46, volume=10, open_interest=20)
    result = builder(DueCapture("provider-g", KICKOFF, "15m", NOW),
                     [_row(), *reference], [kalshi], loader(), NOW)[0]
    assert result["reference_context"]["exact_threshold_over_no_vig_probability"] == pytest.approx(.5)
    assert result["kalshi_context"]["midpoint"] == .55


def test_production_adapter_has_no_order_or_wager_behavior():
    import app.production_pipeline as module
    names = dir(module)
    assert not any(name.startswith(("place_", "submit_", "execute_", "wager_")) for name in names)
