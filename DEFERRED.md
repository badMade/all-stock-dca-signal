# DEFERRED.md — Rocket Fuel Audit & Refactor, cycle 1

Findings from `ISSUES.md` that were valid but out of scope for this cycle.
Carried forward for a future cycle or a direct decision from the repo owner.

- **`budgets.json` cap-policy contradiction.** `one_order_per_ticker_per_day:
  true` contradicts `same_day_top_up_policy` (`claude/SECOND-EXECUTOR-FINDING.md`).
  Needs a domain decision on intended trading semantics before any code
  changes — not something Codex should resolve unilaterally.

- **Reliability-engine live wiring.** `broker_reconciliation.py`,
  `run_state_machine.py`, `run_ledger.py`, `idempotency.py`,
  `scheduler_lease.py`, `reason_codes.py` are implemented and tested but not
  wired into the live decision path (`docs/reliability_spec.md` §10). Two live
  executors use separate ad hoc `ref_id` schemes instead. `CLAUDE.md` calls
  this kind of consolidation "a decision, not a cleanup" needing its own
  review and acceptance run — too large for an audit cycle.

- **Hardcoded paths / opaque scheduler task IDs.** Per `PRODUCTION_READINESS.md`,
  much of this lives in external scheduler prompt text outside this repo's
  version control, so a repo-only fix would be partial at best.

- **Test-default account number literal.** Committed in `conftest.py` /
  `tests/test_morning_dca.py`. Already mitigated by
  `test_config_invariants.py`'s regex gate against real account-shaped
  literals; lower priority to change the synthetic test value itself.

- **Dead Polygon fetch subsystem.** 5 of 5,502 tickers fetched, blocked on
  missing credentials since 2026-04-26. Flagged for archival/deletion, but
  needs confirmation that nothing still depends on the partial functionality
  before deleting.

- **Commit hygiene.** Non-descriptive commit messages ("ok go", "dodod",
  "gogogo") and bare merge commits with no PR context landing directly on
  `main`. A human process/review-discipline issue, not something a code Rock
  can fix.

- **"Fix test after merge" pattern.** Several commits patch tests/lint after
  the PR that broke them already landed on `main`. No single code fix
  addresses this; Rocks 3 and 4 this cycle harden the specific invariants that
  have already broken this way twice, but the underlying pre-merge gate
  strength is a process question.
