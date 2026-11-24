# ADR: DCA backtest harness and statistics layer (backtest protocol items 7 to 17)

- **Status:** Proposed (2026-11-23). Not accepted; nothing is implemented. Open questions carry proposed
  defaults that are not operator decisions.
- **Scope:** research only. The stdlib-only core, the ladder, `config/`, and all three entry points
  (`run_dca.run_slot()`, the `MorningDCAExecutor` hierarchy, `dca_runner.py`) are untouched. Nothing here
  writes live sizing, publishes a suggestion to a live slot, or places orders.
- **Inputs:** [`backtest-gap-audit.md`](backtest-gap-audit.md) (same branch), which rates items 7 to 17
  as 0 present, 6 partial, 5 missing for this layer. Builds on
  [`adr-outcome-ledger-pit-harness.md`](adr-outcome-ledger-pit-harness.md) (Accepted 2026-11-03) and
  [`adr-price-source-massive-alpaca.md`](adr-price-source-massive-alpaca.md).
- **Not covered:** items 1 to 6 (the data layer). They are guards and columns on the existing stores, and
  Step 3 of the kickoff writes their failing tests first. This ADR assumes those guards exist and reads
  only through them.

## Context

The repo has no DCA backtest that could support a promotion decision:

- `all_stock_dca/backtest.py` fills at the signal's own close, charges no cost, has no cash account,
  emits no benchmark and substitutes default quantiles when a record lacks them (audit items 6, 7, 8, 9).
- `docs/backtest_curve.md` reports an average-cost headline ("discount vs prior close") over 60 sessions,
  with same-bar proxy fills (items 7, 10).
- The ledger already has a `trials` table, a trial-count macro and a recorded `PROMOTION_DSR_THRESHOLD`
  of 0.95, but nothing computes a deflated Sharpe or writes a trial (items 12, 13).
- The point-in-time price store (`adjusted_bars(as_of_date, known_at)`) and EDGAR first-filed facts are
  the right inputs, but no backtest reads them (audit item 2).

The question this harness must answer is narrow: **does a sizing rule leave the investor with more
money than plain DCA on the same prices and the same budget, by more than chance and trial count
explain?** Everything below serves that question.

## Decision (proposed)

### A. Harness (items 7 to 10)

1. **Location and engine.** A new package `dca-sizing-signals/backtest/` beside `market_data_ingest/`,
   reading DuckDB through the existing macros only (`adjusted_bars`, `universe_members_as_of`,
   `market_sessions`, first-filed facts). Never imported by a core module;
   `dca-sizing-signals/tests/test_architecture_boundaries.py` gains a rule for it.
2. **Event loop, one session at a time.** For each session `t` on the one backtest calendar (item 4's
   decision): read inputs with `known_at` = the decision timestamp of `t`'s slot, ask the rule for a
   decision, then fill.
3. **Fills (item 7).** A decision at `t` fills at the next tradable price strictly after the decision
   timestamp: the next session's open for a close-time decision, the next intraday print for an
   intraday decision where `intraday_quotes` covers it, otherwise the next open. A fill on the bar that
   produced the signal is a hard error, not a warning. Cost is
   `notional * (half_spread_bps + slippage_bps) / 1e4 + commission`, charged in cash. Robinhood charges
   no commission, so `commission` defaults to 0 but stays a parameter.
4. **Cash account (item 8).** One cash ledger per run. Each period's budget arrives on its scheduled
   date (the live weekly and daily caps from `config/budgets.json`, read once into the run manifest).
   Idle cash accrues the 3-month T-bill yield each session, using a dated series (open question 2), first-release
   values only. A buy that would take cash below zero is refused and logged, never financed. Fractional
   shares are allowed, matching live.
5. **Benchmarks on identical paths (item 9).** Every run executes three legs over the same path id
   (a hash of symbol set, session range, `known_at` policy and price rows):
   - **Rule:** the candidate sizing rule.
   - **Plain DCA:** the same budget arrivals spent at a flat 1x on the same slots (the existing
     `baseline_fills` definition, extended from live scoring to the backtest).
   - **Lump sum:** each budget arrival invested in full at its first fill opportunity.
   A run that cannot produce all three legs is invalid and writes no result.
6. **Primary metric (item 10).** The paired terminal-wealth difference: for each path, rule wealth
   minus plain-DCA wealth at the horizon end, with uninvested cash at its accrued value. The report
   shows its distribution across paths (sub-periods, bootstrap resamples, tickers) as the headline.
   IRR, average cost and "discount vs prior close" may appear, labelled secondary, and never as the
   first number.

### B. Statistics layer (items 11 to 17)

7. **Validation scheme (item 11).** Anchored walk-forward by default: fit or calibrate on
   `[start, t)`, evaluate on `[t, t + step)`, roll. Where a rule has tuned parameters and a k-fold view
   is wanted, purged k-fold CV with an embargo of at least the label horizon in sessions
   (López de Prado 2018, ch. 7), reusing the idea of `wondrous-octopus/horizons.py`'s horizon embargo.
   Any scaler, quantile or calibration is fit inside the training fold only; the fold object exposes
   training rows to `fit()` and evaluation rows to `predict()` and nothing else.
8. **Trial ledger (item 12).** Every harness run writes one `trials` row through the existing
   `LedgerStore.ingest_trials` (`config_hash` = hash of rule parameters plus the run manifest). Failed,
   abandoned and re-run variants count. The trial count used by item 13 comes from `trial_counts`, never
   from the caller.
9. **Overfitting statistics (item 13), computed on the paired series (rule minus plain DCA):**
   - **Deflated Sharpe Ratio** (Bailey and López de Prado 2014) with N from `trial_counts`, plus skew
     and kurtosis of the paired returns. Promotion bar stays the recorded 0.95.
   - **PBO via CSCV** (Bailey, Borwein, López de Prado and Zhu 2017) across all trials of an
     experiment, with S = 16 blocks by default.
   - **Hansen's SPA** (2005) with plain DCA as the benchmark and every trial of the experiment as a
     competitor; White's Reality Check (2000) reported beside it.
   - **Block bootstrap:** stationary bootstrap (Politis and Romano 1994) of the paired series for
     confidence intervals and as SPA's resampling engine. Mean block length is an open question.
10. **Robustness reports (item 14).** Each candidate gets three tables: (a) sub-periods (calendar
    years and named regimes), (b) a parameter neighborhood (each tuned parameter at plus and minus
    one grid step, all combinations), (c) cost multipliers of 0x, 1x, 2x and 3x on spread and slippage.
    A candidate whose paired terminal-wealth median changes sign anywhere in (b) or at 2x cost is
    flagged fragile.
11. **Sealed holdout (item 15).** The most recent span (open question 4) is sealed. Reads go through
    one function that requires a reason string, appends a row to a `holdout_access` table (who, when,
    reason, trial id, manifest hash) before returning data, and refuses a second read for the same
    `config_hash`. The harness refuses to tune on any session inside the window.
12. **Shadow log (item 16).** When a rule runs forward in suggest-only mode, each suggestion is one
    append-only row: `suggestion_id, rule_id, config_hash, symbol, slot, decided_at, input_hash,
    suggested_multiplier, known_at`, where `input_hash` is the sha256 of the canonical JSON of every
    input the rule read. Same storage conventions as the ledger (`content_hash`, `as_of`,
    `ingested_at`, `run_id`). No write path to `config/` or to any slot.
13. **Reproducibility (item 17).** Each run writes a manifest: `known_at`, calendar version, universe
    query, rule `config_hash`, seed, git commit, `requirements.txt` hash, DuckDB file hash or snapshot id.
    Every random draw derives from the manifest seed (the `derive_seed` pattern in
    `wondrous-octopus/harness.py`). Re-running a manifest must reproduce byte-identical result rows.

## Dependencies

**No new dependency is proposed.**

- `duckdb==2.0.0` is already pinned in `dca-sizing-signals/requirements.txt` and holds every table.
- The statistics use the standard library: `statistics.NormalDist` for the DSR's normal CDF and
  inverse, `math.comb` and `itertools.combinations` for CSCV, `random.Random(seed)` for the stationary
  bootstrap. At this data size (about 90 tickers by 10 years by a few hundred trials) loops in Python
  are fast enough. This needs measuring (Verification, last bullet).
- **Alternatives rejected:** numpy (already in `wondrous-octopus`, but not in `dca-sizing-signals`; adds
  a heavy dependency to a package that does not need it yet); `arch` (has SPA and bootstraps, but pulls
  in numpy, scipy and pandas); `mlfinlab` (licence and maintenance uncertain); `vectorbt` and
  `backtrader` (vectorised or event engines with their own data model; neither enforces `known_at`,
  so the point-in-time rules would have to be re-imposed from outside).
- If measured runtime fails the bar, numpy is the first addition considered, and it would get its own ADR
  amendment.

## Alternatives considered

| Option | Why not chosen |
|---|---|
| Extend `all_stock_dca/backtest.py` | Built around Octopus prediction records and same-bar fills; SQLite output; would need rewriting to meet items 7 to 10. Left as is (not dead code per CLAUDE.md). |
| Put the harness in `wondrous-octopus/` | Couples the DCA harness to one forecaster; the harness must judge every suggester, Octopus included. |
| Simple train/test split instead of walk-forward | One split gives one number; walk-forward gives the path distribution the primary metric needs. |
| IRR as headline | IRR rewards timing of cash flows that DCA does not control and can rank a rule that deploys less cash above one that ends richer. |
| Sharpe of the rule alone | Measures the rule against cash, not against plain DCA, which is the decision being made. |
| Bonferroni on trial count instead of DSR | Too conservative across correlated variants; DSR uses the trial count with the variance of trial Sharpes. |

## Trade-offs

- **Stdlib statistics are slower and need their own tests.** Each formula gets a test against a
  published worked example before it scores anything.
- **Next-open fills understate a close-time rule's real fill quality** for the 15:30 slot, which fills
  near the close live. Accepted: the bias is against the rule, and the cost-multiplier report bounds it.
- **A sealed holdout costs the most recent data.** Fewer sessions for tuning, in exchange for one
  unspoiled out-of-sample read per candidate.
- **Three legs per run triples compute.** Accepted: without identical-path benchmarks the primary
  metric does not exist.
- **The universe is still survivor-biased** until audit item 5's membership source lands. Every result
  carries the `survivorship_biased` flag until then, and the robustness report states it first.

## Open questions

| # | Question | Proposed default | Why |
|---|---|---|---|
| 1 | Spread and slippage values | Half-spread 5 bps plus 5 bps slippage for the active large-cap universe, to be replaced by measured values from `intraday_quotes` once 60 sessions exist | Conservative for large caps; measured beats assumed |
| 2 | T-bill series source | FRED `DTB3` (3-month secondary market, daily), first-release vintage via ALFRED, stored with `as_of` in DuckDB | Official, free, dated; matches `overnight_futures/fair_value.py`'s choice. Terms and access unverified this session |
| 3 | Label horizon and embargo | t+5 and t+20 sessions (as in the outcome-ledger ADR); embargo = 20 sessions | Embargo must cover the longest horizon scored |
| 4 | Holdout window | The most recent 12 months of sessions, rolling forward only when the operator re-seals it | Enough sessions for a paired estimate; short enough to leave a long training history |
| 5 | SPA bootstrap mean block length | 10 sessions, with 5 and 20 in the robustness report | Roughly two trading weeks; sensitivity shown rather than chosen once |
| 6 | Promotion rule | DSR >= 0.95 **and** PBO < 0.5 **and** SPA p < 0.05 **and** not flagged fragile | Each test covers a different failure; all four must pass |
| 7 | Who may read the holdout | Only a run whose `trials` row is already written, once per `config_hash` | Ties every peek to a counted trial |
| 8 | CI | `dca-sizing-signals` has its own CI job; harness tests join it. Long statistical tests are marked and run nightly | Keeps PR CI fast |
| 9 | Location of the Step 3 tests | `dca-sizing-signals/tests/` (CLAUDE.md convention) rather than root `tests/` | The code under test lives in that sibling |

## Consequences

- A sizing change (inverse martingale, ML suggester, cap curves) gets one comparable score against
  plain DCA and an honest trial count, instead of a one-off table.
- `docs/backtest_curve.md`-style results would no longer be accepted as promotion evidence on their own.
- New tables in the ledger file: `holdout_access`, `shadow_suggestions`, `run_manifests`,
  `backtest_results`, all append-only under the existing conventions.
- Nothing in the live path changes. Promotion of any rule to live sizing stays a separate, reviewed
  change with its own acceptance run (CLAUDE.md, "Consolidating is a decision").

## Verification (when implemented)

- A synthetic path where the rule equals plain DCA gives a paired terminal-wealth difference of exactly
  0 on every path, at every cost multiplier.
- A rule given lookahead (it sees `t+1`'s close) is caught: the same-bar or future-read guard raises.
- DSR, PBO and SPA each reproduce a published worked example within a stated tolerance.
- Two runs of one manifest give byte-identical `backtest_results` rows.
- A holdout read without a reason, or a second read for one `config_hash`, is refused and logged.
- Timing: 91 tickers by 10 years, three legs, 200 trials, PBO with S = 16, on the Mac Studio, in under
  10 minutes with the stdlib implementation.

## Sourcing

The methods are cited from the published literature from memory and were not re-fetched this session:
Bailey and López de Prado (2014), "The Deflated Sharpe Ratio"; Bailey, Borwein, López de Prado and Zhu
(2017), "The Probability of Backtest Overfitting"; White (2000), "A Reality Check for Data Snooping";
Hansen (2005), "A Test for Superior Predictive Ability"; Politis and Romano (1994), "The Stationary
Bootstrap"; López de Prado (2018), *Advances in Financial Machine Learning*. Before implementing, each
formula is checked against the paper, not this summary. FRED/ALFRED availability and terms are unverified.
Repository claims come from [`backtest-gap-audit.md`](backtest-gap-audit.md), with that file's confidence
levels.

## Rollback

Revert the PR. The harness and its tables are read by nothing in the live path.
