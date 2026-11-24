# ADR: DCA backtest harness and statistics layer (backtest protocol items 7 to 17)

- **Status:** Proposed (2026-11-23), revised the same day by Amendment 1. Not accepted; nothing is
  implemented. Open questions carry proposed defaults that are not operator decisions.
- **Scope:** research only. The stdlib-only core, the ladder, `config/`, and all three entry points
  (`run_dca.run_slot()`, the `MorningDCAExecutor` hierarchy, `dca_runner.py`) are untouched. Nothing here
  writes live sizing, publishes a suggestion to a live slot, or places orders.
- **Inputs:**
  - [`backtest-gap-audit.md`](backtest-gap-audit.md) (same branch), which rates items 7 to 17 as 0
    present, 6 partial and 5 missing.
  - [`research/2026-11-23-backtest-overfitting.md`](research/2026-11-23-backtest-overfitting.md), the
    operator-supplied overfitting report behind Amendment 1. Its claims are unverified in this session;
    see its sourcing note.
  - It builds on [`adr-outcome-ledger-pit-harness.md`](adr-outcome-ledger-pit-harness.md) (Accepted
    2026-11-03) and [`adr-price-source-massive-alpaca.md`](adr-price-source-massive-alpaca.md).
- **Not covered:** items 1 to 6 (the data layer). They are guards and columns on the existing stores.
  Step 3 of the kickoff writes their failing tests first. This ADR assumes those guards exist and reads
  only through them.

## Context

The repo has no DCA backtest that could support a promotion decision:

- `all_stock_dca/backtest.py` fills at the signal's own close, charges no cost and has no cash account.
  It emits no benchmark and substitutes default quantiles when a record lacks them (audit items 6, 7,
  8 and 9).
- `docs/backtest_curve.md` reports an average-cost headline ("discount vs prior close") over 60 sessions,
  with same-bar proxy fills (items 7 and 10).
- The ledger already has a `trials` table, a trial-count macro and a recorded
  `PROMOTION_DSR_THRESHOLD` of 0.95. Nothing computes a deflated Sharpe or writes a trial (items 12
  and 13).
- The point-in-time price store (`adjusted_bars(as_of_date, known_at)`) and EDGAR first-filed facts are
  the right inputs, but no backtest reads them (audit item 2).

The question this harness must answer is narrow: **does a sizing or timing rule leave the investor with
more money than plain DCA, on the same prices and the same contributions, by more than chance and the
number of variants tried explain?**

The prior is that it does not. The report cites AQR's 196 buy-the-dip variants, Hansen-SPA work on
dip-buying versus DCA, and Hayley's result that buy-more-when-down rules look good on IRR and average
cost only through hindsight bias. All are unverified here. The live 2x tier, the proposed tier C, the
seven-slot schedule and the Phase 3/4 selection engines are each a search over many rules. With 1,000
zero-skill rules, the expected best Sharpe is about 3.26 standard errors, which is roughly 1.46 annualized
on 5 years of daily data (rechecked in session). So the harness must count trials and deflate for them,
or a good result means nothing.

## Decision (proposed)

### A. Harness (items 7 to 10)

1. **Location and engine.** A new package `dca-sizing-signals/backtest/` beside `market_data_ingest/`.
   It reads DuckDB only through the existing macros (`adjusted_bars`, `universe_members_as_of`,
   `market_sessions` and first-filed facts). No core module ever imports it, and
   `dca-sizing-signals/tests/test_architecture_boundaries.py` gains a rule to enforce that.
2. **Event loop, one session at a time.** For each session `t` on the one backtest calendar (item 4's
   decision), the harness:
   - reads inputs with `known_at` set to the decision timestamp of `t`'s slot,
   - asks the rule for a decision,
   - then fills it.
3. **Fills (item 7).** A decision at `t` fills at the next tradable price strictly after the decision
   timestamp:
   - the next session's open, for a close-time decision;
   - the next intraday print, for an intraday decision where `intraday_quotes` covers it;
   - otherwise the next open.

   A fill on the bar that produced the signal is a hard error, not a warning. Cost is
   `notional * (half_spread_bps + slippage_bps) / 1e4 + commission`, charged in cash. Robinhood charges
   no commission, so `commission` defaults to 0 but stays a parameter.
4. **Cash account (item 8).**
   - One cash ledger per run. Each period's contribution arrives on its scheduled date, taken from the
     live caps in `config/budgets.json` and read once into the run manifest.
   - Idle cash, including any reserve held back to fund multipliers, accrues the 3-month T-bill yield
     each session from a dated, first-release series (open question 2).
   - A buy that would take cash below zero is refused and logged, never financed.
   - Fractional shares are allowed, matching live.
5. **Benchmarks on identical paths (item 9).** Every run executes three legs over the same path id: a
   hash of the symbol set, session range, `known_at` policy and price rows.
   - **Rule:** the candidate rule.
   - **Plain DCA:** the same cumulative contributions on the same dates, spent at a flat 1x on the same
     slots. This extends the existing `baseline_fills` definition from live scoring to the backtest.
   - **Lump sum:** each contribution invested in full at its first fill opportunity.

   A run that cannot produce all three legs is invalid and writes no result.
6. **Metrics (item 10).** There is one headline metric plus two daily series that the statistics run
   on. The headline answers the question; the series give it a valid test.
   - **Headline: paired terminal wealth.** For each path, the rule's wealth minus plain DCA's wealth at
     the horizon end, with uninvested cash at its accrued value. The report shows the distribution
     across paths (sub-periods, bootstrap resamples, tickers) first.
   - **M2, tilt return versus plain DCA (sizing rules: tiers, universe weighting, bandits).** The daily
     P&L of the difference portfolio, `sum_i (H_rule[i,t] - H_naive[i,t]) * r[i,t+1] +
     (C_rule[t] - C_naive[t]) * rf[t]`, where H is shares held and C is uninvested cash. It is
     self-financing, so it has a valid t-stat and Sharpe. Its compounded sum is the terminal-wealth
     difference, so the headline and M2 cannot disagree in sign.
   - **M1, same-dollar execution improvement in bps (timing rules: slots, top-up, the 10:30 trend
     filter).** `(P_ref - P_fill) / P_ref * 1e4`, dollar-weighted over the same dollars. `P_ref` is what
     a naive rule pays for those dollars that day: all at 09:35, or the TWAP of the seven slots. Both
     references are reported.
   - Each series is aggregated to one dollar-weighted observation per trading day, because same-day
     observations across tickers are correlated.
   - IRR, average cost per share and "discount vs prior close" may appear only as labelled secondary
     figures. They are biased toward buy-more-when-down rules.

### B. Statistics layer (items 11 to 17)

7. **Validation scheme (item 11).**
   - **Parameter-free rules (the 2x tier, slot rules):** expanding walk-forward, re-anchored monthly. The
     report shows every fold, not only the average.
   - **Fitted models (Octopus, `ml-forecaster`, meta-labeling):** purged k-fold with an embargo of at
     least the label horizon in sessions (López de Prado 2018, ch. 7), reusing the idea behind
     `wondrous-octopus/horizons.py`'s horizon embargo. Any training row whose label window overlaps the
     test fold is purged.
   - **Model selection, and the matrix PBO needs:** combinatorial purged CV (CPCV, ch. 12).
   - Any scaler, quantile or calibration is fit inside the training fold only. The fold object exposes
     training rows to `fit()`, evaluation rows to `predict()`, and nothing else.
8. **Trial registry, fail closed (item 12).**
   - **Write first.** The harness writes the trial row with status `started` *before* it reads any
     price. It refuses to run if that write fails, and closes the row as `completed`, `failed` or
     `abandoned`. A run that dies leaves a `started` row, and that row still counts toward N.
   - **Schema.** The existing `trials` table (`ledger/store.py:106-112`) lacks most of the fields
     deflation needs. Because the ledger is append-only, a companion table `trial_meta` keyed on
     `trial_id` adds them: `family_id`, `hypothesis_id`, `component`, `author`, `rationale`,
     `code_commit`, `data_vintage_hash`, `universe_snapshot_id`, `start`, `end`, `metric_name` (M1 or
     M2), `daily_series_path`, `t_stat`, `touched_holdout`, `status` and `is_backfill`.
   - **What counts.** Parameter sweeps, notebook runs and every cell of a grid each count as one trial.
     N per family comes from `trial_counts` (extended to group by `family_id`), never from the caller.
   - **Backfill** (open question 10). Variants already tried by hand on tiers, slots and Octopus are
     entered once with `is_backfill = true` and an approximate count, so N does not start at zero
     dishonestly.
9. **Overfitting statistics (item 13).** All of these run on the daily M1 or M2 series, with plain DCA
   (M = 0) as the benchmark.
   - **Deflated Sharpe Ratio** (Bailey and López de Prado 2014), on the non-annualized Sharpe at daily
     frequency. It uses skew, raw kurtosis and an expected-max hurdle built from the variance of every
     registered trial's Sharpe in the family.
     - **Effective N:** `N_eff = rho_bar + (1 - rho_bar) * N`, where `rho_bar` is the mean pairwise
       correlation of the trials' daily series (open question 11).
     - When the family is too small to estimate the variance, fall back to Lo's (2002) analytic
       variance `(1 + SR^2 / 2) / (T - 1)`.
     - The bar stays the recorded 0.95.
   - **PBO via CSCV** (Bailey, Borwein, López de Prado and Zhu 2017) on the T x K matrix of the family,
     with S = 16 blocks by default (12,870 splits). The report also shows the out-of-sample degradation
     slope and `P(OOS < 0)` for the in-sample winner. The bar is **PBO < 0.2**: pure noise scores about
     0.47 to 0.48, so a bar near 0.5 would pass noise.
   - **Hansen's SPA** (2005), with the consistent p-value as primary and White's Reality Check (2000)
     beside it. **Romano-Wolf StepM** names which variants survive, with familywise error control.
   - **Benjamini-Hochberg FDR** is for screening very large families only (pairs, per-ticker), and only
     into shadow mode, never into promotion.
   - **Stationary bootstrap** (Politis and Romano 1994), with block length from Politis and White
     (2004) as corrected by Patton, Politis and White (2009). It drives the confidence intervals and
     SPA/StepM resampling. For the bootstrap of price paths, joint daily returns across the universe are
     resampled in blocks so cross-correlation is kept.
   - **Cross-checks, reported but not gating:** the Harvey and Liu (2015) haircut via
     Bonferroni/Holm/BHY, and a MinBTL check of trials tried against the history length.
10. **Robustness reports (item 14).** Each candidate gets five tables. A candidate is flagged
    **fragile** if its M2 or M1 mean is negative in any regime label, or not positive in a majority of
    them, or if its paired terminal-wealth median changes sign anywhere in the neighborhood table or at
    2x cost.
    - **Regimes, labelled ahead of time by a rule** and stored point in time: terciles of the trailing
      60-day S&P 500 return and realized volatility, plus named windows (the Feb to Mar 2020 crash, the
      2022 bear, the Apr 2025 selloff, the 2023 to 2024 bull). Each regime gets a mean and t-stat.
    - **Sub-periods** by calendar year.
    - **Parameter neighborhood:** each tuned parameter at plus and minus one grid step, in all
      combinations.
    - **Cost multipliers** of 0x, 1x, 2x and 3x on spread and slippage.
    - **Stress paths:** 2000 to 2002 and 2008 replays from the archive (research use of yfinance is
      allowed by `docs/yfinance_replacement_plan.md`), synthetic slow-grind declines (for example -30%
      over 12 months at low daily volatility), and large-overnight-gap days for slot rules.
11. **Sealed holdout (item 15).**
    - The most recent span plus all future shadow data is sealed (open question 4).
    - Reads go through one function. It requires a frozen, pre-registered `hypothesis_id` and a reason,
      and it appends a `holdout_access` row (who, when, reason, `hypothesis_id`, `trial_id`, manifest
      hash) *before* it returns any data.
    - Each access counts toward that family's N. After one access, the window counts as burned for
      that hypothesis.
    - The harness refuses to tune on any session inside the window.
    - Because the recent past has very likely been seen already, the window starts burned and is
      re-frozen forward (open question 12).
12. **Shadow log and pre-registration (item 16).**
    - **Pre-registration first.** Before a hypothesis enters shadow, one immutable `hypotheses` row
      records:
      - `hypothesis_id`, economic rationale, and the exact rule and parameters;
      - the metric (M1 or M2), the baseline, the primary test and alpha, and the family size K;
      - the minimum days, the stopping rule, the regimes that must be non-negative, and the decision
        for each outcome.
    - **Power calculation.** The minimum days come from
      `n = (z(1 - alpha/K) + z(1 - beta))^2 * sigma_d^2 / delta^2 * DE`, where DE is the design effect
      from autocorrelation (`1 + 2 * sum(rho_k)`). sigma_d is measured from the Phase 0 ledger, never
      assumed. For scale, 30 bps daily noise and a 3 bps effect need about 618 sessions at K = 1, and
      about 1,168 at K = 10.
    - **Interim looks** are fixed in advance with alpha spending, or not taken at all.
    - **The shadow log itself.** Each suggestion is one append-only row: `suggestion_id`,
      `hypothesis_id`, `config_hash`, `symbol`, `slot`, `decided_at`, `known_at`, `input_hash` and
      `suggested_multiplier`. `input_hash` is the sha256 of the canonical JSON of every input the rule
      read. Rows follow the ledger's storage conventions (`content_hash`, `as_of`, `ingested_at` and
      `run_id`).
    - There is no write path to `config/` or to any slot.
13. **Reproducibility (item 17).**
    - Each run writes a manifest recording `known_at`, the calendar version, the universe snapshot id,
      the rule `config_hash`, the seed, the git commit, the `requirements.txt` hash, and the DuckDB file
      hash or snapshot id.
    - Every random draw derives from the manifest seed (the `derive_seed` pattern in
      `wondrous-octopus/harness.py`).
    - Re-running a manifest must reproduce byte-identical result rows.
14. **Forecast gate before forecast-driven sizing.** A rule that sizes on a forecast, such as tier C
    on Octopus, is not scored on M2 until the forecast passes two checks on purged CV and on the
    holdout:
    - (a) Kupiec unconditional coverage and Christoffersen conditional coverage at the 10, 50 and 90%
      quantiles;
    - (b) the median forecast beats a random-walk forecast under Diebold-Mariano or SPA.

    This stops an unvalidated forecast from being stacked on an unvalidated sizing rule.
15. **Promotion rule.** A candidate moves to **shadow** only if all of these hold:
    - SPA consistent p < 0.05;
    - DSR >= 0.95;
    - PBO < 0.2;
    - not flagged fragile.

    A candidate moves to **live** only after its pre-registered shadow test passes. Even then it is a
    separate change, reviewed with its own acceptance run (CLAUDE.md, "Consolidating is a decision").
    This ADR never moves anything to live.

## Dependencies

**No new runtime dependency is proposed for the gating path.**

- `duckdb==2.0.0` is already pinned in `dca-sizing-signals/requirements.txt` and holds every table.
- The gating statistics use the standard library:
  - `statistics.NormalDist` for the DSR's normal CDF and its inverse;
  - `math.comb` and `itertools.combinations` for CSCV;
  - `random.Random(seed)` for the stationary bootstrap.

  This needs measuring (see Verification). At this data size (about 90 tickers, 10 years, a few hundred
  trials) plain Python may be fast enough, but CSCV's 12,870 splits are the risk.
- **Correctness cross-checks, not gating.** The stdlib implementations are checked against outside
  references in a one-off script kept outside CI: `arch` >= 9.0 (SPA, StepM, bootstraps) and R's `pbo`
  (PBO). Its versions and the arch 8.0 bug come from the report and are unverified.
- **If runtime fails the bar,** numpy is the first addition considered, then `arch`. Either one gets its
  own ADR amendment.
- **Rejected:**
  - `mlfinlab`: closed source, according to the report;
  - `pypbo`: dormant; at about 100 lines it is cheaper to reimplement and test;
  - `skfolio`: pulls in the scientific stack just to split CPCV folds, which is about 50 lines;
  - `vectorbt` and `backtrader`: they have their own data models, and neither enforces `known_at`.

## Alternatives considered

| Option | Why not chosen |
|---|---|
| Extend `all_stock_dca/backtest.py` | It is built around Octopus prediction records and same-bar fills, with SQLite output. Meeting items 7 to 10 would mean rewriting it. Left as it is (not dead code, per CLAUDE.md). |
| Put the harness in `wondrous-octopus/` | That couples the DCA harness to one forecaster, when the harness must judge every suggester, Octopus included. |
| Simple train/test split instead of walk-forward | One split gives one number. Walk-forward gives the distribution of paths the headline metric needs. |
| IRR or average cost as headline | Both are biased toward buy-more-when-down rules (Hayley, via the report). |
| Sharpe of the rule alone | It measures the rule against cash, not against plain DCA, and plain DCA is the decision being made. M2's Sharpe is the right one. |
| Bonferroni on trial count instead of DSR | Too conservative across correlated variants. DSR with an effective N handles correlation; Bonferroni stays as a cross-check. |
| Holdout alone, no trial counting | Reusing a holdout turns it back into in-sample data. The report puts it as false positives becoming "expected" after about 20 uses. |

## Trade-offs

- **The stdlib statistics are slower and need their own tests.** Each formula gets a test against a
  published worked example and a property test before it scores anything. Pure noise should give PBO
  near 0.5 and DSR near 0.5, and a planted Sharpe of 3 should give PBO near 0.
- **Next-open fills understate a close-time rule's real fill quality** for the 15:30 slot, which fills
  near the close in live trading. This is accepted: the bias runs against the rule, and the
  cost-multiplier report bounds it.
- **The sealed holdout costs the most recent data.** Tuning gets fewer sessions in exchange for one
  unspoiled out-of-sample read per hypothesis.
- **Three legs per run triples compute.** This is accepted, because the headline metric does not exist
  without benchmarks on identical paths.
- **Honest shadow periods are long.** Single-digit-bps effects may need 2 to 5 years, and some may be
  unmeasurable at this account's scale. That would be a legitimate conclusion, not a failure of the
  harness.
- **The universe is still survivor-biased** until audit item 5's membership source lands. Every result
  carries the `survivorship_biased` flag until then, and the robustness report states it first.

## Open questions

| # | Question | Proposed default | Why |
|---|---|---|---|
| 1 | Spread and slippage values | Half-spread of 5 bps plus 5 bps slippage for the active large-cap universe. Replace with values measured from `intraday_quotes` once 60 sessions exist | Conservative for large caps; measured beats assumed |
| 2 | T-bill series source | FRED `DTB3` (3-month secondary market, daily), first-release vintage via ALFRED, stored with `as_of` in DuckDB | Official, free and dated; matches `overnight_futures/fair_value.py`'s choice. Terms and access unverified this session |
| 3 | Label horizon and embargo | t+5 and t+20 sessions, as in the outcome-ledger ADR; embargo of 20 sessions | The embargo must cover the longest horizon scored |
| 4 | Holdout window | The most recent 12 months plus all future shadow data | Matches the report; leaves a long training history |
| 5 | Bootstrap block length | Politis-White automatic choice, with 5, 10 and 20 sessions shown for sensitivity | Data-driven, with the sensitivity visible |
| 6 | Promotion rule | Decision 15: SPA p < 0.05, DSR >= 0.95, PBO < 0.2, not fragile | Each test covers a different failure, so all must pass |
| 7 | Who may read the holdout | Only a run with a frozen `hypothesis_id` and a `started` trial row, once per hypothesis | Ties every peek to a counted trial |
| 8 | CI | Harness tests join the `dca-sizing-signals` CI job. Long statistical and property tests are marked and run nightly | Keeps PR CI fast |
| 9 | Location of the Step 3 tests | `dca-sizing-signals/tests/` (CLAUDE.md convention), not root `tests/` | The code under test lives in that sibling |
| 10 | Backfilling past trials | One `is_backfill` row per remembered family (tiers, slots, Octopus, cap curves such as `docs/backtest_curve.md`'s five), with an approximate count the operator supplies | N must not start at zero |
| 11 | Effective N method | `rho_bar` approximation first; ONC clustering only if a family grows past about 50 trials | Simple first; clustering when it matters |
| 12 | Holdout already seen? | Assume yes. Start the sealed window at the ADR's acceptance date and grow it forward | Past data was looked at while the ladder was built |
| 13 | Live 2x tier while it is unproven | **Out of scope here.** The report suggests reverting live sizing to 1x until H-2X passes. That is a live change for the operator to decide, with its own review | This ADR is research only |

## Consequences

- A sizing or timing change (the 2x tier, tier C, inverse martingale, the ML suggester, cap curves,
  slot curves) gets one comparable, deflated score against plain DCA, plus an honest trial count,
  instead of a one-off table.
- Results in the style of `docs/backtest_curve.md` would no longer count as promotion evidence on their
  own.
- New append-only tables in the ledger file: `trial_meta`, `hypotheses`, `holdout_access`,
  `shadow_suggestions`, `regime_labels`, `run_manifests` and `backtest_results`. All follow the existing
  conventions.
- Every Phase 3/4 engine (universe selection, bandits, meta-labeling, pairs screening) has to emit
  trial rows automatically. 100 tickers make 4,950 pairs, so pairs screening is the largest implicit
  search in the roadmap.
- The Friday review gains a fixed panel showing N per family, the best DSR, PBO, the holdout access
  count, and each hypothesis's shadow progress against its required days. That review is the
  outcome-ledger ADR's open question 6; this ADR adds the panel's content, not the wiring.
- Nothing in the live path changes.

## Verification (when implemented)

- A synthetic path where the rule equals plain DCA gives a paired terminal-wealth difference and an M2
  of exactly 0 on every path, at every cost multiplier.
- A rule given lookahead (it sees `t+1`'s close) is caught: the same-bar or future-read guard raises.
- DSR, PBO and SPA each reproduce a published worked example within a stated tolerance.
- Property tests on pure noise give PBO between 0.4 and 0.6 and DSR near 0.5; a planted Sharpe of 3
  gives PBO below 0.05.
- A harness run whose trial-row write fails reads no price and writes no result.
- Two runs of one manifest give byte-identical `backtest_results` rows.
- A holdout read without a frozen `hypothesis_id`, or a second read for one hypothesis, is refused and
  logged.
- Timing: 91 tickers over 10 years, three legs, 200 trials and PBO with S = 16 run on the Mac Studio in
  under 10 minutes with the stdlib implementation.

## Amendment 1 (2026-11-23): overfitting report

Folded in from [`research/2026-11-23-backtest-overfitting.md`](research/2026-11-23-backtest-overfitting.md):

- M1 and M2 added under the headline metric (Decision 6).
- Fail-closed, write-first trial registry with `trial_meta` and backfill (Decision 8).
- Effective N, Lo's fallback, StepM, the Harvey-Liu and MinBTL cross-checks, and PBO's bar tightened
  from 0.5 to 0.2 (Decision 9).
- Regime labels and stress paths (Decision 10).
- A holdout tied to frozen hypotheses and counted in N (Decision 11).
- Pre-registration and power-based shadow length (Decision 12).
- The forecast coverage gate (Decision 14).
- Open questions 10 to 13.

Not adopted: the report's numpy/scipy/arch reference code as the implementation. The stdlib stays the
default and those libraries become cross-checks. Its live-sizing recommendations are out of scope (open
question 13).

## Sourcing

- **Methods** are cited from the published literature by name and were not re-fetched this session:
  - Bailey and López de Prado (2014), the deflated Sharpe ratio;
  - Bailey, Borwein, López de Prado and Zhu (2014, 2017), minimum backtest length and the probability of
    backtest overfitting;
  - White (2000), the Reality Check; Hansen (2005), SPA; Romano and Wolf (2005), StepM;
  - Lo (2002), on the statistics of Sharpe ratios;
  - Harvey and Liu (2015), on Sharpe ratio haircuts;
  - Politis and Romano (1994) and Politis and White (2004), on the stationary bootstrap and its block
    length;
  - Kupiec (1995) and Christoffersen (1998), on coverage tests;
  - López de Prado (2018), *Advances in Financial Machine Learning*.
- **Before implementing,** each formula is checked against its paper, not against this summary or the
  report.
- **The report's empirical claims** (AQR, Hayley, the Utah State paper, PatternsWizard, Price Action
  Lab) and its library versions are unverified.
- **FRED/ALFRED** availability and terms are unverified.
- **Repository claims** come from [`backtest-gap-audit.md`](backtest-gap-audit.md), at that file's
  confidence levels.

## Rollback

Revert the PR. Nothing in the live path reads the harness or its tables.
