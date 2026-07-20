# Hybrid Notion Reporting — Near-Close Graded DCA

Lightweight hybrid reporting for the near-close graded DCA strategy
(`near_close_dca.py`). Execution stays **Robinhood-only** for live/manual runs;
Alpaca is read-only validation/account context. There is no Slack path. All
Notion output is produced as markdown/payload files and forwarded by the
scheduled task that owns connector runtime access — no secrets, tokens, or page
IDs live in this repo.

## Flow at a glance

1. **Daily — local structured files (compact).** The live run writes machine-readable
   artifacts to a local directory via `run_daily_compact`. No narrative report,
   weekly matrix, backtest, or full Notion dashboard runs on this path.
2. **Daily — compact Notion latest-run sync.** A small markdown block is built
   from the run summary and pushed to the dashboard's "Latest Run" section only.
3. **Weekly (Fridays) — full performance dashboard refresh.** Accumulated run
   artifacts are rolled up into the full set of dashboard sections.
4. **Monthly — automation cost tracker.** Prior-month run artifacts are aggregated
   by component into the Notion "Automation Cost Tracker" section using usage
   proxies (never exact credits).

## Keeping daily tasks compact (token/credit-lean)

The daily live path is deliberately cheap so scheduled runs burn as little
token/credit as possible:

- Use `near_close_dca.run_daily_compact(...)` for the scheduled 3:20 PM run. It
  writes structured JSON/CSV plus a compact summary and **nothing else** — no long
  narrative markdown, no weekly performance matrix, no historical backtest, and no
  full Notion dashboard.
- Push heavier analysis to **Fridays** (`dca_weekly_dashboard`) and the **monthly**
  cost review (`dca_cost_tracker`). `run_and_report` is the heavier manual/Friday
  path that renders the full narrative report; do not call it on the daily path.
- The daily Notion sync updates only the compact **"Latest Run — Compact Sync"**
  section (`dca_notion_sync`), not the full dashboard.
- **Avoid manual full runs unless something fails.** Each run_state records
  `usage_proxy.manual_recovery_required`; only re-run manually when a daily run
  reports errors or manual-review flags that need reconciliation.

### Usage-proxy block in `run_state.json`

Every daily run_state carries a `usage_proxy` object with lightweight activity
counters used by the monthly cost tracker. These are **proxies, not credit spend**:

`tickers_scanned`, `finance_calls_attempted`, `robinhood_calls_attempted`,
`signal_rows_written`, `buy_candidates_written`, `order_reviews_attempted`,
`orders_placed_count`, `state_file_bytes`, `csv_artifact_count`,
`total_artifact_bytes`, `notifications_sent`, `errors_count`,
`manual_recovery_required`, and `cost_control_notes`.

## 1. Daily local structured files

`near_close_dca.run_daily_compact(..., artifacts_dir="runs/2026-06-29")` runs the
scan and writes only structured artifacts:

| File | Contents |
| --- | --- |
| `run_state.json` | Full run state: signal rows, buy candidates with `target_price` at signal time and per-symbol planned dollar amount, `decision_bar_close`, previous close, trend fields, order ids, skip/block reasons, budget/reserve fields, and the `usage_proxy` block. |
| `signals.csv` | One row per (ticker, source) signal. |
| `buy_candidates.csv` | One row per buy candidate with target price and outcome. |
| `orders.csv` | Durable per-order execution/slippage log. |

`run_daily_compact` never renders the narrative markdown report or any weekly/
dashboard output. (`run_once(..., artifacts_dir=...)` writes the same run_state
JSON/CSVs and is used internally by the compact path.)

`target_price` is the decision-bar close captured at signal time (finance
preferred, Robinhood fallback). When neither source carried a usable price the
candidate is flagged `target_price_missing=true` so slippage is never computed
against a fabricated value.

## 2. Daily post-trade reports + compact Notion sync

Feed the run state plus broker fill/order data into `dca_reporting.generate_reports`:

```python
from dca_reporting import load_run_state, generate_reports
from dca_notion_sync import load_latest_run_summary, write_compact_sync

run_state = load_run_state("runs/2026-06-29/run_state.json")
fills = [...]  # broker fills: [{"ticker": "AAPL", "fill_price": 101.0, ...}]
paths = generate_reports(run_state, fills, "runs/2026-06-29")

summary = load_latest_run_summary(paths["latest_run_summary"])
write_compact_sync(summary, "runs/2026-06-29")
```

`generate_reports` emits:

- `slippage_ranked.csv` — per-fill slippage, worst-first. Slippage is only
  calculated when `target_price` exists; missing-target rows carry null slippage
  and `target_price_missing=true`, and sort last.
- `missing_fills_report.csv` — buy candidates that never produced a fill.
- `slippage_and_coverage_report.md` — human-readable summary.
- `latest_run_summary.json` — compact machine-readable summary (coverage,
  execution quality, budget).

`dca_notion_sync.build_compact_sync_markdown(summary)` renders the
**"Latest Run — Compact Sync"** section. `build_notion_sync_payload(summary,
page_id=...)` returns a connector-agnostic payload; the `page_id` is supplied by
the scheduler, never embedded in code. The Notion dashboard page id/URL is
configured only in the scheduled task.

## 3. Weekly full performance dashboard

`dca_weekly_dashboard.build_weekly_dashboard(run_states, summaries, sector_map)`
rolls up accumulated `run_state.json` and `latest_run_summary.json` files into the
full dashboard markdown with these sections:

- Performance snapshot
- Graded DCA vs fixed daily DCA
- Sector performance matrix (`sector_map` maps ticker → sector; unmapped →
  `Unclassified`)
- Budget and reserve effectiveness
- Execution quality
- Coverage and missed opportunities
- Weekly review notes

```python
from dca_weekly_dashboard import load_json_files, write_weekly_dashboard

run_states = load_json_files(["runs/2026-06-29/run_state.json", ...])
summaries = load_json_files(["runs/2026-06-29/latest_run_summary.json", ...])
write_weekly_dashboard(run_states, "runs/week-2026-W27", summaries=summaries,
                       week_label="2026-W27")
```

## 4. Monthly automation cost tracker

`dca_cost_tracker` aggregates the prior month's `run_state.json` files by
component and renders the Notion **"Automation Cost Tracker"** section. Values are
**estimates/proxies derived from local activity counters — never exact credit or
dollar spend.** Per-component "estimated proxy units" are counts × fixed unitless
weights, used only to rank which components dominate automation load.

```python
from dca_cost_tracker import generate_cost_tracker

# Aggregate June 2026 runs found under the runs root.
paths = generate_cost_tracker("runs", 2026, 6, "runs/cost-2026-06")
# -> automation_cost_tracker_2026-06.md  and  _2026-06.json (connector-agnostic payload)
```

Components: `finance_data`, `robinhood_data`, `order_execution`, `signal_scan`,
`notifications`, `artifact_io_kb`, `errors_recovery`. The payload's `page_id` is
supplied by the scheduler, never embedded in code, and `value_type` is
`estimate_proxy` so consumers never treat it as real credit spend.

## Live ladder & budget (reference)

Ladder (drop vs previous close → multiplier): `<0.1% = 0x`, `0.1–<1% = 1x`,
`1–<2% = 1.5x`, `2–<3% = 2x`, `3–<4% = 2.5x`, `4–<5% = 3x`, `5–<6% = 3.5x`,
`6–<7% = 4x`, `7–<8% = 4.5x`, `>=8% = 5x`.

Budget/risk: `$2` base amount with per-run tiered caps (daily/weekly) —
Normal `$180`/`$900`, Elevated `$240`/`$1,200`, High-volatility `$300`/`$1,500`,
Stress `$360`/`$1,800`. Weekly rollover reserve capped at `$900`. The tier is the
highest triggered by either planned spend or the count of 3x-or-greater signals.

## Tests

```bash
python -m pytest test_dca_reporting.py test_near_close_dca.py test_dca_cost_tracker.py
# or, without pytest:
python test_dca_reporting.py && python test_near_close_dca.py && python test_dca_cost_tracker.py
```

## Safety

These modules (`dca_reporting`, `dca_notion_sync`, `dca_weekly_dashboard`,
`dca_cost_tracker`) never place trades, never call broker execution tools, and hold
no credentials. Live placement remains gated inside `near_close_dca.py`
(`dry_run=True` by default, Robinhood-only execution; Alpaca is read-only
validation/account context). The cost tracker reports usage **proxies/estimates
only**, never exact credits.
