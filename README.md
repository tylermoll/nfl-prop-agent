# NFL Prop Agent — V1 Market Data Validator

A recommendation-only NFL player-prop research system focused on:
- Hard Rock Bet Florida as an executable sportsbook
- Kalshi as an executable prediction-market venue
- Major sportsbooks as reference/price-discovery markets
- Pre-game player props only

V1 goal: validate data quality before building a betting model.

The separate, leakage-safe historical football feature foundation is
documented in [`docs/historical-data.md`](docs/historical-data.md). It uses
cached nflverse releases and contains no odds, recommendations, or wagering.
The first football-only point-prediction and uncertainty benchmark is described
in [`docs/football-benchmark.md`](docs/football-benchmark.md); its generated
models and evaluation tables are written to ignored artifact directories.
Prospective, immutable paper observations, exact-threshold football scoring,
settlement, reporting, and explicit market-history limitations are documented
in [`docs/shadow-evaluation.md`](docs/shadow-evaluation.md). This framework is
research-only and cannot place a wager or order.

The quota-aware prospective capture scheduler, targeted event requests,
idempotent slot ledger, dry-run CLI, cost model, and deployment guidance are
documented in [`docs/snapshot-scheduler.md`](docs/snapshot-scheduler.md).
The atomic nflverse-only production current-feature refresh job is documented
in [`docs/current-features.md`](docs/current-features.md).
The isolated nflverse-only automated settlement CLI and Railway cron deployment
are documented in [`docs/settlement-job.md`](docs/settlement-job.md).

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

Persisted production shadow activity can be audited at the read-only HTML
dashboard `http://localhost:8000/research` and its JSON endpoints. See
[`docs/research-dashboard.md`](docs/research-dashboard.md) for filters and
response shapes. These descriptive views are not betting recommendations.

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

### Unified live opportunity scanner

`GET /unified-opportunities` performs one read-only load of the existing Hard
Rock/reference sportsbook and Kalshi adapters, then builds a deterministic
report. The Odds API supplies separate full `away_team` and `home_team` values
and the adapter renders `away at home`. Kalshi market payloads instead identify
the game through `event_ticker` (for example, a suffix such as `LARSF`), while
their title/subtitle describe the player threshold and `event_title` may be
absent. The former matcher preferred that absent Kalshi event title and never
decoded the ticker, so its Kalshi event key was null and dense, otherwise
compatible curves could not join sportsbook rows.

The scanner now resolves exact full NFL team names and an allowlist of
unambiguous abbreviations to franchise IDs, then sorts the two IDs into an
orientation-independent identity such as `nfl:lar:sf`. Provider-native game IDs
are deliberately not compared, ambiguous abbreviations are rejected, and no
fuzzy team or player matching is performed. Player normalization is limited to
case, whitespace, Unicode punctuation, and periods. An integer Kalshi `N+`
title is normalized to the equivalent sportsbook Over line `N - 0.5`.

Each exact match retains Hard Rock's raw American prices, implied and paired
no-vig probabilities, the reference median line, same-threshold reference
no-vig consensus, contributing books, and Kalshi bid, ask, descriptive
midpoint, last trade, spread, timestamp, volume, and open interest. Kalshi
contracts are also exposed as a non-interpolated threshold curve with
monotonicity and nearest-below/exact/above points. The response has separate
unmatched Hard Rock and Kalshi lists for coverage diagnosis. Unmatched reasons
now distinguish market, player, event, and exact-threshold failures on both
sides. `diagnostic_funnel` reports the Cartesian candidate count followed by
event, player, market, and threshold survivors, plus the requested independent
market → player → event → threshold dimension audit.

Signals are descriptive booleans: `line_divergence` uses the absolute Hard
Rock/reference-median line difference; `price_divergence` compares Hard Rock's
Over no-vig probability with the same-threshold Kalshi midpoint and executable
bid/ask range; `cross_market_confirmation` means the reference line and Kalshi
price point in the same Over/Under direction relative to Hard Rock; and
`cross_market_conflict` means those directions differ. The midpoint is marked
non-executable, no missing threshold is interpolated, and no action, expected
value, or profitability label is produced.

### Kalshi liquidity schema audit

Kalshi's current market schema includes fixed-point `volume_fp` and
`open_interest_fp` values represented as decimal strings, alongside deprecated
legacy integer `volume` and `open_interest` fields. Normalization now prefers
the fixed-point string fields and falls back to non-negative legacy numeric
values. Missing, negative, or malformed values remain `null`; the scanner does
not fabricate liquidity. Thus an older run that only accepted JSON numbers
could report all liquidity unavailable even while quotes normalized correctly.
See Kalshi's [official market API reference](https://docs.kalshi.com/api-reference/market/get-markets).

The scanner preserves the four-day Odds API window and bounded,
metadata-driven Kalshi discovery. It makes no order-book call by default and
does not make any provider call beyond those already described below.

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

The Kalshi adapter first uses the public Trade API v2 `GET /series` catalog with
the documented `category=Sports&tags=Football` metadata filters. It accepts only
an exact supported series title whose metadata is tagged Football and names
`nfl.com` as a settlement source. This currently discovers these player-game
series (rather than the generic game-winner series `KXNFL`):

| Kalshi series | Canonical market |
| --- | --- |
| `KXNFLPASSYDS` | `player_pass_yds` |
| `KXNFLRECYDS` | `player_reception_yds` |
| `KXNFLREC` | `player_receptions` |

The empty default `KALSHI_NFL_SERIES_TICKERS=[]` enables catalog discovery; a
configured JSON list acts as an allowlist of those validated catalog results,
not as trusted or guessed identifiers. The adapter then uses `GET /markets`
with the documented `status`, `series_ticker`, `min_close_ts`, and
`max_close_ts` filters once per discovered series. Discovery has hard page and
total-request limits and does not crawl unrelated markets. Kalshi also exposes
`GET /search/filters_by_sport` (whose Football / Pro Football scopes include
Passing Yards, Receiving Yards, and Receptions) and `GET /events` with
`series_ticker` and `with_nested_markets`; these are useful for inspecting the
sport/event hierarchy, while the series catalog plus tightly filtered markets
route is smaller for routine normalization. These market-data routes are
publicly readable and need no credential. A Kalshi API key ID and RSA private key are
needed for authenticated account/trading routes, but are neither needed nor
used here. Exact supported titles are normalized; ambiguous ones are logged and
excluded. Raw market responses are retained. Because each market response
already contains YES/NO bid and ask, last price, volume, and open interest,
order-book depth is disabled by default; when enabled it is requested only for
the most liquid fully normalized shortlist. Raw order books are then retained.
`GET /cross-market-comparisons` exposes matched price/liquidity context without
EV, staking, or action labels. See Kalshi's [official market-data quick
start](https://docs.kalshi.com/getting_started/quick_start_market_data) and
[market API reference](https://docs.kalshi.com/api-reference/market/get-markets).

A future live validation has no documented Kalshi per-request dollar/API-credit
fee: normally expect one market-list request per configured series (plus any
cursor pages), and zero order-book requests. If depth is enabled, add at most
`KALSHI_ORDER_BOOK_SHORTLIST_LIMIT` GET requests, while the global request cap
still applies. A normal Kalshi run therefore starts with one series-list request
and then makes one market-list request per discovered series (plus bounded
cursor pages). The Odds API portion remains
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
