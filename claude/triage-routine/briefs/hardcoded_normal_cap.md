# Fix brief: `hardcoded_normal_cap` (PROPOSAL: an agent may start only when Approval is exactly "Approved")

Built from `claude/triage-routine/FIX-BRIEF-TEMPLATE.md`. Docs only: no code, config or live-prompt change.

## Row
- Key: `hardcoded_normal_cap`
- Notion row: https://app.notion.com/p/3e0202e5e67781059cfafbf97a9e6eaa
- Severity / Class / Component: Cosmetic / Structural / Digest
- Approval at time of writing: Proposed
- Evidence: `claude/triage-routine/EXTERNAL-PROMPT-FIXES-2026-10-12.md` (#1642, merged) may already cover this Key.

## Problem
Digest uses hardcoded cap tier. Remediation per the row: Read the tier the executors actually selected rather than assuming the default

## Proposed plan (from the row; the root cause is a HYPOTHESIS)
- Hypothesis: digest assumes the Normal tier cap instead of the tier executors selected.
- Files (read only): dca_weekly_dashboard.py, scripts/alert_digest.py, config/budgets.json.
- Test first: digest under the Stress tier reports the Stress cap.
- Risk: low; must stay in parity with config/budgets.json.
- Rollback: revert commit.

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
