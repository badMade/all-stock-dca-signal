# ADR: Dated no-buy window for tickers sold at a loss

- **Status:** Accepted 2026-11-20 (operator chose this design in chat after the exclusion-list
  alternative was costed). Slice 1 and 2 are implemented; later slices are listed below.
- **Builds on:** `docs/adr-tax-loss-sale.md` (item 3, the buy pause, and open question 4, "how the
  executor enforces the pause"). This is the dedicated mechanism that ADR deferred.
- **Not tax advice.** Confirm every consequence with a tax professional before filing.

## Context

On 2026-11-20 the operator sold DFTX, RGTI, LITE, CBRS, VCX, INTC and SNDK at a realized loss for
tax-loss harvesting. The DCA program buys most names almost daily, and a purchase of the same stock
within 30 days before or after a loss sale can wash the loss (IRC section 1091). Buys in any of the
taxpayer's accounts count, and a buy in an IRA or Roth can disallow the loss permanently. A read-only
lookback the same day found the DCA account (the MCP account) holding many small fractional buys of six
of the seven names from the prior 30 days, so every further scheduled buy adds replacement shares.

The interim named in the tax-loss ADR is the permanent `excluded` list. Trying that in a scratch copy
showed it touches about ten files (the `EXCLUDED_TICKERS` constant, the `scheduled_tasks.json` mirror,
`ticker_history.csv`, docs and at least six tests that pin the universe size and the excluded set), stops
tracking, and has to be reverted by hand on the day buying resumes.

## Decision

1. `config/tickers.json` gets an optional `no_buy` object keyed by ticker. Each entry has `blocked_on`,
   `until`, `reason` and optionally `released_on` and `release_reason`. A ticker is blocked when
   `blocked_on <= today <= until` (both inclusive, US Eastern) unless `released_on` is set and reached.
2. The window is 31 calendar days after the sale date: 30 days of the statutory window plus a one-day
   buffer. For the 2026-11-20 sales, the last blocked day is 2026-12-21 and buying resumes 2026-12-22. The
   statutory minimum would resume on 2026-12-21.
3. `dca_config.no_buy_blocked_tickers(cfg, on)` is the one rule. It fails closed: a malformed block
   raises `ValueError` naming `no_buy`, and one bad entry fails the whole load. The block is a plain dated
   blocklist and is not cross-checked against `active` or `excluded`, so a config that rewrites `active` cannot
   abort a run through it.
4. `dca_config.load_tickers` returns the blocked set as `no_buy_tickers` (present only when non-empty) and
   leaves `tickers` and `excluded_tickers` alone. `StrategyConfig` and `MorningConfig` carry it as
   `no_buy_tickers`. `load_strategy_config(now=...)` uses the run's own clock, so a replayed day sees that
   day's windows.
5. **The BUY is vetoed at buy time, not by shrinking the universe.** A first attempt folded blocked names
   into `excluded_tickers` and removed them from the run-time ticker list. It broke 33 tests: the backup
   planner's stamped universe hash and symbol count (122 became 115), the committed Perplexity prompts, and
   anything else keyed to the universe. So the universe, its stamps and the signals do not change, and each
   entry point withdraws the BUY itself:
   - `near_close_dca._process_buy_candidate` returns a `skipped` outcome with `skip_reason: no_buy_window`
     before any order, top-up or credit flush is built, so the blocked ticker still appears in the run's
     candidate rows.
   - `morning_dca.MorningDCAExecutor.evaluate_signal` (the four executors and `scripts/stream_candidates`)
     scores the ticker, then returns NO_ACTION with a zero size and `no_buy_window` in the reason and
     warnings. The row keeps the prices and the drop.
   - `scripts/generate_buy_candidates.py` does the same on the reconciled decision.
   - `dca_runner.py` reads `tickers.json` itself, so it checks the block at call time in tier selection,
     `plan_orders` (refusal `no_buy_window`) and `execute_plan`.
   - `dca_order_plan.py` and `scripts/backup_planner.py` refuse a blocked symbol like an excluded one when
     they build or validate a plan, without touching the universe they stamp.
6. Early release is `python -m scripts.no_buy_window release SYMBOL --reason "..." --accept-wash`. A
   release inside the window refuses without `--accept-wash`, always needs a reason, sets `released_on`
   (never deletes the entry), and changes only that entry's line. `status` lists every entry.

## Not covered (known gaps)

- **Tracking is partial.** A blocked ticker is still scored and keeps its signal row and prices, so the
  signals show what the ladder wanted. There is no separate would-have-bought dollar record yet; the
  3:30 PM path shows it as a skipped candidate with `no_buy_window`, the morning family as a NO_ACTION row.
- **Bare `StrategyConfig()` constructions** never read `tickers.json` and so never see the block. Every
  live path found uses `load_strategy_config` or the stream loaders; a new code path that builds a bare
  config would bypass it.
- **The live prompts** in `claude/dca-executor-prompts/live-mirror` hold no ticker lists and obey the
  candidate files and `dca_runner`, which enforce the block. Nothing in this repo can stop an external
  agent that ignores both.
- **Manual orders and other accounts.** The block only stops this program. It does not watch the main
  account, the IRAs or the managed accounts, dividend reinvestment, or a spouse's accounts.
- **BYDDY** (a whole-share ticker sold at a loss on 2026-11-17) is not blocked: the config forbids a
  ticker in both `excluded` and `whole_share`. Its exposure is small, about one share per accumulator fire.
- **No lookback.** The 30 days before a sale are not checked. That needs the cross-account lot ledger.

## Operational note: planner and prompts

`backup_planner.pyz` bundles the import closure of the backup planner, which includes `dca_config` and
`near_close_dca`, and the Perplexity backup prompts stamp its build id. Any change to those sources, this
one included, therefore means rebuilding the planner (`python -m scripts.build_backup_planner --out
backup_planner.pyz`) and regenerating the prompts (`python -m scripts.generate_perplexity_backup_prompts
--write`). The universe stamp itself is unchanged (122 symbols, same hash), so only the build stamp moves.
The live Perplexity tasks hold the pasted prompt text and must be updated to the regenerated prompts.

## Later slices (each needs its own test first)

1. A single guard at order submission plus a pre-sale lookback report.
2. A cross-account lot ledger and a daily loss-sale detector that proposes (never writes) new entries.
3. A blocked-buy record in run artifacts.
4. A year-end reconciliation against the 1099-B for Form 8949 code W lines.

## Rollback

Delete the `no_buy` block from `config/tickers.json`. Nothing else holds state. Removing the mechanism itself means
removing the `no_buy_tickers` field and the veto in each entry point listed above, then rebuilding the planner
and regenerating the prompts.

## Verification

`tests/test_no_buy_window.py` (the rule), `tests/test_no_buy_enforcement.py` (the loader, each stream
config and `dca_runner`) and `scripts/tests/test_no_buy_window_cli.py` (release safety).
