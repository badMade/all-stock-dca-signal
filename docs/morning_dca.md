# Morning DCA Dry-Run Stream

A **dry-run / analytics-only** morning stream that grades each ticker's morning
price weakness against **yesterday's regular-session close**. It is implemented
by [`morning_dca.MorningDCAExecutor`](../morning_dca.py) and governed by
[`config/morning_dca.json`](../config/morning_dca.json).

> **Not live trading.** The morning stream places **no orders**, runs **no
> Robinhood order review**, and calls **no broker placement path**. It only
> writes signal artifacts with *hypothetical* dollar amounts. Robinhood account
> `562057042` remains the sole execution broker for the afternoon live task
> (`10ce362c`); Alpaca stays read-only.

## Why it is fully separate from the afternoon stream

The afternoon live task (`10ce362c`, which supersedes the deleted `ce16ba6b`)
must not be cannibalized or validated by morning activity. The morning stream is
isolated on every axis:

| Axis | Afternoon live (`10ce362c`) | Morning dry-run |
|---|---|---|
| Ladder | 0x → **5x** (`config/sizing_ladder.json`) | 0x → **3x** (this stream) |
| Budget ledger | `StrategyState` / `BUDGET_TIERS` | `MorningLedger` namespace `morning` |
| Artifact root | `.../cron_tracking/ce16ba6b/` | `.../cron_tracking/morning_dca/` |
| Run type | `scheduled_live_test` @ `15:20` | `morning_dca_dry_run` @ `09:45` |
| Pointer | `latest_scheduled_run.json` | `latest_morning_run.json` |
| Trades | **yes** | **no** |

The morning run never writes into the `ce16ba6b` tree, never updates
`latest_scheduled_run.json`, and its `run_classification` carries
`valid_afternoon_evidence=false` plus `stream="morning"`. Afternoon consumers
(`67aa802b`, `a46bf007`, `041d9d90`, `0656657a`) glob only
`.../ce16ba6b/scheduled_runs/**`, so they never traverse the morning root.

## Morning sizing ladder (0x → 3x)

Drop is measured as a fraction below yesterday's regular-session close. The
largest tier threshold ≤ the actual drop wins.

| Drop vs. prior close | Multiplier |
|---|---|
| < 0.1% | 0x (no buy) |
| 0.1% – < 2% | 1x |
| 2% – < 5% | 1.5x |
| 5% – < 8% | 2x |
| ≥ 8% | 3x (no upper cutoff) |

Hypothetical dollars = `base_amount_usd (default $3) × multiplier`.

## Budget ledger

`MorningLedger` is a standalone budget session (namespace `morning`) tracking
only hypothetical morning notional. Configurable hooks:

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
write under a distinct `manual_morning_*` run id and never update the pointer.

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
  "run_type": "morning_dca_dry_run",
  "stream": "morning",
  "cron_id": "morning-dca-0945",
  "supersedes_cron_id": null,
  "scheduled_for_et": "09:45",
  "scheduled_window_start_et": "09:40",
  "scheduled_window_end_et": "09:50",
  "manual_run": false,
  "run_id": "morning_YYYYMMDD_0945ET",
  "state_schema_version": "run-classification-v1",
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
result = executor.run(now, robinhood_data_provider)  # dry-run only
```
