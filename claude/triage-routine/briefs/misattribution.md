# Fix brief: `misattribution` (PROPOSAL: an agent may start only when Approval is exactly "Approved")

Built from `claude/triage-routine/FIX-BRIEF-TEMPLATE.md`. Docs only: no code, config or live-prompt change.

## Row
- Key: `misattribution`
- Notion row: https://app.notion.com/p/3e0202e5e677819c930ef76f5d665f8e
- Severity / Class / Component: Cosmetic / Structural / Digest
- Approval at time of writing: Proposed
- Evidence: `claude/triage-routine/EXTERNAL-PROMPT-FIXES-2026-10-12.md` (#1642, merged) may already cover this Key.

## Problem
Digest misattributes sizing defects. Remediation per the row: Split orders by executor before attributing sizing behaviour

## Proposed plan (from the row; the root cause is a HYPOTHESIS)
- Hypothesis: digest attributes sizing to the wrong executor when orders are pooled.
- Files (read only): dca_weekly_dashboard.py, scripts/alert_digest.py, dca_reporting.py.
- Test first: mixed-executor orders are split by executor before sizing checks.
- Risk: low; reporting only.
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
