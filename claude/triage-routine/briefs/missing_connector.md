# Fix brief: `missing_connector` (PROPOSAL: an agent may start only when Approval is exactly "Approved")

Built from `claude/triage-routine/FIX-BRIEF-TEMPLATE.md`. Docs only: no code, config or live-prompt change.

## Row
- Key: `missing_connector`
- Notion row: https://app.notion.com/p/3e0202e5e67781e7a46cf4c485ced2bc
- Severity / Class / Component: Blocks slot / Structural / Finance connector
- Approval at time of writing: Proposed
- Evidence: `claude/triage-routine/EXTERNAL-PROMPT-FIXES-2026-10-12.md` (#1642, merged) may already cover this Key.

## Problem
Finance connector not wired. Remediation per the row: Connect a Finance data source, or formally approve single-source scoring for the affected slots

## Proposed plan (from the row; the root cause is a HYPOTHESIS)
- Hypothesis: Finance connector absent in session; needs operator decision (connect it or approve single-source scoring).
- Files (read only): dca_live_adapters.py FinanceDataProvider, docs/live_wiring.md.
- Test first: missing connector yields a labeled single-source mode or a fail-closed skip.
- Risk: MEDIUM, scoring policy.
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
- PR TITLE names the Key (e.g. "[AI-Assisted] <key>: ...") so the triage reconciler can match it; a body-only mention does not count as a fix.
