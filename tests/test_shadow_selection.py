from __future__ import annotations

import ast
import inspect
from datetime import datetime, timedelta, timezone

import pytest

from app.shadow_selection import (
    DecisionAction, PairingFailure, SelectionPolicyConfig, build_opportunity,
    canonical_probabilities, decide, decide_capture)

NOW = datetime(2026, 9, 22, 12, tzinfo=timezone.utc)
KICKOFF = NOW + timedelta(hours=2)
CONFIG = SelectionPolicyConfig("integrity", "v1", "90m")


def row(side="over", **changes):
    model = .60 if side == "over" else .40
    value = {"observation_id": f"obs-{side}", "observed_at_utc": NOW,
        "kickoff_utc": KICKOFF, "game_id": "game", "player_id": "player",
        "player_name": "Display Only", "canonical_market": "player_pass_yds",
        "line": 249.5, "side": side, "hard_rock_offered_odds": -110,
        "model_probability": model, "raw_probability_edge_pp": 7.62 if side == "over" else -12.38,
        "reference_context": {"exact_threshold_over_no_vig_probability": .55},
        "kalshi_context": {"midpoint": .54},
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
        config = SelectionPolicyConfig("integrity", "v1", slot)
        rows = [row(line=line, context={"capture_slot": slot, "capture_target_time_utc": NOW.isoformat()}),
                row("under", line=line, context={"capture_slot": slot, "capture_target_time_utc": NOW.isoformat()})]
        assert opportunity(rows, config).exposure_id == base
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
    rows = [row(model_probability=.40, raw_probability_edge_pp=-12),
            row("under", model_probability=.60, raw_probability_edge_pp=7)]
    candidate = opportunity(rows)
    decision = decide(candidate, CONFIG, decided_at_utc=NOW)
    assert decision.action == DecisionAction.SELECT_UNDER
    assert candidate.under.probabilities.reference_side_probability == pytest.approx(.45)
    assert candidate.under.probabilities.kalshi_side_probability == pytest.approx(.46)


def test_pass_lifecycle_and_window_have_no_stake():
    wrong = decide(opportunity(), SelectionPolicyConfig("integrity", "v1", "15m"), decided_at_utc=NOW)
    assert wrong.action == DecisionAction.PASS and wrong.intended_stake is None
    assert wrong.selected_observation_id is None and wrong.reason_codes == ("PASS_WINDOW_NOT_ELIGIBLE",)
    repeat = decide(opportunity(), CONFIG, decided_at_utc=NOW, exposure_already_selected=True)
    assert repeat.reason_codes == ("PASS_EXPOSURE_ALREADY_SELECTED",)


def test_both_positive_edges_pass_as_ambiguous_and_post_kickoff_passes():
    candidate = opportunity([row(raw_probability_edge_pp=1), row("under", raw_probability_edge_pp=1)])
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
