# Graded DCA Strategy

Version-controlled backup of the current graded dollar-cost-averaging (DCA)
strategy. Human-readable and audit-friendly. **No credentials or secrets** are
stored in this repo.

Machine-readable source of truth: [`config/`](../config). This document mirrors
those files in prose.

## Overview

Each trading weekday, near the close, the strategy inspects the drop of each
active ticker versus its previous regular-session close and, if the buy signal
fires, places a single market buy sized by a graded multiplier of a **dynamic
first-allocation base amount**. The base is selected before the ladder: below the
prior close **and** the prior regular-session low uses a **$2 base**; below the
prior close **only** (not below the prior low) uses a reduced **$1 base**; not
below the prior close is **no buy**. All buys pass a review gate and respect the
selected tiered daily/weekly budget caps. It never sells.

The strategy identity is the **All Stock graded DCA live test**. Live execution
is on **Robinhood account `<ROBINHOOD_ACCOUNT_NUMBER>` only**; Alpaca is read-only validation /
account context only.

## Signal rules

A buy signal fires for a ticker only when **all three** conditions hold:

1. The decision price is **below the previous regular-session close**.
2. The **60-minute intraday trend is down**.
3. The ladder **multiplier is > 0** (i.e. the drop is at least 0.1%).

The order dollar amount before budget constraints is
`selected_base_amount_usd × multiplier`, where `selected_base_amount_usd` is the
dynamic first-allocation base: **$2** when the decision price is below the prior
close **and** the prior regular-session low, or **$1** when it is below the prior
close **only**. A missing prior low is treated conservatively as not-below-low
(the **$1** scale).

The **morning (10:30)** and **midday (12:30)** live streams keep requiring all
three conditions, including the 60-minute downward-trend filter (condition 2),
unless changed later.

### Final buy of the day (3:30 PM ET, task `10ce362c`)

For the **final live buy of the day** the run performs a **full active-universe
scan** and, **for this final-buy decision only**, **ignores the 60-minute
downward-trend filter** (condition 2 above). Every other gate still applies:

- current / decision price **strictly below the prior completed regular-session
  close**,
- ladder **multiplier > 0** (from the dynamic-base 0x-to-8x ladder),
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

67 active symbols (see [`config/tickers.json`](../config/tickers.json)):

```text
DFTX TRIN AAAU SPCX EVR LUNR IRDM DXYZ LITE ASML DRAM STX
MSFT AAPL AMAT AMD CBRS MRVL BTX STM SNDK WDC TSM AVGO NVDA QBTS FLKR
INTC GOOGL GOOG AMZN GLW MA GS MU NFLX XLG TOPT QTOP SATA RGTI IBM GFUZ TXN RKLB
GE VRT CAT QQQ QCOM ABBV JPM VT RVI VCX TRLV LLY TSLA
VIAV CIEN ANET CSCO FFIV COHR QUBT APH TEL
```

- **Excluded:** `QBUT` -- added 2026-08-16 without an in-repo Robinhood instrument search; Robinhood returned not-found for it on every live run since, so it was excluded 2026-09-18 (no confirmed buying mechanism at all -- see `exclusion_reasons.QBUT` in `config/tickers.json`). QBUT was a misspelling of `QUBT` (Quantum Computing Inc.), added to the active list 2026-09-24 after Robinhood resolved it; QBUT stays excluded as a guard against stale ticker lists.
- **Whole-share tickers:** `BYDDY` and `RVII` -- not tradable for fractional/notional buys on Robinhood, so they are evaluated by the same ladder but bought via whole-share accumulation instead (see [whole_share_accumulation.md](whole_share_accumulation.md)). `RVII` moved here from the active list on 2026-08-24.
- **Symbol corrections applied:** `IRDMI -> IRDM`, `GLWR -> GLW`, `QBUT -> QUBT`
- **`GFUZ` (General Fusion Group Ltd.)** added 2026-07-15 (user-approved). Uses
  **Robinhood primary data** (resolved active, tradeable, `fractional_tradability='tradable'`,
  regular-hours dollar orders, prior close available). The Finance connector lookup
  returned `NOT_FOUND`, so finance data may be unresolved/unavailable for `GFUZ`.

> **TICKER UNIVERSE UPDATE JUL 19 2026:** TXN is added to the active DCA
> universe and GFUZ remains active. Robinhood resolved TXN as Texas Instruments
> Incorporated Common Stock, state active, tradeable true, fractional
> tradability tradable, regular-hours dollar orders supported, and prior close
> available. Finance lookup resolved TXN as Texas Instruments Incorporated.
> Existing exclusions remain BYDDY, VCX, TRLV, and RVI; corrected symbols IRDM
> and GLW remain; IBM and GFUZ remain included (GFUZ with Robinhood primary if
> finance is unresolved).
>
> **TICKER UNIVERSE UPDATE JUL 27 2026:** RKLB (Rocket Lab Corporation) is
> added to the active DCA universe. Robinhood resolved RKLB as Rocket Lab
> Corporation Common Stock, state active, tradeable true, fractional
> tradability tradable, regular-hours dollar orders supported, and prior close
> available ($63.91 as of 2026-07-24). Existing exclusions remain BYDDY, VCX,
> TRLV, and RVI; corrected symbols IRDM and GLW remain; IBM, GFUZ, and TXN
> remain included.
>
> **TICKER UNIVERSE UPDATE AUG 2 2026:** `GE`, `VRT`, `CAT`, `QQQ`, `QCOM`,
> `ABBV`, and `JPM` are added to the active DCA universe. The active universe
> is now **52 symbols**. `DJX` ("Dow Jones Industrial Average Mini Index") was
> considered and excluded -- Robinhood instrument search returns no equity/ETF
> match for it, only a market-index result, so it cannot be traded through the
> account's equity-order path. Existing exclusions remain BYDDY, VCX, TRLV,
> and RVI; corrected symbols IRDM and GLW remain; IBM, GFUZ, TXN, and RKLB
> remain included. This adds whitelisted symbols only; it preserves all
> existing trading safety, pre-authorization, top-up, run-classification,
> budget, cost-conscious, manual-exclusion, and recovery text.
>
> **TICKER UNIVERSE UPDATE AUG 4 2026:** `VT` (Vanguard Total World Stock ETF)
> is added to the active DCA universe, taking it to **53 symbols**. Read this
> one differently from the additions above: every earlier symbol was confirmed
> by a Robinhood instrument search before it was whitelisted, and `VT` was not
> — the session that added it had no broker access, so `VT`'s state,
> tradability, fractional tradability, and prior-close availability are
> **unverified**. Nothing in the code assumes `VT` resolves; the per-run
> preflight and data-confidence gates admit or gate it out fail-closed exactly
> as they would any other symbol, so an unresolvable `VT` costs a gated-out
> ticker, not a bad order. Confirm it on the first live run. `VT` is also
> absent from `tickers.txt` (the history-fetch universe), so it has no
> calibrated sizing and uses the global fallback ladder until history is
> fetched and `scripts/generate_per_ticker_sizing.py` is re-run. Existing
> exclusions remain BYDDY, VCX, TRLV, and RVI; corrected symbols IRDM and GLW
> remain; IBM, GFUZ, TXN, RKLB, and the 2026-08-02 additions remain included.
> This adds a whitelisted symbol only; it preserves all existing trading
> safety, pre-authorization, top-up, run-classification, budget,
> cost-conscious, manual-exclusion, and recovery text.
>
> **TICKER UNIVERSE UPDATE AUG 16 2026 (user-requested):** `QBUT` and `RVII`
> are added to the active DCA universe as new symbols. `RVI` and `VCX` are
> also added, **reversing the AUG 5 2026 exclusion decision** above at the
> owner's explicit request (confirmed interactively when this session
> flagged the conflict with that decision), taking the active universe to
> **57 symbols**. As with `VT`, no Robinhood instrument search was run
> in-repo for any of the four — this session had no broker access — so
> their state, tradability, fractional tradability, and prior-close
> availability are **unverified**; the run-time preflight and
> data-confidence gates admit or gate each out fail-closed exactly as they
> would any other symbol. None of the four are in `tickers.txt` (the
> history-fetch universe), so they have no calibrated sizing and use the
> global fallback ladder until history is fetched and
> `scripts/generate_per_ticker_sizing.py` is re-run. `TRLV` remains
> excluded; `BYDDY` remains excluded separately for its ADR wind-down
> status.
>
> **TICKER UNIVERSE UPDATE AUG 16 2026, SECOND UPDATE (user-requested):**
> `TRLV` is added to the active DCA universe, reversing the last piece of
> the AUG 5 2026 exclusion decision above and leaving `BYDDY` as the
> **only** excluded symbol, taking the active universe to **58 symbols**.
> As with the first AUG 16 2026 update, no Robinhood instrument search was
> run in-repo for `TRLV`; it is unverified and admitted or gated out
> fail-closed by the run-time preflight/data-confidence gates. `TRLV` is
> not in `tickers.txt`, so it uses the global fallback sizing ladder until
> history is fetched and `scripts/generate_per_ticker_sizing.py` is
> re-run. Separately, `BYDDY`'s documented exclusion reason is now
> **fractional-tradability** — Robinhood resolves it as not tradable for
> fractional/notional buys, so its normal graded-ladder buy would always
> be blocked at the `is_tradable_for_fractional_buy` gate — per explicit
> user instruction. See
> [`config/tickers.json`](../config/tickers.json)'s `exclusion_reasons`
> and [whole_share_accumulation.md](whole_share_accumulation.md) for the
> opt-in path built to track `BYDDY`'s would-be buys and fire a single
> whole-share purchase instead.
>
> **TICKER UNIVERSE UPDATE AUG 24 2026 (user-requested):** `RVII` moves out of
> the active DCA universe into the **whole-share** category, taking the active
> universe back to **57 symbols** and making `BYDDY` and `RVII` the two
> whole-share symbols. `RVII` cannot be bought in fractional/notional dollar
> amounts, so its ordinary dollar-amount graded-ladder order would always be
> blocked at the `is_tradable_for_fractional_buy` gate — exactly the condition
> the whole-share path exists for. This is **not** an exclusion: `RVII` is
> still evaluated by the same ladder, still appears in `signals.csv`, and is
> still bought; only the order mechanism changes to accumulate-then-fire-one-
> whole-share (see [whole_share_accumulation.md](whole_share_accumulation.md),
> whose **Scope** section still limits the wiring to the 3:30 PM canonical
> stream). `RVII`'s unverified-instrument caveat from the first AUG 16 2026
> update and its global-fallback sizing entry both still stand.

## Sizing ladder (latest, active)

Maps the drop vs. previous close to a buy multiplier, applied to the selected
first-allocation base ($2 below prior close and low, $1 below prior close only).
Boundaries are min-inclusive, max-exclusive. There is **no upper cutoff**: any
drop of 16% or more is 8x. See [`config/sizing_ladder.json`](../config/sizing_ladder.json).

| Drop vs. prior close | Multiplier | Dollars ($2 base) | Dollars ($1 base) |
| --- | --- | --- | --- |
| < 0.1% | 0x (no buy) | $0.00 | $0.00 |
| 0.1% to < 0.5% | 1x | $2.00 | $1.00 |
| 0.5% to < 1% | 1.5x | $3.00 | $1.50 |
| 1% to < 1.5% | 2x | $4.00 | $2.00 |
| 1.5% to < 2% | 2.5x | $5.00 | $2.50 |
| 2% to < 2.75% | 3x | $6.00 | $3.00 |
| 2.75% to < 3.5% | 3.5x | $7.00 | $3.50 |
| 3.5% to < 4.5% | 4x | $8.00 | $4.00 |
| 4.5% to < 5.5% | 4.5x | $9.00 | $4.50 |
| 5.5% to < 7% | 5x | $10.00 | $5.00 |
| 7% to < 8.5% | 5.5x | $11.00 | $5.50 |
| 8.5% to < 10.5% | 6x | $12.00 | $6.00 |
| 10.5% to < 12.5% | 6.5x | $13.00 | $6.50 |
| 12.5% to < 14.5% | 7x | $14.00 | $7.00 |
| 14.5% to < 16% | 7.5x | $15.00 | $7.50 |
| >= 16% | 8x | $16.00 | $8.00 |

**Rescaled 2026-10-04 (log-scaled).** The curve is now log-scaled across 0.1% to 16%:
fifteen tiers from 1x to 8x in half-step increments, dense on shallow dips and
widening into deep drops, with no upper cutoff (>= 16% is 8x). It replaced the
2026-08-02 curve (0.1%-to-8%+, max 5x). The per-order ceiling is now $16
(8x x $2; the $1 scale tops out at $8) -- see `max_per_order_usd` in
`config/budgets.json`. Budget-tier selection still counts **3x-or-greater**
signals; 3x now needs a 2% drop.

> **This table is the fallback, not the live curve.** [`config/per_ticker_sizing.json`](../config/per_ticker_sizing.json)
> is `status: active` and carries an entry for every active symbol plus the
> whole-share symbols (which the ladder evaluates identically). Since 2026-10-05 it
> is built by `scripts/generate_per_ticker_sizing.py` from each ticker's **last 90
> days** of adjusted prior-close-to-low drops: one tier per percentile from the
> **5th to the 100th** (96 tiers), on a log-scaled curve that is 1.00x through
> the 10th percentile, 2.46x at the 50th, 8.00x at the 95th and 10.00x at the
> 100th (the window's single largest drop). The base is $1.00, so a ticker's top
> tier is at most $10. 66 symbols are calibrated this way; the ones in
> `fallback_tickers` (currently only RVII, under the 40-session minimum) carry a
> copy of the global ladder above. `graded_buy_multiplier` reads a ticker's
> calibrated tiers first and falls back to the shared table only for symbols
> with no entry; its ceiling is 10x. The table above therefore governs the
> fallback path and the test suite, while per-ticker calibration governs live
> sizing.
>
> **Per-ticker multipliers are interpolated, not stepped.** A calibrated entry
> carries `"interpolation": "linear"`, and the 96 tier points are treated as
> samples of one smooth curve: a drop between two thresholds earns the
> proportional blend of their two multipliers
> (`near_close_dca.interpolate_tier_multiplier`, rounded to 4 dp), so a slightly
> bigger drop never waits for the next step. Below the 5th-percentile
> threshold there is no buy, and beyond the 100th the multiplier clamps at 10x.
> Fallback tickers and the global ladder stay stepped. `dca_runner`'s
> candidate-evidence validation (`_configured_calibration_bounds`) binds an
> in-between multiplier to the segment that can produce it.
>
> The close-window "what would have fired" report (`scripts/close_window_signal.py`,
> shown in the console) grades flagged tickers the same way, so its per-ticker
> multipliers agree with live sizing between tier points.

### Historical / superseded ladders

Older ladder variants — **including the 0.1%-to-12% sizing ladders and the
uniform 1%-step 0.1%-to-8% ladder this one replaced** — are **historical and
superseded**. If a request references the "0.1% to 12% ladder," treat it as
background context, not the current rule.

## Budget & tiered cap rules

The first-allocation base is dynamic: **$2.00** when the decision price is below
the prior close **and** the prior regular-session low, **$1.00** when below the
prior close **only** (a missing prior low is treated as below-close-only), and
**no buy** when not below the prior close. See
[`config/budgets.json`](../config/budgets.json) (`base_amount_policy`). Every
candidate dollar amount is **`selected_base_amount_usd × multiplier`**. The
automation must never infer, discover, or fall back to **$50.00** or any
broker/account default base amount. If the base-amount config is missing or
unreadable, continue using **$2.00** (below close and low) or **$1.00** (below
close only) as hardcoded, user-confirmed fallbacks.

> **Dynamic first-allocation base (Jul 24 2026):** the user approved a dynamic
> base for the first same-day allocation per ticker: **$2** below the prior close
> and prior low, **$1** below the prior close only, no buy otherwise. The order
> amount is `selected_base × multiplier`. The max per order is now $16
> (8x x $2, since the 2026-10-04 ladder); the $1 scale tops out at $8. Ladder multipliers, the ticker
> universe, exclusions, budget caps, the top-up guard, pre-authorization,
> confidence, recovery, and all other safety rules are unchanged.
>
> **Base-amount change (Jul 19 2026):** the user changed all future DCA
> scheduled runs from a $3 base amount to a $2 base amount. Superseded by the
> dynamic first-allocation base above; retained for history.

The daily and weekly caps are not fixed: each run selects a budget tier keyed
**solely** on the number of **3x-or-greater signals** (buy candidates whose
ladder multiplier is >= 3.0) in the Gate 7 candidate list -- a value fixed
before sizing runs, not an output of the sizing decision. Tier selection never
keys on planned spend.

| Tier | Select when | Daily cap | Weekly cap |
| --- | --- | --- | --- |
| Normal | 3x+ signals <= 5 | $180 | $900 |
| Elevated | 3x+ signals 6-10 | $240 | $1,200 |
| High-volatility | 3x+ signals 11-17 | $300 | $1,500 |
| Stress | 3x+ signals > 17 | $360 | $1,800 |

- **Stress** capacity is usable only to the extent prior unused weekly rollover
  reserve is available.
- The **weekly rollover reserve is capped at $900**.
- Allocations are prioritized by **higher multiplier first, then larger percent
  drop, then ticker** (alphabetical) for a deterministic tie-break. Capped
  tickers are flagged.
- **One strategy order per ticker per stream per day.** The same ticker may
  still be bought again later the same day by a *different* stream, subject to
  the same-day top-up guard below — `config/budgets.json`'s
  `one_order_per_ticker_per_day` is deliberately `false`; only
  `one_order_per_ticker_per_stream` is enforced.
- **Max 8 buy attempts per day, account-wide** — shared across every stream and
  manual buying combined; counts attempts (including rejected/cancelled orders),
  not just fills. See `config/budgets.json`'s `circuit_breakers.max_orders_per_day`.

Caps are absolute ceilings: a run never exceeds the selected daily cap, the
selected weekly cap, available buying power, or the broker/order-review limit.

### Intraday cap curve backtest (analytics only, under review)

An analytics-only backtest of an intraday cumulative daily cap curve (cumulative
cap usage limits by 10:30, 12:30, and 3:30, not per-run allocations), layered on
a $300 weekly-add rollover funding model with a $1 backtest base, is documented in
[backtest_curve.md](backtest_curve.md) with machine-readable candidate results in
[`config/backtest_intraday_cap_curve.json`](../config/backtest_intraday_cap_curve.json).
The user-highlighted candidate is 35 / 65 / 100. These are non-live modeling
candidates for review; they do not change the live base amount, ladder, or caps
above.

## Same-day prior-run offset / top-up guard

> **Read this first (added 2026-08-03): there are TWO top-up guards, and the
> one described in this section failed OPEN in production.**
>
> **The incident (2026-07-30/31).** The filesystem-based guard described below
> resolves prior same-day allocations by scanning run-state roots on disk. When
> a state root was **missing** — not empty, but absent — the scan read that as
> "no prior allocation for this ticker" and authorized a **full-size repeat
> buy** instead of an incremental top-up. It failed toward buying more, not
> less. `config/scheduled_tasks.json`'s
> `same_day_top_up_guard_enforcement_2026_07_31` block records this.
>
> **The replacement.** `scripts/same_day_top_up_gate.py` sources prior same-day
> allocation from **broker truth** (Robinhood `get_equity_orders` pages) rather
> than from the filesystem, and fails **closed** on incomplete order history
> (`BROKER_HISTORY_INCOMPLETE`) instead of treating absence as zero.
>
> **What is actually running today — be precise about this.** The gate is a
> **standalone CLI**, invoked as
> `python3 -m scripts.same_day_top_up_gate --account … --trading-day …
> --orders-pages … --candidates …`. It is called by the **external executor
> prompts**, NOT from any in-repo Python: `market_open_dca.py`,
> `morning_dca.py`, `midday_dca.py`, `intraday_dca.py`, `near_close_dca.py`,
> and `run_dca.py` contain **zero calls to it** (grep-verified 2026-08-03).
> Every in-repo executor still uses the original filesystem `rglob` guard
> described in the rest of this section — the one with the fail-open bug.
>
> **Practical consequence.** Whether a given run is protected by the fix
> depends entirely on *who invoked it*: an external prompt that runs the gate
> CLI is protected; any in-repo Python path is not. Wiring the gate into
> `near_close_dca.evaluate_top_up_guard` is a live-money behavior change and is
> tracked as an explicit follow-up, not done implicitly.

One guard governs the **morning (10:30)**, **midday (12:30)**, and **afternoon
(3:30 PM ET)** live streams as well as **manual override same-day fills**. An
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

Worked example: a morning run bought $2 of a ticker; the afternoon signal wants
$4. If the afternoon price is below the morning reference, the afternoon run buys
the **$2** difference. If the afternoon desired ≤ the morning amount, it buys
**$0**. If the afternoon price is not below the morning reference, it buys **$0**
even though the signal fired.

Only earlier runs generated **before 2:30 PM ET** contribute, and scheduled runs
are never read as prior context — so morning/manual orders before 2:30 PM ET are
read for top-up only, never as afternoon scheduled execution evidence,
slippage, watchdog confirmation, scheduled cost basis, or scheduled fill counts.
When multiple earlier runs touched a ticker the dollar amounts are summed and that
ticker's **most recent** valid reference price wins (near_close_dca loader: by `generated_at` when parseable; manual CSV loader: by `generated_at` when parseable, with ties/untimestamped runs deferring to later scan/merge order).
A manual fill folded in last is
treated as the most recent reference. Any unknown contributing reference still
poisons the aggregate to unknown. The scheduled run always writes a valid
scheduled `run_state` even when every buy is fully offset to $0.

> **Ticker-scoped top-up (Jul 24 2026):** the same-day top-up guard is applied
> independently per ticker. A ticker with no prior same-day context is always
> evaluated as a first buy against yesterday's close and low, and prior same-day
> buys in other symbols must never suppress it. A ticker with same-day context is
> gated only against its own same-day reference; when the current price is not
> strictly lower, or the corrected delta is 0, it is skipped with reason
> `same_day_topup_not_lower_or_no_delta`. The canonical statement of this rule and
> its reason strings live in
> [`config/scheduled_tasks.json`](../config/scheduled_tasks.json)
> (`canonical_first_buy_and_topup_rule`). The runner implements that rule exactly:
> a ticker's same-day comparison reference is its most recent valid same-day buy
> reference (by `generated_at`; ties and untimestamped runs defer to later scan
> order, and a manual fill folded in last is treated as most recent). Any unknown
> contributing reference poisons the aggregate to unknown and routes the guard to
> its conservative missing-price / manual review $0 path.

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
shared dynamic-base 0x-to-8x ladder as the afternoon run — as of the Fri Jul 10 2026
live-automation update every live stream shares one ladder (the former morning
0x-to-3x ladder is historical/superseded). Both are independent of the afternoon
3:30 PM task and never count as afternoon scheduled evidence.

The scheduled afternoon run auto-loads prior context from its own task root
**plus** the morning-live root (`.../cron_tracking/morning_dca`), the
midday-live root (`.../cron_tracking/midday_dca`), and the manual missed-morning
recovery root (`.../cron_tracking/morning_dca/manual_recovery/YYYYMMDD_*`), so
morning, midday, manual override, and manual missed-morning recovery same-day
fills all offset the afternoon buy. Roots that did not run that day contribute
nothing.

**Manual missed-morning recovery (added Jul 20 2026).** If the 10:30 scheduled
morning live task did not run, a human may place the missed buys by hand under
`.../cron_tracking/morning_dca/manual_recovery/YYYYMMDD_*` (example
`20260720_1048ET`). Both the **12:30 midday** and **3:30 final** runs scan that
path and fold any same-day recovery fills into this top-up guard as prior
allocation. Those artifacts (`run_origin='manual'`, `manual_run=true`,
`same_day_top_up_context_only=true`, `valid_scheduled_evidence=false`) are
prior-allocation / top-up context **only** — never proof the 10:30 scheduled task
ran and never scheduled evidence. See
[run_classification.md](run_classification.md#manual-missed-morning-recovery-added-2026-07-20).

### Manual override same-day fills

Manual override reruns (e.g. an out-of-band manual buy on account <ROBINHOOD_ACCOUNT_NUMBER>) are
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

Before computing any signal, the scheduled 3:30 PM ET run persists every raw
Robinhood/Finance/Alpaca/Yahoo connector response it used under
`scheduled_runs/YYYYMMDD_1530ET/raw/` (deterministic filenames
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

### Intraday 2:30 PM ET scheduled recovery (2026-07-24)

On **2026-07-24** the **2:30 PM ET intraday scheduled run** (stream
`intraday_1430`, run id `intraday_live_20260724_1430ET`) prepared its
market-data and signal artifacts but could not call `place_equity_order` from
the scheduled subagent. Following the failure-recovery policy, the parent
submitted the qualifying `place_equity_order` calls with idempotency keys and
reconciled them against Robinhood: **18 orders filled, 0 missing, $72.00 total
recovered spend**. Artifacts live under
`/home/user/workspace/cron_tracking/intraday_dca/20260724_1430ET/`
(`submitted_orders.csv`, `run_state.json`, `run_summary.json`, and
`parent_recovery_submission_summary.json`).

This is classified as a **scheduled intraday recovery**: `run_origin='scheduled'`,
`manual_run=false`, `recovery_rerun=true`, `parent_recovery_submission=true`. It
is valid scheduled evidence for the `intraday_1430` stream and valid same-day
prior-allocation / top-up context for later same-day runs (gated by the canonical
first-buy and same-day top-up rule). It **must not** be treated as manual evidence
and **must not** be counted as proof that any other stream (morning 10:30, midday
12:30, or afternoon 3:30) ran.

Fills: TRIN $3.00 @ 17.2164, IRDM $5.00 @ 45.7799, DXYZ $6.00 @ 22.9287, ASML
$4.00 @ 1760.8199, STX $8.00 @ 859.2699, AMAT $6.00 @ 536.3400, AMD $4.00 @
524.7103, BTX $3.00 @ 8.2099, STM $5.00 @ 51.7199, TSM $4.00 @ 405.8750, AVGO
$5.00 @ 380.2600, NVDA $1.00 @ 207.0400, INTC $7.00 @ 94.0800, GS $1.50 @
1062.7200, XLG $1.00 @ 59.9196, QTOP $3.00 @ 35.8067, RGTI $4.00 @ 14.4278, TXN
$1.50 @ 280.3527. The canonical
[first-buy and same-day top-up rule](../config/scheduled_tasks.json)
(`canonical_first_buy_and_topup_rule`), active universe, exclusions, provider
policy, artifact contract, and cost-conscious mode are unchanged by this record.

## Execution constraints

See [`docs/safeguards.md`](safeguards.md) for the full list. Summary:

- Only broker/account for live/manual runs: **Robinhood Agentic account `<ROBINHOOD_ACCOUNT_NUMBER>`**.
- Alpaca is read-only validation / account context only (unless explicitly changed later).
- One strategy order per ticker per stream per day (see the top-up guard above for same-day repeat buys by a different stream).
- Market buy orders with `dollar_amount` notional and `time_in_force = gfd`, only after `review_equity_order` passes.
- No sells, options, crypto, margin expansion, order replacement, account changes, or trading outside regular hours.
- Test window approved through **Fri Oct 30, 2026 inclusive**, per
  `config/trading_window.json` (the authoritative gate; extended 2026-07-26
  from the original Aug 28, 2026 end date). Deliberately stops before the
  2026-11-01 DST transition — see `docs/safeguards.md` for why.
