import asyncio
from datetime import datetime, timezone
import httpx
import pytest
from app.models import MarketType
from app.providers.kalshi import (
    KalshiApiError, KalshiProvider, discover_nfl_player_prop_series,
    normalize_kalshi_market,
)
NOW = datetime(2026, 9, 11, tzinfo=timezone.utc)
def market(**changes):
    value = {"ticker":"KXNFLRECYDS-PUKA-80","event_ticker":"KXNFLRECYDS-LARSF","series_ticker":"KXNFLRECYDS","title":"Puka Nacua: 80+ receiving yards","yes_bid":55,"yes_ask":61,"no_bid":39,"no_ask":45,"last_price":58,"volume":200,"open_interest":75,"close_time":"2026-09-13T20:00:00Z","updated_time":"2026-09-11T12:00:00Z"}
    value.update(changes); return value
def series(title="Pro Football Receiving Yards", ticker="KXNFLRECYDS", **changes):
    value = {"ticker": ticker, "title": title, "category": "Sports", "tags": ["Football"],
             "settlement_sources": [{"name": "the Governing League", "url": "https://www.nfl.com/"}]}
    value.update(changes); return value
def catalog(*items):
    return {"series": list(items or (series(),))}
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
def test_fixed_point_string_liquidity_schema_is_normalized_with_legacy_fallback():
    row=normalize_kalshi_market(market(volume=999,open_interest=888,volume_fp="200.50",open_interest_fp="75.00"),NOW)
    assert row.volume==200.5 and row.open_interest==75.0
    legacy=normalize_kalshi_market(market(volume=12,open_interest=3),NOW)
    assert legacy.volume==12 and legacy.open_interest==3
def test_invalid_or_missing_liquidity_is_not_fabricated():
    row=normalize_kalshi_market(market(volume=None,open_interest=None,volume_fp="unknown",open_interest_fp="-1"),NOW)
    assert row.volume is None and row.open_interest is None
def test_optional_orderbook_is_delayed_until_after_validation_and_preserved():
    requests=[]
    def handler(request):
        requests.append(request)
        if request.url.path.endswith('/series'): return httpx.Response(200,json=catalog())
        if request.url.path.endswith('/markets'): return httpx.Response(200,json={"markets":[market()],"cursor":""})
        return httpx.Response(200,json={"orderbook":{"yes_dollars":[["0.55",4]]}})
    async def fetch():
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            provider=KalshiProvider(client=client,fetch_order_books=True,now=lambda:NOW)
            return await provider.fetch_nfl_player_props(),provider.discovery_stats
    (rows,stats)=asyncio.run(fetch())
    assert rows[0].order_book_depth=={"yes_dollars":[["0.55",4]]} and rows[0].raw["market"]["ticker"]=="KXNFLRECYDS-PUKA-80"
    assert [r.method for r in requests]==["GET","GET","GET"]
    assert stats.series_list_requests==1 and stats.market_list_requests==1 and stats.order_book_requests==1

def test_server_filters_window_and_default_avoids_orderbooks():
    requests=[]
    def handler(request):
        requests.append(request)
        if request.url.path.endswith('/series'): return httpx.Response(200,json=catalog())
        return httpx.Response(200,json={"markets":[market(),market(ticker="KXNFL-OLD",close_time="2026-09-10T20:00:00Z")],"cursor":""})
    async def fetch():
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            provider=KalshiProvider(client=client,series_tickers=("KXNFLRECYDS",),lookahead_days=3,now=lambda:NOW)
            return await provider.fetch_nfl_player_props(),provider.discovery_stats
    rows,stats=asyncio.run(fetch())
    query=requests[1].url.params
    assert requests[0].url.params["tags"] == "Football"
    assert query["series_ticker"]=="KXNFLRECYDS" and query["status"]=="open"
    assert query["min_close_ts"]==str(int(NOW.timestamp()))
    assert query["max_close_ts"]==str(int((NOW.replace(day=14)).timestamp()))
    assert len(rows)==1 and len(requests)==2
    assert stats.as_dict() | {} == stats.as_dict()
    assert (stats.pages_retrieved,stats.markets_inspected,stats.nfl_candidates,stats.supported_contracts)==(1,2,2,1)

def test_page_and_request_safety_limits_are_deterministic():
    requests=[]
    def handler(request):
        requests.append(request)
        if request.url.path.endswith('/series'): return httpx.Response(200,json=catalog())
        return httpx.Response(200,json={"markets":[],"cursor":"again"})
    async def fetch():
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            provider=KalshiProvider(client=client,max_pages=2,max_requests=3,now=lambda:NOW)
            return await provider.fetch_nfl_player_props(),provider.discovery_stats
    rows,stats=asyncio.run(fetch())
    assert rows==[] and len(requests)==3
    assert stats.pages_retrieved==2 and stats.market_list_requests==2

def test_invalid_contracts_never_trigger_orderbook_requests():
    paths=[]
    def handler(request):
        paths.append(request.url.path)
        if request.url.path.endswith('/series'): return httpx.Response(200,json=catalog())
        return httpx.Response(200,json={"markets":[market(title="Puka yards maybe"),market(ticker="KXNBA-X",event_ticker="KXNBA-LALBOS",series_ticker="KXNBA")],"cursor":""})
    async def fetch():
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            provider=KalshiProvider(client=client,fetch_order_books=True,now=lambda:NOW)
            return await provider.fetch_nfl_player_props(),provider.discovery_stats
    rows,stats=asyncio.run(fetch())
    assert rows==[] and paths==["/trade-api/v2/series","/trade-api/v2/markets"] and stats.order_book_requests==0

def test_series_discovery_maps_exact_supported_nfl_metadata():
    payload = catalog(
        series("Pro Football Passing Yards", "KXNFLPASSYDS"),
        series("Pro Football Receiving Yards", "KXNFLRECYDS"),
        series("Pro Football Player Receptions", "KXNFLREC"),
        series("Pro Football Rushing Yards", "KXNFLRSHYDS"),
        series("Pro Football Passing Yards", "KXNCAAFPASSYDS",
               settlement_sources=[{"url": "https://www.ncaa.com/"}]),
        series("Pro Football Passing Yards", "KXBAD", tags=["Basketball"]),
    )
    assert discover_nfl_player_prop_series(payload) == {
        "KXNFLPASSYDS": MarketType.PASS_YDS,
        "KXNFLRECYDS": MarketType.RECEPTION_YDS,
        "KXNFLREC": MarketType.RECEPTIONS,
    }

def test_configured_series_are_an_allowlist_of_discovered_metadata():
    requests=[]
    def handler(request):
        requests.append(request)
        if request.url.path.endswith('/series'):
            return httpx.Response(200,json=catalog(
                series("Pro Football Passing Yards", "KXNFLPASSYDS"),
                series("Pro Football Receiving Yards", "KXNFLRECYDS")))
        return httpx.Response(200,json={"markets":[],"cursor":""})
    async def fetch():
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            provider=KalshiProvider(client=client,series_tickers=("KXNFLPASSYDS",),now=lambda:NOW)
            return await provider.fetch_nfl_player_props(),provider.discovery_stats
    rows,stats=asyncio.run(fetch())
    assert rows == []
    assert stats.discovered_series == {"KXNFLPASSYDS": "player_pass_yds"}
    assert [request.url.path for request in requests] == ["/trade-api/v2/series", "/trade-api/v2/markets"]

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
