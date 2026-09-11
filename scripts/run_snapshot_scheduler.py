"""CLI entry point for the read-only prospective snapshot scheduler."""
from __future__ import annotations
import argparse, asyncio, importlib, json
from datetime import timedelta
from app.config import settings
from app.providers.kalshi import KalshiProvider
from app.providers.the_odds_api import TheOddsApiProvider
from app.scheduler import CaptureWindow, SchedulerConfig, SnapshotScheduler
from app.shadow_storage import ShadowStore

def parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(description="Collect due research-only NFL shadow snapshots (never wagers)")
    p.add_argument("--dry-run", action="store_true", help="discover/plan only; no props, Kalshi, or persistence")
    p.add_argument("--pipeline", help="module:function returning (model_loader, observation_builder)")
    return p

def _pipeline(spec: str | None):
    if not spec: return (lambda: None), (lambda *_: [])
    module, function = spec.split(":", 1)
    return getattr(importlib.import_module(module), function)()

async def main() -> int:
    args = parser().parse_args()
    if not args.dry_run and not args.pipeline:
        parser().error("live mode requires --pipeline module:function for repository-specific current features")
    loader, builder = _pipeline(args.pipeline)
    windows = tuple(CaptureWindow(f"{m//60}h" if m >= 60 and m % 60 == 0 else f"{m}m",
        timedelta(minutes=m), timedelta(minutes=settings.scheduler_tolerance_minutes)) for m in settings.scheduler_slots_minutes)
    final = None if settings.scheduler_final_capture_minutes is None else CaptureWindow("final",
        timedelta(minutes=settings.scheduler_final_capture_minutes), timedelta(minutes=settings.scheduler_tolerance_minutes))
    config = SchedulerConfig(windows=windows, final_window=final,
        minimum_quota_reserve=settings.scheduler_min_quota_reserve,
        max_credits_per_execution=settings.scheduler_max_credits_per_run,
        max_games_per_execution=settings.scheduler_max_games_per_run, retry_budget=settings.scheduler_retry_budget,
        provider_stale_after=timedelta(seconds=settings.scheduler_provider_stale_seconds),
        model_stale_after=timedelta(seconds=settings.scheduler_model_stale_seconds))
    scheduler = SnapshotScheduler(odds=TheOddsApiProvider(), store=ShadowStore(settings.database_url),
        kalshi=KalshiProvider(fetch_order_books=False), load_model=loader, build_observations=builder, config=config)
    print(json.dumps(await scheduler.run(dry_run=args.dry_run), indent=2, default=str))
    return 0

if __name__ == "__main__": raise SystemExit(asyncio.run(main()))
