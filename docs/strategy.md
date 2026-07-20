# Graded DCA Strategy

Version-controlled backup of the current graded dollar-cost-averaging (DCA)
strategy. Human-readable and audit-friendly. **No credentials or secrets** are
stored in this repo.

Machine-readable source of truth: [`config/`](../config). This document mirrors
those files in prose.

## Overview

Each trading weekday, near the close, the strategy inspects the drop of each
active ticker versus its previous regular-session close and, if the buy signal
fires, places a single market buy sized by a graded multiplier of a **$3 base
amount**. All buys pass a review gate and respect the selected tiered
daily/weekly budget caps. It never sells.

The strategy identity is the **All Stock graded DCA live test**. Live execution
is on **Robinhood account `562057042` only**; Alpaca is read-only validation /
account context only.

## Signal rules

A buy signal fires for a ticker only when **all three** conditions hold:

1. The decision price is **below the previous regular-session close**.
2. The **60-minute intraday trend is down**.
3. The ladder **multiplier is > 0** (i.e. the drop is at least 0.1%).

The order dollar amount before budget constraints is
`base_amount_usd × multiplier` = `$3 × multiplier`.

The **morning (10:30)** and **midday (12:30)** live streams keep requiring all
three conditions, including the 60-minute downward-trend filter (condition 2),
unless changed later.

### Final buy of the day (3:20 PM ET, task `10ce362c`)

For the **final live buy of the day** the run performs a **full active-universe
scan** and, **for this final-buy decision only**, **ignores the 60-minute
downward-trend filter** (condition 2 above). Every other gate still applies:

- current / decision price **strictly below the prior completed regular-session
  close**,
- ladder **multiplier > 0** (from the $3-base 0x-to-5x ladder),
- **data-confidence** check passes,
- **`spread_ok=true`**,
- **budget / tiered-cap** pass,
- **regular trading hours only**,
- **whitelisted ticker** (active universe), and
- all **hard circuit breakers** clear.

The same-day top-up guard below still governs the final buy (see the guard
section). Dropping the trend filter widens only which tickers the final buy may
consider; it never relaxes the price-below-prior-close, budget, data-confidence,
spread, whitelist, or circuit-breaker requirements.

## Active ticker universe

43 active symbols (see [`config/tickers.json`](../config/tickers.json)):

```
DFTX TRIN AAAU SPCX EVR LUNR IRDM DXYZ LITE ASML DRAM STX
MSFT AAPL AMAT AMD CBRS MRVL BTX STM SNDK WDC TSM AVGO NVDA QBTS FLKR
INTC GOOGL GOOG AMZN GLW MA GS MU NFLX XLG TOPT QTOP SATA RGTI IBM GFUZ
```

- **Excluded:** `BYDDY`, `VCX`, `TRLV`, `RVI`
- **Symbol corrections applied:** `IRDMI -> IRDM`, `GLWR -> GLW`
- **`GFUZ` (General Fusion Group Ltd.)** added 2026-07-15 (user-approved). Uses
  **Robinhood primary data** (resolved active, tradeable, `fractional_tradability='tradable'`,
  regular-hours dollar orders, prior close available). The Finance connector lookup
  returned `NOT_FOUND`, so finance data may be unresolved/unavailable for `GFUZ`.

## Sizing ladder (latest, active)

Maps the drop vs. previous close to a buy multiplier of the $3 base amount.
Boundaries are min-inclusive, max-exclusive. There is **no upper cutoff**: any
drop of 8% or more is 5x. See [`config/sizing_ladder.json`](../config/sizing_ladder.json).

| Drop vs. prior close | Multiplier | Dollars (base $3) |
|---|---|---|
| < 0.1%          | 0x (no buy) | $0.00 |
| 0.1% to < 1%    | 1x   | $3.00 |
| 1% to < 2%      | 1.5x | $4.50 |
| 2% to < 3%      | 2x   | $6.00 |
| 3% to < 4%      | 2.5x | $7.50 |
| 4% to < 5%      | 3x   | $9.00 |
| 5% to < 6%      | 3.5x | $10.50 |
| 6% to < 7%      | 4x   | $12.00 |
| 7% to < 8%      | 4.5x | $13.50 |
| >= 8%           | 5x   | $15.00 |

### Historical / superseded ladders

Older ladder variants — **including the 0.1%-to-12% sizing ladders** — are
**historical and superseded**. The active ladder above (0.1% to 8%+, max 5x, no
upper cutoff) is the only one in force. If a request references the "0.1% to 12%
ladder," treat it as background context, not the current rule.

## Budget & tiered cap rules

Base amount is exactly **$3.00**. See
[`config/budgets.json`](../config/budgets.json) (`base_amount_policy`). Every
candidate dollar amount is **`$3.00 × multiplier`**. The automation must never
infer, discover, or fall back to **$50.00** or any broker/account default base
amount. If the base-amount config is missing or unreadable, continue using
**$3.00** and record `base_amount_source='hardcoded_user_confirmed_3_usd'`.

The daily and weekly caps are not fixed: each run selects a budget tier from the
day's planned spend and the number of **3x-or-greater signals** (buy candidates
whose ladder multiplier is >= 3.0). Pick the highest tier triggered by **either**
dimension.

| Tier | Select when | Daily cap | Weekly cap |
|---|---|---|---|
| Normal | planned spend <= $129 **and** 3x+ signals <= 5 | $180 | $900 |
| Elevated | planned spend > $129 **or** 3x+ signals 6-10 | $240 | $1,200 |
| High-volatility | planned spend > $213 **or** 3x+ signals 11-17 | $300 | $1,500 |
| Stress | planned spend > $272 **or** 3x+ signals > 17 | $360 | $1,800 |

- **Stress** capacity is usable only to the extent prior unused weekly rollover
  reserve is available.
- The **weekly rollover reserve is capped at $900**.
- Allocations are prioritized by **higher multiplier first, then larger percent
  drop, then ticker** (alphabetical) for a deterministic tie-break. Capped
  tickers are flagged.
- **One strategy order per ticker per day.**

Caps are absolute ceilings: a run never exceeds the selected daily cap, the
selected weekly cap, available buying power, or the broker/order-review limit.

### Intraday cap curve backtest (analytics only, under review)

An analytics-only backtest of an intraday cumulative daily cap curve (cumulative
cap usage limits by 10:30, 12:30, and 3:20, not per-run allocations), layered on
a $300 weekly-add rollover funding model with a $1 backtest base, is documented in
[backtest_curve.md](backtest_curve.md) with machine-readable candidate results in
[`config/backtest_intraday_cap_curve.json`](../config/backtest_intraday_cap_curve.json).
The user-highlighted candidate is 35 / 65 / 100. These are non-live modeling
candidates for review; they do not change the live base amount, ladder, or caps
above.

## Same-day prior-run offset / top-up guard

One guard governs the **morning (10:30)**, **midday (12:30)**, and **afternoon
(3:20 PM ET)** live streams as well as **manual override same-day fills**. An
earlier run must **never block** a later run. Instead, each later run treats any
earlier same-day allocation as prior-allocation context and **tops up** only when
it is buying into a lower price:

- **No earlier same-day allocation** for the ticker → normal allocation (buy the
  full afternoon desired amount).
- **Earlier allocation exists** and the afternoon decision price is **strictly
  below** the earlier run's reference price → buy only the **incremental** dollar
  difference (afternoon desired − amount already bought); $0 if that is ≤ 0.
- **Earlier allocation exists** but the afternoon price is **not below** the
  reference price → buy **$0**.
- **Earlier amount known but no comparable price** (missing prior reference or
  current afternoon price) → conservative **$0** with `top_up_manual_review`
  flagged (a `manual_review_candidate`), rather than topping up blind.

**Reference price preference.** A prior run's reference price is resolved in this
order: the run's logged `target_price`, then `decision_price`, then the confirmed
`average_fill_price` (from `orders.csv`, or a manual override's
`filled_orders.csv`). When none of the three is available the reference is
**unknown**, which routes the candidate to the conservative no-top-up /
`manual_review_candidate` path above.

Worked example: a morning run bought $3 of a ticker; the afternoon signal wants
$6. If the afternoon price is below the morning reference, the afternoon run buys
the **$3** difference. If the afternoon desired ≤ the morning amount, it buys
**$0**. If the afternoon price is not below the morning reference, it buys **$0**
even though the signal fired.

Only earlier runs generated **before 2:30 PM ET** contribute, and scheduled runs
are never read as prior context — so morning/manual orders before 2:30 PM ET are
read for top-up only, never as afternoon scheduled execution evidence,
slippage, watchdog confirmation, scheduled cost basis, or scheduled fill counts.
When multiple earlier runs touched a ticker the dollar amounts are summed and the
**lowest** available reference price wins. The scheduled run always writes a valid
scheduled `run_state` even when every buy is fully offset to $0.

Per candidate, the scheduled `run_state` records `prior_same_day_amount`,
`prior_same_day_reference_price`, `prior_same_day_run_id`, `prior_same_day_source`,
`afternoon_desired_amount`, `afternoon_decision_price`, `incremental_order_amount`,
`top_up_guard_result`, `top_up_skipped`, and `top_up_manual_review` (see
[`schemas/run_state.schema.json`](../schemas/run_state.schema.json)).

The **midday** stream applies the same top-up guard against morning/manual prior
same-day allocations (buying only the incremental difference into a lower price),
and midday fills in turn become prior-allocation context for the afternoon guard.
The morning stream ([morning_dca.md](morning_dca.md)) and the midday stream
([midday_dca.md](midday_dca.md), a 12:30 PM ET live execution) both run the same
shared $3-base 0x-to-5x ladder as the afternoon run — as of the Fri Jul 10 2026
live-automation update every live stream shares one ladder (the former morning
0x-to-3x ladder is historical/superseded). Both are independent of the afternoon
3:20 PM task and never count as afternoon scheduled evidence.

The scheduled afternoon run auto-loads prior context from its own task root
**plus** the morning-live root (`.../cron_tracking/morning_dca`) and the
midday-live root (`.../cron_tracking/midday_dca`), so morning, midday, and manual
override same-day fills all offset the afternoon buy. Roots that did not run that
day contribute nothing.

### Manual override same-day fills

Manual override reruns (e.g. an out-of-band manual buy on account 562057042) are
recorded under `.../cron_tracking/midday_dca/manual_reruns/<timestamp>/` as
`run_state.json` plus a confirmed-fill `filled_orders.csv` (`run_origin='manual'`,
`manual_run=true`). They are **excluded** from every scheduled metric — evidence,
slippage, reliability, fill counts, watchdog confirmation, and cost basis — and
are used **only** as prior-allocation context for the afternoon same-day top-up
guard. Example (2026-07-09): `AMZN` $3.00 @ 242.1099, `GOOG` $4.50 @ 352.1778,
and `GOOGL` $4.50 @ 354.9800 were manual override fills — excluded from scheduled
evidence and only affecting afternoon top-up eligibility.

#### Manual final buy override — 2026-07-13 incident

On **2026-07-13** the scheduled 3:20 PM ET run (`10ce362c`) placed **0 orders**,
and the user then requested the final buy **manually**. A final manual-review
scan ran the final-buy behavior above (full active-universe scan, trend filter
ignored) and applied the same-day top-up offsets. **34 orders filled**, planned
total **$234.00**, tickers: `DFTX TRIN SPCX LUNR IRDM DXYZ LITE ASML DRAM STX
AMAT AMD CBRS MRVL BTX STM SNDK WDC TSM AVGO NVDA QBTS FLKR INTC GOOGL GOOG GLW
GS MU XLG TOPT QTOP SATA RGTI`.

These are **manual final-buy overrides and same-day top-up context only** — they
are **not** scheduled-afternoon execution evidence. Their `run_state.json`
carries `run_type='final_manual_buy_override'`, `run_origin='manual'`,
`manual_run=true`, `valid_afternoon_scheduled_evidence=false`, and
`same_day_top_up_context_only=true`. Artifacts (run_state + `filled_orders.csv`
with all order IDs) were backfilled under
`.../cron_tracking/ce16ba6b/manual_runs/final_manual_override_20260713_1536ET/`
and `.../cron_tracking/midday_dca/manual_reruns/final_manual_override_20260713_1536ET/`.
All order IDs are **excluded** from scheduled slippage, scheduled fills,
scheduled reliability, scheduled cost basis, and watchdog confirmation, and are
surfaced only in the **"Manual Final Buy Override / Same-Day Top-Up Context"**
report section.

**Exclusion scan paths.** Downstream slippage/watchdog/reporting scan these
manual exclusion roots for final-override order IDs: `final_manual_review/**`,
`.../cron_tracking/ce16ba6b/manual_runs/final_manual_override_*`, and
`.../cron_tracking/midday_dca/manual_reruns/` (in addition to the task's own
`manual_runs/` root). See
[run_classification.md](run_classification.md) and
[`config/run_classification.json`](../config/run_classification.json).

### Raw connector-input persistence (added 2026-07-10)

Before computing any signal, the scheduled 3:20 PM ET run persists every raw
Robinhood/Finance/Alpaca/Yahoo connector response it used under
`scheduled_runs/YYYYMMDD_1520ET/raw/` (deterministic filenames
`quotes_batchN.json`, `historicals_batchN.json`, `tradability_pre_place.json`,
`portfolio_pre_place.json`, `orders_after_place.json`, `place_SYMBOL.json`),
normalizing large connector `output_path` pointers into parseable JSON, and
computes from those persisted files only — never in-memory tool responses. If
persistence/normalization fails it still writes a valid scheduled `run_state`
(`order_submit_status='not_submitted_error'`, no orders) instead of escalating
without state. See
[run_classification.md](run_classification.md#raw-connector-input-persistence-added-2026-07-10).

The **2026-07-10 3:20 PM run** was recovered under this fix (re-fetch, persist,
normalize, compute, place): **9 incremental scheduled afternoon orders totalling
$31.50** under
`/home/user/workspace/cron_tracking/ce16ba6b/scheduled_runs/20260710_1520ET/`,
`recovery_from_escalation=true` — valid afternoon scheduled evidence.

## Execution constraints

See [`docs/safeguards.md`](safeguards.md) for the full list. Summary:

- Only broker/account for live/manual runs: **Robinhood Agentic account `562057042`**.
- Alpaca is read-only validation / account context only (unless explicitly changed later).
- One strategy order per ticker per day.
- Market buy orders with `dollar_amount` notional and `time_in_force = gfd`, only after `review_equity_order` passes.
- No sells, options, crypto, margin expansion, order replacement, account changes, or trading outside regular hours.
- Test window approved through **Fri Aug 28, 2026 inclusive**.
