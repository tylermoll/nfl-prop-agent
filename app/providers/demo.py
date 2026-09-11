from datetime import datetime, timezone, timedelta
from app.models import MarketSnapshot, MarketType, Side
from app.providers.base import OddsProvider, PredictionMarketProvider

NOW = datetime.now(timezone.utc)

class DemoOddsProvider(OddsProvider):
    name = "demo_sportsbooks"

    async def fetch_nfl_player_props(self):
        rows = []
        books = [
            ("hardrockbet", 78.5, -105, -115),
            ("draftkings", 80.5, -110, -110),
            ("fanduel", 80.5, -115, -105),
            ("betmgm", 79.5, -115, -105),
            ("williamhill_us", 80.5, -110, -110),
        ]
        for book, line, over, under in books:
            for side, odds in [(Side.OVER, over), (Side.UNDER, under)]:
                rows.append(MarketSnapshot(
                    source=book,
                    source_market_id=f"{book}:demo:puka:recyds",
                    game_id="DEMO-LAR-SF",
                    player_name="Puka Nacua",
                    team="LAR",
                    market_type=MarketType.RECEPTION_YDS,
                    line=line,
                    side=side,
                    american_odds=odds,
                    source_updated_at_utc=NOW - timedelta(seconds=25 if book=="hardrockbet" else 10),
                    raw={"demo": True},
                ))
        return rows

class DemoKalshiProvider(PredictionMarketProvider):
    name = "kalshi_demo"

    async def fetch_nfl_player_props(self):
        return [MarketSnapshot(
            source="kalshi",
            source_market_id="DEMO-KALSHI-PUKA-79",
            game_id="DEMO-LAR-SF",
            player_name="Puka Nacua",
            team="LAR",
            market_type=MarketType.RECEPTION_YDS,
            line=79.5,
            side=Side.YES,
            contract_price=0.59,
            source_updated_at_utc=NOW - timedelta(seconds=5),
            raw={"demo": True, "volume": 31400},
        )]
