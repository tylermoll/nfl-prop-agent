from __future__ import annotations

import ast
import inspect
from datetime import datetime, timedelta, timezone

import pytest

from app.shadow_selection import (
    DecisionAction, FEATURE_MAX_AGE_SECONDS, FROZEN_POLICY_NAME, FROZEN_POLICY_VERSION,
    MODEL_VERSION_ALLOWLIST, PairingFailure, SelectionPolicyConfig, build_opportunity,
    canonical_probabilities, decide, decide_capture)

NOW = datetime(2026, 9, 22, 12, tzinfo=timezone.utc)
KICKOFF = NOW + timedelta(hours=2)
CONFIG = SelectionPolicyConfig(FROZEN_POLICY_NAME, FROZEN_POLICY_VERSION, "90m")


def row(side="over", **changes):
    model = .60 if side == "over" else .40
    value = {"observation_id": f"obs-{side}", "observed_at_utc": NOW,
        "kickoff_utc": KICKOFF, "game_id": "game", "player_id": "player",
        "player_name": "Display Only", "canonical_market": "player_pass_yds",
        "line": 249.5, "side": side, "hard_rock_offered_odds": -110,
        "model_probability": model,
        "offered_price_break_even_probability": 110 / 210,
        "raw_probability_edge_pp": (model - 110 / 210) * 100,
        "model_version": MODEL_VERSION_ALLOWLIST["player_pass_yds"],
        "feature_built_at_utc": NOW - timedelta(minutes=5),
        "uncertainty_method": "shrunk_prediction_conditional_empirical_residual_ecdf",
        "uncertainty_version": "2", "residual_bucket": 1, "uncertainty_scale": 15.0,
        "reference_context": {"reference_book_count": 2,
            "exact_threshold_over_no_vig_probability": .55,
            "books": {name: {"over_odds": -110, "under_odds": -110,
                "exact_threshold_over_no_vig_probability": .55}
                for name in ("draftkings", "fanduel")}},
        "kalshi_context": {"midpoint": .54},
        "freshness": {"provider_updated_at_utc": (NOW-timedelta(seconds=30)).isoformat(),
            "provider_age_seconds": 30, "provider_stale": False,
            "model_feature_age_seconds": 300, "model_stale": False},
        "source_observation_ids": {"hardrock": "game:hardrock:pass:player"},
        "context": {"capture_slot": "90m", "capture_target_time_utc": NOW.isoformat()}}
    value.update(changes)
    return value


def opportunity(rows=None, config=CONFIG):
    result = build_opportunity(rows or [row(), row("under")], config)
    assert not isinstance(result, PairingFailure)
    return result


def test_probability_adapter_is_explicitly_side_aligned():
    over = canonical_probabilities(row())
    under = canonical_probabilities(row("under"))
    assert (over.model_over_probability, over.model_under_probability, over.model_side_probability) == (.6, .4, .6)
    assert (under.model_over_probability, under.model_under_probability, under.model_side_probability) == (.6, .4, .4)
    assert (over.reference_over_probability, over.reference_under_probability,
            over.reference_side_probability) == pytest.approx((.55, .45, .55))
    assert (under.reference_over_probability, under.reference_under_probability,
            under.reference_side_probability) == pytest.approx((.55, .45, .45))
    assert (over.kalshi_yes_probability, over.kalshi_no_probability,
            over.kalshi_side_probability) == pytest.approx((.54, .46, .54))
    assert (under.kalshi_yes_probability, under.kalshi_no_probability,
            under.kalshi_side_probability) == pytest.approx((.54, .46, .46))
    assert under.kalshi_basis == "yes_midpoint"


@pytest.mark.parametrize(("rows", "reason"), [
    ([row()], "PASS_MISSING_SIDE"),
    ([row(), row(observation_id="duplicate-over"), row("under")], "PASS_DUPLICATE_SIDE"),
    ([row(), row("under", line=250.5)], "PASS_MISSING_SIDE"),
    ([row(), row("under", player_id="other")], "PASS_MISSING_SIDE"),
])
def test_malformed_groups_never_produce_a_selection(rows, reason):
    decisions, failures = decide_capture(rows, CONFIG, decided_at_utc=NOW)
    assert not decisions and any(reason in failure.reason_codes for failure in failures)


def test_direct_pair_validation_reports_mismatches():
    result = build_opportunity([row(), row("under", line=250.5)], CONFIG)
    assert isinstance(result, PairingFailure) and "PASS_LINE_MISMATCH" in result.reason_codes
    result = build_opportunity([row(), row("under", game_id="other")], CONFIG)
    assert isinstance(result, PairingFailure) and "PASS_SIDE_PAIR_MISMATCH" in result.reason_codes


def test_opportunity_id_is_deterministic_and_ignores_random_observation_ids():
    first = opportunity()
    second = opportunity([row(observation_id="new-over"), row("under", observation_id="new-under")])
    assert first.opportunity_id == second.opportunity_id
    assert first.input_digest == second.input_digest


def test_exposure_is_stable_across_windows_and_lines_but_not_core_identity():
    base = opportunity().exposure_id
    for slot, line in (("24h", 250.5), ("6h", 251.5), ("15m", 248.5)):
        rows = [row(line=line, context={"capture_slot": slot, "capture_target_time_utc": NOW.isoformat()}),
                row("under", line=line, context={"capture_slot": slot, "capture_target_time_utc": NOW.isoformat()})]
        assert opportunity(rows).exposure_id == base
    for change in ({"game_id": "different"}, {"player_id": "different"},
                   {"canonical_market": "player_receptions"}):
        assert opportunity([row(**change), row("under", **change)]).exposure_id != base


def test_decisions_are_deterministic_one_sided_and_fixed_unit():
    candidate = opportunity()
    first = decide(candidate, CONFIG, decided_at_utc=NOW)
    second = decide(candidate, CONFIG, decided_at_utc=NOW)
    assert first == second
    assert first.action == DecisionAction.SELECT_OVER
    assert first.selected_observation_id == candidate.over.observation_id
    assert first.intended_stake == 10
    assert tuple(sorted(first.reason_codes)) == first.reason_codes


def test_under_can_be_selected_without_using_raw_over_context():
    rows = [row(model_probability=.40, raw_probability_edge_pp=(.40-110/210)*100,
                reference_context={**row()["reference_context"], "exact_threshold_over_no_vig_probability": .40,
                    "books": {name: {"over_odds": 150, "under_odds": -110,
                        "exact_threshold_over_no_vig_probability": .40} for name in ("draftkings", "fanduel")}}),
            row("under", model_probability=.60, raw_probability_edge_pp=(.60-110/210)*100,
                reference_context={**row()["reference_context"], "exact_threshold_over_no_vig_probability": .40,
                    "books": {name: {"over_odds": 150, "under_odds": -110,
                        "exact_threshold_over_no_vig_probability": .40} for name in ("draftkings", "fanduel")}})]
    candidate = opportunity(rows)
    decision = decide(candidate, CONFIG, decided_at_utc=NOW)
    assert decision.action == DecisionAction.SELECT_UNDER
    assert candidate.under.probabilities.reference_side_probability == pytest.approx(.60)
    assert candidate.under.probabilities.kalshi_side_probability == pytest.approx(.46)


def test_pass_lifecycle_and_window_have_no_stake():
    wrong = decide(opportunity([row(context={"capture_slot": "15m", "capture_target_time_utc": NOW.isoformat()}),
                                row("under", context={"capture_slot": "15m", "capture_target_time_utc": NOW.isoformat()})]),
                   CONFIG, decided_at_utc=NOW)
    assert wrong.action == DecisionAction.PASS and wrong.intended_stake is None
    assert wrong.selected_observation_id is None and wrong.reason_codes == ("PASS_WINDOW_NOT_ELIGIBLE",)
    repeat = decide(opportunity(), CONFIG, decided_at_utc=NOW, exposure_already_selected=True)
    assert repeat.reason_codes == ("PASS_EXPOSURE_ALREADY_SELECTED",)


def test_both_positive_edges_pass_as_ambiguous_and_post_kickoff_passes():
    reference = {**row()["reference_context"], "exact_threshold_over_no_vig_probability": .50,
        "books": {name: {"over_odds": 100, "under_odds": 100,
            "exact_threshold_over_no_vig_probability": .50} for name in ("draftkings", "fanduel")}}
    common = {"model_probability": .50, "hard_rock_offered_odds": 150,
              "offered_price_break_even_probability": .40, "raw_probability_edge_pp": 10,
              "reference_context": reference}
    candidate = opportunity([row(**common), row("under", **common)])
    assert decide(candidate, CONFIG, decided_at_utc=NOW).reason_codes == ("PASS_AMBIGUOUS_BOTH_SIDES",)
    with pytest.raises(ValueError, match="PASS_POST_KICKOFF"):
        decide(candidate, CONFIG, decided_at_utc=KICKOFF)
    with pytest.raises(ValueError, match="timezone-aware"):
        decide(candidate, CONFIG, decided_at_utc=NOW.replace(tzinfo=None))


def test_selection_module_has_no_settlement_or_execution_dependency():
    import app.shadow_selection as module
    tree = ast.parse(inspect.getsource(module))
    imports = {alias.name for node in ast.walk(tree) if isinstance(node, ast.Import)
               for alias in node.names}
    imports |= {node.module or "" for node in ast.walk(tree) if isinstance(node, ast.ImportFrom)}
    assert not any(any(term in name for term in ("settlement", "provider", "storage", "wager", "order"))
                   for name in imports)


def test_frozen_configuration_rejects_semantic_overrides_and_hashes_are_exact():
    with pytest.raises(ValueError, match="literal 90m"):
        SelectionPolicyConfig(FROZEN_POLICY_NAME, FROZEN_POLICY_VERSION, "15m")
    assert MODEL_VERSION_ALLOWLIST == {
        "player_pass_yds": "ddd3214caf4af113ed0b0409a87d10464aded7702b3fa0cd182b76b3a3a09cff",
        "player_reception_yds": "c9142a38dd44a12025a13ae71d37c843b2690fb5dd45529c381a300a253b2d87",
        "player_receptions": "fb64013cec878eb2cec6153d494d93e2842412ae0bfdcd4dfeceba6b6d94ae67"}


@pytest.mark.parametrize("slot", ["24h", "6h", "15m"])
def test_noneligible_windows_pass(slot):
    rows = [row(context={"capture_slot": slot, "capture_target_time_utc": NOW.isoformat()}),
            row("under", context={"capture_slot": slot, "capture_target_time_utc": NOW.isoformat()})]
    assert decide(opportunity(rows), CONFIG, decided_at_utc=NOW).reason_codes == ("PASS_WINDOW_NOT_ELIGIBLE",)


@pytest.mark.parametrize(("changes", "reason"), [
    ({"model_version": "wrong"}, "PASS_MODEL_VERSION_NOT_ALLOWED"),
    ({"uncertainty_method": "wrong"}, "PASS_UNCERTAINTY_CONTRACT_INVALID"),
    ({"uncertainty_version": "1"}, "PASS_UNCERTAINTY_CONTRACT_INVALID"),
    ({"residual_bucket": -1}, "PASS_UNCERTAINTY_CONTRACT_INVALID"),
    ({"uncertainty_scale": float("nan")}, "PASS_UNCERTAINTY_CONTRACT_INVALID"),
    ({"freshness": {}}, "PASS_PROVIDER_TIMESTAMP_MISSING"),
    ({"freshness": {"provider_updated_at_utc": (NOW-timedelta(seconds=301)).isoformat()}}, "PASS_PROVIDER_STALE"),
    ({"feature_built_at_utc": NOW-timedelta(seconds=3601)}, "PASS_FEATURE_STALE"),
    ({"feature_built_at_utc": NOW+timedelta(seconds=1)}, "PASS_FUTURE_FEATURE_TIMESTAMP"),
])
def test_hard_gates_fail_closed(changes, reason):
    decision = decide(opportunity([row(**changes), row("under", **changes)]), CONFIG, decided_at_utc=NOW)
    assert decision.action == DecisionAction.PASS and reason in decision.reason_codes


def test_six_hour_cache_validity_does_not_relax_one_hour_observation_gate():
    assert FEATURE_MAX_AGE_SECONDS == 3600
    changes = {"feature_built_at_utc": NOW-timedelta(hours=2),
               "freshness": {"provider_updated_at_utc": NOW.isoformat(),
                   "model_feature_age_seconds": 7200, "model_stale": True}}
    decision = decide(opportunity([row(**changes), row("under", **changes)]), CONFIG, decided_at_utc=NOW)
    assert decision.reason_codes == ("PASS_FEATURE_STALE",)


def test_reference_and_model_must_independently_beat_hard_rock_price():
    weak_reference = {**row()["reference_context"], "exact_threshold_over_no_vig_probability": .50,
        "books": {name: {"over_odds": 100, "under_odds": 100,
            "exact_threshold_over_no_vig_probability": .50} for name in ("draftkings", "fanduel")}}
    result = decide(opportunity([row(reference_context=weak_reference),
        row("under", reference_context=weak_reference)]), CONFIG, decided_at_utc=NOW)
    assert result.action == DecisionAction.PASS
    assert "PASS_REFERENCE_PRICE_VALUE_NOT_POSITIVE" in result.reason_codes

    low_model = .50
    rows = [row(model_probability=low_model, raw_probability_edge_pp=(low_model-110/210)*100),
            row("under", model_probability=1-low_model,
                raw_probability_edge_pp=((1-low_model)-110/210)*100)]
    result = decide(opportunity(rows), CONFIG, decided_at_utc=NOW)
    assert result.action == DecisionAction.PASS
    assert "PASS_NO_POSITIVE_OFFERED_PRICE_EDGE" in result.reason_codes


def test_reference_coverage_and_edge_recomputation_are_hard_gates():
    one = {**row()["reference_context"], "reference_book_count": 1,
           "books": {"draftkings": row()["reference_context"]["books"]["draftkings"]}}
    decision = decide(opportunity([row(reference_context=one), row("under", reference_context=one)]),
                      CONFIG, decided_at_utc=NOW)
    assert "PASS_INSUFFICIENT_REFERENCE_COVERAGE" in decision.reason_codes
    decision = decide(opportunity([row(raw_probability_edge_pp=99), row("under")]), CONFIG, decided_at_utc=NOW)
    assert decision.reason_codes == ("PASS_EDGE_RECOMPUTATION_MISMATCH",)


def test_kalshi_is_never_decision_causing():
    absent = decide(opportunity([row(kalshi_context={}), row("under", kalshi_context={})]),
                    CONFIG, decided_at_utc=NOW)
    disagreement = decide(opportunity([row(kalshi_context={"midpoint": .01}),
        row("under", kalshi_context={"midpoint": .01})]), CONFIG, decided_at_utc=NOW)
    assert absent.action == disagreement.action == DecisionAction.SELECT_OVER
