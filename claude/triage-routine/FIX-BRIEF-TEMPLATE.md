# Fix brief: `<key>` (PROPOSAL: an agent may start only when Approval is exactly "Approved")

<!--
Template emitted by the DCA triage routine (STEP 5b) for every Open row that has a plan, so a Claude Code
agent can pick it up cold and implement the fix. The routine only PROPOSES: it fills this
in as text (Notion "Proposed plan" and the digest appendix). It never edits files, opens
PRs or sets Approval. An agent must not start until the row's Approval is exactly "Approved".
Fill every field; write "unknown" rather than guessing.
-->

## Row
- Key: `<snake_case key>`
- Notion row: <url>
- Severity / Class / Component: <..> / <..> / <..>
- Approval at time of writing: <empty | Proposed | Approved>
- Progress: <label from tr.derive_progress>
- Evidence: <branch / PR / commit / issue links, or "none">

## Problem
<2-4 sentences from the row's Name and Remediation. Mark any root cause as HYPOTHESIS.>

## Where the fix lives
- [ ] This repo: `<files, read-only hints>`
- [ ] External prompt/task (paste-only, see claude/triage-routine/EXTERNAL-PROMPT-FIXES-*.md)
- [ ] Session/connector layer (runbook text only)
- Already fixed on main? <yes: commit/PR | no | unknown>. If yes, the task is "verify and mark Resolved", not new code.

## Tests first
1. <repo rows: failing test to write before any change, under tests/ or scripts/tests/>
2. <external-prompt / session / connector rows: no repo test applies; give a non-code verification procedure instead, e.g. a dry run or checklist the operator performs after pasting the text, with the expected observable result>

## Constraints (copy verbatim)
- Standard library only at runtime; no hard-coded absolute paths; no account number literals.
- Fail closed. Never retry, rephrase or bypass a permission denial or a safety gate.
- Open the PR as a DRAFT. Update pyproject.toml for any new top-level module or config file.
- Order placement and caps logic: needs its own review and acceptance run; do not merge.

## Acceptance
- `ruff check .` and the targeted pytest files pass; `python run_dca.py --demo --report --now 2026-08-03T15:45 --artifacts-dir <scratch>` still passes.
- PR TITLE names the Key (e.g. "[AI-Assisted] <key>: ...") so the triage reconciler can match it; a body-only mention does not count as a fix.

## Risk and rollback
- Risk: <low | medium | high> because <reason>
- Rollback: <revert commit / repaste prompt>
