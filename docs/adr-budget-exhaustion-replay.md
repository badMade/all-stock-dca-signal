# ADR: Budget-exhaustion count from live Gate 5 documents (gate for the Markov category-1 ideas)

Status: accepted, revision 3. Section 8 answered 2026-11-23 (operator); tests approved; implemented as
`python -m scripts.budget_exhaustion_count [--root <cron-tracking root>] [--json] [--slots]`
(tests: `scripts/tests/test_budget_exhaustion_count.py`, real scrubbed fixtures beside it).
Decided 2026-11-23 (operator): report location is a CLI only, no console page for now.
Date: 2026-11-23
Related: `claude/markov-extensions.md`, `HANDOFF_budget_exhaustion_opportunity.md`, draft PR #2403.
Supersedes: revision 2 of this ADR (replay live sizing from config history and the DuckDB ledger).

## 1) Context

Before building the MDP cash-pacing suggester or the absorbing tier chain we need one number: how
often a scheduled slot had a buy the live system sized, but the day's or week's remaining budget
could not fund it.

Revision 2 proposed replaying live sizing from config versions in git history. That was heavy
(config effective time, per-ticker ladder history, top-up artifacts, kill switch) and still an
approximation. A read-only survey of the live artifact tree on 2026-11-23 found a better source:
every slot already saved its own decision-time sizing.

## 2) What the live tree holds (surveyed 2026-11-23, read-only)

Per scheduled slot directory (`<stream>/<YYYYMMDD>_<HHMM>ET/`, and `ce16ba6b/scheduled_runs/` for 3:30):

| File | What it is | Coverage |
|---|---|---|
| `candidates_<day>_<slot>.json` | Gate 5 generator output. `cands[]` rows carry `ticker`, `desired_usd`, `multiplier`, `selected_base_amount_usd`, `drop_pct`, `prior_close`, `prior_low`, `sizing_min/max_drop_pct`. Document carries `trading_day`, `runner_slot_id`, `kill_switch_engaged`, `deployed_last_7d_usd`, `same_day_top_up_candidates` | market_open 34, morning 28, intraday 88, midday 1, 3:30 16 (see below) |
| `orders_pages_week_<Monday>_<slot>.json` | The ISO-week broker order history this slot used for Gate 6 | 20 intraday, 12 market_open, 7 morning, 9 at 3:30, 1 midday (of the slots with a candidates doc) |
| `orders_pages_<day>_<slot>.json` | Same-day history (often a merged increment) | wider than the week file, see section 6 |
| `budget_authorized_cands_<day>_<slot>.json` | What survived Gate 6 | market_open only, 14 files |
| `gate9_report.json` | `eligible_count` and `dropped.{price_recovered, spread_over_1pct, daily_cap}` | 89 of 167 slots with a candidates doc |
| `finalize_budget.json`, `run_summary.json` | Tier and caps used; final status (`ORDERS_PLACED`, `NO_ORDERS_WEEKLY_CAP_EXHAUSTED`, `ABORT_GATE6_HISTORY_INCOMPLETE`, ...) | most slots |

Shape differences that the reader must handle:

- **3:30 PM** does write a Gate 5 document (16 of them, 2026-10-21 onward), but a different shape: a
  `candidates[]` list with `action`, `buy_multiplier`, `suggested_amount_usd`, no `cands` / `desired_usd`.
  Three files are oddly named (`candidates_raw.json`, `candidates_1530.json`). The kickoff note that
  3:30 has no candidates file was wrong.
- **Midday** (12:30) mostly saves only `cands_<day>_1230.json` (post-Gate-6 survivors, 13 slots) and a
  `gen_out.json` in one slot. The full Gate 5 document exists for 1 of 37 midday slots.

Facts that change the design:

1. **`dropped.daily_cap` already exists but folds three causes.** The live prompts define it as
   "tickers zeroed or dropped because a daily, weekly or same-day cap left no room at Gate 6 or 6.5,
   including the same-day top-up guard zeroing a ticker already covered today". Summed over 202 Gate 9
   reports it is 1,817 ticker-drops. It cannot separate budget exhaustion from top-up zeroing, so it
   is a cross-check, not the answer. (The earlier grep for `budget_blocked` / `cap_exceeded` found
   nothing because the key is named `daily_cap`.)
2. **The weekly cap is what binds.** market_open 2026-11-20 09:35: 60 candidates, `dropped.daily_cap`
   54, but only $8.11 spent today. The week envelope shows $857.29 of $900 spent by Thursday morning.
   Those 54 were weekly-cap drops. (The kickoff said this slot placed nothing; it placed 2 orders,
   AAAU $8.11 and ANET $7.82, both filled.)
3. **The saved envelopes are post-placement snapshots.** That slot's week file was snapshotted at
   09:47:05, after its own AAAU order. Pre-slot spend must exclude the slot's own orders.
4. **Gate 6 allocation is live code.** `dca_runner.plan_orders(candidates, snapshot, tier, ...)` sorts
   in allocation-priority order, stops the batch at the first cap boundary (no back-fill), and records
   each skip as `daily_cap_reached`, `weekly_cap_reached`, `insufficient_buying_power` or
   `daily_order_count_cap_reached`. `dca_runner.budget_snapshot(orders, day)` turns broker rows into
   day and week notional. `scripts/primary_gates.py budget` is the slot's own scripted Gate 6 on top
   of both. So daily vs weekly attribution comes from live code for free.

## 3) Decision

A read-only CLI, `scripts/budget_exhaustion_count.py`, stdlib plus repo modules, that counts from the
slots' own documents instead of replaying sizing.

For each scheduled slot directory with a Gate 5 document:

1. **Candidates.** Read the Gate 5 document. Normalize 3:30's `candidates[]` (`action == "BUY"`,
   `suggested_amount_usd`) and the other streams' `cands[]` (`desired_usd`) to one row shape. Sizing is
   whatever live code wrote; nothing is recomputed.
2. **Pre-slot spend.** Load the slot's `orders_pages_week_*` envelope with the same row parser Gate 6
   uses (`assemble_order_pages._order_rows`, `backup_planner._to_broker_order`), drop the slot's own
   orders (see Q3), and call `dca_runner.budget_snapshot`.
3. **Caps.** The tier and caps the slot recorded in `finalize_budget.json`. Fallback per Q5.
4. **Allocation.** plan_orders' own allocation order (`dca_runner._allocation_priority_key`) and its
   stop-at-first-boundary rule with its `SKIPPED_*` reasons. Every cap skip is a missed buy: ticker,
   the live-sized dollars, and which cap.
   Finding (2026-11-23): calling `plan_orders` itself is not usable on history, because its
   per-candidate refusals read TODAY's `ACTIVE_TICKERS`, dated no-buy windows and calibration config.
   On the real 11-20 3:30 document it refused all 82 candidates (`inconsistent_multiplier_notional`;
   3:30 rows carry no per-ticker bounds). On 11-20 09:35, even with the slot's own bounds, it refused
   VCX for a no-buy window although live bought VCX that morning. So the CLI's `allocate()` keeps the
   priority function and the boundary loop and drops the refusals; a parity test pins it equal to
   `plan_orders` on every real candidate `plan_orders` accepts, at weekly, daily, order-count and
   non-binding snapshots.
5. **Cross-check.** Compare the counted cap skips with `gate9_report.dropped.daily_cap`, and with
   `budget_authorized_cands` where it exists. Report disagreements; do not reconcile them.

Separate buckets, never merged into the missed-buy count:

- **Gate 6 aborts.** Slots whose status says they aborted at or before Gate 6 with candidates in hand
  (`ABORT_GATE6_*`, `ABORT_GATE6_HISTORY_INCOMPLETE`, `..._NOT_PERSISTABLE`, output_cap failures).
  Reported as slots and as the sum of their `desired_usd`, under "aborted, not budget".
- **Unresolved.** A slot whose inputs cannot be proven point in time (no week envelope, no caps, an
  unparseable document) is counted as unresolved with a reason, never guessed and never dropped.

Report: per slot, per stream, per cap (daily / weekly / buying power / order count), per reason, with
sessions observed, sessions with any missed buy, missed dollars, and the unresolved share.

Parity: sizing is read from what live code wrote; spend is `budget_snapshot` and the priority is
`_allocation_priority_key`, both called (a spy test pins it); the boundary loop is pinned equal to
`plan_orders` by the parity test in step 4.

## 4) Non-negotiable constraints

- Read-only. Never writes into `cron_tracking`, state, config or the ledger. Never imports a broker adapter.
- Point in time: every input is the slot's own decision-time file. Nothing from a later slot is used,
  except the slot's own post-placement envelope minus its own orders (Q3).
- Fail closed: unresolvable slots are reported as unresolved with a reason.
- Manual runs (`manual_*` directories, `run_origin != "scheduled"`) never count (docs/run_classification.md).
- The account number in the envelopes is never printed or copied. Test fixtures use the synthetic
  `999000111` from `conftest.py`.

## 5) Evaluation

If missed sessions are rare, the MDP and the absorbing chain drop down the roadmap
(`claude/markov-extensions.md`). If they are common but almost all weekly-cap drops late in the week,
that is a pacing question across days, not within a day, and item 1 (MDP) would need a weekly horizon.

## 6) Known limits

- **Coverage is thin for the week envelope.** Of 167 slots with a Gate 5 document, about 49 kept an
  `orders_pages_week_*` file (they became standard around 2026-11-05, after the week-baseline reuse
  rule). Earlier slots fall to unresolved unless Q4 allows the day envelope plus earlier slots.
- **Midday is almost absent** (1 full Gate 5 document of 37 slots).
- **`desired_usd` is the pre-top-up amount.** Gate 6.5 can shrink a same-day repeat to its incremental
  delta or zero it. A cap skip on a ticker already bought today overstates missed dollars (Q6).
- **Unplaced is not only budget.** Price recovery, spread and platform refusals happen after Gate 6;
  this count stops at Gate 6, so a budget-funded candidate that later failed is not a "missed buy" here.
- **Buying power** is not saved per slot as a number in a fixed place (Q5).
- Coverage begins 2026-09-03. Count sessions before trusting a ratio.

## 7) Alternatives considered

- Revision 2, full replay from config history and the DuckDB ledger: rejected. Heavy, still
  approximate, and the slot documents already hold the live-sized answer.
- Sum `gate9_report.dropped.daily_cap`: rejected as the answer; it merges daily, weekly and top-up
  zeroing. Kept as the cross-check.
- Global ladder estimate (`dca-sizing-signals/.../opportunity.py`): stays as the coarse upper-bound
  estimate; not the gate number.

## 8) Open questions (answered 2026-11-23, operator)

Accepted as recommended: 1, 2, 3, 5, 6, 7. Changed: 4. The headline keeps slots without a week
envelope as unresolved, and the same run also computes the derived figure (rebuilt from the earliest
same-week envelope, from any stream, snapshotted at or after the slot's trigger, rows filtered to
before the trigger), reported separately and labeled "derived".

1. **3:30 PM coverage.** Include the 16 3:30 documents via the `candidates[]` / `suggested_amount_usd`
   shape? Its tier is dynamic (Normal to Stress); use the tier in that slot's `finalize_budget.json`.
   Recommendation: include.
2. **Gate 6 aborts.** Report as a separate "aborted, not budget" bucket (slots and desired dollars),
   never in the missed count? Recommendation: yes.
3. **Excluding the slot's own orders from its envelope.** Options: (a) drop rows created at or after
   the slot's `triggered_at_utc`, (b) drop rows whose order ids appear in the slot's
   `finalize_orders.json` / `submitted_orders.csv`, (c) both, and flag any disagreement.
   Recommendation: (c).
4. **Slots without a week envelope** (about 118 of 167). Options: (a) unresolved, (b) rebuild pre-slot
   spend from the latest earlier slot's week envelope plus the same-day envelope, flagged as derived.
   Recommendation: (a) for the first run, so the headline number uses only direct evidence; add (b) only
   if coverage is too thin to decide.
5. **Caps and buying power.** Use `finalize_budget.json` caps; when it is missing, fall back to Normal
   ($180/$900) for non-3:30 slots (the prompts pin Normal) and unresolved for 3:30. Ignore buying power
   (pass none to `plan_orders`) and say so in the report? Recommendation: yes to both.
6. **Same-day repeats.** When a skipped ticker was already bought earlier that day, count its dollars at
   `desired_usd` with a `same_day_repeat` flag, or move it to its own bucket? Recommendation: own
   bucket, so the headline is first-allocation buys only.
7. **Daily vs weekly attribution.** Take `plan_orders`' own first-boundary reason (it stops at the
   first cap hit, so later skips inherit it). Also report "would the daily cap alone have bound?" by
   a second `plan_orders` call with the weekly cap lifted? Recommendation: yes, it shows whether the
   week, not the day, is the real constraint.

## 9) Provenance (2026-11-24)

The implementation reached `main` without a pull request. The worktree branch was created tracking
`origin/main`, and the IDE's source-control sync committed and pushed the work in progress on
2026-11-23 as five commits, `2c6a5586`, `c1631128`, `e6386934`, `7d48458d` and `e9083c3c`. Their
messages were generated by the IDE and do not describe the diffs exactly: two are labeled
"refactor" for new code, and `e6386934` and `7d48458d` share one message. The content is the
reviewed work: fixtures, tests, this ADR, `scripts/budget_exhaustion_count.py` and its
registration. The operator chose to keep the commits on `main` rather than revert them, and this
note was added in a follow-up draft PR so the change has a reviewable record.

Local CI on `e9083c3c` (`scripts/local_ci.sh --changed --keep-going`) passed: ruff, the coverage
gate, `scripts/tests` and the dry-run demo smoke test. Originating chat:
https://claude.ai/code/session_01KWTqCeCtG9Mrrt4fcrA5ZK
