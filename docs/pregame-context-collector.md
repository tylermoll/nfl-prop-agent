# Production pregame context collector

The collector appends context only for persisted observations whose kickoff is
still upcoming on the current `America/New_York` calendar date. It reads no
market provider, prediction market, model, or wagering service. Outdoor weather
comes from the NOAA/National Weather Service hourly forecast at the home
stadium. Indoor venues are recorded without weather values; retractable venues
remain `retractable` with an `unknown` roof state unless an official state is
available. Injury evidence comes from NFL.com and role evidence comes from each
club's official depth-chart page. Current-team, rookie, new-team, or transaction
fields remain `unknown` unless those exact official documents establish them;
the collector never infers a change from news text.
Missing or unparseable categories are stored as `unavailable`/`unknown` and do
not stop other categories.

Run on Railway:

```bash
python -m scripts.run_pregame_context_cycle
```

Required environment:

* `DATABASE_URL` — Railway PostgreSQL URL used by the existing application.
* `NWS_USER_AGENT` — recommended identifying user agent with an operator email,
  for example `nfl-prop-agent/1.0 (ops@example.com)`. The CLI has a non-secret
  fallback, but an operator contact is preferred by the NWS API.

No persistent volume is required; PostgreSQL stores every snapshot. If the
deployed database predates `pregame_context_snapshots`, run this exact one-time
schema bootstrap:

```bash
python -c 'from app.config import settings; from app.shadow_storage import ShadowStore; ShadowStore(settings.database_url)'
```

The CLI emits sanitized aggregate output and never emits database URLs, raw
official pages, or credentials. Example:

```json
{
  "started_at_utc": "2026-09-13T12:00:00+00:00",
  "scope": "upcoming games before midnight America/New_York",
  "observations_inspected": 42,
  "games_represented": 6,
  "snapshots_prepared": 42,
  "snapshots_appended": 42,
  "idempotent_duplicates": 0,
  "category_failures": [],
  "network_sources": ["api.weather.gov", "www.nfl.com"],
  "odds_api_calls": 0,
  "kalshi_calls": 0,
  "model_calls": 0,
  "wagering_calls": 0
}
```

Snapshot IDs are deterministic over observation ID, normalized context, source
timestamps, and provenance. An unchanged rerun is ignored, while changed
official data creates a new immutable snapshot. `as_of_utc` and
`collected_at_utc` preserve when the collector first observed that version.

It is safe to run immediately against today's production database after the
bootstrap has succeeded. The job performs only `SELECT` on observations and
append-only `INSERT` on context snapshots. Source coverage may legitimately be
unavailable, particularly before official injury reports are published.
