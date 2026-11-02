# Graded DCA Strategy Backup

Version-controlled, human-readable backup of the graded DCA strategy: its
sizing ladder, ticker universe, budgets, safeguards, scheduled tasks, reporting
workflow, and Notion template. **No credentials, secrets, or account identifiers
are stored here.** The live account number is referenced as
`"${ROBINHOOD_AGENTIC_ACCOUNT_NUMBER}"` in `config/*.json` and supplied by the
environment (see the repository README).

## Docs

| File | Contents |
|---|---|
| [newcomer_guide.md](newcomer_guide.md) | First-read architecture guide: core components, execution flow, safety boundaries, and a recommended learning path |
| [repo_map.md](repo_map.md) | Structural map of every runner, report producer, and prediction tool: slot-to-entry-point ownership, which reporting modules are library-only, and which tools can reach live sizing. Read before opening a core module |
| [codebase_roadmap.md](codebase_roadmap.md) | End-to-end flow of one live slot, layers with real importer counts, which reliability engines are wired, and where the code differs from older docs |
| [object_model_roadmap.md](object_model_roadmap.md) | What the plan-first object model changed, where handoffs are still wider than needed, the phased path, and the console's checkout-only Architecture map |
| [handoff_2026-10-23_payload_and_prompts.md](handoff_2026-10-23_payload_and_prompts.md) | One-time handoff for the 2026-10-21 9:35 payload abort and the backup-prompt fixes: what happened, what merged, and the repaste steps |
| [module_graph.md](module_graph.md) | Generated import graph of the core modules (importers, import counts, line counts, modules nothing imports); refreshed automatically on every push to `main`, never hand-edited |
| [dead_code_report.md](dead_code_report.md) | Tiered list of unused code, unreferenced files, and duplicated root files, with the method used and its limits |
| [local_ci.md](local_ci.md) | Running CI locally: `scripts/local_ci.sh` (fast copy of the gates; `scripts/local_ci_status.sh` posts its result as a `local-ci` commit status) and `scripts/run_ci_locally.sh` (the real `ci.yml` under act + Colima: setup, job ids, what differs from GitHub's runners); the measured Actions-minute breakdown, the 2026-10-31 trigger cuts, and the opt-in self-hosted-runner switch (`CI_RUNS_ON`) |
| [local_console.md](local_console.md) | `python3 from.py`: checkout-only local interface, real-data by default with an opt-in demo sandbox, daily summaries, timing and Octopus predictions; authenticated LAN/proxy access and launchd runbooks; never places brokerage orders |
| [console_changelog.md](console_changelog.md) | Versioned changelog for `console/` and `dca_console*.py`; every UI change needs an entry and a version bump |
| [robinhood_payload.md](robinhood_payload.md) | Prebuilt Robinhood slot payload: Codex on the Mac builds the market-data payload ahead of a live slot so the session does not have to pull it live |
| [reliability_spec.md](reliability_spec.md) | Formal run-state, reconciliation, and artifact-contract spec + honest implementation-status matrix |
| [gate6_history_completeness_abort_2026-10-22.md](gate6_history_completeness_abort_2026-10-22.md) | 2026-10-22 live-slot abort on unproven weekly-history completeness, the log-and-continue decision, its risk and a revisit trigger |
| [incident_2026-10-26_0935_unrecognized_orders.md](incident_2026-10-26_0935_unrecognized_orders.md) | 2026-10-26 refused 0935 payload and the 25 buys the Perplexity backup placed on its own cloud-computed plan, the cap check, why no evidence was forged, and open items |
| [strategy.md](strategy.md) | Strategy overview, ticker universe, latest sizing ladder, budgets |
| [yfinance_replacement_plan.md](yfinance_replacement_plan.md) | Where yfinance is allowed (non-buying scheduled tasks only), the `data` extra that declares it, who uses it today, and the plan to replace it |
| [ticker_advisory.md](ticker_advisory.md) | The `ticker-advisor/` tool: scores candidate tickers, proposes universe additions for manual review |
| [live_wiring.md](live_wiring.md) | Live providers/broker adapters, orchestrator/CLI, and the two placement safety gates |
| [inverse_martingale.md](inverse_martingale.md) | Opt-in deferred-accumulation ("inverse Martingale") sizing mode: tracks dip-tier amounts instead of buying into a decline, fires once on recovery |
| [whole_share_accumulation.md](whole_share_accumulation.md) | Whole-share accumulation for tickers not tradable for fractional buys (built for BYDDY): tracks would-be graded-ladder buys, fires one whole-share purchase once they sum to a share's price. Wired into the 3:30 PM canonical stream only |
| [plan_first_recommendations.md](plan_first_recommendations.md) | Plan-first recommendations (the 5-minute Claude courier is retired; the payload program fetches the data and runs `ingest`, opt-in via `DCA_PLAN_FIRST_INGEST=1`): the program computes `{ticker, usd, ref_id}` per slot with the primary's own gates, and the buying agent reads only that (no prices); freshness rule, fill comparison, publishing. Drafts only until shadow mode completes |
| [sp500_snapshot_design.md](sp500_snapshot_design.md) | Proposed S&P 500 market snapshot task; design only, not implemented or scheduled |
| [safeguards.md](safeguards.md) | Broker/account, trading constraints, budget caps, test window |
| [schedule_manifest.md](schedule_manifest.md) | The canonical 15-row recurring schedule (17 rows verified 2026-07-27; 2 removed 2026-08-02), one-time tasks, retired entries, and the expired dated scheduler overrides (kept as historical record, authorizing nothing today) |
| [run_classification.md](run_classification.md) | Scheduled-vs-manual run contract (paths, required fields, exclusion rules) |
| [schedule_verification.md](schedule_verification.md) | Daily per-slot evidence audit (`scripts/verify_schedule.py`) and the Gate 9 finalizer every live prompt runs |
| [workspace_probe.md](workspace_probe.md) | Read-only probe that shows whether a scheduled cloud run keeps its `cron_tracking` files between fires (`scripts/workspace_probe.py`) |
| [cloud_clone_runs.md](cloud_clone_runs.md) | Running the live slots from a cloud clone: pinned-release preflight, the private evidence store, publish, and the slot claim (`scripts/cloud_preflight.py`, `scripts/evidence_publish.py`) |
| [drift_detection.md](drift_detection.md) | Post-artifact signals-vs-submitted-orders drift detector + Slack alerting |
| [adhoc_orders.md](adhoc_orders.md) | Fallback for placing a known list of buys when the scheduled path is unavailable: `scripts/adhoc_orders.py` validates it against the universe, caps, attempt cap and buying power, writes a manual-run plan and execution state, and never places orders from the CLI |
| [alert_digest.md](alert_digest.md) | Why Gate 9 alerts piled up undelivered in Gmail drafts, and the digest that counts them by gate/slot/streak and flags dead delivery channels |
| [triage_routine.md](triage_routine.md) | The daily propose-only DCA triage routine: what it reads and may write in Notion, the Progress labels `selfheal/triage_reconcile.py` derives, and the digest it emits; the prompt itself is `claude/triage-routine/prompt.txt` |
| [intraday_archive.md](intraday_archive.md) | Per-poll price/timestamp/volume archive under `ticker_data/_intraday/`, supplementing the daily OHLCV history |
| [sp500_snapshot_design.md](sp500_snapshot_design.md) | Proposed read-only S&P 500 market snapshot task, separate from the buy-universe courier; not implemented or wired into live sizing |
| [purchase_history.md](purchase_history.md) | Consolidated buy-history ledger (`scripts/purchase_history.py`): every confirmed fill ever placed, at what price, across every stream and day |
| [average_cost_snapshot.md](average_cost_snapshot.md) | Weekly cross-account average-cost-basis snapshot (`dca_average_cost_snapshot.py`): per-account and combined average cost per ticker, built from externally-fetched Robinhood positions |
| [slack_webhook_injection.md](slack_webhook_injection.md) | How to inject the `DCA_DRIFT_SLACK_WEBHOOK` webhook across cron/systemd, agent/MCP scheduler, local shell, and GitHub Actions |
| [market_graph.md](market_graph.md) | The 3D market graph ("The Ocean"): Pulse/Wave modes, the volume/high-low/open-close axes, and the two tiers of behavioural ticker grouping |
| [reporting_workflow.md](reporting_workflow.md) | Daily/periodic artifact + Notion flow |
| [notion_dashboard_template.md](notion_dashboard_template.md) | Notion page template + conceptual API sync |
| [cost_tracking.md](cost_tracking.md) | Cost-control policy and usage proxies |
| [credit_usage_proxy_report.md](credit_usage_proxy_report.md) | One-time 2026-07-27 4:30 PM ET credit-usage proxy report contract, including the separate recovery-run section |
| [restore_runbook.md](restore_runbook.md) | Restore procedure + audit checklist |
| [backtest_curve.md](backtest_curve.md) | Analytics-only weekly-rollover intraday cap curve backtest (35 / 65 / 100 candidate) |
| [workflow_policy.md](workflow_policy.md) | The CI-enforced workflow policy checks (job timeouts, concurrency, checkout credentials) and how to declare a reasoned exception |
| [scheduled_session_repo_access.md](scheduled_session_repo_access.md) | Dated probe log for whether a Routine-fired scheduled session actually has working repo (clone/fetch/push) and environment (account number) access |
| [morning_dca.md](morning_dca.md) | The morning stream (`MorningDCAExecutor`): the 10:30 live mode (the 9:45 watchlist mode was retired; `mode` is required), task/ET/artifact-root tables, the Jul 10 2026 ladder unification |
| [midday_dca.md](midday_dca.md) | The midday stream (`MiddayDCAExecutor`): 12:30 live mode, trend validation, artifact contract |
| [market_open_signal_entry.md](market_open_signal_entry.md) | Gate 5 signal generation for the 9:35 AM market-open slot (`scripts/generate_market_open_candidates.py`): payload contract, refresh step, output document |
| [morning_signal_entry.md](morning_signal_entry.md) | Gate 5 signal generation for the 10:30 AM morning slot (`scripts/generate_morning_candidates.py`) |
| [midday_signal_entry.md](midday_signal_entry.md) | Gate 5 signal generation for the 12:30 PM midday slot (`scripts/generate_midday_candidates.py`) and its required 7-day deployment figure |
| [intraday_signal_entry.md](intraday_signal_entry.md) | Gate 5 signal generation for the 11:30 AM, 1:30 PM and 2:30 PM intraday slots (`scripts/generate_intraday_candidates.py --slot`) |
| [GATE5_FIX.md](GATE5_FIX.md) | The Gate 5 failure fix: how signal generation became a real, tested entry point per slot |
| [risk_management.md](risk_management.md) | Dynamic risk management: the rolling cash-burn report and the 7-day deployment kill switch (`dca_risk.py`) |
| [dca_operational_todos.md](dca_operational_todos.md) | Operational notes, including the standing Perplexity backup-pass policy behind the seven Claude slots |
| [OPEN_ISSUES_2026-10-22.md](OPEN_ISSUES_2026-10-22.md) | Open issue record: Gate 6 weekly-history completeness aborting the 0935 and 1030 live slots; operator decision is log-and-continue, fix not started |
| [adr-octopus-ledger-filesystem-boundary.md](adr-octopus-ledger-filesystem-boundary.md) | ADR: the Octopus ledger's descriptor-relative, no-follow filesystem boundary |
| [local_env_file.md](local_env_file.md) | The gitignored local `.env` that supplies the account number and alert address to the executor sandbox: setup, verification, troubleshooting (`dca_env.py`) |
| [adr-dotenv-loader.md](adr-dotenv-loader.md) | ADR: why a small allowlisted, mode-0600 `.env` loader runs from `dca_paths` instead of per-command sourcing in the prompts |
| [adr-simultaneous-start-guard.md](adr-simultaneous-start-guard.md) | ADR: closing the simultaneous-start race (two executors both seeing an empty window) with a per-order history re-pull, a slot claim and the deterministic broker `ref_id`; DCA Issues keys `duplicate_fills`, `competing_executor` |
| [adr-incremental-order-history.md](adr-incremental-order-history.md) | ADR: incremental order history, batched tradability and an audited per-order record for the 9:35 slot (`scripts/merge_order_pages.py`, `scripts/check_per_order_record.py`) |
| [adr-plan-first-backup-executor.md](adr-plan-first-backup-executor.md) | ADR (proposed): plan files, Mac-first backup with compute fallbacks, fail-closed re-validation; `dca_order_plan.py` and `schemas/order_plan.schema.json` |
| [adr-price-source-massive-alpaca.md](adr-price-source-massive-alpaca.md) | ADR (proposed): Massive (primary) + Alpaca (failover, intraday IEX) replace Yahoo as the ongoing price source; append-only point-in-time DuckDB store under `dca-sizing-signals/market_data_ingest/prices/`, Yahoo kept as `yahoo_legacy`; not wired into live sizing |
| [adr-comovement-signals.md](adr-comovement-signals.md) | ADR (proposed): pairs and co-movement as suggest-only signals, not a strategy: cluster concentration cap, correlated-selloff detector, pair-breakdown warning, cheaper-leg tilt (shadow), staples-vs-semis regime log |
| [price_source_poc_report.md](price_source_poc_report.md) | The opt-in live PoC report for that price source (20 tickers x 60 sessions vs Yahoo) |
| [verified_sizing_and_backup_facts.md](verified_sizing_and_backup_facts.md) | Re-checkable repo facts found during the backup-executor work: live per-ticker sizing and the $16 cap, stale "$10"/"5x" prose, same-day repeats, where sources disagree |
| [wondrous_octopus.md](wondrous_octopus.md) | The `wondrous-octopus/` tool: walk-forward, online-updated next-day OHLC forecasting across the whole archive; research artifacts only, never wired into live sizing |

## Machine-readable backup

- [`../config/`](../config) — tickers, sizing ladder, budgets, scheduled tasks, notifications, reporting.
- [`../schemas/`](../schemas) — JSON schemas for config, run-state, slippage, cost-tracker, and model-usage-log artifacts.
- [`../scripts/`](../scripts) — `backup_strategy.py` to re-extract config from a transcript, `run_classification.py` to enforce the scheduled-vs-manual contract, `purchase_history.py` to build the consolidated buy-history ledger, plus tests.
- **Reliability engines** (see [reliability_spec.md](reliability_spec.md)): `broker_reconciliation.py` (fail-closed same-day reconciliation + effective prior), `reason_codes.py` (unified decision codes), `run_state_machine.py`, `run_ledger.py`, `correlation.py`, `scheduler_lease.py`, `idempotency.py`. Implemented and unit-tested; wiring into the live decision path is a tracked follow-up.

## Key facts (quick reference)

- **Active universe:** 91 tickers (includes `CCI` added 2026-10-23, and `GFUZ`, `TXN`, `RKLB`, the
  2026-08-02 additions `GE`, `VRT`, `CAT`, `QQQ`, `QCOM`, `ABBV`, `JPM`, the
  2026-08-04 addition `VT`, the 2026-08-16 additions `QBUT`,
  `RVI`, `VCX`, `TRLV`, the 2026-09-05 additions `LLY`, `TSLA`, and the
  2026-09-11 additions `VIAV`, `CIEN`, `ANET`, `CSCO`, `FFIV`, `COHR`, the 2026-10-12 additions `APH`, `TEL`, the 2026-10-14 addition `GFS`, and the 22 free-cash-flow-yield names added 2026-10-19 (`TSN`, `RPRX`, `CVS`, `SJM`, `KR`, `CAH`, `HCA`, `HRL`, `PFGC`, `SYY`, `KVUE`, `HSY`, `USFD`, `MRK`, `RMD`, `DGX`, `CHE`, `ABT`, `MDLZ`, `MCK`, `CHD`, `CL`) — all
  whitelisted without a broker instrument search, so unverified until their
  first live run — plus `QUBT`, added 2026-09-24 as the corrected spelling of
  `QBUT` after Robinhood resolved it; `QBUT` itself stays excluded; `near_close_dca.ACTIVE_TICKERS` and `config/tickers.json`
  are the count of record and `tests/test_config_invariants.py` pins them);
  `BYDDY` and `RVII` are whole-share tickers, not excluded (fractional-tradability; `RVII` moved
  out of the active list 2026-08-24 — see [whole_share_accumulation.md](whole_share_accumulation.md)); `IRDMI→IRDM`, `GLWR→GLW`, `QBUT→QUBT`; `GFUZ` uses
  Robinhood primary data (finance `NOT_FOUND`).
- **Latest ladder:** 0.1%→16%+ log-scaled, base $2, max **8x** (no upper cutoff). Older
  0.1%–12% ladders are **superseded**.
- **Budgets:** $2 base · tiered caps selected per run solely by 3x+ signal
  count (never planned spend) — Normal $180/$900 · Elevated $240/$1,200 · High-vol $300/$1,500 ·
  Stress $360/$1,800 (daily/weekly); weekly rollover reserve capped at $900.
- **Live execution:** the prior Perplexity-run three-stream model
  (`2ba19526`/`42173154`/`7be82b97`/`a6239dae`/`10ce362c`) is still the
  account's ONLY currently-functioning live execution. A seven-slot Claude-executed roster (weekdays 9:35 AM through 3:30 PM ET) is the intended eventual replacement. Its Gate 5 (the repo's own signal generation) has had a real, tested entry point per slot since 2026-08-26 (`PRODUCTION_READINESS.md`, "Status, 2026-08-26"), and placement was authorized for all seven slots on 2026-09-02; the earlier "hard-aborts at Gate 5" wording in this and other docs described the state before that. [safeguards.md](safeguards.md#live-execution-roster) is authoritative for what is live today.
  Robinhood account `<ROBINHOOD_ACCOUNT_NUMBER>` only; Alpaca read-only.
  The **5:00 PM ET** task is analytics (slippage + Notion sync; retimed
  2026-08-02 from 3:35 PM ET).
- **Run classification:** each currently-functioning stream writes under its OWN
  artifact root and pointer, not a shared `scheduled_runs/` path: market-open
  (`2ba19526`) under `market_open_dca/` + `latest_market_open_run.json`, morning
  (`42173154`) under `morning_dca/` + `latest_morning_run.json`, midday
  (`a6239dae`) under `midday_dca/` + `latest_midday_run.json`, intraday
  (`7be82b97`) under `intraday_dca/` + `latest_intraday_run.json`; only the final
  afternoon stream (`10ce362c`) uses `scheduled_runs/YYYYMMDD_1530ET/` and
  `latest_scheduled_run.json` (under the legacy `ce16ba6b` root). The
  canonical live-mirror prompts (`claude/dca-executor-prompts/live-mirror/`)
  write exactly these five stream pointers, the ones
  `scripts/same_day_pointer.py` maps; the slot-suffixed
  `latest_scheduled_run_<slot HHMM>ET.json` names and the `claude_roster_1530/`
  root appear only in the SUPERSEDED prompt generation
  (`claude/dca-executor-prompts/1-…6-executor-*.txt`) and are never written or
  read by any Python in the repo. Its 3:30 PM slot writes to its own
  separate `claude_roster_1530/` root
  (never the legacy `ce16ba6b` one, to avoid colliding with `10ce362c`'s real
  evidence). Manual runs write under each stream's own `manual_runs/<timestamp>/`
  and never update that stream's pointer. Reporting excludes manual runs/order
  IDs from scheduled-live metrics. See [run_classification.md](run_classification.md).
- **Afternoon completion (rewritten 2026-08-02):** the 3:05 PM monitor-only
  signal check (`e3e6f09e`) and the 3:40 PM read-only watchdog (`a46bf007`)
  were both **removed from the schedule entirely**. Afternoon **trading** now
  completes **no later than 3:30 PM ET** (`10ce362c`). Same-day
  artifact-writing moved later: the post-trade slippage + Notion sync
  (`67aa802b`) moved from 3:35 PM ET to **5:00 PM ET daily**, and the Friday
  weekly Notion refresh (`041d9d90`) moved from 5:00 PM ET to **5:30 PM ET**
  the same day to clear the resulting Friday collision. The month-end
  volatility risk refresh (`c9fb1fb8` / `17a3b2a9`) moved from its own prior
  5:00 PM ET to **5:15 PM ET** the same day for the same reason -- it
  collided with `67aa802b`'s new daily 5:00 PM slot too. These three tasks
  are the only DCA-related tasks approved to run after 3:30 PM ET.
- **Test window:** through **2026-10-30** inclusive per `config/trading_window.json`
  (extended 2026-07-26 from the original 2026-08-28 end date; stops the Friday
  before the 2026-11-01 DST change since the live tasks' cron schedules are
  fixed-UTC/EDT-only — see the window file's `description` for why. This is the
  in-repo code gate — verify the external scheduler's own
  `programmatic_trigger_check.sh` trigger window separately, since it lives
  outside this repo).
