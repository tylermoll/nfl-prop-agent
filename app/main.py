import asyncio
from fastapi import FastAPI
from app.config import settings
from app.providers.demo import DemoOddsProvider, DemoKalshiProvider
from app.providers.the_odds_api import TheOddsApiProvider
from app.providers.kalshi import KalshiProvider
from app.services import source_quality, disagreements
from app.consensus import market_divergences
from app.cross_market import cross_market_comparisons

app = FastAPI(title="NFL Prop Agent V1")

async def load_rows():
    if not settings.demo_mode:
        odds, kalshi = await asyncio.gather(
            TheOddsApiProvider().fetch_nfl_player_props(),
            KalshiProvider(
                series_tickers=settings.kalshi_nfl_series_tickers,
                lookahead_days=settings.kalshi_lookahead_days,
                max_pages=settings.kalshi_max_pages,
                max_requests=settings.kalshi_max_requests,
                fetch_order_books=settings.kalshi_fetch_order_books,
                order_book_shortlist_limit=settings.kalshi_order_book_shortlist_limit,
            ).fetch_nfl_player_props(),
        )
        return odds + kalshi
    odds = DemoOddsProvider()
    kalshi = DemoKalshiProvider()
    a, b = await asyncio.gather(
        odds.fetch_nfl_player_props(),
        kalshi.fetch_nfl_player_props(),
    )
    return a + b

@app.get("/health")
async def health():
    return {"ok": True, "demo_mode": settings.demo_mode}

@app.get("/source-quality")
async def get_source_quality():
    rows = await load_rows()
    return source_quality(rows, settings.stale_after_seconds)

@app.get("/markets/latest")
async def latest_markets():
    rows = await load_rows()
    return [r.model_dump(mode="json") | {"implied_probability": r.implied_probability} for r in rows]

@app.get("/disagreements")
async def get_disagreements():
    rows = await load_rows()
    return disagreements(rows)

@app.get("/divergences")
async def get_divergences():
    rows = await load_rows()
    return market_divergences(
        rows,
        reference_bookmakers=settings.the_odds_api_reference_bookmakers,
        target_bookmaker=settings.the_odds_api_target_bookmaker,
        min_reference_books=settings.consensus_min_reference_books,
    )

@app.get("/cross-market-comparisons")
async def get_cross_market_comparisons():
    rows = await load_rows()
    return cross_market_comparisons(
        rows,
        reference_bookmakers=settings.the_odds_api_reference_bookmakers,
        target_bookmaker=settings.the_odds_api_target_bookmaker,
    )

if __name__ == "__main__":
    import uvicorn
    uvicorn.run("app.main:app", host="0.0.0.0", port=8000, reload=False)
