# Threshold review

The first target for the `improve` skill. Every threshold the vault uses, where it comes from, and what would
settle it. Values live in `config.yaml` or the repo's own files; this note records status only.

| Threshold | Where it lives | Origin | Status | What would settle it |
| --- | --- | --- | --- | --- |
| all-stock-dca limits (base amount, per-order cap, caps, breakers) | `config/budgets.json`, `docs/safeguards.md` | This repo | Reconciled 2026-10-03 | safeguards.md updated from Max 8 to Max 120 matching budgets.json |
| `capex_delta_flag_pct` | `config.yaml` | Original brief | Candidate (Verified 2026-10-03) | Empirically verified against MSFT/GOOGL/AMZN; flags real inflection quarters (+20% to +54%) while passing steady quarters (+3.3%) |
| `capex_research_addition_usd_m` | `config.yaml` | Original brief | Flawed heuristic (2026-10-03) | Flat $500M is scale-blind: hyperscalers shift spend by $5B-$10B/qtr, tripping on noise; propose tiering or % basis via reflect |
| `backtest_sharpe_min` | `config.yaml` | Original brief | Partially settled (2026-10-03) | DLTP README defines floor as Sharpe > 1.0; 1.2 in config is an aspirational selective threshold |
| `live_slippage_vs_model_bps` | `config.yaml` | Original brief | Blocked (2026-10-03) | Audited dl-trading-pipeline: zero slippage modeling exists (frictionless returns); retire or defer until DLTP adds execution costs |

## Not adopted

The brief's "all-stock-dca: max slip 5bp per position, min 50bp between entry and exit" appears in no repo
document and does not describe a graded ladder with budget caps. It stays out of `config.yaml` unless the owner
shows where it comes from.

## How a change gets made

Propose it through `reflect`. `improve` applies it only after the same signal repeats and a fresh verifier fails to
refute it. Record the decision and its evidence in this table.
