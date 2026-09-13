"""Leakage-safe residual probability calibration shared by training and scoring."""
from __future__ import annotations

from dataclasses import dataclass

import numpy as np


METHOD = "shrunk_prediction_conditional_empirical_residual_ecdf"
VERSION = "2"


@dataclass(frozen=True)
class ResidualCalibration:
    """Empirical-Bayes ECDF parameters.

    ``prior_weight`` is a number of market-wide pseudo-observations.  The
    Jeffreys continuity correction prevents impossible zero/one estimates; it
    is deliberately not an arbitrary probability floor.
    """

    bin_count: int = 4
    prior_weight: float = 100.0


def select_residual_calibration(predictions: np.ndarray, residuals: np.ndarray,
                                offsets: tuple[float, ...] = (-1, -.5, 0, .5, 1)) -> tuple[ResidualCalibration, list[dict]]:
    """Select only on the final season's chronological internal holdout.

    The first 60% supplies candidate residual distributions and the last 40%
    supplies outcomes. Half-point thresholds around the prediction are used so
    count-statistic ties have the same semantics as sportsbook half lines.
    """
    predictions, residuals = np.asarray(predictions, float), np.asarray(residuals, float)
    split = max(2, min(len(predictions) - 1, int(.6 * len(predictions))))
    fit_prediction, fit_residual = predictions[:split], residuals[:split]
    test_prediction, test_residual = predictions[split:], residuals[split:]
    scale = max(float(np.std(fit_residual, ddof=1)), 1e-9)
    thresholds = np.asarray([[np.floor(point + offset * scale) + .5 for offset in offsets]
                             for point in test_prediction])
    outcomes = test_prediction[:, None] + test_residual[:, None] > thresholds
    records = []
    for bin_count in (1, 2, 4, 8):
        edges = prediction_bin_edges(fit_prediction, bin_count)
        fit_bins = assign_bins(fit_prediction, edges)
        test_bins = assign_bins(test_prediction, edges)
        market_sorted = np.sort(fit_residual)
        pools = {bucket: np.sort(fit_residual[fit_bins == bucket])
                 for bucket in range(len(edges) - 1)}
        for prior_weight in (0.0, 25.0, 100.0, 400.0):
            probabilities = np.empty_like(thresholds)
            for row_index, (point, row) in enumerate(zip(test_prediction, thresholds)):
                pool = pools[int(test_bins[row_index])]
                cutoffs = row - point
                local = (len(pool) - np.searchsorted(pool, cutoffs, side="right") + .5) / (len(pool) + 1)
                global_probability = (len(market_sorted) - np.searchsorted(
                    market_sorted, cutoffs, side="right") + .5) / (len(market_sorted) + 1)
                weight = len(pool) / (len(pool) + prior_weight)
                probabilities[row_index] = weight * local + (1 - weight) * global_probability
            records.append({"bin_count": bin_count, "prior_weight": prior_weight,
                            "brier_score": float(np.mean((probabilities - outcomes) ** 2)),
                            "mean_probability_error": float(np.mean(probabilities - outcomes)),
                            "extreme_probability_frequency": float(np.mean(
                                (probabilities < .05) | (probabilities > .95))),
                            "evaluation_rows": int(outcomes.size)})
    selected = min(records, key=lambda row: (row["brier_score"], row["bin_count"], row["prior_weight"]))
    return ResidualCalibration(selected["bin_count"], selected["prior_weight"]), records


def prediction_bin_edges(predictions: np.ndarray, bin_count: int) -> np.ndarray:
    values = np.asarray(predictions, float)
    edges = np.unique(np.quantile(values, np.linspace(0, 1, bin_count + 1)))
    if len(edges) < 2:
        return np.array([-np.inf, np.inf])
    edges[0], edges[-1] = -np.inf, np.inf
    return edges


def assign_bins(predictions: np.ndarray, edges: np.ndarray) -> np.ndarray:
    return np.clip(np.searchsorted(edges, predictions, side="right") - 1, 0, len(edges) - 2)


def smoothed_exceedance(pool: np.ndarray, market_pool: np.ndarray, cutoff: float,
                        prior_weight: float) -> float:
    """Return a continuity-corrected bucket ECDF shrunk to the market ECDF."""
    pool, market_pool = np.asarray(pool, float), np.asarray(market_pool, float)
    if not len(pool) or not len(market_pool):
        raise ValueError("residual calibration pools must not be empty")
    local = (np.count_nonzero(pool > cutoff) + .5) / (len(pool) + 1.0)
    global_probability = (np.count_nonzero(market_pool > cutoff) + .5) / (len(market_pool) + 1.0)
    weight = len(pool) / (len(pool) + float(prior_weight))
    return float(weight * local + (1.0 - weight) * global_probability)


def residual_probability(prediction: float, threshold: float, calibration_predictions: np.ndarray,
                         calibration_residuals: np.ndarray, edges: np.ndarray,
                         prior_weight: float) -> tuple[float, int, np.ndarray]:
    bins = assign_bins(np.asarray(calibration_predictions, float), edges)
    bucket = int(assign_bins(np.asarray([prediction], float), edges)[0])
    residuals = np.asarray(calibration_residuals, float)
    pool = residuals[bins == bucket]
    probability = smoothed_exceedance(pool, residuals, threshold - prediction, prior_weight)
    return probability, bucket, pool
