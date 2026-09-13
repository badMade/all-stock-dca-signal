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

1. Work directly on the `main` branch. Do not create or use a worktree or a feature branch unless the user explicitly overrides this instruction.
2. Inspect `git status --short` before editing and preserve any existing user changes.
3. Prefer small, reviewable patches. Avoid unrelated refactors.
4. Add or update tests when behavior changes. If a bug is fixed, add a regression test when practical.
5. Keep runtime code portable and environment-agnostic. Do not hard-code local absolute paths, credentials, or machine-specific state. Artifact roots come from `dca_paths.stream_root(...)` / `cron_tracking_root()` (honouring `$DCA_CRON_TRACKING_ROOT`), never from a literal `/home/user/...` string.
6. Run the narrowest relevant check first, then the standard checks before handing off:
   - `ruff check .`
   - `pytest`
   - `python run_dca.py --demo --report --now <YYYY-MM-DDTHH:MM> --artifacts-dir /tmp/dca-demo`
     (use any near-close timestamp, e.g. today's date at 15:45)
7. Document any check that cannot be run and why.
8. Before committing, review the diff for secrets, generated artifacts, and excessive churn.
9. Before writing a new script, one-off snippet, or re-deriving strategy/gate logic by
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

## Pull request expectations

Include:

- Summary of behavior/documentation changes.
- Tests or checks run, with exact commands.
- Any known limitations or follow-up work.
- Confirmation that no secrets were added.
