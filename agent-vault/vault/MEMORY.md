# Agent Working Memory

> The agent's index. It stays SHORT: one line per fact, linking to detail notes. Past ~40 lines the agent must
> consolidate. Read at session start, written once at session close (see CLAUDE.md, "Hard rules").

## Who I'm working for

- The owner of badMade/all-stock-dca. The profile interview has not run yet; `Profiles/` holds a seed from the
  setup brief only.
- Notion is the human-curated reference and is read-only for me. Exports to it are manual (`../runbooks/notion-export.md`).

## Active goals

- Validate the loop on all-stock-dca (dry-run) before relying on the capex and backtest skills.
- Re-evaluate every task threshold; `Knowledge/threshold-review.md` is the first `improve` target.

## Standing lessons

<!-- The reflect skill promotes repeated lessons from Reflections/ to here. Max 10. -->

## Knowledge index

<!-- One line per note in Knowledge/, added by the capture skill. -->

- [all-stock-dca rules](Knowledge/all-stock-dca-rules.md) - pointer to the repo's safeguards, ladder and run commands
- [capex monitoring KPIs](Knowledge/capex-monitoring-kpis.md) - watched companies, baselines (skeleton, untested)
- [dl-trading backtests](Knowledge/dl-trading-backtests.md) - model setup, validation rules, seed policy (untested)
- [threshold review](Knowledge/threshold-review.md) - status of every threshold and what would settle it
