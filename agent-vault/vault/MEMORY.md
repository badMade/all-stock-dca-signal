# Agent Working Memory

> The agent's index. It stays SHORT: one line per fact, linking to detail notes. Past ~40 lines the agent must
> consolidate. Read at session start, written once at session close (see CLAUDE.md, "Hard rules").

## Who I'm working for

- The owner of badMade/all-stock-dca. Voice and taste profiles calibrated ([Profiles/voice.md](Profiles/voice.md), [Profiles/taste.md](Profiles/taste.md)).
- Notion is the human-curated reference and is read-only for me. Exports to it are manual (`../runbooks/notion-export.md`).

## Active goals

- Automate recurring capex delta scans across the 51 watched operating companies.
- Settle capex_research_addition_usd_m with scale-relative tiering in threshold-review.md.
- Evaluate paired TCN vs LSTM walk-forward performance in dl-trading-pipeline.

## Standing lessons

<!-- The reflect skill promotes repeated lessons from Reflections/ to here. Max 10. -->

## Knowledge index

<!-- One line per note in Knowledge/, added by the capture skill. -->

- [all-stock-dca rules](Knowledge/all-stock-dca-rules.md) - pointer to the repo's safeguards, ladder and run commands
- [capex monitoring KPIs](Knowledge/capex-monitoring-kpis.md) - 51 watched companies, 8 verified baselines
- [dl-trading backtests](Knowledge/dl-trading-backtests.md) - walk-forward backtest baseline established (mean Sharpe 3.56)
- [threshold review](Knowledge/threshold-review.md) - status of all thresholds; 4 settled, 1 pending tiering
