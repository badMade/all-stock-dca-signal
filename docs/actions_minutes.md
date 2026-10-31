# Actions minutes and local CI

On 2026-10-31 every GitHub Actions job in this repository stopped starting: the check-run
annotation read "recent account payments have failed or your spending limit needs to be
increased". This page records where the minutes were going, what was changed, and how to run
the same gates on this Mac instead.

## Where the minutes went (measured)

Sampled from the Actions API for 2026-10-24 20:37Z → 2026-10-31 04:20Z (6.3 days, the most the
runs API returns: 1,000 runs). Minutes are per job, rounded up to the minute the way GitHub bills
them, averaged over the 10-12 most recent finished runs of each kind.

| Workflow | Trigger | Runs | Billed runs | Avg min/run | Min in window | Share |
| --- | --- | ---: | ---: | ---: | ---: | ---: |
| CI | push to `main` | 76 | 76 | 59.1 | ~4,490 | 61% |
| CI | pull_request | 286 | 252 | 10.6 | ~2,670 | 36% |
| Sync Labelled PR Branches | push to `main` | 109 | 109 | 1.0 | ~109 | 1.5% |
| Refresh Generated Docs | push to `main` (+ weekly) | 64 | 64 | 1.0 | ~64 | 0.9% |
| Drift detector self-test, planner repaste, self-heal | schedule / push / workflow_run | ~320 | ~15 | 1.0 | ~15 | 0.2% |
| Claude Code Review | issue_comment | 82 | 0 | – | 0 | 0% |
| CapEx scan, compute-schedule | weekly / monthly | 0-1 | – | – | – | ~0% |

Total ≈ 7,350 minutes in 6.3 days, i.e. **~35,000 Linux minutes a month**, against a private-repo
allowance of 2,000 (Free) or 3,000 (Pro). (Self-heal fires on every CI completion but its job is
`if:`-skipped unless main failed, and skipped jobs are not billed.)

Inside a push-to-main CI run, per job: core DCA 3.11 ≈ 19.4 min, core DCA 3.12 ≈ 21.9 min,
wondrous-octopus ≈ 10.4 min (the CPU torch install and training tests), and eight one-minute jobs
(changes, dev audit, research audit ×2, the other six siblings). On a PR the core 3.12 leg
averaged 6.6 min. There are no required status checks: the only ruleset (`protection`) has no
rules, and `main` has no branch protection, so skipping a job blocks nothing.

## What changed in `ci.yml`

PR #2067 (the base of this change) makes draft PRs skip CI: `pull_request` also fires on
`ready_for_review`, and the root jobs skip while the PR is a draft. This change adds:

1. **Pushes to `main` run 3.12 only.** The 3.11 leg moves to a new weekday scheduled run
   (`cron: "0 10 * * 1-5"`, 06:00 ET) and `workflow_dispatch`, which run the whole matrix, every
   sibling, and the research audit. Self-heal still reacts: the scheduled run is a CI run on
   `main`, and its scope pattern (`^Lint & test \(core DCA\)`) matches the 3.11 leg too.
2. **Pushes to `main` path-filter sibling projects.** The `changes` job now diffs the pushed range
   (`compare/$BEFORE...$AFTER`) the way it already diffed PRs. It fails open: an API error, a
   300-file (truncated) listing, a new branch, a change to `requirements-dev.txt` or `ci.yml`, or
   any non-push/non-PR event runs everything.
3. **The research audit runs only when a dependency manifest moved** (`requirements*.txt`,
   `uv.lock`, `pyproject.toml`), plus on every scheduled/manual run, which is where a new advisory
   against unchanged pins gets caught.
4. **Every job's runner is a variable**: `runs-on: ${{ vars.CI_RUNS_ON || 'ubuntu-26.04' }}`.
   Unset, nothing changes. Set it to a self-hosted runner's label and the whole workflow stops
   using GitHub-hosted runner minutes (see below for the pricing caveat).

`scripts/tests/test_workflow_policy.py::test_ci_minute_budget_triggers` pins all four.

Estimated effect of 1-3 alone: ~4,500 min/week (PR ~2,170, push ~1,900, schedule ~300, other
workflows ~190), a ~38% cut. #2067's draft skip removes most of the PR share, since nearly every
PR here spends its life as a draft. Together that is roughly ~2,500 min/week (push ~1,900,
schedule ~300, ready PRs and other workflows ~300), still over a monthly allowance. 4 (self-hosted)
plus running the gates locally is the real fix.

Not changed, and why: `Sync Labelled PR Branches` and `Refresh Generated Docs` are about 2.5% of
minutes together; the other workflows are ≤1 run a week. No workflow was disabled or deleted.

## Recommendation

1. **Run `scripts/local_ci.sh` before pushing** (below). It is the same gate set as CI, takes
   about as long as the PR leg did, and costs nothing.
2. **Register a self-hosted runner on this Mac and set `CI_RUNS_ON`**, so the existing workflows,
   self-heal, and PR checks keep running unchanged without using GitHub-hosted runner minutes.
   **Check current pricing before relying on this being free:** in December 2025 GitHub announced
   a per-minute platform charge for self-hosted runners on private repositories, then postponed
   it, so whether self-hosted minutes cost anything (and count toward the spending limit) depends
   on GitHub's pricing page at the time you set this up, not on this doc. It is also not
   documented clearly whether a *failed-payment* lock stops self-hosted jobs. Check with one
   `workflow_dispatch` after setting the variable.
3. **If you stay on GitHub-hosted runners**, the biggest remaining lever is the push-to-main
   core run (~22 min × ~76 pushes a week). The PR already ran the same 3.12 suite, so a push run
   mostly re-proves a merge. Dropping it in favour of the weekday scheduled run would save another
   ~1,700 min/week. But self-heal reacts to push-run failures within minutes, and that would then
   wait up to a day. That is your decision, so it is not in this change.

A pre-push hook is available but opt-in (below): `core.hooksPath` is shared by every worktree,
so turning it on makes every agent session's push wait for the quick gates too.

For running `ci.yml` itself locally in Linux containers (act + Colima, `scripts/run_ci_locally.sh`),
see [local_ci.md](local_ci.md). The two tools complement each other: `local_ci.sh` is the fast copy
of the steps, while `run_ci_locally.sh` runs the real workflow file.

## `scripts/local_ci.sh`

(Rewritten for full CI parity in PR #2067; summarised here.)

```bash
scripts/local_ci.sh              # ruff, coverage-gated pytest, scripts/tests, dry-run demo
scripts/local_ci.sh --quick      # same minus the full coverage run (~1 min)
scripts/local_ci.sh --changed    # + sibling projects changed vs origin/main (ci.yml's rule)
scripts/local_ci.sh --all        # everything the scheduled CI run does
```

| CI job | Local equivalent |
| --- | --- |
| Lint & test (core DCA) (3.12) | default run (ruff, `pytest --cov=. --cov-fail-under=90`, demo) |
| Lint & test (core DCA) (3.11) | `--py311` (uv-managed env, Python 3.11) |
| Dependency & secret audit | `--audit` (`pip-audit --requirement requirements-dev.txt --strict`) |
| Dependency audit (research) ×2 | `--audit` (the same `uv export` / `uv pip compile` / `pip-audit --locked` sequence, for 3.11 and 3.12) |
| Lint & test (\<sibling\>) | `--changed`, `--all`, or a project name; each runs in a uv-managed env built from the same requirement files and Python version as its CI job |
| *(not in CI)* | `scripts/tests` (incl. the workflow-policy tests), always run |

The core interpreter is `$PYTHON`, else `./.venv`, else the main checkout's `.venv` (worktrees
under `.claude/worktrees/` have none of their own). `--py311`, `--audit` and the siblings need
`uv` and network on first use. The main difference from CI is the OS: macOS rather than Ubuntu.

### Reporting the result to GitHub

```bash
scripts/local_ci_status.sh --all     # any local_ci.sh flags pass straight through
```

This runs `local_ci.sh` and posts its real result as a `local-ci` commit status on the commit it
tested: `pending` at the start, then `success` only on exit 0, `failure` otherwise, and `error` if
interrupted. PRs then show a green or red local check while Actions is skipped (drafts) or
unavailable (the billing block). It refuses a dirty tree, since the status would describe code
that isn't the commit, and refuses a commit GitHub doesn't have yet (push first).
`$LOCAL_CI_CONTEXT` renames the context. It is a plain commit status: no ruleset requires it, and
making it required would be a ruleset change for you to decide.

### Optional pre-push hook

```bash
git config core.hooksPath scripts/git-hooks   # on; affects every worktree of this clone
git config --unset core.hooksPath             # off
```

`scripts/git-hooks/pre-push` runs `scripts/local_ci.sh --quick`; `git push --no-verify` skips it.

## Self-hosted runner on this Mac

1. GitHub → repository Settings → Actions → Runners → *New self-hosted runner* → macOS / ARM64,
   and follow its download + `./config.sh` steps, accepting the default `self-hosted` label.
2. **Run it as a separate, unprivileged macOS user**, not your own account. PR branches (including
   those that Jules, Copilot, Codex and Claude push) execute arbitrary code on the runner. Your account holds
   the `.env` with `ROBINHOOD_AGENTIC_ACCOUNT_NUMBER`, the shared `cron_tracking/` tree, and the
   agent sessions with Robinhood MCP access. The repository is private, so fork PRs from
   strangers are not a concern, but agent-authored branches are.
3. Install it as a service (`./svc.sh install && ./svc.sh start`) so it survives logout.
4. `gh variable set CI_RUNS_ON --body self-hosted`, then `gh workflow run CI` once to confirm.
   `gh variable delete CI_RUNS_ON` switches back to GitHub-hosted runners.

One runner runs one job at a time, so a full scheduled run (≈ 60 job-minutes) serializes. The
other workflows still use `ubuntu-latest` (≈ 190 min/week, inside the free allowance); point
them at the same variable if you want them off GitHub-hosted runners too.
