# Morning DCA Live Stream

A **live-execution** morning stream that grades each ticker's morning price
weakness against **yesterday's regular-session close** and places real Robinhood
buy orders. It is implemented by
[`morning_dca.MorningDCAExecutor`](../morning_dca.py) and governed by
[`config/morning_dca.json`](../config/morning_dca.json). Live execution was
approved **2026-07-09**.

> **Live trading.** The morning stream places **real market buy orders** through
> Robinhood account `562057042`, which is the **sole execution broker** for both
> the morning live stream and the afternoon live task (`10ce362c`). **Alpaca is
> read-only** validation/account context only. The separate 9:45 AM volatility
> outlier scan (`cd45e95b`) and the 9:00 AM pre-market buying-power/cost analysis
> (`b7961407`) remain **analytics-only** and place no orders, so the morning
> stream never duplicates their work.

## Why it is fully separate from the afternoon stream

The afternoon live task (`10ce362c`, which supersedes the deleted `ce16ba6b`)
must not be cannibalized or validated by morning activity. The morning stream is
isolated on every axis except the same-day top-up guard (see below):

| Axis | Afternoon live (`10ce362c`) | Morning live |
|---|---|---|
| Ladder | 0x → **5x** (`config/sizing_ladder.json`) | 0x → **5x** (shared) |
| Budget ledger | `StrategyState` / `BUDGET_TIERS` | `MorningLedger` namespace `morning` |
| Artifact root | `.../cron_tracking/ce16ba6b/` | `.../cron_tracking/morning_dca/` |
| Run type | `scheduled_live_test` @ `15:20` | `morning_dca_live` @ `09:45` |
| Pointer | `latest_scheduled_run.json` | `latest_morning_run.json` |
| Trades | **yes** | **yes** |

The morning run never writes into the `ce16ba6b` tree, never updates
`latest_scheduled_run.json`, and its `run_classification` carries
`valid_afternoon_evidence=false` plus `stream="morning"`. Afternoon consumers
(`67aa802b`, `a46bf007`, `041d9d90`, `0656657a`) glob only
`.../ce16ba6b/scheduled_runs/**`, so they never traverse the morning root.

Morning orders/fills are therefore **never** counted as afternoon
scheduled-execution evidence, afternoon slippage, afternoon fill counts,
afternoon reliability, afternoon watchdog confirmation, or afternoon scheduled
cost basis.

## Same-day top-up linkage (the only afternoon linkage)

Morning orders/fills **do** count as **prior same-day allocation context** for
the afternoon (`10ce362c`) same-day top-up guard. When the afternoon task
evaluates a ticker that already received a same-day morning buy, it tops up
rather than duplicating:

- `incremental_order_amount = max(0, morning_desired_amount - prior_same_day_amount)`
- When earlier same-day buys exist, the afternoon task buys only if the current
  price is **strictly below** the prior-run reference price; otherwise it holds.

Top-up guard fields carried for the 9:45 → afternoon linkage:
`prior_same_day_amount`, `prior_same_day_reference_price`,
`prior_same_day_source`, `prior_same_day_run_id`, `prior_same_day_order_id`,
`morning_desired_amount`, `incremental_order_amount`, `top_up_guard_result`, and
`reason`.

## Sizing ladder (0x → 5x)

Drop is measured as a fraction below yesterday's regular-session close. The
largest tier threshold ≤ the actual drop wins. This is the current $3-base
0x-to-5x ladder shared with the afternoon stream.

| Drop vs. prior close | Multiplier |
|---|---|
| < 0.1% | 0x (no buy) |
| 0.1% – < 1% | 1x |
| 1% – < 2% | 1.5x |
| 2% – < 3% | 2x |
| 3% – < 4% | 2.5x |
| 4% – < 5% | 3x |
| 5% – < 6% | 3.5x |
| 6% – < 7% | 4x |
| 7% – < 8% | 4.5x |
| ≥ 8% | 5x (no upper cutoff) |

Order dollars = `base_amount_usd (default $3) × multiplier`.

## Budget ledger

`MorningLedger` is a standalone budget session (namespace `morning`) tracking
morning notional. Configurable hooks:

- `morning_daily_cap_usd` (default `90.0`)
- `morning_weekly_cap_usd` (default `450.0`)
- `morning_run_state_root` (default `/home/user/workspace/cron_tracking/morning_dca`)

Exceeding a cap adds a warning to the run artifact; it never touches the
afternoon daily/weekly ledger.

## Artifacts

A scheduled morning run writes to
`.../cron_tracking/morning_dca/YYYYMMDD_0945ET/`:

- `run_state.json` — classification, ladder, budget, signal rows, warnings.
- `morning_signals.csv` — one row per ticker.

and updates `.../cron_tracking/morning_dca/latest_morning_run.json`. Manual runs
write under a distinct `manual_morning_live_*` run id and never update the
pointer.

Each signal row includes: `ticker`, `source`, `action`, `reason`,
`current_price`, `previous_close`, `drop_pct`, `multiplier`,
`hypothetical_amount_usd`, and `warnings`.

## Run classification

Validated by
[`schemas/morning_run_classification.schema.json`](../schemas/morning_run_classification.schema.json).
A scheduled morning run:

```json
{
  "run_origin": "scheduled",
  "run_type": "morning_dca_live",
  "stream": "morning",
  "cron_id": "morning-dca-0945",
  "supersedes_cron_id": null,
  "scheduled_for_et": "09:45",
  "scheduled_window_start_et": "09:40",
  "scheduled_window_end_et": "09:50",
  "manual_run": false,
  "run_id": "morning_live_YYYYMMDD_0945ET",
  "state_schema_version": "morning-live-v1",
  "execution_broker": "robinhood",
  "account_number": "562057042",
  "valid_afternoon_evidence": false
}
```

## Usage

```python
from datetime import datetime, time
from morning_dca import MorningConfig, MorningDCAExecutor
from near_close_dca import EASTERN

cfg = MorningConfig(tickers=("AAPL", "MSFT", "NVDA"))
executor = MorningDCAExecutor(cfg)
now = datetime.now(tz=EASTERN)
result = executor.run(now, robinhood_data_provider)  # live execution
```
