# All Stock DCA

Agent-ready, version-controlled backup and implementation of a graded dollar-cost-averaging (DCA) trading workflow. The repository contains the core strategy logic, configuration, safeguards, reporting helpers, schemas, tests, and operational documentation needed for deterministic review and maintenance.

## Purpose

This project preserves the live DCA strategy in a reproducible form so humans and coding agents can:

- review strategy behavior and safety gates;
- run tests, lint checks, and dry-run smoke tests;
- make small, auditable changes;
- restore or validate scheduled trading/reporting workflows without storing secrets.

No credentials, tokens, or account identifiers are stored in this repository.

This is a **library driven from outside, not a self-contained app**: the live scheduled runs
are executed by external agent sessions that hold Robinhood MCP access and inject plain
callables into the adapters. The repo makes no network calls, and the CLI is dry-run only.
A module with no in-repo caller is usually not dead code. Runtime is **Python ≥3.11,
standard library only**.

### Three entry points

A fix to one does not propagate to the others (details in [`docs/live_wiring.md`](docs/live_wiring.md)).

| Entry point | Driven by |
| --- | --- |
| `run_dca.run_slot()` | The 3:30 PM live path; the only one with a real CLI `__main__` (dry-run only). |
| `MorningDCAExecutor` + `market_open_dca` / `midday_dca` / `intraday_dca` subclasses | An external agent calling `.run(now, data, broker=...)`. |
| `dca_runner.py` | An external agent session running the gate sequence with injected callables. |

### Required environment

The live brokerage account number is supplied by the environment, never by git.
`config/*.json` stores it as the reference `"${ROBINHOOD_AGENTIC_ACCOUNT_NUMBER}"`,
which `dca_config.expand_config_env` resolves when a config file is loaded.

```bash
export ROBINHOOD_AGENTIC_ACCOUNT_NUMBER='<your account number>'  # 6-12 digits
```

A missing or empty value is a hard error at config-load time rather than an
empty-string substitution, and `near_close_dca.account_pinning_failure` refuses
to start any live run that cannot pin its account. Dry runs, the `--demo` CLI,
and the test suite do not need the variable set — the test suite assigns its own
synthetic value in `conftest.py`.

### Optional environment

| Variable | Default | Effect |
| --- | --- | --- |
| `DCA_CRON_TRACKING_ROOT` | `<checkout>/cron_tracking` (production exports the shared tree explicitly) | Root of the per-stream run-artifact tree. Every stream root (`morning_dca`, `midday_dca`, `intraday_dca`, `market_open_dca`, `ce16ba6b`, `attempt_log`, `model_usage`, …) derives from it via `dca_paths`, so one variable relocates the whole tree. Read at import time — export it before invoking the task. |
| `DCA_CONFIG_DIR` | `config/` beside the modules | Directory holding `config/*.json`. |
| `DCA_ATTEMPT_LOG_ROOT` | `<cron root>/attempt_log` | Overrides just the failed-start marker tree. Read at call time. |
| `DCA_INTRADAY_LIVE_STATE_ROOT`, `DCA_MARKET_OPEN_LIVE_STATE_ROOT` | derived from the cron root | Replace (not extend) the recovery/prior-allocation search roots for those streams. |
| `DCA_DRIFT_SLACK_WEBHOOK` | unset | Slack webhook for `scripts/drift_detection.py --slack`. |

## Installation

### Core development environment

```bash
python -m venv .venv
source .venv/bin/activate
python -m pip install --upgrade pip
pip install -r requirements-dev.txt
```

The core strategy uses only the Python standard library at runtime. Development tooling is listed in `requirements-dev.txt`.

### Editable package install

```bash
pip install -e '.[dev]'
```

### Optional research tooling

Research scripts under `trading-strategies/` use third-party packages that are not required for the core DCA workflow.

```bash
pip install -e '.[strategies]'
# or
pip install -r trading-strategies/requirements.txt
```

## Usage

### Local interface and single-command demo

```bash
python3 from.py
```

Opens a local control panel with daily summaries, the ET schedule, job progress,
and Wondrous Octopus forecasts and historical-period comparisons. Startup observes
your real existing artifacts read-only by default; no order is ever placed from
here. Use `python3 from.py --demo` for an isolated synthetic sandbox instead, or
`python3 from.py --once` for a headless snapshot of real data. Research dependencies
and all launcher options are documented in [the local console guide](docs/local_console.md).

### CLI dry run

```bash
python run_dca.py --demo --report --now <YYYY-MM-DDTHH:MM> --artifacts-dir /tmp/dca-demo/scheduled_runs/demo
# Use any near-close timestamp, e.g. today's date at 15:45
```

### Installed console scripts

```bash
run-dca --demo --report --now <YYYY-MM-DDTHH:MM> --artifacts-dir /tmp/dca-demo/scheduled_runs/demo
run-dca-risk --help
```

### Local tests and coverage

```bash
.venv/bin/python -m pytest                 # ~13,300 tests; tests live in tests/
.venv/bin/python -m pytest tests/test_midday_dca.py -q
.venv/bin/python -m pytest scripts/tests -q   # reliability engines; separate from the root sweep
pytest --cov=. --cov-report=term-missing:skip-covered --cov-fail-under=90   # the CI gate
```

Each adjacent subproject (below) has its own suite and is run from its own directory.

### Python API example

```python
from dca_config import load_strategy_config
from near_close_dca import graded_buy_multiplier, select_first_allocation_base_usd

config = load_strategy_config()
previous_close = 100.0
previous_low = 95.0
decision_price = 97.0  # a 3% drop: below the prior close, but not below the prior low

multiplier = graded_buy_multiplier((previous_close - decision_price) / previous_close, config)
# The first same-day allocation's dollar base is selected separately, BEFORE the
# ladder multiplier: $2 when decision_price is below both previous_close and
# previous_low, $1 when below previous_close only (this example), $0 otherwise.
base_amount_usd, base_selection_reason = select_first_allocation_base_usd(
    decision_price, previous_close, previous_low, config
)
print(base_amount_usd * multiplier)
```

## Project structure

| Path | Purpose |
| --- | --- |
| `AGENTS.md` | Codex/agent operating guide for safe autonomous work. |
| `near_close_dca.py`, `morning_dca.py` | Core strategy and artifact-building logic; `market_open_dca.py`, `midday_dca.py`, `intraday_dca.py` are thin stream subclasses. |
| `run_dca.py`, `dca_runner.py` | CLI/pipeline orchestrator and the gate-sequence live runner. |
| `dca_config.py` | Config discovery and loading. |
| `dca_risk.py` | Risk checks and safety validation. |
| `dca_live_adapters.py` | Broker/data-provider interfaces and adapters. |
| `dca_reporting.py`, `dca_cost_tracker.py`, `dca_notion_sync.py`, `dca_weekly_dashboard.py` | Reporting, cost tracking, and dashboard helpers. |
| `config/` | Version-controlled JSON strategy configuration. |
| `schemas/` | JSON schemas for configs and generated artifacts. |
| `scripts/` | Reliability engines, backup, run-classification, and drift utilities. |
| `from.py`, `dca_console*.py`, `console/` | Checkout-only local console (observe/dry-run only). |
| `dca_market_graph.py`, `viz/` | 3D market graph (see `docs/market_graph.md`). |
| `docs/` | Detailed strategy docs, runbooks, and operational notes. |
| `tests/`, `scripts/tests/` | Automated tests (`tests/test_<module>.py` per core module). |
| `ticker-advisor/`, `trading-strategies/`, `analysis/`, `production-safe-agent-loop/`, and more | Adjacent projects — see below. |
| `tickers.txt`, `historical_stock_data.csv`, `stock_fetch_progress.json` | Operational data files used by fetch/analysis scripts; do not delete. |
| `FETCH_*.md`, `FILE_MANIFEST.txt`, `FINAL_EXECUTION_REPORT.md`, `TASK_SUMMARY.txt`, `README_*.md` | Generated operational reports from fetch runs; treat as read-only artifacts. |

## Adjacent projects

Each of these is a self-contained subproject with its own dependencies and tests, excluded
from the core repo's stdlib-only ruff/coverage gates (see `CLAUDE.md`). None is imported by
`near_close_dca.py` or any other core strategy module — each is invoked separately, on its own
schedule or by hand.

| Project | What it does | Run it |
| --- | --- | --- |
| `ticker-advisor/` | Scores candidate tickers on strategy-health, backtested performance, and sentiment signals; proposes universe additions for manual review. Never edits `config/tickers.json` itself. | `cd ticker-advisor && pip install -r requirements.txt && python advise_tickers.py --candidates TICK1 TICK2` — see `docs/ticker_advisory.md`. |
| `trading-strategies/` | Separate research tree: a standalone vectorbt-based backtester plus strategy/sentiment monitors, decoupled from the live pipeline. | `cd trading-strategies && pip install -r requirements.txt` — see `trading-strategies/README.md`. |
| `analysis/` | Dated one-off analysis folders: ladder revisions, execution audits, reliability writeups. Nothing here runs on a schedule. | Browse `analysis/README.md` for an index of what's in each dated folder. |
| `ml-forecaster/`, `wondrous-octopus/`, `ticker-market-breadth/`, `llm-providers/`, `yfinance-prefetch/` | Sizing-adjustment ML retrain, walk-forward OHLC forecasting, `^TICK` breadth archive, provider-agnostic LLM chat interface, and yfinance fallback prefetch. Each has its own `README.md` and `requirements.txt`; none is wired into live sizing. | `cd <dir> && pip install -r requirements.txt && python -m pytest tests/` |
| `production-safe-agent-loop/` | Two safety primitives for running an AI agent loop against something that costs real money: a turn/token circuit breaker, and a forced pre-flight spec. Not wired into any live executor today. | `cd production-safe-agent-loop && pip install -r requirements.txt && python -m pytest tests/` — see `production-safe-agent-loop/README.md`. |

## Agentic workflow compatibility

This repo includes the files and conventions expected by Codex-style workflows:

- `AGENTS.md` with repository-specific operating instructions;
- deterministic setup commands;
- standard lint/test/smoke-test commands;
- CI coverage for Python 3.11 and 3.12;
- documented secret-handling expectations;
- generated/local output ignored via `.gitignore`.

Recommended checks before handoff:

```bash
ruff check .
pytest
python run_dca.py --demo --report --now <YYYY-MM-DDTHH:MM> --artifacts-dir /tmp/dca-demo/scheduled_runs/demo
# Use any near-close timestamp, e.g. today's date at 15:45
```

## Troubleshooting

- **Import errors for test tools:** install `requirements-dev.txt` or `pip install -e '.[dev]'`.
- **Unexpected config values:** inspect `config/` and validate against `schemas/`.
- **Dry-run artifact confusion:** write artifacts to a temporary directory such as `/tmp/dca-demo/scheduled_runs/demo` and remove it after review. Keep the `scheduled_runs/` segment: without it the run scans the parent directory (for example all of `/tmp`) for prior same-day artifacts.
- **Research dependency failures:** keep the core workflow separate from `trading-strategies/`; those scripts require optional dependencies.

## Contributing

1. Check `git status --short` before editing.
2. Keep patches focused and reviewable.
3. Add or update tests for behavior changes.
4. Run lint, tests, and the dry-run smoke test when possible.
5. Confirm no secrets, generated artifacts, or local environment files were added.
6. Summarize changes and verification commands in the pull request.

## More documentation

New contributors should begin with the
[`docs/newcomer_guide.md`](docs/newcomer_guide.md) architecture and learning
guide, then use [`docs/README.md`](docs/README.md) for the complete strategy and
operations index.
