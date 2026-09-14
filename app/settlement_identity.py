"""Fail-closed bridge from immutable observation identities to nflverse games."""
from __future__ import annotations

from dataclasses import dataclass
from datetime import timedelta
from typing import Any

import pandas as pd

from app.identities import canonical_team


# Provider timestamps sometimes retain seconds while nflverse schedules are
# minute-granular.  This tolerance only removes that representation difference;
# it is never applied without an exact, explicit two-team match.
KICKOFF_TOLERANCE = timedelta(seconds=59)


def utc_datetime(value: Any) -> pd.Timestamp:
    stamp = pd.Timestamp(value)
    if stamp.tzinfo is None:
        stamp = stamp.tz_localize("UTC")
    return stamp.tz_convert("UTC")


def _parse_identity(value: Any) -> tuple[tuple[str, str], int | None, int | None] | None:
    """Parse established matchup IDs, including nflverse's season/week form.

    Historically this format was produced by ``canonical_event_identity`` and
    therefore sorted its teams rather than guaranteeing away/home orientation.
    The pair is useful as proof, but its position is not treated as venue proof.
    """
    if not isinstance(value, str):
        return None
    parts = value.split(":")
    if parts[0].casefold() != "nfl" or len(parts) not in {3, 5}:
        return None
    if len(parts) == 5:
        try:
            season, week = int(parts[1]), int(parts[2])
        except ValueError:
            return None
        team_parts = parts[3:]
    else:
        season = week = None
        team_parts = parts[1:]
    teams = canonical_team(team_parts[0]), canonical_team(team_parts[1])
    if None in teams or teams[0] == teams[1]:
        return None
    return teams, season, week  # type: ignore[return-value]


def parse_observation_event_id(value: Any) -> tuple[str, str] | None:
    parsed = _parse_identity(value)
    return parsed[0] if parsed else None


@dataclass(frozen=True)
class GameIdentityResolution:
    game: pd.Series | None
    identity_verified: bool
    match_method: str | None
    failure_reason: str | None
    parsed_teams: tuple[str, str] | None


def resolve_settlement_game(observation: dict, schedules: pd.DataFrame) -> GameIdentityResolution:
    """Prove one schedule game from immutable ID/team/kickoff facts or fail closed."""
    event_id = str(observation.get("game_id", ""))
    identity = _parse_identity(event_id)
    parsed = identity[0] if identity else None
    identity_season = identity[1] if identity else None
    identity_week = identity[2] if identity else None
    supplied = tuple(canonical_team(observation.get(key)) for key in ("team", "opponent"))
    supplied_pair = set(supplied) if None not in supplied and supplied[0] != supplied[1] else None
    parsed_pair = set(parsed) if parsed else None
    if supplied_pair and parsed_pair and supplied_pair != parsed_pair:
        return GameIdentityResolution(None, False, None, "conflicting_teams", parsed)
    proven_pair = parsed_pair or supplied_pair

    direct = schedules[schedules.game_id.astype(str) == event_id]
    if len(direct) > 1:
        return GameIdentityResolution(None, False, None, "ambiguous_game_id", parsed)
    if len(direct) == 1:
        possible = direct
        method = "nflverse_game_id_exact"
    else:
        if proven_pair is None:
            return GameIdentityResolution(None, False, None, "teams_not_provable", parsed)
        possible = schedules[schedules.apply(
            lambda row: {str(row.home_team), str(row.away_team)} == proven_pair, axis=1)]
        if identity_season is not None:
            possible = possible[(possible.season.astype(int) == identity_season) &
                                (possible.week.astype(int) == identity_week)]
        method = "canonical_team_pair_and_kickoff"

    if possible.empty:
        return GameIdentityResolution(None, False, None, "no_team_candidate", parsed)
    if proven_pair is None:
        return GameIdentityResolution(None, False, None, "teams_not_provable", parsed)
    possible = possible[possible.apply(
        lambda row: {str(row.home_team), str(row.away_team)} == proven_pair, axis=1)]
    if possible.empty:
        return GameIdentityResolution(None, False, None, "conflicting_teams", parsed)

    try:
        kickoff = utc_datetime(observation["kickoff_utc"])
    except (KeyError, TypeError, ValueError):
        return GameIdentityResolution(None, False, None, "kickoff_invalid", parsed)
    timed = possible[possible.kickoff.map(
        lambda value: abs(utc_datetime(value) - kickoff) <= KICKOFF_TOLERANCE)]
    if timed.empty:
        return GameIdentityResolution(None, False, None, "kickoff_mismatch", parsed)
    if len(timed) != 1:
        return GameIdentityResolution(None, False, None, "ambiguous_candidates", parsed)
    return GameIdentityResolution(timed.iloc[0], True, method, None, parsed)
