"""Refresh current features when necessary, then run one production snapshot cycle."""
from __future__ import annotations

import asyncio
import fcntl
import json
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable

from app.config import settings
from app.current_features import build_current_features
from app.scheduler import sanitized_error
from scripts.run_snapshot_scheduler import run_scheduler

PIPELINE = "app.production_pipeline:create_scheduler_pipeline"


class CycleAlreadyRunning(RuntimeError):
    pass


@contextmanager
def cycle_lock(path: Path):
    """Hold a process-wide, nonblocking advisory lock for the complete cycle."""
    path.parent.mkdir(parents=True, exist_ok=True)
    handle = path.open("a")
    try:
        try:
            fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            raise CycleAlreadyRunning("another production cycle is already running") from exc
        yield
    finally:
        handle.close()


def _base_report(existed: bool, age: float | None, maximum: int) -> dict:
    return {"feature_cache_existed": existed, "feature_cache_age_seconds": age,
            "feature_cache_max_age_seconds": maximum, "feature_refresh_attempted": False,
            "feature_refresh_succeeded": None, "feature_rows_by_market": {},
            "scheduler_execution_id": None, "due": 0, "selected": 0, "completed": 0,
            "deferred": 0, "failed": 0, "estimated_credits": 0,
            "actual_credits_consumed": 0, "quota_before": None, "quota_after": None,
            "http_request_count": 0, "errors": []}


async def run_cycle(*, now: datetime | None = None,
                    materialize: Callable = build_current_features,
                    scheduler: Callable = run_scheduler) -> tuple[dict, int]:
    """Run under one service-local lock and return a sanitized report and exit code."""
    current = (now or datetime.now(timezone.utc)).astimezone(timezone.utc)
    configured = settings.football_current_feature_path
    maximum = settings.football_current_feature_max_age_seconds
    if not configured:
        report = _base_report(False, None, maximum)
        report["errors"].append({"kind": "configuration_failure", "message": "FOOTBALL_CURRENT_FEATURE_PATH is required"})
        return report, 1
    path = Path(configured).expanduser()
    existed = path.is_file()
    age = max(0.0, current.timestamp() - path.stat().st_mtime) if existed else None
    report = _base_report(existed, age, maximum)
    if maximum < 0:
        report["errors"].append({"kind": "configuration_failure", "message": "FOOTBALL_CURRENT_FEATURE_MAX_AGE_SECONDS must be nonnegative"})
        return report, 1
    try:
        with cycle_lock(path.with_name(f".{path.name}.production-cycle.lock")):
            if not existed or age is None or age > maximum:
                report["feature_refresh_attempted"] = True
                try:
                    _, refresh = materialize(path)
                    report["feature_refresh_succeeded"] = True
                    report["feature_rows_by_market"] = dict(refresh.player_rows_by_market)
                except Exception as exc:
                    report["feature_refresh_succeeded"] = False
                    report["errors"].append({"kind": "feature_refresh_failure", "message": sanitized_error(exc)})
                    return report, 1
            scheduler_report = await scheduler(dry_run=False, pipeline=PIPELINE)
            report.update({key: len(scheduler_report.get(key, [])) for key in
                           ("due", "selected", "completed", "deferred", "failed")})
            for key in ("estimated_credits", "actual_credits_consumed", "quota_before",
                        "quota_after", "http_request_count"):
                report[key] = scheduler_report.get(key)
            report["scheduler_execution_id"] = scheduler_report.get("execution_id")
            report["errors"].extend(scheduler_report.get("errors", []))
            return report, 0
    except CycleAlreadyRunning as exc:
        report["errors"].append({"kind": "cycle_overlap", "message": sanitized_error(exc)})
        return report, 1
    except Exception as exc:
        report["errors"].append({"kind": "cycle_failure", "message": sanitized_error(exc)})
        return report, 1


def main() -> int:
    report, exit_code = asyncio.run(run_cycle())
    print(json.dumps(report, indent=2, sort_keys=True, default=str))
    return exit_code


if __name__ == "__main__":
    raise SystemExit(main())
