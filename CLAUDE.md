# CLAUDE.md

Guidance for Claude Code when working in this repository. Read this first, then the
specific `docs/` page for whatever you are touching.

## What this is

A graded dollar-cost-averaging (DCA) trading system for a single Robinhood account: signal
logic, sizing ladder, budget caps, safety gates, reporting, and a version-controlled backup
of the live strategy config.

Two properties shape everything else:

- **Standard library only at runtime.** Python ≥3.11, zero third-party runtime dependencies.
  Third-party packages appear only in dev tooling and in the out-of-scope `trading-strategies/`
  and `analysis/` research trees.
- **This is a library driven from outside, not a self-contained app.** The live scheduled runs
  are executed by external agent sessions that hold Robinhood MCP access and inject plain
  callables into the adapters. This repo contains no credentials and makes no network calls.
  A module with no in-repo caller is usually *not* dead code — see "Three entry points" below.

## Commands

```bash
# Setup
python -m venv .venv && source .venv/bin/activate
python -m pip install --upgrade pip && pip install -r requirements-dev.txt
# or: pip install -e '.[dev]'

# Lint (ruff, line-length 120, rules E,F,I,B; E501 ignored)
ruff check .

# Tests — ~8,300 tests, ~5 min for the whole suite. Tests live under tests/ (2026-08-23:
# moved out of the repo root, which used to hold one test_<module>.py flat beside every
# module); pyproject.toml's pythonpath = [".", "tests"] keeps both `import near_close_dca`
# and cross-test imports (e.g. `from test_morning_dca import ...`) resolving from there.
.venv/bin/python -m pytest                # pyproject addopts already supply -q and the
                                          # --ignore=production-safe-agent-loop --ignore=ticker-advisor exclusions
.venv/bin/python -m pytest tests/test_midday_dca.py -q
.venv/bin/python -m pytest tests/test_midday_dca.py::test_some_case -q

# The coverage gate CI enforces
pytest --cov=. --cov-report=term-missing:skip-covered --cov-fail-under=90

# Dry-run smoke test (the third standard pre-handoff check)
python run_dca.py --demo --report --now 2026-08-03T15:45 --artifacts-dir /tmp/dca-demo

# Checkout-only local console; remote-access rules: docs/local_console.md#remote-access
python3 from.py --local --no-browser

# 3D market graph -- emits one self-contained HTML file, no network (docs/market_graph.md)
python -m scripts.generate_market_graph --source csv --out /tmp/ocean.html
```

Two suites sit outside the root `pytest` sweep and must be run separately:

```bash
.venv/bin/python -m pytest scripts/tests -q          # reliability engines, run classification, drift
(cd production-safe-agent-loop && python -m pytest tests/)   # own requirements.txt
ruff check --isolated --select=E,F,B --line-length=120 production-safe-agent-loop
(cd ticker-advisor && pip install -r requirements.txt && python -m pytest tests/)   # own requirements.txt
ruff check --isolated --select=E,F,B --line-length=120 ticker-advisor
(cd ml-forecaster && pip install -r requirements.txt && python -m pytest tests/)   # own requirements.txt
ruff check --isolated --select=E,F,B --line-length=120 ml-forecaster
(cd wondrous-octopus && pip install -r requirements.txt && python -m pytest tests/)   # own requirements.txt (torch: CPU wheel suffices)
ruff check --isolated --select=E,F,B --line-length=120 wondrous-octopus
(cd ticker-market-breadth && pip install -r requirements.txt && python -m pytest tests/)   # own requirements.txt
ruff check --isolated --select=E,F,B --line-length=120 ticker-market-breadth
```

CI (`.github/workflows/ci.yml`) runs lint + coverage-gated tests on Python 3.11 and 3.12, the
demo smoke test, `pip-audit` on pinned dev tooling and the resolved transitive dependencies
from `uv.lock` plus each independent research manifest, and the `production-safe-agent-loop`,
`ticker-advisor`, `ml-forecaster` (the sizing-adjustment ML retrain pipeline, see
`docs/ticker_advisory.md`'s "Implemented: the ml-forecaster sibling tool" section),
`wondrous-octopus` (walk-forward OHLC forecasting, see `docs/wondrous_octopus.md`), and
`ticker-market-breadth` (yfinance `^TICK` NYSE breadth-index archive fetch, data only --
see `ticker-market-breadth/README.md`) suites each in their own job. Dev tool versions are
**pinned** in `requirements-dev.txt` on purpose — bump deliberately.
The `dev` extra uses the same exact pins. It also installs `pip-audit`, `uv`, and
Pyright; Pyright uses the existing `[tool.pyright]` configuration but is not a CI
gate while its existing diagnostics remain unresolved. Audit lock exports and
manifest resolutions are temporary CI artifacts, not research package installs.
The research audit has a separate job name and runs on both CI Python versions;
research-only failures stay outside self-heal's documented core-only scope.

## Architecture

| Layer | Modules |
|---|---|
| Strategy core | `near_close_dca.py` (~40k lines: signals, graded ladder, budgets, safeguards, run artifacts, `build_broker`, `StrategyConfig`/`StrategyState`) |
| Stream executors | `morning_dca.py` defines `MorningDCAExecutor`; `market_open_dca.py` (9:35, cron `2ba19526`), `midday_dca.py` (12:30, `a6239dae`), `intraday_dca.py` (11:30 / 1:30 / 2:30, `7be82b97`) are thin subclasses that override identity constants and a few flags only |
| Orchestration | `run_dca.py` — `run_pipeline()`, `run_slot()`, `main()` (CLI); `dca_runner.py` — the gate-sequence live runner (Gate 0 market day → Gate 1 time sanity → Gate 3 window → Gate 4 sole-executor → Gate 6 budget → Gate 7 placement → Gate 9 evidence) |
| Config & paths | `dca_config.py` (discovery, `expand_config_env`, fail-closed loading), `dca_paths.py` (`cron_tracking_root()`, `stream_root()`), `dca_ticker_paths.py` |
| Broker/data | `dca_live_adapters.py` — `RobinhoodDataProvider`, `FinanceDataProvider`, `RobinhoodBroker`, all built from **injected callables** that map 1:1 to Robinhood MCP tools |
| Money & risk | `dca_risk.py`, `dca_cumulative.py` (cumulative sub-dollar credit ledger), `dca_cost_tracker.py`, `dca_inverse_martingale.py` (opt-in deferred-accumulation sizing mode, off by default — see `docs/inverse_martingale.md`), `dca_whole_share_accumulator.py` (whole-share accumulation for tickers not tradable for fractional buys, e.g. BYDDY; wired into `near_close_dca._run_once_impl` — the 3:30 PM canonical stream only — see `docs/whole_share_accumulation.md`) |
| Output | `dca_reporting.py`, `dca_alerts.py`, `dca_fileio.py`, `dca_notion_sync.py`, `dca_weekly_dashboard.py` |
| Local console | `from.py` → `dca_console.py`, `dca_console_{data,jobs,octopus,reader,routes}.py`, `console/`, `deploy/launchd/` — checkout-only; reads artifacts and runs fixed dry-run/test actions, never places brokerage orders. `dca_console_routes.GET_ROUTES` is the read-only route table, `dca_console_reader.BoundedReader` the shared confined reader |
| Visualization | `dca_market_graph.py` + `viz/` (the 3D market graph, "The Ocean" — the only pixel output here; `viz/vendor/three.min.js` is the repo's only vendored third-party asset, browser-side only, never imported by Python — see `docs/market_graph.md`) |
| Reliability engines | `scripts/broker_reconciliation.py`, `run_state_machine.py`, `run_ledger.py`, `idempotency.py`, `scheduler_lease.py`, `reason_codes.py`, `correlation.py`, `same_day_pointer.py`, `same_day_top_up_gate.py`, `file_lock.py` |
| Ops utilities | `scripts/run_classification.py`, `drift_detection.py`, `backup_strategy.py`, `run_ticker_maintenance.py`, `daily_per_ticker_calibration.py`, `generate_per_ticker_sizing.py`, `workflow_policy.py` |
| Data contracts | `config/*.json` validated against `schemas/*.json` (`tests/test_config_invariants.py` is the gate) |

### Three entry points coexist — know which one you are editing

A fix applied to one does **not** propagate to the others. Full detail in `docs/live_wiring.md`.

| Entry point | Shape | Driven by |
|---|---|---|
| `run_dca.run_slot()` | Function pipeline over `near_close_dca` primitives | `run_dca.py` itself; the 3:30 PM live path. The only path with a real `__main__`. |
| `MorningDCAExecutor` + 3 subclasses | Class hierarchy, one per stream | An external agent constructing the class and calling `.run(now, data, broker=…)`. **No in-repo caller, no CLI.** |
| `dca_runner.py` | Gate sequence + injected callables | An external agent session holding MCP runtime access. |

Consequences to internalize before changing anything:

- **Duplicated logic drifts silently.** The three entry points seed their daily/weekly caps
  from three different sources: `dca_runner`'s Gate 6 sums broker order history, the
  executor classes scan same-day run artifacts, and `near_close_dca.within_caps` reads
  `StrategyState` plus artifact seeding. A cap fix applied to one does not reach the others.
  (The morning preflight gate used to be the example here; both callers now share
  `morning_preflight_eligible_tickers()`.)
- **"No in-repo caller" ≠ dead code.** The executor classes, `dca_runner.py`,
  `dca_cost_tracker.py`, `dca_weekly_dashboard.py`, and `scripts/same_day_top_up_gate.py`
  have zero in-repo callers *by design*. Deleting them breaks live automation this repo
  cannot see. (`dca_whole_share_accumulator.py` used to be an example here too, until it
  was wired into `near_close_dca._run_once_impl` — see `docs/whole_share_accumulation.md`'s
  "Scope" section for exactly which of the three entry points that covers.)
- **Consolidating is a decision, not a cleanup.** Making `run_slot()` delegate to the executor
  classes changes the live 3:30 PM path and needs its own review and acceptance run.

## Hard invariants

These are enforced by tests or by fail-closed runtime code. Violating one fails the build.

- **Secrets.** The brokerage account number lives only in `$ROBINHOOD_AGENTIC_ACCOUNT_NUMBER`
  and is referenced in config as `"${ROBINHOOD_AGENTIC_ACCOUNT_NUMBER}"`, resolved by
  `dca_config.expand_config_env`. Never inline an account-shaped literal in config, source,
  docs, or tests — `test_config_invariants.py` greps for it and fails. `conftest.py` assigns a
  synthetic `999000111` unconditionally so the suite is hermetic even on a machine that exports
  a real value.
- **Config ↔ constant parity.** `near_close_dca.BUDGET_TIERS`, `ACTIVE_TICKERS`, and
  `EXCLUDED_TICKERS` must agree with `config/budgets.json` and `config/tickers.json`. Some code
  paths build a bare `StrategyConfig()` and never see the JSON, so a drift changes behavior on
  some paths and not others. `test_config_invariants.py` is the enforcement.
- **No hard-coded absolute paths.** Artifact roots come from `dca_paths.stream_root(...)` /
  `cron_tracking_root()` (honouring `$DCA_CRON_TRACKING_ROOT`); ticker history from
  `dca_ticker_paths.configured_ticker_history_dir()`. Never write a literal `/home/user/...`.
- **New top-level module or config file ⇒ update `pyproject.toml`.** Add the module to
  `[tool.setuptools] py-modules` and any newly-read config to `[tool.setuptools.data-files]`,
  or it works from a checkout and `ImportError`s / silently falls back to defaults in an
  installed wheel. The existing comment trail in that file records every time this was missed.
- **The CLI is dry-run only.** `run_dca.main()` returns 2 if `config.dry_run` is False, if
  `--slot` is passed, or if `--demo` is absent. Live runs go through `run_pipeline()` /
  `run_slot()` with wired callables, never the CLI.
- **Fail closed, always.** Config loading rejects duplicate JSON keys, rejects valid-JSON-that-
  is-not-an-object, and treats a missing account number as a hard error rather than an empty
  substitution. When reconciliation cannot be proven complete, the run must not submit.

### Environment variables

| Variable | Default | Effect |
|---|---|---|
| `ROBINHOOD_AGENTIC_ACCOUNT_NUMBER` | — | Required for live runs; not needed for `--demo` or tests |
| `DCA_CRON_TRACKING_ROOT` | `<checkout>/cron_tracking` (production exports the shared tree explicitly) | Root of the whole per-stream artifact tree. Read at import time |
| `DCA_CONFIG_DIR` | `config/` beside the modules | Where `config/*.json` is loaded from |
| `DCA_ATTEMPT_LOG_ROOT` | `<cron root>/attempt_log` | Failed-start marker tree; read at call time |
| `DCA_INTRADAY_LIVE_STATE_ROOT`, `DCA_MARKET_OPEN_LIVE_STATE_ROOT` | derived | *Replace* (not extend) recovery/prior-allocation search roots |
| `DCA_DRIFT_SLACK_WEBHOOK` | unset | Webhook for `scripts/drift_detection.py --slack` |

## Run classification

Each currently-functioning stream writes under its **own** artifact root and updates its **own**
pointer — there is no shared `scheduled_runs/` path for most of them:

- market-open → `market_open_dca/` + `latest_market_open_run.json`
- morning → `morning_dca/` + `latest_morning_run.json`
- midday → `midday_dca/` + `latest_midday_run.json`
- intraday → `intraday_dca/` + `latest_intraday_run.json`
- final afternoon only → `scheduled_runs/YYYYMMDD_1530ET/` + `latest_scheduled_run.json`
  (under the legacy `ce16ba6b` root, kept for continuity)

Manual runs write under each stream's own `manual_runs/<timestamp>/` and **never** update a
pointer; reporting excludes manual runs and manual order IDs from scheduled-live metrics. Route
scheduled-vs-manual decisions through `scripts/run_classification.py`. See
`docs/run_classification.md`.

## Conventions

- Standard-library-first in all core modules. No broad `try`/`except` around imports.
- Small functions with intention-revealing names; document new public API.
- **Match the comment density.** The house style is heavy inline commentary explaining *why* a
  line exists, frequently citing the review round that motivated it. Preserve those comments
  when editing nearby code, and write new ones in the same voice.
- Tests live under `tests/`, one `test_<module>.py` per core module (moved out of the repo root
  2026-08-23, where each used to sit flat beside its module); `scripts/` tests live in
  `scripts/tests/`, and each heavy-dependency sibling subproject (`ticker-advisor/`,
  `ml-forecaster/`, `wondrous-octopus/`, `dca-sizing-signals/`, `ticker-market-breadth/`) keeps
  its own `tests/`. Add or update tests for every behavior change, and a regression test for
  every bug fix.
- `trading-strategies/`, `analysis/`, `.github/`, `production-safe-agent-loop/`,
  `ticker-advisor/`, `ml-forecaster/`, `wondrous-octopus/`, and `ticker-market-breadth/` are
  excluded from the core ruff gate and the coverage source — they carry their own deps and
  conventions.
- Check `git status --short` before editing and preserve existing user changes. Keep patches
  small; avoid unrelated refactors. Review the diff for secrets and generated artifacts before
  committing.

## Documentation index

| File | Contents |
|---|---|
| `AGENTS.md` | The repo-wide agent operating contract (secrets policy, PR expectations) |
| `docs/repo_map.md` | **Read this before opening a core module.** Structural map of every runner, report producer, and prediction tool: slot-to-entry-point ownership, which reporting modules have a CLI and which are library-only, and which tools can reach live sizing. Complements `scripts/agent_entry_points.py` (that one is task-oriented: "what command do I run") |
| `docs/README.md` | Doc index + quick-reference facts (universe, ladder, budgets, test window) |
| `docs/local_console.md` | Checkout-only console setup, API, authentication, LAN/proxy access and launchd runbooks; never places brokerage orders |
| `docs/live_wiring.md` | Modules, dependency injection, the three entry points, state persistence, local agent quick reference |
| `scripts/agent_entry_points.py` | Live index of existing entry points to reuse instead of writing new code (`--json`/`--search`) |
| `docs/reliability_spec.md` | Run-state lifecycle, broker snapshot contract, honest implementation-status matrix |
| `docs/safeguards.md` | Binding broker/account rules, budget caps, live execution roster |
| `docs/run_classification.md` | Scheduled-vs-manual contract: paths, required fields, exclusions |
| `docs/schedule_manifest.md` | The canonical recurring schedule |
| `docs/strategy.md` | Strategy overview, ticker universe, sizing ladder, budgets |
| `docs/ticker_advisory.md` | The `ticker-advisor/` tool: scores candidate tickers, proposes universe additions for manual review |
| `docs/inverse_martingale.md` | Opt-in deferred-accumulation sizing mode: tracks dip-tier amounts instead of buying into a decline, fires once on recovery |
| `docs/whole_share_accumulation.md` | Whole-share accumulation for tickers not tradable for fractional buys (built for BYDDY): tracks would-be graded-ladder buys, fires one whole-share purchase once they sum to a share's price. Wired into the 3:30 PM canonical stream only |
| `docs/market_graph.md` | The 3D market graph: axis mapping, Pulse/Wave modes, the four data-source loaders, and the two tiers of behavioural ticker grouping |
| `docs/wondrous_octopus.md` | The `wondrous-octopus/` tool: walk-forward, online-updated next-day OHLC forecasting across the whole archive; research artifacts only, never wired into live sizing |
| `docs/morning_dca.md`, `docs/midday_dca.md` | The two executor-class stream references: modes, task/ET/artifact-root tables, the Jul 10 2026 ladder unification |
| `docs/market_open_signal_entry.md`, `docs/morning_signal_entry.md`, `docs/midday_signal_entry.md`, `docs/intraday_signal_entry.md` | The per-slot Gate 5 signal-generation contracts behind the seven candidate generators (payload shape, flags, output document) |
| `docs/risk_management.md` | The rolling cash-burn report and the 7-day deployment kill switch (`dca_risk.py`) |
| `docs/dca_operational_todos.md` | Operational notes, including the standing Perplexity backup-pass policy behind the seven Claude slots |
| `docs/alert_digest.md` | Why executor gate alerts piled up undelivered in Gmail drafts, and `scripts/alert_digest.py` — the digest that counts them by gate/slot/streak and flags alerts whose delivery channel was off or gitignored |
| `docs/drift_detection.md`, `docs/reporting_workflow.md`, `docs/cost_tracking.md` | Post-run detection, reporting, and cost policy |
| `docs/pr_review_workflow.md` | The two-workflow PR review split and its trust boundary |
| `PRODUCTION_READINESS.md` | Tracked gaps between the library and a fully live system |
| `claude/` | Gate 5 proposal, incident writeups, and `dca-executor-prompts/live-mirror/` (the canonical live executor prompts) |
| `.jules/bolt.md`, `.jules/sentinel.md` | Accumulated performance and security learnings — check before optimizing or touching anything that logs external responses |

## Do not modify

- `tickers.txt`, `historical_stock_data.csv`, `stock_fetch_progress.json` — operational data
  used by the fetch/analysis scripts.
- `FETCH_*.md`, `FILE_MANIFEST.txt`, `FINAL_EXECUTION_REPORT.md`, `TASK_SUMMARY.txt`,
  `README_FETCH_SYSTEM.md`, `README_REPORTING.md` — generated run reports; treat as read-only.
- `ticker_data` — a gitignored symlink to a Google Drive ticker-history archive, not source.
- `cron_tracking/`, `artifacts/` — gitignored local run output.
