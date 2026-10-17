# Fix brief: `verifier_window_mismatch` (PROPOSAL: an agent may start only when Approval is exactly "Approved")

Built from `claude/triage-routine/FIX-BRIEF-TEMPLATE.md`. Docs only: no code, config or live-prompt change.

## Row
- Key: `verifier_window_mismatch`
- Notion row: https://app.notion.com/p/3f5202e5e6778156b2f6dd972820e189
- Severity / Class / Component: Cosmetic / Structural / Scheduler/clock
- Approval at time of writing: Proposed
- Evidence: `claude/triage-routine/EXTERNAL-PROMPT-FIXES-2026-10-12.md` (#1642, merged) may already cover this Key.

## Problem
Verifier window closes before executor orders land. Remediation per the row: Move the 9:35 verifier's window close later or derive it from the slot's actual fire time, because first-batch orders consistently land shortly after the fixed window ends and trigger false 'no orders in window' alerts.

## Proposed plan (from the row; the root cause is a HYPOTHESIS)
- Hypothesis: verifier window close is a fixed time, shorter than first-batch order latency.
- Files (read only): scripts/verify_schedule.py, scripts/schedule_watchdog.py, docs/schedule_verification.md.
- Test first: an order landing 2 min after window close is not flagged as missing.
- Risk: low; too-wide a window could mask a real miss.
- Rollback: revert; window constant returns to prior value.

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
