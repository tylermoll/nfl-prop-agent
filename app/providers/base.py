from abc import ABC, abstractmethod
from app.models import MarketSnapshot

class OddsProvider(ABC):
    name: str

    @abstractmethod
    async def fetch_nfl_player_props(self) -> list[MarketSnapshot]:
        ...

class PredictionMarketProvider(ABC):
    name: str

    @abstractmethod
    async def fetch_nfl_player_props(self) -> list[MarketSnapshot]:
        ...
