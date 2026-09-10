import asyncio
from fastapi import FastAPI
from app.config import settings
from app.providers.demo import DemoOddsProvider, DemoKalshiProvider
from app.providers.the_odds_api import TheOddsApiProvider
from app.services import source_quality, disagreements

app = FastAPI(title="NFL Prop Agent V1")

async def load_rows():
    if not settings.demo_mode:
        return await TheOddsApiProvider().fetch_nfl_player_props()
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

if __name__ == "__main__":
    import uvicorn
    uvicorn.run("app.main:app", host="0.0.0.0", port=8000, reload=False)
