# Monte Carlo variance reduction (`dca-sizing-signals/mc_research/`)

**Status:** research-only harness, implemented 2026-11-23. Not wired into any live path; nothing
here writes sizing, config or orders. Source: the operator's research note "Faster Monte Carlo for
all-stock-dca: what the video's variance-reduction tricks can and cannot do" (2026-11-23), built on
Quant Finance From Scratch Part 54 and the literature it cites.

## What it is for

Variance reduction is about **measurement precision, not trading edge**. It cuts the paths needed
for a given error bar, which makes answers to "does 1x/2x/3x tiering beat flat DCA, by how much,
with what confidence?" cheaper, tighter and reproducible. It does not make a model valid, and it
gives no protection from lookahead or data snooping. The literature the note cites (Vanguard
2012/2023, Maggiulli 2019, Hayley) suggests the ladder's true edge over flat DCA is small and
possibly negative. That is exactly why tier C, or any sizing change, needs a precise measurement
before it is promoted.

The biggest win is **common random numbers (CRN)**: run every variant on identical paths and
estimate the *paired* difference, then use flat DCA as a known-mean **control variate** on that
difference. Antithetic paths are a cheap add-on with modest gains.

## Measured on this harness (fixed seeds, `tests/mc_research/`)

| Study | Plain | Antithetic | Control variate | CRN | CRN + flat-DCA CV |
| --- | --- | --- | --- | --- | --- |
| European call, S0=K=100, vol 20%, r=0, T=1, 10,000 sims (BS 7.9656) | SE 0.128 | SE 0.105 (~1.5x) | SE 0.060 (~4.7x) | -- | -- |
| Graded 1x/2x/3x vs flat DCA, GBM mu 7% vol 25%, 63 days, 400 paths | SE 3.04 (independent paths) | -- | -- | SE 0.60 (~25x) | SE 0.30 (~104x) |

The first row reproduces the video (13c, 10.6c, 6c). The second row is a GBM *pilot*, with
illustrative tiers rather than the live ladder. It shows the size of the CRN gain on a correlated
ladder comparison. It is not evidence about the ladder's edge: under GBM a rule keyed on today's
move can only matter through cash timing.

## Design rules (implemented)

| Rule | Where |
| --- | --- |
| Named, hash-derived streams (`("market", "path", i)`), so path i does not depend on path count and a new random source never shifts existing draws | `rng.RandomStreams`, `rng.derive_seed` |
| Path-first: `gbm_paths(...) -> PathSet` once, then strategies as pure functions of a path. CRN holds by construction | `paths.py`, `strategies.run_strategies` |
| Every metric returns `Estimate(mean, se, ci_low, ci_high, n, n_eff, method)`; antithetic SE comes from pair means; IS reports Kish ESS | `estimators.py` |
| Fail closed: fewer than 2 samples, non-finite values, length mismatch, odd antithetic count, or ESS below the floor all raise | `estimators.py` (`InsufficientEffectiveSampleSize`) |
| Score on terminal wealth for equal contributions, with unspent cash earning `cash_rate`. Average cost and IRR are not scores (Hayley's hindsight bias) | `strategies.flat_dca`, `strategies.graded_dca` |
| Every comparison reports independent, CRN, and CRN+CV SEs side by side, so the gain is measured, not assumed | `strategies.compare` -> `Comparison` |
| Stdlib only; imports no live module, suggester or ingest layer, and no repo-root module imports it | `tests/test_architecture_boundaries.py`, `tests/mc_research/` |

```python
from mc_research import LadderVariant, compare, flat_dca, flat_dca_gbm_mean, gbm_paths, run_strategies

paths = gbm_paths(s0=100, mu=0.07, sigma=0.25, n_steps=63, n_paths=400, dt=1/252, root_seed=7)
graded = LadderVariant("graded", ((0.0, 1.0), (0.01, 2.0), (0.02, 3.0)))
out = run_strategies(paths, {"flat": lambda p: flat_dca(p, daily_budget=10),
                             "graded": graded.runner(daily_budget=10, cash_rate=0.04, dt=1/252)})
report = compare(out, variant="graded", baseline="flat",
                 baseline_mean=flat_dca_gbm_mean(s0=100, mu=0.07, n_steps=63, dt=1/252, daily_budget=10))
report.crn.as_dict(), report.crn_variance_reduction(), report.crn_cv_variance_reduction()
```

## Pitfalls the harness does not solve

- **Precision is not validity.** GBM is the analytic sanity generator only. Real studies need at
  least a Markov regime-switching generator and a stationary block bootstrap (Politis and Romano
  1994), with the spread across models reported as model risk. Neither is built yet.
- **Known control means.** `flat_dca_gbm_mean` is exact only under `gbm_paths`. Under a bootstrap,
  use the CRN paired difference alone, because an estimated control mean cancels the control.
- **Antithetics and non-monotone rules.** Tier triggers can make pairs positively correlated and
  raise variance. Check `antithetic_pair_correlation` before relying on it. Bootstraps have no
  "-z", so Hall (1989) antithetic resampling would be needed.
- **Multiple testing.** Every ladder, threshold and slot subset tried must go in the Phase 0
  trials ledger (`docs/adr-outcome-ledger-pit-harness.md`), with deflated Sharpe, White's Reality
  Check or Hansen's SPA applied to historical evaluation.
- **Simulation vs history.** Millions of paths can make a tiny modelled edge significant. History
  holds only a handful of independent bear markets. Report the two uncertainties separately.
- **Point-in-time calibration.** Any fitted vol, drift or transition matrix must be estimated on
  walk-forward windows, never on the full history.

## Not built yet (ranked by the note's benefit-per-effort)

1. Regime-switching and block-bootstrap generators (needed before any ladder result means anything).
2. Goal-attainment probabilities (P(growth >= 20% YoY)) with stratification on the terminal shock.
3. Importance-sampled drawdown and tail probabilities under a tilted regime chain, using the
   existing ESS guard.
4. CSP shadow-log valuation with a Black-Scholes put control (risk-neutral runs) or a terminal-price
   control (real-world P&L). `options.black_scholes_put` is already in place.
5. Scrambled-Sobol RQMC for smooth, low-dimensional metrics. This would need SciPy, which would be
   a new pinned dependency.

## Open questions (operator decisions before any promotion study)

| # | Question |
| --- | --- |
| 1 | Primary objective: terminal wealth for equal contributions (the harness default), a loss-averse utility, or goal-attainment probability? |
| 2 | Unspent budget: what cash yield, carry-over and sweep rule? This sets the cash-drag term |
| 3 | Which generators are canonical, and how is disagreement between them reported? |
| 4 | What decision bar promotes tier C (minimum effect size, maximum CI width, DSR or SPA p-value)? It must be fixed before trials run |
| 5 | Stream partitioning: per ticker, per day, per slot, per regime? |
| 6 | What ESS floor and SE ceiling reject a simulation result (fail closed)? |
| 7 | Point-in-time windows and re-estimation cadence for simulation parameters |
| 8 | CSP valuation measure (risk-neutral, real-world, or both) and vol source |
| 9 | Is every trial logged in the trials ledger for deflation? |
| 10 | Can simulation outputs ever reach `SuggestionPublisher`, and only as evidence, never as a multiplier? |

## Sourcing

The literature figures above (Vanguard, Maggiulli, Hayley, Boyle-Broadie-Glasserman, Kleijnen,
Glasserman-Heidelberger-Shahabuddin and others) come from the operator's research note and were
**not re-verified** for this doc. The two measured rows are this harness's own output on fixed
seeds and are reproduced by `tests/mc_research/test_mc_research.py`. The CRN gains are a GBM pilot
with illustrative tiers, not a measurement of the live ladder.
