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
- Spec revision 2 (31 tests), operator-approved 2026-11-23, and its implementation
  `dca-sizing-signals/market_data_ingest/ledger/opportunity.py`: all 31 pass, full sibling suite
  green (1420 passed). This is the GLOBAL-ladder estimate plus the point-in-time extract.
- `docs/adr-budget-exhaustion-replay.md` (proposed): the faithful replay the operator asked for.
  Per-ticker ladders, ML nudge, inverse-Martingale suppression, kill switch and the same-day top-up
  guard are computed by calling `near_close_dca`'s own functions from `scripts/`, never copied.
- `claude/markov-extensions.md`: the six Markov ideas in two categories, with build order.

## Decided (operator, 2026-11-23)
- Run at all four daily caps; count both dip kinds; report is a CLI only.
- No global-ladder-only shortcut for the final number: per-ticker ladders must be replayed.

## Next step
1. Get the owner's answers to the six open questions in `docs/adr-budget-exhaustion-replay.md`.
2. Then, test-first (tests wait for approval): the replay in `scripts/budget_exhaustion_replay.py`
   and its CLI. Run it on the Mac (it needs the cron tracking artifact tree and the ledger DuckDB).

## Checks
    (cd dca-sizing-signals && python -m pytest tests/ledger/test_budget_exhaustion_opportunity.py -q)
    (cd dca-sizing-signals && python -m pytest tests/ -q)
    ruff check --isolated --select=E,F,B --line-length=120 dca-sizing-signals
    scripts/local_ci.sh --changed

## Open questions for the owner
See section 7 of `docs/adr-budget-exhaustion-replay.md`.

## Known limits
- The day's budget tier is not stored in the ledger, so the cap is a caller parameter.
- Per-ticker calibrated ladders, the ML nudge and the kill-switch cap are not replayed.
- Coverage starts when `intraday_quotes` began filling; count sessions before trusting a ratio.
- Fixtures are synthetic (fake tickers like ZZZA, round numbers), per the ledger-test convention.
