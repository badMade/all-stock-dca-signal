# VTO — Automated self-healing with a diagnostic memory

Vision/Traction document for the `rocket-fuel/self-heal` cycle (Kickoff, 2026-09-14).
Namespaced under `rocket-fuel/self-heal/` so the completed `rocket-fuel/hosted-console`
cycle's root `PLAN.md` / `VTO.md` / `SAME-PAGE-LOG.md` / `DEFERRED.md` stay intact as the
historical record (operator decision, interview Q1).

## What

A closed loop with four parts:

1. **Collect.** Every diagnostic this system already emits — Wondrous Octopus shard crash
   logs, `cron_tracking/` run artifacts, console job output, failed CI runs, and the
   `scripts/healthcheck.py` gate's own output — swept into one normalized, redacted
   incident store on a fixed daily cadence.
2. **Remember.** Each incident is reduced to a stable **fingerprint** and written to an
   append-only ledger that knows what has been seen before, how many times it has been
   attacked, and how those attempts ended.
3. **Heal.** Only genuinely *new* (or still-unrepaired) fingerprints are dispatched to the
   repair pipeline. A repair opens a pull request; it never merges itself.
4. **Govern.** One fail-closed gate stands in front of the dispatch step and enforces every
   limit the operator asked for: one run per day, a spend ceiling, a per-failure attempt
   cap, and a circuit breaker.

## Why now

The repair pipeline already exists (`scripts/self_heal.py`, `scripts/healthcheck.py`,
`.github/workflows/self-heal.yml`) but it is **blind and forgetful**:

- **Blind.** `.gitignore` excludes `*.log` and `cron_tracking/`, so the two richest crash
  sources on this machine are invisible to a hosted runner. Today's live example: shard 1's
  `stdout.log` ends in `RUNTIME FAILURE (NotImplementedError): aten::_nested_tensor_from_mask_left_aligned
  is not currently implemented for the MPS device`. Nothing in the self-heal path can see
  that line, so nothing will ever act on it.
- **Forgetful.** The workflow reacts to a CI conclusion. It has no memory of which failure
  it already tried to fix, so the same failure can be re-attacked indefinitely and a fixed
  one leaves no record that it was fixed.
- **Ungoverned.** There is a weekly cron and a reactive `workflow_run` trigger, and no
  ceiling of any kind on how often the loop may fire, how much it may spend, or how many
  times it may retry the same thing.

## Who

A single operator (the repository owner), on one Mac that holds the artifact tree, with
GitHub Actions as the repair sandbox.

## Success criteria for this cycle

- A crash written to any configured source is, within one day, present in the incident
  store with a stable fingerprint and a redacted excerpt.
- Re-running the collector over unchanged sources produces **zero** new incidents.
- The self-heal dispatch cannot fire twice in one UTC day, and raising that ceiling later
  is a config edit, not a code change.
- A single failure fingerprint can consume at most a bounded, configured number of repair
  attempts in its entire lifetime, after which it is quarantined and surfaced to a human.
- Every refusal to dispatch names a machine-readable reason code and is written to the
  ledger.
- No self-heal artifact committed to this repository ever contains the brokerage account
  number, an absolute home path, or any value from a `SECRET`/`TOKEN`/`PASSWORD`/`API_KEY`
  named environment variable.

## Non-negotiables

- **Standard library only** in everything that runs at core-DCA runtime. Python ≥3.11.
- **The account-number invariant holds.** `tests/test_config_invariants.py` greps for
  account-shaped literals; sanitized digests are committed to a public repository, so
  redaction is a correctness requirement, not a nicety.
- **Fail closed.** If the governor cannot prove a limit is satisfied, it refuses to
  dispatch. An unreadable ledger, an unparseable config, or a clock it cannot trust are all
  refusals, never permissions.
- **Nothing merges itself.** Repairs land as labeled pull requests for human review
  (interview Q4). This repository drives live trading.
- **Self-heal must never react to its own output.** Incidents originating from self-heal's
  own artifacts, branches, or pull requests are dropped before the ledger sees them.
- **The existing trust boundary in `self-heal.yml` is preserved.** No new
  `workflow_dispatch` path into a job holding `contents: write`.
- **No new runtime third-party dependency.** `gh` and `git` are the only external binaries,
  and both are already assumed by the existing workflow.

## Out of scope this cycle

Recorded in `DEFERRED.md`. Headlines: auto-merge of any repair, agent-driven repair of live
trading logic, repairing the sibling research subprojects' own suites, and multi-machine
coordination.
