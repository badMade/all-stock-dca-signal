# Draft fixes for the 2026-10-15 9:35 verifier MISS (external prompt, 2026-10-15)

**Status: DRAFT for operator review. Nothing here is applied.** The 9:35 verifier is an
external scheduled task, so this repo cannot change it. The text below is for the operator to
paste into the task definition. It follows `EXTERNAL-PROMPT-FIXES-2026-10-12.md`, which already
holds the `verifier_window_mismatch` text; read that first. Nothing here relaxes a trading
safety rule or routes around a permission denial.

## What happened

The verifier checked the 9:35 slot at 10:01 ET, one minute after the 09:30 to 10:00 ET
acceptance window closed. It found no buy orders, called it a slot-specific MISS, and posted to
#dca-alerts and DM'd the operator. It skipped the Step 5a "no executor" notice because
#dca-alerts showed an active executor and `claude/dca-cutover-status-2026-07-27.md` said
otherwise.

## Findings

| # | Fact or inference | Evidence |
| --- | --- | --- |
| 1 | FACT: 10:01 ET is inside the grace period, not past it. `verify_schedule` treats a slot as failed only after window end plus `GRACE_MINUTES` (30), so 10:30 ET for this slot. At 10:01 the correct state was `awaiting_evidence`. | `scripts/verify_schedule.py` (`GRACE_MINUTES = 30`, `SLOT_EVIDENCE_LAYOUT["0935"]`) |
| 2 | FACT: the cutover doc named in the report is not in this repo and has no git history here. The verifier reads it from somewhere else. | `git log --all -- 'claude/dca-cutover-status*'` is empty |
| 3 | FACT: "Step 5a" text is not in this repo. | repo-wide search found nothing |
| 4 | INFERENCE: a dated status doc was used as evidence of *current* executor state. It was later overridden by live evidence, but the prompt gave no rule for which source wins. | the report's own "stale" note |
| 5 | UNKNOWN: why the 9:35 window has been empty on 12 earlier days since 09-18. A repo-side cause is not visible: the manifest row (`2ba19526`, `35 13 * * 1-5`, backup `4412c3c4` at 9:55 ET) is consistent, and 2026-10-15 is still EDT. Needs scheduler and broker evidence (see Investigation). | `docs/schedule_manifest.md` |

## 9:35 verifier

### `verifier_premature_miss`
Add: "Before declaring a slot MISS, run `python -m scripts.verify_schedule --json` and read the
slot's state. `awaiting_evidence` means not yet failed: post nothing, and re-check after the
slot's window end plus 30 minutes. Derive the window end from ET boundaries at run time, never
from a fixed UTC value. Only `no_evidence` or `evidence_incomplete` after that deadline are
reportable, and they are reported as 'evidence missing'. Say 'no orders in window' only when
broker order history for the window is complete (pagination chain closed, exit 0 from
`scripts.assemble_order_pages`) and empty."

### `verifier_stale_cutover_doc`
Add: "Executor state is a live fact, not a documented one. Never decide 'no executor' or
'active executor' from a dated status or cutover document. Use, in this order: (1) today's
run artifacts and `run_state.json` for each stream; (2) broker order history for today;
(3) recent #dca-alerts executor messages. If these disagree with each other or with a
document, trust the live sources, say which source was used, and list the disagreeing document
by name under 'possibly stale'. If none of the live sources can be read, report UNVERIFIED and
post neither the slot-miss nor the no-executor message."

### `verifier_alert_before_deadline`
Add: "Post at most one alert per slot per day, and only after the deadline above. If a later
check finds evidence for a slot already alerted, post a single correction on the same thread
rather than a new message. When the slot-specific and no-executor readings conflict, post
neither as fact: post one message that says the cause is unresolved and names both candidate
causes."

## Where the fix lives
- [ ] This repo: nothing to change. `scripts/verify_schedule.py` already encodes the grace period.
- [x] External prompt/task (the 9:35 verifier): paste the three items above.
- [ ] Session/connector layer.
- Already fixed on main? Not for the external prompt. The grace-period logic exists in the repo
  script but the verifier is not calling it.

## Verification procedure (no repo test applies)
1. Paste the text, then run the verifier by hand at 10:01 ET on a trading day with no 9:35
   orders. Expected: no Slack post and no DM; its output says `awaiting_evidence`.
2. Run it again after 10:30 ET with the order history still empty. Expected: exactly one alert
   worded 'evidence missing' or 'no orders in window', and it names the sources it used.
3. Point it at the cutover doc and at a live source that disagrees. Expected: the output cites
   the live source and lists the doc under 'possibly stale'.

## Investigation still open: 12 empty windows since 09-18
Not resolvable from this checkout (no `cron_tracking/`, no broker access). Hypotheses to test
in order, each with its check:

1. HYPOTHESIS: the verifier itself is too early on every day, so these 12 days are false MISSes.
   Check: for each of the 12 days, compare the 9:35 order history with the verifier's check time.
2. HYPOTHESIS: `2ba19526` fires but fails before ordering. Check: the `market_open_dca/YYYYMMDD_0935ET/`
   `run_state.json` `order_submit_status`, and `attempt_log/` markers for those days.
3. HYPOTHESIS: `2ba19526` does not fire. Check: the scheduler's last-run time for the task id, and
   whether the 9:55 backup (`4412c3c4`) also skipped.

## Risk and rollback
- Risk: low. Prompt text only for the verifier, which places no orders.
- Rollback: repaste the previous verifier prompt.
