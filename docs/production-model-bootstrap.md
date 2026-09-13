# One-time production model bootstrap

The bootstrap is deliberately separate from the web process and scheduler. It
downloads only nflverse football data, audits the validation season, builds the
shared historical feature table, and invokes the existing benchmark trainer.
It neither fetches market prices nor places wagers.

## Railway Console command

Configure all three artifact variables to paths on the persistent `/models`
volume, then run this command once after deployment:

```bash
python -m scripts.bootstrap_production_models
```

The production defaults are training seasons 2022, 2023, and 2024, with 2025
alone used for model selection, validation reporting, and residual calibration.
The command rejects 2026 or later outcomes. Override historical years only with
`--training-seasons`; override the calibration year with `--validation-season`.
Use `--keep-temp` to retain a generated temporary directory for investigation,
or `--work-dir /path` to choose and retain one explicitly.

The command also selects and persists probability-calibration v2 parameters
using a chronological internal holdout within the validation season. After a
v2 code deployment, regenerate all three artifacts; scoring fails closed on
older probability semantics. See
[`probability-calibration-v2.md`](probability-calibration-v2.md).

## Operational estimate

The September 2026 audit downloaded approximately 15 MB of compressed parquet
inputs and used 22 MB of temporary disk. Allow **2–10 minutes**, **50 MB of
downloads**, and **250 MB of temporary disk** on Railway to accommodate network,
library, and future nflverse-release growth. The observed fit itself took about
22 seconds. The three observed artifacts were 31 KB, 93 KB, and 93 KB; allow
1 MB total for artifacts and JSON manifests.

Each destination is staged beside its final path and reloaded before an atomic
rename. If installation fails, existing artifacts are restored. Successful
artifacts have a sidecar `.manifest.json` containing seasons, timestamp, feature
schema, selected estimator, validation and baseline metrics, calibration
provenance, nflverse source audit, and the artifact SHA-256.
