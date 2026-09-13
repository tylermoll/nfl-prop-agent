import json
from datetime import datetime, timezone

import joblib
import pandas as pd
import pytest
from sklearn.dummy import DummyRegressor
from sqlalchemy import create_engine, func, insert, select

from app.shadow_storage import metadata, observations
from scripts.trace_production_prediction import _json, select_feature_row, trace


NOW = datetime(2026, 9, 13, 15, tzinfo=timezone.utc)
KICKOFF = datetime(2026, 9, 13, 20, tzinfo=timezone.utc)


def test_trace_is_query_only_and_reproduces_ordered_prediction(tmp_path):
    database = tmp_path / "production.db"
    engine = create_engine(f"sqlite:///{database}")
    metadata.create_all(engine)
    row = {column.name: None for column in observations.columns}
    row.update({"observation_id": "obs-egbuka", "observed_at_utc": NOW,
        "kickoff_utc": KICKOFF, "game_id": "2026_02_TB_CAR", "player_id": "00-0039912",
        "player_name": "Emeka Egbuka", "team": "TB", "opponent": "CAR",
        "canonical_market": "player_receptions", "line": 4.5, "side": "under",
        "hard_rock_offered_odds": -110, "offered_price_break_even_probability": .5238,
        "model_probability": .92474, "model_version": "initial-production-v1-test", "raw_probability_edge_pp": 40.1,
        "hypothetical_expected_return_per_dollar": .1, "edge_bucket": "large",
        "feature_built_at_utc": NOW, "uncertainty_method": "method", "uncertainty_version": "2",
        "point_prediction": 1.7, "reference_context": {}, "kalshi_context": {},
        "source_observation_ids": {}, "freshness": {}, "context": {"capture_slot": "90m"},
        "confirmation_flags": {}, "research_config": {}})
    with engine.begin() as connection:
        connection.execute(insert(observations), row)

    features = tmp_path / "current.parquet"
    pd.DataFrame([{"player_id": "00-0039912", "player_name": "Emeka Egbuka",
        "home_team": "CAR", "away_team": "TB", "kickoff": KICKOFF,
        "canonical_market": "player_receptions", "feature_built_at_utc": NOW,
        "feature_data_as_of_utc": datetime(2026, 9, 8, tzinfo=timezone.utc),
        "rolling_3_mean": 1.5, "pregame_targets": 3.0}]).to_parquet(features)
    model = DummyRegressor(strategy="constant", constant=1.5).fit(
        pd.DataFrame({"pregame_targets": [0.], "rolling_3_mean": [0.]}), [0.])
    artifact = tmp_path / "receptions.joblib"
    joblib.dump({"pipeline": model, "features": ["pregame_targets", "rolling_3_mean"],
                 "additive_bias_correction": .2, "artifact_id": "initial-production-v1-test",
                 "canonical_market": "player_receptions", "uncertainty_version": "2",
                 "uncertainty_method": "v2"}, artifact)

    result = trace(database_url=f"sqlite:///{database}", feature_path=features,
        artifact_path=artifact, history_cache_dir=tmp_path / "absent", observation_id="obs-egbuka")

    assert result["identity"]["stable_player_id"] == "00-0039912"
    assert [x["name"] for x in result["ordered_features"]] == ["pregame_targets", "rolling_3_mean"]
    assert result["prediction"]["reproduced_final_point_prediction"] == (
        result["prediction"]["raw_estimator_prediction"] + .2)
    assert result["prediction"]["reproduced_final_point_prediction"] == 1.7
    assert result["historical_lineage"]["available"] is False
    json.dumps(result, default=_json)
    with engine.connect() as connection:
        assert connection.execute(select(func.count()).select_from(observations)).scalar() == 1


def test_feature_selection_rejects_a_rebuilt_cache_version():
    cache = pd.DataFrame([{"player_id": "p1", "canonical_market": "player_receptions",
        "kickoff": KICKOFF, "feature_built_at_utc": NOW}])
    observation = {"player_id": "p1", "canonical_market": "player_receptions",
        "kickoff_utc": KICKOFF,
        "feature_built_at_utc": datetime(2026, 9, 13, 14, tzinfo=timezone.utc)}
    with pytest.raises(ValueError, match="not the version used"):
        select_feature_row(cache, observation)


def test_explicit_observation_reports_features_when_historical_artifact_is_absent(tmp_path):
    database = tmp_path / "production.db"
    engine = create_engine(f"sqlite:///{database}")
    metadata.create_all(engine)
    row = {column.name: None for column in observations.columns}
    row.update({"observation_id": "obs-egbuka", "observed_at_utc": NOW,
        "kickoff_utc": KICKOFF, "game_id": "2026_02_TB_CAR", "player_id": "00-0039912",
        "player_name": "Emeka Egbuka", "canonical_market": "player_receptions",
        "model_version": "historical-id", "feature_built_at_utc": NOW, "point_prediction": 1.7,
        "hard_rock_offered_odds": -110, "offered_price_break_even_probability": .5238,
        "model_probability": .92474, "raw_probability_edge_pp": 40.1,
        "hypothetical_expected_return_per_dollar": .1, "edge_bucket": "large",
        "uncertainty_method": "method", "uncertainty_version": "2",
        "context": {"capture_slot": "90m", "artifact_provenance": {
            "required_features": ["pregame_targets", "rolling_3_mean"]}},
        "line": 4.5, "side": "under", "source_observation_ids": {}, "freshness": {},
        "reference_context": {}, "kalshi_context": {}, "confirmation_flags": {}, "research_config": {}})
    with engine.begin() as connection:
        connection.execute(insert(observations), row)
    features = tmp_path / "current.parquet"
    pd.DataFrame([{"player_id": "00-0039912", "player_name": "Emeka Egbuka",
        "home_team": "CAR", "away_team": "TB", "kickoff": KICKOFF,
        "canonical_market": "player_receptions", "feature_built_at_utc": NOW,
        "feature_data_as_of_utc": NOW, "pregame_targets": 3.0, "rolling_3_mean": 1.5}]).to_parquet(features)

    result = trace(database_url=f"sqlite:///{database}", feature_path=features,
        artifact_path=None, artifact_search_roots=[tmp_path / "models"],
        history_cache_dir=tmp_path / "history", observation_id="obs-egbuka")

    assert result["artifact"]["available"] is False
    assert result["feature_cache"]["matching_original_row"] is True
    assert result["ordered_features"] == [
        {"position": 0, "name": "pregame_targets", "value": 3.0},
        {"position": 1, "name": "rolling_3_mean", "value": 1.5}]
    assert result["prediction"]["exact_reproduction_possible"] is False
