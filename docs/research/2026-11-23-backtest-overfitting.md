# Backtest overfitting and all-stock-dca: what to change, in priority order

**Research input, not an ADR.** The operator pasted this report into the session on 2026-11-23. It feeds
[`../adr-backtest-protocol.md`](../adr-backtest-protocol.md) (Amendment 1).

**Sourcing disclosure (added on commit, 2026-11-23):**

- The report says it was written without reading the repo ("I did not inspect the badMade/all-stock-dca
  code"). Where it names repo components, [`../backtest-gap-audit.md`](../backtest-gap-audit.md) is the
  authority on what exists.
- Its quotations, statistics and library versions (arch 9.0.0, skfolio 1.14.4 and vectorbt 1.2.1, all
  dated November 2026) were **not re-verified** in this session, and some postdate the assistant's
  training data. The arch 8.0 studentization bug is flagged in the report itself as third-party and
  unverified.
- The arithmetic in sections 1 and 7 was rechecked in session and holds: E[max SR] is about 3.25
  standard errors for N = 1,000, and the power table rows give 618, 1,168, 56 and 155 days.
- Some recommendations change live sizing (section 8, the 2x tier "consider reverting live sizing to
  1x"). They are out of scope for any research change and need their own operator decision, review and
  acceptance run (CLAUDE.md).

The report body follows as pasted, unedited.

---

The video's warning applies directly to all-stock-dca: the dip multipliers (2x, proposed tier C 3x), the seven-slot schedule and the Phase 3/4 selection engines are all searches over many rules, so none of them should affect live sizing until it beats a same-dollar naive DCA baseline after deflating for the number of variants you tried. Phase 0 (trial registry #12, harness #18) is the right place to make that enforceable, and it is the highest-leverage work in the roadmap. Prior evidence says the tiers are more likely noise than edge: AQR tested 196 buy-the-dip variants over 1965 to 2025 and found that "only 16 (i.e., only 8%!) cross the conventional threshold of statistical significance", and none survived a correlation-adjusted hurdle.

## TL;DR

- **The video is right, and if anything understates the problem.** With 1,000 independent zero-skill rules, the expected best Sharpe ratio is about 3.26 standard errors. On 5 years of daily data that is an annualized Sharpe near 1.46, so "best rule above 1, then nothing for 5 years" is the expected outcome, not a fluke. Holdout testing helps only if the holdout is used once and the number of trials is counted.
- **For your system, the biggest risk is selection across many sizing, timing and universe variants**, measured with a metric that flatters dynamic strategies. Average cost per share and IRR are biased in favor of buy-more-when-down rules (Hayley). Evaluate with (a) same-dollar intraday execution improvement in bps and (b) a cash-drag-aware "tilt return" versus naive DCA. Deflate both with DSR, or with SPA and StepM against the naive baseline, using N from the trial registry.
- **Do this first:** make the trial registry mandatory and fail-closed, burn and re-freeze a holdout, rebuild tiers 2x and C as shadow-only hypotheses scored per regime, gate tier C on Octopus coverage tests, and size the shadow period with a pre-registered power calculation. At realistic noise levels that is likely 2 to 5 years per hypothesis for single-digit-bps effects.

## Key Findings

### 1. Do the video's claims hold up?

| Video claim | Verdict | Evidence |
|---|---|---|
| Try 1,000 random rules on a patternless market and the best shows Sharpe > 1, then earns nothing | **Supported, and conservative** | False Strategy Theorem: E[max SR] ≈ sqrt(V)·[(1−γ)Φ⁻¹(1−1/N) + γΦ⁻¹(1−1/(Ne))]. For N=1,000 the bracket is ≈3.26. One standard error of annualized SR is ≈1/sqrt(years), so ≈1.46 over 5 years and ≈1.03 over 10 years (Bailey & López de Prado 2014, JPM 40(5)). Bailey et al.'s online tool "usually has no problem finding a 'profitable' strategy with any desired Sharpe ratio," which then "typically flounders" on a fresh series. |
| Overfitting = fitting noise of one history | Supported | "Random samples contain patterns... a systematic search... will eventually lead to identifying one that profits from the chance configuration" (DSR paper). With memory effects (mean reversion), the overfit rule can be expected to lose out of sample, not just earn zero. |
| Regime change breaks rules | Supported | AQR: buy-the-dip "performs well when market declines are brief, but poorly when declines mark the beginning of a prolonged drawdown" (2020 vs 2022). |
| Fix: in-sample/out-of-sample, test once | **Necessary but not sufficient** | "If we apply the holdout method enough times (say 20 times for a 95% confidence level), false positives are no longer unlikely: They are expected" (Bailey & López de Prado). The holdout must be paired with trial counting. |
| Count trials; more trials need a higher bar | Supported | Harvey, Liu & Zhu (RFS 2016): a new factor "needs to clear a much higher hurdle, with a t-statistic greater than 3.0". Chen & Zimmermann ("Publication Bias in Asset Pricing Research") argue 3.0 is conservative: in their reading t = 3.0 matches a 1% FDR, and "FDR = 5% requires raising the hurdle a bit, to 2.3." |
| Prefer simple rules with an economic reason | Supported, with caveat | Economic priors reduce the effective search space. They do not replace deflation. |
| "A great backtest alone proves very little" | Supported | "A backtest where the researcher has not controlled for the extent of the search involved... is worthless" (DSR paper). |

### 2. What the evidence says about your specific enhancements

| Enhancement | Prior evidence | Implication |
|---|---|---|
| Buying more on dips (2x tier) | AQR "Hold the Dip" (2025): average Sharpe was −0.04 versus passive over 1965 to 2025, with over 60% of 196 variants underperforming. Over 1989 to 2025 the gap was −0.27. Footnote 13: investors should not expect BTD to beat DCA either. A Utah State University graduate report ran Hansen's SPA test and concluded that "buying the dip as a passive long-term strategy is statistically either worse or equal to dollar cost averaging and is definitely worse historically." | Expect roughly zero edge, minus cash drag on the reserve that funds multipliers. Treat 2x as a hypothesis, not a baseline. |
| Value averaging / "buy more when down" | Hayley (City, Univ. of London): VA's higher IRR is "entirely due to a hindsight bias"; "VA does not boost profits – in fact it suffers substantial dynamic inefficiency." | **Do not use IRR or average cost per share as the success metric** for tiers. |
| Intraday slot timing / slot-curve rebuild | Day-of-week and time-of-day effects for DCA are small and unstable. PatternsWizard's test of buying $100 of the S&P 500 every week since February 1993 found "the gap between the best and the worst day is 0.2% of the final value over 33 years and 0.3% over 10 years." Heston, Korajczyk & Sadka (Journal of Finance 2010) report an intraday return-persistence decile spread of "1.66 basis points per half-hour interval," and note that "timing trades can reduce execution costs by the equivalent of the effective spread." | Slot optimization is mostly noise unless measured with large samples. Expect effects of a few bps at most, comparable to spread. |
| Tier C (ML forecast below close and price below forecast) | No direct evidence. Next-day equity price forecasts rarely beat a random walk once multiple comparisons are counted (the motivation for White's Reality Check and Hansen's SPA). | Gate on calibration and on beating a random-walk forecast before any shadow scoring. |

## Details

### 3. Implementable formulas

**3.1 Deflated Sharpe Ratio (Bailey & López de Prado 2014).** Use non-annualized SR at the native frequency.

```python
import numpy as np
from scipy.stats import norm, skew, kurtosis
EMC = 0.5772156649015329

def expected_max_sr(n_trials, var_sr):
    z = (1-EMC)*norm.ppf(1-1/n_trials) + EMC*norm.ppf(1-1/(n_trials*np.e))
    return np.sqrt(var_sr) * z            # SR0, the null hurdle

def psr(sr, sr_star, T, g3, g4):          # g4 = raw kurtosis (3 if normal)
    den = np.sqrt(1 - g3*sr + (g4-1)/4*sr**2)
    return norm.cdf((sr - sr_star)*np.sqrt(T-1)/den)

def dsr(returns, trial_srs):              # trial_srs: SR of every registered trial in the family
    r = np.asarray(returns); T = len(r)
    sr = r.mean()/r.std(ddof=1)
    sr0 = expected_max_sr(len(trial_srs), np.var(trial_srs, ddof=1))
    return psr(sr, sr0, T, skew(r), kurtosis(r, fisher=False))
```

- Decision rule: promote only if DSR ≥ 0.95.
- N should be the **effective** number of independent trials. Cluster correlated variants (López de Prado's ONC clustering), or use the approximation N_eff ≈ ρ̄ + (1−ρ̄)·N, where ρ̄ is the average pairwise correlation of trial return series. AQR used ρ̄ = 0.41 across 196 BTD variants to raise the significance hurdle to about t = 3.52.
- If the registry is too small to estimate var_sr, fall back to Lo's (2002) analytic variance (1 + SR²/2)/(T−1).

**3.2 PBO via CSCV (Bailey, Borwein, López de Prado & Zhu, J. Computational Finance 20(4), 2017).**

```
Input: M = T x N matrix of per-period performance (one column per trial)
Split rows into S contiguous blocks (S even, e.g. 16 -> C(16,8) = 12,870 splits)
for each combination c of S/2 blocks as IS (rest = OOS):
    R_IS  = metric(M[IS rows])  for each trial      # e.g. mean tilt return or SR
    n*    = argmax(R_IS)
    w     = rank of R_OOS[n*] among R_OOS / (N+1)    # relative OOS rank in (0,1)
    lambda_c = log(w / (1-w))
PBO = mean(lambda_c <= 0)       # P(IS winner is below OOS median)
Also report: slope of R_OOS[n*] on R_IS[n*] (degradation), P(R_OOS[n*] < 0)
```

Rule: PBO > 0.2 means the selection process is unreliable. Simulations show a pure-noise PBO of about 0.47 to 0.48, so the threshold must sit well below 0.5.

**3.3 Minimum Backtest Length (Bailey et al., Notices of the AMS 61(5), 2014).**

MinBTL (years) ≈ [((1−γ)Φ⁻¹(1−1/N) + γΦ⁻¹(1−1/(Ne))) / E[max_N]]², bounded above by 2·ln(N)/E[max_N]².

- Bailey's own example: with 5 years of data, "no more than 45 independent model configurations should be tried" if you want to avoid a skill-less best SR of 1.
- For you: DoltHub options data starts in 2019, roughly 7 years, so any options-driven variant search (Phase 2 inverse-vol, IV inputs) supports only a few dozen independent configurations before the noise winner reaches SR ≈ 1.

**3.4 Bootstrap multiple-comparison tests against the naive baseline.** This is the cleanest fit for a DCA system, because the null is "no variant beats naive DCA".

```python
from arch.bootstrap import SPA, StepM, MCS, optimal_block_length
# losses: lower is better -> use negative per-day improvement in bps
bm  = -improvement_naive          # zeros if naive is the reference
mod = -improvement_variants       # T x K DataFrame, one column per registered variant
b = optimal_block_length(mod.mean(axis=1))["stationary"].iloc[0]
spa = SPA(bm, mod, block_size=int(np.ceil(b)), reps=5000, bootstrap="stationary")
spa.compute(); print(spa.pvalues)              # 'consistent' p-value (Hansen 2005)
stepm = StepM(bm, mod, size=0.05); stepm.compute(); print(stepm.superior_models)  # Romano-Wolf
```

- White's Reality Check (Econometrica 2000) is conservative because it uses the least favorable null. Hansen's SPA (JBES 2005) studentizes and "is more powerful and less sensitive to poor and irrelevant alternatives." Report the SPA consistent p-value.
- Use StepM to name which variants survive with familywise error control. Use Benjamini-Hochberg FDR only for screening very large families such as pairs and tickers, where some false positives are tolerable in shadow mode.
- Use arch 9.0.0 or later (released Nov 11, 2026). A third-party issue reports that in 8.0.0 "`studentize=True` had no effect in SPA and StepM"; this is unverified against the official changelog.

**3.5 Harvey & Liu haircut (JPM 42(1), 2015).** Convert SR to a t-stat (t ≈ SR·sqrt(T)), adjust the p-value with Bonferroni, Holm or BHY for N trials, and convert back. Harvey warns that the flat 50% industry haircut is "always a mistake" because the true haircut is non-linear. Duke hosts reference code (Haircut_SR.m). Use it as a cross-check on DSR.

### 4. A metric fit for DCA execution, and how to deflate it

A DCA system does not have a long/short Sharpe ratio. Define two separate metrics, because they answer different questions and have very different noise levels.

| Metric | Definition | Answers | Baseline |
|---|---|---|---|
| **M1: Same-dollar intraday execution improvement (bps)** | For ticker i, day t: IS_it = (P_ref,it − P_fill,it)/P_ref,it × 10⁴, dollar-weighted over the same dollars | Slot timing, top-up rule, 10:30 trend validation | P_ref = price a naive rule pays for the same dollars that day, e.g. all at 09:35, or the TWAP of the seven slots |
| **M2: Tilt return versus naive DCA (cash-drag aware)** | Daily P&L of the difference portfolio: Σ_i (H_rule,it − H_naive,it)·r_i,t+1 + (C_rule,t − C_naive,t)·r_f,t, where H is shares held and C is uninvested cash (the reserve funding multipliers earns T-bills) | Tiers 2x and C, universe and bandit weighting | Naive DCA with the **same cumulative contributions on the same dates**, invested evenly |

Why M2 and not average cost per share: a rule that spends more when prices are low mechanically lowers its average cost, and its IRR is biased upward (Hayley). M2 is a self-financing long/short "timing portfolio", so it has a valid t-stat and Sharpe, and the same DSR, PBO and SPA machinery applies.

Deflation recipe:
1. Each registered variant k produces daily series M1_k or M2_k over identical dates.
2. Run SPA and StepM with naive (M = 0) as the benchmark, with the stationary bootstrap and the Politis-White block length.
3. Compute DSR for the chosen variant, using the SR of every variant in its family from the registry.
4. Compute PBO with CSCV on the T × K matrix.
5. Promote to shadow only if SPA p < 0.05, DSR ≥ 0.95 and PBO < 0.2. Promote to live only after the pre-registered shadow test passes (section 7).

### 5. Validation design for your harness (#18)

| Design | Use for | Settings |
|---|---|---|
| Walk-forward (expanding) | Final pre-shadow evaluation of rules with no fitted parameters (2x tier, slot rules) | Monthly re-anchor. Report every fold, not the average alone. |
| Purged and embargoed k-fold (López de Prado, AFML ch. 7) | Octopus, ml-forecaster, meta-labeling | Purge any training row whose label window (next-day for Octopus, h-day for M2 labels) overlaps the test fold. Embargo about 1% of observations, or at least h days, after each test fold. |
| CPCV (AFML ch. 12) | Model selection where you want a distribution of OOS paths, and as input to PBO | skfolio `CombinatorialPurgedCV(n_folds, n_test_folds, purged_size, embargo_size)` with `optimal_folds_number` |
| Single sealed holdout | Final confirmation only | Last 12 months of history plus all future shadow data. Log every access, and after one access treat it as burned and roll forward. |

**Holdout reuse rule:** add a `holdout_access` table. The harness refuses to evaluate on holdout dates unless a registered, frozen hypothesis ID is supplied, and it increments a counter that feeds N. This matches the fail-closed principle already in the architecture.

### 6. Regime robustness

1. **Label regimes ex ante with a rule, not by eye.** Example: trailing 60-day S&P 500 return and realized vol terciles, plus named event windows (Feb to Mar 2020 crash, 2022 bear, Apr 2025 "Liberation Day" selloff, 2023 to 2024 bull). Store labels as point-in-time.
2. **Report M1 and M2 per regime, with per-regime t-stats.** Require the sign to be non-negative in every regime and positive in a majority. A tier that earns everything in one V-shaped rebound (2020) and loses in 2022 is the textbook BTD failure AQR describes.
3. **Stationary bootstrap of price paths** (Politis & Romano 1994; block length from Politis & White 2004 with the Patton-Politis-White 2009 correction). Resample joint daily returns across the universe in blocks to keep cross-correlation, simulate the full sizing ladder plus the naive baseline on each path, and report the distribution of M2. Block bootstrap preserves short-range dependence but not long regime shifts, so pair it with:
4. **Stress paths:** replay 2000 to 2002 and 2008 with yfinance (research only), plus synthetic "slow grind down" paths (for example −30% over 12 months at low daily vol). Slow declines are where dip multipliers do the most harm.
5. **Gap risk for slot rules:** test days with large overnight gaps separately, because the 09:35 reference behaves differently.

### 7. Shadow-mode power analysis and pre-registration

Treat each shadow hypothesis as a pre-registered experiment. Aggregate to one observation per trading day (the dollar-weighted M1 or M2 across tickers), because same-day observations across tickers are correlated.

**Required days** (one-sided test of mean improvement δ against 0):

n = (z_{1−α/K} + z_{1−β})² · σ_d² / δ² · DE, with design effect DE ≈ 1 + 2·Σρ_k (autocorrelation of the daily series)

| Daily σ_d (assumed) | Effect δ | K hypotheses (Bonferroni) | Power | Days needed (DE=1) |
|---|---|---|---|---|
| 30 bps | 3 bps | 1 | 0.80 | ≈ 618 (≈ 2.5 yrs) |
| 30 bps | 3 bps | 10 | 0.80 | ≈ 1,168 (≈ 4.6 yrs) |
| 30 bps | 10 bps | 1 | 0.80 | ≈ 56 |
| 15 bps | 3 bps | 1 | 0.80 | ≈ 155 |

These σ values are illustrative assumptions. Measure the real σ_d from the Phase 0 ledger before committing. An equivalent check is the Minimum Track Record Length (Bailey & López de Prado 2012): MinTRL = 1 + [1 − γ₃·SR + (γ₄−1)/4·SR²]·(Z_α/(SR − SR*))², implemented in pypbo.

**Pre-registration record** (write before shadow starts, then make it immutable): hypothesis ID, economic rationale, exact rule and parameters, metric (M1 or M2), baseline, primary test and α, K for the family, minimum days, stopping rule, regimes that must be non-negative, and the decision for each outcome. If you want to peek, either fix interim looks in advance with alpha spending or use an always-valid sequential test; do not look ad hoc.

### 8. Component-by-component changes, prioritized

| Priority | Component | Change | Why |
|---|---|---|---|
| **P0** | Trial registry (#12) | Fail-closed: the harness cannot run without writing a row. Schema: `trial_id, family_id, created_at, author, hypothesis_id, rationale, component, params_json, code_commit, data_vintage_hash, universe_snapshot_id, start, end, metric_name, daily_series_path, sr, t_stat, touched_holdout(bool), status`. Count failed and abandoned runs, notebooks and parameter sweeps. | DSR, PBO and haircuts are impossible without N, and the unreported trials are the ones that inflate results. |
| **P0** | Harness (#18) | Implement M1 and M2, same-contribution naive baseline, T-bill cash credit, point-in-time joins on filed_date and quote_date, regime labels, and SPA, StepM, DSR and PBO reports as standard outputs. | Turns "a great backtest" into a deflated, regime-split result. |
| **P0** | Friday review | Add a fixed panel: N per family, best DSR, PBO, holdout access count, and shadow progress against required days. | Makes overfitting visible weekly. |
| **P1** | 2x tier (price ≤ prior low) | Re-label as hypothesis H-2X. Compare against 1x-only and naive on M2 per regime. Consider reverting live sizing to 1x until it passes, consistent with auto-revert. | The AQR and Utah State priors say roughly zero or negative. |
| **P1** | Tier C 3x (Octopus) | Keep it shadow-only. Gate: (a) Octopus quantiles pass Kupiec unconditional coverage and Christoffersen conditional coverage on purged CV and holdout; (b) Octopus median beats a random-walk forecast under SPA or Diebold-Mariano; (c) only then score H-C on M2. | Tier C stacks an unvalidated forecast on an unvalidated sizing rule, so errors compound. |
| **P1** | 10:30 trend validation / slot schedule | Treat the seven slots, the top-up rule and the trend filter as one family. Measure M1 versus all-at-09:35 and versus seven-slot TWAP. Phase 4 "slot-time curve rebuild" must register every curve shape tried. | Expected effects are a few bps; with many curve shapes, the noise winner dominates. |
| **P2** | Octopus calibration (Phase 4) | Make the PIT histogram, empirical coverage at 10/50/90%, pinball loss, and Kupiec and Christoffersen p-values standard Octopus outputs. Recalibrate (for example with conformal intervals) only within purged CV. | Calibration is testable now and does not need live trading. |
| **P2** | Phase 2 inverse-vol / IV | Count variants against the MinBTL implied by data since 2019 (about 7 years). Evaluate as volatility-forecast quality (QLIKE loss versus realized), consistent with "options help vol, not direction". | Limited history allows few independent configurations. |
| **P3** | Universe selection engine | Point-in-time eligibility only: membership, listing status and fundamentals as of filed_date. Include delisted names with terminal returns. Snapshot each universe and store its ID in the registry. Measure survivorship by running current-members versus point-in-time and logging the gap. | Current-constituent backtests overstate returns. In a Price Action Lab test of a Dow 30 trend-following strategy (Norgate data, 2000 to 2026), current constituents gave a CAGR of 11.7% and a Sharpe of 0.91; including delisted names cut that to 6.1% and 0.57, "overstating CAGR by about 5.6%." |
| **P3** | Bandit ticker ranking | Use discounted or sliding-window UCB (Garivier & Moulines 2011) for non-stationarity. Log every pull and reward as a trial. Run in shadow against an equal-weight baseline on M2. Cap the cumulative tilt. | Bandits on low signal-to-noise returns chase noise, and each arm comparison is an implicit multiple test. |
| **P3** | Meta-labeling | Label windows overlap, so purge and embargo. Score with precision/recall on purged CV, then M2. The secondary model's threshold is a trial. | Leakage inflates CV scores. |
| **P3** | Co-movement (pairs) shadow signals | 100 tickers give 4,950 pairs. Screen with BH FDR at 10%, require out-of-sample stability, and record the full pair count as N. | The largest implicit search in the roadmap. |

### 9. Python tooling status (checked late Nov 2026)

| Library | Status | Use |
|---|---|---|
| **arch** (K. Sheppard) | 9.0.0, Nov 11, 2026; NCSA license; production/stable | SPA (Reality Check), StepM, MCS, stationary, circular and moving-block bootstraps |
| **skfolio** | 1.14.4, Nov 20, 2026; BSD-3; active | `CombinatorialPurgedCV`, `WalkForward`, `optimal_folds_number` |
| **pypbo** (esvhd) | GitHub only, MIT, appears dormant (README TODOs, old statsmodels pin) | PBO, PSR, MinTRL, MinBTL, DSR. Vendor and unit-test it, or reimplement (about 100 lines). |
| **R `pbo`** | v1.3.5 | Cross-check for your PBO implementation |
| **mlfinlab** (Hudson & Thames) | Closed source: "licensed under an all rights reserved licence and is NOT open-source" | Avoid. Reimplement the AFML snippets instead. |
| **timeseriescv** | 0.2, Sep 2018; MIT; alpha, inactive | Reference only (`CombPurgedKFoldCV`) |
| **vectorbt** | 1.2.1, Nov 11, 2026; Apache 2.0 with Commons Clause (source-available); PRO is separate and paid | Fast parameter sweeps. Every sweep cell is a trial, so log it. |

Given the ~700-test dca-sizing-signals package, write property tests for DSR and PBO: pure-noise inputs should give PBO ≈ 0.5 and DSR ≈ 0.5, and a planted SR = 3 signal should give PBO ≈ 0. Published reimplementations report pure-noise PBO of 0.47 to 0.48 and PBO ≈ 0.00 to 0.01 with a planted true SR of 3.

## Recommendations (ordered)

1. **This week:** ship the registry schema and fail-closed hook. Backfill every variant you remember testing for tiers, slots and Octopus, marking the backfills as approximate, and freeze a 12-month holdout.
2. **Next 2 to 4 weeks:** implement M1 and M2 with the same-contribution naive baseline and T-bill cash credit. Add SPA, StepM, DSR, PBO and regime reports to the harness.
3. **Then:** re-evaluate the live 2x tier. If it fails SPA or DSR, or is negative in the 2022-type regime, revert live sizing to 1x and move 2x to shadow.
4. **Tier C:** do not launch until Octopus passes the coverage tests and beats a random walk. Then pre-register H-C with a power-based minimum duration.
5. **Phase 3/4:** build point-in-time universe snapshots before any universe, bandit or pairs research. Each of those engines must emit registry rows automatically.

## Caveats

- These recommendations rest on the project description you provided. I did not inspect the badMade/all-stock-dca code, so field names and hook points may need adapting.
- Shadow-period lengths depend on σ_d, which is assumed here. Measure it first. Effects of a few bps may simply be unmeasurable at personal-account scale, and that is a legitimate conclusion.
- The AQR and Utah State studies test dip-buying **with cash waiting** at index level. Your system buys every day and adds on dips, so its cash drag is smaller. The prior of "no timing alpha" still applies to the tilt itself.
- The expected-max-Sharpe figures assume independent trials. Correlated variants lower the effective N, so cluster before applying them.
- The HLZ t > 3 hurdle is contested: Chen (arXiv 2206.15365) estimates "a hurdle of 2.27" for a 5% FDR and argues HLZ's "own methods imply an FDR of just 9%" at the classical cutoff. Under suggest-only and fail-closed rules, the stricter bar is the reasonable choice.
- The arch 8.0 SPA studentization bug comes from a third-party issue, not the official changelog. Pin arch ≥ 9.0 either way.
