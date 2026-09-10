# Codex Build Specification

You are implementing an NFL pre-game player-prop market intelligence system.

## Product objective
Detect when prices available to a Florida user on Hard Rock Bet or Kalshi disagree materially with:
1. broader sportsbook consensus,
2. a later statistical player-prop model,
3. prediction-market microstructure.

This repository is V1: the market-data validation layer.

## Safety / execution constraint
Recommendation-only. Do not implement automated wager placement.
Do not scrape Hard Rock. Obtain Hard Rock prices only through licensed/authorized data APIs.

## V1 deliverables
Complete these in order.

### 1. Provider adapters
Implement adapters behind `OddsProvider` / `PredictionMarketProvider` interfaces.

Required:
- The Odds API adapter
- OddsPapi adapter
- Kalshi adapter

For each adapter:
- preserve raw response
- attach `observed_at_utc`
- attach provider-native update timestamp when available
- return normalized `MarketSnapshot` rows
- handle rate limiting, retries, timeouts and malformed data
- never silently coerce unknown player names or market types

### 2. Canonical markets
Start with:
- player_pass_yds
- player_reception_yds
- player_receptions

Canonical sides: over, under, yes, no.

Canonical player identity: provider player name, normalized display name, nullable stable external IDs, team, game ID.

Do not fuzzy-match players without logging the resolution.

### 3. Storage
Use PostgreSQL. Every market observation is immutable.

Store: source, source market ID, game ID, player ID/name, market type, line, side, American odds or contract price, implied probability, observed time, source update time, raw JSON.

Never overwrite a prior quote.

### 4. Market-quality metrics
Implement source coverage %, Hard Rock coverage %, props/player, stale quote %, median source lag when timestamps support it, exact-line disagreement, consensus median line, consensus no-vig probability when both sides exist, provider discrepancy alerts, and API usage accounting hooks.

### 5. Dashboard/API
Expose:
- GET /health
- GET /source-quality
- GET /markets/latest
- GET /markets/{player}
- GET /disagreements

Streamlit can be added later; FastAPI JSON is sufficient for V1.

### 6. Tests
Tests must cover American odds conversion, no-vig normalization, deduplication, canonical market mapping, missing/invalid API fields, stale quote detection, and provider disagreement.

### 7. Demo mode
Keep deterministic fixture data so the repository runs without credentials.

## Definition of done
V1 is done when a user can run one command during an NFL slate and receive a report that answers:
- Is Hard Rock present?
- Are the desired player props present?
- Which provider is freshest?
- How much data is missing?
- Where do providers disagree?
- Approximately how much API quota was consumed?

Do not build the projection model until this is reliable.
