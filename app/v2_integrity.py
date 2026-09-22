"""Read-only integrity audit for persisted, prospective V2 decisions.

The auditor intentionally starts at ``shadow_opportunity_decisions``.  It does
not infer a strategy population from observations and it has no provider,
scheduler, settlement, or write dependency.
"""
from __future__ import annotations

import math
from collections import Counter
from datetime import datetime, timezone
from typing import Any, Iterable

from sqlalchemy import and_, select
from sqlalchemy.engine import Engine

from app.shadow_selection import (
    EDGE_TOLERANCE_PP, FEATURE_MAX_AGE_SECONDS, FROZEN_ELIGIBLE_WINDOW,
    FROZEN_POLICY_NAME, FROZEN_POLICY_VERSION, INPUT_SCHEMA_VERSION,
    MINIMUM_REFERENCE_BOOKS, MODEL_VERSION_ALLOWLIST, PROBABILITY_TOLERANCE,
    PROVIDER_MAX_AGE_SECONDS, REASON_CODES, UNCERTAINTY_METHOD,
    UNCERTAINTY_VERSION, PairingFailure, SelectionPolicyConfig,
    build_opportunity, canonical_probabilities,
)
from app.shadow_storage import (opportunity_decisions, observations,
                                scheduler_executions, scheduler_slots)


REQUIRED_CHECKS = (
    "policy_identity", "decision_payload", "canonical_action_reasons", "pair_membership", "pair_identity",
    "eligible_window", "pregame", "provider_freshness", "feature_freshness",
    "supported_model", "artifact_identity", "uncertainty_contract", "break_even", "edge",
    "model_probabilities", "model_value", "reference_probability",
    "reference_coverage", "reference_value", "input_digest",
)


def _iso(value: Any) -> str | None:
    if not isinstance(value, datetime):
        return None
    if value.tzinfo is None:
        value = value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc).isoformat()


def _dt(value: Any) -> datetime | None:
    if isinstance(value, str):
        try:
            value = datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError:
            return None
    if not isinstance(value, datetime):
        return None
    if value.tzinfo is None:  # SQLite discards timezone information.
        value = value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


def _number(value: Any) -> float | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    value = float(value)
    return value if math.isfinite(value) else None


def _break_even(odds: Any) -> float | None:
    if isinstance(odds, bool) or not isinstance(odds, int) or odds == 0:
        return None
    return abs(odds) / (abs(odds) + 100) if odds < 0 else 100 / (odds + 100)


def _check(ok: bool | None, detail: str | None = None) -> dict[str, Any]:
    return {"status": "NOT_VERIFIABLE" if ok is None else "PASS" if ok else "FAIL",
            "detail": detail}


def _qualifying_reference(row: dict[str, Any]) -> tuple[int | None, float | None]:
    books = (row.get("reference_context") or {}).get("books")
    if not isinstance(books, dict):
        return None, None
    probabilities = []
    for quote in books.values():
        if not isinstance(quote, dict):
            continue
        probability = _number(quote.get("exact_threshold_over_no_vig_probability"))
        if (isinstance(quote.get("over_odds"), int) and not isinstance(quote.get("over_odds"), bool)
                and isinstance(quote.get("under_odds"), int) and not isinstance(quote.get("under_odds"), bool)
                and probability is not None and 0 <= probability <= 1):
            probabilities.append(probability)
    if not probabilities:
        return 0, None
    ordered = sorted(probabilities); middle = len(ordered) // 2
    median = ordered[middle] if len(ordered) % 2 else (ordered[middle - 1] + ordered[middle]) / 2
    return len(probabilities), median


def audit_decision(decision: dict[str, Any], over: dict[str, Any] | None,
                   under: dict[str, Any] | None) -> dict[str, Any]:
    """Recompute one canonical decision solely from its persisted pair."""
    action = decision.get("action")
    selected = over if action == "SELECT_OVER" else under if action == "SELECT_UNDER" else None
    checks: dict[str, dict[str, Any]] = {}
    checks["policy_identity"] = _check(
        decision.get("policy_name") == FROZEN_POLICY_NAME and
        decision.get("policy_version") == FROZEN_POLICY_VERSION and
        decision.get("input_schema_version") == INPUT_SCHEMA_VERSION)
    payload_ok = ((action == "PASS" and decision.get("selected_observation_id") is None and
                   decision.get("intended_stake") is None) or
                  (action == "SELECT_OVER" and over is not None and
                   decision.get("selected_observation_id") == over.get("observation_id") and
                   decision.get("intended_stake") == 10.0) or
                  (action == "SELECT_UNDER" and under is not None and
                   decision.get("selected_observation_id") == under.get("observation_id") and
                   decision.get("intended_stake") == 10.0))
    checks["decision_payload"] = _check(payload_ok)
    checks["pair_membership"] = _check(None if over is None or under is None else
        over.get("observation_id") != under.get("observation_id") and
        decision.get("over_observation_id") == over.get("observation_id") and
        decision.get("under_observation_id") == under.get("observation_id"))

    decided = _dt(decision.get("decided_at_utc"))
    if over is None or under is None:
        for name in REQUIRED_CHECKS:
            checks.setdefault(name, _check(None, "persisted O/U observation pair unavailable"))
    else:
        contexts = [row.get("context") or {} for row in (over, under)]
        identity_fields = ("game_id", "player_id", "canonical_market", "line", "kickoff_utc")
        pair_ok = (over.get("side") == "over" and under.get("side") == "under" and
                   all(over.get(key) == under.get(key) for key in identity_fields) and
                   (over.get("source_observation_ids") or {}).get("hardrock") ==
                   (under.get("source_observation_ids") or {}).get("hardrock"))
        checks["pair_identity"] = _check(pair_ok)
        slot = contexts[0].get("capture_slot")
        checks["eligible_window"] = _check(slot == contexts[1].get("capture_slot") and
            (action == "PASS" or slot == FROZEN_ELIGIBLE_WINDOW))
        kickoff = _dt(over.get("kickoff_utc"))
        checks["pregame"] = _check(None if decided is None or kickoff is None else
                                    decided < kickoff and all((_dt(r.get("observed_at_utc")) or kickoff) < kickoff
                                                             for r in (over, under)))

        provider_ages, feature_ages = [], []
        for row in (over, under):
            fresh = row.get("freshness") or {}
            provider = _dt(fresh.get("provider_updated_at_utc")) or _dt(fresh.get("provider_observed_at_utc"))
            feature = _dt(row.get("feature_built_at_utc"))
            provider_ages.append(None if decided is None or provider is None else (decided-provider).total_seconds())
            feature_ages.append(None if decided is None or feature is None else (decided-feature).total_seconds())
        checks["provider_freshness"] = _check(None if None in provider_ages else
            all(0 <= age <= PROVIDER_MAX_AGE_SECONDS for age in provider_ages))
        checks["feature_freshness"] = _check(None if None in feature_ages else
            all(0 <= age <= FEATURE_MAX_AGE_SECONDS for age in feature_ages))
        checks["supported_model"] = _check(all(
            row.get("canonical_market") in MODEL_VERSION_ALLOWLIST and
            row.get("model_version") == MODEL_VERSION_ALLOWLIST.get(row.get("canonical_market"))
            for row in (over, under)))
        artifacts = [(row.get("context") or {}).get("artifact_provenance", {}).get("artifact_sha256")
                     for row in (over, under)]
        checks["artifact_identity"] = _check(None if None in artifacts else all(
            artifact == MODEL_VERSION_ALLOWLIST.get(row.get("canonical_market"))
            for artifact, row in zip(artifacts, (over, under))))
        uncertainty_ok = True
        for row in (over, under):
            scale = _number(row.get("uncertainty_scale"))
            uncertainty_ok = uncertainty_ok and (
                row.get("uncertainty_method") == UNCERTAINTY_METHOD and
                str(row.get("uncertainty_version")) == UNCERTAINTY_VERSION and
                isinstance(row.get("residual_bucket"), int) and
                not isinstance(row.get("residual_bucket"), bool) and
                row.get("residual_bucket") >= 0 and scale is not None and scale >= 0)
        checks["uncertainty_contract"] = _check(uncertainty_ok)

        bes = [_break_even(row.get("hard_rock_offered_odds")) for row in (over, under)]
        persisted_bes = [_number(row.get("offered_price_break_even_probability")) for row in (over, under)]
        checks["break_even"] = _check(None if None in bes or None in persisted_bes else all(
            math.isclose(a, b, abs_tol=PROBABILITY_TOLERANCE) for a, b in zip(bes, persisted_bes)))
        try:
            probabilities = [canonical_probabilities(row) for row in (over, under)]
        except (TypeError, ValueError):
            probabilities = None
        edges = [_number(row.get("raw_probability_edge_pp")) for row in (over, under)]
        checks["edge"] = _check(None if probabilities is None or None in bes or None in edges else all(
            math.isclose(edge, (prob.model_side_probability-be)*100, abs_tol=EDGE_TOLERANCE_PP)
            for edge, prob, be in zip(edges, probabilities, bes)))
        checks["model_probabilities"] = _check(None if probabilities is None else
            math.isclose(probabilities[0].model_over_probability,
                         probabilities[1].model_over_probability, abs_tol=PROBABILITY_TOLERANCE) and
            all(math.isclose(p.model_over_probability+p.model_under_probability, 1,
                             abs_tol=PROBABILITY_TOLERANCE) for p in probabilities))
        ref_info = [_qualifying_reference(row) for row in (over, under)]
        checks["reference_coverage"] = _check(all(count is not None and count >= MINIMUM_REFERENCE_BOOKS
                                                    for count, _ in ref_info))
        ref_over = [None if probabilities is None else p.reference_over_probability for p in probabilities or ()]
        checks["reference_probability"] = _check(None if probabilities is None else all(
            probability is not None and median is not None and
            math.isclose(probability, median, abs_tol=PROBABILITY_TOLERANCE)
            for probability, (_, median) in zip(ref_over, ref_info)))
        if action == "PASS":
            checks["model_value"] = _check(True, "not required for PASS")
            checks["reference_value"] = _check(True, "not required for PASS")
        else:
            index = 0 if action == "SELECT_OVER" else 1
            checks["model_value"] = _check(None if probabilities is None or bes[index] is None else
                probabilities[index].model_side_probability > bes[index])
            ref_side = None if probabilities is None else probabilities[index].reference_side_probability
            checks["reference_value"] = _check(None if ref_side is None or bes[index] is None else ref_side > bes[index])
        try:
            normalized_pair = []
            for source in (over, under):
                normalized = dict(source)
                for field in ("observed_at_utc", "kickoff_utc", "feature_built_at_utc"):
                    normalized[field] = _dt(normalized.get(field))
                normalized_pair.append(normalized)
            built = build_opportunity(normalized_pair, SelectionPolicyConfig(
                FROZEN_POLICY_NAME, FROZEN_POLICY_VERSION, FROZEN_ELIGIBLE_WINDOW))
            digest_ok = not isinstance(built, PairingFailure) and built.input_digest == decision.get("input_digest")
            identity_ok = not isinstance(built, PairingFailure) and built.opportunity_id == decision.get("opportunity_id")
        except (KeyError, TypeError, ValueError):
            digest_ok = identity_ok = None
        checks["input_digest"] = _check(digest_ok)
        checks["pair_identity"] = _check(pair_ok and identity_ok is True)

    reason_codes = decision.get("reason_codes")
    checks["reason_codes"] = _check(isinstance(reason_codes, list) and
        len(reason_codes) == len(set(reason_codes)) and not (set(reason_codes) - REASON_CODES))
    select_reasons = {"SELECT_POLICY_ELIGIBLE", "SELECT_POSITIVE_OFFERED_PRICE_EDGE",
        "SELECT_REFERENCE_PRICE_VALUE_POSITIVE", "SELECT_CAPTURE_WINDOW_ELIGIBLE",
        "SELECT_FIRST_EXPOSURE_DECISION", "SELECT_FIXED_UNIT_10"}
    reason_set = set(reason_codes or [])
    checks["canonical_action_reasons"] = _check(
        reason_set == select_reasons if action in {"SELECT_OVER", "SELECT_UNDER"} else
        bool(reason_set) and all(code.startswith("PASS_") for code in reason_set))
    failures = sorted(name for name, value in checks.items() if value["status"] == "FAIL")
    missing = sorted(name for name in REQUIRED_CHECKS if checks[name]["status"] == "NOT_VERIFIABLE")
    row = selected or over or under or {}
    probabilities = None
    try:
        if selected: probabilities = canonical_probabilities(selected)
    except (TypeError, ValueError):
        pass
    be = _break_even(row.get("hard_rock_offered_odds"))
    provider = _dt((row.get("freshness") or {}).get("provider_updated_at_utc")) or _dt(
        (row.get("freshness") or {}).get("provider_observed_at_utc"))
    feature = _dt(row.get("feature_built_at_utc"))
    count, _ = _qualifying_reference(row)
    return {"decision_id": decision.get("decision_id"), "opportunity_id": decision.get("opportunity_id"),
        "exposure_id": decision.get("exposure_id"), "action": action,
        "reason_codes": reason_codes, "event": row.get("game_id"), "player_id": row.get("player_id"),
        "player_name": row.get("player_name"), "market": row.get("canonical_market"), "line": row.get("line"),
        "selected_side": row.get("side") if selected else None,
        "hard_rock_american_price": row.get("hard_rock_offered_odds") if selected else None,
        "hard_rock_break_even_probability": be if selected else None,
        "model_side_probability": probabilities.model_side_probability if probabilities else None,
        "raw_model_edge_pp": row.get("raw_probability_edge_pp") if selected else None,
        "exact_line_reference_side_probability": probabilities.reference_side_probability if probabilities else None,
        "reference_book_count": count, "model_version": row.get("model_version"),
        "artifact_sha256": (row.get("context") or {}).get("artifact_provenance", {}).get("artifact_sha256"),
        "provider_age_seconds": None if decided is None or provider is None else (decided-provider).total_seconds(),
        "feature_age_seconds": None if decided is None or feature is None else (decided-feature).total_seconds(),
        "intended_stake": decision.get("intended_stake"), "checks": checks,
        "failed_checks": failures, "not_verifiable_checks": missing}


def build_report(records: Iterable[tuple[dict[str, Any], dict[str, Any] | None, dict[str, Any] | None]],
                 *, capture: dict[str, Any] | None = None, execution: dict[str, Any] | None = None,
                 generated_at: datetime | None = None) -> dict[str, Any]:
    audited = [audit_decision(*record) for record in records]
    actions = Counter(row["action"] for row in audited)
    markets = Counter(row["market"] or "UNKNOWN" for row in audited)
    reasons = Counter(code for row in audited for code in (row.get("reason_codes") or []))
    selected = [row for row in audited if row["action"] in {"SELECT_OVER", "SELECT_UNDER"}]
    exposures = Counter(row["exposure_id"] for row in selected)
    duplicates = sorted(key for key, count in exposures.items() if count > 1)
    global_checks = {
        "capture_completed": _check(None if capture is None else capture.get("status") == "completed"),
        "canonical_decisions_present": _check(bool(audited)),
        "duplicate_selected_exposures": _check(not duplicates),
        "scheduler_execution_linkage": _check(True if execution is not None else None,
            "execution metadata is not relationally linked to decisions" if execution is None else None),
    }
    has_fail = any(c["status"] == "FAIL" for c in global_checks.values()) or any(r["failed_checks"] for r in audited)
    # Execution identity is optional metadata, not a decision invariant.  Its
    # absence must not make the default latest-capture audit inconclusive.
    has_incomplete = any(c["status"] == "NOT_VERIFIABLE" for name, c in global_checks.items()
                         if name != "scheduler_execution_linkage") or any(
        r["not_verifiable_checks"] for r in audited)
    status = "FAIL" if has_fail else "INCOMPLETE" if has_incomplete else "PASS"
    event_rows = {}
    for _, over, under in records:
        row = over or under or {}
        if row.get("game_id"):
            event_rows[row["game_id"]] = {"event_id": row["game_id"], "kickoff_utc": _iso(row.get("kickoff_utc"))}
    return {"schema_version": "v2-integrity-report-v1", "status": status,
        "generated_at_utc": _iso(generated_at or datetime.now(timezone.utc)), "read_only": True,
        "scope": "prospective_v2_canonical_decisions", "policy_name": FROZEN_POLICY_NAME,
        "policy_version": FROZEN_POLICY_VERSION, "eligible_window": FROZEN_ELIGIBLE_WINDOW,
        "capture": capture, "scheduler_execution": execution,
        "events": sorted(event_rows.values(), key=lambda item: item["event_id"]),
        "observation_count": len({r.get("observation_id") for _, over, under in records
                                  for r in (over, under) if r}),
        "canonical_opportunity_count": len(audited), "select_over_count": actions["SELECT_OVER"],
        "select_under_count": actions["SELECT_UNDER"],
        "total_select_count": actions["SELECT_OVER"] + actions["SELECT_UNDER"],
        "pass_count": actions["PASS"], "counts_by_market": dict(sorted(markets.items())),
        "counts_by_reason_code": dict(sorted(reasons.items())),
        "duplicate_selected_exposure_ids": duplicates, "global_checks": global_checks,
        "select_details": selected, "pass_details": [r for r in audited if r["action"] == "PASS"],
        "integrity_limitations": ["scheduler executions are JSON metadata and have no foreign key to decisions"]}


def load_report(engine: Engine, *, event_id: str | None = None,
                execution_id: str | None = None, generated_at: datetime | None = None) -> dict[str, Any]:
    """Issue SELECT statements only and audit the latest matching 90m capture."""
    over = observations.alias("over_observation"); under = observations.alias("under_observation")
    with engine.connect() as connection:
        execution = None
        execution_events: set[str] = set()
        if execution_id:
            result = connection.execute(select(scheduler_executions).where(
                scheduler_executions.c.execution_id == execution_id)).mappings().first()
            if result:
                execution = dict(result); details = execution.get("details") or {}
                execution_events = {item.get("event_id") for key in ("completed", "selected")
                                    for item in details.get(key, []) if item.get("slot") == FROZEN_ELIGIBLE_WINDOW}
                execution_events.discard(None)
        query = select(opportunity_decisions, over, under).select_from(
            opportunity_decisions.join(over, opportunity_decisions.c.over_observation_id == over.c.observation_id)
            .join(under, opportunity_decisions.c.under_observation_id == under.c.observation_id)).where(
                opportunity_decisions.c.policy_name == FROZEN_POLICY_NAME,
                opportunity_decisions.c.policy_version == FROZEN_POLICY_VERSION,
                over.c.context["capture_slot"].as_string() == FROZEN_ELIGIBLE_WINDOW)
        if execution_id and execution is None:
            query = query.where(opportunity_decisions.c.decision_id == "__missing_execution__")
        allowed_events = ({event_id} if event_id else execution_events)
        if allowed_events:
            query = query.where(over.c.game_id.in_(allowed_events))
        all_rows = connection.execute(query.order_by(opportunity_decisions.c.decided_at_utc.desc())).mappings().all()
        records = []
        for result in all_rows:
            decision = {column.name: result[column.name] for column in opportunity_decisions.columns}
            # SQLAlchemy's duplicate-label mapping is awkward across versions; fetch pair explicitly.
            pair = connection.execute(select(observations).where(observations.c.observation_id.in_([
                decision["over_observation_id"], decision["under_observation_id"]]))).mappings().all()
            by_id = {row["observation_id"]: dict(row) for row in pair}
            records.append((decision, by_id.get(decision["over_observation_id"]), by_id.get(decision["under_observation_id"])))
        if records and not execution_id:
            latest = max((_dt(item[0]["decided_at_utc"]) for item in records), default=None)
            records = [item for item in records if _dt(item[0]["decided_at_utc"]) == latest]
        capture = None
        if records:
            first = records[0][1] or records[0][2] or {}; context = first.get("context") or {}
            target = _dt(context.get("capture_target_time_utc")); game = first.get("game_id")
            slot = connection.execute(select(scheduler_slots).where(and_(
                scheduler_slots.c.event_id == game, scheduler_slots.c.slot == FROZEN_ELIGIBLE_WINDOW,
                scheduler_slots.c.target_time_utc == target))).mappings().first() if target else None
            capture = dict(slot) if slot else {"event_id": game, "slot": FROZEN_ELIGIBLE_WINDOW,
                "target_time_utc": _iso(target), "status": None}
    return build_report(records, capture=capture, execution=execution, generated_at=generated_at)
