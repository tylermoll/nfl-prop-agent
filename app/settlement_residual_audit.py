"""Read-only residual settlement identity recovery for legacy opaque provider event IDs.

This module never writes observations or settlements. It only upgrades an existing
settlement identity audit when a legacy opaque event can be proven by a complete
one-to-one kickoff-slate residual mapping.
"""
from __future__ import annotations

from collections import defaultdict
from datetime import datetime
from typing import Any

import pandas as pd

from app.historical.normalize import normalize_schedules
from app.settlement_audit import audit_settlement_identities
from app.settlement_identity import KICKOFF_TOLERANCE, utc_datetime


def _games_at_kickoff(schedules: pd.DataFrame, kickoff: str) -> pd.DataFrame:
    try:
        target = utc_datetime(kickoff)
    except (TypeError, ValueError):
        return schedules.iloc[0:0]
    return schedules[schedules.kickoff.map(
        lambda value: abs(utc_datetime(value) - target) <= KICKOFF_TOLERANCE)]


def _apply_complete_kickoff_residual_bijections(audit: dict[str, Any],
                                                 schedules: pd.DataFrame) -> None:
    """Resolve exactly one legacy gap from an otherwise fully proven kickoff slate.

    This deliberately does not use kickoff alone. A residual mapping is accepted only
    when all of the following are true for one kickoff instant:

    * there are at least two persisted provider-event groups at that kickoff;
    * the count of persisted groups exactly equals the nflverse game count;
    * every group except one is already independently identity-verified;
    * those verified groups map to distinct nflverse game IDs; and
    * exactly one nflverse game remains unmatched.

    If any condition fails, the original fail-closed audit result is preserved.
    """
    by_kickoff: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for event in audit.get("events", []):
        kickoff = event.get("observation_kickoff_utc")
        if isinstance(kickoff, str):
            by_kickoff[kickoff].append(event)

    for kickoff, group in by_kickoff.items():
        if len(group) < 2:
            continue
        games = _games_at_kickoff(schedules, kickoff)
        if len(games) != len(group):
            continue

        verified = [event for event in group if event.get("identity_verified")]
        unresolved = [event for event in group
                      if not event.get("identity_verified") and
                      event.get("failure_reason") == "persisted_team_identity_not_found"]
        if len(unresolved) != 1 or len(verified) != len(group) - 1:
            continue

        claimed = [str(event.get("candidate_nflverse_game_id")) for event in verified
                   if event.get("candidate_nflverse_game_id")]
        if len(claimed) != len(verified) or len(set(claimed)) != len(claimed):
            continue

        remaining = games[~games.game_id.astype(str).isin(set(claimed))]
        if len(remaining) != 1:
            continue

        candidate = remaining.iloc[0]
        event = unresolved[0]
        event.update({
            "candidate_nflverse_game_id": str(candidate.game_id),
            "nflverse_away_team": str(candidate.away_team),
            "nflverse_home_team": str(candidate.home_team),
            "nflverse_kickoff_utc": utc_datetime(candidate.kickoff).isoformat(),
            "season": int(candidate.season),
            "week": int(candidate.week),
            "match_method": "complete_kickoff_slate_residual_bijection",
            "evidence_chain": [
                "persisted provider_event_id defines one distinct NFL observation group at this kickoff",
                "nflverse contains the same total number of NFL games at this kickoff as persisted provider-event groups",
                "every other provider-event group at this kickoff is independently verified to a distinct nflverse game",
                "exactly one unmatched provider-event group and one unmatched nflverse game remain",
            ],
            "identity_verified": True,
            "failure_reason": None,
            "residual_bijection": {
                "provider_event_groups_at_kickoff": len(group),
                "nflverse_games_at_kickoff": len(games),
                "independently_verified_groups": len(verified),
                "remaining_provider_groups": 1,
                "remaining_nflverse_games": 1,
            },
        })

    audit["verified_event_kickoff_groups"] = sum(
        bool(event.get("identity_verified")) for event in audit.get("events", []))
    audit["residual_bijection_groups"] = sum(
        event.get("match_method") == "complete_kickoff_slate_residual_bijection"
        for event in audit.get("events", []))


def audit_settlement_identities_with_residual_bijection(*, store, nflverse,
                                                         refresh: bool = True,
                                                         now: datetime | None = None) -> dict[str, Any]:
    """Run the existing read-only audit, then apply strict slate residual proof."""
    audit = audit_settlement_identities(
        store=store, nflverse=nflverse, refresh=refresh, now=now)
    # The base audit has just fetched schedules. Re-read the local/cache view without
    # requesting a refresh so this enhancement does not add another network refresh.
    schedules = normalize_schedules(nflverse.fetch("schedules", refresh=False))
    _apply_complete_kickoff_residual_bijections(audit, schedules)
    return audit
