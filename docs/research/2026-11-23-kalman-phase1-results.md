# Kalman residual-dip signal: first Phase 1 walk-forward run

Status: research output only. Not a decision; the D7 gate failed, so nothing moves past Phase 1.
Date: 2026-11-23
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

Phase 2 (live shadow logging) stays blocked until D7 passes on SPY.
