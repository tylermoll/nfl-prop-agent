# Probability diagnostic audit

`scripts/audit_probability_pipeline.py` is a read-only reproduction and audit
of the live empirical-residual probability path. It does not import provider or
scheduler entry points, retrain artifacts, or write to the observation database.

Run it in the production environment, where the deployment-only artifacts and
database credentials exist:

```bash
PYTHONPATH=. python scripts/audit_probability_pipeline.py \
  --artifact player_pass_yds="$FOOTBALL_ARTIFACT_PLAYER_PASS_YDS" \
  --artifact player_reception_yds="$FOOTBALL_ARTIFACT_PLAYER_RECEPTION_YDS" \
  --artifact player_receptions="$FOOTBALL_ARTIFACT_PLAYER_RECEPTIONS" \
  --database-url "$DATABASE_URL" \
  --trace player_receptions 1.7 4.5 \
  --output probability-audit.json
```

The artifact section includes every bucket's exact boundaries, sample count,
prediction and residual summaries, ECDF step size, and sorted residuals. The
trace identifies the selected bucket, cutoff (`line - prediction`), exact
residuals in the strict Over tail, and effective sample size. The production
section reports probability and edge distributions overall and by market,
side, residual bucket, capture window, and their cross-product. It also counts
absolute and positive edges above 5, 8, 10, 15, 20, and 30 percentage points.

Same-threshold reference probabilities are stored in Over orientation. The
audit complements that value for Under rows before measuring disagreement and
reports counts over 10, 20, 30, and 40 percentage points. It also independently
recomputes break-even probability from American odds and recomputes edge from
the offered-side probability, reporting any invariant failures.

## Interpretation of the reported Egbuka values

The live calculation defines residual as `actual - prediction`, selects a pool
by the point prediction, and calculates
`P(Over line) = mean(residual > line - prediction)`. Under is the complement,
so on a half-point receptions line it is exactly `P(actual < line)`.

For a point prediction of 1.7 and line of 4.5, the residual cutoff is 2.8. A
displayed Over probability of 5.4% and Under probability of 94.6% are consistent
with exactly 2 strict exceedances in a 37-row bucket (`2 / 37 = 5.405%`). The
artifact trace is required to identify those two residual values and confirm
the denominator; display rounding alone does not uniquely prove either.

## Feature freshness

Two deliberately separate settings are involved. The production-cycle cache
may be reused for up to `FOOTBALL_CURRENT_FEATURE_MAX_AGE_SECONDS` (default
21,600 seconds), while persisted observation metadata labels model features
stale using `scheduler_model_stale_seconds` (default 3,600 seconds). Thus an age
of roughly 6,618 seconds may pass cache reuse but be labeled stale. This is not
a dashboard calculation error: the dashboard displays the immutable stale flag.
The differing operational meanings should be renamed or explained in the UI,
but changing either threshold would change production behavior and is outside
this diagnostic task.
