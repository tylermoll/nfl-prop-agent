# Prospective snapshot scheduler

This is a **read-only research workflow**. It calls only provider market-data
GET endpoints and the existing football scorer; it has no wager, account,
portfolio, stake-sizing, or order-placement behavior. The research constants
remain a $100 starting paper bankroll and a nominal $10 paper unit, and neither
is an input to capture selection.

## Architecture and due-slot algorithm

`SnapshotScheduler` performs one inexpensive The Odds API NFL event discovery,
parses kickoff times, excludes started games, and computes a stable identity
`(event_id, slot_name, kickoff - lead_time)`. The defaults are 24 hours, 6
hours, 90 minutes, and 15 minutes before kickoff. Each has a ±10 minute window;
all leads and tolerances are configurable. An optional `final` window is off by
default. Run the scheduler every five minutes so every default window normally
has multiple opportunities to succeed.

Completed identities and failures whose bounded retry budget is exhausted are
removed *before* event-level requests. Remaining captures sort by 15m, 90m,
6h, 24h, then kickoff and event ID. Limits are applied deterministically;
unselected captures are logged as `maximum_games_budget`,
`per_run_credit_budget`, or `quota_reserve`, rather than disappearing.

## Quota algorithm and provider requests

Discovery uses `GET /v4/sports/americanfootball_nfl/events`. The Odds API
documents that event listing as zero credits (it is still one HTTP request).
For every distinct selected event, the adapter sends exactly one targeted
`GET /events/{event_id}/odds`, combining Hard Rock and configured reference
books with only `player_pass_yds`, `player_reception_yds`, and
`player_receptions`. With fewer than ten selected books, the estimate is three
credits per game. Before selection, the scheduler checks both:

* `estimated run credits + 3 <= maximum credits per execution`; and
* `last known remaining - estimated run credits - 3 >= minimum reserve`.

Quota headers and HTTP counts are retained by the provider and copied into the
execution log. Typical costs are: one isolated due game = 3 credits; a Sunday
slate with three simultaneously due games = 9; four games = 12; and no due
game = zero credits (one free discovery request). Limits may deliberately defer
the rest. The estimate assumes the configured books remain below the
provider's ten-book pricing block and should be updated if that configuration
changes.

There is no scheduler backoff or cooldown. On each five-minute invocation, a
failed slot is eligible again while it is inside its tolerance window, before
kickoff, and below `SCHEDULER_RETRY_BUDGET` (default: two total attempts). Each
selected retry makes a fresh paid event-level Odds API request. Retry stops at
the first successful completion, when the attempt budget is exhausted, when
the tolerance window/kickoff passes, or when quota/game selection defers it.
Provider HTTP retries are separate: the adapter retries 429, 5xx, timeout, and
network failures with bounded exponential delays inside that same invocation.

Kalshi discovery is invoked once per non-empty execution and shared by every
selected capture. Its bounded existing adapter is used with order-book depth
disabled. Likewise, the injected model/artifact loader is called once and the
loaded object is shared; an observation builder may score multiple players
without reloading it.

## Freshness and partial failure policy

Every observation builder receives scheduler time and capture identity, and
should put execution ID, slot, target time, actual capture time, seconds to
kickoff, and `freshness_metadata()` into the immutable observation context.
That helper records provider observation/update timestamps and ages plus model
feature timestamp/age; missing timestamps and configurable age-threshold
breaches are explicitly stale.

Hard Rock is the minimum viable observation. A valid Hard Rock row may be
persisted when Kalshi is unavailable or reference coverage is below the
configured minimum, provided the builder records empty context and explicit
`kalshi_unavailable` / `insufficient_reference_coverage` flags. Values must
never be imputed. No capture is completed without Hard Rock or a model score.
Each failed-slot execution detail carries event ID, matchup (when discovery
provided it), window, kickoff, a stable failure category, sanitized reason,
whether a provider request occurred, estimated/actual attributable credits,
and whether another attempt is eligible. Provider failure cannot mutate an old
observation. Errors are truncated and redact URLs, API keys, tokens, and
authorization values. `/research/executions` returns these as `failed_slots`,
and the dashboard reveals them beneath each failed execution.

## Persistence schema

`shadow_observations` remains append-only. `shadow_capture_slots` has a unique
constraint on event ID, slot, and target timestamp and stores status, bounded
attempt count, sanitized reason, and update time. Only successful persistence
marks a slot completed. Failed attempts can retry up to the configured budget.
`shadow_scheduler_executions` stores execution ID, start/end, due/selected/
completed/deferred/failed lists, estimates and actual credits, quota
before/after, HTTP count, and sanitized errors as lightweight JSON. Credential
URLs and raw secrets are not stored.

## CLI and deployment

```bash
# Planning only: event discovery may make one zero-credit read-only HTTP GET.
# No event props, Kalshi calls, observations, slot states, or execution row.
python -m scripts.run_snapshot_scheduler --dry-run

# Production collection uses the repository's local-artifact integration.
python -m scripts.run_snapshot_scheduler --pipeline app.production_pipeline:create_scheduler_pipeline
```

The production factory loads all three artifacts exactly once and never
downloads or trains during an execution. Configure
`FOOTBALL_ARTIFACT_PLAYER_PASS_YDS`,
`FOOTBALL_ARTIFACT_PLAYER_RECEPTION_YDS`, and
`FOOTBALL_ARTIFACT_PLAYER_RECEPTIONS` as absolute paths. Configure
`FOOTBALL_CURRENT_FEATURE_PATH` as a Parquet (preferred), JSON, or CSV table.
That table is an explicit deployment input, refreshed before scheduler runs
from nflverse/current sources by a separate job. It contains one row per
current player/game/market, stable `player_id`, `player_name`, `home_team`,
`away_team`, `kickoff`, `canonical_market`, `feature_built_at_utc`, and
`feature_data_as_of_utc`, plus the artifact-required columns produced with the
definitions in `app.historical.features`. Current-game result/usage columns
must not be present. The adapter verifies exact artifact schema, exact player
and matchup identity, and that both timestamps precede kickoff and capture.

For Railway, the simplest reliable current deployment is one persistent
volume mounted read-only by the scheduler (for example at `/models`) containing
the three immutable artifacts and an atomically replaced feature-cache file.
A separate explicit data/model release job uploads versioned files to that
volume; the scheduler must never generate them. Baking models into the image is
also reproducible but makes feature refreshes require an image deployment;
ephemeral runtime downloads are not recommended. Do not commit artifacts,
bulk data, API keys, or private keys. Store provider/database credentials as
Railway secrets and the four local paths as Railway variables.

Recommended automation is a five-minute cron or systemd timer on an always-on
worker. Alternatives are a hosted worker/platform cron or GitHub Actions
`schedule`; GitHub scheduling may be delayed, so use wider tolerances (for
example ±15 minutes), concurrency locking, and a durable external PostgreSQL
database. Database slot uniqueness provides the final idempotency guard when
workers overlap. Never enable Kalshi order-book depth for this workflow.
# Production cycle

Railway should run the scheduler service every five minutes with:

```bash
python -m scripts.run_production_cycle
```

Before enabling or debugging that cycle, run the production preflight. It is
strictly read-only: the default command loads the three artifacts and current
feature cache, scores representative upcoming rows, and queries database
connectivity/history without creating tables, observations, settlements,
capture slots, or scheduler executions. It makes no Odds API or Kalshi calls.

```bash
# Railway command: local inputs and database only (no provider requests)
python -m scripts.run_production_preflight

# Railway command: additionally opt in to minimal upcoming-event/Hard Rock checks
python -m scripts.run_production_preflight --check-live-props
```

The optional live check requests the NFL event list and then checks upcoming
events only until all three canonical Hard Rock prop markets have been found.
It reports event, book, market, HTTP-request, and quota counts. It does not run
capture scheduling and never contacts Kalshi.

Set `FOOTBALL_CURRENT_FEATURE_PATH=/models/current_features.parquet` and optionally
override `FOOTBALL_CURRENT_FEATURE_MAX_AGE_SECONDS=21600` (six hours). The cycle
holds a nonblocking advisory `flock` on
`/models/.current_features.parquet.production-cycle.lock` from the freshness check
through scheduler completion. An overlapping invocation exits nonzero. Only a
missing or stale cache invokes the existing nflverse current-feature materializer;
its atomic replacement remains responsible for preserving a prior valid cache.
