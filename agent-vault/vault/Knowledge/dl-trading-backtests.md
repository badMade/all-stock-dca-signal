# dl-trading-pipeline backtests

STATUS: setup facts read from the `badMade/dl-trading-pipeline` README and code on 2026-10-03. Nothing was run.
The `backtest-dltp` skill reads this note first.

## Model setup (README)

- Deep-learning forecasting (LSTM and TCN backbones) feeding a PPO reinforcement-learning execution agent.
- Walk-forward folds. `backtest.py` records Sharpe per fold and compares LSTM against TCN with a paired t-test.

## Validation rules

- Look-ahead fixes (README banner): macro series are no longer back-filled, and fundamentals are off by default.
  Backtest numbers from before those fixes were optimistic and old checkpoints are incompatible. Do not compare
  across that line.
- Never enable fundamentals to evaluate a backtest; they are a current-day snapshot (live inference only).
- Determinism: `backtest.py` accepts a `seed` threaded into every fold's forecaster training. Always pass one and
  log it in `Daily/`.
- The repo's own gate is `make check` (lint, type, test). A red check ends the iteration.

## Replay scenarios

None defined yet. Add one line per scenario: name, data window, model, seed, expected Sharpe band.

## Things to know before trusting a comparison

- Full codebase audit across `dl-trading-pipeline` (including `models.py`, `rl_agent.py`, `train.py`, `backtest.py`, `config.py`) confirmed that no slippage model or output exists in DLTP. Returns are computed frictionlessly (`position * next_ret`). Thus, `live_slippage_vs_model_bps` in `config.yaml` has no model baseline to compare against.
- DLTP's internal health metric table in its `README.md` defines minimum acceptable annualised Sharpe as `> 1.0`. The `1.2` in `config.yaml` is an aspirational filter above the repository baseline.
- `.mcp.json` in that repo configures order-capable MCP servers (Robinhood trading, Alpaca). Backtests do not need them; a vault session denies the Robinhood order tools in `.claude/settings.json`.

## Prior results

None yet.
