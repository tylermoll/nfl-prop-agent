from datetime import datetime, timezone
from enum import Enum
from pydantic import BaseModel, Field
from app.math_utils import american_to_probability

class MarketType(str, Enum):
    PASS_YDS = "player_pass_yds"
    RECEPTION_YDS = "player_reception_yds"
    RECEPTIONS = "player_receptions"

class Side(str, Enum):
    OVER = "over"
    UNDER = "under"
    YES = "yes"
    NO = "no"

class MarketSnapshot(BaseModel):
    source: str
    source_market_id: str
    game_id: str
    event_name: str | None = None
    home_team: str | None = None
    away_team: str | None = None
    player_name: str
    team: str | None = None
    market_type: MarketType
    line: float | None = None
    side: Side
    american_odds: int | None = None
    contract_price: float | None = Field(default=None, ge=0, le=1)
    yes_bid: float | None = Field(default=None, ge=0, le=1)
    yes_ask: float | None = Field(default=None, ge=0, le=1)
    no_bid: float | None = Field(default=None, ge=0, le=1)
    no_ask: float | None = Field(default=None, ge=0, le=1)
    last_traded_price: float | None = Field(default=None, ge=0, le=1)
    volume: float | None = Field(default=None, ge=0)
    open_interest: float | None = Field(default=None, ge=0)
    order_book_depth: dict | None = None
    observed_at_utc: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
    source_updated_at_utc: datetime | None = None
    raw: dict = Field(default_factory=dict)

    @property
    def implied_probability(self) -> float | None:
        if self.contract_price is not None:
            return self.contract_price
        if self.american_odds is None:
            return None
        return american_to_probability(self.american_odds)
