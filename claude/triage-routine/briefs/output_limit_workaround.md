# Fix brief: `output_limit_workaround` (PROPOSAL: an agent may start only when Approval is exactly "Approved")

Built from `claude/triage-routine/FIX-BRIEF-TEMPLATE.md`. Docs only: no code, config or live-prompt change.

## Row
- Key: `output_limit_workaround`
- Notion row: https://app.notion.com/p/3e0202e5e67781e28f6be26ac7371d68
- Severity / Class / Component: Degrades slot / Structural / Digest
- Approval at time of writing: Proposed
- Evidence: `claude/triage-routine/EXTERNAL-PROMPT-FIXES-2026-10-12.md` (#1642, merged) may already cover this Key.

## Problem
Digest hit output limit. Remediation per the row: Chunk the order fetch in the digest as well

## Proposed plan (from the row; the root cause is a HYPOTHESIS)
- Hypothesis: digest fetches all orders in one call and hits the output cap.
- Files (read only): scripts/assemble_order_pages.py, dca_weekly_dashboard.py.
- Test first: digest consumes paged order envelope across multiple pages.
- Risk: low.
- Rollback: revert.

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
