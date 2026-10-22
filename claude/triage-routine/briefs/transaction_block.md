# Fix brief: `transaction_block` (PROPOSAL: an agent may start only when Approval is exactly "Approved")

Built from `claude/triage-routine/FIX-BRIEF-TEMPLATE.md`. Docs only: no code, config or live-prompt change.

## Row
- Key: `transaction_block`
- Notion row: https://app.notion.com/p/3e0202e5e67781258633e8b1bd8b4b4e
- Severity / Class / Component: Degrades slot / Environmental / Permission layer
- Approval at time of writing: Proposed
- Evidence: `claude/triage-routine/EXTERNAL-PROMPT-FIXES-2026-10-12.md` (#1642, merged) may already cover this Key.

## Problem
Order blocked by transaction permission layer. Remediation per the row: Skip the blocked item, log it, continue the batch, alert at the end rather than halting everything

## Proposed plan (from the row; the root cause is a HYPOTHESIS)
- Hypothesis: same cause as classifier_block; one blocked item halts the batch.
- Files (read only): dca_runner.py (Gate 7), dca_live_adapters.py.
- Test first: blocked item skipped, logged with reason code, batch continues, end-of-batch alert.
- Risk: MEDIUM, order placement; permission denial itself must not be circumvented.
- Rollback: revert commit. Consider fixing together with classifier_block.

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
- PR TITLE names the Key (e.g. "[AI-Assisted] <key>: ...") so the triage reconciler can match it; a body-only mention does not count as a fix.
