# Running CI locally (act + Colima)

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
(it matched GitHub exactly on PR #2066: 13,207 tests, 91.16% coverage). Only `run_ci_locally.sh --report`
posts its results to the PR, as commit statuses; neither turns a failed hosted check green.

## Setup (once)

```bash
brew install act colima docker gh
gh auth login            # the script reuses gh's token; skip if already logged in
```

Nothing else. On the first run the script starts Colima (8 CPUs, 16 GB, 100 GB disk, Apple
Virtualization with Rosetta) and builds the runner image from `deploy/act/Dockerfile`: act's medium
Ubuntu 24.04 image plus the GitHub CLI, which the "Detect changed projects" job calls. The full act image
that already carries `gh` is about 20 GB, so it is not used.

## Use

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

## What to know

* **It runs your working tree, from a clean clone.** The script clones this checkout's HEAD into a temporary
  directory, applies uncommitted edits and copies untracked (not ignored) files on top, and runs act there.
  act copies whatever directory it runs in, and a linked worktree's `.git` is a file naming a host path the
  container cannot see: run directly, every `git` call exited 128 and `from.py` looked for the main
  checkout's `.venv` (14 of the first run's 17 failures). To check a PR, check out its branch first.
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
  status must describe exactly one commit) and a HEAD that is not on GitHub yet. A matrix leg is reported
  with act's `-1`/`-2` suffix, in matrix order (for research audit: `-1` is 3.11, `-2` is 3.12).
  Without `--report`, results stay in the terminal.
* **The token is gh's.** `gh auth token` is handed to act as the `GITHUB_TOKEN` secret and never printed.
* **The Docker socket is not mounted** (`--container-daemon-socket -`). act's default bind-mount fails under
  Colima and no job here starts containers of its own.
* **Jobs run one at a time** (`--concurrent-jobs 1`). act shares one Python tool cache across jobs, and on
  the first run the audit job's pip upgrade broke the 3.12 research-audit leg that was using the same
  Python at that moment. A full run is therefore slower than on GitHub; `-j <job>` runs just one.
* **arm64, not amd64.** Jobs run as native `linux/arm64`. `actions/setup-python` downloads arm64 builds;
  a dependency with no arm64 wheel would fail here and not on GitHub.
* **Colima keeps running** after a run. Stop it with `colima stop` to free its memory; the script starts it
  again when needed. The runner image is rebuilt automatically whenever `deploy/act/Dockerfile` changes.
* **If pip warns that scripts went to `/home/runner/.local/bin`**, act's shared tool cache still holds a
  Python extracted by an older root run. Reset it with `docker volume rm act-toolcache`.
