"""Small explicit build orchestration; no downloads occur on import."""

from __future__ import annotations

import json
import platform
from datetime import datetime, timezone
from pathlib import Path

import pandas as pd

from .features import build_modeling_table
from .ingestion import NflverseClient


def build_seasons(seasons: list[int], output: str | Path, *, cache_dir: str | Path = "data/historical/raw") -> Path:
    """Fetch selected seasons, build, and atomically write a parquet artifact."""
    if not seasons:
        raise ValueError("at least one season is required")
    client = NflverseClient(cache_dir)
    weekly = pd.concat([client.fetch("weekly_stats", season) for season in seasons], ignore_index=True)
    schedules = client.fetch("schedules")
    schedules = schedules[schedules.season.isin(seasons)]
    players = client.fetch("players")
    # Cache the roster partitions as provenance/crosswalk inputs even though
    # stable weekly GSIS IDs remain authoritative for the feature rows.
    for season in seasons:
        client.fetch("rosters", season)
    optional: dict[str, pd.DataFrame] = {}
    for argument, dataset in (("snaps", "snap_counts"), ("injuries", "injuries"), ("depth_charts", "depth_charts")):
        available = [season for season in seasons if season >= {"snap_counts": 2012, "injuries": 2009, "depth_charts": 2002}[dataset]]
        if available:
            optional[argument] = pd.concat([client.fetch(dataset, season) for season in available], ignore_index=True)
    if "snaps" in optional and "pfr_player_id" in optional["snaps"]:
        crosswalk = players[["pfr_id", "gsis_id"]].dropna().drop_duplicates("pfr_id")
        optional["snaps"] = optional["snaps"].merge(crosswalk, left_on="pfr_player_id", right_on="pfr_id", how="left")
        optional["snaps"]["player_id"] = optional["snaps"]["gsis_id"]
    # Source-specific injury/depth publication timestamps must be normalized to
    # observed_at before opting into those joins. Keeping them cached still
    # records availability without inventing when a report became knowable.
    safe_optional = {key: value for key, value in optional.items() if key == "snaps"}
    table = build_modeling_table(weekly, schedules, **safe_optional)
    destination = Path(output)
    destination.parent.mkdir(parents=True, exist_ok=True)
    temporary = destination.with_suffix(".part.parquet")
    table.to_parquet(temporary, index=False)
    temporary.replace(destination)
    manifest = {
        "built_at_utc": datetime.now(timezone.utc).isoformat(),
        "seasons": sorted(seasons),
        "rows": len(table),
        "columns": list(table),
        "python": platform.python_version(),
        "pipeline": "app.historical.features.build_modeling_table",
        "target_definition": "realized football statistic (not over/under result)",
        "injury_depth_note": "not joined unless a trustworthy pre-kickoff observed_at is supplied",
    }
    destination.with_suffix(".build.json").write_text(json.dumps(manifest, indent=2, sort_keys=True))
    return destination
