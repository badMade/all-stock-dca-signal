<!-- Complete, single-piece prompt for task 041d9d90 (Weekly DCA Notion performance dashboard refresh, Fridays 5:30 PM ET). Paste this whole file as the task prompt. No edits or substitutions are needed. -->

Run the **weekly DCA Notion performance dashboard refresh** (mirrors Perplexity task `041d9d90`). Analytics only, no trading. This is the one approved post-3:40 PM DCA task and carries all the heavy analysis deferred from the weekday compact chain.

**Hard prohibition:** analytics only. NEVER call `place_equity_order`, `review_equity_order`, `cancel_equity_order`, or any option/crypto/margin tool. Perplexity is the only live executor for the Agentic Robinhood account. Read tools only.

**Account handling:** identify the account with `get_accounts` (read-only). The Agentic account is the single account that `get_accounts` reports as tradable by this agent (its agentic_allowed field is true). Do NOT select by nickname: the nickname is not reliable. If zero accounts or more than one account are tradable, STOP and go to step 8. Use that account's number for tool calls. Never print, log, post, or DM the account number, and never mention the field name or its value; refer to it only as "the Agentic account".

**Runtime facts:**
- The repo is PRIVATE on GitHub. You CANNOT clone it in this cloud container. Do not try.
- The code lives on Geoff's Mac (device "geoffreacstudio-lan"), reached only through `mcp__remote-devices__device_bash`.
- The Mac runs Python 3.10, so always pass an explicit `+00:00` offset to `--now`, never a bare `Z`.

**Steps:**

1. Locate the repo: `REPO=$(ls -d /sessions/*/mnt/all-stock-dca 2>/dev/null | head -1); echo "REPO=$REPO"`
   If empty, or any `mcp__remote-devices__*` call errors, the Mac is unreachable. STOP and go to step 8.
2. Full report run:
   `cd "$REPO" && python3 run_dca.py --demo --report --now "$(date -u +%Y-%m-%dT%H:%M:%S+00:00)" --artifacts-dir /tmp/dca-weekly 2>&1 | tail -40; echo "EXIT=${PIPESTATUS[0]}"`
   Then the dashboard: `cd "$REPO" && python3 dca_weekly_dashboard.py` (skip without failing if it has no usable entrypoint).
   Non-zero EXIT or a traceback on run_dca.py is a FAILURE — go to step 8.
3. Health gates, so the week's code state is on record: `cd "$REPO" && python3 -m pytest -q 2>&1 | tail -5` and `ruff check . 2>&1 | tail -3`. If pytest or ruff is not installed on the Mac's Python 3.10, say so rather than reporting a failure.
4. Pull the real week's fills from Robinhood via `get_equity_orders` (read-only) for Mon-Fri, for the Agentic account. **Per `config/run_classification.json`, scheduled-live metrics are computed only from validated scheduled runs with all manual order IDs excluded.** Manual fills go in a clearly labeled "Manual Test Runs" section and never affect scheduled-live metrics.
5. Compute: total invested this week, week notional vs the weekly cap, cost basis, slippage trend, missing-fill analysis, order counts and reliability per stream, candidates analyzed vs bought, and cost-basis/control comparison. Update the cost tracker (see `docs/cost_tracking.md`).
6. Refresh the **full** Notion performance dashboard (this is the only run that does a full page rebuild). After the dashboard refresh, add one Notion section titled "Goal progress" (Fridays only; analytics only, places no orders):
   a. Confirm the day with `dca_goal_progress.friday_refresh_sections(today)`. If it does not return `["Goal progress"]`, skip the Goal progress section entirely.
   b. Read the latest row per goal from the ledger: `LedgerStore(Path(dca_paths.stream_root("goal_snapshot", "ledger.duckdb"))).goal_progress()` (`dca-sizing-signals`, run with `~/.cache/all-stock-dca/goal-snapshot-venv/bin/python`, the same ledger the 07:31 task writes), keeping for each `goal_id` the row with the latest `day`. If that venv or the ledger is unreadable, say so in the section and do not invent figures.
   c. Build the section text with the module, not by hand: `section = dca_goal_progress.goal_progress_section(rows, today=today)`. Publish `section["title"]` as the heading and `section["text"]` as the body, unchanged.
   d. A goal whose window is under one year shows its period return only, never an annualized figure: the function already enforces that, so do not add, compute or restate an annualized number anywhere in the section. With no rows, the function returns "no goal progress recorded yet"; publish that line as is.
   e. No account number, no position-level dollar values, and no order or trade tool in this section.
7. Post a comprehensive weekly summary, including the Goal progress section when it was published, to Slack #dca-alerts (C0BKS1ZL5UY) and DM Geoff the detailed version with week-over-week comparison, your confidence level, and recommendations.
8. **FAILURE PATH.** Post to Slack: "🚨 Weekly dashboard refresh FAILED — [one-line reason]" and DM Geoff the error text. Never report a refresh you did not actually perform.

This is the heavy run, so full narrative output is appropriate here (unlike the weekday tasks).
