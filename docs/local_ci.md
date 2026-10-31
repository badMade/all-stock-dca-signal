# Running CI locally (act + Colima)

`scripts/run_ci_locally.sh` runs `.github/workflows/ci.yml` on the operator's Mac with
[act](https://github.com/nektos/act), in Linux containers under [Colima](https://github.com/abiosoft/colima).
It runs the real workflow file, so there is no second copy of the CI steps to keep in sync, and nothing is
billed to the GitHub account.

It was added on 2026-10-31, when a failed account payment stopped GitHub from starting any hosted Actions
job ("The job was not started because recent account payments have failed or your spending limit needs
to be increased"). Every PR check then failed in 2-3 seconds without running.

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
scripts/run_ci_locally.sh -l                     # list job ids
```

Job ids are `changes`, `test`, `audit`, `research-audit`, and one per sibling project
(`production-safe-agent-loop`, `ticker-advisor`, `ml-forecaster`, `wondrous-octopus`,
`ticker-market-breadth`, `llm-providers`, `yfinance-prefetch`). Any other argument goes straight to act,
for example `--matrix python-version:3.11`.

Exit status is act's: 0 when every job that ran succeeded.

## What to know

* **It runs your working tree.** act copies the checkout, uncommitted edits included (gitignored files are
  left out). To check a PR, check out its branch first.
* **A PR run needs the PR number.** The `changes` job lists the PR's files through the GitHub API to decide
  which sibling jobs run. With no `--pr`, the script uses this branch's open PR; with no open PR, pass
  `--pr N` or use `--push`.
* **Results stay local.** Nothing is posted to the PR's checks, so a PR still shows the hosted failures
  until GitHub can run them again.
* **The token is gh's.** `gh auth token` is handed to act as the `GITHUB_TOKEN` secret and never printed.
* **The Docker socket is not mounted** (`--container-daemon-socket -`). act's default bind-mount fails under
  Colima and no job here starts containers of its own.
* **arm64, not amd64.** Jobs run as native `linux/arm64`. `actions/setup-python` downloads arm64 builds;
  a dependency with no arm64 wheel would fail here and not on GitHub.
* **Colima keeps running** after a run. Stop it with `colima stop` to free its memory; the script starts it
  again when needed. To rebuild the runner image, `docker rmi all-stock-dca/act-runner:24.04`.
