# Handoff: budget-exhaustion opportunity query (from a claude.ai session)

Start Claude Code in this repo on branch `claude/budget-exhaustion-opportunity` and say:
"Read HANDOFF_budget_exhaustion_opportunity.md and continue."

## Goal
Decide whether two Markov ideas are worth building, using data you already have:
1. MDP cash pacing (reserve budget for later slots)
2. Absorbing tier chain (probability the day reaches a deeper ladder step)

Both only pay off if this happened often: a later scheduled slot saw a buyable dip while the
day's remaining budget could not fund the buy the ladder would have made. This query counts it.
Read-only. Not wired into any live path.

Owner rules: tests first, wait for approval before implementing; no em dashes; any PR is a draft
and links back to the originating chat session.

## Done (committed on this branch)
- `dca-sizing-signals/tests/ledger/test_budget_exhaustion_opportunity.py`, revision 2: 31 tests, the spec.
  All fail only because `load_global_ladder` etc. do not exist yet (red on purpose).
  Revision 1 (22 tests, approved 2026-11-23) priced every deep dip as a flat $4 "Tier B" buy.
  The repo's ladder is a 1x-8x log-scaled ladder on drop vs prior close (config/sizing_ladder.json),
  times a $2 base strictly below the prior low or a $1 base below the close only
  (dca_ladder_core.select_base_usd), so revision 2 uses the ladder's own planned dollars.
  It also folds in the owner's answers: count both dip kinds, run at all four caps.
- `claude/markov-extensions.md`: the six Markov ideas split into two categories, with build order.

## Next step (only after the owner approves revision 2)
Create `dca-sizing-signals/market_data_ingest/ledger/opportunity.py` with:

    load_global_ladder(config_dir) -> GlobalLadder        # .planned_usd(price, *, prior_close, prior_low)
    load_daily_caps(config_dir) -> tuple[float, ...]       # budgets.json budget_tiers daily_cap_usd
    budget_exhaustion_opportunity(ledger, *, known_at, daily_cap_usd, ladder) -> dict
    budget_exhaustion_opportunity_all_caps(ledger, *, known_at, config_dir=None) -> list[dict]

Contract the tests pin:
- Ladder mirrors dca_ladder_core: largest `min_drop_pct` <= drop wins, raw float compare, no epsilon;
  base $2 only when price < prior low (strict), $1 when below close only, nothing at or above close.
  Global ladder only: per-ticker calibration, ML nudge and kill-switch cap are not replayed.
- Reads `fills_known_at(known_at)` (scheduled only; drop any order id tagged manual in any visible
  version, same rule as `scheduled_outcomes`), `intraday_quotes` with `as_of <= known_at`, and the
  prior session's close and low from `bars_known_at(known_at)`.
- `spent_before` = scheduled fill notional for that session with `filled_at` strictly before the
  quote `ts`, summed across all tickers. Event when `daily_cap_usd - spent_before < planned_usd`.
- No prior bar: skip and count. Bad cap (incl. bool) or naive `known_at`: `ValueError`.
  Missing config file: `FileNotFoundError`. Never writes.
- Report keys: `daily_cap_usd`, `known_at`, `ladder` ("global"), `sessions_observed`,
  `quotes_considered`, `sessions_with_event`, `skipped_no_prior_bar`, `events`, and
  `by_tier` -> {`below_low`, `below_close_only`} -> {`sessions_with_event`, `events`}.

Then run `budget_exhaustion_opportunity_all_caps` on the real ledger DuckDB on the Mac and report
`sessions_with_event / sessions_observed` per cap and per dip kind.

## Checks
    (cd dca-sizing-signals && python -m pytest tests/ledger/test_budget_exhaustion_opportunity.py -q)
    (cd dca-sizing-signals && python -m pytest tests/ -q)
    ruff check --isolated --select=E,F,B --line-length=120 dca-sizing-signals
    scripts/local_ci.sh --changed

## Open questions for the owner
1. Approve revision 2 (31 tests)? (Revision 1 approved; answers: all four caps, both dip kinds.)
2. Where does the report live: CLI under `dca-sizing-signals`, a console page, or both?

## Known limits
- The day's budget tier is not stored in the ledger, so the cap is a caller parameter.
- Per-ticker calibrated ladders, the ML nudge and the kill-switch cap are not replayed.
- Coverage starts when `intraday_quotes` began filling; count sessions before trusting a ratio.
- Fixtures are synthetic (fake tickers like ZZZA, round numbers), per the ledger-test convention.
