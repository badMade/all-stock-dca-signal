# Fix brief: `competing_executor` (PROPOSAL: an agent may start only when Approval is exactly "Approved")

Built from `claude/triage-routine/FIX-BRIEF-TEMPLATE.md`. Docs only: no code, config or live-prompt change.

## Row
- Key: `competing_executor`
- Notion row: https://app.notion.com/p/3e0202e5e677816e8ea3cd2081c6a310
- Severity / Class / Component: Blocks slot / Policy / Perplexity backup
- Approval at time of writing: Proposed
- Evidence: `claude/triage-routine/EXTERNAL-PROMPT-FIXES-2026-10-12.md` (#1642, merged) may already cover this Key.

## Problem
Competing executor detected in interlock window. Remediation per the row: Renew the agentic coexistence carve-out and tighten the backup trigger so it fires only after the primary has actually failed

## Proposed plan (from the row; the root cause is a HYPOTHESIS)
- Hypothesis: backup trigger fires before primary has actually failed; coexistence carve-out expired.
- Files (read only): docs/dca_operational_todos.md, docs/safeguards.md, claude/dca-executor-prompts/live-mirror/, PRs #1631/#1639 (prompt changes).
- Test first: backup does not run while primary evidence shows a live in-progress run.
- Risk: MEDIUM-high, two executors on one account; policy decision by operator.
- Rollback: revert prompt change and repaste.

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
