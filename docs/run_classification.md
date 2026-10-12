# Run Classification Contract

> **Roster reconciliation (updated 2026-10-04):** the seven-slot roster (09:35,
> 10:30, 11:30, 12:30, 13:30, 14:30, 15:30) is the current framing; the older
> three-stream wording (morning `42173154`, midday `a6239dae`, afternoon
> `10ce362c`) is historical. What each slot's in-repo executor actually does is set
> by its config, not by this page: `config/market_open_dca.json`,
> `config/morning_dca.json`, `config/midday_dca.json`, and
> `config/intraday_dca.json` currently ship `live_execution_enabled: true`.
> This records executor capability when invoked, not proof that an external scheduler
> fired or placed an order. The 15:30 final buy runs under `run_dca.py` / `10ce362c`.
> The scheduled-vs-manual contract mechanics below (paths, required
> fields, exclusion rules) apply across every stream regardless. See
> [`safeguards.md`](safeguards.md#live-execution-roster) for the roster.

Prevents **manual runs** of the graded DCA task from being confused with
**scheduled live runs**. This is a binding contract: every run_state artifact
must declare its origin, and every downstream reporting task must honor it.

- Machine-readable policy: [`../config/run_classification.json`](../config/run_classification.json)
- Schema: [`../schemas/run_classification.schema.json`](../schemas/run_classification.schema.json)
- Enforcement helper: [`../scripts/run_classification.py`](../scripts/run_classification.py)
- Embedded in: [`../schemas/run_state.schema.json`](../schemas/run_state.schema.json)
  (the `run_classification` block is **required** on every run_state)

`state_schema_version` for this contract is **`run-classification-v1`**. As of
2026-07-10 a stricter successor **`run-classification-v2-preauthorized`** is also
accepted (see [Accepted schema versions](#accepted-schema-versions-v1--v2)):
downstream consumers accept **both** versions when the required scheduled
identity fields are correct.

## Stream Artifact Roots and Latest Pointers

Each currently-functioning stream writes under its **own** artifact root and updates its **own**
pointer -- there is no shared `scheduled_runs/` path for most of them:

| Stream | Nominal ET Slots | Cron ID | Artifact Root | Latest Pointer |
|---|---|---|---|---|
| `market_open` | 09:35 | `2ba19526` | `market_open_dca/YYYYMMDD_0935ET/` | `latest_market_open_run.json` |
| `morning` | 10:30 | `42173154` | `morning_dca/YYYYMMDD_1030ET/` | `latest_morning_run.json` |
| `intraday` | 11:30, 13:30, 14:30 | `7be82b97` | `intraday_dca/YYYYMMDD_<slot>ET/` | `latest_intraday_run.json` |
| `midday` | 12:30 | `a6239dae` | `midday_dca/YYYYMMDD_1230ET/` | `latest_midday_run.json` |
| `afternoon` (final buy) | 15:30 | `10ce362c` (supersedes `ce16ba6b`) | `scheduled_runs/YYYYMMDD_1530ET/` (under legacy `ce16ba6b` root) | `latest_scheduled_run.json` |

Manual runs write under each stream's own `manual_runs/<timestamp>/` and **never** update a
pointer; reporting excludes manual runs and manual order IDs from scheduled-live metrics. Route
scheduled-vs-manual decisions through `scripts/run_classification.py`.

## Live scheduled task

| Field | Value |
|---|---|
| Cron id | `10ce362c` (repaired live task) |
| Supersedes | `ce16ba6b` (deleted) |
| Schedule | **3:30 PM ET** weekdays (`30 19 * * 1-5` UTC) |
| Accepted window | **3:25–3:35 PM ET** (legacy pre-retime runs: 3:15–3:35 PM ET) |
| Broker/account | Robinhood `<ROBINHOOD_ACCOUNT_NUMBER>` only (Alpaca read-only) |

The repaired live task `10ce362c` supersedes the deleted `ce16ba6b`. The
artifact root stays under `.../ce16ba6b` for continuity across the repair.

**Retimed to 3:30 PM ET on 2026-07-27** (from 3:20 PM ET). Writers emit only the
canonical identity: `scheduled_for_et='15:30'`,
`scheduled_window_start_et='15:25'`, and
`run_id='scheduled_YYYYMMDD_1530ET'` under
`scheduled_runs/YYYYMMDD_1530ET/`. Validators and schemas also **accept** the
legacy identity (`'15:20'`, `'15:15'`, `scheduled_YYYYMMDD_1520ET`) so run_states
written before the retime stay valid scheduled evidence. Order attribution keeps
its lower bound at **15:15 ET**, deliberately wider than the classification
window, so orders placed by a legacy 3:20 PM run remain attributable to that run.
See `SCHEDULED_FOR_ET` / `LEGACY_SCHEDULED_FOR_ET` /
`ORDER_ATTRIBUTION_WINDOW_START_ET` in
[`scripts/run_classification.py`](../scripts/run_classification.py).

**Writer contract:** every scheduled 3:30 PM ET run_state must be written with
**both** `cron_id='10ce362c'` **and** `supersedes_cron_id='ce16ba6b'`.
Placeholder or ambiguous ids such as `ce16ba6b_replacement` are **forbidden**
(`supersedes_cron_id` must be exactly `ce16ba6b` when present; consumers may omit it only for continuity with older artifacts).

**Consumer acceptance:** for continuity, a scheduled run is valid evidence when
**`cron_id='10ce362c'` OR `supersedes_cron_id='ce16ba6b'`**, so continuity
artifacts written before/after the repair both remain valid.

## Artifact layout

```
/home/user/workspace/cron_tracking/ce16ba6b/
├── scheduled_runs/
│   └── YYYYMMDD_1530ET/
│       └── run_state.json          # run_origin = "scheduled"
├── manual_runs/
│   └── <timestamp>/
│       └── run_state.json          # run_origin = "manual"
└── latest_scheduled_run.json       # updated ONLY by scheduled runs
```

- **Scheduled run** → writes `scheduled_runs/YYYYMMDD_1530ET/run_state.json`
  **and** updates `latest_scheduled_run.json`.
- **Manual run** → writes `manual_runs/<timestamp>/run_state.json` and **never**
  touches `latest_scheduled_run.json`.

## The `run_classification` block

### Scheduled run (required fields)

```json
{
  "run_origin": "scheduled",
  "run_type": "scheduled_live_test",
  "cron_id": "10ce362c",
  "supersedes_cron_id": "ce16ba6b",
  "scheduled_for_et": "15:30",
  "scheduled_window_start_et": "15:25",
  "scheduled_window_end_et": "15:35",
  "manual_run": false,
  "run_id": "scheduled_YYYYMMDD_1530ET",
  "triggered_at_utc": "2026-07-06T19:20:01Z",
  "state_schema_version": "run-classification-v1"
}
```

### Manual run (required fields)

```json
{
  "run_origin": "manual",
  "manual_run": true,
  "cron_id": "10ce362c",
  "supersedes_cron_id": "ce16ba6b",
  "run_id": "manual_YYYYMMDD_HHMMSSZ",
  "triggered_at_utc": "2026-07-06T14:05:00Z",
  "state_schema_version": "run-classification-v1"
}
```

The manual `run_id` must be **distinct** and must never match the scheduled
pattern `scheduled_YYYYMMDD_1530ET`.

### Manual final buy override (`final_manual_buy_override`)

When the **final buy of the day** is requested manually (e.g. the scheduled
3:30 PM ET run placed 0 orders and the user asks for the final buy by hand), the
run is a manual run with `run_type='final_manual_buy_override'` and adds two
explicit flags so downstream reporting can never mistake it for scheduled
evidence:

```json
{
  "run_origin": "manual",
  "manual_run": true,
  "run_type": "final_manual_buy_override",
  "valid_afternoon_scheduled_evidence": false,
  "same_day_top_up_context_only": true,
  "cron_id": "10ce362c",
  "supersedes_cron_id": "ce16ba6b",
  "run_id": "manual_YYYYMMDD_HHMMSSZ",
  "triggered_at_utc": "2026-07-13T19:36:00Z",
  "state_schema_version": "run-classification-v1"
}
```

Build it with `build_final_manual_override_classification(...)`. The
**2026-07-13** incident is the canonical example: 34 fills, planned total
$234.00, backfilled under
`.../cron_tracking/ce16ba6b/manual_runs/final_manual_override_20260713_1536ET/`
and `.../cron_tracking/midday_dca/manual_reruns/final_manual_override_20260713_1536ET/`.
Its order IDs are excluded from all scheduled-live metrics and shown only in the
"Manual Final Buy Override / Same-Day Top-Up Context" report section (below).

## Scheduled state-file guarantee (always write, even with no order)

The 3:30 PM live task (`10ce362c`) **always** writes a scheduled
`run_state.json` under `scheduled_runs/YYYYMMDD_1530ET/` **and** updates
`latest_scheduled_run.json` every time it fires — **even when no order is
placed**. A run that fired but placed no order is still proof the task ran; it
is just not proof of an order or fill.

The top-level `order_submit_status` field on the run_state records the outcome:

| `order_submit_status` | Meaning |
|---|---|
| `submitted` | One or more orders submitted; `order_id`(s) on the buy_candidates. |
| `not_submitted_no_candidates` | No ticker qualified; valid scheduled run, no orders. |
| `not_submitted_confirmation_required` | Robinhood review **passed** but submission was **blocked by required post-review user confirmation**. Valid scheduled evidence the task ran; **no `order_id`**, **not** evidence of an order/fill. |
| `not_submitted_error` | An error prevented submission; valid scheduled run, error recorded. |
| `not_submitted_budget_exhausted` | Caps/budget left no room; valid scheduled run, no orders. |
| `dry_run` | Dry-run; no live orders. |

### Confirmation-blocked scheduled run (required fields)

When review passes but confirmation blocks submission, the run_state still
carries a **valid scheduled** `run_classification` block and adds:

```json
{
  "order_submit_status": "not_submitted_confirmation_required",
  "run_classification": {
    "run_origin": "scheduled",
    "manual_run": false,
    "state_schema_version": "run-classification-v1",
    "cron_id": "10ce362c",
    "supersedes_cron_id": "ce16ba6b",
    "scheduled_for_et": "15:30"
  },
  "buy_candidates": [
    {
      "ticker": "AAPL",
      "buy_multiplier": 1.0,
      "planned_amount_usd": 3.0,
      "status": "not_submitted_confirmation_required",
      "order_id": null,
      "reviewed_order": { "notional_usd": 3.0, "side": "buy", "review_passed": true }
    }
  ]
}
```

It **must include** the valid scheduled classification block,
`order_submit_status='not_submitted_confirmation_required'`, `reviewed_order`
details on each affected candidate where available, and the candidate details of
what would have been placed. It **must not include** any `order_id` or a
submitted/filled status.

**Downstream treatment of a confirmation-blocked run:**

- **Slippage (`67aa802b`, retimed 2026-08-02 from 3:35 PM ET to 5:00 PM ET
  daily)** — report **no scheduled fills and no scheduled order IDs**. Do
  **not** invalidate the run_state because confirmation blocked submission, and
  the broker time-window fallback does **not** apply (there was no write
  failure).
- **Weekly/monthly** — count the scheduled run as having occurred; it
  contributes **zero** scheduled fills/orders.
- *(Historical, until 2026-08-02: the watchdog, `a46bf007`, also treated a
  confirmation-blocked run as valid and alerted that no order was placed
  without marking the lineage invalid. It was removed from the schedule
  entirely on 2026-08-02.)*

## Nested run_classification requirement for watchdog confirmation (added 2026-07-17)

The Fri Jul 17 2026 3:20 PM ET scheduled `run_state` was repaired to include a
**nested `run_classification` object**. From this date on:

1. **Nested object required.** Every scheduled `run_state.json` must contain a
   nested `run_classification` object, validated against
   [`../schemas/run_classification.schema.json`](../schemas/run_classification.schema.json).
   For the scheduled 3:30 PM run it carries `run_origin='scheduled'`,
   `manual_run=false`, `run_type='scheduled_live_test'`, `cron_id='10ce362c'`,
   `supersedes_cron_id='ce16ba6b'`, `scheduled_for_et='15:30'`,
   `scheduled_window_start_et='15:25'`, `scheduled_window_end_et='15:35'`,
   `run_id='scheduled_YYYYMMDD_1530ET'`, `triggered_at_utc`, and
   `state_schema_version`.
2. **Nested object is authoritative.** In this repo, scheduled/manual identity fields live
   under `run_classification` (see [`../config/run_classification.json`](../config/run_classification.json));
   top-level mirror fields are not required by the artifact writers/consumers.
3. **Confirmation.** A scheduled run is confirmed **only** when the nested
   `run_classification` object is present and valid. A `run_state` missing it
   is a validation **discrepancy**. (Confirmed by the 3:40 PM watchdog,
   `a46bf007`, until it was removed from the schedule entirely on 2026-08-02;
   now confirmed by `67aa802b`'s post-trade validation.)

See the
`live_reliability_fix_2026_07_17.scheduled_run_state_classification_requirement`
block in [`../config/scheduled_tasks.json`](../config/scheduled_tasks.json) and
the `run_classification` property in
[`../schemas/run_state.schema.json`](../schemas/run_state.schema.json).

## Raw connector-input persistence (added 2026-07-10)

The 2026-07-10 3:20 PM ET run escalated because quotes and historicals were
fetched but the **raw connector responses were never persisted before signal
computation** — the bash compute step had no `quotes_batchN.json` /
`historicals_batchN.json` to read. Contract from that date on:

1. **Persist before compute.** Before computing any signal, the scheduled run
   persists every raw Robinhood/Finance/Alpaca/Yahoo connector response it used
   under `scheduled_runs/YYYYMMDD_1530ET/raw/` with deterministic filenames:
   `quotes_batchN.json`, `historicals_batchN.json`, `tradability_pre_place.json`,
   `portfolio_pre_place.json`, `orders_after_place.json`, and `place_SYMBOL.json`
   where applicable.
2. **Normalize pointer payloads.** A connector result returned indirectly as
   `{"output_path": "..."}` is normalized to parseable JSON before persisting
   (referenced contents under `data`, original pointer preserved under
   `output_path`), so compute can parse the saved file directly.
3. **Compute reads persisted files only** — never in-memory tool responses.
4. **Failure still writes state.** If raw persistence or normalization fails, the
   task still writes a **valid scheduled** `run_state.json`
   (`run_origin='scheduled'`, `manual_run=false`,
   `order_submit_status='not_submitted_error'`, `raw_persistence_error` set,
   empty `raw_inputs_saved`, no orders) rather than escalating without state.

The run_state records `raw_input_dir`, `raw_inputs_saved`, and
`recovery_from_escalation` (see
[`../schemas/run_state.schema.json`](../schemas/run_state.schema.json)). Helpers
live in [`../scripts/run_classification.py`](../scripts/run_classification.py):
`raw_input_dir`, `quotes_batch_filename`, `historicals_batch_filename`,
`place_filename`, `save_raw_input`, `normalize_connector_payload`,
`list_raw_inputs_saved`, `build_raw_persistence_state`, and
`raw_persistence_failure_run_state`.

## Accepted schema versions (v1 + v2)

Two `state_schema_version` values are accepted:

- **`run-classification-v1`** — the original contract.
- **`run-classification-v2-preauthorized`** — a **stricter successor** added
  2026-07-10 for the pre-authorized direct-placement live task (no
  `review_equity_order` step). It is not a different contract; it carries the same
  required scheduled identity fields (`run_origin`, `manual_run`,
  `cron_id`/`supersedes_cron_id`, `scheduled_for_et`, window, `run_id`).

Downstream consumers (slippage `67aa802b`, weekly `041d9d90`) accept **both**
versions and must **not** discard a scheduled run_state for its
`state_schema_version` alone when the identity fields are correct. The helper
exposes `ACCEPTED_STATE_SCHEMA_VERSIONS` and `is_preauthorized_v2(...)`, and
`validate_classification` accepts either version.

## Slippage order-history persistence (added 2026-07-10)

The slippage task (`67aa802b`, retimed 2026-08-02 from 3:35 PM ET to 5:00 PM ET
daily) **persists the Robinhood order-history response before computing
slippage** and computes average fill price / executions from the saved payload
rather than re-querying the broker:

- The payload is saved to
  `scheduled_runs/YYYYMMDD_1530ET/raw/orders_after_place.json` and its path is
  recorded in the slippage report's `source_order_history_payloads`.
- Fills are extracted from the saved payload via
  `dca_reporting.fills_from_order_history_payload`, which accepts a bare list,
  `{"orders": [...]}` / `{"results": [...]}`, or a normalized
  `{"output_path", "data": ...}` pointer payload.
- The **weekly dashboard** (`041d9d90`) includes v2-preauthorized scheduled runs
  and, when per-day slippage report files are missing, falls back to these
  persisted broker-history payloads before declaring slippage unavailable. Manual
  order IDs remain excluded from all scheduled-live metrics.

## Downstream reporting rules

Applies to slippage (`67aa802b`), weekly dashboard (`041d9d90`), and monthly
dashboard (`0656657a`). (Also applied to the watchdog, `a46bf007`, until it was
removed from the schedule entirely on 2026-08-02.)

1. **Validate** scheduled state files against the run_classification schema
   before using them, accepting **both** `run-classification-v1` and
   `run-classification-v2-preauthorized` when the identity fields are correct.
2. **Collect and exclude** every `order_id` found under the manual exclusion
   roots from all scheduled-live metrics. The roots are the task's own
   `manual_runs/**/run_state*.json` **plus** these additional roots (added
   2026-07-13 so out-of-task manual overrides are still excluded):
   `final_manual_review/**`,
   `.../cron_tracking/ce16ba6b/manual_runs/final_manual_override_*`, and
   `.../cron_tracking/midday_dca/manual_reruns/`. `collect_manual_order_ids`
   iterates all of these (see `manual_exclusion_roots` and
   `MANUAL_EXCLUSION_SUBROOTS` in the helper).
3. **Exclude** any run_state whose classification has `run_origin='manual'` or
   `manual_run=true`, *regardless of where the file sits on disk*. If either
   signal says manual, the run is manual.
4. **Never infer** scheduled status from same-day broker orders alone — a valid
   scheduled run_state must exist. A day with broker orders but no valid
   scheduled state is a watchdog **discrepancy**, not a scheduled run.
5. Scheduled-live **cost basis, slippage, reliability, and order-count** metrics
   are computed only from validated scheduled runs.

### Broker time-window fallback (narrow)

Attributing in-window (3:15–3:35 PM ET) broker orders to the scheduled run is
allowed **only** when **all** hold:

- A valid scheduled run_state exists (`run_origin='scheduled'`, passes schema),
  **and**
- it lacks order IDs due to a **known/recorded write failure**.

Even under fallback, manual order IDs are still excluded. Fallback is **never**
allowed when no valid scheduled state exists, and never attributes orders from a
manual-classified run.

## Same-day top-up guard (prior-allocation context only)

The scheduled 3:30 PM ET run may **read** earlier same-day manual/morning runs to
offset its buys (the same-day prior-run offset/top-up guard; see
[strategy.md](strategy.md#same-day-prior-run-offset--top-up-guard)). This reading
does **not** reclassify anything:

- Earlier manual/morning runs feed the guard as **prior-allocation context only**.
  They are still excluded from every scheduled-live metric (evidence, slippage,
  watchdog confirmation, scheduled cost basis, scheduled fill counts) per the
  rules above.
- Only earlier runs generated **before 2:30 PM ET** contribute, and any run
  classified as scheduled is ignored as prior context — so a morning/manual order
  before 2:30 PM ET is read for top-up only, never as afternoon scheduled
  execution evidence.
- A day whose afternoon buys are fully offset to $0 by the guard still produces a
  valid scheduled run_state; the watchdog treats it as a completed scheduled run,
  not a discrepancy.

## Manual missed-morning recovery (added 2026-07-20)

Distinct from the **automatic** recovery reruns below (which stay *scheduled*),
this covers the case where the **10:30 AM scheduled live morning task did not run
at all** and a human places the missed morning buys **by hand**. Such a recovery
is **manual by contract** and is written under a dedicated path:

- Pattern: `/home/user/workspace/cron_tracking/morning_dca/manual_recovery/YYYYMMDD_*`
- Example: `/home/user/workspace/cron_tracking/morning_dca/manual_recovery/20260720_1048ET`
  (Jul 20 2026: the 10:30 scheduled morning live task did not run, so a manual
  recovery ran at 10:48 AM ET.)

Every such artifact carries `run_origin='manual'`, `manual_run=true`,
`same_day_top_up_context_only=true`, and `valid_scheduled_evidence=false`. Under
those flags:

- It is valid **only** as prior-allocation / same-day top-up context.
- It **never** counts as proof the 10:30 scheduled task ran.
- It is **never** written into the midday or afternoon scheduled evidence stream.
- It is **never** used for scheduled reliability, scheduled fill counts,
  scheduled slippage, scheduled cost basis, or watchdog confirmation.
- It may **only** reduce or block additional buys through the same-day top-up
  rule: a later run buys `max(0, desired - prior)` and only if the later decision
  price is **strictly below** the prior reference price.

The **12:30 midday** (`a6239dae`) and **3:30 final** (`10ce362c`) runs **must
scan** this path for same-day manual recovery artifacts and fold them into the
top-up guard as prior allocation. The path is also part of the manual exclusion
roots (rule 2 above), so its order IDs are excluded from all scheduled-live
metrics. See the `manual_missed_morning_recovery` block in
[`../config/run_classification.json`](../config/run_classification.json).

## Manual dry-run runs (added 2026-07-21)

A **manual dry run** exercises the read-only data path (Finance, Robinhood,
Alpaca) and the signal ladder without placing anything. It carries
`run_origin='manual_dry_run'`, `dry_run=true`, `no_purchases=true`,
`order_submit_status='dry_run'`, and `valid_scheduled_evidence=false`. It calls
no `review_equity_order`, no `place_equity_order`, no cancellations, and no
broker modifications.

Dry-run artifacts are diagnostics only. They must **not** be treated as
scheduled evidence, top-up proof, slippage evidence, or confirmation that any
scheduled task ran, and they are excluded from scheduled reliability, fill
counts, slippage, cost basis, and watchdog confirmation. A positive dry-run
signal is a hypothetical sizing preview, not an order.

The **2026-07-21 09:05 ET** run is the canonical example: 44 tickers scanned,
Finance lookup completed with expected unresolved fallback cases AAAU/SPCX/GFUZ,
Robinhood account <ROBINHOOD_ACCOUNT_NUMBER> present with 44 quotes / 44 prior closes / 44
tradability results, Alpaca ACTIVE with buying_power 0, one hypothetical
positive signal (CBRS drop_pct 0.1074, 1x, $2), and no orders placed. Artifacts
under `/home/user/workspace/cron_tracking/dry_runs/20260721_0905ET/`. See the
`manual_dry_run_2026_07_21` block in
[`../config/run_classification.json`](../config/run_classification.json) and
`morning_manual_dry_run_2026_07_21` in
[`../config/scheduled_tasks.json`](../config/scheduled_tasks.json).

## Watchdog all-orders broker query (added 2026-07-21, historical — watchdog removed 2026-08-02)

The 3:40 PM live-run watchdog (`a46bf007`) previously queried the **agentic**
(`placed_agent='agent'`) order subset first, so same-day **user/manual** Robinhood
orders could be missed by primary reconciliation. From **2026-07-21** the watchdog
**had to query ALL same-day Robinhood equity orders for account <ROBINHOOD_ACCOUNT_NUMBER> without any
`placed_agent` filter first**, then classify every returned order. It **could**
report the agentic-only subset separately, but the primary reconciliation ran over
the **full** order set. The watchdog was removed from the schedule entirely on
2026-08-02 (see [`safeguards.md`](safeguards.md#operational-rules)); no
remaining task performs this all-orders/no-filter query. Kept here as
historical documentation, mirroring `config/run_classification.json`'s and
`config/scheduled_tasks.json`'s own `watchdog_all_orders_fix_2026_07_21` blocks.

Each order is classified by:

- **scheduled evidence** — a validated scheduled `run_state` whose
  `submitted_orders.csv` / `filled_orders.csv` contains the order ID;
- **manual artifacts** — the manual-exclusion roots;
- **same-day top-up roots** — `top_up_context_roots()`
  (`morning_dca/manual_recovery`, `midday_dca/manual_recovery`,
  `midday_dca/manual_runs`, `midday_dca/manual_reruns`);
- **`placed_agent`**;
- **order IDs**.

Orders with `placed_agent='user'` or any non-agentic value are reported in a
dedicated **Manual/User Orders** section and **must not** count as scheduled fills
unless their order IDs appear in a valid scheduled `submitted_orders.csv` from the
same-day scheduled run. Manual fills stay excluded from scheduled slippage,
scheduled fill counts, scheduled reliability, scheduled cost-basis evidence,
watchdog proof, and scheduled afternoon evidence. See the
`watchdog_all_orders_fix_2026_07_21` block in
[`../config/run_classification.json`](../config/run_classification.json) and
[`../config/scheduled_tasks.json`](../config/scheduled_tasks.json).

## 3:30 runner self-containment and state-file guarantee (added 2026-07-20)

After the Jul 20 2026 10:30 and 12:30 scheduled live runs both failed (they
invoked saved runners without first generating the same-day raw inputs and
escalated with no scheduled failure-state artifact), the same self-containment and
state-file guarantees apply to the 3:30 afternoon final live task (`10ce362c`,
supersedes `ce16ba6b`):

**Generate raw inputs first.** The task **must not** assume runner inputs already
exist. Before invoking any saved runner or order module, it **creates**
`/home/user/workspace/cron_tracking/ce16ba6b/scheduled_runs/YYYYMMDD_1530ET/raw/`
and persists all required raw inputs: portfolio / buying power, quotes, prior
regular-session close, tradability, `orders_after_1430ET` (for afternoon evidence
checks), the same-day prior-allocation context from the morning-live, midday,
manual missed-morning recovery, and manual override roots, and the historical /
trend bars needed for the trend check. If a runner requires a `RUN_DATE_ET`
argument, pass the current America/New_York date as `YYYYMMDD`. Do **not** reuse
stale files. Inputs come from the Finance connector primary, Alpaca and Robinhood
validation / context, and `yfinance` fallback **only** when connector data is
unavailable, stale, incomplete, or unresolved. If raw input generation fails once,
perform **one** bounded recovery attempt on the alternate approved data source
path, then stop.

**Always write the state files.** The 3:30 scheduled run **always** writes
`run_state.json`, `run_summary.json`, and `latest_scheduled_run.json`, **even when
execution is blocked, is a no-op, or errors**. On a blocking condition it writes a
**valid scheduled** afternoon `run_state` with `run_origin='scheduled'`,
`manual_run=false`, `stream='afternoon'`, `scheduled_for_et='15:30'`,
`cron_id='10ce362c'`, `supersedes_cron_id='ce16ba6b'`,
`state_schema_version='run-classification-v1'`, `orders_submitted_count=0`, a
concrete `order_submit_status`, a concrete `error_class` such as
`missing_or_invalid_raw_inputs` or `no_incremental_top_up`, and a compact list of
missing files, block reasons, or failed data sources. This artifact is valid proof
the 3:30 scheduled task **fired** but is **not** order or fill evidence. If any
step fails after raw collection has started, it writes the `signals.csv` when
possible, an empty `submitted_orders.csv` with headers when no orders were placed,
and `run_state.json` (no half-done run without a state file).

**Manual recovery exclusion.** Manual recovery artifacts (missed-morning under
`morning_dca/manual_recovery/**` and manual 12:30 under
`midday_dca/manual_recovery/**`) remain prior-allocation / top-up context only and
are **never** afternoon scheduled evidence, slippage, reliability, fill-count, or
cost-basis evidence. See the `live_schedule_hardening_2026_07_20` block in
[`../config/run_classification.json`](../config/run_classification.json) and
`config/scheduled_tasks.json`'s
`live_schedule_hardening_2026_07_20.afternoon_1530`.

## Pointer output normalization (added 2026-07-20)

The Jul 20 2026 3:20 scheduled run collected portfolio, quotes, tradability,
same-day orders, and historicals, but large historicals (and possibly other
connector responses) were only available as `output_path` /
`current_session_context` tool-output **pointers**. The background run did **not**
persist those pointer targets into the scheduled raw directory before compute, so
it **escalated after data collection**. This applies to **all daily scheduled DCA
tasks that use connector data**, especially the **10:30** morning (`42173154`),
**12:30** midday (`a6239dae`), **3:05** monitor-only signal check (`e3e6f09e`), and
**3:30** afternoon (`10ce362c`) tasks:

- Any connector response that returns an `output_path` or
  `current_session_context` pointer must be **immediately copied or normalized into
  the run's raw directory before compute** (follow the pointer, load the referenced
  payload, persist as parseable JSON with contents under `data` and the original
  pointer under `output_path`).
- Compute modules must read **only** the persisted raw files, never in-memory tool
  responses.
- **Pointer-only collection is not a completed run.** The task must not stop after
  collecting data without writing `run_state`.
- If pointer normalization fails after **one** bounded recovery attempt, write the
  task's valid failure-state file with a concrete `error_class` (e.g.
  `pointer_normalization_failed` or `missing_or_invalid_raw_inputs`), empty
  `submitted_orders.csv` headers where applicable, and a compact list of the
  missing pointer details.

Parent recovery copied the `current_session_context` / `tool_calls` /
`call_external_tool` outputs into
`/home/user/workspace/cron_tracking/ce16ba6b/scheduled_runs/20260720_1520ET/raw/`,
computed from the persisted raw files, placed scheduled top-up orders, and wrote
`run_state`. The recovered run is **valid scheduled evidence**:
`run_id='scheduled_20260720_1520ET'`, `order_submit_status='filled'`,
`orders_filled_count=6`, total **$10.00** (EVR $1.00 @ 333.600800, DXYZ $1.00 @
24.865000, AMAT $2.00 @ 526.819900, STM $2.00 @ 61.450000, NVDA $2.00 @
202.771200, GLW $2.00 @ 153.810400). Live task text was updated with **POINTER
OUTPUT NORMALIZATION ADDED JUL 20 2026** for the 10:30, 12:30, 3:05, and 3:30
tasks. See the `pointer_output_normalization_2026_07_20` block in
[`../config/run_classification.json`](../config/run_classification.json) and
`config/scheduled_tasks.json`.

## Recovery reruns

> **IMPLEMENTATION STATUS (2026-08-03): PARTIALLY implemented. Read the split
> below before relying on any of it.**
>
> **✅ Implemented — `run_dca.py`'s raw-input persistence path.** That path now
> performs exactly **one** automatic in-process recovery retry, in the **same
> run directory**, and stamps the result on the run_state via
> `build_recovery_state()`:
> - Retried only for `raw_file_write_failure` (an `OSError` while staging,
>   normalizing, or swapping `raw/` — a partial write, interrupted rename,
>   permission or ENOSPC blip), classified by `run_dca._recovery_trigger_for()`.
> - On success the run continues through the **unchanged** guarded path — every
>   signal, budget, spread, confidence, top-up and circuit-breaker check still
>   runs — and the state carries `recovery_rerun=true`, `recovery.attempt=1`,
>   `recovery.escalated=false`. It is **never** reclassified as manual.
> - If the one retry also fails, the run **escalates** with
>   `recovery.escalated=true` and a `blocking_reason`, and does not retry again.
> - A **deterministic** failure (`parsing_failure`,
>   `wrapper_normalization_failure`, or a contract fault such as absent
>   `raw_connector_inputs`) escalates **immediately without retrying** — a second
>   identical attempt on the same in-memory payload would fail identically, and
>   consuming the single permitted attempt there would misreport a payload or
>   config bug as an exhausted transient recovery.
>
> **⚠️ Scope limit — "rerun the required read-only inputs" is NOT implemented
> and cannot be, here.** `run_dca.py` receives `raw_connector_inputs` as an
> already-fetched, immutable mapping and holds no MCP/broker/market-data access
> of its own (`docs/live_wiring.md`: providers "arrive as injected callables").
> It can re-run the *persist → normalize → recompute* half, never the *fetch*
> half. **`transient_connector_throttling` therefore classifies but never
> retries in-process** — clearing it requires a fresh fetch, which only the
> invoking agent session can perform. Recovery from a throttled connector
> remains an agent-level responsibility.
>
> **❌ Still NOT implemented — the four class-based stream executors.**
> `recovery_rerun`, `recovery_reason`, and `recovery_attempts` appear **nowhere**
> in `morning_dca.py`, and therefore nowhere in the `market_open_dca.py` /
> `midday_dca.py` / `intraday_dca.py` subclasses that inherit its
> `run()`/`_run_impl()`. Those still **fail closed on the first error** via
> `_blocked_live_result(...)`, with no second attempt and no `recovery_rerun=true`
> artifact. `config/morning_dca.json`'s
> `automatic_failure_recovery.implementation_status` remains accurate for them.
> `recovery_escalation_run_state()` also still has no non-test caller — the
> escalation evidence above is merged into the existing hardened
> `raw_persistence_failure_run_state` instead, so its prior-order preservation
> is not lost.

The five scheduled **live** tasks — market-open (`2ba19526`), morning
(`42173154`), midday (`a6239dae`), intraday (`7be82b97`), and afternoon
(`10ce362c`) — support **automatic failure recovery**. (Market-open and
intraday were added to this coverage 2026-07-27, Codex review: "Apply
recovery safeguards to the new live tasks"; they previously had no equivalent
recovery rule despite being active real-money execution tasks.) When a run
fails on **internal persistence, parsing, wrapper-normalization, transient
connector throttling, or raw-file write** problems **after data was fetched**,
the task treats it as a recovery trigger (not a terminal stop): it reruns the
required read-only inputs **once**, **persists all raw responses first**,
**recomputes only from the persisted files**, and continues through the same
pre-authorized guarded path if every safeguard still passes.

A recovery rerun does **not** change classification:

- It **reuses the same logical scheduled run directory** (afternoon:
  `scheduled_runs/YYYYMMDD_1530ET/`) and stamps `recovery_rerun=true` on the
  run_state.
- It keeps `run_origin='scheduled'` and is **never** reclassified as manual. For
  the afternoon task it still carries **both** `cron_id='10ce362c'` **and**
  `supersedes_cron_id='ce16ba6b'` and still updates `latest_scheduled_run.json`.
  This is distinct from the midday `manual_reruns/` override tree, which **is**
  manual.
- If the single recovery attempt **also fails**, or a **safety / account /
  order rejection** occurs, the task writes `run_state.json` with the blocking
  reason (`recovery.blocking_reason`, `recovery.escalated=true`) and
  **escalates** — it does not retry further or drop to an interactive path.

The `recovery_rerun` flag and `recovery` block are defined in
[`../schemas/run_state.schema.json`](../schemas/run_state.schema.json), which
requires `recovery` whenever `recovery_rerun=true`; the policy lives in the
`failure_recovery_policy` block of
[`../config/scheduled_tasks.json`](../config/scheduled_tasks.json). Downstream
reporting treats a recovery rerun exactly like any other scheduled run of the
same stream (a successful recovery contributes its scheduled fills; an escalated
recovery contributes zero scheduled fills but is still a valid scheduled run
that fired).

Runtime helpers live in
[`../scripts/run_classification.py`](../scripts/run_classification.py), the
same module the 2026-07-10 raw-persistence fix above uses: `RECOVERY_TRIGGER_REASONS`
(kept in sync with `failure_recovery_policy.recovery_triggers` and the schema's
`recovery.trigger_reason` enum), `build_recovery_state` (stamps `recovery_rerun`/
`recovery` on a successful recovery rerun's run_state), and
`recovery_escalation_run_state` (builds a valid scheduled run_state, mirroring
`raw_persistence_failure_run_state`, for the case where the recovery attempt also
fails or hits a safety/account/order rejection). As with raw persistence, the
orchestrator wrapping the read-only rerun and raw-file persistence around the
`near_close_dca` provider calls is responsible for invoking these helpers with
the outcome of that rerun; they give the recovery contract's `run_state` fields
a concrete, tested implementation rather than being declarative policy alone.

## Manual Test Runs reporting

Manual fills may be reported **only** in a clearly labeled **"Manual Test Runs"**
section (see `manual_test_runs` in
[`../schemas/slippage_report.schema.json`](../schemas/slippage_report.schema.json)).
They must not affect scheduled-live cost basis, scheduled-live slippage,
scheduled-live reliability, or scheduled-live order-count metrics.

## Manual Final Buy Override / Same-Day Top-Up Context reporting

Fills from a `final_manual_buy_override` run (and same-day top-up context) may be
reported **only** in a clearly labeled **"Manual Final Buy Override / Same-Day
Top-Up Context"** section (see `manual_final_buy_override` in
[`../schemas/slippage_report.schema.json`](../schemas/slippage_report.schema.json)).
They must **never** affect scheduled-live cost basis, scheduled-live slippage,
scheduled-live reliability, scheduled-live order-count, or watchdog-confirmation
metrics. Their order IDs come from the manual exclusion roots (rule 2 above) and
appear in the slippage report's `excluded_manual_order_ids`.

## Enforcement helper

[`scripts/run_classification.py`](../scripts/run_classification.py) implements
the contract with the standard library only (no secrets, no network):

| Function | Purpose |
|---|---|
| `build_scheduled_classification` / `build_manual_classification` | Build compliant blocks |
| `build_final_manual_override_classification` | Build a `final_manual_buy_override` manual block (`valid_afternoon_scheduled_evidence=false`, `same_day_top_up_context_only=true`) |
| `write_run_artifacts` | Write to the correct path; update the pointer only for scheduled runs |
| `validate_classification` | Runtime validation mirroring the JSON schema |
| `is_scheduled_state` / `is_manual_state` | Origin checks (manual flag wins) |
| `manual_exclusion_roots` / `cron_tracking_root` | Resolve all manual exclusion roots (task `manual_runs` + `final_manual_review`, `midday_dca/manual_reruns`) |
| `collect_manual_order_ids` / `exclude_manual_orders` | Manual order exclusion across all exclusion roots |
| `select_scheduled_state_for_reporting` | Pick the scheduled state, apply the narrow fallback |

Tests: [`scripts/tests/test_run_classification.py`](../scripts/tests/test_run_classification.py).

## Same-day latest-pointer validation (added 2026-07-21)

A `latest_*.json` pointer (`latest_morning_run.json`, `latest_midday_run.json`,
`latest_scheduled_run.json`, and the monitor / slippage / watchdog equivalents)
is only a convenience shortcut to the most recent run_state for a stream. A
pointer left over from a prior trading day, half-written, or aimed at the wrong
stream can look valid and be mistaken for proof that today's scheduled run
happened. The same-day validation contract (see
`config/scheduled_tasks.json` -> `same_day_pointer_validation_policy` and
[`schemas/latest_pointer.schema.json`](../schemas/latest_pointer.schema.json))
closes that gap.

A latest pointer is trusted as same-day scheduled evidence ONLY when:

- its `trading_day` equals the current America/New_York date, AND
- its `run_state_path` resolves inside the expected stream root, AND
- its `stream` matches the expected stream, AND
- the target run_state exists and is a scheduled, non-manual run
  (`run_origin='scheduled'`, `manual_run=false`).

Stale, malformed, missing, pending, prior-date, future-date, outside-root, and
wrong-stream pointers are ignored: they must not be used as scheduled evidence,
fill evidence, top-up proof, slippage evidence, reliability evidence, or
confirmation that a stream ran. A date-specific same-day run_state file is
preferred over the latest pointer when both are present. A pending same-day
placeholder keeps `valid_scheduled_evidence=false` until the target run_state
is finalized as a scheduled non-manual run.

After each run, the latest pointer is refreshed with same-day metadata:
`run_id`, `run_state_path`, `stream`, `trading_day`, `pointer_status`,
`same_day_required=true`, `updated_at_utc`, and `valid_scheduled_evidence`
derived from the target run_state (never asserted blindly).

[`scripts/same_day_pointer.py`](../scripts/same_day_pointer.py) implements this
with the standard library only:

| Function | Purpose |
|---|---|
| `validate_pointer` | Validate a pointer as same-day scheduled evidence; returns a `PointerValidation` with `pointer_status` and `valid_scheduled_evidence` |
| `refresh_latest_pointer` / `build_pointer_metadata` | Write / build the same-day pointer payload |
| `derive_valid_scheduled_evidence` | True only for a scheduled, non-manual run_state |
| `select_same_day_run_state` | Resolve today's scheduled state, preferring the date-specific file over the pointer |

Tests: [`scripts/tests/test_same_day_pointer.py`](../scripts/tests/test_same_day_pointer.py).

## Scheduled-run artifact contract (added 2026-07-21)

The Jul 21 2026 scheduled runs exposed two artifact-schema failures: the
10:30 morning-live `run_state.json` (and the since-retired 9:45 pre-flight's) carried a **null
top-level `run_classification`**, and the 10:30 live run **dropped signals.csv
and submitted_orders.csv** even though its summary claimed they were written. A
null classification breaks scheduled-vs-manual attribution downstream, and a
missing order CSV makes the drift detector report every intended buy as an
unsubmitted order. Both are **artifact-contract failures, not soft warnings**.

The contract, one source of truth in
[`scripts/scheduled_artifacts.py`](../scripts/scheduled_artifacts.py):

1. **`run_classification` is always a non-null object, never `null`.** Every
   scheduled `run_state.json` (10:30 morning live, 12:30 midday
   live) must carry a non-null top-level `run_classification` object. A null,
   absent, or non-object value is a contract failure.
   - **10:30 morning live** (`build_morning_live_run_classification`): `run_origin='scheduled'`,
     `manual_run=false`, `stream='morning'`, `scheduled_for_et='10:30'`,
     `cron_id='42173154'`, `run_id`, `state_schema_version`, `trading_day`,
     `same_day_required=true`, `valid_afternoon_evidence=false`,
     `valid_scheduled_evidence` derived from the completed state.
2. **Live runs always write both CSVs.** A live run writes `signals.csv`
   whenever signals can be computed, and `submitted_orders.csv` **with a header
   row even when no order was submitted**. When an order is placed the CSV
   contains that row; when the broker response is pending/unconfirmed the row
   keeps its concrete status and is still written (along with the run_state).
   Deliberate non-orders (skipped, cap_exceeded, dry_run, blocked) are not rows.
3. **Same defensive guarantee for midday.** The 12:30 midday live run shares the
   morning runner path, so the always-write-both-CSVs and non-null classification
   guarantees apply to it as well, without weakening any trading safeguard.
4. **Missing artifacts fail the contract.** `validate_run_artifacts` fails when
   `run_state.json` is missing/unreadable, when `run_classification` is
   null/absent/not-an-object, or when a required CSV is missing.
   `require_submitted_orders` is True for live runs and False for read-only
   runs.
5. **Same contract for the 3:30 final live run.** The afternoon final live task
   (`10ce362c`) also writes a non-null `run_classification` object in both
   `run_state.json` and `run_summary.json`. For 3:30 the object carries
   `run_origin='scheduled'`, `manual_run=false`, `stream='afternoon'`,
   `scheduled_for_et='15:30'`, `cron_id='10ce362c'`,
   `supersedes_cron_id='ce16ba6b'`, `run_id`, `trading_day`,
   `same_day_required=true`, and evidence flags derived from the completed
   state. A null `run_summary.run_classification` is an
   `artifact_contract_failure`.

### Root-level artifact set (added 2026-07-27)

Two slots failed the same day in ways the rules above did not catch. The 12:30
midday run failed at startup **before writing any artifact**, and the 1:30
intraday run wrote its four files **only under an `artifacts/` subfolder** with
nothing at the run root, alongside an effectively empty `submitted_orders.csv`.
Both runs had fired; every consumer that discovers a run by directory path saw an
empty slot.

Every live scheduled run (`2ba19526`, `42173154`, `a6239dae`, `7be82b97`,
`10ce362c`) therefore writes these four files **at the run root**, on **every**
outcome, including a zero-order, blocked, or failed run:

| File | Rule |
|---|---|
| `run_state.json` | Non-null `run_classification` object, never a bare string. |
| `run_summary.json` | Mirrors run_state's `run_classification` and carries the outcome fields. |
| `signals.csv` | Header row even at zero signal rows. The stream-specific names (`morning_signals.csv`, `midday_signals.csv`, `market_open_signals.csv`, `intraday_signals.csv`) satisfy this. |
| `submitted_orders.csv` | Canonical header row even at zero orders. |

- **Nesting is a contract failure, not a partial pass.** A required file found
  only under `artifacts/` (or `out/`, `output/`) is reported by name:
  `nested_only_artifacts()` names the offending path so the operator sees the one
  real cause instead of four unrelated "missing file" errors.
- **A zero-byte CSV is not a valid no-signal artifact.** A reader cannot tell it
  from an interrupted write, and the drift detector sees no columns at all.
  `validate_run_artifacts` requires a header row on both CSVs.
- **Blocked is an outcome, not an excuse.** The date/holiday gate stub and the
  early calendar/window block both write the complete set. The afternoon stub
  carries a non-null `run_classification` and an `order_submit_status` so it
  passes the contract and is attributable, *and* `scheduled_evidence_denied` so
  it is never counted as evidence: a null-classification stub reads as
  malformed, while one that passed as evidence would manufacture an
  evidence-shaped artifact for a day the market was shut. It deliberately omits
  a top-level `run_id`, since model-usage logging keys off that to decide
  whether a run happened at all.
- `write_scheduled_run_artifacts()` is the single entry point that writes all
  four; hand-rolling a subset is exactly how the two gaps above happened.
- **Every live stream now has an in-repo producer**: `42173154` and `a6239dae`
  through `MorningDCAExecutor`/`MiddayDCAExecutor`, `10ce362c` through
  `near_close_dca`, and the market-open `2ba19526` and intraday `7be82b97`
  tasks through `market_open_dca.MarketOpenDCAExecutor` and
  `intraday_dca.IntradayDCAExecutor` -- both of which SHIP with
  `live_execution_enabled=false` in their config, so an external
  scheduler/agent still runs those two directly from the manifest (the
  manifest's `no_python_entry_point=true` predates the executors). For those
  two the contract is the task
  text the executor must follow, and the repo supplies the tested helpers
  (`write_scheduled_run_artifacts`, `validate_run_artifacts`,
  `classify_no_order_run`) rather than a producer. See
  `scheduled_run_artifact_contract.root_level_artifact_set_2026_07_27.enforcement_by_stream`.
- **A run that raises before reaching its writer still writes them, in both
  runners.** The 12:30 failure raised during startup, so `guarded_run` alerted
  and re-raised and the slot produced no artifact root at all. `run_once` had the
  same gap on the afternoon side: an exception from the account lookup, signal
  evaluation, or any per-candidate broker check escaped before
  `_finalize_and_write_artifacts` ran. Both scheduled live paths now write the
  complete set from the failure handler with
  `error_class='scheduled_run_startup_failure'`, `is_trading_failure=true`, and a
  non-null `run_classification` carrying `valid_scheduled_evidence=false`, then
  re-raises so the scheduler still sees the failure. Both also refresh their
  same-day pointer (the afternoon one refreshes `latest_scheduled_run.json` when
  a `classification_state_root` is configured), so a pointer-based watchdog sees
  today's slot as fired-and-failed rather than still targeting the previous
  trading day. The artifact proves the slot fired; it is never order evidence and
  never scheduled evidence, and it carries the scheduled `run_id` the schema
  requires. A failure *after* artifacts were written (possibly after orders were
  placed) never overwrites them.
- **A preserved state is merged, not substituted.** The run_state a failure
  handler finds can belong to an EARLIER invocation at the same slot. Preserving
  it is right, but this attempt's placements are folded into it rather than
  discarded, or an order the broker just accepted would be invisible to the
  same-day guards and a later run would buy it again.
- **Companion validation compares order rows, not just headers.**
  `validate_run_artifacts` checks the order CSV's header; a stale CSV from an
  earlier attempt passes that while omitting orders the fresh state records. Both
  handlers additionally require the CSV to cover the state's submissions before
  publishing a valid pointer, and rewrite it when it does not.
- **An accepted order is published the instant it exists.** Both placement loops
  expose their outcomes by reference before placing anything, and the afternoon
  path additionally records a submitted outcome *immediately after*
  `place_market_buy` returns, before the fill lookup or any other fallible
  follow-up. The batch otherwise only stores an outcome once the per-candidate
  call returns, so a raise in between would drop a real, already-accepted order
  from the failure artifact and let a later same-day run re-allocate it.
- **Three predicates, one rule.** `derive_valid_scheduled_evidence()`,
  `is_scheduled_state()`, and `update_latest_scheduled_run_pointer()` all decide
  whether a state is scheduled evidence, and each honors
  `artifact_contract_failure` and `scheduled_evidence_denied`. The pointer helper
  routes through the shared derivation rather than repeating the rule, since an
  inline copy is exactly how a denied state got published as valid.
- Validation accepts **any** recognized signals filename with a usable header, so
  a stale empty `signals.csv` left in a reused directory cannot invalidate a valid
  `morning_signals.csv` beside it. The drift detector picks the same file:
  `load_run_artifacts()` selects the first recognized name with a usable header
  (falling back to the first existing one), so the validator and the consumer
  never disagree about which file the run's signals live in.
- The nested-artifact scan covers only what the caller actually requires. A path
  that deliberately waives one file (the raw-persistence failure path validates
  with `require_signals=False` because no signal could be computed) is not failed
  by a stale nested copy of a file it never wanted.

## Completed no-order runs vs failures (added 2026-07-27)

The 1:30 intraday run recorded `error_class='missing_spread_data'` on a run where
**no ticker had qualified on the ladder at all**, so nothing ever reached the
spread gate and the missing spread was irrelevant to the outcome. A healthy
zero-order run was recorded as a data failure.

The fix is an explicit evaluation order: **ladder first, then the spread gate.**
Only qualifying candidates reach the gate, and only rows that reach the gate
carry a spread verdict. `scripts/run_classification.py` turns the two resulting
counts into the recorded outcome:

| Situation | `run_classification_outcome` | `completion_reason` | `error_class` |
|---|---|---|---|
| No ticker qualified at all | `completed_no_orders` | `no_ladder_candidates` | `null` |
| Qualified, but all stopped by a budget/cap check **before** the spread lookup | `completed_no_orders` | `blocked_before_spread_gate` | `null` |
| Reached the gate, all failed closed for an unproven spread | `completed_no_orders` | `null` | `missing_spread_data` |
| Reached the gate, some blocked and the rest simply did not qualify | `completed_no_orders` | `no_qualifying_orders_after_safety_checks` | `null` |
| Placement attempted on qualifying candidates and every one raised | `failed_after_connector_native_recovery_attempt` | `null` | same code |
| Raised before reaching placement at all | `failed_scheduled_run_startup` | `null` | `scheduled_run_startup_failure` |
| Stopped by a precondition or wiring gate before placement | `failed_scheduled_run_startup` | `null` | `scheduled_run_blocked_before_placement` |
| Raised **after** at least one order was accepted | `failed_after_partial_submission` | `null` | `scheduled_run_failed_after_submission` |
| Some orders submitted, another ticker's placement raised | `failed_after_partial_submission` | `null` | `partial_placement_failure` |
| A **manual** run's placement failed | `failed_placement` | `null` | same code |

Everything from the placement-failure row down is a **failure**, each carrying
`is_trading_failure=true`: failed placement, startup, precondition, post-submission,
partial placement, and manual placement. The first four rows are **safe no-order
skips**:
the run fired, evaluated, and wrote its artifacts, so each is valid scheduled
evidence that the slot executed, is never a missed slot, and never counts as an
error in scheduled reliability. The two failure outcomes stay distinct so
reporting keyed on the placement outcome cannot misattribute a startup or data
failure as a broker-placement failure.

Two qualifiers on the "healthy" rows:

- **Provider errors are never a clean no-op.** When a market-data provider
  raises, its tickers degrade to `NO_DATA`/manual review rather than being
  genuinely evaluated. A zero-order run with `errors_count > 0` keeps
  `completed_no_orders` but carries `error_class='degraded_market_data'` instead
  of a null one, so summary and reliability consumers can see it was degraded. A
  concrete spread cause is more specific and wins.
- **The spread gate marks its own blocks.** A row blocked there carries
  `blocked_by='spread_gate'`, and only those rows count toward
  `candidates_blocked_missing_spread`. Inferring it from `spread_status` alone is
  wrong on a manual run, where an unproven spread does not block placement, so a
  row can carry `spread_status='spread_unavailable'` and still be stopped later
  by human review, which is not a spread-caused outcome.

Two paths write and return before the classifier runs, so each stamps its own
outcome: the calendar/window block
(`completion_reason='date_or_holiday_gate_blocked'`) and the wiring blocks
(absent broker, wrong live account, raw-input
persistence failure). The wiring blocks share
`error_class='scheduled_run_blocked_before_placement'` unless the caller already
set a more specific one, such as `missing_or_invalid_raw_inputs`.

`degraded_market_data` covers two sources in the morning/midday runner, both of
which leave a run looking empty without a clean evaluation: a market-data
provider that raised (caught by `evaluate_signal`, degraded to a `NO_DATA` signal
carrying a `provider error` warning) and a pre-placement broker check that raised
(same-day order lookup, tradability, review), recorded as a **non-deliberate**
block. A deliberate safety gate carries `deliberate_skip=True` and is not an
error, so it leaves the run's clean classification intact.

A concrete terminal `order_submit_status` the placement path produced survives
the no-order classification: replacing `not_submitted_confirmation_required`
would make `is_confirmation_blocked()` return false and lose the fact that broker
confirmation is pending, and a dry run deliberately places nothing. Two are
replaced instead. `not_submitted_budget_exhausted` is the fallback the afternoon
deriver reaches when nothing else matched, so it only stands when a candidate
really was blocked on a budget, cap, or buying-power check. And
`not_submitted_no_candidates` describes exactly the case the contract assigns
`no_orders_submitted`; keeping it would make the afternoon aggregate disagree
with both the contract and the morning stream for the same situation, and its
specificity is preserved by `completion_reason='no_ladder_candidates'`.

Two rules keep failure records internally consistent:

- **The aggregate never contradicts the orders.** A run that raised after placing
  an order records `order_submit_status='submitted'` and
  `failed_after_partial_submission`, not `not_submitted_error`. Otherwise a
  summary consumer would report no submission while `submitted_orders.csv` holds
  the order.
- **A partially submitted batch is both.** When some orders land and another
  ticker's placement raises, the submitted count and status are preserved *and*
  `is_trading_failure=true` with `orders_failed_count` is recorded. Returning the
  run as an ordinary success would hide the failed order entirely.
- **A retry counts the whole slot, not just its own call.** A scheduled retry
  overwrites the fixed-path artifact, and the earlier attempt's submitted rows
  are carried forward into it. Both `orders_submitted_count` and
  `submitted_orders.csv` are derived from the union of this call's
  `broker_outcomes` and those carried-forward rows, deduplicated by order id, so
  a retry that places nothing new is never recorded as a zero-order run for a
  slot that did place orders.
- **Every batch that submits sets `order_submit_status='submitted'`.**
  `build_artifacts()` does not initialize that field, so without this even a
  fully successful morning or midday batch wrote a `run_state.json` with no
  aggregate submit status and a `run_summary.json` whose status was null.
- **The error class matches the outcome.** A failure after submission records
  `scheduled_run_failed_after_submission`, not the startup class, so a consumer
  grouping by `error_class` does not read a late post-trade failure as startup
  trouble.

The fail-closed raw-persistence states (`raw_persistence_failure_run_state` and
`recovery_escalation_run_state`) stamp the same blocked-before-placement outcome
fields, so the afternoon early-return path that writes them through the contract
writer carries them too rather than passing validation while saying nothing about
the outcome. They deliberately do **not** carry `orders_submitted_count`: that
state overwrites the slot's canonical run_state, and a same-slot retry whose
earlier attempt already placed orders would have them erased by an asserted zero.
The caller fills the count in from the slot's preserved `submitted_orders.csv`
when it can establish the real figure, and an absent field is honest where a zero
is wrong.
- **Connector-native outcomes are scheduled-only.** A manual run never runs that
  policy, so its broker failures record the generic `failed_placement`. Both
  runners pass `scheduled=not manual` into the classifier.

The in-flight placement state is cleared at the start of every `run()`. The
executor is reused across calls, so without that reset a startup failure on a
later run (a later trading day, even) would copy the previous run's outcomes into
the new day's artifact and record those orders as current-day allocations.

The failure path forces a pending pointer only for a **newly synthesized**
failure state. When a complete valid state already exists and only a later step
raised, its pointer evidence is derived normally: `validate_pointer()` rejects
any non-`valid` status, so forcing pending would make a successfully completed
run read as unavailable. That branch first revalidates the **full** artifact set,
not just the state file's existence: in a reused same-day directory the summary
or CSVs left in place can belong to an earlier attempt, and advertising a
mismatched summary as valid scheduled evidence is what this contract forbids.

When companion revalidation fails, the **state itself** is failed closed too, not
just the pointer: `select_same_day_run_state()` prefers the date-specific state
and calls `derive_valid_scheduled_evidence()` on it directly, so a pointer-only
downgrade would still hand the run to consumers as valid evidence. The preserved
state gains an `artifact_contract_failure` marker and
`scheduled_evidence_denied`, mirroring what `near_close_dca` does on a failed
contract check.

A synthesized failure state carries `scheduled_evidence_denied=true`, which
`derive_valid_scheduled_evidence()` honors exactly as it honors
`artifact_contract_failure`. This has to be a top-level marker rather than the
nested `run_classification.valid_scheduled_evidence`: the morning writer seeds
that nested field to `false` as a placeholder and then finalizes it *from* the
derivation, so honoring it there would lock every run to `false`. Without the
marker, `select_same_day_run_state()` prefers the date-specific state over the
pending pointer and would hand a failed run to every consumer as valid evidence.

**Denial is not de-attribution.** `derive_valid_scheduled_evidence()` and
`is_scheduled_state()` answer two different questions and must not be collapsed:
the first asks whether the slot *executed* (reliability, watchdogs, missed-slot
accounting), the second decides whether a state's orders are *attributable* to
the scheduled slot (fill and slippage reporting). A run that placed a real order
and then raised answers no to the first and yes to the second. Both failure
handlers deny such a run as evidence while preserving its orders, so
`is_scheduled_state()` rejects a denied state **only when it has no order id to
attribute**, which is exactly the date/holiday-gated stub the check was added
for. Rejecting every denied state made the reporting selector discard the whole
record, and the preserved fills disappeared from scheduled reporting.

Both writers emit a header row for a zero-row `signals.csv` and
`buy_candidates.csv`, so an empty universe (no active tickers, or every
configured ticker excluded) still produces a contract-valid artifact instead of a
zero-byte file that would fail the run and block the latest-pointer update.

The `blocked_before_spread_gate` row matters because the cap, per-order ceiling,
already-bought-today, and same-day-duplicate checks all run **before** the spread
lookup: those tickers carry no spread verdict, but they did qualify on the
ladder, so reporting them as `no_ladder_candidates` would misstate what happened.
The candidate count, not the spread-gate count, is what separates the two.

A run whose placements all **raised** is a failure, not a healthy no-order run,
even though it also submitted nothing. Any candidate left at `status='failed'`
marks the run `failed_after_connector_native_recovery_attempt` with
`is_trading_failure=true`.

Per-row fields (`spread_ok`, `spread_pct`, `spread_status`) record each
candidate's own verdict: `spread_ok`, `spread_unavailable`, `spread_not_ok`, or
`not_evaluated` for a row that never reached the gate. A `not_evaluated` row is
never marked `spread_ok=true` and is never counted as blocked for a missing
spread. Run-level counts `candidates_reaching_spread_gate` and
`candidates_blocked_missing_spread` make the classification auditable from the
artifact alone.

**Partial availability fails only the affected rows.** A ticker whose spread was
never captured is skipped; every other qualifying ticker keeps being evaluated
and submitted. The run is never aborted for a subset, and spread is never
estimated, inferred, or defaulted.

**Morning/midday now fail closed too.** Before this the morning runner blocked
only on a spread that was captured *and* too wide, so a scheduled candidate whose
bid/ask was never captured could still be placed. It now fails that row closed,
matching the afternoon runner. Manual runs still go through `review_market_buy`
as a human-reviewed safety net, so they are not blocked on a missing spread
alone.

## 3:30 compute-to-placement contract (added 2026-07-22)

The Jul 22 2026 3:20 PM ET scheduled final live run generated 20 planned buy
orders with `planned_spend=60.0`, but the saved compute module stopped with
`error_class='placement_not_implemented_in_compute_module'` and submitted zero
orders. That is not a valid terminal state for a pre-authorized scheduled live
run when planned orders remain.

From Jul 22 2026 forward, once the 3:30 task writes `signals.csv`, every
eligible row with `incremental_usd > 0` or an equivalent planned order amount
must enter a placement phase in the same scheduled run. The placement phase
calls Robinhood `place_equity_order` directly after all ordinary safeguards
pass: validation, budget, spread, data confidence, regular-hours-only,
whitelist, buy-only, max-per-order, and same-day top-up. It must not call
`review_equity_order` and must not request per-order confirmation.

If the saved compute module cannot place orders, the scheduled task must invoke
the main live placement runner or use the direct platform connector placement
path itself. It must not write `order_submit_status='not_submitted'` with
`error_class='placement_not_implemented_in_compute_module'` while planned
orders remain.

If the placement phase is unreachable, the task writes
`order_submit_status='blocked_placement_phase_unreachable'` and
`error_class='placement_phase_unreachable'`, preserves the planned orders in
`signals.csv`, writes `submitted_orders.csv` with headers, notifies, and marks
completed order evidence false while still proving the scheduled task fired.

| Function | Purpose |
|---|---|
| `build_morning_live_run_classification` | Build the non-null scheduled classification for the 10:30 run |
| `submitted_order_rows` | Rows for submitted_orders.csv (placed/in-flight statuses or an order_id; pending/unconfirmed preserved) |
| `write_signals_csv` / `write_submitted_orders_csv` | Write the CSVs; both emit a header even when empty |
| `validate_run_artifacts` | Validate the run directory against the contract |

Tests: [`scripts/tests/test_scheduled_artifacts.py`](../scripts/tests/test_scheduled_artifacts.py)
and the live artifact tests in
[`test_morning_dca.py`](../tests/test_morning_dca.py).
