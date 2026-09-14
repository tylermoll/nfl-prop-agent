# Production shadow settlement

The settlement worker is isolated from snapshot collection and from every odds
or prediction-market adapter. It reads passed-kickoff, unsettled immutable
`shadow_observations`, resolves each game against nflverse schedules, loads one
weekly-statistics partition per represented final season, and writes the
existing `shadow_settlements` relation in one transaction. The existing
primary key on `observation_id` is the final duplicate/race safeguard.

## Identity and result rules

An observation resolves only through its exact nflverse `game_id`, through an
`nfl:<team>:<team>` identity, or through the immutable structured event identity
now persisted in its context, plus one unique exact canonical team pair and its
scheduled UTC kickoff.
The deployed matchup identity was historically sorted and is therefore not
trusted to prove home/away orientation by itself; nflverse supplies venue only
after the unique team-and-time match. Explicit aliases are those in
`app.identities.NFL_TEAMS`; no fuzzy aliases are used. Kickoffs may differ by at
most 59 seconds solely because provider timestamps can retain seconds while the
nflverse schedule is minute-granular. Team pairing is always required and time
proximity alone is never used. Zero, conflicting, or multiple candidates fail
closed. A schedule must contain explicit final/completed state or
the nflverse postgame `result`. A weekly row must then match season, week,
stable nflverse/GSIS `player_id`, and nflverse game ID when that field is
published (otherwise the exact player team within the already-resolved game).
The canonical market maps through the existing historical `MARKETS` mapping.
Missing/null statistics are unavailable—not zero—and remain retryable.

Settlement delegates Over/Under/push and American-price P&L calculation to the
existing shadow settlement helpers. It passes `SHADOW_NOMINAL_UNIT` (default
`10`) and preserves each observation's captured Hard Rock American price. It
never updates the observation and has no wagering or recommendation path.

PostgreSQL advisory locking prevents overlapping workers. The settlement
primary key still rejects duplicates if separate processes race. All rows in a
successful execution are inserted transactionally; an error rolls the batch
back. The `/research`, `/research/observations/{id}`, and `/research/summary`
queries already outer-join `shadow_settlements`, so they reflect commits
immediately without copying data.

## CLI and Railway

Run the identity audit before the write-capable worker:

```bash
python -m scripts.audit_settlement_identities
```

This command performs only database reads and an nflverse schedule fetch. It
groups observations by immutable event identity and kickoff, reports the
candidate nflverse ID, teams, kickoff, season/week, match method, verification
state, failure reason, associated observation count, capture slots, and safe
identity excerpts from scheduler executions and observation provenance. It
never emits raw provider payloads or invokes the settlement writer.

The 32-character production observation IDs originate unchanged from The Odds
API event-list `id`: the scheduler uses that value for `DueCapture.event_id`,
the production builder copies it to `shadow_observations.game_id`, and Hard Rock
source-market IDs repeat it as their prefix. Capture-slot rows retain the same
ID and timing but no teams. Successful scheduler execution capture entries also
retain only ID/timing. Historical recovery is therefore possible only for an
event that has other already-persisted scheduler execution evidence—such as a
failed capture or rejected-prop diagnostic—binding that same provider event ID
to a canonical team pair and the same kickoff. The audit reports this chain;
kickoff, slot metadata, source-market prefixes, or player identity alone never
verify a game. Events without persisted team evidence are unrecoverable without
rewriting history and remain failed closed.

Future observations persist a structured `context.event_identity` containing
the provider event ID, canonical event, explicit away/home teams, and kickoff.
It is written with the observation and cross-validated internally before use;
it does not modify legacy observations.

Exact Start Command:

```bash
python -m scripts.run_settlement_cycle
```

Deploy it as a separate Railway cron service. Recommended cron expression:

```cron
0 * * * *
```

Hourly is frequent enough during and after game windows while allowing for
nflverse publication lag; `*/30 * * * *` is also safe if a 30-minute retry is
operationally preferred. A five-minute cadence adds unnecessary release checks.

Required Railway variable:

* `DATABASE_URL` — the same PostgreSQL database used by capture and research.

Optional variables (defaults shown):

* `SHADOW_NOMINAL_UNIT=10`
* `SETTLEMENT_CACHE_DIR=data/historical/raw`
* `SETTLEMENT_NFLVERSE_REFRESH=true`

No Odds API or Kalshi credentials are required by this service. Model artifact
variables and the current-feature cache are also not required. A persistent
volume is **not required**: PostgreSQL owns durable observations/settlements and
the local nflverse cache can be rebuilt. Mounting one at the configured cache
directory is recommended only to retain parquet/manifest audit artifacts
between deployments.

Each run makes HTTPS GETs only to maintained nflverse GitHub release assets:
one schedules release and at most one weekly-statistics release per necessary
season. It makes no request per observation and no Odds API, Hard Rock, Kalshi,
model-training, or wagering request. Local storage contains cached nflverse
parquet files and provenance sidecars; database storage adds only settlement
rows.
