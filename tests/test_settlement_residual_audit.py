from __future__ import annotations

import pandas as pd

from app.settlement_residual_audit import _apply_complete_kickoff_residual_bijections


KICKOFF = "2026-09-13T20:25:00+00:00"


def schedule_rows() -> pd.DataFrame:
    return pd.DataFrame([
        {"season": 2026, "week": 1, "game_id": "2026_01_ARI_LAC",
         "away_team": "ari", "home_team": "lac", "kickoff": pd.Timestamp(KICKOFF)},
        {"season": 2026, "week": 1, "game_id": "2026_01_MIA_LV",
         "away_team": "mia", "home_team": "lv", "kickoff": pd.Timestamp(KICKOFF)},
        {"season": 2026, "week": 1, "game_id": "2026_01_WAS_PHI",
         "away_team": "was", "home_team": "phi", "kickoff": pd.Timestamp(KICKOFF)},
        {"season": 2026, "week": 1, "game_id": "2026_01_GB_MIN",
         "away_team": "gb", "home_team": "min", "kickoff": pd.Timestamp(KICKOFF)},
    ])


def verified(event_id: str, game_id: str) -> dict:
    return {"observation_event_identity": event_id,
            "observation_kickoff_utc": KICKOFF,
            "candidate_nflverse_game_id": game_id,
            "identity_verified": True,
            "failure_reason": None,
            "match_method": "persisted_scheduler_canonical_event_and_kickoff"}


def unresolved(event_id: str) -> dict:
    return {"observation_event_identity": event_id,
            "observation_kickoff_utc": KICKOFF,
            "candidate_nflverse_game_id": None,
            "identity_verified": False,
            "failure_reason": "persisted_team_identity_not_found",
            "match_method": None,
            "evidence_chain": []}


def test_complete_kickoff_slate_resolves_single_residual_event():
    audit = {"events": [
        verified("provider-a", "2026_01_ARI_LAC"),
        verified("provider-b", "2026_01_MIA_LV"),
        verified("provider-c", "2026_01_WAS_PHI"),
        unresolved("provider-d"),
    ], "verified_event_kickoff_groups": 3}

    _apply_complete_kickoff_residual_bijections(audit, schedule_rows())

    event = audit["events"][-1]
    assert event["identity_verified"] is True
    assert event["candidate_nflverse_game_id"] == "2026_01_GB_MIN"
    assert event["nflverse_away_team"] == "gb"
    assert event["nflverse_home_team"] == "min"
    assert event["match_method"] == "complete_kickoff_slate_residual_bijection"
    assert event["failure_reason"] is None
    assert event["residual_bijection"] == {
        "provider_event_groups_at_kickoff": 4,
        "nflverse_games_at_kickoff": 4,
        "independently_verified_groups": 3,
        "remaining_provider_groups": 1,
        "remaining_nflverse_games": 1,
    }
    assert audit["verified_event_kickoff_groups"] == 4
    assert audit["residual_bijection_groups"] == 1


def test_single_event_at_kickoff_is_not_treated_as_residual_proof():
    audit = {"events": [unresolved("provider-only")],
             "verified_event_kickoff_groups": 0}
    one_game = schedule_rows().iloc[[0]].copy()

    _apply_complete_kickoff_residual_bijections(audit, one_game)

    event = audit["events"][0]
    assert event["identity_verified"] is False
    assert event["failure_reason"] == "persisted_team_identity_not_found"
    assert audit["verified_event_kickoff_groups"] == 0
    assert audit["residual_bijection_groups"] == 0


def test_incomplete_provider_slate_does_not_resolve_by_elimination():
    audit = {"events": [
        verified("provider-a", "2026_01_ARI_LAC"),
        verified("provider-b", "2026_01_MIA_LV"),
        unresolved("provider-c"),
    ], "verified_event_kickoff_groups": 2}

    _apply_complete_kickoff_residual_bijections(audit, schedule_rows())

    assert audit["events"][-1]["identity_verified"] is False
    assert audit["verified_event_kickoff_groups"] == 2
    assert audit["residual_bijection_groups"] == 0


def test_multiple_unresolved_events_do_not_resolve_by_elimination():
    audit = {"events": [
        verified("provider-a", "2026_01_ARI_LAC"),
        verified("provider-b", "2026_01_MIA_LV"),
        unresolved("provider-c"),
        unresolved("provider-d"),
    ], "verified_event_kickoff_groups": 2}

    _apply_complete_kickoff_residual_bijections(audit, schedule_rows())

    assert all(not event["identity_verified"] for event in audit["events"][2:])
    assert audit["verified_event_kickoff_groups"] == 2
    assert audit["residual_bijection_groups"] == 0
