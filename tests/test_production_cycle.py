import fcntl
import os
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace

import pytest

from app.config import settings
from scripts.run_production_cycle import PIPELINE, run_cycle

NOW = datetime(2026, 9, 12, 12, tzinfo=timezone.utc)


def _scheduler_report():
    return {"execution_id": "execution-1", "due": [1, 2], "selected": [1],
            "completed": [1], "deferred": [2], "failed": [], "estimated_credits": 3,
            "actual_credits_consumed": 3, "quota_before": 100, "quota_after": 97,
            "http_request_count": 2, "errors": []}


def _configure(monkeypatch, tmp_path, age=None, maximum=21600):
    path = tmp_path / "current_features.parquet"
    if age is not None:
        path.write_bytes(b"valid-cache")
        timestamp = (NOW - timedelta(seconds=age)).timestamp()
        os.utime(path, (timestamp, timestamp))
    monkeypatch.setattr(settings, "football_current_feature_path", str(path))
    monkeypatch.setattr(settings, "football_current_feature_max_age_seconds", maximum)
    return path


@pytest.mark.asyncio
@pytest.mark.parametrize("age,refresh_expected", [(None, True), (60, False), (21601, True)])
async def test_missing_fresh_and_stale_cache(monkeypatch, tmp_path, age, refresh_expected):
    path = _configure(monkeypatch, tmp_path, age)
    calls = {"refresh": 0, "scheduler": 0}

    def refresh(output):
        calls["refresh"] += 1
        Path(output).write_bytes(b"new-cache")
        return None, SimpleNamespace(player_rows_by_market={"player_pass_yds": 4})

    async def scheduler(**kwargs):
        calls["scheduler"] += 1
        assert kwargs == {"dry_run": False, "pipeline": PIPELINE}
        return _scheduler_report()

    report, code = await run_cycle(now=NOW, materialize=refresh, scheduler=scheduler)
    assert code == 0
    assert calls == {"refresh": int(refresh_expected), "scheduler": 1}
    assert report["feature_cache_existed"] is (age is not None)
    assert report["feature_refresh_attempted"] is refresh_expected
    assert report["feature_refresh_succeeded"] is (True if refresh_expected else None)
    assert report["scheduler_execution_id"] == "execution-1"
    assert (report["due"], report["selected"], report["completed"], report["deferred"], report["failed"]) == (2, 1, 1, 1, 0)
    assert report["feature_rows_by_market"] == ({"player_pass_yds": 4} if refresh_expected else {})


@pytest.mark.asyncio
async def test_configured_max_age_controls_refresh(monkeypatch, tmp_path):
    _configure(monkeypatch, tmp_path, age=61, maximum=60)
    attempted = []
    async def scheduler(**kwargs):
        return _scheduler_report()
    report, _ = await run_cycle(now=NOW,
        materialize=lambda output: (attempted.append(output), SimpleNamespace(player_rows_by_market={})),
        scheduler=scheduler)
    assert attempted
    assert report["feature_cache_max_age_seconds"] == 60


@pytest.mark.asyncio
async def test_failed_refresh_preserves_cache_and_prevents_all_market_calls(monkeypatch, tmp_path):
    path = _configure(monkeypatch, tmp_path, age=21601)
    scheduler_called = False
    original = path.read_bytes()

    def refresh(_):
        # Failure occurs before atomic replacement, as the real materializer guarantees.
        raise RuntimeError("failed https://user:secret@example.test/data?api_key=topsecret")

    async def scheduler(**kwargs):
        nonlocal scheduler_called
        scheduler_called = True
        return _scheduler_report()

    report, code = await run_cycle(now=NOW, materialize=refresh, scheduler=scheduler)
    assert code == 1 and not scheduler_called
    assert path.read_bytes() == original
    assert report["feature_refresh_succeeded"] is False
    error = report["errors"][0]["message"]
    assert "topsecret" not in error and "user:secret" not in error and "[URL REDACTED]" in error


@pytest.mark.asyncio
async def test_overlap_is_rejected_before_refresh_or_scheduler(monkeypatch, tmp_path):
    path = _configure(monkeypatch, tmp_path, age=None)
    lock_path = path.with_name(f".{path.name}.production-cycle.lock")
    lock_path.touch()
    handle = lock_path.open("a")
    fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
    try:
        report, code = await run_cycle(now=NOW,
            materialize=lambda _: pytest.fail("must not refresh"),
            scheduler=lambda **_: pytest.fail("must not call Odds API or Kalshi"))
    finally:
        handle.close()
    assert code == 1
    assert report["errors"][0]["kind"] == "cycle_overlap"


def test_cycle_has_no_training_or_order_entry_points():
    import scripts.run_production_cycle as module
    names = dir(module)
    assert not any(name.startswith(("train", "bootstrap", "place_", "submit_", "wager_")) for name in names)
