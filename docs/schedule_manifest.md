# Scheduled Tasks Manifest

Backup of the current scheduled tasks. Machine-readable source:
[`config/scheduled_tasks.json`](../config/scheduled_tasks.json).

**Timezone note:** cron expressions are in **UTC**. ET equivalents below are for
US Eastern Daylight Time (EDT, UTC-4). Re-verify around DST transitions.

> **Afternoon completion rule:** all afternoon **trading and artifact-writing**
> tasks complete **no later than 3:40 PM ET**, when the read-only watchdog
> (`a46bf007`) begins its final verification. The watchdog's own post-3:40 PM
> verification and the Friday weekly Notion performance refresh at **5:00 PM ET**
> are the only approved post-3:40 PM DCA-related exceptions.

> **TICKER UNIVERSE UPDATE JUL 19 2026:** TXN is added to the active DCA
> universe and GFUZ remains active, used by every universe-bearing scheduled
> task (the pre-market dashboard, the 9:45 scans, the morning/midday/afternoon
> live streams, the monitor-only signal check, the post-trade slippage sync, the
> watchdog, and the weekly/monthly refreshes). The active universe is now **44
> symbols** including both GFUZ and TXN. Robinhood resolved TXN as Texas
> Instruments Incorporated Common Stock, state active, tradeable true, fractional
> tradability tradable, regular-hours dollar orders supported, and prior close
> available. Finance lookup resolved TXN as Texas Instruments Incorporated.
> Existing exclusions remain BYDDY, VCX, TRLV, and RVI; corrected symbols IRDM
> and GLW remain; IBM and GFUZ remain included (GFUZ with Robinhood primary if
> finance is unresolved). This adds whitelisted symbols only; it preserves all
> existing trading safety, pre-authorization, top-up, run-classification,
> budget, cost-conscious, manual-exclusion, and recovery text.

> **Schedule integrity note:** the live trading task runs at **3:20 PM ET**; the
> post-trade slippage / Notion sync runs at **3:35 PM ET**. These are two
> different tasks. Do not merge them or move the live trade off 3:20 PM ET.

> **3:20 PM state-file guarantee:** the live task (`10ce362c`) **always** writes
> a scheduled `run_state.json` under
> `cron_tracking/ce16ba6b/scheduled_runs/YYYYMMDD_1520ET/` and updates
> `latest_scheduled_run.json` every time it fires, **even when no order is
> placed**. If Robinhood review passes but submission is blocked by required
> post-review user confirmation, the run_state stays valid and carries
> `order_submit_status='not_submitted_confirmation_required'`, `reviewed_order`
> details where available, candidate details, and **no `order_id`**. That is
> valid scheduled-run evidence the task ran — **not** evidence of an order/fill.
> See [run_classification.md](run_classification.md).

> **Raw data persistence fix (added Jul 10 2026):** before signal computation the
> live task (`10ce362c`) must persist every raw Robinhood/Finance/Alpaca/Yahoo
> connector response it used under
> `cron_tracking/ce16ba6b/scheduled_runs/YYYYMMDD_1520ET/raw/` with deterministic
> filenames (`quotes_batchN.json`, `historicals_batchN.json`,
> `tradability_pre_place.json`, `portfolio_pre_place.json`,
> `orders_after_place.json`, `place_SYMBOL.json`), normalizing large connector
> `output_path` pointers into parseable JSON. **Compute must read only these
> persisted files, not in-memory tool responses.** If persistence/normalization
> fails, the task still writes a valid scheduled `run_state.json`
> (`run_origin='scheduled'`, `manual_run=false`,
> `order_submit_status='not_submitted_error'`, no orders) rather than escalating
> without state. See the `10ce362c` task detail below.

> **Post-artifact drift detection (added Jul 16 2026):** every live trading task
> (morning `42173154` 10:30 AM, midday `a6239dae` 12:30 PM, afternoon
> `10ce362c` 3:20 PM) must invoke `scripts/drift_detection.py` against its own
> run directory **immediately after writing artifacts**. The detector reconciles
> `signals.csv` (intended auto-buys) against `submitted_orders.csv` (actual order
> records) and fires an **immediate alert** when any symbol has
> `auto_buy_signal` true (case-insensitive `true`/`1`/`yes`) and
> `incremental_order_amount > 0` or a positive desired amount, but **no
> submitted-order record exists** for that symbol. This exists because of the
> **Jul 16 2026 failure mode**: `signals.csv` held **8** `auto_buy_signal=true`
> rows with positive incremental amounts while `submitted_orders.csv` was
> **empty** and `raw/` was **missing** — signals computed, no orders placed or
> persisted, and nothing alerted until the next day. The detector writes a
> compact JSON + markdown diagnostic (affected symbols, expected amount,
> suspected cause, run directory, recommended action) and, when
> `DCA_DRIFT_SLACK_WEBHOOK` is set and `--slack` is passed, posts an immediate
> Slack alert (no webhook is ever hardcoded). The 3:20 PM check must complete
> **before** the 3:40 PM watchdog (`a46bf007`). See
> [drift_detection.md](drift_detection.md) and the `drift_detection_policy` block
> in [`config/scheduled_tasks.json`](../config/scheduled_tasks.json). The drift
> detector shipped in **PR #80** (merge commit `3641b90`) and is wired into all
> three live tasks via their `post_artifact_drift_check` blocks and the
> `d1f7a2c0` monitoring entry.

> **Cost-saving operating mode (requested Thu Jul 16 2026):** the weekday chain
> runs in **compact** mode and all heavy analysis is deferred to the **Friday
> 5:00 PM ET** weekly refresh. Machine-readable source:
> [`config/scheduled_tasks.json`](../config/scheduled_tasks.json) →
> `operating_mode` (`cost_saving_daily_compact`). Daily weekday compact chain:
> **9:45** pre-flight (`morning-dca-0945`), **10:30** morning live (`42173154`),
> **12:30** midday live (`a6239dae`), **3:20** final live (`10ce362c`), **3:35**
> post-trade slippage + compact Notion Latest Run sync (`67aa802b`), **3:40**
> watchdog (`a46bf007`). Daily tasks write compact JSON/CSV artifacts only, avoid
> long narrative summaries, suppress no-op notifications, and notify only for
> actionable events (placed/rejected orders, budget-capped candidates,
> missing/invalid artifacts, broker/log mismatch, data-confidence failures,
> drift-detector failures, recovery attempts/failures, meaningful exceptions).
> Use **drift detection instead of manual checking**, batch market-data pulls and
> persist raw connector responses once for downstream reads, keep daily Notion
> writes to the Latest Run section (full page weekly), and cap automated retries
> at **one** targeted recovery rerun (recover only failed tickers/steps). This
> mode changes cost/reporting behavior only — **all trading safety rules are
> preserved** (Robinhood-only execution on `562057042`; Alpaca/Yahoo/Finance
> read-only; buy-only, no options/crypto/margin; active universe includes IBM and GFUZ
> and excludes BYDDY/VCX/TRLV/RVI; manual/morning/midday/final streams stay
> separated; prior-run fills are same-day top-up context only, never scheduled
> evidence). See [cost_tracking.md](cost_tracking.md).

> **Weekly-rollover cap-curve backtest (added Jul 17 2026):** an analytics-only,
> non-live backtest of an intraday cumulative cap curve on a weekly-rollover
> funding model (base $1 for the backtest, $300 weekly add with unused cash rolled
> forward, daily cap = remaining weekly cash / remaining trading days, intraday
> proxy 10:30 open / 12:30 open-low average / 3:20 close, over the same 60 trading
> day price history). The user-highlighted candidate is **35 / 65 / 100**. This
> does **not** change any live schedule or execution logic; the monthly review
> (`0656657a`) should include this analysis. Machine-readable data:
> [`config/backtest_intraday_cap_curve.json`](../config/backtest_intraday_cap_curve.json);
> full methodology and results: [backtest_curve.md](backtest_curve.md).

> **Live reliability fix (added Jul 17 2026):** applied to all three
> pre-authorized live trading tasks (morning `42173154` 10:30 AM, midday
> `a6239dae` 12:30 PM, afternoon `10ce362c` 3:20 PM). It closes the
> execution-lineage gap where an actionable signal could end a run with neither
> an `order_id` nor a recorded block/rejection. The rules now in the live task
> text:
> 1. **Describe schemas first.** Before calling Robinhood `place_equity_order`,
>    describe the Robinhood connector schemas needed in the current agent
>    session, including `place_equity_order` and `get_equity_orders`.
> 2. **Always persist artifacts.** Always write the final `run_state.json`,
>    `run_summary.json`, `signals.csv`, `submitted_orders.csv`, and
>    `filled_orders.csv` where applicable, even for no-order, blocked, failed, or
>    recovery runs.
> 3. **One persisted result per actionable signal.** Every actionable signal must
>    end with exactly one persisted result: `order_id`, broker rejection, top-up
>    block, budget block, cap block, data-confidence block, spread block, trend
>    block, or safety block.
> 4. **Lineage-incomplete recovery.** If an actionable signal lacks both an
>    `order_id` and a block or rejection reason, classify the run
>    `execution_lineage_incomplete`, run one recovery retry in the same scheduled
>    directory, retry only the missing tickers with the same `ref_id` values, and
>    reconcile any duplicate `ref_id` or timeout through `get_equity_orders`
>    before declaring failure.
> 5. **Manual overrides never count.** Manual overrides stay `manual_run=true` and
>    never count as scheduled evidence.
>
> **Jul 17 2026 manual recovery artifact:** a manual override recovery ran under
> `/home/user/workspace/cron_tracking/morning_dca/manual_overrides/20260717_1042ET`
> from source scheduled run `morning_live_20260717_1030ET`: **33** requested
> orders, submitted response count **33**, filled count **33** after
> broker-history reconciliation, with the **MRVL** duplicate `ref_id` reconciled
> through `get_equity_orders`. These are **manual recovery only**
> (`valid_morning_evidence=false`, `valid_midday_evidence=false`,
> `valid_afternoon_evidence=false`): excluded from all scheduled evidence,
> slippage, reliability, fill counts, watchdog confirmation, and cost basis.
> Later scheduled runs may use them **only** as same-day prior allocation /
> top-up context. See the `live_reliability_fix_2026_07_17` block in
> [`config/scheduled_tasks.json`](../config/scheduled_tasks.json).
>
> **Live 12:30 no-placeholder fix (added Jul 17 2026):** applied to the midday
> task `a6239dae` on top of the reliability rules above. The scheduled midday run
> must **never** write placeholder signals or empty `submitted_orders.csv` /
> `filled_orders.csv` while marking `valid_midday_evidence=true`:
> 1. **No placeholder success.** The scheduled task must never write placeholder
>    signals or empty submitted/filled files while marking
>    `valid_midday_evidence=true`.
> 2. **Blocked runs are honest.** If it cannot compute full signals, it must write
>    `valid_midday_evidence=false` with concrete `blocking_errors` and notify.
> 3. **Nested quote parsing.** It must parse Robinhood `get_equity_quotes` results
>    from `data.results[].quote` and `data.results[].close`.
> 4. **Follow historical `output_path` pointers.** It must follow `output_path`
>    pointers for large `get_equity_historicals` responses and load the referenced
>    result JSON before computing trend.
> 5. **Placeholder equals failure.** A placeholder artifact with
>    `blocking_errors=['persistence_placeholder_no_signal_compute']` is a **failed
>    run** and must trigger recovery, not a completed scheduled run.
>
> **Jul 17 2026 12:30 recovery result:** the original background task escalated
> after writing placeholder artifacts with
> `blocking_errors=['persistence_placeholder_no_signal_compute']`. Parent recovery
> persisted raw quote, history, tradability, portfolio, and order-history files
> under `/home/user/workspace/cron_tracking/midday_dca/20260717_1230ET/raw/`. The
> corrected nested Robinhood quote parser and `output_path` historical parser
> found **exactly one** eligible scheduled midday buy: **TRIN $3.00**. TRIN
> `order_id 6a5a5b53-6502-476d-b6d4-9cc73bab4f31`, state **filled**,
> `average_fill_price 17.736500`, `quantity 0.169142`,
> `created_at 2026-07-17T16:41:55.91467Z`. This scheduled recovery is marked
> `valid_midday_evidence=true` and `valid_afternoon_evidence=false`; the Jul 17
> morning manual override remains manual context only. See the
> `live_reliability_fix_2026_07_17.midday_no_placeholder_fix` and
> `jul17_midday_recovery` blocks in
> [`config/scheduled_tasks.json`](../config/scheduled_tasks.json).
>
> **Final buy budget-tier fix (added Jul 17 2026):** applied to the afternoon
> final-buy task `10ce362c`. Before placing any final-buy order the run computes
> the budget tier from the full actionable incremental plan and the count of
> 3x-or-greater signals:
> 1. **Normal.** Planned spend <= $129 and 3x-or-greater signals <= 5, corrected
>    daily cap $180.
> 2. **Elevated.** Planned spend > $129 or 3x-or-greater signals 6 to 10,
>    corrected daily cap $240.
> 3. **High-volatility.** Planned spend > $213 or 3x-or-greater signals 11 to 17,
>    corrected daily cap $300.
> 4. **Stress.** Planned spend > $272 or 3x-or-greater signals > 17, corrected
>    daily cap $360 **only** to the extent unused weekly rollover reserve is
>    available.
>
> Same-day strategy allocation already placed in the manual recovery, morning,
> midday, and earlier final orders is counted as corrected-daily-cap usage **for
> safety**, while the streams stay separate for evidence and slippage. If planned
> spend exceeds the remaining corrected daily capacity, the remaining eligible
> orders are ranked by deepest drop percentage, then multiplier, then amount; only
> the orders that fit the remaining cap and the per-order $15 max are placed, and
> the rest are written to `budget_blocked.csv` with per-symbol
> `block_reason='budget_block_corrected_daily_cap'`. If a budget-logic
> inconsistency is detected **after** any order is placed, the run recovers inside
> the same scheduled run directory, reconciles the already placed orders, applies
> the corrected cap logic, and finalizes state rather than escalating without
> complete artifacts. See the
> `live_reliability_fix_2026_07_17.final_buy_budget_tier_fix` block and the
> `10ce362c` task `final_buy_budget_tier_fix` detail in
> [`config/scheduled_tasks.json`](../config/scheduled_tasks.json).
>
> **Jul 17 2026 final-run recovery result:** the background final run placed
> **SPCX** first, then escalated because the budget logic was incomplete. The
> corrected tier was **elevated** because planned spend was **187.5** and the
> 3x-or-greater count was **6**, so the corrected daily cap was **240**. Prior
> same-day strategy allocation counted for safety was **207.0** and SPCX assumed
> **15.0**, leaving **18.0** remaining capacity. Recovery placed **GFUZ $13.50**
> and **ASML $4.50**, then budget-blocked the remaining **27** signals. Filled
> orders: SPCX `order_id 6a5a8182-9460-4d0d-99c3-86af604192a7`, `average_fill_price
> 123.600000`, `quantity 0.121359`; GFUZ `order_id
> 6a5a81d4-d250-43e3-89f3-06c2c4add387`, `average_fill_price 8.449900`, `quantity
> 1.597652`; ASML `order_id 6a5a81d4-f921-49c3-9a1c-1f0e43c2002b`,
> `average_fill_price 1747.709900`, `quantity 0.002574`. The `run_state` has valid
> scheduled evidence for the final 3:20 run, and the manual/midday streams remain
> only prior allocation context where applicable. See the
> `live_reliability_fix_2026_07_17.jul17_final_run_recovery` block in
> [`config/scheduled_tasks.json`](../config/scheduled_tasks.json).
>
> **Jul 17 2026 manual budget-block overrides:** the 3:20 PM scheduled final buy
> run (`cron_tracking/ce16ba6b/scheduled_runs/20260717_1520ET`) placed and
> finalized **SPCX**, **GFUZ**, and **ASML** under the corrected elevated daily
> cap, then budget-blocked **27** remaining signals under the corrected cap: NFLX,
> AMAT, CBRS, DXYZ, TSM, IBM, EVR, GOOGL, GOOG, GS, NVDA, GLW, INTC, BTX, MA, AMD,
> MSFT, QTOP, XLG, TOPT, SNDK, AVGO, AMZN, STM, MRVL, DFTX, QBTS. Two **manual**
> budget-block override runs then filled all 27:
> 1. **First override**
>    (`cron_tracking/final_manual_review/budget_block_override_20260717_1551ET/run_state.json`)
>    attempted all 27 and accepted/submitted **20**: NFLX, AMAT, CBRS, DXYZ, TSM,
>    IBM, EVR, GOOGL, GOOG, GS, NVDA, GLW, INTC, BTX, MA, AMD, MSFT, QTOP, XLG,
>    TOPT. The remaining **7** failed on Robinhood connector 429 throttling /
>    do-not-retry state.
> 2. **Second override**
>    (`cron_tracking/final_manual_review/budget_block_override_20260717_1556ET/run_state.json`)
>    placed and verified filled all remaining **7**: SNDK `$4.50` avg fill
>    `1360.550000` qty `0.003307` `order_id 6a5a88c4-e4df-4b9d-a6d5-1549a77cea31`;
>    AVGO `$4.50` avg fill `372.385000` qty `0.012084`
>    `order_id 6a5a88d5-9809-4159-b930-4649e3a377c6`; AMZN `$3.00` avg fill
>    `247.630000` qty `0.012114` `order_id 6a5a88e4-a818-4019-9291-7dbfa5d1c4ee`;
>    STM `$3.00` avg fill `62.439900` qty `0.048046`
>    `order_id 6a5a88f5-090a-4712-b9f1-0955b63aa1d5`; MRVL `$3.00` avg fill
>    `189.359900` qty `0.015842` `order_id 6a5a8904-df24-4817-aa98-61e7bda8de7f`;
>    DFTX `$3.00` avg fill `44.509900` qty `0.067400`
>    `order_id 6a5a891b-daf3-443d-84f3-19f11fbcfada`; QBTS `$3.00` avg fill
>    `16.749900` qty `0.179105` `order_id 6a5a892a-7df2-4e90-a373-fce4ef28f4a5`.
>
> These manual overrides are `manual_run=true`, `valid_scheduled_evidence=false`,
> `valid_afternoon_evidence=false`: excluded from the scheduled 3:20 execution
> evidence, scheduled slippage, scheduled reliability, and scheduled cost-basis
> metrics. They are usable **only** as manual override context and future same-day
> / top-up allocation context. Both artifacts live under the
> `final_manual_review/` manual exclusion root, so their order IDs are collected as
> `excluded_manual_order_ids`. See the
> `live_reliability_fix_2026_07_17.jul17_manual_budget_block_overrides` block in
> [`config/scheduled_tasks.json`](../config/scheduled_tasks.json).
>
> **Nested run_classification requirement (added Jul 17 2026):** the 3:20 PM
> scheduled `run_state` was repaired to include a **nested `run_classification`
> object**. From this date on, every scheduled `run_state.json` must contain a
> nested `run_classification` object (validated against
> [`schemas/run_classification.schema.json`](../schemas/run_classification.schema.json)). The 3:40 PM
> watchdog (`a46bf007`) confirms a scheduled run **only** when the nested
> `run_classification` object is present and valid; a `run_state` missing the
> nested object is a watchdog **discrepancy**.
> `live_reliability_fix_2026_07_17.scheduled_run_state_classification_requirement`
> block in [`config/scheduled_tasks.json`](../config/scheduled_tasks.json) and
> [run_classification.md](run_classification.md).
>
> **Run-classification note:** the live task (`10ce362c`, the repaired task
> that supersedes the deleted `ce16ba6b`; artifact root stays under
> `.../ce16ba6b` for continuity) classifies every
> run as scheduled or manual. Every scheduled 3:20 PM ET run_state must be
> written with **both** `cron_id='10ce362c'` **and**
> `supersedes_cron_id='ce16ba6b'`; placeholder/ambiguous ids such as
> `ce16ba6b_replacement` are **forbidden**. Scheduled runs write under
> `cron_tracking/ce16ba6b/scheduled_runs/YYYYMMDD_1520ET/` and update
> `latest_scheduled_run.json`; manual runs write under `manual_runs/<timestamp>/`
> and never touch that pointer. The slippage (`67aa802b`), watchdog
> (`a46bf007`), weekly (`041d9d90`), and monthly (`0656657a`) tasks consume only
> validated scheduled runs and exclude manual runs/order IDs. See
> [run_classification.md](run_classification.md).

> **Automatic failure recovery (live trading tasks):** the three scheduled
> **live Robinhood execution** tasks — the **10:30 AM ET** morning task
> (`42173154`), the **12:30 PM ET** midday task (`a6239dae`), and the
> **3:20 PM ET** afternoon task (`10ce362c`) — treat a mid-run failure as a
> **recovery trigger, not a terminal stop**, when the failure is caused by
> **internal persistence, parsing, wrapper-normalization, transient connector
> throttling, or raw-file write problems that occur after data was already
> fetched**. On such a failure the task **immediately reruns the required
> read-only inputs once**, **persists all raw responses first**, **recomputes
> only from the persisted files**, and continues through the **same
> pre-authorized guarded execution path** (all validation, signal, budget,
> spread, confidence/trend, top-up, and circuit-breaker checks must still pass —
> nothing is loosened). The recovery attempt **reuses the same logical scheduled
> run directory** with `recovery_rerun=true` and is **never classified as
> manual** (it keeps the stream's scheduled `run_classification`). If the
> recovery attempt **also fails**, or a **safety / account / order rejection**
> occurs, the task writes `run_state.json` with the **blocking reason** and
> **escalates** rather than retrying further. Recovery is a within-schedule
> repair only: it never changes the fire time and never places more than the one
> permitted strategy order per ticker per stream. See
> [run_classification.md](run_classification.md#recovery-reruns) and
> [safeguards.md](safeguards.md).
>
> **Cost-conscious model instruction (added Jul 17 2026):** every daily DCA
> scheduled task carries the note **COST-CONSCIOUS MODEL INSTRUCTION JUL 17 2026:
> Use a cost-conscious/default model where available; do not use ChatGPT 5.5 or
> premium models unless needed for failure recovery, safety validation, or
> connector/debug escalation. Keep daily output compact, write JSON/CSV
> artifacts, suppress no-op notifications, and leave heavy analysis to Friday
> weekly or monthly review.** It applies to the 9:00 AM pre-market buying-power
> dashboard (`b7961407`), the 9:45 AM pre-flight / early-warning scans
> (`morning-dca-0945`, `cd45e95b`), the 10:30 AM morning live execution
> (`42173154`), the 12:30 PM midday live execution (`a6239dae`), the 3:05 PM
> monitor-only signal check (`e3e6f09e`), the 3:20 PM final live buy
> (`10ce362c`), the 3:35 PM slippage plus Notion latest-run sync (`67aa802b`),
> and the 3:40 PM watchdog (`a46bf007`). It is a model-selection and
> output-compactness rule only and changes no trading, schedule, top-up, budget,
> recovery, run-classification, manual-exclusion, GFUZ, or cost-saving operating
> mode behavior. See the `cost_conscious_model_instruction_2026_07_17` field on
> each task in [`config/scheduled_tasks.json`](../config/scheduled_tasks.json).

> **Same-day top-up guard:** a morning/manual run must **never block** the
> scheduled 3:20 PM ET run. Instead the scheduled run reads earlier same-day
> morning/manual allocations (as prior-allocation context only — never as
> scheduled evidence, slippage, watchdog confirmation, scheduled cost basis, or
> scheduled fill counts) and, per ticker, buys only the **incremental** dollar
> difference between the afternoon desired amount and the amount already bought,
> and only when the afternoon decision price is **strictly below** the earlier
> run's reference price. Otherwise it buys **$0** for that ticker. Only earlier
> runs generated **before 2:30 PM ET** contribute; scheduled runs are excluded.
> The scheduled run still writes a valid scheduled run_state even when every buy
> is fully offset to $0. See [strategy.md](strategy.md#same-day-prior-run-offset--top-up-guard).
>
> **Morning two-stream note:** the morning `MorningDCAExecutor` runs on the
> shared **$2-base 0x-to-5x** live ladder (as of the Fri Jul 10 2026
> live-automation update every live stream shares one ladder; the former
> morning-only 0x-to-3x ladder is historical/superseded) and its own `morning`
> budget ledger in **two** modes.
> (1) A **9:45 AM ET read-only pre-flight validation**
> (`morning-dca-0945`, run_type `morning_dca_preflight`,
> `state_schema_version='morning-preflight-v2-machine-complete'`, **no trades**)
> writing under `cron_tracking/morning_dca_preflight/YYYYMMDD_0945ET/`
> (`run_state.json`, `validation.csv`, `signals.csv`, `run_summary.json`,
> `latest_preflight.json`). As of **2026-07-13** the `run_state.json` is
> **machine-complete** (schema
> [`schemas/morning_preflight.schema.json`](../schemas/morning_preflight.schema.json)):
> it always writes top-level `overall_ok`, `eligible_count`, `ineligible_count`,
> `unresolved_tickers`, `blocking_errors`, `warnings`, `run_classification`, and a
> `per_ticker_eligibility` list with **every active-universe symbol exactly once**
> so the 10:30 live task can gate directly off machine fields (added after the
> 2026-07-13 10:30 run was blocked by a 9:45 artifact missing `overall_ok` and the
> per-ticker eligibility list). Unresolved symbols (e.g. AAAU, SPCX) are rows with
> `eligible_to_execute=false`, `ineligible_reason='unresolved_ticker'` and never
> set `overall_ok=false`; `overall_ok` is false only for
> global/account/buying-power/auth/schema-write failures.
> It emits **`eligible_to_execute=true/false` per ticker** — a **coarse
> pre-screen** for the 10:30 evaluator, **not** a final approval. (A read-only
> `watchlist` early-warning mode remains available under
> `cron_tracking/morning_dca_watchlist/`, run_type `morning_dca_watchlist_scan`.)
> (2) A **10:30 AM ET live Robinhood execution** (`42173154`, cron
> `30 14 * * 1-5` = **14:30 UTC weekdays** during EDT, run_type
> `morning_dca_live`, `state_schema_version='morning-live-v2-preauthorized'`,
> **trades=yes**) writing under `cron_tracking/morning_dca/YYYYMMDD_1030ET/`
> (`run_id='morning_live_YYYYMMDD_1030ET'`), giving a true **60-minute intraday
> trend window**. **Robinhood account 562057042 is the only execution broker**;
> Alpaca/Finance/Yahoo are read-only. It **depends on the same-day 9:45
> pre-flight artifact and evaluates only `eligible_to_execute=true` tickers**,
> then **recomputes a fresh price/prior close/spread/60-minute downtrend/signal/
> budget before placing** (skip / manual-review if trend cannot be validated); if
> the pre-flight artifact is missing, a **scheduled** run fails closed (evaluates
> zero tickers and writes a blocked `run_state.json`); only a **manual** run
> degrades to the full universe with a warning. **Pre-authorized direct placement**
> (scheduled live runs only, approved 2026-07-10 11:20 AM ET): no per-order
> confirmation, no `review_equity_order`; `place_equity_order` only after every
> safeguard passes (manual/ad hoc runs still require review + confirmation).
> **GFUZ special handling:** Robinhood is primary because Finance may not resolve
> GFUZ; an unresolved GFUZ on Finance does not block the run; the *intent* is
> that if the 9:45 pre-flight lacks GFUZ the 10:30 task runs a supplemental
> GFUZ read-only validation and persists `gfuz_supplemental_preflight.json`
> before decisioning — **not yet implemented**: today `_apply_preflight_gate()`
> simply gates GFUZ out (safe, fail-closed; it is just skipped for that run)
> when the preflight artifact lacks a GFUZ row. See `config/morning_dca.json`'s
> `gfuz_special_handling.supplemental_preflight.implementation_status`.
> **Raw data persistence (added Jul 15 2026, confirmed Jul 16 2026):** the
> *intended* contract is that before any signal/budget/top-up computation or
> placement, the task persists every raw connector response under
> `cron_tracking/morning_dca/YYYYMMDD_1030ET/raw/` (normalizing
> `output_path`/wrapped outputs), the decision engine reads only from persisted
> files, and if fetch succeeds but persistence/normalization fails it places no
> orders and still writes `run_state.json` with the blocking reason —
> **not yet implemented** in `MorningDCAExecutor`: `run()` evaluates directly
> from the in-memory data provider today. **Automatic failure recovery:** the
> *intended* contract is that on recoverable internal/persistence/parsing/
> normalization/throttling/raw-write failures after data was fetched, the task
> reruns read-only inputs once, persists raw first, recomputes only from
> persisted files, continues guarded execution if safeguards pass, reuses the
> same logical run directory with `recovery_rerun=true`, and never classifies
> recovery as manual, escalating with a blocking `run_state.json` if recovery
> also fails — **not yet implemented**: there is no recovery-rerun code path in
> `morning_dca.py` today. See `config/morning_dca.json`'s
> `raw_data_persistence.implementation_status` and
> `automatic_failure_recovery.implementation_status`. **Confidence rule:** if
> price/drop passes but the intraday trend cannot be validated, the live run does
> **not** auto-buy — it classifies the candidate as
> `execution_status='manual_review_candidate'`. All morning modes carry
> `stream='morning'`, `valid_morning_evidence=true`, and
> `valid_afternoon_evidence=false`, never update `latest_scheduled_run.json`, and
> are never afternoon scheduled evidence; live morning fills count only as
> prior-allocation context for the midday/afternoon same-day top-up guard.
> See [morning_dca.md](morning_dca.md).
>
> **Midday stream note:** the midday `MiddayDCAExecutor` (`a6239dae`, run_type
> `midday_dca_live`, `state_schema_version='midday-live-v1'`, **trades=yes**)
> is a single **12:30 PM ET live Robinhood execution** on account `562057042`,
> writing under `cron_tracking/midday_dca/YYYYMMDD_1230ET/`
> (`run_id='midday_live_YYYYMMDD_1230ET'`). It reuses the **afternoon 0x-to-5x
> ladder** and the same $2 base (the shared live ladder used by all streams as
> of Jul 10 2026), the same
> active universe (excluding BYDDY/VCX/TRLV/RVI), the same confidence rule, and
> its own `midday` budget ledger. It carries `stream='midday'` and
> `valid_afternoon_evidence=false`, never updates `latest_scheduled_run.json`,
> and is never afternoon scheduled evidence. Morning/manual same-day fills are
> **prior-allocation context only** for the midday top-up guard (midday buys the
> incremental difference only when its decision price is strictly below the prior
> reference price); midday fills in turn count only as prior-allocation context
> for the afternoon 3:20 PM top-up guard. See [midday_dca.md](midday_dca.md).
> **Base-amount note:** the graded DCA base amount is exactly **$2.00**; every
> candidate dollar amount is **`$2.00 × multiplier`**. The automation must never
> infer, discover, or fall back to **$50.00** or any broker/account default. If
> the base-amount config (`config/budgets.json`) is missing or unreadable,
> continue using **$2.00** as a hardcoded, user-confirmed fallback.

> **Base-amount change (Jul 19 2026):** the user changed all future DCA
> scheduled runs from a $3 base amount to a $2 base amount. The order amount is
> now $2 x multiplier, and the max per order is $10 under the 5x tier. This
> applies to every universe-bearing live and analytics task below (morning
> 10:30, midday 12:30, afternoon 3:20, and the analytics/monitor scans). Ladder
> multipliers, the ticker universe, exclusions, budget caps, the top-up guard,
> pre-authorization, confidence, recovery, and all other safety rules are
> unchanged.

> **Missed 10:30 morning run recovery (added Jul 20 2026):** when the scheduled
> **10:30 AM ET morning live task did not run**, a human-triggered **manual
> recovery** may place the missed morning buys by hand and write artifacts under
> a dedicated manual-recovery path:
> `/home/user/workspace/cron_tracking/morning_dca/manual_recovery/YYYYMMDD_*`
> (example: `.../manual_recovery/20260720_1048ET` for the Jul 20 2026 missed
> morning run). These artifacts are valid **prior-allocation / same-day top-up
> context only** when `run_origin='manual'`, `manual_run=true`,
> `same_day_top_up_context_only=true`, and `valid_scheduled_evidence=false` all
> hold. They **never** count as proof that the 10:30 scheduled task ran, are
> **never** written into the midday or afternoon scheduled evidence stream, and
> are **never** used for scheduled reliability, scheduled fill counts, scheduled
> slippage, scheduled cost basis, or watchdog confirmation. They may **only**
> reduce or block additional buys through the same-day top-up rule: a later run
> buys `max(0, desired - prior)` and only if the later decision price is
> **strictly below** the prior reference price. The **12:30 midday** run
> (`a6239dae`) and the **3:20 final** run (`10ce362c`) **must scan** this path for
> same-day manual recovery artifacts and fold them into the top-up guard as prior
> allocation. Machine-readable source:
> [`config/scheduled_tasks.json`](../config/scheduled_tasks.json) →
> `missed_morning_run_recovery_policy` (and
> `config/morning_dca.json:manual_missed_run_recovery`,
> `config/run_classification.json:manual_missed_morning_recovery`). See
> [run_classification.md](run_classification.md) and [strategy.md](strategy.md#same-day-prior-run-offset--top-up-guard).

> **Market-data source policy:** the **Finance connector is the primary**
> market-data source. **Alpaca and Robinhood** provide validation / context.
> **Yahoo Finance through yfinance is a fallback only**, used when connector data
> is **unavailable, stale, incomplete, or unresolved**. Fallback data must be
> **labeled as fallback** and must **not weaken** confidence, spread, trend,
> top-up, budget, whitelist, buy-only, regular-hours-only, or any safety rule.
> **Robinhood remains the only execution broker.** **GFUZ** may use Robinhood as
> its primary source when Finance cannot resolve it. Machine-readable source:
> [`config/scheduled_tasks.json`](../config/scheduled_tasks.json) →
> `market_data_source_policy` (mirrored in
> `config/budgets.json:market_data_source_policy`). See
> [safeguards.md](safeguards.md).

> **Midday raw input generation and failure-state fix (added Jul 20 2026):**
> after the 12:30 midday run (`a6239dae`) escalated on Jul 20 2026, the scheduled
> task must **generate the same-day raw inputs before invoking any saved runner**
> (e.g. `.../cron_tracking/a6239dae/run_midday_live.sh` or
> `midday_live_runner.py`). It creates
> `.../cron_tracking/midday_dca/YYYYMMDD_1230ET/raw/` and persists
> `raw/portfolio.json`, `raw/quotes.json` (or named quote batches normalized into
> it), and the historical / trend files needed for the 60-minute trend check,
> passing the current America/New_York date as `RUN_DATE_ET=YYYYMMDD` when the
> runner requires it. It must **not** assume `raw/quotes.json` or historicals
> already exist from a prior run; inputs come from the Finance connector primary,
> Alpaca and Robinhood validation / context, and `yfinance` fallback only when
> connector data is unavailable, stale, incomplete, or unresolved. One bounded
> recovery attempt on the alternate approved data source path is allowed, then
> stop. **State-file guarantee:** the run **always** writes `run_state.json`,
> `run_summary.json`, and `latest_midday_run.json` even with no orders or a
> blocked run; if raw inputs are missing, malformed, stale, or not generated it
> writes a valid scheduled midday `run_state` with `run_origin='scheduled'`,
> `manual_run=false`, `stream='midday'`, `scheduled_for_et='12:30'`,
> `cron_id='a6239dae'`, `valid_afternoon_evidence=false`,
> `order_submit_status='not_submitted_error'`,
> `error_class='missing_or_invalid_raw_inputs'`, `orders_submitted_count=0`, and a
> compact list of missing files or failed data sources. That artifact proves the
> task **fired** but is **not** order or fill evidence; do not escalate without
> writing it unless the workspace itself is unavailable. **Missed-morning
> recovery interaction:** same-day manual missed-10:30 recovery artifacts under
> `.../morning_dca/manual_recovery/YYYYMMDD_*` are loaded **after** raw market
> data is available and **before** order sizing, remain prior-allocation / top-up
> context only, and must **not** satisfy raw quote or historical input
> requirements, be copied into the scheduled evidence stream, or count as proof
> the 10:30 scheduled task ran. Machine-readable source:
> [`config/scheduled_tasks.json`](../config/scheduled_tasks.json) →
> `midday_raw_input_and_failure_state_fix_2026_07_20` and task `a6239dae`
> (`raw_input_generation`, `state_file_guarantee`), mirrored in
> [`config/midday_dca.json`](../config/midday_dca.json). See
> [midday_dca.md](midday_dca.md#raw-input-generation-and-failure-state-guarantee-added-jul-20-2026).

> **Full live schedule hardening (added Jul 20 2026):** after the 10:30 morning
> (`42173154`) and 12:30 midday (`a6239dae`) scheduled live runs both failed on
> Jul 20 2026 by invoking saved runners without first generating the same-day raw
> inputs and escalating with no scheduled failure-state artifact, the same
> self-containment and state-file guarantees now apply to the **10:30 morning** and
> **3:20 afternoon** (`10ce362c`, supersedes `ce16ba6b`) scheduled live tasks. Each
> task must **generate its same-day raw inputs before invoking any saved runner or
> order module** — creating its `raw/` directory and persisting portfolio / buying
> power, quotes, prior close, tradability, the run-specific order context (10:30:
> `orders_since_midnight`; 3:20: `orders_after_1430ET` plus same-day
> prior-allocation context from the morning-live, midday, manual missed-morning
> recovery, and manual override roots), and the historical / trend files. The
> 10:30 task may assume **only** the same-day 9:45 pre-flight artifact; neither
> task reuses stale files. Pass the current America/New_York date as
> `RUN_DATE_ET=YYYYMMDD` when the runner requires it; Finance connector primary,
> Alpaca and Robinhood validation / context, `yfinance` fallback only, one bounded
> recovery attempt then stop. Each task **always** writes `run_state.json`,
> `run_summary.json`, and its latest-run pointer even when blocked, a no-op, or on
> error, with `run_origin='scheduled'`, `manual_run=false`, the correct `stream`
> and `scheduled_for_et`/`cron_id` (10:30: `stream='morning'`,
> `scheduled_for_et='10:30'`, `cron_id='42173154'`; 3:20: `stream='afternoon'`,
> `scheduled_for_et='15:20'`, `cron_id='10ce362c'`, `supersedes_cron_id='ce16ba6b'`,
> `state_schema_version='run-classification-v1'`), `orders_submitted_count=0` when
> none, a concrete `order_submit_status`, a concrete `error_class` (e.g.
> `missing_preflight_artifact`, `missing_or_invalid_raw_inputs`,
> `no_incremental_top_up`), and a compact list of missing files / block reasons /
> failed sources. That artifact proves the task **fired** but is **not** order or
> fill evidence; it must not escalate without writing it unless the workspace is
> unavailable, and a failure after raw collection still writes `signals.csv` when
> possible, an empty `submitted_orders.csv` with headers when no orders, and
> `run_state.json` (no half-done run). Manual recovery artifacts (missed-morning
> and manual 12:30) stay prior-allocation / top-up context only. A **manual 12:30
> recovery** ran at 12:59 ET on Jul 20 2026 under
> `.../cron_tracking/midday_dca/manual_recovery/20260720_1259ET` (fills AAPL $1.00,
> TRIN $2.00, ASML $2.00, IRDM $2.00), available to the 3:20 run as
> prior-allocation context only and never scheduled evidence. Machine-readable
> source: [`config/scheduled_tasks.json`](../config/scheduled_tasks.json) →
> `live_schedule_hardening_2026_07_20` and `midday_dca_manual_recovery_2026_07_20`,
> mirrored in [`config/morning_dca.json`](../config/morning_dca.json),
> [`config/midday_dca.json`](../config/midday_dca.json), and
> [`config/run_classification.json`](../config/run_classification.json). See
> [morning_dca.md](morning_dca.md) and [run_classification.md](run_classification.md).

> **Live task text compactness (added Jul 20 2026):** the schedule service appears
> to **truncate or drop the tail of very long scheduled-task text**, so critical
> guardrails placed near the end of a long task can be silently lost. Live
> scheduled task text (morning `42173154`, midday `a6239dae`, afternoon
> `10ce362c`) **must stay compact enough that critical guardrails are not pushed
> past the service truncation boundary**. Keep every essential safeguard inside the
> accepted (non-truncated) text and point at the repo config/docs source of truth
> for full detail rather than inlining long-form rules that risk truncation. The
> essential guardrails that must remain in the task text are: raw input
> self-containment / generation before invoking any saved runner; the state-file
> guarantee and no-half-done-run rule; the same-day top-up guard; and the rule that
> manual recovery artifacts are prior-allocation / top-up context only, never
> scheduled evidence. For the **3:20 task** specifically, `10ce362c` was replaced
> on Jul 20 2026 with a compact **~5,850 character** source-of-truth version that
> keeps all essential safeguards inside the accepted text, including the **RAW INPUT
> SELF-CONTAINMENT FIX ADDED JUL 20 2026**, the **STATE FILE GUARANTEE AND NO
> HALF-DONE RUN RULE ADDED JUL 20 2026**, the same-day top-up guard, the Jul 20
> midday recovery artifact path
> `/home/user/workspace/cron_tracking/midday_dca/manual_recovery/20260720_1259ET`,
> and the filled AAPL/TRIN/ASML/IRDM recovery context (AAPL $1.00, TRIN $2.00,
> ASML $2.00, IRDM $2.00) as prior-allocation / top-up context only.
> Machine-readable source:
> [`config/scheduled_tasks.json`](../config/scheduled_tasks.json) →
> `live_task_text_compactness_policy`.

> **Pointer output normalization (added Jul 20 2026):** the Jul 20 2026 3:20
> scheduled run collected Robinhood portfolio, quotes, tradability, same-day
> orders, and historicals, but large historicals (and possibly other connector
> responses) were only available as `output_path` / `current_session_context`
> tool-output **pointers**. The background run did **not** persist those pointer
> targets into the scheduled raw directory before compute, so it **escalated after
> data collection**. Parent recovery copied the `current_session_context` /
> `tool_calls` / `call_external_tool` outputs into
> `/home/user/workspace/cron_tracking/ce16ba6b/scheduled_runs/20260720_1520ET/raw/`,
> computed from the persisted raw files, placed scheduled top-up orders, and wrote
> `run_state`. Operating requirement for **all daily scheduled DCA tasks that use
> connector data**, especially the **10:30** morning (`42173154`), **12:30**
> midday (`a6239dae`), **3:05** monitor-only signal check (`e3e6f09e`), and
> **3:20** afternoon (`10ce362c`) tasks: any connector response returning an
> `output_path` or `current_session_context` pointer must be **immediately copied
> or normalized into the run raw directory before compute**; compute modules must
> read **only** persisted raw files, never in-memory tool responses;
> **pointer-only collection is not a completed run** and the task must not stop
> after collecting data without writing `run_state`; and if pointer normalization
> fails after one bounded recovery attempt, the task writes its valid failure-state
> file with a concrete `error_class`, empty `submitted_orders.csv` headers where
> applicable, and the missing pointer details. Live task text has been updated with
> **POINTER OUTPUT NORMALIZATION ADDED JUL 20 2026** for the 10:30, 12:30, 3:05,
> and 3:20 tasks. The Jul 20 recovered run (`run_id='scheduled_20260720_1520ET'`,
> `order_submit_status='filled'`, `orders_filled_count=6`, total **$10.00**) filled
> EVR $1.00 @ 333.600800, DXYZ $1.00 @ 24.865000, AMAT $2.00 @ 526.819900, STM
> $2.00 @ 61.450000, NVDA $2.00 @ 202.771200, and GLW $2.00 @ 153.810400. This is
> valid scheduled evidence recovered after the pointer persistence escalation.
> Machine-readable source:
> [`config/scheduled_tasks.json`](../config/scheduled_tasks.json) →
> `pointer_output_normalization_2026_07_20` (mirrored in
> [`config/run_classification.json`](../config/run_classification.json)). See
> [run_classification.md](run_classification.md).

| ID | Name | Type | Trades | Cron (UTC) | ET (EDT) | Status |
|---|---|---|---|---|---|---|
| `b7961407` | Pre-market DCA buying-power dashboard | analytics | no | `00 13 * * 1-5` | 9:00 AM weekdays | active |
| `cd45e95b` | Compact DCA volatility outlier scan | analytics | no | `45 13 * * 1-5` | 9:45 AM weekdays | active |
| `morning-dca-0945` | Morning graded DCA pre-flight validation (`MorningDCAExecutor`) | analytics | no | `45 13 * * 1-5` | 9:45 AM weekdays | active |
| `42173154` | Weekday 10:30 AM morning DCA live Robinhood execution (`MorningDCAExecutor`) | trading | **yes** | `30 14 * * 1-5` | 10:30 AM weekdays | active |
| `a6239dae` | Weekday 12:30 PM midday DCA live Robinhood execution (`MiddayDCAExecutor`) | trading | **yes** | `30 16 * * 1-5` | 12:30 PM weekdays | active |
| `e3e6f09e` | Monitor-only graded DCA signal check | monitoring | no | `5 19 * * 1-5` | 3:05 PM weekdays | active |
| `10ce362c` | Graded DCA automatic live test - $2 allocation (supersedes `ce16ba6b`) | trading | **yes** | `20 19 * * 1-5` | 3:20 PM weekdays | active |
| `d72aa525` | Capital-priority briefing | analytics | no | `25 19 * * 1-5` | 3:25 PM weekdays | active |
| `67aa802b` | Post-trade slippage + compact Notion latest-run sync | analytics | no | `35 19 * * 1-5` | 3:35 PM weekdays | active |
| `a46bf007` | Live-run watchdog | monitoring | no | `40 19 * * 1-5` | 3:40 PM weekdays | active |
| `041d9d90` | Weekly DCA Notion performance dashboard refresh | analytics | no | `0 21 * * 5` | Fri 5:00 PM | active |
| `0656657a` | Monthly graded DCA dashboard refresh | analytics | no | `45 12 1-7 * 1-5` | 1st weekday/mo 8:45 AM | active |
| `bec4a6ee` | Monthly DCA automation cost review | analytics | no | `55 13 1-7 * 1-5` | 1st weekday/mo 9:55 AM | active |
| `f7411889` | One-time reminder: evaluate past Aug 28 | reminder | no | one-shot | 2026-08-15 11:45 AM | active |

## Task details

### `b7961407` — Pre-market DCA buying-power dashboard
- Analytics only, **no trading**. Runs **9:00 AM ET weekdays**.
- Pre-market buying-power dashboard using the current **$2 base 0x-to-5x ladder**.

- **Cost-conscious model instruction (Jul 17 2026):** use a cost-conscious/default model where available; do not use ChatGPT 5.5 or premium models unless needed for failure recovery, safety validation, or connector/debug escalation. Keep daily output compact, write JSON/CSV artifacts, suppress no-op notifications, and leave heavy analysis to Friday weekly or monthly review. See the top-of-file cost-conscious model instruction note.

### `cd45e95b` — Compact DCA volatility outlier scan
- Analytics only, **no trading**. Runs **9:45 AM ET weekdays**.
- Scans only **>5% outliers**; no full 41+ ticker narrative on no-outlier days. Uses the current $2 base 0x-to-5x ladder.

- **Cost-conscious model instruction (Jul 17 2026):** use a cost-conscious/default model where available; do not use ChatGPT 5.5 or premium models unless needed for failure recovery, safety validation, or connector/debug escalation. Keep daily output compact, write JSON/CSV artifacts, suppress no-op notifications, and leave heavy analysis to Friday weekly or monthly review. See the top-of-file cost-conscious model instruction note.

### `morning-dca-0945` — Morning graded DCA pre-flight validation (`MorningDCAExecutor`)
- Analytics / **read-only**, **no trading**. Runs **9:45 AM ET weekdays** (executor mode `preflight`).
- Evaluates morning price weakness vs. yesterday’s regular-session close on the shared **$2 base 0x-to-5x ladder** (`config/morning_dca.json`) as a pre-screen for the 10:30 live evaluator; runs no Robinhood order review and calls no broker placement path. Writes pre-flight artifacts under `/home/user/workspace/cron_tracking/morning_dca_preflight/YYYYMMDD_0945ET/` — `run_state.json`, `validation.csv`, `signals.csv`, `run_summary.json`, and the `latest_preflight.json` pointer (`run_id='morning_preflight_YYYYMMDD_0945ET'`).
- Emits **`eligible_to_execute=true/false` per ticker**. This is a **coarse pre-screen** (is a ticker worth re-evaluating at 10:30?), **NOT** a final buy approval — the 10:30 live run recomputes signals/budget/spread/trend/top-up from scratch and may still decline. A ticker is eligible when it graded to a positive-multiplier BUY at 9:45 with a usable current price and previous close.
- `stream='morning'`, `run_type='morning_dca_preflight'`, `state_schema_version='morning-preflight-v2-machine-complete'`, `valid_afternoon_evidence=false`. Never writes into the afternoon `ce16ba6b` tree or updates `latest_scheduled_run.json`. (A read-only `watchlist` early-warning mode remains available, writing under `cron_tracking/morning_dca_watchlist/`.)

- **Cost-conscious model instruction (Jul 17 2026):** use a cost-conscious/default model where available; do not use ChatGPT 5.5 or premium models unless needed for failure recovery, safety validation, or connector/debug escalation. Keep daily output compact, write JSON/CSV artifacts, suppress no-op notifications, and leave heavy analysis to Friday weekly or monthly review. See the top-of-file cost-conscious model instruction note.

### `42173154` — Weekday 10:30 AM morning DCA live Robinhood execution (`MorningDCAExecutor`)
- **Live trading** (executor mode `live`). Runs **10:30 AM ET weekdays** — cron `30 14 * * 1-5` (**14:30 UTC weekdays** during EDT) — moved from the former 9:45 AM live slot to give a true **60-minute intraday trend window**.
- Uses the shared **$2 base 0x-to-5x live ladder** and the `morning` budget ledger (`config/morning_dca.json`). **Robinhood account 562057042 is the only execution broker**; **Alpaca, Finance, and Yahoo are read-only** validation/account context only.
- **Depends on the same-day 9:45 pre-flight artifact** (`cron_tracking/morning_dca_preflight/`) and evaluates **only `eligible_to_execute=true` tickers**, then **recomputes a fresh price, prior close, spread, 60-minute downtrend, signal, and budget before placing** — and **skips / routes to manual review if the trend cannot be validated**. If the pre-flight artifact is missing, a **scheduled** run fails closed (`_apply_preflight_gate()` returns zero tickers and writes a blocked `run_state.json`); only a **manual** run degrades to the full universe with a warning.
- **Pre-authorized direct placement (scheduled live runs only):** as approved Fri Jul 10 2026 11:20 AM ET, this scheduled live task auto-places qualifying buys within all caps/circuit breakers **without per-order confirmation** and **without calling `review_equity_order`**; it calls `place_equity_order` directly only after every safeguard passes. **Manual / ad hoc runs still go through Robinhood's review + confirmation path:** `review_equity_order` must pass before `place_equity_order` is attempted, and if Robinhood's own review requires additional post-review confirmation (e.g. a step-up/2FA gate), placement raises `OrderConfirmationRequiredError` and the run records `not_submitted_confirmation_required` with **no order placed** — there is no separate confirmation step implemented in this code beyond that broker-driven gate.
- **GFUZ special handling:** GFUZ is in the active universe, but **Robinhood is its primary source** because **Finance may not resolve GFUZ**; an unresolved GFUZ on Finance **must not block the whole run**. The *intent* is that if the **9:45 pre-flight lacks GFUZ**, the 10:30 task runs a **supplemental GFUZ read-only validation** and **persists `gfuz_supplemental_preflight.json`** under `/home/user/workspace/cron_tracking/morning_dca/YYYYMMDD_1030ET/` before GFUZ decisioning — **not yet implemented**: today a missing GFUZ preflight row just gates GFUZ out (safe, fail-closed) via the plain `tickers ∩ eligible_tickers` intersection. See `config/morning_dca.json`'s `gfuz_special_handling.supplemental_preflight.implementation_status`.
- **Raw data persistence (added Jul 15 2026, confirmed Jul 16 2026):** the *intended* contract is that **before any signal computation, budget calculation, top-up calculation, or order placement**, the task persists every raw Robinhood/Finance/Alpaca/Yahoo connector response under `/home/user/workspace/cron_tracking/morning_dca/YYYYMMDD_1030ET/raw/` with deterministic filenames, **normalizing** `output_path`/wrapped outputs into parseable JSON, with the decision engine reading only from the persisted files (fetch → persist → normalize → compute → place), and if data fetch succeeds but raw persistence/normalization fails, placing no orders and still writing `run_state.json` with the blocking reason — **not yet implemented**: `MorningDCAExecutor.run()` evaluates directly from the in-memory data provider. See `config/morning_dca.json`'s `raw_data_persistence.implementation_status`.
- **Automatic failure recovery:** the *intended* contract is that if the run fails because of internal persistence, parsing, wrapper-normalization, transient connector throttling, or a raw-file write problem **after data was fetched**, it immediately **reruns the required read-only inputs once**, persists all raw responses first, **recomputes only from the persisted files**, and continues the same guarded execution if safeguards pass, reusing the **same logical run directory** with **`recovery_rerun=true`** and **never classifying recovery as manual** — **not yet implemented**: there is no recovery-rerun code path in `morning_dca.py` today. See `config/morning_dca.json`'s `automatic_failure_recovery.implementation_status`.
- **Confidence rule:** if a candidate's price/drop passes the ladder but the **intraday trend cannot be validated** (fewer than `trend_min_bars` usable intraday bars at/before the decision time), the run does **not** auto-buy; it classifies the candidate as `execution_status='manual_review_candidate'`. There is no loosened trend-unavailable execution path.
- **Hard circuit breakers:** max per order **$10**, budget caps, available buying power, whitelist-only, data confidence, spread threshold, trend validation, regular-hours-only, and one strategy order per ticker per run — each **skips** rather than resizing past a cap.
- **Artifacts:** always writes `run_state.json` and `morning_signals.csv` under `/home/user/workspace/cron_tracking/morning_dca/YYYYMMDD_1030ET/` (`run_id='morning_live_YYYYMMDD_1030ET'`) — **not** `signals.csv`, and **not** `run_summary.json`/`validation.csv` (those are preflight-only). It additionally writes `submitted_orders.csv` whenever at least one order was actually submitted; `filled_orders.csv` is **never** written by this task (there is no fill-confirmation step). See `config/morning_dca.json`'s `artifacts.manifest_accuracy_note`.
- **Raw input persistence (implemented Jul 21 2026):** before `evaluate_signal()`/`_place_broker_orders()` computes from a connector response, that response (previous close, intraday bars, quotes, bid/ask, account number, buying power, tradability, same-day order existence) is persisted under `.../morning_dca/YYYYMMDD_1030ET/raw/`. A persistence failure caught before any order is placed blocks the run with `status='blocked'` and `error_class='missing_or_invalid_raw_inputs'`; a failure discovered only after placement has begun is recorded on `raw_persistence_error` without retroactively blocking. Applies identically to the 12:30 midday task. See `config/morning_dca.json`'s `raw_data_persistence.implementation_status`.

- **Cost-conscious model instruction (Jul 17 2026):** use a cost-conscious/default model where available; do not use ChatGPT 5.5 or premium models unless needed for failure recovery, safety validation, or connector/debug escalation. Keep daily output compact, write JSON/CSV artifacts, suppress no-op notifications, and leave heavy analysis to Friday weekly or monthly review. See the top-of-file cost-conscious model instruction note.

### `a6239dae` — Weekday 12:30 PM midday DCA live Robinhood execution (`MiddayDCAExecutor`)
- **Live trading** (executor mode `live`). Runs **12:30 PM ET weekdays** (window 12:25–12:35 PM ET).
- Uses the shared **$2 base 0x-to-5x live ladder** (`config/midday_dca.json`, the same ladder used by all live streams as of Jul 10 2026) and its own `midday` budget ledger. **Robinhood account 562057042 only**; Alpaca is read-only for validation/context.
- Same active universe (excludes BYDDY/VCX/TRLV/RVI). **Confidence rule:** if a candidate's price/drop passes the ladder but the **intraday trend cannot be validated** (fewer than `trend_min_bars` usable intraday bars at/before 12:30), the run does **not** auto-buy; it classifies the candidate as `execution_status='manual_review_candidate'`.
- Writes live artifacts under `/home/user/workspace/cron_tracking/midday_dca/YYYYMMDD_1230ET/` (`run_id='midday_live_YYYYMMDD_1230ET'`) and updates its own `latest_midday_run.json` pointer.
- `stream='midday'`, `run_type='midday_dca_live'`, `state_schema_version='midday-live-v1'`, `cron_id='a6239dae'`, `supersedes_cron_id=null`, `valid_afternoon_evidence=false`. Never writes into the morning or afternoon (`ce16ba6b`) trees or updates `latest_scheduled_run.json`.
- **Automatic failure recovery:** if the run fails on internal persistence, parsing, wrapper-normalization, transient connector throttling, or raw-file write problems **after data was fetched**, it is a recovery trigger, not a terminal stop. The task reruns the required read-only inputs **once**, **persists all raw responses first**, **recomputes only from the persisted files**, and continues through the same pre-authorized guarded path if every safeguard still passes. The recovery reuses the same `midday_dca/YYYYMMDD_1230ET/` scheduled run directory with `recovery_rerun=true` and stays `stream='midday'` scheduled (**never** reclassified as manual — distinct from the `manual_reruns/` override tree). If the recovery attempt also fails or a safety/account/order rejection occurs, it writes `run_state.json` with the blocking reason and escalates.
- **Raw input generation and failure-state fix (Jul 20 2026):** after the Jul 20 2026 12:30 run escalated, the task must **generate the same-day raw inputs before invoking any saved runner** (e.g. `.../cron_tracking/a6239dae/run_midday_live.sh` or `midday_live_runner.py`). It creates `.../midday_dca/YYYYMMDD_1230ET/raw/` and persists `raw/portfolio.json`, `raw/quotes.json` (or named quote batches normalized into it), and the historical/trend files for the 60-minute trend check, passing the current America/New_York date as `RUN_DATE_ET=YYYYMMDD` when required; it must **not** assume `raw/quotes.json` or historicals exist from a prior run. Inputs come from Finance primary, Alpaca/Robinhood validation-context, and `yfinance` fallback only when connector data is unavailable, stale, incomplete, or unresolved; one bounded recovery attempt on the alternate source path is allowed, then stop. The run **always** writes `run_state.json`, `run_summary.json`, and `latest_midday_run.json` even with no orders or a blocked run; on missing/malformed/stale/ungenerated raw inputs it writes a valid scheduled `run_state` with `run_origin='scheduled'`, `manual_run=false`, `stream='midday'`, `scheduled_for_et='12:30'`, `cron_id='a6239dae'`, `valid_afternoon_evidence=false`, `order_submit_status='not_submitted_error'`, `error_class='missing_or_invalid_raw_inputs'`, `orders_submitted_count=0`, and a compact list of missing files or failed data sources — proof the task **fired**, not order/fill evidence. Do not escalate without writing it unless the workspace itself is unavailable. Same-day manual missed-10:30 recovery artifacts are loaded **after** raw data is available and **before** order sizing, stay prior-allocation/top-up context only, and never satisfy raw input requirements, enter the scheduled evidence stream, or prove the 10:30 scheduled task ran. See [midday_dca.md](midday_dca.md#raw-input-generation-and-failure-state-guarantee-added-jul-20-2026).
- **Same-day top-up guard:** morning/manual earlier same-day allocations (before **2:30 PM ET**, non-scheduled) are **prior-allocation context only** — never midday scheduled evidence. Per ticker, the run buys only the **incremental** dollar difference (midday desired minus amount already bought) and only when the midday decision price is **strictly below** the prior reference price; otherwise **$0**. The run_state records `prior_same_day_amount`, `prior_same_day_reference_price`, `prior_same_day_run_id`/`prior_same_day_source`, `midday_desired_amount`, `midday_decision_price`, `incremental_order_amount`, `top_up_guard_result`, `top_up_skipped`, and `top_up_manual_review`. Midday fills count only as prior-allocation context for the afternoon 3:20 PM top-up guard. See [midday_dca.md](midday_dca.md).
- **Live reliability fix (Jul 17 2026):** before calling `place_equity_order`, describe the Robinhood connector schemas needed in-session (`place_equity_order` and `get_equity_orders`); always write the final `run_state.json`, `run_summary.json`, `signals.csv`, `submitted_orders.csv`, and `filled_orders.csv` where applicable, even for no-order/blocked/failed/recovery runs; every actionable signal ends with exactly one persisted result (`order_id`, broker rejection, top-up/budget/cap/data-confidence/spread/trend/safety block); an actionable signal missing both an `order_id` and a block/rejection is classified `execution_lineage_incomplete`, triggering one recovery retry in the same scheduled directory for only the missing tickers with the same `ref_id` values, reconciling duplicate `ref_id` or timeout through `get_equity_orders` before declaring failure. Manual overrides stay `manual_run=true` and never count as scheduled evidence. See the top-of-file live reliability fix note.
- **Live 12:30 no-placeholder fix (Jul 17 2026):** the scheduled midday run must **never** write placeholder signals or empty `submitted_orders.csv` / `filled_orders.csv` while marking `valid_midday_evidence=true`; if it cannot compute full signals it must write `valid_midday_evidence=false` with concrete `blocking_errors` and notify; it must parse Robinhood `get_equity_quotes` results from `data.results[].quote` and `data.results[].close`; it must follow `output_path` pointers for large `get_equity_historicals` responses and load the referenced result JSON before computing trend; a placeholder artifact with `blocking_errors=['persistence_placeholder_no_signal_compute']` is a **failed run** that must trigger recovery, not a completed scheduled run. See the top-of-file no-placeholder fix note.
- **Jul 17 2026 12:30 recovery result:** the original background task escalated after writing placeholder artifacts with `blocking_errors=['persistence_placeholder_no_signal_compute']`. Parent recovery persisted raw quote, history, tradability, portfolio, and order-history files under `/home/user/workspace/cron_tracking/midday_dca/20260717_1230ET/raw/`, corrected the nested Robinhood quote parser and the `output_path` historical parser, and found **exactly one** eligible scheduled midday buy: **TRIN $3.00** (`order_id 6a5a5b53-6502-476d-b6d4-9cc73bab4f31`, state **filled**, `average_fill_price 17.736500`, `quantity 0.169142`, `created_at 2026-07-17T16:41:55.91467Z`). Marked `valid_midday_evidence=true`, `valid_afternoon_evidence=false`; the Jul 17 morning manual override remains manual context only.
- **Manual missed-12:30 recovery (Jul 20 2026):** after the Jul 20 2026 12:30 scheduled midday run failed, a human placed the missed midday buys by hand at 12:59 ET under `/home/user/workspace/cron_tracking/midday_dca/manual_recovery/20260720_1259ET` (fills **AAPL $1.00** @ 324.389900, **TRIN $2.00** @ 17.399900, **ASML $2.00** @ 1739.506300, **IRDM $2.00** @ 46.669900). It carries `run_origin='manual'`, `manual_run=true`, `same_day_top_up_context_only=true`, `valid_scheduled_evidence=false` and is prior-allocation / same-day top-up context **only** for the 3:20 afternoon run — never midday scheduled evidence, never proof the 12:30 scheduled task ran, and never counted in scheduled reliability, fill counts, slippage, cost basis, or watchdog confirmation. Machine-readable source: `config/scheduled_tasks.json` → `midday_dca_manual_recovery_2026_07_20` (mirrored in `config/midday_dca.json`). See [midday_dca.md](midday_dca.md#manual-missed-1230-recovery-added-jul-20-2026).

- **Cost-conscious model instruction (Jul 17 2026):** use a cost-conscious/default model where available; do not use ChatGPT 5.5 or premium models unless needed for failure recovery, safety validation, or connector/debug escalation. Keep daily output compact, write JSON/CSV artifacts, suppress no-op notifications, and leave heavy analysis to Friday weekly or monthly review. See the top-of-file cost-conscious model instruction note.

### `e3e6f09e` — Monitor-only graded DCA signal check
- Analytics and notifications only; **no trades**. Runs **3:05 PM ET weekdays**.
- Monitor-only signal check that surfaces the planned allocation and tier selection using the current $2 base 0x-to-5x ladder.
- **Pointer output normalization (Jul 20 2026):** although this task places no orders, it uses connector data, so any connector response returning an `output_path` or `current_session_context` pointer must be copied or normalized into the run raw directory before compute, and compute must read only the persisted raw files. See `config/scheduled_tasks.json` → `pointer_output_normalization_2026_07_20`.

- **Cost-conscious model instruction (Jul 17 2026):** use a cost-conscious/default model where available; do not use ChatGPT 5.5 or premium models unless needed for failure recovery, safety validation, or connector/debug escalation. Keep daily output compact, write JSON/CSV artifacts, suppress no-op notifications, and leave heavy analysis to Friday weekly or monthly review. See the top-of-file cost-conscious model instruction note.

### `10ce362c` — Graded DCA automatic live test - $2 allocation (the only trading task)
- Repaired live task `10ce362c`, which **supersedes the deleted `ce16ba6b`**; the artifact root stays under `.../ce16ba6b` for continuity.
- Approved live trading task running the **$2 base 0x-to-5x ladder with tiered caps**. Places real market buy orders automatically within safeguards. Runs **3:20 PM ET weekdays**.
- Uses **only** Robinhood account `562057042`; Alpaca is read-only.
- Trigger gate: `programmatic_trigger_check.sh`, allowing weekdays **2026-07-02 through 2026-08-28**, **3:20–3:35 PM ET** (aligned to the 15:15–15:35 ET scheduled classification window so a late-but-permitted start cannot place buys after the 3:35 PM post-trade sync).
- Writes standardized same-day state files under `/home/user/workspace/cron_tracking/ce16ba6b` ([`schemas/run_state.schema.json`](../schemas/run_state.schema.json)); must **not** fall back to interactive confirmation.
- **State-file guarantee:** **always** writes the scheduled `run_state.json` and updates `latest_scheduled_run.json` on every fire, **even when no order is placed**. A confirmation-blocked run (review passed, submission blocked by required post-review confirmation) records `order_submit_status='not_submitted_confirmation_required'`, `reviewed_order` details where available, candidate details, and **no `order_id`** — valid scheduled-run evidence, not order/fill evidence.
- **Run classification:** a scheduled run writes `scheduled_runs/YYYYMMDD_1520ET/run_state.json` (`run_origin='scheduled'`, `manual_run=false`, `scheduled_for_et='15:20'`, `state_schema_version='run-classification-v1'`, `run_id='scheduled_YYYYMMDD_1520ET'`, and **both** `cron_id='10ce362c'` **and** `supersedes_cron_id='ce16ba6b'`) and updates `latest_scheduled_run.json`; a manual run writes `manual_runs/<timestamp>/run_state.json` (`run_origin='manual'`, `manual_run=true`) and never updates that pointer. Placeholder/ambiguous ids such as `ce16ba6b_replacement` are forbidden; for continuity, consumers still accept `cron_id='10ce362c'` OR `supersedes_cron_id='ce16ba6b'` as valid evidence. See [run_classification.md](run_classification.md).
- **Manual runs do not count as scheduled-run evidence.** Any stale stored display name is superseded by the authoritative **3:20 PM ET** schedule.
- **RAW DATA PERSISTENCE FIX ADDED JUL 10 2026:** the 2026-07-10 3:20 PM run
  (`cron_id 10ce362c`) escalated because quotes and historicals were fetched
  successfully but the **raw connector responses were never persisted to disk
  before signal computation** — the bash compute step had no
  `quotes_batchN.json` / `historicals_batchN.json` to read. From this date on,
  **before signal computation** the scheduled run must **persist every raw
  Robinhood/Finance/Alpaca/Yahoo connector response** it used under
  `scheduled_runs/YYYYMMDD_1520ET/raw/` with deterministic filenames:
  `quotes_batchN.json`, `historicals_batchN.json`, `tradability_pre_place.json`,
  `portfolio_pre_place.json`, `orders_after_place.json`, and `place_SYMBOL.json`
  where applicable.
  - **Normalize before persisting.** Large connector outputs that return an
    `output_path` pointer instead of inline data must be normalized into
    parseable JSON (the referenced file's contents embedded under `data`, the
    original pointer preserved under `output_path`) so the compute step can parse
    the saved file directly.
  - **Compute reads persisted files only.** Signal computation must read from the
    persisted/normalized files under `raw/`, **not** from in-memory tool
    responses. Fetch → persist → normalize → compute → place, in that order.
  - **Persistence failure still writes state.** If raw persistence or
    normalization fails, the task must **still write a valid scheduled**
    `run_state.json` with `run_origin='scheduled'`, `manual_run=false`,
    `order_submit_status='not_submitted_error'`, the error in
    `raw_persistence_error`, empty `raw_inputs_saved`, and **no orders placed** —
    rather than escalating without any state.
  - The run_state records `raw_input_dir` (absolute path to `raw/`),
    `raw_inputs_saved` (filenames actually written), and
    `recovery_from_escalation` (true when the state was produced by recovering an
    escalated run). Helpers: `scripts/run_classification.py`
    (`raw_input_dir`, `save_raw_input`, `normalize_connector_payload`,
    `build_raw_persistence_state`, `raw_persistence_failure_run_state`); schema
    fields in [`schemas/run_state.schema.json`](../schemas/run_state.schema.json).
  - **Recovered run (2026-07-10 3:20 PM ET):** the escalated run was recovered by
    re-fetching quotes/historicals, saving raw connector outputs under
    `/home/user/workspace/cron_tracking/ce16ba6b/scheduled_runs/20260710_1520ET/raw/`,
    normalizing the large `output_path` pointers, computing signals from those
    files, then placing qualifying incremental orders: **9 incremental scheduled
    afternoon orders totalling $31.50**, saved under
    `/home/user/workspace/cron_tracking/ce16ba6b/scheduled_runs/20260710_1520ET/`.
    These are valid afternoon scheduled evidence. Broker policy is unchanged
    (Robinhood-only execution; Alpaca/Finance/Yahoo read-only; no
    `review_equity_order` in scheduled live runs).
- **Same-day top-up guard:** loads earlier same-day (before **2:30 PM ET**, non-scheduled) allocations from its own artifact root **plus** the morning-live root (`.../cron_tracking/morning_dca`) and the midday-live root (`.../cron_tracking/midday_dca`, which also holds manual override reruns under `manual_reruns/`), and offsets each ticker so it buys only the incremental amount `max(0, desired − prior)` into a lower price. The prior reference price is resolved by preference: logged `target_price`, then `decision_price`, then `average_fill_price` (from `orders.csv`/`filled_orders.csv`); a missing reference → `manual_review_candidate` at `$0`. Per-candidate the run_state records `prior_same_day_amount`, `prior_same_day_reference_price`, `prior_same_day_run_id`/`prior_same_day_source`, `afternoon_desired_amount`, `afternoon_decision_price`, `incremental_order_amount`, `top_up_guard_result`, `top_up_skipped`, and `top_up_manual_review`. Morning/midday/manual override prior-context fills are **never** afternoon scheduled evidence, slippage, reliability, fill counts, or cost basis. A fully-offset day still writes a valid scheduled run_state. See [strategy.md](strategy.md#same-day-prior-run-offset--top-up-guard).
- **Final buy of the day behavior:** for the **final live buy of the day** the run performs a **full active-universe scan** and, **for this final-buy decision only**, **ignores the 60-minute downward-trend filter**. Every other gate still holds: current/decision price **strictly below the prior completed regular-session close**, ladder **multiplier > 0** (from the $2-base 0x-to-5x ladder), **data-confidence** pass, **`spread_ok=true`**, **budget/tiered-cap** pass, **regular trading hours only**, **whitelisted (active-universe) ticker**, and all **hard circuit breakers** clear. The same-day top-up guard above still applies to the final buy. (Morning 10:30 and midday 12:30 live runs still require trend validation unless changed later.) See [strategy.md](strategy.md#final-buy-of-the-day-320-pm-et-task-10ce362c).
- **Jul 13 2026 final manual override incident:** the scheduled 3:20 PM ET run placed **0 orders**; the user then requested the final buy **manually**. A final manual-review scan ran the final-buy behavior above (full scan, trend ignored) with same-day top-up offsets and filled **34 orders**, planned total **$234.00** (`DFTX TRIN SPCX LUNR IRDM DXYZ LITE ASML DRAM STX AMAT AMD CBRS MRVL BTX STM SNDK WDC TSM AVGO NVDA QBTS FLKR INTC GOOGL GOOG GLW GS MU XLG TOPT QTOP SATA RGTI`). These are **manual final-buy overrides / same-day top-up context only — NOT scheduled-afternoon execution evidence**. Their run_state carries `run_type='final_manual_buy_override'`, `run_origin='manual'`, `manual_run=true`, `valid_afternoon_scheduled_evidence=false`, `same_day_top_up_context_only=true`. Artifacts (run_state + `filled_orders.csv` with all order IDs) were backfilled under `/home/user/workspace/cron_tracking/ce16ba6b/manual_runs/final_manual_override_20260713_1536ET/` and `/home/user/workspace/cron_tracking/midday_dca/manual_reruns/final_manual_override_20260713_1536ET/`. All order IDs are excluded from scheduled slippage/fills/reliability/cost basis/watchdog confirmation and shown only in the **"Manual Final Buy Override / Same-Day Top-Up Context"** section. See [run_classification.md](run_classification.md) and [strategy.md](strategy.md#manual-final-buy-override--2026-07-13-incident).
- **Automatic failure recovery:** if the run fails on internal persistence, parsing, wrapper-normalization, transient connector throttling, or raw-file write problems **after data was fetched**, it is a recovery trigger, not a terminal stop. The task reruns the required read-only inputs **once**, **persists all raw responses first**, **recomputes only from the persisted files**, and continues through the same pre-authorized guarded path if every safeguard still passes. The recovery reuses the same `scheduled_runs/YYYYMMDD_1520ET/` directory with `recovery_rerun=true`, keeps `run_origin='scheduled'` (both `cron_id='10ce362c'` and `supersedes_cron_id='ce16ba6b'`), and still updates `latest_scheduled_run.json` — it is **never** reclassified as manual. If the recovery attempt also fails or a safety/account/order rejection occurs, it writes `run_state.json` with the blocking reason and escalates.
- **Live reliability fix (Jul 17 2026):** before calling `place_equity_order`, describe the Robinhood connector schemas needed in-session (`place_equity_order` and `get_equity_orders`); always write the final `run_state.json`, `run_summary.json`, `signals.csv`, `submitted_orders.csv`, and `filled_orders.csv` where applicable, even for no-order/blocked/failed/recovery runs; every actionable signal ends with exactly one persisted result (`order_id`, broker rejection, top-up/budget/cap/data-confidence/spread/trend/safety block); an actionable signal missing both an `order_id` and a block/rejection is classified `execution_lineage_incomplete`, triggering one recovery retry in the same scheduled directory (`scheduled_runs/YYYYMMDD_1520ET/`) for only the missing tickers with the same `ref_id` values, reconciling duplicate `ref_id` or timeout through `get_equity_orders` before declaring failure. Manual overrides stay `manual_run=true` and never count as scheduled evidence. See the top-of-file live reliability fix note.
- **Pointer output normalization (Jul 20 2026):** the Jul 20 3:20 scheduled run collected portfolio, quotes, tradability, same-day orders, and historicals, but large historicals (and possibly other connector responses) were only available as `output_path` / `current_session_context` tool-output **pointers** that the background run never persisted into the scheduled raw directory before compute, so it **escalated after data collection**. Any connector response returning an `output_path` or `current_session_context` pointer must be **immediately copied or normalized into the run raw directory before compute**; compute must read **only** persisted raw files; **pointer-only collection is not a completed run** (never stop after collecting data without writing `run_state`); and if pointer normalization fails after one bounded recovery attempt, write the valid failure-state file with a concrete `error_class`, empty `submitted_orders.csv` headers where applicable, and the missing pointer details. Parent recovery copied the `current_session_context` / `tool_calls` / `call_external_tool` outputs into `.../ce16ba6b/scheduled_runs/20260720_1520ET/raw/`, computed from the persisted files, and placed scheduled top-up orders: `run_id='scheduled_20260720_1520ET'`, `order_submit_status='filled'`, `orders_filled_count=6`, total **$10.00** (EVR $1.00 @ 333.600800, DXYZ $1.00 @ 24.865000, AMAT $2.00 @ 526.819900, STM $2.00 @ 61.450000, NVDA $2.00 @ 202.771200, GLW $2.00 @ 153.810400), valid scheduled evidence recovered after the escalation. Live task text updated with **POINTER OUTPUT NORMALIZATION ADDED JUL 20 2026** for 10:30, 12:30, 3:05, and 3:20. Machine-readable source: `config/scheduled_tasks.json` → `pointer_output_normalization_2026_07_20` (mirrored in `config/run_classification.json`).
- **Compact live task text (Jul 20 2026):** because the schedule service appears to truncate or drop the tail of very long task text, `10ce362c` was replaced with a compact **~5,850 character** source-of-truth version that keeps all essential safeguards inside the accepted text: the **RAW INPUT SELF-CONTAINMENT FIX ADDED JUL 20 2026**, the **STATE FILE GUARANTEE AND NO HALF-DONE RUN RULE ADDED JUL 20 2026**, the same-day top-up guard, the Jul 20 midday recovery artifact path `/home/user/workspace/cron_tracking/midday_dca/manual_recovery/20260720_1259ET`, and the filled AAPL/TRIN/ASML/IRDM recovery context as prior-allocation / top-up context only. Live task text must stay compact so critical guardrails are not pushed past the truncation boundary. Machine-readable source: `config/scheduled_tasks.json` → `live_task_text_compactness_policy`.
- **3:20 runner self-containment and state-file guarantee (Jul 20 2026):** part of the full Jul 20 2026 hardening after the 10:30 and 12:30 runs failed. Before invoking any saved runner or order module the task must **generate its same-day raw inputs**, creating `.../ce16ba6b/scheduled_runs/YYYYMMDD_1520ET/raw/` and persisting portfolio / buying power, quotes, prior close, tradability, `orders_after_1430ET` (afternoon evidence checks), the same-day prior-allocation context from the morning-live, midday, manual missed-morning recovery, and manual override roots, and the historical / trend files — passing `RUN_DATE_ET=YYYYMMDD` when required and never reusing stale files (Finance primary, Alpaca/Robinhood validation-context, `yfinance` fallback only; one bounded recovery attempt then stop). It **always** writes `run_state.json`, `run_summary.json`, and `latest_scheduled_run.json` even when blocked, a no-op, or on error; on a block it writes a valid scheduled `run_state` with `run_origin='scheduled'`, `manual_run=false`, `stream='afternoon'`, `scheduled_for_et='15:20'`, `cron_id='10ce362c'`, `supersedes_cron_id='ce16ba6b'`, `state_schema_version='run-classification-v1'`, `orders_submitted_count=0`, a concrete `order_submit_status`, an `error_class` such as `missing_or_invalid_raw_inputs` or `no_incremental_top_up`, and a compact list of missing files / block reasons / failed sources — proof the task **fired**, not order/fill evidence. A post-raw failure still writes `signals.csv` when possible, an empty `submitted_orders.csv` with headers when no orders, and `run_state.json` (no half-done run). Manual recovery artifacts (missed-morning and manual 12:30) stay prior-allocation / top-up context only and are never afternoon scheduled evidence. Machine-readable source: `config/scheduled_tasks.json` → `live_schedule_hardening_2026_07_20.afternoon_1520` (mirrored in `config/run_classification.json`). See [run_classification.md](run_classification.md).

- **Cost-conscious model instruction (Jul 17 2026):** use a cost-conscious/default model where available; do not use ChatGPT 5.5 or premium models unless needed for failure recovery, safety validation, or connector/debug escalation. Keep daily output compact, write JSON/CSV artifacts, suppress no-op notifications, and leave heavy analysis to Friday weekly or monthly review. See the top-of-file cost-conscious model instruction note.

#### Final scheduled run — 2026-07-16 (afternoon scheduled, final buy of the day)
- **Scheduled** live run (not a recovery, not a manual override):
  `run_id='scheduled_20260716_1520ET'`, `scheduled_for_et='15:20'`,
  `run_origin='scheduled'`, `manual_run=false`, `cron_id='10ce362c'`,
  `supersedes_cron_id='ce16ba6b'`,
  `state_schema_version='run-classification-v2-preauthorized'`. This was the
  **final Robinhood scheduled buy run of the day**, using **Robinhood account
  `562057042` as the only execution broker** (Alpaca/Finance/Yahoo read-only).
- Used the current **$3 base 0x-to-5x ladder** (`<0.1%=0x`; `0.1–<1%=1x`;
  `1–<2%=1.5x`; `2–<3%=2x`; `3–<4%=2.5x`; `4–<5%=3x`; `5–<6%=3.5x`; `6–<7%=4x`;
  `7–<8%=4.5x`; `>=8%=5x`). As the final buy of the day it **ignored the
  60-minute downward-trend filter for the final-buy decision only**, but still
  required price **below the prior completed regular-session close**,
  **multiplier > 0**, **data confidence**, **spread check**, **budget/tiered
  cap**, **regular-hours-only**, **whitelist (active universe)**, and the
  **same-day top-up guard**.
- **Daily-cap math:** same-day spend before the final run was **$213.00**;
  remaining daily cap before the final run was **$147.00**; the eligible
  ignore-trend planned spend was **$150.00**. To stay within the daily cap,
  **AMZN was explicitly skipped due to the daily cap** — AMZN was the **sole
  eligible ticker skipped for daily cap**, and this skip is **expected
  behavior, not an error**.
- **Result:** the final scheduled run submitted and filled **29 Robinhood buy
  orders totalling $145.50** (`order_submit_status='submitted'`). Artifacts under
  `/home/user/workspace/cron_tracking/ce16ba6b/scheduled_runs/20260716_1520ET/`.
- **Slippage (`67aa802b`):** confirmed **29 scheduled fills**, notional-weighted
  average slippage **+30.03 bps**, median **+13.80 bps**, with **84
  manual/override order IDs excluded** from the scheduled-live metrics. Artifacts
  under `/home/user/workspace/cron_tracking/ce16ba6b/slippage/20260716/`.
- **Watchdog (`a46bf007`):** confirmed a valid scheduled artifact and matched the
  **29 filled broker orders** against the scheduled `filled_orders.csv` with **no
  broker/log mismatch**, and sent an **OK** status update. Summary:
  `cron_tracking/a46bf007/watchdog_runs/20260716_1540ET_run11_summary.json`.
- Machine-readable records: `config/run_classification.json`
  (`final_scheduled_run_2026_07_16`, `slippage_reporting.scheduled_slippage_2026_07_16`)
  and `config/scheduled_tasks.json` (`scheduled_run_outcomes.runs`).

### `d72aa525` — Capital-priority briefing
- Analytics only, **no trading**. Runs **3:25 PM ET weekdays**.
- Uses the current $2 base 0x-to-5x ladder. Sends **email/in-app notification only if at least one ticker triggers 1.5x or higher**; otherwise silent.

### `67aa802b` — Post-trade slippage + compact Notion latest-run sync
- Analytics only, **no trading**. Runs **3:35 PM ET weekdays**.
- Reads the **scheduled** live-run state and Robinhood order history, writes slippage/coverage artifacts, updates the compact Notion **Latest Run** section only.
- **Run classification:** scheduled-live rows exclude all manual order IDs and any `run_origin='manual'`/`manual_run=true` state; manual fills go only in the labeled **Manual Test Runs** section. See [run_classification.md](run_classification.md).
- **Manual exclusion roots + final override (added Jul 13 2026):** collect and exclude manual order IDs from all manual exclusion roots — the task's own `manual_runs/**` **plus** `final_manual_review/**`, `.../cron_tracking/ce16ba6b/manual_runs/final_manual_override_*`, and `.../cron_tracking/midday_dca/manual_reruns/`. Fills from a `final_manual_buy_override` run (e.g. the 2026-07-13 incident) are excluded from scheduled slippage/fills/reliability/cost basis and reported only in the labeled **"Manual Final Buy Override / Same-Day Top-Up Context"** section (`manual_final_buy_override` in [`schemas/slippage_report.schema.json`](../schemas/slippage_report.schema.json)).
- **Confirmation-blocked run:** when the scheduled state has `order_submit_status='not_submitted_confirmation_required'`, report **no scheduled fills and no scheduled order IDs**; do **not** invalidate the run_state and do **not** apply the broker time-window fallback (no write failure occurred).
- **Schema-version acceptance (added Jul 10 2026):** accept scheduled run_states tagged **either** `state_schema_version='run-classification-v1'` **or** `run-classification-v2-preauthorized` as long as the required scheduled identity fields are correct (`run_origin='scheduled'`, `manual_run=false`, `cron_id='10ce362c'` OR `supersedes_cron_id='ce16ba6b'`, `scheduled_for_et='15:20'`, window, `run_id`). v2 is a **stricter successor** of v1 (pre-authorized direct placement), not a different contract.
- **Order-history payload persistence (added Jul 10 2026):** persist the Robinhood order-history payloads used for slippage (e.g. `scheduled_runs/YYYYMMDD_1520ET/raw/orders_after_place.json`) **before** slippage computation, and compute average fill price / executions from the **saved payloads** (`dca_reporting.fills_from_order_history_payload`) rather than re-querying the broker. Record the payload paths in `source_order_history_payloads` (see [`schemas/slippage_report.schema.json`](../schemas/slippage_report.schema.json)).

- **Cost-conscious model instruction (Jul 17 2026):** use a cost-conscious/default model where available; do not use ChatGPT 5.5 or premium models unless needed for failure recovery, safety validation, or connector/debug escalation. Keep daily output compact, write JSON/CSV artifacts, suppress no-op notifications, and leave heavy analysis to Friday weekly or monthly review. See the top-of-file cost-conscious model instruction note.

### `a46bf007` — Live-run watchdog
- Read-only, **no trading**. Runs **3:40 PM ET weekdays** — the **final scheduled afternoon event**.
- Alerts only on missed, incomplete, error, or discrepancy conditions.
- **Run classification:** validates that a scheduled run_state exists and `latest_scheduled_run.json` points at it; a manual-only or broker-orders-only day is a discrepancy, not a scheduled run.
- **Confirmation-blocked run:** when `order_submit_status='not_submitted_confirmation_required'`, alert that **no order was placed**, but do **not** mark the lineage/scheduled state invalid — a confirmation-blocked run with the required fields present is a valid scheduled run that fired.
- **Watchdog scan fix (added Jul 13 2026):** when broker order history shows orders after 3:15 PM ET but no scheduled state accounts for them, scan the manual exclusion roots — `final_manual_review/**`, `.../cron_tracking/ce16ba6b/manual_runs/final_manual_override_*`, and `.../cron_tracking/midday_dca/manual_reruns/` — before flagging a discrepancy. Orders matched to a `final_manual_buy_override` run are manual overrides (`valid_afternoon_scheduled_evidence=false`): exclude them from scheduled-confirmation and do **not** treat them as scheduled evidence. The 3:40 PM 2026-07-13 alert was caused by final-override fills lacking artifacts under the scanned paths; artifacts were backfilled under the two `final_manual_override_20260713_1536ET/` roots. See [run_classification.md](run_classification.md).

- **Cost-conscious model instruction (Jul 17 2026):** use a cost-conscious/default model where available; do not use ChatGPT 5.5 or premium models unless needed for failure recovery, safety validation, or connector/debug escalation. Keep daily output compact, write JSON/CSV artifacts, suppress no-op notifications, and leave heavy analysis to Friday weekly or monthly review. See the top-of-file cost-conscious model instruction note.

### `041d9d90` — Weekly Notion performance dashboard refresh
- Analytics only, **no trading**. Runs **Fridays 5:00 PM ET** — the only approved post-3:40 PM exception.
- Recalculates and refreshes the full Notion performance dashboard. No routine email.
- **Run classification:** weekly scheduled-live metrics use only validated scheduled runs with manual runs/order IDs excluded; manual fills, if shown, are confined to a labeled **Manual Test Runs** section. See [run_classification.md](run_classification.md).
- **Schema-version compatibility (added Jul 10 2026):** include **v2-preauthorized** scheduled runs (`state_schema_version='run-classification-v2-preauthorized'`) in weekly metrics whenever the required scheduled identity fields are present, alongside v1 runs.
- **Slippage fallback (added Jul 10 2026):** if per-day slippage artifacts are missing, compute from the persisted broker-history payloads (`scheduled_runs/YYYYMMDD_1520ET/raw/orders_after_place.json` via `dca_reporting.fills_from_order_history_payload`) **before** declaring slippage unavailable for that day.

#### Recovered slippage — 2026-07-10 (afternoon scheduled)
- **9 valid afternoon scheduled fills**, total **$31.50**.
- Average `slippage_pct` ≈ **0.1047%**; largest absolute slippage symbol **QBTS** ≈ **0.2089%**.
- **46 manual order IDs excluded** from the scheduled-live metrics.
- Artifacts under `/home/user/workspace/cron_tracking/ce16ba6b/slippage/20260710/`; fills computed from the scheduled run's raw `orders_after_place.json`.

### `0656657a` — Monthly graded DCA dashboard refresh
- Analytics only, **no trading**. First weekday of the month **8:45 AM ET**. No-op otherwise.
- **Monthly review scope (added Jul 17 2026):** the monthly review should include
  the **weekly-rollover intraday cap curve** analytics backtest and re-evaluate the
  user-highlighted **35 / 65 / 100** candidate against the alternatives. This is an
  **analytics-only, non-live** candidate: it never changes live schedules or
  execution logic. Machine-readable data:
  [`config/backtest_intraday_cap_curve.json`](../config/backtest_intraday_cap_curve.json);
  methodology and results: [backtest_curve.md](backtest_curve.md).

### `bec4a6ee` — Monthly DCA automation cost review
- Analytics only, **no trading**. First weekday of the month **9:55 AM ET**.
- Reports **usage proxies only**; exact Perplexity credit consumption is not exposed, so do not claim exact credits.

### `f7411889` — One-time reminder
- Reminder only, **no trading**. Fires **2026-08-15 11:45 AM ET** to evaluate continuing live testing past 2026-08-28.
