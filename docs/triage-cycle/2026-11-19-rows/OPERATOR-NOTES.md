# Operator notes (cycle 2026-11-19 rows): decisions and paste-ready text

Nothing here was written to Notion. Facts cite a repo path, a PR, a run artifact field, or a Notion row Key; anything that cannot be checked
from those is marked "unverified".

## 1. Progress column: one design decision (issue 9)

Both halves of the issue are already handled by existing rules:
- A Progress cell holding free text is kept and the derived label is reported beside it: `selfheal/triage_reconcile.py` `is_progress_label` and the batch's
  `progress_note_kept` (`scripts/triage_reconcile_batch.py`), and the prompt's STEP 4 bullet. On 2026-11-19 night this affected 20 of 39 Approved rows.
- The Notion SQL table for DCA Issues exposes `createdTime` only (verified from the fetched schema), so `last_edited_time` must come from fetching the page before a write.
  That is the prompt's STEP 3 / HARD RULE 6 instruction to the routine; the batch has no timestamp input and does not enforce it.

**Recommendation (your call):** add a text column "Derived progress" that the digest may write, and leave "Progress" for your notes. Effects if accepted:
the digest's only Notion write to existing rows would move from `Progress` to `Derived progress` (`WRITABLE` in `scripts/triage_reconcile_batch.py`, prompt HARD RULE 1 and STEP 4),
`progress_note_kept` would stop being needed, and the 20 notes would stop hiding derived labels. If rejected, nothing changes. Deferred in `DEFERRED.md` until you decide.

## 2. `output_cap_exceeded` row: paste-ready update (issue 10)

Why: the row's Progress ends at "12:30 still open" and predates the same-day baseline chain; the digest is propose-only and cannot write it.

Verified facts:
- 2026-11-05 15:30 recurred (row's own note, Notion Key `output_cap_exceeded`; PR 2345).
- 2026-11-12 the 2:30 PM run aborted `ABORT_GATE6_HISTORY_NOT_PERSISTABLE` / `output_cap_exceeded` with 0 orders (`docs/adr-incremental-order-history.md`, line about 2026-11-12; `scripts/find_order_baseline.py` docstring). The SAME-DAY BASELINE CHAIN rule followed (live-mirror prompts `claude/dca-executor-prompts/live-mirror/1330ET-130-pm-afternoon-buyiing-run.txt` line 97, the 14:30 and 15:30 files).
- The chain worked on at least these runs: 2026-11-16 13:30 (`intraday_dca/20261116_1330ET/run_state.json` `day_history_mode: baseline_merge`), 2026-11-19 14:30 (`intraday_dca/20261119_1430ET/run_state.json` `history_mode: day baseline_merge (1230 base)`), 2026-11-19 15:30 (`ce16ba6b/scheduled_runs/20261119_1530ET/run_state.json` `history_mode: baseline_merge (14:30 base) then incremental`).
- 2026-11-19 13:30 aborted at Gate 6 with 0 orders, no `day_history_mode` or `history_mode` recorded (`intraday_dca/20261119_1330ET/run_state.json` `error_class: output_cap_exceeded`; `gate9_report.json` reason "today's get_equity_orders history (about 37 orders) could not be saved verbatim"), although earlier slots that day had saved valid same-day envelopes (`orders_pages_2026-11-19_0935/1030/1130/1230.json`). `scripts.triage_log_summary --date 2026-11-19` flags it `baseline_unused=true` (PR https://github.com/badMade/all-stock-dca/pull/2378).
- Dollar impact was close to zero: the normal-tier $180 cap was already nearly spent by 12:30 (`docs/triage-cycle/2026-11-19/CAP-CHECK.md`).

Hypothesis, unverified: because the chain was recorded as working on 2026-11-16 13:30, a stale live 13:30 prompt is less likely than the session skipping or failing the
`find_order_baseline` step. The 2026-11-19 13:30 session transcript would settle it; this repo cannot see it. Diffing the live 13:30 routine prompt against its live-mirror file is still worth doing.

Suggested Progress text (replace; keep Status Monitoring and Fix state Repaired until a 13:30 run completes with the chain):

> Recurred 2026-11-05 15:30 (week-scoped history too large to save): WEEK BASELINE REUSE rule, PR #2345. Recurred 2026-11-12 14:30 (day history ~35 KB): SAME-DAY BASELINE CHAIN (docs/adr-incremental-order-history.md) in the 13:30/14:30/15:30 prompts. The chain worked 2026-11-16 13:30 and 2026-11-19 14:30 and 15:30. Recurred 2026-11-19 13:30 (0 orders; no history mode recorded although earlier slots had saved baselines; scripts.triage_log_summary reports baseline_unused=true, PR #2378). Cause undetermined: session skipped/failed find_order_baseline vs stale live prompt. Open: read the 11-19 13:30 session transcript and diff the live 13:30 prompt against live-mirror.

## 3. Two rows whose reconciler skip is noise

`robinhood_payload_rate_limited` (PR 2221) and `bridge_python_alert_channels` (PR 2052) already carry hand-set PR, Commit URL and (for the first) Branch cells with operator notes
saying so; `recorded_pr_unconfirmed` skips them every run only because neither PR's text names the Key. No action needed from you beyond leaving them; the new digest section lists
them as "operator-recorded, not matched by text".

## 4. Still owed from cycle 1

Sync the scheduled digest task's prompt (`~/.claude/scheduled-tasks/dca-triage-digest/SKILL.md`) to `claude/triage-routine/prompt.txt`; this cycle's prompt edits (the "Unsupported fix claims" section) need the same sync.

## 5. Found while doing this, not fixed

The `baseline_unused` check shipped in #2378 keys on `day_history_mode` being absent from `run_state.json`, but the 2026-11-19 14:30 and 15:30 runs recorded the mode under `history_mode`
(gate9 notes) instead. A slot that used the chain under that name and still aborted would be mis-flagged. Candidate next-cycle Rock: also read `history_mode` / `gate9_report`.
