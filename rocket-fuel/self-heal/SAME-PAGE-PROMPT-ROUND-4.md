# Same-Page Prompt — Round 4

Round 4. Read rocket-fuel/self-heal/PLAN.md (Revision 5), VTO.md (Revision 5) and DEFERRED.md
(Revision 5). All three were rewritten — your round-3 new BLOCKER (governing documents define
different products) was correct: VTO and DEFERRED still described the deleted hosted design.
They now describe only what ships.

Four of your round-3 findings were regressions Revision 4 introduced by over-shortening rather
than deliberate cuts, and are restored: self-origin provenance filtering (finding 16), the
complete unredacted private crash archive (finding 5), and the two document-drift items.

Specifications you asked for are now written rather than gestured at:

- Finding 1: repair runs in a detached `git worktree` at origin/main under a minimal
  environment (PATH/HOME/LANG only), never the operator's dirty checkout.
- Findings 2 and 21: ONE authoritative `state.json` under ONE lock, mutated by
  read/validate/mutate/temp-write/fsync/os.replace/fsync-dir; `incidents.jsonl` demoted to an
  anchored-chain journal so valid-prefix truncation is detectable; explicit PR-create intent
  journaling with `gh pr list --head` recovery.
- Finding 3: the consecutive/any contradiction resolved to ANY N failures within the epoch;
  key loss now FAILS CLOSED with FINGERPRINT_KEY_UNAVAILABLE and a human --adopt-new-key that
  does not reset the epoch counter (your round-3 Q2 answer).
- Finding 4 and your Q3: one externally enforced whole-tick `setitimer(ITIMER_REAL)` deadline
  bounding the coordinator, not just source subprocesses; killpg for collectors hung before
  their own deadline check; a TTL on the single-run lock so a hung observer cannot suppress
  later ticks.
- Finding 7: the clock is anchored to state.json's own max observed_at with a bounded forward
  band; outside it, repair refuses but OBSERVATION STILL RUNS because an implausible clock is
  itself evidence; reboot detected via kern.boottime.
- Finding 10: one widened regex covering acct/account/broker/robinhood/alpaca/rh with 0-4
  separators AND the camel/snake no-separator forms you named.
- Findings 6, 14, 17, 19, 22: numeric Actions ceiling with calendar-month period and async
  settlement; discovery_cursor advancing independently of archive_cursor; the full
  merge/close/cancel/CI-red/lease-expiry state machine with separate execution and review
  leases; missing-required-field and missing-packaged-config validation; and every proof is
  now a literal executable command with no placeholder (Rock 7's packaging proof installs
  into a temp venv and runs from /tmp so an in-tree package cannot shadow the wheel, and it
  loads config to prove package data shipped).

Also: [SUITE] is green. The failures you reproduced came from this cycle's own tracked
PLAN-rev2.md.bak; the .bak snapshots and Codex transcripts are now gitignored and untracked,
and .selfheal_fingerprint_key is genuinely in .gitignore now rather than merely claimed.

For each carried finding plus your round-3 new BLOCKER, state RESOLVED, RESOLVED-BY-SCOPE, or
STILL OPEN with the same [SEVERITY] short-title label. Judge the plan AS SCOPED. Where a
finding is irreducible for a single-operator local daemon, say so and mark it
ACCEPTED-RESIDUAL rather than holding it open forever — a plan cannot be blocked on a property
no implementation of this product could have. Answer the three open questions at the end of
Revision 5 directly.

End with exactly one line, and make it the last line: `VERDICT: SAME PAGE` if you have zero
open FIX or KILL items, otherwise `VERDICT: BLOCKED — N open finding(s)`.
