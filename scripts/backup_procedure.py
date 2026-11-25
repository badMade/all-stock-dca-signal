#!/usr/bin/env python3
"""The Perplexity backup-executor preflight and procedure, served by the stamped planner.

WHY THIS EXISTS (2026-10-18): Perplexity's Instructions box keeps only the first 20,000 characters. The full
generated prompts were 27,2xx-27,4xx characters, so every one was cut inside step 6 and the agent never saw the
finalize arguments, the order-placement step (7), the state record or the notifications.

The pasted prompt is now a short signal check. Part 0 holds the hard rules, calendar, universe check and stand-down
gate; parts 1-3 hold steps 1-9. All four are printed on demand by

    python3 backup_planner.pyz procedure --task <task id> --part <0|1|2|3>

Because this module ships inside ``backup_planner.pyz`` (it is in the planner's static import closure), the
procedure is covered by the same ``content_sha256`` stamp the prompt checks in step 0: a procedure that does
not match the prompt's build cannot be served by a build that passes that check.

``scripts/generate_perplexity_backup_prompts.py`` renders the short pasted prompt and the full reference prompt.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from pathlib import Path

import dca_config
import dca_order_plan
import dca_rec_store
import dca_runner
from near_close_dca import BUDGET_TIERS
from scripts import backup_planner

# The latest placement time per slot is dca_order_plan.LATEST_PLACEMENT_ET (the NEXT canonical slot's
# window open, and the 16:00 close for the final slot), so the prompts and the planner cannot disagree.
_NEXT_SLOT_START = dca_order_plan.LATEST_PLACEMENT_ET
_SLOT_LABEL = {"0935": "market-open", "1030": "morning", "1130": "intraday", "1230": "midday",
               "1330": "intraday", "1430": "intraday", "1530": "final"}


@dataclass(frozen=True)
class Backup:
    """One backup task: its file, id, fire times, and the canonical slot each fire backs up."""

    file: str
    task_id: str
    fires: tuple[str, ...]
    slots: tuple[str, ...]


BACKUPS = (
    Backup("1000ET-935-backup.txt", "4412c3c4", ("10:00",), ("0935",)),
    Backup("1050ET-1030-backup.txt", "e028361e", ("10:50",), ("1030",)),
    Backup("1150ET-1130-backup.txt", "e3590042", ("11:50",), ("1130",)),
    Backup("1250ET-1230-backup.txt", "062605c6", ("12:50",), ("1230",)),
    Backup("1350-1450ET-1330-1430-backup.txt", "54ab24d2", ("13:50", "14:50"), ("1330", "1430")),
    # 2026-11-09: 15:40, not 15:45. The 15:45 fire reached its gates at 16:03 ET and skipped with skipped_outside_window.
    Backup("1540ET-1530-backup.txt", "f83b8d62", ("15:40",), ("1530",)),
)

HARD_RULES_TEMPLATE = """\
1. BUY-ONLY, regular hours only. Market orders sized by dollar amount (notional dollar_amount), time_in_force gfd, regular_hours. No sells, no cancels, no modifications, no options, no crypto, no margin or account changes, no review_equity_order, no per-order confirmation prompts.
2. ACCOUNT: never use a stored account number. At the start of the run (after the step 1 gates) call the Robinhood account list, keep only the accounts this agent is allowed to trade, and continue ONLY if exactly one account qualifies and its number ends in ${ROBINHOOD_AGENTIC_ACCOUNT_LAST4}. Otherwise end the run before reading any orders or buying, record status failed_unexpected_error, and notify. Use that one account for the rest of the run. Never print the full account number in any message, alert, plan, file, or state record; state records carry only the last 4 digits (account_last4). The one exception is the planner's order-evidence envelope: orders_pages_poll1.json and orders_pages_poll2.json carry the resolved account number because the planner pins the pages to it. The raw placement responses step 7 saves (placement_<SYMBOL>.json) can carry it too. Keep all of these only in this run's working directory, delete them before the run ends (every status, including failures), and never sync, attach, submit, or copy them anywhere.
3. UNIVERSE: only the symbols printed by the planner's `universe` command (step 0). That list already leaves out the do-not-buy and whole-share symbols, and the planner refuses any other symbol even if a plan, a file, or a quote names it. Never order, quote, or scan a symbol the command did not list.
4. CAPS (a backup is Normal-only, like the Claude slots: the planner never escalates the budget tier whatever the count of 3x-or-greater signals, and you never choose or escalate a tier yourself. You place only what the planner's finalize returns in to_place). Per order at most $@@PER_ORDER@@. Per day at most @@NORMAL_DAILY@@ across ALL orders on the account (every slot plus manual buying). Per ISO week at most @@NORMAL_WEEKLY@@. Per day at most @@MAX_ORDERS@@ buy attempts. Caps are read from a FRESH full-day and full-week order pull, never from a plan, a file, or memory. If the batch would breach a cap, the planner trims it for you: finalize returns in to_place only the longest prefix, in allocation order, that fits, and lists every order it left out in skipped with the cap that stopped it. Place exactly to_place, never more: do not add an order the planner left out, do not resize one, and do not back-fill a smaller order past the first one that did not fit. An empty to_place is a normal result: place nothing and record the skipped orders.
5. ONE ORDER PER TICKER PER SLOT. Never buy a ticker that already has a buy attempt (filled, open, cancelled, or rejected) inside this slot's window, whoever placed it. A ticker bought in an earlier slot may still receive this slot's resized top-up when the Claude or plan-first plan lists it, because the repo allows repeats across slots; a computed plan never tops up and excludes any ticker bought earlier today.
6. PRICE AND SPREAD: each order requires a fresh quote strictly below that ticker's prior regular-session close at the moment of the order, and a fresh bid and ask that are both present, positive, uncrossed, and within a spread of @@MAX_SPREAD@@% of the mid price (the primaries' Gate 6.7 hard circuit breaker). A missing, unreadable, crossed, or wide quote means skip that ticker only.
7. BROKER ORDER HISTORY IS THE ONLY EVIDENCE of what was bought. Mac Studio files, logs, plans, published recommendations, and anything a sub-agent says are INPUTS only. Mac content is never purchase evidence (rule 7).
8. FAIL CLOSED. Any missing, stale, malformed, unverifiable, or contradictory input, any planner error, any incomplete pagination: place NOTHING, record status failed_unexpected_error, notify (step 9), and stop.
9. TIME: never submit an order at or after the latest placement time above, and never outside regular hours.
10. Never resize an amount the planner returned. NEVER compute ladder multipliers, base amounts, caps, or priorities by hand; only the planner does. Nor sum, filter, reshape, or convert any data by hand: raw broker responses go to files untouched, and the planner reads them.
11. NO UNPRESCRIBED RETRIES OR LOOPS. Make each call at most once for the same purpose, EXCEPT where this prompt or the served procedure prescribes repeating it: following every pagination cursor, the second evidence poll and its classify, the late-start heartbeat recheck, the per-order tradability, history, quote and check-placement steps, and one pass per order in the planner's list. A documented fallback outcome is not an error: NO_VALID_PLAN (exit 2) moves on to the next plan source as step 3 says. Never repeat an identical or near-identical call to double-check a result, never re-run a check that passed unless the procedure says to, and never investigate an unexpected result: go straight to the state record and notification (steps 8 and 9) with failed_unexpected_error. A batch is not capped by a call count; if you find yourself repeating calls the procedure did not prescribe, stop BETWEEN orders (never after a submission and before its check-placement), and keep enough calls in reserve for steps 8 and 9."""

PART0_TEMPLATE = """\
STANDING BACKUP EXECUTOR @@TITLE@@
Task @@TASK_ID@@. Fires @@FIRES@@ ET on trading days. Backs up the Claude @@SLOT_LINE@@.
Latest placement: @@LATEST@@ ET.
@@LIMITATION@@
Sizing version @@LADDER@@. Universe hash @@UNIVERSE_HASH@@ (@@N@@ symbols). Market calendar retrieved @@CAL_RETRIEVED@@.

HARD RULES. @@HARD_RULES@@

MARKET CALENDAR (America/New_York). Closed: @@CLOSED@@. Early close at @@EARLY_TIME@@ ET: @@EARLY@@.

STEP 0 - SETUP
Run `python3 backup_planner.pyz universe` (Mac: `python3 -m scripts.backup_planner universe`). It prints {"status", "symbols", "count", "universe_hash"}. The symbols it lists are the only ones this task fetches data for or orders; if status is not OK, or its universe_hash is not @@UNIVERSE_HASH@@, place NOTHING, record failed_unexpected_error, and notify. No symbol is named anywhere else in this prompt, by design: the stocks live in the planner's configuration.

STAND-DOWN GATE (operator directive 2026-10-25). On a normal day the Claude slot has already bought, so the run ends here, before the account lookup, any broker call, and any procedure part:
a. STEP B's preflight already printed the gates result (its status, slot, latest_placement_et and now_et fields are the gates JSON): use it and do not run it again. Only if this run has no preflight JSON, run `python3 backup_planner.pyz gates --task @@TASK_ID@@` (Mac: `python3 -m scripts.backup_planner gates --task @@TASK_ID@@`) and keep its JSON; it is the procedure's step 1 command. Status PROCEED: go on to (b); its slot is this run's slot. Status skipped_market_closed or skipped_outside_window: stop with exactly that status, here (2026-10-31: a skipped fire used to fetch all three parts just to write this record). Make no account lookup and no broker call, make no device listing, request no procedure part, and send no notification. Write one JSON state record to ${PPLX_RUN_STATE_DIR}/run_state_<YYYYMMDD>_<HHMM>ET.json (create directories as needed) with: trading_day, slot (null when the gates JSON has none), task_id, fired_at_et, planner_build (STEP B's build-info), universe_hash (step 0's), sizing_version (@@LADDER@@), status, run_origin scheduled_backup, mac_status, account_last4 and plan_source null, and executor_orders, orders_planned, orders_placed, skipped and errors as []. Never include the account number. Then end the run. Any other gates status, a non-zero exit, or output that is not that JSON document: place NOTHING, record failed_unexpected_error, notify, and stop.
b. Call list_devices (or this task's equivalent device listing) with a timeout of 60 seconds and find an online device whose name contains "Mac Studio". Record mac_status as one of online, not_found, offline, timeout, device_error (no device-listing tool at all is not_found) and set mac_path_abandoned to false. The 3 minute Mac budget starts now; if it is spent, mac_path_abandoned becomes true.
c. Only when mac_status is online and mac_path_abandoned is false: use run_local_subagent on the Mac, working directory the all-stock-dca checkout, to run exactly:
   python3 -m scripts.local_heartbeat status --trading-day <YYYY-MM-DD> --slot <slot>
   Keep its {"local_status", "backup_may_act"}.
d. If local_status is in_progress or succeeded: stop with status stood_down_local_active. Make no account lookup and no get_equity_orders call, read no published plan, and request no procedure part. Write one JSON state record to ${PPLX_RUN_STATE_DIR}/run_state_<YYYYMMDD>_<HHMM>ET.json (create directories as needed) with: trading_day, slot, task_id, fired_at_et, mac_status, mac_path_abandoned, mac_elapsed_seconds, routing {path none, local_status, reason, timestamp_et}, planner_build (step 0's build-info), universe_hash (step 0's), sizing_version (the header's), status stood_down_local_active, run_origin scheduled_backup, account_last4 and plan_source null, and executor_orders, orders_planned, orders_placed, skipped and errors as []. Never include the account number. Then send no notification and end the run.
e. Any other result (mac_status not online, local_status off or errored, any other probe value or an unparseable result, a non-zero exit, or a spent Mac budget): continue with the PROCEDURE below. Its step 1 and step 3 reuse what this gate recorded, so nothing here is repeated.

PROCEDURE (steps 1-9). The rest of this task is served below. Follow it in order and do exactly what each part says. If any requested part fails, has the wrong task id or part number, or is missing its end line, place NOTHING, record failed_unexpected_error, notify, and stop.
The later requests, only after each preceding part's steps are complete, are:
   python3 backup_planner.pyz procedure --task @@TASK_ID@@ --part 2 --token <the token printed at the end of part 1>
   python3 backup_planner.pyz procedure --task @@TASK_ID@@ --part 3 --token <the token printed at the end of part 2>."""


PROCEDURE_TEMPLATE = """\
STEP 1 - GATES
The STAND-DOWN GATE in this task's instructions already ran the command below and kept its JSON: use that JSON and do not run it again. It is described here so its result is read the same way.
Run `python3 backup_planner.pyz gates --task @@TASK_ID@@` (Mac: `python3 -m scripts.backup_planner gates --task @@TASK_ID@@`). It reads the real clock, converts it to America/New_York, applies the rules below, and prints one JSON document. Never pass --now; never take the date or time from recollection. @@SLOT_RESOLUTION@@ Market day: today must be a weekday that is not in the Closed list; on an early-close day a fire at or after @@EARLY_TIME@@ ET also counts as closed.
- status PROCEED: continue. Its slot and latest_placement_et are this run's slot and latest placement time (rule 9).
- status skipped_market_closed or skipped_outside_window: stop with exactly that status.
- Any other status, a non-zero exit, or output that is not that JSON document: place NOTHING, record status failed_unexpected_error, and notify (step 9).

STEP 2 - EVIDENCE POLL 1 (broker)
WEEK HISTORY (2026-11-14: on 2026-11-13 the 10:30 slot's full-week order fetch was too large to save and it placed nothing; a seven-day fetch through this sandbox has the same shape). Decide week_history_mode here: prior_baseline_merge or full. Once it is full it stays full for the rest of the run.
a. Only when mac_status is online and mac_path_abandoned is false: use run_local_subagent on the Mac, working directory the all-stock-dca checkout, to run exactly:
   python3 -m scripts.backup_week_history lookup --trading-day <YYYY-MM-DD> --slot <slot> --account-last4 <the looked-up account's last 4 digits>
   It prints {"ok", "base", "created_at_gte", "work_dir", ...}. On exit 0, call get_equity_orders for the looked-up account with created_at_gte set to the printed created_at_gte verbatim, following every cursor exactly as the FULL POLL below says, take the current UTC time from a real clock right after the last page as an offset-aware ISO instant (snapshot_at), save each raw page untouched to its own file, and put those files, unchanged, in the printed work_dir on the Mac through run_local_subagent. Then run on the Mac exactly:
   python3 -m scripts.backup_week_history merge --trading-day <YYYY-MM-DD> --slot <slot> --account-last4 <last 4> --label poll1 --base <the printed base> --created-at-gte <the printed created_at_gte> --snapshot-at <snapshot_at> <the page files in work_dir, in fetch order>
   On exit 0, week_history_mode is prior_baseline_merge. Keep its printed out as the WEEK FILE and its next_created_at_gte, and record its payload_sha256 as week_history_sha256. The WEEK FILE never comes into this sandbox: it holds this ISO week from Monday, and the Mac planner reads it. It and every file put in work_dir stay on the Mac under its cron root, like the Claude slots' own envelopes; never copy them back into this sandbox or anywhere else.
b. Otherwise (mac_status not online, mac_path_abandoned true, the lookup or the merge exits non-zero or prints anything but its JSON, or the Mac stops answering), week_history_mode is full: make the FULL POLL below into orders_pages_poll1.json. Never retype, condense, or move rows by hand, and never reuse a partial increment.
WEEK FILE RULE (every later step): while week_history_mode is prior_baseline_merge, a planner command that takes --orders-pages or --prior-orders-pages runs on the Mac (python3 -m scripts.backup_planner ...) with that argument set to the current WEEK FILE and with --week-envelope --account-last4 <last 4> added. Every other file it names (executor_ids.json, published.json, market.json, plan.json, fresh_quotes.json, fresh_spreads.json, fresh_prior_closes.json, placement_<SYMBOL>.json) is put, unchanged, in work_dir first. A later increment (late-start recheck, poll 2, step 7c) is fetched from the next_created_at_gte the previous merge printed, and merged by the same merge command with a new --label and --base set to the current WEEK FILE. Its printed out becomes the WEEK FILE and its next_created_at_gte the next one. If such a merge or command cannot run on the Mac (it exits non-zero, the Mac stops answering, or mac_path_abandoned becomes true), switch to week_history_mode full, make the FULL POLL into the file that step names, and run the sandbox form of the command instead (except where step 7 says otherwise).
FULL POLL: Call get_equity_orders for the looked-up account with created_at_gte set to 00:00 ET seven days before today (the weekly cap sums the ISO week and the kill-switch figure sums the trailing seven days, so a window seven days back covers both and a today-only fetch would understate them) and follow EVERY pagination cursor to completion: make ONE call for the first page, then one call per cursor the response itself returns, and no other call. A response with no next/cursor field is the final page; do not hunt for one. NEVER re-query with a different state, date, or window to double-check the pull, and never write ad-hoc scripts to count, group, or inspect the rows (the 2026-10-20 09:55 run burned its whole budget that way). The planner, not you, judges completeness: save the pages untouched and let the next planner command accept or refuse them; if it refuses, stop (rule 8). Save every raw page, in fetch order, to orders_pages_poll1.json as {"account_number": <resolved account>, "trading_day": "<today YYYY-MM-DD>", "pages": [...]} (the pages span seven days; trading_day stays today); never retype or condense rows by hand.
Do not classify these orders by hand: the planner's classify command does it in step 3d, before any plan is read.
PUBLISHED PLAN-FIRST SET. Then read the plan-first recommendation the Mac publishes for cloud readers: the primary's own {ticker, usd, ref_id} list per slot, the same one the Claude slot places. It is an INPUT (rule 7). Read @@PUBLISHED_CHANNELS@@ Save the text untouched to published.json; never retype, reorder, or edit it. If no channel can be read or the text is not JSON, continue without published.json: that is not an error, the run falls back to the other plan sources.

STEP 3 - MAC-FIRST (replaces the 2026-09-23 rule never to check the Mac; operator directive 2026-10-06)
The STAND-DOWN GATE already did (a) and, when the Mac was online, the status command of (c): reuse the mac_status and local_status the STAND-DOWN GATE recorded, and do not call list_devices or the status command again (only the LATE-START GUARD recheck repeats it), and the 3 minute Mac budget started at the gate's list_devices.
a. Call list_devices (or this task's equivalent device listing) with a timeout of 60 seconds. Find an online device whose name contains "Mac Studio". Record mac_status as one of: online, not_found, offline, timeout, device_error. Set mac_path_abandoned to false. If this session has no device-listing tool at all (the 2026-10-12 15:45 run found none), that is mac_status not_found: the Mac is out of reach, not an error, and the rule below applies.
b. The whole Mac path has a budget of 3 minutes, counted from the start of (a). When it is spent at any point, abandon the Mac path: set mac_path_abandoned to true, ignore its partial output, run no further Mac command, and continue with the sandbox parts of (d) and (e); a plan computed after that is plan_source cloud_compute. Once mac_path_abandoned is true it never goes back to false, and every Mac command below (3c, 3e, 3f, 5, 6, 7) requires it to be false, whatever mac_status still says.
c. LOCAL FIRST. The Mac run is the sole executor of the buy; this task buys only when it has failed. Decide local_status:
   - mac_status offline or not_found: local_status off. timeout: unreachable. device_error: errored. The backup may act; skip every Mac command below and continue with (d).
   - mac_status online and mac_path_abandoned false: use run_local_subagent on the Mac, working directory the all-stock-dca checkout, to run exactly:
     python3 -m scripts.local_heartbeat status --trading-day <YYYY-MM-DD> --slot <slot>
     It prints {"local_status", "backup_may_act"}. If backup_may_act is true (local_status off or errored, meaning no heartbeat or a heartbeat older than 300 seconds or a failed run), continue to (d), except that LATE-START GUARD applies first when local_status is off. If it is false with local_status in_progress or succeeded, stop with status stood_down_local_active and send no notification.
   - LATE-START GUARD (local_status off while mac_status is online and mac_path_abandoned is false, meaning the Mac is awake but shows no heartbeat yet; Claude has started up to 20 minutes late): wait 5 minutes (sleep via bash; this wait does not count against the 3 minute Mac budget, but the latest placement time still binds), then repeat the status command once and redo the broker poll of step 2 from scratch (it replaces orders_pages_poll1.json). Act only if the second status is still off or errored. If it is in_progress or succeeded, or the repeated poll holds any order inside this slot's window whose placed_agent is not user, recurring, or drip (Claude may have started), stop with stood_down_local_active. Do not wait when mac_status is not online or mac_path_abandoned is true.
     In week_history_mode prior_baseline_merge, "redo the broker poll of step 2 from scratch" means the next increment merged with --label poll1b under the WEEK FILE RULE (step 2), and the window test is the Mac command python3 -m scripts.backup_planner classify --trading-day <YYYY-MM-DD> --slot <slot> --orders-pages <the WEEK FILE> --week-envelope --account-last4 <last 4> (no --executor-ids, no --published): anything but status CLEAR with an empty executor_orders list means such an order exists.
   - Any other probe value, an unparseable result, or a non-zero exit: local_status unknown. Stop with status stood_down_local_unknown and notify (step 9). Never act on unknown.
   Never buy when local_status is reachable, in_progress, succeeded, or unknown. A cloud run places nothing by itself; only this task's step 7 may place a fallback order, and only when finalize returns PROCEED (operator directive 2026-10-13: a backup that cannot reach the Mac places its own computed plan).
d. EXECUTOR RECOGNITION. Claude's plan-first slot sends a UUID ref_id and the broker answers with its own, so Claude's orders carry no dca-runner- reference at the broker and are recognized by order id instead. If mac_status is online and mac_path_abandoned is false, use run_local_subagent on the Mac, working directory the all-stock-dca checkout, to run exactly:
   python3 -m scripts.backup_planner executor-ids --trading-day <YYYY-MM-DD> --slot <slot>
   It prints {"status", "trading_day", "slot", "order_ids", "sources", "errors"}: the broker order ids Claude saved for this slot, from the plan-first reader's placements and the live prompt's place_<TICKER>.json responses alike. Save that output untouched to executor_ids.json. A status other than OK, or a non-zero exit: place nothing, status failed_unexpected_error, notify. Then, in this sandbox, run:
   python3 backup_planner.pyz classify --trading-day <YYYY-MM-DD> --slot <slot> --orders-pages orders_pages_poll1.json --executor-ids executor_ids.json --published published.json
   leaving out --executor-ids when there is no executor_ids.json and --published when there is no published.json (in prior_baseline_merge, the WEEK FILE RULE: on the Mac against the WEEK FILE). It classifies EVERY order created inside this slot's window as the user's own activity (placed_agent user, recurring, or drip; noted, never blocking), expected executor traffic (an order id in executor_ids.json, a dca-runner-<slot>- reference, or a buy of a symbol the published set for this slot lists for exactly its dollar amount), or UNRECOGNIZED. Status CLEAR (exit 0): continue. Status UNRECOGNIZED: place nothing, record stood_down_unrecognized_order, and notify. Any other status or output: place nothing, status failed_unexpected_error, notify. Expected executor traffic is not a reason to stop: tickers already ordered are excluded later by the planner, so a partly finished Claude batch is completed, not abandoned. Keep its executor_orders for the state record.
e. PLAN-FIRST SET FIRST. If mac_status is online and mac_path_abandoned is false, use run_local_subagent on the Mac, working directory the all-stock-dca checkout, to run exactly:
   python3 -m scripts.backup_planner plan --source mac_rec --trading-day <YYYY-MM-DD> --slot <slot> --now <ET ISO timestamp>
   A VALID_PLAN is plan_source mac_rec: save its plan to plan.json and go to step 6. Otherwise, when published.json was saved, run in this sandbox:
   python3 backup_planner.pyz plan --source published_rec --published published.json --trading-day <YYYY-MM-DD> --slot <slot> --now <ET ISO timestamp>
   A VALID_PLAN is plan_source published_rec: save its plan to plan.json and go to step 6. Both apply the Claude reader's own freshness rule: the set for this slot, stamped at or before now on today's ET date, at most @@MAX_AGE@@ minutes old. NO_VALID_PLAN (no set, a stale set, or an invalid or unreadable one) is not an error: continue with (f). Any other status or output: place nothing, status failed_unexpected_error, notify. Never edit, merge, or re-date a set to make it pass.
f. If mac_status is online and mac_path_abandoned is false, use run_local_subagent on the Mac, working directory the all-stock-dca checkout, to run exactly:
   python3 -m scripts.backup_planner plan --source mac_plan --trading-day <YYYY-MM-DD> --slot <slot>
   It prints one JSON document {"status", "plan", "errors"} and exits 0 when status is VALID_PLAN. A VALID_PLAN is plan_source mac_plan: go to step 6. Any other status means the Mac has no valid plan for this day and slot: go to step 4 and compute on the Mac (plan_source mac_compute). When mac_status is not online, or the Mac path was abandoned, go to step 4 and compute in this sandbox (plan_source cloud_compute).
g. Whatever the Mac or a published copy says is an INPUT. It is never evidence that anything was bought (rule 7).

@@STEP4_5@@

STEP 6 - EVIDENCE POLL 2, THEN FINALIZE
Immediately before the first order, repeat step 2's poll: in week_history_mode full the FULL POLL from scratch into orders_pages_poll2.json; in prior_baseline_merge an increment merged with --label poll2 under the WEEK FILE RULE (its WEEK FILE is poll 2, and its snapshot_at is the time of poll 2). Then re-fetch the latest quote of every planned symbol into fresh_quotes.json as {"SYMBOL": price}, the latest bid and ask of every planned symbol into fresh_spreads.json as {"SYMBOL": {"bid": x, "ask": y}} (null for a symbol whose quote cannot be read), and the account's available buying power in USD. Never place with a missing or unreadable buying power.
CLASSIFY POLL 2 before finalizing: an executor that started after poll 1 is visible only here, and step 7c's repull starts at poll 2. If mac_status is online and mac_path_abandoned is false, first rerun the step 3d executor-ids command on the Mac and save its output untouched over executor_ids.json (a status other than OK, or a non-zero exit: place nothing, status failed_unexpected_error, notify). Then run the step 3d classify command again with --orders-pages orders_pages_poll2.json (in prior_baseline_merge, the WEEK FILE), with the same rule for leaving out --executor-ids and --published. Status CLEAR: continue. Status UNRECOGNIZED: place nothing, record stood_down_unrecognized_order, and notify. Any other status or output: place nothing, status failed_unexpected_error, notify.
For plan_source mac_rec or published_rec (a recommendation carries no prices), also fetch the prior regular session's close of every planned symbol into fresh_prior_closes.json as {"SYMBOL": close} and add --fresh-prior-closes fresh_prior_closes.json to the command below; a symbol without one is skipped. Then run, where the plan was produced (Mac for a mac_ source while mac_path_abandoned is false, otherwise this sandbox; if the Mac is no longer reachable or mac_path_abandoned is true use the sandbox planner on the saved plan file and keep the recorded plan_source):
   python3 -m scripts.backup_planner finalize --trading-day <YYYY-MM-DD> --slot <slot> --plan plan.json --orders-pages orders_pages_poll2.json --fresh-quotes fresh_quotes.json --fresh-spreads fresh_spreads.json --buying-power <USD> --now <ET ISO timestamp>@@LOCAL_STATUS_ARG@@
   (sandbox form: python3 backup_planner.pyz finalize ...; in prior_baseline_merge, the WEEK FILE RULE overrides where it runs: on the Mac against the WEEK FILE)@@LOCAL_STATUS_RULE@@
It prints {"status", "to_place", "would_place", "skipped", "errors"}. It drops tickers already bought (matched by client order id, then symbol, within this slot's window for a Claude or plan-first plan and anywhere today for a computed plan), drops tickers whose fresh quote is not below the prior close or whose spread is missing, crossed, or wide, and refuses the whole batch when any cap or the buying power would be breached. Status PROCEED with an empty to_place is nothing_to_place. Status SHADOW_ONLY means @@SHADOW_REASON@@: record would_place as the shadow result (status shadow_only) and place nothing. An ABORT whose error is kill_switch_engaged_since_plan means spend landed after the plan was sized and the seven-day guard is now tripped: place NOTHING, status failed_unexpected_error, notify; never reuse that plan. An ABORT whose only error is kill_switch_unproven (WEEK FILE only: the plan is sized above 1x and a WEEK FILE cannot prove the days before Monday) is not a failure: switch to week_history_mode full, make the FULL POLL into orders_pages_poll2.json, rerun the CLASSIFY POLL 2 command and then finalize once in this sandbox on the saved plan.json, and read that result instead. Any other status, or a non-zero exit: place nothing, status failed_unexpected_error, notify.

STEP 7 - PLACE THE ORDERS
Place orders ONLY when the status is PROCEED. For any other status skip this step. Before the FIRST order, only when mac_status is online and mac_path_abandoned is false, run `python3 -m scripts.local_heartbeat claim --trading-day <YYYY-MM-DD> --slot <slot>` on the Mac (the same step 3 probe path); place nothing unless it prints backup_may_act true, and run it exactly once per run. It records an unconfirmed claim that blocks a second backup run for 10 minutes; it is not a buy. Right after the FIRST order is validated as placed (step e below), run `python3 -m scripts.local_heartbeat confirm --trading-day <YYYY-MM-DD> --slot <slot>` once on the Mac (only while mac_path_abandoned is false) to record the buy. When mac_status is not online or mac_path_abandoned is true there is no Mac to hold a claim: skip both the claim and the confirm, record claim: skipped_mac_unreachable in the state record, and rely on the broker evidence alone (the per-order repull in step c, the caps, and the spread check), which the claim never replaced. If the run stops before any order is accepted, run no confirm: the claim expires and a later run may claim again. Place exactly the to_place list, one order at a time, sequentially, never in parallel. For each order, in order:
a. If the ET time is at or after the latest placement time, stop the batch (rule 9).
b. Check fractional tradability for the symbol (get_equity_tradability); skip it if it is not tradable for fractional buys.
c. PER-ORDER REPULL: re-pull the account's agentic orders created at or after the previous order placement timestamp (for the first order, the time of poll 2) and follow every cursor. If a buy for this symbol now exists, skip this symbol. If any order appears that is neither one this run placed (an order_id step 7e's check-placement printed) nor the user's own activity, stop the batch and record stood_down_unrecognized_order: another executor started mid-batch. If any buy by the user or a recurring order (committed spend this batch's caps did not see) now exists since poll 2, stop the batch and record status partial (or nothing_to_place when none was placed): do not continue against caps that are no longer proven. Never use a midnight window for this pull. An incomplete pull stops the batch (rule 8). In prior_baseline_merge this pull is the next increment under the WEEK FILE RULE (--label order<N>, N the order's position in to_place), so it starts up to 120 seconds before the previous snapshot. Judge the rules above only on its rows created at or after the previous order placement timestamp (for the first order, the time of poll 2): the overlap rows exist for the merge. If that merge or the Mac fails before this order is submitted, switch to week_history_mode full: make the FULL POLL into orders_pages_poll2.json (this order's prior history), do this repull in this sandbox as above, and continue the batch with the sandbox check-placement.
c2. Re-fetch the symbol's quote and its bid and ask; skip it unless the price is strictly below the prior close and the spread is proven within @@MAX_SPREAD@@% of the mid price (rule 6).
d. Take the current UTC time from a real clock as an offset-aware ISO instant (submitted_after), then submit the buy with side buy, dollar_amount equal to the planner's notional_usd to the cent, market order, time_in_force gfd, regular_hours, and the planner's ref_id as the client reference. Do not change the amount. Save the raw response untouched to placement_<SYMBOL>.json.
e. Validate the response before the next order. The broker answers with its own ref_id, never the one sent, so never compare ref_ids by hand. Run, in this sandbox:
   python3 backup_planner.pyz check-placement --trading-day <YYYY-MM-DD> --slot <slot> --response placement_<SYMBOL>.json --symbol <SYMBOL> --amount <notional_usd> --sent-ref-id <ref_id> --submitted-after <submitted_after> --prior-orders-pages orders_pages_poll2.json --placed-responses <every earlier placement_*.json of this run>
   (leave out --placed-responses for the first order). In prior_baseline_merge, the WEEK FILE RULE applies with --prior-orders-pages set to the WEEK FILE written by THIS order's step c merge. If that command cannot run on the Mac after the order was submitted, never fall back for it: record the order with state unverified, stop the batch, and record status failed_unexpected_error. Status PLACED (exit 0): the order counts as placed; record its order_id. Anything else is a failure: it names no new order created by this call for exactly this symbol, side, and amount. Never retry a failed order.
f. A platform or permission refusal that never reached the broker proves nothing: stop the batch, notify, and do not count it as an attempt.
g. Stop the batch after @@CONSEC@@ consecutive placement failures. One rejected ticker does not stop the rest.

STEP 8 - STATE RECORD
Write one JSON state record to ${PPLX_RUN_STATE_DIR}/run_state_<YYYYMMDD>_<HHMM>ET.json (create directories as needed) with: trading_day, slot, task_id, account_last4, fired_at_et, preflight_at_et (STEP B preflight's now_et), plan_source (mac_rec, published_rec, mac_plan, mac_compute, or cloud_compute), mac_status, mac_path_abandoned, executor_orders (classify's list: symbol, created_at, basis), routing {path perplexity-fallback or none, local_status, reason, timestamp_et}, week_history_mode (prior_baseline_merge or full, its value at the end of the run; null when the run ended before step 2), week_history_sha256 (the last WEEK FILE merge's payload_sha256, else null), kill_switch_basis (week_envelope_treated_as_engaged when any compute or finalize of this run read a WEEK FILE, because a WEEK FILE starts on Monday and the days before Monday are not proven, so the planner capped multipliers at 1x; otherwise full_seven_day_history; null when no compute or finalize ran), mac_elapsed_seconds, planner_build, status, orders_planned, orders_placed [{symbol, notional_usd, ref_id, order_id, state}], skipped [{symbol, reason}], errors, universe_hash, sizing_version. Never include the account number. run_origin scheduled_backup. status is exactly one of: placed, partial, nothing_to_place, shadow_only, stood_down_unrecognized_order, stood_down_local_active, stood_down_local_unknown, skipped_market_closed, skipped_outside_window, failed_unexpected_error.
Append every placed order (symbol, notional_usd, ref_id, order_id, state; never the account number) to ${PPLX_RUN_STATE_DIR}/submitted_orders.csv, creating it with headers if absent. If the project-files checkout needs syncing before writes, run pplx project files sync first and pplx project files submit -m "<trading day> <slot> state" after writing, so records persist for future runs.

STEP 9 - NOTIFY
Send a notification through this task's existing alert channels (email to ${DCA_ALERT_EMAIL_ADDRESS}, never a hardcoded address) for every status except placed, nothing_to_place, shadow_only, stood_down_local_active, skipped_market_closed, and skipped_outside_window. Include the slot, status, plan_source, mac_status, the reasons, and the tickers placed and skipped. Never include the account number.
If NO order was placed and NO notification-worthy status occurred, call pplx automation suppress-run-notification before finishing so no routine completion push is sent.
NEVER post to Slack or Notion, run no reports, slippage analysis, or monitor-only or watchdog work.
@@LIMITATION@@"""

_GATED_STEPS = """\
STEP 4 - MARKET DATA (only when step 3 found no valid plan)
For every symbol the `universe` command listed, fetch the raw responses of get_equity_historicals (interval 5minute, bounds regular, from 09:30 ET today up to now), of get_equity_historicals for the previous completed session's daily bar (interval day, bounds regular, covering the last 14 days), and of get_equity_quotes, in as many calls as the tools require. Save every raw response untouched, one small file per tool call, into the folder raw_parts/ (never one big raw.json: a single write that large stalls the run), named so they sort in fetch order (raw_001.json, raw_002.json, ...). Each file is {"historicals": [<raw response>]} for a 5-minute response, {"daily": [<raw response>]} for an interval-day response, or {"quotes": [<raw response>]} for a quotes response, never mixed in one file; never retype, filter, merge, or condense anything by hand. If one response is still too large to write, split the symbols across more calls instead. A symbol the broker returns nothing usable for is simply absent: the assembler marks it as an error and the primary's own checks keep it from being bought. Then run, where step 5 will run (Mac or sandbox):
   python3 -m scripts.assemble_market_data --trading-day <YYYY-MM-DD> --raw raw_parts --out market.json
   (sandbox form: python3 backup_planner.pyz assemble-market-data --trading-day <YYYY-MM-DD> --raw raw_parts --out market.json)
It prints one JSON document with status ASSEMBLED and exits 0, or status FAILED and exits 2 (place nothing). Also fetch the account's available buying power in USD; never place with a missing or unreadable buying power.

STEP 5 - COMPUTE (only when step 3 found no valid plan)
Run, on the Mac when mac_status is online and mac_path_abandoned is false (plan_source mac_compute), otherwise in this sandbox (plan_source cloud_compute):
   python3 -m scripts.backup_planner compute --trading-day <YYYY-MM-DD> --slot <slot> --market-data market.json --deployed-last-7d-usd auto --orders-pages orders_pages_poll1.json --buying-power <USD> --now <ET ISO timestamp>
   (sandbox form: python3 backup_planner.pyz compute ...; in prior_baseline_merge, the WEEK FILE RULE: on the Mac against the WEEK FILE, plan_source mac_compute)
This runs the primary's own signal checks on the data, so what to buy and at what size are the primary's own decisions. `--deployed-last-7d-usd auto` makes the planner derive the kill-switch figure from the same order history; never type a number there. A WEEK FILE starts on Monday, so it cannot prove the trailing seven days: the planner then treats the kill switch as engaged and sizes at 1x at most. That is expected, not an error. It prints {"status", "plan", "errors"}. Any status other than VALID_PLAN, a non-zero exit, a missing file, or a planner error: place nothing, status failed_unexpected_error, notify.\
"""

_QUOTES_STEPS = """\
STEP 4 - MARKET DATA (only when step 3 found no valid plan)
NOTE: the signal gate is not wired for this slot yet, so this task can only report what the price ladder would buy; nothing is placed (status shadow_only). For every symbol the `universe` command listed, fetch the latest quote (decision_price) and the prior regular session's close and low (prior_close, prior_low; null only when truly unavailable). Save quotes.json as {"quotes": [{"symbol", "prior_close", "prior_low", "decision_price"}, ...]}. A symbol whose data cannot be read is left out; never estimate a value. Also fetch the account's available buying power in USD; never place with a missing or unreadable buying power.

STEP 5 - COMPUTE (only when step 3 found no valid plan)
Run, on the Mac when mac_status is online and mac_path_abandoned is false (plan_source mac_compute), otherwise in this sandbox (plan_source cloud_compute):
   python3 -m scripts.backup_planner compute --trading-day <YYYY-MM-DD> --slot <slot> --quotes quotes.json --orders-pages orders_pages_poll1.json --buying-power <USD> --now <ET ISO timestamp>
   (sandbox form: python3 backup_planner.pyz compute ...; in prior_baseline_merge, the WEEK FILE RULE: on the Mac against the WEEK FILE, plan_source mac_compute)
It prints {"status", "plan", "errors"}. Any status other than VALID_PLAN, a non-zero exit, a missing file, or a planner error: place nothing, status failed_unexpected_error, notify.\
"""

_GATED_SHADOW = ("a computed plan finalized without --local-status off, errored or unreachable, meaning the Mac may be acting")
_GATED_LOCAL_STATUS_ARG = " --local-status <off|errored|unreachable>"
_GATED_LOCAL_STATUS_RULE = (
    "\nPass --local-status with the local_status step 3 decided, only when it is off, errored or unreachable; "
    "never pass --local-status when local_status is unknown, in_progress or succeeded (those stand down in step 3 "
    "and never reach this step). Without it a computed plan only reports (SHADOW_ONLY)."
)
_QUOTES_SHADOW = ("the plan came from compute, which does not run the primary's trend and data-confidence gates")


# Where the plan-first program mirrors its agent payload for cloud readers (dca_rec_publish.GitHubBranchPublisher pushes
# to this repository's remote). Not in config: the publisher pushes to a git remote NAME, and Perplexity needs the slug.
GITHUB_REPOSITORY = "badMade/all-stock-dca"


def _published_channels(publish: dict) -> str:
    """Where step 2 reads the published set, from ``config/recommendations.json``'s publish block.

    The branch is always named, even while its publisher is off: a missing or stale file is simply no published set,
    and the run falls back, so enabling the publisher needs no repaste. Notion is named only once a page id is set.
    """
    branch = publish["github_branch"]["branch"]
    text = (f'the file recommendations.json at the tip of branch "{branch}" of the GitHub repository '
            f"{GITHUB_REPOSITORY} (a data-only branch; read the raw file, never another branch).")
    page_id = publish["notion"].get("page_id")
    if page_id:
        text += f" If that cannot be read, use the text of the first code block on Notion page {page_id} instead."
    return text


_LIMITATION = """
KNOWN LIMITATION (accepted 2026-10-06): the Mac-first check can use time that this fire needs before the 16:00 ET close. If the latest placement time arrives first, stop with skipped_outside_window. No slot-specific shortcut is allowed."""


def _slot_resolution(backup: Backup) -> str:
    if len(backup.fires) == 1:
        slot, start = backup.slots[0], backup.fires[0]  # the fire time is the earliest start (2026-10-26 incident)
        return (f"This fire backs up slot {slot} ({_SLOT_LABEL[slot]}). The ET time must be from {start} up to the "
                f"latest placement time {_NEXT_SLOT_START[slot]}.")
    # The two-fire task (13:50 and 14:50): the clock decides which slot this run backs up.
    return ("This task fires twice a day, so the clock decides the slot. ET time from 13:40 to 14:20 means slot 1330 "
            f"(intraday), latest placement {_NEXT_SLOT_START['1330']}. ET time from 14:40 to 15:20 means slot 1430 "
            f"(intraday), latest placement {_NEXT_SLOT_START['1430']}. Any other time is outside the range.")


def _latest(backup: Backup) -> str:
    if len(backup.fires) == 1:
        return _NEXT_SLOT_START[backup.slots[0]]
    return (
        f"{_NEXT_SLOT_START[backup.slots[0]]} for the {backup.fires[0]} fire, "
        f"{_NEXT_SLOT_START[backup.slots[1]]} for the {backup.fires[1]} fire"
    )



def common_values(directory: Path | None = None) -> dict[str, str]:
    """Every config-derived placeholder shared by all six prompts EXCEPT the build stamp (that one is the
    generator's: the procedure must not depend on the hash of the build that contains it)."""
    directory = Path(directory) if directory is not None else dca_config.get_config_dir()
    config = dca_config.load_strategy_config(directory)
    calendar = json.loads((directory / "market_calendar.json").read_text(encoding="utf-8"))
    breakers = json.loads((directory / "budgets.json").read_text(encoding="utf-8"))["circuit_breakers"]
    normal = next(tier for tier in BUDGET_TIERS if tier.name == "normal")
    recommendations = dca_rec_store.load_settings(directory)
    return {
        "@@PUBLISHED_CHANNELS@@": _published_channels(recommendations.publish),
        "@@MAX_AGE@@": f"{recommendations.max_age.total_seconds() / 60:g}",
        "@@LADDER@@": dca_order_plan.current_ladder_version(directory),
        "@@UNIVERSE_HASH@@": dca_order_plan.universe_hash(config.tickers),
        "@@N@@": str(len(config.tickers)),
        "@@CAL_RETRIEVED@@": str(calendar["retrieved"]),
        "@@CLOSED@@": ", ".join(calendar["holidays_closed"]),
        "@@EARLY@@": ", ".join(calendar["early_close_1300"]),
        "@@EARLY_TIME@@": str(calendar["early_close_time_et"]),
        "@@PER_ORDER@@": f"{dca_runner.MAX_PER_ORDER_USD:g}",
        "@@NORMAL_DAILY@@": f"${normal.daily_cap_usd:g}",
        "@@NORMAL_WEEKLY@@": f"${normal.weekly_cap_usd:g}",
        "@@MAX_ORDERS@@": str(dca_runner.MAX_ORDERS_PER_DAY),
        "@@CONSEC@@": str(breakers["max_consecutive_placement_failures"]),
        "@@MAX_SPREAD@@": f"{config.max_bid_ask_spread_pct:g}",
    }


def backup_values(backup: Backup, common: dict[str, str]) -> dict[str, str]:
    """The per-task placeholders (everything that differs between the six prompts)."""
    slot_word = "slot" if len(backup.slots) == 1 else "slots"
    gated = all(slot in backup_planner.GATED_SLOTS for slot in backup.slots)
    return {
        **common,
        "@@HARD_RULES@@": substitute(HARD_RULES_TEMPLATE, common),
        "@@TITLE@@": "- " + " / ".join(f"slot {s} ({_SLOT_LABEL[s]})" for s in backup.slots),
        "@@TASK_ID@@": backup.task_id,
        "@@FIRES@@": " and ".join(backup.fires),
        "@@SLOT_LINE@@": f"{slot_word} " + " and ".join(backup.slots),
        "@@LATEST@@": _latest(backup),
        "@@SLOT_RESOLUTION@@": _slot_resolution(backup),
        "@@LIMITATION@@": _LIMITATION if backup.slots == ("1530",) else "",
        "@@STEP4_5@@": _GATED_STEPS if gated else _QUOTES_STEPS,
        "@@SHADOW_REASON@@": _GATED_SHADOW if gated else _QUOTES_SHADOW,
        "@@LOCAL_STATUS_ARG@@": _GATED_LOCAL_STATUS_ARG if gated else "",
        "@@LOCAL_STATUS_RULE@@": _GATED_LOCAL_STATUS_RULE if gated else "",
    }


def substitute(template: str, values: dict[str, str]) -> str:
    for key, value in values.items():
        template = template.replace(key, value)
    return template


# Part 0 is the stamped preflight. Parts 1-3 are cut on rendered step headings, so the gated and quotes variants
# of steps 4-5 need no separate handling.
PART_STARTS = ("STEP 1 - ", "STEP 4 - ", "STEP 7 - ")
PARTS = (1, 2, 3)


def _render_part_body(backup: Backup, part: int, directory: Path | None = None) -> str:
    common = common_values(directory)
    if part == 0:
        return substitute(PART0_TEMPLATE, backup_values(backup, common))
    return split_parts(render_procedure(backup, common))[part]


def render_procedure(backup: Backup, common: dict[str, str]) -> str:
    """The whole procedure (steps 1-9) for one task, exactly as the full reference prompt carries it."""
    return substitute(PROCEDURE_TEMPLATE, backup_values(backup, common))


def split_parts(procedure: str) -> dict[int, str]:
    """Cut a rendered procedure at its step headings into three parts that concatenate back to it."""
    cuts = []
    for marker in PART_STARTS:
        index = 0 if marker == PART_STARTS[0] else procedure.find("\n" + marker) + 1
        if index <= 0 and marker != PART_STARTS[0]:
            raise ValueError(f"procedure has no {marker.strip()} heading")
        cuts.append(index)
    bounds = cuts + [len(procedure)]
    return {part: procedure[bounds[i]:bounds[i + 1]] for i, part in enumerate(PARTS)}


def find_backup(task_id: str) -> Backup | None:
    return next((backup for backup in BACKUPS if backup.task_id == task_id), None)


def part_token(task_id: str, part: int, directory: Path | None = None) -> str:
    """``t`` plus eight hex digits the agent must present to get the NEXT part (bound to the task, the part and its text).

    A guard against skipping or reordering, not a secret: it makes later parts fail instead of silently serving
    placement instructions before the preflight, gates, evidence and finalize steps have been read. An agent that
    ignores the prompt can still copy a token, but it cannot guess one.
    """
    backup = find_backup(task_id)
    if backup is None:
        raise KeyError(f"unknown task id {task_id!r}")
    if part not in (0, 1, 2):
        raise ValueError("token part must be one of [0, 1, 2]")
    body = _render_part_body(backup, part, directory)
    # 2026-11-05: the leading "t" is not decoration. Eight hex digits are all decimal about 2.3% of the time
    # ((10/16)**8), and 54ab24d2's part-0 token came out as one, so the reference prompt carried a bare 8-digit
    # number that the account-shaped-literal guard (test_no_em_dash_no_account_shaped_literal_no_secret) rightly
    # rejects. A letter glued to the front means no token can ever read as an account number, whatever the hash.
    return "t" + hashlib.sha256(f"{task_id}:{part}:{body}".encode("utf-8")).hexdigest()[:8]


def render_part(task_id: str, part: int, directory: Path | None = None, token: str | None = None) -> str:
    """One part of one task's procedure, framed so a wrong task or a skipped part is visible.

    ``KeyError``/``ValueError`` for an unknown task, an unknown part, or a missing or wrong ``token`` for parts 1-3.
    """
    backup = find_backup(task_id)
    if backup is None:
        raise KeyError(f"unknown task id {task_id!r}")
    if part not in (0, *PARTS):
        raise ValueError(f"part must be one of {[0, *PARTS]}")
    if part > 0 and token != part_token(task_id, part - 1, directory):
        raise ValueError(f"part {part} needs the --token printed at the end of part {part - 1}")
    body = _render_part_body(backup, part, directory)
    footer = f"END OF PART {part} OF {len(PARTS)} FOR TASK {task_id}\n"
    if part < len(PARTS):
        footer += (f"NEXT, only when this part's steps are done: python3 backup_planner.pyz procedure --task {task_id} "
                   f"--part {part + 1} --token {part_token(task_id, part, directory)}\n")
    return f"PROCEDURE FOR TASK {task_id} - PART {part} OF {len(PARTS)}\n\n{body.rstrip(chr(10))}\n\n{footer}"
