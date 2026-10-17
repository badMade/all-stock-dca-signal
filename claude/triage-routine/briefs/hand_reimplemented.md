# Fix brief: `hand_reimplemented` (PROPOSAL: an agent may start only when Approval is exactly "Approved")

Built from `claude/triage-routine/FIX-BRIEF-TEMPLATE.md`. Docs only: no code, config or live-prompt change.

## Row
- Key: `hand_reimplemented`
- Notion row: https://app.notion.com/p/3e0202e5e67781a1b1b4f6e4f0d2b9bf
- Severity / Class / Component: Degrades slot / Policy / generate_morning_candidates
- Approval at time of writing: Proposed
- Evidence: `claude/triage-routine/EXTERNAL-PROMPT-FIXES-2026-10-12.md` (#1642, merged) may already cover this Key.

## Problem
Signal logic reimplemented by hand. Remediation per the row: Fix the data path so the repo entry point is always usable; never allow hand-replicated signal logic

## Proposed plan (from the row; the root cause is a HYPOTHESIS)
- Hypothesis: when the data path failed, the session hand-wrote signal logic instead of calling the repo generator.
- Files (read only): morning_dca.py, docs/morning_signal_entry.md, claude/dca-executor-prompts/live-mirror/.
- Test first: prompt-drift test requires 'never reimplement signals' and generator entry point.
- Risk: low (prompt text).
- Rollback: revert and repaste.

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
