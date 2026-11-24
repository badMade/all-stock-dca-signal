# ADR: Offline walk-forward cost-basis backtest harness

- **Status:** Proposed (2026-11-23). Research only. Builds Priority 1 of the operator-supplied
  report "Applying Stat-Arb and Pairs-Trading Ideas to all-stock-dca" (2026-11-23), which
  `docs/adr-residual-reversal-suggester.md` makes a precondition for any live wiring of that
  suggester.
- **Scope:** a new package, `dca-sizing-signals/cost_basis_backtest/`. It is stdlib only,
  makes no network or broker calls, writes no files, and nothing in the live path imports it.
  `config/`, the live prompts, `sizing_adjustment_reader.py` and all three entry points are
  untouched.

## Context

The residual-reversal suggester is unscored (`eval_metric_mae = +inf`). The question it has to
answer is not forecast error but whether moving the marginal dollar between names lowers what
the account pays per share. The report expects a few basis points of cost-basis improvement at
best, which is small enough to be lost in harness arithmetic or found by luck across variants.

### Does the Phase 0 outcome ledger already cover this?

No, but it supplies two of the pieces, and the harness reuses them instead of copying them.

| Ledger piece (`docs/adr-outcome-ledger-pit-harness.md`) | Covers this? |
|---|---|
| `fills` (scheduled live graded fills) | Yes, as the baseline's cash flows. `baseline_from_ledger_fills` reads those rows as plain dicts and drops manual runs |
| `baseline_fills` (flat 1x counterfactual, open question 4) | No. That is a flat baseline, not the graded one this task compares against |
| `outcomes` (each fill's price at t+5 and t+20) | No. It scores fills the live system made. It cannot replay a counterfactual sizing rule, and it computes no cost basis |
| `trials` (trial registry for deflated Sharpe) | Yes. `trial_row` and `rejected_trial_row` shape each run for `LedgerStore.ingest_trials`, so `trial_count(experiment=...)` counts every variant ever tried |

I extended the ledger at its seams rather than its schema. DuckDB is a third-party dependency,
and the task asks for a stdlib package. A replay is also a pure function of its inputs, so it
needs no store of its own. If the operator later wants per-run replay rows in DuckDB, that is
one new append-only table in `market_data_ingest/ledger/store.py` fed from
`BacktestResult.fills`. It is not built here.

## Decision (proposed)

### Replay

For each decision session t in the baseline's cash flows (`{session: {ticker: usd}}`):

1. The candidate gets a `PointInTimeView` with closes dated before t and events whose
   `known_at` is before t. An 8-K enters on its SEC `filed_date` (`CorporateEvent.known_at`),
   so one filed on t is first visible to the decision for t+1.
2. It returns `CandidateDecision(multipliers, scores, hedges)`. A ticker it leaves out keeps
   1.0, which is the same fallback `SizingAdjustmentReader` gives a ticker with no suggestion.
3. Both arms fill at the close of t. The baseline buys `usd` and the candidate buys
   `usd * multiplier`. Both pay the same one-way buy cost (`buy_cost_bps`, plus an optional
   `per_order_cost_usd`). Nothing is sold, so there are no sell costs.

The decision sees data through the t-1 close only, but the fill is at the t close. That is
stricter than the live 3:30 PM slot, which sees a near-close quote for t. It can only
understate a signal.

`SuggesterCandidate` adapts any `SizingSuggester`-shaped object by duck typing (`evaluate()`
for a whole day, otherwise `suggest()` per ticker), so the harness imports no suggester. The
residual-reversal binding is about 20 lines in `tests/cost_basis_backtest/_binding.py`, and the
README has a copy of it.

### Metrics

Every period is reported three ways: the whole run, before `oos_start`, and from it.

| Metric | Definition |
|---|---|
| **Primary: paired per-ticker cost-basis improvement** | per ticker, `(cb_baseline - cb_candidate) / cb_baseline` in bp, where cost basis = dollars (costs included) / shares. Reported as mean, median, baseline-dollar-weighted mean, share improved, and a cross-ticker t-stat |
| Terminal wealth per dollar | value at the last close of the shares bought, / dollars spent, per arm, and the difference in bp |
| Forward return of the tilt dollars | `d` = candidate minus baseline dollars. `sum(d * R) / sum(abs(d))` at 5, 10 and 20 sessions and to the end, net of the buy cost |
| Tilt hit rate | share of tilts whose sign matches the ticker's forward return minus that session's equal-weight mean |
| Worst rolling 3-month underperformance | worst 63-session `prod(1 + r_c) / prod(1 + r_b) - 1` of the two arms' daily time-weighted returns |
| Tilt turnover | `sum(abs(d)) / sum(baseline dollars)`, and the mean day-to-day change in a ticker's multiplier |
| Forward residual return by score bucket | `ln(P'/P) - beta * ln(B'/B)` over 5, 10 and 20 sessions, using the candidate's own benchmark and beta, bucketed at s = -2, -1.25, 1.25, 2 |

**Promotion test (necessary, not sufficient):** out of sample, the bucket means must be
strictly monotone in the expected direction (falling as s rises) at every horizon, and every
bucket needs at least 30 points. With too few points the result is "too thin", not a pass.
Nothing reads `promotion.passed` to change sizing.

### Guards

| Guard | Outcome |
|---|---|
| The candidate asks the view for a date on or after t, even if it catches the `LookaheadError` | the run is **rejected**, with no metrics |
| Malformed decision (an unproposed ticker, a non-finite multiplier, or one outside [0, 4]) | **rejected** |
| Malformed closes, cash flows or events | `BacktestInputError` before any decision |
| A peek around the view (a closure over the full data) | **flagged**: mean daily Spearman IC of `multiplier - 1` against the return into the fill or the next session's return is at least 0.3 over at least 20 sessions. Promotion fails |
| Candidate exception or `ok=False` on a session | that session replays at 1.0 and is counted under `unavailable_sessions` |
| Universe not from a dated membership table | `survivor_biased: true`, as the Phase 0 ADR requires |

The IC canary is a heuristic, not proof. A genuine daily signal's mean IC is a few hundredths,
and an oracle's is near 1. The threshold sits far from both.

### Overfitting across variants

`compare_variants` aligns the variants' daily active-return series and reports:

- **Deflated Sharpe ratio** (Bailey and Lopez de Prado 2014) of the best variant. `n_trials`
  should come from the ledger's `trial_count(experiment=...)`, and it is never fewer than the
  number of variants passed in. The ledger records the bar, 0.95, as a field. Nothing here
  enforces it.
- **PBO** (Bailey, Borwein, Lopez de Prado and Zhu 2014): CSCV over 10 blocks, which gives
  252 splits.

A rejected variant cannot be compared, but `rejected_trial_row` still records it as a trial.

I took both formulas from the papers' published statements and have not re-derived them. The
tests check only limiting behaviour: one trial gives the PSR against zero, more trials deflate
more, pure noise gives a PBO near 0.5, and a dominant variant gives a PBO near 0.

## What this does not do

- **Replay the graded ladder itself.** The baseline's dollars come from the caller, either
  from the ledger's `fills` or from a ladder replay run outside this package, because no
  package here may import `near_close_dca`. A replay script that calls the ladder point in time
  over the ticker archive is a follow-up.
- **Run on real data.** No sector-ETF mapping exists in `config/`, and none is added. The tests
  use seeded synthetic data with real OU reversal, which checks the plumbing and says nothing
  about the strategy.
- **Model fills beyond close price plus a proportional cost.** Robinhood's $1 fractional
  minimum and intraday slot prices are not modelled.
- **Correct for overlapping forward returns.** Daily decisions with 20-session horizons overlap,
  so only bucket means and the cross-ticker cost-basis t-stat are reported. No per-observation
  t-stats are given.

## Verification

`dca-sizing-signals/tests/cost_basis_backtest/` (52 tests, about 12 s):

- `test_zero_signal.py`: a candidate that tilts nothing, a suggester that publishes 0.0 for
  every ticker, and one that always raises all reproduce the baseline cost basis **exactly**
  (`==`, not approximately), with and without costs.
- `test_lookahead.py`: a suggester that asks the view for tomorrow's close is rejected, and so
  is one that swallows the error. One that peeks through a closure is flagged and cannot
  promote. The honest residual-reversal binding trips nothing, and rewriting every close after
  session k leaves every decision through k+1 bit-identical.
- `test_metrics.py`: hand-computed cost basis, costs, tilt return, hit rate, turnover, residual
  hedge, buckets, rolling worst, and every fail-closed input.
- `test_overfitting.py`, `test_ledger_io.py` (including a round trip through
  `LedgerStore.ingest_trials`), `test_residual_reversal_replay.py` (8-K filed-date timing,
  budget neutrality, and that every metric is reported).
- `test_boundary.py` and `tests/test_architecture_boundaries.py`: stdlib only, no DuckDB, no
  file writes, no suggester or core imports, and the two-way rule (no sibling package or
  repo-root module imports it).

## Rollback

Revert the PR. Nothing reads its output, and it writes no files.
