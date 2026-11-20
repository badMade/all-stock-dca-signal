# ISSUES (Clarity Break from the 2026-11-19 triage digest)

Ranked by impact. Source: the `dca-triage-digest` local run of 2026-11-19 18:20 ET (session
`local_34322382`), read second-hand. Nothing here is verified against Notion or the run artifacts yet;
the repo checks are noted. The 2026-11-02 cycle (`docs/triage-cycle/`, root `ISSUES.md`) is left untouched.

1. [impact: high] 13:30 intraday slot aborted with `ABORT_GATE6_HISTORY_NOT_PERSISTABLE` (0 orders, `output_cap_exceeded`, "day order history not persistable from session") although Gate 5 produced 66 buys and 0 provider errors. The `output_cap_exceeded` row is Approved, Monitoring, PR 2345 merged, so the fix did not hold or does not cover this path. A whole slot's buys were lost.
2. [impact: high] `schedule_watchdog` reported ok with no failing slots on the day of the 13:30 abort. Same false-all-clear shape as 2026-10-30 (`verify_schedule` ok while 9:35 and 3:30 aborted). Operators relying on the watchdog get no signal; only `triage_log_summary` caught it.
3. [impact: high] 14:30 slot left 63 tickers unplaced after the normal-tier daily cap ran out, and 15:30 ran at the stress tier with a $37.66 buying-power ceiling. Likely a knock-on of #1 (13:30 bought nothing, then later slots hit caps) or a cap-seeding mismatch between the three entry points (`docs/live_wiring.md`). Unclear which.
4. [impact: med] The digest finished PARTIAL: its own COMPLETE definition (STEPs 1-6 by hand per Key) conflicts with the sanctioned `triage_reconcile_batch` path, and `verify_schedule --orders-json` was skipped, so no `order_attribution`. A routine that is correct but can never say COMPLETE trains the operator to ignore the header.
5. [impact: med] Executor slot tasks (9:35 to 15:30) are absent from `list_scheduled_tasks`, so enabled, lastRunAt and nextRunAt read UNKNOWN every run. The prompt treats UNKNOWN as a finding, so this is a permanent false positive or a real blind spot.
6. [impact: med] `market_data_unavailable`: PR 2373 (sliding worker pool, open, checks green) is the live fix; the row's PR number was stale at 2221 until today. Needs merge and a post-merge check; not yet evidence of a fix.
7. [impact: med] Three rows record a PR that neither matches nor cites the Key (`robinhood_payload_rate_limited` #2221, `bridge_python_alert_channels` #2052, `run_states_unclassifiable` #1514). They are skipped every run until the operator corrects them.
8. [impact: med] 14 Approved rows sit at Progress "No work" and 16 at "Merged, docs only" with no code evidence (carry-over of 2026-11-02 issue #1; `no_afternoon_run_states` still has no repo trail by that digest). A fix may be marked done with nothing behind it.
9. [impact: low] 20 rows carry operator-note Progress text, so derived labels are never written; the row query does not return `last_edited_time`, forcing an extra fetch before every write.
10. [impact: low] The digest said the 2026-11-19 recurrence is not recorded on the `output_cap_exceeded` row; propose-only rules forbid it writing there, so the operator must add it.

## Updates found while planning (2026-11-19 evening)
- #2: the watchdog already lists the 13:30 slot in `error_slots` and exits 1. Only its top-level `ok` stayed true (copied from
  `verify_day`), and the digest read `ok`. See PLAN.md Rock 2.
- #1: earlier slots saved valid same-day orders_pages envelopes before 13:30, yet the 13:30 run recorded no `day_history_mode`.
  `triage_log_summary` now reports `baseline_unused=true` for it (PLAN.md Rock 1). The cause (stale live routine prompt vs agent
  skipping the rule) is still undetermined.
- #3: see CAP-CHECK.md: the 14:30 exhaustion came from the morning slots, not from the 13:30 miss.
- #6: PR 2373 was MERGED at 23:35Z, after the digest ran. See MARKET-DATA-2373-CHECK.md.
