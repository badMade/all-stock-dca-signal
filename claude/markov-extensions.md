# Markov extensions: two categories

Status: proposed. Research and planning only. Nothing here touches live sizing.
Date: 2026-11-23
Builds on: the 2026-11-21 Markov deep dive (regime detection, intraday tier chain, stress simulation) and the proposed RegimeSuggester ADR.

## Category 1: could change how many dollars get bought per day

Both stay suggest-only. They reach sizing only through `SizingSuggester` / `ForecastProvider` and `SizingAdjustmentReader`, after shadow mode and the evaluation gate.

| # | Type | Problem it fixes | What it suggests | Success metric |
|---|---|---|---|---|
| 1 | MDP cash pacing (dynamic programming over tier, slot, cash left, regime) | Budget is spent slot by slot with no forward view, so a later Tier B/C dip can find the day's cash already gone | A reserve fraction per slot | Average cost basis and terminal value per dollar vs fixed 0%, 25%, 50% reserves, with block-bootstrap CIs |
| 2 | Absorbing tier chain (intraday chain plus terminal states: filled at A, B, C, or day ended unfilled) | Top-up timing is guesswork | P(day ends at Tier B or C), expected slots until a deeper tier, a "wait vs buy now" flag | Brier score vs the Brownian barrier baseline, then dollars saved per top-up in shadow mode |

Dependency: item 1 needs a calibrated item 2. Item 1 stays shadow-only until item 2 passes its Brier check.

### Gate before building either

Opportunity sizing (read-only): count sessions where a later scheduled slot saw price <= prior low while the day's remaining budget could not fund a Tier B buy ($2 base x 2x = $4).
Spec: `dca-sizing-signals/tests/ledger/test_budget_exhaustion_opportunity.py` (test-first; implementation not started). Handoff: `HANDOFF_budget_exhaustion_opportunity.md`.
If the count is near zero, both items drop down the roadmap.

Known limits of the count: the daily cap is a parameter ($180/$240/$300/$360 by tier) because the day's tier is not stored in the ledger; Tier C is not reconstructed (needs the forecast); quotes come from `intraday_quotes`, so coverage starts when that table started filling.

## Category 2: improves decisions made around the system

None of these changes the dollars bought on a given day.

| # | Type | Decision it improves | Output | Success metric | Needs first |
|---|---|---|---|---|---|
| 3 | Migration matrix (ticker performance quintile, month to month) | Which tickers stay active under the 100-ticker cap | Persistence probability per quintile; promotion threshold | Active-list turnover; forward return of promoted vs demoted | Phase 3/4 dynamic-universe ADR |
| 4 | Hidden semi-Markov model (explicit regime durations) | When to trust a regime signal | Expected remaining regime duration at detection | Share of signals with more than 10 trading days left; RegimeSuggester gate decides | RegimeSuggester baseline in shadow mode |
| 5 | Continuous-time chain (rate matrix) | How many parameters the intraday chain needs | One generator matrix for any slot gap | Out-of-sample log loss vs the per-slot discrete chain; parameter stability | Same data as item 2; built as its variant |
| 6 | Markov reward process (regime paths through the real rules) | What the goal odds are | P(hitting the 20% YoY goal) and drawdown spread for the goals GUI | Calibration of predicted P(goal) vs realized years | Regime model plus backtest harness |

## Build order

1. Opportunity-sizing query (gate for category 1)
2. Item 2, absorbing chain
3. Item 5, continuous-time variant (kept only if log loss beats item 2)
4. Item 1, MDP
5. Item 4, HSMM
6. Item 6, reward process
7. Item 3, migration matrix

## Open questions

1. Which daily caps should the opportunity report run at: all four tiers, or only $180?
2. Should the report also count Tier A misses (price < prior close, budget gone), or B-or-deeper only?
3. Where does the report live: a CLI under `dca-sizing-signals`, a console page, or both?
