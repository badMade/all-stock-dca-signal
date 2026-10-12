# Draft fixes for DCA Issues rows that live outside this repo (2026-10-12)

**Status: DRAFT for operator review. Nothing here is applied.** Each item below is proposed
replacement or additional text for an *external* scheduled-task prompt (digest, run-log
recorder, 9:35 verifier, Friday dashboard refresh) or a session-level runbook. This repo does
not contain those prompts, so the operator pastes the accepted text into the task definition.
None of these items relax a trading safety rule, and none retries or routes around a
permission denial.

Keys are the DCA Issues `Key` values, so the triage reconciler can match this PR.

## Digest task

### `false_all_clear`
Add: "Determine each slot's outcome from `run_state.json` `order_submit_status` and the Gate 9
evidence (`python -m scripts.verify_schedule --json`), never from a task's `lastRunAt`. A task
that ran and failed has a recent `lastRunAt`. `verified` only proves the slot fired and
finalized: a `not_submitted_error` run can still be valid scheduled evidence
(`config/run_classification.json`). Report ALL CLEAR only when every due slot is `verified`
AND its `order_submit_status` is an explicitly non-failing value from that file's
`order_submit_status_values`. A failing or unrecognized value, an absent outcome field, or any
source that could not be read is listed by name and the digest says UNVERIFIED instead of
all-clear (fail closed)."

### `hardcoded_normal_cap`
Add: "Report the cap figures the executor recorded for each run (`budget.daily_cap_usd` /
`budget.weekly_cap_usd` in `run_state.json`; the runner writes the EFFECTIVE weekly cap there,
which for the Stress tier can be below the nominal figure after the rollover reserve). Use
`config/budgets.json` `budget_tiers` only to validate the recorded tier name and to flag a
mismatch, never as the number to print. Never assume the Normal tier. If no tier or cap is
recorded, print 'tier not recorded' rather than a default."

### `misattribution`
Add: "Group orders by executor (`stream`/`run_classification` in the run state) before
judging sizing. Do not attribute a sizing anomaly to a guard until the offending order is
traced to its own executor. Manual orders are excluded from scheduled-live metrics
(`docs/run_classification.md`)."

### `output_limit_workaround`
Add: "`scripts/assemble_order_pages.py` does not fetch history; it validates page files you
already captured. Fetch with `get_equity_orders`, keep each page's raw output as its own file in
fetch order, then run from the repo root: `python3 -m scripts.assemble_order_pages --account
${ROBINHOOD_AGENTIC_ACCOUNT_NUMBER} --trading-day <YYYY-MM-DD> --out <orders_pages file>
<page file 1> <page file 2> ...` (mirrors the OVERSIZED ORDER PAGES paragraph in the live
prompts). Exit 0 means the pagination chain is complete. Never transcribe rows by hand. If the tool output is refused or
truncated, abort the section with `output_cap_exceeded` and say so in the digest."

## Run-log recorder

### `recorder_transcript_unavailable`
Replace the transcript-reader step with: "Use only cloud-readable sources: the
scheduled-task list, broker order history, repo alert files, and `scripts/verify_schedule.py`
output. If a source is unavailable, write a run-log row whose Component names the source
and whose text is 'source unavailable', so an empty log can never be mistaken for a clean
day. Never leave the log silently empty."

## 9:35 verifier

### `verifier_window_mismatch`
Replace the fixed window close with: "Treat the slot's evidence as due at the slot's
acceptance-window end plus 30 minutes (`scripts/verify_schedule.GRACE_MINUTES`), i.e. use
`python -m scripts.verify_schedule --json` and read `awaiting_evidence` as not-yet-failed.
`no_evidence` and `evidence_incomplete` are EVIDENCE failures, not proof that nothing was
ordered: a run can place an order and fail before its artifacts finalize. Report them as
'evidence missing' and consult broker order history (via the assemble_order_pages workflow
below) before saying anything about whether an order was submitted. Say 'no orders in window'
only when broker history for the window is complete and empty.
Treat `verified` as 'fired and finalized', not 'succeeded': report the run's
`order_submit_status` alongside it, and flag any failing or absent outcome instead of calling
the slot healthy."

## Friday dashboard refresh

### `device_bridge_unreachable`
Add: "Run the report step only from sources reachable in the cloud. If the Mac device bridge is
unavailable, skip that step, write 'device bridge unreachable, step skipped' to the dashboard
header, and finish the remaining sections. Do not fail the whole refresh."

## Executor prompt hygiene

### `hand_reimplemented`
Add to Gate 5 of every live prompt: "Signal logic comes ONLY from the repo generator named for
this slot. If it cannot run or its data path fails, finalize as `not_submitted_error` with
the failure class and alert. Never recompute signals, sizes or candidates by hand." After
accepting, run `python -m scripts.check_live_prompt_drift` and repaste.

## Alerting

### `approval_required`
Proposed: for scheduled (unattended) runs, deliver alerts through the channels that work
without interaction (the repo alert file and the in-app message center; Slack where its webhook
env var is set) and do not depend on the Gmail draft/send path. Keep email as an
operator-facing optional channel. This changes `config/notifications.json` channel policy,
so it needs an explicit operator decision before any config edit; at least one
unattended channel must always stay enabled.

## Session-side rows (runbook text only)

### `real_world_transactions_denied`, `session_refusal`, `missing_connector`
These are decisions made by the session, permission layer or connector set, not repo bugs.
Proposed runbook language for every live prompt, already consistent with the abort-and-alert rule:
"If the session refuses, a transaction is denied, or a required connector is missing, stop
placing orders, finalize as `not_submitted_error` with the matching error class, and alert on
all Gate 9 channels. Do not retry the denied action, rephrase it, or place it through another
route. Missing Finance connector: follow the documented single-source policy only as
`config/scheduled_tasks.json` `market_data_source_policy` allows for that slot (conditions
and per-slot overrides; mirrored in `config/budgets.json`). Do not improvise a fallback."
