# Fix brief: `recorder_transcript_unavailable` (PROPOSAL: an agent may start only when Approval is exactly "Approved")

Built from `claude/triage-routine/FIX-BRIEF-TEMPLATE.md`. Docs only: no code, config or live-prompt change.

## Row
- Key: `recorder_transcript_unavailable`
- Notion row: https://app.notion.com/p/3f5202e5e677815b860edec6c1585a01
- Severity / Class / Component: Silent data loss / Environmental / Scheduler/clock
- Approval at time of writing: Proposed
- Evidence: `claude/triage-routine/EXTERNAL-PROMPT-FIXES-2026-10-12.md` (#1642, merged) may already cover this Key.

## Problem
Run-log recorder cannot read executor transcripts. Remediation per the row: Keep the recorder on cloud-readable sources only (scheduled-task list, broker order history, Slack alerts), since the transcript-reader tool is unavailable in cloud runs and left the run log empty for about three weeks.

## Proposed plan (from the row; the root cause is a HYPOTHESIS)
- Hypothesis: recorder depends on a transcript tool absent from cloud runs.
- Files (read only): scripts/verify_schedule.py, scripts/finalize_run_evidence.py, docs/schedule_verification.md.
- Test first: recorder run with transcript source unavailable emits an explicit 'source unavailable' entry instead of an empty log.
- Risk: low; recorder-only.
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
- PR TITLE names the Key (e.g. "[AI-Assisted] <key>: ...") so the triage reconciler can match it; a body-only mention does not count as a fix.
