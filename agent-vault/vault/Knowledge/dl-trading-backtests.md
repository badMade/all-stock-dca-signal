# dl-trading-pipeline backtests

STATUS: Walk-forward LSTM baseline established on 2026-10-03. The `backtest-dltp` skill reads this note first.

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

- default_20_tickers: 2012-01-03 to 2016-08-22, LSTMForecaster + PPO, seed 42, expected Sharpe > 2.0 (actual mean: 3.56).

## Things to know before trusting a comparison

- Full codebase audit across `dl-trading-pipeline` (including `models.py`, `rl_agent.py`, `train.py`, `backtest.py`, `config.py`) confirmed that no slippage model or output exists in DLTP. Returns are computed frictionlessly (`position * next_ret`). Thus, `live_slippage_vs_model_bps` in `config.yaml` has no model baseline to compare against.
- DLTP's internal health metric table in its `README.md` defines minimum acceptable annualised Sharpe as `> 1.0`. The `1.2` in `config.yaml` is an aspirational filter above the repository baseline.
- `.mcp.json` in that repo configures order-capable MCP servers (Robinhood trading, Alpaca). Backtests do not need them; a vault session denies the Robinhood order tools in `.claude/settings.json`.

## Prior results

### 2026-10-03: Walk-forward LSTM baseline (5 splits, seed 42)
Command: `python backtest.py --model lstm --seed 42`
Look-ahead fixes active (macro not backfilled, fundamentals disabled). 20 tickers, 74,318 rows, 22 features.

| Fold | Train Window | Test Window | Sharpe | Sortino | Max DD | CAGR | Win Rate |
| --- | --- | --- | --- | --- | --- | --- | --- |
| 1 | 2012-01-03 to 2014-12-01 | 2014-12-02 to 2015-04-06 | 2.17 | 2.57 | -6.68% | 19.0% | 100% |
| 2 | 2012-01-03 to 2015-04-06 | 2015-04-07 to 2015-08-10 | 3.58 | 8.17 | -1.96% | 22.3% | 100% |
| 3 | 2012-01-03 to 2015-08-10 | 2015-08-11 to 2015-12-14 | 4.57 | 8.69 | -2.61% | 43.6% | 100% |
| 4 | 2012-01-03 to 2015-12-14 | 2015-12-15 to 2016-04-18 | 3.26 | 3.38 | -4.66% | 35.0% | 100% |
| 5 | 2012-01-03 to 2016-04-18 | 2016-04-19 to 2016-08-22 | 4.19 | 5.93 | -2.37% | 28.6% | 100% |
| **Mean** | - | - | **3.56** | **5.75** | **-3.66%** | **29.7%** | **100%** |

Summary: All 5 folds substantially exceed `backtest_sharpe_min: 1.2` and the `> 1.0` DLTP health floor. Chart saved to `results/wf_lstm_chart.png`.

### 2026-10-03: Paired Model Comparison: LSTM vs TCN (5 splits, seed 42)
Command: `python backtest.py --model both --seed 42`
Look-ahead fixes active, 20 tickers, 74,318 rows, 22 features, seed 42.

| Metric | LSTMForecaster | TCNForecaster | Advantage | p-value (paired t-test) |
| --- | --- | --- | --- | --- |
| Mean Sharpe | 3.2625 | 3.5504 | +0.2880 (TCN) | 0.4199 (not statistically significant) |
| Mean Sortino | 5.75 | 5.49 | +0.26 (LSTM) | - |
| Mean 1d IC | 0.0346 | 0.0121 | +0.0225 (LSTM) | - |
| Max Drawdown | -3.66% | -3.54% | +0.12% (TCN) | - |
| Mean CAGR | 29.7% | 31.1% | +1.4% (TCN) | - |

Findings:
- TCN achieved higher raw risk-adjusted return (mean Sharpe 3.55 vs 3.26), with winner advantage of +0.288 Sharpe units.
- With N=5 folds, the paired difference is not statistically significant (p=0.42).
- LSTM demonstrated superior directional signal quality at 1-day horizon (IC 0.0346 vs 0.0121).
- Artifacts: `results/wf_comparison.json`, `results/wf_lstm_chart.png`, `results/wf_tcn_chart.png`.
