"""Read-only pre-settlement identity audit."""
from __future__ import annotations

from collections import defaultdict
from datetime import datetime, timezone
from typing import Any

from app.historical.normalize import normalize_schedules
from app.settlement_identity import resolve_settlement_game, utc_datetime


def audit_settlement_identities(*, store, nflverse, refresh: bool = True,
                                now: datetime | None = None) -> dict[str, Any]:
    """Inspect eligible unsettled rows without opening a write transaction."""
    inspected_at = (now or datetime.now(timezone.utc)).astimezone(timezone.utc)
    observations, _ = store.settlement_candidates(inspected_at)
    schedules = normalize_schedules(nflverse.fetch("schedules", refresh=refresh))
    groups: dict[tuple[str, str], list[dict]] = defaultdict(list)
    for observation in observations:
        groups[(str(observation["game_id"]), utc_datetime(
            observation["kickoff_utc"]).isoformat())].append(observation)

    events = []
    for (event_id, kickoff), rows in sorted(groups.items()):
        resolutions = [resolve_settlement_game(row, schedules) for row in rows]
        verified_ids = {str(item.game.game_id) for item in resolutions
                        if item.identity_verified and item.game is not None}
        all_verified = all(item.identity_verified for item in resolutions) and len(verified_ids) == 1
        resolution = resolutions[0]
        game = resolution.game if all_verified else None
        parsed = resolution.parsed_teams
        failure = None if all_verified else next(
            (item.failure_reason for item in resolutions if item.failure_reason),
            "inconsistent_observation_facts",
        )
        events.append({
            "observation_event_identity": event_id,
            # These are the two syntactic positions in the deployed identity.
            # See match_method: legacy IDs were orientation-independent, so the
            # verified nflverse fields below are authoritative for venue.
            "parsed_away_team": parsed[0] if parsed else None,
            "parsed_home_team": parsed[1] if parsed else None,
            "observation_kickoff_utc": kickoff,
            "candidate_nflverse_game_id": str(game.game_id) if game is not None else None,
            "nflverse_away_team": str(game.away_team) if game is not None else None,
            "nflverse_home_team": str(game.home_team) if game is not None else None,
            "nflverse_kickoff_utc": utc_datetime(game.kickoff).isoformat() if game is not None else None,
            "season": int(game.season) if game is not None else None,
            "week": int(game.week) if game is not None else None,
            "match_method": resolution.match_method if all_verified else None,
            "identity_verified": all_verified,
            "failure_reason": failure,
            "observation_count": len(rows),
        })
    return {
        "read_only": True,
        "inspected_at_utc": inspected_at.isoformat(),
        "total_unsettled_observations": len(observations),
        "unique_observation_event_identities": len({row["game_id"] for row in observations}),
        "event_kickoff_groups": len(events),
        "verified_event_kickoff_groups": sum(item["identity_verified"] for item in events),
        "events": events,
    }
