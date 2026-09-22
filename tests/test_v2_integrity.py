from __future__ import annotations

import json
from copy import deepcopy
from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy import create_engine, insert

from app.shadow_selection import (MODEL_VERSION_ALLOWLIST, SelectionPolicyConfig,
                                  build_opportunity, decide)
from app.shadow_storage import metadata, observations, opportunity_decisions, scheduler_slots
from app.v2_integrity import audit_decision, build_report, load_report

NOW = datetime(2026, 9, 22, 12, tzinfo=timezone.utc)
KICKOFF = NOW + timedelta(minutes=90)


def observation(side="over", **changes):
    probability = .60 if side == "over" else .40
    reference = .55
    row = {"observation_id": f"obs-{side}", "observed_at_utc": NOW-timedelta(seconds=5),
        "kickoff_utc": KICKOFF, "game_id": "event-1", "player_id": "p-1", "player_name": "Player One",
        "canonical_market": "player_pass_yds", "line": 250.5, "side": side,
        "hard_rock_offered_odds": -110, "offered_price_break_even_probability": 110/210,
        "model_probability": probability, "raw_probability_edge_pp": (probability-110/210)*100,
        "model_version": MODEL_VERSION_ALLOWLIST["player_pass_yds"],
        "feature_built_at_utc": NOW-timedelta(minutes=5),
        "uncertainty_method": "shrunk_prediction_conditional_empirical_residual_ecdf",
        "uncertainty_version": "2", "residual_bucket": 1, "uncertainty_scale": 10.0,
        "reference_context": {"exact_threshold_over_no_vig_probability": reference,
            "reference_book_count": 2, "books": {book: {"over_odds": -110, "under_odds": -110,
                "exact_threshold_over_no_vig_probability": reference} for book in ("a", "b")}},
        "freshness": {"provider_updated_at_utc": (NOW-timedelta(seconds=20)).isoformat()},
        "source_observation_ids": {"hardrock": "source-1"},
        "context": {"capture_slot": "90m", "capture_target_time_utc": NOW.isoformat(),
                    "artifact_provenance": {"artifact_sha256": MODEL_VERSION_ALLOWLIST["player_pass_yds"]}},
        "kalshi_context": {}, "confirmation_flags": {}, "research_config": {}}
    row.update(changes)
    return row


def record(action="SELECT_OVER", *, over=None, under=None, mutate=None):
    over, under = over or observation(), under or observation("under")
    opportunity = build_opportunity([over, under], SelectionPolicyConfig("nfl_prop_v2", "v2.0", "90m"))
    decision = decide(opportunity, SelectionPolicyConfig("nfl_prop_v2", "v2.0", "90m"), decided_at_utc=NOW)
    data = decision.as_row()
    if action == "PASS":
        data.update(action="PASS", selected_observation_id=None, intended_stake=None,
                    reason_codes=["PASS_NO_POSITIVE_OFFERED_PRICE_EDGE"])
    elif action == "SELECT_UNDER":
        data.update(action="SELECT_UNDER", selected_observation_id=under["observation_id"])
    if mutate:
        mutate(data, over, under)
    return data, over, under


def under_record():
    ref = {"exact_threshold_over_no_vig_probability": .40, "reference_book_count": 2,
           "books": {b: {"over_odds": 150, "under_odds": -110,
                          "exact_threshold_over_no_vig_probability": .40} for b in ("a", "b")}}
    over = observation(model_probability=.40, raw_probability_edge_pp=(.40-110/210)*100, reference_context=ref)
    under = observation("under", model_probability=.60, raw_probability_edge_pp=(.60-110/210)*100,
                        reference_context=ref)
    return record("SELECT_UNDER", over=over, under=under)


@pytest.mark.parametrize("factory", [record, under_record])
def test_valid_select_sides(factory):
    audited = audit_decision(*factory())
    assert audited["failed_checks"] == []
    assert audited["selected_side"] in {"over", "under"}


def test_valid_pass_and_mixed_capture_accounting():
    selected = record(); passed = record("PASS")
    report = build_report([selected, passed], capture={"status": "completed"}, execution={"execution_id": "x"},
                          generated_at=NOW)
    assert report["total_select_count"] == 1 and report["pass_count"] == 1
    assert report["counts_by_reason_code"]["PASS_NO_POSITIVE_OFFERED_PRICE_EDGE"] == 1
    assert report["schema_version"] == "v2-integrity-report-v1"
    json.dumps(report)


def test_duplicate_exposure_is_a_global_failure():
    one = record(); two = deepcopy(one); two[0]["decision_id"] = "other"
    report = build_report([one, two], capture={"status": "completed"}, execution={"execution_id": "x"})
    assert report["status"] == "FAIL" and report["duplicate_selected_exposure_ids"]


@pytest.mark.parametrize(("mutator", "failed"), [
    (lambda d, o, u: d.update(intended_stake=9), "decision_payload"),
    (lambda d, o, u: (o["context"].update(capture_slot="15m"), u["context"].update(capture_slot="15m")), "eligible_window"),
    (lambda d, o, u: (o["freshness"].update(provider_updated_at_utc=(NOW-timedelta(seconds=301)).isoformat()),
                      u["freshness"].update(provider_updated_at_utc=(NOW-timedelta(seconds=301)).isoformat())), "provider_freshness"),
    (lambda d, o, u: (o.update(feature_built_at_utc=NOW-timedelta(seconds=3601)),
                      u.update(feature_built_at_utc=NOW-timedelta(seconds=3601))), "feature_freshness"),
    (lambda d, o, u: (o.update(kickoff_utc=NOW-timedelta(seconds=1)),
                      u.update(kickoff_utc=NOW-timedelta(seconds=1))), "pregame"),
    (lambda d, o, u: o.update(model_version="wrong"), "supported_model"),
    (lambda d, o, u: o["context"]["artifact_provenance"].update(artifact_sha256="wrong"), "artifact_identity"),
    (lambda d, o, u: o["reference_context"].update(books={"a": o["reference_context"]["books"]["a"]}), "reference_coverage"),
    (lambda d, o, u: o.update(model_probability=.50), "model_value"),
    (lambda d, o, u: o["reference_context"].update(
        exact_threshold_over_no_vig_probability=.50,
        books={b: {"over_odds": 100, "under_odds": 100,
                   "exact_threshold_over_no_vig_probability": .50} for b in ("a", "b")}), "reference_value"),
    (lambda d, o, u: o.update(raw_probability_edge_pp=99), "edge"),
    (lambda d, o, u: d.update(selected_observation_id="not-in-pair"), "decision_payload"),
])
def test_integrity_violations_are_detected(mutator, failed):
    audited = audit_decision(*record(mutate=mutator))
    assert failed in audited["failed_checks"]


def test_pass_cannot_contain_selection_or_stake():
    audited = audit_decision(*record("PASS", mutate=lambda d, o, u: d.update(
        selected_observation_id=o["observation_id"], intended_stake=10)))
    assert "decision_payload" in audited["failed_checks"]


def test_missing_prospective_fields_are_not_verifiable():
    over, under = observation(), observation("under")
    over["freshness"].clear(); under["freshness"].clear()
    missing = record("PASS", over=over, under=under)
    audited = audit_decision(*missing)
    assert "provider_freshness" in audited["not_verifiable_checks"]
    report = build_report([missing],
        capture={"status": "completed"}, execution={"execution_id": "x"})
    assert report["status"] == "INCOMPLETE"


def test_module_has_no_provider_or_mutation_dependency():
    import inspect
    import app.v2_integrity as module
    source = inspect.getsource(module)
    assert "insert(" not in source.lower() and "update(" not in source.lower() and "delete(" not in source.lower()
    assert "OddsProvider" not in source and "Kalshi" not in source


def test_database_report_executes_selects_only():
    engine = create_engine("sqlite://")
    metadata.create_all(engine)
    decision, over, under = record()
    # Supply columns not relevant to this audit but required by the immutable schema.
    for row in (over, under):
        row.update(hypothetical_expected_return_per_dollar=.1, edge_bucket="edge",
                   point_prediction=260.0)
    with engine.begin() as connection:
        connection.execute(insert(observations), [over, under])
        connection.execute(insert(opportunity_decisions), decision)
        connection.execute(insert(scheduler_slots), {"event_id": "event-1", "slot": "90m",
            "target_time_utc": NOW, "status": "completed", "attempts": 1,
            "reason": None, "updated_at_utc": NOW})
    statements = []
    from sqlalchemy import event
    event.listen(engine, "before_cursor_execute",
                 lambda conn, cursor, statement, parameters, context, executemany: statements.append(statement))
    report = load_report(engine, event_id="event-1", generated_at=NOW)
    assert report["canonical_opportunity_count"] == 1
    assert report["status"] == "PASS"
    assert statements and all(statement.lstrip().upper().startswith("SELECT") for statement in statements)
