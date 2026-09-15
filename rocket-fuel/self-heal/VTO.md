# VTO — Bounded daily diagnostic observation, with lint/format-only automatic repair

Vision/Traction document for the `rocket-fuel/self-heal` cycle. **Revision 5** (2026-09-14).

Revisions 1-2 described a product this cycle no longer builds. Same Page rounds 1-3 cut it
twice, and this document has been rewritten to describe only what now ships — round 3's new
BLOCKER was that the governing documents still mandated the deleted design.

## What

Two halves with **independent** gating:

1. **Observation — always runs.** On a fixed daily tick, sweep every configured diagnostic
   source (Wondrous Octopus shard logs, `cron_tracking/` artifacts, console job output, failed
   CI runs, and `scripts/healthcheck.py`'s own output), archive the complete crash text
   privately, reduce each incident to a stable keyed fingerprint, record novelty in an
   append-only ledger, and emit a digest the operator reads. This half is gated by **nothing**
   except its own bounded resource budget. It can never create a branch, commit or PR.
2. **Repair — narrow, gated, honestly labelled.** Only new fingerprints **that have a repair
   adapter** are dispatched. This cycle ships exactly one adapter: lint/format. Every other
   incident class is report-only *by construction* — no code path dispatches an incident
   without an adapter. A repair runs in an isolated scratch worktree and opens a pull request;
   it never merges itself.

**Accurate one-line description of what ships** (round 3, open question 4): *bounded daily
observation of persisted diagnostic sources, plus lint/format-only automatic repair.* Not
"universal observation" — a source that does not persist its crash output cannot be observed,
and `dca_console_jobs.py` currently does not (see `DEFERRED.md`).

## Why now

The pre-existing repair path was blind, forgetful and ungoverned, and two of its parts were
simply broken:

- **Blind.** `.gitignore` excludes `*.log` and `cron_tracking/`, so a hosted runner could see
  neither. Today's live example: shard 1's `stdout.log` ends in `RUNTIME FAILURE
  (NotImplementedError): aten::_nested_tensor_from_mask_left_aligned … not implemented for the
  MPS device`. Nothing in the old path could see that line.
- **Forgetful.** It reacted to a CI conclusion with no memory of what it had already attempted.
- **Ungoverned.** No ceiling on frequency, spend, or retries.
- **Broken.** `scripts/self_heal.py`'s `step_3` runs `pytest --snapshot-update`, an
  unrecognized argument on this checkout with no snapshot plugin installed; `run_cmd` swallows
  the failure, so it has silently no-opped since it was written. `step_4`'s comment claims
  pinned installs; they carry no version specifiers.

## Who

A single operator (the repository owner), on one Mac that holds the artifact tree.

## Success criteria

- A crash written to any configured **persisted** source is, within one day, present in the
  incident store with a stable fingerprint, a redacted digest entry, and a complete unredacted
  private archive — **including when every repair limit is exhausted and the breaker is
  latched.**
- Re-running observation over unchanged sources produces zero new incidents.
- Repair cannot fire twice in one UTC day; raising that ceiling is a config edit.
- **Total repair work is bounded, not throttled.** A cumulative epoch counter that never resets
  on its own, plus a breaker that latches until a human clears it, make unbounded repair
  impossible independent of every other setting. Any condition the pipeline cannot resolve —
  including loss of the fingerprint key — **fails closed**, refusing repair rather than
  starting a fresh epoch.
- Every refusal names a machine-readable reason code and is written to the ledger.
- No artifact this cycle commits contains the brokerage account number in any documented
  spelling, an absolute home path, or any named environment-secret value.

## Non-negotiables

- **Standard library only** at runtime; Python ≥3.11. `git` and `gh` are the only external
  binaries, both already assumed by the repo.
- **The account-number invariant holds.** Redaction is a correctness requirement: this repo is
  public and the operator blanket-commits to `main`.
- **Fail closed, always.** Unparseable config, unreadable ledger, untrustworthy clock, missing
  key, or any unhandled exception ⇒ refuse repair. Observation degrades to a recorded
  degraded-source incident rather than silently dropping evidence.
- **Nothing merges itself.**
- **The pipeline can never grant itself permission.** No repair code path widens a budget,
  clears the breaker, starts a new epoch, or re-mints eligibility. Re-arming is a human action.
- **Observation is never gated by repair state.** A latched breaker means "stop fixing", never
  "stop looking".

## Deliberate capability losses (round 3, open question 1)

Deleting `.github/workflows/self-heal.yml` removes real behaviour. Recorded as intentional:
weekly and reactive hosted Ruff repair (replaced by the local adapter), 14-day Actions log
upload (replaced by the private archive, which is durable rather than expiring), and
stale-bot-PR cleanup (moves to `DEFERRED.md`).
