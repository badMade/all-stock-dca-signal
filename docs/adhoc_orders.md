# Ad hoc orders

`scripts/adhoc_orders.py` is the fallback for placing a known list of buys when the scheduled
path is unavailable (agent permission gate, a dead slot, a half-finished batch). It validates
the list and writes a manual-run plan. It never contacts a broker.

## Usage

```bash
# orders.csv: header "ticker,usd", one row per buy (or a JSON list of {"ticker", "usd"})
python3 -m scripts.adhoc_orders --orders orders.csv --trading-day 2026-10-13 \
    --spent-today 40.00 --spent-week 310.00 --attempts-today 12 --buying-power 500.00
```

Exit 0: plan valid, sheet printed, plan written to
`<cron root>/manual_runs/<timestamp>/adhoc_order_plan.json`. Exit 2: nothing written, every
problem listed by code. Reusing a `--timestamp` also exits 2 and leaves the first run untouched.

## Rules

| Check | Source |
| --- | --- |
| Ticker in the active universe | `config/tickers.json` `active` |
| Ticker not excluded (QBUT) | `config/tickers.json` `excluded`; wins over the universe |
| Per-order cap | `dca_runner.MAX_PER_ORDER_USD` ($16) |
| Daily and weekly caps | Normal tier in `near_close_dca.BUDGET_TIERS`, plus the spend you pass in |
| Daily attempt cap (120, account-wide) | `config/budgets.json` `circuit_breakers.max_orders_per_day`, plus `--attempts-today` |
| Available buying power | `--buying-power` |
| One order per ticker, positive whole-cent amounts | `build_plan` |

Validation is all-or-nothing: one bad row yields an empty plan. Caps are compared in whole cents.
Every figure and cap must be a finite number (NaN, infinity, bool, strings and negatives are
rejected), so a bad input cannot silently disable a ceiling.

## The four figures are yours to supply

The tool does not read broker history, so the CLI refuses to run without `--spent-today`,
`--spent-week`, `--attempts-today` and `--buying-power`. Take them from a fresh, fully paginated
`get_equity_orders` pull and `get_accounts`. Attempts count rejected and cancelled buys too.

## Placing the orders

The CLI cannot place orders (`run_dca.main()` invariant). From an approved agent session:

```python
plan = build_plan(...)                      # or read the written plan
write_plan(plan, root=root, timestamp=ts)   # the CLI already does this
result = place_adhoc(plan, place_order, confirm=True)
write_execution_state(plan, result, root=root, timestamp=ts)
```

`place_order` maps onto the Robinhood `place_equity_order` tool and is called once per order with the
key `dca-adhoc-<date>-<TICKER>`. A response counts as submitted only if it carries an order id and is
not rejected, cancelled, failed, or for a different symbol or key; anything else halts the batch as
`unconfirmed_response` (the order may still exist, so check broker history before retrying). A raised
exception is recorded by type only, never its message. The batch stops at the first failure, never
retries, and returns the rest as `not_attempted`. Without `confirm=True` it is a dry run.

If the agent's permission gate blocks `place_equity_order`, the operator must allow it
(`/permissions`) or place the sheet by hand in Robinhood.

## Run classification

Output is a manual run (`run_origin: manual`, `manual_run: true`) and never writes a `latest_*`
pointer, so reporting excludes it from scheduled-live metrics (`docs/run_classification.md`).
`adhoc_order_plan.json` holds the intent; `adhoc_execution_state.json` holds the outcome and broker
order ids (status `dry_run`, `completed` or `halted`) so these orders can be excluded and reconciled.
