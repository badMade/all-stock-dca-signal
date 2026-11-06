# Daily composite-rank run (runbook for the scheduled task)

Scheduled task id: `daily-composite-rank` (weekdays, 5:45 PM local time = after the 4:00 PM ET close).
This file is the source of truth for what that task does; edit it here and the next run follows it.

## Scope

The ranked universe is the **S&P 500 only** (`--universe sp500`, the default). Holdings are read only to flag which
constituents you own (held names outside the S&P 500, such as ETFs, are not ranked for now). The wider universe
(`--universe all`: holdings plus a watchlist, later the whole archive) is built in but not scheduled yet.

## Hard rules

- **Read-only.** Never place, cancel, or modify an order. Use only read tools (quotes, positions, portfolio,
  historicals, tradability). Never call `place_equity_order` or any review/place order tool.
- Write only under `artifacts/composite_rank/` (gitignored) and the session scratchpad. Do not edit tracked
  repo files, `config/`, or `ticker_data/`. Do not commit or push anything.
- Never write a full brokerage account number into any output file or the report; mask to the last four digits.
- If a step fails, keep going with what exists, finish the run, and say plainly what was skipped.

## Steps (run from the repository root)

1. **Archive freshness.** Look at the last date in `ticker_data/AAPL.csv`. If it is older than the most recent
   trading day, run the `tickers-daily-update` skill first. If it is still stale afterwards, continue and report it.
2. **Holdings (read-only, used for the "Held" flag).** Call `get_accounts`; for every account with a funded position
   call `get_equity_positions` (follow pagination). Write `artifacts/composite_rank/inputs/holdings.json` as
   `{"symbols": [sorted unique tickers with quantity > 0]}`.
3. **S&P 500 list (weekly).** If `artifacts/composite_rank/inputs/sp500.json` is missing or older than 7 days,
   download `https://en.wikipedia.org/wiki/List_of_S%26P_500_companies` with `curl -A "Mozilla/5.0 (research script)"`
   into the scratchpad and run
   `python3 analysis/composite-rank/composite_rank.py ingest-sp500 <saved.html>`. If parsing fails, keep the old file.
4. **S&P 500 tickers the archive lacks.** Run `python3 analysis/composite-rank/composite_rank.py missing`. For those
   tickers call `get_equity_historicals` (max 10 symbols per call) with `interval: "day"`,
   `adjustment_type: "all"` (dividend-adjusted), `start_time` = 420 days ago. The results are large and get saved to
   files by the tool; pass those file paths to
   `python3 analysis/composite-rank/composite_rank.py ingest-bars <file> [<file> ...]`.
   If a ticker returns no bars, leave it (it is listed in `data_gaps.json`). If a series has an impossible jump
   (for example a 60% one-day drop that matches a stock split), refetch that ticker with
   `adjustment_type: "split"` and note that it is price-only.
5. **Score and rank.** `python3 analysis/composite-rank/composite_rank.py run`
   (thresholds and every weight come from `params.json`; the run is stamped with its `version`; writes
   `runs/<date>/trade_list.csv`, upserts `trade_list_history.csv`, writes `runs/<date>/tickers_proposal.md`; also writes `runs/<date>/ranked_all.csv`, `runs/<date>/evaluation_list.md`, appends `history.csv`, rebuilds
   `movement_summary.md`, refreshes `latest_*`).
6. **Fractional tradability (new names only).** For tickers on the evaluation list that are not held and not in
   `inputs/tradability.json` (or whose entry is older than 7 days), call `get_equity_tradability` on the account
   whose nickname is "MCP" (max 10 symbols per call). Store `{ticker: fractional_tradability}` in
   `inputs/tradability.json`, then re-run step 5 once (the run is idempotent per date).
7. **Report** (short; use `composite_rank.py history --ticker ...` for any rank history you need, never read `history.csv` whole): as-of date; names scored; how many are above 80; who entered and dropped off the list;
   top 10; the five biggest one-day rank climbers and fallers (`movement_summary.md`); data gaps; anything skipped.
   If the as-of date equals the previous run (market holiday), say "no new trading day" and stop.

## The daily trade list and the review-first proposal

Each run builds ONE list: every S&P 500 name scoring above `evaluate_threshold` (80), plus every holding above
`held_threshold` (50). It is appended to `trade_list_history.csv` (`date, ticker, rank, score, reason, params_version`),
so the history of what was on the list each day is kept. The same run compares the list with `config/tickers.json` and
writes `tickers_proposal.md` (names it would add, names that would stop being bought). **The task never edits `config/`.**
The operator reviews the proposal and asks for it to be applied (a normal draft PR: tickers.json, `near_close_dca`
constants, sizing calibration, prompts, planner, tests).

## Forward-looking models in shadow mode (zero weight)

`params.json` `forward_weights` (`octopus`, `ml`) are both **0**, so forward models are logged and measured but cannot
change a score or the list. If `inputs/octopus_sources.json` lists Octopus run directories (JSON list of paths), each
`run` reads their newest `*_next_day_forecasts.csv` exports and upserts `shadow_octopus_history.csv`
(`origin_date, ticker, exp_ret, p10_ret, p90_ret, expected_target_date, fwd_pct`); `shadow-octopus <paths>` does the
same by hand. `shadow-scorecard` reports, per forecast day, the rank correlation (IC) of Octopus's expected return and
of the composite score with the realised return. A model earns weight only when its IC beats the composite's over
enough days; then raise its `forward_weights` entry, bump `version`, and the blend applies from that run on.
ml-forecaster is logged the same way: each `run` (or `shadow-ml`) reads its `sizing_suggestions/ml_forecaster/*.jsonl`
history (default under `$DCA_CRON_TRACKING_ROOT`; `--ml-root` overrides), turns each multiplier into a tilt
(`multiplier - 1`, monotonic in its predicted 5-day return) and upserts `shadow_ml_history.csv`
(`date, ticker, tilt, fwd_pct, status, model_version`). `shadow-scorecard` also scores it over the next 7 calendar days.
It stays empty until ml-forecaster has published at least one run to that history.
Octopus forecasts are only as fresh as its last `daily_update` run, and today it predicts one day ahead.

## Tuning the ranking

All weights, the 20% ROI bonus, the volatility/drawdown blend and both cutoffs live in `params.json`. Change a value,
bump `version`, commit. `composite_rank.py effectiveness --horizon 20` then reports, per day and params version, the
forward return of that day's list against the whole scored universe, which is the evidence for the next change.
Nothing adjusts weights automatically.

## Files

| Path (under `artifacts/composite_rank/`) | What it is |
| --- | --- |
| `inputs/holdings.json`, `sp500.json`, `ext_prices.json`, `tradability.json`, `watchlist.json` | Inputs. `watchlist.json` is an optional JSON list of extra tickers to score. |
| `runs/<date>/ranked_all.csv` | Every scored ticker with score, rank, volatility, drawdown, period returns. |
| `runs/<date>/evaluation_list.md` | The day's list (score above the threshold) with new / dropped names and rank change. |
| `runs/<date>/data_gaps.json` | Tickers with no price history or too little history to score. |
| `trade_list_history.csv`, `runs/<date>/trade_list.csv`, `latest_trade_list.csv` | The daily list (score above 80, or held above 50) with the reason and the params version. |
| `runs/<date>/tickers_proposal.{md,json}`, `latest_tickers_proposal.md` | Review-first diff of the list against `config/tickers.json`. Never applied automatically. |
| `shadow_octopus_history.csv`, `shadow_ml_history.csv` | Octopus and ml-forecaster signals logged at zero weight (see above). |
| `history.csv` | One row per ticker per run: rank, score, volatility, drawdown. No holdings column. The basis for movement tracking. Read it with the `history` command, not by loading the file. |
| `movement_summary.md` | How far names typically move by starting rank, and per-ticker rank behaviour. |
| `latest_evaluation_list.md`, `latest_ranked_all.csv` | Copies of the most recent run. |
