# Kalman residual-dip signal: first Phase 1 walk-forward run

Status: research output only. Not a decision. The D7 gate failed in both rounds (2026-11-23 and
2026-11-24), so nothing moves past Phase 1.
Date: 2026-11-23, second round 2026-11-24 (below)
ADR: [`docs/adr-kalman-residual-dip-shadow.md`](../adr-kalman-residual-dip-shadow.md), Phase 1.
Code: `dca-sizing-signals/kalman_signals/` at the commit that added this file.

## What was run

```bash
cd dca-sizing-signals
python -m kalman_signals.backtest_cli --history-dir ../ticker_data --market VT \
    --non-equity AAAU,XLG,QQQ,VT,TOPT,QTOP,FLKR,DXYZ \
    --start 2024-01-02 --end 2026-11-20 --out-json k.json --out-md k.md
```

- Universe: `config/tickers.json` `active` (122 symbols) minus the non-equity list above. That
  list was written by hand and may miss funds (it is a CLI argument, not a check).
- **Market factor: VT, not SPY.** The archive has no SPY file as of 2026-11-23. VT is a
  global fund whose underlying non-US holdings close hours before the US session, so it is
  a noisier, partly asynchronous market factor than SPY.
- Market factor only: no `--clusters` file was passed, so no cluster factor and no
  `cluster_selloff` labels.
- No earnings gate (no point-in-time earnings dates offline).
- Default config: 504-session window, 60-session burn-in, q grid {1e-5, 1e-4, 1e-3} (3 trials),
  monthly refit. 15 seconds on the operator's Mac.

## Results

70,509 scored (ticker, session) events, 35 months.

| D7 check | Result | Pass |
| --- | --- | --- |
| Variance of z_total in 0.9 to 1.1 | 1.297 (high-vol regime 1.399, low-vol 1.233) | No |
| Share beyond ±1.96 in 3% to 7% | 6.1% (high-vol 6.8%, low-vol 5.7%) | Yes |
| Forward returns monotonic in intraday z | No (top bucket breaks the order) | No |
| ≥1,300 events per bucket | smallest bucket 1,323 | Yes, barely |
| ≥6 months | 35 | Yes |

Forward 5-session VT-adjusted log return by intraday z (date-clustered SE):

| Bucket | Events | Mean | SE |
| --- | --- | --- | --- |
| z ≤ -2 | 1,323 | +0.38% | 0.24% |
| -2 < z ≤ -1 | 6,113 | +0.30% | 0.08% |
| -1 < z ≤ 0 | 26,836 | +0.10% | 0.04% |
| 0 < z ≤ 1 | 27,840 | +0.05% | 0.04% |
| z > 1 | 7,900 | +0.09% | 0.08% |

Labels: 64,089 `none`, 5,295 `market_selloff`, 1,125 `idiosyncratic_dip`.

## Reading it

- Variance well above 1 with a near-normal tail share means the scale is off more than the
  tails: R (the OLS residual variance) understates the noise on a typical day. The likely
  causes, none tested yet: VT's asynchronous close inflating residuals; earnings days left in
  (no gate); volatility clustering a constant R cannot follow (variance is highest in the
  high-vol regime).
- The four lower buckets are ordered the way the research predicts (more negative intraday
  residual, higher forward adjusted return), and the -2 < z ≤ -1 bucket is about 3.5 SEs
  above zero. The z ≤ -2 bucket's SE is too wide to separate it from its neighbour. This is one
  run with 3 trials, an imperfect market factor and no cluster factor: suggestive, not evidence.

## Next steps inside Phase 1 (not done here)

1. Get SPY daily history into the archive and re-run with `--market SPY`.
2. Pass a clusters file from `comovement_signals.build_clusters` so the cluster factor and
   `cluster_selloff` label are exercised.
3. Add a point-in-time earnings gate input, or at least report results with and without
   earnings-window sessions.
4. Record the q grid (3 trials, plus any later changes) in the Phase 0 ledger's `trials` table.
5. If variance stays high, consider a time-varying R (e.g. scaled by trailing market vol);
   each such change is another trial.


## Second round (2026-11-24): SPY, clusters, earnings

Worktree: `.claude/worktrees/resolve-merge-conflicts-3d8a6a`, branch
`badMade/kalman-filters-stock-dca-02d0fc`. Scripts: `analysis/2026-11-24-kalman-phase1/`.

Next steps 1 to 4 above were done. Code changes: `--market-file` (SPY lives outside the managed
archive), `--earnings`/`--earnings-pad`, forward returns by label, and the gate now also checks
calibration **within each volatility regime**, as D7.1 says; round 1's gate checked only the pooled
figure. All four runs below were re-run under the tightened gate.

| Run | Market | Clusters | Earnings excluded | Events | Var (pooled / high vol / low vol) | Beyond ±1.96 | Failed checks |
| --- | --- | --- | --- | --- | --- | --- | --- |
| r1 | VT | no | no | 70,509 | 1.297 / 1.399 / 1.233 | 6.1% | variance, variance by regime, monotonic |
| r2 | SPY | no | no | 69,826 | 1.300 / 1.377 / 1.255 | 6.2% | variance, variance by regime, monotonic |
| r3 | SPY | yes | no | 69,826 | 1.300 / 1.377 / 1.255 | 6.2% | variance, variance by regime, monotonic |
| r4 | SPY | yes | ±1 session | 66,353 | **1.046** / 1.143 / 0.990 | 5.2% | variance by regime, monotonic, events per bucket |

r4 forward 5-session SPY-adjusted log return by intraday z (date-clustered SE):

| Bucket | Events | Mean | SE |
| --- | --- | --- | --- |
| z ≤ -2 | 1,107 | +0.59% | 0.25% |
| -2 < z ≤ -1 | 5,762 | +0.30% | 0.08% |
| -1 < z ≤ 0 | 25,783 | +0.12% | 0.04% |
| 0 < z ≤ 1 | 26,121 | +0.09% | 0.04% |
| z > 1 | 7,194 | +0.11% | 0.08% |

r4 by label (descriptive, not a D7 check): `cluster_selloff` 2,826 events +0.41% (SE 0.13%),
`idiosyncratic_dip` 557 events +0.44% (SE 0.33%), `market_selloff` 4,774 events on only 62 dates
+0.23% (SE 0.13%), `none` +0.12% (SE 0.03%).

### Reading it

- **VT was not the problem.** SPY gives the same 1.30 variance.
- **Earnings days were.** Removing the ±1-session window around each announcement (3,473 events,
  5%) brings the pooled variance from 1.30 to 1.05 and the tail share from 6.2% to 5.2%. The
  remaining miss is the high-volatility regime at 1.14: a constant R still understates noise when
  the market is turbulent, which is what next step 5 (time-varying R) targets.
- **Clusters change labels, not scores.** Calibration and buckets are market-factor quantities, so
  r2 and r3 are identical there; with clusters, 2,996 events become `cluster_selloff`, and the
  `idiosyncratic_dip` set shrinks from 1,147 to 714.
- **Monotonicity misses on the top bucket only**: z > 1 is 0.02 points above 0 < z ≤ 1, well inside
  their SEs. The four lower buckets are ordered, and z ≤ -2 is +0.59% at about 2.4 SEs. The gate is
  not loosened to pass on this: that would be a post-hoc change to the bar.
- **The z ≤ -2 bucket fell below 1,300 events** (1,107) once earnings days were removed, since
  many deep dips are earnings reactions.
- **D7 items not yet evaluated:** cluster-adjusted forward returns (D7.2 asks for market- and
  cluster-adjusted; only market-adjusted is computed), survival against the trial count (D7.4,
  deflated Sharpe), and budget-matched terminal value (D7.5).

### Caveats

- Clusters are fixed at 2023-12-29 for all 35 months: no lookahead, but stale by the end.
- Earnings dates are yfinance's `get_earnings_dates`; DRAM, BTX, SATA, RVI and VCX returned none,
  so their earnings days are still in. Not cross-checked against EDGAR.
- The non-equity list is still hand-written.
- Four configurations count as trials so far: these four runs, each with the same 3-value q grid.
  The four trial rows were written to the Phase 0 ledger
  (`cron_tracking/goal_snapshot/ledger.duckdb`, experiment `kalman_residual_dip_phase1`) on
  2026-11-24 with the operator's approval; they are the ledger's first `trials` rows.

### Next steps

1. Time-varying R scaled by trailing market volatility, as one new trial, to address the
   high-vol regime.
2. Cluster-adjusted forward returns, the D7.4 deflated-Sharpe check, and the D7.5 terminal-value
   comparison, so the full gate can be read.
3. A longer history (start before 2024) to lift the z ≤ -2 bucket past 1,300 events.

Phase 2 (live shadow logging) stays blocked until every D7 check passes.
