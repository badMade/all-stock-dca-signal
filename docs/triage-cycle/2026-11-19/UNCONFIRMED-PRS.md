# Rock 7 (#7): recorded PRs the triage reconciler could not confirm

For each row the operator can correct or confirm the Notion PR cell. Evidence is read-only (`gh pr view`, `git log`,
repo text). Nothing was written to Notion. Verdicts: confirm / correct / unknown.

| Key | Recorded PR | What that PR changed | Verdict |
| --- | --- | --- | --- |
| `robinhood_payload_rate_limited` | https://github.com/badMade/all-stock-dca/pull/2221 | Merged 2026-11-11: "market_data_unavailable: back off before retrying a rate-limited payload chunk"; 92 lines added in `deploy/launchd/robinhood_payload.sh`, `docs/robinhood_payload.md`, `tests/test_robinhood_payload.py` | **confirm** (plausible): it adds back-off for rate-limited chunks, which is this Key's subject. It cites `market_data_unavailable`, not this Key, so the reconciler cannot match it by text. The `market_data_unavailable` row also recorded 2221 until 2026-11-19; 2373 now sits on that row. |
| `bridge_python_alert_channels` | https://github.com/badMade/all-stock-dca/pull/2052 | Merged 2026-10-30: "Bridge Python bootstrap script + evidence-free abort markers"; adds `scripts/bridge_python.sh`, `scripts/record_evidence_free_abort.sh` (writes the channel-2 alert file) and `scripts/abort_markers.py` | **confirm** (plausible): the PR body says it writes the alert file for Gate 1/Gate 2 aborts when the bridge has no usable Python. It does not name the Key. |
| `run_states_unclassifiable` | https://github.com/badMade/all-stock-dca/pull/1514 | Merged 2026-09-23: "Verify every live slot daily; one Gate 9 command to finalize evidence" | **correct** (probable): #1514 adds the finalizer and daily verification, not the `dca_risk` fix. `tests/test_dca_risk.py` (the regression for "run_states_unclassifiable: 3 four days after the 2026-09-18 writer fix") ties the fix to the 2026-09-22 12:30 evidence-wedge change, PR #1508 (commit `0d035991`, "Fix 12:30 evidence wedge ... " at https://github.com/badMade/all-stock-dca/pull/1508). Adjacent: #1480 (`29ba03ca`, per-path reasons for the bucket) and #1519 (`9cf51fff`). Operator to confirm which is the row's fix. |

Note: `selfheal/triage_reconcile.py` already carries a regression for `run_states_unclassifiable` / #1514
(`tests/selfheal/test_triage_reconcile.py:255`), so #1514 was a deliberate earlier operator choice; a correction should
be weighed against that history.
