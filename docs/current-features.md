# Production current pregame features

The materializer downloads only the maintained nflverse schedules, weekly
player statistics for the target season and two preceding seasons, and the
current-season roster Parquet release. It does not use
any sportsbook, market, Kalshi, credential, line, or price endpoint. It selects
the next scheduled NFL week, admits roster players with stable GSIS IDs and
completed NFL participation in the bounded history window, and appends outcome-empty target rows
to `app.historical.features.build_modeling_table`. Thus current rows use the
benchmark's exact shifted/rolling definitions rather than a parallel feature
implementation. Previous-game, rolling 3/5/8, usage, role-change, and opponent
history cross a season boundary because their training transforms group by
stable player/team identity. Season-to-date statistics and team rest reset by
season, so Week 1 has no season-to-date value and no artificial offseason rest.
All source games must be completed before the build time (which is strictly
before every target kickoff); released future or target-game rows are ignored.
The nflverse `gameday`/`gametime` pair is an Eastern wall-clock value. The
normalizer localizes it with `America/New_York` (including EST/EDT rules) before
converting it to UTC; it never labels that naive source value as UTC.

Run it on Railway with the model artifact environment variables and output all
pointing into the same mounted volume:

```bash
python -m scripts.build_current_features --output "$FOOTBALL_CURRENT_FEATURE_PATH"
```

For a validation-only run, append `--dry-run`; it fetches, constructs, and
validates but never replaces the destination. A successful normal run writes a
temporary Parquet beside the destination, reads and validates it, then performs
an atomic `os.replace`. Configure, for example, all three
`FOOTBALL_ARTIFACT_PLAYER_*` paths and `FOOTBALL_CURRENT_FEATURE_PATH` beneath
`/models` on one Railway volume.

During NFL weeks, schedule a refresh after nflverse weekly data is updated and
then every 6 hours through the active slate (plus immediately after completed
games are published). This captures changing completed-game usage while the
strict cutoff and timestamps remain before each target kickoff. Daily refreshes
are sufficient in the offseason. The JSON report records retrieval/cutoff
times, requested/used history seasons and row counts, per-player history status,
exclusions, schema status, destination, and run time without printing source
URLs.

After deploying a schedule-timezone normalization change, rebuild any existing
current-feature cache before running production. Kickoff is part of the exact
feature/provider identity, so a cache written with naive Eastern values labeled
as UTC cannot match provider event commence times safely.
