# composite-rank

Daily, read-only ranking of your holdings plus the S&P 500 on a weighted multi-period ROI score with a
low-volatility / low-drawdown tilt, and a history log that tracks how ranks move day to day. Nothing here
touches the live strategy, `config/`, or any order path.

- `composite_rank.py` -- the calculation (standard library only). Commands: `run`, `missing`, `ingest-bars`,
  `ingest-sp500`. The scoring formula is documented at the top of the file.
- `RUNBOOK.md` -- the steps the scheduled task `daily-composite-rank` follows every weekday after the close.
- `tests/` -- hermetic tests on synthetic prices (they run in the root `pytest` sweep).
- Output: `artifacts/composite_rank/` (gitignored). See the table in `RUNBOOK.md`.

Quick start (after the inputs exist):

```bash
python3 analysis/composite-rank/composite_rank.py run --threshold 80
python3 analysis/composite-rank/composite_rank.py run --top 25            # list the top 25 instead
python3 analysis/composite-rank/composite_rank.py run --as-of 2026-10-30   # re-score a past date
```

Known limits: percentile scores are relative to whatever pool is scored that day, so adding or removing
tickers shifts every score slightly. Backfilled history uses today's holdings for every past date. Price
history for tickers outside the Yahoo archive (ETFs, some ADRs) comes from the Robinhood connector and is
only as complete as that source. Past returns do not predict future returns; this is a screening aid.
