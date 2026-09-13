# Newcomer guide

All Stock DCA is a safety-focused Python implementation and version-controlled
backup of a graded dollar-cost-averaging workflow. It is not a single bot
process: strategy evaluation, execution gates, scheduling, artifacts, reports,
and research tools are separate concerns.

The core targets Python 3.11+, uses only the standard library at runtime, and
receives market-data and broker operations through injected callables. This
keeps credentials and network clients outside the repository and makes the core
deterministic to test.

## Start here

```bash
python3 from.py --check
python3 from.py --once
python3 scripts/agent_entry_points.py
python run_dca.py --demo --report --now 2026-09-08T15:45 \
  --artifacts-dir /tmp/dca-demo
```

`from.py` is the local interface. Its default mode uses isolated synthetic
data; `--local` observes existing artifacts. It does not register an external
scheduler or place live orders.

`scripts/agent_entry_points.py` is the canonical index of supported commands.
Check it before writing a script or reconstructing strategy logic from a large
module.

## Repository structure

| Area | Important paths | Responsibility |
|---|---|---|
| Local interface | `from.py`, `dca_console*.py`, `console/` | Loopback dashboard, bounded jobs, daily summaries, and research views |
| Strategy | `near_close_dca.py` | Signals, graded sizing, budget selection, safeguards, state models, and artifacts for the near-close path |
| Stream executors | `morning_dca.py`, `market_open_dca.py`, `midday_dca.py`, `intraday_dca.py` | Class-based market-open, morning, midday, and intraday pipelines |
| Orchestration | `run_dca.py` | Dry-run CLI, reports, functional pipeline, and `run_slot()` |
| Live gates | `dca_runner.py` | Market/time checks, order-history validation, budgets, planning, placement validation, and durable state |
| Configuration | `dca_config.py`, `config/` | Config discovery and validated strategy overlays |
| External interfaces | `dca_live_adapters.py` | Broker and market-data adapters built from injected callables |
| Risk and provenance | `dca_risk.py`, `scripts/run_classification.py`, `scripts/scheduled_artifacts.py` | Deployment evidence and scheduled/manual classification |
| Reports | `dca_reporting.py`, `dca_cost_tracker.py`, `dca_notion_sync.py`, `dca_weekly_dashboard.py` | Run reports, workload proxies, sync payloads, and weekly views |
| Artifacts | `dca_paths.py`, `dca_fileio.py`, `schemas/` | Portable roots, safe file operations, and JSON contracts |
| Operations | `scripts/` | Candidate generators, diagnostics, alerts, ledgers, maintenance, and scheduling helpers |
| Tests | `tests/`, `scripts/tests/` | Core and operational regression tests |
| Research | `wondrous-octopus/`, `ticker-advisor/`, `trading-strategies/`, `analysis/` | Separate forecasting and analysis projects with their own dependencies |

For a comprehensive component inventory, see [repo_map.md](repo_map.md).

## Execution flow

```text
config/*.json
      |
      v
dca_config.load_strategy_config()
      |
      +-- market-data providers --> signal evaluation
      +-- broker history --------> duplicate, budget, and account checks
      |
      v
candidate generation --> gates --> order plan --> broker adapter
      |
      v
run-state, order, report, alert, and drift artifacts
```

Candidate generation and order placement are deliberately separate. Candidate
commands implement signal evaluation (Gate 5); they do not place orders.
`dca_runner.assess()` returns a JSON-serializable assessment, not a live `Plan`.
An in-process caller must build a `BudgetSnapshot`, select a budget tier, call
`plan_orders()`, and pass that plan and the same snapshot/tier objects to
`execute_plan()`.

## Scheduled signal slots

These are the nominal Eastern Time slots. Current scheduling must always be
confirmed against configuration and external scheduler state.

| ET | Candidate module | Signal engine |
|---|---|---|
| 09:35 | `scripts.generate_market_open_candidates` | `MarketOpenDCAExecutor` |
| 10:30 | `scripts.generate_morning_candidates` | `MorningDCAExecutor` |
| 11:30 | `scripts.generate_intraday_candidates --slot 1130` | `IntradayDCAExecutor` |
| 12:30 | `scripts.generate_midday_candidates` | `MiddayDCAExecutor` |
| 13:30 | `scripts.generate_intraday_candidates --slot 1330` | `IntradayDCAExecutor` |
| 14:30 | `scripts.generate_intraday_candidates --slot 1430` | `IntradayDCAExecutor` |
| 15:30 | `scripts.generate_buy_candidates --slot 1530ET` | `near_close_dca` signal path |

The three intraday slots share a pooled budget enforced outside candidate
generation. Some slots also require trend-window data or verified deployment
history; use the entry-point index rather than inferring each input contract.

## Strategy in one minute

- The normal signal requires a price below the previous regular-session close,
  a downward 60-minute trend, and a positive ladder multiplier.
- The 15:30 final-buy path ignores only the trend requirement. Price, spread,
  confidence, budget, whitelist, trading-hours, top-up, and circuit-breaker
  checks remain in force.
- The first-allocation base is selected before applying the drop multiplier.
- Live sizing prefers configured per-ticker calibrated tiers and uses the global
  ladder as a fallback.
- The strategy buys only; it does not sell.

See [strategy.md](strategy.md) for the complete rules and
[safeguards.md](safeguards.md) for execution constraints.

## Architectural traps

### Three execution paths coexist

The repository contains three non-interchangeable architectures:

1. `run_dca.run_slot()` over functional `near_close_dca` primitives;
2. `MorningDCAExecutor` and its stream subclasses;
3. `dca_runner.py`'s plan/execute gate sequence.

Some logic is duplicated. A change to one path may not affect the other two.
Code with no in-repository caller may still be invoked by an external agent
session, so do not classify it as dead code from call-site searches alone.

Before changing signal, sizing, preflight, or execution behavior, identify the
owning slot and execution path. Read [live_wiring.md](live_wiring.md) before
modifying live behavior.

### Persistent state is a safety boundary

Daily/weekly notional totals, rollover reserves, and per-ticker counts live in
`StrategyState`. Creating a fresh state for every scheduled invocation resets
that accounting. Use `dca_runner.state_transaction()` across the complete
load-mutate-save operation; corrupt state fails closed and writes are atomic.

### Schedule configuration is not execution evidence

`config/scheduled_tasks.json` records intended timing. It does not prove that an
external scheduler is installed or that a task fired. Use run artifacts,
classification data, and broker evidence to establish what actually happened.

### Keep paths and secrets external

- Supply the live account through `ROBINHOOD_AGENTIC_ACCOUNT_NUMBER`; never put
  its literal value in source, config, documentation, tests, or logs.
- Derive artifact locations with `dca_paths.cron_tracking_root()` or
  `stream_root()`. Set `DCA_CRON_TRACKING_ROOT` to relocate the tree; do not add
  machine-specific absolute paths.
- Keep research dependencies out of the standard-library-only core.

## What to learn next

1. **Strategy contract:** read `docs/strategy.md`, then
   `config/sizing_ladder.json`, `config/per_ticker_sizing.json`, and
   `config/budgets.json`.
2. **One complete path:** trace `run_dca.main()` to `run_pipeline()` and the
   generated demo artifacts before comparing a class-based stream.
3. **Execution safety:** study `BudgetSnapshot`, `Candidate`, `Plan`,
   `plan_orders()`, `execute_plan()`, and `state_transaction()` in
   `dca_runner.py` alongside `docs/live_wiring.md`.
4. **Evidence contracts:** for a behavior, find its config, schema,
   implementation, regression test, and resulting run artifact.
5. **Research:** only after understanding the core boundary, use the relevant
   subproject README and its isolated environment.

## Development workflow

```bash
python -m venv .venv
source .venv/bin/activate
python -m pip install --upgrade pip
pip install -r requirements-dev.txt

ruff check .
pytest
python run_dca.py --demo --report --now 2026-09-08T15:45 \
  --artifacts-dir /tmp/dca-demo
```

Keep changes focused, add regression tests for behavior changes, preserve
existing working-tree changes, and inspect every final diff for secrets and
generated artifacts.
