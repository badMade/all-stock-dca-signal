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
| [handoff_2026-11-15_scripted_gates.md](handoff_2026-11-15_scripted_gates.md) | One-time handoff for the scripted primary gates (clock, budget, Gate 4) and parallel per-order reads: what changed, open PR, and the Monday 2026-11-16 checks |
| [handoff_2026-10-23_payload_and_prompts.md](handoff_2026-10-23_payload_and_prompts.md) | One-time handoff for the 2026-10-21 9:35 payload abort and the backup-prompt fixes: what happened, what merged, and the repaste steps |
| [follow_up_2026-11-10_payload_coverage.md](follow_up_2026-11-10_payload_coverage.md) | The 2026-11-10 9:35 and 10:30 payloads were refused on the 95% coverage floor after Codex 429s; the three decisions made 2026-11-11 (mitigations live, keep the floor, no executor rebuild) and the clean-day count still being watched |
| [module_graph.md](module_graph.md) | Generated import graph of the core modules (importers, import counts, line counts, modules nothing imports); refreshed automatically on every push to `main`, never hand-edited |
| [dead_code_report.md](dead_code_report.md) | Tiered list of unused code, unreferenced files, and duplicated root files, with the method used and its limits |
| [bug_patterns.md](bug_patterns.md) | Confirmed recurring bug patterns (symptom, root cause, how to check for it, guard) and the template for adding one; security and performance learnings stay in `.jules/` |
| [follow_up_2026-11-10_payload_coverage.md](follow_up_2026-11-10_payload_coverage.md) | Open follow-up: the 9:35 payload was refused on the 95% coverage floor after 429s (2026-11-10) |
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
| [slos.md](slos.md) | Proposed service level objectives (slot fires, orders reconcile, no over-deployment, alerts delivered, missed fires detected) with error-budget rules; measurement only, no runtime change |
| [emergency_stop.md](emergency_stop.md) | How to halt all live trading in layers (pause scheduled tasks, fail live runs closed, revoke broker access), what the code already does, and a rehearsal checklist |
| [artifact_retention.md](artifact_retention.md) | Proposed run-artifact retention policy and the rules any future prune job must follow; no pruning code exists |
| [opt_in_hardening_tools.md](opt_in_hardening_tools.md) | Unwired building blocks for the readiness roadmap: `dca_retry` (retry/backoff that never resends a possibly-landed order), cap-parity tests across the three entry points, and a derived SQLite run-state index; what was deliberately not done |
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
| [backtest-gap-audit.md](backtest-gap-audit.md) | Read-only audit of the 17-item backtest protocol against the repo (present/partial/missing, file:line evidence, proposed CI test per item) |
| [weekly_readiness_check_prompt.md](weekly_readiness_check_prompt.md) | A fixed-rubric, read-only prompt for a scheduled agent that scores the repo weekly against ten external standards (SEC 15c3-5 benchmark, SRE PRR, ASVS L1, NIST SSDF, OpenSSF Scorecard, SLSA, Well-Architected, ISO 25010, FINRA-style records, SOC 2) and reports week-over-week change; a self-assessment, not an audit |
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
| [kill_switch_evidence_gap_proof.md](kill_switch_evidence_gap_proof.md) | Design for proving missing afternoon evidence represents zero deployment at the midday Gate 5 |
| [dca_operational_todos.md](dca_operational_todos.md) | Operational notes, including the standing Perplexity backup-pass policy behind the seven Claude slots |
| [OPEN_ISSUES_2026-10-22.md](OPEN_ISSUES_2026-10-22.md) | Open issue record: Gate 6 weekly-history completeness aborting the 0935 and 1030 live slots; operator decision is log-and-continue, fix not started |
| [adr-octopus-ledger-filesystem-boundary.md](adr-octopus-ledger-filesystem-boundary.md) | ADR: the Octopus ledger's descriptor-relative, no-follow filesystem boundary |
| [adr-octopus-band-scorecard.md](adr-octopus-band-scorecard.md) | ADR (proposed; Rocks 1 to 4 built): interval score and breach-side fields for the Octopus bands, date-block uncertainty, and replay-only evaluation of candidate calibrators |
| [local_env_file.md](local_env_file.md) | The gitignored local `.env` that supplies the account number and alert address to the executor sandbox: setup, verification, troubleshooting (`dca_env.py`) |
| [adr-dotenv-loader.md](adr-dotenv-loader.md) | ADR: why a small allowlisted, mode-0600 `.env` loader runs from `dca_paths` instead of per-command sourcing in the prompts |
| [adr-simultaneous-start-guard.md](adr-simultaneous-start-guard.md) | ADR: closing the simultaneous-start race (two executors both seeing an empty window) with a per-order history re-pull, a slot claim and the deterministic broker `ref_id`; DCA Issues keys `duplicate_fills`, `competing_executor` |
| [adr-incremental-order-history.md](adr-incremental-order-history.md) | ADR: incremental order history, batched tradability and an audited per-order record for the 9:35 slot (`scripts/merge_order_pages.py`, `scripts/check_per_order_record.py`) |
| [adr-plan-first-backup-executor.md](adr-plan-first-backup-executor.md) | ADR (proposed 2026-10-06; later amendments accepted, Phases 2 to 5 built): plan files, Mac-first backup with compute fallbacks, fail-closed re-validation; `dca_order_plan.py` and `schemas/order_plan.schema.json` |
| [adr-price-source-massive-alpaca.md](adr-price-source-massive-alpaca.md) | ADR (proposed; built, not wired): Massive (primary) + Alpaca (failover, intraday IEX) replace Yahoo as the ongoing price source; append-only point-in-time DuckDB store under `dca-sizing-signals/market_data_ingest/prices/`, Yahoo kept as `yahoo_legacy`; not wired into live sizing |
| [adr-selfheal-learning-loop.md](adr-selfheal-learning-loop.md) | ADR (accepted 2026-11-03; design only, nothing built): a learning loop for self-healing in its own append-only `selfheal.duckdb`; scores each repair adapter (held, regressed, time to repair), shadow-first adapters with operator-approved promotion and demotion; `selfheal/` stays stdlib-only |
| [adr-program-maintained-universe.md](adr-program-maintained-universe.md) | ADR (in progress, operator-directed 2026-11-19): program-maintained `config/tickers.json`; supersedes item 8 of the dynamic-universe ADR; rank all names, buy the daily top 100 with no phase-in, one gated writer with approval ID and history row |
| [adr-backup-planner-daily-universe.md](adr-backup-planner-daily-universe.md) | ADR (accepted 2026-11-20; pieces 1 to 4b built, prompt repaste pending): how a daily-changing universe reaches the Perplexity backup planner (the plan carries the list, checked against a hash published for the day); options, risks, verification |
| [fundamentals_feed_scope.md](fundamentals_feed_scope.md) | Scope (no code): the SEC fundamentals, sector and price-derived inputs the daily ranking needs, what exists, the gaps, a 7-step test-first build order, and the operator decisions (ETFs and foreign filers, fractional eligibility, value factor) |
| [adr-dynamic-universe.md](adr-dynamic-universe.md) | ADR (accepted 2026-11-03; item 8 superseded by the program-maintained ADR): rules-based universe capped at 100 active tickers; sector-neutral four-factor score, 75/150 entry and exit buffers, 8 swaps per quarter, a 150-name shadow bench, fail-closed proposals; research only; the program-maintained ADR's gated writer now applies its proposals |
| [adr-outcome-ledger-pit-harness.md](adr-outcome-ledger-pit-harness.md) | ADR (accepted 2026-11-03; storage built, loaders partly built): Phase 0 outcome ledger, read-only tax-lot ledger across all Robinhood accounts, point-in-time harness and trial registry on the existing `dca-sizing-signals` DuckDB store; suggestions only |
| [adr-backtest-protocol.md](adr-backtest-protocol.md) | ADR (proposed): DCA backtest harness and statistics layer (next-tradable fills, cash at T-bill yield, plain DCA and lump-sum benchmarks, paired terminal wealth, walk-forward, fail-closed trial registry, DSR/PBO/SPA/StepM, regime reports, sealed holdout, pre-registered shadow log, reproducible manifests); research only, no new gating dependencies |
| [mc_variance_reduction.md](mc_variance_reduction.md) | Research-only Monte Carlo harness (`dca-sizing-signals/mc_research/`): CRN plus a flat-DCA control variate for ladder A/B tests (~25x / ~104x variance cut on a GBM pilot), antithetic and IS estimators with an ESS guard; measurement, not edge; ten open questions before any tier-C promotion study |
| [adr-budget-exhaustion-replay.md](adr-budget-exhaustion-replay.md) | ADR (accepted 2026-11-23, revision 3; implemented as `python -m scripts.budget_exhaustion_count`): read-only count, from each slot's saved Gate 5 candidates and order history, of how often a later slot had a live-sized buy that the remaining daily or weekly budget could not fund; gate for the Markov MDP and absorbing-chain ideas; CLI only |
| [adr-comovement-signals.md](adr-comovement-signals.md) | ADR (accepted 2026-11-03; all five built, none scheduled): pairs and co-movement as suggest-only signals, not a strategy: cluster concentration cap, correlated-selloff detector, pair-breakdown warning, cheaper-leg tilt (shadow), staples-vs-semis regime log |
| [adr-factor-residual-dip-signals.md](adr-factor-residual-dip-signals.md) | ADR (accepted 2026-11-23; F1+F2 built): APT/factor decomposition as suggest-only signals: shrunk market+sector betas, factor exposure report, factor-share dampener, earnings/8-K flag, residual dip z-score (shadow), regime fields (log only); flags the reader's smallest-\|adj\| combine rule |
| [inventory_reservation_suggester.md](inventory_reservation_suggester.md) | Research-only, shrink-only inventory-reservation suggester (`dca-sizing-signals/inventory_signals/`): cost-basis overweight shrinks size and deepens the top-up discount, scaled by variance left in the day; shadow mode, validation plan, and the reader-combination gap blocking consumption |
| [adr-residual-reversal-suggester.md](adr-residual-reversal-suggester.md) | ADR (proposed): research-only, budget-neutral sector-ETF residual-reversal suggester (OU s-score tilt in [0.75, 1.25], fail-closed relationship-break guard); unscored until the Phase 0 ledger scores it |
| [adr-cost-basis-backtest-harness.md](adr-cost-basis-backtest-harness.md) | ADR (proposed): offline, stdlib walk-forward harness replaying the graded baseline and a candidate suggester on the same cash flows; paired per-ticker cost-basis improvement, tilt-dollar returns, score-bucket promotion test, lookahead rejection, deflated Sharpe and PBO; research only |
| [adr-csp-shadow-log.md](adr-csp-shadow-log.md) | ADR (accepted 2026-11-03; library built, capture task not registered): a 6-month suggest-only shadow log of cash-secured puts on DCA tickers, entered at the bid, scored against the cash sweep and plain DCA; no options order path; respects the tax-loss buy pause |
| [adr-tax-loss-sale.md](adr-tax-loss-sale.md) | ADR (accepted 2026-11-03; research library built, no data feed): the only allowed sell type, an operator-approved tax-loss sale of specific lots with a 31-day buy pause before and after the sale; never automatic, fail closed on any lookback doubt; design only |
| [adr-goals-and-loss-hedge-shadow.md](adr-goals-and-loss-hedge-shadow.md) | ADR (accepted 2026-11-13): operator goals as versioned data (`config/goals.json`, a TWR CAGR target over a rolling horizon, aspirational, never a floor), a console Goals page that proposes changes as pull requests, and a shadow-only loss hedge (options and crypto) beside the CSP shadow log; hedge execution locked in code, no live sizing change; tests written first |
| [adr-overnight-futures-tracker.md](adr-overnight-futures-tracker.md) | ADR (accepted 2026-11-12, proposed defaults): an observe-only `OvernightGapForecastProvider` that forecasts 9:35 gap size and dispersion from fair-value-adjusted ES/NQ/YM moves; `ForecastProvider` only, no `SizingSuggester`, no suggestion file; descriptive expiry, rebalance and ex-dividend flags; 6-month shadow and pinball-loss gate before any later wiring ADR |
| [adr-kalman-residual-dip-shadow.md](adr-kalman-residual-dip-shadow.md) | ADR (accepted 2026-11-23, proposed defaults; Phase 1 offline only): a shadow-only Kalman time-varying beta vs SPY and the ticker's co-movement cluster, logging a standardized intraday residual `z_resid` and an idiosyncratic/cluster/market dip label; no `SizingSuggester`, own JSONL file, D7 calibration and forward-return gate before any wiring ADR. Research: `research/2026-11-23-kalman-filters.md` |
| [adr-prediction-markets.md](adr-prediction-markets.md) | ADR (proposed; Polymarket phase built): point-in-time ingestion of prediction-market and forecasting-platform probabilities (Polymarket Intl and US, Kalshi, index binaries, Metaculus, GJ Open) as research inputs; company-event markets feed volatility and downside risk, not expected return |
| [adr-prediction-markets-methods.md](adr-prediction-markets-methods.md) | Reference companion to `adr-prediction-markets.md`: how stored probabilities become forecast inputs, with the evidence gaps (no calibration study for CEO-succession markets, index ladders vs options, long-dated longshot bias) |
| [price_source_poc_report.md](price_source_poc_report.md) | The opt-in live PoC report for that price source (20 tickers x 60 sessions vs Yahoo) |
| [verified_sizing_and_backup_facts.md](verified_sizing_and_backup_facts.md) | Re-checkable repo facts found during the backup-executor work: live per-ticker sizing and the $16 cap, stale "$10"/"5x" prose, same-day repeats, where sources disagree |
| [sizing_suggestion_history.md](sizing_suggestion_history.md) | Append-only per-suggester history of sizing recommendations (`ml_forecaster`, `octopus`): why no suggester writes live sizing, the JSONL record shape, and what the later comparison needs |
| [market_open_0932_move.md](market_open_0932_move.md) | Proposal (not implemented) to fire the market-open slot at 09:32 on a live-quote decision price; the full change list and the shadow-only `market_open_quote_shadow` recorder that gathers evidence first |
| [research/2026-11-11-overnight-futures.md](research/2026-11-11-overnight-futures.md) | Research input (not an ADR): overnight ES/NQ/YM futures vs next-day open gaps, false reversals, catalysts, index arbitrage and dividend hedging; feeds the proposed overnight futures tracker ADR. Suggest-only, gap-size and volatility context, not directional alpha |
| [research/2026-11-23-backtest-overfitting.md](research/2026-11-23-backtest-overfitting.md) | Research input (not an ADR): backtest overfitting and all-stock-dca; DSR, PBO/CSCV, SPA/StepM, M1/M2 metrics versus plain DCA, regime tests, pre-registered shadow power analysis; feeds Amendment 1 of the backtest protocol ADR. Claims unverified, see its sourcing note |
| [research/2026-11-24-residual-reversal-cost-basis-results.md](research/2026-11-24-residual-reversal-cost-basis-results.md) | Research output (not a decision): first real-data cost-basis harness run of residual_reversal vs a replayed graded ladder, 2021-2026; +1.4 bp out of sample (t 1.0), tilt dollars negative, score buckets not monotone, DSR 0.60, PBO 0.60; promotion failed |
| [adr-no-buy-window.md](adr-no-buy-window.md) | Dated `no_buy` block in `config/tickers.json`: pauses buys of tickers sold at a loss for the wash-sale window, enforced through the shared loader and `dca_runner`, with a recorded early release (`scripts/no_buy_window.py`) |
| [wondrous_octopus.md](wondrous_octopus.md) | The `wondrous-octopus/` tool: walk-forward, online-updated next-day OHLC forecasting across the whole archive; research artifacts only, never wired into live sizing |

## Machine-readable backup

- [`../config/`](../config) — tickers, sizing ladder, budgets, scheduled tasks, notifications, reporting.
- [`../schemas/`](../schemas) — JSON schemas for config, run-state, slippage, cost-tracker, and model-usage-log artifacts.
- [`../scripts/`](../scripts) — `backup_strategy.py` to re-extract config from a transcript, `run_classification.py` to enforce the scheduled-vs-manual contract, `purchase_history.py` to build the consolidated buy-history ledger, plus tests.
- **Reliability engines** (see [reliability_spec.md](reliability_spec.md)): `broker_reconciliation.py` (fail-closed same-day reconciliation + effective prior), `reason_codes.py` (unified decision codes), `run_state_machine.py`, `run_ledger.py`, `correlation.py`, `scheduler_lease.py`, `idempotency.py`. Implemented and unit-tested; wiring into the live decision path is a tracked follow-up.

## Key facts (quick reference)

- **Active universe:** 122 tickers (includes the 31 composite-rank names added 2026-11-06, `LULU` added 2026-11-05, `CCI` added 2026-10-23, and `GFUZ`, `TXN`, `RKLB`, the
  2026-08-02 additions `GE`, `VRT`, `CAT`, `QQQ`, `QCOM`, `ABBV`, `JPM`, the
  2026-08-04 addition `VT`, the 2026-08-16 additions `QBUT`,
  `RVI`, `VCX`, `TRLV`, the 2026-09-05 additions `LLY`, `TSLA`, and the
  2026-09-11 additions `VIAV`, `CIEN`, `ANET`, `CSCO`, `FFIV`, `COHR`, the 2026-10-12 additions `APH`, `TEL`, the 2026-10-14 addition `GFS`, and the 22 free-cash-flow-yield names added 2026-10-19 (`TSN`, `RPRX`, `CVS`, `SJM`, `KR`, `CAH`, `HCA`, `HRL`, `PFGC`, `SYY`, `KVUE`, `HSY`, `USFD`, `MRK`, `RMD`, `DGX`, `CHE`, `ABT`, `MDLZ`, `MCK`, `CHD`, `CL`) — all
  whitelisted without a broker instrument search, so unverified until their
  first live run — plus `QUBT`, added 2026-09-24 as the corrected spelling of
  `QBUT` after Robinhood resolved it; `QBUT` itself stays excluded, as does `KVUE` (moved out 2026-11-02: Robinhood reports it as an inactive instrument); `near_close_dca.ACTIVE_TICKERS` and `config/tickers.json`
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
