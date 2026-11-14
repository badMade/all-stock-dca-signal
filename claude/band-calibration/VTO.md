# VTO: Octopus band calibration

Vision document for the `band-calibration` cycle (rocket-fuel Kickoff, 2026-11-14).
The build plan is `PLAN.md` beside this file. The decision record is
`docs/adr-octopus-band-scorecard.md`.

## What

Better measurement of the Wondrous Octopus p10-p90 bands, then a shadow-only test of
whether a different calibrator makes them better.

## Why now

A planning session on "backwards training" ended with a different finding. The model's
information is in its bands, not its direction (long-run hit rate 50.89%, coverage 79.85%
against 80%, `artifacts/octopus_evaluation_report.md`). The bands are right on average and
uneven per ticker (77.0% to 84.8% in the same report). Nothing in the repo can currently
say whether that spread is noise or a fixable fault.

## Who

The repository owner, reading Octopus research reports.

## Success criteria for this cycle

1. Reports show the interval score and the below/above breach split per horizon and regime.
2. Coverage and skill carry confidence intervals that respect overlapping horizons and
   same-day correlation across tickers.
3. One append-only trust scorecard shows how the bands are doing over time and flags them
   when coverage tests reject.
4. The evaluation harness is shown to report no skill where none exists.
5. A written, evidence-backed answer to: does an asymmetric or volatility-bucket calibrator
   beat the current one?
6. The time-reversed control and CPCV each report into the same scorecard, so their value
   can be judged against their cost.

## Non-negotiables

- Suggest-only. Nothing here reaches live DCA sizing.
- No change to the default calibrator in this cycle. Promotion is a separate decision.
- No lookahead. Every bucket and scale is computed from data revealed at that date.
- Existing run ledgers stay readable. Old records are never rewritten.
- No new dependency.
- Tests first, shown for approval before each implementation.

## Out of scope

Backcasting to fill pre-listing history, training on the model's own point projections,
PBO, Model Confidence Set, and the TSTR round trip (next cycle). Reasons are in
`DEFERRED.md` and the ADR.
