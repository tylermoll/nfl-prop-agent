from __future__ import annotations

import numpy as np
import pandas as pd
import pytest

from app.modeling.benchmark import (
    BenchmarkConfig,
    baseline_predictions,
    chronological_split,
    conditional_uncertainty_diagnostics,
    make_pipeline,
    metric_record,
    select_additive_bias_correction,
    uncertainty_diagnostics,
    validate_feature_names,
)


def fixture_rows() -> pd.DataFrame:
    rows = []
    for season in (2021, 2022, 2023):
        for week in (1, 2, 3):
            rows.append({"season": season, "week": week, "kickoff": pd.Timestamp(f"{season}-09-{week * 7:02d}", tz="UTC"),
                         "canonical_market": "player_pass_yds", "actual_value": season - 1900 + week,
                         "previous_game_value": week - 1 if week > 1 else np.nan,
                         "rolling_3_mean": week + .25, "rolling_5_mean": week + .5,
                         "season_to_date_mean": week + .75, "x": float(season + week)})
    return pd.DataFrame(rows)


def test_chronological_split_never_puts_future_rows_in_training():
    parts = chronological_split(fixture_rows(), BenchmarkConfig(test_season=2023, validation_season=2022,
                                                                 minimum_train_rows=1))
    assert parts["train"].kickoff.max() < parts["validation"].kickoff.min()
    assert parts["validation"].kickoff.max() < parts["test"].kickoff.min()
    assert set(parts["train"].season) == {2021}


def test_preprocessor_statistics_are_fit_on_training_only():
    train = pd.DataFrame({"x": [1.0, np.nan, 3.0]})
    model = make_pipeline("ridge", 1.0).fit(train, [1.0, 2.0, 3.0])
    assert model.named_steps["imputer"].statistics_[0] == 2.0
    # Test distribution has a wildly different median but cannot update fit state.
    model.predict(pd.DataFrame({"x": [10_000.0, np.nan]}))
    assert model.named_steps["imputer"].statistics_[0] == 2.0


@pytest.mark.parametrize("column", ["actual_value", "passing_yards", "sportsbook_line", "american_odds",
                                     "kalshi_price", "bookmaker_probability", "target_result"])
def test_targets_actuals_and_market_features_fail_closed(column):
    with pytest.raises(ValueError, match="forbidden"):
        validate_feature_names([column])


def test_residual_distribution_is_supplied_calibration_period_only():
    test = pd.DataFrame({"actual_value": [10.0, 20.0]})
    diagnostic, _, _ = uncertainty_diagnostics(np.array([-1.0, 1.0]), test, np.array([10.0, 20.0]),
                                                (-1.0, 0.0, 1.0), 5)
    # Test residuals are zero; using them would produce scale zero instead.
    assert diagnostic["residual_scale_std"] == pytest.approx(np.sqrt(2))


def test_conditional_residual_bins_are_fit_without_test_outcomes():
    calibration_prediction = np.arange(8.0)
    calibration_residual = np.array([-1, 0, -2, 1, -4, 4, -8, 8], dtype=float)
    prediction = np.array([1.0, 6.0])
    first, _, _ = conditional_uncertainty_diagnostics(
        calibration_prediction, calibration_residual, pd.DataFrame({"actual_value": [1.0, 6.0]}),
        prediction, (-1.0, 0.0, 1.0), 5, scale_bins=2)
    second, _, _ = conditional_uncertainty_diagnostics(
        calibration_prediction, calibration_residual, pd.DataFrame({"actual_value": [999.0, -999.0]}),
        prediction, (-1.0, 0.0, 1.0), 5, scale_bins=2)
    assert first["prediction_bin_edges"] == second["prediction_bin_edges"]
    assert first["residual_scale_by_prediction_bin"] == second["residual_scale_by_prediction_bin"]
    assert first["residual_scale_by_prediction_bin"]["1"] > first["residual_scale_by_prediction_bin"]["0"]


def test_bias_correction_is_fit_and_selected_on_calibration_data_only():
    correction, proposed, before, after = select_additive_bias_correction(
        np.array([10.0, 12.0, 14.0]), np.array([4.0, 6.0, 8.0]))
    assert correction == 6.0
    assert proposed == 6.0
    assert after["mae"] < before["mae"]


def test_metrics_have_documented_sign_convention_and_values():
    result = metric_record(np.array([1.0, 3.0]), np.array([2.0, 2.0]))
    assert result["mae"] == 1.0
    assert result["rmse"] == 1.0
    assert result["median_absolute_error"] == 1.0
    assert result["bias_mean_signed_error"] == 0.0
    assert result["r2"] == 0.0


def test_baselines_are_reproducible_and_use_only_named_pregame_columns():
    frame = fixture_rows().iloc[:2]
    first, second = baseline_predictions(frame), baseline_predictions(frame.copy())
    assert first.keys() == second.keys()
    for name in first:
        np.testing.assert_equal(first[name], second[name])
    assert first["blended_3_5_season"][0] == pytest.approx((1.25 + 1.5 + 1.75) / 3)
