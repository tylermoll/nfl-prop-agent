"""Bounded, read-only Kalshi NFL player-prop market-data adapter."""

from __future__ import annotations

import asyncio
import logging
import re
import time
from collections.abc import Awaitable, Callable, Sequence
from dataclasses import asdict, dataclass, field
from datetime import datetime, timedelta, timezone
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
SERIES_TITLE_MAP = {
    "Pro Football Passing Yards": MarketType.PASS_YDS,
    "Pro Football Receiving Yards": MarketType.RECEPTION_YDS,
    "Pro Football Player Receptions": MarketType.RECEPTIONS,
}
PATTERNS = (
    re.compile(r"^(?P<player>[A-Za-z][A-Za-z .'-]+?)\s*[:|-]\s*(?P<threshold>\d+(?:\.\d+)?)\+?\s+(?P<stat>passing yards|receiving yards|receptions)$", re.I),
    re.compile(r"^Will (?P<player>[A-Za-z][A-Za-z .'-]+?) (?:have|record|get) (?P<threshold>\d+(?:\.\d+)?)\+? (?P<stat>passing yards|receiving yards|receptions)(?:\?)?$", re.I),
)


class KalshiApiError(RuntimeError):
    pass


@dataclass
class KalshiDiscoveryStats:
    series_list_requests: int = 0
    market_list_requests: int = 0
    pages_retrieved: int = 0
    markets_inspected: int = 0
    nfl_candidates: int = 0
    supported_contracts: int = 0
    discovered_series: dict[str, str] = field(default_factory=dict)
    order_book_requests: int = 0
    elapsed_discovery_seconds: float = 0.0
    errors: list[str] = field(default_factory=list)

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)


class KalshiProvider(PredictionMarketProvider):
    """Discover selected NFL series using only public GET routes.

    The public series catalog is filtered by Kalshi's Football tag and matched
    to exact supported series titles and an NFL settlement source. An optional
    ``series_tickers`` allowlist can further restrict the discovered result.
    """

    name = "kalshi"

    def __init__(
        self, *, client: httpx.AsyncClient | None = None, timeout: float = 20,
        max_retries: int = 3, sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
        series_tickers: Sequence[str] = (), lookahead_days: float = 4,
        max_pages: int = 10, max_requests: int = 25, fetch_order_books: bool = False,
        order_book_shortlist_limit: int = 20,
        now: Callable[[], datetime] = lambda: datetime.now(timezone.utc),
    ):
        if any(not ticker.strip() for ticker in series_tickers):
            raise ValueError("configured Kalshi NFL series tickers must be non-empty")
        if lookahead_days <= 0 or max_pages <= 0 or max_requests <= 0 or order_book_shortlist_limit < 0:
            raise ValueError("lookahead and safety limits must be positive")
        self._client, self.timeout, self.max_retries, self._sleep = client, timeout, max_retries, sleep
        self.series_tickers = tuple(dict.fromkeys(ticker.strip() for ticker in series_tickers))
        self.lookahead_days, self.max_pages, self.max_requests = lookahead_days, max_pages, max_requests
        self.fetch_order_books, self.order_book_shortlist_limit = fetch_order_books, order_book_shortlist_limit
        self._now = now
        self.discovery_stats = KalshiDiscoveryStats()
        self._requests_made = 0

    async def fetch_nfl_player_props(self) -> list[MarketSnapshot]:
        owns = self._client is None
        client = self._client or httpx.AsyncClient(timeout=self.timeout)
        self.discovery_stats, self._requests_made = KalshiDiscoveryStats(), 0
        started = time.monotonic()
        start = self._now().astimezone(timezone.utc)
        end = start + timedelta(days=self.lookahead_days)
        rows: list[MarketSnapshot] = []
        try:
            catalog = await self._get_json(
                client, "/series", {"category": "Sports", "tags": "Football"}, "series_list"
            )
            discovered = discover_nfl_player_prop_series(catalog)
            if self.series_tickers:
                allowed = set(self.series_tickers)
                discovered = {ticker: kind for ticker, kind in discovered.items() if ticker in allowed}
            self.discovery_stats.discovered_series = {
                ticker: market_type.value for ticker, market_type in discovered.items()
            }
            stop = False
            for series in discovered:
                cursor: str | None = None
                while not stop and self.discovery_stats.pages_retrieved < self.max_pages:
                    if self._requests_made >= self.max_requests:
                        stop = True
                        break
                    params = {
                        "status": "open", "limit": "1000", "series_ticker": series,
                        "min_close_ts": str(int(start.timestamp())),
                        "max_close_ts": str(int(end.timestamp())),
                    }
                    if cursor:
                        params["cursor"] = cursor
                    payload = await self._get_json(client, "/markets", params, "market_list")
                    markets = payload.get("markets") if isinstance(payload, dict) else None
                    if not isinstance(markets, list):
                        raise KalshiApiError("Kalshi markets response lacks a markets array")
                    self.discovery_stats.pages_retrieved += 1
                    observed = self._now().astimezone(timezone.utc)
                    for market in markets:
                        self.discovery_stats.markets_inspected += 1
                        if not _is_nfl_market(market):
                            continue
                        self.discovery_stats.nfl_candidates += 1
                        if not _within_close_window(market, start, end):
                            continue
                        parsed = normalize_kalshi_market(market, observed)
                        if parsed is not None:
                            rows.append(parsed)
                            self.discovery_stats.supported_contracts += 1
                    cursor = payload.get("cursor")
                    if not isinstance(cursor, str) or not cursor:
                        break
                if self.discovery_stats.pages_retrieved >= self.max_pages:
                    stop = True

            # List responses already contain top-of-book prices, last price,
            # volume and open interest. Depth is fetched only for the most
            # liquid fully validated contracts when explicitly requested.
            if self.fetch_order_books:
                shortlist = sorted(rows, key=_liquidity_rank, reverse=True)[:self.order_book_shortlist_limit]
                for parsed in shortlist:
                    if self._requests_made >= self.max_requests:
                        break
                    book = await self._get_json(
                        client, f"/markets/{parsed.source_market_id}/orderbook", {"depth": "100"}, "order_book"
                    )
                    parsed.order_book_depth = book.get("orderbook") if isinstance(book, dict) else None
                    parsed.raw["orderbook_response"] = book
            return rows
        except KalshiApiError as exc:
            self.discovery_stats.errors.append(str(exc))
            raise
        finally:
            self.discovery_stats.elapsed_discovery_seconds = time.monotonic() - started
            if owns:
                await client.aclose()

    async def _get_json(self, client: httpx.AsyncClient, path: str, params: dict[str, str], kind: str) -> Any:
        for attempt in range(self.max_retries + 1):
            if self._requests_made >= self.max_requests:
                raise KalshiApiError("Kalshi discovery request safety limit reached")
            self._requests_made += 1
            if kind == "series_list":
                self.discovery_stats.series_list_requests += 1
            elif kind == "market_list":
                self.discovery_stats.market_list_requests += 1
            else:
                self.discovery_stats.order_book_requests += 1
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


def discover_nfl_player_prop_series(payload: Any) -> dict[str, MarketType]:
    """Select only exact, documented NFL single-player stat series metadata."""
    series = payload.get("series") if isinstance(payload, dict) else None
    if not isinstance(series, list):
        raise KalshiApiError("Kalshi series response lacks a series array")
    discovered: dict[str, MarketType] = {}
    for item in series:
        if not isinstance(item, dict):
            continue
        ticker, title = item.get("ticker"), item.get("title")
        tags = item.get("tags")
        sources = item.get("settlement_sources")
        nfl_source = isinstance(sources, list) and any(
            isinstance(source, dict) and "nfl.com" in str(source.get("url", "")).casefold()
            for source in sources
        )
        if (
            isinstance(ticker, str)
            and title in SERIES_TITLE_MAP
            and item.get("category") == "Sports"
            and isinstance(tags, list)
            and "Football" in tags
            and nfl_source
        ):
            discovered[ticker] = SERIES_TITLE_MAP[title]
    return discovered


def _is_nfl_market(raw: Any) -> bool:
    if not isinstance(raw, dict):
        return False
    identifiers = " ".join(str(raw.get(k, "")) for k in ("ticker", "event_ticker", "series_ticker"))
    return "NFL" in identifiers.upper()


def _within_close_window(raw: Any, start: datetime, end: datetime) -> bool:
    if not isinstance(raw, dict):
        return False
    close = _parse_datetime(raw.get("close_time") or raw.get("close_ts"))
    # The server bounds are authoritative when old payloads omit close time.
    return close is None or start <= close <= end


def _liquidity_rank(row: MarketSnapshot) -> tuple[float, float]:
    return row.volume or 0.0, row.open_interest or 0.0


def normalize_kalshi_market(raw: Any, observed: datetime) -> MarketSnapshot | None:
    if not isinstance(raw, dict) or not isinstance(raw.get("ticker"), str):
        logger.warning("Skipping malformed Kalshi market")
        return None
    if not _is_nfl_market(raw):
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
    if len(player.split()) < 2 or {part.casefold() for part in player.split()} & {"team", "total"}:
        logger.warning("Excluding ambiguous Kalshi market %s: player identity is not explicit", raw["ticker"])
        return None
    threshold = float(match.group("threshold"))
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
