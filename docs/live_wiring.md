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

run_pipeline(
    datetime.now(tz=EASTERN), config, StrategyState(),
    finance, robinhood, broker,
    artifacts_dir="artifacts/2026-07-06", generate_full_reports=False,
)
```

## Safety gates (both must be flipped to place real orders)

1. **`StrategyConfig.dry_run`** — True by default. While True, `run_once` never calls
   `place_market_buy` on a live broker; it records a simulated `DRY-RUN-SIM-…` order id.
2. **`RobinhoodBroker.enable_live_orders`** — False by default. Even with `dry_run=False`,
   `place_market_buy` returns an `RH-DRYRUN-…` sentinel unless this is True **and** a
   `place_fn` is wired in.

Additional invariants enforced by `run_once`: the broker account must equal
`config.target_account_number` (`562057042`), the trading day must be in
`config.approved_test_dates` (the weekday window in `config/trading_window.json`), and
one order per ticker per day within the daily/weekly/reserve caps.

## CLI (dry-run only)

```bash
python run_dca.py --demo --report            # deterministic in-memory dry-run + reports
python run_dca.py --demo --now 2026-07-06T15:45 --artifacts-dir artifacts/demo
```

The CLI is dry-run only: a bare Python process has no MCP callables, so live trading is
driven from the scheduler via `run_pipeline`. The CLI refuses to run if
`config.dry_run` is False.
