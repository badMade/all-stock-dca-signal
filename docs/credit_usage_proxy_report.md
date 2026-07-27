# One-time DCA credit-usage proxy report (2026-07-27, 4:30 PM ET)

Analytics and reporting only. **No trading.** No order review, no order
submission, no cancellations, no sells, and no broker or account modification.

Machine-readable source:
[`config/scheduled_tasks.json`](../config/scheduled_tasks.json) ->
`credit_usage_proxy_report_2026_07_27`. Task entry:
`onetime-1630-credit-usage-proxy-2026-07-27`.

| Field | Value |
|---|---|
| Name | One-time 4:30 PM DCA credit-usage proxy report with recovery runs |
| Fires | 2026-07-27 4:30 PM America/New_York |
| Cadence | One-time |
| Type | Analytics only |
| Trades | no |
| Writes `run_state.json` | no |
| Valid scheduled evidence | never |
| Scheduler-registered | **yes** (`cron_utc`: `30 20 27 7 *`, 20:30 UTC = 4:30 PM EDT on 2026-07-27) |
| Executor | [`scripts/generate_credit_usage_proxy_report.py`](../scripts/generate_credit_usage_proxy_report.py) |

## Registered with the scheduler

`cron_utc` and an executable command are now set on
`onetime-1630-credit-usage-proxy-2026-07-27`, the same convention every other
active task in this manifest uses -- see `scheduler_registration_note`.
[`scripts/generate_credit_usage_proxy_report.py`](../scripts/generate_credit_usage_proxy_report.py)
is a real, tested script that reads persisted same-day artifacts under
`cron_tracking/` and produces all six `required_sections`. As with every
other task's `cron_utc`, actually invoking the command at the scheduled time
is the external scheduler's responsibility, not something this repo runs on
its own; if the external scheduler is not wired to run it by 2026-07-27
4:30 PM ET, this task must still be reported as `did_not_run` rather than
assumed to have fired.

The registered `command` runs the script as a **module**
(`python -m scripts.generate_credit_usage_proxy_report`), not as a bare
script path: direct execution puts `scripts/` rather than the repository
root on `sys.path`, so the script's `from scripts.*` absolute imports would
raise `ModuleNotFoundError` before argument parsing even started. Output
paths are concrete, real paths under `cron_tracking/`, never a bracketed
`<placeholder>` -- a shell parses `<name>` as input/output redirection and
fails before Python runs at all.

**Known scheduling gap:** a 5-field cron expression has no year field, so
`30 20 27 7 *` recurs every July 27, not only in 2026, unless the external
scheduler independently retires this entry after it fires once. Two
independent script-side guards close the re-fire hole:

1. `generate_report()` content-gates to `trading_day='2026-07-27'` and raises
   for any other value.
2. Because the registered command **hardcodes** `--trading-day 2026-07-27`,
   guard 1 alone could never fire on a 2027 re-run: the hardcoded argument
   satisfies it, and the run would re-read the 2026 artifact tree and
   overwrite the 2026 outputs. `_main()` therefore also compares the **actual**
   America/New_York execution date -- the one input a hardcoded command cannot
   pin -- and refuses with exit code 2, writing nothing, when its calendar
   year differs from the trading day's. The comparison is by year, not exact
   date, so a legitimate same-year manual re-run still works. A deliberate
   later replay must pass `--allow-execution-date-mismatch` explicitly; the
   scheduler entry never does.

The scheduler-side one-shot behavior still needs either a scheduler-native
one-time trigger or a manual retirement step after 2026-07-27, but an
unretired entry now **fails closed** instead of republishing over the real
report.

## Why proxies and not credits

Exact Perplexity credit totals are **not exposed** to this repo or to the
scheduled tasks. The report therefore summarizes the day with **measurable
proxy metrics** and **low / moderate / high bands**.

The report must never print an exact credit count, an estimated credit count, a
dollar cost, or any billing figure, and must not imply one by arithmetic. This
matches the existing rule for the monthly cost review `bec4a6ee` in
[cost_tracking.md](cost_tracking.md).

## Required sections, in order

1. `scheduled_runs`
2. `recovery_and_escalation_runs`
3. `usage_counts`
4. `proxy_bands`
5. `biggest_usage_drivers`
6. `caveats`

### 1. Scheduled runs

One row per scheduled run of the active daily chain, **per ET slot**:

| ET | Task | ID |
|---|---|---|
| 9:35 AM | Market-open DCA live Robinhood execution | `2ba19526` |
| 9:45 AM | Morning DCA pre-flight validation (writes the artifact 42173154's gate requires) | `bb4f6ef1` (preflight) |
| 9:45 AM | Morning DCA watchlist early-warning scan | `bb4f6ef1` (watchlist) |
| 10:30 AM | Morning DCA live Robinhood execution | `42173154` |
| 11:30 AM | Intraday DCA live Robinhood execution | `7be82b97` |
| 12:30 PM | Midday DCA live Robinhood execution | `a6239dae` |
| 1:30 PM | Intraday DCA live Robinhood execution | `7be82b97` |
| 2:30 PM | Intraday DCA live Robinhood execution | `7be82b97` |
| 3:30 PM | Final DCA live Robinhood execution | `10ce362c` |
| 3:35 PM | Post-trade slippage plus Notion latest-run sync | `67aa802b` |
| 3:40 PM | DCA live-run watchdog | `a46bf007` |

Rules:

- All eight task ids must appear **even when a task did not fire**, was a no-op,
  or failed to start. A task with no run is reported explicitly as
  `did_not_run` with the reason, never omitted.
- `7be82b97` is broken out **per ET slot** (11:30, 1:30, 2:30). It is one task
  with three slots and must not be collapsed to a single line.
- `bb4f6ef1` fires **two distinct executions** at the same 9:45 AM slot: the
  manifest requires it to invoke `MorningDCAExecutor` once in preflight mode
  and once in watchlist mode, each producing its own run ID, artifact
  directory, and model-usage row (see `stream_note` on
  `model_usage_log.applies_to_daily_tasks_et` in
  `config/scheduled_tasks.json`). List both executions separately even though
  they share a scheduler ID and ET slot; do not merge them into one row or
  omit one execution.
- `e3e6f09e` is **paused**, so it is reported as paused rather than as missing.

### 2. Recovery and escalation runs

This is its **own section**. Recovery runs are **never folded into**
`scheduled_runs`, and their counts are reported separately rather than summed
into the scheduled totals.

Counting only normal scheduled runs is a **contract violation**. Every one of
the following artifact types is searched for explicitly on the current trading
day:

| Type | |
|---|---|
| `same_day_recovery_run` | `parent_recovery_run` |
| `manual_recovery_artifact` | `subagent_escalation` |
| `manual_rerun` | `failed_scheduled_start` |
| `scheduled_recovery_attempt` | `retry` |
| `order_placement_recovery_artifact` | `repair_artifact` |
| `missed_run_recovery_artifact` | |

A type with nothing found is reported as **zero, not omitted**, so a silent
miss is distinguishable from a real zero. `order_placement_recovery_artifact`
covers any re-submission, repair, or reconciliation artifact touching
`submitted_orders.csv`, and is read only: this report never reviews or submits
an order.

`missed_run_recovery_artifact` requires wording that identifies a missed
**run or slot** (e.g. "missed run", "the slot missed", "did not fire", "never
ran"), not just any use of the word "missed" -- a recorded reason like "missed
fill for AAPL" or "reconcile missed order" describes a missed order/fill on a
slot that DID fire, and is `manual_recovery_artifact`/`manual_rerun` instead.

### Recovery context only label

Every recovery, manual recovery, manual rerun, and missed-run recovery artifact
is labeled **`recovery context only`**. The label is mandatory and appears on
the artifact row itself, not only in section prose. Such an artifact is never
presented as valid scheduled evidence for any trading task, and no completeness
of its artifact set can promote it.

### Recovery counts by stream

The recovery section reports recovery run counts **broken down by stream**,
keyed by the same canonical names `per_run_field_rules.stream` declares:
`market_open`, `preflight`, `watchlist`, `morning`, `intraday`, `midday`,
`afternoon`, `slippage`, `watchdog`, `recovery`. A stream with no recovery
activity is reported as `0`. The per-stream counts must sum to
`recovery_and_escalation_count`, **not** `run_count_recovery` (see below).

This rule previously named `post_trade`, contradicting the `slippage` every
other part of the contract uses, so the promised key never appeared in a
generated report -- a consumer following this rule could not distinguish an
omitted stream from a confirmed zero. `preflight` and `recovery` are listed
because the report emits a key for each: `preflight` is `bb4f6ef1`'s
separately-logged sub-invocation, and `recovery` is the fallback bucket for a
manual artifact whose scanned root maps to no chain stream.

### Two recovery counters, defined

`recovery_and_escalation_count` is the total number of **rows** in
`recovery_and_escalation_runs`: one row per distinct artifact found across
every type in the recovery-artifact-types table above (a type with nothing
found still contributes `0`, never omitted).

`run_count_recovery` is the **subset** of those rows that represent an actual
additional execution with its own run ID. Every type counts toward it
**except** `failed_scheduled_start` (by definition zero successful
execution) and `subagent_escalation` (a delegation event, not the executor's
own additional run).

**Deduplication:** a single underlying artifact is one row, filed under
whichever type label is most specific to it (e.g. a recovery rerun that is
also a retry is filed once, as `scheduled_recovery_attempt`, not listed again
under `retry`) -- it is never counted twice into either counter for matching
more than one type description.

A recovery run **never upgrades its own classification beyond what its own
source artifact actually records.** A manual recovery's classification comes
from its source artifact, not from being a recovery: `missed_morning_run_recovery_policy`
and `midday_dca_manual_recovery_2026_07_20` both stamp
`same_day_top_up_context_only=true` and `valid_scheduled_evidence=false` on
their artifacts, so those runs are `top_up_context_only`, matching the
source, not `manual_context_only`. `manual_context_only` is reserved for a
manual run whose source artifact is **not** marked
`same_day_top_up_context_only=true` (e.g. a manual dry run or diagnostic that
participates in no same-day guard).

**A successful in-place scheduled recovery is different from a separate
recovery run.** `failure_recovery_policy` reuses the SAME logical scheduled
run directory, stamps `recovery_rerun=true`, and keeps `run_origin='scheduled'`
(never reclassified as manual). When it succeeds, the resulting artifact IS
the scheduled slot's own `valid_scheduled_evidence`, so it is reported once,
as the substantive row in `scheduled_runs`, and counts toward
`run_count_scheduled`, not `run_count_recovery`. It is also cross-referenced
in `recovery_and_escalation_runs` (noting `recovery_rerun=true` and the
trigger reason), satisfying "every same-day recovery must appear here"; that
cross-reference counts toward `recovery_and_escalation_count` but not toward
`run_count_recovery` or `run_count_scheduled` again. `run_count_recovery` is
reserved for a recovery that represents its own separate execution with a
real run ID, distinct from an in-place retry of the same scheduled slot: every
`recovery_artifact_type` counts toward it **except** `failed_scheduled_start`
(zero successful execution by definition) and `subagent_escalation` (a
delegation event, not the executor's own additional run).

That distinction is about the row's own **classification**, not its `source`
label alone: a manual override that lands directly in a chain slot's own
`run_dir` (rather than under a separate manual-scan root) is filed under the
more specific `parent_recovery_submission`/`parent_recovery_run` label, not
`manual` -- but it still counts toward `run_count_recovery` whenever its own
classification is *not* `valid_scheduled_evidence` (e.g. the documented
parent-takes-over-after-subagent-failure shape: `run_origin='manual'` with
`parent_recovery_submission=true`, so `run_count_scheduled` never counts it
either). Only when the SAME source's own classification genuinely IS the
successful in-place recovery described above is it excluded here, to avoid
double-counting the one execution `run_count_scheduled` already counted.

**A recovery that itself fails is a failed start, not a recovery attempt.**
`recovery_rerun=true` alongside `run_classification.valid_scheduled_evidence=false`
is the documented shape when the bounded recovery does not produce valid
evidence -- the scheduled slot's own classification is
`failed_start_no_evidence`. That row is filed under `failed_scheduled_start`
(`source='failed_start_escalation'`) like any other failed start, not under
`scheduled_recovery_attempt`, so it counts toward `error_count`,
`failed_start_escalation_count`, and `error_load`'s mandatory high override;
it still counts toward `retry_count`, since a failed retry is still an
observed retry.

A scheduled run that was **attempted** and failed before writing any artifact
(`run_id` = `no_run_id_written`) is classified `failed_start_no_evidence`: it
is neither `valid_scheduled_evidence` (no artifact exists) nor
`top_up_context_only` or `manual_context_only` (it is not a top-up input or a
manual run), and is recorded here, in the recovery section, not in
`scheduled_runs`.

A scheduled slot that **never fired at all** (no attempt was made, as distinct
from an attempted-and-failed start) is classified `did_not_run`, with `run_id`
= `not_applicable_did_not_run` plus the concrete reason in
`triggering_issue`. It is recorded in `scheduled_runs` (every one of the eight
active ids must appear even with no run) and does not count toward
`recovery_and_escalation_count` or `error_count`.

**Attempted-and-failed starts appear in both sections, but only one is
substantive.** `scheduled_runs` must still list the id (every one of the
eight must appear); `recovery_and_escalation_runs` must hold the only real
`failed_start_no_evidence` record. Resolve this by making the
`scheduled_runs` row a **cross-reference only**: `classification` =
`failed_start_no_evidence`, `run_id` = `no_run_id_written`,
`triggering_issue` = `"see recovery_and_escalation_runs"`, and every other
per-run field this contract otherwise requires (`artifacts_written`,
`order_attempts`, `connector_calls`, `model_usage_rows`) set to the literal
string `"cross_reference_only"` rather than a real measurement -- `stream` is
the one exception, always populated so the row still identifies which slot
it cross-references. This row is counted exactly once, from its real record
in the recovery section (`run_count_scheduled`, `recovery_and_escalation_count`,
and `error_count` are not double-incremented by the cross-reference row).

### Distinguishing a failed start from a missed slot

An empty run directory alone cannot tell "never triggered" apart from
"triggered and crashed before writing anything" -- Slack alerts and heartbeat
pings (`dca_alerts.py`) are external, fire-and-forget calls with no local
trace. `dca_alerts.guarded_run` now closes this for the streams it wraps: on
an escaping exception it calls `dca_alerts.write_failed_start_marker(stream,
exc)`, persisting a durable local record under `attempt_log/YYYYMMDD/<stream>.json`
(see Data sources). The report reads that marker **first**: if a same-day
marker exists for a slot with no run directory, classify
`failed_start_no_evidence` (using the marker's `error_class`/`error_message`
as `triggering_issue`); only when neither a run directory nor a marker exists
is the slot `did_not_run`.

**Failure phase.** Not every escaping exception is a failed *start*. A
scheduled run can persist a valid `run_state.json` and only then raise -- the
supported `generate_full_reports=True` path fails after `run_daily_compact()`
has already written the run. Each marker therefore records a `failure_phase`:
`before_run_artifact` (the genuine failed start this marker was built for) or
`after_run_artifact` (an ordinary late error on a run that completed its
artifact). The report classifies only the former as
`failed_start_no_evidence`; a late-phase marker becomes that run's own
`recorded_error`, so it still counts toward `error_count` without inventing
an earlier failed start plus a successful retry -- which previously inflated
the recovery and error counts and forced `error_load` high for a run that had
done its work. Every production `guarded_run` call site (afternoon, and
morning/midday which share `MorningDCAExecutor.run()`) passes a
`run_artifact_written` probe so this phase is actually observed rather than
defaulting to `before_run_artifact` unconditionally. The probe itself
snapshots the artifact's mtime (or absence) BEFORE the invocation runs and
compares against that snapshot when the exception escapes, rather than a bare
existence check: a retry can reuse a slot's directory after an earlier
invocation already wrote `run_state.json` there, and an existence-only probe
would credit that stale file to a later invocation that crashed before
writing anything of its own. A marker carrying no `failure_phase` at all
(written before the field existed) is read as a failed start, preserving the
prior behavior
for existing artifacts.

A run can fail in a later phase **more than once** (report generation fails,
the scheduler retries, and it fails again before eventually succeeding),
leaving multiple `after_run_artifact` markers for the same slot. The
scheduled row's own `recorded_error` reflects only the **latest** attempt;
every earlier one is filed as its own `recovery_and_escalation_runs` row
(`source='late_error_escalation'`, `recovery_artifact_type='retry'`) so it
still reaches `error_count` and `retry_count` -- never as
`failed_start_escalation`, since an `after_run_artifact` marker is by
definition not a failed start, and that source is reserved for
`error_load`'s mandatory `any_failed_start` override.

The append-only `<stream>.attempts.jsonl` history can itself have a
corrupted or truncated line. A truncated **last** line is still recoverable
from the single `<stream>.json` marker (see above), but a malformed line
strictly **between** two valid ones has no fallback at all -- if it recorded
an earlier attempt, the surviving markers alone understate that stream's
true recovery/error/retry counts. Rather than presenting that partial
history as complete, the report discloses it in `caveats` by stream name.

**Coverage:** `guarded_run` wraps the in-repo Python entrypoints for
`42173154` (morning), `a6239dae` (midday, inherits
`MorningDCAExecutor.run()`), and `10ce362c` (afternoon, via `run_dca.py`).
`2ba19526`, `bb4f6ef1` (both sub-invocations), and `7be82b97` have no in-repo
executor -- the external scheduler/agent invokes them directly, so no in-repo
code exists to write a marker for them -- and `67aa802b`/`a46bf007` are
read-only consumers of other tasks' artifacts. For those five, an
attempted-and-crashed invocation genuinely remains indistinguishable from a
missed slot from this repo's own artifacts; the report states this
limitation explicitly in `caveats` for any of those five classified
`did_not_run`, rather than silently assuming a clean miss.

### Per-run fields (both sections)

| Field | Rule |
|---|---|
| `run_id` | Exact id as written in the artifact. If the run failed before writing anything, record `no_run_id_written` plus the id it would have used. |
| `stream` | One of `market_open`, `watchlist`, `preflight`, `morning`, `intraday`, `midday`, `afternoon`, `slippage`, `watchdog`, `recovery`. `slippage` (not `post_trade`) matches the value the 3:35 PM `67aa802b` task's own model-usage directory entry and pointer-validation policy already persist. `preflight` matches `bb4f6ef1`'s separately-logged pre-flight sub-invocation. |
| `triggering_issue` | `scheduled` for scheduled runs. For everything in the recovery section, the concrete failure or gap that caused the run, not a generic label. |
| `artifacts_written` | Files actually persisted for the run. |
| `order_attempts` | Count of order placement attempts recorded in the artifact, with fills if recorded. Read from persisted artifacts only. |
| `connector_calls` | Connector or provider pulls **only where logged**. Where not logged, write `not_logged` rather than estimating. |
| `model_usage_rows` | Appended rows for this run in `model_usage/YYYYMM/model_usage.jsonl`, but only written as a count when at least one row matches this run's exact `run_id` -- including a `run_id` recorded only inside a chain slot's own nested `run_classification`, not just the top level. If NO row matches -- whether the file is absent, or present but has no row for this run -- write `not_available` with a reason, never `0`. `log_model_usage()` is deliberately best-effort and silently swallows its own append failures, so a missing row never proves zero usage; treating it as `0` could hide a premium-routed run and understate `model_load`. A monthly log where any line is malformed, non-object, missing its own `run_id`, or carries a `model_routing_mode` outside the recognized enum is treated as wholly unavailable rather than partially matched, since that line could itself be the dropped entry this run's `run_id` was meant to match. |
| `classification` | Exactly one of `valid_scheduled_evidence`, `top_up_context_only`, `manual_context_only`, `failed_start_no_evidence`, `did_not_run`. |

`order_attempts` is read from persisted artifacts. This report **never calls a
broker and never reviews orders**.

The order artifact is looked up under each of this repo's documented names in
turn -- `submitted_orders.csv`, then `filled_orders.csv`, then `orders.csv`
(mirroring `scripts/drift_detection.ORDER_CSV_NAMES`) -- because a manual
override or recovery under `midday_dca/manual_reruns` records its attempts in
`filled_orders.csv` rather than `submitted_orders.csv`. Manual rows feed
`placed_order_count`, so looking only for the submitted-order file reported
`not_logged` for a fully recorded recovery and made the whole aggregate
unavailable. The orphan-repair sweep discovers directories by the same set of
names, so such a recovery is found even with no `run_state.json`.

With no readable order CSV at all, a run whose `order_submit_status` is
`not_submitted_no_candidates`, `dry_run`, or `not_submitted_budget_exhausted`
still reports `0`: that status is the run's own persisted proof it attempted
nothing, which is a measured zero rather than an unmeasured gap (morning and
midday write no `submitted_orders.csv` when there was nothing to submit).
`not_submitted_error` and `not_submitted_confirmation_required` are
deliberately excluded -- an attempt may have been made and then failed or been
blocked mid-flight -- and stay `not_logged`. A persisted CSV always wins over
the status field.

`submitted_orders.csv` must also carry its documented header
(`ticker,order_id,submitted_amount_usd,status`) to be trusted as a row count.
`scripts/scheduled_artifacts.py`'s own artifact validator treats a zero-byte or
truncated-before-the-header file as **corrupt**, not as a valid zero-order
file -- the header is what proves the writer actually ran -- so a missing or
malformed header reports `not_logged` rather than a measured `0`.

A manual recovery's own `run_classification.stream`, when present, is used
only if it is one of this report's canonical stream names; a producer-specific
alias (e.g. `midday_manual`) falls back to the stream inferred from the
scanned root instead, so `recovery_run_count_by_stream` never gains an
unexpected key outside the canonical set.

A consumer-only role (`67aa802b`, `a46bf007`) counts as having succeeded only
when its logged `artifact_path` yields the **complete** output set its own
producer documents. `dca_reporting.generate_reports()` writes all four
post-trade files (`slippage_ranked.csv`, `missing_fills_report.csv`,
`slippage_and_coverage_report.md`, `latest_run_summary.json`) and appends its
`model_usage.jsonl` row from a `finally` block, so an invocation that wrote
its first report and then raised still logs a row -- accepting any single
current-day file let that failure pass as a clean run. `a46bf007` has no
comparable documented set, so any current-day file still suffices for it.

Each invocation is evaluated separately, and the scheduled row reports only
the **succeeding** invocations' `model_usage_rows`. Every failed attempt that
preceded a success already gets its own recovery-section row carrying
`model_usage_rows=1`, so counting all matches on the scheduled row billed the
failed attempt's log entry twice.

A retry commonly reuses the **same** `output_dir` as the attempt before it, so
two invocations can log an identical `artifact_path`. Evaluating each row only
against its own artifact path cannot then tell a later failed retry from the
earlier successful attempt that already wrote the complete set into that
shared directory: a file's presence alone does not say which invocation
produced it. When an output path repeats, only files modified strictly after
the previous invocation sharing that path's own completion count toward the
later invocation's own completeness -- a failed retry that left the directory
untouched is correctly excluded, while a genuine later rewrite is still
credited.

`10ce362c` (afternoon) requires the **canonical** `run_summary.json`
specifically, not the `run_summary.json`/`latest_run_summary.json` any-of
group other streams accept: `near_close_dca.py`'s own artifact check
(`validate_run_artifacts(..., require_summary=True)`) rejects
`latest_run_summary.json` (the separate post-trade/consumer report) as a
substitute, since the canonical file must mirror the run state's own
classification.

### 3. Usage counts

`run_count_scheduled`, `run_count_recovery`, `no_op_count`,
`placed_order_count`, `recovery_and_escalation_count`, `notification_count`,
`notion_updates`, `drive_updates`, `model_usage_jsonl_row_count`,
`artifact_count`, `artifact_bytes_approx`, `connector_pull_count_where_logged`,
`error_count`, `retry_count`, `recovery_run_count_by_stream`,
`recovery_count_by_artifact_type`,
`connector_retry_count_where_logged`, `connector_error_count_where_logged`.

`recovery_count_by_artifact_type` reports **every** type in
`recovery_artifact_types`, with `0` for a type nothing was found under, so a
real zero is distinguishable from a type the generator never searched. Each
recovery row is filed under exactly one type -- the most specific its own
evidence supports -- so the mapping sums to `recovery_and_escalation_count`.

`notification_count` is summed from each run's persisted
`usage_proxy.notifications_sent` (written by `near_close_dca.build_usage_proxy()`)
over exactly the runs that recorded it. That field is Optional and defaults to
`None`, so a run that did not record it is out of scope rather than a `0`; only
when no covered run recorded one is the counter `not_logged`.

Recovery rows are filed under the **most specific** artifact type their own
evidence supports. The flags are not exclusive -- the documented parent-recovery
shape (`intraday_dca_scheduled_recovery_2026_07_24`) sets `recovery_rerun=true`
and `parent_recovery_submission=true` on the same `run_state` -- so
`parent_recovery_submission` and `recovery_from_escalation` are both checked
ahead of the generic rerun flag. A row filed under the more specific type still
counts toward `retry_count` when its `run_state` carried `recovery_rerun`: the
flag is tracked separately from the type label, so classifying it more precisely
never drops the retry.

`model_usage_jsonl_row_count` is `not_available` whenever the monthly
`model_usage.jsonl` itself is absent or malformed -- never a sum of the
`did_not_run` placeholders' definitional zeroes. Those zeroes are honest only
when the log **exists** and simply carries no row for a slot that never ran;
with no readable source at all, summing them emitted `0` while this report's
own caveat simultaneously said "reported `not_available`, not 0", and for a
stream with no attempt marker a missing artifact cannot prove the invocation
consumed no model usage before crashing.

A `did_not_run` row's own `model_usage_rows` follows the same rule, scoped
per task: `0` **only** for the three ids whose in-repo entrypoint writes an
`attempt_log` marker (`42173154`, `a6239dae`, `10ce362c`), where
`did_not_run` means no run directory *and* no marker and so genuinely rules
out an attempted-and-crashed invocation. For the other five ids no in-repo
code writes a marker at all, so the same evidence cannot distinguish a slot
that never fired from one that crashed after consuming model usage -- they
report `not_available`, matching the caveat this report already emits for
them. Hardcoding `0` there asserted a measurement the caveat simultaneously
disclaimed, and could hold `model_load` at low despite genuinely unknown
usage.

`retry_count` counts two distinct shapes of retry: a run that stamped
`recovery_rerun=true` on its own `run_state`, and an **evidenced** retry -- a
crash that left an `attempt_log` marker followed by a later invocation that
did write the run directory. An external-scheduler retry has no in-repo code
path to stamp `recovery_rerun`, so counting only the flag reported zero for
exactly that case.

Every counter is derived from persisted same-day artifacts and logs. A counter
that cannot be measured is reported as `not_logged` or `not_available` with a
one-line reason. Counters are **never** estimated, inferred, or backfilled from
expectation. `artifact_bytes_approx` is explicitly approximate and rounded, for
example to the nearest KB.

### 4. Proxy bands

Bands are `low`, `moderate`, `high`, or `not_available`. Each reported band
names the dimension, the observed proxy value, and the threshold it crossed,
so the band is reproducible from the artifacts rather than asserted. If the
counter(s) a dimension depends on are `not_logged` or `not_available` for any
covered run, that dimension's band is `not_available` with the same reason:
never guessed, defaulted to `low`, or omitted. (This applies in particular to
`model_load` whenever `model_usage/YYYYMM/model_usage.jsonl` itself is
absent.)

For `artifact_load`, evaluate the three predicates in this fixed order and
stop at the first match: **high**, then **moderate**, then **low**. They are
mutually exclusive by construction, so a measurement never matches more than
one.

| Dimension | Proxy | low | moderate | high |
|---|---|---|---|---|
| `run_volume` | scheduled plus recovery runs, combined total | total at or below 11 runs (the 11-execution scheduled baseline: eight active ids, `bb4f6ef1` firing twice -- preflight and watchlist -- `7be82b97` x3 slots, `e3e6f09e` paused/no slot; any mix of scheduled and recovery runs at or below 11 total is low) | total 12 to 17 | total 18 or more |
| `recovery_load` | `recovery_and_escalation_count` | 0 | 1 to 2 | 3 or more |
| `connector_load` | `connector_pull_count_where_logged` | at or below 50 (routine per-run pulls across the 10-run baseline) | 51 to 90 | 91 or more |
| `model_load` | count of premium-routed rows (`model_routing_mode='premium'`) in `model_usage_jsonl` | 0 premium rows | exactly 1 premium row (with a recorded reason) | 2 or more premium rows |
| `artifact_load` | `artifact_count`, `artifact_bytes_approx`, `retry_count` | not high and not moderate: `artifact_count` <= 50 AND `artifact_bytes_approx` <= 2 MB | not high: `artifact_count` 51-80, OR `artifact_bytes_approx` > 2 MB and <= 5 MB | `artifact_count` > 80, OR `artifact_bytes_approx` > 5 MB, OR `retry_count` >= 2 (checked first, overrides the others) |
| `error_load` | `error_count` plus `retry_count` | not high and not moderate: 0 | not high: 1 to 3 | 4 or more, OR any failed scheduled start (checked first, overrides the others) |

`connector_load` bands are derived solely from the `connector_pull_count_where_logged`
count; this report's per-run fields don't capture call scope or provider
identity, so a qualitative cause (a full-universe refetch, a provider
failover) belongs in `biggest_usage_drivers` or `caveats` as narrative
context, not as a band determinant.

That count is read from each run's own persisted
`usage_proxy.finance_calls_attempted` plus `usage_proxy.robinhood_calls_attempted`
(written by `near_close_dca.build_usage_proxy()`), summed over exactly the
runs that recorded them -- which is what `_where_logged` scopes the counter
to, so a run without that telemetry is out of scope rather than an unknown
that voids the total. Only when **no** covered run persisted a connector
count is the counter `not_logged` and the band `not_available`; a fabricated
`0`/`low` is never emitted. `connector_retry_count_where_logged` and
`connector_error_count_where_logged` remain `not_logged`: `usage_proxy`
records attempted calls and an overall `errors_count`, neither of which is a
connector-specific retry or error count.

`model_load`'s prior high trigger, "sustained escalation," named no count,
duration, or persisted field and could select high with fewer than 2 premium
rows; the band is now a pure count of premium rows.

`artifact_load`'s prior high trigger, "any single artifact path rewritten 2+
times," isn't observable from a persisted artifact: this contract defines no
write-history log, and reading only final file contents/mtimes can't tell a
once-written file from a rewritten one with identical final content.
`retry_count` is the one already-persisted counter that reflects repeated
rewrites in practice (a retry reuses and rewrites the same run directory),
so it replaces that predicate.

`error_load` now states explicit precedence (high, then moderate, then low)
matching `artifact_load`, since "any failed scheduled start" could otherwise
select high even when the count alone (e.g. 1) would read as moderate.

### 5. Biggest usage drivers

Ranked list of what consumed the most work today, each with the proxy evidence
behind it. Ranking weighs order attempts, premium model routing, logged
connector pulls, recovery/recorded-error activity, and artifact volume, in
that order -- a run with a large logged `connector_calls` count but no orders
and no premium routing still ranks on that pull count, since
`connector_pull_count_where_logged` is one of this report's own measured
proxy dimensions.

### 6. Caveats

Restates that credits are not exposed and that all figures are proxies, and
lists any metric that could not be measured.

## Data sources

- cron_tracking artifact roots for the 2026-07-27 trading day:
  `run_state.json`, `run_summary.json`, `signals.csv`, `submitted_orders.csv`
- raw connector response captures under each run's `raw/` directory
- notification records emitted today (Slack drift alerts, watchdog alerts,
  Notion latest-run sync)
- `model_usage/202607/model_usage.jsonl`
- `attempt_log/20260727/<stream>.attempts.jsonl` -- the append-only history of
  **every** failed start `dca_alerts.guarded_run` recorded for that stream
  today, one JSON object per line, read in preference to the single marker
  below. A stream can fail more than once on a trading day, and
  `<stream>.json` is a single fixed path each failure overwrites, so reading
  only that undercounted repeated failures
- `attempt_log/20260727/<stream>.json` -- the latest failed-start marker for
  that stream (see "Distinguishing a failed start from a missed slot" above);
  still written for existing readers, and used as the fallback when no history
  file exists. Read before concluding a task with no run directory
  `did_not_run`. The `attempt_log` root itself honors the `DCA_ATTEMPT_LOG_ROOT`
  environment override the same way `dca_alerts.write_failed_start_marker`
  does -- a deployment that points that variable somewhere other than
  `<cron_tracking_root>/attempt_log` is still read from correctly, not just
  written to
- `latest_scheduled_run.json` and same-day pointers, **read only**
- recovery, manual rerun, missed-run recovery, and repair artifact roots for
  2026-07-27

If a source is absent for 2026-07-27, the report says so explicitly in
`caveats` and reports the affected metric as `not_available`. It never
substitutes a prior day, never reuses the 2026-07-24 2:30 PM intraday recovery
as if it were today, and never fabricates a run.

## Output mode: compact and cost-conscious

The report reads **local artifacts already on disk** plus the model usage log.
It performs no network fetch, no connector pull, and no broker call of its own,
so producing the report adds negligible usage on top of the day it measures.

Default output is **compact**: tables and counts. Long narrative prose is
**suppressed unless there is an actionable issue**. When nothing is actionable
the report emits the tables plus a single line stating that no actionable
issues were found.

An issue is actionable when any of these hold:

- any failed scheduled start
- any recovery, manual rerun, or repair artifact present today
- any connector error or retry
- any `error_count` or `retry_count` above zero
- any required artifact missing for a task that was expected to run
- any proxy band evaluating to `high`

When actionable, narrative is written **only for the specific issues found**,
not for the whole day.

## Relationship to Friday heavy reporting

This one-time 4:30 PM ET report is **separate from** the Friday 5:00 PM ET
weekly heavy reporting task `041d9d90`. It does not replace it, does not
satisfy it, does not consume its budget, and does not write to its artifacts or
its Notion dashboard. 2026-07-27 is a **Monday**, so `041d9d90` does not run
today and there is no overlap.

## No secrets

The report never emits a secret. No raw Robinhood account number, no API key,
no token, no webhook URL, and no credential may appear in the report or in any
artifact it writes, even if present in a source artifact it reads. Account
numbers follow the obfuscation rule below; every other secret class is
**omitted entirely rather than masked**. Environment variable *names* may be
referenced, never their values.

## Account-number obfuscation

A repository-wide search found no prior implemented account-number masking
transformation. This report defines its own concrete rule instead of
deferring to an unwritten one: if an account number appears in any source
artifact this report reads, the report shows only the **last 4 digits** and
replaces every preceding digit with `*` (example, using a **synthetic**
placeholder rather than any real account number: `123456789` becomes
`*****6789`). This applies to any account number the report would otherwise
surface in `biggest_usage_drivers` or `caveats` narrative text; the report
never prints a full, unmasked account number, and this doc never restates a
real account number for illustration.

## Preserved unchanged

Reporting only. This task changes no strategy or safety behavior. It preserves
the canonical first-buy and same-day top-up rule, the active universe and
exclusions, the provider policy, the scheduled-run artifact contract,
cost-conscious mode, and Robinhood-only execution. Account numbers appearing
in any artifact are obfuscated per the rule above.

This task adds no ticker, removes no ticker, and changes no provider ordering.
The active universe, the exclusions, and `market_data_source_policy` are
read-only inputs and are left intact.

The retired entries `b7961407`, `cd45e95b`, `d72aa525`, `d1f7a2c0`, and
`morning-dca-0945` are **not reintroduced**. In particular the 9:00 AM
pre-market buying-power dashboard `b7961407` stays removed: the report neither
expects a 9:00 AM run nor flags its absence as a missing run.
