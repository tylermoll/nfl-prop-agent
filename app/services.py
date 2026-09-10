from collections import defaultdict
from datetime import datetime, timezone
from statistics import median
from app.models import MarketSnapshot

def source_quality(rows: list[MarketSnapshot], stale_after_seconds: int = 180) -> dict:
    now = datetime.now(timezone.utc)
    by_source = defaultdict(list)
    for r in rows:
        by_source[r.source].append(r)

    sources = {}
    for source, items in sorted(by_source.items()):
        ages = []
        for r in items:
            t = r.source_updated_at_utc or r.observed_at_utc
            ages.append(max(0, (now - t).total_seconds()))
        sources[source] = {
            "quotes": len(items),
            "players": len({r.player_name for r in items}),
            "markets": len({r.market_type.value for r in items}),
            "median_age_seconds": round(median(ages), 1) if ages else None,
            "stale_pct": round(100 * sum(a > stale_after_seconds for a in ages) / len(ages), 1) if ages else None,
        }

    hard_rock = by_source.get("hardrockbet_fl", [])
    return {
        "hard_rock_present": bool(hard_rock),
        "total_quotes": len(rows),
        "sources": sources,
        "generated_at_utc": now.isoformat(),
    }

def disagreements(rows: list[MarketSnapshot]) -> list[dict]:
    groups = defaultdict(list)
    for r in rows:
        key = (r.game_id, r.player_name, r.market_type.value)
        groups[key].append(r)

    out = []
    for (game_id, player, market), items in groups.items():
        sportsbook_lines = [r.line for r in items if r.american_odds is not None and r.line is not None]
        if len(sportsbook_lines) < 2:
            continue
        lo, hi = min(sportsbook_lines), max(sportsbook_lines)
        if hi != lo:
            out.append({
                "game_id": game_id,
                "player": player,
                "market": market,
                "min_line": lo,
                "max_line": hi,
                "line_spread": hi - lo,
                "consensus_median_line": median(sportsbook_lines),
            })
    return sorted(out, key=lambda x: x["line_spread"], reverse=True)
