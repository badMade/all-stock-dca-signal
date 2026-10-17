# Fix brief: `classifier_block` (PROPOSAL: an agent may start only when Approval is exactly "Approved")

Built from `claude/triage-routine/FIX-BRIEF-TEMPLATE.md`. Docs only: no code, config or live-prompt change.

## Row
- Key: `classifier_block`
- Notion row: https://app.notion.com/p/3e0202e5e677811babd1c73abbc58b1d
- Severity / Class / Component: Degrades slot / Environmental / Permission layer
- Approval at time of writing: Proposed
- Evidence: `claude/triage-routine/EXTERNAL-PROMPT-FIXES-2026-10-12.md` (#1642, merged) may already cover this Key.

## Problem
Order classifier block halted batch. Remediation per the row: Relax the no-retry batch-stop rule for permission-class blocks

## Proposed plan (from the row; the root cause is a HYPOTHESIS)
- Hypothesis: batch-stop rule treats a permission-class block like a hard failure.
- Files (read only): dca_runner.py (Gate 7), claude/dca-executor-prompts/live-mirror/.
- Test first: a blocked order is skipped and logged, remaining orders still attempted, alert raised at end.
- Risk: MEDIUM, touches order placement; must never retry or bypass the block.
- Rollback: revert; needs operator review before any live use.

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
