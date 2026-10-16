# Draft verifier fixes for the missed 2026-09-30 9:35 alert (external prompt, 2026-10-16)

**Status: DRAFT for operator review. Nothing here is applied.** The 9:35 verifier is an external
scheduled task, so this repo cannot change it; the operator pastes accepted text into the task
definition. Read `EXTERNAL-PROMPT-FIXES-2026-10-12.md` and `EXTERNAL-PROMPT-FIXES-2026-10-15.md`
first; this file extends them. Evidence is in
`claude/dca-935-slot-empty-window-investigation-2026-10-16.md`. None of these items relaxes a
trading safety rule or routes around a permission denial.

## What was missed

On 2026-09-30 the 9:30 to 10:00 ET window was empty (first buy 10:10:57 ET, from broker order
history) but `#dca-alerts` has no 9:35 verifier message that morning. Every other empty day
from 09-18 to 10-15 got one. Two related observations from the channel history:

- **The repeat-pattern list is carried forward, not recomputed.** The 10-01 alert says the window
  was empty on "09-18, 09-23, 09-25, 09-29" and then adds that "yesterday's earliest fill batch
  didn't start until ~14:11 UTC, just past this window's close". Yesterday was 09-30, so it
  had the fact that 09-30 was empty in the same message and still left it off the list. Later
  alerts inherit the gap.
- **The 09-29 alert was posted at 11:23 ET**, about 80 minutes after the other days' 10:01 to
  10:06 ET alerts, so the verifier does not always run at the same time.

## Findings

| # | Fact or inference | Evidence |
| --- | --- | --- |
| 1 | FACT: no 9:35 verifier post exists for 2026-09-30 in `#dca-alerts`. Only the 17:14 ET run-log failure and the 18:31 ET month-end refresh appear. | channel history |
| 2 | FACT: 09-30 was empty in the window: first order 10:10:57 ET, 0 orders 09:30 to 10:00 ET. | broker history |
| 3 | FACT: the 10-01 alert did not list 09-30 as empty although it cited 09-30's first fill time. | 10-01 10:02 ET alert |
| 4 | HYPOTHESIS A: on 09-30 the verifier ran late, queried with `created_at_gte` only, saw the 10:10 batch, and passed. `docs/schedule_manifest.md` tells verifiers to pass `W_START` as `created_at_gte` and names no upper bound; `get_equity_orders` has no upper-bound filter. | manifest DST rule; tool schema |
| 5 | HYPOTHESIS B: the task did not run, or ran and failed before posting, and nothing records that. | no heartbeat exists |
| 6 | HYPOTHESIS C: it ran, saw the empty window, and a deduplication or "already alerted" rule suppressed the post (the 09-29 alert was still open). | unverified |

Which hypothesis is true is **unknown**. The task's run history for 09-30 settles it. Items 1
and 2 below fix A and C regardless; item 3 makes B visible.

## 9:35 verifier

### `verifier_unbounded_window_false_pass`
Add: "Always bound the window on BOTH sides. `get_equity_orders` only takes `created_at_gte`, so
fetch with `created_at_gte` = the window start, then discard every order whose `created_at` is
after the window end before counting. Derive both bounds from the ET window at run time
(09:30 and 10:00 ET for this slot), never from a fixed UTC value. A run that happens after the
window end (including the re-check after the grace deadline from the 2026-10-15 draft) must
count only orders inside the window. Orders after the window end are reported separately as
'late batch', with the first order time and the count, and never turn a missing window into a
pass."

### `verifier_always_report_daily`
Add: "On every trading day, post exactly one message from this task by the grace deadline: either
the slot alert, or a one-line 'ok' (`9:35 window: N orders, first at HH:MM ET`). Silence is not
a valid result. If the task cannot read the broker, post 'UNVERIFIED' with the reason. If a
message for today's slot already exists in the channel, post nothing new and edit nothing, but do
not skip the day's check because an earlier day's alert is still open."

### `verifier_history_from_data`
Replace the repeat-pattern text with: "Never copy the list of earlier empty days from a previous
alert. Recompute it each time from broker history for the last 20 trading days using the bounded
window rule above, and print the count and the dates. Include days that were empty but never
alerted."

### `verifier_backup_attribution`
Add: "When the window is empty, state whether a later batch exists and when it started. Do not
call the slot 'covered' unless the batch's task is known. A batch within the grace period (until
window end plus 30 minutes, 10:30 ET) is reported as 'late batch, executor unknown' rather than
'no orders in window'. The sole-executor and top-up rules stay as they are."

## Where the fix lives
- [ ] This repo: nothing to change.
- [x] External prompt/task (the 9:35 verifier): paste the four items above.
- [ ] Session/connector layer.
- Already fixed on main? No.

## Verification procedure (no repo test applies)
1. Replay 2026-09-30 by hand: run the verifier prompt at 10:20 ET against that day's history.
   Expected: it reports an empty window and a late batch at 10:10 ET, and does not pass.
2. Run it on 2026-10-06 (3 orders in window). Expected: 'ok', no alert.
3. Block its broker access once. Expected: an 'UNVERIFIED' message, not silence.
4. Check that the repeat-pattern line lists 09-30.

## Risk and rollback
- Risk: low. Prompt text only for a read-only verifier that places no orders.
- Rollback: repaste the previous verifier prompt.
