# Threshold review

The first target for the `improve` skill. Every threshold the vault uses, where it comes from, and what would
settle it. Values live in `config.yaml` or the repo's own files; this note records status only.

| Threshold | Where it lives | Origin | Status | What would settle it |
| --- | --- | --- | --- | --- |
| all-stock-dca limits (base amount, per-order cap, caps, breakers) | `config/budgets.json`, `docs/safeguards.md` | This repo | Checked by reading, 2026-10-03 | Nothing to do; the files are authoritative |
| `capex_delta_flag_pct` | `config.yaml` | Original brief | UNVERIFIED | Run `analyze-capex` on real filings and see how often it fires |
| `capex_research_addition_usd_m` | `config.yaml` | Original brief | UNVERIFIED | Same run; check it against the watched companies' scale |
| `backtest_sharpe_min` | `config.yaml` | Original brief | UNVERIFIED | A seeded post-fix backtest to see what the pipeline actually produces |
| `live_slippage_vs_model_bps` | `config.yaml` | Original brief | UNVERIFIED | Find or add a model slippage output first; today there is none to compare |

## Not adopted

The brief's "all-stock-dca: max slip 5bp per position, min 50bp between entry and exit" appears in no repo
document and does not describe a graded ladder with budget caps. It stays out of `config.yaml` unless the owner
shows where it comes from.

## How a change gets made

Propose it through `reflect`. `improve` applies it only after the same signal repeats and a fresh verifier fails to
refute it. Record the decision and its evidence in this table.
