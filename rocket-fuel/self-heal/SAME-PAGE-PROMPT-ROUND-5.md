# Same-Page Prompt — Round 5

Round 5 — the last round. Read rocket-fuel/self-heal/PLAN.md (Revision 6), VTO.md and
DEFERRED.md. Anything still open after this goes to the operator as a tie-break, so please be
decisive: mark each finding RESOLVED, RESOLVED-BY-SCOPE, ACCEPTED-RESIDUAL, or STILL OPEN, and
reserve STILL OPEN for defects you would actually block a build on.

Your new KILL was verified and adopted in full: compute-schedule.yml exists only to rewrite
self-heal.yml's `# AUTO-UPDATED` cron line, holds contents+pull-requests write, and runs
monthly. The deletion now cascades to .github/workflows/compute-schedule.yml,
scripts/compute_schedule.py, .github/self-heal-schedule.yml, SELF_HEAL_SETUP.md,
scripts/tests/test_compute_schedule.py and the docs/repo_map.md row.

Your CI-cost finding was also verified and was worse than my plan assumed: a pull_request fans
out to ci.yml plus antigravity-pr-review.yml (10m) plus antigravity-pr-review-post.yml (5m and
15m), a Gemini-backed reviewer Revision 5 did not count at all. ci_worst_case_minutes is raised
from 25 to 150 and is now derived from the sum of every timeout-minutes reachable from one PR.

Changes made this round, by your finding:

- 1: the coordinator now runs from an installed wheel at an absolute path in a dedicated
  venv, never the mutable checkout. The same-UID `gh` credential is recorded as
  ACCEPTED-RESIDUAL in DEFERRED.md.
- 4 / Q2: setitimer is replaced by an out-of-process supervisor that Popens the worker in a
  new session, kills the process group on deadline, WAITS for termination, and only then
  releases the lock. TTL lock theft is removed — an expired lock cannot coexist with a live
  worker.
- 5: "complete" is withdrawn in favour of "as-captured up to a declared cap"; archive names
  are run-scoped and sequence-prefixed so they neither collide nor require a key; the
  artifact root is accepted if outside the worktree OR git check-ignore'd.
- 6: 150-minute worst case; settlement sums every run for the head SHA.
- 7: cumulative forward skew is capped per epoch and the budget period is a monotonic counter
  in state.json, not the wall clock's calendar month; the --now guard is an explicit
  SystemExit, not an assert stripped by python -O.
- 10: the regex now covers the zero-separator camel/snake forms; unset-variable former values
  are ACCEPTED-RESIDUAL.
- 14: cursors are composite (run_id, run_attempt) and pagination is oldest-first.
- 16: composite (run_id, run_attempt) + head SHA + PR number provenance, never workflow id;
  head AND merge commit both journaled; unclassified incidents held provenance_pending;
  dropped incidents are still archived privately.
- 18: incidents whose originating ref is not `main` are report-only regardless of adapter,
  closing the contributor-PR exhaustion path from your Q3.
- 19: safe upper bounds restored alongside the missing-field checks.
- 20: an exact plist contract plus tests/selfheal/accept_launchd.sh with no placeholder.
- 21: PR intent is written to state.json BEFORE branch creation and push, with
  git ls-remote + gh pr list recovery covering the orphaned-branch case.
- 22: the dry-run proof now asserts a byte-identical sha256 manifest before and after, and
  Rock 7 removes the existing tests that assert the deleted workflow's presence.
- docs: VTO's external-binary list, the "complete archive" wording, and the gh residual are
  all corrected; DEFERRED.md gains an explicit Accepted-residual-risks section.

[SUITE] is green: the full suite passes (exit 0) and tests/test_config_invariants.py passes.

Answer the four open questions at the end of Revision 6 directly, and say plainly whether the
remaining open items are things you would block a build on or refinements you would take as
follow-up work.

End with exactly one line, and make it the last line: `VERDICT: SAME PAGE` if you have zero
open FIX or KILL items, otherwise `VERDICT: BLOCKED — N open finding(s)`.
