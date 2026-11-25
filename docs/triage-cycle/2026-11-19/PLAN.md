# PLAN: triage prep (issues #1-#7 of docs/triage-cycle/2026-11-19/ISSUES.md)

Revision 3 (after Same Page rounds 1-2). Subject of the Same Page Meeting. Vision and non-negotiables: `docs/triage-cycle/2026-11-19/VTO.md`
(the file beside this one; the repo-root `VTO.md`/`PLAN.md`/`ISSUES.md` belong to an unrelated hosted-console cycle and are out of scope).

## Starting point (facts verified 2026-11-19 against the operator's cron_tracking tree)

- 13:30 run (`intraday_dca/20261119_1330ET/run_state.json`): `order_submit_status=not_submitted_error`,
  `error_class=output_cap_exceeded`, 0 orders. Gate 9 reason: "today's get_equity_orders history (about 37 orders) could
  not be saved verbatim". `day_history_mode` and `week_history_mode` are absent. Same shape as the 2026-11-12 2:30 abort
  that `docs/adr-incremental-order-history.md` and `scripts/find_order_baseline.py` were built for.
- Every earlier slot that day (09:35, 10:30, 11:30, 12:30) saved `orders_pages_2026-11-19_<hhmm>.json` and a week file, so a
  valid baseline existed for the 13:30 slot. The 14:30 slot also saved them. Only 13:30 failed.
- `claude/dca-executor-prompts/live-mirror/1330ET-130-pm-afternoon-buyiing-run.txt` contains the SAME-DAY BASELINE CHAIN
  (line 97) and `tests/test_live_prompt_same_day_baseline.py` guards it. The 13:30 run did not use it, so either the live
  routine prompt differs from the mirror or the agent skipped the rule. This repo cannot see the live routine.
- `scripts/schedule_watchdog.py` already flags error outcomes: for 2026-11-19 it exits 1 and prints `error_slots: ["1330"]`.
  The misleading field is the top-level `ok`, copied from `verify_day`, which stays `true` because evidence was complete.
  The digest quoted "ok with no failing slots". Correction to ISSUES.md #2: the gap is the `ok` field and the digest's reading
  of it, not a missing detection.
- `scripts/triage_log_summary.py` already lists 1330 in `attention_slots` (verified by running it).
- The digest prompt (`~/.claude/scheduled-tasks/dca-triage-digest/SKILL.md`, outside the repo) defines COMPLETE as STEPs 1-6
  "for every Key" while also directing the batch script for STEPs 2, 4 and 6; the repo copy is
  `claude/triage-routine/prompt.txt`.
- `list_scheduled_tasks` holds no executor slot tasks (they are cloud routines).

## Rock 1 (#1): flag "baseline existed but was not used"

Add to `scripts/triage_log_summary.py` a per-slot field `baseline_unused` and the reason `baseline_available_not_used`: true
when the slot's `error_class` is `output_cap_exceeded`, its run_state has no `day_history_mode`, and
`scripts.find_order_baseline.find_baseline` (the same validity checks the live prompt relies on: trusted writer, matching
payload_sha256, no condensed row, snapshot_at not in the future) returns a day baseline for that slot and trading day, called with `now=` the failed slot's fire time (its slot time on that
trading day in ET, converted to an aware datetime), so a snapshot saved after the failed run cannot count as available; pass
`root=` the summary's own tracking root. Do not
re-implement the checks and do not decide on file readability alone. `find_baseline` needs the account value: read it from
`$ROBINHOOD_AGENTIC_ACCOUNT_NUMBER` (or the repo's dca_env path), never print or store it; if it is unavailable or the call
raises `BaselineRejected` for another reason, emit `baseline_unused: null` with `baseline_check: unavailable`, never false.
Add the field to text and JSON output and make such a slot's attention line say "live prompt may lack the baseline chain: diff it
against live-mirror".
Non-goals: editing any live prompt or live-mirror file; deciding whether the live routine is stale; touching `find_order_baseline.py`.
Proof: `.venv/bin/python -m pytest scripts/tests/test_triage_log_summary.py -q && ruff check scripts/triage_log_summary.py scripts/tests/test_triage_log_summary.py`
(the test builds a tmp tree mirroring the 2026-11-19 shape).

## Rock 2 (#2): watchdog `ok` reflects error outcomes

In `scripts/schedule_watchdog.py` `main()`, after computing `error_slots`, set `report["ok"] = report["ok"] and not
report["error_slots"]` and keep `verify_schedule_ok` (the original value) as its own key. Exit code and alert files do not
change. Update the `claude/triage-routine/prompt.txt` STEP 1 sentence so the digest reads `error_slots`, not just `ok`.
Non-goals: changing which statuses count as errors; touching `verify_schedule.py` (it is a Perplexity backup-planner source,
changing it forces a repaste of every backup prompt).
Proof: `.venv/bin/python -m pytest scripts/tests/test_schedule_watchdog.py -q && ruff check scripts/schedule_watchdog.py`

## Rock 3 (#3): cap fallout check (read-only note, no code)

Write `docs/triage-cycle/2026-11-19/CAP-CHECK.md`: from the 2026-11-19 run artifacts (`run_state.json`, `submitted_orders.csv`,
`gate9_report.json` of every slot) record dollars placed per slot, the normal-tier daily cap and where it ran out, and answer
"did the 13:30 miss cause the 14:30 cap exhaustion, or is it a cap-seeding mismatch between the three entry points
(`docs/live_wiring.md`)?" with the numbers. No account number, no order IDs.
Non-goals: any change to caps or budget code.
Proof: a human opens the note; every figure cites a file and field; `git diff --stat` shows only that file.

## Rock 4 (#4): the digest can be COMPLETE; orders are attributed

In `claude/triage-routine/prompt.txt` and `docs/triage_routine.md`: define COMPLETE as "STEPs 1-7 ran, STEPs 2, 4 and 6 via
`triage_reconcile_batch` when it exited 0"; require STEP 1 to run `verify_schedule --orders-json` with the day's order IDs
and report `order_attribution` (or say the order list could not be fetched, which makes the run PARTIAL). Keep PARTIAL for
a batch error fallback.
Non-goals: relaxing HARD RULES; changing `triage_reconcile_batch.py`.
Proof: `.venv/bin/python -m pytest tests -q -k "triage and prompt"` if such tests exist, else
`git diff --stat` shows only prompt.txt and triage_routine.md and `rg -n "COMPLETE" claude/triage-routine/prompt.txt` shows one definition.

## Rock 5 (#5): executor-task status with less UNKNOWN

In the same two files, change the executor-slot status rule: when `list_scheduled_tasks` does not list a slot, keep `enabled`
UNKNOWN (past run evidence cannot prove a task is currently enabled), write `lastRunAt` from that slot's evidence in
`triage_log_summary` labelled "evidence-derived, lower bound" (`first_file_et` is only a lower bound), and write `nextRunAt`
as "not in local scheduler". UNKNOWN stays for `enabled` and for any field whose evidence is unreadable.
Non-goals: adding executor tasks to the local scheduler.
Proof: as Rock 4, plus `rg -n "UNKNOWN" claude/triage-routine/prompt.txt` shows `enabled` still UNKNOWN and no evidence-derived `enabled`.

## Rock 6 (#6): PR 2373 post-merge acceptance note (no code)

Write `docs/triage-cycle/2026-11-19/MARKET-DATA-2373-CHECK.md` following `docs/triage-cycle/MARKET-DATA-1530-CHECK.md`: what to
check after the operator merges PR 2373 (the next trading day's payload build times and chunk counts, which artifact files
and fields), and the stated condition under which the `market_data_unavailable` row may move off "PR open".
Non-goals: merging or reviewing 2373; any Notion write.
Proof: the note names concrete files and fields; `git diff --stat` shows only that file.

## Rock 7 (#7): unconfirmed recorded PRs, evidence table (no code)

Using read-only `gh pr view` and `git log --grep`, write `docs/triage-cycle/2026-11-19/UNCONFIRMED-PRS.md`: for each of
`robinhood_payload_rate_limited` (#2221), `bridge_python_alert_channels` (#2052), `run_states_unclassifiable` (#1514), what that PR
actually changed, whether it plausibly fixes the Key, and any PR or commit that does, so the operator can correct or confirm
the Notion cell. Mark each as confirm / correct / unknown.
Non-goals: writing to Notion; changing `selfheal/triage_reconcile.py` matching.
Proof: each row cites a PR URL under `https://github.com/badMade/all-stock-dca/` and the evidence line used.

## Order and gates

Rocks 1, 2 (code) first, then 4, 5 (prompt/doc), then 3, 6, 7 (notes). Every Rock: Codex builds Rocks 1 and 2 under
`workspace-write`; the notes and prompt edits are done by the Visionary. Existing suite must still pass for touched modules.
