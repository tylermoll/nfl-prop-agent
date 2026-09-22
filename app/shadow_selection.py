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


INPUT_SCHEMA_VERSION = "shadow-selection-input-v1"
OPPORTUNITY_ID_VERSION = "shadow-opportunity-v1"
EXPOSURE_ID_VERSION = "shadow-exposure-v1"
PROBABILITY_TOLERANCE = 1e-9


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
SELECT_POLICY_ELIGIBLE = "SELECT_POLICY_ELIGIBLE"
SELECT_POSITIVE_OFFERED_PRICE_EDGE = "SELECT_POSITIVE_OFFERED_PRICE_EDGE"
SELECT_CAPTURE_WINDOW_ELIGIBLE = "SELECT_CAPTURE_WINDOW_ELIGIBLE"
SELECT_FIRST_EXPOSURE_DECISION = "SELECT_FIRST_EXPOSURE_DECISION"
SELECT_FIXED_UNIT_10 = "SELECT_FIXED_UNIT_10"

REASON_CODES = frozenset({
    PASS_POST_KICKOFF, PASS_MISSING_SIDE, PASS_DUPLICATE_SIDE,
    PASS_SIDE_PAIR_MISMATCH, PASS_PLAYER_IDENTITY_MISSING, PASS_LINE_MISMATCH,
    PASS_CAPTURE_IDENTITY_MISMATCH, PASS_INVALID_MODEL_PROBABILITY,
    PASS_MODEL_PROBABILITIES_NOT_COMPLEMENTARY, PASS_WINDOW_NOT_ELIGIBLE,
    PASS_EXPOSURE_ALREADY_SELECTED, PASS_DUPLICATE_POLICY_EVALUATION,
    PASS_AMBIGUOUS_BOTH_SIDES, PASS_NO_POSITIVE_OFFERED_PRICE_EDGE,
    SELECT_POLICY_ELIGIBLE, SELECT_POSITIVE_OFFERED_PRICE_EDGE,
    SELECT_CAPTURE_WINDOW_ELIGIBLE, SELECT_FIRST_EXPOSURE_DECISION,
    SELECT_FIXED_UNIT_10,
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
            raise ValueError("V2 phase 1 requires the fixed $10 nominal unit")


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
        "policy": asdict(config)})
    return SelectionOpportunity(opportunity_id, exposure_id, kickoff,
        over_identity["capture_slot"], over_identity["capture_target_time_utc"], over, under, input_digest)


def opportunity_group_key(row: dict[str, Any]) -> tuple[Any, ...]:
    """Group exact snapshot sides; malformed singletons subsequently fail closed."""
    identity = _identity(row)
    return tuple(identity[key] for key in ("game_id", "player_id", "canonical_market", "line",
        "kickoff_utc", "capture_slot", "capture_target_time_utc", "source_id"))


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
        eligible = [candidate for candidate in (opportunity.over, opportunity.under)
                    if candidate.raw_probability_edge_pp > 0]
        if len(eligible) > 1:
            reasons.add(PASS_AMBIGUOUS_BOTH_SIDES)
        elif not eligible:
            reasons.add(PASS_NO_POSITIVE_OFFERED_PRICE_EDGE)
        else:
            candidate = eligible[0]
            action = DecisionAction.SELECT_OVER if candidate.side == "over" else DecisionAction.SELECT_UNDER
            selected, stake = candidate.observation_id, config.nominal_unit
            reasons.update((SELECT_POLICY_ELIGIBLE, SELECT_POSITIVE_OFFERED_PRICE_EDGE,
                            SELECT_CAPTURE_WINDOW_ELIGIBLE, SELECT_FIRST_EXPOSURE_DECISION,
                            SELECT_FIXED_UNIT_10))
    decision_seed = {"opportunity_id": opportunity.opportunity_id, "policy_name": config.policy_name,
                     "policy_version": config.policy_version, "action": action.value,
                     "input_digest": opportunity.input_digest}
    decision_id = str(UUID(_stable_digest("shadow-decision-v1", decision_seed)[:32]))
    return SelectionDecision(decision_id, opportunity.opportunity_id, opportunity.exposure_id,
        opportunity.over.observation_id, opportunity.under.observation_id, selected, action, stake,
        decided_at, tuple(sorted(reasons)), config.policy_name, config.policy_version,
        config.input_schema_version, opportunity.input_digest)
