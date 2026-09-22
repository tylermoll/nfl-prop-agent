from datetime import datetime, timedelta, timezone
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from types import SimpleNamespace

import pytest
from sqlalchemy import func, insert, select
from sqlalchemy.exc import IntegrityError

from app.shadow import make_observation
from app.shadow_selection import (FROZEN_POLICY_NAME, FROZEN_POLICY_VERSION,
                                  MODEL_VERSION_ALLOWLIST, SelectionPolicyConfig,
                                  build_opportunity, decide)
from app.shadow_storage import (ShadowStore, observations, opportunity_decisions,
                                scheduler_slots)

NOW = datetime(2026, 9, 22, 12, tzinfo=timezone.utc)
KICKOFF = NOW + timedelta(hours=2)
CONFIG = SelectionPolicyConfig(FROZEN_POLICY_NAME, FROZEN_POLICY_VERSION, "90m")


def observation(side, *, line=249.5, slot="90m"):
    score = SimpleNamespace(over_probability=.60, under_probability=.40,
        model_version=MODEL_VERSION_ALLOWLIST["player_pass_yds"], point_prediction=260.0,
        feature_built_at_utc=NOW-timedelta(minutes=5),
        uncertainty_method="shrunk_prediction_conditional_empirical_residual_ecdf",
        uncertainty_version="2", residual_bucket=1, uncertainty_scale=15.0)
    return make_observation(observed_at_utc=NOW, kickoff_utc=KICKOFF, game_id="game",
        player_id="player", player_name="Player", team="BUF", opponent="MIA",
        canonical_market="player_pass_yds", line=line, side=side,
        offered_odds=-110, model_score=score,
        reference_context={"reference_book_count": 2,
            "exact_threshold_over_no_vig_probability": .55,
            "books": {name: {"over_odds": -110, "under_odds": -110,
                "exact_threshold_over_no_vig_probability": .55}
                for name in ("draftkings", "fanduel")}},
        freshness={"provider_updated_at_utc": (NOW-timedelta(seconds=30)).isoformat(),
                   "provider_age_seconds": 30, "provider_stale": False,
                   "model_feature_age_seconds": 300, "model_stale": False},
        source_ids={"hardrock": "source"}, context={"capture_slot": slot,
            "capture_target_time_utc": NOW.isoformat()})


def batch_decision(rows=None, config=CONFIG, **kwargs):
    rows = rows or [observation("over"), observation("under")]
    opportunity = build_opportunity(rows, config)
    return rows, decide(opportunity, config, decided_at_utc=NOW, **kwargs).as_row()


def slot(status="completed"):
    return {"event_id": "game", "slot": "90m", "target_time_utc": NOW,
            "status": status, "attempts": 1, "reason": None, "updated_at_utc": NOW}


def counts(store):
    with store.engine.connect() as connection:
        return tuple(connection.scalar(select(func.count()).select_from(table))
                     for table in (observations, opportunity_decisions, scheduler_slots))


def test_atomic_capture_decision_and_idempotent_completed_retry():
    store = ShadowStore("sqlite://")
    rows, decision = batch_decision()
    store.complete_capture_with_decisions(rows, [decision], slot())
    assert counts(store) == (2, 1, 1)
    retry_rows, retry_decision = batch_decision()
    store.complete_capture_with_decisions(retry_rows, [retry_decision], slot())
    assert counts(store) == (2, 1, 1)


def test_one_decision_per_opportunity_and_one_select_per_exposure():
    store = ShadowStore("sqlite://")
    rows, decision = batch_decision()
    store.complete_capture_with_decisions(rows, [decision], slot())
    duplicate = dict(decision, decision_id="different-id")
    with pytest.raises(IntegrityError):
        with store.engine.begin() as connection:
            connection.execute(insert(opportunity_decisions), duplicate)

    later_rows = [observation("over", line=250.5), observation("under", line=250.5)]
    _, later = batch_decision(later_rows, CONFIG)
    with pytest.raises(IntegrityError):
        with store.engine.begin() as connection:
            connection.execute(insert(observations), later_rows)
            connection.execute(insert(opportunity_decisions), later)


def test_invalid_decision_rolls_back_observations_and_slot():
    store = ShadowStore("sqlite://")
    rows, decision = batch_decision()
    decision["intended_stake"] = 11
    with pytest.raises(IntegrityError):
        store.complete_capture_with_decisions(rows, [decision], slot())
    assert counts(store) == (0, 0, 0)


def test_concurrent_selects_for_one_exposure_cannot_both_commit(tmp_path: Path):
    url = f"sqlite:///{tmp_path / 'decisions.db'}"
    store = ShadowStore(url)
    rows, decision = batch_decision()
    with store.engine.begin() as connection:
        connection.execute(insert(observations), rows)
    competing = dict(decision, decision_id="competing", opportunity_id="competing-opportunity")

    def write(payload):
        worker = ShadowStore(url)
        try:
            with worker.engine.begin() as connection:
                connection.execute(insert(opportunity_decisions), payload)
            return "committed"
        except IntegrityError:
            return "rejected"

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(write, (decision, competing)))
    assert sorted(results) == ["committed", "rejected"]
    with store.engine.connect() as connection:
        assert connection.scalar(select(func.count()).select_from(opportunity_decisions)) == 1


def test_pass_is_persisted_but_cannot_carry_selection_or_stake():
    store = ShadowStore("sqlite://")
    rows = [observation("over", slot="15m"), observation("under", slot="15m")]
    rows, decision = batch_decision(rows=rows)
    store.complete_capture_with_decisions(rows, [decision], slot())
    assert counts(store) == (2, 1, 1)
    assert decision["action"] == "PASS" and decision["intended_stake"] is None


def test_no_migration_or_runtime_backfill_entry_point():
    assert not hasattr(ShadowStore, "backfill_decisions")
