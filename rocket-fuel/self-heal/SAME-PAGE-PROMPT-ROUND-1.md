You are the Integrator reviewing a plan written by the Visionary. Read
rocket-fuel/self-heal/PLAN.md and rocket-fuel/self-heal/VTO.md end to end
(rocket-fuel/self-heal/DEFERRED.md lists what is intentionally out of scope). Ground your
review in the actual repository: CLAUDE.md states the hard invariants, and the plan cites
specific files (scripts/self_heal.py, scripts/healthcheck.py, .github/workflows/self-heal.yml,
.gitignore, scripts/file_lock.py, scripts/reason_codes.py, scripts/model_usage_log.py,
dca_console_jobs.py, pyproject.toml) — open them and check whether the plan's claims about
them are true. A factually wrong premise is a BLOCKER.

The operator's requirements, verbatim, are: every self-healing run stores crash logs and
debug reports durably; something detects NEW failures and feeds them to the self-healing
automation; self-heal runs at most once per day (raisable later by config); there are
defined spend limits; and there is explicit, reliable prevention of self-healing loops that
never stop.

You are read-only — do not propose file edits, propose plan changes instead. Pay particular
attention to:
  - whether the four loop-prevention mechanisms actually compose into a guarantee, or
    whether some interleaving still permits unbounded work;
  - head-of-line blocking, clock manipulation, ledger corruption, and what happens when the
    collector itself crashes;
  - whether the redaction rules are sufficient given that sanitized digests are committed to
    a PUBLIC repository and the account-number invariant is enforced by a grep;
  - the trust boundary on .github/workflows/self-heal.yml (the plan's own Finding A);
  - scope creep: seven Rocks is the cap, and anything that cannot be proven by its stated
    proof command should be cut.

For every real problem you find, emit exactly one line in this format:

[SEVERITY] short-title — one sentence on why it matters

Severity is one of BLOCKER, MAJOR, MINOR, NIT. After the findings, for each one state a
verdict: FIX (plan must change before anyone builds), DEFER (valid, but out of scope for
this cycle), KILL (this scope should be cut entirely), or ACCEPT (flagged, but on reflection
it's fine — explain why). End your reply with exactly one line, and make it the last line:
`VERDICT: SAME PAGE` if you have zero open FIX or KILL items, otherwise
`VERDICT: BLOCKED — N open finding(s)`.
