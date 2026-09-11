"""Aggregation of settled paper observations; no wagering decisions."""
from __future__ import annotations
from collections import defaultdict
from math import floor
from statistics import mean

from app.math_utils import american_to_decimal

def maximum_drawdown(profits: list[float], starting_bankroll: float = 100) -> float:
    equity = peak = starting_bankroll
    drawdown = 0.0
    for profit in profits:
        equity += profit; peak = max(peak, equity); drawdown = max(drawdown, peak-equity)
    return drawdown

def probability_bucket(probability: float, width: float = .1) -> str:
    lo = min(floor(probability / width) * width, 1-width)
    return f"{lo:.1f}-{lo+width:.1f}"

def aggregate(rows: list[dict], group_by: tuple[str, ...] = ("canonical_market",),
              minimum_sample: int = 30, starting_bankroll: float = 100) -> list[dict]:
    groups = defaultdict(list)
    for row in rows: groups[tuple(row.get(k) for k in group_by)].append(row)
    output = []
    for key, items in groups.items():
        decided = [r for r in items if r["result"] != "push"]
        profits = [r["fixed_unit_profit_loss"] for r in items]
        calibration = defaultdict(list)
        for r in items:
            calibration[probability_bucket(r["model_probability"])].append(r)
        output.append({**dict(zip(group_by, key)), "sample_size": len(items),
          "small_sample_warning": len(items) < minimum_sample,
          "win_rate": sum(r["result"] == "win" for r in decided)/len(decided) if decided else None,
          "average_offered_odds": mean(r["hard_rock_offered_odds"] for r in items),
          "average_model_edge_pp": mean(r["raw_probability_edge_pp"] for r in items),
          "average_expected_return": mean(r["hypothetical_expected_return_per_dollar"] for r in items),
          "realized_roi": sum(r["profit_loss_per_dollar"] for r in items)/len(items),
          "total_fixed_unit_profit_loss": sum(profits), "maximum_hypothetical_drawdown": maximum_drawdown(profits, starting_bankroll),
          "brier_score": mean((r["model_probability"]-(1 if r["result"] == "win" else 0))**2 for r in decided) if decided else None,
          "calibration": [{"bucket": bucket, "n": len(values),
             "mean_probability": mean(v["model_probability"] for v in values),
             "win_rate": mean(v["result"] == "win" for v in values if v["result"] != "push")
                         if any(v["result"] != "push" for v in values) else None}
             for bucket, values in sorted(calibration.items())]})
    return output
