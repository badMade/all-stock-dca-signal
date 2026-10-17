# Fix handoff: open DCA Issues rows (2026-10-17)

Written by the DCA triage routine session at the operator's request, as the hand-off for a Claude Code agent
that will implement fixes. **Proposal only.** The routine itself changed no code and set no Approval.

## Gate: read before doing anything
- Start work on a Key **only if its Notion row has Approval exactly `Approved`.** At the time of writing, all 19
  rows below are `Proposed` and none is approved. Re-read the row first; this file may be stale.
- Every root cause below is a **HYPOTHESIS** from the routine's plan, not a verified finding. Reproduce with the failing test first.
- Do not retry, rephrase or bypass any permission denial or safety gate. Fail closed.
- One draft PR per Key (or per tightly coupled group where noted). Name the Key in the PR body so
  `selfheal/triage_reconcile.py` can match it. Open PRs as DRAFT (CLAUDE.md).
- Follow `claude/triage-routine/FIX-BRIEF-TEMPLATE.md` constraints and acceptance: `ruff check .`, targeted pytest,
  and `python run_dca.py --demo --report --now 2026-08-03T15:45 --artifacts-dir <scratch>`. Add a regression test per fix.
- Update `pyproject.toml` for any new top-level module or config file. No absolute paths, no account literals.

## Status context
- 12 rows read "Merged, checks green" only because they are named in the body of PR #1642, which is documentation-only
  (`claude/triage-routine/EXTERNAL-PROMPT-FIXES-2026-10-12.md` plus the brief template). That is **not** a code fix.
  For those rows first check whether the fix belongs in an external prompt (paste-only) or in this repo.
- PR #1642 states that `classifier_block`, `transaction_block`, `stress_tier_undefined`, `max_orders_per_day_unenforced`,
  `duplicate_fills`, `cap_consumed`, `cap_breach` and `competing_executor` may already be covered by `effective_weekly_cap()`,
  the `MAX_ORDERS_PER_DAY` checks in all three entry points, and the plan-first backup executor (#1619/#1624).
  For these, step one is "verify on main and propose marking Resolved", not new code.
- The three entry points do not share fixes (see CLAUDE.md "Three entry points"); apply and test a cap fix in each.

## A. Reporting / digest / verifier (low risk, repo-only)
### `device_bridge_unreachable`
- Notion row: https://app.notion.com/p/3f5202e5e677813ba0edd1831b48ca99
- Severity / Class / Component: Degrades slot / Environmental / Sandbox shell
- Where the fix lives: repo
- Progress label: Merged, checks green
- Name: Weekly dashboard refresh cannot reach the Mac
- Remediation (from row): Run the weekly dashboard refresh only while the Mac is awake and connected, or move its report step to a source reachable from the cloud, because the device bridge is unavailable in scheduled cloud runs.
- Plan (hypothesis):
  - Hypothesis: weekly dashboard step calls a Mac-only device bridge that cloud runs lack.
  - Files (read only): dca_weekly_dashboard.py, docs/cost_tracking.md.
  - Test first: dashboard build with no bridge callable returns a labeled skip, not an exception.
  - Risk: low, report-only path.
  - Rollback: revert the one commit.

### `verifier_window_mismatch`
- Notion row: https://app.notion.com/p/3f5202e5e6778156b2f6dd972820e189
- Severity / Class / Component: Cosmetic / Structural / Scheduler/clock
- Where the fix lives: repo
- Progress label: Merged, checks green
- Name: Verifier window closes before executor orders land
- Remediation (from row): Move the 9:35 verifier's window close later or derive it from the slot's actual fire time, because first-batch orders consistently land shortly after the fixed window ends and trigger false 'no orders in window' alerts.
- Plan (hypothesis):
  - Hypothesis: verifier window close is a fixed time, shorter than first-batch order latency.
  - Files (read only): scripts/verify_schedule.py, scripts/schedule_watchdog.py, docs/schedule_verification.md.
  - Test first: an order landing 2 min after window close is not flagged as missing.
  - Risk: low; too-wide a window could mask a real miss.
  - Rollback: revert; window constant returns to prior value.

### `recorder_transcript_unavailable`
- Notion row: https://app.notion.com/p/3f5202e5e677815b860edec6c1585a01
- Severity / Class / Component: Silent data loss / Environmental / Scheduler/clock
- Where the fix lives: repo
- Progress label: Merged, checks green
- Name: Run-log recorder cannot read executor transcripts
- Remediation (from row): Keep the recorder on cloud-readable sources only (scheduled-task list, broker order history, Slack alerts), since the transcript-reader tool is unavailable in cloud runs and left the run log empty for about three weeks.
- Plan (hypothesis):
  - Hypothesis: recorder depends on a transcript tool absent from cloud runs.
  - Files (read only): scripts/verify_schedule.py, scripts/finalize_run_evidence.py, docs/schedule_verification.md.
  - Test first: recorder run with transcript source unavailable emits an explicit 'source unavailable' entry instead of an empty log.
  - Risk: low; recorder-only.
  - Rollback: revert commit.

### `hardcoded_normal_cap`
- Notion row: https://app.notion.com/p/3e0202e5e67781059cfafbf97a9e6eaa
- Severity / Class / Component: Cosmetic / Structural / Digest
- Where the fix lives: repo
- Progress label: Merged, checks green
- Name: Digest uses hardcoded cap tier
- Remediation (from row): Read the tier the executors actually selected rather than assuming the default
- Plan (hypothesis):
  - Hypothesis: digest assumes the Normal tier cap instead of the tier executors selected.
  - Files (read only): dca_weekly_dashboard.py, scripts/alert_digest.py, config/budgets.json.
  - Test first: digest under the Stress tier reports the Stress cap.
  - Risk: low; must stay in parity with config/budgets.json.
  - Rollback: revert commit.

### `misattribution`
- Notion row: https://app.notion.com/p/3e0202e5e677819c930ef76f5d665f8e
- Severity / Class / Component: Cosmetic / Structural / Digest
- Where the fix lives: repo
- Progress label: Merged, checks green
- Name: Digest misattributes sizing defects
- Remediation (from row): Split orders by executor before attributing sizing behaviour
- Plan (hypothesis):
  - Hypothesis: digest attributes sizing to the wrong executor when orders are pooled.
  - Files (read only): dca_weekly_dashboard.py, scripts/alert_digest.py, dca_reporting.py.
  - Test first: mixed-executor orders are split by executor before sizing checks.
  - Risk: low; reporting only.
  - Rollback: revert.

### `false_all_clear`
- Notion row: https://app.notion.com/p/3e0202e5e67781a3b7fcf3e1dc75afa0
- Severity / Class / Component: Silent data loss / Structural / Digest
- Where the fix lives: repo
- Progress label: Merged, checks green
- Name: Digest reports false all-clear
- Remediation (from row): Have the digest read run outcomes rather than lastRunAt
- Plan (hypothesis):
  - Hypothesis: digest keys off lastRunAt, which updates even for failed runs.
  - Files (read only): scripts/alert_digest.py, scripts/verify_schedule.py, dca_weekly_dashboard.py.
  - Test first: a run with failed outcome and recent lastRunAt is not reported all-clear.
  - Risk: low; reporting.
  - Rollback: revert.

### `output_limit_workaround`
- Notion row: https://app.notion.com/p/3e0202e5e67781e28f6be26ac7371d68
- Severity / Class / Component: Degrades slot / Structural / Digest
- Where the fix lives: repo
- Progress label: Merged, checks green
- Name: Digest hit output limit
- Remediation (from row): Chunk the order fetch in the digest as well
- Plan (hypothesis):
  - Hypothesis: digest fetches all orders in one call and hits the output cap.
  - Files (read only): scripts/assemble_order_pages.py, dca_weekly_dashboard.py.
  - Test first: digest consumes paged order envelope across multiple pages.
  - Risk: low.
  - Rollback: revert.

### `approval_required`
- Notion row: https://app.notion.com/p/3e0202e5e67781a7bfd2f37d60ea5b88
- Severity / Class / Component: Silent data loss / Environmental / Gmail connector
- Where the fix lives: repo
- Progress label: Merged, checks green
- Name: Alert email requires interactive approval
- Remediation (from row): Drop Gmail from the alert chain for scheduled runs; Slack and the repo alert file both work unattended
- Plan (hypothesis):
  - Hypothesis: Gmail send needs interactive approval so scheduled alerts are lost.
  - Files (read only): config/notifications.json, dca_alerts.py, docs/alert_digest.md.
  - Test first: scheduled-run alert chain omits Gmail and still writes Slack/file alert.
  - Risk: low; must keep at least one delivery channel.
  - Rollback: revert config change.


## B. Order placement and caps (MEDIUM-HIGH risk: own review and acceptance run, do not merge)
### `classifier_block`
- Notion row: https://app.notion.com/p/3e0202e5e677811babd1c73abbc58b1d
- Severity / Class / Component: Degrades slot / Environmental / Permission layer
- Where the fix lives: repo (order placement)
- Progress label: No work
- Name: Order classifier block halted batch
- Remediation (from row): Relax the no-retry batch-stop rule for permission-class blocks
- Plan (hypothesis):
  - Hypothesis: batch-stop rule treats a permission-class block like a hard failure.
  - Files (read only): dca_runner.py (Gate 7), claude/dca-executor-prompts/live-mirror/.
  - Test first: a blocked order is skipped and logged, remaining orders still attempted, alert raised at end.
  - Risk: MEDIUM, touches order placement; must never retry or bypass the block.
  - Rollback: revert; needs operator review before any live use.

### `transaction_block`
- Notion row: https://app.notion.com/p/3e0202e5e67781258633e8b1bd8b4b4e
- Severity / Class / Component: Degrades slot / Environmental / Permission layer
- Where the fix lives: repo (order placement)
- Progress label: No work
- Name: Order blocked by transaction permission layer
- Remediation (from row): Skip the blocked item, log it, continue the batch, alert at the end rather than halting everything
- Plan (hypothesis):
  - Hypothesis: same cause as classifier_block; one blocked item halts the batch.
  - Files (read only): dca_runner.py (Gate 7), dca_live_adapters.py.
  - Test first: blocked item skipped, logged with reason code, batch continues, end-of-batch alert.
  - Risk: MEDIUM, order placement; permission denial itself must not be circumvented.
  - Rollback: revert commit. Consider fixing together with classifier_block.

### `duplicate_fills`
- Notion row: https://app.notion.com/p/3e0202e5e677817cae9ccfc312ac19d9
- Severity / Class / Component: Degrades slot / Policy / Perplexity backup
- Where the fix lives: repo (order placement)
- Progress label: No work
- Name: Backup duplicated fills mid-batch
- Remediation (from row): Interlock only pre-checks; add a mid-batch re-check or make the backup wait for primary completion
- Plan (hypothesis):
  - Hypothesis: interlock only pre-checks, so the backup can place orders the primary also fills mid-batch.
  - Files (read only): scripts/same_day_top_up_gate.py, scripts/same_day_pointer.py, scripts/idempotency.py, scripts/scheduler_lease.py.
  - Test first: backup re-checks broker order history before each order and skips duplicates.
  - Risk: HIGH, order placement; needs acceptance run.
  - Rollback: revert; keep backup disabled meanwhile (operator).

### `cap_breach`
- Notion row: https://app.notion.com/p/3e0202e5e6778135834fdf16c79f9edd
- Severity / Class / Component: Degrades slot / Policy / Digest
- Where the fix lives: repo (caps)
- Progress label: No work
- Name: Cap breach recorded
- Remediation (from row): Consequence of uncoordinated shared-cap spending between executors
- Plan (hypothesis):
  - Hypothesis: consequence of uncoordinated shared-cap spending between executors (per Remediation), not an independent defect.
  - Files (read only): dca_runner.py Gate 6, near_close_dca.py within_caps, morning_dca.py.
  - Test first: two executors in one day cannot jointly exceed the daily cap.
  - Risk: MEDIUM, money caps; fix with cap_consumed/duplicate_fills.
  - Rollback: revert; needs acceptance run.

### `cap_consumed`
- Notion row: https://app.notion.com/p/3e0202e5e67781e48832de7b655a4b13
- Severity / Class / Component: Blocks slot / Policy / Perplexity backup
- Where the fix lives: repo (caps)
- Progress label: No work
- Name: Backup consumed the shared daily cap
- Remediation (from row): Coordinate the shared budget between executors, or give the backup its own allocation
- Plan (hypothesis):
  - Hypothesis: backup draws on the same daily cap as primary executors, uncoordinated.
  - Files (read only): dca_runner.py Gate 6, near_close_dca.py within_caps, docs/safeguards.md.
  - Test first: backup placement refused when primary caps are already consumed.
  - Risk: MEDIUM-high, caps; operator must choose shared vs separate allocation.
  - Rollback: revert. Related: cap_breach.

### `competing_executor`
- Notion row: https://app.notion.com/p/3e0202e5e677816e8ea3cd2081c6a310
- Severity / Class / Component: Blocks slot / Policy / Perplexity backup
- Where the fix lives: prompt + operator policy
- Progress label: No work
- Name: Competing executor detected in interlock window
- Remediation (from row): Renew the agentic coexistence carve-out and tighten the backup trigger so it fires only after the primary has actually failed
- Plan (hypothesis):
  - Hypothesis: backup trigger fires before primary has actually failed; coexistence carve-out expired.
  - Files (read only): docs/dca_operational_todos.md, docs/safeguards.md, claude/dca-executor-prompts/live-mirror/, PRs #1631/#1639 (prompt changes).
  - Test first: backup does not run while primary evidence shows a live in-progress run.
  - Risk: MEDIUM-high, two executors on one account; policy decision by operator.
  - Rollback: revert prompt change and repaste.


## C. Prompt / session / source-policy rows (operator decision first)
### `hand_reimplemented`
- Notion row: https://app.notion.com/p/3e0202e5e67781a1b1b4f6e4f0d2b9bf
- Severity / Class / Component: Degrades slot / Policy / generate_morning_candidates
- Where the fix lives: prompt text
- Progress label: Merged, checks green
- Name: Signal logic reimplemented by hand
- Remediation (from row): Fix the data path so the repo entry point is always usable; never allow hand-replicated signal logic
- Plan (hypothesis):
  - Hypothesis: when the data path failed, the session hand-wrote signal logic instead of calling the repo generator.
  - Files (read only): morning_dca.py, docs/morning_signal_entry.md, claude/dca-executor-prompts/live-mirror/.
  - Test first: prompt-drift test requires 'never reimplement signals' and generator entry point.
  - Risk: low (prompt text).
  - Rollback: revert and repaste.

### `real_world_transactions_denied`
- Notion row: https://app.notion.com/p/3e0202e5e67781bf8216ec066d95c537
- Severity / Class / Component: Blocks slot / Environmental / Permission layer
- Where the fix lives: session layer (no repo fix likely)
- Progress label: Merged, checks green
- Name: Real-world transactions denied
- Remediation (from row): Same as transaction_block; the denial is session-side and not fixable from task config
- Plan (hypothesis):
  - Hypothesis: session-side permission denial; per Remediation not fixable from task config. No repo fix likely.
  - Files (read only): docs/safeguards.md, dca_runner.py Gate 7.
  - Test first: denial surfaces as a distinct reason code and alerts.
  - Risk: must not attempt to bypass the denial.
  - Rollback: n/a. Suggest operator decide Rejected or Policy follow-up.

### `session_refusal`
- Notion row: https://app.notion.com/p/3e0202e5e67781fb87e2d4a51ff2d5a7
- Severity / Class / Component: Blocks slot / Environmental / Permission layer
- Where the fix lives: session layer (no repo fix likely)
- Progress label: Merged, checks green
- Name: Session refused to place orders
- Remediation (from row): Unpredictable per session; route placement through a context with stable trade authorisation
- Plan (hypothesis):
  - Hypothesis: session-side refusal, unpredictable; not fixable in repo per Remediation.
  - Files (read only): claude/dca-executor-prompts/live-mirror/.
  - Test first: refusal is finalized as not_submitted_error and alerts.
  - Risk: low; must not coax or bypass the refusal.
  - Rollback: n/a. Operator decision on routing.

### `provider_error`
- Notion row: https://app.notion.com/p/3e0202e5e67781df8de9ca77dc318023
- Severity / Class / Component: Blocks slot / Structural / Finance connector
- Where the fix lives: repo + operator source policy
- Progress label: No work
- Name: Finance dual-source provider error
- Remediation (from row): Same as missing_connector; check whether the sanctioned yfinance fallback can serve these entry points
- Plan (hypothesis):
  - Hypothesis: dual-source finance provider errors with no sanctioned single-source path wired.
  - Files (read only): dca_live_adapters.py FinanceDataProvider, yfinance-prefetch/, docs/live_wiring.md.
  - Test first: provider error routes to labeled yfinance fallback or a clean not_submitted_error.
  - Risk: MEDIUM; source policy decision by operator.
  - Rollback: revert.

### `missing_connector`
- Notion row: https://app.notion.com/p/3e0202e5e67781e7a46cf4c485ced2bc
- Severity / Class / Component: Blocks slot / Structural / Finance connector
- Where the fix lives: repo + operator source policy
- Progress label: Merged, checks green
- Name: Finance connector not wired
- Remediation (from row): Connect a Finance data source, or formally approve single-source scoring for the affected slots
- Plan (hypothesis):
  - Hypothesis: Finance connector absent in session; needs operator decision (connect it or approve single-source scoring).
  - Files (read only): dca_live_adapters.py FinanceDataProvider, docs/live_wiring.md.
  - Test first: missing connector yields a labeled single-source mode or a fail-closed skip.
  - Risk: MEDIUM, scoring policy.
  - Rollback: revert.

## Suggested order
1. Group A in one sitting; shared files are `dca_weekly_dashboard.py`, `scripts/alert_digest.py`, `scripts/verify_schedule.py`.
2. Group B only after the operator confirms which rows PR #1642 already covers; fix `cap_breach` and `cap_consumed` together,
   `classifier_block` with `transaction_block`. Each needs an acceptance run; do not merge.
3. Group C needs an operator decision (connect the Finance source or approve single-source scoring; route placement context) before any code.

## Not covered
Logs were unreachable for this run (`$DCA_CRON_TRACKING_ROOT` unset), so no new failure candidates were derived.
