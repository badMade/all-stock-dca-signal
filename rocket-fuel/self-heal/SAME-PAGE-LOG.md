# Same Page Meeting log — rocket-fuel/self-heal

Visionary: Claude (Opus 5). Integrator: Codex CLI 0.145.0,
`codex exec -m gpt-5.6-sol --sandbox read-only --json`.
Subject: `rocket-fuel/self-heal/PLAN.md` (with `VTO.md`, `DEFERRED.md`).
Max 5 rounds; the verdict line is the literal last line of each Codex reply.

**CLI drift recorded for this machine.** `~/.codex/config.toml` pins `model = "gpt-6-astra"`,
which this account cannot serve; every round passes `-m gpt-5.6-sol` explicitly. `codex exec`
on 0.145.0 rejects `--ask-for-approval` ("unexpected argument"), so the flag the skill
prescribes is omitted — the `--sandbox read-only` sandbox, not the approval flag, is what
prevents writes, and a non-interactive run has no prompt to hang on. `codex exec resume`
additionally rejects `--sandbox`, so rounds 2+ pass `-c 'sandbox_mode="read-only"'`.

## Round 1 — plan as submitted (Revision 1)

Timestamp: 2026-09-14T20:20Z (dispatched)
PLAN.md sha256 at dispatch: cb17f43af312f992b4c5dfef…  VTO.md sha256: e011f790dfd0e6aba12d7970…

**Process note, recorded rather than hidden.** `PLAN.md` was revised to Revision 2 *while*
round 1 was still executing, at the operator's direction, folding two Clarity-Break defects
into Rock 7 (the `workflow_dispatch` trust-boundary contradiction and CLAUDE.md's incorrect
"`scripts/tests` sits outside the root sweep" claim), and a third edit added the write-time
redaction requirement. Codex's round-1 findings are therefore against Revision 1. Where a
finding is already answered by Revision 2, this log says so explicitly and does **not** claim
the plan pre-empted it.

### Prompt sent

See `rocket-fuel/self-heal/SAME-PAGE-PROMPT-ROUND-1.md` (verbatim, committed).

### Codex reply

[BLOCKER] diagnostics-branch trust paradox — A diagnostics-only orphan has neither workflow nor repair code and the current job condition rejects push, while adding a privileged workflow there lets the mutable ref control execution because GitHub uses the workflow version associated with the event ref ([GitHub workflow semantics](https://docs.github.com/en/actions/concepts/workflows-and-actions/workflows)).
[BLOCKER] admission is neither atomic nor authoritative — Locking individual appends does not make read-evaluate-reserve atomic, and recording local DISPATCH before CI repeats the governor makes CI either reject the consumed daily slot or ignore it and permit a cross-machine race.
[BLOCKER] loop controls only throttle — Daily reset, remintable fingerprints, an automatically reopening breaker, and heuristic provenance filtering still permit repair work forever, contradicting the claim that any one mechanism halts a runaway.
[BLOCKER] unbounded work precedes the governor — Collection, GitHub access, and timeout-free healthchecks occur before rate and wall-clock gates, so a hung source or subprocess can perform unlimited unmetered work.
[BLOCKER] durable evidence contract is unmet — The plan retains only a four-KiB incident excerpt and fourteen-day Actions artifacts, with no durable run envelope before setup or collection can crash.
[BLOCKER] spend caps are postpaid — Usage records containing final time and consumption are written after dispatch, allowing near-cap, concurrent, crashed, or timed-out runs to exceed limits without being charged.
[BLOCKER] forward clock jumps bypass limits — Rejecting only backward movement while permitting unrestricted live `--now` values lets forward jumps advance daily and monthly buckets and expire breaker cooldowns.
[BLOCKER] a refusing step does not stop the job — Exiting the governor step successfully does not terminate the Actions job, so the existing repair steps remain eligible and run after a refusal.
[BLOCKER] the budget default violates the account grep — The tracked literal `200000` matches the repository’s forbidden six-to-twelve-digit account pattern and is not exempt, so Rock 4’s stated invariant proof cannot pass.
[BLOCKER] public redaction is not closed over the artifact — The borrowed redactor misses account forms such as `broker123456789` and `rh_123456789` when the original environment value is unavailable, while the proof covers only one standalone value and not every serialized field.
[BLOCKER] the public fingerprint is secret-derived — Hashing raw messages before complete redaction can turn the committed unkeyed fingerprint into an offline verification oracle for a low-entropy account number.
[BLOCKER] the frozen repair baseline is not as claimed — `scripts/self_heal.py` invokes an undeclared `--snapshot-update` option, performs unversioned stub installs despite the “pinned” claim, swallows command failures, and can miss edits when porcelain status text remains unchanged.
[BLOCKER] incidents never reach the healer — `scripts/self_heal.py` accepts no incident input and Rock 7 explicitly freezes it, so the selected fingerprint, diagnostic, and source-specific success condition are never fed to the repair process.
[BLOCKER] CI collection is lossy and logless — `gh run list --limit 20` returns metadata rather than failed-step logs and has no cursor or pagination, so bursts and collector outages can permanently hide new failures.
[BLOCKER] hard console crashes are invisible — The console initially persists an empty queued record, keeps running output in memory, and does not persist its restored interrupted state, while the proposed collector selects only nonzero terminal records.
[BLOCKER] self-origin filtering is late and heuristic — VTO requires self-origin incidents to be dropped before the ledger, but the plan records them first and relies on paths, branch names, and signatures that miss self-heal failures occurring on `main`.
[BLOCKER] incident states have dead ends — Eligibility stops at `attempts == max` although quarantine requires `attempts > max`, and `dispatched` has no lease or reconciler for workflow crashes, closed PRs, or later merges.
[MAJOR] unactionable incidents can monopolize admission — The known research MPS failure cannot be repaired or validated by the frozen core healer, yet it can consume three daily slots and trip the global breaker without any fairness policy.
[BLOCKER] runtime config is not fail-closed — Test-time schema validation does not reject runtime duplicate keys, non-object JSON, booleans-as-integers, nonfinite or negative values, missing fields, or missing packaged config before defaults grant permission.
[BLOCKER] launchd wiring can fail or relaunch indefinitely — Rock 6’s cited model uses a system-only PATH that omits the actual `gh` and virtualenv tools plus failure-based KeepAlive, while its proof neither forbids rapid restart loops nor loads and executes the agent.
[BLOCKER] ledger corruption and branch races lack recovery — A process lock cannot coordinate the Mac with GitHub, JSONL can tear or truncate, and the local log, public mirror, and compacted index can diverge without sequencing, compare-and-swap, or recovery rules.
[BLOCKER] stated proofs cannot establish the plan — Rocks 1–6 do not rerun the existing suite as claimed, Rock 1 promises a collector-output walk while declaring I/O out of scope, and no proof exercises the critical crash, branch, admission, public-commit, or scheduler interleavings.
[MAJOR] future-model accounting is scope creep — Rock 5 builds token pricing and USD accounting for an LLM repair path explicitly deferred, so all current runs report zero and no stated command can prove real enforcement.

diagnostics-branch trust paradox: KILL — Replace the privileged orphan-branch push topology with a default-branch trusted consumer of inert, provenance-validated data.
admission is neither atomic nor authoritative: FIX — Design one authoritative atomic reservation keyed by run and fingerprint before any repair work begins.
loop controls only throttle: FIX — Add a latched global breaker requiring human reset and a finite lifetime work bound covering every execution path.
unbounded work precedes the governor: FIX — Specify monotonic global and per-source deadlines, byte and incident caps, process-group termination, and a single-run lock.
durable evidence contract is unmet: FIX — Require an immutable run record before fallible work, complete private crash/debug archival, final status, and an independent emergency sink.
spend caps are postpaid: FIX — Reserve worst-case cost atomically before dispatch, settle afterward, and charge abandoned reservations conservatively.
forward clock jumps bypass limits: FIX — Restrict `--now` to tests or dry runs, reject excessive forward skew, and measure elapsed work monotonically.
a refusing step does not stop the job: FIX — Make admission a separate job whose output gates every side-effecting job and step.
the budget default violates the account grep: FIX — Remove the account-shaped literal without weakening or exempting the standing secret invariant.
public redaction is not closed over the artifact: FIX — Define recursive whole-object sanitization and scan the exact staged public bytes with the repository’s complete invariant patterns.
the public fingerprint is secret-derived: FIX — Fingerprint fully sanitized canonical input or use a private keyed construction that is never committed.
the frozen repair baseline is not as claimed: FIX — Make a working, pinned, error-propagating, timeout-bounded repair baseline an explicit prerequisite and test its real commands end to end.
incidents never reach the healer: FIX — Define an incident-input and source-specific verification contract, leaving unsupported classes report-only.
CI collection is lossy and logless: FIX — Persist a cursor, paginate every unseen run, archive failed logs before advancing it, and durably retry gaps.
hard console crashes are invisible: FIX — Persist running state and bounded output incrementally and atomically convert stale work into a durable failure record.
self-origin filtering is late and heuristic: FIX — Use trusted run, workflow, PR, and causal-parent provenance before ledger insertion, retaining the governor check as defense in depth.
incident states have dead ends: FIX — Correct the attempt boundary and add leases plus reconciliation for workflow, PR, merge, cancellation, and crash outcomes.
unactionable incidents can monopolize admission: KILL — Keep unsupported research failures in collection and reporting, but remove their automatic dispatch this cycle and define fair selection for supported incidents.
runtime config is not fail-closed: FIX — Require strict runtime parsing and semantic validation, with missing or invalid configuration always refusing dispatch.
launchd wiring can fail or relaunch indefinitely: FIX — Specify resolved executables, explicit no-KeepAlive behavior, overlap prevention, durable stderr, and install/load/kickstart acceptance.
ledger corruption and branch races lack recovery: FIX — Choose one authoritative sequenced store with atomic reservation, integrity checks, conflict handling, and rebuildable derived mirrors.
stated proofs cannot establish the plan: FIX — Replace isolated proofs with executable composition tests and remove any behavior that cannot be proven within seven Rocks.
future-model accounting is scope creep: KILL — Retain enforceable Actions, wall-clock, and attempt limits now and move model-token and pricing accounting to the deferred LLM cycle.
VERDICT: BLOCKED — 23 open finding(s)


### Visionary's read (pending adjudication)

Round 1 returned `VERDICT: BLOCKED — 23 open finding(s)`: 20 BLOCKER, 2 MAJOR, 3 KILL
(diagnostics-branch topology, unactionable-incident dispatch, model-token accounting).
Usage: 7,664,603 input tokens (7,397,888 cached), 58,819 output, 48,087 reasoning.
Per-finding verdicts are recorded in round 2's prompt, not here.
