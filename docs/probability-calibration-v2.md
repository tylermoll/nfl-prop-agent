# Probability calibration v2 production decision

## Diagnosis and selection

The v1 arithmetic is correct. A 1.7 reception prediction against 4.5 asks for
the residual tail above 2.8; 64 / 1,133 is 5.65%. The directional edge is
primarily the large point-to-line displacement, not an ECDF sign or complement
bug, and must not be hidden by a probability cap.

V1 nevertheless has two defensible defects: a raw bucket frequency can return
exactly zero or one, and hard prediction buckets treat their conditional
residual laws as known without error. The 2025 residual bucket mean biases were
-3.450/+3.754/+3.346/-3.650 yards (passing),
+5.687/+4.115/+5.775/+8.135 yards (receiving), and
+0.336/+0.137/+0.259/+0.180 catches. Receiving yards therefore has a distinct
market-wide positive bias and greater heteroscedasticity than receptions; the
markets should not share one uncertainty scale.

Candidate selection used only 2025 outcomes from a model fit on 2022--2024.
The first chronological 60% supplied residuals and the final 40% supplied
held-out outcomes. Candidates were raw four-bucket ECDF, Jeffreys continuity
correction, 1/2/4/8 buckets, and 0/25/100/400 market-wide pseudo-observation
weights. A five-point half-line threshold grid around each prediction tests the
prediction-minus-threshold mapping without prices or today's outcomes. A
logistic threshold classifier was rejected as needless complexity: repeated
synthetic thresholds do not create independent outcomes.

V2 continuity-corrects local and market ECDFs and blends them by
`n / (n + prior_weight)`. Selection chose 4 buckets/weight 100 for passing,
8/0 for receiving yards, and 8/25 for receptions. Thus a 1,133-row bucket is
only lightly shrunk; v2 does not erase a supported extreme tail.

## Held-out results

| Market | v1 Brier | v2 Brier | v1 / v2 ECE | v1 / v2 extreme frequency |
|---|---:|---:|---:|---:|
| Passing yards | 0.197779 | 0.197195 | 0.033015 / 0.024558 | 0.00000 / 0.00000 |
| Receiving yards | 0.152197 | 0.151842 | 0.008512 / 0.012304 | 0.18523 / 0.18346 |
| Receptions | 0.161147 | 0.160569 | 0.014001 / 0.010269 | 0.10154 / 0.10364 |

Extreme means below 5% or above 95%. Over mean probability error changed from
+2.306pp to +2.415pp (passing), +0.375pp to +0.392pp (receiving), and +0.621pp
to +0.687pp (receptions). Under errors are exactly the negatives because Under
remains the strict-Over complement. Prediction-quartile v1/v2 Brier scores were
0.211262/0.213411, 0.196061/0.195473, 0.203171/0.200255,
0.180676/0.179659 (passing); 0.097317/0.097010, 0.131048/0.130040,
0.169504/0.169629, 0.210911/0.210682 (receiving); and 0.123425/0.122128,
0.133768/0.132549, 0.187146/0.187909, 0.200246/0.199688 (receptions).
Existing four-bucket 50%/80% coverage on the complete out-of-fit 2025 residual
set was 49.71%/80.12%, 50.01%/79.88%, and 50.01%/79.88%, respectively. The
small gains support a narrow robustness correction, not broad edge suppression.

Historical football data has no sportsbook-line field, so it cannot establish
the live line distribution. The read-only audit now reports live
`line - point_prediction` and old/new counts above 10pp, 20pp, and 30pp without
updating observations. No production credential or observation export was
available here, so production counts are intentionally not invented.

## Deployment

Artifacts must be regenerated. Live scoring fails closed on v1 rather than
silently applying new semantics to an old artifact. With all three artifact
environment variables pointing into `/models`, run in Railway:

```bash
python -m scripts.bootstrap_production_models
python -m scripts.run_production_preflight
```

New observations carry method
`shrunk_prediction_conditional_empirical_residual_ecdf`, version `2`; immutable
old observations retain v1. Deployment before a slate is safe only after all
three artifacts regenerate atomically and preflight exits zero. Neither command
makes a live Odds API or Kalshi request by default.
