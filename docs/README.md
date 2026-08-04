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
| [reliability_spec.md](reliability_spec.md) | Formal run-state, reconciliation, and artifact-contract spec + honest implementation-status matrix |
| [strategy.md](strategy.md) | Strategy overview, ticker universe, latest sizing ladder, budgets |
| [live_wiring.md](live_wiring.md) | Live providers/broker adapters, orchestrator/CLI, and the two placement safety gates |
| [safeguards.md](safeguards.md) | Broker/account, trading constraints, budget caps, test window |
| [schedule_manifest.md](schedule_manifest.md) | The canonical 15-row recurring schedule (17 rows verified 2026-07-27; 2 removed 2026-08-02), one-time tasks, and retired entries |
| [run_classification.md](run_classification.md) | Scheduled-vs-manual run contract (paths, required fields, exclusion rules) |
| [drift_detection.md](drift_detection.md) | Post-artifact signals-vs-submitted-orders drift detector + Slack alerting |
| [intraday_archive.md](intraday_archive.md) | Per-poll price/timestamp/volume archive under `ticker_data/_intraday/`, supplementing the daily OHLCV history |
| [slack_webhook_injection.md](slack_webhook_injection.md) | How to inject the `DCA_DRIFT_SLACK_WEBHOOK` webhook across cron/systemd, agent/MCP scheduler, local shell, and GitHub Actions |
| [reporting_workflow.md](reporting_workflow.md) | Daily/periodic artifact + Notion flow |
| [notion_dashboard_template.md](notion_dashboard_template.md) | Notion page template + conceptual API sync |
| [cost_tracking.md](cost_tracking.md) | Cost-control policy and usage proxies |
| [credit_usage_proxy_report.md](credit_usage_proxy_report.md) | One-time 2026-07-27 4:30 PM ET credit-usage proxy report contract, including the separate recovery-run section |
| [restore_runbook.md](restore_runbook.md) | Restore procedure + audit checklist |
| [backtest_curve.md](backtest_curve.md) | Analytics-only weekly-rollover intraday cap curve backtest (35 / 65 / 100 candidate) |
| [pr_review_workflow.md](pr_review_workflow.md) | The two-workflow PR review split, its trust boundary, and the CI-enforced workflow policy checks |
| [scheduled_session_repo_access.md](scheduled_session_repo_access.md) | Dated probe log for whether a Routine-fired scheduled session actually has working repo (clone/fetch/push) and environment (account number) access |

## Machine-readable backup

- [`../config/`](../config) — tickers, sizing ladder, budgets, scheduled tasks, notifications, reporting.
- [`../schemas/`](../schemas) — JSON schemas for config, run-state, slippage, cost-tracker, and model-usage-log artifacts.
- [`../scripts/`](../scripts) — `backup_strategy.py` to re-extract config from a transcript, `run_classification.py` to enforce the scheduled-vs-manual contract, plus tests.
- **Reliability engines** (see [reliability_spec.md](reliability_spec.md)): `broker_reconciliation.py` (fail-closed same-day reconciliation + effective prior), `reason_codes.py` (unified decision codes), `run_state_machine.py`, `run_ledger.py`, `correlation.py`, `scheduler_lease.py`, `idempotency.py`. Implemented and unit-tested; wiring into the live decision path is a tracked follow-up.

## Key facts (quick reference)

- **Active universe:** 53 tickers (includes `GFUZ`, `TXN`, `RKLB`, the
  2026-08-02 additions `GE`, `VRT`, `CAT`, `QQQ`, `QCOM`, `ABBV`, `JPM`, and the
  2026-08-04 addition `VT` — whitelisted without a broker instrument search, so
  unverified until its first live run);
  `BYDDY`, `VCX`, `TRLV`, `RVI` excluded; `IRDMI→IRDM`, `GLWR→GLW`; `GFUZ` uses
  Robinhood primary data (finance `NOT_FOUND`).
- **Latest ladder:** 0.1%→8%+, base $2, max **5x** (no upper cutoff). Older
  0.1%–12% ladders are **superseded**.
- **Budgets:** $2 base · tiered caps selected per run by planned spend and 3x+
  signal count — Normal $180/$900 · Elevated $240/$1,200 · High-vol $300/$1,500 ·
  Stress $360/$1,800 (daily/weekly); weekly rollover reserve capped at $900.
- **Live execution:** the prior Perplexity-run three-stream model
  (`2ba19526`/`42173154`/`7be82b97`/`a6239dae`/`10ce362c`) is still the
  account's ONLY currently-functioning live execution. A seven-slot
  Claude-executed roster (weekdays 9:35 AM through 3:30 PM ET) is the
  intended eventual replacement, but every one of those seven prompts
  hard-aborts at Gate 5 (no signal-generation entry point exists yet; see
  `claude/GATE5-PROPOSAL.md`), so it runs alongside the five Perplexity-era
  tasks, not instead of them, until Gate 5 is wired. See
  [safeguards.md](safeguards.md#live-execution-roster) for the current
  roster. Robinhood account `<ROBINHOOD_ACCOUNT_NUMBER>` only; Alpaca read-only.
  The **5:00 PM ET** task is analytics (slippage + Notion sync; retimed
  2026-08-02 from 3:35 PM ET).
- **Run classification:** each currently-functioning stream writes under its OWN
  artifact root and pointer, not a shared `scheduled_runs/` path: market-open
  (`2ba19526`) under `market_open_dca/` + `latest_market_open_run.json`, morning
  (`42173154`) under `morning_dca/` + `latest_morning_run.json`, midday
  (`a6239dae`) under `midday_dca/` + `latest_midday_run.json`, intraday
  (`7be82b97`) under `intraday_dca/` + `latest_intraday_run.json`; only the final
  afternoon stream (`10ce362c`) uses `scheduled_runs/YYYYMMDD_1530ET/` and
  `latest_scheduled_run.json` (under the legacy `ce16ba6b` root). The proposed
  seven-slot Claude roster mirrors this for its own six non-final slots
  (`scheduled_runs/YYYYMMDD_<slot HHMM>ET/`, under a plain, un-prefixed root --
  audit-only today, no reader wired). Those six slots share that one
  `scheduled_runs/` namespace, so each updates its OWN slot-specific pointer
  filename, `latest_scheduled_run_<slot HHMM>ET.json` (e.g.
  `latest_scheduled_run_0935ET.json`), never the bare, unqualified
  `latest_scheduled_run.json` a later slot would otherwise silently overwrite
  (Codex review: "Give each slot an unambiguous latest-run pointer"; "Name
  slot-specific pointers in the overview"). Its 3:30 PM slot writes to its own
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
