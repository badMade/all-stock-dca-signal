# All Stock DCA

Agent-ready, version-controlled backup and implementation of a graded dollar-cost-averaging (DCA) trading workflow. The repository contains the core strategy logic, configuration, safeguards, reporting helpers, schemas, tests, and operational documentation needed for deterministic review and maintenance.

## Purpose

This project preserves the live DCA strategy in a reproducible form so humans and coding agents can:

- review strategy behavior and safety gates;
- run tests, lint checks, and dry-run smoke tests;
- make small, auditable changes;
- restore or validate scheduled trading/reporting workflows without storing secrets.

No credentials or secret values are stored in this repository.

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

### CLI dry run

```bash
python run_dca.py --demo --report --now <YYYY-MM-DDTHH:MM> --artifacts-dir /tmp/dca-demo
# Use any near-close timestamp, e.g. today's date at 15:45
```

### Installed console scripts

```bash
run-dca --demo --report --now <YYYY-MM-DDTHH:MM> --artifacts-dir /tmp/dca-demo
run-dca-risk --help
```

### Python API example

```python
from dca_config import load_strategy_config
from near_close_dca import graded_buy_multiplier

config = load_strategy_config()
multiplier = graded_buy_multiplier(0.03, config)
print(config.base_buy_amount_usd * multiplier)
```

## Project structure

| Path | Purpose |
| --- | --- |
| `AGENTS.md` | Codex/agent operating guide for safe autonomous work. |
| `near_close_dca.py`, `morning_dca.py` | Core strategy and artifact-building logic. |
| `run_dca.py` | CLI orchestrator. |
| `dca_config.py` | Config discovery and loading. |
| `dca_risk.py` | Risk checks and safety validation. |
| `dca_live_adapters.py` | Broker/data-provider interfaces and adapters. |
| `dca_reporting.py`, `dca_cost_tracker.py`, `dca_notion_sync.py`, `dca_weekly_dashboard.py` | Reporting, cost tracking, and dashboard helpers. |
| `config/` | Version-controlled JSON strategy configuration. |
| `schemas/` | JSON schemas for configs and generated artifacts. |
| `scripts/` | Backup and run-classification utilities. |
| `docs/` | Detailed strategy docs, runbooks, and operational notes. |
| `test_*.py`, `scripts/tests/` | Automated tests. |
| `trading-strategies/`, `analysis/` | Optional research tooling and historical analysis artifacts. |
| `tickers.txt`, `historical_stock_data.csv`, `stock_fetch_progress.json` | Operational data files used by fetch/analysis scripts; do not delete. |
| `FETCH_*.md`, `FILE_MANIFEST.txt`, `FINAL_EXECUTION_REPORT.md`, `TASK_SUMMARY.txt`, `README_*.md` | Generated operational reports from fetch runs; treat as read-only artifacts. |

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
python run_dca.py --demo --report --now <YYYY-MM-DDTHH:MM> --artifacts-dir /tmp/dca-demo
# Use any near-close timestamp, e.g. today's date at 15:45
```

## Troubleshooting

- **Import errors for test tools:** install `requirements-dev.txt` or `pip install -e '.[dev]'`.
- **Unexpected config values:** inspect `config/` and validate against `schemas/`.
- **Dry-run artifact confusion:** write artifacts to a temporary directory such as `/tmp/dca-demo` and remove it after review.
- **Research dependency failures:** keep the core workflow separate from `trading-strategies/`; those scripts require optional dependencies.

## Contributing

1. Check `git status --short` before editing.
2. Keep patches focused and reviewable.
3. Add or update tests for behavior changes.
4. Run lint, tests, and the dry-run smoke test when possible.
5. Confirm no secrets, generated artifacts, or local environment files were added.
6. Summarize changes and verification commands in the pull request.

## More documentation

Start with [`docs/README.md`](docs/README.md) for the strategy documentation index and operational runbooks.
