# Handoff: budget-exhaustion opportunity query (from a claude.ai session)

Start Claude Code in this repo on branch `claude/budget-exhaustion-opportunity` and say:
"Read HANDOFF_budget_exhaustion_opportunity.md and continue."

## Goal
Decide whether two Markov ideas are worth building, using data you already have:
1. MDP cash pacing (reserve budget for later slots)
2. Absorbing tier chain (probability the day reaches Tier B or C)

Both only pay off if this happened often: a later scheduled slot saw price <= the prior session's
low (Tier B or deeper) while the day's remaining budget could not fund a Tier B buy
($2.00 base x 2x = $4.00). This query counts it. Read-only. Not wired into any live path.

Owner rules: tests first, wait for approval before implementing; no em dashes; any PR is a draft
and links back to the originating chat session.

## Done (committed on this branch)
- `dca-sizing-signals/tests/ledger/test_budget_exhaustion_opportunity.py`: 22 tests, the spec.
  All fail only with `ModuleNotFoundError: market_data_ingest.ledger.opportunity` (red on purpose).
  Fixture setup (calendar, prior bar, fills, `intraday_quotes` row) already runs.
- `claude/markov-extensions.md`: the six Markov ideas split into two categories, with build order.

## Next step (only after the owner approves the tests)
Create `dca-sizing-signals/market_data_ingest/ledger/opportunity.py` with:

    budget_exhaustion_opportunity(ledger, *, known_at, daily_cap_usd, tier_b_cost_usd=4.0) -> dict

Contract the tests pin:
- Reads `fills_known_at(known_at)` (scheduled only; drop manual runs AND any order id tagged manual,
  same rule as `scheduled_outcomes`), `intraday_quotes` with `as_of <= known_at`, and the prior
  session's low from `bars_known_at(known_at)` (latest session strictly before the quote session).
- `spent_before` = scheduled fill notional for that session with `filled_at` strictly before the
  quote `ts`, summed across all tickers (the daily cap is account-wide).
- Event when `price <= prior_low` and `daily_cap_usd - spent_before < tier_b_cost_usd`.
- No prior bar: skip and count in `skipped_no_prior_bar`. Never guess.
- `ValueError` for non-finite or non-positive `daily_cap_usd` / `tier_b_cost_usd`, or a naive `known_at`.
- Never writes. Report keys: `daily_cap_usd`, `tier_b_cost_usd`, `known_at`, `sessions_observed`,
  `quotes_considered`, `sessions_with_event`, `skipped_no_prior_bar`, `events` (sorted list of
  `{session, symbol, slot, price, prior_low, spent_before_usd, remaining_usd}`).

Then run it on the real ledger DuckDB on the Mac at caps 180, 240, 300 and 360, and report
`sessions_with_event / sessions_observed` for each.

## Checks
    (cd dca-sizing-signals && python -m pytest tests/ledger/test_budget_exhaustion_opportunity.py -q)
    (cd dca-sizing-signals && python -m pytest tests/ -q)
    ruff check --isolated --select=E,F,B --line-length=120 dca-sizing-signals
    scripts/local_ci.sh --changed

## Open questions for the owner
1. Approve the 22 tests as written?
2. Run at all four daily caps, or only $180?
3. Also count Tier A misses (price < prior close, budget gone), or B-or-deeper only?
4. Where does the report live: CLI under `dca-sizing-signals`, a console page, or both?

## Known limits
- The day's budget tier is not stored in the ledger, so the cap is a caller parameter.
- Tier C is not reconstructed (needs the forecast).
- Coverage starts when `intraday_quotes` began filling; count sessions before trusting a ratio.
- Fixtures are synthetic (fake tickers like ZZZA, round numbers), per the ledger-test convention.
