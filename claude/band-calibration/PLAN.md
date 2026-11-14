# PLAN: Octopus band calibration

Build plan for the `band-calibration` cycle. Vision in `VTO.md`. Decision record in
`docs/adr-octopus-band-scorecard.md`.

**Same Page Meeting: not held.** The Codex CLI was not reachable from the session that
wrote this plan, so no adversarial review has run. Treat this plan as unreviewed until the
operator either runs that review or waives it.

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

## Rocks

Order is fixed. Each Rock is its own draft PR.

### Rock 1: Band scorecard

**Scope.** `score_forecast` adds `interval_score`, `interval_score_raw`, `below_band` and
`above_band` per component. `MetricsAccumulator` and `HorizonMetricsAccumulator` report mean
interval score and below/above rates. Records from older ledgers still report: the
calibrated interval score is recovered from the stored pinball losses, and the fields that
cannot be recovered are `None`.

**Non-goals.** No calibrator change. No model change. No markdown layout redesign beyond
adding the new columns.

**Proof.** `cd wondrous-octopus && python -m pytest tests/test_band_scorecard.py tests/test_metrics.py tests/test_multi_horizon.py -q`
then the full `wondrous-octopus` suite.

**Tests.** `wondrous-octopus/tests/test_band_scorecard.py` (written, 24 of 25 failing by
design until the implementation is approved).

### Rock 2: Honest uncertainty

**Scope.** A torch-free module that takes per-date aggregates and returns a moving-block
bootstrap confidence interval for coverage, interval score and skill vs persistence. Whole
dates are resampled together and block length is at least the horizon. Kupiec and
Christoffersen tests at horizon 1 only.

**Non-goals.** No CPCV, no PBO, no Model Confidence Set.

**Proof.** Tests on a seeded synthetic ledger with a known breach rate recover it inside the
interval at the stated rate, and a ledger with clustered breaches gets a wider interval than
an independent one. Existing suite passes.

### Rock 3: Calibrator replay harness

**Scope.** An offline tool that reads stored raw quantiles and realized values from an
existing run directory, runs them through any calibrator that satisfies the existing
interface, and writes a scorecard using Rocks 1 and 2. Read-only on the source ledger.

**Non-goals.** No retraining. No writes into the source run directory.

**Proof.** Replaying the current calibrator reproduces the ledger's calibrated coverage
exactly. Existing suite passes.

### Rock 4: Candidate calibrators (shadow)

**Scope.** Two candidates behind the existing interface: separate lower and upper scales,
and scales per trailing-volatility bucket. Compared in the replay harness only.

**Non-goals.** No change to the default. No per-ticker scales (too few observations per
ticker at long horizons).

**Proof.** A replay report comparing each candidate with the current calibrator on interval
score, with bootstrap intervals, split by regime. Existing suite passes.

## Promotion rule (pre-registered, not applied in this cycle)

A candidate is eligible for a promotion decision only if its interval score beats the
current calibrator with a 95% block-bootstrap interval that excludes zero, and its coverage
in every named stress regime is no further from 80% than the current calibrator's.

## Deferred

See `DEFERRED.md` beside this file.
