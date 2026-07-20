# Live Task Reliability Update - 2026-07-17

On Fri Jul 17 2026 a scheduled live reliability fix was applied to the three
pre-authorized live trading tasks so the version-controlled manifest matches the
live scheduler state:

- `42173154`: 10:30 AM ET morning DCA live Robinhood execution, schedule
  `30 14 * * 1-5` UTC.
- `a6239dae`: 12:30 PM ET midday DCA live Robinhood execution, schedule
  `30 16 * * 1-5` UTC.
- `10ce362c`: 3:20 PM ET final live buy, schedule `20 19 * * 1-5` UTC.

The fix closes the execution-lineage gap where an actionable signal could end a
run with neither an `order_id` nor a recorded block or rejection, leaving no
persisted evidence of what happened.

## Reliability rules now in live task text

1. Before calling Robinhood `place_equity_order`, describe the Robinhood
   connector schemas needed in the current agent session, including
   `place_equity_order` and `get_equity_orders`.
2. Always write the final `run_state.json`, `run_summary.json`, `signals.csv`,
   `submitted_orders.csv`, and `filled_orders.csv` where applicable, even for
   no-order, blocked, failed, or recovery runs.
3. Every actionable signal must end with exactly one persisted result:
   `order_id`, broker rejection, top-up block, budget block, cap block,
   data-confidence block, spread block, trend block, or safety block.
4. If an actionable signal lacks both an `order_id` and a block or rejection
   reason, classify the run `execution_lineage_incomplete`, run one recovery
   retry in the same scheduled directory, retry only the missing tickers with
   the same `ref_id` values, and reconcile any duplicate `ref_id` or timeout
   through `get_equity_orders` before declaring failure.
5. Manual overrides stay `manual_run=true` and never count as scheduled
   evidence.

## Live 12:30 no-placeholder fix for midday task a6239dae

Applied Fri Jul 17 2026 on top of the reliability rules above, specific to the
midday task `a6239dae`:

1. The scheduled task must never write placeholder signals or empty
   `submitted_orders.csv` / `filled_orders.csv` while marking
   `valid_midday_evidence=true`.
2. If it cannot compute full signals, it must write `valid_midday_evidence=false`
   with concrete `blocking_errors` and notify.
3. It must parse Robinhood `get_equity_quotes` results from
   `data.results[].quote` and `data.results[].close`.
4. It must follow `output_path` pointers for large `get_equity_historicals`
   responses and load the referenced result JSON before computing trend.
5. A placeholder artifact with
   `blocking_errors=['persistence_placeholder_no_signal_compute']` is a failed
   run and must trigger recovery, not a completed scheduled run.

## Jul 17 2026 12:30 recovery result

- The original background task escalated after writing placeholder artifacts with
  `blocking_errors=['persistence_placeholder_no_signal_compute']`.
- Parent recovery persisted raw quote, history, tradability, portfolio, and
  order-history files under
  `/home/user/workspace/cron_tracking/midday_dca/20260717_1230ET/raw/`.
- The corrected nested Robinhood quote parser and `output_path` historical parser
  found exactly one eligible scheduled midday buy: TRIN $3.00.
- TRIN `order_id 6a5a5b53-6502-476d-b6d4-9cc73bab4f31`, state filled,
  `average_fill_price 17.736500`, `quantity 0.169142`,
  `created_at 2026-07-17T16:41:55.91467Z`.
- `valid_midday_evidence=true`, `valid_afternoon_evidence=false`. The Jul 17
  morning manual override remains manual context only (prior same-day allocation
  and top-up context).

## Final buy budget-tier fix for afternoon task 10ce362c

Applied live Fri Jul 17 2026, specific to the afternoon final-buy task
`10ce362c`. Before placing any final-buy order the run computes the budget tier
from the full actionable incremental plan and the count of 3x-or-greater
signals:

1. Normal: planned spend <= $129 and 3x-or-greater signals <= 5, corrected daily
   cap $180.
2. Elevated: planned spend > $129 or 3x-or-greater signals 6 to 10, corrected
   daily cap $240.
3. High-volatility: planned spend > $213 or 3x-or-greater signals 11 to 17,
   corrected daily cap $300.
4. Stress: planned spend > $272 or 3x-or-greater signals > 17, corrected daily
   cap $360 only to the extent unused weekly rollover reserve is available.

Additional rules:

- Count same-day strategy allocation already placed in the manual recovery,
  morning, midday, and earlier final orders as corrected-daily-cap usage for
  safety, while keeping the streams separate for evidence and slippage.
- If planned spend exceeds the remaining corrected daily capacity, rank the
  remaining eligible orders by deepest drop percentage, then multiplier, then
  amount, place only the orders that fit the remaining cap and the per-order $15
  max, and write `budget_blocked.csv` plus per-symbol
  `block_reason='budget_block_corrected_daily_cap'` for the rest.
- If a budget-logic inconsistency is detected after any order is placed, recover
  inside the same scheduled run directory, reconcile the already placed orders,
  apply the corrected cap logic, and finalize state rather than escalating
  without complete artifacts.

## Jul 17 2026 final-run recovery result

- The background final run placed SPCX first, then escalated because the budget
  logic was incomplete.
- The corrected tier was elevated because planned spend was 187.5 and the
  3x-or-greater count was 6, so the corrected daily cap was 240.
- Prior same-day strategy allocation counted for safety was 207.0 and SPCX
  assumed 15.0, leaving 18.0 remaining capacity.
- Recovery placed GFUZ $13.50 and ASML $4.50, then budget-blocked the remaining
  27 signals.
- Filled orders:
  - SPCX `order_id 6a5a8182-9460-4d0d-99c3-86af604192a7`, `average_fill_price
    123.600000`, `quantity 0.121359`.
  - GFUZ `order_id 6a5a81d4-d250-43e3-89f3-06c2c4add387`, `average_fill_price
    8.449900`, `quantity 1.597652`.
  - ASML `order_id 6a5a81d4-f921-49c3-9a1c-1f0e43c2002b`, `average_fill_price
    1747.709900`, `quantity 0.002574`.
- `run_state` has valid scheduled evidence for the final 3:20 run, and the
  manual/midday streams remain only prior allocation context where applicable.

## Jul 17 2026 nested run_classification requirement

The 3:20 PM scheduled `run_state` was repaired to include a nested
`run_classification` object. From this date on:

- Every scheduled `run_state.json` must contain a nested `run_classification`
  object (validated against `schemas/run_classification.schema.json`).
  Scheduled/manual identity fields (`run_origin`, `manual_run`, `cron_id`,
  `supersedes_cron_id`, `scheduled_for_et`, `run_id`) live only under that
  nested object; top-level mirror fields are not required by the artifact
  writers/consumers.
- The 3:40 PM watchdog (`a46bf007`) confirms a scheduled run only when the nested
  `run_classification` object is present and valid. A `run_state` missing the
  nested object is a watchdog discrepancy.

## Jul 17 2026 manual budget-block overrides

The 3:20 PM scheduled final buy run at
`/home/user/workspace/cron_tracking/ce16ba6b/scheduled_runs/20260717_1520ET`
placed and finalized SPCX, GFUZ, and ASML under the corrected elevated daily cap,
then budget-blocked 27 remaining signals under the corrected cap: NFLX, AMAT,
CBRS, DXYZ, TSM, IBM, EVR, GOOGL, GOOG, GS, NVDA, GLW, INTC, BTX, MA, AMD, MSFT,
QTOP, XLG, TOPT, SNDK, AVGO, AMZN, STM, MRVL, DFTX, QBTS.

Two manual budget-block override runs then filled all 27:

- First override artifact:
  `/home/user/workspace/cron_tracking/final_manual_review/budget_block_override_20260717_1551ET/run_state.json`.
  It attempted all 27 and accepted/submitted 20: NFLX, AMAT, CBRS, DXYZ, TSM,
  IBM, EVR, GOOGL, GOOG, GS, NVDA, GLW, INTC, BTX, MA, AMD, MSFT, QTOP, XLG,
  TOPT. The remaining 7 failed due to Robinhood connector 429 throttling /
  do-not-retry state.
- Second override artifact:
  `/home/user/workspace/cron_tracking/final_manual_review/budget_block_override_20260717_1556ET/run_state.json`.
  It placed and verified filled all remaining 7:
  - SNDK $4.50, `average_fill_price 1360.550000`, `quantity 0.003307`,
    `order_id 6a5a88c4-e4df-4b9d-a6d5-1549a77cea31`.
  - AVGO $4.50, `average_fill_price 372.385000`, `quantity 0.012084`,
    `order_id 6a5a88d5-9809-4159-b930-4649e3a377c6`.
  - AMZN $3.00, `average_fill_price 247.630000`, `quantity 0.012114`,
    `order_id 6a5a88e4-a818-4019-9291-7dbfa5d1c4ee`.
  - STM $3.00, `average_fill_price 62.439900`, `quantity 0.048046`,
    `order_id 6a5a88f5-090a-4712-b9f1-0955b63aa1d5`.
  - MRVL $3.00, `average_fill_price 189.359900`, `quantity 0.015842`,
    `order_id 6a5a8904-df24-4817-aa98-61e7bda8de7f`.
  - DFTX $3.00, `average_fill_price 44.509900`, `quantity 0.067400`,
    `order_id 6a5a891b-daf3-443d-84f3-19f11fbcfada`.
  - QBTS $3.00, `average_fill_price 16.749900`, `quantity 0.179105`,
    `order_id 6a5a892a-7df2-4e90-a373-fce4ef28f4a5`.

Classification: `manual_run=true`, `valid_scheduled_evidence=false`,
`valid_afternoon_evidence=false`. Excluded from the scheduled 3:20 execution
evidence, scheduled slippage, scheduled reliability, and scheduled cost-basis
metrics. Usable only as manual override context and future same-day / top-up
allocation context. Both artifacts live under the `final_manual_review/` manual
exclusion root, so their order IDs are collected as `excluded_manual_order_ids`.

## Jul 17 2026 manual recovery artifact

- Manual override directory:
  `/home/user/workspace/cron_tracking/morning_dca/manual_overrides/20260717_1042ET`.
- Source scheduled run: `morning_live_20260717_1030ET`.
- Requested orders: 33. Submitted response count: 33. Filled count: 33 after
  broker-history reconciliation.
- The MRVL duplicate `ref_id` was reconciled through `get_equity_orders`.
- Classification: manual recovery only. `valid_morning_evidence=false`,
  `valid_midday_evidence=false`, `valid_afternoon_evidence=false`.
- Excluded from all scheduled evidence, slippage, reliability, fill counts,
  watchdog confirmation, and cost basis. Later scheduled runs may use these
  fills only as same-day prior allocation and top-up context.

## Universe details preserved

- GFUZ and EVR are active and on the buy list.
- BYDDY, VCX, TRLV, and RVI remain excluded.
- IRDMI to IRDM and GLWR to GLW corrections remain in place; IBM is included.

## Safety constraints unchanged

- Robinhood account `562057042` only; Alpaca, Finance, and Yahoo read-only.
- Buy-only, regular-hours-only market orders. No sells, options, crypto,
  margin, account changes, or cancellations.
- No more than one strategy order per ticker per stream.
- Shared $3 base 0x-to-5x ladder with a per-order ceiling of 5x times $3 = $15.

Machine-readable source of truth:
[`config/scheduled_tasks.json`](../../config/scheduled_tasks.json)
(`live_reliability_fix_2026_07_17` block). Human-readable backup:
[`docs/schedule_manifest.md`](../../docs/schedule_manifest.md).
