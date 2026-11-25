# Residual reversal vs the graded ladder: cost-basis run (2026-11-24)

The first real-data run of `dca-sizing-signals/cost_basis_backtest/` against
`residual_reversal`. Results and reading:
[`docs/research/2026-11-24-residual-reversal-cost-basis-results.md`](../../docs/research/2026-11-24-residual-reversal-cost-basis-results.md).
Nothing here is read by the strategy. The report JSON is written outside the repo.

| File | What it is |
| --- | --- |
| `run_cost_basis_backtest.py` | Loads the archive, replays the 3:30 PM ladder point in time as the baseline (`dca_ladder_core`), runs the three pre-declared variants through the harness, and writes one report JSON with each variant's summary, its `trials` row (the three rows were ingested into the Phase 0 ledger on 2026-11-24) and the deflated Sharpe / PBO comparison. No network. |
| `sector_map_draft.json` | Ticker to sector ETF, assigned by hand on 2026-11-24. **Draft, not reviewed**: not the committed table `docs/adr-factor-residual-dip-signals.md` rule 4 asks for. |
| `test_run_cost_basis_backtest.py` | Offline checks of the ladder replay and the CSV loader. |

Run from the repo root (it lives here, not in `dca-sizing-signals/`, because it imports
`dca_ladder_core`, which no package there may import):

```bash
PYTHONPATH=.:dca-sizing-signals python analysis/2026-11-24-residual-reversal-cost-basis/run_cost_basis_backtest.py \
    --history-dir ../ticker_data --out /tmp/rr_cost_basis.json
PYTHONPATH=.:dca-sizing-signals python -m pytest analysis/2026-11-24-residual-reversal-cost-basis -q
```

The sector-ETF histories (SPY and 13 sector ETFs) were added to the archive on 2026-11-24 with
the `stock-history-downloader` skill; before that it had none.
