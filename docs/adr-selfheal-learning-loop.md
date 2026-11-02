# ADR: A learning loop for self-healing, scored in its own DuckDB file

- **Status:** Proposed (2026-11-02). Operator request, 2026-11-01: put the self-healing part of
  the program on its own learning and improvement loop, as its own function, using DuckDB.
  Open questions carry labeled proposed defaults.
- **Scope:** design only. The stdlib decision core in `selfheal/`, `state.json`, the
  `.github/workflows/self-heal.yml` pipeline and every trading path are untouched. Later PRs add
  a research-tree ingest and scoring package; the decision core only ever receives plain
  summaries from it.

## Context

Self-healing today has three parts:

- `selfheal/notion_issue_loop.py`: the decision core over the Notion DCA Issues database. It
  selects an issue with a written remediation, dispatches one repair branch, reads the PR outcome
  back, and later checks whether the fix stopped holding (`plan_regression_check`). Attempts are
  charged at dispatch and a refused queue latches a breaker.
- `selfheal/triage_reconcile.py`: matches issue rows to git evidence, propose-only.
- `rocket-fuel/self-heal/PLAN.md` and `VTO.md`: bounded daily observation (HMAC fingerprints,
  `state.json` as the only admission input) plus one repair adapter, lint/format.

What is missing is memory of *how well* repairs work. The loop knows whether one PR merged and
whether one error code came back. It cannot answer: which repair kinds hold, which regress,
which incident classes recur after a "fix", how long repairs take, or whether a new adapter is
good enough to open PRs on its own. That is the same gap the trading Phase 0 ledger closes
(`docs/adr-outcome-ledger-pit-harness.md`), so this ADR reuses its design.

## Decision (proposed)

1. **Its own loop, its own file.** A separate DuckDB file, `selfheal.duckdb`, under the
   self-heal state directory (path from configuration, never hard-coded). It shares no tables
   with the trading ledger, so neither loop can read or corrupt the other's scores.
2. **Same storage rules as the trading ledger.** Append-only, no `UPDATE` or `DELETE`; every row
   carries `source, as_of, inputs_known_at, run_id` and a `content_hash` key. Fingerprints are
   the existing HMAC fingerprints; raw crash text stays in the private archive, never in DuckDB.
3. **Tables.**
   - `incidents`: one row per fingerprint first seen (fingerprint, source, error code, class).
   - `occurrences`: every sighting, from the Run Log, CI failures, shard logs and
     `cron_tracking/` artifacts.
   - `repair_attempts`: adapter, adapter version, branch, PR number, charged at, mode
     (`shadow` or `pr`).
   - `repair_outcomes`: per attempt, scored at fixed horizons (+7 and +30 days): merged, main CI
     green, fingerprint recurred, regressed. A missing input is `unscored`, never "held".
   - `adapter_trials`: every adapter version ever evaluated, with its metrics, so promotion
     counts every trial.
4. **Scores.** Per adapter: held rate, regression rate, median time to repair, refused rate. Per
   incident class: recurrence after repair. Shadow attempts are scored on whether the proposed
   patch would have passed the same checks.
5. **Promotion and demotion.** A new adapter starts in **shadow** mode: it writes a patch file
   and scores, and opens no PR. It is proposed for promotion to PR mode only after enough shadow
   repairs held. A promoted adapter is proposed for demotion when its regression rate rises.
   Promotion and demotion are operator decisions recorded in the ledger. Nothing auto-merges,
   ever.
6. **Boundary.** DuckDB is a third-party package, so ingest and scoring live in the research
   tree (proposed: a new `selfheal-ledger/` sibling with its own `requirements.txt` and tests).
   `selfheal/` stays stdlib-only and receives scores as plain mappings, the injected pattern it
   already uses for Notion rows. A missing or stale score file means "no change to admission"
   (fail closed).
7. **Review.** One section in the Friday digest: adapters, held and regression rates, proposed
   promotions and demotions, and incident classes with no adapter yet.

## Open questions

| # | Question | Proposed default |
|---|---|---|
| 1 | Where the ledger code lives | New `selfheal-ledger/` sibling subproject |
| 2 | Horizons for "held" | +7 and +30 days after merge |
| 3 | Promotion bar for a shadow adapter | At least 10 shadow repairs, held rate at least 90%, zero regressions |
| 4 | Demotion trigger | Regression rate above 10% over the last 10 PR-mode repairs |
| 5 | Where it runs while GitHub Actions is billing-blocked | On the Mac, through `scripts/local_ci.sh` hooks or a launchd job, read-only to trading state |
| 6 | Which adapter to trial next after lint/format | Decide from the first month of incident-class recurrence data |

## Consequences

- The self-heal loop gains a measurable track record per adapter, which is what makes adding
  adapters safe.
- One more research subproject to maintain, with its own dependency (DuckDB, already pinned in
  `dca-sizing-signals`).
- No change to what can merge or to who decides: the operator still approves promotions.

## Verification

- A test proves `repair_outcomes` never marks an attempt "held" when the +7 or +30 day
  occurrence data is missing.
- A test proves the decision core's admission is unchanged when the score file is absent,
  stale or malformed.
- A shadow run over the last 30 days of real incidents produces scores before any adapter is
  promoted.

## Rollback

Revert the PRs and delete `selfheal.duckdb`. The existing loop keeps working unchanged.
