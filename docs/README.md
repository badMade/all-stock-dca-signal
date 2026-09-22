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
| [local_console.md](local_console.md) | `python3 from.py`: checkout-only local interface, real-data by default with an opt-in demo sandbox, daily summaries, timing and Octopus predictions; authenticated LAN/proxy access and launchd runbooks; never places brokerage orders |
| [reliability_spec.md](reliability_spec.md) | Formal run-state, reconciliation, and artifact-contract spec + honest implementation-status matrix |
| [strategy.md](strategy.md) | Strategy overview, ticker universe, latest sizing ladder, budgets |
| [ticker_advisory.md](ticker_advisory.md) | The `ticker-advisor/` tool: scores candidate tickers, proposes universe additions for manual review |
| [live_wiring.md](live_wiring.md) | Live providers/broker adapters, orchestrator/CLI, and the two placement safety gates |
| [inverse_martingale.md](inverse_martingale.md) | Opt-in deferred-accumulation ("inverse Martingale") sizing mode: tracks dip-tier amounts instead of buying into a decline, fires once on recovery |
| [whole_share_accumulation.md](whole_share_accumulation.md) | Whole-share accumulation for tickers not tradable for fractional buys (built for BYDDY): tracks would-be graded-ladder buys, fires one whole-share purchase once they sum to a share's price. Wired into the 3:30 PM canonical stream only |
| [safeguards.md](safeguards.md) | Broker/account, trading constraints, budget caps, test window |
| [schedule_manifest.md](schedule_manifest.md) | The canonical 15-row recurring schedule (17 rows verified 2026-07-27; 2 removed 2026-08-02), one-time tasks, retired entries, and the expired dated scheduler overrides (kept as historical record, authorizing nothing today) |
| [run_classification.md](run_classification.md) | Scheduled-vs-manual run contract (paths, required fields, exclusion rules) |
| [drift_detection.md](drift_detection.md) | Post-artifact signals-vs-submitted-orders drift detector + Slack alerting |
| [alert_digest.md](alert_digest.md) | Why Gate 9 alerts piled up undelivered in Gmail drafts, and the digest that counts them by gate/slot/streak and flags dead delivery channels |
| [intraday_archive.md](intraday_archive.md) | Per-poll price/timestamp/volume archive under `ticker_data/_intraday/`, supplementing the daily OHLCV history |
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
| [pr_review_workflow.md](pr_review_workflow.md) | The two-workflow PR review split, its trust boundary, and the CI-enforced workflow policy checks |
| [scheduled_session_repo_access.md](scheduled_session_repo_access.md) | Dated probe log for whether a Routine-fired scheduled session actually has working repo (clone/fetch/push) and environment (account number) access |
| [morning_dca.md](morning_dca.md) | The morning stream (`MorningDCAExecutor`): 9:45 watchlist/pre-flight and 10:30 live modes, task/ET/artifact-root tables, the Jul 10 2026 ladder unification |
| [midday_dca.md](midday_dca.md) | The midday stream (`MiddayDCAExecutor`): 12:30 live mode, trend validation, artifact contract |
| [market_open_signal_entry.md](market_open_signal_entry.md) | Gate 5 signal generation for the 9:35 AM market-open slot (`scripts/generate_market_open_candidates.py`): payload contract, refresh step, output document |
| [morning_signal_entry.md](morning_signal_entry.md) | Gate 5 signal generation for the 10:30 AM morning slot (`scripts/generate_morning_candidates.py`), including the inherited 9:45 pre-flight gate |
| [midday_signal_entry.md](midday_signal_entry.md) | Gate 5 signal generation for the 12:30 PM midday slot (`scripts/generate_midday_candidates.py`) and its required 7-day deployment figure |
| [intraday_signal_entry.md](intraday_signal_entry.md) | Gate 5 signal generation for the 11:30 AM, 1:30 PM and 2:30 PM intraday slots (`scripts/generate_intraday_candidates.py --slot`) |
| [GATE5_FIX.md](GATE5_FIX.md) | The Gate 5 failure fix: how signal generation became a real, tested entry point per slot |
| [risk_management.md](risk_management.md) | Dynamic risk management: the rolling cash-burn report and the 7-day deployment kill switch (`dca_risk.py`) |
| [dca_operational_todos.md](dca_operational_todos.md) | Operational notes, including the standing Perplexity backup-pass policy behind the seven Claude slots |
| [adr-octopus-ledger-filesystem-boundary.md](adr-octopus-ledger-filesystem-boundary.md) | ADR: the Octopus ledger's descriptor-relative, no-follow filesystem boundary |
| [wondrous_octopus.md](wondrous_octopus.md) | The `wondrous-octopus/` tool: walk-forward, online-updated next-day OHLC forecasting across the whole archive; research artifacts only, never wired into live sizing |

## Machine-readable backup

- [`../config/`](../config) — tickers, sizing ladder, budgets, scheduled tasks, notifications, reporting.
- [`../schemas/`](../schemas) — JSON schemas for config, run-state, slippage, cost-tracker, and model-usage-log artifacts.
- [`../scripts/`](../scripts) — `backup_strategy.py` to re-extract config from a transcript, `run_classification.py` to enforce the scheduled-vs-manual contract, `purchase_history.py` to build the consolidated buy-history ledger, plus tests.
- **Reliability engines** (see [reliability_spec.md](reliability_spec.md)): `broker_reconciliation.py` (fail-closed same-day reconciliation + effective prior), `reason_codes.py` (unified decision codes), `run_state_machine.py`, `run_ledger.py`, `correlation.py`, `scheduler_lease.py`, `idempotency.py`. Implemented and unit-tested; wiring into the live decision path is a tracked follow-up.

## Key facts (quick reference)

- **Active universe:** 65 tickers (includes `GFUZ`, `TXN`, `RKLB`, the
  2026-08-02 additions `GE`, `VRT`, `CAT`, `QQQ`, `QCOM`, `ABBV`, `JPM`, the
  2026-08-04 addition `VT`, the 2026-08-16 additions `QBUT`,
  `RVI`, `VCX`, `TRLV`, the 2026-09-05 additions `LLY`, `TSLA`, and the
  2026-09-11 additions `VIAV`, `CIEN`, `ANET`, `CSCO`, `FFIV`, `COHR` — all
  whitelisted without a broker instrument search, so unverified until their
  first live run; `near_close_dca.ACTIVE_TICKERS` and `config/tickers.json`
  are the count of record and `tests/test_config_invariants.py` pins them);
  `BYDDY` and `RVII` are whole-share tickers, not excluded (fractional-tradability; `RVII` moved
  out of the active list 2026-08-24 — see [whole_share_accumulation.md](whole_share_accumulation.md)); `IRDMI→IRDM`, `GLWR→GLW`; `GFUZ` uses
  Robinhood primary data (finance `NOT_FOUND`).
- **Latest ladder:** 0.1%→8%+, base $2, max **5x** (no upper cutoff). Older
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
