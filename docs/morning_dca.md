# Morning DCA Stream

The **morning** DCA stream grades each ticker's morning price weakness against
**yesterday's regular-session close** on the shared **$3-base 0x-to-5x** live
ladder. As of the **Fri Jul 10 2026** live-automation update every live stream
(morning 10:30, midday 12:30, afternoon 3:20) shares one ladder (bound to
`near_close_dca.StrategyConfig.sizing_tiers`); the former morning-only 0x-to-3x
ladder is **historical/superseded**. It is implemented by
[`morning_dca.MorningDCAExecutor`](../morning_dca.py) and governed by
[`config/morning_dca.json`](../config/morning_dca.json).

The stream runs in these modes:

| Mode | Task | ET | Trades | run_type | schema | Artifact root |
|---|---|---|---|---|---|---|
| `preflight` | `morning-dca-0945` | 9:45 AM | **no** | `morning_dca_preflight` | `morning-preflight-v1` | `.../cron_tracking/morning_dca_preflight/` |
| `live` | `42173154` | 10:30 AM | **yes** | `morning_dca_live` | `morning-live-v1` | `.../cron_tracking/morning_dca/` |
| `watchlist` | (read-only, optional) | 9:45 AM | **no** | `morning_dca_watchlist_scan` | `morning-watchlist-v1` | `.../cron_tracking/morning_dca_watchlist/` |

## 9:45 AM pre-flight validation (read-only)

A **read-only / analytics-only** pre-screen for the 10:30 live evaluator. It
grades morning weakness on the shared ladder and emits
**`eligible_to_execute=true/false` per ticker**. It places **no orders**, runs
**no Robinhood order review**, and calls **no broker placement path**. Writes
under `.../cron_tracking/morning_dca_preflight/YYYYMMDD_0945ET/` —
`run_state.json`, `validation.csv`, `signals.csv`, `run_summary.json` — and
updates that root's `latest_preflight.json` (`run_id='morning_preflight_YYYYMMDD_0945ET'`).

**`eligible_to_execute` semantics:** a **coarse pre-screen** answering "is this
ticker worth re-evaluating at 10:30?", **NOT** a final buy approval. A ticker is
eligible when it graded to a positive-multiplier BUY at 9:45 with a usable
current price and previous close. The 10:30 live run recomputes everything fresh
and may still decline; an ineligible ticker is simply skipped by the 10:30 gate.

(An optional read-only `watchlist` early-warning scan mode remains available,
writing under `.../cron_tracking/morning_dca_watchlist/`, run_type
`morning_dca_watchlist_scan`; its dollar amounts are hypothetical/watchlist-only.)

## 10:30 AM live Robinhood execution

The live execution runs at **10:30 AM ET** so the trend read has a true
**60-minute intraday window**. It uses **Robinhood account `562057042`** as the
sole execution broker (Alpaca stays read-only for validation/context), the shared
**$3 base 0x-to-5x live ladder**, and the `morning` budget ledger. It **loads the
same-day 9:45 pre-flight artifact** and evaluates **only `eligible_to_execute=true`
tickers**, recomputing signals/budget/spread/trend/top-up fresh; if the pre-flight
artifact is missing it degrades to the full universe with a warning rather than
blocking all buys. As approved Fri Jul 10 2026 11:20 AM ET this scheduled live
task is **pre-authorized** to place qualifying buys directly via `place_equity_order`
(no per-order confirmation, no `review_equity_order`) after every internal check
passes. Writes under `.../cron_tracking/morning_dca/YYYYMMDD_1030ET/`
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
isolated on every axis except the same-day top-up guard (see below):

| Axis | Afternoon live (`10ce362c`) | Morning stream |
|---|---|---|
| Ladder | 0x → **5x** (`config/sizing_ladder.json`) | 0x → **5x** (shared) |
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

# Pre-flight validation (9:45 AM, read-only): emits eligible_to_execute per ticker.
preflight = MorningDCAExecutor(
    MorningConfig(tickers=("AAPL", "MSFT", "NVDA")),
    mode="preflight",
)

# Live (10:30 AM) execution mode: loads the same-day pre-flight and evaluates
# only eligible_to_execute=true tickers.
live = MorningDCAExecutor(
    MorningConfig(tickers=("AAPL", "MSFT", "NVDA")),
    mode="live",
)

now = datetime.now(tz=EASTERN)
result = live.run(now, robinhood_data_provider)
```

## Recovery record — 2026-07-10 10:30 AM ET morning live run

On **Fri Jul 10 2026** the morning 10:30 live execution was recovered and its
artifacts persisted under
`/home/user/workspace/cron_tracking/morning_dca/20260710_1030ET/`
(`run_state.json`, `run_summary.json`, `submitted_orders.csv`,
`filled_orders.csv`). The run placed **14 filled morning orders totaling $78.00**
on the shared $3-base 0x-to-5x ladder. These are **valid morning evidence only** —
they carry `stream='morning'` and `valid_afternoon_evidence=false`, and count
only as **prior-allocation context** for the afternoon same-day top-up guard
(never afternoon scheduled evidence, slippage, watchdog confirmation, scheduled
cost basis, or scheduled fill counts).

| Ticker | Amount (USD) | Avg fill price |
|---|---|---|
| DFTX | $13.50 | 45.1150 |
| LUNR | $9.00 | 16.0099 |
| NFLX | $7.50 | 72.9950 |
| MRVL | $6.00 | 234.6599 |
| SPCX | $6.00 | 148.3099 |
| QBTS | $6.00 | 20.0999 |
| GLW | $4.50 | 187.1988 |
| LITE | $4.50 | 772.5971 |
| STM | $4.50 | 70.3899 |
| GOOGL | $4.50 | 353.1899 |
| AAPL | $3.00 | 313.3850 |
| GOOG | $3.00 | 351.0700 |
| AMZN | $3.00 | 244.9499 |
| MA | $3.00 | 522.0085 |
| **Total** | **$78.00** | — |

The DFTX fill of **$13.50 = 4.5× $3** is only reachable on the 0x-to-5x ladder
(the former morning-only 0x-to-3x ladder capped at 3× = $9), confirming the
morning live stream now shares the canonical 0x-to-5x ladder.
