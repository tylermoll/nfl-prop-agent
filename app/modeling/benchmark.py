"""Reproducible, time-ordered football-only benchmark modeling.

The module deliberately knows nothing about lines, prices, wagers, or market
providers.  Its only target is the realized football statistic in
``actual_value``.
"""

from __future__ import annotations

import json
import time
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any, Iterable

import joblib
import numpy as np
import pandas as pd
from sklearn.ensemble import HistGradientBoostingRegressor
from sklearn.impute import SimpleImputer
from sklearn.linear_model import Ridge, SGDRegressor
from sklearn.metrics import mean_absolute_error, mean_squared_error, median_absolute_error, r2_score
from sklearn.pipeline import Pipeline
from sklearn.preprocessing import RobustScaler, StandardScaler

MARKETS = ("player_pass_yds", "player_reception_yds", "player_receptions")

# This is an allowlist, not a heuristic selection of numeric columns. Current
# outcomes, identifiers, provider fields, and future additions are excluded by
# default until reviewed here.
FEATURE_GROUPS: dict[str, tuple[str, ...]] = {
    "recent_performance": (
        "previous_game_value", "rolling_3_mean", "rolling_5_mean",
        "rolling_8_mean", "rolling_3_std", "rolling_5_std",
        "rolling_8_std", "season_to_date_mean", "season_to_date_std",
    ),
    "usage": (
        "pregame_attempts", "attempts_rolling_3", "pregame_dropbacks",
        "dropbacks_rolling_3", "pregame_targets", "targets_rolling_3",
        "pregame_receptions", "receptions_rolling_3", "pregame_snap_share",
        "snap_share_rolling_3", "pregame_target_share", "target_share_rolling_3",
        "pregame_air_yards_share", "air_yards_share_rolling_3",
        "targets_per_team_pass", "yards_per_attempt", "yards_per_target", "catch_rate",
    ),
    "role_change": (
        "snap_share_delta_prior", "snap_share_delta_rolling_3",
        "target_share_delta_prior", "target_share_delta_rolling_3",
        "usage_trend", "usage_acceleration", "depth_chart_movement", "depth_rank",
    ),
    "team_environment": ("pregame_team_pass_attempts", "team_pass_attempts_rolling_3"),
    "opponent_context": (
        "opponent_recent_passing_allowed", "opponent_recent_receiving_allowed",
        "days_rest", "is_home",
    ),
}

FORBIDDEN_TOKENS = (
    "actual", "target", "outcome", "result", "line", "odds", "price",
    "sportsbook", "bookmaker", "kalshi", "probability", "payout", "stake", "kelly",
)
BASELINES = {
    "previous_game": ("previous_game_value",),
    "rolling_3": ("rolling_3_mean",),
    "rolling_5": ("rolling_5_mean",),
    "season_to_date": ("season_to_date_mean",),
    "blended_3_5_season": ("rolling_3_mean", "rolling_5_mean", "season_to_date_mean"),
}


@dataclass(frozen=True)
class BenchmarkConfig:
    """Explicit split/evaluation settings recorded in every run manifest."""

    test_season: int | None = None
    validation_season: int | None = None
    minimum_train_rows: int = 50
    random_state: int = 417
    threshold_offsets: tuple[float, ...] = (-1.0, -0.5, 0.0, 0.5, 1.0)
    calibration_bins: int = 10
    # Production bootstrap has no retrospective test set: the named validation
    # season is used for selection, reporting, and calibration, while the fitted
    # estimator remains training-season-only.
    production_bootstrap: bool = False
    training_seasons: tuple[int, ...] | None = None


def validate_feature_names(columns: Iterable[str]) -> list[str]:
    """Fail closed if any target-, identity-, or market-derived field is requested."""
    columns = list(columns)
    bad = [c for c in columns if c in {"season", "week", "player_id", "game_id", "canonical_event_id", "canonical_market",
                                           "passing_yards", "receiving_yards", "receptions", "attempts", "dropbacks"}
           or any(token in c.lower() for token in FORBIDDEN_TOKENS)]
    # Pregame target usage/share features are football covariates, not modeling targets.
    safe_target_names = {"pregame_targets", "targets_rolling_3", "targets_per_team_pass",
                         "pregame_target_share", "target_share_rolling_3",
                         "target_share_delta_prior", "target_share_delta_rolling_3",
                         "yards_per_target"}
    bad = [c for c in bad if c not in safe_target_names]
    if bad:
        raise ValueError(f"forbidden modeling features: {sorted(bad)}")
    return columns


def available_features(frame: pd.DataFrame, groups: Iterable[str] | None = None) -> list[str]:
    selected = groups or FEATURE_GROUPS
    columns = [c for group in selected for c in FEATURE_GROUPS[group] if c in frame.columns]
    return validate_feature_names(columns)


def chronological_split(frame: pd.DataFrame, config: BenchmarkConfig) -> dict[str, pd.DataFrame]:
    """Split whole seasons, requiring strict temporal separation."""
    data = frame.copy()
    data["kickoff"] = pd.to_datetime(data["kickoff"], utc=True)
    seasons = sorted(int(x) for x in data.season.dropna().unique())
    if config.production_bootstrap:
        validation = config.validation_season
        if validation is None:
            raise ValueError("production bootstrap requires validation_season")
        requested = set(config.training_seasons or (s for s in seasons if s < validation))
        if not requested or any(s >= validation for s in requested):
            raise ValueError("training seasons must be non-empty and precede validation")
        unexpected = set(seasons) - requested - {validation}
        if unexpected:
            raise ValueError(f"outcome seasons outside production split: {sorted(unexpected)}")
        train = data[data.season.isin(requested)].sort_values("kickoff").copy()
        calibration = data[data.season == validation].sort_values("kickoff").copy()
        if train.empty or calibration.empty:
            raise ValueError("empty production training or validation split")
        if len(train) < config.minimum_train_rows:
            raise ValueError(f"training split has {len(train)} rows; need {config.minimum_train_rows}")
        if train.kickoff.max() >= calibration.kickoff.min():
            raise ValueError("split is not strictly chronological")
        return {"train": train, "validation": calibration, "test": calibration.copy()}
    test = config.test_season if config.test_season is not None else (seasons[-1] if seasons else None)
    validation = config.validation_season if config.validation_season is not None else (max((s for s in seasons if s < test), default=None) if test is not None else None)
    if test is None or validation is None or validation >= test:
        raise ValueError("at least three ordered seasons are required (train, validation, test)")
    parts = {
        "train": data[data.season < validation].sort_values("kickoff").copy(),
        "validation": data[data.season == validation].sort_values("kickoff").copy(),
        "test": data[data.season == test].sort_values("kickoff").copy(),
    }
    if any(part.empty for part in parts.values()):
        raise ValueError(f"empty chronological split for validation={validation}, test={test}")
    if len(parts["train"]) < config.minimum_train_rows:
        raise ValueError(f"training split has {len(parts['train'])} rows; need {config.minimum_train_rows}")
    if not (parts["train"].kickoff.max() < parts["validation"].kickoff.min() <= parts["validation"].kickoff.max() < parts["test"].kickoff.min()):
        raise ValueError("split is not strictly chronological")
    return parts


def make_pipeline(model: str, parameter: float, random_state: int = 417) -> Pipeline:
    """Median imputation (learned at fit time) plus an estimator."""
    imputer = SimpleImputer(strategy="median", add_indicator=True, keep_empty_features=True)
    if model == "ridge":
        estimator: Any = Pipeline([("scale", StandardScaler()), ("regressor", Ridge(alpha=parameter))])
    elif model == "robust_huber_sgd":
        estimator = Pipeline([("scale", RobustScaler()), ("regressor", SGDRegressor(
            loss="huber", penalty="l2", alpha=parameter, max_iter=2_000,
            tol=1e-4, shuffle=False, random_state=random_state))])
    elif model == "hist_gradient_boosting":
        estimator = HistGradientBoostingRegressor(max_iter=150, learning_rate=.06, max_leaf_nodes=int(parameter),
                                                   l2_regularization=1.0, random_state=random_state)
    else:
        raise ValueError(f"unknown model: {model}")
    return Pipeline([("imputer", imputer), ("model", estimator)])


def metric_record(actual: np.ndarray, prediction: np.ndarray) -> dict[str, float]:
    actual, prediction = np.asarray(actual, float), np.asarray(prediction, float)
    return {
        "n": int(len(actual)), "mae": float(mean_absolute_error(actual, prediction)),
        "rmse": float(mean_squared_error(actual, prediction) ** .5),
        "median_absolute_error": float(median_absolute_error(actual, prediction)),
        "bias_mean_signed_error": float(np.mean(prediction - actual)),
        "r2": _finite_or_none(r2_score(actual, prediction)) if len(actual) > 1 else None,
    }


def baseline_predictions(frame: pd.DataFrame) -> dict[str, np.ndarray]:
    """Deterministic pregame-only baseline predictions."""
    result = {}
    for name, columns in BASELINES.items():
        values = frame[list(columns)].apply(pd.to_numeric, errors="coerce")
        result[name] = values.mean(axis=1, skipna=True).to_numpy(float)
    return result


def select_additive_bias_correction(actual: np.ndarray, prediction: np.ndarray) -> tuple[float, float, dict, dict]:
    """Estimate and validate a mean-residual intercept adjustment in calibration data."""
    proposed = float(np.mean(np.asarray(actual, float) - np.asarray(prediction, float)))
    uncorrected = metric_record(actual, prediction)
    corrected = metric_record(actual, np.asarray(prediction, float) + proposed)
    selected = proposed if corrected["mae"] < uncorrected["mae"] else 0.0
    return selected, proposed, uncorrected, corrected


def _finite_metrics(rows: pd.DataFrame, prediction: np.ndarray) -> dict[str, float]:
    mask = np.isfinite(prediction) & np.isfinite(rows.actual_value.to_numpy(float))
    return metric_record(rows.actual_value.to_numpy(float)[mask], prediction[mask]) | {"missing_predictions": int((~mask).sum())}


def _empirical_exceedance(residuals: np.ndarray, prediction: np.ndarray, threshold: np.ndarray) -> np.ndarray:
    # P(Y > t) = P(residual > t - prediction), using calibration residual ECDF.
    residuals = np.sort(np.asarray(residuals, float))
    return np.asarray([np.mean(residuals > delta) for delta in threshold - prediction])


def uncertainty_diagnostics(calibration_residuals: np.ndarray, test: pd.DataFrame, predictions: np.ndarray,
                            offsets: tuple[float, ...], bins: int) -> tuple[dict, pd.DataFrame, pd.DataFrame]:
    residuals = np.asarray(calibration_residuals, float)
    quantiles = {str(level): np.quantile(residuals, [(1-level)/2, 1-(1-level)/2]).tolist() for level in (.5, .8)}
    intervals: dict[str, Any] = {"residual_scale_std": float(np.std(residuals, ddof=1)), "residual_quantiles": quantiles}
    interval_rows = []
    actual = test.actual_value.to_numpy(float)
    for level, (lo, hi) in quantiles.items():
        covered = (actual >= predictions + lo) & (actual <= predictions + hi)
        intervals[f"coverage_{level}"] = float(covered.mean())
        interval_rows.extend({"row_index": int(i), "level": float(level), "lower": predictions[i] + lo,
                              "upper": predictions[i] + hi, "covered": bool(covered[i])} for i in range(len(test)))
    scale = max(float(np.std(residuals, ddof=1)), 1e-9)
    calibration = []
    for multiple in offsets:
        threshold = predictions + multiple * scale
        probability = _empirical_exceedance(residuals, predictions, threshold)
        outcome = (actual > threshold).astype(float)
        for p, y, t in zip(probability, outcome, threshold):
            calibration.append({"probability": p, "outcome": y, "threshold": t, "offset_scale": multiple})
    raw = pd.DataFrame(calibration)
    raw["bucket"] = pd.cut(raw.probability, np.linspace(0, 1, bins + 1), include_lowest=True).astype(str)
    reliability = raw.groupby("bucket", observed=False).agg(count=("outcome", "size"), mean_probability=("probability", "mean"),
                                                              empirical_frequency=("outcome", "mean")).reset_index()
    intervals["threshold_brier_score"] = float(np.mean((raw.probability - raw.outcome) ** 2))
    return intervals, pd.DataFrame(interval_rows), reliability


def conditional_uncertainty_diagnostics(calibration_predictions: np.ndarray, calibration_residuals: np.ndarray,
                                        test: pd.DataFrame, predictions: np.ndarray,
                                        offsets: tuple[float, ...], bins: int,
                                        scale_bins: int = 4) -> tuple[dict, pd.DataFrame, pd.DataFrame]:
    """Prediction-conditional empirical residual intervals and probabilities.

    Quantile boundaries and every within-bin residual distribution are learned
    solely from the calibration period. Test predictions only select a bin.
    """
    calibration_predictions = np.asarray(calibration_predictions, float)
    calibration_residuals = np.asarray(calibration_residuals, float)
    edges = np.unique(np.quantile(calibration_predictions, np.linspace(0, 1, scale_bins + 1)))
    if len(edges) < 3:
        return uncertainty_diagnostics(calibration_residuals, test, predictions, offsets, bins)
    edges[0], edges[-1] = -np.inf, np.inf
    calibration_bin = np.clip(np.searchsorted(edges, calibration_predictions, side="right") - 1, 0, len(edges) - 2)
    test_bin = np.clip(np.searchsorted(edges, predictions, side="right") - 1, 0, len(edges) - 2)
    pools = {i: calibration_residuals[calibration_bin == i] for i in range(len(edges) - 1)}
    actual = test.actual_value.to_numpy(float)
    diagnostics: dict[str, Any] = {"prediction_bin_edges": [None if not np.isfinite(x) else float(x) for x in edges]}
    interval_rows, calibration_rows = [], []
    for level in (.5, .8):
        covered = np.zeros(len(test), dtype=bool)
        for i, pool in pools.items():
            selected = test_bin == i
            lo, hi = np.quantile(pool, [(1-level)/2, 1-(1-level)/2])
            covered[selected] = ((actual[selected] >= predictions[selected] + lo) &
                                 (actual[selected] <= predictions[selected] + hi))
            interval_rows.extend({"row_index": int(j), "level": level, "lower": predictions[j] + lo,
                                  "upper": predictions[j] + hi, "covered": bool(covered[j]),
                                  "prediction_bin": int(i), "method": "prediction_conditional"}
                                 for j in np.flatnonzero(selected))
        diagnostics[f"coverage_{level}"] = float(covered.mean())
    global_scale = max(float(np.std(calibration_residuals, ddof=1)), 1e-9)
    for multiple in offsets:
        threshold = predictions + multiple * global_scale
        for j in range(len(test)):
            probability = float(np.mean(pools[test_bin[j]] > threshold[j] - predictions[j]))
            calibration_rows.append({"probability": probability, "outcome": float(actual[j] > threshold[j]),
                                     "threshold": threshold[j], "offset_scale": multiple})
    raw = pd.DataFrame(calibration_rows)
    raw["bucket"] = pd.cut(raw.probability, np.linspace(0, 1, bins + 1), include_lowest=True).astype(str)
    reliability = raw.groupby("bucket", observed=False).agg(count=("outcome", "size"), mean_probability=("probability", "mean"),
                                                              empirical_frequency=("outcome", "mean")).reset_index()
    diagnostics["threshold_brier_score"] = float(np.mean((raw.probability - raw.outcome) ** 2))
    diagnostics["residual_scale_by_prediction_bin"] = {str(i): float(np.std(pool, ddof=1)) for i, pool in pools.items()}
    return diagnostics, pd.DataFrame(interval_rows), reliability


def _model_candidates(config: BenchmarkConfig):
    for family, parameters in (("ridge", (.1, 1.0, 10.0)), ("robust_huber_sgd", (.0001, .001)), ("hist_gradient_boosting", (15, 31))):
        for parameter in parameters:
            yield family, parameter, make_pipeline(family, parameter, config.random_state)


def run_benchmark(table: pd.DataFrame, output_dir: str | Path, config: BenchmarkConfig = BenchmarkConfig()) -> dict:
    """Train three independent market benchmarks and persist reproducible artifacts."""
    started = time.perf_counter()
    output = Path(output_dir)
    output.mkdir(parents=True, exist_ok=True)
    summary: dict[str, Any] = {"config": asdict(config), "markets": {}}
    all_predictions, all_intervals, all_calibration, all_calibration_residuals = [], [], [], []
    for market in MARKETS:
        market_rows = table[table.canonical_market == market].copy()
        parts = chronological_split(market_rows, config)
        features = available_features(market_rows)
        if not features:
            raise ValueError(f"no allowlisted features available for {market}")
        train, validation, test = parts["train"], parts["validation"], parts["test"]
        candidates = []
        for family, parameter, pipeline in _model_candidates(config):
            pipeline.fit(train[features], train.actual_value)
            candidate_prediction = pipeline.predict(validation[features])
            candidate_metrics = metric_record(validation.actual_value, candidate_prediction)
            candidates.append((candidate_metrics["mae"], family, parameter, candidate_metrics))
        _, selected_family, selected_parameter, _ = min(candidates, key=lambda item: item[0])
        calibration_model = make_pipeline(selected_family, selected_parameter, config.random_state)
        calibration_model.fit(train[features], train.actual_value)
        calibration_prediction = calibration_model.predict(validation[features])
        calibration_residuals = validation.actual_value.to_numpy(float) - calibration_prediction
        bias_correction, proposed_bias_correction, validation_uncorrected, validation_corrected = select_additive_bias_correction(
            validation.actual_value.to_numpy(float), calibration_prediction)
        all_calibration_residuals.append(pd.DataFrame({
            "canonical_market": market, "season": validation.season.to_numpy(),
            "week": validation.week.to_numpy(), "kickoff": validation.kickoff.to_numpy(),
            "residual": calibration_residuals,
            "proposed_corrected_residual": calibration_residuals - proposed_bias_correction,
            "selected_corrected_residual": calibration_residuals - bias_correction,
            "selected_bias_correction": bias_correction, "proposed_bias_correction": proposed_bias_correction,
            "source_split": "validation",
            "fit_split": "train_only",
        }))
        final_train = (train if config.production_bootstrap else
                       pd.concat([train, validation]).sort_values("kickoff"))
        final_model = make_pipeline(selected_family, selected_parameter, config.random_state)
        final_model.fit(final_train[features], final_train.actual_value)
        uncorrected_prediction = final_model.predict(test[features])
        proposed_corrected_prediction = uncorrected_prediction + proposed_bias_correction
        prediction = uncorrected_prediction + bias_correction
        learned_uncorrected = _finite_metrics(test, uncorrected_prediction)
        learned_proposed_corrected = _finite_metrics(test, proposed_corrected_prediction)
        learned = _finite_metrics(test, prediction)
        baseline_metrics = {name: _finite_metrics(test, values) for name, values in baseline_predictions(test).items()}
        # Sequential feature-group ablation, selected estimator and fixed hyperparameters.
        ablation, included = [], []
        previous = None
        for group in FEATURE_GROUPS:
            included.append(group)
            group_features = available_features(market_rows, included)
            candidate = make_pipeline(selected_family, selected_parameter, config.random_state)
            candidate.fit(final_train[group_features], final_train.actual_value)
            score = _finite_metrics(test, candidate.predict(test[group_features]))
            score.update({"added_group": group, "feature_count": len(group_features),
                          "mae_change_vs_previous": None if previous is None else score["mae"] - previous})
            previous = score["mae"]
            ablation.append(score)
        global_uncorrected, global_uncorrected_rows, global_uncorrected_reliability = uncertainty_diagnostics(
            calibration_residuals, test, uncorrected_prediction, config.threshold_offsets, config.calibration_bins)
        conditional_uncorrected, conditional_uncorrected_rows, conditional_uncorrected_reliability = conditional_uncertainty_diagnostics(
            calibration_prediction, calibration_residuals, test, uncorrected_prediction,
            config.threshold_offsets, config.calibration_bins)
        corrected_calibration_residuals = calibration_residuals - proposed_bias_correction
        uncertainty, interval_rows, reliability = uncertainty_diagnostics(
            corrected_calibration_residuals, test, proposed_corrected_prediction, config.threshold_offsets, config.calibration_bins)
        conditional, conditional_rows, conditional_reliability = conditional_uncertainty_diagnostics(
            calibration_prediction + proposed_bias_correction, corrected_calibration_residuals, test, proposed_corrected_prediction,
            config.threshold_offsets, config.calibration_bins)
        global_uncorrected_rows["method"] = "global_uncorrected"
        conditional_uncorrected_rows["method"] = "prediction_conditional_uncorrected"
        interval_rows["method"] = "global_corrected"
        interval_rows = pd.concat([global_uncorrected_rows, conditional_uncorrected_rows, interval_rows, conditional_rows], ignore_index=True)
        prediction_rows = test[[c for c in ("season", "week", "kickoff", "game_id", "player_id", "player_name", "position", "player_position", "actual_value") if c in test]].copy()
        prediction_rows["canonical_market"], prediction_rows["model"], prediction_rows["prediction"] = market, selected_family, prediction
        prediction_rows["uncorrected_prediction"] = uncorrected_prediction
        prediction_rows["proposed_corrected_prediction"] = proposed_corrected_prediction
        prediction_rows["residual"] = prediction_rows.actual_value - prediction
        all_predictions.append(prediction_rows)
        interval_rows["canonical_market"] = market
        global_uncorrected_reliability["method"] = "global_uncorrected"
        conditional_uncorrected_reliability["method"] = "prediction_conditional_uncorrected"
        reliability["method"] = "global_corrected"
        conditional_reliability["method"] = "prediction_conditional_corrected"
        reliability = pd.concat([global_uncorrected_reliability, conditional_uncorrected_reliability,
                                 reliability, conditional_reliability], ignore_index=True)
        reliability["canonical_market"] = market
        all_intervals.append(interval_rows); all_calibration.append(reliability)
        breakdowns = {"season": {str(k): metric_record(g.actual_value, g.prediction) for k, g in prediction_rows.groupby("season")}}
        position_column = next((c for c in ("position", "player_position") if c in prediction_rows), None)
        breakdowns["position"] = ({str(k): metric_record(g.actual_value, g.prediction) for k, g in prediction_rows.groupby(position_column, dropna=False)} if position_column else {"unavailable": learned})
        breakdowns["position_uncorrected"] = ({str(k): metric_record(g.actual_value, g.uncorrected_prediction)
                                                 for k, g in prediction_rows.groupby(position_column, dropna=False)}
                                                if position_column else {"unavailable": learned_uncorrected})
        breakdowns["position_proposed_corrected"] = ({str(k): metric_record(g.actual_value, g.proposed_corrected_prediction)
                                                        for k, g in prediction_rows.groupby(position_column, dropna=False)}
                                                       if position_column else {"unavailable": learned_proposed_corrected})
        core_usage = [c for c in ("pregame_targets", "targets_rolling_3", "pregame_target_share",
                                  "target_share_rolling_3", "pregame_snap_share") if c in validation]
        distribution_shift = {str(season): {"rows": int(len(group)), "actual_mean": float(group.actual_value.mean()),
                                             "actual_median": float(group.actual_value.median())}
                              for season, group in market_rows.groupby("season")}
        summary["markets"][market] = {
            "split": {name: {"seasons": sorted(map(int, part.season.unique())), "rows": len(part),
                              "min_kickoff": part.kickoff.min().isoformat(), "max_kickoff": part.kickoff.max().isoformat()} for name, part in parts.items()},
            "features": features, "feature_groups": {k: [c for c in v if c in features] for k, v in FEATURE_GROUPS.items()},
            "selected_model": {"family": selected_family, "parameter": selected_parameter},
            "validation_search": [{"family": f, "parameter": p, **metrics} for _, f, p, metrics in candidates],
            "baselines": baseline_metrics, "learned_model_uncorrected": learned_uncorrected,
            "learned_model_proposed_corrected": learned_proposed_corrected,
            "learned_model": learned, "ablation": ablation,
            "bias_correction": {"proposed_yards_added": proposed_bias_correction, "selected_yards_added": bias_correction,
                                "selection_rule": "keep only when validation MAE improves",
                                "validation_uncorrected": validation_uncorrected, "validation_corrected": validation_corrected},
            "uncertainty": {"global_uncorrected": global_uncorrected,
                            "prediction_conditional_uncorrected": conditional_uncorrected,
                            "global_corrected": uncertainty,
                            "prediction_conditional_corrected": conditional}, "breakdowns": breakdowns,
            "error_diagnostics": {"absolute_error_p90": float(np.quantile(np.abs(prediction - test.actual_value), .9)),
                                  "residual_std_by_prediction_quartile": {str(k): float(v) for k, v in prediction_rows.assign(q=pd.qcut(prediction, 4, duplicates="drop")).groupby("q", observed=True).residual.std().items()},
                                  "season_distribution": distribution_shift,
                                  "validation_usage_missing_rate": {c: float(validation[c].isna().mean()) for c in core_usage},
                                  "imputation_indicator_count": int(len(final_model.named_steps["imputer"].indicator_.features_))},
            "residual_source": "validation rows predicted by a model fit on training rows only",
            "production_bootstrap": config.production_bootstrap,
        }
        # The correction is selected solely on validation. Its matching residual
        # distribution remains out-of-fit because the estimator saw train only.
        live_calibration_prediction = calibration_prediction + bias_correction
        live_calibration_residuals = calibration_residuals - bias_correction
        live_edges = np.unique(np.quantile(live_calibration_prediction, np.linspace(0, 1, 5)))
        joblib.dump({"pipeline": final_model, "additive_bias_correction": bias_correction,
                     "rejected_or_selected_candidate_correction": proposed_bias_correction,
                     "features": features, "canonical_market": market,
                     "calibration_predictions": live_calibration_prediction,
                     "calibration_residuals": live_calibration_residuals,
                     "prediction_bin_edges": live_edges.tolist(),
                     "uncertainty_method": "prediction_conditional_empirical_residual_ecdf",
                     "uncertainty_version": "1"}, output / f"{market}.joblib")
    pd.concat(all_predictions).to_parquet(output / "predictions.parquet", index=False)
    pd.concat(all_intervals).to_parquet(output / "predictive_intervals.parquet", index=False)
    pd.concat(all_calibration_residuals).to_parquet(output / "calibration_residuals.parquet", index=False)
    pd.concat(all_calibration).to_csv(output / "threshold_calibration.csv", index=False)
    summary["runtime_seconds"] = time.perf_counter() - started
    summary["disclaimer"] = "Diagnostic football-stat distributions only; not validated or suitable for betting decisions."
    (output / "metrics.json").write_text(json.dumps(_json_safe(summary), indent=2, sort_keys=True, allow_nan=False, default=_json_default))
    (output / "split_metadata.json").write_text(json.dumps({m: v["split"] for m, v in summary["markets"].items()}, indent=2, sort_keys=True))
    (output / "feature_lists.json").write_text(json.dumps({m: {"features": v["features"], "groups": v["feature_groups"]}
                                                              for m, v in summary["markets"].items()}, indent=2, sort_keys=True))
    return summary


def _json_default(value: Any):
    if isinstance(value, (np.integer,)): return int(value)
    if isinstance(value, (np.floating,)): return None if not np.isfinite(value) else float(value)
    if isinstance(value, pd.Interval): return str(value)
    raise TypeError(type(value).__name__)


def _finite_or_none(value: float) -> float | None:
    return float(value) if np.isfinite(value) else None


def _json_safe(value: Any) -> Any:
    """Recursively replace non-finite native floats before strict JSON encoding."""
    if isinstance(value, dict):
        return {str(k): _json_safe(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [_json_safe(v) for v in value]
    if isinstance(value, (float, np.floating)) and not np.isfinite(value):
        return None
    return value
