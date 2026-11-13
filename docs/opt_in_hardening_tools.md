# Opt-in hardening tools (retry helper, cap parity tests, run-state index)

Added 2026-11-13 against `PRODUCTION_READINESS.md` gaps 1, 2 and 6. Everything here is **additive and unwired**:
no live path imports it, no default changes, and no existing test was modified. Each item is a building block or a
tripwire so that the structural work in Phase 2/3 of the readiness roadmap can be done as a small, reviewed change
instead of a large one. Wiring any of it into a live path needs its own review and acceptance run (`CLAUDE.md`:
"Consolidating is a decision, not a cleanup").

## 1. `dca_retry.py`: retry/backoff that cannot double-buy

Gap 6: no retry or backoff anywhere, and a 429 is an observed live failure (2026-07-17: 7 of 27 manual override
orders failed). `dca_retry` provides the pieces and pins their safety properties in `tests/test_dca_retry.py`:

| Property | How |
|---|---|
| Unknown errors are never retried | `default_classifier` returns `DO_NOT_RETRY` for anything not positively transient |
| A throttle is safe to repeat; a timeout is not | 429 is `RETRY`; timeouts and 5xx are `AMBIGUOUS` because the order may have landed |
| A possibly-landed order is never blindly resent | `place_with_reconciliation` calls `lookup(ref_id)` first; found means return it, not found means resend, lookup failure raises `PlacementUncertain` and does **not** resend |
| One idempotency key per logical order | the same `ref_id` is passed on every attempt; an empty `ref_id` is refused |
| Bounded | `RetryPolicy` caps attempts, the per-delay ceiling and an optional wall-clock deadline (for example the slot window); NaN, negative and boolean values are rejected |

Reads use `call_with_retry`. Placement uses `place_with_reconciliation` with the broker's own order-history lookup
as `lookup`. **Not wired into `RobinhoodBroker` or any runner.** To adopt it, wrap the adapter's `place_fn` and
history lookup where they are injected, then run the three-entry-point acceptance pass.

## 2. `tests/test_cap_parity_across_entry_points.py`: one set of limits

The three entry points seed *spent-so-far* from three sources (broker history, same-day artifacts, `StrategyState`).
Unifying that is a live-path decision and is **not done here**. The cheaper half of the same hazard is that the
**limits** must be identical everywhere, and now a test fails if they are not:

- the per-order cap across `StrategyConfig`, `MorningConfig`, `dca_runner.MAX_PER_ORDER_USD`,
  `morning_dca.load_max_per_order_usd` and `config/budgets.json`;
- the Normal-tier daily/weekly caps across `BUDGET_TIERS[0]` and every executor stream's defaults;
- both loaders overlay the same `max_orders_per_day`.

Together with `tests/test_kill_switch_all_streams.py` (previous PR) this pins the limits and the kill switch on every
stream, which is the safety net needed before anyone consolidates the seeding.

## 3. `scripts/index_run_states_sqlite.py`: a derived index, not a new database

Gap 2: "the file tree is the database" and each run rescans it. Moving canonical state into SQLite changes the live
path and is **not done here**. This script builds a *derived, rebuildable* index of `run_state*.json` files so
questions like "runs per stream", "manual vs scheduled" and "which states do not parse" are instant:

- JSON files stay the only source of truth; delete the database any time and rebuild it.
- Read-only on the artifact tree; the database must live outside it (refused otherwise).
- Incremental (size and mtime), idempotent, symlinks never followed; unparseable files are **recorded with their
  error**, not skipped, so "no history" and "history I could not read" stay distinguishable.

```bash
python scripts/index_run_states_sqlite.py --db ~/dca-index.sqlite3
python scripts/index_run_states_sqlite.py --db ~/dca-index.sqlite3 --summary
```

## Deliberately not done, and why

| Item | Why it is not in this change |
|---|---|
| Single source for daily/weekly *seeding* across the three entry points | Changes the live 3:30 PM path; needs its own design and acceptance run |
| Committed runner entrypoint per stream | Needs the Robinhood MCP callables that only the external agent sessions hold; cannot be built or verified from this repo |
| Canonical state in SQLite (replacing the JSON tree) | Live-path change plus a migration; the derived index above is the safe first step |
| Wiring `dca_retry` into the broker adapter | Changes order placement behaviour; opt-in wrapper first, then an acceptance run |
