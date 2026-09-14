from datetime import datetime, timedelta, timezone

import pandas as pd
import pytest
from sqlalchemy import func, insert, select

from app.research import ResearchRepository
from app.settlement import SettlementCycle
from app.settlement_audit import audit_settlement_identities
from app.settlement_identity import resolve_settlement_game
from app.shadow_storage import (ShadowStore, observations, scheduler_executions,
                                scheduler_slots, settlements)

NOW = datetime(2026, 9, 14, 12, tzinfo=timezone.utc)
# nflverse's 20:00 Eastern kickoff falls after midnight UTC during EDT.
KICKOFF = datetime(2026, 9, 14, 0, tzinfo=timezone.utc)


def observation(identifier="o1", *, market="player_pass_yds", side="over", line=249.5,
                odds=-110, player="p1", game="provider-event", kickoff=KICKOFF):
    return {"observation_id": identifier, "observed_at_utc": kickoff-timedelta(hours=1),
        "kickoff_utc": kickoff, "game_id": game, "player_id": player, "player_name": "Exact Player",
        "team": "BUF", "opponent": "MIA", "canonical_market": market, "line": line, "side": side,
        "hard_rock_offered_odds": odds, "offered_price_break_even_probability": .52,
        "model_probability": .55, "model_version": "fixed", "raw_probability_edge_pp": 3,
        "hypothetical_expected_return_per_dollar": .05, "edge_bucket": "edge_2_to_5pp",
        "feature_built_at_utc": kickoff-timedelta(hours=2), "uncertainty_method": "fixed",
        "uncertainty_version": "v1", "residual_bucket": None, "uncertainty_scale": None,
        "point_prediction": 250, "reference_context": {}, "kalshi_context": {},
        "source_observation_ids": {}, "freshness": {}, "context": {},
        "confirmation_flags": {}, "research_config": {"nominal_unit": 10}}


class FootballOnly:
    def __init__(self, *, final=True, stats=True, weekly=None, game_id="nfl-game"):
        self.calls = []
        self.schedules = pd.DataFrame([{"season": 2026, "week": 1, "game_id": game_id,
            "home_team": "BUF", "away_team": "MIA", "gameday": "2026-09-13",
            "gametime": "20:00", "result": "BUF 27 - MIA 20" if final else None}])
        self.weekly = weekly if weekly is not None else pd.DataFrame([{"season": 2026, "week": 1,
            "game_id": game_id, "player_id": "p1", "player_name": "Exact Player", "team": "BUF",
            "passing_yards": 260 if stats else None, "receiving_yards": 80 if stats else None,
            "receptions": 6 if stats else None}])

    def fetch(self, dataset, season=None, *, refresh=False):
        assert dataset in {"schedules", "weekly_stats"}  # no market provider is reachable here
        self.calls.append((dataset, season, refresh))
        return (self.schedules if dataset == "schedules" else self.weekly).copy()


@pytest.fixture
def store(tmp_path):
    return ShadowStore(f"sqlite:///{tmp_path/'settlement.db'}")


def run(store, client):
    return SettlementCycle(store=store, nflverse=client, now=lambda: NOW).run()


def add(store, *rows):
    for row in rows:
        store.append(row)


def settlement_rows(store):
    with store.engine.connect() as connection:
        return [dict(r._mapping) for r in connection.execute(select(settlements))]


def test_final_available_settles_and_dashboard_reflects_immediately(store):
    add(store, observation())
    report = run(store, FootballOnly())
    assert (report["observations_settled"], report["wins"], report["paper_profit_loss"]) == (1, 1, pytest.approx(9.090909))
    detail = ResearchRepository(engine=store.engine).observation("o1")
    assert detail["settlement_status"] == "settled" and detail["settlement"]["actual_value"] == 260


def test_nonfinal_game_remains_unsettled_and_does_not_fetch_weekly(store):
    add(store, observation())
    client = FootballOnly(final=False)
    report = run(store, client)
    assert report["games_final"] == 0 and not settlement_rows(store)
    assert [call[0] for call in client.calls] == ["schedules"]


def test_final_missing_result_is_retryable_and_never_zero(store):
    add(store, observation())
    report = run(store, FootballOnly(stats=False))
    assert report["games_results_not_available"] == 1 and not settlement_rows(store)
    assert any(error["kind"] == "result_not_available" for error in report["errors"])


@pytest.mark.parametrize("mutation", ["player", "event"])
def test_exact_identity_is_enforced(store, mutation):
    row = observation(player="different") if mutation == "player" else observation(kickoff=KICKOFF+timedelta(minutes=1))
    add(store, row)
    report = run(store, FootballOnly())
    assert not settlement_rows(store)
    expected = "result_not_available" if mutation == "player" else "event_identity_not_exact"
    assert any(e["kind"] == expected for e in report["errors"])


@pytest.mark.parametrize(("market", "actual"), [
    ("player_pass_yds", 260), ("player_reception_yds", 80), ("player_receptions", 6)])
def test_all_canonical_markets(store, market, actual):
    add(store, observation(market=market, line=actual-1))
    report = run(store, FootballOnly())
    assert report["wins"] == 1 and settlement_rows(store)[0]["actual_value"] == actual


@pytest.mark.parametrize(("identifier", "side", "line", "odds", "result", "pnl"), [
    ("win", "over", 250, 150, "win", 15),
    ("loss", "under", 250, -110, "loss", -10),
    ("push", "over", 260, 120, "push", 0),
    ("negative-price-win", "over", 250, -200, "win", 5),
])
def test_threshold_and_american_price_pnl(store, identifier, side, line, odds, result, pnl):
    add(store, observation(identifier, side=side, line=line, odds=odds))
    run(store, FootballOnly())
    row = settlement_rows(store)[0]
    assert row["result"] == result and row["fixed_unit_profit_loss"] == pytest.approx(pnl)


def test_idempotent_rerun_and_primary_key_duplicate_prevention(store):
    add(store, observation())
    first, second = run(store, FootballOnly()), run(store, FootballOnly())
    assert first["observations_settled"] == 1 and second["observations_settled"] == 0
    assert second["skipped_already_settled_rows"] == 1 and len(settlement_rows(store)) == 1
    with pytest.raises(Exception), store.engine.begin() as connection:
        connection.execute(insert(settlements), settlement_rows(store)[0])
    assert len(settlement_rows(store)) == 1


def test_batch_insert_failure_rolls_back_every_settlement(store, monkeypatch):
    add(store, observation("o1"), observation("o2", market="player_receptions", line=5))
    original = store.insert_settlements
    def partial_then_fail(rows, *, connection=None):
        original(rows[:1], connection=connection)
        raise RuntimeError("password=hunter2 https://private.example/payload")
    monkeypatch.setattr(store, "insert_settlements", partial_then_fail)
    report = run(store, FootballOnly())
    assert not settlement_rows(store) and report["settlement_failures"] == 1
    serialized = str(report)
    assert "hunter2" not in serialized and "private.example" not in serialized
    assert "[REDACTED]" in serialized and "[URL REDACTED]" in serialized


def test_groups_downloads_by_season_not_observation(store):
    add(store, observation("o1"), observation("o2", market="player_receptions", line=5))
    client = FootballOnly()
    run(store, client)
    assert client.calls == [("schedules", None, True), ("weekly_stats", 2026, True)]


def normalized_games(*rows):
    from app.historical.normalize import normalize_schedules
    return normalize_schedules(pd.DataFrame(rows))


def schedule(game_id="2026_01_MIA_BUF", *, home="BUF", away="MIA",
             day="2026-09-13", time="20:00"):
    return {"season": 2026, "week": 1, "game_id": game_id, "home_team": home,
            "away_team": away, "gameday": day, "gametime": time, "result": "final"}


def test_current_matchup_identity_bridges_to_nflverse_id_without_observation_team_fields(store):
    row = observation(game="nfl:buf:mia")
    row["team"] = row["opponent"] = None
    add(store, row)
    client = FootballOnly(game_id="2026_01_MIA_BUF")
    report = run(store, client)
    assert report["observations_settled"] == 1
    assert settlement_rows(store)[0]["result_source_id"].split(":")[4] == "2026_01_MIA_BUF"


def test_explicit_abbreviation_aliases_and_second_normalization_resolve():
    games = normalized_games(schedule(home="KC", away="ARI"))
    row = observation(game="nfl:arz:kcc", kickoff=KICKOFF + timedelta(seconds=59))
    row["team"] = row["opponent"] = None
    resolved = resolve_settlement_game(row, games)
    assert resolved.identity_verified and resolved.game.game_id == "2026_01_MIA_BUF"


def test_season_week_canonical_identity_resolves_deterministically():
    games = normalized_games(schedule(), {**schedule("2027_01_MIA_BUF"), "season": 2027})
    row = observation(game="nfl:2026:1:mia:buf")
    row["team"] = row["opponent"] = None
    resolved = resolve_settlement_game(row, games)
    assert resolved.identity_verified and resolved.game.game_id == "2026_01_MIA_BUF"


def test_ambiguous_schedule_candidates_fail_closed():
    games = normalized_games(schedule("one"), schedule("two"))
    result = resolve_settlement_game(observation(game="nfl:buf:mia"), games)
    assert not result.identity_verified and result.failure_reason == "ambiguous_candidates"


@pytest.mark.parametrize(("row", "reason"), [
    (observation(game="nfl:buf:nyj"), "conflicting_teams"),
    (observation(game="nfl:buf:mia", kickoff=KICKOFF + timedelta(minutes=1)), "kickoff_mismatch"),
    (observation(game="nfl:buf:nyj", kickoff=KICKOFF, player="p1"), "conflicting_teams"),
])
def test_identity_bridge_rejects_wrong_matchup_or_kickoff(row, reason):
    games = normalized_games(schedule())
    result = resolve_settlement_game(row, games)
    assert not result.identity_verified and result.failure_reason == reason


def test_no_candidate_fails_closed():
    games = normalized_games(schedule(home="KC", away="DEN"))
    row = observation(game="nfl:buf:mia")
    row["team"] = row["opponent"] = None
    result = resolve_settlement_game(row, games)
    assert not result.identity_verified and result.failure_reason == "no_team_candidate"


def test_read_only_audit_reports_mapping_and_does_not_change_observation(store):
    row = observation(game="nfl:buf:mia")
    row["team"] = row["opponent"] = None
    add(store, row)
    before = store.all()
    audit = audit_settlement_identities(store=store, nflverse=FootballOnly(
        game_id="2026_01_MIA_BUF"), now=NOW)
    assert audit["read_only"] and audit["total_unsettled_observations"] == 1
    assert audit["events"][0]["candidate_nflverse_game_id"] == "2026_01_MIA_BUF"
    assert audit["events"][0]["identity_verified"] is True
    assert store.all() == before and settlement_rows(store) == []


def test_read_only_audit_recovers_opaque_provider_id_only_from_persisted_scheduler_evidence(store):
    opaque = "17885cb8dcade8f6c3bce14b2de805e8"
    row = observation(game=opaque)
    row["team"] = row["opponent"] = None
    row["source_observation_ids"] = {"hardrock": f"{opaque}:hardrockbet:player_pass_yds:Safe Player"}
    add(store, row)
    details = {"completed": [{"event_id": opaque, "kickoff_utc": KICKOFF.isoformat(),
        "slot": "90m", "target_time_utc": (KICKOFF-timedelta(minutes=90)).isoformat()}],
        "rejected_props": [{"provider_event_id": opaque, "canonical_event": "nfl:buf:mia"}]}
    with store.engine.begin() as connection:
        connection.execute(insert(scheduler_slots), {"event_id": opaque, "slot": "90m",
            "target_time_utc": KICKOFF-timedelta(minutes=90), "status": "completed", "attempts": 1,
            "reason": None, "updated_at_utc": NOW})
        connection.execute(insert(scheduler_executions), {"execution_id": "execution-1",
            "started_at_utc": NOW, "ended_at_utc": NOW, "details": details})
    before = store.all()
    audit = audit_settlement_identities(store=store, nflverse=FootballOnly(
        game_id="2026_01_MIA_BUF"), now=NOW)
    event = audit["events"][0]
    assert event["provider_source_event_ids"] == [opaque]
    assert event["persisted_canonical_events"] == ["nfl:buf:mia"]
    assert event["candidate_nflverse_game_id"] == "2026_01_MIA_BUF"
    assert event["match_method"] == "persisted_scheduler_canonical_event_and_kickoff"
    assert event["identity_verified"] is True and len(event["evidence_chain"]) == 4
    assert store.all() == before and settlement_rows(store) == []


def test_read_only_audit_does_not_recover_opaque_id_from_kickoff_slot_or_source_id_alone(store):
    opaque = "1edfa5ceaa1ad2cb57df1c1b908731f6"
    row = observation(game=opaque)
    row["team"] = row["opponent"] = None
    row["source_observation_ids"] = {"hardrock": f"{opaque}:hardrockbet:market:Player"}
    add(store, row)
    with store.engine.begin() as connection:
        connection.execute(insert(scheduler_slots), {"event_id": opaque, "slot": "90m",
            "target_time_utc": KICKOFF-timedelta(minutes=90), "status": "completed", "attempts": 1,
            "reason": None, "updated_at_utc": NOW})
    audit = audit_settlement_identities(store=store, nflverse=FootballOnly(), now=NOW)
    event = audit["events"][0]
    assert event["provider_source_event_ids"] == [opaque] and event["capture_slots"]
    assert event["identity_verified"] is False
    assert event["failure_reason"] == "persisted_team_identity_not_found"
