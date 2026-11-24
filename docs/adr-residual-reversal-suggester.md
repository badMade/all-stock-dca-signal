# ADR: Budget-neutral residual-reversal sizing suggester (sector-ETF OU s-score)

- **Status:** Proposed (2026-11-23). Research only. Builds Priorities 2 and 3 of the
  operator-supplied report "Applying Stat-Arb and Pairs-Trading Ideas to all-stock-dca"
  (2026-11-23). It extends the 2026-11-01 operator decision recorded in
  `docs/adr-comovement-signals.md`: use pairs and co-movement ideas as **signals**, never as
  a pairs strategy.
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
- **Backtest harness.** The report's Priority 1 is not built. Before anyone proposes live
  wiring, the suggester must be scored by a walk-forward, cost-basis comparison against the
  current graded baseline. The Phase 0 outcome ledger
  (`docs/adr-outcome-ledger-pit-harness.md`) is the intended place for that. The report's
  promotion test still applies: forward tilt-dollar returns must be monotone in s-score
  bucket out of sample.
- **Intraday top-up threshold.** The report's Priority 5 is not built.
- **Sector mapping.** The ETF for each ticker is supplied by the caller. No mapping is added
  to `config/`.

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
