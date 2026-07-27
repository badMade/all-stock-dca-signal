# Live wiring & orchestrator

How the strategy library becomes a running system, and where the safety gates are.

## Modules

| Module | Role |
|---|---|
| `near_close_dca.py` | Pure strategy library: signals, budgets, safeguards, artifacts. Standard library only. |
| `dca_config.py` | Loads `config/*.json` onto `StrategyConfig` defaults (tickers, budgets, sizing ladder, approved trading window). |
| `dca_live_adapters.py` | `RobinhoodDataProvider`, `FinanceDataProvider`, `RobinhoodBroker` — conform to the `MarketDataProvider` / `BrokerAdapter` protocols via **injected callables**. No credentials, no network in-module. |
| `run_dca.py` | Orchestrator + CLI: config → providers/broker → `run_daily_compact` → post-trade reports. |

## Dependency injection (no secrets in the repo)

The adapters never call Robinhood directly. The scheduler/agent — which holds MCP
runtime access — passes plain callables that map 1:1 to the Robinhood MCP tools and
return their raw response dicts; all parsing lives in `dca_live_adapters`.

```python
from datetime import datetime
from dca_config import load_strategy_config
from dca_live_adapters import RobinhoodDataProvider, FinanceDataProvider
from near_close_dca import EASTERN, StrategyState, build_broker
from run_dca import run_pipeline

config = load_strategy_config()  # dry_run=True by default

# `rh_*` wrap the Robinhood MCP tools (get_equity_historicals, get_equity_quotes, …).
robinhood = RobinhoodDataProvider(historicals_fn=rh_historicals, quotes_fn=rh_quotes)
finance = FinanceDataProvider(bars_fn=finance_bars, previous_close_fn=finance_prev_close)
broker = build_broker(
    config,
    review_fn=rh_review,          # review_equity_order
    place_fn=rh_place,            # place_equity_order
    orders_fn=rh_orders,          # get_equity_orders
    tradability_fn=rh_tradability, # get_equity_tradability
    buying_power_fn=rh_buying_power,
    order_fill_fn=rh_order_fill,
    # enable_live_orders=True,    # <- the second gate; leave off until validated
)

state = load_strategy_state()   # <- see "State persistence" below; NOT a fresh StrategyState()
run_pipeline(
    datetime.now(tz=EASTERN), config, state,
    finance, robinhood, broker,
    artifacts_dir="artifacts/2026-07-06", generate_full_reports=False,
)
save_strategy_state(state)
```

Post-trade reporting re-queries `order_fill_fn` for any order still unfilled at trade
time. By default those lookups run sequentially, since `order_fill_fn` is usually a thin
wrapper around MCP calls and MCP client sessions are commonly not thread-safe. Only pass
`build_broker(config, ..., supports_concurrent_fill_lookup=True)` once the wired
`order_fill_fn` has been verified safe to call from multiple threads at once — e.g. it
opens an independent connection/session per call rather than sharing one.

## State persistence (required for the budget safeguards)

`StrategyState` holds the counters the daily/weekly/reserve caps depend on —
`submitted_notional_by_day`, `submitted_notional_by_week`, `weekly_rollover_reserve_usd`,
`reserve_week_key`, `finalized_budget_days`, and per-ticker buy counts. These live only in
memory, and `run_pipeline` does **not** load or save them. Each scheduled invocation is a
fresh process, so the caller **must** persist `StrategyState` across runs — passing a fresh
`StrategyState()` every day silently resets the reserve/weekly accounting and defeats the
documented budget caps. Persist it however the scheduler prefers (e.g. a JSON blob written
next to the daily artifacts and reloaded at the start of the next run); the `run_state.json`
artifact records the same figures for audit but is not auto-rehydrated into `StrategyState`.

## Safety gates (both must be flipped to place real orders)

1. **`StrategyConfig.dry_run`** — True by default. While True, `run_once` never calls
   `place_market_buy` on a live broker; it records a simulated `DRY-RUN-SIM-…` order id.
2. **`RobinhoodBroker.enable_live_orders`** — False by default. Even with `dry_run=False`,
   `place_market_buy` returns an `RH-DRYRUN-…` sentinel unless this is True **and** a
   `place_fn` is wired in.

Additional invariants enforced by `run_once`: the broker account must equal
`config.target_account_number` (`562057042`), the trading day must be in
`config.approved_test_dates` (the weekday window in `config/trading_window.json`), and
one order per ticker per stream per day within the daily/weekly/reserve caps and
the **$10 max-per-order** circuit breaker.

## Scheduled-live pre-authorization (approved Fri Jul 10 2026 11:20 AM ET)

Scheduled live DCA tasks are **pre-authorized** to place qualifying buys within
all caps and circuit breakers **without per-order human confirmation**:

- Scheduled live tasks **must not** call `review_equity_order` (it forces a
  preview + confirmation) and **must not** request per-order confirmation. They
  call `place_equity_order` directly **only after** every internal validation,
  signal, budget, spread, confidence/trend, and top-up check passes.
- **Manual / ad hoc runs still use the review + confirmation path**
  (`review_equity_order` first). The `review_fn`/`place_fn` wiring above is the
  manual path; a scheduled live run skips `review_fn`.

The morning 10:30 live run additionally loads the same-day 9:45 **pre-flight**
artifact and evaluates only `eligible_to_execute=true` tickers. See
[safeguards.md](safeguards.md), [morning_dca.md](morning_dca.md), and the
`execution_authorization_policy` / `circuit_breakers` blocks in
[`config/budgets.json`](../config/budgets.json).

## Afternoon placement contract (added Jul 22 2026)

The 3:30 PM ET final live task (`10ce362c`) must not stop at compute-only
planning. The Jul 22 2026 scheduled run produced 20 planned buy orders with
planned spend of $60, but the saved compute module did not call the placement
path and wrote `error_class='placement_not_implemented_in_compute_module'`.
That state is no longer an allowed terminal outcome when planned orders remain.

After `signals.csv` is written, every eligible row with `incremental_usd > 0`
or an equivalent planned order amount must enter the same-run placement phase.
If the saved compute module cannot submit orders, the scheduler must invoke the
main live placement runner or use the direct platform connector path itself.
Scheduled live placement still calls `place_equity_order` only after every
validation, budget, spread, data-confidence, regular-hours, whitelist,
buy-only, max-per-order, and same-day top-up check passes. It still must not
call `review_equity_order`.

If the placement phase is unreachable, the task writes
`order_submit_status='blocked_placement_phase_unreachable'` and
`error_class='placement_phase_unreachable'`, keeps the planned orders in
`signals.csv`, writes `submitted_orders.csv` with headers, notifies, and marks
completed order evidence false while still proving the task fired. It must not
write `order_submit_status='not_submitted'` with
`error_class='placement_not_implemented_in_compute_module'` while planned
orders remain.

Both `run_state.json` and `run_summary.json` must contain the same non-null
`run_classification` object for the 3:30 task. A null
`run_summary.run_classification` is an artifact-contract failure.

## CLI (dry-run only)

```bash
python run_dca.py --demo --report            # deterministic in-memory dry-run + reports
python run_dca.py --demo --now 2026-07-06T15:45 --artifacts-dir artifacts/demo
```

The CLI is dry-run only: a bare Python process has no MCP callables, so live trading is
driven from the scheduler via `run_pipeline`. The CLI refuses to run if
`config.dry_run` is False.
