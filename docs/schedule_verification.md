# Daily schedule verification

How to tell, for one ET trading day, whether each of the seven live roster
slots (`dca_runner.SLOTS`) actually fired and finalized, and the one command
each slot's Gate 9 runs so the answer can be "yes".

## Why this exists (2026-09-22)

The console's Schedule page showed every elapsed live slot as
`elapsed_unverified`, and no code ever changed that to verified or missing.
Checking `cron_tracking/` by hand for 2026-09-22 found:

| Slot | Run dir | `run_state.json` | Pointer |
| --- | --- | --- | --- |
| 09:35 | none | – | wrong shape, dated 09-16 |
| 10:30 | yes | **no** | dated 07-23 |
| 11:30 | yes | **no** | wrong shape, dated 09-17 |
| 12:30 | yes | **no** | missing |
| 13:30 | none | – | – |
| 14:30 | yes (free-form `gate9_report_*.json`) | **no** | wrong shape |
| 15:30 | none | – | missing |

The live-mirror prompts said "write run_state.json ... and refresh
`latest_<stream>_run.json`" without naming the code that does it. Each
session improvised, writing pointers shaped `{slot, trading_day, run_dir,
outcome}` that `scripts/same_day_pointer.validate_pointer` rejects as
`malformed` (missing `run_id`, `run_state_path`, `stream`). The slots fired,
but nothing they left behind could be verified.

## Daily alert (launchd)

`scripts/schedule_watchdog.py --notify` runs the watchdog below and, when any slot fails, also raises
a macOS notification naming the date and the failing slot ids. The watchdog's idempotent per-slot alert
files, exit code and report are unchanged. It exists because the Oct 13-16 2026 aborts each left a Gate 1/2
alert file nobody read (email undeliverable, message center per-session): files alone were not seen.

The agent template is `deploy/launchd/com.all-stock-dca.schedule-watchdog.plist.template`: it runs
Mon-Fri at 16:35, after the last slot's grace deadline. The Mac must be pinned to America/New_York
(launchd has no ET), and `DCA_CRON_TRACKING_ROOT` must match the root the live sessions write to.

```bash
sed -e "s|REPLACE_WITH_REPO_PATH|$PWD|g" -e "s|REPLACE_WITH_CRON_TRACKING_ROOT|$PWD/cron_tracking|g" \
  deploy/launchd/com.all-stock-dca.schedule-watchdog.plist.template \
  > ~/Library/LaunchAgents/com.all-stock-dca.schedule-watchdog.plist
launchctl bootstrap gui/$(id -u) ~/Library/LaunchAgents/com.all-stock-dca.schedule-watchdog.plist
launchctl kickstart gui/$(id -u)/com.all-stock-dca.schedule-watchdog   # run once now to test
```

If the first run is blocked by macOS privacy protection, grant the venv Python access to
`~/Documents` as for the robinhood-payload agent. Notifications may need allowing for
`osascript` under System Settings > Notifications.

## Stale-planner check (launchd)

`scripts/planner_stamp_check.py` answers one question each weekday morning: is the `backup_planner.pyz` you
pasted into Perplexity still the planner `origin/main` builds? Step 0 of every backup prompt demands the planner's
`content_sha256`, and that hash moves whenever any file inside the planner changes (it moved four times on
2026-10-21 for changes unrelated to the one being shipped). A stale planner costs a missed slot and nothing flagged it.
A cloud routine cannot run this check (a scheduled session has no repo or GitHub credentials, confirmed
2026-10-21), so it runs on the Mac next to the checkout.

* **Baseline** is the stamp inside your own `backup_planner.pyz`; nothing is stored anywhere else, so nothing needs
  updating after a repaste. Keep the attached file at `<checkout>/backup_planner.pyz`.
* **Fresh stamp** comes from `git fetch origin main`, then a planner built from that ref in a temporary directory.
  The checkout's working tree, branch and uncommitted edits are never touched.
* Exit `0` unchanged (silent), `1` stale, `2` could not check. `--notify` raises a macOS notification on `1` and `2`:
  a silent failed check looks exactly like "all good". The JSON report holds hashes, a commit and a status only.

```bash
python3 -m scripts.planner_stamp_check --planner backup_planner.pyz            # run it by hand
sed -e "s|REPLACE_WITH_REPO_PATH|$PWD|g" \
  deploy/launchd/com.all-stock-dca.planner-stamp-check.plist.template \
  > ~/Library/LaunchAgents/com.all-stock-dca.planner-stamp-check.plist
launchctl bootstrap gui/$(id -u) ~/Library/LaunchAgents/com.all-stock-dca.planner-stamp-check.plist
launchctl kickstart gui/$(id -u)/com.all-stock-dca.planner-stamp-check            # run once now to test
```

The agent runs Mon-Fri at 07:45, at least an hour before the earliest slot window opens (09:35), on Mac-local time
(pin the Mac to America/New_York). When it reports STALE: `git pull`, rebuild with
`python3 -m scripts.build_backup_planner --out backup_planner.pyz`, refill the prompts, run
`scripts.verify_perplexity_pair`, then attach the planner and repaste the prompts (see
`claude/dca-executor-prompts/perplexity/README.md`). It needs `git` to be able to fetch the remote without a prompt
(a credential helper or SSH key); if the fetch fails it reports ERROR instead of guessing.

## Verify a day

```bash
python -m scripts.verify_schedule                 # today, ET
python -m scripts.verify_schedule --date 2026-09-22 --json
```

For each slot it reads that slot's own dated directory
(`<stream root>/<YYYYMMDD>_<HHMM>ET/run_state.json`), not the stream pointer.
Intraday's single `latest_intraday_run.json` is overwritten by each of its
three slots and cannot say which slot wrote it.

| Status | Meaning | Fails the audit |
| --- | --- | --- |
| `verified` | `run_state.json` names this day, stream and `scheduled_for_et`, passes `derive_valid_scheduled_evidence`, and its directory meets the artifact contract (`validate_run_artifacts`, summary required). This proves the slot fired, not that any order filled | no |
| `evidence_incomplete` | The run directory exists but `run_state.json` is missing, unreadable, or not scheduled evidence for this slot | **yes** |
| `no_evidence` | The grace deadline has passed and there is no run directory | **yes** |
| `awaiting_evidence` | Past the nominal time, but before the window end + 30 min | no |
| `not_due` | Before the nominal time | no |
| `market_closed` | Gate 0 says the slot ends silently today and nothing was written | no |

Exit status is 1 when any slot fails. The console's Schedule page
(`dca_console_schedule.slot_evidence`) gives the same verdict through its
`BoundedReader`. It replaces `elapsed_unverified` on the matching live
manifest row (matched on both time and cron id) and adds a "Slot verified"
column to the roster matrix.

### Attribute broker order IDs (CLI and Python API)

`scripts.slot_attribution.attribute_orders(day, order_ids, root=...)` matches
broker IDs against `buy_candidates[].order_id` and
`orders[].order_id` / `broker_order_id` in each slot's own dated
`run_state.json`. It returns `attributed` (ID → slot), `unattributed` (IDs no
readable artifact names), and `ambiguous` (ID → multiple claiming slots).
Missing or malformed states contribute no claims; order times are never used
to guess a slot. A match is an artifact claim, not proof of a verified run or
confirmed fill.

Pass `order_ids=[...]` to `verify_day(day, now, root=...)` to include this result
as `order_attribution`. Omitting it preserves the existing report shape;
attribution never changes `ok`, `failing_slots`, or the CLI exit status.

From the command line, `--orders-json PATH` takes a JSON array of broker order
IDs (`-` reads it from stdin) and adds the same `order_attribution` block:

```bash
echo '["<order id>", "<order id>"]' | python -m scripts.verify_schedule --date 2026-10-20 --json --orders-json -
```

Text output lists each attributed ID with its slot, then any `unattributed` and
`ambiguous` IDs. The input must be a JSON array of non-empty strings: anything
else (unreadable file, invalid JSON, a non-list, a non-string or empty entry)
is a usage error, exit status 2, with nothing printed to stdout, so a verifier
never reports attribution for a silently filtered subset. Attribution is
reporting only; the exit status still reflects slot evidence alone.

### Reporting without `list_scheduled_tasks` (2026-10-20)

A verifier or digest session that lacks the `list_scheduled_tasks` tool may read
task status from `list_triggers`, but only when that tool actually lists the
executor tasks (`enabled`, `last_fired_at` and `next_run_at` stand in for
`enabled`, `lastRunAt` and `nextRunAt`). What `list_triggers` returns depends on
the session: on 2026-10-20 one run saw only unrelated reminders, while a session
checked on 2026-10-21 saw all seven executor tasks with their `last_run`. For any
executor task neither tool shows, the session reports `enabled`, `lastRunAt` and
`nextRunAt` as `UNKNOWN`, which is a finding and never a pass. Slot outcomes still
come from `verify_schedule` and `run_state.json`, and a task's `lastRunAt` is
never evidence of success.

A 2026-10-20 run grouped broker orders into clusters and guessed which slot made
each from order times. Those times do not line up with slot times (scheduler
jitter, backup periods), so use `--orders-json` above and report
`unattributed` and `ambiguous` IDs as such. Paste-ready wording for the external
verifier tasks is in `claude/triage-routine/EXTERNAL-PROMPT-FIXES-2026-10-20.md`;
`claude/triage-routine/prompt.txt` carries the same rules for the triage routine.

## Finalize a slot (Gate 9)

Every live-mirror prompt's Gate 9 now starts with:

```bash
python3 -m scripts.finalize_run_evidence --slot 1430 \
    --started-at 2026-09-22T14:30:41-04:00 \
    --outcome NO_ORDERS_GATE6_5_ZEROED --order-submit-status no_orders_submitted \
    [--orders-json $ROOT/finalize_orders.json] [--report-json $ROOT/gate9_report.json]
```

- It refuses a `--started-at` outside the union of `dca_runner.SLOTS`'s window
  and the prompt's own Gate 1 window (nominal − 5 min). The end of the window
  is exclusive, as in `gate_time_sanity`. An off-window fire is not a
  scheduled fire.
- Finalizer states keep `generated_at` and `finalized_at` as the artifact write
  time. Risk accounting uses `run_classification.triggered_at_utc`, floored at
  the trading day's nominal `scheduled_for_et`, so delayed backfills retain
  their original date and accepted early afternoon fires count toward the
  afternoon guard. A missing invocation clock is not replaced by write time.
- It refuses order rows that aren't real orders. Every row needs a ticker, a
  broker `order_id`, a positive `submitted_amount_usd` and a status from
  `ORDER_ATTEMPT_STATUSES`, and duplicate ids are rejected. `submitted` also
  requires `--budget-json` (Gate 6's tier and caps). Without a budget, the
  state records the schema's `budget_evaluated: false`, and `counts` is always
  written.
- It redacts the pinned account number from `run_classification`, leaving
  `account_number_redacted: true` in its place.
- It stamps the run_classification the stream's own writer would:
  `<Executor>(mode="live").run_classification()` for 09:35–14:30, and
  `build_afternoon_live_run_classification` for 15:30.
- It writes `run_state.json` and `run_summary.json`, plus header-only CSVs
  where missing, then checks them with `validate_run_artifacts`.
- For states it builds, `generated_at` records the supplied fire time
  (`--started-at`), clamped to no earlier than the nominal slot time for
  accepted early starts. The classification retains the actual start, while
  `finalized_at` records when finalization ran. This keeps historical and
  early-start afternoon evidence eligible regardless of the backfill's clock.
- It refreshes the pointer through `same_day_pointer.refresh_latest_pointer`,
  or `update_latest_scheduled_run_pointer` for 15:30, then re-validates the
  pointer and the slot. It exits 0 only when both verify.
- A `run_state.json` written by an executor's own `run()` is never
  overwritten; the command only refreshes the pointer from it. If the summary
  or a CSV is missing beside it (the executor was interrupted), it writes just
  that missing file.
- It refuses a zero-order finalize (`no_orders_submitted` or
  `not_submitted_no_candidates`, without `--error-class`) that can't show
  Gate 5 actually evaluated data. On 2026-09-18 the 12:30 and 3:30 sessions
  lost market data, hand-wrote an empty candidates file and ended silently as
  a "quiet tape" (Notion keys `fabricated_empty_candidates`,
  `logged_as_quiet_tape`). The command finds this slot's generator document
  in the run directory by content (the count fields plus a matching
  `runner_slot_id` and `trading_day`), not by filename. It then requires:
  - `not_submitted_no_candidates`: the document must exist.
  - `no_orders_submitted`: a missing document is fine, because a Gate 4
    stand-down ends before Gate 5. A `*cand*.json` that isn't a document is
    refused, since that is the fabrication signature.
  - Zero buys: at least one usable ticker, counted from the document's
    `candidates` rows (not `NO_DATA`, not a provider error). The 15:30
    document has no `no_data_count`, and a provider-error ticker is itself a
    `NO_DATA` row. One provider error is routine and doesn't block.
    `evaluated_count: 0` with `already_covered_tickers` is Gate 4's served
    case.
  - A file with duplicate JSON keys is invalid evidence, whatever its name.
  - A `run_state.json` this command didn't write gets the same check before
    the pointer is refreshed. For an executor's own state, the check uses its
    `counts.evaluated` and `counts.no_data`; a state without them (hand-written)
    falls back to the directory's generator document.

  The refusal tells the session to finalize an outage honestly, as
  `not_submitted_error --error-class market_data_unavailable`, which then
  alerts. A passing check is recorded as `gate5_evidence` in `run_state.json`.
  A fully forged generator document still gets through; this catches the
  shortcut that actually happened.
- Everything after argument checks runs under a per-run `cross_process_lock`
  (`.<run dir>.finalize.lock` beside the run directory), so overlapping
  retries can't drop each other's orders. A state this
  command wrote earlier (a retry of the same fire) is rebuilt, and its orders
  are merged by `order_id`.

Exit codes: 0 means verified. 1 means files were written but do not verify
(the prompt alerts with the printed errors). 2 means the command refused and
wrote nothing.

`python3 -m scripts.check_live_prompt_drift` reports a live `SKILL.md` that
never runs this command as `missing_finalize_evidence`.

## Watchdog: alerting independently of the executor session (2026-09-25)

`scripts/verify_schedule.py` answers "did the slot fire" from durable
evidence; it does not alert anyone on its own. Every alert channel an
executor session has (in-app message center, the Gate 9 repo alert file,
email) is written **by that same session** -- so a session that reads a
real or perceived conflict in its own instructions and ends by asking an
absent user to confirm, instead of following the standing rule ("a decline
in a scheduled run is an abort that must alert on all three channels ... it
must never end on a question", `docs/safeguards.md`'s live execution roster
note), never reaches its own alerting step either. That pattern is recorded
on 2026-09-14, 09-15, 09-17, 09-22 x2, and 2026-09-25 (12:30 ET, before
Gate 4) -- each one a day that would have looked clean to every existing
channel.

`scripts/schedule_watchdog.py` closes that single point of failure. It is a
thin wrapper over `verify_day` -- it adds no new evidence detection, only
alerting on what that function already finds -- and it does not depend on
the executor session at all:

```bash
python -m scripts.schedule_watchdog                       # today, ET; text report
python -m scripts.schedule_watchdog --date 2026-09-22 --json
python -m scripts.schedule_watchdog --slack \
    --slack-webhook-env DCA_WATCHDOG_SLACK_WEBHOOK          # falls back to
                                                             # DCA_ALERT_SLACK_WEBHOOK,
                                                             # then DCA_DRIFT_SLACK_WEBHOOK
```

For every slot whose status is not `verified`/`not_due`/`awaiting_evidence`/
`market_closed`, it:

* writes its own durable, idempotent alert file (`write_watchdog_alert_files`,
  not `dca_alerts.write_alert_file` -- that writer resolves its root
  independently of `--root` and numbers every collision, both wrong for a
  recurring check reporting the same still-open absence) --
  `<root>/schedule_watchdog/alerts/<day>-<slot>-SCHEDULE_WATCHDOG.txt`, the
  same `<day>-<slot>-<gate>.txt` shape `scripts/alert_digest.py` already
  parses. `SCHEDULE_WATCHDOG` is registered in that module's `_REASON_CODES`
  so `detect_gate_evidence` recognizes it and buckets it separately from a
  gate an executor reported on itself, rather than loading as `gate=None`;
* re-running it for a still-unresolved (day, slot) overwrites that same file
  rather than creating a numbered duplicate, so `alert_digest`'s per-slot
  counts and streaks reflect one unresolved incident, not one per invocation;
* optionally POSTs a Slack summary of every failing slot, opt-in and
  env-only like every other webhook path in this repo (`--slack`, no secret
  ever in git);
* exits 1 when anything is failing, 0 otherwise.

**Deploying this.** Same constraint as the seven executor tasks: registering
a recurring invocation lives outside this repo (the external scheduler/Cowork
-- see `docs/live_wiring.md`), so committing this script changes nothing on
its own. A slot stays `awaiting_evidence` (excluded from an alert) until its
OWN acceptance window ends plus `GRACE_MINUTES` (30) -- not merely until its
nominal time -- so on this roster (windows run 25-30 minutes past nominal)
the earliest a fire usefully catches a silent slot is roughly 55-60 minutes
after nominal. Run it with a few more minutes of margin on top of that, or
once daily well after the last slot's deadline.
If the invocation is itself an agent session rather than a bare cron job,
give it nothing to decide -- its only job should be running this command and
relaying the result, since the exit code has already made the ABORT/alert
call. That is deliberately the opposite of the failure mode this script
exists to catch.

## Post-slot submission check (2026-10-07)

`verify_schedule` proves a slot fired and finalized, "not that any order
filled". On 2026-10-07 the 09:35 slot stopped at Gate 7 when the auto-mode
classifier refused `place_equity_order` for TSM before it reached the broker:
4 of 20 eligible buys filled, 16 were never sent, and every existing check
passed. `scripts/check_slot_fills.py` reads the slot's own dated run directory
and fails loudly:

```bash
python -m scripts.check_slot_fills --slot 0935 --date 2026-10-07 --json
```

| Fails when | Source |
| --- | --- |
| any `.json` / `.txt` / `.md` / `.log` / `.csv` file in the run directory contains `PLATFORM REFUSAL` (case-insensitive), or a report is unreadable, or is a symlink out of the root | report text |
| `counts.submitted != gate9_report.eligible_count - sum(gate9_report.dropped)` | `run_state.json` (see below) |
| `run_state.json` is missing, unreadable, not an object, has duplicate keys, or its `trading_day` differs from `--date` | fails closed |
| a count is missing, not an integer, or negative | fails closed |

`gate9_report` is the object a slot passes to `finalize_run_evidence
--report-json`. The check needs `eligible_count` and a `dropped` object keyed
exactly `price_recovered`, `spread_over_1pct` and `daily_cap` (all three required). A slot that omits them
fails closed, so the live prompts must supply them. The seven `live-mirror/` prompts now instruct this in their `--report-json` bullet (`eligible_count` = Gate 5 buy candidates before Gate 6.5; `price_recovered` = refresh or per-order revalidation drops; `spread_over_1pct` = Gate 6.7 zeros; `daily_cap` = cap or same-day top-up-guard zeros). Editing a mirror changes nothing live: apply each body through `update_scheduled_task`, then run `check_live_prompt_drift`. Exit 0 passes, 1
fails. Read-only; it never contacts a broker.

This is a submission-completeness check, not a settlement check. `counts.submitted` counts every order row recorded, including `pending`, `unconfirmed` and `potentially_submitted` ones, so a pass means every eligible buy was sent or accounted for, not that each one filled. Confirmed fills are Gate 8's job (and the order-history sync).

**Deploying this.** Like `schedule_watchdog`, registering the post-slot
invocation lives outside this repo (the external scheduler/Cowork). Run it once
per slot after Gate 9, for example as the last command of each live task, or
from a scheduler entry a few minutes after the slot's window ends.

## Deploying the prompt change

Editing `claude/dca-executor-prompts/live-mirror/` changes nothing on its
own (see that directory's README). A human applies each mirror to its live
task through `update_scheduled_task`'s full-body replacement, with the account
placeholder expanded, and then runs `check_live_prompt_drift` to confirm that
no slot reports `missing_finalize_evidence`.
