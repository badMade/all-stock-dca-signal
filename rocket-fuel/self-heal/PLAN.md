# PLAN — Bounded daily diagnostic observation, with lint/format-only automatic repair

Build plan for the `rocket-fuel/self-heal` cycle. Vision in `VTO.md`, deferrals in
`DEFERRED.md`, full reasoning record in `SAME-PAGE-LOG.md`. **Revision 6.**

Round 4 returned `BLOCKED — 15 open` (ten findings resolved; trajectory 23 → 22 → 18 → 15),
plus one new KILL. Revision 6 addresses every fixable item and marks the genuinely irreducible
ones ACCEPTED-RESIDUAL in place rather than leaving them to recur.

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
- **The orphaned scheduler is deleted with it (round 4, new KILL).** `compute-schedule.yml`
  exists solely to rewrite `self-heal.yml`'s `# AUTO-UPDATED` cron line and
  `.github/self-heal-schedule.yml`; it holds `contents: write` + `pull-requests: write`, runs
  monthly, and opens a PR. Deleting its only consumer would leave a privileged monthly PR
  writer maintaining a file that no longer exists. The deletion therefore cascades:
  `.github/workflows/compute-schedule.yml`, `scripts/compute_schedule.py`,
  `.github/self-heal-schedule.yml`, `SELF_HEAL_SETUP.md`,
  `scripts/tests/test_compute_schedule.py`, and the `docs/repo_map.md` row. Verified: no other
  consumer references `compute_schedule` or `AUTO-UPDATED`.
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
- **The coordinator runs from an installed artifact, not the mutable checkout (round 4,
  finding 1).** launchd invokes an absolute path into a dedicated virtualenv
  (`~/Library/Application Support/all-stock-dca/venv/bin/python -m selfheal observe`) populated
  by `pip install .` from a reviewed commit — never `.venv/` in the working tree, whose
  contents change with every edit. Upgrading is an explicit operator action.
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
- **PR-create recovery (findings 17, 21).** The `intent` — run id and branch name — is written
  to **`state.json`**, the authoritative store, and *before the branch is created or pushed*.
  Revision 5 journaled it to the non-authoritative journal and only before `gh pr create`,
  leaving a crash after `git push` with an untracked remote branch and no record (round 4).
  Recovery queries both `git ls-remote --heads origin <branch>` and
  `gh pr list --head <branch>`: PR found ⇒ adopt; branch found without a PR ⇒ delete the remote
  branch and charge the attempt; neither ⇒ intent abandoned, charged at worst case.
- `diagnostics/` mirrors and any compacted index are **derived** and rebuildable; they are
  never read for a decision.

## Bounding observation (finding 4, round 3 Q3)

Round 3 was right that per-source subprocess caps do not bound the coordinator. Revision 5:

- **An out-of-process supervisor, not an in-process timer (round 4, Q2).** Revision 5's
  `signal.setitimer(ITIMER_REAL)` was not external enforcement: a Python signal handler cannot
  interrupt a thread blocked in a C extension or a kernel call. Replaced by
  `selfheal.supervisor`, a parent process that `Popen`s the worker with
  `start_new_session=True`, `wait(timeout=max_tick_seconds)` (default **600**), and on expiry
  sends `SIGTERM` then `SIGKILL` to the **process group**, waits for actual termination, and
  only then releases the lock. This bounds the coordinator, config load, ledger work,
  reconciliation and digest generation, and it bounds the repair side's `git` and `gh`
  operations too.
- Per-source deadlines remain, enforced by the parent via `subprocess` timeout plus
  `os.killpg`, so a collector that hangs *before* checking its own deadline is still killed.
- Ledger, reconciliation, digest and emergency-log operations each carry an explicit record
  cap; exceeding one is a recorded degraded operation.
- **No TTL lock theft.** Round 4 correctly noted that stealing an expired lock admits a second
  process alongside a wedged first one. Because the supervisor *verifies termination* before
  releasing the lock, an expired lock cannot coexist with a live worker; a lock whose owning
  supervisor PID is gone is reclaimed only after confirming via `os.kill(pid, 0)` that neither
  supervisor nor worker group survives.

## Durable evidence (finding 5)

Restored — Revision 4 dropped the complete archive when shortening, which was not a deliberate
cut. Before any fallible work (before config load, ledger, or lock acquisition):

1. `runs/<run_id>/envelope.json` written and `fsync`ed.
2. `runs/<run_id>/_emergency.log` opened `O_APPEND`, written line-at-a-time with `fsync`.
3. For every incident, the unredacted crash text is archived at
   `runs/<run_id>/raw/<seq>-<fingerprint_or_NOKEY>.txt`. Round 4 was right that "complete"
   contradicted a byte cap and that a per-fingerprint filename collides on re-occurrence and
   cannot be formed at all when the key is unavailable: the name is therefore run-scoped and
   sequence-prefixed, and the word used is **"as-captured, up to a declared cap"**, never
   "complete" — written temp → `fsync` → `os.replace`, so a
   half-written archive is never observed. Bounded by `max_archive_bytes_per_incident`
   (default **1 MiB**) and `max_archive_bytes_per_run` (default **64 MiB**); truncation is
   recorded in the incident, never silent.
4. A final status record is written last. An envelope without one is what the next run's
   reconciler converts into a durable failure record.

The archive lives only under `$DCA_CRON_TRACKING_ROOT`. The writer refuses to start unless that
root is **either** outside the repository working tree entirely **or** confirmed ignored by
`git check-ignore` — round 4 correctly noted that `check-ignore` alone rejects the supported
case of an artifact root pointing outside the checkout.

## Clock (finding 7)

There is no trusted external time source, so the plan stops pretending otherwise and anchors to
what it controls:

- **Anchor:** the maximum `observed_at` in `state.json`. Wall clock is accepted only if it is
  ≥ anchor and ≤ anchor + `max_forward_skew_seconds` (default **172_800**, two days — larger
  than any legitimate gap between daily ticks, smaller than a jump that could manufacture a new
  budget period).
- **The anchor cannot ratchet (round 4).** Successive accepted two-day jumps would otherwise
  walk the anchor forward indefinitely, and a small jump across a month boundary manufactures a
  fresh budget period. Two corrections: cumulative forward advance is capped at
  `max_cumulative_skew_seconds` per epoch, and the budget period is a **monotonic period
  counter in `state.json`** incremented only when wall time has advanced past the recorded
  period end by a full period — never the wall clock's calendar month directly.
- Outside that band ⇒ `CLOCK_IMPLAUSIBLE`, repair refused; **observation still runs** and
  records the anomaly, because an implausible clock is itself evidence.
- All elapsed measurement uses `time.monotonic()`. Across a reboot, monotonic resets, so any
  reservation whose monotonic base predates the current boot (`psutil`-free check: compare to
  the kernel boot time from `sysctl kern.boottime`) is treated as abandoned and charged at
  worst case.
- `--now` is accepted **only** under `--dry-run` or pytest, enforced by an explicit
  `if ...: raise SystemExit(2)` — **not** an `assert`, which `python -O` strips (round 4).

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
- the labelled spellings, with a regex that actually covers the zero-separator camel/snake
  forms round 4 pointed out the displayed pattern still missed —
  `(?i)(acct|account|broker|robinhood|alpaca|rh)(?:nbr|number|no|id)?[^0-9A-Za-z]{0,4}\d{6,12}`
  — which matches `accountNumber…`, `account_id_…` and `acctNbr.…` as well as separator forms;
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

- Drop on a **composite identity** — `(run_id, run_attempt)`, head SHA, and PR number —
  recorded in `state.json` as one this pipeline created. Round 4 was right that a *workflow* id
  names the shared workflow definition and would suppress unrelated runs of it.
- A dropped incident is still **archived privately**; self-origin exclusion removes it from
  repair admission, never from the evidence record.
- Drop if the incident's originating commit is **either** the head commit of a self-heal PR this
  pipeline opened **or** its recorded merge commit — the `main`-CI-after-merge case. Round 4
  flagged Revision 5's head-vs-merge wording as contradictory; both are journaled, and because
  reconciliation can lag collection, an incident whose commit is not yet classified is held as
  `provenance_pending` and re-evaluated next tick rather than admitted.
- Substring checks on path/branch/signature survive only as defence in depth.

Trustworthy because there is exactly one writer; that assumption is recorded in `DEFERRED.md`.

## Repair target is `origin/main` only (round 4, finding 18)

Contributor-PR lint failures *do* have an adapter, and Revision 5 left them admissible even
though the repair worktree is checked out at `origin/main` — so an untrusted external PR could
consume attempts, epoch capacity and breaker failures against a defect that does not exist on
the repair target. Corrected: an incident whose originating ref is **not** `main` is
**report-only**, regardless of adapter. Only failures observed on `origin/main` (or on a
self-heal PR this pipeline itself opened) are admissible. This closes the exhaustion-denial
path round 4 raised in open question 3.

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
Worst case per repair = `max_wall_clock_seconds/60` local minutes + `ci_worst_case_minutes`.
Revision 5's **25** was wrong by roughly 6×: a `pull_request` on this repo fans out to `ci.yml`
(core matrix + audit + five sibling-suite jobs) **and** `antigravity-pr-review.yml`
(`timeout-minutes: 10`) **and** `antigravity-pr-review-post.yml` (two jobs, 5 and 15), the
latter two being a Gemini-backed reviewer that Revision 5 did not count at all. Default raised
to **150** and derived in the plan from the sum of every `timeout-minutes` reachable from one
PR, so it stays honest when workflows change. Settlement sums **every** run for the PR's head
SHA, not one; unsettled after the review lease ⇒ charged at worst case.

## CI collection (finding 14)

Cursors are composite `(run_id, run_attempt)`, not bare run id — round 4 correctly noted a
re-run reuses the run id with a new attempt and would be lost. Pagination walks **oldest-first**
from the cursor, so a capped page can never advance past an older unseen run.
`discovery_cursor` advances **independently** of `archive_cursor` (the highest
`(run_id, run_attempt)` whose failed-step log is archived). An unavailable or expired log records a
`LOG_UNAVAILABLE` gap and does **not** block discovery — round 3's head-of-line case. Gaps are
retried until the review lease expires, then recorded permanently as unarchivable.

## Config (finding 19)

Strict runtime validation rejecting: duplicate keys, non-object JSON, unknown keys, **missing
required fields**, **missing packaged config file**, bools-as-ints, non-finite, negative,
**zero**, and cross-field violations. **Safe upper bounds are restored** — Revision 5 silently dropped
them when adding the missing-field checks (round 4): `max_tick_seconds` ≤ 3600,
`max_repairs_per_epoch` ≤ 50, `max_attempts_per_fingerprint` ≤ 10,
`max_archive_bytes_per_run` ≤ 512 MiB, `monthly_actions_minutes` ≤ 2000,
`max_forward_skew_seconds` ≤ 604_800. Without ceilings a configured value can defeat
boundedness as effectively as a missing one. No standalone 6–12 digit literal in any tracked file.

## Document consistency (round 4)

`VTO.md` Revision 6 corrects three claims round 4 found false: the external-binary list now
names `git`, `gh`, `ruff`, the healthcheck tooling and `sysctl` rather than just the first two;
the archive is described as "as-captured up to a declared cap", not "complete"; and the
`gh`-identity residual is now genuinely written into `DEFERRED.md` rather than merely
referenced by this plan.

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
byte-identical before and after. Plus a dry-run test that snapshots a `sha256` manifest of the entire artifact root and the
worktree before and after `python -m selfheal observe --dry-run` and **asserts the manifests are
identical** — round 4 noted Revision 5 only ran the command without asserting no writes; then
**[SUITE]**.

**Rock 7 — deletions (cascaded), launchd, docs, packaging.** Includes removing
`scripts/tests/test_compute_schedule.py`, `scripts/tests/test_pr_review_workflow_trust_boundary.py`'s
self-heal cases, and any other existing test that asserts the presence of the deleted workflow
or legacy healer — round 4 correctly noted the current suite would go red on the deletion
otherwise.
*Proof (three commands):*
(a) `.venv/bin/python scripts/workflow_policy.py`;
(b) scheduler acceptance, one script with no placeholder — `tests/selfheal/accept_launchd.sh`
renders the plist to
`"$HOME/Library/LaunchAgents/com.all-stock-dca.self-heal.plist"`, asserts its contract
(`ProgramArguments` = the absolute installed-venv python + `-m selfheal observe`;
`StartCalendarInterval` = 04:00; `WorkingDirectory` = the artifact root; `StandardOutPath` /
`StandardErrorPath` under it; **`KeepAlive` absent or `false`**; `RunAtLoad` false), then
`launchctl bootstrap gui/$UID <that path>`, `launchctl kickstart -p
gui/$UID/com.all-stock-dca.self-heal`, polls `launchctl print` until the run completes, asserts
the PID does not reappear within 120 seconds, and `launchctl bootout`s in a `trap`;
(c) packaging — `python -m venv /tmp/wheelcheck && /tmp/wheelcheck/bin/pip install . &&
cd /tmp && /tmp/wheelcheck/bin/python -c "import selfheal, selfheal.adapters.lint_format;
from selfheal.config import load; load()"` — run from `/tmp`, **outside the checkout**, so an
in-tree package cannot shadow the wheel, and loading config proves package data shipped;
then **[SUITE]**.

## Open questions for round 5

- With the coordinator bound to an installed wheel, repair confined to an `origin/main`
  worktree, and non-`main` incidents report-only, is anything in finding 1 still fixable, or is
  the remainder genuinely the ACCEPTED-RESIDUAL same-UID `gh` credential?
- Does the supervisor/process-group design close finding 4, given the supervisor is itself a
  Python process? Its only blocking call is `waitpid` with a timeout.
- Is the monotonic period counter sufficient to stop month-boundary budget manufacture, or does
  it need an independent durable monotonic source?
- Are the remaining open findings now genuinely at the level where further rounds refine rather
  than protect? Round 5 is the last; anything still open goes to the operator as a tie-break.
