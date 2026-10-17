# Fix brief: `cap_consumed` (PROPOSAL: an agent may start only when Approval is exactly "Approved")

Built from `claude/triage-routine/FIX-BRIEF-TEMPLATE.md`. Docs only: no code, config or live-prompt change.

## Row
- Key: `cap_consumed`
- Notion row: https://app.notion.com/p/3e0202e5e67781e48832de7b655a4b13
- Severity / Class / Component: Blocks slot / Policy / Perplexity backup
- Approval at time of writing: Proposed
- Evidence: `claude/triage-routine/EXTERNAL-PROMPT-FIXES-2026-10-12.md` (#1642, merged) may already cover this Key.

## Problem
Backup consumed the shared daily cap. Remediation per the row: Coordinate the shared budget between executors, or give the backup its own allocation

## Proposed plan (from the row; the root cause is a HYPOTHESIS)
- Hypothesis: backup draws on the same daily cap as primary executors, uncoordinated.
- Files (read only): dca_runner.py Gate 6, near_close_dca.py within_caps, docs/safeguards.md.
- Test first: backup placement refused when primary caps are already consumed.
- Risk: MEDIUM-high, caps; operator must choose shared vs separate allocation.
- Rollback: revert. Related: cap_breach.

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
