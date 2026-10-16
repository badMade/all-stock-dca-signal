# Plan: Precomputed buy recommendations, thin buying agents

## Context

Today every live slot (09:35 … 15:30) runs a 28–48K-token prompt that makes the agent fetch
bars/quotes for ~65 tickers, run the signal step, gate caps, re-quote before every order, and
place dollar-based buys. That is token-heavy, fragile (prices/fractional values flowing
through the model have caused failures), and gives no live intraday data between slots.

**Goal:** a program on the Mac computes the optimal buys every 5 minutes, using the same rules
and math that already exist, and writes a small file. At slot time the buying agent (Claude
primary; Perplexity backup) reads only `{ticker, usd, ref_id}` from the newest fresh file,
places the buys, and saves the raw broker responses. The program keeps the price it expected
(never shown to the agent) and later compares it to the actual fill.

**Decisions locked with the user (2026-10-13):**
- Data: **a tiny Claude fetch task every 5 min** (Rev 5; headless Robinhood proved impossible
  in Rock 0). **No yfinance** anywhere in this path.
- Agent role: read file → one order-history call (dedupe / sole-executor) → place → save raw
  responses. No quote or price fetching by the agent.
- Freshness: newest file with `as_of ≤ now`, same trading day, age ≤ **15 min**
  (`config/recommendations.json`, adjustable). 12:07 uses 12:00; nothing fresh → buy nothing, alert.
- Publish: local file **and** a Notion page **and** a GitHub orphan data branch, so Perplexity
  (which can't see the Mac) can read it.
- Workflow: rocket-fuel (Claude = Visionary, Codex = Integrator). New rocket-fuel files go
  **beside** the hosted-console ones, prefixed `-plan-first` (e.g. `PLAN-plan-first.md`).

## Starting point

- Branch off **`origin/main`**, not this branch: this checkout is 10 commits behind, and main
  already has the broker-free "market data → sized plan" path this builds on:
  `scripts/backup_planner.py compute --market-data` → `dca_order_plan.plan_from_decisions`,
  `scripts/assemble_market_data.py`, `dca_ladder_core.py`, `trailing_deployed_usd`.
- Reuse, don't rewrite:
  - Signal step: `scripts/stream_candidates.build_document(..., decision_time_et=)` (6 slots),
    `scripts/generate_buy_candidates.generate_candidates` (15:30), `scripts/backup_stream_specs.py`.
  - Sizing/caps: `dca_order_plan.plan_from_decisions`, `dca_runner.budget_snapshot`,
    `scripts/same_day_top_up_gate.py`, `near_close_dca.graded_buy_multiplier` / `select_first_allocation_base_usd`.
  - Normalizing raw MCP JSON: `scripts/build_robinhood_market_data.build_payload`, `scripts/ingest_market_data.py`.
  - ref_id: `scripts/validate_native_placement.native_ref_id` (UUIDv5); post-placement check `validate_native_placement check`.
  - Fill price: `scripts/broker_reconciliation._fill_price`; slippage math: `dca_reporting.compute_slippage_rows`.
  - IO/scheduling: `dca_fileio.atomic_write_text`, `dca_paths.stream_root`, `scripts/scheduler_lease.py`,
    `deploy/launchd/prefetch_slot.sh` + `prefetch_plan.py` (trading-window/holiday guard pattern).

### Facts about the existing compute path (verified on `origin/main` cfda4e2f)

`scripts/backup_planner._cmd_compute_gated` (line 321) is the market-data → plan path that
`RecommendationEngine` wraps:

- **Seven slots, one route each.**
  - `GATED_SLOTS` covers all seven slots.
  - Slot 1530 goes through `_decisions_final_buy`, which calls `gbc.generate_candidates` at
    the **fixed** 15:30 decision time.
  - The other six go through `_decisions_stream`, which calls
    `stream_candidates._run_with_args` with `decision_time_et=None` (the slot's own anchor).
- **Normal tier only.** The tier is always `_NORMAL_TIER`; it never escalates.
- **Repeat buys are skipped, not topped up.** `already_placed_symbols(..., scope="day")`
  removes any ticker already bought today, because a computed plan cannot resize a top-up.
- **Kill-switch input.** `deployed_last_7d_usd` comes from broker history through
  `trailing_deployed_usd`.

**Rev 2 (Same Page round 1).** The backup planner's rules differ from the primary's in
three ways: it always uses the Normal tier, it skips tickers already bought today, and its
caps are all-or-nothing (D4). Because the VTO requires **same rules**,
`RecommendationEngine` does **not** wrap `_cmd_compute_gated`. It chains the
**primary's** own Gate 5, Gate 6 and Gate 6.5 functions, in the order the live executor
prompts run them:

1. **Gate 5 (signal).** The six stream slots use `stream_candidates.build_document(spec, ...,
   decision_time_et=T)` with `backup_stream_specs.stream_spec(slot)`; `build_document`
   already moves `executor.decision_time` (line 1092). Slot 1530 uses
   `generate_buy_candidates.generate_candidates(slot="1530ET", now=T, ...)`, with the
   15:30 row→decision mapping that `backup_planner._decisions_final_buy` already does.
   `already_covered_tickers` is left **empty**, because Gate 6.5 owns repeat buys.
2. **Gate 6 (budget).**
   - `dca_runner.budget_snapshot(orders, day)` builds the snapshot.
   - `dca_runner._tier_selection_inputs` plus `near_close_dca.select_budget_tier` choose the
     tier, which escalates as on the primary.
   - `dca_runner.plan_orders(cands, snapshot, tier, weekly_cap_usd=, buying_power_usd=)`
     applies the caps. It keeps orders up to the first cap boundary, which is the
     primary's rule; it is not the backup planner's all-or-nothing rule.
   - The Stress tier's `weekly_rollover_reserve_usd` comes from the same saved state
     `dca_runner.assess` reads. If that state is unavailable, the tier caps at High
     Volatility and the cap reason is logged.
3. **Gate 6.5 (top-up).** `scripts/same_day_top_up_gate.evaluate_slot(pages=, candidates=,
   account_number=, trading_day=)` gives each ticker its `buy_usd`, which may be the full
   amount, a top-up or $0. An `ABORT` verdict means the program writes **no**
   recommendation for that cycle.
4. **Plan document.** `dca_order_plan.plan_from_survivors(rows, generator "claude")` builds
   it, the same writer `emit_order_plan` uses today.

**Rev 5 (Rock 0 result + user decision, 2026-10-13): the data source is a Claude fetch every 5 minutes.**

**Rock 0 result.** Robinhood's OAuth at `agent.robinhood.com/mcp/trading` accepts the
dynamic registration and opens its authorize page. For a self-registered client, though,
that page only lists the agents already approved (Claude among them) and never redirects
back. Only known agent apps can obtain tokens, so a headless Python client is **not
possible**.

**Decision (operator).** A **tiny Claude scheduled task runs every 5 minutes** in market
hours. It is a data courier only and computes nothing. It makes three calls:

1. `python -m scripts.snapshot_cycle fetch-plan`, which prints the exact tool calls and the
   output file for each.
2. Those calls, in order: `get_equity_quotes`; `get_equity_historicals` for the newest
   bars only; the prior-session daily bars, once per day; `get_equity_orders` since
   Monday; and `get_portfolio`.
3. `python -m scripts.snapshot_cycle ingest`, which validates the files, merges them into
   the day's local history, computes and writes the recommendation, and publishes it.

The orders and portfolio fetches let Gate 6.5 and the caps see broker state.

**Consequences for the build:**

- **Removed from this cycle:** `dca_mcp_client.py`, `scripts/rh_headless_probe.py`,
  `HeadlessRobinhoodSource` and `HeadlessBrokerStateSource`. They are logged in
  `DEFERRED-plan-first.md` and can come back if Robinhood opens client registration.
  Keeping unused token-handling code in the repo is a liability.
- **`AgentFetchSource`** reads the courier's raw files. **`AgentBrokerStateSource`**
  assembles the saved order pages through `assemble_order_pages.assemble` and takes
  buying power from the saved portfolio response.
- **New `MarketHistoryStore`** keeps the day's raw responses under
  `<cron root>/recommendations/<day>/raw/`. It merges each run's newest bars into the
  day, de-duplicated by symbol and bar start. That way every courier run fetches only the
  last few bars, and `assemble` runs on the merged day.
- **No launchd job.** `ingest` runs the computation as soon as the courier has saved
  data. If the courier misses runs, the recommendation file goes stale, and the 15-minute
  freshness rule blocks buying.
- **ChatGPT is now connected to Robinhood** (operator, 2026-10-13), so ChatGPT or Codex is a
  viable alternative courier or backup reader if Claude's token cost proves too high.
- **Token budget.** Each run is roughly a 1-2K-token prompt plus about 15-30K tokens of
  data, over about 78 runs a day. It replaces the 7 heavy slot prompts' own fetching.
  Measure it in shadow mode.

**Rev 4 (Rock 2 build finding, 2026-10-13): the budget tier depends on the slot.**
Rev 2 sent every slot through `dca_runner.assess`, and `assess` escalates the tier
dynamically. The live prompts and the stream configs say otherwise:

- **Six slots stay on Normal.** The 09:35, 10:30, 11:30, 12:30, 13:30 and 14:30 slots use
  the Normal tier ($180/$900) **unconditionally**: "Do NOT run config/budgets.json's dynamic
  tier selection here". `config/morning_dca.json` and `config/midday_dca.json` say the same
  in `budget_ledger`, and `market_open_dca` caps itself flat at 180/900.
- **Only 15:30 escalates.** It escalates by the count of 3x-or-greater signals alone, which
  is exactly what `near_close_dca.select_budget_tier` does.
- **`assess` is not wired to the live prompts.** Its own docstring says so. Following it on
  the six stream slots would authorize up to $360/day where the live slots allow $180.

**The rule from here:**

- For the six stream slots, the engine runs `dca_runner.plan_orders(...)` directly with the
  **Normal** tier and the same snapshot. Buying power that cannot be read means ABORT,
  matching `assess`. Gates 0, 1, 3 and 4 are left to the agent.
- For 15:30, the engine uses `assess` as described in Rev 2.
- A test pins the difference: on a fixture with 20 signals at 3x or above, every stream
  slot stays Normal and 15:30 escalates.

**Rev 3 (Same Page round 2).**

- **15:30 moved time.** `generate_buy_candidates.generate_candidates` pins
  `decision_time_et` to `SLOT_SPECS["1530ET"]` (line ~1200). Rock 2 adds one keyword-only
  parameter, `decision_time_et: time | None = None`. `None` keeps today's behaviour
  byte-for-byte, and a regression test pins the default output. A value replaces the spec
  time exactly the way `stream_candidates.build_document` moves `executor.decision_time`.
  This is the only change to an existing module's behaviour in this cycle, and it
  defaults off.
- **7-day deployment input (kill switch).** The figure is supplied only where the route
  takes it. That means the specs with `requires_deployed_last_7d_usd` (12:30) and the
  15:30 generator; `build_document` raises if a non-requiring spec is given one. The figure
  comes from the **same source the primary uses**: the Mac's scheduled run-state artifacts
  through `dca_risk.deployed_by_day(...)` plus `dca_risk.seven_day_deployed(...)`, the
  risk-report path, read from `cron_tracking_root()`. If
  `dca_risk.has_unusable_deployed_notional` flags those artifacts, the engine falls back to
  `dca_order_plan.trailing_deployed_usd` over broker history. That figure is an upper bound
  that can only trip the 1x cap earlier, never later. The engine records which source it
  used in the program-only expected file.

The engine adds no new math; every number comes from an existing function. The only new
inputs are the time `T` and the 7-day deployment figure, both described above. If `dca_runner`'s Gate 6 code and the live prompts' Gate 6 prose
disagree, the code is the reference. The parity test pins the code, and any disagreement
found is logged in `DEFERRED-plan-first.md`.

**ref_id and placement validation (Rev 2).** `scripts/validate_native_placement.py`
(`native_ref_id`, `check`) exists only in open PR #1660 (`fix/native-placement-identity-check`,
6 commits ahead of main). This branch **merges that branch first**, as Rock 0a, and
re-merges it if the PR changes. Until #1660 lands on main, this branch's PR is stacked on
it.

The agent file carries `ref_id = native_ref_id(slot, day, ticker)`, a UUIDv5. The plan
document's own `dca-runner-…` ref_id stays as it is: `validate_plan` checks it. The agent
never sees that string.

## Design (object-oriented; only data crosses boundaries)

```
launchd every 5 min (market hours)
  SnapshotCycle.run_once(now)
    MarketSource (HeadlessRobinhoodSource | AgentFetchSource) -> MarketSnapshot
    BrokerStateSource (same headless client: orders, buying power) -> BrokerState
    RecommendationEngine.compute(snapshot, broker_state, slot_for(now)) -> (RecommendationSet, ExpectedPrices)
    RecommendationStore.write(...)        # agent file + private expected-price file + latest pointer
    Publishers: NotionPublisher, GitHubBranchPublisher (agent payload only)
Slot agent: `read_recommendation --slot S` -> [{ticker, usd, ref_id}] -> place -> save raw responses
FillComparator (next cycle / EOD): expected vs actual fills -> fill_comparison_<day>.json
```

New top-level modules (each added to `pyproject.toml` `py-modules`; new config to `data-files`):

| Module | Classes / role |
|---|---|
| `dca_rec_models.py` | Frozen dataclasses: `MarketSnapshot`, `BrokerState`, `Recommendation(ticker, usd, ref_id)`, `RecommendationSet(trading_day, as_of_et, slot, source, ladder_version, universe_hash, items)`, `ExpectedPrice(ticker, decision_price, prior_close, prior_low, multiplier, drop_pct)`, `FillComparison`. `to_json`/`from_json` with strict validation (fail closed on unknown keys, non-object JSON). |
| `dca_mcp_client.py` | Minimal stdlib MCP client (JSON-RPC over streamable HTTP via `urllib`). **Hard allowlist of read-only tools** (`get_equity_quotes`, `get_equity_historicals`, `get_equity_orders`, `get_equity_tradability`, portfolio/buying-power read). Any other tool name raises before a request is built — test-enforced, so the program can never place an order. Token read at call time from macOS Keychain via `security find-generic-password`; never logged, never in repo. |
| `dca_market_sources.py` | `MarketSource` protocol. `HeadlessRobinhoodSource` (25-ticker chunks, as `RobinhoodDataProvider` does) and `AgentFetchSource` (reads raw JSON saved by the tiny Claude fetch task). Both normalize through `build_payload` → identical `MarketSnapshot`. Whole-universe source switch, labeled `source` in output (matches existing policy: never mix sources). |
| `dca_recommender.py` | `RecommendationEngine`: `slot_for(now)` (slot whose placement window is current/next, from `dca_order_plan.placement_window`), then the primary Gate 5 → 6 → 6.5 chain at `T=now` (see "Rev 2" above), then `plan_from_survivors`, then `native_ref_id` per order. Returns agent set + expected prices separately. No new math. |
| `dca_rec_store.py` | `RecommendationStore`: writes `<cron root>/recommendations/<day>/rec_<HHMM>.json` (agent payload), `expected_<HHMM>.json` (program-only), `latest.json`. Kept out of slot artifact dirs (verify_schedule reads those). `select_for(now, max_age)` implements the freshness rule. |
| `dca_rec_publish.py` | `Publisher` protocol; `NotionPublisher` (overwrites one page block via Notion API; token in Keychain) and `GitHubBranchPublisher` (git plumbing force-push of one JSON to orphan branch `recommendations`; CI ignores it). Agent payload only; publish failure never blocks the local write. |
| `dca_fill_compare.py` | `FillComparator`: joins `expected_*.json` with saved placement responses + later `get_equity_orders` fill data → per-order expected vs fill price, shares, slippage %. |

Scripts / deploy:
- `scripts/snapshot_cycle.py` — `run-once [--now] [--demo]`, `status` (FRESH/STALE + age), `fetch-list` and `ingest` (for the Claude fallback). Uses `scheduler_lease` so cycles never overlap.
- `scripts/read_recommendation.py --slot S [--now]` — prints only `[{ticker, usd, ref_id}]` or `NONE <reason>`; exit codes for each.
- `deploy/launchd/com.all-stock-dca.snapshot.plist.template` — `StartInterval 300`, script exits immediately outside the trading window (reuse `prefetch_plan` guard).
- `config/recommendations.json` (+ schema in `schemas/`): `max_age_minutes: 15`, cadence, publishers on/off, Notion page id (non-secret), branch name.

Agent prompts (drafts only; live change requires the operator to repaste — not done by this plan):
- `claude/dca-executor-prompts/drafts/<slot>.plan-reader.draft.txt` — target ≤ 2K tokens: Gate 0/1 clock, `read_recommendation`, one paginated `get_equity_orders` (sole-executor + skip already-bought), place each `{ticker, usd, ref_id}` as a dollar-based market buy, save raw response, `validate_native_placement check`, Gate 9 `finalize_run_evidence`.
- `claude/dca-executor-prompts/drafts/fetch-fallback.draft.txt` — tiny Claude task at slot−10 min (7/day): run `snapshot_cycle status`; if FRESH stop; else call the quote/historical tools for `fetch-list`, save raw JSON, run `ingest`.
- Perplexity drafts: same reader logic against the Notion page / data branch.

## Rocks (rocket-fuel build units)

| # | Rock | Proof command (Claude re-runs) |
|---|---|---|
| 0 | Headless Robinhood feasibility spike: `dca_mcp_client` + probe CLI; allowlist | `pytest tests/test_dca_mcp_client.py -q` + `python -m scripts.rh_headless_probe --symbols SPY` prints one quote (user provisions the token in Keychain; I never handle it). If Robinhood refuses non-approved clients, stop and report — fallback becomes the Claude fetch task at 5-min cadence (token cost surfaced to user). |
| 1 | Models + `MarketSource` + `AgentFetchSource` | `pytest tests/test_dca_rec_models.py tests/test_dca_market_sources.py -q` |
| 2 | `RecommendationEngine` with parity | `pytest tests/test_dca_recommender.py -q`. **(a) Parity:** for each of the 7 slots, with `T` = that slot's own anchor, the engine's `{ticker: buy_usd}` == the primary chain (Gate 5 generator → `_tier_selection_inputs`/`select_budget_tier`/`plan_orders` → `evaluate_slot`) run by hand on the same fixture market data and order pages. Fixtures cover: a tier escalation, a same-day top-up, a cap boundary, and a top-up `ABORT`. **(b) Moved time:** with `T` ≠ anchor, the engine == the same chain with the decision time moved, and bars/quotes stamped after `T` are ignored. This is a consistency property, not parity with today's live output. |
| 3 | Store + freshness selector + `read_recommendation` | `pytest tests/test_dca_rec_store.py -q` — 12:07→12:00 accepted; 12:16 with only 12:00 rejected; future-stamped and prior-day files ignored; agent file contains no price fields |
| 4 | `snapshot_cycle` + launchd template + publishers | `python -m scripts.snapshot_cycle run-once --demo --now 2026-10-13T12:05` writes files; publisher tests with fake transports |
| 5 | `FillComparator` | `pytest tests/test_dca_fill_compare.py -q` |
| 6 | Slim prompt drafts + docs (`docs/plan_first_recommendations.md`, CLAUDE.md index, repo_map) | token-count check ≤ 2K per reader draft; `scripts/check_live_prompt_drift` unaffected |

Every Rock's proof also includes `ruff check .` and the full `pytest` suite passing.

## Rocket-fuel execution order

1. Branch `rocket-fuel/plan-first-recommendations` from `origin/main`. Rock 0a: copy
   **only** `scripts/validate_native_placement.py` and its test from PR #1660's branch.
   The full merge conflicts in the 09:35 live-mirror prompt: main says "NO FALLBACK DATA
   SOURCE" and the PR says "PREFETCH-FIRST yfinance". That conflict belongs to PR #1660 and
   is logged in `DEFERRED-plan-first.md`. Proof:
   `pytest scripts/tests/test_validate_native_placement.py -q` passes (23 tests).
2. Write `VTO-plan-first.md` + `PLAN-plan-first.md` (this plan, expanded).
3. Same Page Meeting with Codex (read-only, ≤5 rounds, log to `SAME-PAGE-LOG-plan-first.md`, deferrals to `DEFERRED-plan-first.md`). Re-verify Codex 0.160.0 flags first (memory notes for 0.145 say: `-m gpt-5.6-sol`, no `--ask-for-approval`, resume uses `-c sandbox_mode=...`). Deadlock → user tie-break.
4. Build Rocks 0–6. Codex builds where the permission classifier allows `workspace-write`; otherwise I build. Network/Keychain/launchd proofs run by me (Codex sandbox can't bind sockets).
5. Present diffs + my own proof output → user commit approval. PR opened as **draft**.

## Rollout (after merge, operator-driven)

Shadow mode ≥ 5 trading days: snapshot loop runs, files written and published, live slots
unchanged; daily diff of "file said" vs "live slot bought". Then the operator repastes the
plan-reader prompts for Claude slots, then Perplexity.

## Deferred / out of scope

- Deeper OO refactor of `near_close_dca` / executor classes / consolidating the three entry points (a separate decision per CLAUDE.md).
- Changing strategy rules, ladder, caps, or tickers.
- Console page for fill comparison and the 5-min live data (follow-up).
- Pre-existing ref_id inconsistencies (`dca_runner.execute_plan:6323`, `dca_order_plan._expected_ref_id`) — noted; fixed only if Rock 2 parity requires it.

## Risks to watch

- Headless Robinhood access may be impossible or against terms → Rock 0 is first and gates the rest of the data design.
- Computing at `decision_time=now` instead of the fixed slot time: values move by up to 15 min vs today; shadow mode measures it.
- Caps between cycles: the program sees broker orders only as of its last cycle; the agent's single order-history read skips already-bought tickers, and all-or-nothing caps in `plan_from_decisions` keep a stale file from overspending beyond one slot's plan.

## Verification (end-to-end)

1. `ruff check .` and `.venv/bin/python -m pytest` (full suite) + `pytest scripts/tests -q`.
2. `python run_dca.py --demo --report --now 2026-08-03T15:45 --artifacts-dir /tmp/dca-demo` still passes.
3. `python -m scripts.snapshot_cycle run-once --demo --now <slot-5min>` then `python -m scripts.read_recommendation --slot <slot> --now <slot+7min>` prints ticker/usd/ref_id only; `--now <slot+20min>` prints `NONE stale`.
4. Live (Rock 0 onward, with user's token): `rh_headless_probe`, then one market-hours `run-once` against real data — read-only, places nothing.
5. Fill comparison against a fixture day of real placement responses.

## Build status (2026-10-13)

| Rock | Built by | Result |
|---|---|---|
| 0a | Claude | Copied `scripts/validate_native_placement.py` and its test from PR #1660, not the whole branch: the full merge conflicts with main's 09:35 prompt (see DEFERRED) |
| 0 | Codex, 2 fix rounds, then Claude | Built and tested. The live test showed Robinhood will not finish OAuth for a self-registered client, so the code was **removed** (Rev 5) |
| 1 | Codex, 1 fix round | `dca_rec_models`, `dca_market_sources` (`AgentFetchSource`) |
| 1b | Codex | `dca_market_history` (courier fetch plan, merged day bars, order pages, buying power); headless code removed; `slots_for` and per-slot compute |
| 2 | Codex, 1 fix round, then Claude | `dca_recommender`: the primary Gate 5 → 6 → 6.5 → 6.7 chain. Rev 4 tier split. Real-chain parity on all 7 slots; Claude added the non-empty parity assertion and the 15:30 escalation check |
| 3 | Claude | `dca_rec_store` (freshness rule, slot-keyed files, handed record), `scripts/read_recommendation.py`, `config/recommendations.json` and its schema |
| 4 | Codex | `scripts/snapshot_cycle.py` (fetch-plan, ingest, status), the Gate 9 budget record, packaging |
| 5 | Claude | `dca_fill_compare` (expected vs fill, joined by broker order id) |
| — | Claude | `dca_rec_publish` (Notion code block, orphan GitHub data branch); both off by default |
| 6 | Codex, then Claude | `scripts/plan_first_slot.py` (begin, filter, finish), reader and courier prompt drafts (about 585 and 280 tokens), `docs/plan_first_recommendations.md`. Claude made the drafts' repo-location step self-contained |
