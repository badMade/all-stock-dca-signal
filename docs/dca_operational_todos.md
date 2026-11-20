# DCA Operational Notes

This file tracks follow-up work from live DCA incidents so the same failure modes are not lost in chat history.

## Handoff - 2026-10-31 Perplexity backup fires reduced

- Status: operator decision (Perplexity credit plan step 5, `docs/cost_tracking.md`). Only the 10:00 (`4412c3c4`, moved from 09:55 on 2026-11-19), 12:50 (`062605c6`) and 15:40 (`f83b8d62`, was 15:45 until 2026-11-09) backup fires stay scheduled. `e028361e` (10:50), `e3590042` (11:50) and `54ab24d2` (13:50/14:50) are paused.
- This amends the 2026-09-08 standing policy below: the policy still never lapses, but each slot now records `backup_status` in `config/scheduled_tasks.json`.
- Operator step: pause the three tasks in Perplexity's scheduler; this repo cannot. To re-enable one, set it back to active in the config and resume the task.

## Handoff - 2026-09-25 silent-abort incident + independent watchdog

- Status: the 12:30 ET slot halted before Gate 4 and asked the (absent) user for
  authorization instead of following the standing rule in `docs/safeguards.md`'s
  live execution roster note ("a decline in a scheduled run is an abort that must
  alert on all three channels ... it must never end on a question"). The 6th
  recorded occurrence of this exact pattern since 09-14 (09-14, 09-15, 09-17,
  09-22 x2, 09-25). Because the session never reached its own Gate 9, nothing
  alerted on any of the three existing channels (in-app message center, repo
  alert file, email) and the day would have looked clean to every one of them.
- Root cause (inference, not directly observable): the only thing that could
  alert on this failure was the same session whose judgment had already failed
  -- a single point of failure covering both the decision and its own audit
  trail. This is model-judgment variance under an unattended prompt, not a
  connector outage: the same day's 11:30 slot failed for the opposite reason
  (no broker tools reachable at all), ruling out one shared cause.
- Remedy implemented: `scripts/schedule_watchdog.py` -- an independent,
  deterministic wrapper over `scripts.verify_schedule.verify_day` that does not
  depend on the executor session at all. Writes its own durable, idempotent
  repo alert file per failing slot (`write_watchdog_alert_files`, under
  `schedule_watchdog/alerts/`, honoring `--root`) and optionally posts to
  Slack (`DCA_WATCHDOG_SLACK_WEBHOOK`, falling back to
  `DCA_ALERT_SLACK_WEBHOOK` then `DCA_DRIFT_SLACK_WEBHOOK`). See
  `docs/schedule_verification.md`'s watchdog section for the full contract.
- Recommended cadence once deployed (deployment is outside this repo -- see
  below): the earliest a fire usefully catches a silent slot is roughly
  55-60 minutes after that slot's nominal time -- `verify_day` keeps a slot as
  `awaiting_evidence` (no alert) until its acceptance window ends plus the
  30-minute grace period, not merely until its nominal time. Run
  `python -m scripts.schedule_watchdog --slack` with a few extra minutes of
  margin on top of that (roughly 65-75 minutes after each of the seven slots'
  nominal times: 09:35, 10:30, 11:30, 12:30, 13:30, 14:30, 15:30 ET), or at
  minimum once daily well after 16:00 ET to catch the whole day in one pass.
  Pair with a daily
  `python -m scripts.alert_digest --drafts <export> --messages <export> --ledger
  --fail-on-warnings` run (needs a Gmail-connector session to produce the
  exports) so undelivered-channel drift (drafted-not-sent email, a gitignored
  alert path) is caught even on days the watchdog itself is clean -- see
  `docs/alert_digest.md`.
- Deployment caveat, same as every scheduled-task change in this repo: this
  file and the scripts it documents live in the repo; registering an actual
  recurring invocation (a Cowork task, a cron entry, however the operator's
  scheduler works) happens outside it and is not something a repo commit can
  do on its own. Nothing above runs on a schedule until that registration
  happens.
- Also see the live-mirror prompt-text hardening in
  `claude/dca-executor-prompts/README.md`'s Gate 4 entry (2026-09-25): the
  "conflicting directives" ambiguity between the Perplexity-coexistence
  framing and the never-ask rule was closed across all seven prompts, and the
  abort branch was restructured so there is no path in the prompt text that
  reaches an interactive question. That is a mitigation, not a fix -- a
  prompt cannot force a non-deterministic session to follow it -- which is
  why the watchdog above is the one remedy that does not depend on the
  session's cooperation at all. **Editing the repo copy still changes nothing
  live**: it takes effect only once a human re-pastes it via
  `update_scheduled_task` and the mirror is re-captured, same as every other
  correction in that file's own history.

## Handoff - 2026-09-08 standing Perplexity backup policy

- Status: user direction: "Perplexity shouldn't be disabled anywhere permanently... Perplexity is supposed to back up Claude if it fails, not be disabled." Also: "make sure that if Claude fails on a run that there is a mechanism to run it with perplexity."
- What this supersedes: the recurring pattern below (Aug 17, Aug 28, Aug 31, Sep 8-11 handoffs) of one-off, date-gated Perplexity buy authorizations that had to be reissued every few days, each disabling the previous batch's leftover task IDs. That churn read as Perplexity repeatedly being disabled; the actual standing intent, recorded in `claude/PERPLEXITY-CLAUDE-COORDINATION.md` since 2026-08-02 ("Long term, Claude is primary and Perplexity is backup"), was never disablement.
- What changed: `config/scheduled_tasks.json` -> `standing_perplexity_backup_policy` (new top-level key, non-expiring) is now the authoritative record. It covers all seven Claude slots, each backed up by a Perplexity task firing 10 minutes later: 09:35->09:45 (`4412c3c4`), 10:30->10:40 (`e028361e`), 11:30->11:40 (`e3590042`), 12:30->12:40 (`062605c6`), 13:30->13:40, 14:30->14:40, 15:30->15:40 (all three afternoon backups are one task, `54ab24d2`). All five backup task IDs were already live in the scheduler under the Sep 8-11 handoff below; nothing new was deployed to make this current, only the authorization's status changed from temporary to standing.
- The Sep 8-11 handoff below is marked superseded, not deleted, for audit history. Do not renew or extend it after 2026-09-11 -- the standing policy already covers what renewal would have re-granted.
- Real gap found and fixed in the same change: the 12:30 PM slot's own Gate 4 (`claude/dca-executor-prompts/live-mirror/1230ET-1230-pm-midday-buying-run.txt`) called `dca_runner.gate_sole_executor` directly, which ignores `placed_agent` entirely and has no compatible-executor branch -- so a genuine `062605c6` backup fill in that window would have aborted the whole midday batch as a rogue collision, the opposite of standing backup. The other six slots (including all three `7be82b97` fires) already recognized a compatible Perplexity order in prose per `claude/PERPLEXITY-CLAUDE-COORDINATION.md` section 3. Fixed by adding the same recognition to the 1230 prompt's Gate 4 script and Gate 5's candidate exclusion, mirroring the existing pattern rather than inventing a new one; see the coordination doc's updated section 5.5.
- `docs/schedule_manifest.md` carries the same standing table, positioned above the (now superseded) dated override blocks per this repo's own "authoritative table first" convention.
- Deployment caveat unchanged from every other entry here: this repo's config documents the intended standing policy; it does not and cannot create or remove tasks in Perplexity's own live scheduler. All five backup task IDs are already deployed there as of this handoff.

## Handoff - 2026-09-08 delayed morning scheduler cleanup

- Status: User explicitly authorized Perplexity to run only the delayed morning buy-only DCA schedule for Sep 8-11, 2026 so Claude gets first chance to buy first.
- Active Perplexity buy slots for this week-limited override: 09:45, 10:40, and 11:40 ET.
- Live scheduler task IDs left active for the override: `4412c3c4` at 09:45, `e028361e` at 10:40, and `e3590042` at 11:40. Expired Aug 28-gated buy tasks `0c3cde00` (12:30) and `44b3975d` (13:30/14:30/15:30) were deleted from the live scheduler.
- Authorization is date-gated to America/New_York trading days `2026-09-08` through `2026-09-11` only. It does not authorize 12:30 buys, afternoon buys, or any buys outside those delayed morning windows.
- Role split: Perplexity handles delayed scheduled buy-run execution plus required buy-run artifacts and notifications only. Claude handles scheduled reports and gets first chance to buy.
- Safety scope remains unchanged: buy-only Robinhood Agentic account <ROBINHOOD_ACCOUNT_NUMBER>, active whitelist only, regular-hours only, no `review_equity_order`, no per-order confirmation, no sells, no cancellations, no options, no crypto, no margin, and no account-setting changes.
- Repo mirror: `config/scheduled_tasks.json` records this under `temporary_live_scheduler_overrides.authorize_2026_09_08_delayed_morning_perplexity_buy_slots_week_only`; `docs/schedule_manifest.md` mirrors the same dated override.

## Handoff - 2026-08-31 morning scheduler override

- Status: User explicitly authorized Perplexity to run the morning buy-only DCA schedule for Monday, Aug 31, 2026 at 09:35, 10:30, and 11:30 ET.
- Active Perplexity buy slots for this same-day override: 09:35, 10:30, and 11:30 ET.
- Live scheduler task IDs for the override are the existing canonical tasks: `2ba19526` at 09:35, `42173154` at 10:30, and `7be82b97` at 11:30. Parallel one-time IDs `4e8c92af`, `01e4f18b`, and `6db8bbf1` must remain disabled.
- Authorization is date-gated to America/New_York trading day `2026-08-31` only. It does not reactivate the recurring Aug 28-gated buy slots or authorize afternoon buys.
- Role split: Perplexity handles scheduled buy-run execution plus required buy-run artifacts and notifications only. Claude handles scheduled reports. Perplexity buy tasks must not perform dashboard reporting, Notion report refreshes, slippage-only reports, monitor-only reports, or watchdog-only reporting.
- Safety scope remains unchanged: buy-only Robinhood Agentic account <ROBINHOOD_ACCOUNT_NUMBER>, active whitelist only, regular-hours only, no `review_equity_order`, no per-order confirmation, no sells, no cancellations, no options, no crypto, no margin, and no account-setting changes.
- Repo mirror: `config/scheduled_tasks.json` records this under `temporary_live_scheduler_overrides.authorize_2026_08_31_morning_perplexity_buy_slots_today_only`; `docs/schedule_manifest.md` mirrors the same dated override.

## Handoff - 2026-08-28 intraday scheduler override

- Status: User explicitly authorized Perplexity to run all scheduled buy-only DCA tasks for Friday, Aug 28, 2026, including the missed 09:35 ET run as a same-day catch-up.
- Active Perplexity buy slots for this same-day override: 09:35 catch-up, 10:30, 11:30, 12:30, 13:30, 14:30, and 15:30 ET.
- Live scheduler task IDs for the override: `2c1d3792` one-time 09:35 catch-up, then canonical IDs `42173154` at 10:30, `7be82b97` at 11:30/13:30/14:30, `a6239dae` at 12:30, and `10ce362c` at 15:30. Canonical ID `2ba19526` remains the authorized 09:35 task, although its scheduled occurrence had already passed before the catch-up was created. Parallel replacement IDs `4412c3c4`, `e028361e`, `e3590042`, `0c3cde00`, and `44b3975d` must remain disabled.
- Authorization is date-gated to America/New_York trading day `2026-08-28` only. Recurring slots should silently no-op after that date unless the user gives a new authorization.
- Role split: Perplexity handles scheduled buy-run execution plus required buy-run artifacts and notifications only. Claude handles scheduled reports. Perplexity buy tasks must not perform dashboard reporting, Notion report refreshes, slippage-only reports, monitor-only reports, or watchdog-only reporting.
- Safety scope remains unchanged: buy-only Robinhood Agentic account <ROBINHOOD_ACCOUNT_NUMBER>, active whitelist only, regular-hours only, no `review_equity_order`, no per-order confirmation, no sells, no cancellations, no options, no crypto, no margin, and no account-setting changes.
- Repo mirror: `config/scheduled_tasks.json` records this under `temporary_live_scheduler_overrides.authorize_2026_08_28_all_perplexity_buy_slots_today_only`; `docs/schedule_manifest.md` mirrors the same dated override.

## Handoff - 2026-08-21 close

- Status: Perplexity-primary buy-only execution remained the active primary workflow for the Aug 17-21 limited schedule. Claude remains backup only unless the user changes the role assignment.
- Active live-buy slots for the limited schedule: 09:35, 10:30, 11:30, 12:30, 13:30, 14:30, and 15:30 ET. These are the only tasks that buy or directly affect buying.
- Disabled validators for this week: 15:35 slippage/latest-run sync and 15:40 watchdog were intentionally disabled for Aug 17-21. They should resume only if the next-week authorization explicitly enables them or the disable gate expires as expected.
- 09:15 pre-validation status: paused for one week by user request. Before changing or restoring it again, verify the canonical scheduled task id and name because prior handoffs show both a 09:15 readiness scan and older 09:45/preflight wording tied to `bb4f6ef1`.
- Aug 21 scheduled buy runs completed:
  - 09:35 ET placed 11 filled buys totaling $17.00: CBRS, MRVL, AMAT, SPCX, AAPL, BTX, SNDK, INTC, AMZN, NFLX, and TXN.
  - 10:30 ET placed 13 filled buys totaling $20.50: TXN, GFUZ, MU, GLW, CBRS, WDC, NVDA, MRVL, STX, IRDM, AMD, LITE, and DXYZ.
  - 11:30 ET placed 2 filled buys totaling $6.00: MRVL and GLW.
  - 12:30 ET placed 1 filled buy totaling $2.00: AMAT.
  - 13:30 ET placed 1 filled buy totaling $1.00: TXN. DXYZ, INTC, and MU each had a $0.50 top-up remainder skipped by the $1 broker minimum.
  - 14:30 ET placed 1 filled buy totaling $1.00: DFTX. DXYZ and GFUZ each had a $0.50 remainder but were blocked by the normal 14:30 spread guard.
  - 15:30 ET placed 16 filled buys totaling $30.50: DFTX, DXYZ, LITE, AAPL, AMAT, CBRS, MRVL, BTX, WDC, NVDA, INTC, AMZN, GLW, MU, NFLX, and TXN.
- Aug 21 final state: same-day strategy total $78.00, weekly total $470.00, and post-pass buying power $223.54 after the 15:30 run.
- Minimum-order aggregate top-up status: no separate 15:30 aggregate minimum-order top-up was needed on Aug 21 because later final-pass fills satisfied the qualifying remainders. Continue to keep this logic integrated inside the 15:30 task, not as a separate 15:33 task.
- Artifact roots to inspect first if reconciliation is needed: `/home/user/workspace/cron_tracking/perplexity_primary_20260821/2026-08-21_0935ET/`, `..._1030ET/`, `..._1130ET/`, `..._1230ET/`, `..._1330ET/`, `..._1430ET/`, and `..._1530ET/`.
- Open follow-ups:
  - Investigate the Aug 19 09:35 Slack safety-classifier block if Slack notification reliability matters next week.
  - Investigate Aug 21 15:30 Alpaca validation unavailability if cross-broker validation remains required for final-pass confidence.
  - Confirm whether the Aug 17-21 disabled gates should be extended, removed, or replaced before the next trading week.
  - Keep the Aug 18 manual override artifacts excluded from scheduled-live metrics via the existing manual override classification rules.

## 2026-08-03 follow-ups

| Priority | Area | Problem observed | Follow-up fix |
| --- | --- | --- | --- |
| High | 11:30 intraday scheduling | The active schedule did not contain the 11:30 Perplexity-primary buying slot when checked at 11:35 AM ET. | Add a pre-market schedule integrity check that verifies every primary buying slot for the current trading day: 9:35, 10:30, 11:30, 12:30, 1:30, 2:30, and 3:30. If a slot is missing, create or repair it before market open and write a compact schedule readiness artifact. |
| High | Latest pointer publishing | The 9:35 Perplexity-primary run wrote complete root artifacts and 25 filled orders, but the expected latest pointer was missing, which made pointer-based checks report no record. | Require each Perplexity-primary buying run to publish both its canonical stream pointer and the Perplexity-primary same-day alias pointer after root artifacts are written. Add a post-write assertion that fails the run artifact contract if the pointer is missing. |
| High | Direct path fallback | The 12:30 task needed to use 9:35 fills as top-up context even when the 9:35 latest pointer was missing. | Keep the direct date/window artifact fallback for same-day top-up context. Pointer absence should be recorded as `latest_pointer_missing_artifact_contract_failure`, but direct path discovery should still load completed same-day root artifacts. |
| High | Alias pointer fallback | The 11:30 catch-up orchestration can see `latest_pointer_missing_artifact_contract_failure` when the canonical pointer is absent even though the Perplexity-primary alias root has a valid same-day pointer and complete artifacts. | Implemented shared fallback support in `scripts/same_day_pointer.select_same_day_run_state()`: the helper now checks the dated Perplexity-primary alias pointer automatically after the canonical pointer fails, while still allowing caller-supplied fallback pointers. It validates same-day, stream, root containment, target run_state, and scheduled non-manual evidence before returning a state, so future pointer-missing events can self-heal without trusting arbitrary files. |
| Medium | Artifact completion timing | A raw-input-only directory was visible before the finalized 9:35 run_state and order CSV files appeared, causing an early false read of the run as incomplete. | Implemented as `scripts/scheduled_artifacts.await_finalized_run_artifacts()`: it re-runs the artifact contract (required root files plus a non-null `run_classification`) until the run_state also carries a completion marker — any non-empty field of `COMPLETION_MARKER_FIELDS` — or the grace period expires (default 20s, polled every 1s). The window can only delay a verdict, never invent one: once it closes the contract result is returned exactly as `validate_run_artifacts` computed it, so a contract-valid run that writes no marker still reports `ok=True` (with `finalized=False`) and a genuinely broken run still reports its real errors. The clock and sleep are injectable so callers and tests can drive the window deterministically. Use it for a run that fired MOMENTS ago; `validate_run_artifacts` alone stays correct once the writer has returned. |
| Medium | Broker minimum order size | At 10:30, AAPL qualified for a small same-day top-up, but Robinhood rejected it because the dollar amount was below the broker minimum. | Resolved 2026-08-14: the 3:30 final pass now owns the broker-minimum carry-forward policy. It aggregates same-day top-up remainders by ticker when they were blocked earlier only by Robinhood's $1 minimum. Aggregate remainder >= $1.00 buys the aggregate amount, $0.50 to <$1.00 rounds up to exactly $1.00, and <$0.50 is forgotten for the day. The behavior is documented in `config/scheduled_tasks.json` and `docs/schedule_manifest.md`. |
| Medium | Slack and in-app diagnostics | Some failures were visible only after manual checks or background result messages. | Ensure every buying run notifies only on placed orders, blocking errors, broker mismatch, missing artifacts, drift, or meaningful exceptions. Include the artifact root, run_id, and whether top-up context was loaded from pointer or direct path. |
| Low | Cost control | Recovery and debug churn drove July usage above the expected band. | Prefer targeted repair passes for missing slots or failed tickers. Avoid full-universe manual reruns unless a scheduled run fails and drift detection confirms missing orders. |

## Definition of done for these follow-ups

- Schedule integrity check writes a same-day readiness artifact before the first buying slot.
- Every primary buying slot has a same-day task or an explicit disabled reason.
- Every buying run writes `run_state.json`, `run_summary.json`, `signals.csv`, and `submitted_orders.csv` at the root.
- Every buying run writes a non-null `run_classification`.
- Every buying run publishes a same-day latest pointer after root artifacts are written.
- Later same-day runs can load prior fills from both validated pointers and direct date/window artifact paths.
- Pointer-based orchestration checks the same-day Perplexity-primary alias pointer with its alias root before surfacing a missing canonical pointer as a terminal contract failure.
- Missing pointers are recorded as artifact-contract failures without erasing valid order evidence.
- A just-fired run is given a bounded finalization window before its artifacts are judged, and that window never converts a passing contract into a failure.
