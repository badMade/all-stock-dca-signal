Implement exactly Rock 1 from PLAN.md in this repo (read PLAN.md's "Rock 1"
section first for full context — scope, non-goals, and the exact test to add,
including the round-2 fix about monkeypatching
`morning_dca._dated_manual_override_cap_seed_roots`).

Summary: three functions (`morning_dca.default_pooled_cap_seed_roots`,
`market_open_dca.default_market_open_cap_seed_roots`,
`intraday_dca.default_intraday_cap_seed_roots`) hand-duplicate the same
6-root tuple in different orders. Introduce one shared
`default_all_stream_cap_seed_roots()` in `morning_dca.py`, and make the three
existing functions thin wrappers around it so every existing call site and
test keeps working unchanged. Add the new regression test from PLAN.md's Rock
1 section to `tests/test_morning_dca.py`, including the monkeypatch of
`_dated_manual_override_cap_seed_roots` to `()` so the exact-tuple assertion
is isolated from the dynamic glob behavior.

Do NOT touch `StrategyState`, `BudgetSnapshot`, `MorningLedger`, or
`prior_allocation_roots` — out of scope per the plan.

When done, run this proof command yourself and report the output verbatim:
```
.venv/bin/python -m pytest tests/test_morning_dca.py tests/test_market_open_dca.py tests/test_intraday_dca.py tests/test_midday_dca.py -q
```
Also run `ruff check morning_dca.py market_open_dca.py intraday_dca.py tests/test_morning_dca.py` and report that output too.
