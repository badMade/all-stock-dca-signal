# DCA artifact drift detection

`scripts/drift_detection.py` reconciles what a live run *intended* to buy against
what it *actually* ordered, for a single run directory, and alerts on **drift**:
any symbol that carried an auto-buy signal with a positive intended dollar amount
but for which no submitted-order record exists.

- Status: **merged in PR #80** (merge commit `3641b90`) and wired into the live
  task chain — each live task carries a `post_artifact_drift_check` block and the
  `d1f7a2c0` monitoring entry tracks it.
- Machine-readable policy: [`config/scheduled_tasks.json`](../config/scheduled_tasks.json) → `drift_detection_policy`.
- Notification routing: [`config/notifications.json`](../config/notifications.json) → `routing.drift_alerts`.
- Tests: [`scripts/tests/test_drift_detection.py`](../scripts/tests/test_drift_detection.py).

Standard library only. **Never places or reviews trades** — it reads artifact
files. **No secrets** — the optional Slack webhook is read from the environment.

## Why this exists — the Jul 16 2026 failure mode

On **Thu Jul 16 2026** a live run wrote `signals.csv` with **8 rows** carrying
`auto_buy_signal=true` and **positive incremental amounts**, while
`submitted_orders.csv` was **empty** and the `raw/` directory was **missing**.
Signals were computed, but no orders were placed or persisted — and nothing
alerted until the next day. This detector is wired to run immediately *after*
each live run writes its artifacts so that exact condition pages on the spot.

For that run the detector reports `drift_detected=true`,
`run_suspected_cause="raw_data_persistence_failure"`, lists all 8 affected
symbols with their expected amounts, and (with `--slack`) fires an immediate
Slack alert.

## Detection rule

A signal row is an **expected buy** when **both** hold:

1. `auto_buy_signal` is true — parsed case-insensitively from `true` / `1` /
   `yes` / `y` / `t`.
2. `incremental_order_amount > 0` **or** a desired amount > 0
   (`afternoon_desired_amount`, `desired_amount`, `planned_amount_usd`, …).

An expected buy is **drift** when no row in `submitted_orders.csv` (fallbacks:
`orders.csv`, `filled_orders.csv`) references that symbol.

Column lookups are alias-based and case-insensitive so the morning, midday, and
near-close signal schemas all work without configuration.

## Suspected-cause inference

The detector picks the most likely run-level cause, in priority order:

| Cause | Trigger |
|---|---|
| `raw_data_persistence_failure` | `raw/` directory missing |
| `submitted_orders_missing` | no order-record CSV written at all |
| `submitted_orders_empty` | `submitted_orders.csv` present but has zero rows |
| `order_errors_or_rejections` | `order_errors.csv` / `rejections.csv` present and non-empty |
| `run_state_error_or_missing_classification` | `run_state.json` missing/errored, `order_submit_status` contains `error`, or no `run_classification` |
| `per_row_guard_or_block` | the row itself carries a `top_up_guard_result`/`block_reason`/`manual_review` status, or a budget/spread/confidence/trend signal explaining a deliberate $0 buy |
| `unknown_no_order_record` | none of the above |

A per-row guard explanation (e.g. `top_up_no_incremental`, a budget cap, a
missing-confidence `manual_review_candidate`) is surfaced as `per_row_guard_or_block`
rather than a false alarm, but is **still reported** so an operator can confirm
the block was intended.

## Output

Compact JSON to stdout, plus optional `--json-out` / `--md-out` files. Each
report carries the affected symbols, each symbol's expected order amount and
suspected cause, the run directory, and a recommended action.

```json
{"affected_symbols":[{"auto_buy_signal":true,"evidence":[],"expected_amount_usd":3.0,"suspected_cause":"raw_data_persistence_failure","symbol":"AAPL"}],"drift_detected":true,"expected_symbol_count":8,"raw_dir_present":false,"recommended_action":"raw/ is missing: ...","run_dir":"/.../20260716_1230ET","run_suspected_cause":"raw_data_persistence_failure","submitted_orders_empty":true,...}
```

## CLI

```bash
python scripts/drift_detection.py \
  --run-dir /home/user/workspace/cron_tracking/midday_dca/20260716_1230ET \
  --json-out <run_dir>/drift_report.json \
  --md-out   <run_dir>/drift_report.md \
  --slack --fail-on-drift
```

Exit codes: `0` no drift, `2` drift detected (only with `--fail-on-drift`),
`1` usage/IO error. `--fail-on-drift` lets a scheduler gate on the result.

## Slack alert integration (optional, no secrets)

Slack is **off by default** and requires **no secret in git**. The webhook URL is
read at runtime from the `DCA_DRIFT_SLACK_WEBHOOK` environment variable (or an
explicit `--slack-webhook`). To enable:

1. Set `DCA_DRIFT_SLACK_WEBHOOK` in the scheduler environment (secret store /
   env only — never commit it).
2. Pass `--slack` to the detector. On detected drift it POSTs a compact alert to
   the webhook. Add `--slack-always` to also confirm clean runs.

If `--slack` is set but no webhook is configured, the detector prints a warning
and continues; the exit code still reflects drift. When Slack is not configured,
drift still routes to `in_app_message_center` per
[`config/notifications.json`](../config/notifications.json).

## How scheduled tasks trigger the immediate alert

Each live trading task carries a `post_artifact_drift_check` block in
[`config/scheduled_tasks.json`](../config/scheduled_tasks.json) and MUST invoke
the detector **immediately after writing artifacts**:

| Task | ET | Run directory |
|---|---|---|
| `42173154` morning live | 10:30 AM | `cron_tracking/morning_dca/YYYYMMDD_1030ET/` |
| `a6239dae` midday live | 12:30 PM | `cron_tracking/midday_dca/YYYYMMDD_1230ET/` |
| `10ce362c` afternoon live | 3:20 PM | `cron_tracking/ce16ba6b/scheduled_runs/YYYYMMDD_1520ET/` |

The 3:20 PM check must complete before the 3:40 PM watchdog (`a46bf007`). When
an auto-buy signal exists but no order record does, the task fires the Slack
alert (webhook present) and/or the in-app drift alert immediately — it does not
wait for the next-day review.

## Running the tests

```bash
python -m pytest scripts/tests/test_drift_detection.py -q
# or, without pytest:
python scripts/tests/test_drift_detection.py
```

Fixtures cover: clean matched orders, top-up-blocked rows, auto-buy rows without
orders, the missing-`raw/` Jul 16 failure mode, and an empty
`submitted_orders.csv`.
