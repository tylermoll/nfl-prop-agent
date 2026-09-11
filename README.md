# NFL Prop Agent — V1 Market Data Validator

A recommendation-only NFL player-prop research system focused on:
- Hard Rock Bet Florida as an executable sportsbook
- Kalshi as an executable prediction-market venue
- Major sportsbooks as reference/price-discovery markets
- Pre-game player props only

V1 goal: validate data quality before building a betting model.

## What V1 does
1. Pulls NFL event/prop data from odds providers.
2. Pulls Kalshi market/trade/order-book data through an adapter.
3. Normalizes observations into a common schema.
4. Stores immutable timestamped market snapshots.
5. Produces a source-quality report: Hard Rock coverage, player-prop coverage, stale/missing observations, price disagreements, and request/credit accounting hooks.
6. Runs in DEMO_MODE without API keys.

## What V1 deliberately does NOT do
- Auto-place bets
- Scrape Hard Rock
- Promise profitable picks
- Use an LLM to invent probabilities
- Risk real money automatically

## Quick start

```bash
cp .env.example .env
docker compose up -d db
python -m venv .venv
source .venv/bin/activate   # Windows: .venv\Scripts\activate
pip install -r requirements.txt
python -m app.main
```

Then open `http://localhost:8000/health` and `http://localhost:8000/source-quality`.

By default `DEMO_MODE=true`, so it runs with deterministic mock data.

## Before live data
Put keys in `.env` locally. Never commit `.env`.

```env
THE_ODDS_API_KEY=
THE_ODDS_API_LOOKAHEAD_DAYS=4
THE_ODDS_API_REFERENCE_BOOKMAKERS='["draftkings","fanduel","betmgm","williamhill_us"]'
THE_ODDS_API_TARGET_BOOKMAKER=hardrockbet
CONSENSUS_MIN_REFERENCE_BOOKS=2
ODDSPAPI_API_KEY=
KALSHI_API_KEY_ID=
KALSHI_PRIVATE_KEY_PATH=
```

Exact provider endpoint details should be confirmed against each provider's current documentation before enabling production polling.

### The Odds API (Milestone 1)

The read-only v4 adapter discovers current `americanfootball_nfl` events, then
requests the event-level `player_pass_yds`, `player_reception_yds`, and
`player_receptions` markets. It requests the official `hardrockbet` bookmaker
key by default so Florida availability is measured rather than inferred. The
adapter preserves each raw outcome, provider update time, observation time, and
quota headers; it retries rate limits and transient provider failures. See the
[official v4 API guide](https://the-odds-api.com/liveapi/guides/v4/) and
[official market list](https://the-odds-api.com/sports-odds-data/betting-markets.html).

This integration only reads market data. It does not submit wagers.

## Price-aware consensus and Kalshi

`GET /divergences` keeps prices, vig-inclusive and no-vig probabilities,
freshness, named matchups, reference min/max/median/range, and distinct
same-line `better_price` and `better_line` fields. It makes no EV claim.

The Odds API's official bookmaker table identifies Caesars with provider key
`williamhill_us`; the key is not silently replaced. An explicitly requested
book can be absent when it offers none of the requested event-level props. The
prior report's zero valid Caesars rows means its responses contained no usable
`williamhill_us` outcomes, not that the normalizer aliased it. Raw responses let
future runs distinguish book absent, market absent, and malformed outcomes. See
the [official bookmaker list](https://the-odds-api.com/sports-odds-data/bookmaker-apis.html).

The Kalshi adapter uses Trade API v2 `GET /markets` and
`GET /markets/{ticker}/orderbook`. These market-data routes are publicly
readable and need no credential. A Kalshi API key ID and RSA private key are
needed for authenticated account/trading routes, but are neither needed nor
used here. Exact supported titles are normalized; ambiguous ones are logged and
excluded. Raw market and order-book responses are retained.
`GET /cross-market-comparisons` exposes matched price/liquidity context without
EV, staking, or action labels. See Kalshi's [official market-data quick
start](https://docs.kalshi.com/getting_started/quick_start_market_data) and
[market API reference](https://docs.kalshi.com/api-reference/market/get-markets).

A future live validation has no documented Kalshi per-request dollar/API-credit
fee: expect one market-list request per page and one order-book request per exact
supported contract, subject to public rate limits. The Odds API portion remains
`3 × E` credits and `1 + E` requests for `E` eligible games.

Run the live, read-only smoke report after injecting the key through your environment's secret manager (never commit it to `.env`):

```bash
DEMO_MODE=false python -m scripts.smoke_the_odds_api
```

By default, only events commencing in the next four days are queried for props.
Set `THE_ODDS_API_LOOKAHEAD_DAYS` to a positive number to adjust that window.
The JSON report separates all discovered events from eligible events queried and
contains Hard Rock row count, per-market coverage, provider freshness, HTTP
request and quota-consumption accounting, missing markets, and sanitized API errors.

### Reference consensus

`GET /divergences` matches event, normalized player name, and canonical market
across Hard Rock and the configured reference books. It reports raw Over/Under
American prices alongside vig-inclusive implied probabilities, the median
reference line, coverage, and whether Hard Rock is higher, lower, or the same.
It identifies market divergence; it is not a betting recommendation.

Hard Rock, DraftKings, FanDuel, BetMGM, and Caesars (`williamhill_us`) are sent
together in one event-level request, and that response is reused for all three
markets. Under the documented v4 formula, five explicitly selected bookmakers
(fewer than ten) and three markets cost **3 credits per eligible event**; event
discovery is free. One comparison run therefore costs `3 × E` credits and makes
`1 + E` HTTP requests, where `E` is the number of unstarted NFL events in the
four-day window. Retries can add HTTP requests; response quota headers are the
source of truth for actual cost.

## Codex
Open this repository in Codex and tell it to read `CODEX.md` first.
