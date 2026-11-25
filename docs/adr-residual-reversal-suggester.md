# ADR: Budget-neutral residual-reversal sizing suggester (sector-ETF OU s-score)

- **Status:** Tested and failed (2026-11-24). Research only; **nothing gets wired live.**
  Proposed 2026-11-23. Nine walk-forward cost-basis runs (see "Results") found no
  cost-basis improvement distinguishable from zero and no monotone score buckets out of
  sample, so the report's promotion test failed every time. The package stays in the repo as
  research code; reopening it needs a new trial, counted against the nine already recorded.
  Builds Priorities 2 and 3 of the operator-supplied report "Applying Stat-Arb and
  Pairs-Trading Ideas to all-stock-dca" (2026-11-23). It extends the 2026-11-01 operator
  decision recorded in `docs/adr-comovement-signals.md`: use pairs and co-movement ideas as
  **signals**, never as a pairs strategy.
- **Scope:** a new package, `dca-sizing-signals/residual_reversal/`. It publishes bounded,
  unscored suggestions through the existing `SizingSuggester` / `SuggestionPublisher`
  boundary to its own file, `residual_reversal_suggestions.json`. Nothing in the live path
  imports it. `config/`, the live prompts, `sizing_adjustment_reader.py` and all three entry
  points are untouched. Wiring it into a live slot needs its own later ADR.

## Context

Taken from the report, which cites the papers below. I have not independently re-checked
these figures:

- Classic pairs and plain short-term reversal have decayed. Do and Faff (2010) report the
  top-20-pairs return falling to 0.24% a month in 2003 to 2009. Blitz et al. (2024, abstract
  only) say classic reversal "has steadily weakened over time".
- The residual versions held up better. Within-industry residual reversal earned 1.34% a month
  long-short (Da, Liu and Schaumburg 2014, 1982 to 2009, gross); Blitz et al. (2013) report
  0.81% a month net in the largest 100 stocks, long-short.
- Most of the pairs alpha in Gatev, Goetzmann and Rouwenhorst (2006) came from the short
  leg, which this buy-only system cannot hold. Budget-neutral underweighting of residually
  rich names is the closest long-only analog, and it only affects new purchase dollars.
- No source the report found gives a net-of-cost, long-only, large-cap, post-2000 alpha for
  residual losers. Expect basis points of cost-basis improvement at best.

## Decision (proposed)

For each proposed ticker on a slot day, using daily closes dated before the as-of date (or
on it only if the bar is marked final):

1. **Beta.** OLS of the ticker's log returns on its caller-mapped sector ETF over the last
   90 aligned returns, with an intercept.
2. **OU fit.** Cumulate the newest 60 residuals, fit AR(1) `X[k+1] = a + b X[k] + zeta`, then
   `kappa = -ln(b) * 252`, `m = a / (1 - b)`, `sigma_eq = sqrt(var(zeta) / (1 - b^2))`. The OU
   window is shorter than the beta window to avoid the Avellaneda-Lee appendix artifact
   (fitting both on the same days forces the last cumulated residual to zero).
3. **s-score.** `s = (X_now - m) / sigma_eq`. Optional cross-sectional centring of `m` is off
   by default.
4. **Raw tilt.** `1.0` while `|s| < 1.25`. Otherwise `clip(1 - 0.1 * s, 0.75, 1.25)`, so
   cheap names lean up and rich names lean down.
5. **Budget neutral.** One common shift across every unguarded ticker, found by bisection, so
   that `sum(proposed_usd * multiplier) == sum(proposed_usd)`. The publisher re-checks this
   and refuses a batch that adds or removes dollars.

The **relationship-break guard** suppresses a ticker. It publishes no suggestion for that
ticker, not a neutral 1.0, so the reader falls back to the base tier. The guard trips on any
of the following:

| Reason | Trigger |
|---|---|
| `insufficient_history` | fewer than 90 + 20 aligned returns |
| `stale_history` | ticker or ETF newest close more than 4 calendar days old, or the ticker has no return on the ETF's newest session |
| `no_benchmark` | no ETF mapped, or the ETF has no closes |
| `low_r_squared` | beta-regression R² below 0.25 |
| `not_mean_reverting` | AR(1) `b` outside `(0, exp(-1/30))`, which means mean-reversion time is not under half the 60-session window |
| `half_life_jump` | half-life more than 3x the median of the previous 20 days' half-lives |
| `no_half_life_baseline` | fewer than 10 of those 20 days had a tradable fit |
| `corporate_event` | an 8-K Item 1.01, 2.01 or 5.01 (or a caller-tagged `material` event) dated within 90 days, or earnings within 3 calendar days either side. Only events whose `known_at` is on or before the as-of date count |

The whole run **fails closed** (`ok=False`, nothing published) on a loader error, any
malformed close row (future rows included), duplicate rows, bad buys, bad events, bad config,
or a failed budget-neutral self-check.

### Departures from the report

- **Earnings window.** The report says to guard when "an earnings date falls inside the
  window". The 60-session window covers about one earnings cycle by design, so taken
  literally that would guard nearly every name. This build guards only on earnings within ±3
  calendar days of the decision. That is a guess and should be revisited with ledger data.
- **Engle-Granger.** The rolling Engle-Granger check is left out. The report scopes it to
  single-peer variants (its Priority 6), and this build is ETF-only.
- **Unscored.** Like the co-movement rules, the suggester carries `eval_metric_mae = +inf`
  and `scored=False`, and the file says `"mae": null`, so no MAE-gated path can prefer it.

## What this does not do

- **Regime or crowding modulation.** The report's Priority 4 (shrinking the tilt cap in
  turbulent regimes, never flipping its sign) belongs in `SizingAdjustmentReader`, the one
  component allowed to combine suggesters. This package stays regime-blind.
- **Backtest harness.** Built since as `dca-sizing-signals/cost_basis_backtest/`
  (`docs/adr-cost-basis-backtest-harness.md`); its results are below.
- **Intraday top-up threshold.** The report's Priority 5 is not built.
- **Sector mapping.** The ETF for each ticker is supplied by the caller. No mapping is added
  to `config/`.

## Results (2026-11-24)

Every run used `ResidualReversalConfig` defaults unless named, the binding in
`dca-sizing-signals/tests/cost_basis_backtest/_binding.py`, and the cost-basis harness. All nine
were accepted: no lookahead flags, budget-neutral every session, and in the six runs that
checked it a zero-signal replay matched the baseline exactly (0.0 bp). Cost-basis improvement is
the paired per-ticker mean, out of sample; positive means the suggester paid less.

| Run | Baseline | Benchmark | Costs | OOS window | Cost-basis bp (t) | Terminal wealth bp | Promotion |
|---|---|---|---|---|---|---|---|
| default (slope 0.1) | graded-ladder replay | draft sector map | 0 bp | 2024-01-02 on | +1.43 (1.03) | +0.6 | failed: buckets not monotone |
| slope 0.05 | graded-ladder replay | draft sector map | 0 bp | 2024-01-02 on | +0.73 (1.05) | +0.3 | failed: not monotone |
| slope 0.2 | graded-ladder replay | draft sector map | 0 bp | 2024-01-02 on | +2.06 (1.02) | −0.4 | failed: not monotone |
| proxy_qqq | flat $1 | QQQ | 3 bp | 2024-01-02 on | +1.0 (0.89) | −1.8 | failed: not monotone |
| proxy_vt | flat $1 | VT | 3 bp | 2024-01-02 on | +0.8 (0.56) | −5.2 | failed: not monotone |
| proxy_sector | flat $1 | sector map, SPY fallback | 3 bp | 2024-01-02 on | +1.0 (0.82) | −1.1 | failed: not monotone |
| live_qqq | 515 live scheduled fills | QQQ | 3 bp | 2026-11-02 on | −0.2 (−0.17) | 0.0 | failed: buckets under 30 points |
| live_vt | 515 live scheduled fills | VT | 3 bp | 2026-11-02 on | +0.5 (0.70) | −0.1 | failed: under 30 points |
| live_sector | 515 live scheduled fills | sector map, SPY fallback | 3 bp | 2026-11-02 on | +0.04 (0.05) | −0.8 | failed: under 30 points |

- **Graded-ladder runs** (`analysis/2026-11-24-residual-reversal-cost-basis/`, write-up in
  `docs/research/2026-11-24-residual-reversal-cost-basis-results.md`): 1,480 sessions,
  2021-01-04 to 2026-11-23, 75,359 replayed 3:30 PM ladder buys ($277,028). Out of sample the
  tilt dollars returned −3 to −10 bp over 5 to 20 sessions and the hit rate was 50%. Across the
  three variants the deflated Sharpe ratio was 0.60 (bar 0.95) and PBO 0.60.
- **Proxy runs:** the same 1,480 sessions with $1 per active ticker per session, Adj Close.
  Hit rate 50 to 51% at every horizon. Over the whole proxy_sector period the 20-session bucket
  means ran +58, +45, +10, −8, −5.5 bp (nearly monotone), but out of sample they were −4, +24,
  +31, +17, +31: the in-sample shape did not hold.
- **Live runs:** the 528 scheduled fills from 2026-10-05 to 2026-11-24 ($1,838), of which 515
  had archive prices, over 20 sessions. Too short to say anything: every bucket is under 30
  points.

**Limits of the evidence**, none of which turns a failure into a pass:

- **Survivor-biased universe:** every run uses today's active ticker list.
- **Baselines are proxies.** The graded-ladder replay is daily and leaves out the 60-minute
  trend filter, budget caps, the other six slots and no-buy windows. The flat-$1 proxy is not the
  graded ladder at all.
- **Hand-made sector maps:** two slightly different ones, neither reviewed
  (`analysis/2026-11-24-residual-reversal-cost-basis/sector_map_draft.json`, and a variant that
  sends funds and unclassified names to SPY).
- **Only 7 weeks of live fills**, too few for a test.
- **No corporate-event guard:** no offline point-in-time 8-K or earnings source was supplied.

All nine runs are trials of one experiment, `residual_reversal_cost_basis`, in the Phase 0
ledger's `trials` table, so a retry's deflated Sharpe starts from `trial_count = 9`.

## Verification

- `dca-sizing-signals/tests/residual_reversal/`: math (OLS, OU recovery of a known `b`,
  budget-neutral bounds and totals), direction (a seeded −3σ residual shock leans up, +3σ
  leans down), no lookahead (future rows and a non-final as-of bar change nothing), every
  guard reason, the 8-K point-in-time rule (a filing dated after the as-of date never reaches
  back), every fail-closed path, and the publisher's own-file-only, bounds, neutrality and
  stale-as-of refusals.
- `tests/residual_reversal/test_boundary.py` and `tests/test_architecture_boundaries.py`:
  stdlib plus `dca_shared_interfaces` only, no network modules, no sibling or core imports in
  either direction, and no repo-root module imports it.

## Rollback

Revert the PR and delete `residual_reversal_suggestions.json` wherever a caller wrote it.
Nothing in the live path reads it.
