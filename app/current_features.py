"""Build the local, pregame-only feature cache consumed by production."""
from __future__ import annotations

import os
import tempfile
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import pandas as pd

from app.historical.features import MARKETS, build_modeling_table
from app.historical.ingestion import NflverseClient
from app.historical.normalize import normalize_schedules, normalize_weekly
from app.identities import canonical_team
from app.production_pipeline import ProductionModels, load_production_models

IDENTITY = ["player_id", "player_name", "home_team", "away_team", "kickoff",
            "canonical_market", "feature_built_at_utc", "feature_data_as_of_utc"]


@dataclass
class BuildReport:
    source_retrieval_timestamp: str
    source_cutoff_timestamp: str | None = None
    upcoming_games_found: int = 0
    player_rows_by_market: dict[str, int] = field(default_factory=dict)
    excluded_players: list[dict[str, str]] = field(default_factory=list)
    artifact_schema_validation: str = "not run"
    output_path: str = ""
    output_row_count: int = 0
    elapsed_runtime_seconds: float = 0.0


def _roster_columns(rosters: pd.DataFrame) -> pd.DataFrame:
    aliases = {
        "player_id": ("player_id", "gsis_id"), "player_name": ("player_name", "full_name"),
        "team": ("team", "team_abbr"), "position": ("position",),
    }
    result = pd.DataFrame(index=rosters.index)
    for output, choices in aliases.items():
        source = next((c for c in choices if c in rosters), None)
        if source is None:
            raise ValueError(f"current roster missing {output}")
        result[output] = rosters[source]
    result["player_id"] = result.player_id.map(lambda value: str(value).strip() if pd.notna(value) else "")
    result["team"] = result.team.map(canonical_team)
    return result


def validate_current_rows(rows: pd.DataFrame, models: ProductionModels) -> None:
    if rows.empty:
        raise ValueError("no current feature rows were constructed")
    if rows.player_id.isna().any() or rows.player_id.astype(str).str.strip().eq("").any():
        raise ValueError("stable nflverse player_id is required")
    kickoff = pd.to_datetime(rows.kickoff, utc=True, errors="raise")
    built = pd.to_datetime(rows.feature_built_at_utc, utc=True, errors="raise")
    cutoff = pd.to_datetime(rows.feature_data_as_of_utc, utc=True, errors="raise")
    if ((built >= kickoff) | (cutoff >= kickoff)).any():
        raise ValueError("feature timestamps must be strictly before kickoff")
    if rows.duplicated(["player_id", "kickoff", "canonical_market"]).any():
        raise ValueError("ambiguous player/game/market identity")
    union = list(dict.fromkeys(feature for market in MARKETS
                              for feature in models.scorers[market].artifact["features"]))
    if list(rows.columns) != IDENTITY + union:
        raise ValueError("artifact/schema mismatch: output contains unexpected or missing columns")
    for market, group in rows.groupby("canonical_market"):
        required = list(models.scorers[market].artifact["features"])
        if not set(required).issubset(group.columns) or len(required) != len(set(required)):
            raise ValueError(f"artifact/schema mismatch for {market}")


def atomic_write(rows: pd.DataFrame, output: str | Path, models: ProductionModels) -> None:
    destination = Path(output).expanduser().resolve()
    destination.parent.mkdir(parents=True, exist_ok=True)
    fd, name = tempfile.mkstemp(prefix=f".{destination.name}.", suffix=".parquet", dir=destination.parent)
    os.close(fd)
    temporary = Path(name)
    try:
        rows.to_parquet(temporary, index=False)
        check = pd.read_parquet(temporary)
        validate_current_rows(check, models)
        os.replace(temporary, destination)
    finally:
        temporary.unlink(missing_ok=True)


def build_current_features(output: str | Path, *, dry_run: bool = False,
                           now: datetime | None = None, client: NflverseClient | None = None,
                           models: ProductionModels | None = None) -> tuple[pd.DataFrame, BuildReport]:
    """Fetch nflverse releases and construct upcoming rows with historical data only."""
    started = time.monotonic()
    now = (now or datetime.now(timezone.utc)).astimezone(timezone.utc)
    client = client or NflverseClient()
    models = models or load_production_models()
    report = BuildReport(now.isoformat(), output_path=str(Path(output).expanduser()))
    schedules_raw = client.fetch("schedules", refresh=True)
    schedules = normalize_schedules(schedules_raw)
    upcoming = schedules[schedules.kickoff > now].sort_values("kickoff")
    # Current slate is the next NFL week, rather than every future schedule.
    if not upcoming.empty:
        first = upcoming.iloc[0]
        upcoming = upcoming[(upcoming.season == first.season) & (upcoming.week == first.week)]
    report.upcoming_games_found = int(len(upcoming))
    if upcoming.empty:
        raise ValueError("no upcoming NFL games found")
    season = int(upcoming.season.iloc[0])
    weekly = normalize_weekly(client.fetch("weekly_stats", season, refresh=True))
    weekly["player_id"] = weekly.player_id.astype(str).str.strip()
    completed_games = schedules[(schedules.season == season) & (schedules.kickoff < now)]
    completed_keys = completed_games[["season", "week"]].drop_duplicates()
    historical = weekly.merge(completed_keys, on=["season", "week"], how="inner")
    if completed_games.empty:
        raise ValueError("no completed prior games available for the current season")
    cutoff = completed_games.kickoff.max()
    report.source_cutoff_timestamp = cutoff.isoformat()
    rosters = _roster_columns(client.fetch("rosters", season, refresh=True))
    synthetic, allowed = [], {"QB": ("player_pass_yds",), "WR": tuple(MARKETS)[1:],
                              "TE": tuple(MARKETS)[1:], "RB": tuple(MARKETS)[1:]}
    history_by_player = historical.groupby("player_id", dropna=False)
    for game in upcoming.itertuples():
        for team in (game.home_team, game.away_team):
            for player in rosters[rosters.team == team].itertuples():
                pid = str(player.player_id).strip() if pd.notna(player.player_id) else ""
                markets = allowed.get(str(player.position).upper(), ())
                if not pid or not markets:
                    if not pid:
                        report.excluded_players.append({"player_name": str(player.player_name), "reason": "missing stable player ID"})
                    continue
                if pid not in history_by_player.groups:
                    report.excluded_players.append({"player_name": str(player.player_name), "reason": "no completed prior-game history"})
                    continue
                prior = historical.loc[history_by_player.groups[pid]]
                for market in markets:
                    target = MARKETS[market]
                    participated = (pd.to_numeric(prior.get("attempts"), errors="coerce").fillna(0) > 0).any() if market == "player_pass_yds" else ((pd.to_numeric(prior.get("targets"), errors="coerce").fillna(0) > 0) | (pd.to_numeric(prior.get("receptions"), errors="coerce").fillna(0) > 0)).any()
                    if not participated:
                        continue
                    synthetic.append({"player_id": pid, "player_name": player.player_name,
                        "team": team, "season": game.season, "week": game.week,
                        "_current_target": True, "_requested_market": market, target: float("nan")})
    if not synthetic:
        raise ValueError("no eligible current players found")
    historical["_current_target"] = False
    combined = pd.concat([historical, pd.DataFrame(synthetic)], ignore_index=True, sort=False)
    relevant_schedules = pd.concat([completed_games, upcoming], ignore_index=True)
    table = build_modeling_table(combined, relevant_schedules, current_row_column="_current_target")
    current = table[table._current_target.fillna(False)].copy()
    current = current[current.apply(lambda r: r.canonical_market == r._requested_market, axis=1)]
    built = now
    union = list(dict.fromkeys(feature for market in MARKETS
                              for feature in models.scorers[market].artifact["features"]))
    pieces = []
    for market in MARKETS:
        required = list(models.scorers[market].artifact["features"])
        part = current[current.canonical_market == market].copy()
        part["feature_built_at_utc"], part["feature_data_as_of_utc"] = built, cutoff
        pieces.append(part[IDENTITY + required])
    rows = pd.concat(pieces, ignore_index=True).reindex(columns=IDENTITY + union)
    validate_current_rows(rows, models)
    report.artifact_schema_validation = "passed"
    report.player_rows_by_market = {m: int((rows.canonical_market == m).sum()) for m in MARKETS}
    report.output_row_count = len(rows)
    if not dry_run:
        atomic_write(rows, output, models)
    report.elapsed_runtime_seconds = round(time.monotonic() - started, 3)
    return rows, report
