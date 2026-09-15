# PLAN — Bounded daily diagnostic observation, with lint/format-only automatic repair

Build plan for the `rocket-fuel/self-heal` cycle. Vision in `VTO.md`, deferrals in
`DEFERRED.md`, full reasoning record in `SAME-PAGE-LOG.md`. **Revision 5.**

Round 3 returned `BLOCKED — 18 open`. Four of those were regressions Revision 4 introduced by
over-shortening (provenance filtering and the complete private archive were dropped, not
deliberately cut), one was document drift, and the rest were under-specification. Revision 5
restores what was lost and specifies what was vague. Superseded revisions are on disk as
`PLAN-rev{2,3,4}.md.bak` (untracked — they carry literals that trip the secrets gate).

## Scope, stated honestly

**Bounded daily observation of persisted diagnostic sources, plus lint/format-only automatic
repair.** Adopted verbatim from round 3's answer to open question 4. "Universal observation" is
withdrawn: a source that does not persist its crash output cannot be observed, and
`dca_console_jobs.py` currently does not (`DEFERRED.md`).

Two halves, **independently** gated:

| Half | Gated by | Runs when | May open a PR |
|---|---|---|---|
| Observation | its own whole-tick deadline only | every tick, breaker latched or not | No |
| Repair | the full admission transaction | only when every limit permits | Yes |

## Deletions

- **`.github/workflows/self-heal.yml` deleted in full** — second dispatcher, `contents: write`
  on a reactive trigger, a `workflow_dispatch:` line contradicting its own header comment, and
  a healer that cannot accept an incident. Its capability losses are recorded as intentional in
  `VTO.md` and `DEFERRED.md` (round 3, Q1: they are real losses, not nonexistent ones).
- **`scripts/self_heal.py` reduced** to the `lint_format` adapter's implementation: `ruff check
  --fix` and `ruff format` only, errors propagated, per-command timeout. `pytest
  --snapshot-update` is deleted (unrecognized argument; silently no-opped since written).

## Isolation — repair never runs in the live checkout (finding 1)

Round 3 correctly noted that a local daemon executing repair in the operator's own working tree
under their authenticated `gh` identity relocates the trust problem rather than removing it.
Revision 5:

- Repair runs in a **detached `git worktree`** created under
  `$DCA_CRON_TRACKING_ROOT/self_heal/worktrees/<run_id>`, checked out at the current `origin/main`
  commit — never the operator's dirty tree, so an in-progress edit can neither contaminate a
  repair nor be clobbered by one. The worktree is removed in a `finally`.
- The adapter's subprocesses run with `cwd` set to that worktree and a **minimal environment**:
  `PATH`, `HOME`, `LANG` and nothing else inherited, so no `ROBINHOOD_*` value is reachable
  from a repair subprocess.
- `gh pr create` remains the operator's identity — unavoidable for a single-operator local
  daemon, and recorded as an accepted residual risk in `DEFERRED.md` under "Multi-machine
  coordination". It is bounded by the epoch counter: at most `max_repairs_per_epoch` PRs can
  ever be opened without a human re-arm.

## Durability protocol (findings 2, 21)

One authoritative file, one lock, one transaction. Revision 4's split between a "authoritative
ledger" and a separate `reservations.json` was the defect.

- **`state.json`** holds epoch counter, breaker latch, daily counters, budget reservations and
  per-fingerprint state. It is the *only* input to an admission decision.
- Every mutation is one transaction under `file_lock.cross_process_lock`:
  read → validate → mutate → write temp → `fsync(temp)` → `os.replace` → `fsync(dir)`.
  `os.replace` is atomic on POSIX, so a crash leaves either the old or the new state, never a
  torn one.
- **`incidents.jsonl`** is an append-only *journal*, not authoritative state: each record
  carries a monotonic sequence number, the previous record's digest (an anchored chain), and
  its own checksum. It is `fsync`ed on append. On load, a torn or chain-breaking tail is
  truncated to the last valid anchor and the loss is recorded. Because the chain anchors each
  record to its predecessor, a **valid-prefix truncation is detectable** — the head sequence in
  `state.json` will exceed the journal's last valid sequence.
- **PR-create recovery (finding 17).** Before `gh pr create`, an `intent` record is journaled
  with the run id and branch. After it returns, the PR number is journaled. A crash between the
  two is recovered by the next run's reconciler, which queries `gh pr list --head <branch>`:
  found ⇒ adopt it; not found ⇒ the intent is abandoned and charged at worst case.
- `diagnostics/` mirrors and any compacted index are **derived** and rebuildable; they are
  never read for a decision.

## Bounding observation (finding 4, round 3 Q3)

Round 3 was right that per-source subprocess caps do not bound the coordinator. Revision 5:

- **One externally enforced whole-tick deadline.** `observe` installs
  `signal.setitimer(ITIMER_REAL, max_tick_seconds)` (default **600**) at start; the handler
  finalizes the envelope and exits non-zero. This bounds the coordinator, config load, ledger
  work, reconciliation and digest generation — not just source subprocesses.
- Per-source deadlines remain, enforced by the parent via `subprocess` timeout plus
  `os.killpg`, so a collector that hangs *before* checking its own deadline is still killed.
- Ledger, reconciliation, digest and emergency-log operations each carry an explicit record
  cap; exceeding one is a recorded degraded operation.
- **The single-run lock carries a TTL** (`max_tick_seconds * 2`). A hung or killed observer
  cannot suppress every later tick — the next tick steals an expired lock and records the
  theft. Revision 4's unbounded overlap prevention was itself a denial-of-observation path.

## Durable evidence (finding 5)

Restored — Revision 4 dropped the complete archive when shortening, which was not a deliberate
cut. Before any fallible work (before config load, ledger, or lock acquisition):

1. `runs/<run_id>/envelope.json` written and `fsync`ed.
2. `runs/<run_id>/_emergency.log` opened `O_APPEND`, written line-at-a-time with `fsync`.
3. For every incident, the **complete unredacted crash text** is archived at
   `runs/<run_id>/raw/<fingerprint>.txt` — written temp → `fsync` → `os.replace`, so a
   half-written archive is never observed. Bounded by `max_archive_bytes_per_incident`
   (default **1 MiB**) and `max_archive_bytes_per_run` (default **64 MiB**); truncation is
   recorded in the incident, never silent.
4. A final status record is written last. An envelope without one is what the next run's
   reconciler converts into a durable failure record.

The archive lives only under `$DCA_CRON_TRACKING_ROOT`, and the writer **refuses to start** if
`git check-ignore` does not confirm that root is ignored.

## Clock (finding 7)

There is no trusted external time source, so the plan stops pretending otherwise and anchors to
what it controls:

- **Anchor:** the maximum `observed_at` in `state.json`. Wall clock is accepted only if it is
  ≥ anchor and ≤ anchor + `max_forward_skew_seconds` (default **172_800**, two days — larger
  than any legitimate gap between daily ticks, smaller than a jump that could manufacture a new
  budget period).
- Outside that band ⇒ `CLOCK_IMPLAUSIBLE`, repair refused; **observation still runs** and
  records the anomaly, because an implausible clock is itself evidence.
- All elapsed measurement uses `time.monotonic()`. Across a reboot, monotonic resets, so any
  reservation whose monotonic base predates the current boot (`psutil`-free check: compare to
  the kernel boot time from `sysctl kern.boottime`) is treated as abandoned and charged at
  worst case.
- `--now` is accepted **only** under `--dry-run` or pytest, enforced by an assertion on
  `sys.modules`, not by convention.

## Loop termination (finding 3)

Two mechanisms, no contradiction. Revision 4 said "three consecutive" in one place and "any
three" in another; the rule is:

- **Latched breaker:** **any** `breaker_failure_threshold` (default **3**) unsuccessful repairs
  **within the current epoch** latch it `open`. Not consecutive — alternating outcomes must not
  dodge the latch. No automatic cooldown; only a human `--reset-breaker` clears it.
- **Cumulative epoch bound:** a monotonic counter of admitted-or-abandoned repairs that never
  resets on its own. At `max_repairs_per_epoch` (default **10**) repair stops until a human runs
  `--new-epoch`. Refusals do not count (they perform no repair); crashes and abandonments do.
- **Key loss fails closed (round 3 Q2).** Revision 4 said a lost fingerprint key "starts a new
  epoch" — which handed the pipeline the human-only re-arm and reduced the bound to a ratchet.
  Corrected: a missing or unreadable key refuses repair with `FINGERPRINT_KEY_UNAVAILABLE` and
  requires an explicit human `--adopt-new-key`, which itself does not reset the epoch counter.

No pipeline code path writes the epoch counter's reset, the breaker latch, or the key adoption.

## Redaction (finding 10)

Recursive over the whole object graph, applied in the `Incident` constructor. The staged-bytes
scan runs on the **exact bytes to be published** and covers:

- the repository's own `_ACCOUNT_SHAPED` and `_LABELED_ACCOUNT`;
- the labelled spellings round 3 found missing: `accountNumber…`, `account_id_…`, `acctNbr.…`,
  `broker123456789`, `rh_123456789` — one widened regex covering
  `(?i)(acct|account|broker|robinhood|alpaca|rh)[^0-9A-Za-z]{0,4}\d{6,12}` **and** the
  camel/snake forms with no separator at all;
- absolute home paths (`/Users/<name>`, `$HOME`) → `~`;
- the literal value of every environment variable whose name matches
  `SECRET|TOKEN|PASSWORD|API_KEY|ACCOUNT_NUMBER`, whether or not it is currently set — the set
  of *names* is read from config so an unset variable is still pattern-matched by shape.

A hit aborts publication and latches the breaker.

Fingerprints: HMAC-SHA256 over fully-sanitized canonical input, domain-separated with a
constant prefix, keyed by `.selfheal_fingerprint_key` — created `O_EXCL` mode `0600`, and
**already added to `.gitignore` in this cycle** (round 3 correctly noted Revision 4 claimed this
without doing it).

## Self-origin exclusion (finding 16 — restored)

Revision 4 dropped this while shortening; the VTO requires pre-ledger exclusion. Restored, and
based on the ledger's own records rather than substrings:

- Drop if the incident's GitHub run id, workflow id, or PR number appears in `state.json` as one
  this pipeline created.
- Drop if the incident's originating commit is the head commit of a self-heal PR this pipeline
  opened — this is the `main`-CI-after-merge case round 2 raised, resolved by journaling each
  PR's merge commit when the reconciler observes the merge.
- Substring checks on path/branch/signature survive only as defence in depth.

Trustworthy because there is exactly one writer; that assumption is recorded in `DEFERRED.md`.

## Incident state machine (finding 17)

```
new ──admit──▶ dispatched(execution lease, minutes)
                   │
                   ├─ verify() fails / adapter error ─▶ refused      (attempts+1)
                   ├─ execution lease expires         ─▶ abandoned   (attempts+1, charged)
                   └─ PR opened ──▶ in_review(review lease, days)
                                        ├─ PR merged  & main CI green ─▶ repaired (terminal)
                                        ├─ PR merged  & main CI red   ─▶ refused  (attempts+1)
                                        ├─ PR closed unmerged         ─▶ refused  (attempts+1)
                                        ├─ CI red on the PR           ─▶ refused  (attempts+1)
                                        └─ review lease expires       ─▶ refused  (attempts+1)
eligible ⟺ state ∈ {new, refused} ∧ attempts < max_attempts_per_fingerprint
quarantined (terminal) ⟸ attempts ≥ max_attempts_per_fingerprint
```

The execution lease (minutes) and review lease (days, default **14**) are separate — Revision 3
conflated them.

## Budget (finding 6)

`monthly_actions_minutes` default **300**, accounting period = calendar month in UTC.
Worst case per repair = `max_wall_clock_seconds/60` local minutes + `ci_worst_case_minutes`
(default **25**, from the workflow's own `timeout-minutes`). Debited at reservation. Actions
minutes are settled **asynchronously** by the next run's reconciler reading the completed run
via `gh run view --json`; unsettled after the review lease ⇒ charged at worst case.

## CI collection (finding 14)

`discovery_cursor` (highest run id seen) advances **independently** of `archive_cursor` (highest
run id whose failed-step log is archived). An unavailable or expired log records a
`LOG_UNAVAILABLE` gap and does **not** block discovery — round 3's head-of-line case. Gaps are
retried until the review lease expires, then recorded permanently as unarchivable.

## Config (finding 19)

Strict runtime validation rejecting: duplicate keys, non-object JSON, unknown keys, **missing
required fields**, **missing packaged config file**, bools-as-ints, non-finite, negative,
**zero**, and cross-field violations (e.g. `max_repairs_per_epoch` ≥ 1,
`max_tick_seconds` < `lock_ttl_seconds`). No standalone 6–12 digit literal in any tracked file.

## Rocks

Seven. Every proof ends with **[SUITE]** =
`ruff check . && .venv/bin/python -m pytest -q && python run_dca.py --demo --report --now 2026-08-03T15:45 --artifacts-dir /tmp/dca-demo`.
**[SUITE]** is green as of Revision 5 (205 tests in `test_config_invariants.py`; the failures
round 3 found came from this cycle's own tracked `.bak` snapshots and are fixed by gitignoring
them).

**Rock 1 — `config.py`, `reasons.py`, `config/self_heal.json`, `schemas/self_heal.schema.json`.**
*Proof:* `.venv/bin/python -m pytest tests/selfheal/test_config.py tests/test_config_invariants.py -q`
— one rejection test per malformed case above, including missing-required-field and
missing-packaged-file; then **[SUITE]**.

**Rock 2 — `redaction.py`, `fingerprint.py`.**
*Proof:* `.venv/bin/python -m pytest tests/selfheal/test_redaction.py tests/selfheal/test_fingerprint.py -q`
— the shard-1 MPS line fingerprints identically across differing timestamp, home path and
`shard_N`; **a parametrized test over every documented account spelling** (`accountNumber…`,
`account_id_…`, `acctNbr.…`, `broker…`, `rh_…`, bare) is redacted from a nested field; home
paths and named env-secret values are redacted; a rotated key changes the digest; the keyfile is
created `0600` and `git check-ignore` confirms it ignored; then **[SUITE]**.

**Rock 3 — `state.py` + `ledger.py`: the durability protocol.**
*Proof:* `.venv/bin/python -m pytest tests/selfheal/test_state.py tests/selfheal/test_ledger.py -q`
— a `SIGKILL` injected between temp-write and `os.replace` leaves valid old state; valid-prefix
truncation is detected via the anchored chain; a fingerprint at exactly `max` is quarantined,
not lost; every transition in the state machine above has a test; two concurrent transactions
serialize; then **[SUITE]**.

**Rock 4 — `observe.py` + `sources/`, the ungated half.**
*Proof:* `.venv/bin/python -m pytest tests/selfheal/test_observe.py -q` — **with the breaker
latched and the epoch exhausted, every incident is still collected, archived unredacted, and
recorded**; the whole-tick `setitimer` deadline fires and still finalizes the envelope; a
collector hung before its own deadline check is killed by `killpg`; an expired lock is stolen by
the next tick; a CI log that is unavailable does not block discovery; then **[SUITE]**.

**Rock 5 — `admission.py`.**
*Proof:* `.venv/bin/python -m pytest tests/selfheal/test_admission.py -q` — one test per gate;
a latch test asserting **no non-human entry point** clears the breaker (introspect the module
for writers); an epoch test proving repair `max+1` is refused however every other gate is set;
refusals do not consume epoch budget, abandonments do; a missing key yields
`FINGERPRINT_KEY_UNAVAILABLE` and does **not** start a new epoch; a clock outside the band
refuses repair while observation proceeds; then **[SUITE]**.

**Rock 6 — `adapters/lint_format.py`, `repair.py`, `publication.py`, `cli.py`, `__main__.py`.**
*Proof (one command):*
`.venv/bin/python -m pytest tests/selfheal/test_end_to_end.py -q` — creates a scratch git
worktree, writes a file with a real ruff violation, runs `observe` then `repair` against a
stubbed `gh`, and asserts: the incident was detected, the worktree (not the live checkout) was
modified, `verify()` confirms *that* violation is gone, the PR body passes the staged-bytes
scan with a planted account number present in the raw archive, and the live checkout is
byte-identical before and after. Plus `.venv/bin/python -m selfheal observe --dry-run` writing
nothing; then **[SUITE]**.

**Rock 7 — deletions, launchd, docs, packaging.**
*Proof (three commands):*
(a) `.venv/bin/python scripts/workflow_policy.py`;
(b) scheduler acceptance — `launchctl bootstrap gui/$UID <plist>`, `launchctl kickstart -p
gui/$UID/com.all-stock-dca.self-heal`, poll `launchctl print` until the run completes, assert
the PID does **not** reappear within **120 seconds**, then `launchctl bootout`;
(c) packaging — `python -m venv /tmp/wheelcheck && /tmp/wheelcheck/bin/pip install . &&
cd /tmp && /tmp/wheelcheck/bin/python -c "import selfheal, selfheal.adapters.lint_format;
from selfheal.config import load; load()"` — run from `/tmp`, **outside the checkout**, so an
in-tree package cannot shadow the wheel, and loading config proves package data shipped;
then **[SUITE]**.

## Open questions for round 4

- Does worktree isolation plus a minimal subprocess environment adequately bound finding 1 for a
  single-operator local daemon, or is any local execution irreducibly untrusted?
- Is `setitimer(ITIMER_REAL)` a sufficient external bound given it cannot interrupt a blocking
  syscall in a C extension? The observer is pure stdlib, so I believe yes.
- Does charging abandoned repairs at worst case create a denial path where an attacker who can
  crash the daemon exhausts the epoch? One writer, local only — I believe out of threat model.
