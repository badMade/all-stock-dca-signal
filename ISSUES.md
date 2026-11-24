# ISSUES (Clarity Break from the 2026-11-02 triage digest)

Ranked by impact. Digest claims are unverified against Notion; repo checks are noted.

1. [impact: high] `intraday_bars_unavailable`, `output_size_limit`, `token_limit_exceeded`, `no_afternoon_run_states` are Approved at "No work" with Fix state Repaired/In review and no PR or commit. Only the first three appear in the repo (`claude/triage-routine/REMAINING-ROWS-EVIDENCE-2026-10-17.md`). `no_afternoon_run_states` appears nowhere, so its fix has no code or doc trail. A fix is marked done with no evidence.
2. [impact: high] `market_data_unavailable`: PR #2064 merged, but the first real test is the 15:30 slot of 2026-11-02 and it is unverified. Needs a look at that day's run artifacts.
3. [impact: high] The triage routine ran with `DCA_CRON_TRACKING_ROOT` unset, so there was no `verify_schedule`, `schedule_watchdog` or `alert_digest`, and every task status read UNKNOWN. Cause: triage-digest container setup. See `docs/triage_routine.md`.
4. [impact: med] `selfheal/triage_reconcile.py` (`match_git_evidence`) could not run in the container, so evidence matching was done by hand. Likely causes are the shallow clone and no `git` access. This checkout is not shallow.
5. [impact: med] `window_insufficient` has empty Status and Severity, Approval = Proposed, no work. It appears only in `scripts/tests/test_triage_log_summary.py`, so no code handles it.
6. [impact: med] 5 rows noted "brief acceptance not demonstrated" are stuck in Monitoring: `false_all_clear`, `hardcoded_normal_cap`, `misattribution`, `output_limit_workaround`, `recorder_transcript_unavailable`.
7. [impact: low] `run_states_unclassifiable`: recorded PR #1514 not found by search, no Approval value. `max_orders_per_day_unenforced`, `stress_tier_undefined` (#1745) and `output_cap_exceeded` (#1520) not re-fetched.
8. [impact: low] Policy gap: 10 rows have operator-written Progress notes, and the derived "Merged, docs only" label is never written over them. Decide whether `triage_reconcile` should append instead.

Note: PLAN.md and VTO.md already exist in this checkout and were not created by this run. I will ask before reusing or overwriting them.

## 2026-11-23 (11:30 ET intraday slot): order history cannot be saved as script-built pages [impact: high, NOT a blocker]
- 11:30 slot aborted at Gate 6.5 (`output_cap_exceeded`) because `get_equity_orders` returned inline only and the prompt forbids hand transcription, so `assemble_order_pages` had no file to read. Cause unknown.
- Operator direction (2026-11-23): this must never block a run. Treat as email plus triage; fix later. Proposed fix: the executor should save tool output to the bridge shell directly, or the prompt should downgrade this to log-and-continue like the 2026-10-22 history-completeness rule.
- Evidence: cron_tracking/intraday_dca/20261123_1130ET, alert file 2026-11-23-1130-gate6_5.txt.
