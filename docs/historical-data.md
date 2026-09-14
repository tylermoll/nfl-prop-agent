# Historical NFL modeling-data foundation

This layer produces football outcomes and pregame covariates only. It neither
contacts a sportsbook/prediction market nor trains a model or recommends a bet.

## Sources and coverage

All raw inputs are maintained, programmatically accessible Parquet releases
from `nflverse/nflverse-data`. `catalog.py` is the machine-readable inventory.
The cache manifest records source URL, retrieval method/time, HTTP ETag and
Last-Modified when present, SHA-256, byte/row counts, observed Arrow schema,
cataloged coverage, and limitations.

| dataset | cataloged seasons | use / limitation |
|---|---:|---|
| weekly player stats | 1999–current release | outcomes and basic volume; fields vary by era |
| play-by-play | 1999–current release | dropbacks and air-yard derivations; IDs/air yards can be missing |
| schedules | 1999–current release | game, kickoff, opponent, venue/rest identities; future games can change |
| players / rosters | players: historical master; rosters: 1920–current | GSIS IDs and display/team crosswalks; old IDs/timing are incomplete |
| injuries | 2009–current release | reports are incomplete and do not by themselves prove publication time |
| depth charts | 2002–current release | publication timing and weekly coverage vary |
| snap counts | 2012–current release | offense share when present; denominators/availability vary |

The default build downloads weekly stats, schedules, snap counts, injuries and
depth charts for availability, but deliberately joins only snaps. Injury/depth
rows are accepted by the feature function only with an explicit `observed_at`;
this avoids pretending a season file's eventual state was known pregame.
Play-by-play, players and rosters use the same cached client and are available
for audits or explicit derivations; weekly stats remain the target authority.

## Layers and identities

1. `catalog.py` and `ingestion.py`: raw releases plus immutable sidecars.
2. `normalize.py`: exact team aliases, stable nflverse/GSIS player IDs, names,
   schedule game IDs and `nfl:{season}:{week}:{away}:{home}` event IDs.
3. `features.py`: football targets and pregame features.
4. `AdvancedUsageProvider`: optional licensed player/game observations.
5. `WeatherProvider`: optional game forecasts.
6. A later, intentionally absent, market-feature join.

Names are display aids, never fuzzy identity keys. Unknown teams and name-only
players fail closed. Canonical markets are `player_pass_yds`,
`player_reception_yds`, and `player_receptions`.

## Output and feature definitions

Each applicable player/game/market row contains season, week, nflverse game ID,
canonical event ID, stable player ID/name, canonical team/opponent, home/away,
canonical market, realized `actual_value`, kickoff and pregame features. A pass
row requires an attempt; receiving rows require a target or reception. Targets
are actual passing yards, receiving yards, or receptions—not an Over result.

For the realized market value, the table provides previous game, rolling
3/5/8 mean and sample standard deviation, and season-to-date expanding mean and
sample standard deviation. Volume/efficiency includes lagged attempts,
dropbacks (attempts fallback), targets, receptions, passing/receiving yards,
snap/target/air-yard share, their three-game means, yards per attempt/target,
catch rate, targets per lagged team pass/dropback, home/away and schedule days
of rest. Opponent passing and receiving production allowed are the opponent's
trailing three completed games. Missing source observations remain null.

### Transparent role-change proxies

* `snap_share_delta_prior`: last game's share minus the preceding game's share.
* `snap_share_delta_rolling_3`: last game's share minus the mean of up to three
  games preceding that last game. Target-share deltas use the same definition.
* `targets_per_team_pass`: last-game targets divided by last-game team
  attempts/dropbacks.
* `usage_trend`: trailing-two mean of lagged targets minus trailing-five mean.
* `usage_acceleration`: current pregame usage trend minus its prior value.
* `air_yards_share`: lagged player share when nflverse supplies receiving air
  yards; otherwise null.
* `depth_chart_movement`: difference between consecutive strictly pregame depth
  ranks when timestamped depth observations are supplied.

These are opportunity-change proxies, not route participation.

## Leakage controls

Rows are ordered by UTC kickoff. Player targets and every usage series are
shifted one game **before** rolling/expanding operations. Season aggregates
group by season after shifting. Opponent values shift within the defensive team
before rolling. Rest is a backwards team schedule difference. Enrichments use
`merge_asof(direction="backward", allow_exact_matches=False)`, so even an
observation timestamped exactly at kickoff is rejected. No backward fill is
used. Deterministic tests mutate current/future outcomes and verify earlier
features remain identical, and separately cover opponent, injury, depth and
role-change boundaries.

## Optional interfaces

`AdvancedUsageProvider.pregame_features` reserves routes, route participation,
targets/yards per route, alignment, and coverage splits. It must return
player/game observations with timestamps. It is not implemented or scraped.
`WeatherProvider.pregame_features` similarly reserves temperature, wind/gust,
precipitation and probability keyed by game and forecast observation time.
Both use a separate strict pre-kickoff as-of join; neither is required.

## Cache, reproducibility, and sizing

`NflverseClient` streams to a temporary file, validates Parquet, then atomically
renames it. `build_seasons` writes a Parquet table and JSON build manifest.
`data/historical/` is gitignored. For roughly 10 recent seasons, expect about
4–10 GB cached when play-by-play is retained (well under 1 GB without it), an
approximately 0.2–1 GB feature table, and 10–40 minutes on a typical broadband
laptop. All-season play-by-play can exceed 10 GB. Release compression, chosen
seasons, network and hardware make these planning estimates—not guarantees.

Known gaps include reliable historical route participation, proprietary
charting/coverage/alignment, consistently timestamped injury/depth publication,
and complete older snap/air-yard data. They remain null or behind provider
contracts rather than being inferred from future information.

## Read-only settled research and player history

`python -m scripts.export_settled_research --date 2026-09-13 --output /tmp/settled-2026-09-13.csv`
exports a UTC kickoff slate; use a `.json` suffix or `--format json` for JSON. It
reads `DATABASE_URL`, joins only immutable `shadow_settlements`, and makes no
provider calls. Repeated observations are selected nearest their named 24h,
6h, 90m, and optional 15m targets. Missing windows and context remain null.
Line movement is retained separately in every window.

`app.player_history.build_player_history` creates one nflverse player/game row
with passing/receiving/reception outcomes, opportunity metrics, score/spread,
venue/weather, and explicitly available historical context. Its
`contextual_splits` helper provides N, hits, descriptive hit rate, mean, median,
sample standard deviation, threshold, and prominent small-N warnings. This is
research/V2-V3 discovery only and is not imported by the production pipeline.

The discretionary `research_selection_journal` is append-only and independent
of recommendations and wagering. Nothing is backfilled. Before deployment,
apply `migrations/20260914_research_history.sql`; then deploy the application
and run the existing settlement schedule. Existing settlement rows are not
rewritten; their immutable observation remains the legacy price authority.
