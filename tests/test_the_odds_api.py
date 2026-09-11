from datetime import datetime, timedelta, timezone
import asyncio

import httpx
import pytest

from app.models import MarketType, Side
from app.providers.the_odds_api import TheOddsApiError, TheOddsApiProvider


def event_payload():
    return {
        "id": "event-1",
        "sport_key": "americanfootball_nfl",
        "bookmakers": [
            {
                "key": "hardrockbet",
                "last_update": "2026-09-10T16:00:00Z",
                "markets": [
                    {
                        "key": "player_reception_yds",
                        "last_update": "2026-09-10T16:01:00Z",
                        "outcomes": [
                            {"name": "Over", "description": "Puka Nacua", "price": -115, "point": 78.5},
                            {"name": "Under", "description": "Puka Nacua", "price": -105, "point": 78.5},
                        ],
                    }
                ],
            }
        ],
    }


def run(coroutine):
    return asyncio.run(coroutine)


def test_fetches_event_props_and_prioritizes_hard_rock():
    future = (datetime.now(timezone.utc) + timedelta(days=1)).isoformat()

    def handler(request: httpx.Request):
        assert request.url.params["apiKey"] == "secret"
        if request.url.path.endswith("/events"):
            return httpx.Response(200, json=[{"id": "event-1", "commence_time": future}])
        assert request.url.params["bookmakers"] == "hardrockbet"
        assert set(request.url.params["markets"].split(",")) == {
            "player_pass_yds", "player_reception_yds", "player_receptions"
        }
        return httpx.Response(
            200,
            json=event_payload(),
            headers={"x-requests-used": "7", "x-requests-remaining": "493", "x-requests-last": "3"},
        )

    async def fetch():
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            provider = TheOddsApiProvider("secret", client=client)
            return await provider.fetch_nfl_player_props(), provider

    rows, provider = run(fetch())

    assert len(rows) == 2
    assert rows[0].source == "hardrockbet"
    assert rows[0].market_type is MarketType.RECEPTION_YDS
    assert rows[0].side is Side.OVER
    assert rows[0].player_name == "Puka Nacua"
    assert rows[0].raw["outcome"]["price"] == -115
    assert rows[0].source_updated_at_utc == datetime(2026, 9, 10, 16, 1, tzinfo=timezone.utc)
    assert provider.usage == {
        "requests_used": 7, "requests_remaining": 493, "requests_last": 3,
        "http_requests": 2, "quota_consumed": 3
    }
    assert provider.discovered_event_count == 1
    assert provider.eligible_event_count == 1


def test_skips_started_events():
    past = (datetime.now(timezone.utc) - timedelta(minutes=1)).isoformat()
    calls = 0

    def handler(request: httpx.Request):
        nonlocal calls
        calls += 1
        return httpx.Response(200, json=[{"id": "old", "commence_time": past}])

    async def fetch():
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            return await TheOddsApiProvider("secret", client=client).fetch_nfl_player_props()

    rows = run(fetch())
    assert rows == []
    assert calls == 1


def test_does_not_query_events_outside_default_four_day_window():
    now = datetime.now(timezone.utc)
    events = [
        {"id": "eligible", "commence_time": (now + timedelta(days=3)).isoformat()},
        {"id": "too-far", "commence_time": (now + timedelta(days=5)).isoformat()},
        {"id": "unknown", "commence_time": "not-a-date"},
    ]
    requested_event_ids = []

    def handler(request: httpx.Request):
        if request.url.path.endswith("/events"):
            return httpx.Response(200, json=events)
        requested_event_ids.append(request.url.path.split("/")[-2])
        payload = event_payload()
        payload["id"] = "eligible"
        return httpx.Response(200, json=payload)

    async def fetch():
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            provider = TheOddsApiProvider("secret", client=client)
            await provider.fetch_nfl_player_props()
            return provider

    provider = run(fetch())
    assert requested_event_ids == ["eligible"]
    assert provider.discovered_event_count == 3
    assert provider.eligible_event_count == 1
    assert provider.usage["http_requests"] == 2


def test_lookahead_window_is_configurable():
    future = (datetime.now(timezone.utc) + timedelta(days=6)).isoformat()
    paths = []

    def handler(request: httpx.Request):
        paths.append(request.url.path)
        if request.url.path.endswith("/events"):
            return httpx.Response(200, json=[{"id": "day-six", "commence_time": future}])
        payload = event_payload()
        payload["id"] = "day-six"
        return httpx.Response(200, json=payload)

    async def fetch():
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            provider = TheOddsApiProvider("secret", client=client, lookahead_days=7)
            await provider.fetch_nfl_player_props()
            return provider

    provider = run(fetch())
    assert any("day-six/odds" in path for path in paths)
    assert provider.eligible_event_count == 1


def test_retries_rate_limit_then_succeeds():
    attempts = 0
    sleeps = []

    async def fake_sleep(delay):
        sleeps.append(delay)

    def handler(request: httpx.Request):
        nonlocal attempts
        attempts += 1
        if attempts == 1:
            return httpx.Response(429, headers={"retry-after": "2"})
        return httpx.Response(200, json=[])

    async def fetch():
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            provider = TheOddsApiProvider("secret", client=client, sleep=fake_sleep)
            return await provider.fetch_nfl_player_props()

    assert run(fetch()) == []
    assert attempts == 2
    assert sleeps == [2.0]


def test_invalid_json_is_reported():
    def handler(request: httpx.Request):
        return httpx.Response(200, text="not json")

    async def fetch():
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            return await TheOddsApiProvider("secret", client=client).fetch_nfl_player_props()

    with pytest.raises(TheOddsApiError, match="invalid JSON"):
        run(fetch())


def test_malformed_outcome_is_not_coerced(caplog):
    payload = event_payload()
    payload["bookmakers"][0]["markets"][0]["outcomes"] = [
        {"name": "Higher", "description": None, "price": "-110", "point": "78.5"}
    ]
    provider = TheOddsApiProvider("secret")
    assert provider._normalize_event(payload, datetime.now(timezone.utc)) == []
    assert "without coercion" in caplog.text


def test_http_error_does_not_expose_api_key():
    def handler(request: httpx.Request):
        return httpx.Response(401, json={"message": "unauthorized"})

    async def fetch():
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            return await TheOddsApiProvider("super-secret", client=client).fetch_nfl_player_props()

    with pytest.raises(TheOddsApiError) as caught:
        run(fetch())
    assert "super-secret" not in str(caught.value)
    assert str(caught.value).endswith("/sports/americanfootball_nfl/events")
