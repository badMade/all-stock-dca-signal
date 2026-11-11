# Daily composite-rank run (runbook for the scheduled task)

Scheduled task id: `daily-composite-rank` (weekdays, 5:45 PM local time = after the 4:00 PM ET close).
This file is the source of truth for what that task does; edit it here and the next run follows it.

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
2. **Holdings (read-only).** Call `get_accounts`; for every account with a funded position call
   `get_equity_positions` (follow pagination). Write `artifacts/composite_rank/inputs/holdings.json` as
   `{"symbols": [sorted unique tickers with quantity > 0]}`.
3. **S&P 500 list (weekly).** If `artifacts/composite_rank/inputs/sp500.json` is missing or older than 7 days,
   download `https://en.wikipedia.org/wiki/List_of_S%26P_500_companies` with `curl -A "Mozilla/5.0 (research script)"`
   into the scratchpad and run
   `python3 analysis/composite-rank/composite_rank.py ingest-sp500 <saved.html>`. If parsing fails, keep the old file.
4. **Tickers the archive lacks.** Run `python3 analysis/composite-rank/composite_rank.py missing`. For those
   tickers call `get_equity_historicals` (max 10 symbols per call) with `interval: "day"`,
   `adjustment_type: "all"` (dividend-adjusted), `start_time` = 420 days ago. The results are large and get saved to
   files by the tool; pass those file paths to
   `python3 analysis/composite-rank/composite_rank.py ingest-bars <file> [<file> ...]`.
   If a ticker returns no bars, leave it (it is listed in `data_gaps.json`). If a series has an impossible jump
   (for example a 60% one-day drop that matches a stock split), refetch that ticker with
   `adjustment_type: "split"` and note that it is price-only.
5. **Score and rank.** `python3 analysis/composite-rank/composite_rank.py run --threshold 80`
   (writes `runs/<date>/ranked_all.csv`, `runs/<date>/evaluation_list.md`, appends `history.csv`, rebuilds
   `movement_summary.md`, refreshes `latest_*`).
6. **Fractional tradability (new names only).** For tickers on the evaluation list that are not held and not in
   `inputs/tradability.json` (or whose entry is older than 7 days), call `get_equity_tradability` on the account
   whose nickname is "MCP" (max 10 symbols per call). Store `{ticker: fractional_tradability}` in
   `inputs/tradability.json`, then re-run step 5 once (the run is idempotent per date).
7. **Report** (short): as-of date; names scored; how many are above 80; who entered and dropped off the list;
   top 10; the five biggest one-day rank climbers and fallers (`movement_summary.md`); data gaps; anything skipped.
   If the as-of date equals the previous run (market holiday), say "no new trading day" and stop.

## Files

| Path (under `artifacts/composite_rank/`) | What it is |
| --- | --- |
| `inputs/holdings.json`, `sp500.json`, `ext_prices.json`, `tradability.json`, `watchlist.json` | Inputs. `watchlist.json` is an optional JSON list of extra tickers to score. |
| `runs/<date>/ranked_all.csv` | Every scored ticker with score, rank, volatility, drawdown, period returns. |
| `runs/<date>/evaluation_list.md` | The day's list (score above the threshold) with new / dropped names and rank change. |
| `runs/<date>/data_gaps.json` | Tickers with no price history or too little history to score. |
| `history.csv` | One row per ticker per run: rank and score. The basis for movement tracking. |
| `movement_summary.md` | How far names typically move by starting rank, and per-ticker rank behaviour. |
| `latest_evaluation_list.md`, `latest_ranked_all.csv` | Copies of the most recent run. |
