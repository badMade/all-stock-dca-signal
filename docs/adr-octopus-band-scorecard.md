# Octopus band scorecard and calibrator evaluation

Status: proposed. (2026-11-24 audit: Rocks 1 to 4 are in the code: the interval score and breach sides in
`wondrous-octopus/metrics.py`, `band_uncertainty.py`, `trust_scorecard.py` and `synthetic_truth.py`.
Rock 5, the calibrator replay, waits on open questions 1 and 2.)

## Context

Wondrous Octopus presents p10-p90 bands that an adaptive conformal calibrator widens or
narrows once per market day. Reports show coverage and mean band width as separate
numbers. That leaves three questions unanswerable:

- Is a band good, or only wide? Coverage rewards width and width rewards narrowness.
- Does the band fail on one side? The calibrator scales symmetrically around p50.
- Is 79.85% different from 80%? Horizon-h errors from consecutive days overlap, and
  thousands of tickers share each day's market move, so a count of ticker-days overstates
  the evidence.

A planning session first considered estimating accuracy by training backwards in time, or
by retraining on the model's own projections. Both were set aside. Equity volatility
behaves differently in reverse (Zumbach 2009, arXiv:0708.4022), so a reversed model is
scored on dynamics the forward model never faces. Retraining on projections scores the
model against itself.

## Decision

1. Add the interval score (Gneiting and Raftery 2007, JASA 102:359-378) for the calibrated
   and raw bands, and a below/above breach flag, to the per-forecast score record.
2. Keep the change additive. Existing keys keep their meaning. Older ledgers stay readable:
   the calibrated interval score equals `(2 / alpha)` times the sum of the stored p10 and
   p90 pinball losses, so it is recovered; breach side and the raw score are reported as
   `None`.
3. Estimate uncertainty by resampling whole dates in blocks at least as long as the
   horizon. Run Kupiec and Christoffersen tests at horizon 1 only, because at longer
   horizons breaches are serially dependent by construction.
4. Evaluate candidate calibrators by replaying stored raw quantiles, never by retraining,
   and never by changing the default in the same change.

## Alternatives considered

| Alternative | Why not |
|---|---|
| CRPS | Needs a full predictive distribution; the model emits three quantiles |
| Per-ticker calibrator scales | Too few independent observations per ticker at long horizons |
| Backfilling the new fields into old ledgers | Rewrites checkpointed, hashed shards; the recovery identity makes it unnecessary |
| A statistics dependency for the tests | The formulas are short and the subproject already carries numpy |

## Consequences

- Score records grow by four small per-component maps.
- Reports can rank calibrators on one proper score.
- Nothing here reaches live sizing. Octopus stays a research artifact.

## Operator decisions (2026-11-14)

- The evaluation target is live forward trust, judged over time. Forward-only results stay
  the headline; cross-validated results are context.
- A standing, append-only trust scorecard is a permanent requirement.

## Open questions

1. Replay source for Rock 5: which run directory, and does it hold raw quantiles for every
   horizon?
2. Volatility bucket definition for Rock 5: trailing realized-volatility terciles, or the
   existing ticker clusters?
3. Online components inside CPCV folds (Rock 7): frozen or replayed.

Settled on 2026-11-14: the promotion rule in `claude/band-calibration/PLAN.md`; trust
scorecard horizons 1, 5, 21, 63 and 90; a 63-trading-day rolling window.
