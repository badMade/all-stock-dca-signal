# Midday DCA Stream

The **midday** DCA stream is a single **12:30 PM ET live Robinhood execution**
that grades each ticker's price weakness against **yesterday's regular-session
close** on the shared **$3-base 0x-to-5x** live ladder (as of the Fri Jul 10 2026
live-automation update every live stream — morning, midday, afternoon — shares
this one ladder; the former morning-only 0x-to-3x ladder is historical/superseded).
It is implemented by [`midday_dca.MiddayDCAExecutor`](../midday_dca.py) and
governed by [`config/midday_dca.json`](../config/midday_dca.json).

| Mode | Task | ET | Trades | run_type | schema | Artifact root |
|---|---|---|---|---|---|---|
| `live` | `a6239dae` | 12:30 PM | **yes** | `midday_dca_live` | `midday-live-v1` | `.../cron_tracking/midday_dca/` |

`MiddayDCAExecutor` subclasses `MorningDCAExecutor` and reuses the entire
evaluate/build/write pipeline, overriding only the stream-identity attributes and
adding the same-day top-up guard.

## 12:30 PM live Robinhood execution

Uses **Robinhood account `562057042`** as the sole execution broker (Alpaca stays
read-only for validation/context), the **$3 base afternoon 0x-to-5x ladder**, the
same active universe (excludes BYDDY/VCX/TRLV/RVI), and its own `midday` budget
ledger. Writes under `.../cron_tracking/midday_dca/YYYYMMDD_1230ET/`
(`run_id='midday_live_YYYYMMDD_1230ET'`) and updates that root's
`latest_midday_run.json`. The 12:30 PM decision time gives a real intraday trend
window.

### Confidence rule (no trend, no auto-buy)

A candidate whose price/drop passes the ladder is auto-bought **only when the
intraday trend can be validated** — at least `trend_min_bars` (default **6**)
usable intraday bars (finite, positive close, timestamp at or before the 12:30
decision time). If price/drop passes but the intraday trend **cannot** be
validated, the run does **not** auto-buy; the candidate is classified
`execution_status='manual_review_candidate'` and `trend_validated=false`. There is
**no** loosened trend-unavailable execution path.

## Same-day top-up guard (prior-allocation context only)

Morning **live** and **manual** same-day buys are **prior-allocation context
only** — never midday scheduled evidence. Per ticker the guard applies:

- **No earlier same-day allocation** → normal allocation (buy the full midday
  desired amount).
- **Earlier allocation and midday decision price strictly below** the earlier
  run's reference price → buy only the **incremental** dollar difference
  (midday desired − amount already bought); `$0` if that is `≤ 0`.
- **Earlier allocation but midday price not below** the reference price → `$0`.
- **Earlier amount known but no comparable price** (missing prior reference or
  current midday price) → conservative `$0` with `top_up_manual_review` flagged
  (a `manual_review_candidate`).

**Reference price preference.** A prior run's reference price is resolved in this
order: the run's logged `target_price`, then `decision_price`, then the confirmed
`average_fill_price` (from `orders.csv`, or a manual override's
`filled_orders.csv`). A missing reference (none available) → no automatic top-up
(`manual_review_candidate`).

Only earlier runs generated **before 2:30 PM ET** contribute; scheduled afternoon
runs are excluded as prior context. Prior allocations are scanned across
`prior_allocation_roots` (default the morning-live root); when a ticker appears in
more than one root the amounts are summed and the lowest reference price wins, so
midday must beat the best earlier entry before it tops up.

Per-candidate the run_state records `prior_same_day_amount`,
`prior_same_day_reference_price`, `prior_same_day_run_id`/`prior_same_day_source`,
`midday_desired_amount`, `midday_decision_price`, `incremental_order_amount`,
`top_up_guard_result`, `top_up_skipped`, and `top_up_manual_review`. The run also
lists `top_up_offset_tickers` and `top_up_manual_review_tickers`.

## Why it is fully separate from the other streams

The midday stream is **independent** of the afternoon 3:20 PM live task
(`10ce362c`, which supersedes the deleted `ce16ba6b`) and of the morning stream:

| Axis | Afternoon live (`10ce362c`) | Morning stream | Midday stream |
|---|---|---|---|
| Ladder | 0x → **5x** | 0x → **3x** | 0x → **5x** |
| Budget ledger | `StrategyState` / `BUDGET_TIERS` | namespace `morning` | namespace `midday` |
| Artifact root | `.../ce16ba6b/` | `.../morning_dca[_watchlist]/` | `.../midday_dca/` |
| Run type | `scheduled_live_test` @ `15:20` | `morning_dca_*` @ `09:45`/`10:30` | `midday_dca_live` @ `12:30` |
| Pointer | `latest_scheduled_run.json` | `latest_morning_run.json` | `latest_midday_run.json` |

The midday stream never writes into the morning or `ce16ba6b` trees, never
updates `latest_scheduled_run.json`, and its `run_classification` always carries
`stream="midday"` and `valid_afternoon_evidence=false`. Afternoon consumers
(`67aa802b`, `a46bf007`, `041d9d90`, `0656657a`) glob only
`.../ce16ba6b/scheduled_runs/**`, so they never traverse the midday root.

**Prior-allocation context only:** midday fills are read by the afternoon
**same-day top-up guard** as prior-allocation context, but never as afternoon
scheduled evidence, slippage, watchdog confirmation, scheduled cost basis, or
scheduled fill counts. See
[strategy.md](strategy.md#same-day-prior-run-offset--top-up-guard).

## Manual override reruns

An out-of-band manual buy of the midday stream on account `562057042` is written
under `.../cron_tracking/midday_dca/manual_reruns/<timestamp>/` as `run_state.json`
plus a confirmed-fill `filled_orders.csv`. Such a run is a **manual invocation** of
the same executor: `run_origin='manual'`, `manual_run=true`, `run_type` stays
`midday_dca_live`, and it uses a non-scheduled `run_id`
(`manual_midday_live_YYYYMMDD_HHMMSSZ_<suffix>`), so it validates against the
manual branch of
[`schemas/midday_run_classification.schema.json`](../schemas/midday_run_classification.schema.json).

Manual override reruns are **excluded** from every scheduled metric — evidence,
slippage, reliability, fill counts, watchdog confirmation, and cost basis — and
are used **only** as prior-allocation context for the **afternoon** 3:20 PM
same-day top-up guard (the afternoon guard scans the midday root, which contains
`manual_reruns/`). They do **not** affect the midday run itself (they post-date the
12:30 PM decision).

Example — 2026-07-09 manual override fills (excluded from scheduled evidence,
affecting afternoon top-up eligibility only):

| Ticker | Amount | Avg fill price |
|---|---|---|
| `AMZN` | $3.00 | 242.1099 |
| `GOOG` | $4.50 | 352.1778 |
| `GOOGL` | $4.50 | 354.9800 |

## Midday sizing ladder (0x → 5x)

Drop is measured as a fraction below yesterday's regular-session close. The
largest tier threshold ≤ the actual drop wins.

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

Dollars = `base_amount_usd (default $3) × multiplier` (actual order size,
subject to the top-up guard offset).

## Budget ledger

The midday ledger is a standalone budget session (namespace `midday`) tracking
only midday notional, independent of both the morning ledger and the afternoon
`StrategyState` / `BUDGET_TIERS` ledger:

- `midday_daily_cap_usd` (default `90.0`)
- `midday_weekly_cap_usd` (default `450.0`)

Exceeding a cap adds a warning to the run artifact; midday buys never consume or
validate the morning or afternoon budget.

## Artifacts

A scheduled midday run writes to `.../cron_tracking/midday_dca/YYYYMMDD_1230ET/`:

- `run_state.json` — classification, ladder, budget, signal rows, buy
  candidates (with top-up fields), warnings.
- `midday_signals.csv` — one row per ticker.

and updates that root's `latest_midday_run.json`. Manual runs write under a
distinct `manual_midday_live_*` run id and never update the pointer.

## Run classification

Validated by
[`schemas/midday_run_classification.schema.json`](../schemas/midday_run_classification.schema.json).
A scheduled live run:

```json
{
  "run_origin": "scheduled",
  "run_type": "midday_dca_live",
  "stream": "midday",
  "trades": true,
  "cron_id": "a6239dae",
  "supersedes_cron_id": null,
  "scheduled_for_et": "12:30",
  "scheduled_window_start_et": "12:25",
  "scheduled_window_end_et": "12:35",
  "manual_run": false,
  "run_id": "midday_live_YYYYMMDD_1230ET",
  "state_schema_version": "midday-live-v1",
  "valid_afternoon_evidence": false
}
```

## Usage

```python
from datetime import datetime
from midday_dca import MiddayConfig, MiddayDCAExecutor
from near_close_dca import EASTERN

live = MiddayDCAExecutor(MiddayConfig(tickers=("AAPL", "MSFT", "NVDA")))

now = datetime.now(tz=EASTERN)
result = live.run(now, robinhood_data_provider)
```
