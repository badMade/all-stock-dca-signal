# Rock 2 (#8): the 14 Approved rows the digest derives as "No work" (2026-11-19 night)

Method: the 39 Approved rows were read from the Notion DCA Issues database (operator-authored text, treated as data) and run through the
new `scripts.triage_reconcile_batch` read-only against GitHub (`--collect`). The batch reproduced the digest's numbers
(`approved_groups`: Merged checks green 9, Merged docs only 16, No work 14; 20 Progress notes kept; 2 skips) and returned the new
`unsupported_fixes` block: 12 Keys in `unsupported_approved_fix`, split 5 `unsupported_with_recorded_evidence` and 7 `unsupported_no_recorded_evidence`.
The table has 14 rows = the "No work" `count`. Only Approved rows were exported, so non-approved rows are not audited here.
Verdicts: **has repo evidence** (cite it), **no repo fix possible** (operational or external, say why), **unsupported** (operator: document the fix or reopen).
A recorded commit or PR is the operator's claim; the audit does not verify it.

| Key | Notion Status / Fix state | Recorded in Notion | Repo evidence found | Verdict |
| --- | --- | --- | --- | --- |
| `claude_app_not_running` | Resolved / Repaired | commit dd5ceed (watchdog) | `deploy/launchd/claude_app_watchdog.sh`, `tests/test_claude_app_watchdog.py`; commit dd5ceed subject "Add a weekday Claude app watchdog so live slots cannot miss a day" does not name the Key | has repo evidence (audit false positive: commit never names the Key). Row notes the install on the Mac is operator-confirmed; why the app quit is unexplained |
| `window_insufficient` | Resolved / Repaired | none | `scripts/tests/test_triage_log_summary.py::test_window_insufficient_abort_is_flagged_for_attention`; `scripts/triage_log_summary.py` reports `first_file_delay_min` | has repo evidence (test and reporter exist); the row cites them in its own note but records no PR or commit, so the audit cannot see it |
| `fabricated_empty_candidates` | Resolved / Repaired | commit 9cf51fff | `scripts/finalize_run_evidence.py` + `scripts/tests/test_finalize_run_evidence.py` (commit 9cf51fff "finalizer refuses a zero-order run with no Gate 5 evidence (#1519)") | has repo evidence (audit false positive: commit subject does not name the Key) |
| `logged_as_quiet_tape` | Resolved / Repaired | commit 9cf51fff | same as above (the finalizer refuses a zero-order status dressed as a quiet tape, `scripts/finalize_run_evidence.py` around line 416) | has repo evidence (audit false positive) |
| `robinhood_payload_rate_limited` | Resolved / Repaired | PR 2221, commit b90e6025, branch `badMade/payload-429-backoff` | `deploy/launchd/robinhood_payload.sh` (`DCA_RATE_LIMIT_BACKOFF_SECONDS`, line 77), `tests/test_robinhood_payload.py`; PR 2221 merged 2026-11-11 | has repo evidence; row's own note says the fix is not yet verified under a real 429 |
| `bridge_python_alert_channels` | Resolved / Repaired | PR 2052, branch `badMade/workspace-prep-rocket-fuel-1feedc` | `scripts/bridge_python.sh`, `scripts/record_evidence_free_abort.sh`, `scripts/abort_markers.py` (PR 2052 merged 2026-10-30) | has repo evidence (PR 2052 does not name the Key) |
| `backup_planner_missing` | Monitoring / In review | none | `scripts/verify_perplexity_pair.py` and `scripts/build_backup_planner.py` exist and are tested; the 2026-11-13 failure was a deleted file in the Perplexity project-files repo, outside this repo | no repo fix possible for the deletion itself (operational); the row's open item (backup runs from the operator's Mac Desktop folder) is a deployment question |
| `bridge_disk_low` | Monitoring / New | none | live-mirror prompts carry a `--reclaim` preflight (`claude/dca-executor-prompts/live-mirror/0935ET-935-am-dca-morning-buy.txt` and the other slots); row says repasted 2026-11-05 | has repo evidence for the prompt side (not a code fix); Fix state "New" is consistent with that |
| `slot_delivered_no_evidence` | Resolved / New | none | none by key. The cause in the row is a 2026-11-13 connector outage on the Claude side (bridge, Robinhood, Gmail all failed to connect) | no repo fix possible (external connector outage); Status Resolved with Fix state New is an operator decision |
| `intraday_bars_unavailable` | Monitoring / Repaired | none | `claude/triage-routine/REMAINING-ROWS-EVIDENCE-2026-10-17.md` "Rows closed on 2026-10-12 on the strength of a yfinance fallback": the fallback no longer exists for scheduled runs; suggested disposition Monitoring (fallback removed) | unsupported: Status was moved to Monitoring but Fix state still reads Repaired, which the evidence note contradicts. Operator: set Fix state to New or In review |
| `output_size_limit` | Monitoring / Repaired | none | same note as above | unsupported (same: Fix state stale) |
| `token_limit_exceeded` | Monitoring / Repaired | none | same note as above | unsupported (same: Fix state stale) |
| `no_afternoon_run_states` | Resolved / In review | none | none: `rg` for the Key finds nothing outside this cycle's own docs | unsupported: Resolved with no PR, commit, doc or test. Operator: record the fix or reopen |
| `scheduled_runs_path_missing` | Resolved / In review | none | none by Key (same search). CLAUDE.md documents the legacy `ce16ba6b/scheduled_runs/` root for the final slot, but nothing ties that to this row | unsupported (same) |

## Reading the table
- **5 rows** are audit false positives of a known kind: the operator recorded a real commit or PR whose text never names the Key
  (`claude_app_not_running`, `fabricated_empty_candidates`, `logged_as_quiet_tape`, `bridge_python_alert_channels`, `robinhood_payload_rate_limited`).
  They now appear in the digest under "operator-recorded, not matched by text: confirm", not as "no evidence".
- **5 rows** are real gaps for the operator: `intraday_bars_unavailable`, `output_size_limit`, `token_limit_exceeded` (stale Fix state),
  `no_afternoon_run_states`, `scheduled_runs_path_missing` (no trail at all).
- **4 rows** need no repo fix: `backup_planner_missing`, `bridge_disk_low`, `slot_delivered_no_evidence`, plus `window_insufficient` whose evidence is a test the row
  names in text only.
- `kill_switch_evidence_incomplete` and similar rows with an operator note but a matched merged PR are in "Merged, checks green", not here.

## Rock 1 acceptance (real-data run)
`unsupported_approved_fix` (12): the five above plus `backup_planner_missing`, `intraday_bars_unavailable`, `no_afternoon_run_states`, `output_size_limit`,
`scheduled_runs_path_missing`, `token_limit_exceeded`, `window_insufficient`. `brief_acceptance_not_demonstrated` (4): `false_all_clear`, `hardcoded_normal_cap`,
`misattribution`, `output_limit_workaround` (all "Applied to digest routine (APPLIED-2026-10-27.md); brief acceptance not demonstrated").
`recorded_pr_not_found` (2): `bridge_python_alert_channels`, `robinhood_payload_rate_limited`. `audit_not_run`: none.
