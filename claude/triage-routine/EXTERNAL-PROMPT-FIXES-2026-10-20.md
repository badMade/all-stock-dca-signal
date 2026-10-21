# Verifier and digest fixes: unknown task status and order-to-slot guessing (external prompt, 2026-10-20)

**Status: DRAFT for operator review. Nothing here is applied.** The verifiers and the digest are
external scheduled tasks, so this repo cannot change them; the operator pastes accepted text into
the task definition. `claude/triage-routine/prompt.txt` (the triage routine) already carries the same
two rules. None of this relaxes a trading safety rule or routes around a permission denial.

## What was observed

A verification run on 2026-10-20 reported two limits of its own:

- It had no `list_scheduled_tasks` tool, so enabled, `lastRunAt` and `nextRunAt` for the seven
  executor tasks were unknown. `list_triggers` returned only unrelated reminders.
- Order times did not line up with slot times, so it guessed which slot made which order cluster.

## Findings

| # | Fact or inference | Evidence |
| --- | --- | --- |
| 1 | FACT: `list_triggers` does not return the executor tasks. | run note 2026-10-20; `docs/scheduled_session_repo_access.md` |
| 2 | FACT: slot outcomes come from `run_state.json` and Gate 9 evidence, never from `lastRunAt`. | `EXTERNAL-PROMPT-FIXES-2026-10-12.md`, `false_all_clear` |
| 3 | FACT: `scripts.slot_attribution.attribute_orders` maps order IDs to slots from each slot's own dated `run_state.json` (merged in #1804); `verify_schedule --orders-json` exposes it on the CLI. | `scripts/slot_attribution.py`, `scripts/verify_schedule.py` |
| 4 | INFERENCE: order times drift from slot times because of per-task scheduler jitter and backup periods, so time clustering will misattribute. | `claude/SECOND-EXECUTOR-FINDING.md` jitter table |

## Paste-ready text

### `task_status_unknown`
Add: "Scheduled-task status (enabled, lastRunAt, nextRunAt) comes only from the `list_scheduled_tasks`
tool. If you do not have that tool, write enabled, lastRunAt and nextRunAt as UNKNOWN for every
executor task. `list_triggers` returns only unrelated reminders, never the executor tasks, so do not
read task status from it and do not infer it from order history. UNKNOWN is a finding to report, not
a pass."

### `order_slot_guessing`
Add: "Attribute a broker order to a slot only through that slot's own dated `run_state.json`. Write
the day's order IDs to a JSON array and run `python3 -m scripts.verify_schedule --date <YYYY-MM-DD> --json --orders-json <file>` from the repo root (`-` reads the array from stdin); the report's `order_attribution` block carries the result. Report `attributed` IDs under their slot, list `unattributed` and
`ambiguous` IDs as such, and never assign an order to a slot by its created_at time. A match is an
artifact claim, not proof the slot verified or the order filled."

## Verification

Write a JSON array holding one order ID that a slot's `run_state.json` names and one it does not, then
run `python3 -m scripts.verify_schedule --date <YYYY-MM-DD> --json --orders-json <file>` against that
tree: the named ID appears under `order_attribution.attributed` with its slot and the other under
`unattributed`. Malformed input exits 2 with nothing on stdout, and the exit status never depends on
attribution. Covered by `scripts/tests/test_verify_schedule_orders_flag.py`.
