# Morning DCA Stream

The **morning** DCA stream grades each ticker's morning price weakness against
**yesterday's regular-session close** on its own **0x-to-3x** ladder. It is
implemented by [`morning_dca.MorningDCAExecutor`](../morning_dca.py) and governed
by [`config/morning_dca.json`](../config/morning_dca.json).

As of the **2026-07-09** schedule change the stream runs in **two modes**:

| Mode | Task | ET | Trades | run_type | schema | Artifact root |
|---|---|---|---|---|---|---|
| `watchlist` | `morning-dca-0945` | 9:45 AM | **no** | `morning_dca_watchlist_scan` | `morning-watchlist-v1` | `.../cron_tracking/morning_dca_watchlist/` |
| `live` | `42173154` | 10:30 AM | **yes** | `morning_dca_live` | `morning-live-v1` | `.../cron_tracking/morning_dca/` |

## 9:45 AM watchlist / early-warning scan (read-only)

A **read-only / analytics-only** scan. It grades morning weakness and may surface
early candidates, but every dollar amount is **hypothetical / watchlist-only**.
It places **no orders**, runs **no Robinhood order review**, and calls **no
broker placement path**. Writes under
`.../cron_tracking/morning_dca_watchlist/YYYYMMDD_0945ET/`
(`run_id='morning_YYYYMMDD_0945ET'`) and updates that root's
`latest_morning_run.json`.

## 10:30 AM live Robinhood execution

The live execution moved from the former 9:45 AM slot to **10:30 AM ET** so the
trend read has a true **60-minute intraday window**. It uses **Robinhood account
`562057042`** as the sole execution broker (Alpaca stays read-only for
validation/context), the **$3 base morning 0x-to-3x ladder**, and the `morning`
budget ledger. Writes under `.../cron_tracking/morning_dca/YYYYMMDD_1030ET/`
(`run_id='morning_live_YYYYMMDD_1030ET'`).

### Confidence rule (no trend, no auto-buy)

A candidate whose price/drop passes the ladder is auto-bought **only when the
intraday trend can be validated** — at least `trend_min_bars` (default **6**)
usable intraday bars (finite, positive close, timestamp at or before the decision
time). If price/drop passes but the intraday trend **cannot** be validated, the
run does **not** auto-buy; the candidate is classified
`execution_status='manual_review_candidate'` and `trend_validated=false`. There is
**no** loosened trend-unavailable execution path.

Signal rows in live mode add `execution_status`
(`auto_buy_eligible` | `manual_review_candidate`) and `trend_validated`; the
run_state counts and lists `manual_review_candidates` / `manual_review_tickers`.

## Why it is fully separate from the afternoon stream

The afternoon live task (`10ce362c`, which supersedes the deleted `ce16ba6b`)
must not be cannibalized or validated by morning activity. The morning stream is
isolated on every axis:

| Axis | Afternoon live (`10ce362c`) | Morning stream |
|---|---|---|
| Ladder | 0x → **5x** (`config/sizing_ladder.json`) | 0x → **3x** (this stream) |
| Budget ledger | `StrategyState` / `BUDGET_TIERS` | `MorningLedger` namespace `morning` |
| Artifact root | `.../cron_tracking/ce16ba6b/` | `.../cron_tracking/morning_dca[_watchlist]/` |
| Run type | `scheduled_live_test` @ `15:20` | `morning_dca_watchlist_scan` @ `09:45` / `morning_dca_live` @ `10:30` |
| Pointer | `latest_scheduled_run.json` | `latest_morning_run.json` |
| Trades | **yes** | watchlist **no** / live **yes** (morning-only) |

Neither morning mode writes into the `ce16ba6b` tree, neither updates
`latest_scheduled_run.json`, and each `run_classification` carries
`valid_afternoon_evidence=false` plus `stream="morning"`. Afternoon consumers
(`67aa802b`, `a46bf007`, `041d9d90`, `0656657a`) glob only
`.../ce16ba6b/scheduled_runs/**`, so they never traverse either morning root.

**Prior-allocation context only:** morning **live** fills are read by the
afternoon **same-day top-up guard** as prior-allocation context, but never as
afternoon scheduled evidence, slippage, watchdog confirmation, scheduled cost
basis, or scheduled fill counts. See
[strategy.md](strategy.md#same-day-prior-run-offset--top-up-guard).

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

Dollars = `base_amount_usd (default $3) × multiplier` (hypothetical in
watchlist mode, actual order size in live mode).

## Budget ledger

`MorningLedger` is a standalone budget session (namespace `morning`) tracking
only morning notional. Configurable hooks:

- `morning_daily_cap_usd` (default `90.0`)
- `morning_weekly_cap_usd` (default `450.0`)
- per-mode roots `watchlist_run_state_root` / `live_run_state_root` (legacy
  `morning_run_state_root` mirrors the watchlist root)

Exceeding a cap adds a warning to the run artifact; it never touches the
afternoon daily/weekly ledger.

## Artifacts

A scheduled morning run writes to its mode's root
(`.../cron_tracking/morning_dca_watchlist/YYYYMMDD_0945ET/` for watchlist,
`.../cron_tracking/morning_dca/YYYYMMDD_1030ET/` for live):

- `run_state.json` — classification, ladder, budget, signal rows, warnings.
- `morning_signals.csv` — one row per ticker.

and updates that root's `latest_morning_run.json`. Manual runs write under a
distinct `manual_morning[_live]_*` run id and never update the pointer.

Each signal row includes: `ticker`, `source`, `action`, `reason`,
`current_price`, `previous_close`, `drop_pct`, `multiplier`,
`hypothetical_amount_usd`, and `warnings`; live-mode rows also carry
`execution_status` and `trend_validated`.

## Run classification

Validated by
[`schemas/morning_run_classification.schema.json`](../schemas/morning_run_classification.schema.json).
A scheduled watchlist run:

```json
{
  "run_origin": "scheduled",
  "run_type": "morning_dca_watchlist_scan",
  "stream": "morning",
  "trades": false,
  "cron_id": "morning-dca-0945",
  "supersedes_cron_id": null,
  "scheduled_for_et": "09:45",
  "scheduled_window_start_et": "09:40",
  "scheduled_window_end_et": "09:50",
  "manual_run": false,
  "run_id": "morning_YYYYMMDD_0945ET",
  "state_schema_version": "morning-watchlist-v1",
  "valid_afternoon_evidence": false
}
```

A scheduled live run:

```json
{
  "run_origin": "scheduled",
  "run_type": "morning_dca_live",
  "stream": "morning",
  "trades": true,
  "cron_id": "42173154",
  "supersedes_cron_id": null,
  "scheduled_for_et": "10:30",
  "scheduled_window_start_et": "10:25",
  "scheduled_window_end_et": "10:35",
  "manual_run": false,
  "run_id": "morning_live_YYYYMMDD_1030ET",
  "state_schema_version": "morning-live-v1",
  "valid_afternoon_evidence": false
}
```

## Usage

```python
from datetime import datetime
from morning_dca import MorningConfig, MorningDCAExecutor
from near_close_dca import EASTERN

# Watchlist (9:45 AM, read-only) is the default mode.
watch = MorningDCAExecutor(MorningConfig(tickers=("AAPL", "MSFT", "NVDA")))

# Live (10:30 AM) execution mode.
live = MorningDCAExecutor(
    MorningConfig(tickers=("AAPL", "MSFT", "NVDA")),
    mode="live",
)

now = datetime.now(tz=EASTERN)
result = live.run(now, robinhood_data_provider)
```
