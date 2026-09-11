"""Run a secret-safe, read-only smoke test against The Odds API.

Usage (do not put the key on the command line):
    THE_ODDS_API_KEY=... DEMO_MODE=false python -m scripts.smoke_the_odds_api
"""

from __future__ import annotations

import asyncio
import json
from datetime import datetime, timezone
from statistics import median

from app.config import settings
from app.providers.the_odds_api import MARKET_MAP, TheOddsApiError, TheOddsApiProvider


async def smoke() -> tuple[dict, int]:
    if settings.demo_mode:
        return {"ok": False, "error": "DEMO_MODE must be false for a live smoke test"}, 2
    if not settings.the_odds_api_key:
        return {
            "ok": False,
            "error": "THE_ODDS_API_KEY is not configured (its value was not read or printed)",
        }, 2

    provider = TheOddsApiProvider()
    try:
        rows = await provider.fetch_nfl_player_props()
    except TheOddsApiError as exc:
        return {
            "ok": False,
            "error": str(exc),
            "discovered_nfl_events": provider.discovered_event_count,
            "eligible_nfl_events_queried": provider.eligible_event_count,
            "quota": provider.usage,
        }, 1

    hard_rock_rows = [row for row in rows if row.source == "hardrockbet"]
    counts = {
        market: sum(row.market_type.value == market for row in hard_rock_rows)
        for market in MARKET_MAP
    }
    missing = [market for market, count in counts.items() if count == 0]
    timestamps = [
        row.source_updated_at_utc
        for row in hard_rock_rows
        if row.source_updated_at_utc is not None
    ]
    now = datetime.now(timezone.utc)
    freshness = {
        "rows_with_provider_timestamp": len(timestamps),
        "oldest_provider_timestamp_utc": min(timestamps).isoformat() if timestamps else None,
        "newest_provider_timestamp_utc": max(timestamps).isoformat() if timestamps else None,
        "median_age_seconds": round(median((now - value).total_seconds() for value in timestamps), 1)
        if timestamps
        else None,
    }
    return {
        "ok": True,
        "discovered_nfl_events": provider.discovered_event_count,
        "eligible_nfl_events_queried": provider.eligible_event_count,
        "hardrockbet_present": bool(hard_rock_rows),
        "hardrockbet_player_prop_rows": len(hard_rock_rows),
        "hardrockbet_market_counts": counts,
        "provider_freshness": freshness,
        "quota": provider.usage,
        "missing_markets": missing,
        "api_errors": [],
    }, 0


def main() -> int:
    report, exit_code = asyncio.run(smoke())
    print(json.dumps(report, indent=2, sort_keys=True))
    return exit_code


if __name__ == "__main__":
    raise SystemExit(main())
