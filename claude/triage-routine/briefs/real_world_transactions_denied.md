# Fix brief: `real_world_transactions_denied` (PROPOSAL: an agent may start only when Approval is exactly "Approved")

Built from `claude/triage-routine/FIX-BRIEF-TEMPLATE.md`. Docs only: no code, config or live-prompt change.

## Row
- Key: `real_world_transactions_denied`
- Notion row: https://app.notion.com/p/3e0202e5e67781bf8216ec066d95c537
- Severity / Class / Component: Blocks slot / Environmental / Permission layer
- Approval at time of writing: Proposed
- Evidence: `claude/triage-routine/EXTERNAL-PROMPT-FIXES-2026-10-12.md` (#1642, merged) may already cover this Key.

## Problem
Real-world transactions denied. Remediation per the row: Same as transaction_block; the denial is session-side and not fixable from task config

## Proposed plan (from the row; the root cause is a HYPOTHESIS)
- Hypothesis: session-side permission denial; per Remediation not fixable from task config. No repo fix likely.
- Files (read only): docs/safeguards.md, dca_runner.py Gate 7.
- Test first: denial surfaces as a distinct reason code and alerts.
- Risk: must not attempt to bypass the denial.
- Rollback: n/a. Suggest operator decide Rejected or Policy follow-up.

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
