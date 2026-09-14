"""Descriptive player-game history for research and future feature discovery only."""
from __future__ import annotations

from statistics import mean, median, stdev
from typing import Any, Callable, Iterable

import pandas as pd

from app.historical.normalize import normalize_schedules, normalize_weekly

STAT_COLUMNS = ("passing_yards", "receiving_yards", "receptions", "targets", "snap_share",
                "target_share", "team_pass_attempts", "attempts", "dropbacks")
SMALL_SAMPLE_N = 10


def build_player_history(weekly: pd.DataFrame, schedules: pd.DataFrame, *,
                         contexts: pd.DataFrame | None = None) -> pd.DataFrame:
    """Return one stable-ID player/game row, preserving unavailable values."""
    stats, games = normalize_weekly(weekly), normalize_schedules(schedules)
    schedule_columns = [c for c in ("season", "week", "game_id", "kickoff", "gameday", "home_team",
                                    "away_team", "home_score", "away_score", "result", "spread_line",
                                    "roof", "wind", "temp") if c in games]
    # Weekly game IDs are preferred.  Season/week/team is an exact fallback for
    # old releases; no player-name matching is used.
    if "game_id" in stats and stats.game_id.notna().any():
        joined = stats.merge(games[schedule_columns], on=["season", "week", "game_id"], how="inner")
    else:
        joined = stats.merge(games[schedule_columns], on=["season", "week"], how="inner")
        joined = joined[(joined.team == joined.home_team) | (joined.team == joined.away_team)]
    joined["opponent"] = joined.apply(lambda r: r.away_team if r.team == r.home_team else r.home_team, axis=1)
    joined["home_away"] = joined.apply(lambda r: "home" if r.team == r.home_team else "away", axis=1)
    joined["date"] = pd.to_datetime(joined.get("gameday", joined["kickoff"])).dt.date
    attempts = pd.to_numeric(joined.get("attempts"), errors="coerce") if "attempts" in joined else pd.Series(pd.NA, index=joined.index)
    if "team_pass_attempts" not in joined:
        joined["team_pass_attempts"] = attempts.groupby([joined.game_id, joined.team]).transform("sum")
    if "target_share" not in joined and "targets" in joined:
        targets = pd.to_numeric(joined.targets, errors="coerce")
        joined["target_share"] = targets / targets.groupby([joined.game_id, joined.team]).transform("sum").replace(0, pd.NA)
    if "dropbacks" not in joined:
        joined["dropbacks"] = attempts + pd.to_numeric(joined.get("sacks", 0), errors="coerce")
    joined["favorite_underdog"] = joined.apply(_favorite, axis=1)
    joined["venue_type"] = joined.get("roof", pd.Series(pd.NA, index=joined.index)).map(_venue)
    joined["weather_status"] = joined.apply(_weather, axis=1)
    if contexts is not None and not contexts.empty:
        keys = [c for c in ("player_id", "game_id") if c in contexts]
        joined = joined.merge(contexts, on=keys, how="left", suffixes=("", "_context"))
    required = ["player_id", "season", "week", "date", "game_id", "player_name", "team", "opponent",
                "home_away", *STAT_COLUMNS, "favorite_underdog", "venue_type", "weather_status"]
    for column in required:
        if column not in joined:
            joined[column] = pd.NA
    optional = [c for c in ("home_score", "away_score", "result", "spread_line", "injury_status",
                            "role_status", "weather", "injuries", "role") if c in joined]
    return joined[required + optional].drop_duplicates(["player_id", "game_id"]).sort_values(
        ["player_id", "season", "week"]).reset_index(drop=True)


def _favorite(row: pd.Series) -> str | None:
    if "spread_line" not in row or pd.isna(row.get("spread_line")):
        return None
    # nflverse spread_line is conventionally from the home-team perspective.
    home_favorite = float(row.spread_line) < 0
    return "favorite" if (row.team == row.home_team) == home_favorite else "underdog"


def _venue(value: Any) -> str | None:
    if pd.isna(value):
        return None
    return "indoor" if str(value).lower() in {"dome", "closed", "indoor", "closed_roof"} else "outdoor"


def _weather(row: pd.Series) -> str | None:
    if _venue(row.get("roof")) == "indoor":
        return "indoor"
    wind = pd.to_numeric(pd.Series([row.get("wind")]), errors="coerce").iloc[0]
    return None if pd.isna(wind) else "high_wind" if wind >= 20 else "neutral"


def split_statistics(records: pd.DataFrame | Iterable[dict], *, statistic: str, threshold: float,
                     side: str = "over", predicate: Callable[[dict], bool] | None = None,
                     last_n: int | None = None, label: str = "all", small_sample_n: int = SMALL_SAMPLE_N) -> dict:
    """Summarize a contextual split; hit rate is explicitly not probability."""
    rows = records.to_dict("records") if isinstance(records, pd.DataFrame) else list(records)
    rows = [row for row in rows if predicate is None or predicate(row)]
    rows.sort(key=lambda row: (row.get("season", 0), row.get("week", 0), str(row.get("date", ""))))
    if last_n is not None:
        if last_n < 1:
            raise ValueError("last_n must be positive")
        rows = rows[-last_n:]
    values = [float(row[statistic]) for row in rows if row.get(statistic) is not None and not pd.isna(row[statistic])]
    hits = sum(value > threshold if side == "over" else value < threshold for value in values)
    n = len(values)
    return {"split": label, "statistic": statistic, "side": side, "threshold": float(threshold),
            "sample_size": n, "hits": hits, "hit_rate": hits / n if n else None,
            "mean_actual": mean(values) if values else None, "median_actual": median(values) if values else None,
            "standard_deviation": stdev(values) if n > 1 else None,
            "small_sample": n < small_sample_n,
            "warning": (f"Very small sample (N={n}); descriptive hit rate is not a model probability."
                        if n < small_sample_n else "Descriptive historical rate; not a model probability.")}


def contextual_splits(records: pd.DataFrame, *, statistic: str, threshold: float, side: str = "over",
                      last_n: int | None = None, opponent: str | None = None,
                      flag: str | None = None) -> list[dict]:
    """Standard home/away, spread, venue, weather, role/injury and history splits."""
    specs: list[tuple[str, Callable[[dict], bool]]] = []
    for field, values in (("home_away", ("home", "away")), ("favorite_underdog", ("favorite", "underdog")),
                          ("venue_type", ("indoor", "outdoor")), ("weather_status", ("high_wind", "neutral"))):
        specs.extend((f"{field}:{value}", lambda row, f=field, v=value: row.get(f) == v) for value in values)
    if opponent:
        specs.append((f"opponent:{opponent}", lambda row: row.get("opponent") == opponent))
    if flag:
        specs.extend(((f"with:{flag}", lambda row: bool(row.get(flag))),
                      (f"without:{flag}", lambda row: not bool(row.get(flag)))))
    results = [split_statistics(records, statistic=statistic, threshold=threshold, side=side,
                                predicate=predicate, label=label) for label, predicate in specs]
    results.append(split_statistics(records, statistic=statistic, threshold=threshold, side=side,
                                    last_n=last_n, label=f"last_{last_n}" if last_n else "season_to_date"))
    return results
