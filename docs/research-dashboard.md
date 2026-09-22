# Production research observation dashboard

The `/research` surface is a read-only, descriptive audit view over shadow
observations, settlements, capture slots, and scheduler executions already in
Postgres. It never calls an odds provider, Kalshi, a model, or a wagering API.
These fields are research context, **not betting recommendations** or promises
of performance. Raw settlements are model-evaluation data. Strategy performance
remains unavailable until a prospectively selected journal row has settled.

## Endpoints

* `GET /research` — dependency-free, responsive sportsbook-style dashboard with
  summary KPIs, sortable/filterable observations, settled research, and
  scheduler diagnostics. Browser requests remain limited to these read-only
  research endpoints.
* `GET /research/executions?limit=50&offset=0` — newest-first scheduler history.
* `GET /research/observations?limit=50&offset=0` — newest-first observations.
* `GET /research/observations/{observation_id}` — one complete sanitized record,
  including safe source IDs and provenance.
* `GET /research/observations/{observation_id}/timeline` — chronological pregame
  captures for the same game/player/market/side identity. This single grouped
  query powers the on-demand detail timeline and avoids per-row requests.
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

The dashboard opens observation provenance and settlement details in an
on-demand drawer. Its filters are client-side exact-value filters, with simple
case-insensitive text matching for player and team. Market names, signed
American odds, percentages, percentage-point edges, and UTC detail timestamps
are formatted in the browser without modifying the API values.

Settled/unsettled counts describe immutable observations. The Brier score is a
model-calibration metric computed over settled raw side observations with a
binary win/loss outcome; pushes are excluded. Because OVER and UNDER terms can
be paired and correlated, the dashboard explicitly says these observations are
not a betting record.

Strategy W/L, paper P&L, realized ROI, and grouped results are computed only by
joining `research_selection_journal` to `shadow_observations` and
`shadow_settlements` on `observation_id`, filtered to `selected = true`. A
`selected = false` row never contributes. With no selected rows, the API emits
`status: awaiting_selections`, `available: false`, and `realized_roi: null`;
the dashboard states that no prospective selections have been recorded rather
than displaying raw settlement results or a zero ROI. This is read-only and no
historical selection is inferred or backfilled.

Credential-like keys, URLs, and credential-shaped text in legacy JSON/error
fields are redacted. Raw payloads and database/private configuration are never
returned. All routes are GET-only; there is no mutation or action route.

## Independent pregame context overlay

`GET /research/observations/{observation_id}/context?as_of=<ISO-8601>` returns the
latest append-only context known at that time (now by default). List and detail
responses also expose `context_overlay`: timestamped weather, injuries, role,
sources, descriptive flags, six evidence dimensions, and their plain-language
reasons. Dashboard filters cover evidence quality, weather/injury concern, and
role uncertainty.

`pregame_context_snapshots` is separate from immutable `shadow_observations`.
A separate collector appends rows; the research service never refreshes a
provider itself. Selected public sources are NOAA/National Weather Service
forecasts, official NFL/team game-status and inactive reports, and official team
depth charts/transactions. Keep provider update times and document IDs in
`sources`. Reporting must use `status_confidence=reported` or
`change_status=reported`; only explicit structured evidence may be confirmed.
No paid Odds API request or new Kalshi request is made.

Weather is fresh for 3 hours, injury data for 12 hours, and role data for 7 days;
the combined assessment becomes stale when any present category exceeds its limit. Refresh at 24h, 6h, 90m,
and after official inactive lists. Historical reconstruction selects the newest
row with `as_of_utc` no later than the requested time.

Evidence dimensions are `model_signal`, `market_confirmation`,
`weather_context`, `injury_context`, `role_context`, and `data_freshness`.
Deterministic overall rules are: two missing categories is
`insufficient_context`; stale data or four concerns is `weak`; two concerns is
`mixed`; an edge of at least five points confirmed within five points by a
same-threshold consensus is `strong`; otherwise `moderate`. These are evidence
labels, not probabilities. Week 1 sets `historical_role_may_be_stale` only with
explicit prior-usage dependency and a confirmed role/team/injury mismatch.

### Deployment

Run the existing schema bootstrap (`ShadowStore(DATABASE_URL)`) once to create
the new table, deploy the API normally, and configure an independent collector
to append the documented snapshots. No model retraining or scheduler change is
needed. Rollback can leave the unused table in place; model observations remain
unchanged.
