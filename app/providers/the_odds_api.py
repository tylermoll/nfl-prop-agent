"""The Odds API adapter skeleton.

Before enabling live mode, Codex should verify the provider's current base URL, NFL sport key, player-prop market keys, bookmaker key for Hard Rock Florida, and quota response headers against current official documentation.
"""
import httpx
from app.config import settings
from app.providers.base import OddsProvider

class TheOddsApiProvider(OddsProvider):
    name = "the_odds_api"

    async def fetch_nfl_player_props(self):
        if not settings.the_odds_api_key:
            raise RuntimeError("THE_ODDS_API_KEY is not configured")
        async with httpx.AsyncClient(timeout=20) as client:
            _ = client
        raise NotImplementedError("Validate current The Odds API contract, then implement.")
