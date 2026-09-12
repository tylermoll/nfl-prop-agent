"""Leakage-safe football feature engineering.

All observations are sorted by kickoff. Every realized/usage series is shifted
one game before expanding or rolling calculations. Consequently the target
row can never contribute to its own features.
"""

from __future__ import annotations

import numpy as np
import pandas as pd

from .normalize import normalize_schedules, normalize_weekly
from .providers import join_asof_pregame

MARKETS = {
    "player_pass_yds": "passing_yards",
    "player_reception_yds": "receiving_yards",
    "player_receptions": "receptions",
}
USAGE = ("attempts", "dropbacks", "targets", "receptions", "passing_yards", "receiving_yards", "snap_share", "target_share", "air_yards_share", "team_pass_attempts")


def _number(frame: pd.DataFrame, name: str, default: float = 0.0) -> pd.Series:
    return pd.to_numeric(frame[name], errors="coerce") if name in frame else pd.Series(default, index=frame.index, dtype=float)


def _lag_features(rows: pd.DataFrame) -> pd.DataFrame:
    keys = ["player_id", "canonical_market"]
    grouped = rows.groupby(keys, sort=False, dropna=False)
    lagged_target = grouped["actual_value"].shift(1)
    rows["previous_game_value"] = lagged_target
    for window in (3, 5, 8):
        rows[f"rolling_{window}_mean"] = lagged_target.groupby([rows[k] for k in keys]).transform(
            lambda series: series.rolling(window, min_periods=1).mean())
        rows[f"rolling_{window}_std"] = lagged_target.groupby([rows[k] for k in keys]).transform(
            lambda series: series.rolling(window, min_periods=2).std())
    season_keys = ["player_id", "canonical_market", "season"]
    # Unlike the rolling features, season-to-date is based on a shift within
    # the season.  Shifting before adding season to the grouping keys would
    # incorrectly make Week 1 inherit the previous season's final game.
    season_lag = rows.groupby(season_keys, sort=False, dropna=False)["actual_value"].shift(1)
    season_groups = [rows[k] for k in season_keys]
    rows["season_to_date_mean"] = season_lag.groupby(season_groups).transform(lambda s: s.expanding(1).mean())
    rows["season_to_date_std"] = season_lag.groupby(season_groups).transform(lambda s: s.expanding(2).std())
    return rows


def _usage_features(rows: pd.DataFrame) -> pd.DataFrame:
    group_keys = [rows["player_id"], rows["canonical_market"]]
    for metric in USAGE:
        current = _number(rows, metric, np.nan)
        lag = current.groupby(group_keys).shift(1)
        rows[f"pregame_{metric}"] = lag
        rows[f"{metric}_rolling_3"] = lag.groupby(group_keys).transform(lambda s: s.rolling(3, min_periods=1).mean())
    for metric in ("snap_share", "target_share"):
        rows[f"{metric}_delta_prior"] = rows[f"pregame_{metric}"] - _number(rows, metric, np.nan).groupby(group_keys).shift(2)
        prior_rolling = _number(rows, metric, np.nan).groupby(group_keys).shift(2).groupby(group_keys).transform(
            lambda s: s.rolling(3, min_periods=1).mean())
        rows[f"{metric}_delta_rolling_3"] = rows[f"pregame_{metric}"] - prior_rolling
    lag_targets = rows["pregame_targets"]
    lag_volume = rows["pregame_dropbacks"].fillna(rows["pregame_attempts"])
    rows["targets_per_team_pass"] = lag_targets / lag_volume.replace(0, np.nan)
    rolling_short = lag_targets.groupby(group_keys).transform(lambda s: s.rolling(2, min_periods=1).mean())
    rolling_long = lag_targets.groupby(group_keys).transform(lambda s: s.rolling(5, min_periods=1).mean())
    rows["usage_trend"] = rolling_short - rolling_long
    rows["usage_acceleration"] = rows["usage_trend"] - rows["usage_trend"].groupby(group_keys).shift(1)
    attempts = rows["pregame_attempts"]
    rows["yards_per_attempt"] = rows["pregame_passing_yards"] / attempts.replace(0, np.nan)
    rows["yards_per_target"] = rows["pregame_receiving_yards"] / lag_targets.replace(0, np.nan)
    rows["catch_rate"] = rows["pregame_receptions"] / lag_targets.replace(0, np.nan)
    return rows


def _opponent_features(rows: pd.DataFrame) -> pd.DataFrame:
    # One offensive-team/game row. The opponent allowed these values.
    observations = rows.drop_duplicates(["game_id", "player_id"])
    game_team = observations.groupby(["game_id", "kickoff", "team", "opponent"], as_index=False).agg(
        pass_allowed_value=("passing_yards", "sum"),
        receiving_allowed_value=("receiving_yards", "sum"),
    ).rename(columns={"team": "offense", "opponent": "defense"})
    game_team = game_team.sort_values("kickoff")
    for value, output in (("pass_allowed_value", "opponent_recent_passing_allowed"),
                          ("receiving_allowed_value", "opponent_recent_receiving_allowed")):
        game_team[output] = game_team.groupby("defense")[value].transform(
            lambda s: s.shift(1).rolling(3, min_periods=1).mean())
    lookup = game_team[["game_id", "offense", "opponent_recent_passing_allowed", "opponent_recent_receiving_allowed"]]
    return rows.merge(lookup, left_on=["game_id", "team"], right_on=["game_id", "offense"], how="left").drop(columns="offense")


def build_modeling_table(weekly: pd.DataFrame, schedules: pd.DataFrame, *, snaps: pd.DataFrame | None = None,
                         injuries: pd.DataFrame | None = None, depth_charts: pd.DataFrame | None = None,
                         current_row_column: str | None = None) -> pd.DataFrame:
    """Build one player/game/market row using only observations before kickoff."""
    stats, games = normalize_weekly(weekly), normalize_schedules(schedules)
    home = games[["season", "week", "game_id", "kickoff", "canonical_event_id", "home_team", "away_team"]].copy()
    # Current nflverse weekly releases include their own game_id. Schedule IDs
    # remain authoritative here; avoid pandas suffixes while preserving the
    # established season/week/team join semantics.
    joined = stats.drop(columns="game_id", errors="ignore").merge(home, on=["season", "week"], how="inner")
    joined = joined[(joined.team == joined.home_team) | (joined.team == joined.away_team)].copy()
    joined["opponent"] = np.where(joined.team == joined.home_team, joined.away_team, joined.home_team)
    joined["home_away"] = np.where(joined.team == joined.home_team, "home", "away")
    joined["is_home"] = (joined["home_away"] == "home").astype(float)
    had_dropbacks = "dropbacks" in joined
    for column in set(MARKETS.values()) | set(USAGE):
        joined[column] = _number(joined, column)
    if not had_dropbacks:
        joined["dropbacks"] = joined["attempts"] + _number(joined, "sacks")
    team_attempts = joined.groupby(["game_id", "team"])["attempts"].transform("sum")
    team_targets = joined.groupby(["game_id", "team"])["targets"].transform("sum")
    joined["team_pass_attempts"] = team_attempts
    joined["target_share"] = np.where(team_targets > 0, joined.targets / team_targets, np.nan)
    if "receiving_air_yards" in joined:
        air = pd.to_numeric(joined["receiving_air_yards"], errors="coerce")
        team_air = air.groupby([joined.game_id, joined.team]).transform("sum")
        joined["air_yards_share"] = air / team_air.replace(0, np.nan)
    if snaps is not None and not snaps.empty:
        snap_cols = [c for c in ("player_id", "season", "week", "offense_pct", "snap_share") if c in snaps]
        snap_data = snaps[snap_cols].copy()
        if "snap_share" not in snap_data:
            raw_pct = snap_data["offense_pct"]
            parsed = pd.to_numeric(raw_pct.astype(str).str.rstrip("%"), errors="coerce")
            snap_data["snap_share"] = np.where(raw_pct.astype(str).str.contains("%"), parsed / 100, parsed)
        joined = joined.drop(columns="snap_share", errors="ignore").merge(snap_data[["player_id", "season", "week", "snap_share"]], on=["player_id", "season", "week"], how="left")
    joined = joined.sort_values(["kickoff", "game_id", "player_id"])
    # Rest uses the team's preceding scheduled appearance, never a later game.
    team_games = pd.concat([games[["game_id", "kickoff", "home_team"]].rename(columns={"home_team": "team"}),
                            games[["game_id", "kickoff", "away_team"]].rename(columns={"away_team": "team"})]).sort_values("kickoff")
    # Training defines rest within an NFL season; an offseason is not a
    # meaningful rest interval and must not become a live-only value.
    team_games = team_games.merge(games[["game_id", "season"]], on="game_id", how="left")
    team_games["days_rest"] = team_games.groupby(["team", "season"])["kickoff"].diff().dt.total_seconds().div(86400)
    joined = joined.merge(team_games[["game_id", "team", "days_rest"]], on=["game_id", "team"], how="left")
    rows = pd.concat([joined.assign(canonical_market=market, actual_value=joined[target]) for market, target in MARKETS.items()], ignore_index=True)
    applicable = ((rows.canonical_market == "player_pass_yds") & (rows.attempts > 0)) | ((rows.canonical_market != "player_pass_yds") & ((rows.targets > 0) | (rows.receptions > 0)))
    # A current builder may append outcome-free target rows.  They must pass
    # through the *same* transforms as benchmark rows, but are not made
    # applicable merely by fabricated current-game usage.
    if current_row_column and current_row_column in rows:
        applicable |= rows[current_row_column].fillna(False).astype(bool)
    rows = rows[applicable].sort_values(["kickoff", "game_id", "player_id", "canonical_market"]).reset_index(drop=True)
    rows = _lag_features(rows)
    rows = _usage_features(rows)
    rows = _opponent_features(rows)
    if injuries is not None:
        rows = join_asof_pregame(rows, injuries, by=["player_id"], timestamp="observed_at")
    if depth_charts is not None:
        rows = join_asof_pregame(rows, depth_charts, by=["player_id"], timestamp="observed_at")
        if "depth_rank" in rows:
            keys = [rows.player_id, rows.canonical_market]
            rows["depth_chart_movement"] = rows.groupby(["player_id", "canonical_market"])["depth_rank"].diff()
    required = ["season", "week", "game_id", "canonical_event_id", "player_id", "player_name", "team", "opponent", "home_away", "canonical_market", "actual_value"]
    return rows[required + [column for column in rows if column not in required and column not in {"gameday", "gametime"}]]
