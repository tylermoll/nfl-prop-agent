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

Run the live, read-only smoke report after injecting the key through your
environment's secret manager (never commit it to `.env`):

```bash
DEMO_MODE=false python -m scripts.smoke_the_odds_api
```

The JSON report contains event and Hard Rock row counts, per-market coverage,
provider freshness, quota headers, missing markets, and sanitized API errors.

## Codex
Open this repository in Codex and tell it to read `CODEX.md` first.
