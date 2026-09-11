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
Failures are classified in the execution details as Odds API, missing Hard
Rock, insufficient reference coverage, Kalshi unavailable, model scoring,
persistence, or quota/defer conditions. Provider failure cannot mutate an old
observation. Errors are truncated and redact URLs, API keys, tokens, and
authorization values.

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

# Live collection requires an application-specific integration factory. It
# returns (shared_model_loader, observation_builder).
python -m scripts.run_snapshot_scheduler --pipeline myapp.snapshot:factory
```

The integration factory is explicit because current-game football features
must come from a deployment's versioned data pipeline; the scheduler does not
invent them. Configure it with environment-backed settings, store credentials
in a secret manager, and use PostgreSQL shared by all invocations.

Recommended automation is a five-minute cron or systemd timer on an always-on
worker. Alternatives are a hosted worker/platform cron or GitHub Actions
`schedule`; GitHub scheduling may be delayed, so use wider tolerances (for
example ±15 minutes), concurrency locking, and a durable external PostgreSQL
database. Database slot uniqueness provides the final idempotency guard when
workers overlap. Never enable Kalshi order-book depth for this workflow.
