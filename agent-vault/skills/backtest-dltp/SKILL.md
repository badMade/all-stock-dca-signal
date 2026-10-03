---
name: backtest-dltp
description: Orchestrate a dl-trading-pipeline backtest iteration and compare it with live results. Use when the user says "run a backtest iteration", "test the new volatility model", or asks for a dl-trading-pipeline check. UNTESTED.
---

# Backtest DLTP

One job: run a seeded backtest in the dl-trading-pipeline checkout, record the metrics,
and compare them with live results when the user supplies them.

STATUS: UNTESTED. Drafted from the setup brief and the pipeline's README, never run.
Thresholds are `UNVERIFIED`. Numbers from before the pipeline's look-ahead fixes are
not comparable with new ones.

> `vault/…` paths resolve against the vault base printed in the SessionStart header.

## Steps

1. **Read** `vault/Knowledge/dl-trading-backtests.md`: model setup, validation rules,
   prior results.
2. **Locate the checkout** from the `DLTP_REPO` environment variable. If it is unset,
   ask; never hardcode a path. Record the commit SHA of the checkout.
3. **Gate on its own checks.** Run the repo's `make check` first. A red check ends the
   iteration; log it.
4. **Run the backtest** through the entry point its README documents, with an explicit
   seed. Record the seed, the model, the fold count and the exact command.
5. **Collect** per-fold and mean Sharpe, plus any slippage the run reports. Do not
   compute a slippage figure the pipeline does not produce.
6. **Compare with live** only from a results file the user names. With none, log
   `no live comparison available`. Flag a mean Sharpe below `backtest_sharpe_min` in
   `config.yaml`; both it and the slippage limit are `UNVERIFIED`.
7. **Log** under today's `## Sessions` in `vault/Daily/YYYY-MM-DD.md`: seed, SHA,
   command, metrics, flags. Name the file by date only.
8. **Capture** durable findings into the Knowledge note with the `capture` skill.
9. **Close** with the `reflect` skill. Start its commit summary with
   `[dl-trading-pipeline] YYYY-MM-DD <outcome>`.

## Rules

- Never push to the pipeline repo or commit there unless the user asks in this session.
- Same seed and same data must give the same numbers; if they do not, report that as
  the finding and stop.
- No order tools. No Notion writes.
