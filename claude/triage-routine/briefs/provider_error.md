# Fix brief: `provider_error` (PROPOSAL: an agent may start only when Approval is exactly "Approved")

Built from `claude/triage-routine/FIX-BRIEF-TEMPLATE.md`. Docs only: no code, config or live-prompt change.

## Row
- Key: `provider_error`
- Notion row: https://app.notion.com/p/3e0202e5e67781df8de9ca77dc318023
- Severity / Class / Component: Blocks slot / Structural / Finance connector
- Approval at time of writing: Proposed
- Evidence: `claude/triage-routine/EXTERNAL-PROMPT-FIXES-2026-10-12.md` (#1642, merged) may already cover this Key.

## Problem
Finance dual-source provider error. Remediation per the row: Same as missing_connector; check whether the sanctioned yfinance fallback can serve these entry points

## Proposed plan (from the row; the root cause is a HYPOTHESIS)
- Hypothesis: dual-source finance provider errors with no sanctioned single-source path wired.
- Files (read only): dca_live_adapters.py FinanceDataProvider, yfinance-prefetch/, docs/live_wiring.md.
- Test first: provider error routes to labeled yfinance fallback or a clean not_submitted_error.
- Risk: MEDIUM; source policy decision by operator.
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
