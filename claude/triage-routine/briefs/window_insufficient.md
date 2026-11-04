# Fix brief: `window_insufficient` (PROPOSAL: an agent may start only when Approval is exactly "Approved")

Built from `claude/triage-routine/FIX-BRIEF-TEMPLATE.md`. Proposal only: no code or live-gate change.

## Row
- Key: `window_insufficient`
- Notion row: (row URL not recorded here)
- Status / Severity / Fix state: empty / empty / empty
- Approval at time of writing: Proposed
- Evidence: none; no matching branch, PR, commit or issue.

## Problem
The 2026-10-30 09:35 slot ended with `ABORT_GATE7_WINDOW_INSUFFICIENT`, fired 36 minutes late, and placed nothing. Its `run_state.json` evidence is represented by the FRIDAY fixture in `scripts/tests/test_triage_log_summary.py`. The underlying cause is a HYPOTHESIS: a late slot fire left too little market window for Gate 7 to proceed safely.

## Proposed plan (root cause is a HYPOTHESIS)
- Hypothesis: the slot's late fire caused Gate 7 to find the remaining window insufficient; confirm against the gate and recorded run evidence before proposing any fix.
- Files (read only): `near_close_dca.py` Gate 7 and the 2026-10-30 09:35 run evidence.
- Test first: preserve a classification test proving this error class and abort outcome are flagged for attention.
- Risk: HIGH, order placement timing; any gate change needs its own review and acceptance run.
- Rollback: revert the eventual change; needs acceptance run.

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
- PR TITLE names the Key (e.g. "[AI-Assisted] window_insufficient: ...") so the triage reconciler can match it; a body-only mention does not count as a fix.

## Risk and rollback
- Risk: HIGH because a gate timing change can affect order placement.
- Rollback: revert commit; needs acceptance run.
