# Live Task Reliability Update — 2026-07-23

This update records the DCA live-execution contract fixes applied on 2026-07-23
and supersedes the schedule/stream assumptions in the
[2026-06-29 reliability update](../2026-06-29-execution-audit/live_task_reliability_update_2026-06-29.md).
The single-task 3:45 PM ET model (`ce16ba6b`) has been replaced by four
Robinhood-only scheduled live streams plus a read-only monitor.

## Scheduled stream manifest

| Stream | ET fire time | Cron id | Role | Stream root (artifacts) |
|---|---|---|---|---|
| Preflight | 9:45 AM | `morning-dca-0945` | Read-only same-day validation, no orders | `cron_tracking/morning_dca_preflight/YYYYMMDD_0945ET/` |
| Morning live | 10:30 AM | `42173154` | Live buy execution | `cron_tracking/morning_dca/YYYYMMDD_1030ET/` |
| Midday live | 12:30 PM | `a6239dae` | Live buy execution | `cron_tracking/midday_dca/YYYYMMDD_1230ET/` |
| Monitor | 3:05 PM | `e3e6f09e` | Read-only monitor, no trading | `cron_tracking/dca_monitor_1505/YYYYMMDD_1505ET/` |
| Final live | 3:20 PM | `10ce362c` (supersedes `ce16ba6b`) | Final live buy execution of the day | `cron_tracking/ce16ba6b/scheduled_runs/YYYYMMDD_1520ET/` |

Cron ids per `config/scheduled_tasks.json` →
`model_routing_policy.applies_to_daily_tasks_et`, which lists every stream in
this table. (`operating_mode.daily_compact_chain_et` covers the same chain
except the 3:05 monitor, which is not part of the compact chain.)

Common rules (unchanged): Robinhood is the only execution broker, trading only
the single configured live account (`config/scheduled_tasks.json` →
`execution_authorization_policy.broker_roles.robinhood.account`; the number is
not duplicated here per the AGENTS.md secrets policy); buy-only equities;
regular hours only; `$2` base amount with the
0x-to-5x graded ladder; one strategy order per ticker per stream per day; daily,
reserve, and weekly caps enforced. Routine notifications are in-app and email;
standing exception: a scheduled non-dry-run raw-persistence failure also sends
a Slack drift alert when the configured webhook is present (`run_dca.py`
raw-persistence failure path → `scripts/drift_detection.send_slack_alert`).

## Fixes applied 2026-07-23

### 1. Same-day pointer must be validated before it is trusted

Symptom: on 2026-07-23 the preflight `latest_preflight.json` pointer was stale,
pointing at the 2026-07-22 trading day, while no same-day 9:45 artifact existed
at first.

Contract:

- A runner must not trust any `latest_*` pointer unless BOTH `trading_day`
  matches the current America/New_York date AND `run_state_path` resolves under
  the expected stream root.
- When the pointer is stale or the same-day artifact is missing, the runner must
  prefer a date-specific same-day artifact, or perform a bounded same-day direct
  validation recovery, before blocking.
- A recovery pointer is marked `valid_scheduled_evidence=false` with the
  schema-valid non-trusted status `pointer_status="pending"`, so downstream
  consumers never count a recovery as scheduled evidence. The recovery
  provenance goes in an additional field the schema already permits, e.g.
  `recovery_reason="same_day_recovery_after_failed_start"`. Do NOT write that
  string into `pointer_status` itself: it is not in the
  `schemas/latest_pointer.schema.json` enum, and `scripts/same_day_pointer.py`
  normalizes an unknown declared status to `malformed`, which would make the
  recovery indistinguishable from a corrupted pointer.

Recovery evidence: a same-day recovery artifact was written at
`cron_tracking/morning_dca_preflight/20260723_0945ET/` and
`latest_preflight.json` was updated to `trading_day=2026-07-23` (ISO
`YYYY-MM-DD` — the format `scripts/same_day_pointer.py` and all tracked
writers compare; a compact `YYYYMMDD` value would fail the same-day check and
be rejected instead of counting as the pending recovery state),
`valid_scheduled_evidence=false`.

### 2. First allocation vs. top-up must not be conflated

Symptom: the top-up rule incorrectly blocked first allocations with reason
`no_prior_same_day_reference_no_auto_topup`.

Contract:

- **No prior same-day fill/order context for a ticker → first allocation.** Buy
  the full desired amount if it passes the normal signal, budget, spread,
  confidence, tradability, and regular-hours checks. Absence of a prior
  reference is NOT a block.
- **Prior same-day context exists → top-up only.** Buy `max(0, desired - prior_usd)`
  and only if the current decision price is strictly below the lowest prior
  same-day reference price. Otherwise skip.

### 3. Duplicate-submission guard before any fallback placement (pending in tracked runners)

Symptom: a background 11:55 recovery wrote planned orders and later submitted
them, while a foreground recovery started after initially seeing a missing
`submitted_orders.csv` — creating duplicate-submission risk.

Contract — before any direct recovery/fallback placement the runner must:

1. Re-read the same-day `submitted_orders.csv` AND the Robinhood same-day order
   history.
2. Dedupe by symbol, amount, and order ID.
3. Never place a fallback order when the saved/background runner has already
   submitted it, or is still in a pending placement phase.
4. If artifacts are incomplete, wait and reload once, then **fail closed** unless
   broker history proves no matching order was submitted.

Runtime status: this fallback gate is enforced operationally through the
scheduled/recovery task text — it is **PENDING in the tracked runner code**.
The tracked runners' only broker-side duplicate check is the single-shot
per-symbol same-day lookup (`RobinhoodBroker._same_day_orders` /
`same_day_strategy_order_exists`, `dca_live_adapters.py`), which fails closed
on lookup errors but makes one non-paginated request, never re-reads
`submitted_orders.csv`, and has no wait-and-reload step for incomplete
artifacts; no tracked fallback-placement routine implements steps 1-4. Until
the gate lands in tracked code, only the task-text procedure provides it, and
a code-only foreground recovery could still race a background runner's
in-flight placement.

### 4. Fractional dollar buys use market notional orders only

Contract: dollar-based scheduled buys must call `place_equity_order` with
`type='market'`, `dollar_amount=<usd>`, `time_in_force='gfd'`. Never use a
fractional limit quantity for a dollar-based scheduled buy.

Implementation note: the tracked adapter (`RobinhoodBroker.place_market_buy` in
`dca_live_adapters.py`) passes exactly `account_number`, `symbol`, `side='buy'`,
`type='market'`, `dollar_amount`, `time_in_force` (default `'gfd'`), and
`ref_id`; no tracked code passes or implements a `market_hours` option.
Regular-hours-only behavior currently comes from the scheduled fire times (all
inside the regular session) plus the broker's default handling of market/`gfd`
orders — extended-hours execution is never requested. Wiring an explicit
`market_hours='regular_hours'` argument through the adapter is PENDING and is
not claimed as implemented by this contract.

### 5. Non-null run classification and always-written order artifacts

Contract — scoped to scheduled LIVE runs that reached signal computation (10:30
morning, 12:30 midday, 3:20 final): every such run must write a non-null
`run_classification` object in BOTH `run_state.json` and `run_summary.json`, and
must always write `signals.csv` and `submitted_orders.csv` with header rows
(even when zero orders are submitted). A null classification or a missing order
CSV is an artifact-contract failure, not a soft warning. `run_classification`
must be an **object** (not a string), and `trading_day` must be non-null; a run
whose local artifact violated this was repaired in place (see the
[midday result](#midday-live-1230) below).

Standing exceptions (valid states, NOT contract failures):

- The read-only 9:45 preflight keeps a non-null `run_classification` in
  `run_state.json` but by design does not mirror it into `run_summary.json`:
  `scripts/scheduled_artifacts.py` enforces the summary mirror (and the
  `submitted_orders.csv` requirement) only for live runs.
- A scheduled live run that fails raw persistence before signal computation is
  validated with `require_signals=False` (`run_dca.py`): it writes the failure
  `run_state.json`, the mirrored `run_summary.json`, and a header-only
  `submitted_orders.csv`, but no `signals.csv`, because no signals were
  computed.

Runtime status: the 3:20 final runner satisfies this contract today
(`run_dca.py` writes and validates through `scripts/scheduled_artifacts.py`).
For the tracked 10:30 morning and 12:30 midday writers the contract is
**PENDING**: `MorningDCAExecutor.write_artifacts` (inherited by
`MiddayDCAExecutor`) names the signals CSV `morning_signals.csv` /
`midday_signals.csv` rather than `signals.csv`, and writes `run_summary.json`
only in the preflight branch — so a tracked live morning/midday run that
reached signal computation does not yet produce the literal `signals.csv`
name or a `run_summary.json` classification mirror. Consumers must not flag
those tracked-writer outputs as contract failures until the writers are
updated; for the morning/midday streams this section is the target contract,
enforced operationally through the scheduled task text.

### 6. JUL 23 2026 EFFECTIVE PRIOR ALLOCATION PATCH (pending in tracked runners)

Applied to the **10:30 morning**, **12:30 midday**, and **3:20 final** live task
text (the operational task prompts) — **PENDING in the tracked runner code; see
the runtime status below.** This refines fix #3: the duplicate guard must dedupe
against BOTH artifact context and same-day broker history, and the per-name
top-up math must use that combined figure.

Symptom: the 3:20 final task escalated because the duplicate guard saw same-day
Robinhood orders. Root cause: `order_plan.csv` carried artifact `prior_usd=0`
while the Robinhood same-day figure `prior_usd_rh` was populated and was being
ignored, so the plan looked like a fresh allocation that collided with the real
broker orders.

Contract:

- Compute `effective_prior_usd` from **deduped same-day artifacts PLUS Robinhood
  same-day order history**. Do not ignore `prior_usd_rh`.
- `corrected_delta_usd = max(0, desired_usd - effective_prior_usd)`.
- Skip names already met or overfilled by same-day orders
  (`already_met_or_overfilled_by_same_day_orders`).

Runtime status: the tracked runners do NOT implement this contract yet — no
tracked code computes `effective_prior_usd` or reads `prior_usd_rh`.
`run_dca.py` aggregates prior same-day allocations from artifact roots (plus
manual top-up context) only, and `near_close_dca.py` deliberately bypasses the
generic broker same-day-order check whenever an artifact prior allocation is
loaded for a ticker, so a same-day Robinhood fill missing from the artifacts
can still leave a positive top-up in those runners. Until the combined
artifact-plus-broker-history aggregation lands in the tracked runners, this
section is a pending contract enforced operationally through the scheduled
task text, not an implemented runtime safeguard.

### 7. Robinhood same-day order snapshot must be account-scoped and complete (pending in tracked adapter)

Applied to the **10:30 morning**, **12:30 midday**, **3:20 final**, **3:35
slippage**, and **3:40 watchdog** live task text (the operational task
prompts) — **PENDING in the tracked adapter code; see the runtime status
below.**

Symptom: the 3:40 watchdog noted that raw Robinhood same-day snapshots returned
**0 orders** for the default individual account and missed the 12:30 midday TXN
fill, even though the same-day `submitted_orders.csv` contained that order. A
snapshot that silently drops a real fill can cause the duplicate guard and the
effective-prior math (fix #6) to under-count same-day exposure.

Contract — every same-day Robinhood snapshot must:

- Explicitly query the configured live `account_number`
  (`config/scheduled_tasks.json` →
  `execution_authorization_policy.broker_roles.robinhood.account`) rather than
  relying on the default individual account.
- Page through **all** same-day equity orders first, without applying a
  `placed_agent` filter as the initial cut, then classify.
- Persist the raw paginated responses for audit.
- Perform one **bounded, account-specific `order_id` fallback** for each order ID
  present in `submitted_orders.csv` whenever the snapshot returns 0 orders or
  omits an order that an artifact records, so an artifact fill is never lost.

Runtime status: the tracked adapter does NOT implement this snapshot contract
yet. `RobinhoodBroker._same_day_orders` (`dca_live_adapters.py`) makes a
single per-symbol `orders_fn(account_number, symbol, created_at_gte)` call and
returns only that response's `orders` array — no cursor/page loop, no raw-page
persistence, and no `submitted_orders.csv`-driven per-`order_id` fallback — so
an incomplete or empty first history response can still under-count same-day
exposure there. Like fix #6, this section is a pending contract enforced
operationally through the scheduled task text until the paging and fallback
land in the tracked adapter.

## Scheduled evidence results — 2026-07-23

<a name="midday-live-1230"></a>
### Midday live (12:30, `a6239dae`) — filled

TXN `$1.00` placed and filled in the configured Robinhood account
(`run_id=midday_live_20260723_1230ET`), `status=filled`. The broker order id,
fill timestamp, and average fill price are operational identifiers and are
recorded only in the local gitignored artifacts under
`cron_tracking/midday_dca/20260723_1230ET/`, not in this report. The local
artifact initially stored `run_classification` as a **string** with a **null**
`trading_day`; it was repaired in place to a non-null `run_classification`
object with `trading_day=2026-07-23` and `valid_scheduled_evidence=true`
(`artifact_repair_applied_at_utc` recorded in `run_summary.json`).

### Monitor (3:05, `e3e6f09e`) — failed, backfilled, no trading

The monitor-only task failed with
`error_class=internal_start_error_before_artifacts` before writing any output.
Compact monitor-failure artifacts were backfilled under
`cron_tracking/dca_monitor_1505/20260723_1505ET/`: `monitor_state.json`
(`order_submit_status=failed_internal_start_error`,
`valid_scheduled_evidence=false`), `run_summary.json`, and header-only
`signals.csv` / `submitted_orders.csv`. Monitor-only — no orders were placed.

### Final live (3:20, `10ce362c`) — corrected, no orders needed

After applying the effective-prior-allocation patch above, the corrected top-up
plan required no orders (`run_id=scheduled_20260723_1520ET`):
`order_submit_status=no_orders_after_corrected_same_day_topup`,
`orders_submitted_count=0`, and `corrected_topup_summary.json` shows
`corrected_submit_count=0`, `corrected_total_usd=0`, and `blocked_count=28` all
with reason `already_met_or_overfilled_by_same_day_orders`. Repaired artifacts
written under `cron_tracking/ce16ba6b/scheduled_runs/20260723_1520ET/`:
`run_state.json`, `run_summary.json`, header-only `submitted_orders.csv`,
`corrected_topup_plan.csv`, `corrected_topup_summary.json`, and
`latest_scheduled_run.json`. The run is valid scheduled evidence
(`valid_scheduled_evidence=true`) that the task fired; it is not evidence of a
fill.

### Slippage (3:35) and watchdog (3:40) — afternoon validated, 0 scheduled fills

The 3:35 slippage check and the 3:40 watchdog confirmed the afternoon scheduled
run was **valid with 0 scheduled fills**. The 12:30 TXN fill counts as midday
scheduled activity / context, **not** afternoon scheduled evidence. The 3:40
watchdog also surfaced the Robinhood snapshot gap addressed by fix #7.

## Manual / recovery evidence policy

Manual and recovery artifacts are same-day top-up context only and carry
`valid_scheduled_evidence=false`; they must never be counted as scheduled
evidence. This includes today's rerun at
`cron_tracking/morning_dca/manual_recovery/20260723_1208ET_rerun`. See the
[recovery changelog](./recovery_changelog_2026-07-23.md) for the specific fills.
