# Group A: paste-ready prompt text and Notion dispositions (2026-10-17)

**Status: DRAFT for operator review. Nothing here is applied to any live task.** Docs plus one drift check;
no order-placement, cap or config change. Keys are the DCA Issues `Key` values so the triage reconciler can match
this PR.

Why a doc and not code: the digest, run-log recorder, 9:35 verifier and Friday dashboard refresh are external
scheduled tasks. A repo grep finds no `lastRunAt`, device-bridge or transcript logic in `scripts/alert_digest.py`,
`dca_weekly_dashboard.py` or `scripts/verify_schedule.py`, so the hypotheses in the handoff do not describe repo code.
The text below consolidates `EXTERNAL-PROMPT-FIXES-2026-10-12.md` (verbatim) and points at the 10-15 and 10-16
verifier drafts. Paste into the named task, one task at a time.

## 1. Digest task (paste all four additions)

**`false_all_clear`**
> Determine each slot's outcome from `run_state.json` `order_submit_status` and the Gate 9 evidence
> (`python -m scripts.verify_schedule --json`), never from a task's `lastRunAt`. A task that ran and failed has a
> recent `lastRunAt`. `verified` only proves the slot fired and finalized: a `not_submitted_error` run can still be
> valid scheduled evidence (`config/run_classification.json`). Report ALL CLEAR only when every due slot is
> `verified` AND its `order_submit_status` is an explicitly non-failing value from that file's
> `order_submit_status_values`. A failing or unrecognized value, an absent outcome field, or any source that could
> not be read is listed by name and the digest says UNVERIFIED instead of all-clear (fail closed).

**`hardcoded_normal_cap`**
> Report the cap figures the executor recorded for each run (`budget.daily_cap_usd` / `budget.weekly_cap_usd` in
> `run_state.json`; the runner writes the EFFECTIVE weekly cap there, which for the Stress tier can be below the
> nominal figure after the rollover reserve). Use `config/budgets.json` `budget_tiers` only to validate the recorded
> tier name and to flag a mismatch, never as the number to print. Never assume the Normal tier. If no tier or cap is
> recorded, print 'tier not recorded' rather than a default.

**`misattribution`**
> Group orders by executor (`stream`/`run_classification` in the run state) before judging sizing. Do not attribute a
> sizing anomaly to a guard until the offending order is traced to its own executor. Manual orders are excluded from
> scheduled-live metrics (`docs/run_classification.md`).

**`output_limit_workaround`**
> `scripts/assemble_order_pages.py` does not fetch history; it validates page files you already captured. Fetch with
> `get_equity_orders`, keep each page's raw output as its own file in fetch order, then run from the repo root:
> `python3 -m scripts.assemble_order_pages --account ${ROBINHOOD_AGENTIC_ACCOUNT_NUMBER} --trading-day <YYYY-MM-DD>
> --out <orders_pages file> <page file 1> <page file 2> ...`. Exit 0 means the pagination chain is complete. Never
> transcribe rows by hand. If the tool output is refused or truncated, abort the section with `output_cap_exceeded`
> and say so in the digest.

## 2. Run-log recorder

**`recorder_transcript_unavailable`**: replace the transcript-reader step with:
> Use only cloud-readable sources: the scheduled-task list, broker order history, repo alert files, and
> `scripts/verify_schedule.py` output. If a source is unavailable, write a run-log row whose Component names the
> source and whose text is 'source unavailable', so an empty log can never be mistaken for a clean day. Never leave
> the log silently empty.

## 3. 9:35 verifier

**`verifier_window_mismatch`**: paste the `verifier_window_mismatch` block from `EXTERNAL-PROMPT-FIXES-2026-10-12.md`,
then the items in `EXTERNAL-PROMPT-FIXES-2026-10-15.md` and `-10-16.md` (bounded window, one message per day, history
recomputed from data, late-batch attribution). The repo side (#1748) already reports a late-landing 9:35 run as
`awaiting_evidence` within `scripts/verify_schedule.GRACE_MINUTES`.

## 4. Friday dashboard refresh

**`device_bridge_unreachable`**
> Run the report step only from sources reachable in the cloud. If the Mac device bridge is unavailable, skip that
> step, write 'device bridge unreachable, step skipped' to the dashboard header, and finish the remaining sections.
> Do not fail the whole refresh.

## 5. Alerting (needs your decision before any config edit)

**`approval_required`**: for unattended runs, deliver alerts through the repo alert file, the in-app message center,
and Slack where its webhook env var is set; do not depend on the Gmail draft/send path. At least one unattended
channel must always stay enabled. This changes `config/notifications.json` channel policy, so it is not applied here.

## 6. Executor prompts: `hand_reimplemented` (done in the repo mirrors)

Gate 5 of every `claude/dca-executor-prompts/live-mirror/*.txt` now carries:
> NO HAND-REIMPLEMENTED SIGNALS (2026-10-17, DCA Issues key hand_reimplemented): signal logic comes ONLY from the repo
> generator named in this gate. If it cannot run or its data path fails, finalize as not_submitted_error with the
> failure class and alert on all Gate 9 channels. Never recompute signals, sizes or candidates by hand.

`scripts/check_live_prompt_drift.py` now flags `missing_no_hand_signals_rule` for any live body without it, so after
repasting the mirrors, `python3 -m scripts.check_live_prompt_drift` goes clean for this finding. Until you repaste,
it reports all 7 live bodies as drifted on this finding by design. Tests:
`scripts/tests/test_check_live_prompt_drift.py` (finding flagged, finding absent, every committed mirror carries the
rule and names its generator).

## Verification (non-code, per task)
1. After pasting, run the task once off-hours and confirm it prints the new wording's observable result: UNVERIFIED
   instead of ALL CLEAR on an unreadable source; 'tier not recorded' on a run without a recorded tier; 'source
   unavailable' rows instead of an empty log; 'device bridge unreachable, step skipped' in the header.
2. `python3 -m scripts.check_live_prompt_drift` after repasting the executor prompts.

## Suggested Notion dispositions (operator to apply)

| Key | Suggested | Why |
| --- | --- | --- |
| false_all_clear, hardcoded_normal_cap, misattribution, output_limit_workaround | Monitoring until pasted; Resolved after the digest task shows the new behavior | external prompt, text above |
| recorder_transcript_unavailable | Monitoring until pasted, then Resolved | external prompt |
| device_bridge_unreachable | Monitoring until pasted, then Resolved | external prompt |
| verifier_window_mismatch | Monitoring until the verifier prompt is pasted | repo side #1748 merged; verifier is external |
| hand_reimplemented | Monitoring until the 7 mirrors are repasted, then Resolved | repo mirrors and drift check updated here |
| approval_required | Open: operator decision on `config/notifications.json` | channel policy change |

## Risk and rollback
- Risk: low. Prompt text for read-only reporting tasks; the mirror edit adds one sentence to Gate 5 and cannot change
  a placement decision.
- Rollback: repaste the previous task prompt; revert this commit to drop the mirror sentence and the drift finding.
