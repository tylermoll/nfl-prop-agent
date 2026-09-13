"""Read-only trace of one production point prediction and its feature lineage.

The command opens the observation database with SQLAlchemy's query-only API,
loads (but never writes) the configured feature cache and model artifact, and
uses only already-cached nflverse parquet files for optional source lineage.
It deliberately imports no scheduler or market-data provider.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
from datetime import date, datetime
from pathlib import Path
from typing import Any

import joblib
import numpy as np
import pandas as pd
from sqlalchemy import create_engine, select

from app.historical.features import USAGE, build_modeling_table
from app.historical.normalize import normalize_schedules, normalize_weekly
from app.shadow_storage import normalize_database_url, observations


def _json(value: Any) -> Any:
    if value is None or value is pd.NA or (isinstance(value, float) and np.isnan(value)):
        return None
    if isinstance(value, (np.integer, np.floating)):
        return value.item()
    if isinstance(value, (pd.Timestamp, datetime, date)):
        return value.isoformat()
    return value


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def read_cache(path: Path) -> pd.DataFrame:
    suffix = path.suffix.lower()
    if suffix in {".parquet", ".pq"}:
        return pd.read_parquet(path)
    if suffix == ".json":
        return pd.read_json(path)
    if suffix == ".csv":
        return pd.read_csv(path)
    raise ValueError("current feature cache must be parquet, JSON, or CSV")


def find_observation(database_url: str, *, observation_id: str | None, player: str | None,
                     market: str, point: float | None, line: float | None,
                     capture_window: str | None, side: str | None) -> dict[str, Any]:
    query = select(observations).where(observations.c.canonical_market == market)
    if observation_id:
        query = query.where(observations.c.observation_id == observation_id)
    if player:
        query = query.where(observations.c.player_name == player)
    if point is not None:
        query = query.where(observations.c.point_prediction.between(point - .051, point + .051))
    if line is not None:
        query = query.where(observations.c.line == line)
    if side:
        query = query.where(observations.c.side == side)
    if capture_window:
        query = query.where(observations.c.context["capture_slot"].as_string() == capture_window)
    query = query.order_by(observations.c.observed_at_utc.desc()).limit(2)
    engine = create_engine(normalize_database_url(database_url))
    with engine.connect() as connection:  # no begin(), DDL, or DML
        rows = [dict(row._mapping) for row in connection.execute(query)]
    if len(rows) != 1:
        raise ValueError(f"observation selector resolved {len(rows)} rows; use --observation-id")
    return rows[0]


def select_feature_row(cache: pd.DataFrame, observation: dict[str, Any]) -> pd.Series:
    kickoff = pd.to_datetime(cache.kickoff, utc=True)
    wanted = pd.Timestamp(observation["kickoff_utc"])
    if wanted.tzinfo is None:
        wanted = wanted.tz_localize("UTC")
    rows = cache[(kickoff == wanted) &
                 (cache.canonical_market == observation["canonical_market"]) &
                 (cache.player_id.astype(str) == str(observation["player_id"]))]
    if len(rows) != 1:
        raise ValueError(f"feature selector resolved {len(rows)} rows")
    row = rows.iloc[0]
    cache_built = pd.to_datetime(row.feature_built_at_utc, utc=True)
    observation_built = pd.to_datetime(observation["feature_built_at_utc"], utc=True)
    if cache_built != observation_built:
        raise ValueError("current cache row is not the version used by this observation")
    return row


def cached_lineage(cache_dir: Path, feature: pd.Series, observation: dict[str, Any]) -> dict[str, Any]:
    """Rebuild transforms from existing parquet inputs; never download missing data."""
    season = pd.Timestamp(observation["kickoff_utc"]).year
    schedule_path = cache_dir / "schedules" / "all.parquet"
    weekly_paths = [cache_dir / "weekly_stats" / f"{year}.parquet"
                    for year in range(season - 2, season + 1)]
    missing = [str(path) for path in [schedule_path, *weekly_paths] if not path.is_file()]
    if missing:
        return {"available": False, "reason": "cached nflverse input absent; no network fallback is allowed",
                "missing_paths": missing}
    schedules = normalize_schedules(pd.read_parquet(schedule_path))
    weekly = pd.concat([normalize_weekly(pd.read_parquet(path)) for path in weekly_paths], ignore_index=True)
    cutoff = pd.Timestamp(feature.feature_data_as_of_utc)
    if cutoff.tzinfo is None:
        cutoff = cutoff.tz_localize("UTC")
    completed = schedules[(schedules.season.between(season - 2, season)) & (schedules.kickoff <= cutoff)]
    keys = completed[["season", "week"]].drop_duplicates()
    historical = weekly.merge(keys, on=["season", "week"], how="inner")
    pid = str(observation["player_id"])
    player = historical[historical.player_id.astype(str) == pid].copy()
    if player.empty:
        return {"available": False, "reason": "stable player ID is absent from cached weekly inputs"}
    synthetic = {"player_id": pid, "player_name": observation["player_name"],
                 "team": observation.get("team") or player.sort_values(["season", "week"]).team.iloc[-1],
                 "season": season, "week": int(schedules.loc[
                     schedules.game_id == observation["game_id"], "week"].iloc[0]),
                 "_trace_target": True, "receptions": np.nan}
    historical["_trace_target"] = False
    table = build_modeling_table(pd.concat([historical, pd.DataFrame([synthetic])], ignore_index=True),
                                 pd.concat([completed, schedules[schedules.game_id == observation["game_id"]]])
                                   .drop_duplicates("game_id"), current_row_column="_trace_target")
    history = table[(table.player_id.astype(str) == pid) &
                    (table.canonical_market == "player_receptions") &
                    ~table._trace_target.fillna(False)].sort_values("kickoff")
    columns = ["season", "week", "game_id", "kickoff", "team", "opponent", "actual_value", *USAGE]
    rows = [{name: _json(row.get(name)) for name in columns} for _, row in history.tail(8).iterrows()]
    latest_2026 = history[history.season == season].tail(1)
    return {"available": True, "source_rows_oldest_to_newest": rows,
            "window_membership": {"previous_game_value": [r["game_id"] for r in rows[-1:]],
                                  "rolling_3_mean": [r["game_id"] for r in rows[-3:]],
                                  "rolling_5_mean": [r["game_id"] for r in rows[-5:]],
                                  "rolling_8_mean": [r["game_id"] for r in rows[-8:]]},
            "usage_reproduction": {metric: {"pregame": _json(history.iloc[-1].get(metric)),
                "rolling_3_sources": [_json(x) for x in history[metric].tail(3)] if metric in history else []}
                for metric in USAGE},
            "latest_completed_current_season_game": (None if latest_2026.empty else
                {name: _json(latest_2026.iloc[-1].get(name)) for name in columns}),
            "latest_completed_current_season_game_present": not latest_2026.empty}


def find_historical_artifact(artifact_id: str, market: str, *, configured_path: Path | None,
                             search_roots: list[Path]) -> tuple[Path | None, dict[str, Any] | None]:
    """Find a retained artifact by embedded ID, never by a mutable filename."""
    candidates: list[Path] = []
    if configured_path is not None and configured_path.is_file():
        candidates.append(configured_path)
    for root in search_roots:
        root = root.expanduser().resolve()
        if root.is_file():
            candidates.append(root)
        elif root.is_dir():
            candidates.extend(path for path in root.rglob("*")
                              if path.is_file() and path.suffix.lower() in {".joblib", ".pkl", ".pickle"})
    seen: set[Path] = set()
    for path in candidates:
        resolved = path.expanduser().resolve()
        if resolved in seen:
            continue
        seen.add(resolved)
        try:
            artifact = joblib.load(resolved)
        except Exception:
            continue
        if (isinstance(artifact, dict) and artifact.get("artifact_id") == artifact_id and
                artifact.get("canonical_market") == market):
            return resolved, artifact
    return None, None


def trace(*, database_url: str, feature_path: Path, artifact_path: Path | None,
          history_cache_dir: Path, artifact_search_roots: list[Path] | None = None,
          observation_id: str | None = None,
          player: str | None = None, market: str = "player_receptions",
          point: float | None = None, line: float | None = None,
          capture_window: str | None = None, side: str | None = None) -> dict[str, Any]:
    observation = find_observation(database_url, observation_id=observation_id, player=player,
        market=market, point=point, line=line, capture_window=capture_window, side=side)
    row = select_feature_row(read_cache(feature_path), observation)
    expected_id = observation.get("model_version")
    artifact, resolved_path = None, None
    if artifact_path is not None and artifact_path.is_file():
        configured = joblib.load(artifact_path)
        if (configured.get("artifact_id") == expected_id and
                configured.get("canonical_market") == observation["canonical_market"]):
            resolved_path, artifact = artifact_path.expanduser().resolve(), configured
    if artifact is None and observation_id:
        resolved_path, artifact = find_historical_artifact(expected_id, observation["canonical_market"],
            configured_path=artifact_path, search_roots=artifact_search_roots or [])
    if artifact is None and not observation_id:
        raise ValueError("configured artifact is not the artifact used by this observation")

    provenance = (observation.get("context") or {}).get("artifact_provenance") or {}
    features = list(artifact["features"] if artifact is not None else provenance.get("required_features") or [])
    missing_features = [name for name in features if name not in row.index]
    if missing_features:
        raise ValueError(f"feature cache row lacks artifact-required features: {missing_features}")
    base = {"observation": {key: _json(observation.get(key)) for key in
            ("observation_id", "player_id", "player_name", "game_id", "kickoff_utc", "team",
             "opponent", "canonical_market", "line", "point_prediction", "observed_at_utc")},
        "identity": {"stable_player_id": str(row.player_id), "player_name": row.player_name,
                     "home_team": row.home_team, "away_team": row.away_team,
                     "kickoff": _json(pd.Timestamp(row.kickoff)),
                     "team": observation.get("team"), "opponent": observation.get("opponent")},
        "feature_cache": {"path": str(feature_path), "matching_original_row": True,
                          "feature_built_at_utc": _json(pd.Timestamp(row.feature_built_at_utc)),
                          "feature_data_as_of_utc": _json(pd.Timestamp(row.feature_data_as_of_utc))},
        "ordered_features": [{"position": i, "name": name, "value": _json(row[name])}
                             for i, name in enumerate(features)],
        "historical_lineage": cached_lineage(history_cache_dir, row, observation)}
    if artifact is None:
        base.update({"artifact": {"available": False, "artifact_id": expected_id,
                                  "canonical_market": observation["canonical_market"],
                                  "reason": "exact historical artifact was not found in retained files"},
                     "prediction": {"exact_reproduction_possible": False,
                                    "stored_final_point_prediction": _json(observation.get("point_prediction")),
                                    "reason": "the exact estimator is unavailable; no substitute was used"}})
        return base

    frame = pd.DataFrame([{name: row[name] for name in features}])
    raw = float(artifact["pipeline"].predict(frame[features])[0])
    adjustment = float(artifact.get("additive_bias_correction", 0.0))
    base.update({"artifact": {"available": True, "path": str(resolved_path), "sha256": _sha256(resolved_path),
                     "artifact_id": artifact.get("artifact_id"),
                     "canonical_market": artifact.get("canonical_market"),
                     "uncertainty_method": artifact.get("uncertainty_method"),
                     "uncertainty_version": artifact.get("uncertainty_version")},
        "prediction": {"exact_reproduction_possible": True, "raw_estimator_prediction": raw,
                       "additive_bias_correction": adjustment,
                       "reproduced_final_point_prediction": raw + adjustment,
                       "stored_final_point_prediction": _json(observation.get("point_prediction"))}})
    return base


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--observation-id")
    parser.add_argument("--player")
    parser.add_argument("--market", default="player_receptions")
    parser.add_argument("--point", type=float)
    parser.add_argument("--line", type=float)
    parser.add_argument("--side", choices=("over", "under"))
    parser.add_argument("--capture-window")
    parser.add_argument("--history-cache-dir", type=Path,
                        default=Path(os.getenv("FOOTBALL_HISTORY_CACHE_DIR", "data/historical/raw")))
    parser.add_argument("--artifact-search-root", action="append", type=Path, default=[],
                        help="read-only root to scan for the observation's exact historical artifact")
    args = parser.parse_args()
    required = {"DATABASE_URL": os.getenv("DATABASE_URL"),
                "FOOTBALL_CURRENT_FEATURE_PATH": os.getenv("FOOTBALL_CURRENT_FEATURE_PATH")}
    configured_artifact = os.getenv("FOOTBALL_ARTIFACT_PLAYER_RECEPTIONS")
    if not args.observation_id and not configured_artifact:
        required["FOOTBALL_ARTIFACT_PLAYER_RECEPTIONS"] = None
    if missing := [key for key, value in required.items() if not value]:
        parser.error(f"missing environment variables: {', '.join(missing)}")
    result = trace(database_url=required["DATABASE_URL"],
        feature_path=Path(required["FOOTBALL_CURRENT_FEATURE_PATH"]),
        artifact_path=Path(configured_artifact) if configured_artifact else None,
        artifact_search_roots=args.artifact_search_root,
        history_cache_dir=args.history_cache_dir, observation_id=args.observation_id,
        player=args.player, market=args.market, point=args.point, line=args.line,
        capture_window=args.capture_window, side=args.side)
    print(json.dumps(result, indent=2, sort_keys=False, default=_json))


if __name__ == "__main__":
    main()
