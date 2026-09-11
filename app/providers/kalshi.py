"""Read-only Kalshi market-data adapter.

The public Trade API v2 market and order-book routes are used.  This module
intentionally contains no trading or order-entry surface.
"""

from __future__ import annotations

import asyncio
import logging
import re
from collections.abc import Awaitable, Callable
from datetime import datetime, timezone
from typing import Any

import httpx

from app.models import MarketSnapshot, MarketType, Side
from app.providers.base import PredictionMarketProvider
from app.providers.the_odds_api import _parse_datetime

logger = logging.getLogger(__name__)
BASE_URL = "https://api.elections.kalshi.com/trade-api/v2"
STAT_MAP = {
    "passing yards": MarketType.PASS_YDS,
    "receiving yards": MarketType.RECEPTION_YDS,
    "receptions": MarketType.RECEPTIONS,
}
PATTERNS = (
    re.compile(r"^(?P<player>[A-Za-z][A-Za-z .'-]+?)\s*[:|-]\s*(?P<threshold>\d+(?:\.\d+)?)\+?\s+(?P<stat>passing yards|receiving yards|receptions)$", re.I),
    re.compile(r"^Will (?P<player>[A-Za-z][A-Za-z .'-]+?) (?:have|record|get) (?P<threshold>\d+(?:\.\d+)?)\+? (?P<stat>passing yards|receiving yards|receptions)(?:\?)?$", re.I),
)


class KalshiApiError(RuntimeError):
    pass


class KalshiProvider(PredictionMarketProvider):
    name = "kalshi"

    def __init__(self, *, client: httpx.AsyncClient | None = None, timeout: float = 20,
                 max_retries: int = 3, sleep: Callable[[float], Awaitable[None]] = asyncio.sleep):
        self._client, self.timeout, self.max_retries, self._sleep = client, timeout, max_retries, sleep

    async def fetch_nfl_player_props(self) -> list[MarketSnapshot]:
        owns = self._client is None
        client = self._client or httpx.AsyncClient(timeout=self.timeout)
        try:
            rows, cursor = [], None
            while True:
                params = {"status": "open", "limit": "1000"}
                if cursor:
                    params["cursor"] = cursor
                payload = await self._get_json(client, "/markets", params)
                markets = payload.get("markets") if isinstance(payload, dict) else None
                if not isinstance(markets, list):
                    raise KalshiApiError("Kalshi markets response lacks a markets array")
                observed = datetime.now(timezone.utc)
                for market in markets:
                    parsed = normalize_kalshi_market(market, observed)
                    if parsed is None:
                        continue
                    book = await self._get_json(client, f"/markets/{parsed.source_market_id}/orderbook", {"depth": "100"})
                    parsed.order_book_depth = book.get("orderbook") if isinstance(book, dict) else None
                    parsed.raw["orderbook_response"] = book
                    rows.append(parsed)
                cursor = payload.get("cursor")
                if not isinstance(cursor, str) or not cursor:
                    return rows
        finally:
            if owns:
                await client.aclose()

    async def _get_json(self, client: httpx.AsyncClient, path: str, params: dict[str, str]) -> Any:
        for attempt in range(self.max_retries + 1):
            try:
                response = await client.get(f"{BASE_URL}{path}", params=params)
                if response.status_code == 429 or response.status_code >= 500:
                    if attempt < self.max_retries:
                        await self._sleep(0.5 * 2**attempt)
                        continue
                try:
                    response.raise_for_status()
                except httpx.HTTPStatusError as exc:
                    raise KalshiApiError(f"Kalshi returned HTTP {response.status_code} for {path}") from exc
                try:
                    return response.json()
                except ValueError as exc:
                    raise KalshiApiError(f"invalid JSON from Kalshi {path}") from exc
            except (httpx.TimeoutException, httpx.NetworkError) as exc:
                if attempt == self.max_retries:
                    raise KalshiApiError(f"Kalshi request failed for {path}") from exc
                await self._sleep(0.5 * 2**attempt)
        raise AssertionError("retry loop exited")


def normalize_kalshi_market(raw: Any, observed: datetime) -> MarketSnapshot | None:
    if not isinstance(raw, dict) or not isinstance(raw.get("ticker"), str):
        logger.warning("Skipping malformed Kalshi market")
        return None
    identifiers = " ".join(str(raw.get(k, "")) for k in ("ticker", "event_ticker", "series_ticker"))
    if "NFL" not in identifiers.upper():
        return None
    title, subtitle = str(raw.get("title", "")).strip(), str(raw.get("subtitle", "")).strip()
    texts = {value for value in (title, subtitle, str(raw.get("yes_sub_title", "")).strip(),
             f"{title}: {subtitle}" if title and subtitle else "") if value}
    matches = [match for text in texts for pattern in PATTERNS if (match := pattern.fullmatch(text))]
    identities = {(m.group("player").casefold(), m.group("threshold"), m.group("stat").casefold()) for m in matches}
    if len(identities) != 1:
        logger.warning("Excluding ambiguous Kalshi market %s: title is not an exact supported prop", raw["ticker"])
        return None
    match = matches[0]
    player = " ".join(match.group("player").split())
    threshold = float(match.group("threshold"))
    # Kalshi's X+ YES contracts correspond to sportsbook Over X-0.5.
    line = threshold - 0.5 if "." not in match.group("threshold") else threshold
    yes_bid, yes_ask = _price(raw, "yes_bid"), _price(raw, "yes_ask")
    midpoint = (yes_bid + yes_ask) / 2 if yes_bid is not None and yes_ask is not None else None
    event = raw.get("event_ticker") or raw.get("series_ticker") or raw["ticker"]
    return MarketSnapshot(
        source="kalshi", source_market_id=raw["ticker"], game_id=str(event),
        event_name=raw.get("event_title"), player_name=player,
        market_type=STAT_MAP[match.group("stat").casefold()], line=line, side=Side.YES,
        contract_price=midpoint, yes_bid=yes_bid, yes_ask=yes_ask,
        no_bid=_price(raw, "no_bid"), no_ask=_price(raw, "no_ask"),
        last_traded_price=_price(raw, "last_price"), volume=_number(raw, "volume"),
        open_interest=_number(raw, "open_interest"), observed_at_utc=observed,
        source_updated_at_utc=_parse_datetime(raw.get("updated_time") or raw.get("last_updated_ts")),
        raw={"market": raw},
    )


def _price(raw: dict, field: str) -> float | None:
    dollars = raw.get(f"{field}_dollars")
    value = dollars if dollars is not None else raw.get(field)
    if value is None or isinstance(value, bool):
        return None
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    if dollars is None:
        number /= 100
    return number if 0 <= number <= 1 else None


def _number(raw: dict, field: str) -> float | None:
    value = raw.get(field)
    return float(value) if isinstance(value, (int, float)) and not isinstance(value, bool) and value >= 0 else None
