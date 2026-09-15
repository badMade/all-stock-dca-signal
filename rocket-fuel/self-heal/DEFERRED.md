# Deferred backlog — `rocket-fuel/self-heal`

Items raised during this cycle and deliberately left out of it. **Revision 5** — rewritten to
describe the cut scope; earlier revisions described the deleted hosted design (round 3 new
BLOCKER: "governing documents define different products").

## Deferred because the source cannot yet be observed

- **Incremental persistence in `dca_console_jobs.py`.** The console writes an initial queued
  record and a terminal record, holding running output in memory in between. A hard crash
  (SIGKILL, power loss, OOM) therefore leaves an empty queued record and no output, so the
  observer has nothing to collect — this is why the VTO says *bounded observation of persisted
  sources* rather than "universal". Fixing it means changing the console's job writer to flush
  bounded output incrementally and atomically. Out of scope here because it modifies a
  different subsystem with its own test suite; the observer treats a stale in-flight record as
  a durable failure record in the meantime, which detects *that the job died* but not *why*.

## Deferred capability losses from deleting `.github/workflows/self-heal.yml`

- **Stale self-heal PR cleanup.** The deleted workflow closed `github-actions[bot]`-authored
  `selfheal-repair-*` PRs older than 7 days by `updatedAt`. The local repair path opens far
  fewer PRs, so this is not yet needed; revisit if repair PRs accumulate.
- **Hosted 14-day Actions log upload.** Superseded rather than lost: the private crash archive
  is durable and does not expire.

## Deferred because the repair path is deliberately narrow

- **Adapters beyond lint/format.** Every other incident class — including the shard-1 MPS
  `NotImplementedError` — is report-only by construction this cycle. Adding an adapter means
  adding a repair *and* an incident-specific `verify()` that proves that incident is gone; a
  repair that cannot verify its own success is worse than none.
- **Agent/LLM-driven repair of core trading logic**, and with it the token/USD price table.
  The reservation mechanism is built generically, so a cost-metered adapter has a slot waiting.
- **Auto-merge of any repair.** Operator decision: this repository drives live trading.
- **Repairing the sibling research suites** (`production-safe-agent-loop/`, `ticker-advisor/`,
  `ml-forecaster/`, `wondrous-octopus/`, `ticker-market-breadth/`). Their heavy dependencies
  are not installable in the observation budget.

## Deferred hardening

- **Multi-machine coordination.** One operator, one Mac. The single-writer assumption is what
  makes the ledger transaction tractable; it must be revisited before a second dispatcher ever
  exists.
- **Signed or attested provenance.** Self-origin exclusion uses the ledger's own record of
  which runs and PRs self-heal created. That is trustworthy because there is one writer; it is
  not cryptographic attestation.
- **`pytest.importorskip("jsonschema")` in `tests/test_config_invariants.py`.** On a machine
  without `jsonschema` the whole-repo schema gate silently skips. Pre-existing, not introduced
  here, but worth closing.
