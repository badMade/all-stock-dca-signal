# Runbook: dl-trading-pipeline backtest

STATUS: untested. Thresholds are UNVERIFIED. Results from before the pipeline's look-ahead fixes are not comparable.

1. Export `DLTP_REPO` pointing at your dl-trading-pipeline checkout (no path is hardcoded anywhere).
2. `cd agent-vault && claude`, then ask: "Run a backtest iteration".
3. The agent follows `skills/backtest-dltp/SKILL.md`: reads `vault/Knowledge/dl-trading-backtests.md`, records the
   checkout's commit SHA, and runs `make check`. A red check ends the iteration.
4. It runs the documented backtest entry point with an explicit seed, and records the seed, model, fold count and
   command in `vault/Daily/YYYY-MM-DD.md`.
5. It collects Sharpe per fold and the mean. It compares with live results only from a file you name; otherwise it
   logs "no live comparison available".
6. A mean Sharpe below `backtest_sharpe_min` is flagged, with the caveat that the number is UNVERIFIED.
7. It never commits or pushes to the pipeline repo unless you ask in that session.
8. It closes with `reflect`; the commit summary starts `[dl-trading-pipeline] YYYY-MM-DD <outcome>`.
