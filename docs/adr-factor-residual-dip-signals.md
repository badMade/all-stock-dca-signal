# ADR: Factor-residual dip signals (APT decomposition as suggest-only signals)

- **Status:** Proposed (2026-11-23). Nothing here is built. Open questions carry proposed
  defaults; they are not operator decisions.
- **Scope:** research-only, suggest-only components in `dca-sizing-signals/`, publishing through
  the existing `SizingSuggester` / `SuggestionPublisher` boundary like `comovement_signals/`.
  `config/`, the live prompts, `near_close_dca.py` and all three entry points are untouched.
  Wiring any component into a live slot needs its own later ADR.
- **Source research:** "APT and Multi-Factor Ideas for all-stock-dca" deep-research report
  (pasted by the operator, 2026-11-23). Its author could not read this repository, so its
  recommendations were mapped against the code here before this ADR was written (see
  "What already exists"). Paper findings are cited as that report gives them; **none were
  re-verified for this ADR.** The two numbers this ADR leans on (Collin-Dufresne and Daniel's
  ~10% temporary share of an idiosyncratic shock, half-life ~2.5 days, and Ang and Chen's
  downside-correlation asymmetry) should be spot-checked against the primary papers before any
  live-wiring ADR cites them. Collin-Dufresne and Daniel is an unpublished working paper.

## Context

The graded ladder upgrades a ticker to 2x/3x when its price is at or below the prior close or
prior low. That treats every dip the same, whatever caused it. The report's argument, in one line:
split each dip into the part the market and sector explain and the stock-specific remainder,
because (as reported) the remainder is what partially reverses, while market/sector dips show
short-term momentum and are one correlated bet placed many times.

Reported evidence, with the report's own caveats:

- Residual (factor-adjusted) reversal earns about twice the risk-adjusted return of raw reversal
  (Blitz, Huij, Lansdorp and Verbeek 2013); only the non-fundamental residual component of
  reversal is significant (Da, Liu and Schaumburg 2014).
- In the 100 largest US stocks, 1972-2014, about 10% of an idiosyncratic daily shock is
  temporary, half-life about 2.5 days (Collin-Dufresne and Daniel, working paper 2016). A 3%
  residual dip therefore implies roughly 30 bp of expected recovery on the incremental dollars,
  before slippage. **Small, a tiebreaker not an engine.**
- Raw short-term reversal has largely vanished (Blitz, van der Grient and Honarvar 2024), and
  news-driven dips drift instead of reversing (Chan 2003).
- Downside correlations exceed what a normal model implies (Ang and Chen 2002), most for small,
  value and loser stocks.

Because this system buys anyway, the dip ladder only shifts when and how much of a planned buy
happens; round-trip reversal-strategy costs do not apply. The benefit is still bounded by the
small temporary component above. The expected value is mainly **risk** (less stacking of
market-driven escalations), secondarily a few bp per incremental dollar.

## What already exists (the report's ranks 1-3 are mostly built)

| Report recommendation | Already in this repo | Gap this ADR fills |
|---|---|---|
| #1 Validation ADR: pre-registration, walk-forward, trial counting, deflated Sharpe, survivorship flags, decay haircut | `docs/adr-outcome-ledger-pit-harness.md` (Accepted 2026-11-03): `trials` table, deflated Sharpe >= 0.95 over every registered trial after a 50% haircut, t+5/t+20 horizons, flat-1x baseline, survivor-biased labels, `ASOF JOIN` outcomes. Implemented in `market_data_ingest/ledger/store.py` | None structural. Each component below registers its variants in `trials` |
| #2/#3 Concentration guard, correlated-dip dampener | `docs/adr-comovement-signals.md` signals 1 and 2: `ClusterCapSuggester` (35% cluster cap) and `CorrelatedSelloffSuggester` (60% of a 3+ cluster down 2% shares one escalation budget) | Clusters come from return **correlation**, not explicit factor betas, and the selloff trigger is a raw-return count. Neither tells a stock-specific dip from a market-driven one on the same day |
| #4 News/earnings exclusion | `market_data_ingest/snapshot.py` carries `days_until_next_earnings`; `edgar_fundamentals.py` reads EDGAR | No flag any suggester consumes; no 8-K filing-timestamp reader |
| #7 Factor scorecard | `ticker-advisor/` scores candidates; EDGAR PIT facts exist | Out of scope here (report: low value for a few dozen names) |
| #8 Macro/VIX logging | Not present | Logging-only fields, below |

## Decision (proposed)

| # | Component | Interface | Status |
|---|---|---|---|
| F1 | **Factor betas** (`factor_signals/betas.py`): per ticker, daily OLS of returns on SPY and the sector ETF's return orthogonalized to SPY, trailing 252 sessions through the prior close, Blume-shrunk (`0.23 + 0.77 * beta`), plus residual daily volatility and R-squared | Pure stdlib function over caller-supplied close rows, same row contract as `comovement_signals.returns_matrix` (final, non-interpolated, dated on or before `as_of`) | Build first |
| F2 | **Factor exposure report**: dollar-weighted portfolio beta, sector weights, mean R-squared, and the same for the hypothetical "every eligible slot hits 2x/3x" spend | Read-only JSON report, not a suggester | Build with F1 |
| F3 | **Factor-share dampener** (`FactorShareDampenerSuggester`): at a slot, `factor_move = b_m * SPY move + b_s * orthogonal sector move` since prior close; if more than the threshold share of a ticker's dip is factor-driven, suggest removing the escalation above 1x | `SizingSuggester`; publishes only `factor_share_dampener_suggestions.json` | Shadow experiment |
| F4 | **Event flag** (`event_flags.py`): earnings date or a fresh 8-K (EDGAR filing timestamp, point-in-time) in the prior or current session | Flag function consumed by F3/F5; it never publishes a suggestion of its own | Shadow experiment |
| F5 | **Residual dip z-score** (`ResidualDipSuggester`): `z = residual / (sigma_idio * sqrt(elapsed session fraction))`; suggests keeping an upgrade only when the raw tier already qualifies **and** z clears the tier's cutoff and F4 is clear | `SizingSuggester`; publishes only `residual_dip_suggestions.json` | Shadow experiment, lowest confidence |
| F6 | **Regime fields**: VIX level, scheduled CPI/jobs/FOMC day flag, 2s10s and high-yield spread change | Columns on the F3/F5 shadow rows only | Log only; no sizing use without a later ADR |

Rules shared by all six (same as the co-movement ADR, plus three specific to factors):

1. **Point-in-time.** Betas use daily data through the prior close only; the slot applies them to
   intraday moves. No intraday-bar beta estimation (microstructure noise).
2. **Additive and fail closed.** The raw "at or below prior close/low" rule stays the eligibility
   gate. A missing or stale beta, ETF quote or sector mapping publishes nothing and the existing
   tier stands. **A component may only lower a multiplier above 1x; none may reduce the 1x base
   buy or skip a slot.**
3. **No yfinance in live slots.** Live ETF quotes come from the Robinhood/Alpaca read-only path
   (`docs/yfinance_replacement_plan.md`). Kenneth French library data is research-only: it lags by
   weeks and is restated retroactively; any research use pins the dated archive vintage.
4. **Sector mapping is versioned.** SIC from `sec_company_map.py` as of the decision date, mapped
   to a sector ETF through a committed, versioned table so a reclassification cannot leak backward.
5. **Same-day top-ups** measure the residual since the earlier reference slot, not since the prior
   close, so a later slot never re-buys the same residual.
6. **Every variant is a trial.** Window, shrinkage, regressor set, thresholds and z cutoffs are
   frozen before the first backtest; any later change is a new row in `trials`.

## A gap this exposes in `sizing_adjustment_reader.py`

`load_combined_adjustment(combine="most_conservative")` picks the suggestion with the **smallest
absolute** adjustment (`min(values, key=abs)`). For a dampener that is the wrong rule: a -50%
dampener and a +20% Octopus upgrade combine to **+20%**, the less conservative of the two for a
dip buyer. The report's suggested resolution is "the lowest multiplier wins for any multiplier
above 1x." That change is on the live-DCA side of the boundary and needs its own review; it is
open question 6 here, not part of this ADR's build.

## Open questions

| # | Question | Proposed default | Why |
|---|---|---|---|
| 1 | Objective | Lower average cost per share of the above-1x dollars vs the flat-1x baseline, at t+5 and t+20 (the Phase 0 metric) | Matches the decision the system actually makes; not a long-short Sharpe |
| 2 | Factor set | Market plus orthogonalized sector only, frozen. Size/value/momentum/rates ETFs are a later trial | Each regressor adds noise on 252 days and a trial |
| 3 | F3 threshold | Factor share of the dip > 70% removes the escalation above 1x | Report's starting point; calibrate in backtest |
| 4 | F5 cutoffs | 2x needs z <= -1.5; 3x needs z <= -2 plus Octopus agreement plus F4 clear | Report's starting point; calibrate in backtest |
| 5 | Overlap with `CorrelatedSelloffSuggester` | Run both in shadow; score F3 against signal 2 head to head in the ledger before either is proposed live | They target the same risk by different measures |
| 6 | Reader combine rule | Separate ADR: for multipliers above 1x, the lowest suggested multiplier wins | See the reader gap above |
| 7 | Shadow period | At least 6 months, and the Phase 0 promotion bar | Report and Phase 0 agree |
| 8 | Small-cap applicability | Score large and small names separately; do not assume the large-cap reversal magnitude transfers | Strongest daily evidence is the 100 largest stocks |

## Not in scope

- APT long-short "arbitrage" (needs shorting; this system is buy-only).
- Estimating factor premia (Fama-MacBeth) on the hand-picked universe: far too few names.
- A macro-regime sizing suggester (no demonstrated daily predictive basis; F6 only logs).
- Options IV as a directional signal. IV may later replace `sigma_idio` in F5's denominator as a
  registered trial.

## Tests (written first, failing by design)

1. F1 never uses a return dated after `as_of`, a non-final bar, or an interpolated row.
2. F1 shrinkage, orthogonalization and residual volatility match a hand-computed fixture.
3. A missing, stale or malformed beta, ETF quote or sector mapping makes F3 and F5 publish
   nothing (`ok=False` with a reason).
4. No component ever suggests a multiplier below 1x or a skipped slot.
5. F3 removes the escalation when the factor share exceeds the threshold and leaves it otherwise.
6. F5 suggests nothing when F4 flags the ticker.
7. A same-day top-up measures the residual from the earlier reference slot.
8. `tests/test_architecture_boundaries.py` covers the new package (no `near_close_dca`,
   `ml_forecaster`, `wondrous_octopus` or sibling-suggester imports).

## Consequences

- A visible portfolio beta and sector exposure, including the worst-case all-escalate spend.
- Fewer escalations stacked on market-driven dips, if the shadow scoring supports it.
- No new return claim. Any return effect is expected to be a few bp per incremental dollar.

## Rollback

Revert the PRs and delete the suggestion and report files. Nothing in the live path reads them.
