# Prospective shadow evaluation

This subsystem is read-only paper accounting. It contains no account client,
order type, wager submission, recommendation label, stake sizing, or execution
endpoint. The football estimator remains independent: a leakage-safe feature
row produces a point estimate, and the exact Hard Rock threshold is evaluated
directly with the validation-period, prediction-conditional empirical residual
ECDF. Sportsbook and Kalshi fields are attached only after scoring. In
particular, sportsbook lines are never interpolated into a model probability,
and the rejected receiving-yards additive correction is not applied live.

## Observation and price semantics

`shadow_observations` is append-only: its UUID is the primary key and repeated
player/market/line quotes insert new rows. Each side records game, kickoff,
player/team/opponent, line, raw Hard Rock American price, offered-price
break-even probability, paired no-vig context when supplied, model probability,
edge, expected return, artifact and feature/uncertainty provenance, reference
line median/range/book count/exact-line consensus, Kalshi microstructure, raw
source IDs and freshness timestamps. PostgreSQL is the deployment target;
SQLAlchemy SQLite URLs are supported for local tests.

For American odds `a`, break-even is `100/(a+100)` when positive and
`abs(a)/(abs(a)+100)` when negative. Decimal payout is `1+a/100` when positive
and `1+100/abs(a)` when negative. Expected net return per $1 risked is
`p*(decimal_payout-1) - (1-p)`. This uses the offered price—not paired no-vig
probability. Kalshi YES ask is executable for buying YES; YES bid is executable
for selling YES/opposite-side context; midpoint is descriptive and
non-executable.

Default absolute model-edge research tiers are `<2pp`, `2–5pp`, `5–8pp`, and
`8pp+`; boundaries are configurable. Consensus agreement, Kalshi agreement,
both-agree, both-disagree, and insufficient-data flags are independent context,
not filters or approvals.

## Snapshots, settlement, and reports

Post-kickoff observations are rejected. Within an exact game/player/market/
line/side series, chronological first, latest, best payout, and final strictly
pre-kickoff rows are identifiable. Comparing first or any entry with final line
and same-line price supports descriptive closing-line research; movement does
not prove predictive edge.

Completed nflverse/historical statistics join on canonical game, player and
market. Actual above/below the threshold settles Over/Under inversely; equality
is a push. A win earns decimal payout minus one, a loss earns -1, and a push 0
per dollar. Reports use fixed $10 units and separately retain the initial $100
research bankroll; there is no compounding or resizing.

Reporting can group by market, side, edge tier, model version/family,
line-divergence, reference agreement, Kalshi agreement, joint confirmation,
Kalshi liquidity/spread bucket, and time-to-kickoff bucket. It returns count,
win rate excluding pushes, average odds/edge/estimated return, realized ROI,
$10 P&L, Brier score, probability-bucket calibration, and chronological maximum
drawdown. Configurable minimum-sample warnings prohibit premature conclusions.

## Historical limitation and collection cadence

There is no historical archive of Hard Rock player-prop prices sufficient for
a clean retrospective market backtest. Never fabricate historical odds,
backfill today's prices into old games, or claim historical betting ROI before
prospective observations exist.

Given the existing Odds API cost of three credits per eligible game, collect a
small number of slate-wide snapshots: approximately 24 hours, 6 hours, 90
minutes, and 15 minutes before kickoff, with one final pregame capture where
quota permits. Deduplicate polling schedules, query only eligible unstarted
events, retain quota response headers, and reduce cadence rather than exceed the
account's current quota. Kalshi collection can accompany these read-only runs;
never infer missing exact thresholds or substitute its midpoint for execution.
