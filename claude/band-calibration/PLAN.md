# PLAN: Octopus band calibration

Build plan for the `band-calibration` cycle. Vision in `VTO.md`. Decision record in
`docs/adr-octopus-band-scorecard.md`.

**Same Page Meeting: waived.** The Codex CLI was not reachable from the session that wrote
this plan, so no adversarial review ran. The operator waived it for this cycle on
2026-11-14. Review happens on each draft PR instead.

## Starting point (facts, read 2026-11-14 at `158955be`)

- `model_interface.ConformalCalibrator` and `HorizonConformalCalibrator` already apply an
  adaptive conformal width update, once per market day, per component and per horizon. One
  symmetric scale around p50, shared by every ticker.
- `octopus_model.py` already trains on Huber point loss plus pinball loss per quantile.
- `metrics.score_forecast` ledgers `pinball`, `in_band`, `in_band_raw` and `band_width`.
  It has no interval score and no breach side.
- `metrics.HorizonMetricsAccumulator` reports skill vs persistence on MAE only.
- No bootstrap, Kupiec, Christoffersen or Diebold-Mariano code exists in `wondrous-octopus/`.
- `scripts/run_90day_prediction_experiment.py` defaults to `--model persistence`.
- `data.py` scales each bar by the archive's `Adj Close` ratio, so prices are back-adjusted
  as of the download date. Scoring is in log-return space.

## Operator decisions (2026-11-14)

- The target is **live forward trust**, with an accuracy assessment that can be judged
  over time. Generalization across regimes is secondary.
- All five candidate designs from the planning research are in scope. They are built in
  cost order, and each is kept or dropped on evidence from the standing scorecard.
- A standing way to judge how well things are working is a permanent requirement, not a
  one-off report.
- Signed off: the promotion rule below, the trust scorecard horizons (1, 5, 21, 63, 90)
  and its 63-trading-day rolling window.

## Rocks

Seven Rocks, at the cycle limit. Order is fixed: each one is scored with the ones before it.
Each Rock is its own draft PR, tests first. Rocks 5 to 7 need full Octopus retrains, so each
starts with a measured cost estimate and waits for a go-ahead.

| # | Rock | Research design | Retrains |
|---|---|---|---|
| 1 | Band scorecard | A | 0 |
| 2 | Honest uncertainty | A | 0 |
| 3 | Trust scorecard over time | A, cross-cutting | 0 |
| 4 | Synthetic-truth recovery | D | 0 (small synthetic runs) |
| 5 | Calibrator replay and candidates | (accuracy improvement) | 0 |
| 6 | Time-reversed control | C | about 2 |
| 7 | Purged and embargoed CPCV | B | 5 to 15 |

### Rock 1: Band scorecard

**Scope.** `score_forecast` adds `interval_score`, `interval_score_raw`, `below_band` and
`above_band` per component. `MetricsAccumulator` and `HorizonMetricsAccumulator` report mean
interval score and below/above rates in `report.json`. Records from older ledgers still
report: the calibrated interval score is recovered from the stored pinball losses, and the
fields that cannot be recovered are `None`.

**Non-goals.** No calibrator change. No model change. No change to `report.md` tables.

**Proof.** `cd wondrous-octopus && python -m pytest tests/test_band_scorecard.py tests/test_metrics.py tests/test_multi_horizon.py -q`
then the full `wondrous-octopus` suite.

**Status.** Implemented on PR #2300.

### Rock 2: Honest uncertainty

**Scope.** A torch-free module that takes per-date aggregates and returns a moving-block
bootstrap confidence interval for coverage, interval score and skill vs persistence. Whole
dates are resampled together and block length is at least the horizon. Kupiec and
Christoffersen tests at horizon 1 only. Skill vs persistence on interval score, beside the
existing MAE skill.

**Non-goals.** No Model Confidence Set (deferred).

**Proof.** `cd wondrous-octopus && python -m pytest tests/test_band_uncertainty.py -q`, then
the full suite. A seeded series with a known 80% rate is recovered inside the interval, and
the same daily values arranged in runs get a wider interval than when interleaved.

**Status.** Implemented as `band_uncertainty.py` on the PR stacked on #2300. Functions only;
wiring into reports is Rock 3.

### Rock 3: Trust scorecard over time

**Scope.** The standing answer to "how well is this working". `trust_scorecard.py` appends
one row per as-of date, model and horizon to `<run>/trust/trust_scorecard.jsonl`:
rolling-window coverage with a date-block interval, below/above rates, interval score, skill
vs persistence, and per-ticker coverage-test rejection shares at horizon 1. Each row has a
status: `ok`, `flagged` (the coverage interval excludes 80%, in either direction) or
`unknown` (too little history). Every later Rock reports into the same history, so designs
are compared on one table.

**Non-goals.** No alerting channel. No console page (follow-up). No change to
`daily_update.py`: the scorecard is a separate command, so the hardened daily cycle is
untouched. The flag is a label on research output and does not reach live sizing.

**Window.** The signed-off 63 trading dates for horizons 1 and 5. Horizons 21, 63 and 90 use
84, 252 and 360 dates, because 63 dates hold at most one block at those horizons and no
interval can be estimated from one block.

**Proof.** `cd wondrous-octopus && python -m pytest tests/test_trust_scorecard.py -q`, the
full suite, and `python trust_scorecard.py --run-dir <demo run>` twice: the second run
appends nothing.

**Status.** Implemented on the PR stacked on #2303.

### Rock 4: Synthetic-truth recovery

**Scope.** `synthetic_truth.py` writes simulated archives with a known answer and replays
them through the unmodified harness: a random walk (no skill is possible), a
volatility-clustering process with a leverage effect (size is predictable, direction is
not), and a planted drift with lag-1 correlation 0.5 (skill must be found).

**Non-goals.** No claim that the simulators resemble real markets. They test the harness.
Only the GBM quantile model is replayed; replaying Octopus itself is a longer run and is
listed in `DEFERRED.md`.

**Proof.** `python wondrous-octopus/synthetic_truth.py --scenario all --work-dir <dir>`
exits 0. On the random walk and the volatility process the skill interval does not lie
above zero and the hit-rate interval does not lie above 50%. On the planted drift both do.
A unit test plants a lookahead leak in score records and confirms the check fails.

**Status.** Implemented on the PR stacked on #2304. All three scenarios pass; the measured
table is in `docs/wondrous_octopus.md`.

**Finding for Rock 5.** Calibrated coverage was about 77% in all three runs, with intervals
that exclude 80%. On 390 scored dates the adaptive calibrator had not closed the gap.

### Rock 5: Calibrator replay and candidates

**Scope.** An offline tool that reads stored raw quantiles and realized values from an
existing run directory, runs them through any calibrator that satisfies the existing
interface, and scores the result with Rocks 1 to 3. Two shadow candidates: separate lower
and upper scales, and scales per trailing-volatility bucket.

**Non-goals.** No retraining. No writes into the source run directory. No change to the
default calibrator. No per-ticker scales.

**Proof.** Replaying the current calibrator reproduces the ledger's calibrated coverage
exactly. A replay report compares each candidate with the current calibrator.

### Rock 6: Time-reversed control

**Scope.** Train the identical pipeline on time-reversed series, with every feature
recomputed on the reversed series, and score both models on the same forward held-out
data. A diagnostic of whether the model uses direction-specific structure. It is not an
accuracy test.

**Non-goals.** No reversed model in any forecast path. No reuse of precomputed features.

**Proof.** A test that fails if any feature of the reversed run was computed before
reversal. A scorecard row comparing forward-trained and reversed-trained models.

### Rock 7: Purged and embargoed CPCV

**Scope.** Combinatorial purged cross-validation with a purge and embargo of at least 90
trading rows, reported as a distribution in the trust scorecard beside the forward-only
result.

**Non-goals.** Not a replacement for forward-only evaluation, which stays the headline
because the target is live forward trust.

**Proof.** A test that no training label overlaps a test block. A report showing whether
the forward-only result falls inside the CPCV distribution.

**Open before this Rock starts.** Whether the online-updated components are frozen inside
each fold or replayed update by update.

## How the designs get compared

Every Rock writes its result into the Rock 3 history using the same metrics, and
`DEFERRED.md` records the cost of each. A design is kept if it changed a decision or caught
a fault; otherwise it is retired after one cycle.

## Promotion rule (pre-registered, signed off 2026-11-14, not applied in this cycle)

A candidate calibrator is eligible for a promotion decision only if its interval score beats
the current calibrator with a 95% block-bootstrap interval that excludes zero, and its
coverage in every named stress regime is no further from 80% than the current calibrator's.

## Deferred

See `DEFERRED.md` beside this file.
