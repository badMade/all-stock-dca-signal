# Fix brief: `duplicate_fills` (PROPOSAL: an agent may start only when Approval is exactly "Approved")

Built from `claude/triage-routine/FIX-BRIEF-TEMPLATE.md`. Docs only: no code, config or live-prompt change.

## Row
- Key: `duplicate_fills`
- Notion row: https://app.notion.com/p/3e0202e5e677817cae9ccfc312ac19d9
- Severity / Class / Component: Degrades slot / Policy / Perplexity backup
- Approval at time of writing: Proposed
- Evidence: `claude/triage-routine/EXTERNAL-PROMPT-FIXES-2026-10-12.md` (#1642, merged) may already cover this Key.

## Problem
Backup duplicated fills mid-batch. Remediation per the row: Interlock only pre-checks; add a mid-batch re-check or make the backup wait for primary completion

## Proposed plan (from the row; the root cause is a HYPOTHESIS)
- Hypothesis: interlock only pre-checks, so the backup can place orders the primary also fills mid-batch.
- Files (read only): scripts/same_day_top_up_gate.py, scripts/same_day_pointer.py, scripts/idempotency.py, scripts/scheduler_lease.py.
- Test first: backup re-checks broker order history before each order and skips duplicates.
- Risk: HIGH, order placement; needs acceptance run.
- Rollback: revert; keep backup disabled meanwhile (operator).

## Where the fix lives
- [ ] This repo
- [ ] External prompt/task (paste-only)
- [ ] Session/connector layer (runbook text only)
- Already fixed on main? unknown. Verify against code/tests first; if yes the task is "verify and mark Resolved".

## Constraints
- Standard library only at runtime; no hard-coded absolute paths; no account number literals.
- Fail closed. Never retry, rephrase or bypass a permission denial or a safety gate.
- Order placement and caps logic needs its own review and acceptance run; do not merge without operator approval.

## Acceptance
- Failing test (or non-code verification procedure) written first, then the smallest change; `ruff check .` and targeted pytest pass.
- PR body names the Key so the triage reconciler can match it.
