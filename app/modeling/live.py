"""Live inference from football-only benchmark artifacts.

Thresholds enter only after the point prediction.  They are evaluated against
validation residuals and are never features supplied to the estimator.
"""
from __future__ import annotations

import hashlib
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

import joblib
import numpy as np
import pandas as pd


@dataclass(frozen=True)
class FootballScore:
    point_prediction: float
    over_probability: float
    under_probability: float
    model_version: str
    feature_built_at_utc: datetime
    uncertainty_method: str
    uncertainty_version: str
    residual_bucket: int
    uncertainty_scale: float


class FootballArtifactScorer:
    """Score leakage-safe rows using prediction-conditional empirical errors."""
    METHOD = "prediction_conditional_empirical_residual_ecdf"
    VERSION = "1"

    def __init__(self, artifact_path: str | Path):
        self.path = Path(artifact_path)
        self.artifact = joblib.load(self.path)
        required = {"pipeline", "features", "calibration_predictions", "calibration_residuals"}
        if missing := required - self.artifact.keys():
            raise ValueError(f"artifact lacks live uncertainty provenance: {sorted(missing)}")
        self.version = self.artifact.get("artifact_id") or hashlib.sha256(self.path.read_bytes()).hexdigest()

    def score(self, features: pd.DataFrame, threshold: float,
              feature_built_at_utc: datetime) -> FootballScore:
        if feature_built_at_utc.tzinfo is None:
            raise ValueError("feature build timestamp must be timezone-aware")
        if len(features) != 1:
            raise ValueError("live scoring requires exactly one feature row")
        point = (float(self.artifact["pipeline"].predict(features[self.artifact["features"]])[0]) +
                 float(self.artifact.get("additive_bias_correction", 0.0)))
        predictions = np.asarray(self.artifact["calibration_predictions"], float)
        residuals = np.asarray(self.artifact["calibration_residuals"], float)
        edges = np.asarray(self.artifact.get("prediction_bin_edges") or
                           np.quantile(predictions, np.linspace(0, 1, 5)), float)
        edges[0], edges[-1] = -np.inf, np.inf
        buckets = np.clip(np.searchsorted(edges, predictions, side="right") - 1, 0, len(edges)-2)
        bucket = int(np.clip(np.searchsorted(edges, point, side="right") - 1, 0, len(edges)-2))
        pool = residuals[buckets == bucket]
        if not len(pool):
            raise ValueError("selected residual bucket is empty")
        # Strict exceedance correctly leaves integer-threshold ties outside Over.
        over = float(np.mean(pool > float(threshold) - point))
        return FootballScore(point, over, 1-over, self.version,
                             feature_built_at_utc.astimezone(timezone.utc), self.METHOD,
                             self.VERSION, bucket, float(np.std(pool, ddof=1)) if len(pool) > 1 else 0.0)
