# PLAN.md — Rocket Fuel Audit & Refactor, cycle 1

Six Rocks selected from `ISSUES.md`, covering all four themes the repo owner
picked: cap-tracking duplication, merge/CI process safety, security hardening,
and cleanup/coverage gaps. Each Rock is scoped to avoid the larger, riskier
consolidations `CLAUDE.md` and `docs/reliability_spec.md` explicitly warn
against — see `DEFERRED.md` for what was deliberately left out and why.

## Rock 1 — "One List of Roots" (dedupe `cap_seed_roots`)

**Scope:** Three near-identical functions hand-enumerate the same six
sibling-stream artifact roots in different orders:
`morning_dca.py:493` `default_pooled_cap_seed_roots()`,
`market_open_dca.py:186` `default_market_open_cap_seed_roots()`,
`intraday_dca.py:194` `default_intraday_cap_seed_roots()`
(`midday_dca.py` already inherits `MorningConfig`'s default — 3 of 4 streams
duplicate). Missing a stream when adding one silently under-seeds that
stream's cap. Introduce one shared `default_all_stream_cap_seed_roots()` in
`morning_dca.py` (every other stream module already imports from it — zero new
import edges, no cycle risk). The three existing functions become thin
wrappers/re-exports so existing call sites and tests keep working.

**Non-goals:** Do not touch `StrategyState`, `BudgetSnapshot`, or
`MorningLedger` — three genuinely different cap-tracking data models by
design. This Rock only deduplicates the *list of roots*. Do not touch
`prior_allocation_roots` (a separately-scoped, intentionally narrower list).

**Proof command:**
```bash
.venv/bin/python -m pytest tests/test_morning_dca.py tests/test_market_open_dca.py tests/test_intraday_dca.py tests/test_midday_dca.py -q
```
plus a new test in `tests/test_morning_dca.py` that asserts exact ordered-tuple
equality against an explicit, hardcoded canonical list — not mere set equality
among the three functions, which would still pass if all three drifted
together to the same wrong list:
```python
def test_all_streams_share_identical_cap_seed_roots():
    from morning_dca import default_pooled_cap_seed_roots
    from market_open_dca import default_market_open_cap_seed_roots
    from intraday_dca import default_intraday_cap_seed_roots
    expected = tuple(stream_root(name) for name in (
        "market_open_dca", "morning_dca", "midday_dca", "intraday_dca",
        "ce16ba6b", "final_manual_review",
    ))
    assert default_pooled_cap_seed_roots() == expected
    assert default_market_open_cap_seed_roots() == expected
    assert default_intraday_cap_seed_roots() == expected
```
(Round 1 Same Page finding, FIX: set equality permits duplicates and cannot
prove the canonical six roots are actually present — resolved by asserting
against an explicit expected tuple instead.)

**Round 2 finding (FIX):** the exact-tuple assertion above conflicts with
`_append_dated_manual_override_cap_seed_roots` (`morning_dca.py:558`), which
globs `cron_tracking_root()` for `perplexity_primary_*/manual_override_*`
directories and appends any it finds — in an environment where such
directories exist (e.g. a real, non-isolated `$DCA_CRON_TRACKING_ROOT`), the
returned tuple would have more than 6 entries and the test would fail for
reasons unrelated to the drift it's meant to catch. Fix: monkeypatch
`morning_dca._dated_manual_override_cap_seed_roots` (the glob helper it calls
internally) to return `()` for this test, so the exact-tuple assertion checks
only the static canonical list, independent of what override directories
happen to exist on the machine running the suite.

## Rock 2 — Pin (don't unify) the two reconciliation implementations

**Scope:** `near_close_dca.py:2742` `StrategyState.reconcile_prior_allocation()`
and `morning_dca.py:3690` `_compute_top_up()`/`TopUpOutcome` (`morning_dca.py:589`)
both solve "idempotently top up a ticker's already-bought-today amount," but
are not directly comparable — `reconcile_prior_allocation` is a pure in-memory
method taking a cumulative `(amount_usd, attempt_count, order_ids)` triple;
`_compute_top_up` requires a fully wired `MorningDCAExecutor` and derives its
prior amount by scanning the filesystem across `cap_seed_roots`. Unifying them
is out of scope.

New `tests/test_reconciliation_parity.py`, parametrized over 4 scenarios
(fresh day, partial fill needing top-up, fully allocated, multiple orders for
one ticker), driving both implementations from the same
`(amount_usd, order_id)` order history (reuse the existing
`_write_prior_run_state` fixture pattern from `tests/test_morning_dca.py`).
Per Round 1 Same Page finding (FIX — "comparing two stored prior-amount fields
omits incremental top-up decisions, attempt counts, and order-ID behavior
central to the claimed relationship"), the original intent was to assert on
attempt-count and order-ID-absorption fields on both sides in addition to the
amount. **Round 2 finding (FIX, scope reduction):** confirmed by reading
`TopUpOutcome` (`morning_dca.py:589`) directly — it has no attempt-count or
order-ID field at all (only `prior_same_day_amount`,
`prior_same_day_run_id`, `prior_same_day_source`, and a few pricing/guard
fields). Asserting parity on fields one side doesn't expose would mean
reaching into `_compute_top_up`'s internal loader state
(`load_prior_same_day_allocations_multi`'s return value) rather than its
public result — a bigger, more invasive test than a scoped Rock should be.
Reduced scope: the parity test asserts equivalence/documented-divergence on
`StrategyState.seeded_prior_context_amount_by_ticker_day[ticker][day]` vs.
`TopUpOutcome.prior_same_day_amount` only, across the 4 scenarios. The
Non-goals below make this reduction explicit so it isn't mistaken for the
originally-promised broader parity.

**Non-goals:** No logic changes to either implementation. Test-only. Does not
assert parity on attempt counts or order-ID absorption — `TopUpOutcome`
doesn't expose that granularity, and reaching past its public result into
`_compute_top_up`'s internal loader state to manufacture a comparison would
turn a scoped test-only Rock into something closer to an implementation
change. If attempt-count/order-ID parity matters, that's a separate,
larger-scoped future Rock (add it to `DEFERRED.md` at execution time).

**Proof command:**
```bash
.venv/bin/python -m pytest tests/test_reconciliation_parity.py -q
```

## Rock 3 — Large-deletion CI guard (replaces the originally-proposed canary test)

**Round 1 Same Page finding (KILL the original design):** the plan originally
proposed a new `tests/test_protected_surface.py` smoke test asserting the SSRF
guard symbols still exist. Codex correctly killed this: a stale-branch clobber
that deletes `dca_net_utils.py` would just as easily delete a same-repo canary
test file in the same commit — it adds no protection beyond what already
exists today (any of the several *other* modules/tests that import
`dca_net_utils` already fail on import the instant it's deleted). A test whose
survival depends on the same diff it's meant to guard against is not a guard.

**Round 2 finding (BLOCKER, my response: ACCEPT with justification, not
further redesign):** Codex correctly points out that a check defined inside
`ci.yml` on the PR branch can be omitted or modified by the very same
stale/bad branch it's meant to catch, so it cannot be a hard guarantee
independent of the PR's own contents — only a GitHub branch-protection
required-status-check (enforced from the default branch's settings,
independent of what a given PR's workflow file says) closes that gap fully.
That's a repository-settings change, not a code change, and per my own
operating rules it needs your explicit action/permission (it modifies shared
GitHub infrastructure) — it is not something I will fold into an
autonomously-built Rock. Both real incidents in this repo's history
(PR #658, and the second near-miss) were accidental, not adversarial, so I'm
keeping this Rock as a **best-effort, defense-in-depth detector** rather than
killing it outright: it would have caught both actual past incidents (see
findings 6/7 fixes below) even though it cannot mathematically guarantee
catching a deliberately-evasive future one. **I'm recommending, not
performing, the branch-protection step** — see "Recommended follow-up" below;
tell me if you want me to attempt it via `gh api` and I'll treat that as its
own explicit-permission action.

**Revised scope (also fixes Round 2 findings 6 and 7):** Add a fast, early
step to `.github/workflows/ci.yml` that runs on `pull_request` with
`actions/checkout`'s `fetch-depth: 0` (non-shallow, needed for merge-base) and
does two independent checks against `git merge-base origin/${{ github.base_ref }} HEAD`:

1. **Protected-path deletion check** (fixes finding 6 — a threshold alone
   would miss the ~372-line `dca_net_utils.py` near-miss): `git diff
   --name-status <merge-base> HEAD` over a short curated list of load-bearing
   files (`dca_net_utils.py`, `near_close_dca.py`, `dca_runner.py`,
   `morning_dca.py`, `market_open_dca.py`, `midday_dca.py`,
   `intraday_dca.py`, and their `tests/test_*.py` counterparts) — fail
   immediately if any show status `D` (deleted), regardless of overall diff
   size.
2. **Absolute large-deletion threshold** (fixes finding 7 — "net" deletions
   is ambiguous and gameable by padding with insertions): parse the
   `deletions(-)` count specifically from `git diff --shortstat <merge-base>
   HEAD` (the absolute number of deleted lines, not deletions-minus-insertions),
   and fail if it exceeds **1,500** — calibrated against this repo's own
   history (`git log --shortstat -200`, largest legitimate single-commit
   deletion found was ~1,090 lines from the 2026-08-23 `tests/` reorg) so
   ordinary refactors pass but PR #658's ~150k-line revert would not.

Allow an explicit override via a PR label (e.g. `large-deletion-ok`) for
intentional large/protected-path deletions (e.g. archiving the dead Polygon
subsystem per `DEFERRED.md`).

**Non-goals:** Not a GitHub branch-protection/required-status-check
configuration change (see above — recommended, not performed, this cycle).
Not claimed to be adversarially bypass-proof — explicitly framed as
defense-in-depth against accidental large reverts, the failure mode actually
observed twice in this repo's history.

**Recommended follow-up (for you to action, not this Rock):** once this check
exists on `main`, mark it as a required status check in the repo's branch
protection settings. A required check enforced at the GitHub-settings level
still blocks a merge even if a PR's own branch tries to remove or alter the
check — closing the exact gap this Rock's code-only version cannot.

**Proof command:**
```bash
python -c "import yaml; yaml.safe_load(open('.github/workflows/ci.yml'))"
# Simulate both checks locally against this branch's own diff from main:
git fetch origin main
git diff --name-status $(git merge-base origin/main HEAD) HEAD -- dca_net_utils.py near_close_dca.py dca_runner.py morning_dca.py market_open_dca.py midday_dca.py intraday_dca.py tests/test_dca_net_utils.py tests/test_near_close_dca.py tests/test_dca_runner.py tests/test_morning_dca.py tests/test_market_open_dca.py tests/test_midday_dca.py tests/test_intraday_dca.py
git diff --shortstat $(git merge-base origin/main HEAD) HEAD
```
Full confidence requires a real PR run (a deliberate small test PR with a
large/protected-path deletion) to confirm the new job actually fails as
intended — flag this explicitly rather than claiming local simulation proves
the CI logic works end to end.

## Rock 4 — Structural test: every `build_opener()` call carries the safety handlers

**Scope:** `dca_net_utils.install_opener()` patches the global default opener
at import time, but callers building their own opener (`dca_alerts.py:175,308`,
`scripts/drift_detection.py:1259`) must remember to pass
`PinnedHTTPHandler, PinnedHTTPSHandler, BlockedFileHandler, BlockedFTPHandler`
themselves — the exact class of bug `.jules/sentinel.md` documents recurring
in `strategy_monitor.py`/`drift_detection.py` before centralizing. Both
current core call sites already do this correctly, but nothing pins the
invariant going forward. New `tests/test_network_guard_coverage.py`: parse
(via `ast`) every in-scope core `.py` module (excluding `tests/`,
`trading-strategies/`, `analysis/`, `ticker-advisor/`, `ml-forecaster/`,
`wondrous-octopus/`, `ticker-market-breadth/`, `production-safe-agent-loop/` —
matching the existing ruff/coverage exclusion list) for
`urllib.request.build_opener(...)` call sites, and assert each one's arguments
include the four safety handler classes.

**Non-goals:** Does not touch `ticker-advisor/signals.py`'s `requests.get`
calls — that subproject has its own deps/conventions and is out of core scope.

**Proof command:**
```bash
.venv/bin/python -m pytest tests/test_network_guard_coverage.py -q
```

## Rock 5 — Stop 3 one-off scripts from riding on incidental coverage

**Scope:** `build_raw_data.py` and `snap.py` are pure hardcoded-literal data
dumps tied to specific historical dates, no functions; `run_market_open_signals.py`
is a manual investigation shim with side effects at module import time,
replaying one specific captured day's data through the real
`MarketOpenDCAExecutor.evaluate_signal()`. None have reusable logic worth unit
testing beyond re-asserting literal values. Add all three to `pyproject.toml`'s
`[tool.coverage.run] omit` list, following the existing comment convention
(see the `ticker-advisor/*` entry for style).

**Non-goals:** No changes to the scripts' own logic/output.

**Proof command:**
```bash
.venv/bin/python -m pytest --cov=. --cov-report=term-missing:skip-covered --cov-fail-under=90
```

## Rock 6 — Pin GitHub Actions by commit SHA, not mutable tag

**Scope:** All 6 workflow files under `.github/workflows/` reference actions
by mutable tag. Replace with commit SHAs (looked up via the public GitHub API,
current as of this cycle):

| Action@tag | SHA |
|---|---|
| `actions/checkout@v7` | `3d3c42e5aac5ba805825da76410c181273ba90b1` |
| `actions/setup-python@v7` | `5fda3b95a4ea91299a34e894583c3862153e4b97` |
| `actions/github-script@v9` | `373c709c69115d41ff229c7e5df9f8788daa9553` |
| `actions/download-artifact@v8` | `3e5f45b2cfb9172054b4087a40e8e0b5a5461e7c` |
| `actions/upload-artifact@v7` | `043fb46d1a93c77aae656e7c1c64a875d1fc6a0a` |

Replace every `uses: <action>@<tag>` with `uses: <action>@<sha> # <tag>` across
all 6 workflow files: `compute-schedule.yml`, `drift-check.yml`,
`antigravity-pr-review-post.yml`, `self-heal.yml`, `ci.yml`,
`antigravity-pr-review.yml`.

**Non-goals:** No changes to job logic, triggers, or permissions — pin syntax
only.

**Proof command:**
```bash
python -c "import yaml, glob; [yaml.safe_load(open(f)) for f in glob.glob('.github/workflows/*.yml')]"
grep -rn "uses:.*@v[0-9]" .github/workflows/   # must return nothing
```
Full confidence this didn't break anything only comes from a real CI run on
the eventual PR — local YAML parsing only proves syntax validity.

## Final acceptance criteria (Round 1 Same Page finding, FIX)

Per-Rock proof commands above are necessary but not sufficient. Before this
cycle is considered done, run `AGENTS.md`'s three standard pre-handoff checks
against the full working tree (not just the touched files):

```bash
ruff check .
.venv/bin/python -m pytest
python run_dca.py --demo --report --now 2026-09-15T15:45 --artifacts-dir /tmp/dca-demo
```

All three must pass (or any that cannot be run must be documented with why,
per `AGENTS.md` §6) before the final diff is presented for commit approval.
