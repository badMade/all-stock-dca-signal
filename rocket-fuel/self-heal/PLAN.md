# PLAN — Diagnostic observation, with narrow automatic repair

Build plan for the `rocket-fuel/self-heal` cycle. Vision in `VTO.md`, deferrals in
`DEFERRED.md`, full reasoning record in `SAME-PAGE-LOG.md`.

**Revision 4.** Round 2 returned `BLOCKED — 22 open`; two findings resolved, one new BLOCKER.
Revision 4 is a **scope cut**, taken by the operator on 2026-09-14, not another elaboration.
Revisions 2 and 3 are preserved at `PLAN-rev2.md.bak` and `PLAN-rev3.md.bak`.

## What changed, and why

Round 2's new BLOCKER — *repair admission suppresses failure detection* — showed that
Revisions 2 and 3 had the operator's priorities inverted. The operator's first requirement was
**"whenever the self-healing automation runs. Period. I want crash logs, debug reports, all of
that. Saved."** Both revisions gated *collection* behind the same governor that gates *repair*,
so the system would stop recording crashes at exactly the moment it began failing — when
evidence matters most.

Revision 4 therefore splits the product into two halves with **independent** gating:

| Half | Gated by | Runs when | May open a PR |
|---|---|---|---|
| **Observation** — collect, redact, fingerprint, store, report | Nothing but its own resource caps | **Always**, every scheduled tick, even with the breaker latched and every budget spent | No |
| **Repair** — dispatch an adapter, verify, publish | The full admission guard | Only when every limit permits | Yes |

A latched breaker now means *"stop fixing things"*, never *"stop looking"*. That is the
correct reading of the operator's requirement and it dissolves the new BLOCKER.

The second change is a deletion. Round 2 kept findings 1, 13 and 18 open for one reason: Rock
7 retained the hosted healer as a **second dispatcher** that bypasses the adapter registry.
Revision 4 deletes it (see "Deletions" below) rather than defending it.

The third change is honesty about what gets fixed automatically. Per Codex's answer to open
question 4, one adapter is an acceptable MVP **only if the product says so**. It now does:
this cycle ships *universal observation* and *lint/format-only repair*.

## Deletions (finding 1, 13, 18 — resolved by removal)

- **`.github/workflows/self-heal.yml` is deleted in full.** It is the second dispatcher, it
  carries `contents: write` on a reactive trigger, it contains the `workflow_dispatch:` line
  that contradicts its own header comment, and it invokes a healer that cannot accept an
  incident. It is also already ineffective: it can see neither `cron_tracking/` nor any
  `*.log`, both gitignored. Deleting it resolves Correction 1 by removal.
- **`scripts/self_heal.py` is reduced to the `lint_format` adapter's implementation.** Its two
  steps that work (`ruff check --fix`, `ruff format`) move into `selfheal/adapters/lint_format.py`
  with errors propagated and timeouts enforced. `step_3`'s `pytest --snapshot-update` is
  **deleted** — it is an unrecognized argument on this checkout and has silently no-opped since
  it was written (finding 12). The remaining steps (`pip install -e .`, unpinned stub installs,
  `pip-compile`) are not repairs of any detected incident and are dropped.
- The existing `CI` workflow is untouched and becomes the sole verifier of repair PRs.

## Package layout

```
selfheal/
  __init__.py      __main__.py      cli.py
  config.py        # strict fail-closed runtime load (finding 19)
  reasons.py       # reason-code constants, house pattern of scripts/reason_codes.py
  redaction.py     # recursive whole-object sanitization (finding 10)
  fingerprint.py   # keyed, sanitized-input-only identity (finding 11)
  observe.py       # THE UNGATED HALF: collect -> redact -> ledger -> report
  sources/         # bounded collectors, one per diagnostic source
  ledger.py        # one authoritative sequenced store (findings 2, 17, 21)
  admission.py     # THE GATE, repair only (findings 3, 4, 6, 7)
  adapters/        # incident -> repair contract (finding 13); lint_format only
  repair.py        # reserve -> repair -> verify -> publish -> settle
  publication.py   # nothing unredacted or unverified leaves (findings 10, 11)
tests/selfheal/
```

## Half 1 — Observation (ungated)

`python -m selfheal observe`. Runs on every launchd tick regardless of admission state.
Read-only with respect to the repository; writes only to its own artifact tree; never creates
a branch, commit or PR. Its own resource caps — per-source monotonic deadline, byte cap,
incident cap, process-group termination — are the only limits that apply, and exceeding one
records a *degraded source* incident rather than dropping evidence silently (finding 4).

**Durable evidence is written before anything fallible (finding 5).** The very first action,
before config load, before the ledger, before any lock, is to create
`$DCA_CRON_TRACKING_ROOT/self_heal/runs/<run_id>/envelope.json` and open `_emergency.log`
`O_APPEND`. An envelope with no final status is what the next run's reconciler converts into a
durable failure record. Round 2 correctly noted Revision 3 created these *after* fallible
config/ledger/lock work; Revision 4 moves them to the top.

Sources: `octopus_shard`, `cron_artifacts`, `console_jobs`, `ci_runs`, `healthcheck`.
Round-2 requirements carried in: an ordered high-watermark with a resumable gap policy keyed by
run attempt for `ci_runs` (finding 14), and — since no Rock may assume behaviour it does not
build — `console_jobs` is **collected as-is**, with `dca_console_jobs.py`'s lack of incremental
persistence recorded in `DEFERRED.md` rather than assumed away (finding 15).

Output: a redacted incident per fingerprint, an updated ledger, and a daily digest the
operator reads. **This half alone satisfies operator requirement #1 in full.**

## Half 2 — Repair (gated)

`python -m selfheal repair`. Runs only after `admission.evaluate()` admits.

**Termination rests on two mechanisms, and neither is a throttle (finding 3).**

- **A latched breaker.** Three consecutive unsuccessful repairs latch it `open` with **no
  automatic cooldown**. Only `--reset-breaker`, run by a human, clears it. No repair code path
  writes the field. Round 2 noted a failure-only latch can be dodged by alternating outcomes;
  Revision 4 therefore latches on *any* three unsuccessful repairs within an epoch, not on
  three *consecutive* ones.
- **A cumulative epoch bound, not a rolling window.** Round 2 was right that `7 per rolling 30
  days` permits seven more forever. Revision 4 uses an **epoch**: a monotonic counter of
  admitted-or-abandoned repairs that never resets on its own. At `max_repairs_per_epoch`
  (default **10**) the system stops repairing until a human runs `--new-epoch`. Per Codex's
  answer to question 3, **refusals do not count** — they perform no repair — while an admitted
  repair that crashes or is abandoned does count, so a crash loop cannot mine the counter.

Observation continues throughout both states. A latched breaker or exhausted epoch produces a
loud digest entry, not silence.

Other admission gates (kill switch, single-run lock, daily rate limit default **1**, clock
sanity, Actions-minute budget, per-fingerprint attempts default **3**, adapter-exists,
wall-clock remaining, novelty) are fairness and cost controls, and are explicitly *not* the
termination argument.

**Reservation (findings 2, 6).** A single authoritative `reservations.json` guarded by
`cross_process_lock` — not one file per run, which Round 2 correctly noted is no mutex at all
— holds the shared budget. Worst-case cost is debited before any repair work; `settle()`
replaces it with actuals. Actions minutes are settled **asynchronously** by the next run's
reconciler reading the completed CI run, since they are unknowable at PR-creation time
(finding 6). An unsettled reservation older than `max_wall_clock_seconds` is charged at worst
case.

**The adapter contract (finding 13).** `matches()` / `repair()` / `verify()`, where `verify()`
proves *that incident* is gone, not merely that the suite is green. Exactly one adapter ships:
`lint_format`. Every other incident class is **report-only by construction** — there is no code
path that dispatches an incident without an adapter, so the shard-1 MPS `NotImplementedError`
is reported to the operator and can never consume a slot or latch the breaker (finding 18).

## Publication

`publication.release()`: recursive whole-object sanitization; a staged-bytes scan of the exact
content to be published using the repository's own `_ACCOUNT_SHAPED` **and** `_LABELED_ACCOUNT`
patterns **plus** absolute-home-path and every named environment-secret value (finding 10,
widened per round 2); write-time containment that refuses to write unredacted text to any path
`git check-ignore` does not confirm as ignored. A hit aborts publication and latches the
breaker.

Fingerprints are HMAC-keyed over fully-sanitized canonical input (finding 11). Round 2's
follow-ups are adopted: the key has an exact gitignored filename (`.selfheal_fingerprint_key`,
added to `.gitignore` beside `.lease_fingerprint_key`), domain separation in the HMAC message,
atomic owner-only (`0600`, `O_EXCL`) creation, and a documented loss policy — a lost key starts
a new epoch rather than silently reminting identities.

## Rocks

Seven. Every proof ends with **[SUITE]** =
`ruff check . && .venv/bin/python -m pytest -q && python run_dca.py --demo --report --now 2026-08-03T15:45 --artifacts-dir /tmp/dca-demo`.
**[SUITE]** is currently green as of Revision 4 (the two `test_config_invariants.py` failures
round 2 found were fixed by gitignoring this cycle's Codex transcripts and the
`analysis/real-octopus-run/` replay output; 205 tests pass).

**Rock 1 — `config.py`, `reasons.py`, `config/self_heal.json`, schema.** Strict runtime
validation: duplicate keys, non-object JSON, bools-as-ints, non-finite, negative, **zero**,
**unknown keys**, **safe upper bounds**, and **cross-field constraints** (finding 19, widened
per round 2 — a zero-length epoch must not be able to disable the backstop). No standalone
6–12 digit literal in any tracked artifact.
*Proof:* `pytest tests/selfheal/test_config.py tests/test_config_invariants.py -q`, one
rejection test per malformed case; then **[SUITE]**.

**Rock 2 — `redaction.py`, `fingerprint.py`.** Recursive sanitization over the whole object
graph; HMAC-keyed fingerprints over sanitized input only; keyfile creation `O_EXCL` + `0600`.
*Proof:* `pytest tests/selfheal/test_redaction.py tests/selfheal/test_fingerprint.py -q` — the
real shard-1 MPS line fingerprints identically across differing timestamp, home path and
`shard_N`; a planted account number survives in no nested field; home paths and named env
secrets are redacted too; a rotated key changes the digest, proving it is not an unkeyed
oracle; then **[SUITE]**.

**Rock 3 — `observe.py` + `sources/`, the ungated half.** Envelope and emergency sink created
**first**; per-source deadlines, byte caps, process-group kill; CI high-watermark with
resumable gap policy.
*Proof:* `pytest tests/selfheal/test_observe.py -q` — **a run with the breaker latched and the
epoch exhausted still collects, redacts and records every incident** (the new BLOCKER); a
hanging source is killed and recorded degraded; a collector killed mid-run still leaves a
readable envelope and emergency-log entry; an unseen CI run beyond page 1 is collected and its
log archived before the watermark advances; then **[SUITE]**.

**Rock 4 — `ledger.py` + digest.** One authoritative sequenced store with an anchored head and
per-record checksums; torn-tail truncation repair; derived mirrors rebuildable from it; the
corrected attempt boundary (`eligible ⟺ attempts < max`; `quarantined ⟸ attempts ≥ max`); a
**review lease** distinct from the execution lease, since a PR may sit in human review for days
(finding 17, per round 2).
*Proof:* `pytest tests/selfheal/test_ledger.py -q` — valid-prefix truncation is detected via
the anchored head; a fingerprint at exactly `max` is quarantined, not lost; a crash after
`gh pr create` but before journaling the PR number is recovered idempotently by the reconciler;
then **[SUITE]**.

**Rock 5 — `admission.py`.** Latched breaker, cumulative epoch bound, shared-file reservation
under one lock, clock sanity, all fairness gates.
*Proof:* `pytest tests/selfheal/test_admission.py -q` — a latch test proving no non-human code
path clears the breaker; an epoch test proving repair `max+1` is refused however every other
gate is configured; a test proving **refusals do not consume epoch budget** and abandoned
repairs do; two concurrent invocations yield exactly one reservation; then **[SUITE]**.

**Rock 6 — `adapters/lint_format.py`, `repair.py`, `publication.py`, `cli.py`, `__main__.py`.**
The one adapter; branch/commit/push/`gh pr create` as explicit steps; asynchronous Actions
settlement.
*Proof:* an **end-to-end test reaching a verified PR from a real detected lint incident** —
introduce a real ruff violation in a scratch worktree, observe it, repair it, verify *that*
incident is gone, and assert the PR body is redacted (Codex's stated condition for calling one
adapter an honest MVP); plus `python -m selfheal --dry-run` writing nothing; then **[SUITE]**.

**Rock 7 — Deletions, launchd, docs, packaging.** Delete `.github/workflows/self-heal.yml` and
the dead parts of `scripts/self_heal.py`; `render_self_heal_agent.py` with fully-resolved
executables (launchd's PATH omits both `/opt/homebrew/bin/gh` and the venv), explicit
no-`KeepAlive`, overlap prevention, durable stderr; `selfheal/README.md` documenting the two
halves and why the gate lives outside the governed system; `docs/self_healing.md`;
`pyproject.toml` (`packages` must include `selfheal` and `selfheal.*`, plus an **installed-wheel
import proof** — finding 22); **Correction 2**, fixing CLAUDE.md's `scripts/tests` claim.
*Proof:* an executable scheduler acceptance — `launchctl bootstrap`, `kickstart -p`, poll
`launchctl print` for a single completed run, assert no restart within N seconds, `bootout` —
plus `python scripts/workflow_policy.py`, and `pip install . && python -c "import selfheal"`
from a temp venv; then **[SUITE]**.

## Open questions for round 3

- Does deleting `self-heal.yml` outright leave any capability the operator relied on? My read:
  no — it could not see `cron_tracking/` or any `*.log`, and its `step_3` never worked.
- Is `max_repairs_per_epoch = 10` with human `--new-epoch` re-arming a true bound, or merely a
  slower ratchet?
- Does the ungated observation half have any path to unbounded work, given its caps are
  self-enforced rather than externally gated?
- Is "universal observation + lint/format-only repair" an honest description of what ships?
