# DEFERRED: band-calibration cycle

| Item | Why deferred |
|---|---|
| Purged and embargoed CPCV | High compute; answers "generalization across regimes", which is not this cycle's question |
| Probability of Backtest Overfitting | Only useful once several model variants are being chosen between |
| Model Confidence Set, Diebold-Mariano | Needs Rock 2's per-date series first; candidate for the next cycle |
| Time-reversed model as a control | Valid diagnostic, roughly doubles training cost, does not improve the bands |
| Train-on-sampled-paths round trip (TSTR) | Indirect and expensive; no evidence it would change a decision |
| Pre-listing backcast of ticker history | Rejected, not deferred: it invents data and breaks the eligibility rule in `horizons.py` |
| Delisted tickers in the archive | Real gap (survivorship). Needs a data-source decision of its own |
| Labeling the 90-day experiment report with the model it ran | Small fix to `scripts/run_90day_prediction_experiment.py`; separate PR |
