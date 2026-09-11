from datetime import datetime,timezone
from app.cross_market import cross_market_comparisons
from app.models import MarketSnapshot,MarketType,Side
from app.providers.kalshi import normalize_kalshi_market
def sportsbook(book,side,odds):
    return MarketSnapshot(source=book,source_market_id=f"{book}-{side}",game_id="sports-id",event_name="Los Angeles Rams at San Francisco 49ers",player_name="Puka Nacua",market_type=MarketType.RECEPTION_YDS,line=79.5,side=side,american_odds=odds)
def test_clean_cross_market_match_and_direction():
    rows=[sportsbook(book,side,odds) for book in ("hardrockbet","draftkings") for side,odds in ((Side.OVER,-120),(Side.UNDER,100))]
    kalshi=normalize_kalshi_market({"ticker":"KXNFL-k","event_ticker":"KXNFL-other-id","event_title":"Los Angeles Rams at San Francisco 49ers","title":"Puka Nacua: 80+ receiving yards","yes_bid":55,"yes_ask":61,"volume":20,"open_interest":9},datetime.now(timezone.utc))
    result=cross_market_comparisons(rows+[kalshi],reference_bookmakers=("draftkings",))
    assert len(result)==1 and result[0]["threshold"]==79.5
    assert result[0]["kalshi_spread"]==.06 and result[0]["direction_agreement"]=="same"
