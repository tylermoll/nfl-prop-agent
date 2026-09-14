"""Pure, read-only shadow-evaluation calculations (never order execution)."""
from __future__ import annotations

from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from typing import Any
from uuid import uuid4

from app.math_utils import american_to_decimal, american_to_probability, hypothetical_return


@dataclass(frozen=True)
class ShadowConfig:
    edge_boundaries_pp: tuple[float, float, float] = (2, 5, 8)
    starting_bankroll: float = 100
    nominal_unit: float = 10
    minimum_report_sample: int = 30


def edge_bucket(edge: float, boundaries: tuple[float, float, float] = (2, 5, 8)) -> str:
    a, b, c = boundaries
    if not 0 <= a <= b <= c:
        raise ValueError("edge boundaries must be ordered and non-negative")
    pp = edge * 100
    return (f"edge_lt_{a:g}pp" if pp < a else f"edge_{a:g}_to_{b:g}pp" if pp < b
            else f"edge_{b:g}_to_{c:g}pp" if pp < c else f"edge_{c:g}pp_plus")


def confirmation_flags(side: str, model_probability: float,
                       reference_over_probability: float | None,
                       kalshi_midpoint: float | None) -> dict[str, bool]:
    """Compare independent market direction to model direction versus 50%."""
    model_over = model_probability >= .5 if side == "over" else model_probability < .5
    ref = None if reference_over_probability is None else (reference_over_probability >= .5) == model_over
    kalshi = None if kalshi_midpoint is None else (kalshi_midpoint >= .5) == model_over
    return {"sportsbook_consensus_agrees": ref is True, "kalshi_agrees": kalshi is True,
            "both_agree": ref is True and kalshi is True,
            "both_disagree": ref is False and kalshi is False,
            "insufficient_external_confirmation": ref is None or kalshi is None}


def price_diagnostics(model_probability: float, odds: int) -> dict[str, float | int]:
    break_even = american_to_probability(odds)
    return {"model_probability": model_probability,
            "offered_price_break_even_probability": break_even,
            "raw_probability_edge_pp": (model_probability-break_even)*100,
            "american_price": odds, "decimal_payout": american_to_decimal(odds),
            "hypothetical_expected_return_per_dollar": hypothetical_return(model_probability, odds)}


def make_observation(*, observed_at_utc: datetime, kickoff_utc: datetime, game_id: str,
                     player_id: str | None, player_name: str, team: str | None,
                     opponent: str | None, canonical_market: str, line: float, side: str,
                     offered_odds: int, model_score: Any, reference_context: dict | None = None,
                     kalshi_context: dict | None = None, source_ids: dict | None = None,
                     freshness: dict | None = None, context: dict | None = None,
                     config: ShadowConfig = ShadowConfig()) -> dict:
    """Create one side-specific immutable pregame record; reject late quotes."""
    observed, kickoff = observed_at_utc.astimezone(timezone.utc), kickoff_utc.astimezone(timezone.utc)
    if observed >= kickoff:
        raise ValueError("post-kickoff observations are not valid pregame candidates")
    probability = model_score.over_probability if side == "over" else model_score.under_probability
    diag = price_diagnostics(probability, offered_odds)
    reference_context, kalshi_context = reference_context or {}, kalshi_context or {}
    flags = confirmation_flags(side, probability,
        reference_context.get("exact_threshold_over_no_vig_probability"), kalshi_context.get("midpoint"))
    return {"observation_id": str(uuid4()), "observed_at_utc": observed, "kickoff_utc": kickoff,
            "game_id": game_id, "player_id": player_id, "player_name": player_name,
            "team": team, "opponent": opponent, "canonical_market": canonical_market,
            "line": line, "side": side, "hard_rock_offered_odds": offered_odds,
            **diag, "model_version": model_score.model_version,
            "point_prediction": model_score.point_prediction,
            "feature_built_at_utc": model_score.feature_built_at_utc,
            "uncertainty_method": model_score.uncertainty_method,
            "uncertainty_version": model_score.uncertainty_version,
            "residual_bucket": model_score.residual_bucket,
            "uncertainty_scale": model_score.uncertainty_scale,
            "edge_bucket": edge_bucket(diag["raw_probability_edge_pp"] / 100, config.edge_boundaries_pp),
            "reference_context": reference_context, "kalshi_context": kalshi_context,
            "source_observation_ids": source_ids or {}, "freshness": freshness or {},
            "context": context or {}, "confirmation_flags": flags,
            "research_config": asdict(config)}


def settle(actual: float, line: float, side: str, odds: int, unit: float = 10) -> dict:
    comparison = 0 if actual == line else (1 if actual > line else -1)
    won = comparison > 0 if side == "over" else comparison < 0
    result = "push" if comparison == 0 else ("win" if won else "loss")
    per_dollar = 0.0 if result == "push" else american_to_decimal(odds)-1 if won else -1.0
    return {"actual_value": actual, "result": result, "profit_loss_per_dollar": per_dollar,
            "fixed_unit": unit, "fixed_unit_profit_loss": per_dollar*unit}


def settle_observations(observations: list[dict], realized_statistics: list[dict],
                        unit: float = 10, settled_at_utc: datetime | None = None) -> list[dict]:
    """Join completed historical rows by exact canonical identity and market."""
    actuals = {(r["game_id"], r.get("player_id"), r["canonical_market"]): r
               for r in realized_statistics}
    settled_at = settled_at_utc or datetime.now(timezone.utc)
    output = []
    for row in observations:
        result = actuals.get((row["game_id"], row.get("player_id"), row["canonical_market"]))
        if result is None or row["observed_at_utc"] >= row["kickoff_utc"]:
            continue
        output.append({"observation_id": row["observation_id"], "settled_at_utc": settled_at,
                       **settle(result["actual_value"], row["line"], row["side"],
                                row["hard_rock_offered_odds"], unit),
                       "line": row["line"], "side": row["side"],
                       "american_odds": row["hard_rock_offered_odds"],
                       "result_source_id": result.get("result_source_id")})
    return output
