# Agent Working Memory

> The agent's index. It stays SHORT: one line per fact, linking to detail notes. Past ~40 lines the agent must
> consolidate. Read at session start, written once at session close (see CLAUDE.md, "Hard rules").

## Who I'm working for

- The owner of badMade/all-stock-dca. Voice and taste profiles calibrated ([Profiles/voice.md](Profiles/voice.md), [Profiles/taste.md](Profiles/taste.md)).
- Notion is the human-curated reference and is read-only for me. Exports to it are manual (`../runbooks/notion-export.md`).

## Active goals

- Automated recurring capex delta scanner implemented (`agent-vault/scripts/scan_capex_deltas.py`).
- Schedule weekly capex monitoring reports against the two-tier addition rule.

## Standing lessons

<!-- The reflect skill promotes repeated lessons from Reflections/ to here. Max 10. -->

## Knowledge index

<!-- One line per note in Knowledge/, added by the capture skill. -->

- [all-stock-dca rules](Knowledge/all-stock-dca-rules.md) - pointer to the repo's safeguards, ladder and run commands
- [capex monitoring KPIs](Knowledge/capex-monitoring-kpis.md) - 51 watched companies, 8 verified baselines
- [dl-trading backtests](Knowledge/dl-trading-backtests.md) - LSTM vs TCN backtest comparison (TCN +0.288 Sharpe advantage)
- [threshold review](Knowledge/threshold-review.md) - status of all thresholds; all 5 formally settled
