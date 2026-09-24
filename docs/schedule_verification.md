# Daily schedule verification

How to tell, for one ET trading day, whether each of the seven live roster
slots (`dca_runner.SLOTS`) actually fired and finalized, and the one command
each slot's Gate 9 runs so the answer can be "yes".

## Why this exists (2026-09-22)

The console's Schedule page showed every elapsed live slot as
`elapsed_unverified`, and no code ever changed that to verified or missing.
Checking `cron_tracking/` by hand for 2026-09-22 found:

| Slot | Run dir | `run_state.json` | Pointer |
| --- | --- | --- | --- |
| 09:35 | none | – | wrong shape, dated 09-16 |
| 10:30 | yes | **no** | dated 07-23 |
| 11:30 | yes | **no** | wrong shape, dated 09-17 |
| 12:30 | yes | **no** | missing |
| 13:30 | none | – | – |
| 14:30 | yes (free-form `gate9_report_*.json`) | **no** | wrong shape |
| 15:30 | none | – | missing |

The live-mirror prompts said "write run_state.json ... and refresh
`latest_<stream>_run.json`" without naming the code that does it. Each
session improvised, writing pointers shaped `{slot, trading_day, run_dir,
outcome}` that `scripts/same_day_pointer.validate_pointer` rejects as
`malformed` (missing `run_id`, `run_state_path`, `stream`). The slots fired,
but nothing they left behind could be verified.

## Verify a day

```bash
python -m scripts.verify_schedule                 # today, ET
python -m scripts.verify_schedule --date 2026-09-22 --json
```

For each slot it reads that slot's own dated directory
(`<stream root>/<YYYYMMDD>_<HHMM>ET/run_state.json`), not the stream pointer.
Intraday's single `latest_intraday_run.json` is overwritten by each of its
three slots and cannot say which slot wrote it.

| Status | Meaning | Fails the audit |
| --- | --- | --- |
| `verified` | `run_state.json` names this day, stream and `scheduled_for_et`, passes `derive_valid_scheduled_evidence`, and its directory meets the artifact contract (`validate_run_artifacts`, summary required). This proves the slot fired, not that any order filled | no |
| `evidence_incomplete` | The run directory exists but `run_state.json` is missing, unreadable, or not scheduled evidence for this slot | **yes** |
| `no_evidence` | The grace deadline has passed and there is no run directory | **yes** |
| `awaiting_evidence` | Past the nominal time, but before the window end + 30 min | no |
| `not_due` | Before the nominal time | no |
| `market_closed` | Gate 0 says the slot ends silently today and nothing was written | no |

Exit status is 1 when any slot fails. The console's Schedule page
(`dca_console_schedule.slot_evidence`) gives the same verdict through its
`BoundedReader`. It replaces `elapsed_unverified` on the matching live
manifest row (matched on both time and cron id) and adds a "Slot verified"
column to the roster matrix.

## Finalize a slot (Gate 9)

Every live-mirror prompt's Gate 9 now starts with:

```bash
python3 -m scripts.finalize_run_evidence --slot 1430 \
    --started-at 2026-09-22T14:30:41-04:00 \
    --outcome NO_ORDERS_GATE6_5_ZEROED --order-submit-status no_orders_submitted \
    [--orders-json $ROOT/finalize_orders.json] [--report-json $ROOT/gate9_report.json]
```

- It refuses a `--started-at` outside the union of `dca_runner.SLOTS`'s window
  and the prompt's own Gate 1 window (nominal − 5 min). The end of the window
  is exclusive, as in `gate_time_sanity`. An off-window fire is not a
  scheduled fire.
- It refuses order rows that aren't real orders. Every row needs a ticker, a
  broker `order_id`, a positive `submitted_amount_usd` and a status from
  `ORDER_ATTEMPT_STATUSES`, and duplicate ids are rejected. `submitted` also
  requires `--budget-json` (Gate 6's tier and caps). Without a budget, the
  state records the schema's `budget_evaluated: false`, and `counts` is always
  written.
- It redacts the pinned account number from `run_classification`, leaving
  `account_number_redacted: true` in its place.
- It stamps the run_classification the stream's own writer would:
  `<Executor>(mode="live").run_classification()` for 09:35–14:30, and
  `build_afternoon_live_run_classification` for 15:30.
- It writes `run_state.json` and `run_summary.json`, plus header-only CSVs
  where missing, then checks them with `validate_run_artifacts`.
- It refreshes the pointer through `same_day_pointer.refresh_latest_pointer`,
  or `update_latest_scheduled_run_pointer` for 15:30, then re-validates the
  pointer and the slot. It exits 0 only when both verify.
- A `run_state.json` written by an executor's own `run()` is never
  overwritten; the command only refreshes the pointer from it. If the summary
  or a CSV is missing beside it (the executor was interrupted), it writes just
  that missing file.
- It refuses a zero-order finalize (`no_orders_submitted` or
  `not_submitted_no_candidates`, without `--error-class`) that can't show
  Gate 5 actually evaluated data. On 2026-09-18 the 12:30 and 3:30 sessions
  lost market data, hand-wrote an empty candidates file and ended silently as
  a "quiet tape" (Notion keys `fabricated_empty_candidates`,
  `logged_as_quiet_tape`). The command finds this slot's generator document
  in the run directory by content (the count fields plus a matching
  `runner_slot_id` and `trading_day`), not by filename. It then requires:
  - `not_submitted_no_candidates`: the document must exist.
  - `no_orders_submitted`: a missing document is fine, because a Gate 4
    stand-down ends before Gate 5. A `*cand*.json` that isn't a document is
    refused, since that is the fabrication signature.
  - Zero buys: at least one usable ticker, counted from the document's
    `candidates` rows (not `NO_DATA`, not a provider error). The 15:30
    document has no `no_data_count`, and a provider-error ticker is itself a
    `NO_DATA` row. One provider error is routine and doesn't block.
    `evaluated_count: 0` with `already_covered_tickers` is Gate 4's served
    case.
  - A file with duplicate JSON keys is invalid evidence, whatever its name.
  - A `run_state.json` this command didn't write gets the same check before
    the pointer is refreshed. For an executor's own state, the check uses its
    `counts.evaluated` and `counts.no_data`; a state without them (hand-written)
    falls back to the directory's generator document.

  The refusal tells the session to finalize an outage honestly, as
  `not_submitted_error --error-class market_data_unavailable`, which then
  alerts. A passing check is recorded as `gate5_evidence` in `run_state.json`.
  A fully forged generator document still gets through; this catches the
  shortcut that actually happened.
- Everything after argument checks runs under a per-run `cross_process_lock`
  (`.<run dir>.finalize.lock` beside the run directory), so overlapping
  retries can't drop each other's orders. A state this
  command wrote earlier (a retry of the same fire) is rebuilt, and its orders
  are merged by `order_id`.

Exit codes: 0 means verified. 1 means files were written but do not verify
(the prompt alerts with the printed errors). 2 means the command refused and
wrote nothing.

`python3 -m scripts.check_live_prompt_drift` reports a live `SKILL.md` that
never runs this command as `missing_finalize_evidence`.

## Deploying the prompt change

Editing `claude/dca-executor-prompts/live-mirror/` changes nothing on its
own (see that directory's README). A human applies each mirror to its live
task through `update_scheduled_task`'s full-body replacement, with the account
placeholder expanded, and then runs `check_live_prompt_drift` to confirm that
no slot reports `missing_finalize_evidence`.
