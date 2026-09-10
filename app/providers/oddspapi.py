"""OddsPapi adapter skeleton.

V1 validates whether the selected plan returns NFL player props, whether Hard Rock Florida is available, and freshness/coverage. Do not assume free-plan player-prop availability; detect capabilities from actual responses.
"""
import httpx
from app.config import settings
from app.providers.base import OddsProvider

class OddsPapiProvider(OddsProvider):
    name = "oddspapi"

    async def fetch_nfl_player_props(self):
        if not settings.oddspapi_api_key:
            raise RuntimeError("ODDSPAPI_API_KEY is not configured")
        async with httpx.AsyncClient(timeout=20) as client:
            _ = client
        raise NotImplementedError("Validate current OddsPapi contract, then implement.")
