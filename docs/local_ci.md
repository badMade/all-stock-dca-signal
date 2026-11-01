# Running CI locally

`scripts/run_ci_locally.sh` runs `.github/workflows/ci.yml` on the operator's Mac with
[act](https://github.com/nektos/act), in Linux containers under [Colima](https://github.com/abiosoft/colima).
It runs the real workflow file, so there is no second copy of the CI steps to keep in sync, and nothing is
billed to the GitHub account.

It was added on 2026-10-31, when a failed account payment stopped GitHub from starting any hosted Actions
job ("The job was not started because recent account payments have failed or your spending limit needs
to be increased"). Every PR check then failed in 2-3 seconds without running.

## Which local tool

Two scripts check a branch before GitHub does. They are complementary, not duplicates:

| | `scripts/local_ci.sh` | `scripts/run_ci_locally.sh` (this page) |
| --- | --- | --- |
| Runs | its own copy of `ci.yml`'s steps | `ci.yml` itself, through act |
| Where | the checkout's `.venv`, on macOS | Ubuntu 24.04 containers, as GitHub's `runner` user |
| Needs | nothing beyond the dev setup | act, Colima, Docker CLI (below) |
| Speed | fast: no image, no clone, `--quick` skips coverage | a full run is ~16 minutes, one job at a time |
| Can drift from CI | yes, when `ci.yml` changes and the script does not | no |

Use `local_ci.sh` while iterating, and `run_ci_locally.sh` before merging when GitHub cannot run the PR
(it matched GitHub exactly on PR #2066: 13,207 tests, 91.16% coverage). Neither turns a PR's failed hosted
checks green, but each can post its result as commit statuses beside them: `scripts/local_ci_status.sh`
(below) as one `local-ci` status for a `local_ci.sh` run, and `run_ci_locally.sh --report` as one
`local-ci (act) / <job>` status per `ci.yml` job plus an overall `local-ci (act)`. The context names differ,
so both can report on the same commit without overwriting each other.

## `scripts/local_ci.sh`

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

### Sweeping every open PR (`scripts/local_ci_sweep.py`)

`local_ci_status.sh` tests one commit. The sweeper runs it for all open PRs, unattended, on the Mac:

```bash
python3 -m scripts.local_ci_sweep --dry-run     # list the PRs it would test; runs nothing
python3 -m scripts.local_ci_sweep               # test up to 3 PRs with the full core gate (~14 min each)
python3 -m scripts.local_ci_sweep --mode quick   # lint, scripts/tests and demo only
python3 -m scripts.local_ci_sweep --mode changed --max-prs 5
```

- **Which PRs:** only branches of this repo, never forks (their code would run on the Mac). Drafts are
  included. A head commit that already has a `local-ci` `success` or `failure` is skipped; a new head
  commit, an `error` (interrupted run), or a `pending` older than 2 hours (a run that died) is tested.
- **Mode (changed 2026-11-01):** the default is the full core gate, the same checks as CI's core job. The
  first real sweep used `--quick` and posted `local-ci: success` on a PR with 14 failing root tests, because
  `--quick` skips the root suite. `--mode quick` stays available and warns about that in the dry run. Each PR
  takes about 14 minutes, so one sweep of 3 can pass the 30-minute timer; the lock prevents overlap.
- **Isolation:** each PR runs in a throwaway `git worktree` under `$TMPDIR/dca-local-ci-sweep`, never your
  checkout, and the worktree is always removed. One PR failing never stops the others.
- **Overlap:** a lock file (`$TMPDIR/dca-local-ci-sweep.lock`) stops two sweeps overlapping; a lock older
  than 4 hours is taken over.
- **Exit codes:** a failing CI shows in the commit status, not here. 1 means a PR could not be tested at all
  (git or the runner broke); 3 means another sweep holds the lock.
- **Schedule:** `deploy/launchd/com.all-stock-dca.local-ci-sweep.plist.template` runs it every 30 minutes
  (fill in `REPLACE_WITH_REPO_PATH`, copy to `~/Library/LaunchAgents/`, then
  `launchctl bootstrap gui/$(id -u) <plist>`). It needs `gh` logged in and uv on PATH; log at
  `/tmp/dca-local-ci-sweep.log`.

### Optional pre-push hook

```bash
git config core.hooksPath scripts/git-hooks   # on; affects every worktree of this clone
git config --unset core.hooksPath             # off
```

`scripts/git-hooks/pre-push` runs `scripts/local_ci.sh --quick`; `git push --no-verify` skips it.

## `scripts/run_ci_locally.sh` (act + Colima)

### Setup (once)

```bash
brew install act colima docker gh
gh auth login            # the script reuses gh's token; skip if already logged in
```

Nothing else. On the first run the script starts Colima (8 CPUs, 16 GB, 100 GB disk, Apple
Virtualization with Rosetta) and builds the runner image from `deploy/act/Dockerfile`: act's medium
Ubuntu 24.04 image plus the GitHub CLI, which the "Detect changed projects" job calls. The full act image
that already carries `gh` is about 20 GB, so it is not used.

### Use

```bash
scripts/run_ci_locally.sh                        # every job, as a PR run of this branch's open PR
scripts/run_ci_locally.sh -j test                # one job
scripts/run_ci_locally.sh --pr 2066 -j audit     # name the PR explicitly
scripts/run_ci_locally.sh --push                 # as a push to main: both Pythons, every sibling job
scripts/run_ci_locally.sh --report               # also post each job's result to GitHub (see below)
scripts/run_ci_locally.sh -l                     # list job ids
```

Job ids are `changes`, `test`, `audit`, `research-audit`, and one per sibling project
(`production-safe-agent-loop`, `ticker-advisor`, `ml-forecaster`, `wondrous-octopus`,
`ticker-market-breadth`, `llm-providers`, `yfinance-prefetch`). Any other argument goes straight to act,
for example `--matrix python-version:3.11`.

Exit status is act's: 0 when every job that ran succeeded.

### What to know

* **It runs your working tree, from a clean clone.** The script clones this checkout's HEAD into a temporary
  directory, applies uncommitted edits and copies untracked (not ignored) files on top, and runs act there.
  act copies whatever directory it runs in, and a linked worktree's `.git` is a file naming a host path the
  container cannot see: run directly, every `git` call exited 128 and `from.py` looked for the main
  checkout's `.venv` (14 of the first run's 17 failures). To check a PR, check out its branch first.
* **The snapshot keeps one loose ref** (`refs/heads/act-ci-snapshot`, 2026-10-31). A fresh clone packs every
  ref, act's copy into the container drops the empty `.git/refs`, and git there then reports "not a git
  repository": 12 tests that call git failed under act only.
* **Jobs run as `runner` (uid 1001), not root,** as on GitHub. Root ignores file modes, so the tests that
  prove a read-only directory is refused failed locally as root.
* **A PR run needs the PR number.** The `changes` job lists the PR's files through the GitHub API to decide
  which sibling jobs run. With no `--pr`, the script uses this branch's open PR; with no open PR, pass
  `--pr N` or use `--push`.
* **`--report` tells GitHub.** With it, the script posts a commit status for every job, named
  `local-ci (act) / <job name>`, plus an overall `local-ci (act)` that is `pending` while the run is going
  and then `success` or `failure`. They show in the PR's checks list beside the hosted Actions checks. Those
  hosted checks still read failed: a status cannot change an Actions check run, and if branch protection
  ever requires the hosted checks, these statuses do not satisfy it. `--report` refuses a dirty tree (the
  status must describe exactly one commit), a HEAD that is not on GitHub yet, and any act argument such as
  `-j test` (one passing job would otherwise post an overall success). A run that is interrupted or errors
  after posting `pending` marks the overall status `error` rather than leaving it pending. A matrix leg is reported
  with act's `-1`/`-2` suffix, in matrix order (for research audit: `-1` is 3.11, `-2` is 3.12).
  Without `--report`, results stay in the terminal.
* **The token is gh's.** `gh auth token` is handed to act as the `GITHUB_TOKEN` secret and never printed.
* **The Docker socket is not mounted** (`--container-daemon-socket -`). act's default bind-mount fails under
  Colima and no job here starts containers of its own.
* **One run per Mac.** act names each job's container and volumes after the workflow and job, not the
  checkout, so two runs at once (two sessions, two worktrees) delete each other's containers mid-test: on
  2026-10-31 that killed a core-test run with exit 137 and failed another at setup with
  "...-env: no such volume". The script takes a lock in `~/.cache/all-stock-dca/act.lock` and waits for
  any other run of it to finish, for at most an hour (`ACT_LOCK_WAIT_SECONDS`), then exits 4; a lock whose
  process is gone is cleared.
* **Jobs run one at a time** (`--concurrent-jobs 1`). act shares one Python tool cache across jobs, and on
  the first run the audit job's pip upgrade broke the 3.12 research-audit leg that was using the same
  Python at that moment. A full run is therefore slower than on GitHub; `-j <job>` runs just one.
* **arm64, not amd64.** Jobs run as native `linux/arm64`. `actions/setup-python` downloads arm64 builds;
  a dependency with no arm64 wheel would fail here and not on GitHub.
* **Colima keeps running** after a run. Stop it with `colima stop` to free its memory; the script starts it
  again when needed. The runner image is rebuilt automatically whenever `deploy/act/Dockerfile` changes.
* **If pip warns that scripts went to `/home/runner/.local/bin`**, act's shared tool cache still holds a
  Python extracted by an older root run. Reset it with `docker volume rm act-toolcache`.

## Actions minutes and the 2026-10-31 trigger cuts

On 2026-10-31 every GitHub Actions job in this repository stopped starting: the check-run
annotation read "recent account payments have failed or your spending limit needs to be
increased". This section records where the minutes were going and what was changed in
`ci.yml`; the sections above cover running the same gates on this Mac instead.

### Where the minutes went (measured)

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

### What changed in `ci.yml`

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
4. **Every job's runner is a variable**: `runs-on: ${{ inputs.runner || vars.CI_RUNS_ON || 'ubuntu-26.04' }}`
   (`inputs.runner` lets one manual run pick a label: `gh workflow run CI -f runner=self-hosted`).
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

### Recommendation

1. **Run `scripts/local_ci.sh` before pushing** (above). It is the same gate set as CI, takes
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

A pre-push hook is available but opt-in (above): `core.hooksPath` is shared by every worktree,
so turning it on makes every agent session's push wait for the quick gates too.

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

The macOS runner has not been tried yet. The first `workflow_dispatch` on it should confirm that
`actions/setup-python` installs 3.11 and 3.12 on macOS arm64, and that the wondrous-octopus job's
`--index-url https://download.pytorch.org/whl/cpu` torch install resolves there. That index is
built for Linux, while PyPI's plain macOS wheel is already CPU-only.

### Python on the self-hosted runners

`actions/setup-python` cannot run there: its macOS builds install a system-wide package with
`sudo installer` (every job failed with "sudo: a password is required" on 2026-11-01), and giving
`ghrunner` passwordless sudo would make it an admin. So every `ci.yml` job uses
`.github/actions/setup-python` instead. It calls `actions/setup-python` unchanged on GitHub-hosted
runners. On self-hosted ones it builds a per-job virtualenv from a uv-managed standalone Python
inside `ghrunner`'s home, with uv pinned to `requirements-dev.txt`. No sudo is needed, and the
interpreter and pip caches persist across jobs. The runner setup also points `AGENT_TOOLSDIRECTORY`
at `/Users/runner/hostedtoolcache` (owned by `ghrunner`), since the workflows' other actions expect
a tool cache to exist.
