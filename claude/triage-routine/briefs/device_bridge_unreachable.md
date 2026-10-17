# Fix brief: `device_bridge_unreachable` (PROPOSAL: an agent may start only when Approval is exactly "Approved")

Built from `claude/triage-routine/FIX-BRIEF-TEMPLATE.md`. Docs only: no code, config or live-prompt change.

## Row
- Key: `device_bridge_unreachable`
- Notion row: https://app.notion.com/p/3f5202e5e677813ba0edd1831b48ca99
- Severity / Class / Component: Degrades slot / Environmental / Sandbox shell
- Approval at time of writing: Proposed
- Evidence: `claude/triage-routine/EXTERNAL-PROMPT-FIXES-2026-10-12.md` (#1642, merged) may already cover this Key.

## Problem
Weekly dashboard refresh cannot reach the Mac. Remediation per the row: Run the weekly dashboard refresh only while the Mac is awake and connected, or move its report step to a source reachable from the cloud, because the device bridge is unavailable in scheduled cloud runs.

## Proposed plan (from the row; the root cause is a HYPOTHESIS)
- Hypothesis: weekly dashboard step calls a Mac-only device bridge that cloud runs lack.
- Files (read only): dca_weekly_dashboard.py, docs/cost_tracking.md.
- Test first: dashboard build with no bridge callable returns a labeled skip, not an exception.
- Risk: low, report-only path.
- Rollback: revert the one commit.

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
