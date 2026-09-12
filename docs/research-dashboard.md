# Production research observation dashboard

The `/research` surface is a read-only, descriptive audit view over shadow
observations, settlements, capture slots, and scheduler executions already in
Postgres. It never calls an odds provider, Kalshi, a model, or a wagering API.
These fields are research context, **not betting recommendations** or promises
of performance. ROI/model-performance metrics are intentionally absent.

## Endpoints

* `GET /research` — dependency-free HTML overview and ten latest captures.
* `GET /research/executions?limit=50&offset=0` — newest-first scheduler history.
* `GET /research/observations?limit=50&offset=0` — newest-first observations.
* `GET /research/observations/{observation_id}` — one complete sanitized record,
  including safe source IDs and provenance.
* `GET /research/summary` — observation, settlement, capture-health, model,
  freshness, execution, and quota aggregates.

`limit` is 1–200 and `offset` is zero-based. Observation filters are exact (not
fuzzy): `market`, `player`, `event` (game ID, team, or opponent), `side`,
`capture_window`, `edge_tier`, and `settled=true|false`. `start` and `end` are
inclusive ISO-8601 capture timestamps. UTC offsets are preserved/normalized.

Example list response (values abbreviated):

```json
{"items":[{"observation_id":"...","captured_at_utc":"2026-09-12T12:00:00Z","capture_window":"90m","canonical_market":"player_pass_yds","side":"over","hard_rock_line":249.5,"hard_rock_american_price":-110,"model_probability":0.56,"probability_edge_pp":3.62,"edge_tier":"edge_2_to_5pp","settlement_status":"unsettled","settlement":null}],"limit":50,"offset":0}
```

Example execution response:

```json
{"items":[{"execution_id":"...","dry_run":false,"due_count":3,"selected_count":2,"completed_count":2,"deferred_count":1,"failed_count":0,"estimated_credits":6,"actual_credits_consumed":6,"quota_before":100,"quota_after":94,"http_request_count":3,"errors":[]}],"limit":50,"offset":0}
```

Credential-like keys, URLs, and credential-shaped text in legacy JSON/error
fields are redacted. Raw payloads and database/private configuration are never
returned. All routes are GET-only; there is no mutation or action route.
