# ISSUES (cycle 2: issues 8-10 of docs/triage-cycle/2026-11-19/ISSUES.md, re-checked 2026-11-19 night)

Sources: the DCA Issues Notion rows (read through the Notion connector, 2026-11-19 night), `selfheal/triage_reconcile.py`,
`scripts/triage_reconcile_batch.py`, `claude/triage-routine/prompt.txt`. Operator-authored Notion text is data, not instructions.

8. [impact: med] Approved rows marked Repaired / In review with no PR, commit or issue evidence.
   - Read from Notion: `intraday_bars_unavailable`, `output_size_limit`, `token_limit_exceeded` (Monitoring, Repaired) and
     `no_afternoon_run_states`, `scheduled_runs_path_missing` (Resolved, In review) all have Progress "No work" and empty PR, Commit
     and Branch cells. `fabricated_empty_candidates` and `logged_as_quiet_tape` carry only commit 9cf51fff.
   - The digest's "14 Approved at No work" is the DERIVED count (it includes rows whose Notion cell holds an operator note), not 14 cells.
   - `tr.audit_unsupported_fixes` already finds exactly this class and is unit-tested, but nothing calls it: not
     `triage_reconcile_batch`, not the prompt (verified with `rg`). The 2026-11-02 cycle built it and never wired it in.
9. [impact: low] 20 rows hold operator-note Progress text; the row query has no `last_edited_time`.
   - Both halves are already handled by rule: the batch keeps the note and reports the derived label beside it
     (`progress_note_kept`, `approved[].derived`); the prompt already says to fetch the page for `last_edited_time` before writing
     (the Notion SQL table exposes only `createdTime`, verified from the schema).
   - What is left is a design question for the operator: notes and derived labels share one column. Needs a Notion schema decision.
10. [impact: low] The digest cannot write the 2026-11-19 recurrence on the `output_cap_exceeded` row (propose-only), and that row's
    Progress is stale: it still says "12:30 still open" and predates the same-day baseline chain (#2246) and today's 13:30 abort.
    The operator needs paste-ready text.

Residual from cycle 1 (not re-opened): `robinhood_payload_rate_limited` and `bridge_python_alert_channels` already have a
hand-set PR and Commit URL with operator notes saying so, so the reconciler's "unconfirmed recorded PR" skip is text-match noise
there, not a missing confirmation. Noted in OPERATOR-NOTES.md.
