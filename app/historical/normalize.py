"""Normalize nflverse frames into stable project identities."""

from __future__ import annotations

import pandas as pd

from app.identities import canonical_team, normalize_player_name


TEAM_RENAMES = {"LA": "LAR", "OAK": "LV", "SD": "LAC", "STL": "LAR"}
NFLVERSE_SCHEDULE_TIMEZONE = "America/New_York"


def normalize_weekly(weekly: pd.DataFrame) -> pd.DataFrame:
    """Retain nflverse IDs and normalize only explicit names/team aliases."""
    result = weekly.copy()
    if "player_id" not in result and "gsis_id" in result:
        result["player_id"] = result["gsis_id"]
    if "player_name" not in result and "player_display_name" in result:
        result["player_name"] = result["player_display_name"]
    if "team" not in result and "recent_team" in result:
        result["team"] = result["recent_team"]
    missing = [column for column in ("player_id", "player_name", "season", "week", "team") if column not in result]
    if missing:
        raise ValueError(f"weekly stats missing canonical keys: {missing}")
    if result["player_id"].isna().any():
        unidentified = result[result["player_id"].isna()]
        material = pd.Series(False, index=unidentified.index)
        for column in ("attempts", "targets", "receptions", "passing_yards", "receiving_yards"):
            if column in unidentified:
                material |= pd.to_numeric(unidentified[column], errors="coerce").fillna(0).ne(0)
        if material.any():
            raise ValueError("stable nflverse player_id is required for every model-relevant weekly row")
        # nflverse includes non-player/team aggregate placeholders with no ID and
        # no passing/receiving participation. They cannot create a market row.
        result = result[result["player_id"].notna()].copy()
    result["player_name_normalized"] = result["player_name"].map(normalize_player_name)
    result["team"] = result["team"].replace(TEAM_RENAMES).map(canonical_team)
    if result["team"].isna().any():
        raise ValueError("unknown team in weekly statistics")
    return result


def normalize_schedules(schedules: pd.DataFrame) -> pd.DataFrame:
    required = {"season", "week", "game_id", "home_team", "away_team", "gameday"}
    missing = required - set(schedules)
    if missing:
        raise ValueError(f"schedules missing columns: {sorted(missing)}")
    games = schedules.copy()
    games["home_team"] = games["home_team"].replace(TEAM_RENAMES).map(canonical_team)
    games["away_team"] = games["away_team"].replace(TEAM_RENAMES).map(canonical_team)
    if "gametime" not in games:
        games["gametime"] = "00:00"
    # nflverse's ``gameday`` and ``gametime`` fields are US Eastern wall-clock
    # values, not UTC values.  Localize first so the IANA timezone database
    # applies the correct EST/EDT offset for the date, then convert to UTC.
    # Passing utc=True directly to the naive combined value merely labels the
    # Eastern clock reading as UTC and moves every kickoff four/five hours early.
    local_kickoff = pd.to_datetime(
        games["gameday"].astype(str) + " " + games["gametime"].fillna("00:00").astype(str),
        errors="raise",
    )
    games["kickoff"] = local_kickoff.dt.tz_localize(
        NFLVERSE_SCHEDULE_TIMEZONE, ambiguous="raise", nonexistent="raise"
    ).dt.tz_convert("UTC")
    games["canonical_event_id"] = games.apply(
        lambda row: f"nfl:{int(row.season)}:{int(row.week)}:{row.away_team}:{row.home_team}", axis=1
    )
    return games
