# Residual reversal vs the graded ladder: first real-data cost-basis run

Status: research output only. Not a decision. The promotion test failed for every variant, so
nothing here supports wiring `residual_reversal` into live sizing.
Date: 2026-11-24
ADRs: [`docs/adr-cost-basis-backtest-harness.md`](../adr-cost-basis-backtest-harness.md) (the harness),
[`docs/adr-residual-reversal-suggester.md`](../adr-residual-reversal-suggester.md) (the candidate).
Code: `analysis/2026-11-24-residual-reversal-cost-basis/` at the commit that added this file.

## What was run

```bash
PYTHONPATH=.:dca-sizing-signals python analysis/2026-11-24-residual-reversal-cost-basis/run_cost_basis_backtest.py \
    --history-dir ../ticker_data --out rr_cost_basis.json
```

- **Window:** 1,480 sessions, 2021-01-04 to 2026-11-23. In sample to 2023-12-29, out of sample
  from 2024-01-02. Closes from 2020-01-02 on are loaded for warm-up.
- **Universe:** `config/tickers.json` `active` as of 2026-11-24 (122 symbols), so
  **survivor-biased**: names that were dropped or delisted are absent, and the harness labels
  the run that way.
- **Sector map:** `sector_map_draft.json` (`2026-11-24-draft`), assigned by hand and **not
  reviewed**. 102 tickers map to 10 ETFs (SMH, XLK, XLC, XLY, XLF, XLI, XLV, XLP, XLRE, XLU); the
  20 funds and unclassified tickers are unmapped, so they buy at the baseline. Today's
  classification is applied to every past session.
- **ETF data:** the archive had no sector-ETF histories. Full daily Yahoo history for SPY and 13
  sector/industry ETFs (XLK XLF XLV XLE XLY XLP XLI XLC XLB XLU XLRE SMH IGV) was added to the
  Drive `Tickers` archive on 2026-11-24 with the `stock-history-downloader` skill, with the
  operator's approval.
- **Baseline:** a point-in-time replay of the 3:30 PM ladder at daily granularity: decision price =
  the session close, base $2 below the prior close and low or $1 below the prior close only, the
  stepped global ladder from `config/sizing_ladder.json`. 75,359 buys, $277,028. **Not modelled:**
  the 60-minute down-trend filter, budget caps and tier escalation, the other six slots, no-buy
  windows, whole-share accumulation, per-ticker calibration. Real dollars were lower and fewer.
- **Candidate:** `ResidualReversalSuggester` with the ADR's untuned defaults, plus two slope
  variants (0.05, 0.2) declared before the first run. All three are trials.
- **No corporate events:** there is no offline point-in-time 8-K or earnings source, so the
  `corporate_event` guard never fired.
- **Costs:** 0 bp. Robinhood charges no commission, and both arms fill at the same close, so a
  spread cost would scale both arms equally and cancel in the paired comparison.

## Results

Every variant was accepted: no lookahead flags, no unavailable sessions, and budget-neutral on
every session.

### Primary: paired per-ticker cost-basis improvement (bp, positive = candidate paid less)

| Variant | Period | Mean | Median | $-weighted | t (cross-ticker) | Tickers improved |
|---|---|---|---|---|---|---|
| default (slope 0.1) | in sample | +1.92 | +1.46 | +2.12 | 1.99 | 56% |
| | **out of sample** | **+1.43** | **0.00** | **+2.03** | **1.03** | **47%** |
| slope 0.05 | in sample | +0.97 | +0.81 | +1.08 | 2.00 | 57% |
| | out of sample | +0.73 | 0.00 | +1.04 | 1.05 | 46% |
| slope 0.2 | in sample | +2.65 | +1.81 | +2.87 | 1.84 | 58% |
| | out of sample | +2.06 | 0.00 | +2.92 | 1.02 | 48% |

The improvement scales with the slope, as a budget-neutral tilt's would, but out of sample it is
one standard error from zero and fewer than half the tickers improved. The out-of-sample median
of 0.00 reflects the many tickers at exactly zero: the 20 unmapped ones and those the guard
suppressed for the whole period.

### Also reported (default variant)

| Metric | In sample | Out of sample |
|---|---|---|
| Terminal wealth per dollar, candidate minus baseline | +5.9 bp | +0.6 bp |
| Forward return of the tilt dollars, 5 / 10 / 20 sessions | +2.8 / +18.1 / +42.4 bp | −3.4 / −10.4 / −6.2 bp |
| Tilt hit rate, 5 / 10 / 20 sessions | 50% / 50% / 50% | 50% / 50% / 50% |
| Worst rolling 63-session underperformance | −3.7 bp | −2.1 bp |
| Tilt turnover (gross tilt / baseline dollars) | 2.0% | 2.0% |

Out of sample the tilted dollars went into names that then did slightly *worse* over 5 to 20
sessions, and the hit rate is a coin flip. The small positive cost-basis number is consistent
with noise: the tilt moves about 2% of each day's dollars, so a few bp is the most it could move
cost basis even with skill.

### Promotion test: out-of-sample forward residual return by s-score bucket (bp)

Expected shape: falling as s rises (a negative s should be followed by a positive residual).

| Horizon | s < −2 | −2 to −1.25 | −1.25 to 1.25 | 1.25 to 2 | s > 2 | Monotone |
|---|---|---|---|---|---|---|
| 5 | +13.0 (n 181) | +32.6 (1,448) | +19.5 (13,378) | −9.8 (1,401) | +37.2 (224) | no |
| 10 | +16.2 (181) | +29.5 (1,433) | +29.4 (13,290) | −16.4 (1,374) | +14.5 (219) | no |
| 20 | +23.0 (178) | +41.5 (1,407) | +50.9 (13,127) | +5.2 (1,352) | −11.8 (211) | no |

The buckets are identical across variants because the slope changes the multiplier, not the
s-score. There is a hint of reversal in the moderate buckets (−2 to −1.25 above 1.25 to 2 at
every horizon), but both tails break the ordering, and the tails are where the tilt is largest.
**Promotion: failed for all three variants, at every horizon.**

### Overfitting across the three variants (out of sample, 727 sessions)

| | Value |
|---|---|
| Best variant | slope 0.05 (daily active Sharpe 0.011) |
| Deflated Sharpe ratio (3 trials) | 0.60, against the ledger's 0.95 bar |
| PBO (CSCV, 10 blocks) | 0.60 |

A PBO above 0.5 means the in-sample winner is more likely than not to rank below the median out
of sample. With only three trials the DSR still falls well short of 0.95.

## Reading

On this universe and window, residual reversal against a sector ETF does not lower cost basis
beyond noise, its tilt dollars earn nothing out of sample, and its score buckets are not
monotone. The run's limitations (survivor-biased universe, unreviewed sector map, no event guard,
a simplified daily baseline) could move the numbers by a few basis points, which is the size of
everything measured. They do not change the conclusion: there is no evidence here to wire it.

## What was not done

- **Trials not ingested.** Each variant's `trials` row is in the report JSON (`trial_row`), but
  no ledger was written. Ingesting needs the operator's approval, as in
  `analysis/2026-11-24-kalman-phase1/record_trials.py`.
- **The report JSON is not committed** (generated output, as for the Kalman Phase 1 run).
- **No re-run with a reviewed sector map, an 8-K/earnings source, or a dated universe.** Those
  would be the next steps only if there were a reason to revisit the suggester.
