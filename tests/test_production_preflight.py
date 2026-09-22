from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import numpy as np
import pandas as pd
import pytest
from sqlalchemy import create_engine, event

from app.config import settings
from app.modeling.benchmark import MARKETS
from app.modeling.live import FootballArtifactScorer
from app.production_pipeline import ProductionModels
from app.shadow_selection import MODEL_VERSION_ALLOWLIST
from app.shadow_storage import metadata
from scripts.run_production_preflight import _kickoff_crosscheck, run_preflight

NOW = datetime(2026, 9, 12, 12, tzinfo=timezone.utc)


class ConstantPipeline:
    def predict(self, rows):
        return np.full(len(rows), 10.0)


def models():
    scorers, provenance = {}, {}
    for market in MARKETS:
        artifact = {"canonical_market": market, "pipeline": ConstantPipeline(),
                    "features": ["rolling_3_mean"],
                    "calibration_predictions": [9.0, 10.0, 11.0, 12.0],
                    "calibration_residuals": [-1.0, 0.0, 1.0, 2.0],
                    "prediction_bin_edges": [-100.0, 100.0],
                    "artifact_id": MODEL_VERSION_ALLOWLIST[market],
                    "probability_calibration": {"bin_count": 1, "prior_weight": 100.0},
                    "uncertainty_version": "2",
                    "provenance": {"built_at_utc": "2026-09-01T00:00:00+00:00",
                        "training_seasons": [2022, 2023], "validation_calibration_season": 2024,
                        "feature_schema": ["rolling_3_mean"], "calibration_methodology": "empirical",
                        "calibration_version": "1"}}
        scorer = object.__new__(FootballArtifactScorer)
        scorer.path, scorer.artifact, scorer.version = None, artifact, artifact["artifact_id"]
        scorers[market] = scorer
        provenance[market] = {"artifact_path": f"/models/{market}.joblib",
                              "artifact_sha256": MODEL_VERSION_ALLOWLIST[market],
                              "model_version": scorer.version, "required_features": artifact["features"]}
    return ProductionModels(scorers, provenance)


def cache_rows():
    rows = []
    for index, market in enumerate(MARKETS):
        rows.append({"player_id": f"p{index}", "player_name": f"Player {index}",
                     "home_team": "BUF", "away_team": "MIA", "kickoff": NOW + timedelta(days=1),
                     "canonical_market": market, "feature_built_at_utc": NOW - timedelta(minutes=10),
                     "feature_data_as_of_utc": NOW - timedelta(hours=1), "rolling_3_mean": 10.0})
    raw = pd.DataFrame(rows)
    enriched = raw.copy()
    enriched["_kickoff"] = pd.to_datetime(enriched.kickoff, utc=True)
    enriched["_built"] = pd.to_datetime(enriched.feature_built_at_utc, utc=True)
    enriched["_asof"] = pd.to_datetime(enriched.feature_data_as_of_utc, utc=True)
    enriched["_player"] = enriched.player_name.str.lower()
    enriched["_event"] = "MIA@BUF"
    return enriched


def test_current_feature_and_provider_events_match_on_true_utc_kickoff():
    future = cache_rows()
    future["_kickoff"] = pd.Timestamp("2026-09-13T17:00:00Z")
    future["_event"] = "nfl:buf:mia"
    exact = _kickoff_crosscheck([{
        "id": "odds-event", "away_team": "Miami Dolphins", "home_team": "Buffalo Bills",
        "commence_time": "2026-09-13T17:00:00Z",
    }], future)
    assert exact["status"] == "passed"
    assert exact["matched_event_count"] == 1
    assert exact["comparisons"][0]["absolute_difference_seconds"] == 0

    mislabeled_eastern_as_utc = _kickoff_crosscheck([{
        "id": "odds-event", "away_team": "Miami Dolphins", "home_team": "Buffalo Bills",
        "commence_time": "2026-09-13T17:00:00Z",
    }], future.assign(_kickoff=pd.Timestamp("2026-09-13T13:00:00Z")))
    assert mislabeled_eastern_as_utc["status"] == "failed"
    assert mislabeled_eastern_as_utc["material_mismatch_count"] == 1
    assert mislabeled_eastern_as_utc["comparisons"][0]["absolute_difference_seconds"] == 4 * 3600


@pytest.mark.asyncio
async def test_default_preflight_has_no_provider_calls_or_database_writes(monkeypatch, tmp_path):
    for market in MARKETS:
        monkeypatch.setattr(settings, f"football_artifact_{market}", f"/models/{market}.joblib")
    feature_path = tmp_path / "current.parquet"
    feature_path.write_bytes(b"existence is independently reported")
    monkeypatch.setattr(settings, "football_current_feature_path", str(feature_path))
    monkeypatch.setattr(settings, "football_current_feature_max_age_seconds", 21600)
    monkeypatch.setattr(settings, "shadow_selection_eligible_window", "90m")
    monkeypatch.setattr(settings, "shadow_selection_policy_name", "nfl_prop_v2")
    monkeypatch.setattr(settings, "shadow_selection_policy_version", "v2.0")
    monkeypatch.setattr(settings, "shadow_nominal_unit", 10.0)
    monkeypatch.setattr(settings, "scheduler_provider_stale_seconds", 300)
    monkeypatch.setattr(settings, "the_odds_api_key", "configured-but-never-exposed")
    engine = create_engine("sqlite://")
    metadata.create_all(engine)
    statements = []
    event.listen(engine, "before_cursor_execute", lambda conn, cursor, statement, *args: statements.append(statement))
    provider_calls = []

    def forbidden_provider(**kwargs):
        provider_calls.append(kwargs)
        raise AssertionError("default preflight must not instantiate an Odds API provider")

    report, code = await run_preflight(now=NOW, model_loader=models,
        feature_cache_factory=lambda path: SimpleNamespace(rows=cache_rows()),
        engine_factory=lambda url: engine, odds_provider_factory=forbidden_provider)

    assert code == 0 and report["status"] == "ready" and report["v2_status"] == "READY"
    assert provider_calls == []
    assert statements and all(statement.lstrip().upper().startswith("SELECT") for statement in statements)
    live = next(check for check in report["checks"] if check["name"] == "live_hard_rock_props")
    assert live["status"] == "skipped"
    serialized = str(report)
    assert "configured-but-never-exposed" not in serialized
    assert report["policy"]["feature_max_age_seconds"] == 3600
    assert report["policy"]["broader_cache_max_age_seconds"] == 21600
    artifacts = next(check for check in report["checks"] if check["name"] == "production_model_artifacts")
    assert all(item["allowlist_match"] for item in artifacts["artifacts"].values())
    features = next(check for check in report["checks"] if check["name"] == "current_feature_cache")
    assert features["feature_file_exists"] and features["v2_freshness_satisfied"]
    database = next(check for check in report["checks"] if check["name"] == "database_read_only")
    assert database["decision_table_available"] and database["decision_count"] == 0


@pytest.mark.asyncio
async def test_default_preflight_does_not_import_or_call_kalshi(monkeypatch):
    import app.providers.kalshi as kalshi
    monkeypatch.setattr(kalshi.KalshiProvider, "fetch_nfl_player_props",
                        lambda *args, **kwargs: pytest.fail("Kalshi must not be called"))
    # Deliberately invalid inputs make the command fail closed without changing
    # its provider-I/O guarantee.
    monkeypatch.setattr(settings, "football_artifact_player_pass_yds", None)
    report, code = await run_preflight(now=NOW, model_loader=lambda: (_ for _ in ()).throw(RuntimeError("missing")),
                                       engine_factory=lambda url: create_engine("sqlite://"))
    assert code == 1 and report["status"] == "failed"


@pytest.mark.asyncio
async def test_v2_preflight_fails_actual_row_freshness_not_broader_cache_limit(monkeypatch, tmp_path):
    path = tmp_path / "current.parquet"
    path.write_bytes(b"present")
    monkeypatch.setattr(settings, "football_current_feature_path", str(path))
    monkeypatch.setattr(settings, "football_current_feature_max_age_seconds", 21600)
    monkeypatch.setattr(settings, "shadow_selection_eligible_window", "90m")
    rows = cache_rows()
    rows["feature_built_at_utc"] = NOW - timedelta(seconds=3601)
    rows["_built"] = pd.to_datetime(rows.feature_built_at_utc, utc=True)
    engine = create_engine("sqlite://")
    metadata.create_all(engine)
    report, code = await run_preflight(now=NOW, model_loader=models,
        feature_cache_factory=lambda _: SimpleNamespace(rows=rows),
        engine_factory=lambda _: engine)
    assert code == 1 and report["v2_status"] == "NOT_READY"
    check = next(item for item in report["checks"] if item["name"] == "current_feature_cache")
    assert not check["v2_freshness_satisfied"]
    assert check["feature_age_seconds_min"] == check["feature_age_seconds_max"] == 3601
    assert check["feature_built_at_utc_min"] == check["feature_built_at_utc_max"]
    assert report["policy"]["broader_cache_max_age_seconds"] == 21600
    assert report["policy"]["feature_max_age_seconds"] == 3600
