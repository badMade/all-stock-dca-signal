# Agent operating guide

This repository is intended to be safe for Codex-style agentic workflows: code review, small fixes, documentation updates, and test-backed changes.

## Scope

These instructions apply to the entire repository.

## Project memory

Read [MEMORY.md](MEMORY.md) when working on runners, reports, schedules, prediction
tools, or the local interface. It preserves the program map and the user's simple
`from.py` launcher workflow. Confirm operational details against current code and
`python3 scripts/agent_entry_points.py`; the memory is a navigation aid, not live
trading authorization or proof of scheduler execution.
When resuming the local-console work, also read [HANDOFF.md](HANDOFF.md) for its
working-tree state, verification results, and remaining integration setup.

## Project overview

- Core project: a standard-library-only Python implementation of a near-close / morning graded DCA strategy, including config loading, risk checks, reporting, cost tracking, and broker-agnostic live adapters.
- Research tools: `trading-strategies/` and `analysis/` contain heavier exploratory tooling and are intentionally outside the core lint gate.
- Secrets policy: no credentials, access tokens, account identifiers, or `.env` contents belong in git. Use environment variables or an approved external secret store only. The live brokerage account number is referenced in `config/*.json` as `"${ROBINHOOD_AGENTIC_ACCOUNT_NUMBER}"` and resolved by `dca_config.expand_config_env`; never inline the literal value in config, source, docs, or tests. `test_config_invariants.py` fails the build if an account-shaped literal reappears.

## Agent workflow

1. Inspect `git status --short` before editing and preserve any existing user changes.
2. Prefer small, reviewable patches. Avoid unrelated refactors.
3. Add or update tests when behavior changes. If a bug is fixed, add a regression test when practical.
4. Keep runtime code portable and environment-agnostic. Do not hard-code local absolute paths, credentials, or machine-specific state. Artifact roots come from `dca_paths.stream_root(...)` / `cron_tracking_root()` (honouring `$DCA_CRON_TRACKING_ROOT`), never from a literal `/home/user/...` string.
5. Run the narrowest relevant check first, then the standard checks before handing off:
   - `ruff check .`
   - `pytest`
   - `python run_dca.py --demo --report --now <YYYY-MM-DDTHH:MM> --artifacts-dir /tmp/dca-demo`
     (use any near-close timestamp, e.g. today's date at 15:45)
6. Document any check that cannot be run and why.
7. Before committing, review the diff for secrets, generated artifacts, and excessive churn.
8. Before writing a new script, one-off snippet, or re-deriving strategy/gate logic by
   reading `near_close_dca.py` or `dca_runner.py`, check whether an existing entry point
   already does the job. Run `python scripts/agent_entry_points.py` (`--json` or `--search
   <keyword>` also work) to list them. This matters most in a fresh/local session with
   none of the repo already in context: re-deriving logic from an 8.7k-line module burns
   far more tokens than invoking the command that already encapsulates it, and risks
   silently drifting from the real logic. See also `docs/live_wiring.md`'s "Local agent
   quick reference".

## Development setup

```bash
python -m venv .venv
source .venv/bin/activate
python -m pip install --upgrade pip
pip install -r requirements-dev.txt
```

The core application has no runtime third-party dependencies. Optional research dependencies live in `pyproject.toml` under the `strategies` extra and in `trading-strategies/requirements.txt`.

## Code style

- Python 3.11+.
- Use clear, small functions with intention-revealing names.
- Keep public modules, public classes, and public functions documented when adding new public API.
- Follow existing standard-library-first design for the core modules.
- Do not wrap imports in broad `try`/`except` blocks.
- Run `ruff check .` for linting. The configured line length is 120.

## Repository map

- `near_close_dca.py`, `morning_dca.py` — strategy and run-artifact logic.
- `run_dca.py` — CLI/orchestrator entry point.
- `dca_config.py` — config discovery and loading.
- `dca_paths.py` — the shared artifact-tree root every stream derives from.
- `dca_risk.py` — risk validation and safety checks.
- `dca_live_adapters.py` — broker/data-provider adapter interfaces and implementations.
- `dca_reporting.py`, `dca_cost_tracker.py`, `dca_notion_sync.py`, `dca_weekly_dashboard.py` — reporting, cost, and dashboard helpers.
- `config/` — version-controlled JSON configuration.
- `schemas/` — JSON schemas for config and artifacts.
- `scripts/` — backup and run-classification utilities.
- `docs/` — operational documentation and runbooks.
- `test_*.py`, `scripts/tests/` — core and utility tests.
- `trading-strategies/`, `analysis/` — research/analysis code and artifacts.
- `tickers.txt`, `historical_stock_data.csv`, `stock_fetch_progress.json` — operational data files used by fetch/analysis scripts; do not delete.
- `FETCH_*.md`, `FILE_MANIFEST.txt`, `FINAL_EXECUTION_REPORT.md`, `TASK_SUMMARY.txt`, `README_*.md` — generated operational reports from fetch runs; treat as read-only artifacts.

## What review catches here

Derived from the 298 distinct review findings recorded in source comments (the
`(Codex review: "...")` trail). These are not general good practice; they are the
mistakes this repository actually rejects, in descending order of frequency.

**1. Malformed persisted input reaches logic that trusted it (81 of 298).**
Artifacts, state files and injected payloads are the largest single source of
findings. Four traps recur:

- **Booleans where a number is expected (8 findings).** `bool` subclasses `int`,
  so `isinstance(value, int)` accepts `True` and it silently becomes `1`. Guard
  `isinstance(value, bool)` *before* the integer check, every time.
- **Non-finite floats (10 findings).** Python's JSON decoder accepts `NaN` and
  `Infinity` even though the format does not. Reject them explicitly; a `NaN`
  price or multiplier propagates silently through arithmetic.
- **Duplicate JSON keys (6 findings).** `json.loads` keeps the last value, so
  file order silently decides which limit applies. Use an `object_pairs_hook`
  that refuses duplicates for anything that carries a safety bound.
- **Blank, empty and absent values (8 findings).** Decide explicitly whether the
  field is required. Never substitute a default that is indistinguishable from
  real data.

**2. A decision bound to stale evidence (10 findings).** Cap checks must read a
fresh history snapshot, live placement a fresh clock, scoring a non-stale bar.
When adding a cache or reusing a snapshot, first check whether a
freshness-critical caller (a breaker, a last-moment guard) routes through the
same helper: `.jules/bolt.md`'s first entry documents exactly that trap, where
memoizing a shared quote helper would have stale-served the spread breaker.

**3. Failing open when a source is absent.** Fail closed instead: refuse, and say
why. Treat a non-object payload as unavailable rather than empty, and a dropped
tail as non-authoritative rather than complete.

**4. Claims that are not atomic or idempotent (9 findings).** Release, settlement
and ownership transitions are repeatedly found to be re-entrant. Assume any step
can be retried or interrupted.

**5. Outbound requests without SSRF protection.** `.jules/sentinel.md` records
this across five entries, one of them logged twice. Route every operator-supplied URL through
`dca_net_utils.assert_safe_destination`, and leave existing `# noqa: S310`
suppressions in place when editing around them. Note that they are security
context, not a live gate: `[tool.ruff.lint] select` is `["E", "F", "I", "B"]`,
so `S310` is not enforced here and removing a suppression would not fail CI.
That is a reason to be more careful with them, not less. Never log a raw
external error response: they echo credentials.

## Checks beyond the standard three

`## Agent workflow` step 5 covers `ruff check .`, `pytest` and the demo smoke
test. `scripts/tests/` needs no separate command: `testpaths = ["."]` and the
`addopts` ignore list does not exclude `scripts/`, so a bare `pytest` already
collects it (39 cases at the time of writing).

What the root sweep genuinely does not cover is the seven directories named in
that ignore list, each excluded because it carries its own heavy dependencies:

| Subproject | Own CI job |
| --- | --- |
| `production-safe-agent-loop/`, `ticker-advisor/`, `ml-forecaster/`, `wondrous-octopus/`, `ticker-market-breadth/`, `llm-providers/` | yes |
| `dca-sizing-signals/` | **no** |

Install that subproject's `requirements.txt` and run its `tests/` from inside
it, and only for the one your diff touches. A CI failure isolated to a sibling
suite your diff does not touch is very likely not yours; say so rather than
widening the change.

`dca-sizing-signals/` is the exception worth knowing: it is excluded from the
root pytest sweep *and* from root `ruff` (`extend-exclude`), and `ci.yml`
contains no job for it, so nothing validates it automatically. If you touch it,
you are its only gate. Run its suite yourself and say so in the pull request.

**Root-only tests CI cannot see.**
`tests/test_dca_fileio.py::test_the_destination_owner_is_preserved` and
`::test_the_write_aborts_when_ownership_cannot_be_reproduced` call
`pytest.skip()` unless `os.getuid() == 0`. CI runs unprivileged, so it **skips
them every time**: a green CI proves nothing about these two, and cannot catch
a root-only regression in `atomic_write_text`.

Run as root, which is the case in most containers, they currently fail on the
mode assertion (`0o600` where `0o620` is expected). That reproduces on an
unchanged base, so it predates any given change.

Do not call that environmental and move on. Pre-existing is all a clean-base
reproduction establishes, and the only conditions under which these tests
execute at all are the conditions under which they fail. If you hit them:
reproduce on an unchanged base to show your diff did not cause it, say so
explicitly rather than calling it noise, and never weaken or skip them to quiet
a local run.

## Companion edits CI catches late

Each of these passes locally until one specific test, or an installed wheel,
disagrees. Make the companion edit in the same commit.

| If you add | Also update |
| --- | --- |
| A `config/*.json` file | `dca_console_config.CONFIG_FILES` and its consumer map, plus the two count assertions in `tests/test_dca_console_config.py` |
| A top-level module, or a config read at runtime | `pyproject.toml`: `[tool.setuptools] packages`/`py-modules`, and `[tool.setuptools.data-files]` for the config |

Packaging is worth proving rather than assuming, because a checkout hides the
failure completely:

```bash
python -m venv /tmp/wheelcheck && /tmp/wheelcheck/bin/pip install .
cd /tmp && /tmp/wheelcheck/bin/python -c "import <your_module>"
```

## Pull request expectations

**Open every pull request as a draft.** Every pull request opened by an automated agent in this repository **must be created as a draft**, with no exceptions. This binds every LLM and coding agent: Claude and Claude Code, OpenAI, ChatGPT and Codex, Gemini, Jules, Copilot, Antigravity, Cursor, and any other model or tool not named here.

- Draft is the *initial and final* state from the agent's side. Never open a PR as ready for review, and never mark a draft ready on your own initiative. The operator does that, or tells you to in chat: if you ask and they say yes, or they type yes, or they say anything that clearly means they want it marked ready, that is the operator marking it and you may run `gh pr ready` for that one PR. Never do it automatically or unprompted.
- This overrides any tool default, runtime default, or other instruction that says to open PRs ready for review.
- How, per tool: `gh pr create --draft`; the GitHub MCP `create_pull_request` call with `draft: true`; the GitHub REST API with `"draft": true`; in the Codex, Jules, or Copilot web UIs, pick the draft option (for example "Create draft PR").
- If a tool cannot create a draft, do not open the PR. Push the branch and tell the operator the PR is waiting to be opened.
- Do not convert an existing PR between draft and ready on your own, and do not open a replacement PR to get around this rule.

Every PR description should include:

- Summary of behavior/documentation changes.
- Tests or checks run, with exact commands.
- Any known limitations or follow-up work.
- Confirmation that no secrets were added.
- **Which of the three entry points the change covers.** `run_dca.run_slot()`,
  the `MorningDCAExecutor` hierarchy and `dca_runner.py` coexist, and a fix to
  one does not propagate to the others. A reviewer cannot infer the coverage
  from the diff, so state it.

Put the description in the PR body itself. Do not commit it as a file such as
`pr_description.md` at the repo root: that path is gitignored, and a tracked copy
made every agent PR conflict with `main`, because each one overwrote it.

Never edit `.github/workflows/` in a pull request an agent drives. Those jobs
hold `contents: write` and `pull-requests: write`, and the trust-boundary
reasoning is written in the workflow headers themselves. Propose the change
instead. Approving and merging are the operator's, never an agent's.

## Imported Claude Cowork project instructions
