# V2.0 post-capture integrity report

Run the report immediately after the 90-minute scheduler capture:

```bash
python -m scripts.run_v2_integrity_report
```

The default is the latest persisted `nfl_prop_v2` / `v2.0` 90-minute
canonical decision batch. Optional selectors are:

```bash
python -m scripts.run_v2_integrity_report --event-id PROVIDER_EVENT_ID
python -m scripts.run_v2_integrity_report --execution-id SCHEDULER_EXECUTION_UUID
python -m scripts.run_v2_integrity_report --compact
```

The command is read-only: it creates a SQLAlchemy engine without running table
creation and issues only `SELECT` statements. It does not instantiate the
scheduler or any sportsbook, Kalshi, settlement, or wagering client. Output is
JSON with a stable `v2-integrity-report-v1` schema identifier. It exits zero
only for `PASS`; `FAIL` and `INCOMPLETE` exit one.

`shadow_opportunity_decisions` is the strategy population. Its persisted over
and under observations are used only to independently recompute identity,
digest, price, probability, reference, model, freshness, timestamp, and frozen
policy invariants. Raw observations and settlement results are never treated
as V2 selections or performance.

## Status meanings

* `PASS`: every required reconstructable invariant passed.
* `FAIL`: at least one invariant failed (including no canonical decisions, an
  incomplete capture slot, or a duplicate selected exposure).
* `INCOMPLETE`: no invariant failed, but required prospective evidence was not
  persisted and is reported as `NOT_VERIFIABLE`.

Scheduler execution records store event lists in JSON and have no relational
foreign key to decisions. `--execution-id` therefore scopes decisions through
the execution's persisted 90-minute event identities, and the report explicitly
states this schema limitation. No migration or environment-variable change is
required.
