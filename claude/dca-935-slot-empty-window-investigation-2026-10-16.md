# 9:35 slot: 13 empty windows, 2026-09-18 to 2026-10-15 (investigation, 2026-10-16)

**Status: investigation notes. Nothing here is applied and no order was placed.** Read-only
evidence from broker order history and `#dca-alerts`. The executors run on the operator's Mac,
which this investigation could not reach, so the root cause is **not confirmed**.

Companion to `claude/triage-routine/EXTERNAL-PROMPT-FIXES-2026-10-15.md`, which covers the
verifier's false-alarm wording. That doc guessed the first hypothesis was "the verifier ran too
early". The data below refutes it: the orders really were absent from the 09:30 to 10:00 ET
window.

## Answer

The canonical 9:35 task (`2ba19526`) placed no orders on 13 mornings. The most likely causes are
(a) the live task still carries an old Gate 5 that ends every fire with no buy list, or (b) the
task intermittently does not run to placement. The 9:55 backup (`4412c3c4`) appears to cover
those mornings with a batch at 10:01 to 10:11 ET. Verify with the three checks at the end.

## Evidence

Source: `get_equity_orders` for the agentic account, created_at from 2026-09-18, 702 orders, all
pages read (the last page had no `next`). Times are ET.

| Day | Orders in 09:30 to 10:00 ET | First order | Note |
| --- | --- | --- | --- |
| 09-18 Fri | 0 | 13:59 | verifier alerted; 7 orders all day |
| 09-21 Mon | 8 | 09:39 | random (v4) refs; batch 09:39 to 09:42 |
| 09-22 Tue | 26 | 09:44 | random (v4) refs |
| 09-23 Wed | 0 | 11:04 | 35-order batch at 11:04 to 11:09 |
| 09-24 Thu | 20 | 09:51 | v5 refs |
| 09-25 Fri | 0 | 10:03 | |
| 09-28 Mon | 26 | 09:40 | v5 refs |
| 09-29 Tue | 0 | 10:07 | |
| 09-30 Wed | 0 | 10:10 | **no verifier alert was posted** |
| 10-01 Thu | 0 | 10:11 | |
| 10-02 Fri | 0 | 10:02 | |
| 10-05 Mon | 0 | 10:04 | |
| 10-06 Tue | 3 | 09:40 | v5 refs |
| 10-07 Wed | 4 | 09:42 | v5 refs |
| 10-08 Thu | 0 | 10:03 | 35 orders at 10:03 to 10:05 |
| 10-09 Fri | 0 | 10:04 | |
| 10-12 Mon | 0 | 10:03 | 42 orders at 10:03 to 10:05 |
| 10-13 Tue | 0 | 14:11 | only 13 orders all day |
| 10-14 Wed | 0 | 10:01 | 48 orders at 10:01 to 10:03 |
| 10-15 Thu | 0 | 15:49 | only 3 orders all day; the 9:35 verifier alerted |

Empty days: 13 (09-18, 09-23, 09-25, 09-29, 09-30, 10-01, 10-02, 10-05, 10-08, 10-09, 10-12,
10-13, 10-14), plus 10-15. The verifier's alerts list 12 of the first 13; it missed 09-30.

## Facts

1. In-window orders on six days (09-21, 09-22, 09-24, 09-28, 10-06, 10-07) landed at 09:39 to
   09:54 ET. On 09-24, 09-28, 10-06 and 10-07 the ref IDs are UUID version 5, which matches the
   live 9:35 prompt's deterministic `dca-runner-0935-<date>-<ticker>` key. On 09-21 and 09-22 they
   are random (v4), so only the last four are positively the runner path.
2. On empty days the first large batch lands at 10:01 to 10:11 ET with random ref IDs whose
   version differs by day (v4, v7, v9, v0). That is not the deterministic format.
3. `claude/dca-executor-prompts/live-mirror/0935ET-935-am-dca-morning-buy.txt` and
   `claude/dca-executor-prompts/README.md` state that the committed 9:35 copy is ahead of the live
   task for Gates 5 and 7, and that until it is applied through `update_scheduled_task` the live
   task "will keep ending every fire with no buy list". `scripts/generate_market_open_candidates.py`
   exists in the repo.
4. No 9:35 executor gate alert appears in `#dca-alerts` on any empty day. Other slots did post
   gate aborts on 09-18, 09-21 and 09-22.
5. The 2026-10-12 scheduler check reported all 7 slots and the digest as fired and succeeded, yet
   that day's 9:35 window was empty.
6. The run-log recorder has written no rows since 2026-09-18 (no transcript access), so there is no
   per-slot status record for the empty days.

## Inferences

| Claim | Confidence |
| --- | --- |
| The 10:0x batches come from the 9:55 backup (`4412c3c4`), not the 10:30 slot. Timing fits (fire 9:55, orders about 10:02 to 10:10); the 10:30 slot's runner-tagged batch is separate (about 10:46). | medium |
| The 9:35 path works sometimes, so the task is not simply dead or fully on old text. Intermittent failure (Mac asleep, shell or connector down at fire time) fits. | medium |
| The random ref IDs on the backup's batches suggest it is not using the deterministic planner refs its prompt describes. Unverified. | low |
| Live task text is still the old Gate 5 on at least some of these days. Fits fact 3 and fact 4, but the six working days argue against it being old on all days. | low to medium |

## Not verified

- Whether the live `~/Claude/Scheduled/935-am-dca-morning-buy/SKILL.md` matches the repo mirror.
- Whether the 9:35 task fired on each empty day, and where it stopped.
- Whether the Mac was awake at 09:35 ET on those days.
- Which executor placed each 10:0x batch (`placed_agent` is `agentic` for every order).

## Checks that settle it (operator, on the Mac or scheduler)

1. Diff the live 9:35 task body against the repo mirror, with the account placeholder expanded.
   If the live Gate 5 is the old text, apply the mirror body via `update_scheduled_task`.
2. Read `cron_tracking/market_open_dca/YYYYMMDD_0935ET/run_state.json` for 10-12, 10-13 and
   10-14. Expected: `order_submit_status` and the failing gate, or no directory if it never ran.
3. Compare the scheduler's last-run time for `2ba19526` against Mac sleep logs for the empty days.

## Follow-ups worth a ticket

- The verifier skipped 09-30, so its alert list is incomplete.
- The run-log recorder has been blind since 09-18; give it a transcript source or have each slot
  post a status line to `#dca-alerts`.
- Backup batches with non-deterministic refs weaken the sole-executor and idempotency guarantees;
  confirm what ref format `4412c3c4` actually sends.
