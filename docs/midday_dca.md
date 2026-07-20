# Midday DCA Stream

The **midday** DCA stream is a single **12:30 PM ET live Robinhood execution**
that grades each ticker's price weakness against **yesterday's regular-session
close** on the shared **$2-base 0x-to-5x** live ladder (as of the Fri Jul 10 2026
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
read-only for validation/context), the **$2 base afternoon 0x-to-5x ladder**, the
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
`prior_allocation_roots` (the morning-live root **and** the manual missed-morning
recovery root `.../cron_tracking/morning_dca/manual_recovery/YYYYMMDD_*`); when a
ticker appears in more than one root the amounts are summed and the lowest
reference price wins, so midday must beat the best earlier entry before it tops
up.

**Missed 10:30 morning run recovery (added Jul 20 2026).** When the 10:30
scheduled morning live task did not run and a **manual** recovery placed the
missed buys under `.../cron_tracking/morning_dca/manual_recovery/YYYYMMDD_*`
(e.g. `20260720_1048ET`), the 12:30 midday run **scans that path** and folds any
same-day manual recovery fills into this top-up guard as prior allocation. Those
manual artifacts (`run_origin='manual'`, `manual_run=true`,
`same_day_top_up_context_only=true`, `valid_scheduled_evidence=false`) are
prior-allocation / top-up context **only** — never proof the 10:30 scheduled task
ran and never midday scheduled evidence. See
[morning_dca.md](morning_dca.md#manual-missed-run-recovery-added-jul-20-2026).

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

### Manual missed-12:30 recovery (added Jul 20 2026)

Separate from a manual **override** rerun, this covers the case where the **12:30
scheduled midday task did not run or failed**. A human may then place the missed
midday buys by hand under a dedicated path:

- Pattern: `/home/user/workspace/cron_tracking/midday_dca/manual_recovery/YYYYMMDD_*`
- Example: `/home/user/workspace/cron_tracking/midday_dca/manual_recovery/20260720_1259ET`
  (Jul 20 2026: placed at 12:59 ET after the 12:30 scheduled midday run failed).

Fills in that recovery:

| Ticker | Amount | Avg fill price |
|---|---|---|
| `AAPL` | $1.00 | 324.389900 |
| `TRIN` | $2.00 | 17.399900 |
| `ASML` | $2.00 | 1739.506300 |
| `IRDM` | $2.00 | 46.669900 |

The artifact carries `run_origin='manual'`, `manual_run=true`,
`same_day_top_up_context_only=true`, and `valid_scheduled_evidence=false`. It is
prior-allocation / same-day top-up context **only** and must be available to the
**3:20 afternoon** run as such. It is **never** midday scheduled evidence, never
proof the 12:30 scheduled task ran, and never counted in scheduled reliability,
fill counts, slippage, cost basis, or watchdog confirmation. See
`config/scheduled_tasks.json`'s `midday_dca_manual_recovery_2026_07_20`.

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

Dollars = `base_amount_usd (default $2) × multiplier` (actual order size,
subject to the top-up guard offset).

## Budget ledger

The midday ledger is a standalone budget session (namespace `midday`) tracking
only midday notional, independent of both the morning ledger and the afternoon
`StrategyState` / `BUDGET_TIERS` ledger:

- `midday_daily_cap_usd` (default `90.0`)
- `midday_weekly_cap_usd` (default `450.0`)

Exceeding a cap adds a warning to the run artifact; midday buys never consume or
validate the morning or afternoon budget.

## Raw input generation and failure-state guarantee (added Jul 20 2026)

After the Jul 20 2026 12:30 run escalated (it invoked a saved runner without
first generating the same-day raw inputs and wrote no failure-state artifact),
the scheduled 12:30 task must:

**Generate raw inputs first.** Before invoking any saved runner such as
`.../cron_tracking/a6239dae/run_midday_live.sh` or `midday_live_runner.py`, the
task **creates** `.../cron_tracking/midday_dca/YYYYMMDD_1230ET/raw/` and persists
all required raw inputs for that same-day run:

- `raw/portfolio.json`
- `raw/quotes.json` (or clearly named quote batches normalized into
  `raw/quotes.json`)
- historical / trend files needed for the 60-minute trend check

If the runner requires a `RUN_DATE_ET` argument, pass the current
America/New_York date as `YYYYMMDD`. The task **must not** assume
`raw/quotes.json` or historicals already exist from a prior run. Inputs come from
the Finance connector primary, Alpaca and Robinhood validation / context, and
`yfinance` fallback **only** when connector data is unavailable, stale,
incomplete, or unresolved (see [safeguards.md](safeguards.md#market-data-source-policy)).
If raw input generation fails once, perform **one** bounded recovery attempt with
the alternate approved data source path, then stop.

**Always write the state files.** The 12:30 scheduled run **always** writes
`run_state.json`, `run_summary.json`, and `latest_midday_run.json` under the
midday root, **even when no orders are placed or execution is blocked**. If raw
inputs are missing, malformed, stale, or not generated, it writes a **valid
scheduled** midday `run_state` with `run_origin='scheduled'`, `manual_run=false`,
`stream='midday'`, `scheduled_for_et='12:30'`, `cron_id='a6239dae'`,
`valid_afternoon_evidence=false`, `order_submit_status='not_submitted_error'`,
`error_class='missing_or_invalid_raw_inputs'`, `orders_submitted_count=0`, and a
compact list of missing files or failed data sources. This failure-state artifact
is valid proof the 12:30 scheduled task **fired**, but it is **not** order or fill
evidence. The task **must not** escalate without writing this state file unless
the workspace itself is unavailable.

**Missed-morning recovery interaction.** When same-day manual missed-10:30
recovery artifacts exist under
`.../cron_tracking/morning_dca/manual_recovery/YYYYMMDD_*`, load them **after**
raw market data is available and **before** order sizing. They remain
prior-allocation / top-up context only: they **must not** satisfy raw quote or
historical input requirements, **must not** be copied into the scheduled evidence
stream, and **must not** be counted as proof the 10:30 scheduled task ran. See
[run_classification.md](run_classification.md#manual-missed-morning-recovery-added-2026-07-20).

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
