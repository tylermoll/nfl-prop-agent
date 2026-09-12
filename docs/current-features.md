# Production current pregame features

The materializer downloads only the maintained nflverse schedules, weekly
player statistics, and current-season roster Parquet releases. It does not use
any sportsbook, market, Kalshi, credential, line, or price endpoint. It selects
the next scheduled NFL week, admits roster players with stable GSIS IDs and
completed current-season participation, and appends outcome-empty target rows
to `app.historical.features.build_modeling_table`. Thus current rows use the
benchmark's exact shifted/rolling definitions rather than a parallel feature
implementation.

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
times, slate and row counts, exclusions, schema status, destination, and run
time without printing source URLs.
