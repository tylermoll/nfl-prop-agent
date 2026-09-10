"""Read-only The Odds API v4 adapter.

Contract verified against the official v4 documentation on 2026-09-10:
https://the-odds-api.com/liveapi/guides/v4/

Player props are event-level markets.  The adapter therefore discovers NFL
events first and requests odds for each event.  It never places wagers.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Awaitable, Callable
from datetime import datetime, timezone
from typing import Any

import httpx

from app.config import settings
from app.models import MarketSnapshot, MarketType, Side
from app.providers.base import OddsProvider

logger = logging.getLogger(__name__)

BASE_URL = "https://api.the-odds-api.com/v4"
NFL_SPORT_KEY = "americanfootball_nfl"
MARKET_MAP = {
    "player_pass_yds": MarketType.PASS_YDS,
    "player_reception_yds": MarketType.RECEPTION_YDS,
    "player_receptions": MarketType.RECEPTIONS,
}
SIDE_MAP = {"Over": Side.OVER, "Under": Side.UNDER}
QUOTA_HEADERS = (
    "x-requests-used",
    "x-requests-remaining",
    "x-requests-last",
)


class TheOddsApiError(RuntimeError):
    """The provider returned an unusable response."""


class TheOddsApiProvider(OddsProvider):
    """Fetch and normalize current, pre-game NFL player props."""

    name = "the_odds_api"

    def __init__(
        self,
        api_key: str | None = None,
        *,
        bookmakers: tuple[str, ...] = ("hardrockbet",),
        timeout: float = 20,
        max_retries: int = 3,
        client: httpx.AsyncClient | None = None,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
    ) -> None:
        self.api_key = api_key if api_key is not None else settings.the_odds_api_key
        self.bookmakers = bookmakers
        self.timeout = timeout
        self.max_retries = max_retries
        self._client = client
        self._sleep = sleep
        self.usage: dict[str, int | None] = {
            "requests_used": None,
            "requests_remaining": None,
            "requests_last": None,
            "http_requests": 0,
        }
        self.upcoming_event_count = 0

    async def fetch_nfl_player_props(self) -> list[MarketSnapshot]:
        if not self.api_key:
            raise RuntimeError("THE_ODDS_API_KEY is not configured")

        owns_client = self._client is None
        client = self._client or httpx.AsyncClient(timeout=self.timeout)
        try:
            events = await self._get_json(
                client, f"/sports/{NFL_SPORT_KEY}/events", {"dateFormat": "iso"}
            )
            if not isinstance(events, list):
                raise TheOddsApiError("events response must be a JSON array")

            rows: list[MarketSnapshot] = []
            now = datetime.now(timezone.utc)
            upcoming_events: list[dict[str, Any]] = []
            for event in events:
                event_id = event.get("id") if isinstance(event, dict) else None
                if not isinstance(event_id, str) or not event_id:
                    logger.warning("Skipping The Odds API event without a valid id")
                    continue
                commence = _parse_datetime(event.get("commence_time"))
                if commence is not None and commence < now:
                    continue
                upcoming_events.append(event)
            self.upcoming_event_count = len(upcoming_events)
            for event in upcoming_events:
                event_id = event["id"]
                payload = await self._get_json(
                    client,
                    f"/sports/{NFL_SPORT_KEY}/events/{event_id}/odds",
                    {
                        "bookmakers": ",".join(self.bookmakers),
                        "markets": ",".join(MARKET_MAP),
                        "oddsFormat": "american",
                        "dateFormat": "iso",
                    },
                )
                rows.extend(self._normalize_event(payload, now))
            return rows
        finally:
            if owns_client:
                await client.aclose()

    async def _get_json(
        self, client: httpx.AsyncClient, path: str, params: dict[str, str]
    ) -> Any:
        request_params = {"apiKey": self.api_key, **params}
        for attempt in range(self.max_retries + 1):
            try:
                self.usage["http_requests"] = int(self.usage["http_requests"] or 0) + 1
                response = await client.get(f"{BASE_URL}{path}", params=request_params)
                self._capture_usage(response.headers)
                if response.status_code == 429 or response.status_code >= 500:
                    if attempt < self.max_retries:
                        retry_after = response.headers.get("retry-after")
                        delay = float(retry_after) if retry_after else 0.5 * (2**attempt)
                        await self._sleep(delay)
                        continue
                try:
                    response.raise_for_status()
                except httpx.HTTPStatusError as exc:
                    raise TheOddsApiError(
                        f"The Odds API returned HTTP {response.status_code} for {path}"
                    ) from exc
                try:
                    return response.json()
                except ValueError as exc:
                    raise TheOddsApiError(f"invalid JSON from {path}") from exc
            except (httpx.TimeoutException, httpx.NetworkError) as exc:
                if attempt >= self.max_retries:
                    raise TheOddsApiError(f"request failed for {path}") from exc
                await self._sleep(0.5 * (2**attempt))
        raise AssertionError("retry loop exited unexpectedly")

    def _capture_usage(self, headers: httpx.Headers) -> None:
        for header in QUOTA_HEADERS:
            value = headers.get(header)
            if value is not None:
                try:
                    self.usage[header.removeprefix("x-").replace("-", "_")] = int(value)
                except ValueError:
                    logger.warning("Ignoring malformed quota header %s=%r", header, value)

    def _normalize_event(
        self, payload: Any, observed_at: datetime
    ) -> list[MarketSnapshot]:
        if not isinstance(payload, dict) or not isinstance(payload.get("id"), str):
            raise TheOddsApiError("event odds response lacks a valid event id")

        rows: list[MarketSnapshot] = []
        event_id = payload["id"]
        bookmakers = payload.get("bookmakers", [])
        if not isinstance(bookmakers, list):
            raise TheOddsApiError("event bookmakers must be a JSON array")
        for bookmaker in bookmakers:
            if not isinstance(bookmaker, dict) or not isinstance(bookmaker.get("key"), str):
                logger.warning("Skipping malformed bookmaker in event %s", event_id)
                continue
            book_key = bookmaker["key"]
            updated = _parse_datetime(bookmaker.get("last_update"))
            markets = bookmaker.get("markets", [])
            if not isinstance(markets, list):
                logger.warning("Skipping malformed markets for %s", book_key)
                continue
            for market in markets:
                market_key = market.get("key") if isinstance(market, dict) else None
                if market_key not in MARKET_MAP:
                    logger.warning("Skipping unknown The Odds API market %r", market_key)
                    continue
                market_updated = _parse_datetime(market.get("last_update")) or updated
                outcomes = market.get("outcomes", [])
                if not isinstance(outcomes, list):
                    logger.warning("Skipping malformed outcomes for %s", market_key)
                    continue
                for outcome in outcomes:
                    row = self._normalize_outcome(
                        outcome,
                        event_id,
                        book_key,
                        market_key,
                        market_updated,
                        observed_at,
                        payload,
                    )
                    if row is not None:
                        rows.append(row)
        return rows

    @staticmethod
    def _normalize_outcome(
        outcome: Any,
        event_id: str,
        book_key: str,
        market_key: str,
        updated: datetime | None,
        observed_at: datetime,
        raw_response: dict[str, Any],
    ) -> MarketSnapshot | None:
        if not isinstance(outcome, dict):
            logger.warning("Skipping non-object outcome for %s", market_key)
            return None
        player = outcome.get("description")
        side = SIDE_MAP.get(outcome.get("name"))
        point = outcome.get("point")
        price = outcome.get("price")
        if (
            not isinstance(player, str)
            or not player.strip()
            or side is None
            or not isinstance(point, (int, float))
            or isinstance(point, bool)
            or not isinstance(price, int)
            or isinstance(price, bool)
            or price == 0
        ):
            logger.warning("Skipping malformed outcome for %s without coercion", market_key)
            return None
        player = player.strip()
        return MarketSnapshot(
            source=book_key,
            source_market_id=f"{event_id}:{book_key}:{market_key}:{player}",
            game_id=event_id,
            player_name=player,
            market_type=MARKET_MAP[market_key],
            line=float(point),
            side=side,
            american_odds=price,
            observed_at_utc=observed_at,
            source_updated_at_utc=updated,
            raw={
                "response": raw_response,
                "bookmaker": book_key,
                "market": market_key,
                "outcome": outcome,
            },
        )


def _parse_datetime(value: Any) -> datetime | None:
    if not isinstance(value, str):
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        if parsed.tzinfo is None:
            return parsed.replace(tzinfo=timezone.utc)
        return parsed.astimezone(timezone.utc)
    except ValueError:
        logger.warning("Ignoring malformed provider timestamp %r", value)
        return None
