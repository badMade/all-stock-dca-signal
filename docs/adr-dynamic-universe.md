# ADR: Dynamic, rules-based universe capped at 100 active tickers

- **Status:** Proposed (2026-11-01). Open questions carry labeled proposed defaults. Research
  and shadow work may proceed on them; executor wiring needs its own later ADR.
- **Scope:** a research-only scorer and selection rule (`ticker-advisor/`), plus append-only
  DuckDB tables beside the Phase 0 ledger (`docs/adr-outcome-ledger-pit-harness.md`). The
  stdlib-only core, `config/tickers.json`, `near_close_dca.ACTIVE_TICKERS`, the live prompts and
  all three entry points are untouched.
- **Source research:** "Dynamic 100-Ticker Universe for a Buy-Only DCA System" (claude.ai research
  report, 2026-11-01), and "Self improving DCA strategy options" (2026-11-01).

## Context

The universe is hand-picked: 91 active tickers in `config/tickers.json`, concentrated in
semiconductors, mega-cap tech, quantum and space names. The operator wants new names to earn a
place on evidence, with never more than 100 active at once, and a tracked bench that can replace
weak names. Today `ticker-advisor/` scores candidates on demand and proposes diffs for manual
review, but there is no standing pool, ranking rule, turnover control or bench.

Evidence that shapes the decision:

- Momentum, profitability, low volatility and value replicate out of sample (Hou, Xue and Zhang
  2020; Novy-Marx 2013; Blitz and van Vliet 2007). Size is weak. Systematic buy-the-dip rules
  underperformed holding in AQR's 196-variant test.
- Published factor returns fall about 26% out of sample and 58% after publication (McLean and
  Pontiff), so backtest edges get a 50% haircut here.
- A buy/hold spread is the most effective simple turnover control (Novy-Marx and Velikov 2016).
  The Nasdaq-100 75/100/125 rank rule and MSCI and Russell buffers apply the same idea.

## Decision (proposed)

1. **Candidate pool.** NMS-listed US common stock; price above $5; market cap above $2B; 60-day
   median dollar volume above $20M; Robinhood fractional-eligible on the scan date; at least 252
   trading days of history.
2. **Score.** Equal-weight composite of four factors, each z-scored within sector and winsorized
   at plus or minus 3: momentum (average of 6- and 12-month, skipping the latest month),
   gross profits over total assets (EDGAR, first-filed, `filed <= as_of`), low volatility
   (negative 1-year daily volatility), and value (earnings yield or book-to-price, first-filed).
   A ticker missing two or more factors is unscored.
3. **Select.** Incumbents ranked 1 to 150 that pass caps stay. Non-members ranked 1 to 75 at this
   review and the prior one enter in rank order until 100. Caps: 25 per sector, 10 per industry.
   Never more than 100 active.
4. **Exit.** Below rank 150 after at least 2 quarters of tenure, or on a hard fail at any time
   (delisted, fractional-ineligible, price under $5 for 20 sessions, pending acquisition).
   Demotion means new buys stop. Shares are never sold by this system.
5. **Bench.** Ranks 101 to 250, shadow-tracked with the same buy rules in the ledger's
   counterfactual tables, so a candidate builds evidence before promotion.
6. **Cadence and turnover.** Quarterly membership proposals, at most 8 rank-based swaps each.
   Monthly rescoring for monitoring only. The move off the hand-picked 91 phases in at the same cap.
7. **Fail closed.** Reject the whole proposal and keep the last approved list when: pool coverage
   falls below 95% of the prior run; more than 8 rank-based swaps are proposed; the active list
   would be under 90 or over 100; any input is stale (quotes older than 3 sessions, EDGAR bulk not
   refreshed in 7 days); or any factor is missing for more than 10% of the pool. Missing data for
   one ticker means hold, never demote.
8. **Suggest-only boundary.** The scorer writes `universe_proposal.json` (with a content hash)
   and DuckDB rows. It never edits `config/tickers.json`. Applying a proposal stays an operator
   action until a later ADR defines an approved-list file and how the executor reads it.
9. **Tables (append-only).** `universe_scores`, `universe_proposals`, `universe_membership`
   (`effective_from`, `effective_to`, `approval_id`). Every scoring variant goes in `trials`.

## Open questions

| # | Question | Proposed default |
|---|---|---|
| 1 | Maximum return or risk-adjusted return? | Risk-adjusted: low volatility keeps its 25% weight |
| 2 | Accept sector caps that push out some semiconductor, quantum and space names? | Yes: 25 per sector, 10 per industry |
| 3 | Grandfather the current 91, or apply the rules at once? | Apply the rules, phased in at 8 swaps per quarter |
| 4 | Sector taxonomy (GICS is licensed) | EDGAR SIC codes |
| 5 | Include value despite long dry spells? | Yes, 25% |
| 6 | Market-cap floor | $2B |
| 7 | Dollar allocation within the 100 | Existing graded ladder, unchanged |
| 8 | Approval owner and window; what if nobody approves? | Operator only; no approval means keep the old list |
| 9 | Will selling ever be added? | Only the operator-approved tax-loss sale in `docs/adr-tax-loss-sale.md` (PR #2099). Demotion from this list never sells |

## Consequences

- The list changes slowly: a fast riser can take two quarters to enter. That is the price of
  bounded turnover and fewer false signals.
- Sector caps will force out some current names the operator likes.
- The free point-in-time pool (historical S&P 500) is large-cap only, so backtests cannot
  validate behavior on the small-cap speculative names held today.

## Verification

- Point-in-time backtest 2010 to 2026 on historical S&P 500 membership with delisted prices:
  daily DCA of the same budget into (A) the dynamic top-100 rule versus (B) equal-weight DCA into
  the whole eligible pool. Pass: A beats B on money-weighted annual return after the 50% haircut;
  deflated Sharpe of A minus B is at least 0.95 counting every trial; average annual membership
  turnover is 35% or less.
- Then 4 quarters of shadow tracking before the first real swap.

## Rollback

Revert the PRs. Delete the universe tables and any `universe_proposal.json`. Nothing in the live
path reads them.
