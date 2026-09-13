"""Read-only diagnostics for production prop probabilities.

This command intentionally imports no scheduler or provider code and performs no
writes.  It can audit joblib artifacts alone, or combine them with the immutable
observation table when ``--database-url`` is supplied.
"""
from __future__ import annotations

import argparse
import json
from collections import defaultdict
from pathlib import Path
from typing import Any

import joblib
import numpy as np
from sqlalchemy import create_engine, select

from app.math_utils import american_to_probability
from app.modeling.calibration import smoothed_exceedance
from app.shadow_storage import normalize_database_url, observations


EDGE_THRESHOLDS = (5, 8, 10, 15, 20, 30)
DISAGREEMENT_THRESHOLDS = (10, 20, 30, 40)


def _number(value: Any) -> float | None:
    return float(value) if isinstance(value, (int, float)) else None


def _summary(values: list[float] | np.ndarray) -> dict[str, Any]:
    a = np.asarray(values, dtype=float)
    if not len(a):
        return {"count": 0}
    return {"count": int(len(a)), "min": float(a.min()),
            "q05": float(np.quantile(a, .05)), "q25": float(np.quantile(a, .25)),
            "median": float(np.median(a)), "mean": float(a.mean()),
            "q75": float(np.quantile(a, .75)), "q95": float(np.quantile(a, .95)),
            "max": float(a.max())}


def artifact_audit(path: str | Path) -> dict[str, Any]:
    artifact = joblib.load(path)
    predictions = np.asarray(artifact["calibration_predictions"], float)
    residuals = np.asarray(artifact["calibration_residuals"], float)
    raw_edges = artifact.get("prediction_bin_edges")
    edges = np.asarray(raw_edges if raw_edges is not None else
                       np.quantile(predictions, np.linspace(0, 1, 5)), float)
    edges[0], edges[-1] = -np.inf, np.inf
    assigned = np.clip(np.searchsorted(edges, predictions, side="right") - 1,
                       0, len(edges) - 2)
    buckets = []
    for bucket in range(len(edges) - 1):
        selected_predictions = predictions[assigned == bucket]
        pool = residuals[assigned == bucket]
        buckets.append({
            "bucket": bucket, "lower_inclusive": None if np.isneginf(edges[bucket]) else float(edges[bucket]),
            "upper_exclusive": None if np.isposinf(edges[bucket + 1]) else float(edges[bucket + 1]),
            "prediction_summary": _summary(selected_predictions), "residual_summary": _summary(pool),
            "ecdf_probability_step_pp": 100 / len(pool) if len(pool) else None,
            "residuals_sorted": [float(x) for x in np.sort(pool)],
        })
    return {"artifact": str(path), "artifact_id": artifact.get("artifact_id"),
            "uncertainty_method": artifact.get("uncertainty_method"),
            "uncertainty_version": artifact.get("uncertainty_version"),
            "probability_calibration": artifact.get("probability_calibration"),
            "calibration_sample_size": int(len(residuals)), "prediction_bin_edges": [
                None if not np.isfinite(x) else float(x) for x in edges], "buckets": buckets}


def probability_trace(artifact_report: dict[str, Any], point: float, line: float) -> dict[str, Any]:
    """Reproduce live.py exactly: residual=actual-prediction and strict Over tail."""
    edges = np.asarray([-np.inf if x is None and i == 0 else
                        np.inf if x is None else x
                        for i, x in enumerate(artifact_report["prediction_bin_edges"])], float)
    bucket = int(np.clip(np.searchsorted(edges, point, side="right") - 1, 0, len(edges) - 2))
    pool = np.asarray(artifact_report["buckets"][bucket]["residuals_sorted"], float)
    cutoff = line - point
    exceed = pool[pool > cutoff]
    raw_over = len(exceed) / len(pool)
    calibration = artifact_report.get("probability_calibration")
    market_pool = np.concatenate([np.asarray(x["residuals_sorted"], float)
                                  for x in artifact_report["buckets"]])
    over = (smoothed_exceedance(pool, market_pool, cutoff, calibration["prior_weight"])
            if calibration else raw_over)
    return {"point_prediction": point, "line": line, "residual_cutoff": cutoff,
            "residual_bucket": bucket, "effective_calibration_n": int(len(pool)),
            "over_tail_condition": "residual > line - point", "over_tail_residuals": exceed.tolist(),
            "over_tail_count": int(len(exceed)), "raw_bucket_over_probability": raw_over,
            "over_probability": over,
            "under_probability": 1 - over,
            "under_definition": "complement of strict Over (therefore actual <= line)"}


def _group_report(rows: list[dict], keys: tuple[str, ...]) -> dict[str, Any]:
    groups: dict[tuple, list[dict]] = defaultdict(list)
    for row in rows:
        groups[tuple(row.get(k) for k in keys)].append(row)
    output = {}
    for group, items in sorted(groups.items(), key=lambda x: str(x[0])):
        probabilities = [float(x["model_probability"]) for x in items]
        edges = [float(x["raw_probability_edge_pp"]) for x in items]
        output[" | ".join(str(x) for x in group)] = {
            "count": len(items), "model_probability": _summary(probabilities),
            "probability_edge_pp": _summary(edges),
            "edge_counts": {str(t): {"absolute": sum(abs(x) > t for x in edges),
                                      "model_positive": sum(x > t for x in edges)}
                            for t in EDGE_THRESHOLDS}}
    return output


def observation_audit(database_url: str, artifact_reports: dict[str, Any] | None = None) -> dict[str, Any]:
    engine = create_engine(normalize_database_url(database_url))
    with engine.connect() as connection:
        rows = [dict(x._mapping) for x in connection.execute(select(observations))]
    for row in rows:
        row["capture_window"] = (row.get("context") or {}).get("capture_slot", "unknown")
    disagreements = []
    rescored = []
    invariant_failures = {"break_even_from_price": 0, "edge_uses_offered_side": 0}
    for row in rows:
        expected_break_even = american_to_probability(int(row["hard_rock_offered_odds"]))
        if not np.isclose(row["offered_price_break_even_probability"], expected_break_even):
            invariant_failures["break_even_from_price"] += 1
        expected_edge = 100 * (row["model_probability"] - expected_break_even)
        if not np.isclose(row["raw_probability_edge_pp"], expected_edge):
            invariant_failures["edge_uses_offered_side"] += 1
        report = (artifact_reports or {}).get(row["canonical_market"])
        if report and row.get("point_prediction") is not None:
            trace = probability_trace(report, float(row["point_prediction"]), float(row["line"]))
            probability = trace["over_probability"] if row["side"] == "over" else trace["under_probability"]
            new_edge = 100 * (probability - expected_break_even)
            rescored.append({"old": float(row["raw_probability_edge_pp"]), "new": new_edge,
                             "cutoff": float(row["line"] - row["point_prediction"]),
                             "market": row["canonical_market"], "side": row["side"]})
        reference_over = _number((row.get("reference_context") or {}).get(
            "exact_threshold_over_no_vig_probability"))
        if reference_over is None:
            continue
        # Stored model_probability is offered-side probability; reference is always Over.
        reference_side = reference_over if row["side"] == "over" else 1 - reference_over
        disagreements.append(100 * (float(row["model_probability"]) - reference_side))
    dimensions = {"overall": (), "by_market": ("canonical_market",), "by_side": ("side",),
                  "by_residual_bucket": ("residual_bucket",), "by_capture_window": ("capture_window",),
                  "by_market_side_bucket_window": (
                      "canonical_market", "side", "residual_bucket", "capture_window")}
    edge_comparison = {str(t): {"old": sum(abs(x["old"]) > t for x in rescored),
                                "new": sum(abs(x["new"]) > t for x in rescored)}
                       for t in (10, 20, 30)}
    return {"observation_count": len(rows), "matchup_count": len({r["game_id"] for r in rows}),
            "read_only_rescore": {"count": len(rescored), "absolute_edge_counts": edge_comparison,
                                   "threshold_minus_prediction": _summary([x["cutoff"] for x in rescored])},
            "mathematical_invariant_failure_counts": invariant_failures,
            "distributions": {name: _group_report(rows, keys) for name, keys in dimensions.items()},
            "model_vs_same_threshold_reference_pp": {
                **_summary(disagreements),
                "absolute_difference_counts": {str(t): sum(abs(x) > t for x in disagreements)
                                               for t in DISAGREEMENT_THRESHOLDS},
                "available_count": len(disagreements)}}


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--artifact", action="append", default=[], metavar="MARKET=PATH")
    parser.add_argument("--database-url")
    parser.add_argument("--trace", nargs=3, metavar=("MARKET", "POINT", "LINE"))
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()
    reports = {market: artifact_audit(path) for market, path in
               (value.split("=", 1) for value in args.artifact)}
    result: dict[str, Any] = {"artifacts": reports}
    if args.database_url:
        result["production_observations"] = observation_audit(args.database_url, reports)
    if args.trace:
        market, point, line = args.trace
        result["trace"] = probability_trace(reports[market], float(point), float(line))
    rendered = json.dumps(result, indent=2, sort_keys=True)
    if args.output:
        args.output.write_text(rendered + "\n")
    else:
        print(rendered)


if __name__ == "__main__":
    main()
