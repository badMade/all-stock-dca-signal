# Fix brief: `cap_breach` (PROPOSAL: an agent may start only when Approval is exactly "Approved")

Built from `claude/triage-routine/FIX-BRIEF-TEMPLATE.md`. Docs only: no code, config or live-prompt change.

## Row
- Key: `cap_breach`
- Notion row: https://app.notion.com/p/3e0202e5e6778135834fdf16c79f9edd
- Severity / Class / Component: Degrades slot / Policy / Digest
- Approval at time of writing: Proposed
- Evidence: `claude/triage-routine/EXTERNAL-PROMPT-FIXES-2026-10-12.md` (#1642, merged) may already cover this Key.

## Problem
Cap breach recorded. Remediation per the row: Consequence of uncoordinated shared-cap spending between executors

## Proposed plan (from the row; the root cause is a HYPOTHESIS)
- Hypothesis: consequence of uncoordinated shared-cap spending between executors (per Remediation), not an independent defect.
- Files (read only): dca_runner.py Gate 6, near_close_dca.py within_caps, morning_dca.py.
- Test first: two executors in one day cannot jointly exceed the daily cap.
- Risk: MEDIUM, money caps; fix with cap_consumed/duplicate_fills.
- Rollback: revert; needs acceptance run.

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
