from __future__ import annotations

import json
from pathlib import Path

import joblib
import numpy as np
import pandas as pd
import pytest
from sklearn.dummy import DummyRegressor

from app.modeling.benchmark import BenchmarkConfig, MARKETS, chronological_split
from scripts import bootstrap_production_models as module


def rows() -> pd.DataFrame:
    return pd.DataFrame([
        {"season": season, "week": week, "kickoff": pd.Timestamp(f"{season}-09-{week:02d}", tz="UTC")}
        for season in (2022, 2023, 2024, 2025) for week in (1, 2)
    ])


def artifact(market: str) -> dict:
    model = DummyRegressor().fit([[0.0]], [0.0])
    return {"pipeline": model, "features": ["rolling_3_mean"], "canonical_market": market,
            "calibration_predictions": np.array([1.0, 2.0]), "calibration_residuals": np.array([-1.0, 1.0]),
            "uncertainty_method": "prediction_conditional_empirical_residual_ecdf", "uncertainty_version": "1"}


def test_2025_validation_isolation_and_no_2026_outcomes():
    parts = chronological_split(rows(), BenchmarkConfig(validation_season=2025, production_bootstrap=True,
                                                         training_seasons=(2022, 2023, 2024), minimum_train_rows=1))
    assert set(parts["train"].season) == {2022, 2023, 2024}
    assert set(parts["validation"].season) == {2025}
    assert set(parts["test"].season) == {2025}
    contaminated = pd.concat([rows(), pd.DataFrame([{"season": 2026, "week": 1,
        "kickoff": pd.Timestamp("2026-09-01", tz="UTC")}])], ignore_index=True)
    with pytest.raises(ValueError, match="outside production split"):
        chronological_split(contaminated, BenchmarkConfig(validation_season=2025, production_bootstrap=True,
                                                           training_seasons=(2022, 2023, 2024), minimum_train_rows=1))
    with pytest.raises(ValueError, match="2026"):
        module.bootstrap([2022, 2023, 2024], 2026, {m: Path("unused") for m in MARKETS})


def test_three_market_environment_mapping_is_exact():
    assert tuple(module.ENV_BY_MARKET) == MARKETS
    assert len(set(module.ENV_BY_MARKET.values())) == 3
    assert module.ENV_BY_MARKET["player_reception_yds"].endswith("RECEPTION_YDS")
    assert module.ENV_BY_MARKET["player_receptions"].endswith("RECEPTIONS")


def test_artifact_reloadability_and_canonical_market_validation(tmp_path):
    path = tmp_path / "model.joblib"
    joblib.dump(artifact("player_pass_yds"), path)
    assert module._validate_artifact(path, "player_pass_yds")["features"] == ["rolling_3_mean"]
    with pytest.raises(ValueError, match="not canonical market"):
        module._validate_artifact(path, "player_receptions")


def test_atomic_replacement_and_failed_build_preserves_existing_files(tmp_path, monkeypatch):
    destinations, staged = {}, {}
    for market in MARKETS:
        destinations[market] = tmp_path / "live" / f"{market}.joblib"
        destinations[market].parent.mkdir(exist_ok=True)
        destinations[market].write_bytes(("old-" + market).encode())
        staged[market] = tmp_path / f"new-{market}.joblib"
        joblib.dump(artifact(market), staged[market])
    original = module._validate_artifact
    calls = 0
    def fail_second(path, market):
        nonlocal calls
        calls += 1
        if calls == 2:
            raise ValueError("injected failure")
        return original(path, market)
    monkeypatch.setattr(module, "_validate_artifact", fail_second)
    with pytest.raises(ValueError, match="injected"):
        module.atomic_install(staged, destinations)
    assert all(path.read_bytes() == ("old-" + market).encode() for market, path in destinations.items())
    monkeypatch.setattr(module, "_validate_artifact", original)
    module.atomic_install(staged, destinations)
    assert all(module._validate_artifact(path, market)["canonical_market"] == market
               for market, path in destinations.items())


def test_bootstrap_provenance_residual_source_manifest_and_cleanup(tmp_path, monkeypatch):
    work = tmp_path / "temporary"
    destinations = {market: tmp_path / "models" / f"{market}.joblib" for market in MARKETS}
    monkeypatch.setattr(module, "audit_season", lambda *_: {"season": 2025, "safe_for_validation_calibration": True,
                                                            "sources": {"weekly_stats": {"rows": 1}}})
    def fake_build(seasons, output, **_):
        pd.DataFrame({"season": seasons}).to_parquet(output)
        return output
    monkeypatch.setattr(module, "build_seasons", fake_build)
    def fake_benchmark(table, output, config):
        output.mkdir(parents=True)
        markets = {}
        for market in MARKETS:
            joblib.dump(artifact(market), output / f"{market}.joblib")
            markets[market] = {"selected_model": {"family": "dummy", "parameter": 0},
                               "learned_model": {"mae": 1.0},
                               "baselines": {"rolling_3": {"mae": 1.2}},
                               "residual_source": "validation"}
        return {"markets": markets}
    monkeypatch.setattr(module, "run_benchmark", fake_benchmark)
    result = module.bootstrap([2022, 2023, 2024], 2025, destinations, work_dir=work)
    for market, path in destinations.items():
        loaded = joblib.load(path)
        provenance = loaded["provenance"]
        manifest = json.loads(path.with_suffix(".joblib.manifest.json").read_text())
        assert provenance["training_seasons"] == [2022, 2023, 2024]
        assert provenance["validation_calibration_season"] == 2025
        assert provenance["calibration_residual_source"].startswith("validation-only")
        assert manifest["artifact_sha256"] == module._sha256(path)
        assert manifest["feature_schema"] == ["rolling_3_mean"]
    assert result["manifests"]
    # An explicitly supplied work directory is retained, equivalent to --keep-temp.
    assert work.exists()


def test_owned_temporary_directory_cleanup(tmp_path, monkeypatch):
    created = tmp_path / "owned"
    monkeypatch.setattr(module.tempfile, "mkdtemp", lambda **_: str(created))
    monkeypatch.setattr(module, "audit_season", lambda *_: {"safe_for_validation_calibration": False,
                                                            "sources": {}, "blocker": "fixture"})
    with pytest.raises(RuntimeError, match="audit failed"):
        module.bootstrap([2022, 2023, 2024], 2025, {m: tmp_path / m for m in MARKETS})
    assert not created.exists()


def test_bootstrap_module_has_no_market_or_wager_provider_dependencies():
    source = Path(module.__file__).read_text()
    assert "the_odds_api" not in source.lower()
    assert "kalshi" not in source.lower()
    assert "place_wager" not in source.lower()
