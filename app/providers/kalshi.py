"""Kalshi adapter skeleton.

Codex should implement read-only market data first: markets, order books, trades, volume and timestamps. Authentication/signing must follow current official Kalshi documentation. Do not implement order placement in V1.
"""
from app.providers.base import PredictionMarketProvider

class KalshiProvider(PredictionMarketProvider):
    name = "kalshi"

    async def fetch_nfl_player_props(self):
        raise NotImplementedError("Validate current Kalshi market-data contract, then implement.")
