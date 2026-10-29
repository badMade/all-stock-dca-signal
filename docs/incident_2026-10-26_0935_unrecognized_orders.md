# Incident 2026-10-26: 0935 payload refused, Perplexity backup placed 25 buys on its own plan

Status: understood. The Claude 0935 slot placed nothing, and the Perplexity backup (task `4412c3c4`) placed
the 25 buys. An earlier version of this note attributed them to Claude's reader. That was wrong. Open items
listed at the end.

## Summary

The Claude 09:35 ET slot on 2026-10-26 had no usable payload and finalized at 09:40 with no orders. A run
of the Perplexity "9:55 AM ET DCA backup pass" (task `4412c3c4`) started at 09:46 ET, nine minutes before its
scheduled time. The operator confirmed they started it by hand. It could not reach the Mac and found no published recommendation set, so it computed its own
plan in the cloud (`plan_source cloud_compute`) and placed 25 orders between 09:48:56 and 09:50:40 ET. That
is the backup's designed behavior when both the Mac and the published set are absent, not a gap in the
Claude slot.

## Facts

Verified from the Robinhood MCP account (ending 7042, read-only), the Mac's run folder as pasted by the
operator, the Perplexity run transcript, and this repository:

- 25 filled buys were created 09:48:56 to 09:50:40 ET, all market orders, regular hours, day
  time-in-force, agent-initiated, dollar-based, $1.09 to $4.61 each, $52.40 in total.
- The Perplexity run's own summary reports 25 of 26 approved orders placed, $52.40 in total, status
  `placed`, state record `run_state_20261026_0951ET.json`. Its last order call completed at 09:50:40 ET.
  Count, total and timing match the broker.
- That run reports: gates passed at 09:46 ET; Mac not reachable (empty device list); the `recommendations`
  branch returned 404; both evidence polls classified CLEAR (it saw no earlier orders in the window); skipped
  GFUZ and RVI (spread), ABT (quote not below prior close at finalize) and GFS (per-order gate).
- The 9:57 run of the same task (a second thread) passed its gates at 09:57:22 ET, found the Mac reachable but
  `executor-ids` empty and no published set, classified the 25 orders as UNRECOGNIZED, placed nothing and sent
  a push notification (`stood_down_unrecognized_order`). Its own assessment said the 25 orders "look like the
  primary Claude slot running late". That was an inference from the empty ids. It did not know the earlier run
  of its own task had placed them. The operator's incident summary repeated that attribution, and the first
  version of this note inherited it.
- Claude's 0935 run folder (`market_open_dca/20261026_0935ET`) holds `payload_plan_0935.json` (09:38) and
  header-only `submitted_orders.csv` and `market_open_signals.csv`. All state files were written by 09:40.
  There is no `market_data_*` and no `candidates_*` file, so no buy list was built.
- `recommendations/2026-10-26/` holds only `raw/incoming/` fetch plans every five minutes. There is no
  computed set and no `placements/` folder.
- Ten more agent buys came later from other batches (about 10:56, 11:42, 12:56 and 15:00 ET), for
  $16.61. The day total for the account from 09:00 ET is 35 orders and $69.01.
- PR 1961 is merged into `main` as `5eafd4b6`. The Mac Studio checkout is at `8c219fe9`, which
  contains it.

Reported by the operator, not independently verified:

- The `codex exec` workers for chunks 4 and 10 died on both retries, leaving 16 of 91 tickers
  without data. Coverage was 75/91 (82%), below the 95% floor, so the 0935 payload was refused.
- The 10:30 fetch succeeded at 90/91, with one stale bar for FLKR.

## Cause

1. The 0935 payload was refused (operator-reported cause above), so the Claude slot had no data and
   finalized without ordering. Inference: its 09:40 finish, about five minutes after the slot, suggests the
   deployed prompt still used the older five-minute payload wait. The repo's prompt waits up to 12
   minutes. Not verified.
2. With no Claude orders and no published set, the backup's classification was CLEAR and it computed its
   own plan. It started early because the operator started it by hand (confirmed in chat; the reason and the
   method were not recorded).
3. The classifier (`scripts/backup_planner.py`, `_classify_row`) accepts an order as the Claude reader's
   only through a placement-file order id, a `dca-runner-<slot>-` reference, the derived native reference,
   or a published-set match. A cloud-computed backup order matches none of these. Inference: a later run
   in the same window would read these 25 orders as unrecognized and stand down. Confirmed by the 9:57
   run, which did exactly that (see Facts). A backup therefore cannot recognise its own sibling run's orders.

## Impact

- Money: none beyond the intended strategy. $52.40 for the 25 orders and $69.01 for the day,
  against the $180 Normal daily cap (38%) and the $16 backup per-order ceiling. The weekly cap was not
  checked precisely. About 8% of $900 if the week started Monday (assumption).
- Reporting: the 25 orders have no Claude-side evidence. The backup's own state record is in Perplexity's
  project files. The classifier is scoped to one slot window on one trading day, so Oct 27 is unaffected.
- Caveat: caps pool every stream, and the figures above cover only this account from 09:00 ET.

## Decision: no manual evidence

There is no sanctioned command to mark orders as recognized after the fact, and none is needed: the
orders belong to the backup, whose state record is the evidence. Writing Claude placement files by hand
would forge evidence of something the Claude reader did not do, so none were written.

## Actions taken

- PR 1961 merged to `main` as `5eafd4b6` (dead-chunk rescue for the payload fetch).
- Mac Studio checkout pulled with `git pull --ff-only origin main`. Result: already up to date at
  `8c219fe9`, which has `5eafd4b6` as an ancestor.

## Follow-up: 2026-10-27 slots

The day after, the Claude slots did not place nothing. The broker shows 43 agent-initiated buys for $76.23
(42% of the $180 daily cap) on account ending 7042, so no cap blocked anything. `placed_agent: agentic` is
shared by Claude and Perplexity, so the broker cannot attribute a batch to a slot. The slot column below is
an inference from timing.

| Batch (ET) | Orders | Total | Likely slot |
| --- | --- | --- | --- |
| 09:52 to 09:54 | 10 | $17.80 | 09:35 |
| 10:30 to 11:59 | 0 | $0 | 10:30 placed nothing |
| 12:02 to 12:04 | 9 | $32.36 | 11:30, started about 32 minutes late |
| 13:01 to 13:02 | 10 | $20.29 | 12:30, after a refusal and retry (inferred) |
| 13:59 | 2 | $2.91 | 13:30 |
| 14:58 | 1 | $1.44 | 14:30 |
| 15:40 | 1 | $1.43 | 15:30 |

Verified from the operator's Mac (2026-10-28):

- Run folders exist for 09:35, 10:30, 11:30, 12:30, 13:30 and 14:30. The 15:30 folder was not listed by the
  command used.
- `grep -ril "PLATFORM REFUSAL" cron_tracking/*/20261027_*` matched one file only:
  `market_open_dca/20261027_0935ET/alert_text.txt`.
- **09:35 placed nothing.** Its alert says Gate 7 failed: the auto-mode classifier (`[Real-World Transactions]`)
  refused `place_equity_order` twice (the first call plus one retry, on SYY at $2.46) before the broker saw
  it. The batch stopped, no order was placed, and no placement attempt was consumed. Gates 0 to 6.7 had passed
  with this resolved list: SYY $2.46, KVUE $2.36, SJM $2.13, MCK $1.78, IBM $1.78, MA $1.64, NFLX $1.55,
  MSFT $1.53, MRK $1.36, RVI $1.29, CHE $1.28 ($19.16 in all). 21 other candidates were dropped at the refresh
  because their price had recovered above the Gate 5 decision price. The alert named the remedy: grant
  classifier permission, or place the listed buys by hand. The 09:35 window closes at 10:00 ET.
- **The 09:52 to 09:54 batch is that same list, placed by the operator.** The ten broker orders match the
  alert's amounts exactly, SYY through CHE, minus MRK $1.36 (total $17.80). The operator confirmed in chat that
  they placed this batch by hand, following the alert's remedy. The orders show `placed_agent: agentic`, so
  they went through an agent session, not the Robinhood app. Reported by the operator, not independently
  verified. It means the Perplexity backup was not involved and there was no double buy. Why MRK was left out
  was not recorded.
- `scripts.check_slot_fills` fails for 09:35, 10:30 and 12:30 because `gate9_report` lacks `eligible_count`
  and the three `dropped` counts, and for 13:30 because `run_state.json` lacks the `counts` and `gate9_report`
  objects. Only 09:35 also fails on the refusal marker. 10:30 and 12:30 therefore show no classifier text in
  their reports, so their cause is still unknown.
- Inference: the missing counts match a live prompt that predates the mirrors, whose `--report-json` bullet
  tells the slot to supply them (`docs/schedule_verification.md`, "Post-slot submission check"). Not
  verified against the live task text; `scripts/check_live_prompt_drift.py` on the Mac would confirm it.

Not known: why MRK was left out of the hand-placed batch, why 10:30 placed nothing, what the 13:30 `market_data_unavailable` and
14:30 zero-buy statuses mean for the 13:59 and 14:58 orders, and whether the 13:01 batch was a 12:30 retry.

Likely cause of the 09:35 stop: the Cowork task's classifier permission for `place_equity_order`.
`.claude/settings.json` allows the tool, but it governs sessions in this repo, not the live tasks. Prior
instances: 2026-09-04 and 2026-10-07.

## Open items

1. **Manual early start (answered).** The operator started the 9:46 run by hand. The run passed its gates
   because `scripts/backup_gates.py` lets a single-fire backup start from ten minutes before its fire time
   (09:45 for a 09:55 fire), pinned by `scripts/tests/test_backup_gates.py`. 09:46 was inside that tolerance
   by one minute. An earlier version of this note wrongly said the window opened at the slot's 09:35 start.
   Record the reason next time, since the run's transcript shows no trigger.
2. **Misattribution.** Two agents reached "Claude placed these" from an empty `executor-ids` alone. Treat an
   empty list as "not recorded by the Claude reader", not as "placed by Claude", in future write-ups.
3. **Duplicate-buy risk.** A backup started early, by hand or otherwise, can buy while the Claude slot is
   still waiting for its payload. Here the Claude run had already finalized at 09:40, so there was no
   double buy. Decide whether the backup's start time needs a lower bound.
4. **Verify the deployed 0935 Claude prompt** matches `claude/dca-executor-prompts/live-mirror/` (12-minute
   wait). Repaste if not.
5. The Perplexity run warned that the market-open config's `live_task.broker_account` could not be
   resolved and would record `account_pinning_failed`. It did not affect this slot. Check whether the
   sandbox needs the account variable.
6. Tomorrow (2026-10-27) after the 09:35 slot, confirm coverage reached the 95% floor,
   `placements/0935/*.json` has per-symbol files, and the `recommendations` branch has a set. After the
   09:55 run, confirm its classify step passed.
7. The `#dca-alerts` Slack post failed with `not_in_channel`. Add the app to the channel to enable alerts.
8. Check whether the later batches (about 10:56 to 15:00 ET) were recognised.
