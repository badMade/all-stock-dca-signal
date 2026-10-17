# Handoff: dry-run mode for the live executor (from a Cowork session)

Start Claude Code in this repo and say: "Read HANDOFF_dry_run.md and continue." Nothing here is committed.

## Goal
Test the live 9:35 executor pipeline end to end outside market hours, with zero orders. Rules from the owner: tests first, wait for approval before implementing; no em dashes; any PR must link back to the originating chat session.

## Findings
- dca_runner.execute_plan already has a two-gate dry_run (broker.dry_run + enable_live_orders) and an injectable clock (now_fn). Covered by tests/test_dca_runner.py.
- scripts/finalize_run_evidence.py refused any --started-at outside the slot window, so Gate 9 evidence could not be smoke-tested off-hours.
- The Claude executor places orders via the native place_equity_order tool, not execute_plan, so a runner-level dry_run does not cover it. That needs a wording change in the scheduled-task prompt (owner applies it).

## Done (uncommitted, in the working tree)
- tests/test_finalize_run_evidence_dry_run.py (new, 7 tests, all pass)
- scripts/finalize_run_evidence.py: dry_run arg on finalize(), --dry-run CLI flag, _finalize_dry_run(). Skips the window check, writes only manual_runs/dry_run/<slot dir>/run_state.json, never touches the scheduled dir or any pointer, refuses orders or a 'submitted' status.

## Open items
1. Confirm tests/test_dca_console_schedule.py::test_command_tokens_never_carry_embedded_paths fails on a clean checkout too (a "~<path>" mismatch in --state-root). Cowork could not verify: its git stash failed on a lock. Likely environment-related.
2. Decide whether to commit/PR the change (branch, attribution lines, link to the Cowork session).
3. Executor prompt: add a dry-run switch that skips only place_equity_order and relaxes only the Gate 1 clock, then run the full gate sequence in market hours at least once.
4. generate_market_open_candidates reported decision_time_et 09:50 for slot 0935 when fed the 09:35 payload. Unexplained.
5. config/market_open_dca.json artifact_paths.prior_allocation_roots point at /home/user/workspace/... which does not exist on this Mac mount.
6. Not tested yet: Gates 6 and 7 planning/placement, the final fresh-price check, Gate 9 real evidence and drift check.

## Housekeeping
- A stale lock was renamed to .git/stale-index-lock. A leftover temp object remains under .git/objects/c1/. Both are safe to delete.
- A scratch dry-run folder exists at cron_tracking/market_open_dca/20261016_0935ET/dryrun/ (gitignored tracking output).
- Today's 9:35 scheduled fire ran at 22:21 ET (scheduler fault, Gate 1 abort). Check the cron/timezone of that task.
- Tests need pytest, which the bridge Python lacks. Use a venv outside the repo.
