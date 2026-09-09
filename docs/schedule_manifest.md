# Scheduled Tasks Manifest

> **Stale as of 2026-07-28:** this manifest and its machine-readable source
> (`config/scheduled_tasks.json`) still describe the pre-2026-07-28
> three-stream Perplexity-era live roster (morning `42173154`, midday
> `a6239dae`, afternoon `10ce362c`) using the OLD three-stream naming, but
> those five tasks are still the account's ONLY currently-functioning live
> execution -- do not retire them. A seven-slot Claude roster is the
> intended eventual replacement; its Gate 5 has had a real entry point per
> slot since 2026-08-26 and placement was authorized for all seven slots on
> 2026-09-02 (see `PRODUCTION_READINESS.md`, "Status, 2026-08-26"), so the
> "hard-aborts at Gate 5" wording that survives further down this file is
> historical. [`safeguards.md`](safeguards.md#live-execution-roster) is
> authoritative for the current roster until this file is reconciled.

Backup of the current scheduled tasks. Machine-readable source:
[`config/scheduled_tasks.json`](../config/scheduled_tasks.json).

Operational follow-ups from live-run incidents are tracked in
[`dca_operational_todos.md`](dca_operational_todos.md). Review that file before
changing buying-task schedules, artifact contracts, same-day pointer behavior, or
top-up loading rules.

> **PR #314 pointer fallback update (added 2026-08-03):** PR #314
> (`d905769`) updates `scripts/same_day_pointer.select_same_day_run_state()` so
> the 11:30 catch-up orchestration and other same-day pointer consumers can
> self-heal when the canonical latest pointer is missing. Resolution order is:
> date-specific run_state, canonical latest pointer, caller-supplied fallback
> pointers, then the same-day Perplexity-primary alias pointer derived from the
> trading day and stream. Alias fallback still validates same-day date, stream,
> expected root containment, target `run_state.json`, scheduled non-manual
> evidence, and `run_classification` before returning a state. Missing canonical
> pointers should be recorded as artifact-contract failures, but they are no
> longer terminal when a valid same-day Perplexity-primary alias pointer and
> complete artifacts exist. Operational tracking lives in
> [`dca_operational_todos.md`](dca_operational_todos.md).

**Timezone note:** cron expressions are in **UTC**. ET equivalents below are in
America/New_York and vary with DST (EDT, UTC-4, in Mar-Oct; EST, UTC-5, in Jan,
Feb, Nov, Dec). Re-verify around DST transitions.

> **Afternoon completion rule (rewritten 2026-08-02):** the 3:05 PM monitor-only
> signal check (`e3e6f09e`) and the 3:40 PM read-only live-run watchdog
> (`a46bf007`) were both **removed from the schedule entirely** on 2026-08-02.
> Afternoon **trading** now completes **no later than 3:30 PM ET** (`10ce362c`).
> Same-day **artifact-writing** moved later: the post-trade slippage + compact
> Notion latest-run sync (`67aa802b`) moved from 3:35 PM ET to **5:00 PM ET
> daily**, and the Friday weekly Notion performance dashboard refresh
> (`041d9d90`) moved from 5:00 PM ET to **5:30 PM ET** the same day to clear the
> resulting Friday collision. The month-end volatility risk refresh
> (`c9fb1fb8` / `17a3b2a9`) moved from its own prior 5:00 PM ET to **5:15 PM
> ET** the same day for the same reason -- it independently collided with
> `67aa802b`'s new 5:00 PM slot. These three tasks are the
> only DCA-related tasks approved to run after 3:30 PM ET. See
> [`docs/safeguards.md`](safeguards.md#operational-rules), the authoritative
> source, and `config/scheduled_tasks.json`'s `afternoon_completion_rule`. This
> file's own detailed `e3e6f09e`/`a46bf007` sections below were not fully
> rewritten for this change; treat them as historical.

> **⚠️ NEITHER NIGHTLY TASK IS ACTUALLY REGISTERED WITH A SCHEDULER (verified
> 2026-08-03).** Both `claude-tickers-daily-update` and
> `claude-per-ticker-calibration` carry `"scheduler_registered": false` in
> `config/scheduled_tasks.json`. The times stated below are the **intended**
> schedule, not confirmed firing times — nothing in this repo or any registered
> scheduler causes them to run automatically today. The underlying pipeline is
> real and works correctly when invoked manually (the updater writes
> `ticker_update_event.json`; `scripts/daily_per_ticker_calibration.py` consumes
> it and regenerates `config/per_ticker_sizing.json`), so the gap is
> registration only. **Practical consequence:** unless someone runs these by
> hand, `config/per_ticker_sizing.json` — which governs live per-ticker order
> sizing for all 57 active symbols (plus the two whole-share symbols) — goes
> stale, silently. Read the times below as
> "should run at", not "runs at".
>
> **Nightly ticker update and per-ticker recalculation:**
> `claude-tickers-daily-update` is scheduled for **5:45 PM ET on weekdays** and is the
> separate owner of the managed ticker-history archive. When it finishes, it
> writes a `ticker_update_event.json` handoff listing every changed ticker and
> the subset in the active DCA universe. `claude-per-ticker-calibration` is
> independently scheduled at **6:30 PM ET on Fridays** (changed from weekdays
> 2026-08-05 at the owner's direction, so that once registered it gives
> `config/per_ticker_sizing.json` a weekly refresh instead of none — per the
> registration warning above, this cadence is not actually firing today) to
> rebuild next-session trigger percentages from the local
> archive. It can additionally use the update event
> as an immediate trigger when the external Claude scheduler supports artifact
> events. That event route is available for testing the two-job handoff without
> merging the jobs. Both tasks are
> **Claude only**: neither may be registered, run, mirrored, or counted by
> Perplexity. They are analytics/configuration only and never connect to a
> brokerage or place an order. The external Claude scheduler still needs to
> register the manifest commands; a Codex automation is not a substitute for
> that Claude-only registration.

> **Ticker archive location:** the scheduled updater and recalculation tools
> default to the repository's `ticker_data` path. On this Mac it is a local link
> to the managed Google Drive `Tickers` folder, so the program and Drive use one
> archive rather than separate copies that could drift. `DCA_TICKER_HISTORY_DIR`
> remains an explicit override for a different archive.

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

> **TICKER UNIVERSE UPDATE JUL 27 2026:** RKLB (Rocket Lab Corporation) is
> added to the active DCA universe, used by every universe-bearing scheduled
> task (the pre-market dashboard, the 9:45 scans, the morning/midday/afternoon
> live streams, the monitor-only signal check, the post-trade slippage sync, the
> watchdog, and the weekly/monthly refreshes). The active universe is now **45
> symbols**. Robinhood resolved RKLB as Rocket Lab Corporation Common Stock,
> state active, tradeable true, fractional tradability tradable, regular-hours
> dollar orders supported, and prior close available ($63.91 as of 2026-07-24).
> Existing exclusions remain BYDDY, VCX, TRLV, and RVI; corrected symbols IRDM
> and GLW remain; IBM, GFUZ, and TXN remain included. This adds a whitelisted
> symbol only; it preserves all existing trading safety, pre-authorization,
> top-up, run-classification, budget, cost-conscious, manual-exclusion, and
> recovery text.

> **Schedule integrity note:** the live trading task runs at **3:30 PM ET**; the
> post-trade slippage / Notion sync runs at **5:00 PM ET daily** (moved
> 2026-08-02 from 3:35 PM ET). These are two different tasks. Do not merge them
> or move the live trade off 3:30 PM ET.
>
> **3:30 PM minimum-order aggregate top-up rule (added 2026-08-14):** the
> broker-minimum carry-forward behavior is part of the existing **3:30 PM final
> live pass**, not a separate 3:33 PM task. During the 15:30 final pass, after
> normal current-candidate evaluation and before final artifacts, aggregate
> same-day top-up remainders by ticker when they were not filled earlier only
> because Robinhood's $1 minimum order size blocked them. If a ticker's
> aggregate qualifying remainder is **$1.00 or greater**, buy the aggregate
> remainder amount. If it is **$0.50 to less than $1.00**, round up and buy
> exactly **$1.00**. If it is **less than $0.50**, forget it for that trading
> day. This does not convert spread, confidence, budget, duplicate, tradability,
> or price-reference blocks into buys. It still enforces active whitelist,
> tradability, account access, buying power, duplicate protection, daily cap,
> weekly cap, regular-hours-only, buy-only, max-per-order, no
> `review_equity_order`, and no per-order confirmation. These orders, when
> placed, are valid 15:30 final-pass orders and must be recorded in the normal
> 15:30 artifact contract (`run_state.json`, `run_summary.json`, `signals.csv`,
> `submitted_orders.csv`, `filled_orders.csv`, and raw responses — **not**
> `validation.csv`, which belongs to the morning preflight flow only). Applies
> only to `10ce362c`; see `config/scheduled_tasks.json`'s
> `same_day_top_up_policy.minimum_order_aggregate_topup_1530` for the full rule
> text, its audit-field contract (`aggregate_minimum_topup_applied` /
> `_remainder_usd` / `_order_usd` / `_reason`, recorded only on a submitted
> order), and a documented known gap: qualifying remainders are discovered only
> through the cumulative sub-dollar credit ledger, not yet reconstructed from
> other prior-run artifacts or broker order history when a ledger entry is
> missing (codex review discussion_r3785797973, discussion_r3785797977,
> discussion_r3785797985, discussion_r3785797988).

> **3:30 PM state-file guarantee:** the live task (`10ce362c`) **always** writes
> a scheduled `run_state.json` under
> `cron_tracking/ce16ba6b/scheduled_runs/YYYYMMDD_1530ET/` and updates
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
> `cron_tracking/ce16ba6b/scheduled_runs/YYYYMMDD_1530ET/raw/` with deterministic
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
> (market open `2ba19526` 9:35 AM, morning `42173154` 10:30 AM, intraday
> `7be82b97` 11:30 AM / 1:30 PM / 2:30 PM, midday `a6239dae` 12:30 PM, final
> `10ce362c` 3:30 PM) must invoke `scripts/drift_detection.py` against its own
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
> Slack alert (no webhook is ever hardcoded). The 3:30 PM check must complete
> **before** the 3:40 PM watchdog (`a46bf007`). See
> [drift_detection.md](drift_detection.md) and the `drift_detection_policy` block
> in [`config/scheduled_tasks.json`](../config/scheduled_tasks.json). The drift
> detector shipped in **PR #80** (merge commit `3641b90`) and is wired into every
> live trading task via their `post_artifact_drift_check` blocks. The
> `d1f7a2c0` schedule entry was **retired on 2026-07-27** (it was never a scheduler
> task); the **detector itself is not retired** and every live trading run still
> invokes it in-process.

> **Cost-saving operating mode (requested Thu Jul 16 2026):** the weekday chain
> runs in **compact** mode and all heavy analysis is deferred to the **Friday
> 5:00 PM ET** weekly refresh. Machine-readable source:
> [`config/scheduled_tasks.json`](../config/scheduled_tasks.json) →
> `operating_mode` (`cost_saving_daily_compact`). Daily weekday compact chain:
> **9:35** market-open live (`2ba19526`), **9:45** watchlist early-warning scan
> (`bb4f6ef1`), **10:30** morning live (`42173154`), **11:30 / 1:30 / 2:30**
> intraday live (`7be82b97`), **12:30** midday live (`a6239dae`), **3:30** final
> live (`10ce362c`), **3:35**
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
> preserved** (Robinhood-only execution on `<ROBINHOOD_ACCOUNT_NUMBER>`; Alpaca/Yahoo/Finance
> read-only; buy-only, no options/crypto/margin; active universe includes IBM and GFUZ
> (BYDDY and RVII are whole-share tickers, not excluded -- see docs/whole_share_accumulation.md); manual/morning/midday/final streams stay
> separated; prior-run fills are same-day top-up context only, never scheduled
> evidence). See [cost_tracking.md](cost_tracking.md).

> **Weekly-rollover cap-curve backtest (added Jul 17 2026):** an analytics-only,
> non-live backtest of an intraday cumulative cap curve on a weekly-rollover
> funding model (base $1 for the backtest, $300 weekly add with unused cash rolled
> forward, daily cap = remaining weekly cash / remaining trading days, intraday
> proxy 10:30 open / 12:30 open-low average / 3:30 close, over the same 60 trading
> day price history). The user-highlighted candidate is **35 / 65 / 100**. This
> does **not** change any live schedule or execution logic; the monthly review
> (`0656657a`) should include this analysis. Machine-readable data:
> [`config/backtest_intraday_cap_curve.json`](../config/backtest_intraday_cap_curve.json);
> full methodology and results: [backtest_curve.md](backtest_curve.md).

> **Live reliability fix (added Jul 17 2026):** applied to all three
> pre-authorized live trading tasks (morning `42173154` 10:30 AM, midday
> `a6239dae` 12:30 PM, afternoon `10ce362c` 3:30 PM). It closes the
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
> `valid_afternoon_evidence=false`: excluded from the scheduled 3:30 execution
> evidence, scheduled slippage, scheduled reliability, and scheduled cost-basis
> metrics. They are usable **only** as manual override context and future same-day
> / top-up allocation context. Both artifacts live under the
> `final_manual_review/` manual exclusion root, so their order IDs are collected as
> `excluded_manual_order_ids`. See the
> `live_reliability_fix_2026_07_17.jul17_manual_budget_block_overrides` block in
> [`config/scheduled_tasks.json`](../config/scheduled_tasks.json).
>
> **Nested run_classification requirement (added Jul 17 2026):** the 3:30 PM
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
> run as scheduled or manual. Every scheduled 3:30 PM ET run_state must be
> written with **both** `cron_id='10ce362c'` **and**
> `supersedes_cron_id='ce16ba6b'`; placeholder/ambiguous ids such as
> `ce16ba6b_replacement` are **forbidden**. Scheduled runs write under
> `cron_tracking/ce16ba6b/scheduled_runs/YYYYMMDD_1530ET/` and update
> `latest_scheduled_run.json`; manual runs write under `manual_runs/<timestamp>/`
> and never touch that pointer. The slippage (`67aa802b`), watchdog
> (`a46bf007`), weekly (`041d9d90`), and monthly (`0656657a`) tasks consume only
> validated scheduled runs and exclude manual runs/order IDs. See
> [run_classification.md](run_classification.md).

> **Automatic failure recovery (live trading tasks):** the three scheduled
> **live Robinhood execution** tasks — the **10:30 AM ET** morning task
> (`42173154`), the **12:30 PM ET** midday task (`a6239dae`), and the
> **3:30 PM ET** afternoon task (`10ce362c`). Treat a mid-run failure as a
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
> weekly or monthly review.** It applies to every task in the canonical active
> schedule: the 9:35 AM market-open live execution (`2ba19526`), the 9:45 AM
> watchlist early-warning scan (`bb4f6ef1`), the 10:30 AM morning live execution
> (`42173154`), the 11:30 AM / 1:30 PM / 2:30 PM intraday live executions
> (`7be82b97`), the 12:30 PM midday live execution (`a6239dae`), the 3:05 PM
> monitor-only signal check (`e3e6f09e`, currently paused), the 3:30 PM final live buy
> (`10ce362c`), the 3:35 PM slippage plus Notion latest-run sync (`67aa802b`),
> and the 3:40 PM watchdog (`a46bf007`). It is a model-selection and
> output-compactness rule only and changes no trading, schedule, top-up, budget,
> recovery, run-classification, manual-exclusion, GFUZ, or cost-saving operating
> mode behavior. See the `cost_conscious_model_instruction_2026_07_17` field on
> each task in [`config/scheduled_tasks.json`](../config/scheduled_tasks.json).

> **Same-day top-up guard:** a morning/manual run must **never block** the
> scheduled 3:30 PM ET run. Instead the scheduled run reads earlier same-day
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
> (1) A **9:45 AM ET read-only pre-flight validation** (run by the canonical 9:45
> slot `bb4f6ef1`; the former repo-internal alias `morning-dca-0945` is retired as
> a schedule entry but its artifact contract still applies), run_type `morning_dca_preflight`,
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
> trend window**. **Robinhood account <ROBINHOOD_ACCOUNT_NUMBER> is the only execution broker**;
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
> is a single **12:30 PM ET live Robinhood execution** on account `<ROBINHOOD_ACCOUNT_NUMBER>`,
> writing under `cron_tracking/midday_dca/YYYYMMDD_1230ET/`
> (`run_id='midday_live_YYYYMMDD_1230ET'`). It reuses the **afternoon 0x-to-5x
> ladder** and the same $2 base (the shared live ladder used by all streams as
> of Jul 10 2026), the same
> active universe, the same confidence rule, and
> its own `midday` budget ledger. It carries `stream='midday'` and
> `valid_afternoon_evidence=false`, never updates `latest_scheduled_run.json`,
> and is never afternoon scheduled evidence. Morning/manual same-day fills are
> **prior-allocation context only** for the midday top-up guard (midday buys the
> incremental difference only when its decision price is strictly below the prior
> reference price); midday fills in turn count only as prior-allocation context
> for the afternoon 3:30 PM top-up guard. See [midday_dca.md](midday_dca.md).
> **Base-amount note:** the graded DCA first-allocation base is dynamic: **$2.00**
> when the decision price is below the prior close **and** the prior
> regular-session low, **$1.00** when below the prior close **only** (a missing
> prior low is treated as below-close-only), and **no buy** when not below the
> prior close. Every candidate dollar amount is
> **`selected_base_amount_usd × multiplier`**. The automation must never infer,
> discover, or fall back to **$50.00** or any broker/account default. If the
> base-amount config (`config/budgets.json`) is missing or unreadable, continue
> using **$2.00** (below close and low) or **$1.00** (below close only) as
> hardcoded, user-confirmed fallbacks.

> **Dynamic first-allocation base (Jul 24 2026):** the user approved a dynamic
> base for the first same-day allocation per ticker: **$2** below the prior close
> and prior low, **$1** below the prior close only, no buy otherwise. The order
> amount is `selected_base × multiplier`. The per-order ceiling is unchanged at
> $10 (5x x $2); the $1 scale tops out at $5. This applies to every
> universe-bearing live and analytics task below (morning 10:30, midday 12:30,
> afternoon 3:30, and the analytics/monitor scans). Ladder multipliers, the
> ticker universe, exclusions, budget caps, the top-up guard, pre-authorization,
> confidence, recovery, and all other safety rules are unchanged.

> **Base-amount change (Jul 19 2026):** the user changed all future DCA
> scheduled runs from a $3 base amount to a $2 base amount. Superseded by the
> dynamic first-allocation base above; retained for history.

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
> (`a6239dae`) and the **3:30 final** run (`10ce362c`) **must scan** this path for
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
> **3:30 afternoon** (`10ce362c`, supersedes `ce16ba6b`) scheduled live tasks. Each
> task must **generate its same-day raw inputs before invoking any saved runner or
> order module** — creating its `raw/` directory and persisting portfolio / buying
> power, quotes, prior close, tradability, the run-specific order context (10:30:
> `orders_since_midnight`; 3:30: `orders_after_1430ET` plus same-day
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
> `scheduled_for_et='10:30'`, `cron_id='42173154'`; 3:30: `stream='afternoon'`,
> `scheduled_for_et='15:30'`, `cron_id='10ce362c'`, `supersedes_cron_id='ce16ba6b'`,
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
> TRIN $2.00, ASML $2.00, IRDM $2.00), available to the 3:30 run as
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
> scheduled evidence. For the **3:30 task** specifically, `10ce362c` was replaced
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
> **3:30** afternoon (`10ce362c`) tasks: any connector response returning an
> `output_path` or `current_session_context` pointer must be **immediately copied
> or normalized into the run raw directory before compute**; compute modules must
> read **only** persisted raw files, never in-memory tool responses;
> **pointer-only collection is not a completed run** and the task must not stop
> after collecting data without writing `run_state`; and if pointer normalization
> fails after one bounded recovery attempt, the task writes its valid failure-state
> file with a concrete `error_class`, empty `submitted_orders.csv` headers where
> applicable, and the missing pointer details. Live task text has been updated with
> **POINTER OUTPUT NORMALIZATION ADDED JUL 20 2026** for the 10:30, 12:30, 3:05,
> and 3:30 tasks. The Jul 20 recovered run (`run_id='scheduled_20260720_1530ET'`,
> `order_submit_status='filled'`, `orders_filled_count=6`, total **$10.00**) filled
> EVR $1.00 @ 333.600800, DXYZ $1.00 @ 24.865000, AMAT $2.00 @ 526.819900, STM
> $2.00 @ 61.450000, NVDA $2.00 @ 202.771200, and GLW $2.00 @ 153.810400. This is
> valid scheduled evidence recovered after the pointer persistence escalation.
> Machine-readable source:
> [`config/scheduled_tasks.json`](../config/scheduled_tasks.json) →
> `pointer_output_normalization_2026_07_20` (mirrored in
> [`config/run_classification.json`](../config/run_classification.json)). See
> [run_classification.md](run_classification.md).

## Active schedule (canonical, verified 2026-07-27)

This is the authoritative live schedule. A task that is not in this table is not
active. Machine-readable source:
[`config/scheduled_tasks.json`](../config/scheduled_tasks.json) ->
`canonical_schedule_reconciliation_2026_07_27`.

**Every dated temporary override recorded against this schedule has expired.**
None of them grants any authorization today. They are kept, verbatim, under
[Expired temporary live scheduler overrides](#expired-temporary-live-scheduler-overrides-historical-record)
below, *after* the canonical table, so the first schedule table a reader meets in
this section is the authoritative one. Three of those override blocks used to sit
above it and read in the present tense, which is exactly how an expired
authorization gets mistaken for a live one.

Standing Perplexity backup policy, added Sep 8, 2026 -- PERMANENT, does not expire:

> **ADDED 2026-09-08:** The user directed that Perplexity's backup role be
> standing rather than a recurring week-by-week or same-day grant: "Perplexity
> shouldn't be disabled anywhere permanently... Perplexity is supposed to back
> up Claude if it fails, not be disabled." The three dated override blocks
> below it (Aug 28, Aug 31, Sep 8-11) are the history of how that standing
> intent kept getting re-issued as a series of temporary exceptions instead of
> being recorded once -- each block authorized Perplexity for a handful of
> days and had to explicitly disable the previous batch's leftover task IDs,
> which reads as Perplexity repeatedly being shut off when the actual design,
> recorded in `claude/PERPLEXITY-CLAUDE-COORDINATION.md` since 2026-08-02, has
> always been standing coexistence. This entry replaces that pattern. All five
> backup task IDs below were already live under the Sep 8-11 override; nothing
> new was deployed to make this table current, only the authorization's status
> changed from temporary to standing. Machine-readable source:
> `config/scheduled_tasks.json` -> `standing_perplexity_backup_policy`.

| Canonical ET | Canonical task | Backup ET | Backup task | Trades | Status |
|---|---|---|---|---|---|
| 9:35 AM | `2ba19526` | 9:45 AM | `4412c3c4` | **yes** | standing |
| 10:30 AM | `42173154` | 10:40 AM | `e028361e` | **yes** | standing |
| 11:30 AM | `7be82b97` (11:30 fire) | 11:40 AM | `e3590042` | **yes** | standing |
| 12:30 PM | `a6239dae` | 12:40 PM | `062605c6` | **yes** | standing |
| 1:30 PM | `7be82b97` (1:30 fire) | 1:40 PM | `54ab24d2` | **yes** | standing |
| 2:30 PM | `7be82b97` (2:30 fire) | 2:40 PM | `54ab24d2` | **yes** | standing |
| 3:30 PM | `10ce362c` | 3:40 PM | `54ab24d2` | **yes** | standing |

Each backup fires 10 minutes after its canonical counterpart so Claude gets
first chance to buy; if Claude's slot fails, aborts, or never reaches
placement, Perplexity's own per-slot check (see the coordination doc's
section 4) finds no qualifying order in the window and proceeds. The 12:30 PM
slot's own Gate 4 could not recognize a compatible Perplexity order until a
2026-09-08 fix to `claude/dca-executor-prompts/live-mirror/1230ET-1230-pm-midday-buying-run.txt`
(see `config/scheduled_tasks.json` -> `standing_perplexity_backup_policy.midday_gate4_dependency`
and the coordination doc's section 5.5); the other six slots already
recognized it. This standing policy does not authorize Perplexity to run
scheduled reports, dashboard refreshes, slippage-only reports, monitor-only
reports, or watchdog reports -- those remain Claude-only, same as every
override below it.

Temporary live scheduler override, Aug 28, 2026:

> **ADDED 2026-08-28:** The user explicitly authorized Perplexity to run all
> scheduled buy-only DCA tasks for Friday, Aug 28, 2026, including an immediate
> one-time 9:35 AM catch-up because the normal 9:35 occurrence had already
> passed. The recurring Perplexity buy tasks are date-gated to the current
> America/New_York trading date `2026-08-28` and must silently no-op after that
> date unless the user gives a new authorization. Perplexity's scope for this
> override is scheduled buy-run execution plus required buy-run artifacts and
> notifications only. Claude handles scheduled reports. Machine-readable source:
> `config/scheduled_tasks.json` ->
> `temporary_live_scheduler_overrides.authorize_2026_08_28_all_perplexity_buy_slots_today_only`.

| ET | Live scheduler task | ID | Cadence | Trades | Aug 28 override status |
|---|---|---|---|---|---|
| 9:35 AM | Perplexity live DCA buy pass | `4412c3c4` | Weekdays | **yes** | date-gated active only for 2026-08-28 |
| immediate 9:35 catch-up | Perplexity DCA catch-up buy run | `2c1d3792` | One-time | **yes** | authorized catch-up for 2026-08-28 only |
| 10:30 AM | Perplexity live DCA buy pass | `e028361e` | Weekdays | **yes** | date-gated active only for 2026-08-28 |
| 11:30 AM | Perplexity live DCA buy pass | `e3590042` | Weekdays | **yes** | date-gated active only for 2026-08-28 |
| 12:30 PM | Perplexity live DCA buy pass | `0c3cde00` | Weekdays | **yes** | date-gated active only for 2026-08-28 |
| 1:30 PM | Perplexity live DCA buy passes | `44b3975d` | Weekdays | **yes** | date-gated active only for 2026-08-28 |
| 2:30 PM | Perplexity live DCA buy passes | `44b3975d` | Weekdays | **yes** | date-gated active only for 2026-08-28 |
| 3:30 PM | Perplexity live DCA buy passes | `44b3975d` | Weekdays | **yes** | date-gated active only for 2026-08-28 |

The Aug 28 override does not authorize Perplexity to run scheduled reports,
dashboard refreshes, slippage-only reports, monitor-only reports, or watchdog
reports. Those scheduled reporting responsibilities remain Claude-only unless
the user changes the role split.

Temporary live scheduler override, Aug 31, 2026:

> **ADDED 2026-08-31:** The user explicitly authorized Perplexity to run the
> morning buy-only DCA schedule for Monday, Aug 31, 2026 at 09:35, 10:30, and
> 11:30 ET. The authorization uses one-time active buy tasks and is date-gated
> to the America/New_York trading date `2026-08-31`; it does not reactivate the
> recurring Aug 28-gated buy slots or authorize afternoon buys. Perplexity's
> scope for this override is scheduled buy-run execution plus required buy-run
> artifacts and notifications only. Claude handles scheduled reports.
> Machine-readable source: `config/scheduled_tasks.json` ->
> `temporary_live_scheduler_overrides.authorize_2026_08_31_morning_perplexity_buy_slots_today_only`.

| ET | Live scheduler task | ID | Cadence | Trades | Aug 31 override status |
|---|---|---|---|---|---|
| 9:35 AM | Today 9:35 AM Perplexity active DCA buy pass | `4e8c92af` | One-time | **yes** | date-gated active only for 2026-08-31 |
| 10:30 AM | Today 10:30 AM Perplexity active DCA buy pass | `01e4f18b` | One-time | **yes** | date-gated active only for 2026-08-31 |
| 11:30 AM | Today 11:30 AM Perplexity active DCA buy pass | `6db8bbf1` | One-time | **yes** | date-gated active only for 2026-08-31 |

The Aug 31 override does not authorize Perplexity to run scheduled reports,
dashboard refreshes, slippage-only reports, monitor-only reports, watchdog
reports, or afternoon buy tasks. Those responsibilities remain outside this
morning-only override unless the user changes the scope.

Temporary live scheduler cleanup and delayed buy-slot override, Sep 8-11, 2026 (SUPERSEDED 2026-09-08 by the standing policy above; kept for audit history):

> **ADDED 2026-09-08:** The user explicitly authorized Perplexity to run the
> buy-only DCA schedule for Tuesday, Sep 8 through Friday, Sep 11, 2026,
> delayed 10 minutes so Claude gets first chance to buy. The expired
> Aug 28-gated Perplexity buy tasks `0c3cde00` (12:30 PM ET) and `44b3975d`
> (1:30/2:30/3:30 PM ET) were deleted from the live scheduler so they stop
> posting stale authorization/no-op messages; replacement delayed tasks now
> cover those slots. Perplexity's scope for this override is delayed buy-run
> execution plus required buy-run artifacts and notifications only. Claude
> handles scheduled reports. Machine-readable
> source: `config/scheduled_tasks.json` ->
> `temporary_live_scheduler_overrides.authorize_2026_09_08_delayed_morning_perplexity_buy_slots_week_only`.

| ET | Live scheduler task | ID | Cadence | Trades | Sep 8-11 override status |
|---|---|---|---|---|---|
| 9:45 AM | Perplexity delayed DCA buy pass | `4412c3c4` | Weekdays | **yes** | active Sep 8-11 only; delayed from 9:35 |
| 10:40 AM | Perplexity delayed DCA buy pass | `e028361e` | Weekdays | **yes** | active Sep 8-11 only; delayed from 10:30 |
| 11:40 AM | Perplexity delayed DCA buy pass | `e3590042` | Weekdays | **yes** | active Sep 8-11 only; delayed from 11:30 |
| 12:40 PM | Perplexity delayed DCA buy pass | `062605c6` | Weekdays | **yes** | active Sep 8-11 only; delayed from 12:30 |
| 1:40 PM | Perplexity delayed DCA buy pass | `54ab24d2` | Weekdays | **yes** | active Sep 8-11 only; delayed from 1:30 |
| 2:40 PM | Perplexity delayed DCA buy pass | `54ab24d2` | Weekdays | **yes** | active Sep 8-11 only; delayed from 2:30 |
| 3:40 PM | Perplexity delayed DCA buy pass | `54ab24d2` | Weekdays | **yes** | active Sep 8-11 only; delayed from 3:30 |

The Sep 8-11 override does not authorize Perplexity to run scheduled reports,
dashboard refreshes, slippage-only reports, monitor-only reports, or watchdog
reports. Those responsibilities remain outside this delayed-buy-only override
unless the user changes the scope. **Superseded 2026-09-08**: this same
authorization for these same five task IDs is now standing (see the table
above); do not renew or extend this block after 2026-09-11, and do not treat
its end-of-week expiry as Perplexity's backup role lapsing.

| ET | Task | ID | Cadence | Cron (UTC) | Trades | Status |
|---|---|---|---|---|---|---|
| 9:35 AM | Market-open DCA live Robinhood execution | `2ba19526` | Weekdays | `35 13 * * 1-5` | **yes** | active |
| 9:45 AM | Morning DCA watchlist early-warning scan | `bb4f6ef1` | Weekdays | `45 13 * * 1-5` | no | active |
| 10:30 AM | Morning DCA live Robinhood execution | `42173154` | Weekdays | `30 14 * * 1-5` | **yes** | active |
| 11:30 AM | Intraday DCA live Robinhood execution | `7be82b97` | Weekdays | `30 15,17,18 * * 1-5` | **yes** | active |
| 12:30 PM | Midday DCA live Robinhood execution | `a6239dae` | Weekdays | `30 16 * * 1-5` | **yes** | active |
| 1:30 PM | Intraday DCA live Robinhood execution | `7be82b97` | Weekdays | `30 15,17,18 * * 1-5` | **yes** | active |
| 2:30 PM | Intraday DCA live Robinhood execution | `7be82b97` | Weekdays | `30 15,17,18 * * 1-5` | **yes** | active |
| 3:30 PM | Final DCA live Robinhood execution | `10ce362c` | Weekdays | `30 19 * * 1-5` | **yes** | active |
| 5:00 PM | Post-trade slippage plus Notion latest-run sync | `67aa802b` | Weekdays | `0 21 * * 1-5` | no | active |
| 5:30 PM | Weekly DCA Notion performance dashboard refresh | `041d9d90` | Fridays | `30 21 * * 5` | no | active |
| 8:45 AM | Monthly graded DCA dashboard refresh | `0656657a` | First weekday gate | `45 12 1-7 * 1-5` | no | active |
| 9:55 AM | Monthly DCA automation cost review | `bec4a6ee` | First weekday gate | `55 13 1-7 * 1-5` | no | active |
| 5:15 PM | Month-end DCA volatility risk refresh, EDT | `c9fb1fb8` | Days 25-31, final eligible market-day gate | `15 21 25-31 3-10 *` | no | active |
| 5:15 PM | Month-end DCA volatility risk refresh, EST | `17a3b2a9` | Seasonal EST months, final eligible market-day gate | `15 22 25-31 1,2,11,12 *` | no | active |
| One-time future | Evaluate graded DCA live test continuation | `f7411889` | One-time | one-shot 2026-08-15 11:45 AM ET | no | active |

**Note on `7be82b97`:** one task fires at three ET slots (11:30 AM, 1:30 PM, and
2:30 PM). Each run records the slot that actually fired in its own
`scheduled_for_et`.

### Expired temporary live scheduler overrides (historical record)

These are **not active** and authorize nothing today. Like the retired entries
below, they are kept as historical records only, and their original text is
reproduced verbatim; the status line above each one is the only thing added.

Reconciled 2026-09-06 ET against `config/scheduled_tasks.json` ->
`temporary_live_scheduler_overrides` and its `tasks` array:

| Override | JSON key | Date gate | Self-expiring | Status today |
|---|---|---|---|---|
| Aug 17 to Aug 24, 2026 pause | `pause_2026_08_17_0915_prevalidation` | restore due `2026-08-24 09:00 ET` | no explicit window | **superseded**: the condition it demanded is met in the current record |
| Aug 28, 2026 authorization | `authorize_2026_08_28_all_perplexity_buy_slots_today_only` | trading day `2026-08-28` | `expires_after_trading_day: true` | **expired** |
| Aug 31, 2026 authorization | `authorize_2026_08_31_morning_perplexity_buy_slots_today_only` | trading day `2026-08-31` | `expires_after_trading_day: true` | **expired** |

Both August authorizations carry
`authorization_window_et.expires_after_trading_day: true`, so each lapsed by its
own terms at the end of its named trading day. Neither was ever a change to the
canonical schedule: both authorized the *existing* canonical task IDs for one day
and required their task text to end silently on any other date. The canonical
table above is unchanged by all three.

One asymmetry survives the reconciliation and is worth knowing. Each
authorization also names `disabled_parallel_task_ids`, the parallel replacement
tasks that had to stay disabled so two executors could not race for the same
slot: `4412c3c4`, `e028361e`, `e3590042`, `0c3cde00`, `44b3975d` (Aug 28) and
`4e8c92af`, `01e4f18b`, `6db8bbf1` (Aug 31). None of those eight has a `tasks`
entry, so unlike `2c1d3792` their disabled state cannot be confirmed from this
repo's record at all. That is expected for IDs that were never supposed to run,
and it is the reason the discoverability rule applies to authorized buy tasks
rather than to every ID an override mentions. If one of them is ever authorized,
it needs a `tasks` entry first.

`tests/test_config_invariants.py` holds this reconciliation in place: an override
whose trading day has passed must be marked expired here, and any one-time task
an override authorized must be `disabled` in `tasks` once its window closes.

#### Temporary live scheduler override, Aug 17 to Aug 24, 2026 (SUPERSEDED)

> **STATUS (reconciled 2026-09-06 ET):** superseded by the current record, not by a
> recorded operator sign-off. The correction below required that `bb4f6ef1` be
> restored or that `42173154` be confirmed to have an alternate pre-flight
> source. In `config/scheduled_tasks.json` today, `bb4f6ef1` is `status:
> "active"` with `cron_utc` `45 13 * * 1-5`, matching its 9:45 AM row in the
> canonical table, so the pre-flight writer the gate depends on is present. The
> restore task `5cc41080` never received a `tasks` entry and appears nowhere but
> in this override record. No operator verification is recorded anywhere in the
> repo, so treat this as a record-state reconciliation: the hazard is closed
> because the writer is active, not because someone signed it off.

> **CORRECTED 2026-08-18:** `bb4f6ef1` is the canonical 9:45 AM watchlist
> scan and required pre-flight writer, not a harmless 9:15 AM readiness task.
> `MorningDCAExecutor._apply_preflight_gate()` fails closed without its artifact,
> so removing this task can suppress the 10:30 AM live buy (`42173154`). Treat
> the 10:30 run as affected and unverified until an operator either restores
> `bb4f6ef1` immediately or confirms that `42173154` has an alternate pre-flight
> source. Before Aug 24, the operator must also verify whether a genuinely
> separate 9:15 task exists under another ID; do not use `bb4f6ef1` for that
> pause. The one-time task `5cc41080` remains scheduled for Aug 24, but waiting
> for it is not a safe substitute for restoring the required pre-flight writer.
> Machine-readable source and complete required action:
> `config/scheduled_tasks.json` ->
> `temporary_live_scheduler_overrides.pause_2026_08_17_0915_prevalidation.correction_2026_08_18`.

#### Temporary live scheduler override, Aug 28, 2026 (EXPIRED)

> **STATUS (reconciled 2026-09-06 ET):** expired at the end of the America/New_York
> trading day `2026-08-28`, per its own
> `authorization_window_et.expires_after_trading_day: true`. It authorizes
> nothing now.

> **ADDED 2026-08-28:** The user explicitly authorized Perplexity to run all
> scheduled buy-only DCA tasks for Friday, Aug 28, 2026, including an immediate
> one-time 9:35 AM catch-up because the normal 9:35 occurrence had already
> passed. The override authorizes the existing canonical tasks; parallel
> Perplexity replacement tasks remain disabled so only one executor owns each
> slot. Perplexity's scope for this override is scheduled buy-run execution plus
> required buy-run artifacts and
> notifications only. Claude handles scheduled reports. Machine-readable source:
> `config/scheduled_tasks.json` ->
> `temporary_live_scheduler_overrides.authorize_2026_08_28_all_perplexity_buy_slots_today_only`.

The table below is the authorization scope as recorded on 2026-08-28. It is a
historical record of one expired trading day, **not** an active schedule table.

| ET | Live scheduler task | ID | Cadence | Trades | Aug 28 override status |
|---|---|---|---|---|---|
| 9:35 AM | Canonical live DCA buy pass | `2ba19526` | Weekdays | **yes** | authorized for 2026-08-28; scheduled occurrence already passed |
| immediate 9:35 catch-up | Perplexity DCA catch-up buy run | `2c1d3792` | One-time | **yes** | authorized catch-up for 2026-08-28 only |
| 10:30 AM | Canonical live DCA buy pass | `42173154` | Weekdays | **yes** | authorized for 2026-08-28 |
| 11:30 AM | Canonical live DCA buy pass | `7be82b97` | Weekdays | **yes** | authorized for 2026-08-28 |
| 12:30 PM | Canonical live DCA buy pass | `a6239dae` | Weekdays | **yes** | authorized for 2026-08-28 |
| 1:30 PM | Canonical live DCA buy pass | `7be82b97` | Weekdays | **yes** | authorized for 2026-08-28 |
| 2:30 PM | Canonical live DCA buy pass | `7be82b97` | Weekdays | **yes** | authorized for 2026-08-28 |
| 3:30 PM | Canonical live DCA buy pass | `10ce362c` | Weekdays | **yes** | authorized for 2026-08-28 |

**Current record for the one-time row:** `2c1d3792` is `status: "disabled"` in
`config/scheduled_tasks.json`'s `tasks` array, with its own note that it was left
disabled because the 2026-08-28 window has expired. It has a `tasks` entry at all
only so an override-named buy task is discoverable and validatable the same way a
canonical task is (codex review `discussion_r3894271690`); it is not, and never
was, part of the canonical schedule. The seven canonical IDs in the rows above are
unaffected: they were authorized for that one day and continue on their own
canonical cadence.

The Aug 28 override does not authorize Perplexity to run scheduled reports,
dashboard refreshes, slippage-only reports, monitor-only reports, or watchdog
reports. Those scheduled reporting responsibilities remain Claude-only unless
the user changes the role split.

#### Temporary live scheduler override, Aug 31, 2026 (EXPIRED)

> **STATUS (reconciled 2026-09-06 ET):** expired at the end of the America/New_York
> trading day `2026-08-31`, per its own
> `authorization_window_et.expires_after_trading_day: true`. It authorizes
> nothing now.

> **ADDED 2026-08-31:** The user explicitly authorized Perplexity to run the
> morning buy-only DCA schedule for Monday, Aug 31, 2026 at 09:35, 10:30, and
> 11:30 ET. The authorization uses the existing canonical tasks and is
> date-gated to the America/New_York trading date `2026-08-31`; parallel
> one-time tasks remain disabled, and the override does not authorize afternoon
> buys. Perplexity's scope for this override is scheduled buy-run execution plus required buy-run
> artifacts and notifications only. Claude handles scheduled reports.
> Machine-readable source: `config/scheduled_tasks.json` ->
> `temporary_live_scheduler_overrides.authorize_2026_08_31_morning_perplexity_buy_slots_today_only`.

The table below is the authorization scope as recorded on 2026-08-31. It is a
historical record of one expired trading day, **not** an active schedule table.

| ET | Live scheduler task | ID | Cadence | Trades | Aug 31 override status |
|---|---|---|---|---|---|
| 9:35 AM | Canonical Perplexity-authorized DCA buy pass | `2ba19526` | Weekdays | **yes** | authorized only for 2026-08-31 |
| 10:30 AM | Canonical Perplexity-authorized DCA buy pass | `42173154` | Weekdays | **yes** | authorized only for 2026-08-31 |
| 11:30 AM | Canonical Perplexity-authorized DCA buy pass | `7be82b97` | Weekdays | **yes** | authorized only for 2026-08-31 |

**Note on the `7be82b97` row:** the Aug 31 override authorized that shared task
for its 11:30 ET fire only. Because the same task also fires at 13:30 and 14:30
ET, its required task text had to fail closed on those two slots
(`authorized_task_slots_et` pins `7be82b97` to `["11:30"]`). That gate is pinned
by `test_aug_31_intraday_override_is_gated_to_the_1130_fired_slot`, and it is the
reason a date guard alone was not sufficient for this override.

The Aug 31 override does not authorize Perplexity to run scheduled reports,
dashboard refreshes, slippage-only reports, monitor-only reports, watchdog
reports, or afternoon buy tasks. Those responsibilities remain outside this
morning-only override unless the user changes the scope.

### One-time tasks added after the 2026-07-27 canonical verification

The canonical table above was originally verified at exactly 17 rows,
byte-identical since verification. On 2026-08-02 the user gave explicit,
direct instructions to remove `e3e6f09e` (3:05 PM monitor-only signal check)
and `a46bf007` (3:40 PM live-run watchdog) entirely, and to retime `67aa802b`
(3:35 PM -> 5:00 PM ET daily) and `041d9d90` (Fridays 5:00 PM -> 5:30 PM ET);
the table above reflects that change (17 -> 15 rows). See
`config/scheduled_tasks.json`'s `canonical_schedule_reconciliation_2026_07_27.canonical_schedule_et_immutability_note`
for the full reasoning. It already contains one pre-existing one-time entry
(`f7411889`) from before the 2026-07-27 verification, which stays in place.
New one-time, non-recurring tasks are recorded here instead of being appended
to the canonical table, so it stays intact and diffable. Both this table and
the canonical table above are active schedule sources.

| Fires | Task | ID | Trades | Status | Scheduler-registered |
|---|---|---|---|---|---|
| 2026-07-27 4:30 PM ET | One-time DCA credit-usage proxy report with recovery runs | `onetime-1630-credit-usage-proxy-2026-07-27` | no | active | **yes**: `cron_utc` `30 20 27 7 *`, executor `scripts/generate_credit_usage_proxy_report.py` |

### Retired from the active schedule on 2026-07-27
These entries are **not active** and must never appear in an active schedule
table. They are kept as historical records only.

| ID | Former slot | Reason |
|---|---|---|
| `b7961407` | 9:00 AM pre-market buying-power dashboard | not in the canonical live schedule |
| `cd45e95b` | 9:45 AM compact volatility outlier scan | superseded by `bb4f6ef1` |
| `morning-dca-0945` | 9:45 AM pre-flight alias | repo-internal alias, superseded by `bb4f6ef1`; the pre-flight artifact contract it describes is required by `42173154` and is now actively produced by `bb4f6ef1` |
| `d72aa525` | 3:25 PM capital-priority briefing | not in the canonical live schedule |
| `d1f7a2c0` | post-artifact drift detector entry | never a scheduler task; the drift detector itself is still required in-process by every live trading run |

`e3e6f09e` (monitor-only signal check, formerly 3:05 PM) is **paused**, not
retired: it is represented but not firing, and is not expected daily evidence
while paused. `f7411889` remains a one-time reminder, not a recurring stream.

## Run-plumbing contracts for every live stream (added 2026-07-27)

These apply to **all five live trading tasks** (`2ba19526`, `42173154`,
`a6239dae`, `7be82b97`, `10ce362c`), not to one slot. On 2026-07-27 the 12:30
midday task and the 2:30 intraday slot failed the identical way minutes apart, so
a contract scoped to one cron id would have left the other able to fail again.
Machine-readable source:
[`config/scheduled_tasks.json`](../config/scheduled_tasks.json) →
`scheduled_run_artifact_contract.root_level_artifact_set_2026_07_27` and
`spread_gate_outcome_contract_2026_07_27`.

### A missing repo entry point is a no-trade condition, not a trigger for a workaround

**Removed 2026-07-30.** This section formerly instructed a scheduled run to
treat a missing repo runner script, entry point, or order-submission
implementation as a trigger to reimplement signal generation directly from
Robinhood/Finance connector data and place live orders via
`place_equity_order`, bypassing the repo's own vetted logic entirely. That
directly contradicted the standing prohibition against building a buy list
without the repo, and contradicted `PRODUCTION_READINESS.md`'s own safety
analysis, which found real unresolved gaps in the seven-slot roster (the
same-day top-up guard doesn't cover four of the seven slots, stale
per-stream budget caps, a sole-executor race) and recommended keeping live
execution disabled until those are fixed. Removed at the account owner's
explicit request. The corresponding `config/scheduled_tasks.json` keys
(`connector_native_execution_policy`, `intraday_connector_native_recovery_policy`)
are kept as tombstones recording the same removal, not deleted outright, so
no reference is left dangling. **A missing repo entry point remains a
no-trade condition today: the current `claude/dca-executor-prompts/*.txt`
Gate 5 text ends the run with a `GATE5_DISABLED` verdict (silent no longer —
it alerts on every fire as of 2026-07-30, per
`PRODUCTION_READINESS.md`, though still not a trigger for
connector-native execution) until a human implements a real
signal-generation entry point, at which point it reverts to a normal
abort-and-alert check on failure like every other gate. No executor should
reimplement trading logic ad hoc and place real orders from config text
alone.**

**This removal covers every executor of these cron ids, not only the
seven-slot Claude roster.** An earlier revision of this fix (main branch PRs
#241/#242, since superseded) tried to scope the disablement to "the
seven-slot Claude roster" by adding a `status: disabled_for_seven_slot_claude_roster`
label next to the policy's literal `rule`/`required_steps`/`order_placement_tool`
text, while leaving that text otherwise unmodified on the theory that the
separate, non-Claude, external ("Perplexity-era") scheduler documented in
[safeguards.md](safeguards.md#live-execution-roster) — which
`docs/README.md` confirms is the account's actual live executor for all
five cron ids this policy names, including `10ce362c` — was untouched by
design. That was reconsidered at the account owner's explicit direction
(2026-07-30): a scoped label next to unmodified bypass instructions doesn't
stop a tool-using agent, Claude or Perplexity, from reading and following
the literal text for a cron id the label doesn't clearly cover, and the
same-day top-up-guard gap this fallback would trigger applies to that agent
exactly as it would to any other. The tombstones above now remove the
literal instructions outright, for every one of the five cron ids
(`2ba19526`, `42173154`, `a6239dae`, `7be82b97`, `10ce362c`), accepting that
whatever executes them should fail closed (place nothing, and alert) on a
missing entry point until a reviewed one exists, the same as the seven-slot
roster's `GATE5_DISABLED`.

### Root-level artifacts, every run, every outcome

Every live run writes `run_state.json`, `run_summary.json`, `signals.csv` (or the
stream-specific name), and `submitted_orders.csv` **at the artifact root**, with
a header row on both CSVs even at zero rows, on every outcome including a
zero-order, blocked, or failed run. Writing them only under an `artifacts/`
subfolder does **not** satisfy the contract; `validate_run_artifacts` names the
nested path rather than reporting four unrelated missing files. See
[run_classification.md](run_classification.md).

### A no-order run is not a failure

Evaluate the ladder **first**, then the spread gate. When no ticker qualifies,
nothing reaches the spread gate and the run records
`completed_no_orders` with `completion_reason='no_ladder_candidates'` and a
**null** `error_class`. `error_class='missing_spread_data'` is recorded only when
candidates actually reached the gate and every one was failed closed there.
Either way it is a safe no-order skip, valid scheduled evidence that the slot
executed, never a missed slot, and never an error in scheduled reliability.

When spread is missing for only some tickers, **only those rows fail closed**;
every other qualifying ticker keeps being evaluated and submitted. Spread is
never estimated, inferred, or defaulted.

## Task details

### `b7961407`: Pre-market DCA buying-power dashboard (RETIRED 2026-07-27, NOT ACTIVE)
> **RETIRED 2026-07-27, NOT ACTIVE.** Not in the canonical live schedule verified 2026-07-27. It has no
> `cron_utc` and must not appear in any active schedule table. Historical record only.
- Analytics only, **no trading**. Formerly ran at **9:00 AM ET weekdays**.
- Pre-market buying-power dashboard using the current **$2 base 0x-to-5x ladder**.

- **Cost-conscious model instruction (Jul 17 2026):** use a cost-conscious/default model where available; do not use ChatGPT 5.5 or premium models unless needed for failure recovery, safety validation, or connector/debug escalation. Keep daily output compact, write JSON/CSV artifacts, suppress no-op notifications, and leave heavy analysis to Friday weekly or monthly review. See the top-of-file cost-conscious model instruction note.

### `cd45e95b`: Compact DCA volatility outlier scan (RETIRED 2026-07-27, NOT ACTIVE)
> **RETIRED 2026-07-27, NOT ACTIVE.** Superseded by the 9:45 AM watchlist early-warning scan `bb4f6ef1`.
> It has no `cron_utc` and must not appear in any active schedule table.
- Analytics only, **no trading**. Formerly ran at **9:45 AM ET weekdays**.
- Scans only **>5% outliers**; no full 41+ ticker narrative on no-outlier days. Uses the current $2 base 0x-to-5x ladder.

- **Cost-conscious model instruction (Jul 17 2026):** use a cost-conscious/default model where available; do not use ChatGPT 5.5 or premium models unless needed for failure recovery, safety validation, or connector/debug escalation. Keep daily output compact, write JSON/CSV artifacts, suppress no-op notifications, and leave heavy analysis to Friday weekly or monthly review. See the top-of-file cost-conscious model instruction note.

### `morning-dca-0945`: Morning graded DCA pre-flight validation (`MorningDCAExecutor`) (RETIRED 2026-07-27, NOT ACTIVE)
> **RETIRED 2026-07-27, NOT ACTIVE as a schedule entry.** This is a repo-internal alias; the canonical 9:45 AM
> slot is `bb4f6ef1`. It must not appear in any active schedule table. The
> **pre-flight artifact contract described below is required** by the 10:30 AM
> live task `42173154`, which fails closed without a same-day pre-flight artifact, and
> is now actively produced by `bb4f6ef1` at the same 9:45 AM ET slot (added 2026-07-27,
> Codex review discussion_r3653976736) rather than by this disabled entry.
- Analytics / **read-only**, **no trading**. Executor mode `preflight`, run as part of the 9:45 AM slot.
- Evaluates morning price weakness vs. yesterday’s regular-session close on the shared **$2 base 0x-to-5x ladder** (`config/morning_dca.json`) as a pre-screen for the 10:30 live evaluator; runs no Robinhood order review and calls no broker placement path. Writes pre-flight artifacts under `/home/user/workspace/cron_tracking/morning_dca_preflight/YYYYMMDD_0945ET/` — `run_state.json`, `validation.csv`, `signals.csv`, `run_summary.json`, and the `latest_preflight.json` pointer (`run_id='morning_preflight_YYYYMMDD_0945ET'`).
- Emits **`eligible_to_execute=true/false` per ticker**. This is a **coarse pre-screen** (is a ticker worth re-evaluating at 10:30?), **NOT** a final buy approval — the 10:30 live run recomputes signals/budget/spread/trend/top-up from scratch and may still decline. A ticker is eligible when it graded to a positive-multiplier BUY at 9:45 with a usable current price and previous close.
- `stream='morning'`, `run_type='morning_dca_preflight'`, `state_schema_version='morning-preflight-v2-machine-complete'`, `valid_afternoon_evidence=false`. Never writes into the afternoon `ce16ba6b` tree or updates `latest_scheduled_run.json`. (A read-only `watchlist` early-warning mode also runs at the same slot, writing under `cron_tracking/morning_dca_watchlist/` — see `bb4f6ef1` above.)

- **Cost-conscious model instruction (Jul 17 2026):** use a cost-conscious/default model where available; do not use ChatGPT 5.5 or premium models unless needed for failure recovery, safety validation, or connector/debug escalation. Keep daily output compact, write JSON/CSV artifacts, suppress no-op notifications, and leave heavy analysis to Friday weekly or monthly review. See the top-of-file cost-conscious model instruction note.

### `2ba19526`: Market-open DCA live Robinhood execution (9:35 AM ET)
- **Live trading**. Runs **9:35 AM ET weekdays**, cron `35 13 * * 1-5` (13:35 UTC during EDT).
- Uses the shared **$2 base 0x-to-5x live ladder**. **Robinhood account <ROBINHOOD_ACCOUNT_NUMBER> is the
  only execution broker**; Alpaca, Finance, and Yahoo are read-only validation/account
  context only. Provider policy is unchanged: Finance primary, Alpaca/Robinhood
  validation, `yfinance` fallback, GFUZ Robinhood-primary.
- **This is the first buy of the trading day.** It has no same-day prior-buy context, so
  the same-day top-up guard does not reduce it. Every later live slot (9:45 watchlist
  input, 10:30, 11:30, 12:30, 1:30, 2:30, and the 3:30 final) treats this run's fills as
  same-day prior-allocation context (Codex review discussion_r3653976738: "Feed
  market-open fills into later top-up guards"). See
  `near_close_dca._prior_run_is_scheduled`, `morning_dca.DEFAULT_MARKET_OPEN_TASK_ROOT`,
  and `run_dca.DEFAULT_MARKET_OPEN_LIVE_STATE_ROOT` (overridable via
  `DCA_MARKET_OPEN_LIVE_STATE_ROOT`).
- Artifacts: `/home/user/workspace/cron_tracking/market_open_dca/YYYYMMDD_0935ET/` with
  `run_state.json`, `run_summary.json`, `signals.csv`, `submitted_orders.csv`,
  `filled_orders.csv`, `raw/`, and the `latest_market_open_run.json` pointer
  (`stream='market_open'`, `run_id='market_open_live_YYYYMMDD_0935ET'`).
- **Aug 3 2026 Perplexity-primary artifact-contract note:** the 9:35 AM run completed
  and reconciled 25 filled Robinhood orders under
  `/home/user/workspace/cron_tracking/perplexity_primary_YYYYMMDD/YYYY-MM-DD_0935ET/`,
  but the expected same-day latest pointer was not published. Treat that as
  `latest_pointer_missing_artifact_contract_failure`, not as a no-order run and not
  as an incomplete order-placement run when the root `run_state.json`,
  `run_summary.json`, `signals.csv`, and `submitted_orders.csv` exist and reconcile.
  Future 9:35 runs must publish the canonical `latest_market_open_run.json` pointer,
  plus the Perplexity-primary alias pointer while Perplexity-primary mode is active,
  so downstream same-day top-up loaders and checks find the record without relying on
  manual date/window path discovery.
- All standard circuit breakers, caps, whitelist, buy-only, and regular-hours-only
  controls apply unchanged. Covered by `failure_recovery_policy` and has its own
  `post_artifact_drift_check` block, matching the morning/midday/afternoon live tasks.

- **Cost-conscious model instruction:** use a cost-conscious/default model where available; do not use premium models unless needed for failure recovery, safety validation, or connector/debug escalation. Keep daily output compact, write JSON/CSV artifacts, suppress no-op notifications, and leave heavy analysis to Friday weekly or monthly review.

### `bb4f6ef1`: Morning DCA watchlist early-warning scan (9:45 AM ET)
- Analytics / **read-only**, **no trading**, **places no orders**. Runs **9:45 AM ET
  weekdays**, cron `45 13 * * 1-5`.
- Canonical 9:45 AM slot. **Supersedes** `cd45e95b` and the repo-internal alias
  `morning-dca-0945` -- and absorbed BOTH of the alias's roles, not just its display
  name (see below).
- **Two responsibilities at the same firing (added 2026-07-27, Codex review
  discussion_r3653976736):**
  1. **The required pre-flight writer.** `10:30 AM 42173154` fails closed
     (`MorningDCAExecutor._apply_preflight_gate`, empty ticker list) without a same-day
     `morning_dca_preflight/YYYYMMDD_0945ET/run_state.json`. Retiring `morning-dca-0945`
     as a standalone schedule entry without keeping this writer active would silently
     break every scheduled 10:30 run, so `bb4f6ef1` invokes `MorningDCAExecutor` in
     `preflight` mode, writing `run_state.json`, `validation.csv`, `signals.csv`,
     `run_summary.json` under `morning_dca_preflight/YYYYMMDD_0945ET/` and updating
     `latest_preflight.json` (schema `morning-preflight-v2-machine-complete`), exactly
     as `morning-dca-0945` did.
  2. **The early-warning watchlist scan.** `MorningDCAExecutor` in `watchlist` mode,
     writing under `morning_dca_watchlist/YYYYMMDD_0945ET/` (`stream='watchlist'`) for
     the 10:30 AM live evaluator. It is a **coarse pre-screen**, never a buy approval:
     the 10:30 live run recomputes price, prior close, spread, 60-minute trend, budget,
     and top-up from scratch and may still decline.
- Artifacts: BOTH `/home/user/workspace/cron_tracking/morning_dca_preflight/YYYYMMDD_0945ET/`
  and `/home/user/workspace/cron_tracking/morning_dca_watchlist/YYYYMMDD_0945ET/`. Never
  writes into the afternoon `ce16ba6b` tree and never updates `latest_scheduled_run.json`.
- No in-repo Python entry point: this is an agent-run analytics task, so the scheduler
  shells out to `python -m scripts.model_usage_log` after the run to record model
  routing, and runs `MorningDCAExecutor` twice (preflight then watchlist) for the two
  distinct artifact contracts above.

- **Cost-conscious model instruction:** use a cost-conscious/default model where available; do not use premium models unless needed for failure recovery, safety validation, or connector/debug escalation. Keep daily output compact, write JSON/CSV artifacts, suppress no-op notifications, and leave heavy analysis to Friday weekly or monthly review.

### `7be82b97`: Intraday DCA live Robinhood execution (11:30 AM, 1:30 PM, 2:30 PM ET)
- **Live trading**. One task, **three ET slots**: **11:30 AM, 1:30 PM, and 2:30 PM ET
  weekdays**, cron `30 15,17,18 * * 1-5` (15:30, 17:30, and 18:30 UTC during EDT).
- Uses the shared **$2 base 0x-to-5x live ladder**. **Robinhood account <ROBINHOOD_ACCOUNT_NUMBER_OBFUSCATED> is the
  only execution broker**; Alpaca, Finance, and Yahoo are read-only validation/account
  context only.
- **Same-day top-up rule (canonical, unchanged):** for a ticker with same-day prior buy
  context, the comparison reference is that ticker's **most recent valid same-day buy
  reference price**. Buy again only when the current decision price is **strictly lower**
  than that most recent reference, and then buy only
  `corrected_delta_usd = max(0, desired_usd - effective_prior_usd)`. There is no
  lowest-price gating.
- **Artifacts (root-level contract, tightened 2026-07-27):**
  `/home/user/workspace/cron_tracking/intraday_dca/YYYYMMDD_<HHMM>ET/`
  (`stream='intraday'`), where `<HHMM>` is the slot that fired (`1130`, `1330`, `1430`).
  Each run records its own `scheduled_for_et`.

  **All three slots (11:30, 1:30, 2:30) must write these four files at the artifact ROOT,
  on every outcome, including zero-order and blocked runs:**

  | File | Requirement |
  |---|---|
  | `run_state.json` | `run_classification` must be a **non-null object**, never null and never a bare string |
  | `run_summary.json` | Same non-null `run_classification` object requirement |
  | `intraday_signals.csv` | Canonical name (corrected 2026-08-03), matching the stream-specific naming every sibling live stream uses. `signals.csv` remains an accepted **legacy alias** for pre-executor artifacts |
  | `submitted_orders.csv` | Must carry its **headers even when zero orders** were submitted. Header-only is correct for a zero-order run |

  **An `artifacts/` subfolder is not an acceptable final state.** Writing the four files
  only under `artifacts/` does not satisfy this contract, because
  `same_day_pointer.derive_valid_scheduled_evidence()` reads the root and will reject the
  run as scheduled evidence. This requirement exists because the 2026-07-27 1:30 run did
  exactly that and had to be repaired; see the 1:30 entry below. Machine-readable source:
  `config/scheduled_tasks.json` -> `intraday_task_patch_2026_07_27.artifact_contract`.
- The **2026-07-24 2:30 PM ET scheduled recovery fills** are recorded under this task.
  Machine-readable source: `config/scheduled_tasks.json` ->
  `intraday_dca_scheduled_recovery_2026_07_24`.
- The recurring task's own `run_classification` (`stream='intraday'`, `cron_id='7be82b97'`)
  validates against [`schemas/intraday_run_classification.schema.json`](../schemas/intraday_run_classification.schema.json),
  pinning window/`run_id` per fired slot; distinct from the ad hoc `intraday_1430`-style
  recovery stream name above.
- **Prior-allocation context (added 2026-07-27):** an 11:30 AM fill is fed into the
  12:30 midday and 3:30 afternoon same-day top-up guards as prior-allocation context,
  so it is never re-bought (Codex review: "Include the 11:30 intraday run in midday
  context"). See `midday_dca.MiddayConfig.prior_allocation_roots`.
- **Aug 3 2026 Perplexity-primary 11:30 catch-up task:** the active schedule did not
  contain the 11:30 live slot when checked at 11:35 AM ET, so a today-only
  Perplexity-primary catch-up task was created as task `174c4163`, scheduled for
  11:39 AM ET. It represents the missing 11:30 intraday slot for Aug 3 only and must
  write root artifacts under
  `/home/user/workspace/cron_tracking/perplexity_primary_20260803/2026-08-03_1130ET/`.
  It explicitly loads
  `/home/user/workspace/cron_tracking/perplexity_primary_20260803/2026-08-03_0935ET/`
  as same-day prior buy context for top-up calculations if the latest pointer is
  missing. The 9:35 fills and any 11:30 catch-up fills are ticker-scoped top-up
  context only for later same-day runs, including 12:30, 1:30, 2:30, and 3:30. They
  are not proof that any other stream ran and are not scheduled slippage evidence for
  another stream.
- **Recovery and drift detection (added 2026-07-27):** covered by `failure_recovery_policy`
  (see `applies_to_tasks`) and has its own `post_artifact_drift_check` block, matching the
  morning/midday/afternoon live tasks.
- **2026-07-27 1:30 PM ET: safe no-order skip, artifact repaired later the same day.** The
  1:30 run (`intraday_live_20260727_1333ET`) fired, evaluated all 44 tickers, and submitted
  **zero orders** because the required `spread_ok` safety check could not be satisfied.
  Bid/ask spread inputs were not captured or persisted, so all 44 signal rows carry
  `spread_ok=false` and `spread_status='missing_bid_ask'` and every ticker was
  conservatively skipped. Recorded as `outcome_classification='completed_no_orders'`
  (the prose label for the outcome, kept distinct from the `run_classification` object).
  This is a **safe no-order skip**, not a trading failure, not a failed run, and not a
  missed slot; it must not be counted as an error in scheduled reliability metrics.
  (`run_summary.json` records `candidates_blocked_missing_spread=0` because,
  independently of the missing spread inputs, no ticker qualified on the ladder either:
  skip reasons were `same_day_topup_not_lower` 20, `not_below_prior_close` 12,
  `same_day_topup_no_delta` 9, `missing_live_price` 3.)

  **The artifact was initially written wrong, then repaired.** It originally wrote
  `run_state.json`, `run_summary.json`, `signals.csv`, and `submitted_orders.csv` **only
  under an `artifacts/` subfolder**, leaving the artifact root
  `/home/user/workspace/cron_tracking/intraday_dca/20260727_1330ET/` with no state files at
  all, and `run_classification` was a bare string rather than an object. The repair placed
  all four files at the **root** and normalized both `run_state.json` and `run_summary.json`
  so `run_classification` is a **non-null object** in each, with top-level `run_origin`,
  `manual_run`, `stream`, `scheduled_for_et`, `cron_id`, `valid_scheduled_evidence`,
  `valid_recovery_context`, `order_submit_status`, `orders_submitted_count`,
  `orders_candidate_count`, `completion_reason`, and `error_class` all present. **Zero orders
  before and after**: the repair changed only how the outcome is persisted, never what was
  traded.

  The repaired artifact records `order_submit_status='completed_no_orders'`,
  `error_class='missing_spread_data_nonfatal'`,
  `completion_reason='completed_no_orders_missing_spread_data_nonfatal'`, and
  `valid_scheduled_evidence=true`. The `nonfatal` suffix is the point: missing spread data
  closed each ticker's row rather than failing the run, which is what
  `spread_data_availability_policy` requires.

  **`valid_scheduled_evidence` is back to `true`, and it is derived rather than asserted.**
  It had been corrected to `false` earlier the same day because
  `same_day_pointer.derive_valid_scheduled_evidence()` rejected the pre-repair artifact, and
  that correction stated the flag should be set true again **only if the artifact were
  repaired to satisfy the contract**. It was. Running the repo's own derivation over the
  persisted files returns `False` for the superseded `artifacts/run_state.json` and `True`
  for the repaired root `run_state.json`, so the ledger now agrees with every in-repo
  validator instead of contradicting one. `slot_executed` remains `true`; the two questions
  (can a consumer validate the artifact, and did the task run) are still distinct, and both
  now answer yes.

  One residual deviation is recorded rather than rewritten: the root `submitted_orders.csv`
  still carries the non-canonical 6-column header
  (`ticker,order_usd,ref_id,order_id,status,reason`) instead of the canonical 23-column
  `submitted_orders` schema. It is header-present and zero-row, which satisfies the
  headers-even-when-zero-orders requirement, and no consumer reads it for this run because
  zero orders were submitted.
- **2026-07-27 2:30 PM ET: failed scheduled startup, then a connector-native recovery.**
  The 2:30 scheduled run escalated **before live order placement** because it reported no
  existing run script and no internal implementation for order submission. No scheduled
  artifact root `20260727_1430ET/` was written, so this is a **failed scheduled
  startup/execution path** and is **not valid scheduled evidence**. A connector-native
  recovery pass ran at about **2:32 PM ET** into its own root
  `/home/user/workspace/cron_tracking/intraday_dca/20260727_1430ET_recovery_1432ET/`
  (`run_id='intraday_recovery_20260727_1432ET'`, `run_origin='scheduled_recovery'`,
  `stream='intraday_1430_recovery'`, `state_schema_version='intraday-live-v2-recovery'`).
  It gathered fresh Finance and Robinhood quotes, **captured Robinhood bid and ask spread
  inputs**, used the same-day **11:30 fills** plus the **12:30 recovery fills** as
  ticker-scoped top-up context only, and placed **exactly one** qualifying order after
  `spread_ok` and all safety checks passed:

  | Ticker | Amount | Order id | Average fill price | Quantity | Path |
  |---|---|---|---|---|---|
  | EVR | $1.00 | `6a67a495-699e-4e4f-bef1-f6bbb5954571` | 336.916900 | 0.002968 | top-up (prior $1.00 @ 337.0674) |

  The recovery artifact is `valid_scheduled_evidence=false`. It is **NOT** slippage proof,
  **NOT** watchdog proof, and **NOT** proof that the scheduled 2:30 task ran successfully.

  **It is also NOT usable top-up context today, despite being intended as exactly that.**
  Its `valid_recovery_context` and top-up flags are recorded **false** with
  `residual_only=true`, because the persisted classification omits
  `same_day_top_up_context_only=true`: `validate_recovery_classification()` rejects it,
  `is_top_up_context_state()` returns false, and the 3:30 PM ET final buy `10ce362c` will
  not load its fill. This paragraph claimed the opposite until it was corrected alongside
  the machine-readable mirrors (codex review: "Stop advertising the rejected EVR recovery
  as usable context"); an operator reading only this section was getting a different
  answer from the runtime. See the residual-gap detail later in this section, and
  `recovery_context_run_outcomes` in [`config/scheduled_tasks.json`](../config/scheduled_tasks.json).
  Once the persisted artifact carries the missing flag, this becomes ticker-scoped
  same-day top-up context for later streams and all four records flip together.

  This pass is also **not** a `failure_recovery_policy` recovery rerun, and is recorded
  with `recovery_rerun=false`. That policy covers only a failure **after** data was
  fetched, repaired in place under `intraday_dca/YYYYMMDD_1430ET/` with
  `run_origin='scheduled'`; this pass fetched fresh Finance and Robinhood data into a
  separate root with `run_origin='scheduled_recovery'`.

  Its fill is recorded in the separate `recovery_context_run_outcomes` ledger, **not** in
  `scheduled_run_outcomes`, whose collection contract is scheduled evidence that may be
  aggregated without a second per-row filter. Machine-readable sources:
  `config/scheduled_tasks.json` ->
  `intraday_dca_scheduled_recovery_2026_07_27` and `scheduled_run_outcomes`, and
  `config/run_classification.json` -> `intraday_no_order_skip_2026_07_27_1330` and
  `intraday_scheduled_recovery_2026_07_27`.
- **Connector-native execution requirement removed (added 2026-07-27, removed 2026-07-30):**
  this task formerly required that if the 11:30, 1:30, or 2:30 intraday background run
  could not find an existing repo runner script or an internal order-submission
  implementation, it must reimplement signal generation from raw Robinhood/Finance
  connector data and place live orders with `place_equity_order` in place of escalating.
  That was removed: it bypassed the repo's own vetted logic, and four of the seven roster
  slots (9:35, 11:30, 1:30, 2:30) are not folded into the same-day top-up guard's
  prior-allocation chain, so the connector-native path could have bought the same ticker
  repeatedly at rising prices on the same day. See
  `config/scheduled_tasks.json` -> `intraday_connector_native_recovery_policy` for the
  full removal rationale. A missing repo runner script or entry point remains a no-trade
  condition today.
- **Missing spread inputs are never a startup failure (added 2026-07-27, revised 2:55 PM
  ET):** the run always fires, always evaluates, and always writes the complete artifact set.
  **Evaluate the ladder first, then the spread gate**, because the correct recorded outcome
  depends on whether any ticker qualified before reaching the spread gate.
  - **No candidate ever reaches the spread gate:** write the complete artifact set and record
    `completed_no_orders` with `completion_reason='no_ladder_candidates'`, `error_class` null,
    and `valid_scheduled_evidence=true`. This is a normal, healthy zero-order run. It must not
    record `error_class='missing_spread_data'`, because missing spread was not what produced
    the outcome.
  - **Candidates qualify but spread data is unavailable:** fail **only those ticker rows**
    closed, with row-level `spread_status='spread_unavailable'` (no bid/ask captured) or
    `'spread_not_ok'` (captured but outside tolerance), write the complete artifact set, and
    **submit any other qualifying rows** that safely pass every check. Never abort the whole
    run because spread is missing for a subset, and never place an order for a ticker whose
    spread could not be proven.
  - Spread is **never** estimated, inferred, or defaulted.
  See `config/scheduled_tasks.json` -> `spread_data_availability_policy`.
- **2026-07-27 about 2:55 PM ET: live task patch (`intraday_task_patch_2026_07_27`).** The
  live recurring intraday task `7be82b97` was patched to fix the 1:30 and 2:30 behavior seen
  earlier the same day. The patch tightens the artifact contract and separates a ladder
  no-candidate outcome from a spread-data outcome. It also originally required
  connector-native execution in place of escalation when local tooling was missing; that
  requirement was removed 2026-07-30 (see above). The **artifact contract** for the 1:30
  and 2:30 slots: write complete **root-level** artifacts under
  `/home/user/workspace/cron_tracking/intraday_dca/YYYYMMDD_HHMMET/` **every time**, namely
  `run_state.json`, `run_summary.json`, `signals.csv`, and `submitted_orders.csv` **with its
  canonical headers even when zero orders were submitted**. Writing the files only under an
  `artifacts/` subfolder does not satisfy the contract. Required top-level `run_state.json`
  fields: non-null `run_classification` (never a bare string), `run_origin`,
  `manual_run=false`, `stream`, `scheduled_for_et`, `cron_id='7be82b97'`,
  `state_schema_version`, `valid_scheduled_evidence`, `valid_recovery_context=false` for a
  normal scheduled run, `order_submit_status`, `orders_submitted_count`, and a concrete
  `error_class` or `completion_reason`. See `config/scheduled_tasks.json` ->
  `intraday_task_patch_2026_07_27`.
- **2026-07-27 about 2:55 PM ET: user-requested 2:30 rerun, recovery context only.** A rerun
  of the 2:30 slot ran immediately after the patch and wrote
  `/home/user/workspace/cron_tracking/intraday_dca/20260727_1430ET_rerun_1455ET/`
  (`run_id='intraday_rerun_20260727_1455ET'`, `run_origin='manual_live_recovery_rerun'`,
  `stream='intraday_1430_rerun'`, `state_schema_version='intraday-recovery-rerun-v1'`,
  `manual_run=false`). It is `valid_scheduled_evidence=false`. **Its `valid_recovery_context` is recorded FALSE**, together with the top-up flags: `run_origin='manual_live_recovery_rerun'` is neither `manual` nor `scheduled_recovery`, the only two origins `is_top_up_context_state()` accepts, so the afternoon guard rejects this artifact and it is NOT usable top-up context. This document claimed the opposite until it was corrected alongside the machine-readable mirrors (codex review: "Mark both intraday reruns as rejected recovery context"). No capital is affected — the pass placed zero orders — but an operator reading only this section was getting a different answer from the runtime.

  It used **fresh Robinhood quotes** and treated the same-day
  fills from the **9:35 AM, 11:30 AM, 12:30 PM recovery, and 2:32 PM recovery** streams as
  ticker-scoped same-day top-up context only. It placed **zero new orders**, because no
  ticker had a positive same-day top-up delta at a price lower than its relevant prior
  same-day reference. Reason counts across the 44 evaluated rows:
  `same_day_topup_not_lower` 25, `not_below_prior_close_or_zero_multiplier` 17,
  `same_day_topup_no_delta` 2. `submitted_orders.csv` was written with the canonical header
  and zero data rows, which is the correct shape for a zero-order run.

  This rerun is **recovery context only**. It is **NOT** scheduled proof that the 2:30 task
  ran, **NOT** slippage proof, and **NOT** watchdog scheduled-run confirmation for
  `a46bf007`. It does **not** replace or supersede the 2:32 PM ET recovery pass; both passes
  stand. It is also **not** a `failure_recovery_policy` recovery rerun and is recorded with
  `recovery_rerun=false`, because it fetched fresh data into a separate artifact root with a
  non-scheduled `run_origin`, and the 2:30 scheduled run persisted no files to recompute
  from. It placed zero orders, so it adds no same-day cost basis. It is recorded in
  `recovery_context_run_outcomes`, **not** in `scheduled_run_outcomes`. Machine-readable
  sources: `config/scheduled_tasks.json` ->
  `intraday_dca_scheduled_recovery_2026_07_27.rerun_pass`, and
  `config/run_classification.json` -> `intraday_rerun_2026_07_27_1455`.
- **2026-07-27 about 3:35 PM ET: a second 2:30 rerun, also recovery context only.** A
  further rerun of the 2:30 slot ran after the corrected same-day top-up evaluation and
  wrote `/home/user/workspace/cron_tracking/intraday_dca/20260727_1430ET_rerun_1535ET/`
  (`run_id='intraday_rerun_20260727_1535ET'`, `run_origin='manual_live_recovery_rerun'`,
  `stream='intraday_1430_rerun'`, `state_schema_version='intraday-recovery-rerun-v2'`,
  `manual_run=false`). It persisted `run_state.json`, `run_summary.json`, `signals.csv`,
  `submitted_orders.csv`, `raw/robinhood_quotes.json`, and
  `raw/robinhood_same_day_orders.json`, all four state files at the **root**, with
  `run_classification` a non-null object in both state files and `submitted_orders.csv`
  carrying the canonical 23-column header with zero data rows.

  It found **zero new qualifying orders**: `orders_candidate_count=0`,
  `orders_submitted_count=0`, `order_submit_status='completed_no_orders'`,
  `completion_reason='no_orders_after_corrected_same_day_topup'`. Reason counts across the
  44 evaluated rows: `not_below_prior_close_or_zero_multiplier` 13,
  `same_day_topup_not_lower` 31.

  Recorded `valid_scheduled_evidence=false`, `valid_recovery_context=false`,
  `same_day_topup_context_only=false`. **Its `valid_recovery_context` is recorded FALSE**, together with the top-up flags: `run_origin='manual_live_recovery_rerun'` is neither `manual` nor `scheduled_recovery`, the only two origins `is_top_up_context_state()` accepts, so the afternoon guard rejects this artifact and it is NOT usable top-up context. This document claimed the opposite until it was corrected alongside the machine-readable mirrors (codex review: "Mark both intraday reruns as rejected recovery context"). No capital is affected — the pass placed zero orders — but an operator reading only this section was getting a different answer from the runtime.

  This pass **does not** replace or supersede the 2:32
  PM ET recovery or the 2:55 PM ET rerun; all three stand as distinct records.
  Machine-readable sources: `config/scheduled_tasks.json` ->
  `intraday_dca_scheduled_recovery_2026_07_27.rerun_pass_1535`, and
  `config/run_classification.json` -> `intraday_rerun_2026_07_27_1535`.
- **The 2:30 recovery and both reruns are same-day top-up context only.** No later stream
  may treat any of them as scheduled proof that the 2:30 task ran, as afternoon scheduled
  evidence, as slippage scheduled evidence, or as scheduled cost-basis evidence. They live
  in `recovery_context_run_outcomes`, never in `scheduled_run_outcomes`.

  **Known gap, recorded rather than papered over:** the 2:32 PM ET recovery artifact is the
  only one of the three that moved capital (the EVR $1.00 top-up), and the afternoon top-up
  guard **will not load it**. Its persisted `run_classification` omits
  `same_day_top_up_context_only=true`, and
  `scripts.run_classification.validate_recovery_classification()` requires that flag as an
  explicit `true` and fails closed without it, so `is_top_up_context_state()` returns false.
  That single missing flag is the only failing condition; every other field in the contract
  is satisfied. The practical impact on 2026-07-27 was bounded, because both later reruns
  placed zero orders, so no duplicate EVR buy resulted. The fix is to add the flag to the
  **persisted artifact**, not to relax the validator: failing closed on a missing explicit
  flag is the intended behavior.


- **Cost-conscious model instruction:** use a cost-conscious/default model where available; do not use premium models unless needed for failure recovery, safety validation, or connector/debug escalation. Keep daily output compact, write JSON/CSV artifacts, suppress no-op notifications, and leave heavy analysis to Friday weekly or monthly review.

### `42173154`: Weekday 10:30 AM morning DCA live Robinhood execution (`MorningDCAExecutor`)
- **Live trading** (executor mode `live`). Runs **10:30 AM ET weekdays** — cron `30 14 * * 1-5` (**14:30 UTC weekdays** during EDT) — moved from the former 9:45 AM live slot to give a true **60-minute intraday trend window**.
- Uses the shared **$2 base 0x-to-5x live ladder** and the `morning` budget ledger (`config/morning_dca.json`). **Robinhood account <ROBINHOOD_ACCOUNT_NUMBER> is the only execution broker**; **Alpaca, Finance, and Yahoo are read-only** validation/account context only.
- **Depends on the same-day 9:45 pre-flight artifact** (`cron_tracking/morning_dca_preflight/`) and evaluates **only `eligible_to_execute=true` tickers**, then **recomputes a fresh price, prior close, spread, 60-minute downtrend, signal, and budget before placing** — and **skips / routes to manual review if the trend cannot be validated**. If the pre-flight artifact is missing, a **scheduled** run fails closed (`_apply_preflight_gate()` returns zero tickers and writes a blocked `run_state.json`); only a **manual** run degrades to the full universe with a warning.
- **Pre-flight artifact location (added Jul 20 2026):** the same-day 9:45 pre-flight artifact **must be loaded from** `/home/user/workspace/cron_tracking/morning_dca_preflight/YYYYMMDD_0945ET/run_state.json`, or from `latest_preflight.json` **only when that pointer points to the same-day** file. It **must not** be sought under `/home/user/workspace/cron_tracking/42173154` or under the morning live output directory `/home/user/workspace/cron_tracking/morning_dca/YYYYMMDD_1030ET/`; the morning live output directory holds this task's own artifacts only. If the artifact is missing from the correct path, perform exactly **one** bounded recovery lookup before escalating: scan `/home/user/workspace/cron_tracking/morning_dca_preflight/` for today's `YYYYMMDD_0945ET` directory and load its `run_state.json`. This fixes the **Jul 20 2026 escalation** where the run looked under the 10:30 live directory instead of the pre-flight root. It is a path-resolution fix only and does not weaken the budget caps, top-up guard, data-confidence rule, spread rule, whitelist, buy-only, regular-hours-only, Robinhood-execution-only, or any other safety control. **Implemented (2026-07-21)** in `morning_dca.py` (`load_morning_config()`, `load_same_day_preflight()`, `MorningDCAExecutor._apply_preflight_gate()`). See the `42173154` `preflight_artifact_resolution` block in [`config/scheduled_tasks.json`](../config/scheduled_tasks.json) and [morning_dca.md](morning_dca.md).
- **Morning budget ledger location (added Jul 20 2026):** the morning stream budget ledger lives under the morning live output directory `/home/user/workspace/cron_tracking/morning_dca/YYYYMMDD_1030ET/`; the task may create `budget_ledger.json` there if absent. It **does not require a pre-existing ledger file**: if none exists it initializes from the configured `morning_daily_cap_usd` (90.0) and `morning_weekly_cap_usd` (450.0) caps and writes the resulting budget calculations into the 10:30 `run_state.json`. A missing ledger file is bootstrapped, **not** escalated, and bootstrapping weakens no cap or safety control. **Implemented (2026-07-21)**: `MorningDCAExecutor.write_artifacts()` always writes `budget_ledger.json` under the run's own output directory as a snapshot of the same budget object written into `run_state.json`. See the `42173154` `budget_ledger_bootstrap` block in [`config/scheduled_tasks.json`](../config/scheduled_tasks.json).
- **Pre-authorized direct placement (scheduled live runs only):** as approved Fri Jul 10 2026 11:20 AM ET, this scheduled live task auto-places qualifying buys within all caps/circuit breakers **without per-order confirmation** and **without calling `review_equity_order`**; it calls `place_equity_order` directly only after every safeguard passes. **Manual / ad hoc runs still go through Robinhood's review + confirmation path:** `review_equity_order` must pass before `place_equity_order` is attempted, and if Robinhood's own review requires additional post-review confirmation (e.g. a step-up/2FA gate), placement raises `OrderConfirmationRequiredError` and the run records `not_submitted_confirmation_required` with **no order placed** — there is no separate confirmation step implemented in this code beyond that broker-driven gate.
- **GFUZ special handling:** GFUZ is in the active universe, but **Robinhood is its primary source** because **Finance may not resolve GFUZ**; an unresolved GFUZ on Finance **must not block the whole run**. The *intent* is that if the **9:45 pre-flight lacks GFUZ**, the 10:30 task runs a **supplemental GFUZ read-only validation** and **persists `gfuz_supplemental_preflight.json`** under `/home/user/workspace/cron_tracking/morning_dca/YYYYMMDD_1030ET/` before GFUZ decisioning — **not yet implemented**: today a missing GFUZ preflight row just gates GFUZ out (safe, fail-closed) via the plain `tickers ∩ eligible_tickers` intersection. See `config/morning_dca.json`'s `gfuz_special_handling.supplemental_preflight.implementation_status`.
- **Raw data persistence (added Jul 15 2026, confirmed Jul 16 2026):** the *intended* contract is that **before any signal computation, budget calculation, top-up calculation, or order placement**, the task persists every raw Robinhood/Finance/Alpaca/Yahoo connector response under `/home/user/workspace/cron_tracking/morning_dca/YYYYMMDD_1030ET/raw/` with deterministic filenames, **normalizing** `output_path`/wrapped outputs into parseable JSON, with the decision engine reading only from the persisted files (fetch → persist → normalize → compute → place), and if data fetch succeeds but raw persistence/normalization fails, placing no orders and still writing `run_state.json` with the blocking reason — **not yet implemented**: `MorningDCAExecutor.run()` evaluates directly from the in-memory data provider. See `config/morning_dca.json`'s `raw_data_persistence.implementation_status`.
- **Automatic failure recovery:** the *intended* contract is that if the run fails because of internal persistence, parsing, wrapper-normalization, transient connector throttling, or a raw-file write problem **after data was fetched**, it immediately **reruns the required read-only inputs once**, persists all raw responses first, **recomputes only from the persisted files**, and continues the same guarded execution if safeguards pass, reusing the **same logical run directory** with **`recovery_rerun=true`** and **never classifying recovery as manual** — **not yet implemented**: there is no recovery-rerun code path in `morning_dca.py` today. See `config/morning_dca.json`'s `automatic_failure_recovery.implementation_status`.
- **Confidence rule:** if a candidate's price/drop passes the ladder but the **intraday trend cannot be validated** (fewer than `trend_min_bars` usable intraday bars at/before the decision time), the run does **not** auto-buy; it classifies the candidate as `execution_status='manual_review_candidate'`. There is no loosened trend-unavailable execution path.
- **Hard circuit breakers:** max per order **$10**, budget caps, available buying power, whitelist-only, data confidence, spread threshold, trend validation, regular-hours-only, and one strategy order per ticker per run — each **skips** rather than resizing past a cap.
- **Artifacts:** always writes `run_state.json` and `morning_signals.csv` under `/home/user/workspace/cron_tracking/morning_dca/YYYYMMDD_1030ET/` (`run_id='morning_live_YYYYMMDD_1030ET'`) — **not** `signals.csv`, and **not** `run_summary.json`/`validation.csv` (those are preflight-only). It additionally writes `submitted_orders.csv` whenever at least one order was actually submitted; `filled_orders.csv` is **never** written by this task (there is no fill-confirmation step). See `config/morning_dca.json`'s `artifacts.manifest_accuracy_note`.
- **Raw input persistence (implemented Jul 21 2026):** before `evaluate_signal()`/`_place_broker_orders()` computes from a connector response, that response (previous close, intraday bars, quotes, bid/ask, account number, buying power, tradability, same-day order existence) is persisted under `.../morning_dca/YYYYMMDD_1030ET/raw/`. A persistence failure caught before any order is placed blocks the run with `status='blocked'` and `error_class='missing_or_invalid_raw_inputs'`; a failure discovered only after placement has begun is recorded on `raw_persistence_error` without retroactively blocking. Applies identically to the 12:30 midday task. See `config/morning_dca.json`'s `raw_data_persistence.implementation_status`.

- **Cost-conscious model instruction (Jul 17 2026):** use a cost-conscious/default model where available; do not use ChatGPT 5.5 or premium models unless needed for failure recovery, safety validation, or connector/debug escalation. Keep daily output compact, write JSON/CSV artifacts, suppress no-op notifications, and leave heavy analysis to Friday weekly or monthly review. See the top-of-file cost-conscious model instruction note.

#### Manual no-purchase dry run, 2026-07-21 09:05 ET (diagnostics only)
- **Manual dry run**, **not scheduled**, **no purchases**: `run_id='manual_dry_run_20260721_0905ET'`,
  `run_origin='manual_dry_run'`, `dry_run=true`, `no_purchases=true`,
  `valid_scheduled_evidence=false`. This is a readiness diagnostic only. It is
  **NOT** scheduled evidence, **NOT** top-up proof, **NOT** slippage evidence, and
  **NOT** confirmation that any scheduled task ran.
- **Coverage:** scanned the full **44-symbol** active universe. Finance lookup
  completed with expected unresolved fallback cases **AAAU, SPCX, GFUZ**.
  Robinhood account access completed with configured account **<ROBINHOOD_ACCOUNT_NUMBER>** present;
  portfolio read completed; **3** quote batches and **5** tradability batches
  completed; `quote_available_count=44`, `prior_close_available_count=44`,
  `tradability_returned_count=44`. Alpaca account read completed, status **ACTIVE**,
  `buying_power=0`.
- **No execution calls:** no `review_equity_order`, no `place_equity_order`, no
  cancellations, and no broker modifications were called.
- **Positive dry-run signal (hypothetical only, not placed):** **CBRS**,
  `drop_pct=0.1074`, multiplier **1x**, hypothetical amount **$2**.
- **Artifacts:** `/home/user/workspace/cron_tracking/dry_runs/20260721_0905ET/` with
  `run_state.json`, `run_summary.json`, `signals_dry_run.csv`, and raw connector
  outputs under `raw/`. These dry-run artifacts must not be treated as scheduled
  evidence, top-up proof, slippage evidence, or confirmation that any scheduled task
  ran.
- Machine-readable records: `config/scheduled_tasks.json`
  (`morning_manual_dry_run_2026_07_21`) and `config/run_classification.json`
  (`manual_dry_run_2026_07_21`). See [run_classification.md](run_classification.md).

### `a6239dae`: Weekday 12:30 PM midday DCA live Robinhood execution (`MiddayDCAExecutor`)
- **Live trading** (executor mode `live`). Runs **12:30 PM ET weekdays** (window 12:25–12:35 PM ET).
- Uses the shared **$2 base 0x-to-5x live ladder** (`config/midday_dca.json`, the same ladder used by all live streams as of Jul 10 2026) and its own `midday` budget ledger. **Robinhood account <ROBINHOOD_ACCOUNT_NUMBER> only**; Alpaca is read-only for validation/context.
- Same active universe. **Confidence rule:** if a candidate's price/drop passes the ladder but the **intraday trend cannot be validated** (fewer than `trend_min_bars` usable intraday bars at/before 12:30), the run does **not** auto-buy; it classifies the candidate as `execution_status='manual_review_candidate'`.
- Writes live artifacts under `/home/user/workspace/cron_tracking/midday_dca/YYYYMMDD_1230ET/` (`run_id='midday_live_YYYYMMDD_1230ET'`) and updates its own `latest_midday_run.json` pointer.
- `stream='midday'`, `run_type='midday_dca_live'`, `state_schema_version='midday-live-v1'`, `cron_id='a6239dae'`, `supersedes_cron_id=null`, `valid_afternoon_evidence=false`. Never writes into the morning or afternoon (`ce16ba6b`) trees or updates `latest_scheduled_run.json`.
- **Automatic failure recovery:** if the run fails on internal persistence, parsing, wrapper-normalization, transient connector throttling, or raw-file write problems **after data was fetched**, it is a recovery trigger, not a terminal stop. The task reruns the required read-only inputs **once**, **persists all raw responses first**, **recomputes only from the persisted files**, and continues through the same pre-authorized guarded path if every safeguard still passes. The recovery reuses the same `midday_dca/YYYYMMDD_1230ET/` scheduled run directory with `recovery_rerun=true` and stays `stream='midday'` scheduled (**never** reclassified as manual — distinct from the `manual_reruns/` override tree). If the recovery attempt also fails or a safety/account/order rejection occurs, it writes `run_state.json` with the blocking reason and escalates.
- **Raw input generation and failure-state fix (Jul 20 2026):** after the Jul 20 2026 12:30 run escalated, the task must **generate the same-day raw inputs before invoking any saved runner** (e.g. `.../cron_tracking/a6239dae/run_midday_live.sh` or `midday_live_runner.py`). It creates `.../midday_dca/YYYYMMDD_1230ET/raw/` and persists `raw/portfolio.json`, `raw/quotes.json` (or named quote batches normalized into it), and the historical/trend files for the 60-minute trend check, passing the current America/New_York date as `RUN_DATE_ET=YYYYMMDD` when required; it must **not** assume `raw/quotes.json` or historicals exist from a prior run. Inputs come from Finance primary, Alpaca/Robinhood validation-context, and `yfinance` fallback only when connector data is unavailable, stale, incomplete, or unresolved; one bounded recovery attempt on the alternate source path is allowed, then stop. The run **always** writes `run_state.json`, `run_summary.json`, and `latest_midday_run.json` even with no orders or a blocked run; on missing/malformed/stale/ungenerated raw inputs it writes a valid scheduled `run_state` with `run_origin='scheduled'`, `manual_run=false`, `stream='midday'`, `scheduled_for_et='12:30'`, `cron_id='a6239dae'`, `valid_afternoon_evidence=false`, `order_submit_status='not_submitted_error'`, `error_class='missing_or_invalid_raw_inputs'`, `orders_submitted_count=0`, and a compact list of missing files or failed data sources — proof the task **fired**, not order/fill evidence. Do not escalate without writing it unless the workspace itself is unavailable. Same-day manual missed-10:30 recovery artifacts are loaded **after** raw data is available and **before** order sizing, stay prior-allocation/top-up context only, and never satisfy raw input requirements, enter the scheduled evidence stream, or prove the 10:30 scheduled task ran. See [midday_dca.md](midday_dca.md#raw-input-generation-and-failure-state-guarantee-added-jul-20-2026).
- **Same-day top-up guard:** morning/manual earlier same-day allocations (before **2:30 PM ET**, non-scheduled) are **prior-allocation context only**, never midday scheduled evidence. Per ticker, the run buys only the **incremental** dollar difference (midday desired minus amount already bought) and only when the midday decision price is **strictly below** the prior reference price; otherwise **$0**. The run_state records `prior_same_day_amount`, `prior_same_day_reference_price`, `prior_same_day_run_id`/`prior_same_day_source`, `midday_desired_amount`, `midday_decision_price`, `incremental_order_amount`, `top_up_guard_result`, `top_up_skipped`, and `top_up_manual_review`. Midday fills count only as prior-allocation context for the afternoon 3:30 PM top-up guard. See [midday_dca.md](midday_dca.md).
- **Live reliability fix (Jul 17 2026):** before calling `place_equity_order`, describe the Robinhood connector schemas needed in-session (`place_equity_order` and `get_equity_orders`); always write the final `run_state.json`, `run_summary.json`, `signals.csv`, `submitted_orders.csv`, and `filled_orders.csv` where applicable, even for no-order/blocked/failed/recovery runs; every actionable signal ends with exactly one persisted result (`order_id`, broker rejection, top-up/budget/cap/data-confidence/spread/trend/safety block); an actionable signal missing both an `order_id` and a block/rejection is classified `execution_lineage_incomplete`, triggering one recovery retry in the same scheduled directory for only the missing tickers with the same `ref_id` values, reconciling duplicate `ref_id` or timeout through `get_equity_orders` before declaring failure. Manual overrides stay `manual_run=true` and never count as scheduled evidence. See the top-of-file live reliability fix note.
- **Live 12:30 no-placeholder fix (Jul 17 2026):** the scheduled midday run must **never** write placeholder signals or empty `submitted_orders.csv` / `filled_orders.csv` while marking `valid_midday_evidence=true`; if it cannot compute full signals it must write `valid_midday_evidence=false` with concrete `blocking_errors` and notify; it must parse Robinhood `get_equity_quotes` results from `data.results[].quote` and `data.results[].close`; it must follow `output_path` pointers for large `get_equity_historicals` responses and load the referenced result JSON before computing trend; a placeholder artifact with `blocking_errors=['persistence_placeholder_no_signal_compute']` is a **failed run** that must trigger recovery, not a completed scheduled run. See the top-of-file no-placeholder fix note.
- **Jul 17 2026 12:30 recovery result:** the original background task escalated after writing placeholder artifacts with `blocking_errors=['persistence_placeholder_no_signal_compute']`. Parent recovery persisted raw quote, history, tradability, portfolio, and order-history files under `/home/user/workspace/cron_tracking/midday_dca/20260717_1230ET/raw/`, corrected the nested Robinhood quote parser and the `output_path` historical parser, and found **exactly one** eligible scheduled midday buy: **TRIN $3.00** (`order_id 6a5a5b53-6502-476d-b6d4-9cc73bab4f31`, state **filled**, `average_fill_price 17.736500`, `quantity 0.169142`, `created_at 2026-07-17T16:41:55.91467Z`). Marked `valid_midday_evidence=true`, `valid_afternoon_evidence=false`; the Jul 17 morning manual override remains manual context only.
- **Manual missed-12:30 recovery (Jul 20 2026):** after the Jul 20 2026 12:30 scheduled midday run failed, a human placed the missed midday buys by hand at 12:59 ET under `/home/user/workspace/cron_tracking/midday_dca/manual_recovery/20260720_1259ET` (fills **AAPL $1.00** @ 324.389900, **TRIN $2.00** @ 17.399900, **ASML $2.00** @ 1739.506300, **IRDM $2.00** @ 46.669900). It carries `run_origin='manual'`, `manual_run=true`, `same_day_top_up_context_only=true`, `valid_scheduled_evidence=false` and is prior-allocation / same-day top-up context **only** for the 3:20 afternoon run — never midday scheduled evidence, never proof the 12:30 scheduled task ran, and never counted in scheduled reliability, fill counts, slippage, cost basis, or watchdog confirmation. Machine-readable source: `config/scheduled_tasks.json` → `midday_dca_manual_recovery_2026_07_20` (mirrored in `config/midday_dca.json`). See [midday_dca.md](midday_dca.md#manual-missed-1230-recovery-added-jul-20-2026).
- **Failed 12:30 start and 1:05 PM ET recovery pass (Jul 27 2026):** on 2026-07-27 the 12:30 PM ET scheduled midday run failed to start, or failed before writing the normal same-day scheduled artifacts. The scheduled artifact root `/home/user/workspace/cron_tracking/midday_dca/20260727_1230ET/` was **never created**, so there is **no scheduled evidence** for the 12:30 slot that day. A 12:30 recovery pass ran at about **1:05 PM ET** into its own separate root `/home/user/workspace/cron_tracking/midday_dca/20260727_1230ET_recovery_1305ET/` (`run_id='midday_recovery_20260727_1305ET'`, `run_origin='scheduled_recovery'`, `stream='midday_recovery'`, `state_schema_version='midday-live-v2-recovery'`), using the **11:30 AM intraday fills** and the **9:35 AM market-open fills** as ticker-scoped same-day top-up context only. It submitted 6 orders and all 6 filled, for **$20.50** of recovery spend:

  | Ticker | Amount | Order id | Average fill price | Quantity | First allocation |
  |---|---|---|---|---|---|
  | SPCX | $3.50 | `6a679049-d567-45c3-b47e-270ebcf6f7e2` | 109.989900 | 0.031821 | no (top-up) |
  | DXYZ | $1.00 | `6a679049-f3df-4185-9d84-30e9b5b4abb4` | 22.601700 | 0.044244 | no (top-up) |
  | FLKR | $4.00 | `6a67904a-d595-4c2e-aca2-3a6ccd9191c0` | 51.627900 | 0.077477 | yes |
  | AMZN | $1.00 | `6a679049-3017-4b37-984e-4cd4b581f0a3` | 231.429900 | 0.004320 | yes |
  | GS | $1.00 | `6a679049-15ed-4607-869b-17adae1673ee` | 1041.181500 | 0.000960 | no (top-up) |
  | GFUZ | $10.00 | `6a679049-7914-49b7-b2a6-2b13159da126` | 6.249900 | 1.600025 | yes |

  This artifact is **recovery context only**. It is **NOT** valid scheduled evidence for the 12:30 scheduled task `a6239dae`, **NOT** slippage scheduled proof, **NOT** watchdog scheduled-run confirmation for `a46bf007`, and **NOT** afternoon scheduled proof. No completeness of its artifact set promotes it: `valid_scheduled_evidence=false`, `valid_recovery_context=true`, `use_for_slippage_scheduled_proof=false`, `exclude_from_watchdog_scheduled_confirmation=true`. It is valid **only** as ticker-scoped same-day top-up context for later same-day streams, including the 3:30 PM ET final buy `10ce362c`. `latest_midday_recovery_run.json` was written and did **not** overwrite `latest_midday_run.json`. Quantities are transcribed from the persisted `filled_orders.csv`; the SPCX quantity there is `0.031821`, which reconciles with `3.50 / 109.989900`.

  This pass is also **not** a `failure_recovery_policy` recovery rerun, and is recorded with `recovery_rerun=false`. That policy covers only a failure **after** data was fetched, repaired in place under `midday_dca/YYYYMMDD_1230ET/` with `run_origin='scheduled'`; this pass matches neither condition, so a consumer applying the policy must not read it as an in-place scheduled recovery. The non-conformance is spelled out in `recovery_policy_conformance` on the task entry.

  **Consumers can now actually read it.** The artifact validates against `schemas/recovery_run_classification.schema.json`; the per-stream scheduled schemas still reject every one of its values, by design. Its pointer uses `pointer_status="valid_recovery_context"` via `same_day_pointer.validate_recovery_pointer()`, because `"valid"` is pinned to `valid_scheduled_evidence=true`, which this pointer must never claim. That status stays in `REJECTED_STATUSES`, so scheduled consumers keep ignoring it.

  `near_close_dca._orders_csv_fills` accepts the persisted `average_fill_price`/`quantity` fill columns, and `midday_recovery` is listed in `_RECOVERY_PRIOR_CONTEXT_STREAMS`, so the 3:30 final buy really does load these fills instead of re-allocating the six tickers. Naming the stream is not sufficient on its own: `_prior_run_is_scheduled` requires the full scheduled-recovery contract before any recovery artifact may reduce a later buy, so a corrupted or misclassified file that merely sets `stream="midday_recovery"` is ignored rather than trusted.

  `run_classification.collect_recovery_context_order_ids()` lets the watchdog's all-day reconciliation classify the six order IDs as recovery context rather than flag them as an unexplained broker/log discrepancy. It validates each artifact before collecting, so a malformed, manual, or scheduled-evidence state written under a `*_recovery_*` directory is not laundered into the recovery bucket. Being classified is not being counted: these IDs stay out of every scheduled metric. It also accepts a `stream_roots` argument, so a deployment that relocates a stream root outside the cron-tracking tree (a configured `artifact_paths.live_run_state_root`, or `DCA_INTRADAY_LIVE_STATE_ROOT` / `DCA_MARKET_OPEN_LIVE_STATE_ROOT`) still has its recovery directories discovered. The watchdog `a46bf007` has no in-repo executor, so the contract its task text must follow is recorded under `watchdog_all_orders_fix_2026_07_21.recovery_context_classification` in `config/scheduled_tasks.json`.

  **One contract, one implementation.** `run_classification.validate_recovery_classification` is the single source of truth for whether a recovery artifact may be trusted, and the afternoon top-up guard, the recovery pointer validator, and `is_top_up_context_state` all delegate to it. It requires the origin and flag markers, a mutually consistent `stream`/`run_type`/`state_schema_version` triple (so one slot's identity cannot be borrowed by another), a non-blank `run_id`, and the classification's own `trading_day`. The stream/run_type/version tables are pinned to `schemas/recovery_run_classification.schema.json` by a test, so code and schema cannot drift apart.

  Its fills are recorded in the separate `recovery_context_run_outcomes` ledger, **not** in `scheduled_run_outcomes`, whose collection contract is scheduled evidence that may be aggregated without a per-row filter. The 12:30 slot itself stays in `scheduled_run_outcomes.runs` as a failed start with zero fills and no `fills[]` array. Machine-readable sources: `config/scheduled_tasks.json` -> `midday_dca_scheduled_recovery_2026_07_27`, `recovery_context_run_outcomes` (recovery fills), and `scheduled_run_outcomes` (the failed 12:30 slot), and `config/run_classification.json` -> `midday_scheduled_recovery_2026_07_27`.

- **Cost-conscious model instruction (Jul 17 2026):** use a cost-conscious/default model where available; do not use ChatGPT 5.5 or premium models unless needed for failure recovery, safety validation, or connector/debug escalation. Keep daily output compact, write JSON/CSV artifacts, suppress no-op notifications, and leave heavy analysis to Friday weekly or monthly review. See the top-of-file cost-conscious model instruction note.

### `e3e6f09e`: $2-base DCA monitor-only signal check (PAUSED)
> **PAUSED as of 2026-07-27.** It does not fire while paused. Its cron
> (`5 19 * * 1-5`, 3:05 PM ET weekdays) is retained so it can be resumed.
- Analytics and notifications only; **no trades**. Runs **3:05 PM ET weekdays**.
- Monitor-only signal check that surfaces the planned allocation and tier selection using the current $2 base 0x-to-5x ladder.
- **Pointer output normalization (Jul 20 2026):** although this task places no orders, it uses connector data, so any connector response returning an `output_path` or `current_session_context` pointer must be copied or normalized into the run raw directory before compute, and compute must read only the persisted raw files. See `config/scheduled_tasks.json` → `pointer_output_normalization_2026_07_20`.

- **Cost-conscious model instruction (Jul 17 2026):** use a cost-conscious/default model where available; do not use ChatGPT 5.5 or premium models unless needed for failure recovery, safety validation, or connector/debug escalation. Keep daily output compact, write JSON/CSV artifacts, suppress no-op notifications, and leave heavy analysis to Friday weekly or monthly review. See the top-of-file cost-conscious model instruction note.

### `10ce362c`: Final DCA live Robinhood execution, $2 allocation (3:30 PM ET)
- Repaired live task `10ce362c`, which **supersedes the deleted `ce16ba6b`**; the artifact root stays under `.../ce16ba6b` for continuity.
- Approved live trading task running the **$2 base 0x-to-5x ladder with tiered caps**. Places real market buy orders automatically within safeguards. Runs **3:30 PM ET weekdays**.
- Uses **only** Robinhood account `<ROBINHOOD_ACCOUNT_NUMBER>`; Alpaca is read-only.
- Trigger gate: `programmatic_trigger_check.sh`, which must allow exactly the weekdays inside `config/trading_window.json`'s window (**2026-07-02 through 2026-10-30** since the 2026-07-26 extension; the gate was last documented against the original 2026-08-28 end date and has not been re-verified against the extension, see T:F02 in the 2026-09-07 diagnostic), **3:30–3:35 PM ET** (retimed from 3:20 PM; aligned to the 15:25–15:35 ET scheduled classification window; order attribution keeps its lower bound at 15:15 ET so legacy 3:20 PM runs stay attributable) so a late-but-permitted start cannot place buys after the 3:35 PM post-trade sync). This script is scheduler-side (outside this repo); it was **not** updated by the 2026-07-26 extension of `config/trading_window.json` to 2026-10-30 and needs separate verification/extension so the two gates stay in sync. Note that `config/trading_window.json` deliberately stops at 2026-10-30 (the Friday before the 2026-11-01 DST change) rather than going further, since this task's `30 19 * * 1-5` cron (retimed from `20 19 * * 1-5`) and this trigger gate are fixed-UTC/EDT-only — do not extend either past that date without first making them DST-aware.
- Independent of both gates above, `_check_execution_gates` in `near_close_dca.py` also blocks on any full NYSE closure (via the computed `nyse_holidays()` calendar) even on a date `config/trading_window.json` approves, so extending the window can never silently reopen a holiday like Labor Day or Thanksgiving for live orders.
- Writes standardized same-day state files under `/home/user/workspace/cron_tracking/ce16ba6b` ([`schemas/run_state.schema.json`](../schemas/run_state.schema.json)); must **not** fall back to interactive confirmation.
- **State-file guarantee:** **always** writes the scheduled `run_state.json` and updates `latest_scheduled_run.json` on every fire, **even when no order is placed**. A confirmation-blocked run (review passed, submission blocked by required post-review confirmation) records `order_submit_status='not_submitted_confirmation_required'`, `reviewed_order` details where available, candidate details, and **no `order_id`** — valid scheduled-run evidence, not order/fill evidence.
- **Run classification:** a scheduled run writes `scheduled_runs/YYYYMMDD_1530ET/run_state.json` (`run_origin='scheduled'`, `manual_run=false`, `scheduled_for_et='15:30'`, `state_schema_version='run-classification-v1'`, `run_id='scheduled_YYYYMMDD_1530ET'`, and **both** `cron_id='10ce362c'` **and** `supersedes_cron_id='ce16ba6b'`) and updates `latest_scheduled_run.json`; a manual run writes `manual_runs/<timestamp>/run_state.json` (`run_origin='manual'`, `manual_run=true`) and never updates that pointer. Placeholder/ambiguous ids such as `ce16ba6b_replacement` are forbidden; for continuity, consumers still accept `cron_id='10ce362c'` OR `supersedes_cron_id='ce16ba6b'` as valid evidence. See [run_classification.md](run_classification.md).
- **Retimed to 3:30 PM ET on 2026-07-27** (from 3:20 PM ET). Writers emit only the canonical `15:30` / `scheduled_YYYYMMDD_1530ET` identity. Validators also **accept** the legacy `15:20` / `scheduled_YYYYMMDD_1520ET` identity so run_states written before the retime stay valid scheduled evidence. The artifact root stays under `cron_tracking/ce16ba6b/` for continuity.
- **Manual runs do not count as scheduled-run evidence.** Any stale stored display name is superseded by the authoritative **3:30 PM ET** schedule.
- **RAW DATA PERSISTENCE FIX ADDED JUL 10 2026:** the 2026-07-10 3:20 PM run
  (`cron_id 10ce362c`) escalated because quotes and historicals were fetched
  successfully but the **raw connector responses were never persisted to disk
  before signal computation** — the bash compute step had no
  `quotes_batchN.json` / `historicals_batchN.json` to read. From this date on,
  **before signal computation** the scheduled run must **persist every raw
  Robinhood/Finance/Alpaca/Yahoo connector response** it used under
  `scheduled_runs/YYYYMMDD_1530ET/raw/` with deterministic filenames:
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
- **Ticker-scoped top-up context on 2026-07-27 (confirmed):** the 3:30 PM ET final buy reads the earlier same-day fills for that day, specifically the **9:35 AM market-open** fills, the **11:30 AM intraday** fills, and the **12:30 recovery pass** `midday_recovery_20260727_1305ET`, **only as ticker-scoped top-up context**. Prior same-day fills are matched **per ticker**: a prior buy in one ticker adjusts only that ticker's remaining top-up delta and **MUST NEVER** suppress, gate, or skip another ticker's **first-allocation** path. A ticker with no prior same-day fill remains eligible for its full first allocation under `canonical_first_buy_and_topup_rule`. The 12:30 recovery pass itself demonstrates this scoping: FLKR, AMZN, and GFUZ executed first allocations with no prior same-day reference price in the same pass where SPCX, DXYZ, and GS were ticker-scoped top-ups. The recovery artifact contributes top-up context only and does **not** satisfy the 3:30 task's own scheduled-evidence requirements. See `config/scheduled_tasks.json` -> `midday_dca_scheduled_recovery_2026_07_27.final_buy_topup_context_rule`.
- **Final buy of the day behavior:** for the **final live buy of the day** the run performs a **full active-universe scan** and, **for this final-buy decision only**, **ignores the 60-minute downward-trend filter**. Every other gate still holds: current/decision price **strictly below the prior completed regular-session close**, ladder **multiplier > 0** (from the $2-base 0x-to-5x ladder), **data-confidence** pass, **`spread_ok=true`**, **budget/tiered-cap** pass, **regular trading hours only**, **whitelisted (active-universe) ticker**, and all **hard circuit breakers** clear. The same-day top-up guard above still applies to the final buy. (Morning 10:30 and midday 12:30 live runs still require trend validation unless changed later.) See [strategy.md](strategy.md#final-buy-of-the-day-320-pm-et-task-10ce362c).
- **Jul 13 2026 final manual override incident:** the scheduled 3:20 PM ET run placed **0 orders**; the user then requested the final buy **manually**. A final manual-review scan ran the final-buy behavior above (full scan, trend ignored) with same-day top-up offsets and filled **34 orders**, planned total **$234.00** (`DFTX TRIN SPCX LUNR IRDM DXYZ LITE ASML DRAM STX AMAT AMD CBRS MRVL BTX STM SNDK WDC TSM AVGO NVDA QBTS FLKR INTC GOOGL GOOG GLW GS MU XLG TOPT QTOP SATA RGTI`). These are **manual final-buy overrides / same-day top-up context only — NOT scheduled-afternoon execution evidence**. Their run_state carries `run_type='final_manual_buy_override'`, `run_origin='manual'`, `manual_run=true`, `valid_afternoon_scheduled_evidence=false`, `same_day_top_up_context_only=true`. Artifacts (run_state + `filled_orders.csv` with all order IDs) were backfilled under `/home/user/workspace/cron_tracking/ce16ba6b/manual_runs/final_manual_override_20260713_1536ET/` and `/home/user/workspace/cron_tracking/midday_dca/manual_reruns/final_manual_override_20260713_1536ET/`. All order IDs are excluded from scheduled slippage/fills/reliability/cost basis/watchdog confirmation and shown only in the **"Manual Final Buy Override / Same-Day Top-Up Context"** section. See [run_classification.md](run_classification.md) and [strategy.md](strategy.md#manual-final-buy-override--2026-07-13-incident).
- **Aug 18 2026 manual cap-override incident:** the 2:30 PM and 3:30 PM Perplexity-primary runs were blocked by daily-cap usage. The user then explicitly instructed Perplexity to ignore the daily cap and buy the combined skipped candidates with a **$10 per-ticker cap**. The manual foreground override `manual_override_20260818_1555ET` filled **34 Robinhood Agentic buy orders** totaling **$207.00** (`DRAM $10, CBRS $10, MRVL $10, STM $10, SNDK $10, WDC $9, TSM $7, QBTS $8, FLKR $10, INTC $10, GLW $10, MU $10, QTOP $5, RGTI $7, AAAU $3, SPCX $2, EVR $3, LUNR $2.50, IRDM $2, DXYZ $2, LITE $10, ASML $6, STX $10, AMAT $6, AMD $6, BTX $4, AVGO $5, NVDA $4, AMZN $1, GS $3, XLG $2, TOPT $2, GFUZ $1.50, TXN $6`). This artifact is **manual foreground context only, NOT scheduled 2:30 or 3:30 execution evidence**. Its run_state carries `run_origin='manual_user_cap_override'`, `manual_run=true`, `stream='manual_override'`, `valid_scheduled_evidence=false`, `foreground_primary_surrogate_context=true`, `valid_primary_executor_evidence=false`, `same_day_top_up_context_only=true`, and `state_schema_version='manual-override-v1'`. Artifacts live under `/home/user/workspace/cron_tracking/perplexity_primary_20260818/manual_override_1555ET/`. Exclude these order IDs from scheduled slippage, scheduled fills, scheduled reliability, scheduled cost basis, and watchdog scheduled proof; use them only as manual foreground context and same-day or future top-up allocation context where applicable. `same_day_top_up_context_only=true` is REQUIRED for `scripts.run_classification.is_top_up_context_state()` to actually treat this artifact as usable top-up context (Codex review, PR #656 follow-up: "Match the override predicate to its persisted artifact shape") -- every other manual-context artifact type (final manual buy override, EVR recovery) declares it explicitly; this incident's contract must too. This explicit manual override does **not** weaken future scheduled cap enforcement.
- **Automatic failure recovery:** if the run fails on internal persistence, parsing, wrapper-normalization, transient connector throttling, or raw-file write problems **after data was fetched**, it is a recovery trigger, not a terminal stop. The task reruns the required read-only inputs **once**, **persists all raw responses first**, **recomputes only from the persisted files**, and continues through the same pre-authorized guarded path if every safeguard still passes. The recovery reuses the same `scheduled_runs/YYYYMMDD_1530ET/` directory with `recovery_rerun=true`, keeps `run_origin='scheduled'` (both `cron_id='10ce362c'` and `supersedes_cron_id='ce16ba6b'`), and still updates `latest_scheduled_run.json`. It is **never** reclassified as manual. If the recovery attempt also fails or a safety/account/order rejection occurs, it writes `run_state.json` with the blocking reason and escalates.
- **Live reliability fix (Jul 17 2026):** before calling `place_equity_order`, describe the Robinhood connector schemas needed in-session (`place_equity_order` and `get_equity_orders`); always write the final `run_state.json`, `run_summary.json`, `signals.csv`, `submitted_orders.csv`, and `filled_orders.csv` where applicable, even for no-order/blocked/failed/recovery runs; every actionable signal ends with exactly one persisted result (`order_id`, broker rejection, top-up/budget/cap/data-confidence/spread/trend/safety block); an actionable signal missing both an `order_id` and a block/rejection is classified `execution_lineage_incomplete`, triggering one recovery retry in the same scheduled directory (`scheduled_runs/YYYYMMDD_1530ET/`) for only the missing tickers with the same `ref_id` values, reconciling duplicate `ref_id` or timeout through `get_equity_orders` before declaring failure. Manual overrides stay `manual_run=true` and never count as scheduled evidence. See the top-of-file live reliability fix note.
- **3:30 placement contract (Jul 22 2026):** the final live task must not stop at compute-only planning. The Jul 22 run generated 20 planned orders with planned spend of $60 but submitted zero orders because the saved compute module ended with `error_class='placement_not_implemented_in_compute_module'`. From this date forward, planned eligible rows with `incremental_usd > 0` must enter the same-run placement phase. If the compute module cannot place orders, the task invokes the main live placement runner or direct connector placement path and calls `place_equity_order` after all normal safeguards pass. It must not call `review_equity_order`. If placement is unreachable, it records `order_submit_status='blocked_placement_phase_unreachable'`, `error_class='placement_phase_unreachable'`, writes `submitted_orders.csv` with headers, notifies, and marks completed-order evidence false. Both `run_state.json` and `run_summary.json` must carry the same non-null `run_classification`; null `run_summary.run_classification` is an artifact-contract failure. Machine-readable source: `config/scheduled_tasks.json` → `afternoon_placement_contract_2026_07_22`.
- **Pointer output normalization (Jul 20 2026):** the Jul 20 3:20 scheduled run collected portfolio, quotes, tradability, same-day orders, and historicals, but large historicals (and possibly other connector responses) were only available as `output_path` / `current_session_context` tool-output **pointers** that the background run never persisted into the scheduled raw directory before compute, so it **escalated after data collection**. Any connector response returning an `output_path` or `current_session_context` pointer must be **immediately copied or normalized into the run raw directory before compute**; compute must read **only** persisted raw files; **pointer-only collection is not a completed run** (never stop after collecting data without writing `run_state`); and if pointer normalization fails after one bounded recovery attempt, write the valid failure-state file with a concrete `error_class`, empty `submitted_orders.csv` headers where applicable, and the missing pointer details. Parent recovery copied the `current_session_context` / `tool_calls` / `call_external_tool` outputs into `.../ce16ba6b/scheduled_runs/20260720_1520ET/raw/`, computed from the persisted files, and placed scheduled top-up orders: `run_id='scheduled_20260720_1520ET'`, `order_submit_status='filled'`, `orders_filled_count=6`, total **$10.00** (EVR $1.00 @ 333.600800, DXYZ $1.00 @ 24.865000, AMAT $2.00 @ 526.819900, STM $2.00 @ 61.450000, NVDA $2.00 @ 202.771200, GLW $2.00 @ 153.810400), valid scheduled evidence recovered after the escalation. Live task text updated with **POINTER OUTPUT NORMALIZATION ADDED JUL 20 2026** for 10:30, 12:30, 3:05, and 3:30. Machine-readable source: `config/scheduled_tasks.json` → `pointer_output_normalization_2026_07_20` (mirrored in `config/run_classification.json`).
- **Compact live task text (Jul 20 2026):** because the schedule service appears to truncate or drop the tail of very long task text, `10ce362c` was replaced with a compact **~5,850 character** source-of-truth version that keeps all essential safeguards inside the accepted text: the **RAW INPUT SELF-CONTAINMENT FIX ADDED JUL 20 2026**, the **STATE FILE GUARANTEE AND NO HALF-DONE RUN RULE ADDED JUL 20 2026**, the same-day top-up guard, the Jul 20 midday recovery artifact path `/home/user/workspace/cron_tracking/midday_dca/manual_recovery/20260720_1259ET`, and the filled AAPL/TRIN/ASML/IRDM recovery context as prior-allocation / top-up context only. Live task text must stay compact so critical guardrails are not pushed past the truncation boundary. Machine-readable source: `config/scheduled_tasks.json` → `live_task_text_compactness_policy`.
- **3:30 runner self-containment and state-file guarantee (Jul 20 2026):** part of the full Jul 20 2026 hardening after the 10:30 and 12:30 runs failed. Before invoking any saved runner or order module the task must **generate its same-day raw inputs**, creating `.../ce16ba6b/scheduled_runs/YYYYMMDD_1530ET/raw/` and persisting portfolio / buying power, quotes, prior close, tradability, `orders_after_1430ET` (afternoon evidence checks), the same-day prior-allocation context from the morning-live, midday, manual missed-morning recovery, and manual override roots, and the historical / trend files, passing `RUN_DATE_ET=YYYYMMDD` when required and never reusing stale files (Finance primary, Alpaca/Robinhood validation-context, `yfinance` fallback only; one bounded recovery attempt then stop). It **always** writes `run_state.json`, `run_summary.json`, and `latest_scheduled_run.json` even when blocked, a no-op, or on error; on a block it writes a valid scheduled `run_state` with `run_origin='scheduled'`, `manual_run=false`, `stream='afternoon'`, `scheduled_for_et='15:30'`, `cron_id='10ce362c'`, `supersedes_cron_id='ce16ba6b'`, `state_schema_version='run-classification-v1'`, `orders_submitted_count=0`, a concrete `order_submit_status`, an `error_class` such as `missing_or_invalid_raw_inputs` or `no_incremental_top_up`, and a compact list of missing files / block reasons / failed sources, proof the task **fired**, not order/fill evidence. A post-raw failure still writes `signals.csv` when possible, an empty `submitted_orders.csv` with headers when no orders, and `run_state.json` (no half-done run). Manual recovery artifacts (missed-morning and manual 12:30) stay prior-allocation / top-up context only and are never afternoon scheduled evidence. Machine-readable source: `config/scheduled_tasks.json` → `live_schedule_hardening_2026_07_20.afternoon_1530` (mirrored in `config/run_classification.json`). See [run_classification.md](run_classification.md).

- **Cost-conscious model instruction (Jul 17 2026):** use a cost-conscious/default model where available; do not use ChatGPT 5.5 or premium models unless needed for failure recovery, safety validation, or connector/debug escalation. Keep daily output compact, write JSON/CSV artifacts, suppress no-op notifications, and leave heavy analysis to Friday weekly or monthly review. See the top-of-file cost-conscious model instruction note.

#### Final scheduled run — 2026-07-16 (afternoon scheduled, final buy of the day)
- **Scheduled** live run (not a recovery, not a manual override):
  `run_id='scheduled_20260716_1520ET'`, `scheduled_for_et='15:20'`,
  `run_origin='scheduled'`, `manual_run=false`, `cron_id='10ce362c'`,
  `supersedes_cron_id='ce16ba6b'`,
  `state_schema_version='run-classification-v2-preauthorized'`. This was the
  **final Robinhood scheduled buy run of the day**, using **Robinhood account
  `<ROBINHOOD_ACCOUNT_NUMBER>` as the only execution broker** (Alpaca/Finance/Yahoo read-only).
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

- **2026-07-27 3:30 PM ET: clean zero-order final run, and a watchdog false alarm.** The
  final run (`scheduled_20260727_1530ET`, `cron_id='10ce362c'`,
  `supersedes_cron_id='ce16ba6b'`) fired and submitted **zero orders**:
  `orders_submitted_count=0`, `order_submit_status='completed_no_orders'`,
  `completion_reason='no_orders_after_corrected_same_day_topup'`, with skip reasons
  `same_day_topup_not_lower` 23 and `not_below_prior_close_or_zero_multiplier` 21. The
  artifact is **internally consistent**: `run_state`, `order_submit_status`, and a
  header-only `submitted_orders.csv` with zero data rows all agree. `run_origin` on the
  nested `run_classification` is `scheduled`, and
  `same_day_pointer.derive_valid_scheduled_evidence()` returns `True`, so
  `valid_scheduled_evidence` and `valid_afternoon_scheduled_evidence` are both **derived**
  `true`. Artifacts under
  `/home/user/workspace/cron_tracking/ce16ba6b/scheduled_runs/20260727_1530ET/`.

  **The 3:40 watchdog nevertheless failed this run, and the failure was a scope bug rather
  than a trading or artifact defect.** The watchdog compared the run against
  `raw/final1530_rh_orders.json`, a **broad same-day broker snapshot** holding **40 earlier
  same-day orders**, and read their presence as unreconciled final-run submissions. Every
  one of those 40 orders was created **before** the final-run window opened. The snapshot
  spans `13:37:57Z` to `18:33:57Z`, and **zero** orders in it were created at or after
  `19:30Z`.

  | Orders | Created (ET) | Created (UTC) | Owning stream |
  |---|---|---|---|
  | 6 | 09:37 | 13:37Z | morning 9:35 scheduled run |
  | 27 | 11:36 to 11:37 | 15:36Z to 15:37Z | intraday 11:30 scheduled run |
  | 6 | 13:07 | 17:07Z | midday 12:30 **scheduled recovery**, which ran at 1:05 PM ET |
  | 1 | 14:33 | 18:33Z | intraday 2:30 recovery, the EVR $1.00 top-up |

  Every one of the 40 is already claimed by an earlier stream artifact, so none of them is
  final-run evidence. **One attribution correction is worth stating plainly:** the six
  orders created at 1:07 PM ET belong to the **12:30 midday scheduled recovery** that ran
  at 1:05 PM ET, and their order IDs match that recovery's recorded fills (AMZN, DXYZ,
  GFUZ, GS, SPCX, FLKR). They are **not** 1:30 orders. The 1:30 intraday run submitted
  **zero** orders that day; it was the safe no-order skip recorded in the `7be82b97`
  section above. Any reconciliation that files the 1:07 PM ET block under the 1:30 slot is
  misattributing it.

  Correct classification is **`same_day_prior_context_present`**, and the correct outcome
  is **pass**. The reconciliation scope rule that prevents a repeat is recorded as the
  **Jul 27 2026 watchdog reconciliation scope patch** in the `67aa802b` and `a46bf007`
  sections below. Machine-readable sources:
  `config/scheduled_tasks.json` -> `final_run_reconciliation_scope_2026_07_27` and
  `scheduled_run_outcomes.runs`, and `config/run_classification.json` ->
  `watchdog_reconciliation_scope_patch_2026_07_27`.

### `d72aa525`: Capital-priority briefing (RETIRED 2026-07-27, NOT ACTIVE)
> **RETIRED 2026-07-27, NOT ACTIVE.** Not in the canonical live schedule verified 2026-07-27. It has no
> `cron_utc` and must not appear in any active schedule table.
- Analytics only, **no trading**. Runs **3:25 PM ET weekdays**.
- Uses the current $2 base 0x-to-5x ladder. Sends **email/in-app notification only if at least one ticker triggers 1.5x or higher**; otherwise silent.

### `67aa802b`: Post-trade slippage + compact Notion latest-run sync
- Analytics only, **no trading**. Runs **3:35 PM ET weekdays**.
- Reads the **scheduled** live-run state and Robinhood order history, writes slippage/coverage artifacts, updates the compact Notion **Latest Run** section only.
- **Run classification:** scheduled-live rows exclude all manual order IDs and any `run_origin='manual'`/`manual_run=true` state; manual fills go only in the labeled **Manual Test Runs** section. See [run_classification.md](run_classification.md).
- **Manual exclusion roots + final override (added Jul 13 2026):** collect and exclude manual order IDs from all manual exclusion roots — the task's own `manual_runs/**` **plus** `final_manual_review/**`, `.../cron_tracking/ce16ba6b/manual_runs/final_manual_override_*`, and `.../cron_tracking/midday_dca/manual_reruns/`. Fills from a `final_manual_buy_override` run (e.g. the 2026-07-13 incident) are excluded from scheduled slippage/fills/reliability/cost basis and reported only in the labeled **"Manual Final Buy Override / Same-Day Top-Up Context"** section (`manual_final_buy_override` in [`schemas/slippage_report.schema.json`](../schemas/slippage_report.schema.json)).
- **Confirmation-blocked run:** when the scheduled state has `order_submit_status='not_submitted_confirmation_required'`, report **no scheduled fills and no scheduled order IDs**; do **not** invalidate the run_state and do **not** apply the broker time-window fallback (no write failure occurred).
- **Schema-version acceptance (added Jul 10 2026):** accept scheduled run_states tagged **either** `state_schema_version='run-classification-v1'` **or** `run-classification-v2-preauthorized` as long as the required scheduled identity fields are correct (`run_origin='scheduled'`, `manual_run=false`, `cron_id='10ce362c'` OR `supersedes_cron_id='ce16ba6b'`, `scheduled_for_et='15:30'`, window, `run_id`). v2 is a **stricter successor** of v1 (pre-authorized direct placement), not a different contract.
- **Order-history payload persistence (added Jul 10 2026):** persist the Robinhood order-history payloads used for slippage (e.g. `scheduled_runs/YYYYMMDD_1530ET/raw/orders_after_place.json`) **before** slippage computation, and compute average fill price / executions from the **saved payloads** (`dca_reporting.fills_from_order_history_payload`) rather than re-querying the broker. Record the payload paths in `source_order_history_payloads` (see [`schemas/slippage_report.schema.json`](../schemas/slippage_report.schema.json)).
- **Final-run reconciliation scope patch (added Jul 27 2026):** slippage reconciliation for the 3:30 PM ET final run uses **only final-run evidence**, defined exactly as in the `a46bf007` watchdog section below: `order_id` or `ref_id` in the final run's own `submitted_orders.csv`, a final-run `ref_id` prefix when stamped, or orders created **inside the closed 19:30Z–19:35Z window** (during EDT; 20:30Z–20:35Z under EST) that are not already claimed by an earlier stream artifact. **18:30Z must not be used as the lower bound, and the window must be bounded at both ends** — a lower bound alone would attribute an unrelated 16:00 ET order to the final run. A broad same-day raw broker snapshot is `broker_context_all_same_day`, not `final_run_submissions`, and contributes **no scheduled slippage fills** for the final run. A zero-order final run contributes zero scheduled slippage fills, and the 2:33 PM ET EVR recovery order remains same-day top-up context only, never scheduled slippage evidence. See `final_run_reconciliation_scope_2026_07_27` in [`config/scheduled_tasks.json`](../config/scheduled_tasks.json).
- **Foreground primary surrogate context patch (added Jul 30 2026):** if any same-day foreground/manual recovery artifact, `run_state.json`, `run_summary.json`, latest pointer, or manifest entry carries one of the recognised markers (`foreground_primary_surrogate_context=true`, the bare marker string `foreground_primary_surrogate_context`, `run_origin='foreground_primary_surrogate'`, `run_origin='manual_foreground_primary'`, or `valid_primary_executor_evidence=true`), treat that as intentional **foreground primary mode** for the current trading day. In foreground primary mode the 3:35 PM ET post-trade validator **must not escalate a missing or invalid background scheduled evidence alert** solely because the scheduled run artifact is absent, incomplete, or `valid_scheduled_evidence=false`. Instead report `foreground_primary_mode_active=true`, list the foreground artifact paths used, and classify those fills/orders as `foreground_primary_surrogate_context`. **Evidence separation is absolute:** those fills stay `valid_scheduled_evidence=false`, `valid_afternoon_scheduled_evidence=false`, and `top_up_context_only=true`. They are execution context for user-approved foreground primary operation, and they never prove that background scheduled execution ran, so they are never scheduled slippage, scheduled fill-count, scheduled reliability, or scheduled cost-basis evidence. **A marker counts only when the artifact carrying it declares a `trading_day` equal to the current ET trading day**; prior-day markers, and markers on artifacts that declare no trading day, are rejected, because otherwise a single foreground day would silence the missing-scheduled-evidence alert permanently (the same failure mode the same-day pointer contract exists to prevent). **Matching is exact equality, never substring:** a marker named inside a prose note is documentation, not a declaration, and the boolean spellings require literal `true`, so the string `"false"` and the integer `0` are not declarations. **The suppression is narrow.** Safety failures, broker/order mismatches, missing required foreground artifacts, stale same-day pointers, invalid schema, drift where a foreground signal says an order was placed but no broker or artifact record exists, and invalid background scheduled artifacts when foreground primary mode is **not** active all continue to escalate: the mode explains an absent background run, it does not excuse a failure. No conflict with the Jul 27 final-run reconciliation scope patch above: a foreground artifact is never final-run evidence and never scheduled slippage evidence, and the 19:30Z to 19:35Z EDT window is untouched. In code, `dca_reporting.generate_scheduled_reports` writes `foreground_primary_context.json` instead of raising when a caller supplies a marked same-day foreground artifact through its `foreground_primary_artifacts` argument and nothing else is escalating; a caller that supplies none raises exactly as before, so loading config alone can never activate the mode. Implemented in [`scripts/foreground_primary.py`](../scripts/foreground_primary.py) (`detect_foreground_primary_context`, `evaluate_scheduled_evidence_alert`). See `foreground_primary_and_close_window_patch_2026_07_30` in [`config/scheduled_tasks.json`](../config/scheduled_tasks.json) and `foreground_primary_surrogate_context_2026_07_30` in [`config/run_classification.json`](../config/run_classification.json).
- **Close-window signal comparison patch (added Jul 30 2026):** during the 3:35 PM ET post-trade validator pass, compute a **read-only `what_would_have_fired` summary** for the active watchlist. Use the current market-close decision price versus the **prior completed regular-session close** (never an in-progress session), grade the drop through the configured sizing ladder, and report which tickers would have fired in **background scheduled mode** and in **foreground primary mode**. Rows carry `ticker`, `decision_price`, `prior_close`, `drop_pct`, `ladder_multiplier_capped_at_3x`, `background_mode_status`, `foreground_mode_status`, `reason_if_not_fired`, and `data_source`. Market data follows the existing provider policy unchanged (Finance connector primary, Alpaca and Robinhood validation/context, yfinance fallback only when needed); the summary consumes those same reads and adds no provider. **Reporting caps the ladder at 3x:** the live ladder in [`config/sizing_ladder.json`](../config/sizing_ladder.json) runs to 5x, so a drop graded above 3x is reported as `3.0` with `ladder_multiplier_label='3x_plus'` to keep the cap visible. That cap is **reporting only**, execution sizing still uses the full 0x-to-5x ladder via `near_close_dca.graded_buy_multiplier`. A watchlist ticker whose quote failed is still reported, as an `unknown_missing_data` row, because a silently dropped ticker is indistinguishable from one that had no signal. **This check is analytics only and must not place, review, cancel, sell, or modify any order**; no budget is consumed and no order path is exercised. When `foreground_primary_surrogate_context` is active, state that foreground primary mode is active and compare the foreground execution context against this watchlist signal summary rather than treating missing scheduled evidence as a failure. Implemented in [`scripts/close_window_signal.py`](../scripts/close_window_signal.py) (`build_close_window_summary`), which performs no network I/O and writes nothing: the caller fetches quotes under the provider policy and passes `decision_price`, `prior_close`, and `data_source` in, so the summary cannot become a second, drifting copy of that policy. See `foreground_primary_and_close_window_patch_2026_07_30` in [`config/scheduled_tasks.json`](../config/scheduled_tasks.json) and `foreground_primary_surrogate_context_2026_07_30` in [`config/run_classification.json`](../config/run_classification.json).

- **Cost-conscious model instruction (Jul 17 2026):** use a cost-conscious/default model where available; do not use ChatGPT 5.5 or premium models unless needed for failure recovery, safety validation, or connector/debug escalation. Keep daily output compact, write JSON/CSV artifacts, suppress no-op notifications, and leave heavy analysis to Friday weekly or monthly review. See the top-of-file cost-conscious model instruction note.

### `a46bf007`: Live-run watchdog
- Read-only, **no trading**. Runs **3:40 PM ET weekdays** — the **final scheduled afternoon event**.
- Alerts only on missed, incomplete, error, or discrepancy conditions.
- **Run classification:** validates that a scheduled run_state exists and `latest_scheduled_run.json` points at it; a manual-only or broker-orders-only day is a discrepancy, not a scheduled run.
- **Confirmation-blocked run:** when `order_submit_status='not_submitted_confirmation_required'`, alert that **no order was placed**, but do **not** mark the lineage/scheduled state invalid — a confirmation-blocked run with the required fields present is a valid scheduled run that fired.
- **Watchdog scan fix (added Jul 13 2026):** when broker order history shows orders after 3:15 PM ET but no scheduled state accounts for them, scan the manual exclusion roots — `final_manual_review/**`, `.../cron_tracking/ce16ba6b/manual_runs/final_manual_override_*`, and `.../cron_tracking/midday_dca/manual_reruns/` — before flagging a discrepancy. Orders matched to a `final_manual_buy_override` run are manual overrides (`valid_afternoon_scheduled_evidence=false`): exclude them from scheduled-confirmation and do **not** treat them as scheduled evidence. The 3:40 PM 2026-07-13 alert was caused by final-override fills lacking artifacts under the scanned paths; artifacts were backfilled under the two `final_manual_override_20260713_1536ET/` roots. See [run_classification.md](run_classification.md).
- **All-orders broker query (added Jul 21 2026):** the watchdog **must query ALL same-day Robinhood equity orders for account <ROBINHOOD_ACCOUNT_NUMBER_OBFUSCATED> without any `placed_agent` filter first**, then classify every returned order by scheduled evidence, manual artifacts, same-day top-up roots, `placed_agent`, and order IDs. It **may** report the agentic-only (`placed_agent='agent'`) subset separately, but the **primary reconciliation runs over the full order set**. Orders with `placed_agent='user'` or any non-agentic value are surfaced in a dedicated **Manual/User Orders** section and **do not** count as scheduled fills unless their order IDs appear in a valid scheduled `submitted_orders.csv` from the same-day scheduled run. Manual fills stay excluded from scheduled slippage, scheduled fill counts, scheduled reliability, scheduled cost-basis evidence, watchdog proof, and scheduled afternoon evidence. See the `watchdog_all_orders_fix_2026_07_21` block in [`config/scheduled_tasks.json`](../config/scheduled_tasks.json) and [`config/run_classification.json`](../config/run_classification.json), and [run_classification.md](run_classification.md).
- **Final-run reconciliation scope patch (added Jul 27 2026):** final-run broker reconciliation must compare **only final-run evidence**. An order counts as final-run evidence only if its `order_id` or `ref_id` appears in the final run's own `submitted_orders.csv`, or it carries the final-run `ref_id` prefix when one is stamped, or it was created **inside the true final-run scheduled UTC window, at or after 19:30Z and at or before 19:35Z** (during EDT) **and** is not already claimed by an earlier stream artifact. Check artifact-linked evidence first; the time-window test is the last resort and is valid only after the earlier-stream-claim exclusion has been applied. A broad same-day raw broker snapshot is **`broker_context_all_same_day`, not `final_run_submissions`**, and a raw filename such as `raw/final1530_rh_orders.json` records only when the snapshot was taken, never which stream placed the orders inside it. **18:30Z must not be used as the final-run lower bound**, because 18:30Z is 2:30 PM ET, a different slot, and using it sweeps the afternoon intraday window (including the 2:33 PM ET EVR recovery order) into the final run. **The window is closed at both ends.** A lower-bound-only rule qualifies any later same-day order not already claimed, so an unrelated or manual 16:00 ET order would be swept into the final run; `scripts/run_classification.py:_in_scheduled_window_et` already enforces a closed window in code, so an unbounded rule here would make this manifest looser than the implementation it drives. Both UTC instants are DST-dependent (19:30Z–19:35Z during EDT, 20:30Z–20:35Z during EST) and must be derived from the 15:30 and 15:35 ET slot boundaries rather than hardcoded. **If `submitted_orders.csv` is empty and `run_state` says `completed_no_orders`, earlier same-day orders in raw broker context must not fail the watchdog:** classify as **`same_day_prior_context_present`** and **pass**, unless matching final-run order evidence contradicts the artifact. This does **not** weaken the Jul 21 all-orders rule above: the watchdog still queries every same-day order with no `placed_agent` filter and still classifies each one. That rule widened what the watchdog must look at; this one narrows what may be counted as a final-run submission. See `final_run_reconciliation_scope_2026_07_27` in [`config/scheduled_tasks.json`](../config/scheduled_tasks.json) and `watchdog_reconciliation_scope_patch_2026_07_27` in [`config/run_classification.json`](../config/run_classification.json).
- **Foreground primary surrogate context patch (added Jul 30 2026):** if any same-day foreground/manual recovery artifact, `run_state.json`, `run_summary.json`, latest pointer, or manifest entry carries one of the recognised markers (`foreground_primary_surrogate_context=true`, the bare marker string `foreground_primary_surrogate_context`, `run_origin='foreground_primary_surrogate'`, `run_origin='manual_foreground_primary'`, or `valid_primary_executor_evidence=true`), treat that as intentional **foreground primary mode** for the current trading day. In foreground primary mode the 3:40 PM ET watchdog **must not escalate a missing or invalid background scheduled evidence alert** solely because the scheduled run artifact is absent, incomplete, or `valid_scheduled_evidence=false`. Instead report `foreground_primary_mode_active=true`, list the foreground artifact paths used, and classify those fills/orders as `foreground_primary_surrogate_context`. **Evidence separation is absolute:** those fills stay `valid_scheduled_evidence=false`, `valid_afternoon_scheduled_evidence=false`, and `top_up_context_only=true`. They are execution context for user-approved foreground primary operation, and they never prove that background scheduled execution ran, so they are never scheduled slippage, scheduled fill-count, scheduled reliability, or scheduled cost-basis evidence. **A marker counts only when the artifact carrying it declares a `trading_day` equal to the current ET trading day**; prior-day markers, and markers on artifacts that declare no trading day, are rejected, because otherwise a single foreground day would silence the missing-scheduled-evidence alert permanently (the same failure mode the same-day pointer contract exists to prevent). **Matching is exact equality, never substring:** a marker named inside a prose note is documentation, not a declaration, and the boolean spellings require literal `true`, so the string `"false"` and the integer `0` are not declarations. **The suppression is narrow.** Safety failures, broker/order mismatches, missing required foreground artifacts, stale same-day pointers, invalid schema, drift where a foreground signal says an order was placed but no broker or artifact record exists, and invalid background scheduled artifacts when foreground primary mode is **not** active all continue to escalate: the mode explains an absent background run, it does not excuse a failure. No conflict with the Jul 21 all-orders rule or the Jul 27 final-run reconciliation scope patch above: the watchdog still queries ALL same-day orders with no `placed_agent` filter and still classifies every returned order, a foreground artifact is never final-run evidence, and the 19:30Z to 19:35Z EDT window is untouched. This patch changes only whether an **absent** background scheduled artifact escalates. Like the rest of `a46bf007`'s contract this is documentation the external scheduler must consume: the watchdog has no in-repo executor, so the repo publishes the shared helper and the contract but cannot push either to the live task itself. Implemented in [`scripts/foreground_primary.py`](../scripts/foreground_primary.py) (`detect_foreground_primary_context`, `evaluate_scheduled_evidence_alert`). See `foreground_primary_and_close_window_patch_2026_07_30` in [`config/scheduled_tasks.json`](../config/scheduled_tasks.json) and `foreground_primary_surrogate_context_2026_07_30` in [`config/run_classification.json`](../config/run_classification.json).
- **Close-window signal comparison patch (added Jul 30 2026):** during the 3:40 PM ET watchdog's close-of-day validator pass, compute a **read-only `what_would_have_fired` summary** for the active watchlist. Use the current market-close decision price versus the **prior completed regular-session close** (never an in-progress session), grade the drop through the configured sizing ladder, and report which tickers would have fired in **background scheduled mode** and in **foreground primary mode**. Rows carry `ticker`, `decision_price`, `prior_close`, `drop_pct`, `ladder_multiplier_capped_at_3x`, `background_mode_status`, `foreground_mode_status`, `reason_if_not_fired`, and `data_source`. Market data follows the existing provider policy unchanged (Finance connector primary, Alpaca and Robinhood validation/context, yfinance fallback only when needed); the summary consumes those same reads and adds no provider. **Reporting caps the ladder at 3x:** the live ladder in [`config/sizing_ladder.json`](../config/sizing_ladder.json) runs to 5x, so a drop graded above 3x is reported as `3.0` with `ladder_multiplier_label='3x_plus'` to keep the cap visible. That cap is **reporting only**, execution sizing still uses the full 0x-to-5x ladder via `near_close_dca.graded_buy_multiplier`. A watchlist ticker whose quote failed is still reported, as an `unknown_missing_data` row, because a silently dropped ticker is indistinguishable from one that had no signal. **This check is analytics only and must not place, review, cancel, sell, or modify any order**; no budget is consumed and no order path is exercised. When `foreground_primary_surrogate_context` is active, state that foreground primary mode is active and compare the foreground execution context against this watchlist signal summary rather than treating missing scheduled evidence as a failure. Implemented in [`scripts/close_window_signal.py`](../scripts/close_window_signal.py) (`build_close_window_summary`), which performs no network I/O and writes nothing: the caller fetches quotes under the provider policy and passes `decision_price`, `prior_close`, and `data_source` in, so the summary cannot become a second, drifting copy of that policy. See `foreground_primary_and_close_window_patch_2026_07_30` in [`config/scheduled_tasks.json`](../config/scheduled_tasks.json) and `foreground_primary_surrogate_context_2026_07_30` in [`config/run_classification.json`](../config/run_classification.json).

- **Cost-conscious model instruction (Jul 17 2026):** use a cost-conscious/default model where available; do not use ChatGPT 5.5 or premium models unless needed for failure recovery, safety validation, or connector/debug escalation. Keep daily output compact, write JSON/CSV artifacts, suppress no-op notifications, and leave heavy analysis to Friday weekly or monthly review. See the top-of-file cost-conscious model instruction note.
- **Budget-cap contract (corrected Jul 30 2026):** the watchdog verifies the **currently-live five-task chain** (`2ba19526`/`42173154`/`7be82b97`/`a6239dae`/`10ce362c`), not the seven-slot Claude roster — that roster places no orders yet (`GATE5_DISABLED`, see [safeguards.md](safeguards.md#live-execution-roster)). Per `config/scheduled_tasks.json`'s `circuit_breakers.budget_caps`, the caps to verify today are: market-open (`2ba19526`), morning (`42173154`), and midday (`a6239dae`) each against their own **$90/day, $450/week** ledger; the three intraday slots (`7be82b97`, 11:30/1:30/2:30) against **one shared $90/day, $450/week** ledger across all three combined, not $90 each; and only the afternoon final run (`10ce362c`) against `config/budgets.json`'s tiered `budget_tiers` table — Normal $180/day, $900/week up to Stress $360/day, $1,800/week, selected per day solely by 3x-or-greater signal count (never planned spend). **Checking any of these weekly figures needs a separate, week-scoped query** — `a46bf007`'s own documented query contract (the `all_orders_reconciliation` block of its task definition, removed from `config/scheduled_tasks.json` with the task itself on 2026-08-02) only fetches "ALL same-day Robinhood equity orders," which is a today-only fetch; comparing that same-day total against a weekly cap (whether a per-stream $450/week ledger or the afternoon tier's $900/$1,200/$1,500/$1,800) silently omits Monday-through-yesterday spend. A watchdog check against any weekly cap requires its own fully-paginated query from the current ISO week's Monday 00:00 America/New_York (through now), classified by the same per-stream/manual rules as the same-day query, before that comparison means anything. **The afternoon run's own cap check is not against that tier cap in isolation**: per the "Final buy budget-tier fix (added Jul 17 2026)" block earlier in this file and `final_buy_budget_tier_fix` in `config/scheduled_tasks.json`, same-day strategy allocation already placed is counted as usage against the tier's *corrected daily cap* before any final-run order is evaluated. The 2026-07-17 config text for that rule names only manual recovery, morning, and midday, but the actual live code is broader: `run_dca.py`'s `_CROSS_STREAM_PRIOR_ALLOCATION_ROOTS` also folds in the market-open and intraday live roots, and `near_close_dca.py`'s `_seed_prior_allocations` feeds all of it into `day_notional()`/`week_notional()` for absolute-cap enforcement (Codex review, discussion_r3679391265: "Preserve prior allocations in cap bookkeeping"). A watchdog that checks only `10ce362c`'s own submitted total, or that follows only the narrower manual-recovery/morning/midday list, against the nominal $180/$240/$300/$360 figure can pass a final run whose own orders fit the tier while prior same-day allocation (including market-open and intraday) plus those orders together exceed it — enumerate all five same-day sources (manual recovery, market-open, morning, midday, intraday) as corrected-daily-cap usage. Even for the afternoon stream taken alone, **Stress's $1,800/week is nominal, not the effective ceiling**: per `docs/live_wiring.md` and `near_close_dca.py`'s `effective_weekly_cap()` (the live calculation, lines 738-750), the actual Stress ceiling is **`min($1,800, $900 + verified_rollover_reserve_usd)`** — the Normal weekly cap plus whatever of the current week's rollover reserve was actually earned, capped at the nominal $1,800 and at the reserve's own $900 ceiling (`config/budgets.json:weekly_rollover_reserve_cap_usd`). A $300 reserve therefore means a $1,200 effective ceiling, not $300 and not the full $1,800; a watchdog check against the bare nominal figure, or against the reserve amount alone, can pass a batch that exceeds this formula's real result or wrongly flag one that doesn't. None of this becomes a single shared $180/day, $900/week total across all seven slots until a human wires Gate 5 and retires these five old tasks (`docs/safeguards.md#live-execution-roster`) — that shared framing is the *target* state, not what the watchdog should check today. A flat **$200/day (or $1,200/week)** cap, wherever the watchdog's task text currently uses one, matches none of the above and is a stale figure from an earlier pasted task revision; repaste the watchdog's task text against this bullet (or directly against `config/scheduled_tasks.json`/`config/budgets.json`). This is a task-text sync gap, not a code bug: `a46bf007` has no in-repo executor — like `2ba19526`/`bb4f6ef1`/`7be82b97`, it is driven entirely by an external scheduler/agent against this manifest, per `config/scheduled_tasks.json`'s `watchdog_all_orders_fix_2026_07_21.implementation_status` ("The watchdog a46bf007 itself has no in-repo executor ... the repo cannot call the helper on the watchdog's behalf") — so this repo can document the correct contract but cannot push it to the live task itself. Unlike those three, `a46bf007` does not carry a `no_python_entry_point` flag: its dedicated task object (`config/scheduled_tasks.json`, `id: "a46bf007"`, `name: "Live-run watchdog"` — it has one, with its own `cron_utc`, `run_classification`, and `notes` fields) documents the same fact only in prose ("Read-only, no trading" / "has no in-repo executor" in the surrounding manifest text), not via that specific machine-readable field.

### `041d9d90`: Weekly Notion performance dashboard refresh
- Analytics only, **no trading**. Runs **Fridays 5:00 PM ET** — the only approved post-3:40 PM exception.
- Recalculates and refreshes the full Notion performance dashboard. No routine email.
- **Run classification:** weekly scheduled-live metrics use only validated scheduled runs with manual runs/order IDs excluded; manual fills, if shown, are confined to a labeled **Manual Test Runs** section. See [run_classification.md](run_classification.md).
- **Schema-version compatibility (added Jul 10 2026):** include **v2-preauthorized** scheduled runs (`state_schema_version='run-classification-v2-preauthorized'`) in weekly metrics whenever the required scheduled identity fields are present, alongside v1 runs.
- **Slippage fallback (added Jul 10 2026):** if per-day slippage artifacts are missing, compute from the persisted broker-history payloads (`scheduled_runs/YYYYMMDD_1530ET/raw/orders_after_place.json` via `dca_reporting.fills_from_order_history_payload`) **before** declaring slippage unavailable for that day.

#### Recovered slippage — 2026-07-10 (afternoon scheduled)
- **9 valid afternoon scheduled fills**, total **$31.50**.
- Average `slippage_pct` ≈ **0.1047%**; largest absolute slippage symbol **QBTS** ≈ **0.2089%**.
- **46 manual order IDs excluded** from the scheduled-live metrics.
- Artifacts under `/home/user/workspace/cron_tracking/ce16ba6b/slippage/20260710/`; fills computed from the scheduled run's raw `orders_after_place.json`.

### `0656657a`: Monthly graded DCA dashboard refresh
- Analytics only, **no trading**. First weekday of the month **8:45 AM ET**. No-op otherwise.
- **Monthly review scope (added Jul 17 2026):** the monthly review should include
  the **weekly-rollover intraday cap curve** analytics backtest and re-evaluate the
  user-highlighted **35 / 65 / 100** candidate against the alternatives. This is an
  **analytics-only, non-live** candidate: it never changes live schedules or
  execution logic. Machine-readable data:
  [`config/backtest_intraday_cap_curve.json`](../config/backtest_intraday_cap_curve.json);
  methodology and results: [backtest_curve.md](backtest_curve.md).

### `bec4a6ee`: Monthly DCA automation cost review
- Analytics only, **no trading**. First weekday of the month **9:55 AM ET**.
- Reports **usage proxies only**; exact Perplexity credit consumption is not exposed, so do not claim exact credits.

### `onetime-1630-credit-usage-proxy-2026-07-27`: One-time DCA credit-usage proxy report
- **Analytics and reporting only, no trading.** Fires once at **2026-07-27 4:30 PM ET** (America/New_York). No order review, no order submission, no cancellations, no sells, no broker or account modification.
- Summarizes today's DCA automation usage with **measurable proxy metrics and low/moderate/high bands**. Exact Perplexity credit totals are not exposed, so the report must never claim an exact credit count, an estimated credit count, or any dollar/billing figure.
- Covers the full active daily chain: `2ba19526`, `bb4f6ef1`, `42173154`, `7be82b97` (broken out per 11:30 / 1:30 / 2:30 slot), `a6239dae`, `10ce362c`, `67aa802b`, `a46bf007`. A task that did not fire is reported as `did_not_run`, never omitted.
- Reports every recovery artifact from the current trading day in a **separate recovery section** whose counts are never folded into the scheduled-run counts: same-day recoveries, manual recovery artifacts, manual reruns, scheduled recovery attempts, order-placement recovery artifacts, missed-run recovery artifacts, parent recoveries, subagent escalations, failed scheduled starts, retries, and repair artifacts. Counting only normal scheduled runs is a contract violation. Recovery counts are broken down **by stream**, and every recovery or manual artifact is labeled `recovery context only`.
- **Compact cost-conscious output.** Reads local artifacts only, makes no network, connector, or broker call of its own, and suppresses long narrative unless there is an actionable issue (failed start, any recovery artifact, connector error or retry, missing expected artifact, or a `high` band).
- **Separate from the Friday 5:00 PM ET weekly heavy reporting task `041d9d90`.** It does not replace, satisfy, or draw budget from it. 2026-07-27 is a Monday, so `041d9d90` does not run today.
- Emits **no secrets**: no raw account number, API key, token, webhook URL, or credential. Account numbers are masked to the last 4 digits; other secret classes are omitted entirely.
- Does not reintroduce the retired 9:00 AM pre-market buying-power dashboard `b7961407`, and neither expects a 9:00 AM run nor flags its absence.
- Per run it records `run_id`, `stream`, `triggering_issue`, `artifacts_written`, `order_attempts`, `connector_calls`, `model_usage_rows`, and a classification of `valid_scheduled_evidence`, `top_up_context_only`, `manual_context_only`, `failed_start_no_evidence` (an attempted scheduled start that failed before writing any artifact), or `did_not_run` (a scheduled slot that never fired at all).
- Writes no `run_state.json`, is **never** valid scheduled evidence, and never creates, updates, or invalidates `latest_scheduled_run.json` or any same-day pointer. It may read `latest_scheduled_run.json` and same-day pointers as a data source (read only, see `credit_usage_proxy_report.md`'s Data sources), but never writes to them.
- **Registered with the scheduler:** `cron_utc` `30 20 27 7 *` (20:30 UTC = 4:30 PM EDT on 2026-07-27) and an executable command now run [`scripts/generate_credit_usage_proxy_report.py`](../scripts/generate_credit_usage_proxy_report.py), a real script that reads persisted same-day artifacts and produces the report. As with every task's `cron_utc`, invoking it at the scheduled time is the external scheduler's responsibility.
- The id is **repo-internal**: no 8-hex scheduler id was supplied for this task. Full contract: [credit_usage_proxy_report.md](credit_usage_proxy_report.md), machine-readable source `credit_usage_proxy_report_2026_07_27`.

### `f7411889` — One-time reminder
- Reminder only, **no trading**. Fires **2026-08-15 11:45 AM ET** to evaluate continuing live testing past 2026-08-28. Stale as of the 2026-07-26 extension of `config/trading_window.json` to 2026-10-30 — this scheduler-side task needs to be rescheduled (or re-scoped) to match; it was not touched by that config change. A good target is mid-to-late October, ahead of the 2026-10-30 end date and the 2026-11-01 DST transition the window deliberately stops before.

### `c9fb1fb8` / `17a3b2a9` — Month-end DCA volatility risk table refresh
- **Analytics and reporting only — no trading.** No order reviews, no order submissions,
  no cancellations, no sells, no options, no crypto, no margin changes, and no
  broker/account modifications. Machine-readable source:
  `month_end_volatility_risk_policy` in
  [`config/scheduled_tasks.json`](../config/scheduled_tasks.json).
- **Paired EDT/EST schedules (final-eligible-market-day gate):** because
  programmatic month-end trigger checks are disabled, two cron schedules cover the
  two US Eastern DST states, and each run self-gates:
  - `c9fb1fb8` — `15 21 25-31 3-10 *` for **EDT** months (Mar–Oct), intended as
    **5:15 PM America/New_York on the final eligible market day of the month**
    (retimed 2026-08-02 from 5:00 PM ET to clear the collision with
    `67aa802b`'s new daily 5:00 PM slot; see `docs/safeguards.md#operational-rules`).
  - `17a3b2a9` — `15 22 25-31 1,2,11,12 *` for **EST** months (Jan, Feb, Nov, Dec),
    intended as **5:15 PM America/New_York on the final eligible market day of the
    month** (same 2026-08-02 retime as `c9fb1fb8`).
- **Silent gate:** every run must **first** determine whether the current
  America/New_York date is the final eligible market day of the month. If it is
  **not**, the run ends **silently** — no notification, no market-data pulls, no
  Notion updates, and no reporting.
- **Active universe (57 symbols):** DFTX, TRIN, AAAU, SPCX, EVR, LUNR, IRDM, DXYZ,
  LITE, ASML, DRAM, STX, MSFT, AAPL, AMAT, AMD, CBRS, MRVL, BTX, STM, SNDK, WDC,
  TSM, AVGO, NVDA, QBTS, FLKR, INTC, GOOGL, GOOG, AMZN, GLW, MA, GS, MU, NFLX, XLG,
  TOPT, QTOP, SATA, RGTI, IBM, GFUZ, TXN, RKLB, GE, VRT, CAT, QQQ, QCOM, ABBV, JPM,
  VT, QBUT, RVI, VCX, TRLV (GE through JPM added 2026-08-02; VT added 2026-08-04;
  QBUT, RVII, RVI, VCX, TRLV added 2026-08-16 — all five **without** a broker
  instrument search — treat them as unverified until their first live run
  resolves them; QBUT, RVII, RVI, VCX, and TRLV also fully reverse the
  2026-08-05 exclusion decision below; RVII then moved to the whole-share
  category 2026-08-24). **Excluded:** none. **Whole-share tickers:**
  BYDDY and RVII -- not tradable for fractional/notional buys on Robinhood, so they are
  evaluated by the same graded ladder but bought via whole-share accumulation instead; see
  [whole_share_accumulation.md](whole_share_accumulation.md).
  **Corrections:** IRDMI→IRDM, GLWR→GLW. IBM, GFUZ, TXN, RKLB, and the
  2026-08-02, 2026-08-04, and 2026-08-16 additions remain included unless later removed. Source of truth:
  [`config/tickers.json`](../config/tickers.json).
- **Data source policy:** Finance connector is **primary** for historical market
  data. **Alpaca and Robinhood are read-only** validation/context. **yfinance is a
  fallback only** when connector data is unavailable, stale, incomplete, or
  unresolved, and fallback rows must be labeled `source=yfinance_fallback`. **GFUZ**
  may use **Robinhood primary** when Finance cannot resolve it. Data-confidence
  rules must not be weakened.
- **Analysis rules (after the gate passes):**
  - Pull the **last 30 trading days** of daily price action.
  - Compute the **daily drop** from the prior completed regular-session close.
  - Count **3x risk-tier events** using the current risk-monitoring threshold for
    **3x and above**.
  - Surface any ticker with **more than two** 3x-or-higher events in the 30-day
    window.
  - Calculate **average drawdown recovery time** as trading days from the 3x event
    close until price recovers to **at least the prior close** used for the signal;
    mark **unrecovered** if not recovered by window end.
  - Refresh the existing **All Stock DCA Master Summary Dashboard** in Notion with a
    **Month-End Volatility Risk Table** section, preserving unrelated content.
- **Artifacts:** compact CSV/JSON under
  `/home/user/workspace/cron_tracking/month_end_volatility_risk/YYYYMM/` with fields
  `ticker`, `sector_or_bucket`, `three_x_event_count`, `max_drop_pct`,
  `average_drop_pct_on_3x_days`, `average_recovery_days`, `unrecovered_event_count`,
  `latest_event_date`, `risk_flag`, `data_source`.
- **Notifications:** notify **in-app and email only** if the Notion refresh fails,
  data quality blocks the run, or one or more tickers triggered the 3x tier more
  than twice. Email subject: **`Month-end DCA volatility risk table`**. **No Slack.**
  See [`config/notifications.json`](../config/notifications.json) →
  `routing.month_end_volatility_risk`.
- **Operating mode:** the daily cost-saving compact operating mode stays intact.
  **Full monthly volatility risk reporting belongs to this month-end task, not to
  daily execution tasks.**
