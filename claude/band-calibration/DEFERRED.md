# DEFERRED: band-calibration cycle

| Item | Why deferred |
|---|---|
| Probability of Backtest Overfitting | Only useful once several model variants are being chosen between |
| Model Confidence Set, Diebold-Mariano | Needs Rock 2's per-date series first; candidate for the next cycle |
| Columns for the new fields in `report.md` | Rock 1 adds them to `report.json` only; the tables are a layout decision |
| Console page for the trust scorecard | Follows Rock 3 once the history shape is settled |
| Train-on-sampled-paths round trip (TSTR, research design E) | Highest cost and least direct. In scope by operator decision, but it would be an eighth Rock, so it moves to the next cycle and is planned once Rocks 6 and 7 show what question is left for it |
| Pre-listing backcast of ticker history | Rejected, not deferred: it invents data and breaks the eligibility rule in `horizons.py` |
| Delisted tickers in the archive | Real gap (survivorship). Needs a data-source decision of its own |
| Labeling the 90-day experiment report with the model it ran | Small fix to `scripts/run_90day_prediction_experiment.py`; separate PR |
| Scheduling the trust scorecard after the daily update | An operator action on the Mac (launchd), or a later `daily_update.py` hook; kept out of Rock 3 so the daily cycle is not touched |
| Multi-horizon end-to-end check of the trust scorecard on a real run | The demo run is horizon 1 only; needs the full-archive run directory on the operator's Mac |
| Synthetic-truth replay of the Octopus model itself | Rock 4 replays the GBM quantile model, which is enough to test the harness and scoring. Octopus has its own training path, so its replay is a separate, longer run on the operator's Mac |
| A subtler planted signal | Lag-1 correlation 0.5 shows the harness has power. How weak a signal it can detect, and at what archive size, is a power study of its own |
