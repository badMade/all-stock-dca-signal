# Cost Tracking

How automation cost is monitored and controlled. Machine-readable artifact
shape: [`schemas/cost_tracker.schema.json`](../schemas/cost_tracker.schema.json).

## Policy

- **Daily tasks**: compact / local-first. Avoid large narratives on quiet days
  (e.g. the volatility scan alerts only on >5% outliers).
- **Heavy analysis**: reserved for **Fridays and monthly** refreshes.
- **Manual full runs**: avoid unless automation fails.

## Cost-saving operating mode (requested Thu Jul 16 2026)

Machine-readable source of truth:
[`config/scheduled_tasks.json`](../config/scheduled_tasks.json) → `operating_mode`
(mode `cost_saving_daily_compact`). Weekday tasks stay **compact**; all heavy
analysis is deferred to the **Friday 5:30 PM ET** weekly refresh (retimed
2026-08-02 from 5:00 PM ET). This mode
changes **cost/reporting behavior only** — it does **not** relax any trading
safety rule (see [safeguards.md](safeguards.md)).

### Daily compact weekday chain

| ET | Task | Role |
| --- | --- | --- |
| 9:35 AM | `2ba19526` | market-open live execution (first buy of the day) |
| 9:45 AM | `bb4f6ef1` | watchlist early-warning scan (read-only) |
| 10:30 AM | `42173154` | morning live execution |
| 11:30 AM | `7be82b97` | intraday live execution |
| 12:30 PM | `a6239dae` | midday live execution |
| 1:30 PM | `7be82b97` | intraday live execution |
| 2:30 PM | `7be82b97` | intraday live execution |
| 3:30 PM | `10ce362c` | final live execution (final buy of the day) |
| 5:00 PM | `67aa802b` | post-trade slippage + compact Notion **Latest Run** sync |

The 3:05 PM monitor-only signal check (`e3e6f09e`) and the 3:40 PM live-run
watchdog (`a46bf007`) were removed from the schedule entirely on 2026-08-02;
`67aa802b` moved the same day from 3:35 PM ET to 5:00 PM ET daily. See
`config/scheduled_tasks.json`'s `afternoon_completion_rule`.

### Daily mode rules

- Write **compact artifacts only** — machine-readable JSON/CSV, no long narrative
  summaries.
- **Suppress no-op / heartbeat notifications**; notify **only for actionable
  events**: placed orders, rejected orders, candidates capped by budget,
  missing/invalid artifacts, broker/log mismatch, data-confidence failures,
  drift-detector failures, recovery attempts/failures, and meaningful exceptions
  (see [`config/notifications.json`](../config/notifications.json) →
  `daily_compact_mode`).
- **Use drift detection instead of manual checking.** The post-artifact drift
  detector (PR #80, merge commit `3641b90`) reconciles `signals.csv` against
  `submitted_orders.csv` after every live run and flags any auto-buy/actionable
  row with no submitted order — **including rows that carry an explicit
  per-row block/rejection reason**. Blocked rows are still reported (tagged
  `per_row_guard_or_block`) rather than silently excluded, so an operator can
  confirm the block was intended. See [drift_detection.md](drift_detection.md).
- **Batch market-data pulls** across the active universe where possible; persist
  each raw connector response **once** and have downstream steps read the saved
  files rather than re-pulling (see `raw_data_persistence` on task `10ce362c`).
- **Limit Notion writes**: the compact **Latest Run** update daily; refresh the
  **full** Notion page only in the Friday weekly refresh and the monthly
  dashboard refresh (`0656657a`, first weekday of the month).
- **Cap automated retries at one targeted recovery rerun.**
- **Model routing** (see [Model routing policy](#model-routing-policy)):
  cost-conscious / default model routing where available for routine daily DCA
  execution, compact artifact writing, repo manifest/config/docs maintenance, and
  normal notification/reporting. **Do not** use ChatGPT 5.5 or other premium
  models for these.

### Friday heavy analysis (`041d9d90`, 5:30 PM ET)

Full Notion refresh, performance tables, slippage trend analysis, missing-fill
analysis, cost-basis / control comparisons, cost tracker update, and the full
weekly dashboard refresh. Retimed 2026-08-02 from 5:00 PM ET to clear the
collision with `67aa802b`'s new daily 5:00 PM ET slot on Fridays. Afternoon
**trading** completes by 3:30 PM ET (`10ce362c`); this task, `67aa802b`
(5:00 PM ET daily), and the month-end volatility risk refresh (`c9fb1fb8` /
`17a3b2a9`, 5:15 PM ET, retimed 2026-08-02 from its own prior 5:00 PM ET to
clear the collision with `67aa802b`'s new 5:00 PM slot) are the only
DCA-related tasks approved to run after 3:30 PM ET.

### Recovery policy

Avoid manual full reruns unless a detector, the watchdog, or a failed scheduled
run signals a real issue. Prefer **targeted recovery** of the specific failed
tickers or failed steps, capped at **one** recovery rerun.

## Model routing policy

Machine-readable source of truth:
[`config/scheduled_tasks.json`](../config/scheduled_tasks.json) →
`model_routing_policy` (reaffirmed 2026-07-20), mirrored on each daily task as
`model_routing_instruction_2026_07_20`.

All **active daily DCA scheduled tasks** use **cost-conscious / default model
routing where available**. This governs model selection and cost/reporting
behavior **only** — it does **not** relax any trading safety rule (see
[safeguards.md](safeguards.md)).

- **Do not** use ChatGPT 5.5 or any other premium/highest-tier model for:
  - routine daily DCA execution,
  - compact artifact writing (machine-readable JSON/CSV),
  - repo manifest / config / docs maintenance,
  - normal notification and reporting.
- **Premium models are allowed only for**:
  - failure recovery,
  - safety validation,
  - connector / debug escalation,
  - an explicit user request for highest quality.

### Applies to these daily tasks

| ET | Task | Role |
| --- | --- | --- |
| 9:35 AM | `2ba19526` | market-open live execution (first buy of the day) |
| 9:45 AM | `bb4f6ef1` | watchlist early-warning scan (read-only) |
| 10:30 AM | `42173154` | morning live execution |
| 11:30 AM | `7be82b97` | intraday live execution |
| 12:30 PM | `a6239dae` | midday live execution |
| 1:30 PM | `7be82b97` | intraday live execution |
| 2:30 PM | `7be82b97` | intraday live execution |
| 3:30 PM | `10ce362c` | final live execution (final buy of the day) |
| 5:00 PM | `67aa802b` | post-trade slippage + compact Notion **Latest Run** sync |

**REMOVED 2026-08-02:** the 3:05 PM monitor-only signal check (`e3e6f09e`,
already paused) and the 3:40 PM live-run watchdog (`a46bf007`) were both
removed from the schedule entirely at the user's request -- neither is a
schedule entry anymore, so neither is listed above. `67aa802b` moved the same
day from 3:35 PM ET to 5:00 PM ET daily. See
`config/scheduled_tasks.json`'s `afternoon_completion_rule` and
[`safeguards.md`](safeguards.md#operational-rules). The retired tasks
`b7961407`, `cd45e95b`, `d72aa525`, and `d1f7a2c0` are not covered either.

### Keep daily runs compact

- Write machine-readable **JSON/CSV** artifacts; no long narrative summaries.
- **Batch** market-data and broker-history pulls across the active universe.
- **Persist each raw connector response once**; downstream steps read the saved
  files instead of re-pulling.
- **Suppress no-op** notifications; notify only for actionable events.
- **Avoid** full Notion/dashboard rebuilds and broad performance reports in daily
  runs — defer to the Friday weekly and monthly refreshes.

This policy is **additive**: it preserves the **$2.00** base amount, the
**0x-to-5x** graded ladder, the **Finance-primary** read-only market-data policy,
the **GFUZ/TXN** active universe, `BYDDY`'s whole-share accumulation category, the
same-day top-up guards, all safety/circuit-breaker rules, and the
run-classification text unchanged.

### Model usage log

So model routing can be **audited over time**, every covered daily task appends
one **compact JSONL** entry recording which model / routing mode it used.

- **Source of truth:** [`config/scheduled_tasks.json`](../config/scheduled_tasks.json)
  → `model_usage_log`. **Entry schema:**
  [`schemas/model_usage_log.schema.json`](../schemas/model_usage_log.schema.json).
- **Path:** `/home/user/workspace/cron_tracking/model_usage/YYYYMM/model_usage.jsonl`
  (append-only; `YYYYMM` is the ET month of the run). If a repo-configured
  artifacts base is used instead, mirror the same
  `model_usage/YYYYMM/model_usage.jsonl` layout beneath it.
- **Each entry records:** `run_id`, `cron_id`/`task_id`, `task_name`,
  `scheduled_for_et`, `run_origin`, `stream`, `model_requested`, `model_actual`,
  `model_routing_mode` (`cost_conscious` / `default` / `premium`),
  `reason_for_premium_model` (only when premium), `triggered_at_utc`,
  `artifact_path`, and `notes`.
- **When the backend model is not exposed:** write `model_actual='not_exposed'`
  and still record `model_requested` and `model_routing_mode` from the task
  policy.
- **Premium reason:** `reason_for_premium_model` is non-null only when
  `model_routing_mode='premium'`, and must be one of `failure_recovery`,
  `safety_validation`, `connector_debug_escalation`, or `explicit_user_request`
  (see [Model routing policy](#model-routing-policy)).

Covered daily tasks (same set as the routing policy):

| ET | Task | Stream |
| --- | --- | --- |
| 9:35 AM | `2ba19526` | market_open |
| 9:45 AM | `bb4f6ef1` | watchlist |
| 10:30 AM | `42173154` | morning |
| 11:30 AM, 1:30 PM, 2:30 PM | `7be82b97` | intraday |
| 12:30 PM | `a6239dae` | midday |
| 3:30 PM | `10ce362c` | afternoon |
| 5:00 PM | `67aa802b` | slippage |

`e3e6f09e` (monitor) and `a46bf007` (watchdog) were removed from the
schedule entirely on 2026-08-02; `build_entry()`/`log_model_usage()` in
`scripts/model_usage_log.py` now raise `UnknownTaskError` for either
`task_id`, since neither ever fires again. `67aa802b` moved the same day
from 3:35 PM to 5:00 PM ET daily.

`7be82b97` fires three times a day from one cron entry, so its manifest row
carries the combined default `"11:30,13:30,14:30"`. Each run must pass its own
slot as an explicit `scheduled_for_et` override so the log records the real fire
time -- the Python `log_model_usage()`/`build_entry()` API accepts it as a
keyword argument, and the CLI accepts it as `--scheduled-for-et` (Codex review:
"Expose the intraday slot override through the logging CLI"). The manifest
keeps exactly one row per `task_id` because `_load_task_directory` keys on it.

**Writer implementation:** [`scripts/model_usage_log.py`](../scripts/model_usage_log.py)
appends the entry; every append is best-effort (never raises into or blocks
the run it reports on) and opt-in via a `model_usage_log_root` parameter that
defaults to `None`, so ad-hoc/test construction never touches the real path.

- **Enabled by default for the two executor-based tasks' normal
  configuration path:** `load_morning_config()`/`load_midday_config()`
  (`42173154`/`a6239dae`) resolve `model_usage_log_root`
  to the real production path unless `artifact_paths.model_usage_log_root`
  in `morning_dca.json`/`midday_dca.json` overrides or nulls it out. A bare
  `MorningConfig()`/`MiddayConfig()` constructed directly (tests, ad-hoc
  scripts) still defaults to `None` -- only the loader enables it.
- **Wired directly into the executor** for the tasks with an in-repo
  Python entry point: `42173154` and (via subclass)
  `a6239dae` (`MorningDCAExecutor.run` / `MiddayDCAExecutor.run` in
  `morning_dca.py`), `10ce362c` (`run_pipeline` in `run_dca.py`), and
  `67aa802b` (`generate_scheduled_reports` in `dca_reporting.py`, which
  appends even when report generation raises, and logs its own report
  directory as `artifact_path` rather than the 15:30 live run's
  `run_state.json` -- that live run's `run_id` is kept in `notes` as an
  explicit reference).
  `run_pipeline`/`generate_scheduled_reports` have no in-repo config-file
  loader (they're driven by an external scheduler that already passes many
  explicit roots, e.g. `classification_state_root`) so, matching that
  existing pattern, `model_usage_log_root` there stays caller-opt-in rather
  than defaulted.
- **CLI for the rest:** `2ba19526`, `bb4f6ef1`, and `7be82b97` have no in-repo
  executor to wire a call into, so the scheduler/agent appends their entry
  directly:
  `python -m scripts.model_usage_log --run-id <id> --task-id <id> --artifact-path <path>`
  (see `--help` for the full option list).
- **What Python cannot know:** this module has no visibility into which
  LLM/agent is actually driving a given run, so `model_actual` defaults to
  `'not_exposed'` per the rule above. Every wired call site (`MorningConfig`'s
  `model_usage_model_*` fields; `model_usage_model_*` kwargs on
  `run_pipeline`/`generate_scheduled_reports`) accepts explicit overrides for
  `model_requested`/`model_actual`/`model_routing_mode`/`reason_for_premium_model`,
  so a scheduler that DOES know it escalated to a permitted premium model
  (e.g. failure recovery) can record that accurately instead of the
  cost-conscious default; callers only override them when they
  genuinely know better (e.g. an explicit premium escalation).

The log is metadata only — it adds no narrative reporting, triggers no
notifications, and preserves all trading rules, the ticker universe, the $2 base,
top-up guards, and safety rules unchanged.

## Monthly cost review (`bec4a6ee`)

- Runs the first weekday of the month at **9:55 AM ET**.
- Reports **usage proxies** (task-run counts, heavy-analysis counts, manual
  runs, approximate tokens). **No exact credit/billing figures** — exact
  Perplexity credit consumption is not exposed, so do not claim exact credits.

## Proxy metrics

| Proxy | Meaning |
| --- | --- |
| `task_runs` | Total scheduled task executions in the period |
| `heavy_analysis_runs` | Weekly/monthly full refreshes |
| `manual_runs` | Ad-hoc manual runs (should trend to zero) |
| `approx_tokens` | Rough token usage estimate, if available |

### Proxy bands

Where a report summarizes usage intensity rather than counting it, use the
bands `low`, `moderate`, and `high`, and name the observed proxy value and the
threshold that produced the band. **Never** state an exact credit count, an
estimated credit count, or a dollar/billing figure: exact Perplexity credit
consumption is not exposed.

### One-time 2026-07-27 credit-usage proxy report

`onetime-1630-credit-usage-proxy-2026-07-27` fires once at **2026-07-27 4:30 PM
ET** to summarize the day's automation usage in proxy bands, with a separate
section for every recovery, retry, escalation, and repair run. Analytics only,
no trading. See [credit_usage_proxy_report.md](credit_usage_proxy_report.md).

### Perplexity backup target band

Operator target (2026-10-25): the seven Perplexity backup fires (09:55, 10:50, 11:50, 12:50, 13:50, 14:50, 15:45
ET) should use **at most 1,000 Perplexity credits per trading day** in total, about 140 per fire on average. This is
a ceiling the operator reads off Perplexity's own usage page. It is not a figure the repo measures or estimates, so
the proxy-band rule above still holds and no report may claim a credit count against it. The lever is the early
stand-down gate in `docs/adr-plan-first-backup-executor.md` ("Amendment: stand down before the broker pull").

**2026-10-30 update.** With that gate live, the operator measured about 2,500 credits per trading day and set a new
target of **under 200 per trading day**. Most of the cost is the stand-down path itself: every fire still lists devices
and runs a Mac sub-agent just to learn that Claude's slot succeeded. The plan:

1. **Measure.** The operator records each fire's credits, read off Perplexity's usage page, in a local ledger. The
   ledger lives under the gitignored cron root, never in this repo, so the proxy-band rule above still holds for
   every generated report:

   ```bash
   python3 -m scripts.backup_cost_ledger add --day 2026-10-29 --task 4412c3c4 --credits 312 --status stood_down_local_active
   # STEP A of the short signal-first prompt stood down on slot_status.json (no state record is written):
   python3 -m scripts.backup_cost_ledger add --day 2026-11-03 --task 4412c3c4 --credits 10 --status stood_down_signal
   python3 -m scripts.backup_cost_ledger report --since 2026-10-27
   python3 -m scripts.backup_cost_ledger report --split 2026-11-02   # before vs from that day on
   ```

2. **Publish a "slot finished" signal.** `scripts/slot_status.py` is written by the Gate 9 finalizer
   (`scripts/finalize_run_evidence.py`), which every live prompt calls on every outcome. Until 2026-11-03 only
   `local_heartbeat start` and `finish` wrote it, and no live prompt calls either, so on 2026-11-02 it never updated.
   - Succeeded: orders placed, no orders (a safety check said do not buy), nothing to buy, or budget used up.
   - Failed: an error, unreachable placement, or a confirmation request (no order went in).
   - Nothing posted: a dry run, or a call the finalizer refused.
   - The Mac dispatcher reads the same local document before its heartbeat probe: a slot marked `succeeded` today
     sends no backup.
   - Where: one small public document, `slot_status.json`, force-pushed to the data-only `slot-status` branch of
     the separate PUBLIC repository `badMade/all-stock-dca-signal` (`config/slot_status.json`). This repository is
     private, so Perplexity cannot read a file here (found 2026-10-31). One-time Mac setup: create that repository,
     then run `git remote add signal https://github.com/badMade/all-stock-dca-signal.git` in the checkout.
   - Content: only the day, whether the market is open, and each slot's status and time.
   - Failure: publishing is best-effort and never fails the primary run.
   - Closed days: run `python3 -m scripts.slot_status market-day` once each morning to publish a holiday as closed.
   - Local copy: `python3 -m scripts.slot_status show` prints it.
   - Tests: set `DCA_DISABLE_SLOT_STATUS_PUBLISH` so nothing is pushed (`conftest.py` does).
3. **A short backup prompt (generated 2026-10-30; live only after a repaste).** Each pasted prompt is now about
   3,000 characters instead of about 12,000.
   - STEP A reads `slot_status.json` once and ends the fire on a slot marked `succeeded` today or a closed market,
     with no other tool call, no file and no notification.
   - Anything else (in progress, failed, missing, stale) goes to STEP B: the build-stamp check, then
     `procedure --part 0` (the unchanged hard rules, calendar, step 0 and the 2026-10-25 stand-down gate), then
     parts 1-3 as before.
   - A bad signal costs credits but never skips a check or adds a buy.
   - See `docs/adr-plan-first-backup-executor.md`, "Amendment: read the slot-status signal first".
4. **Fewer fires (operator decision 2026-10-31).** Only the backups at 09:55, 12:50 and 15:45 stay scheduled
   (for the 09:35, 12:30 and 15:30 slots); the 10:50, 11:50 and 13:50/14:50 tasks are paused, not retired
   (`config/scheduled_tasks.json`, `standing_perplexity_backup_policy.amendment_2026_10_31`).
5. **Backup only on failure (step 6, `scripts/backup_dispatch.py`).** The goal is zero Perplexity credits on a normal
   day.
   - **Mac checker:** a launchd job (`deploy/launchd/com.all-stock-dca.backup-dispatch.plist.template`) checks each
     slot 15 minutes after it starts. It emails Perplexity Computer (computer@perplexity.com) only when the slot
     failed or never started: one email per slot, with the filled prompt and `backup_planner.pyz`.
   - **GitHub watcher:** `.github/workflows/backup-dispatch-watch.yml` covers days the Mac is down. It sends only
     when today's public slot-status document has no entry for the slot.
   - **Shadow first:** `config/backup_dispatch.json` starts in `shadow` mode, which reports `would_send` and sends
     nothing.
   - **Going live:** run a shadow week and one forced-failure test, set `mode` to `live`, then pause the remaining
     scheduled Perplexity tasks.
   - **Mail settings:** sender address, SMTP host, user and app password come from the `.env`
     (`docs/local_env_file.md`) or from repository secrets for the watcher.

## Levers

- Prefer compact Notion "Latest Run" updates daily; full dashboard only weekly/monthly.
- Keep no-outlier days silent.
- Reuse cached/local data instead of re-fetching whole universes.
