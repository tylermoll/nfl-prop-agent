"""Pure prospective shadow-selection contracts and integrity policy.

This module deliberately has no storage, settlement, provider, account, order,
or wagering dependency.  It evaluates only immutable pregame observations.
"""
from __future__ import annotations

import hashlib
import json
import math
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from enum import Enum
from typing import Any, Iterable
from uuid import UUID


INPUT_SCHEMA_VERSION = "shadow-selection-input-v2"
OPPORTUNITY_ID_VERSION = "shadow-opportunity-v1"
EXPOSURE_ID_VERSION = "shadow-exposure-v1"
PROBABILITY_TOLERANCE = 1e-9
EDGE_TOLERANCE_PP = 1e-9
FROZEN_POLICY_NAME = "nfl_prop_v2"
FROZEN_POLICY_VERSION = "v2.0"
FROZEN_ELIGIBLE_WINDOW = "90m"
PROVIDER_MAX_AGE_SECONDS = 300.0
FEATURE_MAX_AGE_SECONDS = 3600.0
MINIMUM_REFERENCE_BOOKS = 2
UNCERTAINTY_METHOD = "shrunk_prediction_conditional_empirical_residual_ecdf"
UNCERTAINTY_VERSION = "2"
MODEL_VERSION_ALLOWLIST = {
    "player_pass_yds": "ddd3214caf4af113ed0b0409a87d10464aded7702b3fa0cd182b76b3a3a09cff",
    "player_reception_yds": "c9142a38dd44a12025a13ae71d37c843b2690fb5dd45529c381a300a253b2d87",
    "player_receptions": "fb64013cec878eb2cec6153d494d93e2842412ae0bfdcd4dfeceba6b6d94ae67",
}


class DecisionAction(str, Enum):
    SELECT_OVER = "SELECT_OVER"
    SELECT_UNDER = "SELECT_UNDER"
    PASS = "PASS"


PASS_POST_KICKOFF = "PASS_POST_KICKOFF"
PASS_MISSING_SIDE = "PASS_MISSING_SIDE"
PASS_DUPLICATE_SIDE = "PASS_DUPLICATE_SIDE"
PASS_SIDE_PAIR_MISMATCH = "PASS_SIDE_PAIR_MISMATCH"
PASS_PLAYER_IDENTITY_MISSING = "PASS_PLAYER_IDENTITY_MISSING"
PASS_LINE_MISMATCH = "PASS_LINE_MISMATCH"
PASS_CAPTURE_IDENTITY_MISMATCH = "PASS_CAPTURE_IDENTITY_MISMATCH"
PASS_INVALID_MODEL_PROBABILITY = "PASS_INVALID_MODEL_PROBABILITY"
PASS_MODEL_PROBABILITIES_NOT_COMPLEMENTARY = "PASS_MODEL_PROBABILITIES_NOT_COMPLEMENTARY"
PASS_WINDOW_NOT_ELIGIBLE = "PASS_WINDOW_NOT_ELIGIBLE"
PASS_EXPOSURE_ALREADY_SELECTED = "PASS_EXPOSURE_ALREADY_SELECTED"
PASS_DUPLICATE_POLICY_EVALUATION = "PASS_DUPLICATE_POLICY_EVALUATION"
PASS_AMBIGUOUS_BOTH_SIDES = "PASS_AMBIGUOUS_BOTH_SIDES"
PASS_NO_POSITIVE_OFFERED_PRICE_EDGE = "PASS_NO_POSITIVE_OFFERED_PRICE_EDGE"
PASS_UNSUPPORTED_MARKET = "PASS_UNSUPPORTED_MARKET"
PASS_MODEL_VERSION_NOT_ALLOWED = "PASS_MODEL_VERSION_NOT_ALLOWED"
PASS_PROVIDER_TIMESTAMP_MISSING = "PASS_PROVIDER_TIMESTAMP_MISSING"
PASS_PROVIDER_STALE = "PASS_PROVIDER_STALE"
PASS_FEATURE_TIMESTAMP_MISSING = "PASS_FEATURE_TIMESTAMP_MISSING"
PASS_FEATURE_STALE = "PASS_FEATURE_STALE"
PASS_FUTURE_FEATURE_TIMESTAMP = "PASS_FUTURE_FEATURE_TIMESTAMP"
PASS_UNCERTAINTY_CONTRACT_INVALID = "PASS_UNCERTAINTY_CONTRACT_INVALID"
PASS_EDGE_RECOMPUTATION_MISMATCH = "PASS_EDGE_RECOMPUTATION_MISMATCH"
PASS_INSUFFICIENT_REFERENCE_COVERAGE = "PASS_INSUFFICIENT_REFERENCE_COVERAGE"
PASS_REFERENCE_PROBABILITY_INVALID = "PASS_REFERENCE_PROBABILITY_INVALID"
PASS_REFERENCE_PRICE_VALUE_NOT_POSITIVE = "PASS_REFERENCE_PRICE_VALUE_NOT_POSITIVE"
SELECT_POLICY_ELIGIBLE = "SELECT_POLICY_ELIGIBLE"
SELECT_POSITIVE_OFFERED_PRICE_EDGE = "SELECT_POSITIVE_OFFERED_PRICE_EDGE"
SELECT_CAPTURE_WINDOW_ELIGIBLE = "SELECT_CAPTURE_WINDOW_ELIGIBLE"
SELECT_FIRST_EXPOSURE_DECISION = "SELECT_FIRST_EXPOSURE_DECISION"
SELECT_FIXED_UNIT_10 = "SELECT_FIXED_UNIT_10"
SELECT_REFERENCE_PRICE_VALUE_POSITIVE = "SELECT_REFERENCE_PRICE_VALUE_POSITIVE"

REASON_CODES = frozenset({
    PASS_POST_KICKOFF, PASS_MISSING_SIDE, PASS_DUPLICATE_SIDE,
    PASS_SIDE_PAIR_MISMATCH, PASS_PLAYER_IDENTITY_MISSING, PASS_LINE_MISMATCH,
    PASS_CAPTURE_IDENTITY_MISMATCH, PASS_INVALID_MODEL_PROBABILITY,
    PASS_MODEL_PROBABILITIES_NOT_COMPLEMENTARY, PASS_WINDOW_NOT_ELIGIBLE,
    PASS_EXPOSURE_ALREADY_SELECTED, PASS_DUPLICATE_POLICY_EVALUATION,
    PASS_AMBIGUOUS_BOTH_SIDES, PASS_NO_POSITIVE_OFFERED_PRICE_EDGE,
    PASS_UNSUPPORTED_MARKET, PASS_MODEL_VERSION_NOT_ALLOWED,
    PASS_PROVIDER_TIMESTAMP_MISSING, PASS_PROVIDER_STALE,
    PASS_FEATURE_TIMESTAMP_MISSING, PASS_FEATURE_STALE, PASS_FUTURE_FEATURE_TIMESTAMP,
    PASS_UNCERTAINTY_CONTRACT_INVALID, PASS_EDGE_RECOMPUTATION_MISMATCH,
    PASS_INSUFFICIENT_REFERENCE_COVERAGE, PASS_REFERENCE_PROBABILITY_INVALID,
    PASS_REFERENCE_PRICE_VALUE_NOT_POSITIVE,
    SELECT_POLICY_ELIGIBLE, SELECT_POSITIVE_OFFERED_PRICE_EDGE,
    SELECT_CAPTURE_WINDOW_ELIGIBLE, SELECT_FIRST_EXPOSURE_DECISION,
    SELECT_FIXED_UNIT_10, SELECT_REFERENCE_PRICE_VALUE_POSITIVE,
})


def _utc(value: datetime, name: str) -> datetime:
    if not isinstance(value, datetime) or value.tzinfo is None or value.utcoffset() is None:
        raise ValueError(f"{name} must be timezone-aware")
    return value.astimezone(timezone.utc)


def _probability(value: Any, name: str, *, optional: bool = False) -> float | None:
    if value is None and optional:
        return None
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(float(value)):
        raise ValueError(f"{name} must be a finite probability")
    result = float(value)
    if not 0 <= result <= 1:
        raise ValueError(f"{name} must be between zero and one")
    return result


def _datetime(value: Any) -> datetime | None:
    if isinstance(value, str):
        try:
            value = datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError:
            return None
    if not isinstance(value, datetime) or value.tzinfo is None or value.utcoffset() is None:
        return None
    return value.astimezone(timezone.utc)


def _american_break_even(odds: int) -> float:
    if not isinstance(odds, int) or isinstance(odds, bool) or odds == 0:
        raise ValueError("Hard Rock American odds must be a nonzero integer")
    return abs(odds) / (abs(odds) + 100) if odds < 0 else 100 / (odds + 100)


def _finite_number(value: Any) -> float | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    result = float(value)
    return result if math.isfinite(result) else None


@dataclass(frozen=True)
class CanonicalProbabilities:
    """Explicit direction-safe probability view for one offered side."""

    model_over_probability: float
    model_under_probability: float
    model_side_probability: float
    reference_over_probability: float | None
    reference_under_probability: float | None
    reference_side_probability: float | None
    kalshi_yes_probability: float | None
    kalshi_no_probability: float | None
    kalshi_side_probability: float | None
    kalshi_basis: str | None


def canonical_probabilities(observation: dict[str, Any]) -> CanonicalProbabilities:
    """Adapt persisted semantics without renaming or changing historical fields.

    ``model_probability`` is offered-side. Reference is canonical OVER and
    Kalshi midpoint is canonical YES (threshold-reaching/OVER).
    """
    side = observation.get("side")
    if side not in {"over", "under"}:
        raise ValueError("side must be over or under")
    model_side = _probability(observation.get("model_probability"), "model_probability")
    model_over = model_side if side == "over" else 1 - model_side
    model_under = 1 - model_over
    reference_over = _probability((observation.get("reference_context") or {}).get(
        "exact_threshold_over_no_vig_probability"), "reference_over_probability", optional=True)
    reference_under = None if reference_over is None else 1 - reference_over
    reference_side = reference_over if side == "over" else reference_under
    kalshi = observation.get("kalshi_context") or {}
    kalshi_yes = _probability(kalshi.get("midpoint"), "kalshi_yes_probability", optional=True)
    kalshi_no = None if kalshi_yes is None else 1 - kalshi_yes
    kalshi_side = kalshi_yes if side == "over" else kalshi_no
    return CanonicalProbabilities(
        model_over, model_under, model_side, reference_over, reference_under,
        reference_side, kalshi_yes, kalshi_no, kalshi_side,
        "yes_midpoint" if kalshi_yes is not None else None)


@dataclass(frozen=True)
class SideCandidate:
    observation_id: str
    side: str
    offered_odds: int
    raw_probability_edge_pp: float
    probabilities: CanonicalProbabilities
    raw_observation: dict[str, Any]

    @classmethod
    def from_observation(cls, row: dict[str, Any]) -> "SideCandidate":
        return cls(str(row["observation_id"]), str(row.get("side")),
                   int(row["hard_rock_offered_odds"]), float(row["raw_probability_edge_pp"]),
                   canonical_probabilities(row), dict(row))


@dataclass(frozen=True)
class SelectionPolicyConfig:
    policy_name: str
    policy_version: str
    eligible_capture_window: str
    nominal_unit: float = 10.0
    input_schema_version: str = INPUT_SCHEMA_VERSION

    def __post_init__(self) -> None:
        if not self.policy_name or not self.policy_version or not self.eligible_capture_window:
            raise ValueError("policy name, version, and one eligible capture window are required")
        if self.nominal_unit != 10.0:
            raise ValueError("frozen V2.0 requires the fixed $10 nominal unit")
        if (self.policy_name, self.policy_version, self.eligible_capture_window) != (
                FROZEN_POLICY_NAME, FROZEN_POLICY_VERSION, FROZEN_ELIGIBLE_WINDOW):
            raise ValueError("frozen V2.0 requires nfl_prop_v2/v2.0 with the literal 90m eligible window")
        if self.input_schema_version != INPUT_SCHEMA_VERSION:
            raise ValueError(f"frozen V2.0 requires {INPUT_SCHEMA_VERSION}")


@dataclass(frozen=True)
class SelectionOpportunity:
    opportunity_id: str
    exposure_id: str
    kickoff_utc: datetime
    capture_slot: str
    capture_target_time_utc: str
    over: SideCandidate
    under: SideCandidate
    input_digest: str


@dataclass(frozen=True)
class SelectionDecision:
    decision_id: str
    opportunity_id: str
    exposure_id: str
    over_observation_id: str
    under_observation_id: str
    selected_observation_id: str | None
    action: DecisionAction
    intended_stake: float | None
    decided_at_utc: datetime
    reason_codes: tuple[str, ...]
    policy_name: str
    policy_version: str
    input_schema_version: str
    input_digest: str

    def __post_init__(self) -> None:
        _utc(self.decided_at_utc, "decided_at_utc")
        if tuple(sorted(set(self.reason_codes))) != self.reason_codes:
            raise ValueError("reason codes must be unique and deterministically sorted")
        if set(self.reason_codes) - REASON_CODES:
            raise ValueError("unknown selection reason code")
        if self.action == DecisionAction.PASS:
            if self.selected_observation_id is not None or self.intended_stake is not None:
                raise ValueError("PASS cannot reference an observation or stake")
        else:
            expected = self.over_observation_id if self.action == DecisionAction.SELECT_OVER else self.under_observation_id
            if self.selected_observation_id != expected or self.intended_stake != 10.0:
                raise ValueError("SELECT must reference its matching side and fixed $10 unit")

    def as_row(self) -> dict[str, Any]:
        row = asdict(self)
        row["action"] = self.action.value
        row["reason_codes"] = list(self.reason_codes)
        return row


@dataclass(frozen=True)
class PairingFailure:
    reason_codes: tuple[str, ...]


def _stable_digest(namespace: str, payload: dict[str, Any]) -> str:
    encoded = json.dumps({"namespace": namespace, **payload}, sort_keys=True,
                         separators=(",", ":"), default=str).encode()
    return hashlib.sha256(encoded).hexdigest()


def _identity(row: dict[str, Any]) -> dict[str, Any]:
    context = row.get("context") or {}
    return {"game_id": row.get("game_id"), "player_id": row.get("player_id"),
            "canonical_market": row.get("canonical_market"), "line": row.get("line"),
            "kickoff_utc": _utc(row["kickoff_utc"], "kickoff_utc").isoformat(),
            "capture_slot": context.get("capture_slot"),
            "capture_target_time_utc": context.get("capture_target_time_utc"),
            "source_id": (row.get("source_observation_ids") or {}).get("hardrock")}


def build_opportunity(rows: Iterable[dict[str, Any]], config: SelectionPolicyConfig) -> SelectionOpportunity | PairingFailure:
    items = list(rows)
    reasons: set[str] = set()
    by_side = {side: [row for row in items if row.get("side") == side] for side in ("over", "under")}
    if any(len(values) == 0 for values in by_side.values()): reasons.add(PASS_MISSING_SIDE)
    if any(len(values) > 1 for values in by_side.values()): reasons.add(PASS_DUPLICATE_SIDE)
    if reasons:
        return PairingFailure(tuple(sorted(reasons)))
    over_row, under_row = by_side["over"][0], by_side["under"][0]
    if not over_row.get("player_id") or not under_row.get("player_id"):
        reasons.add(PASS_PLAYER_IDENTITY_MISSING)
    over_identity, under_identity = _identity(over_row), _identity(under_row)
    if over_identity["line"] != under_identity["line"]: reasons.add(PASS_LINE_MISMATCH)
    for key in ("capture_slot", "capture_target_time_utc"):
        if not over_identity[key] or over_identity[key] != under_identity[key]:
            reasons.add(PASS_CAPTURE_IDENTITY_MISMATCH)
    for key in ("game_id", "player_id", "canonical_market", "kickoff_utc", "source_id"):
        if not over_identity[key] or over_identity[key] != under_identity[key]:
            reasons.add(PASS_SIDE_PAIR_MISMATCH)
    if over_row.get("observation_id") == under_row.get("observation_id"):
        reasons.add(PASS_SIDE_PAIR_MISMATCH)
    kickoff = _utc(over_row["kickoff_utc"], "kickoff_utc")
    if any(_utc(row["observed_at_utc"], "observed_at_utc") >= kickoff for row in (over_row, under_row)):
        reasons.add(PASS_POST_KICKOFF)
    try:
        over, under = SideCandidate.from_observation(over_row), SideCandidate.from_observation(under_row)
    except (KeyError, TypeError, ValueError):
        reasons.add(PASS_INVALID_MODEL_PROBABILITY)
        over = under = None
    if over and under and not math.isclose(over.probabilities.model_over_probability,
                                           under.probabilities.model_over_probability,
                                           abs_tol=PROBABILITY_TOLERANCE):
        reasons.add(PASS_MODEL_PROBABILITIES_NOT_COMPLEMENTARY)
    if reasons:
        return PairingFailure(tuple(sorted(reasons)))
    # Observation UUIDs are deliberately excluded: a retry reconstructs them.
    # Capture target plus provider source identity is the stable source batch.
    opportunity_payload = dict(over_identity)
    opportunity_id = f"{OPPORTUNITY_ID_VERSION}:{_stable_digest(OPPORTUNITY_ID_VERSION, opportunity_payload)}"
    exposure_payload = {"game_id": over_identity["game_id"], "player_id": over_identity["player_id"],
                        "canonical_market": over_identity["canonical_market"],
                        "policy_name": config.policy_name, "policy_version": config.policy_version}
    exposure_id = f"{EXPOSURE_ID_VERSION}:{_stable_digest(EXPOSURE_ID_VERSION, exposure_payload)}"
    input_digest = _stable_digest(config.input_schema_version, {
        "opportunity": opportunity_payload,
        "over": asdict(over.probabilities), "under": asdict(under.probabilities),
        "over_odds": over.offered_odds, "under_odds": under.offered_odds,
        "over_edge": over.raw_probability_edge_pp, "under_edge": under.raw_probability_edge_pp,
        "decision_inputs": [{key: candidate.raw_observation.get(key) for key in (
            "offered_price_break_even_probability", "model_version", "feature_built_at_utc",
            "uncertainty_method", "uncertainty_version", "residual_bucket", "uncertainty_scale",
            "reference_context", "freshness")}
            for candidate in (over, under)],
        "policy": asdict(config)})
    return SelectionOpportunity(opportunity_id, exposure_id, kickoff,
        over_identity["capture_slot"], over_identity["capture_target_time_utc"], over, under, input_digest)


def opportunity_group_key(row: dict[str, Any]) -> tuple[Any, ...]:
    """Group exact snapshot sides; malformed singletons subsequently fail closed."""
    identity = _identity(row)
    return tuple(identity[key] for key in ("game_id", "player_id", "canonical_market", "line",
        "kickoff_utc", "capture_slot", "capture_target_time_utc", "source_id"))


def _candidate_gate_reasons(candidate: SideCandidate, decided_at: datetime) -> set[str]:
    """Validate frozen V2 inputs without consulting outcomes or external state."""
    row = candidate.raw_observation
    reasons: set[str] = set()
    market = row.get("canonical_market")
    if market not in MODEL_VERSION_ALLOWLIST:
        reasons.add(PASS_UNSUPPORTED_MARKET)
    elif row.get("model_version") != MODEL_VERSION_ALLOWLIST[market]:
        reasons.add(PASS_MODEL_VERSION_NOT_ALLOWED)

    freshness = row.get("freshness") or {}
    provider_timestamp = _datetime(freshness.get("provider_updated_at_utc")) or _datetime(
        freshness.get("provider_observed_at_utc"))
    if provider_timestamp is None:
        reasons.add(PASS_PROVIDER_TIMESTAMP_MISSING)
    else:
        provider_age = (decided_at - provider_timestamp).total_seconds()
        if provider_age < 0 or provider_age > PROVIDER_MAX_AGE_SECONDS:
            reasons.add(PASS_PROVIDER_STALE)

    feature_timestamp = _datetime(row.get("feature_built_at_utc"))
    if feature_timestamp is None:
        reasons.add(PASS_FEATURE_TIMESTAMP_MISSING)
    elif feature_timestamp > decided_at:
        reasons.add(PASS_FUTURE_FEATURE_TIMESTAMP)
    elif (decided_at - feature_timestamp).total_seconds() > FEATURE_MAX_AGE_SECONDS:
        reasons.add(PASS_FEATURE_STALE)

    scale = _finite_number(row.get("uncertainty_scale"))
    bucket = row.get("residual_bucket")
    if (row.get("uncertainty_method") != UNCERTAINTY_METHOD or
            str(row.get("uncertainty_version")) != UNCERTAINTY_VERSION or
            isinstance(bucket, bool) or not isinstance(bucket, int) or bucket < 0 or
            scale is None or scale < 0):
        reasons.add(PASS_UNCERTAINTY_CONTRACT_INVALID)

    try:
        break_even = _american_break_even(candidate.offered_odds)
        persisted_break_even = _finite_number(row.get("offered_price_break_even_probability"))
        recomputed_edge = (candidate.probabilities.model_side_probability - break_even) * 100
        if (persisted_break_even is None or
                not math.isclose(persisted_break_even, break_even, abs_tol=PROBABILITY_TOLERANCE) or
                not math.isclose(candidate.raw_probability_edge_pp, recomputed_edge,
                                 abs_tol=EDGE_TOLERANCE_PP)):
            reasons.add(PASS_EDGE_RECOMPUTATION_MISMATCH)
    except ValueError:
        reasons.add(PASS_EDGE_RECOMPUTATION_MISMATCH)

    reference = row.get("reference_context") or {}
    books = reference.get("books")
    qualifying: list[float] = []
    if isinstance(books, dict):
        for quote in books.values():
            if not isinstance(quote, dict):
                continue
            probability = _finite_number(quote.get("exact_threshold_over_no_vig_probability"))
            if (isinstance(quote.get("over_odds"), int) and not isinstance(quote.get("over_odds"), bool) and
                    isinstance(quote.get("under_odds"), int) and not isinstance(quote.get("under_odds"), bool) and
                    probability is not None and 0 <= probability <= 1):
                qualifying.append(probability)
    if len(qualifying) < MINIMUM_REFERENCE_BOOKS:
        reasons.add(PASS_INSUFFICIENT_REFERENCE_COVERAGE)
    reference_over = candidate.probabilities.reference_over_probability
    if reference_over is None or not qualifying:
        reasons.add(PASS_REFERENCE_PROBABILITY_INVALID)
    else:
        ordered = sorted(qualifying)
        middle = len(ordered) // 2
        expected = ordered[middle] if len(ordered) % 2 else (ordered[middle - 1] + ordered[middle]) / 2
        if not math.isclose(reference_over, expected, abs_tol=PROBABILITY_TOLERANCE):
            reasons.add(PASS_REFERENCE_PROBABILITY_INVALID)
    return reasons


def decide_capture(rows: Iterable[dict[str, Any]], config: SelectionPolicyConfig, *,
                   decided_at_utc: datetime,
                   exposure_is_selected: Any = lambda *_: False) -> tuple[list[SelectionDecision], list[PairingFailure]]:
    """Evaluate every exact two-sided opportunity in a capture batch."""
    groups: dict[tuple[Any, ...], list[dict[str, Any]]] = {}
    failures: list[PairingFailure] = []
    for row in rows:
        try:
            groups.setdefault(opportunity_group_key(row), []).append(row)
        except (KeyError, TypeError, ValueError):
            failures.append(PairingFailure((PASS_CAPTURE_IDENTITY_MISMATCH,)))
    decisions: list[SelectionDecision] = []
    for key in sorted(groups, key=lambda value: tuple(str(item) for item in value)):
        built = build_opportunity(groups[key], config)
        if isinstance(built, PairingFailure):
            failures.append(built)
            continue
        decisions.append(decide(built, config, decided_at_utc=decided_at_utc,
            exposure_already_selected=bool(exposure_is_selected(
                built.exposure_id, config.policy_name, config.policy_version))))
    return decisions, failures


def decide(opportunity: SelectionOpportunity, config: SelectionPolicyConfig, *, decided_at_utc: datetime,
           exposure_already_selected: bool = False, duplicate_policy_evaluation: bool = False) -> SelectionDecision:
    decided_at = _utc(decided_at_utc, "decided_at_utc")
    if decided_at >= opportunity.kickoff_utc:
        raise ValueError(PASS_POST_KICKOFF)
    reasons: set[str] = set()
    action, selected, stake = DecisionAction.PASS, None, None
    if duplicate_policy_evaluation:
        reasons.add(PASS_DUPLICATE_POLICY_EVALUATION)
    elif opportunity.capture_slot != config.eligible_capture_window:
        reasons.add(PASS_WINDOW_NOT_ELIGIBLE)
    elif exposure_already_selected:
        reasons.add(PASS_EXPOSURE_ALREADY_SELECTED)
    else:
        target = _datetime(opportunity.capture_target_time_utc)
        if target is None or target >= opportunity.kickoff_utc:
            reasons.add(PASS_CAPTURE_IDENTITY_MISMATCH)
        for candidate in (opportunity.over, opportunity.under):
            reasons.update(_candidate_gate_reasons(candidate, decided_at))
        reference_over = (opportunity.over.probabilities.reference_over_probability,
                          opportunity.under.probabilities.reference_over_probability)
        if (None not in reference_over and
                not math.isclose(reference_over[0], reference_over[1], abs_tol=PROBABILITY_TOLERANCE)):
            reasons.add(PASS_REFERENCE_PROBABILITY_INVALID)
        if not reasons:
            eligible = []
            model_positive = False
            reference_positive = False
            for candidate in (opportunity.over, opportunity.under):
                break_even = _american_break_even(candidate.offered_odds)
                model_value = candidate.probabilities.model_side_probability > break_even
                reference_value = candidate.probabilities.reference_side_probability > break_even
                model_positive = model_positive or model_value
                reference_positive = reference_positive or reference_value
                if model_value and reference_value:
                    eligible.append(candidate)
            if len(eligible) > 1:
                reasons.add(PASS_AMBIGUOUS_BOTH_SIDES)
            elif not eligible:
                if not model_positive:
                    reasons.add(PASS_NO_POSITIVE_OFFERED_PRICE_EDGE)
                if not reference_positive or model_positive:
                    reasons.add(PASS_REFERENCE_PRICE_VALUE_NOT_POSITIVE)
            else:
                candidate = eligible[0]
                action = DecisionAction.SELECT_OVER if candidate.side == "over" else DecisionAction.SELECT_UNDER
                selected, stake = candidate.observation_id, config.nominal_unit
                reasons.update((SELECT_POLICY_ELIGIBLE, SELECT_POSITIVE_OFFERED_PRICE_EDGE,
                                SELECT_REFERENCE_PRICE_VALUE_POSITIVE, SELECT_CAPTURE_WINDOW_ELIGIBLE,
                                SELECT_FIRST_EXPOSURE_DECISION, SELECT_FIXED_UNIT_10))
    decision_seed = {"opportunity_id": opportunity.opportunity_id, "policy_name": config.policy_name,
                     "policy_version": config.policy_version, "action": action.value,
                     "input_digest": opportunity.input_digest}
    decision_id = str(UUID(_stable_digest("shadow-decision-v1", decision_seed)[:32]))
    return SelectionDecision(decision_id, opportunity.opportunity_id, opportunity.exposure_id,
        opportunity.over.observation_id, opportunity.under.observation_id, selected, action, stake,
        decided_at, tuple(sorted(reasons)), config.policy_name, config.policy_version,
        config.input_schema_version, opportunity.input_digest)
