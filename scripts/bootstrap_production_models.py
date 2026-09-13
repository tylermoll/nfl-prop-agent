"""One-time, offline production-model bootstrap for a persistent Railway volume.

This command downloads nflverse football data only.  It never imports or calls
market-data providers, scheduler code, or wagering code.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import shutil
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import joblib
import pandas as pd

from app.historical.build import build_seasons
from app.historical.ingestion import NflverseClient
from app.modeling.benchmark import MARKETS, BenchmarkConfig, run_benchmark

ENV_BY_MARKET = {
    "player_pass_yds": "FOOTBALL_ARTIFACT_PLAYER_PASS_YDS",
    "player_reception_yds": "FOOTBALL_ARTIFACT_PLAYER_RECEPTION_YDS",
    "player_receptions": "FOOTBALL_ARTIFACT_PLAYER_RECEPTIONS",
}
REQUIRED_SCHEMA = {
    "weekly_stats": {"season", "week", "player_id", "player_name", "team", "passing_yards", "receiving_yards", "receptions"},
    "schedules": {"season", "week", "game_id", "home_team", "away_team", "gameday"},
    "players": {"gsis_id", "pfr_id"},
    "rosters": {"season"},
    "snap_counts": {"season", "week", "pfr_player_id"},
}


def audit_season(season: int, cache_dir: Path) -> dict[str, Any]:
    """Audit every nflverse input read by ``build_seasons`` before training."""
    client = NflverseClient(cache_dir)
    report: dict[str, Any] = {"season": season, "audited_at_utc": datetime.now(timezone.utc).isoformat(), "sources": {}}
    frames: dict[str, pd.DataFrame] = {}
    for dataset, partition in (("schedules", None), ("weekly_stats", season), ("players", None),
                               ("rosters", season), ("snap_counts", season)):
        try:
            frame = client.fetch(dataset, partition)
            if "season" in frame:
                frame = frame[frame.season == season]
            frames[dataset] = frame
            required = REQUIRED_SCHEMA[dataset]
            missing = sorted(required - set(frame))
            weeks = sorted(int(x) for x in frame.week.dropna().unique()) if "week" in frame else []
            report["sources"][dataset] = {"available": True, "rows": int(len(frame)), "weeks": weeks,
                                                  "week_min": min(weeks) if weeks else None,
                                                  "week_max": max(weeks) if weeks else None,
                                                  "columns": list(frame.columns), "missing_required_columns": missing}
        except Exception as exc:
            report["sources"][dataset] = {"available": False, "error": f"{type(exc).__name__}: {exc}"}
    required_ok = all(report["sources"][name].get("available") and
                      not report["sources"][name].get("missing_required_columns") and
                      report["sources"][name].get("rows", 0) > 0
                      for name in ("schedules", "weekly_stats", "players", "rosters"))
    snaps = report["sources"]["snap_counts"]
    # Snap features are explicit nullable covariates. Median imputation plus a
    # missingness indicator is shared by benchmark and live scoring; absence is
    # not converted to zero. A wholly absent file still blocks build_seasons.
    snaps_compatible = bool(snaps.get("available") and not snaps.get("missing_required_columns") and snaps.get("rows", 0))
    report["snap_missingness_handling"] = {
        "compatible": snaps_compatible,
        "method": "nullable snap_share; median imputation with missing indicator; never zero-filled",
    }
    report["safe_for_validation_calibration"] = bool(required_ok and snaps_compatible)
    if not report["safe_for_validation_calibration"]:
        report["blocker"] = "one or more required inputs are absent, empty, or schema-incompatible"
    return report


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _validate_artifact(path: Path, market: str) -> dict[str, Any]:
    artifact = joblib.load(path)
    if artifact.get("canonical_market") != market:
        raise ValueError(f"artifact {path} is not canonical market {market}")
    required = {"pipeline", "features", "calibration_predictions", "calibration_residuals"}
    if missing := required - artifact.keys():
        raise ValueError(f"artifact lacks required fields: {sorted(missing)}")
    if artifact.get("uncertainty_version") == "2" and not artifact.get("probability_calibration"):
        raise ValueError(f"artifact {path} lacks version 2 probability calibration parameters")
    return artifact


def atomic_install(staged: dict[str, Path], destinations: dict[str, Path]) -> None:
    """Install a complete artifact set, restoring every old file on failure."""
    backups: dict[str, Path] = {}
    installed: list[str] = []
    try:
        for market in MARKETS:
            destination = destinations[market]
            destination.parent.mkdir(parents=True, exist_ok=True)
            if destination.exists():
                backup = destination.with_name(destination.name + ".bootstrap-backup")
                shutil.copy2(destination, backup)
                backups[market] = backup
            temporary = destination.with_name(destination.name + ".bootstrap-part")
            shutil.copy2(staged[market], temporary)
            _validate_artifact(temporary, market)
            os.replace(temporary, destination)
            installed.append(market)
    except Exception:
        for market in installed:
            destination = destinations[market]
            if market in backups:
                os.replace(backups[market], destination)
            else:
                destination.unlink(missing_ok=True)
        raise
    finally:
        for destination in destinations.values():
            destination.with_name(destination.name + ".bootstrap-part").unlink(missing_ok=True)
        for backup in backups.values():
            backup.unlink(missing_ok=True)


def bootstrap(training_seasons: list[int], validation_season: int, destinations: dict[str, Path],
              *, work_dir: Path | None = None, keep_temp: bool = False) -> dict[str, Any]:
    if validation_season in training_seasons or any(s >= validation_season for s in training_seasons):
        raise ValueError("all training seasons must strictly precede validation season")
    if any(s >= 2026 for s in training_seasons + [validation_season]):
        raise ValueError("2026 or later outcomes are forbidden in the initial production bootstrap")
    owned = work_dir is None
    root = Path(tempfile.mkdtemp(prefix="nfl-model-bootstrap-")) if owned else work_dir
    root.mkdir(parents=True, exist_ok=True)
    try:
        audit = audit_season(validation_season, root / "raw")
        (root / "audit.json").write_text(json.dumps(audit, indent=2, sort_keys=True))
        if not audit["safe_for_validation_calibration"]:
            raise RuntimeError(f"{validation_season} nflverse audit failed: {audit.get('blocker')}")
        seasons = sorted(training_seasons + [validation_season])
        table_path = build_seasons(seasons, root / "historical.parquet", cache_dir=root / "raw")
        table = pd.read_parquet(table_path)
        source_provenance = []
        for metadata_path in sorted((root / "raw").glob("**/*.metadata.json")):
            source_provenance.append(json.loads(metadata_path.read_text()))
        unexpected = sorted(set(map(int, table.season.unique())) - set(seasons))
        if unexpected:
            raise ValueError(f"unexpected outcome seasons in table: {unexpected}")
        benchmark_dir = root / "benchmark"
        config = BenchmarkConfig(validation_season=validation_season, production_bootstrap=True,
                                 training_seasons=tuple(sorted(training_seasons)))
        metrics = run_benchmark(table, benchmark_dir, config)
        built_at = datetime.now(timezone.utc).isoformat()
        manifests: dict[str, dict[str, Any]] = {}
        staged: dict[str, Path] = {}
        for market in MARKETS:
            market_metrics = metrics["markets"][market]
            best_name, best_metrics = min(market_metrics["baselines"].items(), key=lambda item: item[1]["mae"])
            improvement = best_metrics["mae"] - market_metrics["learned_model"]["mae"]
            market_metrics["promotion"] = {
                "best_baseline": best_name, "best_baseline_mae": best_metrics["mae"],
                "learned_mae_improvement": improvement, "beats_baseline": improvement > 0,
            }
            path = benchmark_dir / f"{market}.joblib"
            artifact = _validate_artifact(path, market)
            provenance = {
                "bootstrap_version": "initial-production-v1", "built_at_utc": built_at,
                "training_seasons": sorted(training_seasons), "validation_calibration_season": validation_season,
                "feature_schema": artifact["features"], "selected_model": metrics["markets"][market]["selected_model"],
                "validation_metrics": market_metrics,
                "calibration_methodology": artifact["uncertainty_method"],
                "calibration_version": artifact["uncertainty_version"],
                "calibration_residual_source": "validation-only predictions from training-only model",
                "validation_source_audit": audit["sources"],
                "source_data_provenance": source_provenance,
            }
            artifact["provenance"] = provenance
            artifact["artifact_id"] = f"initial-production-v1-{market}-{built_at}"
            joblib.dump(artifact, path)
            _validate_artifact(path, market)
            provenance["artifact_sha256"] = _sha256(path)
            manifests[market] = provenance
            staged[market] = path
        atomic_install(staged, destinations)
        for market, destination in destinations.items():
            _validate_artifact(destination, market)
            manifest_path = destination.with_suffix(destination.suffix + ".manifest.json")
            manifest_path.write_text(json.dumps(manifests[market], indent=2, sort_keys=True))
        result = {"audit": audit, "metrics": metrics, "manifests": manifests,
                  "destinations": {k: str(v) for k, v in destinations.items()}}
        (root / "bootstrap-result.json").write_text(json.dumps(result, indent=2, sort_keys=True, default=str))
        return result
    finally:
        if owned and not keep_temp:
            shutil.rmtree(root, ignore_errors=True)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--training-seasons", nargs="+", type=int, default=[2022, 2023, 2024])
    parser.add_argument("--validation-season", type=int, default=2025)
    parser.add_argument("--work-dir", type=Path)
    parser.add_argument("--keep-temp", action="store_true")
    args = parser.parse_args()
    destinations = {}
    for market, variable in ENV_BY_MARKET.items():
        value = os.environ.get(variable)
        if not value:
            raise SystemExit(f"required environment variable is not configured: {variable}")
        destinations[market] = Path(value)
    result = bootstrap(args.training_seasons, args.validation_season, destinations,
                       work_dir=args.work_dir, keep_temp=args.keep_temp)
    print(json.dumps({"destinations": result["destinations"], "markets": result["metrics"]["markets"]},
                     indent=2, default=str))


if __name__ == "__main__":
    main()
