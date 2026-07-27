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
analysis is deferred to the **Friday 5:00 PM ET** weekly refresh. This mode
changes **cost/reporting behavior only** — it does **not** relax any trading
safety rule (see [safeguards.md](safeguards.md)).

### Daily compact weekday chain

| ET | Task | Role |
|---|---|---|
| 9:35 AM | `2ba19526` | market-open live execution (first buy of the day) |
| 9:45 AM | `bb4f6ef1` | watchlist early-warning scan (read-only) |
| 10:30 AM | `42173154` | morning live execution |
| 11:30 AM | `7be82b97` | intraday live execution |
| 12:30 PM | `a6239dae` | midday live execution |
| 1:30 PM | `7be82b97` | intraday live execution |
| 2:30 PM | `7be82b97` | intraday live execution |
| 3:30 PM | `10ce362c` | final live execution (final buy of the day) |
| 3:35 PM | `67aa802b` | post-trade slippage + compact Notion **Latest Run** sync |
| 3:40 PM | `a46bf007` | live-run watchdog |

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

### Friday heavy analysis (`041d9d90`, 5:00 PM ET)

Full Notion refresh, performance tables, slippage trend analysis, missing-fill
analysis, cost-basis / control comparisons, cost tracker update, and the full
weekly dashboard refresh. This is the only approved post-3:40 PM DCA-related
exception.

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
|---|---|---|
| 9:35 AM | `2ba19526` | market-open live execution (first buy of the day) |
| 9:45 AM | `bb4f6ef1` | watchlist early-warning scan (read-only); also runs the required pre-flight writer absorbed from `morning-dca-0945` |
| 10:30 AM | `42173154` | morning live execution |
| 11:30 AM | `7be82b97` | intraday live execution |
| 12:30 PM | `a6239dae` | midday live execution |
| 1:30 PM | `7be82b97` | intraday live execution |
| 2:30 PM | `7be82b97` | intraday live execution |
| 3:05 PM | `e3e6f09e` | monitor-only signal check (read-only, **paused**) |
| 3:30 PM | `10ce362c` | final live execution (final buy of the day) |
| 3:35 PM | `67aa802b` | post-trade slippage + compact Notion **Latest Run** sync |
| 3:40 PM | `a46bf007` | live-run watchdog |

The 3:05 PM monitor-only signal check `e3e6f09e` is `paused` and is not in the
live scheduler, so it is not listed above. The retired tasks `b7961407`,
`cd45e95b`, `d72aa525`, and `d1f7a2c0` are not covered either.

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
the **GFUZ/TXN** active universe with `BYDDY`/`VCX`/`TRLV`/`RVI` exclusions, the
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
|---|---|---|
| 9:35 AM | `2ba19526` | market_open |
| 9:45 AM | `bb4f6ef1` | watchlist |
| 10:30 AM | `42173154` | morning |
| 11:30 AM, 1:30 PM, 2:30 PM | `7be82b97` | intraday |
| 12:30 PM | `a6239dae` | midday |
| 3:05 PM | `e3e6f09e` | monitor |
| 3:30 PM | `10ce362c` | afternoon |
| 3:35 PM | `67aa802b` | slippage |
| 3:40 PM | `a46bf007` | watchdog |

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
- **CLI for the rest:** `2ba19526`, `bb4f6ef1`, `7be82b97`, `e3e6f09e`, and
  `a46bf007` have no in-repo executor to wire a call into, so the
  scheduler/agent appends their entry directly:
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
|---|---|
| `task_runs` | Total scheduled task executions in the period |
| `heavy_analysis_runs` | Weekly/monthly full refreshes |
| `manual_runs` | Ad-hoc manual runs (should trend to zero) |
| `approx_tokens` | Rough token usage estimate, if available |

## Levers

- Prefer compact Notion "Latest Run" updates daily; full dashboard only weekly/monthly.
- Keep no-outlier days silent.
- Reuse cached/local data instead of re-fetching whole universes.
