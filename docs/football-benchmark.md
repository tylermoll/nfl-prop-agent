# Football-only benchmark modeling

This benchmark independently predicts realized passing yards, receiving yards,
and receptions. It consumes only the cached historical feature table; it does
not call a sportsbook, The Odds API, or Kalshi and does not produce actions,
stakes, expected value, or recommendations.

## Reproducible design

The default split uses all seasons before the penultimate season for training,
the penultimate season for validation/model selection and residual calibration,
and the latest season as a final untouched test. Whole-season boundaries are
validated so the last training kickoff strictly precedes the first validation
kickoff and the last validation kickoff strictly precedes the first test
kickoff. Explicit seasons can be passed on the command line.

For every canonical market, validation MAE selects among ridge regression
(`alpha` 0.1, 1, 10), L2-regularized robust Huber SGD (`alpha` 0.0001, 0.001), and
histogram gradient boosting (15 or 31 leaves). Median imputers with missingness
indicators are fit inside each training pipeline; linear models are scaled.
The selected family is refit on train plus validation for test prediction.
An optional additive intercept correction is the mean validation residual from
the train-only model. It is retained only when it reduces validation MAE, then
applied unchanged to the refit model's test predictions. Test outcomes never
select or estimate this correction; both corrected and uncorrected metrics are
retained for auditability.

Baselines are previous-game value, rolling-three, rolling-five,
season-to-date, and an equal blend of the latter three. All come directly from
the leakage-safe pregame columns. The feature allowlist and persisted report
group features into recent performance, usage, role change, team environment,
and opponent/game context. New columns are excluded by default, especially
identifiers, outcomes, targets, prices, lines, and provider data.

Validation residuals from a model fit only on the training split define a
market-specific empirical residual distribution. The report includes 50%/80%
interval coverage and an empirical-CDF exceedance probability diagnostic at
thresholds offset around each test prediction, with reliability buckets and a
Brier score. A constant residual distribution can miss heteroskedasticity; the
report therefore compares the global intervals with prediction-conditional
intervals. Conditional bin boundaries and residual distributions are fitted on
validation predictions only, and residual standard deviations by prediction
quartile are also reported.
These provisional distributions are diagnostics and are not validated for
betting decisions.

## Running

```bash
python -m scripts.run_football_benchmark data/historical/features.parquet \
  --validation-season 2023 --test-season 2024 \
  --output artifacts/benchmarks/2024
```

The ignored output directory contains one joblib model per market, split and
feature metadata plus metrics in `metrics.json`, held-out predictions,
predictive intervals, and threshold reliability diagnostics. The metrics JSON
also contains market/season/position breakdowns, the sequential ablation, and
error diagnostics.
