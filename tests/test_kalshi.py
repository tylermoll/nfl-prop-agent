import asyncio
from datetime import datetime, timezone
import httpx
import pytest
from app.models import MarketType
from app.providers.kalshi import KalshiApiError, KalshiProvider, normalize_kalshi_market
NOW = datetime(2026, 9, 11, tzinfo=timezone.utc)
def market(**changes):
    value = {"ticker":"KXNFL-PUKA-80","event_ticker":"LAR-SF","title":"Puka Nacua: 80+ receiving yards","yes_bid":55,"yes_ask":61,"no_bid":39,"no_ask":45,"last_price":58,"volume":200,"open_interest":75,"updated_time":"2026-09-11T12:00:00Z"}
    value.update(changes); return value
def test_exact_player_threshold_and_bid_ask():
    row=normalize_kalshi_market(market(),NOW)
    assert (row.player_name,row.market_type,row.line)==("Puka Nacua",MarketType.RECEPTION_YDS,79.5)
    assert row.yes_bid==.55 and row.yes_ask==.61 and row.contract_price==pytest.approx(.58)
def test_ambiguous_market_is_logged_and_excluded(caplog):
    assert normalize_kalshi_market(market(title="Puka receiving yards Sunday"),NOW) is None
    assert "ambiguous" in caplog.text
def test_absent_liquidity_is_not_invented():
    row=normalize_kalshi_market(market(yes_bid=None,yes_ask=None,volume=None,open_interest=None),NOW)
    assert row.contract_price is None and row.volume is None and row.open_interest is None
def test_mock_fetch_preserves_raw_orderbook():
    def handler(request):
        if request.url.path.endswith('/markets'): return httpx.Response(200,json={"markets":[market()],"cursor":""})
        return httpx.Response(200,json={"orderbook":{"yes_dollars":[["0.55",4]]}})
    async def fetch():
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client: return await KalshiProvider(client=client).fetch_nfl_player_props()
    rows=asyncio.run(fetch())
    assert rows[0].order_book_depth=={"yes_dollars":[["0.55",4]]} and rows[0].raw["market"]["ticker"]=="KXNFL-PUKA-80"
def test_error_is_secret_safe():
    async def fetch():
        async with httpx.AsyncClient(transport=httpx.MockTransport(lambda r:httpx.Response(401,text="secret-body"))) as client: return await KalshiProvider(client=client).fetch_nfl_player_props()
    with pytest.raises(KalshiApiError) as caught: asyncio.run(fetch())
    assert "secret-body" not in str(caught.value)
def test_no_order_placement_surface():
    assert not hasattr(KalshiProvider,"place_order") and not hasattr(KalshiProvider,"place_wager")
