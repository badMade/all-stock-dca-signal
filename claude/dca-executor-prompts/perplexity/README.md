# Perplexity backup prompts (plan-first design)

Six generated prompts for the standing Perplexity backup executor, one per scheduled task:

| File | Task | Fires (ET) | Backs up slot |
| --- | --- | --- | --- |
| `0955ET-935-backup.txt` | `4412c3c4` | 09:55 | 0935 |
| `1050ET-1030-backup.txt` | `e028361e` | 10:50 | 1030 |
| `1150ET-1130-backup.txt` | `e3590042` | 11:50 | 1130 |
| `1250ET-1230-backup.txt` | `062605c6` | 12:50 | 1230 |
| `1350-1450ET-1330-1430-backup.txt` | `54ab24d2` | 13:50 and 14:50 | 1330 and 1430 (the clock decides) |
| `1545ET-1530-backup.txt` | `f83b8d62` | 15:45 | 1530 |

Design: `docs/adr-plan-first-backup-executor.md`. These files are generated, never hand-edited:

```bash
python3 -m scripts.generate_perplexity_backup_prompts --write   # regenerate
python3 -m scripts.generate_perplexity_backup_prompts --check   # exit 1 on drift (also a test)
```

## Status: both build blockers closed; the operator confirms shadow mode before repasting

Like the `live-mirror/` prompts, these are a repo copy. Nothing here changes a live task until a human
repastes a prompt into its Perplexity task. Both blockers below are closed. The one remaining condition
is operational: shadow mode (ADR phase 6) must have run for at least five trading days before a repaste,
and only the operator can confirm that. The repo cannot show it. Section "Keeping the stamp current"
says when a repaste is due.

1. **CLOSED 2026-10-07: `scripts/backup_planner.py` now exists** and implements this contract (library logic
   in `dca_order_plan.py`, tests in `scripts/tests/test_backup_planner.py`). Every subcommand prints one JSON
   document and exits 0 when usable, 2 on any failure (fail closed, nothing placed). It runs from a repo
   checkout, so today only the Mac paths can use it; the sandbox form still needs blocker 2.

   | Command | Input | Output |
   | --- | --- | --- |
   | `plan --source mac_plan --trading-day D --slot S` | the plan file at `dca_order_plan.order_plan_path(D, S)` | `{"status": "VALID_PLAN" or "NO_VALID_PLAN", "plan", "errors"}` via `load_plan` |
   | `compute --trading-day D --slot S --quotes F --orders-pages F --buying-power USD --now T` | quotes file `{"quotes": [...]}`, a poll-1 orders envelope (from the ISO-week Monday), fresh buying power | `{"status", "plan", "errors"}` via `compute_plan_from_file` with the Normal tier |
   | `finalize --trading-day D --slot S --plan F --orders-pages F --fresh-quotes F --fresh-spreads F --buying-power USD --now T` | a plan, a poll-2 envelope, `{"SYMBOL": price}`, `{"SYMBOL": {"bid", "ask"}}`, fresh buying power | `{"status": "PROCEED", "SHADOW_ONLY" or "ABORT", "to_place", "would_place", "skipped", "errors"}` via `reconcile` then `revalidate_for_placement`; for a computed plan, first re-derives the trailing-7-day figure from poll 2 and ABORTs (`kill_switch_engaged_since_plan`) if the kill switch is now tripped and any order is above 1x (`--now` must be on the trading day in ET) |

2. **CLOSED 2026-10-08: the sandbox build exists, and these prompts now use it.** `python -m scripts.build_backup_planner
   --out PATH` builds `backup_planner.pyz` (about 1.4 MB, deterministic, stamped with the source commit; `--check`
   reports drift). The prompts run it in the sandbox (`python3 backup_planner.pyz ...`), verify it with
   `--build-info` first, and record the build in the state record. **How the file reaches the task is still
   undecided**: the prompts assume it is attached to the task's project files, a setup step you must confirm.

## Plan-first recommendations (needs PR #1667)

The Claude slots are moving to plan-first readers (PR #1667, `docs/plan_first_recommendations.md` there). Each slot's
set is the primary's own `{ticker, usd, ref_id}` list, which these backups now read first:

| Command | Where | Output |
| --- | --- | --- |
| `plan --source mac_rec --trading-day D --slot S --now T` | Mac | The slot's set from the local store, by the reader's freshness rule (`config/recommendations.json` `max_age_minutes`), as a `plan_first` plan |
| `plan --source published_rec --published F --trading-day D --slot S --now T` | sandbox | The same, from the payload published to the `recommendations` branch (or the Notion block once `page_id` is set). The Mac's `snapshot_cycle publish` launchd job pushes that branch (`docs/plan_first_recommendations.md`) |
| `executor-ids --trading-day D --slot S` | Mac | The broker order ids Claude saved for the slot: the reader's `placements/<slot>/<TICKER>.json` and the live prompt's `place_<TICKER>.json` in the slot's run directory, with a per-source file count in `sources` |
| `classify --trading-day D --slot S --orders-pages F [--executor-ids F] [--published F]` | sandbox | `CLEAR` or `UNRECOGNIZED` for every order inside this slot's window |

A missing, stale or malformed set is `NO_VALID_PLAN`, and the backup falls back to `mac_plan`, `mac_compute` and
`cloud_compute` as before. A set is placed like a Claude plan (reconciled against this slot's window only), but
`finalize` requires a fresh prior close per symbol (`--fresh-prior-closes`) because a set carries no prices, and the
caps are the Normal tier's, trimmed to fit (the prefix that fits is placed and the rest is reported in
`skipped`; 2026-10-22, `docs/adr-plan-first-backup-executor.md` D4).

The broker echoes its own ref_id, so Claude's plan-first orders carry no `dca-runner-` reference. `classify`
recognises executor orders by the order ids `executor-ids` prints, by a legacy `dca-runner-<slot>-` reference or the
native UUID if the broker ever echoes it, and, with the Mac out of reach, by a buy matching the published set's
symbol and dollar amount to the cent. Any other order is UNRECOGNIZED, and the backup stands down.

**2026-10-26 09:35 stand-down.** The 25 orders that read as UNRECOGNIZED were not Claude's: Claude's 09:35 run
finalized at 09:40 with no orders, and a manual 09:46 run of this slot's own backup (task `4412c3c4`) placed them
on a cloud-computed plan; the 9:57 scheduled run then stood down on them. See
`docs/incident_2026-10-26_0935_unrecognized_orders.md`. The empty `executor-ids` list was correct for that day.
It did expose a real gap, fixed since: `executor-ids` used to read only the reader's `placements/<slot>/`. The
reader prompts (`drafts/plan-first/`) were never pasted live, so every Claude slot still runs the gate-sequence
prompt in `live-mirror/`, which saves each response as `$ROOT/place_<TICKER>.json` in the slot's run directory
(`market_open_dca/<YYYYMMDD>_0935ET/` for 09:35). No `placements/` directory exists in that case (`begin` creates
it before any order, so its absence means the reader never ran, not that a write failed), so Claude's own orders
would have read as UNRECOGNIZED on a day it did buy. `executor-ids` now reads both. Check after any Claude slot
that bought:
`sources.live_prompt_placements` plus `sources.plan_first_placements` should equal the number of orders placed. Zero
for a slot that bought means the recognition chain is broken, and the backup will stand down (fail closed).

## Which slots take the safety-gate path

All seven, read from `scripts.backup_planner.GATED_SLOTS` when the prompts are generated. Each prompt asks the planner
for the symbols to fetch (`backup_planner.pyz universe`), dumps the raw `get_equity_historicals` (5-minute under `historicals`, the previous
session's daily bar under `daily`) and `get_equity_quotes` responses to `raw.json`, runs `assemble-market-data`, then `compute --market-data market.json
--deployed-last-7d-usd auto`, so the primary's own Gate 5 decides what to buy and the planner derives the kill-switch
figure from the order history. No prompt names a stock or mentions Yahoo; the stocks live in the planner's configuration.

Slot 15:30 runs the final-buy engine; the six executor-class slots run the stream CLI path, scored identically from
Robinhood (`scripts/backup_stream_specs.py`). 10:30 and 12:30 are not special: no pre-flight, no Finance source, no
per-ticker exception. A slot dropped from `GATED_SLOTS` falls back to a quotes-only prompt that reports `shadow_only`.

## Placement gates (added 2026-10-07 after review)

- **Only `PROCEED` places.** A plan written by `compute` finalizes as `SHADOW_ONLY` (would-be orders reported,
  nothing placed). `compute --market-data` now runs the primary's own Gate 5 for all seven slots (15:30 through
  `generate_buy_candidates`, the six executor-class slots through `scripts.stream_candidates`), so that gap is
  closed everywhere. The switch `scripts.backup_planner.COMPUTE_PLACEMENT_ENABLED` was turned on 2026-10-13 (operator
  decision: a backup that cannot reach the Mac places its own computed plan). It still places only when `finalize` is
  given `--local-status off|errored|unreachable`; without it, or for `unknown`, `in_progress` or `succeeded`, a
  computed plan stays `SHADOW_ONLY`. A session with no device-listing tool counts as `mac_status not_found`, and the
  Mac `claim` and `confirm` run only when the Mac is online.
- A Claude plan is reconciled against this slot's window only (later slots may top up); a computed plan excludes
  any ticker bought earlier that day.
- Buying power and a per-ticker bid/ask spread are required inputs and fail closed.

## Decisions these prompts encode that need confirmation

- **Latest placement time** is the next canonical slot's start (10:30, 11:30, 12:30, 13:30, 14:30, 15:30; 16:00
  for the final slot), so a backup that fires inside a slot's window can still finish before the next slot's gates.
- **Tier follows the signal count.** The planner's gated `compute` and `finalize` pick the budget tier the way the primary does: at most 5 signals at 3x or more is Normal ($180 per day, $900 per week), 6 to 10 Elevated ($240, $1,200), 11 to 17 High-volatility ($300, $1,500), more than 17 Stress ($360 per day). Stress has no verified rollover reserve in the backup, so it keeps the Normal $900 weekly cap. A batch over the selected tier's cap is still refused whole, never trimmed. The shadow `compute --quotes` path stays Normal.
- **Time sanity** accepts a fire from 10 minutes before its fire time until the latest placement time.
- **Two polls only.** Claude's own prompts also re-fetch order history before every order; the backup does
  not, so a batch that runs long relies on the per-order quote check and the one-order-per-ticker rule.
- **Attribution.** Backup orders reuse the planner's `dca-runner-<slot>-<day>-<TICKER>` reference (the plan's
  `ref_id`), so Claude's and the backup's orders are indistinguishable by reference alone; the state record is
  the only attribution. Broker-side deduplication on a reused reference is unverified (ADR decision D6).

## Placeholders to fill when repasting (2026-10-08)

The prompts no longer contain an account number. Before pasting, set these in the Perplexity task:

| Placeholder | Meaning |
|---|---|
| `${ROBINHOOD_AGENTIC_ACCOUNT_LAST4}` | Last 4 digits the account lookup must match; the run stops if not exactly one tradable account ends in them |
| `${PPLX_RUN_STATE_DIR}` | Directory for the run-state JSON and `submitted_orders.csv` (the project files `cron_tracking/<task id>` folder) |
| `${DCA_ALERT_EMAIL_ADDRESS}` | Alert address, unchanged |

### Filling them from local values (never in the repo)

`scripts/fill_perplexity_backup_prompts.py` writes a private, filled copy of all six prompts from values you keep
locally, so the repo copies stay free of them:

```bash
python3 -m scripts.fill_perplexity_backup_prompts --out ~/perplexity-filled
```

The state folder is the one the prompt names, and the live tasks already write under the repo task id
(`cron_tracking/<task id>/`; checked 2026-10-31 against the 2026-10-30 runs of `4412c3c4`, `062605c6` and
`f83b8d62`). So the default, no `--folder`, keeps each task's existing folder and run history. An earlier version
of this section said Perplexity names the folder after the task's automation UUID; the runs show it does not.
Pass `--folder <task id>=<folder name>` only to move a task to a different folder on purpose. With at least one
`--folder`, every prompt must be mapped and no two tasks may share a folder.

| Placeholder | Where the script reads it |
|---|---|
| `${ROBINHOOD_AGENTIC_ACCOUNT_LAST4}` | `ROBINHOOD_AGENTIC_ACCOUNT_LAST4`, else the last four digits of `ROBINHOOD_AGENTIC_ACCOUNT_NUMBER` |
| `${DCA_ALERT_EMAIL_ADDRESS}` | `DCA_ALERT_EMAIL_ADDRESS` |
| `${PPLX_RUN_STATE_DIR}` | `--state-root` (default `cron_tracking`) plus the `--folder` name for that prompt's task, so no two tasks share a folder |

Environment variables win; otherwise the gitignored local `.env` is read (`docs/local_env_file.md`). The script
fails closed and writes nothing if a value is missing or malformed, a prompt has no task header, or any other
`${NAME}` is left over. It refuses an `--out` inside this checkout, writes owner-only files and never prints a
value. Regenerate the repo prompts first (`--write`), rebuild `backup_planner.pyz` if the stamp changed, then fill.

## Checking the pair before you paste it

The prompts and `backup_planner.pyz` are pasted into two different places in Perplexity (each task's Instructions box
and the project's Files), and nothing compares them until a slot fires. On 2026-10-14 the 9:55 task ran an earlier pair
than the one believed pasted. Run this on your own machine first, against the files you are about to paste:

```bash
python3 -m scripts.verify_perplexity_pair --planner backup_planner.pyz --prompts ~/perplexity-filled
```

It exits 0 only when every prompt's planner stamp, universe hash and symbol count equal what the planner itself reports
and no `${...}` placeholder is left. Pass `--allow-placeholders` to check the unfilled prompts committed here. It only
reads, and it prints file names, the first eight hex digits of a hash, counts and placeholder names, never your last
four digits, email or state folder. It cannot see what is saved inside Perplexity: after pasting, still read each task's
Instructions header (`Universe hash ...` and the symbol count) and, after the next run, the state file's
`planner_build.content_sha256`.

### Morning stale-planner notification (Mac)

```bash
python3 -m scripts.planner_stamp_check --planner backup_planner.pyz --notify
```

This reads the stamp inside your attached planner, fetches `origin/main`, and builds that commit in a temporary
directory without changing your branch or working-tree files. It prints JSON and exits 0 when current (no
notification), 1 when stale, or 2 when it cannot check or deliver a requested notification. Errors do not include
local paths or raw Git output. Git authentication must already work without interaction.

For a weekday 07:45 check, replace `REPLACE_WITH_REPO_PATH` in
`deploy/launchd/com.all-stock-dca.planner-stamp-check.plist.template`, save it as
`~/Library/LaunchAgents/com.all-stock-dca.planner-stamp-check.plist`, and register it with
`launchctl bootstrap gui/$(id -u) ~/Library/LaunchAgents/com.all-stock-dca.planner-stamp-check.plist`.
The Mac must use America/New_York time (or adjust the template's hour); allow notifications for `osascript`.
Logs go to `/tmp/dca-planner-stamp-check.log`. Installing the template is an operator step, not automatic.
Keep the file passed via `--planner` identical to the file actually attached in Perplexity. A stale verdict only
alerts: rebuild, regenerate/fill and verify the prompt pair, then reattach and repaste it yourself.

**Knowing when the pair goes stale (2026-10-21).** The planner stamp changes whenever any planner input changes, so a
pair that verified yesterday can be stale today. `python3 -m scripts.planner_stamp_check --planner backup_planner.pyz`
compares your attached planner with what `origin/main` builds, and a launchd agent can run it each weekday morning
and notify you; see "Stale-planner check (launchd)" in `docs/schedule_verification.md`.

## Signal-first prompt and the `procedure` command (2026-10-30)

Perplexity's Instructions box keeps only the first 20,000 characters. The full generated prompts were about 27,300,
so every pasted prompt was cut inside step 6 and the agent never saw the finalize arguments, order placement, the state
record or the notifications.

The committed `*-backup.txt` files are the PASTEABLE prompts (under 3,500 characters). They first read the public
`slot_status.json` signal and stand down only when the market is closed or the backed-up slot succeeded today. Otherwise
they verify the attached planner's build stamp, fetch part 0, and require its token before requesting part 1. No broker
or Mac tool is named in the pasteable prompt.

```
python3 backup_planner.pyz procedure --task <task id> --part <0|1|2|3>
```

Part 0 carries the hard rules, calendar, universe check and stand-down gate. Part 1 is steps 1-3, part 2 is steps 4-6,
and part 3 is steps 7-9. All four parts live in `scripts/backup_procedure.py`, which ships inside the `.pyz`, so they are
covered by the same `content_sha256` stamp the prompt checks. The full reference text (rules plus steps, for reading in
one place) is `gen.build_prompts()`; the pasteable files are `gen.build_instructions()`, which is what `--write` and
`--check` use. `scripts/tests/test_backup_signal_prompt.py` keeps the pasted prompts under 3,500 characters. Changing
any served part changes the planner stamp, so rebuild the `.pyz`, regenerate the prompts and re-run the pair check in
"Checking the pair before you paste it".

Step 1 is code too: `backup_planner gates --task <id>` reads the real clock, picks the slot, applies the holiday and
early-close calendar and the window, and prints PROCEED / skipped_market_closed / skipped_outside_window
(`scripts/backup_gates.py`, rules unchanged from the old step 1 text). `--now` exists for tests only.
