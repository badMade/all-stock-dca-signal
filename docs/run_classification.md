# Run Classification Contract

Prevents **manual runs** of the graded DCA task from being confused with
**scheduled live runs**. This is a binding contract: every run_state artifact
must declare its origin, and every downstream reporting task must honor it.

- Machine-readable policy: [`../config/run_classification.json`](../config/run_classification.json)
- Schema: [`../schemas/run_classification.schema.json`](../schemas/run_classification.schema.json)
- Enforcement helper: [`../scripts/run_classification.py`](../scripts/run_classification.py)
- Embedded in: [`../schemas/run_state.schema.json`](../schemas/run_state.schema.json)
  (the `run_classification` block is **required** on every run_state)

`state_schema_version` for this contract is **`run-classification-v1`**.

## Live scheduled task

| Field | Value |
|---|---|
| Cron id | `10ce362c` (repaired live task) |
| Supersedes | `ce16ba6b` (deleted) |
| Schedule | **3:20 PM ET** weekdays (`20 19 * * 1-5` UTC) |
| Accepted window | **3:15–3:35 PM ET** |
| Broker/account | Robinhood `562057042` only (Alpaca read-only) |

The repaired live task `10ce362c` supersedes the deleted `ce16ba6b`. The
artifact root stays under `.../ce16ba6b` for continuity across the repair. A
scheduled run is valid evidence when **`cron_id='10ce362c'` OR
`supersedes_cron_id='ce16ba6b'`**, so continuity artifacts remain valid.

## Artifact layout

```
/home/user/workspace/cron_tracking/ce16ba6b/
├── scheduled_runs/
│   └── YYYYMMDD_1520ET/
│       └── run_state.json          # run_origin = "scheduled"
├── manual_runs/
│   └── <timestamp>/
│       └── run_state.json          # run_origin = "manual"
└── latest_scheduled_run.json       # updated ONLY by scheduled runs
```

- **Scheduled run** → writes `scheduled_runs/YYYYMMDD_1520ET/run_state.json`
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
  "scheduled_for_et": "15:20",
  "scheduled_window_start_et": "15:15",
  "scheduled_window_end_et": "15:35",
  "manual_run": false,
  "run_id": "scheduled_YYYYMMDD_1520ET",
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
pattern `scheduled_YYYYMMDD_1520ET`.

## Downstream reporting rules

Applies to slippage (`67aa802b`), watchdog (`a46bf007`), weekly dashboard
(`041d9d90`), and monthly dashboard (`0656657a`):

1. **Validate** scheduled state files against the run_classification schema
   before using them.
2. **Collect and exclude** every `order_id` found in
   `manual_runs/**/run_state*.json` from all scheduled-live metrics.
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

## Manual Test Runs reporting

Manual fills may be reported **only** in a clearly labeled **"Manual Test Runs"**
section (see `manual_test_runs` in
[`../schemas/slippage_report.schema.json`](../schemas/slippage_report.schema.json)).
They must not affect scheduled-live cost basis, scheduled-live slippage,
scheduled-live reliability, or scheduled-live order-count metrics.

## Enforcement helper

[`scripts/run_classification.py`](../scripts/run_classification.py) implements
the contract with the standard library only (no secrets, no network):

| Function | Purpose |
|---|---|
| `build_scheduled_classification` / `build_manual_classification` | Build compliant blocks |
| `write_run_artifacts` | Write to the correct path; update the pointer only for scheduled runs |
| `validate_classification` | Runtime validation mirroring the JSON schema |
| `is_scheduled_state` / `is_manual_state` | Origin checks (manual flag wins) |
| `collect_manual_order_ids` / `exclude_manual_orders` | Manual order exclusion |
| `select_scheduled_state_for_reporting` | Pick the scheduled state, apply the narrow fallback |

Tests: [`scripts/tests/test_run_classification.py`](../scripts/tests/test_run_classification.py).
