# Kalman Filters for the all-stock-dca Program: What Helps, What Is Just an EMA, and What to Shadow-Test First

Status: research input only. Not an ADR, not a decision, not wired into anything.
Date: 2026-11-23
Origin: research report pasted by the operator into a Claude Code session (branch `badMade/kalman-filters-stock-dca-02d0fc`); recorded here as given, apart from this header.
Feeds: the proposed ADR [`docs/adr-kalman-residual-dip-shadow.md`](../adr-kalman-residual-dip-shadow.md), whose "What the research assumed, checked against the code" table corrects several assumptions below.

**Sourcing disclosure (added on recording).** The citations below were not re-fetched or re-verified when this file was written. The academic results (Mergner and Bulla 2008, Choudhry and Wu 2008, Blitz et al. 2013, Da, Liu and Schaumburg 2014, Hansen and Lunde 2006, Jegadeesh, de Groot et al., Miwa 2019, Hayley) are cited from the report as written; the pairs-trading cost figures come from a textbook (Palomar) and an SSRN preprint (Baskaran); F7 is a practitioner blog analysis (Of Dollars and Data). The EMA-equivalence numbers in section 2 follow from the stated Riccati formula and can be checked by hand. Treat every effect size as unverified until reproduced on this universe.

A Kalman filter can help your DCA program, but mainly as a measurement and normalization layer, not as a source of "fair value" alpha: the highest-value use is a Kalman dynamic-beta model that tells each tier decision whether a dip is idiosyncratic (stock-specific, historically more likely to reverse) or market-wide (a cluster/selloff event), while a single-series "Kalman fair price" on a random-walk stock is mathematically an exponential moving average and should not be expected to beat one.

## TL;DR

- **Best first use (high confidence on mechanics, moderate on payoff):** run a shadow-mode Kalman time-varying beta of each stock against SPY and its sector ETF (the video's `hedge_step`, applied to returns), and log the standardized residual innovation as an "idiosyncratic dip" score next to every tier A/B/C decision. This maps directly onto your cluster caps, co-movement shadow signals and Tier C gating, and it rests on the strongest evidence: Kalman betas forecast better than rolling OLS and most GARCH variants, and residual (idiosyncratic) reversal earns risk-adjusted returns twice as large as conventional reversal, with profits that stay significant net of trading costs even in post-1990 large caps (Blitz, Huij, Lansdorp and Verbeek, Journal of Financial Markets 2013).
- **Do not replace last_close/previous_low with a univariate "Kalman fair value" expecting an edge.** Steady-state local-level Kalman equals an EMA with alpha = K (for example Q/R = 0.01 gives K ≈ 0.095, about a 20-period EMA). The Kalman version earns its complexity only through time-varying noise (earnings days, open auction), missing-data handling, the uncertainty band P, and multivariate states (level+slope, alpha+beta).
- **Evaluate on budget-matched terminal wealth and forward residual returns, not average cost basis.** Buying more after price falls mechanically lowers average cost and raises IRR even when expected profit is unchanged, so "average cost vs simple DCA" will flatter every dip-weighted variant. Plan for roughly 1,300+ tier events per comparison group (clustered by date) and 6 to 12 months of shadow logging before any live effect.

## Key Findings

### Facts (sourced)

| # | Finding | Source type | Confidence |
|---|---|---|---|
| F1 | For a price following a random walk plus noise, the Kalman filter is equivalent to the optimal EMA with parameter equal to the Kalman gain (Bruder, Dao, Richard, Roncalli 2011, as summarized in "Trend without hiccups", arXiv 1808.03297). | Academic / practitioner | High |
| F2 | A scalar steady-state Kalman filter with gain K is algebraically an exponential kernel smoother with bandwidth tau = -dt / ln(1 - K); out of steady state (diffuse start, small q) the weights differ, and convergence is slowest when q is tiny. | Academic (arXiv) | High |
| F3 | Kalman random-walk betas gave the best description and forecast of time-varying sector beta for 18 pan-European sectors, weekly 1987 to 2005, against alternatives including bivariate t-GARCH, stochastic volatility and Markov switching (Mergner and Bulla, European Journal of Finance 2008), though Bulla's own summary notes the Kalman superiority "is only partly maintained out-of-sample"; on UK company stocks a Kalman random-walk beta dominated GARCH variants on forecast accuracy (Choudhry and Wu 2008). At least one study found bivariate/BEKK GARCH better for out-of-sample beta, so the evidence is not unanimous. | Academic | Moderate-high |
| F4 | In pairs trading, Kalman hedge ratios trade more: Palomar's textbook example shows about 4.5x the turnover of rolling least squares, and at just 2 bp cost the Kalman version ends negative (-0.03) while rolling LS stays positive (0.11). A 2025/2026 SSRN study (Baskaran) found all seven branches negative net of 28 bp round-trip costs, with static OLS beating Kalman in 12 of 14 folds (fold-mean Sharpe -0.157 vs -2.080); estimated Kalman-OLS cost breakeven was 6 to 7 bp one-way. | Textbook / SSRN | Moderate |
| F5 | Short-term reversal exists but is mostly a liquidity-provision premium: Jegadeesh reports about 2% per month (1934 to 1987); de Groot, Huij and Zhou find net long-short returns of 29.0 bp/week for the 100 largest US stocks but -31.4 bp/week for 1,500 stocks; residual (factor-adjusted) reversal earns roughly twice the risk-adjusted return of conventional reversal (Blitz, Huij, Lansdorp, Verbeek 2013); Da, Liu and Schaumburg (Management Science 2014, sample 1982 to 2009, residual returns net of expected return and cash-flow news) report a 1.34% monthly alpha for a residual-based reversal versus 0.33% (insignificant) for the standard one in the same sample. | Academic | High (existence), moderate (size for a long-only buyer) |
| F6 | Intraday returns reverse in the following week while overnight returns do not, and the reversal is stronger in illiquid stocks and volatile markets (Miwa 2019, US/Japan/UK/Eurozone). | Academic | Moderate-high |
| F7 | Buy-the-dip with perfect foresight underperformed plain DCA over 70% of the time across 40-year US windows starting 1920 to 1980, and missing the bottom by just 2 months cut its chance of beating DCA from 30% to 3% (Maggiulli, Of Dollars and Data, practitioner analysis). | Practitioner | Moderate |
| F8 | Value averaging and DCA achieve significantly lower average purchase cost and higher IRR than lump sum in simulation, yet there is no significant difference in dollar profits; lower average cost is "a retrospective response which has no effect on expected profits" (Hayley, City University). | Academic working paper | High |
| F9 | Market microstructure noise in Dow Jones stocks is small: Hansen and Lunde (Journal of Business & Economic Statistics 2006, 30 DJIA stocks, annual averages for 2000) estimate a noise-to-signal ratio slightly below 0.1% and find the noise is time-dependent and correlated with efficient-price increments. | Academic | High |
| F10 | statsmodels `UnobservedComponents` fits local level / local linear trend by maximum likelihood via the Kalman filter; `MLEResults.extend()` filters only new observations with frozen parameters; `append(refit=False)` keeps existing estimates. pykalman's `em()` estimates `transition_covariance` and `observation_covariance` via EM with an `n_iter` cap to limit overfitting. | Library docs | High |

### Assumptions (stated, not verified)

- A1: Your universe is mostly liquid US large/mid caps, where Robinhood fills are close to the quote and explicit commissions are zero. Spread and slippage still apply.
- A2: Each tier decision is logged with a point-in-time timestamp and the exact quote used, so shadow features can be joined later without leakage.
- A3: Intraday quotes for SPY and sector ETFs (for example SMH, XLK) are obtainable from the same live source as your stock quotes at each slot, since yfinance is excluded from live slots.

### Inferences (my reasoning from the facts)

- I1: Because DCA buys are long-only and budget-bounded, your "edge" from any dip signal can only come from shifting dollars across time and tickers, not from shorting. The reversal literature (F5, F6) suggests the shift is most defensible when the drop is idiosyncratic and intraday, which is exactly what a Kalman beta residual isolates and what a raw "price < last_close" test cannot.
- I2: A univariate Kalman "fair value" on daily closes will, under honest maximum likelihood, usually estimate a gain near 1 (because daily microstructure noise is tiny relative to daily variance, F9), which means the fitted "fair price" is essentially yesterday's close. Any smoothing you see is therefore a prior you imposed, not something the data told you, and it must be tuned (and counted as a trial) on an economic objective.
- I3: Average cost basis and IRR will make every dip-weighted rule look good (F8). Your tier ladder already has this bias; a Kalman suggester evaluated on the same metric will inherit it.

## Details

### 1. Ranked applications mapped to your components

| Rank | Kalman variant | Component it maps to | What it does | Expected value | Risk |
|---|---|---|---|---|---|
| 1 | Dynamic beta / time-varying regression (state = alpha, beta vs SPY and sector ETF) | Cluster caps (AI/semis bloc), co-movement shadow signals, Tier C gate | Residual innovation z_resid = (r_stock - alpha - beta*r_mkt) / sqrt(S). High negative z_resid = idiosyncratic dip; low z_resid with large market move = cluster selloff, route to cluster cap budget | High: strongest evidence base (F3, F5) and genuine multivariate advantage over EMA | Low in shadow; beta can "chase" if Q too large |
| 2 | Local-level innovation z-score on log price | Tier A/B thresholds as a volatility-normalized dip-depth feature | z = (log p - x_prior) / sqrt(P_prior + R) makes "how deep is this dip" comparable across a 1%-vol utility and a 4%-vol semiconductor | Moderate: mostly an EWMA z-score, but useful normalization | Low; mislabeling via fat tails |
| 3 | Kalman on log realized variance (or log implied vol from DoltHub) | Inverse-volatility sizing suggester, target weights | Smooths a noisy daily vol estimate and gives an uncertainty band; can fuse realized and implied vol as two measurements of one latent vol | Moderate | Low-moderate; vol jumps on earnings |
| 4 | Robust Kalman bias tracker on Octopus residuals | Octopus band calibration | State = Octopus P50 bias (and log band scale); measurement = realized minus P50. Corrects drift in forecast bias over time; band coverage still best checked with conformal-style rolling coverage | Moderate | Low |
| 5 | Local linear trend (level + slope) | 10:30 trend validation, trend veto | Slope estimate with its own variance; veto when slope t-stat (slope / sqrt(P_slope)) is strongly negative | Low-moderate: 60 intraday minutes is short; slope is noisy | Moderate: may veto exactly the dips that reverse |
| 6 | Intraday local level on slot quotes | Later-slot top-up reference (vs 09:35 fill) | Replaces "price below earlier reference" with "innovation below -k sigma" | Low: hourly slots are far above microstructure scales (F9) | Low |
| 7 | Pairs / hedge-ratio trading per se | None (out of scope) | Requires shorting and high turnover | Negative for this program (F4) | High |

Notes on rank 1 implementation. The video's `hedge_step(beta, p, a, b, q, r)` is the right shape, but feed it returns, not price levels, for market beta (Quantt's guide makes the same point: a time-varying market beta replaces log prices with stock and market returns). Use daily close-to-close returns to estimate beta point-in-time through the prior close, then at each slot compute the intraday residual from the 09:30 open (or prior close) to the current quote using that frozen beta. Split the residual into overnight gap and intraday components, because only intraday moves show reliable reversal (F6).

### 2. The EMA equivalence, quantified

For the local-level model (x_t = x_{t-1} + w, w ~ N(0,Q); z_t = x_t + v, v ~ N(0,R)), let q = Q/R and p = P_prior/R in steady state. Solving the Riccati equation gives p = (q + sqrt(q^2 + 4q)) / 2 and K = p / (p + 1). The equivalent EMA span uses alpha = K = 2/(N+1).

| q = Q/R | Steady-state K (= EMA alpha) | Equivalent EMA span N | Interpretation |
|---|---|---|---|
| 0.001 | 0.031 | about 63 | Very smooth, slow |
| 0.01 | 0.095 | about 20 | Classic 20-bar EMA |
| 0.1 | 0.270 | about 6.4 | Reactive |
| 1 | 0.618 | about 2.2 | Nearly raw price |
| large (q to infinity) | to 1 | to 1 | "Fair value" = last price |

Only the ratio Q/R matters for the steady-state gain; absolute levels affect transients and the width of the uncertainty band.

**When Kalman genuinely beats an EMA (use it only if you need at least one):**

1. **Time-varying R known in advance.** Inflate R on earnings dates (from SEC EDGAR filings already in your store), ex-dividend dates, and the first minutes after the open; the gain automatically drops and the filter stops treating an earnings gap as a "dip." An EMA cannot do this without ad hoc rules.
2. **Missing data and irregular spacing.** Skip the update but keep the predict step, so P grows correctly across halts, holidays and skipped slots (Quantt notes dropping the timestamp entirely corrupts the model clock).
3. **The uncertainty output.** sqrt(P+R) gives a principled denominator for dip depth and a confidence level for fail-closed gating ("if P is above a pre-set limit, emit no suggestion").
4. **Multivariate state.** Level+slope (trend), alpha+beta (residual dips), or one latent vol observed through both realized and implied vol.
5. **Likelihood-based tuning.** Q and R can be estimated rather than guessed, with diagnostics on innovations.

If none of these are used, implement the EMA, log it as an EMA, and save the complexity.

### 3. Evidence on single-stock mean reversion and fair-value signals

- **Univariate fair value on single stocks:** I found no credible evidence that a Kalman local-level "fair price" produces net-of-cost alpha on individual stocks beyond what an EMA deviation (that is, a scaled short-term return) provides. Since the signal reduces to a recent-return measure, its expected value is whatever the short-term reversal premium is for your universe and horizon (F5), which is thin and concentrated in illiquid names and volatile periods.
- **Residual reversal is the stronger form:** removing market/industry components roughly doubles risk-adjusted reversal profits (Blitz et al.; Da, Liu and Schaumburg), and the effect survives costs in large caps after 1990. This is the core reason rank 1 beats rank 2.
- **Dip-weighted DCA evidence is weak on honest metrics:** studies showing gains use IRR or dollar-weighted return, which Hayley shows are biased upward for any strategy that buys more after losses (F8). Perfect-foresight dip buying still lost to DCA in over 70% of 40-year windows (F7).
- **Pairs/hedge ratios:** the Kalman advantage is a smoother, more adaptive beta; the trading disadvantage is turnover (F4). For you, turnover is irrelevant because you only use beta as a classifier, which is why the beta model transfers well while pairs trading does not.

### 4. Known failure modes

| Failure mode | What happens | Mitigation |
|---|---|---|
| Random-walk prices | "Fair value" is a lagged smoother; deviations are just recent returns | Treat as EWMA z-score feature; do not label it "fair value" |
| Q/R sensitivity, flat likelihood | Different Q/R fit similarly but give very different signals | Profile likelihood, multiple starts, sensitivity table; pick Q/R by walk-forward economic objective |
| Regime change | Fixed Q too small after a regime break, filter lags | Refit monthly on trailing window; monitor rolling innovation variance; optionally inflate Q when innovation variance exceeds threshold |
| Fat tails (earnings, gaps) | One outlier jerks the state; Gaussian z overstates rarity | Pre-scheduled R inflation, ex-ante innovation clipping (Huber), or Student-t observation model |
| Look-ahead in Q/R or initialization | Full-history MLE or EM (which uses the smoother) leaks future data | Fit only on data before the evaluation window; never use smoothed states as signals |
| Beta chasing | Large Q on beta absorbs the very dip you want to detect | Small beta Q, beta uncertainty cap, compare to rolling 60-day OLS beta as a sanity bound |
| Intraday microstructure | Bid/ask bounce at open, stale quotes | Ignore first minutes or inflate R at 09:35; treat stale quotes as missing |

### 5. Tuning Q and R properly (point-in-time)

1. **Model choice first, then tuning.** Decide state (level; level+slope; alpha+beta; log-vol), units (log prices or returns), and frequency (daily for beta and fair value; intraday only for slot features).
2. **Estimate by maximum likelihood on innovations.** `statsmodels.tsa.UnobservedComponents(y, 'local level')` (or `'local linear trend'`) and `.fit()`; the log-likelihood is built from one-step innovations. For dynamic beta use `UnobservedComponents` with `exog` and `mle_regression=False` (regression coefficients in the state), or a hand-rolled filter like the video's.
3. **EM as an alternative.** pykalman `KalmanFilter(em_vars=['transition_covariance','observation_covariance']).em(X, n_iter=5 to 10)`; cap iterations and run only on the training window, since EM's E-step uses the smoother.
4. **filterpy** for hand-specified models and production speed; supply Q and R from the offline fit.
5. **Walk-forward protocol.** Train on a trailing window ending at day t-1 (for example 2 to 3 years daily), freeze parameters, filter forward with `res.extend(new_obs)` (filter-only, no refit), refit on a fixed schedule (monthly), and store the parameter version with each logged suggestion. Burn-in the first 60 to 100 observations after any re-initialization and exclude them from scoring.
6. **Diagnostics before trust.** Out-of-sample standardized innovations should have mean near 0, variance near 1, low lag-1 autocorrelation, and about 5% beyond ±1.96. Check by volatility regime and around earnings, not just in aggregate.
7. **If MLE says K is near 1:** accept that the series has no exploitable smoothing at that frequency. If you still want smoothing, choose q from a small grid (for example 3 to 5 values) by a walk-forward economic objective and record the grid size as the number of trials for deflated-Sharpe style adjustment (Bailey and López de Prado).

Minimal sketch of the two core pieces (pure Python, matching the video's interface):

```python
import numpy as np

def ll_step(x, p, z, q, r):
    p = p + q                      # predict
    s = p + r                      # innovation variance
    zscore = (z - x) / np.sqrt(s)  # prior innovation, use for signals
    k = p / s
    return x + k * (z - x), (1 - k) * p, zscore, k

def beta_step(state, P, r_stock, r_mkt, Q, R):
    P = P + Q                       # state = [alpha, beta]
    H = np.array([1.0, r_mkt])
    e = r_stock - H @ state         # residual innovation
    S = H @ P @ H + R
    K = P @ H / S
    state = state + K * e
    P = P - np.outer(K, H @ P)
    return state, P, e / np.sqrt(S)
```

Use the prior innovation (computed before the update) for any signal; the posterior residual is mechanically shrunk by the same observation.

### 6. Evaluation design for a Kalman SizingSuggester in shadow mode

**Log per slot decision:** ticker, slot, quote timestamp, data as-of time, existing tier and multiplier, suggested multiplier, x, P, R, K, z, alpha, beta, beta variance, z_resid (overnight and intraday parts), Octopus P10/P50/P90, parameter version, fail-closed flag and reason.

**Primary metrics (budget-matched):**

| Metric | Why | Pitfall avoided |
|---|---|---|
| Terminal portfolio value and dollar P&L at equal total dollars deployed vs (a) flat DCA at 09:35, (b) current ladder, (c) ladder + Kalman suggestion | The only metric that captures expected-profit differences | Average-cost and IRR bias (F8) |
| Forward 1, 5, 20-day returns after each trigger, market- and sector-adjusted, by z or z_resid bucket | Tests whether deeper (or more idiosyncratic) dips actually recover | Confusing beta exposure with skill |
| Dollars deployed in the bottom z_resid quintile vs top | Measures whether budget shifts toward the signal | |
| Innovation calibration: coverage of ±1.96 band, variance of z, by regime | Validates the uncertainty band before it gates anything | Overconfident z in fat-tailed periods |
| Cash drag: time-weighted uninvested budget | Dip rules that hoard cash lose (F7) | Ignoring opportunity cost |
| Secondary: average cost vs flat DCA | Report it, but never as the decision metric | |

**Sample sizes (inference from standard power formulas, assumptions stated):**

- Forward 5-day residual returns for single stocks have a standard deviation of roughly 4% to 5% (assumption, typical for daily vol near 2%). To detect a 0.5 percentage point mean difference between two trigger groups at 5% significance and 80% power needs about 2 × (1.96 + 0.84)^2 × (4.5/0.5)^2 ≈ 1,270 independent events per group.
- Events on the same date are correlated, so cluster standard errors by date (or block-bootstrap by week); the effective sample can be several times smaller than the raw count. Plan on at least 1,300 events per group and at least 6 to 12 months of calendar time so you span more than one volatility regime.
- Calibration: to estimate 95% band coverage to within ±1 percentage point (one standard error of 0.5 points) needs about 1,900 innovations per segment.
- If you test several Q/R settings or thresholds, adjust significance for the number of trials (deflated Sharpe ratio or minimum backtest length logic).

**Backtest before shadow:** run the same pipeline over historical data with yfinance (allowed for research), strictly walk-forward, then compare shadow-period live-quote results to the backtest for the same dates to detect data-source discrepancies.

## Recommendations

1. **First application (do this):** build a `KalmanResidualDipSuggester` implementing your SizingSuggester interface that computes a time-varying alpha/beta of each ticker vs SPY and its sector ETF from daily returns (fit point-in-time through the prior close), then at each slot emits z_resid for the intraday move. In shadow mode it should only log: "idiosyncratic dip" (z_resid ≤ -2 with small market move) versus "cluster selloff" (market or sector move dominates). It never changes orders. Fail closed on missing ETF quotes, stale data, beta variance above a cap, burn-in, or earnings within ±1 day.
2. **Second:** add the local-level innovation z as a normalized dip-depth feature for tiers A/B, logged as an EWMA-equivalent so nobody mistakes it for fair value.
3. **Third:** use a Kalman log-vol filter (realized plus DoltHub implied vol as two measurements) as the input to the inverse-volatility suggester on your roadmap.
4. **Defer:** local linear trend as the 10:30 veto (weak evidence, risk of vetoing good dips) and any pairs trading.
5. **Main risk:** the residual model may label a broad crash as "cluster selloff" and push the system to under-buy during exactly the large market-wide dips where DCA historically gains most, or beta may drift (Q too large) and absorb real idiosyncratic moves. Treat the residual signal as an allocation input to cluster caps, not as a brake on total deployment.
6. **One verification step before any live effect:** on walk-forward data, confirm that out-of-sample standardized residual innovations have variance within about 0.9 to 1.1 and roughly 5% exceed ±1.96, and that market- and sector-adjusted forward 5-day returns are monotonically higher in more negative z_resid buckets with date-clustered significance. If either fails, the suggester stays shadow-only.

## Caveats

- **Evidence quality:** the beta-estimation and reversal results come from peer-reviewed academic work; the pairs-trading cost results come from a textbook and a recent SSRN preprint; several practitioner repositories are illustrative only. None of the sources tests a Kalman suggester inside a long-only DCA program, so the expected payoff here is an inference, not a measured result.
- **Conflicting evidence:** most beta comparisons favor Kalman over GARCH and rolling OLS, but at least one study favors bivariate GARCH for out-of-sample beta; a rolling-OLS baseline should be logged alongside.
- **Reversal premia are small and time-varying,** concentrated in illiquid stocks and volatile markets; in a large-cap universe after costs the benefit of shifting DCA dollars may be a few basis points per trade.
- **Sample-size figures** rely on an assumed 4% to 5% standard deviation of 5-day returns; recompute with your own universe.
- **Uncertainty overall:** high confidence on the math (EMA equivalence, tuning, leakage rules), moderate confidence that residual-dip classification improves cluster-cap allocation, low confidence that any Kalman variant materially improves terminal wealth versus your current ladder without at least a year of shadow evidence.
- This is research for a personal research system, not personalized financial advice.
