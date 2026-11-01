# Triage routine: log access (2026-10-31)

The 2026-10-30 and 2026-10-31 triage digests opened with "LOGS UNAVAILABLE in this container", so they
missed Friday 2026-10-30's 9:35 `ABORT_GATE7_WINDOW_INSUFFICIENT` (fired 36 min late) and the 3:30
`ABORT_GATE5_ALL_PROVIDER_ERRORS`. They also reported `list_scheduled_tasks` / `list_triggers` unavailable.

## Cause

The triage routine is a **cloud routine**: its prompt (`prompt.txt`) runs "in a fresh container" and
clones the repo. No triage task exists in `~/Claude/Scheduled/` or `~/.claude/scheduled-tasks/` on the Mac.
The run files live only on the Mac, under the checkout's gitignored `cron_tracking/`, so a cloud clone never
has them, and `list_scheduled_tasks` is a Mac-side tool.

There was a second gap even with the files present: `verify_schedule --json` reported `ok: true` for
2026-10-30, because both failing slots fired and finalized. The outcome lives in each slot's
`run_state.json` `status` and `gate9_report.notes`, which STEP 1 never read.

## What changed in the repo

- `scripts/triage_log_summary.py`: one redacted per-slot summary (outcome, order_submit_status, order count,
  error class/detail, gate9 notes, first/last file times, attention flag). Defaults to the latest trading day,
  so a Saturday run reads Friday. Removes the account number and any account-shaped digits, and emits paths
  relative to the root only. Exit 2 with `LOGS UNAVAILABLE` when the tree cannot be read.
- `prompt.txt` STEP 1: find a shell that can read the tree (a local checkout, else the `remote-devices`
  bridge at `$HOME/mnt/all-stock-dca`, read-only), run the summary first, and stop judging access by
  `$DCA_CRON_TRACKING_ROOT` alone (the repo resolves the root itself).

Dry run on the Mac, 2026-10-31, `python -m scripts.triage_log_summary --date 2026-10-30`:

```
09:35  market_open  ABORT_GATE7_WINDOW_INSUFFICIENT      submitted (32)   <-- attention
       gate9_notes: Fired 36 min late (10:11 ET vs 09:35). ... 32 compatible agentic orders (9:35 backup period) ... placed nothing.
10:30  morning      ORDERS_PLACED                        submitted (6)
11:30  intraday     ORDERS_PLACED_OPERATOR_OVERRIDE      submitted (12)
12:30  midday       NO_ORDERS_GATE6_DAILY_CAP_REACHED    not_submitted_budget_exhausted (0)
13:30  intraday     NO_ORDERS_GATE6_DAILY_CAP_EXHAUSTED  not_submitted_budget_exhausted (0)
14:30  intraday     ABORT_GATE6_DAILY_CAP_EXHAUSTED      not_submitted_budget_exhausted (0)
15:30  afternoon    ABORT_GATE5_ALL_PROVIDER_ERRORS      not_submitted_error (0)   <-- attention
       error_detail: generate_buy_candidates scored 91 of 91 tickers NO_DATA (provider_error_count 91) from the prebuilt Robinhood payload
ATTENTION: 0935, 1530
```

## Operator steps (outside the repo; not applied by the agent)

Do these after this PR merges, so the routine reads the new `prompt.txt` and script from `main`.

**Recommended: run the triage as a local Mac scheduled task.** It is the only option verified to read the
tree, and it also gives the routine `list_scheduled_tasks`, which fixes the "enabled/lastRunAt/nextRunAt
UNKNOWN" finding.

1. Create a local scheduled task in the Claude desktop app (the same kind as `dca-end-of-day-digest`):
   - name `dca-triage-digest`, daily at 18:15 ET (keep the weekend runs; the summary reads Friday then);
   - folder: the checkout `~/Documents/Git/all-stock-dca`;
   - connectors: Notion and GitHub, as the cloud routine has today;
   - prompt: the full text of `claude/triage-routine/prompt.txt` from `main`, with SETUP's first line
     changed from "Clone or fetch badMade/all-stock-dca (default branch)" to "Use the checkout in the
     attached folder; run `git fetch` but do not switch its branch or touch its working tree (the operator
     commits in it)".
2. Run it once by hand and confirm the digest has no `LOGS UNAVAILABLE` line and names 0935 and 1530 for
   2026-10-30 (run it with `--date 2026-10-30` in STEP 1 for that check only).
3. Then disable the cloud triage routine, so two routines never write the same Notion rows.

**Alternative: keep the cloud routine.** Paste the new `prompt.txt` into it. This only helps if the routine's
session has the `remote-devices` connector (`mcp__remote-devices__device_bash`) and the Mac bridge is up at
18:15; that has not been verified for a cloud routine, and it still leaves `list_scheduled_tasks`
unavailable. Without the bridge it keeps reporting `LOGS UNAVAILABLE`, now with the reason named.

The account number never leaves the Mac in either option: the summary carries no broker block and redacts
account-shaped digits.
