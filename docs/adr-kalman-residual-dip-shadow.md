# ADR: Kalman residual-dip score as a shadow-only signal

- **Status:** Accepted (2026-11-23, operator "Accept, build Phase 1"; every open question takes its
  proposed default). Acceptance authorizes Phase 1 below and nothing else. Not wired into any live
  path.
- **Scope:** research and shadow logging only, inside `dca-sizing-signals/`. The stdlib-only core,
  the ladder, `config/`, the live prompts, `sizing_adjustment_reader.py` and all three entry points
  (`run_dca.run_slot()`, the `MorningDCAExecutor` hierarchy, `dca_runner.py`) are untouched. Nothing
  here writes live sizing, moves a slot or places orders.
- **Source research:** `docs/research/2026-11-23-kalman-filters.md` ("the research"). References such
  as "F5" or "research 2" point into that file. Its citations have not been re-verified (see its
  sourcing disclosure).

## Context

- The ladder sizes a buy from how far the price sits below its reference ("current vs last close").
  It cannot tell a stock-specific dip from a market-wide or cluster-wide one.
- The research's strongest claim is that a residual (market-adjusted) reversal is roughly twice as
  strong as a raw one (F5, Blitz et al. 2013; Da, Liu and Schaumburg 2014). That reversal shows up in
  intraday moves, not overnight gaps (F6).
- A Kalman filter with state (alpha, beta) estimates a time-varying beta and gives a standardized
  residual innovation, `z_resid`. Kalman betas forecast at least as well as rolling OLS in most of the
  cited comparisons (F3), though not in all of them.
- A single-series "Kalman fair value" reduces to an EMA in steady state (research 2, F1, F2) and is
  **not** proposed here.
- Evaluating on average cost or IRR flatters any dip-weighted rule (F8). Evaluation must be on
  budget-matched dollars and forward residual returns.

### What the research assumed, checked against the code

| Research assumption | What the code shows (2026-11-23) | Consequence for this ADR |
| --- | --- | --- |
| "Implement your SizingSuggester interface" in shadow mode | In this repo, shadow-only work is deliberately **not** a `SizingSuggester`: co-movement signals 3 to 5 return `ShadowResult` records, and `overnight_futures` is barred from any sizing path by `tests/test_architecture_boundaries.py` | Shadow-only functions and their own JSONL file, no `suggest()`. A `SizingSuggester` wrapper needs a later ADR after the D7 gate |
| Regress on SPY and the stock's sector ETF (e.g. SMH, XLK) | SPY, SMH and XLK are not in `config/tickers.json` `active` (122 symbols). There is no ticker-to-sector map in `config/` | Market factor = SPY returns supplied by the caller. Sector factor = the caller-supplied mean return of the ticker's `comovement_signals` cluster, so no new sector map is needed (D3) |
| Universe is single stocks | `active` includes ETFs and funds (for example QQQ, VT, XLG, AAAU) | Those get `not_applicable`, not a score (D6) |
| numpy in the sketch | `dca-sizing-signals` packages are stdlib-only (`dependencies = []`) | A 2-state filter is a few lines of plain floats; no new dependency |
| Earnings dates from the EDGAR store | `market_data_ingest/snapshot.py` already carries `days_until_next_earnings` as schedule knowledge | The caller passes an earnings-window flag; this package does not read the store (D3) |
| Count the Q/R grid as trials | The Phase 0 ledger has a `trials` table (`market_data_ingest/ledger/store.py`) | Every Q/R setting tried is recorded there (D7) |
| Live intraday SPY quotes at each slot | Not wired anywhere; whether the live executors can quote SPY each slot is unverified | Phase 1 is offline only. Live shadow logging (Phase 2) waits until that is checked |

## Decision

### D1. Purpose: classify the dip, do not size it

Each scored (ticker, session) gets `z_resid` and one label: `idiosyncratic_dip`, `cluster_selloff`,
`market_selloff` or `none`. The label is meant to inform cluster-cap allocation later. It is never a
brake on total deployment (research recommendation 5: under-buying a broad crash is the main risk).

### D2. Interface: shadow functions only

- `kalman_signals.beta_filter`: a pure `beta_step(state, P, r_stock, r_factor, Q, R)` returning the
  **prior** innovation (computed before the update, research 5) and the updated state.
- `kalman_signals.residual_dip`: `residual_dip_shadow(close_rows, factor_rows, as_of, config)`
  returning a `ShadowResult`-shaped value with a record per scored ticker and a reason for every
  ticker it skipped.
- No `suggest()`, no `publish()`, no import of `SizingSuggester`, `SizingSuggestion` or
  `SuggestionPublisher`. Same AST test as `test_overnight_futures_has_no_sizing_path`.

### D3. Package layout and boundaries

New package `dca-sizing-signals/kalman_signals/`, added to `[tool.setuptools.packages.find]` and to
`FORBIDDEN_IMPORTS` both ways. It may import only `dca_shared_interfaces`. All prices, factor returns
and earnings flags arrive as rows from the caller, as in `comovement_signals`. Nothing on the live side
imports it, including `sizing_adjustment_reader.py`.

### D4. Model

- Daily close-to-close log returns. One filter per (ticker, factor) with state (alpha, beta),
  random-walk transition.
- Beta is estimated through the prior close only. The session's residual uses that frozen beta.
- Residual split into an overnight part (prior close to open) and an intraday part (open to
  evaluation price) when an open is supplied. Only the intraday part feeds the label (F6).
- Two factors are run as two separate univariate filters (SPY, cluster mean), not one 3-state
  filter, so each can be checked against its own rolling-OLS baseline.

### D5. Tuning, point in time

- Q and R per ticker are fit on a trailing window that ends the day before the evaluation window,
  refit monthly, frozen in between. Each record carries the parameter version.
- Default fitting is a small grid over q = Q/R (3 to 5 values) scored by one-step predictive
  log-likelihood on the training window only. No smoother output is ever used as a signal.
- The first 60 sessions after any (re)initialization are burn-in and get no score.
- A 60-session rolling OLS beta is logged next to the Kalman beta as a sanity bound and baseline.

### D6. Fail closed

No record (with an explicit reason) for: missing or stale factor data (same 4-calendar-day rule as
`comovement_signals`), burn-in, beta variance above a cap, a non-finite value, a ticker that is an
ETF or fund, or an earnings flag within one trading day. A malformed row fails the whole run.

### D7. Evaluation gate before any later wiring

Run on walk-forward history first. Do not propose wiring unless all of these hold:

1. Out-of-sample standardized innovations: variance 0.9 to 1.1, and 3% to 7% beyond ±1.96, checked by
   volatility regime and around earnings.
2. Market- and cluster-adjusted forward 5-day returns rise monotonically as `z_resid` buckets get more
   negative, with date-clustered standard errors.
3. At least 1,300 events per compared group and at least 6 months of calendar time (research 6).
4. Every Q/R setting and threshold tried is in the ledger's `trials` table, and the result survives
   that trial count.
5. Budget-matched terminal value, not average cost, is the decision metric.

### D8. Storage

One append-only JSONL file, `kalman_residual_dip_shadow.jsonl`, under a directory the caller picks,
with the same rules as `comovement_signals.ShadowWriter`: refuse a `config` path component, refuse
any other file name, `live_effect: "none"`, SHA-256 content hash, no duplicate lines, refuse a record
whose inputs are dated after its `as_of`.

## Alternatives considered

- **Univariate Kalman "fair value" in place of last close.** Rejected: it is an EMA (research 2).
  Logging a local-level innovation z as a dip-depth feature is research recommendation 2; it can be a
  later, separate ADR, named as an EWMA z-score.
- **Rolling OLS beta only.** Cheaper and a fair baseline; logged alongside (D5) so the comparison is
  measured rather than assumed.
- **Sector ETFs plus a new sector map.** More moving parts (a new config file, more quotes, its own
  invariant test) than reusing the existing clusters. Can be revisited if clusters prove too coarse.
- **Pairs or hedge-ratio trading.** Out of scope: needs shorting and loses to costs (F4).
- **Kalman log-vol filter for an inverse-vol suggester** (research recommendation 3). Deferred to its
  own ADR together with that suggester.

## Consequences

- One new research package and one shadow file; no live behavior changes.
- Phase 1 needs daily history for SPY, which is not in `active`. It is a research-only input and does
  not change the universe.
- Results take months to mean anything. Until D7 passes this is a logging cost with no payoff.

## Open questions (resolved 2026-11-23 to the defaults in brackets)

1. Market factor: SPY, or a universe-weighted mean return? [SPY]
2. Label thresholds: `idiosyncratic_dip` when intraday `z_resid <= -2` and the factor move is above
   -1 sigma of its own history? [yes, as a starting point, recorded as trials]
3. Phase 2 slot: log at the 3:30 PM canonical slot only, or every slot? [3:30 PM only, to start]
4. Where Phase 1 history comes from: the existing `ticker_data` archive plus a one-off SPY history
   file? [yes]

## Implementation plan (one draft PR per phase)

1. **Phase 1 (offline):** `kalman_signals/` package, boundary test entries, filter math with unit
   tests (steady-state gain matches the research's table, prior-vs-posterior innovation, missing-day
   predict-only step), `residual_dip_shadow()` and its writer, and a walk-forward backtest script
   that writes D7's diagnostics to a research report. No live inputs.
2. **Phase 2 (live shadow):** only after Phase 1's report is reviewed and live SPY quotes at the
   chosen slot are confirmed. A prompt change that calls `residual_dip_shadow()` and writes the shadow
   file. Still no sizing effect.
3. **Later ADR:** any `SizingSuggester` wrapper, only if D7 passes.

## Verification

- `python -m pytest dca-sizing-signals/tests -q` and
  `ruff check --isolated --select=E,F,B --line-length=120 dca-sizing-signals`.
- The boundary test fails if `kalman_signals` grows a sizing path or is imported from the live side.

## Implementation status (2026-11-23)

Phase 1 code is in `dca-sizing-signals/kalman_signals/` (`kalman_filter.py`, `residual_dip.py`,
`shadow.py`, `backtest.py`, `backtest_cli.py`), with tests in `dca-sizing-signals/tests/kalman/` and
boundary rules in `tests/test_architecture_boundaries.py`. Two deviations from the plan above:

- The `trials` table is not written automatically: `kalman_signals` may not import
  `market_data_ingest` (D3), so the report lists the q grid under `trials` for the operator to record.
- The first walk-forward run used VT as the market factor, because the archive has no SPY. It failed
  D7 (z variance 1.30). Results and next steps: `docs/research/2026-11-23-kalman-phase1-results.md`.
- Second round (2026-11-24): SPY via `--market-file`, point-in-time clusters, and earnings windows
  via `--earnings`. The gate in `backtest.report` now also checks calibration within each volatility
  regime (D7.1); round 1 checked only the pooled figure. With earnings days excluded the pooled
  variance is 1.05, but the high-volatility regime (1.14), strict monotonicity and the
  1,300-event floor still fail, and D7.2's cluster-adjusted returns, D7.4 and D7.5 are not yet computed.
  Trial rows for `market_data_ingest`'s ledger are built by `analysis/2026-11-24-kalman-phase1/record_trials.py`.
