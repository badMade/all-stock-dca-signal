# Fix brief: `session_refusal` (PROPOSAL: an agent may start only when Approval is exactly "Approved")

Built from `claude/triage-routine/FIX-BRIEF-TEMPLATE.md`. Docs only: no code, config or live-prompt change.

## Row
- Key: `session_refusal`
- Notion row: https://app.notion.com/p/3e0202e5e67781fb87e2d4a51ff2d5a7
- Severity / Class / Component: Blocks slot / Environmental / Permission layer
- Approval at time of writing: Proposed
- Evidence: `claude/triage-routine/EXTERNAL-PROMPT-FIXES-2026-10-12.md` (#1642, merged) may already cover this Key.

## Problem
Session refused to place orders. Remediation per the row: Unpredictable per session; route placement through a context with stable trade authorisation

## Proposed plan (from the row; the root cause is a HYPOTHESIS)
- Hypothesis: session-side refusal, unpredictable; not fixable in repo per Remediation.
- Files (read only): claude/dca-executor-prompts/live-mirror/.
- Test first: refusal is finalized as not_submitted_error and alerts.
- Risk: low; must not coax or bypass the refusal.
- Rollback: n/a. Operator decision on routing.

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
