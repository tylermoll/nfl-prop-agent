import asyncio
from datetime import datetime, timezone
import httpx
import pytest
from app.models import MarketType
from app.providers.kalshi import KalshiApiError, KalshiProvider, normalize_kalshi_market
NOW = datetime(2026, 9, 11, tzinfo=timezone.utc)
def market(**changes):
    value = {"ticker":"KXNFL-PUKA-80","event_ticker":"KXNFL-LARSF","series_ticker":"KXNFL","title":"Puka Nacua: 80+ receiving yards","yes_bid":55,"yes_ask":61,"no_bid":39,"no_ask":45,"last_price":58,"volume":200,"open_interest":75,"close_time":"2026-09-13T20:00:00Z","updated_time":"2026-09-11T12:00:00Z"}
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
def test_optional_orderbook_is_delayed_until_after_validation_and_preserved():
    requests=[]
    def handler(request):
        requests.append(request)
        if request.url.path.endswith('/markets'): return httpx.Response(200,json={"markets":[market()],"cursor":""})
        return httpx.Response(200,json={"orderbook":{"yes_dollars":[["0.55",4]]}})
    async def fetch():
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            provider=KalshiProvider(client=client,fetch_order_books=True,now=lambda:NOW)
            return await provider.fetch_nfl_player_props(),provider.discovery_stats
    (rows,stats)=asyncio.run(fetch())
    assert rows[0].order_book_depth=={"yes_dollars":[["0.55",4]]} and rows[0].raw["market"]["ticker"]=="KXNFL-PUKA-80"
    assert [r.method for r in requests]==["GET","GET"]
    assert stats.market_list_requests==1 and stats.order_book_requests==1

def test_server_filters_window_and_default_avoids_orderbooks():
    requests=[]
    def handler(request):
        requests.append(request)
        return httpx.Response(200,json={"markets":[market(),market(ticker="KXNFL-OLD",close_time="2026-09-10T20:00:00Z")],"cursor":""})
    async def fetch():
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            provider=KalshiProvider(client=client,series_tickers=("KXNFL",),lookahead_days=3,now=lambda:NOW)
            return await provider.fetch_nfl_player_props(),provider.discovery_stats
    rows,stats=asyncio.run(fetch())
    query=requests[0].url.params
    assert query["series_ticker"]=="KXNFL" and query["status"]=="open"
    assert query["min_close_ts"]==str(int(NOW.timestamp()))
    assert query["max_close_ts"]==str(int((NOW.replace(day=14)).timestamp()))
    assert len(rows)==1 and len(requests)==1
    assert stats.as_dict() | {} == stats.as_dict()
    assert (stats.pages_retrieved,stats.markets_inspected,stats.nfl_candidates,stats.supported_contracts)==(1,2,2,1)

def test_page_and_request_safety_limits_are_deterministic():
    requests=[]
    def handler(request):
        requests.append(request)
        return httpx.Response(200,json={"markets":[],"cursor":"again"})
    async def fetch():
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            provider=KalshiProvider(client=client,max_pages=2,max_requests=2,now=lambda:NOW)
            return await provider.fetch_nfl_player_props(),provider.discovery_stats
    rows,stats=asyncio.run(fetch())
    assert rows==[] and len(requests)==2
    assert stats.pages_retrieved==2 and stats.market_list_requests==2

def test_invalid_contracts_never_trigger_orderbook_requests():
    paths=[]
    def handler(request):
        paths.append(request.url.path)
        return httpx.Response(200,json={"markets":[market(title="Puka yards maybe"),market(ticker="KXNBA-X",event_ticker="KXNBA-LALBOS",series_ticker="KXNBA")],"cursor":""})
    async def fetch():
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            provider=KalshiProvider(client=client,fetch_order_books=True,now=lambda:NOW)
            return await provider.fetch_nfl_player_props(),provider.discovery_stats
    rows,stats=asyncio.run(fetch())
    assert rows==[] and paths==["/trade-api/v2/markets"] and stats.order_book_requests==0

@pytest.mark.parametrize(("title","expected"),[
    ("Patrick Mahomes: 250+ passing yards",MarketType.PASS_YDS),
    ("Justin Jefferson: 70+ receiving yards",MarketType.RECEPTION_YDS),
    ("CeeDee Lamb: 6+ receptions",MarketType.RECEPTIONS),
])
def test_only_canonical_player_market_thresholds(title,expected):
    row=normalize_kalshi_market(market(title=title),NOW)
    assert row is not None and row.market_type==expected

@pytest.mark.parametrize("title",["Patrick Mahomes passing yards","Team total: 21+ receptions","Patrick Mahomes: lots of passing yards"])
def test_missing_player_or_numeric_threshold_is_excluded(title):
    assert normalize_kalshi_market(market(title=title),NOW) is None
def test_error_is_secret_safe():
    async def fetch():
        async with httpx.AsyncClient(transport=httpx.MockTransport(lambda r:httpx.Response(401,text="secret-body"))) as client: return await KalshiProvider(client=client).fetch_nfl_player_props()
    with pytest.raises(KalshiApiError) as caught: asyncio.run(fetch())
    assert "secret-body" not in str(caught.value)
def test_no_order_placement_surface():
    assert not hasattr(KalshiProvider,"place_order") and not hasattr(KalshiProvider,"place_wager")
