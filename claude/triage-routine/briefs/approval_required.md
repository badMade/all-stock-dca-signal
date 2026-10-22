# Fix brief: `approval_required` (PROPOSAL: an agent may start only when Approval is exactly "Approved")

Built from `claude/triage-routine/FIX-BRIEF-TEMPLATE.md`. Docs only: no code, config or live-prompt change.

## Row
- Key: `approval_required`
- Notion row: https://app.notion.com/p/3e0202e5e67781a7bfd2f37d60ea5b88
- Severity / Class / Component: Silent data loss / Environmental / Gmail connector
- Approval at time of writing: Proposed
- Evidence: `claude/triage-routine/EXTERNAL-PROMPT-FIXES-2026-10-12.md` (#1642, merged) may already cover this Key.

## Problem
Alert email requires interactive approval. Remediation per the row: Drop Gmail from the alert chain for scheduled runs; Slack and the repo alert file both work unattended

## Proposed plan (from the row; the root cause is a HYPOTHESIS)
- Hypothesis: Gmail send needs interactive approval so scheduled alerts are lost.
- Files (read only): config/notifications.json, dca_alerts.py, docs/alert_digest.md.
- Test first: scheduled-run alert chain omits Gmail and still writes Slack/file alert.
- Risk: low; must keep at least one delivery channel.
- Rollback: revert config change.

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
