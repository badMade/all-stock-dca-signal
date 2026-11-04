# PLAN: triage-digest hardening (issues #1-#7 of ISSUES.md; #8 deferred)

Touches only `selfheal/triage_reconcile.py`, `tests/selfheal/test_triage_reconcile.py`, `docs/triage_routine.md`,
`claude/triage-routine/prompt.txt`, the new `claude/triage-routine/briefs/window_insufficient.md`, the new
`docs/triage-cycle/MARKET-DATA-1530-CHECK.md`, and (for #5) a classification test under `scripts/tests/`.
Existing behaviour of `match_git_evidence` for non-shallow clones must not change.

## Rock 1 (#1, #6, #7): unsupported-fix audit
Add `audit_unsupported_fixes(rows, evidence)` to `selfheal/triage_reconcile.py`: returns Keys whose Approval is "Approved"
and Fix state is Repaired or In review but whose matched evidence has no PR, commit or issue. Also returns Keys stuck
in Monitoring with "brief acceptance not demonstrated", and Keys whose recorded PR number was not found.
Non-goals: writing to Notion; changing any Progress label.
Proof: `.venv/bin/python -m pytest tests/selfheal/test_triage_reconcile.py -q && ruff check selfheal tests/selfheal`

## Rock 2 (#3): blind-run preflight
Add `logs_reachable()` to `selfheal/triage_reconcile.py`; it resolves the root via `dca_paths.cron_tracking_root()` (so an unset
`DCA_CRON_TRACKING_ROOT` falling back to a readable `<checkout>/cron_tracking` is NOT blind) and returns a reason string only
when that resolved directory is missing, unreadable, or holds no stream subdirectory. Update `claude/triage-routine/prompt.txt` and `docs/triage_routine.md` so the digest opens with
"BLIND RUN" and refuses to say any task passed.
Non-goals: changing where logs live.
Proof: same pytest file plus `ruff check .`

## Rock 3 (#4): shallow-clone tolerance
`match_git_evidence` gains a keyword argument `shallow: bool = False` (the caller supplies `git rev-parse --is-shallow-repository`)
and a `commit_present: Mapping[str, bool]` argument, built by the caller with `git cat-file -e <sha>^{commit}` per recorded SHA (never from the 50-commit list, whose absence proves nothing). The existing result record gains a `commit_status` value `"unverifiable_shallow"` only when `commit_present[sha]` is false AND `shallow` is true; absent and non-shallow keeps today's behaviour exactly (no commit match, no error),
instead of silently dropping them or raising. Non-shallow results stay identical (assert with a pre/post golden test).
Proof: same pytest file.

## Rock 4 (#5): window_insufficient
Read-only look first: find where `ABORT_GATE7_WINDOW_INSUFFICIENT` / `error_class: window_insufficient` is classified and add
a Notion-row proposal template under `claude/triage-routine/briefs/` plus a classification test. No behaviour change in live gates.
Proof: `.venv/bin/python -m pytest scripts/tests/test_triage_log_summary.py tests/selfheal -q`

## Rock 5 (#2): 2026-11-02 15:30 check
Operational, not code: run `python scripts/verify_schedule.py` for 2026-11-02 against the real cron tree and record the result
in `docs/triage-cycle/MARKET-DATA-1530-CHECK.md`. If the tree is unreachable, record that and stop.
Proof: the file exists and quotes the command output.

## Deferred (DEFERRED.md)
#8 operator-note labelling policy: needs an operator decision first.

## Whole-cycle proof
`.venv/bin/python -m pytest tests/selfheal scripts/tests -q && ruff check .`
