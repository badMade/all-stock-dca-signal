# Evidence for the 10 DCA Issues rows that were waiting on a decision (2026-10-17)

**Status: DRAFT for operator review. Documentation only; no code or config changed.**

These rows were left Open because they touch order placement and caps. This note records what
the code, config and prompts actually guarantee, and what is only a prompt-level contract or
an accepted risk, so each row is closed only as far as the evidence goes.
Keys are the DCA Issues `Key` values so the triage reconciler can match this PR.

Tests cited below pass on main at the time of writing
(`tests/test_dca_runner.py -k "isolates_one_rejection or consecutive_placement_failures or
success_between_failures or halt_vs_isolate or broker_refusal_still_isolates"`,
`scripts/tests/test_stream_candidates.py`, `scripts/tests/test_backup_planner.py`).

## Three kinds of placement failure (do not conflate them)

1. **Broker rejection** (the broker answered; the response has no order id, so nothing was
   accepted): `dca_runner.execute_plan` isolates that ticker, never retries it, and continues;
   it halts after `circuit_breakers.max_consecutive_placement_failures` (3) failures in a row.
   Tests: `test_execute_plan_isolates_one_rejection_and_finishes_the_batch`,
   `test_consecutive_placement_failures_still_halt_the_batch`,
   `test_a_success_between_failures_resets_the_consecutive_failure_streak`.
2. **Ambiguous outcome** (the placement call raised, or a failed response may still hold a live
   order, or an otherwise accepted response leaves orders unaccounted for): `execute_plan`
   stops the batch IMMEDIATELY and books a `potentially_submitted` reservation, so durable
   state never says zero for an order that may exist (`dca_runner.py`, placement loop; test:
   `test_halt_vs_isolate_follows_whether_a_live_order_may_exist`). This is not "continue until
   the third failure".
3. **Platform / permission / session refusal** (the call never reached the broker, for example
   "Blocked by classifier"): this is NOT the ticker-local isolation case. The live prompts say
   it proves nothing about the ticker and everything about the batch, so it STOPS THE BATCH
   (`claude/dca-executor-prompts/live-mirror/1030ET-1030-dca-recurring-buy.txt`, Gate 7: stop
   after one retry of the first refusal; `claude/dca-executor-prompts/perplexity/1050ET-1030-backup.txt`:
   stop immediately and do not count it as an attempt). This contract lives in prompt text; it is
   not enforced or tested by the broker-rejection tests in item 1.

## Correction to an earlier triage note

An earlier note said a denied order "halts the batch" and a later one said it is isolated and
continues. Both were incomplete. Broker rejections isolate, ambiguous outcomes halt at once,
and platform/permission/session refusals halt the batch per the prompts, as above.

## Permission-layer rows

`classifier_block`, `transaction_block`, `real_world_transactions_denied`, `session_refusal`:
the requested "skip the blocked item and continue" is the broker-rejection behavior only. A
platform refusal deliberately stops the batch, and that is a prompt contract, not code. The
cause (the session's permission layer) is outside this repo. Suggested disposition: Monitoring,
with the prompt contract above recorded. Resolve only if the operator accepts that the stop-batch
behavior is the intended answer, or changes the prompt rule.

## Backup-executor rows

`cap_consumed`, `cap_breach`: sequentially covered. `scripts/backup_planner.py` recomputes
headroom from a fresh full-day order pull, so daily and weekly caps are shared with the primary
by construction; it buys nothing with no daily headroom, counts the whole week, and aborts on a
cap breach in the fresh pull. Evidence: `scripts/tests/test_backup_planner.py`.

`duplicate_fills`, `competing_executor`: sequential protections (partial-batch completion,
same-window ticker drop, fresh order pull) do NOT cover two engines running at the same time.
`docs/adr-plan-first-backup-executor.md` ("Known limitations") records that two engines can
still start simultaneously and both see an empty window (accepted risk, coordination doc
section 1), that with no Mac the heartbeat `claim` is skipped, and that broker `ref_id`
deduplication is unverified (D6). Suggested disposition: Monitoring, with the residual
simultaneous-start risk recorded as an accepted operator decision (separate from, and not
dependent on, any decision about a separate backup allocation).

## Data-source rows

`missing_connector`, `provider_error`: there is no fallback data source. `config/scheduled_tasks.json`
`market_data_source_policy.fallback_source` is `null`, and `scripts/stream_candidates.py` states
that Yahoo Finance is removed from every scheduled run (owner direction 2026-10-12); a missing,
failed or incomplete primary aborts with nothing placed
(`test_cli_aborts_when_primary_is_unavailable_without_fallback`; the Yahoo test
`test_cli_rejects_yahoo_finance_fallback_source` verifies rejection, not fallback availability).
The slot overrides make 0935ET, 1030ET, 1130ET, 1230ET, 1330ET and 1430ET Robinhood-primary,
which removes the Finance-connector dependency for those six. The 1530ET final-buy generator
(`scripts/generate_buy_candidates.py`) has its own contract: also no fallback, but it does NOT abort
(corrected 2026-10-17): with Finance down it reconciles off Robinhood alone and a would-be BUY becomes
MANUAL_REVIEW, so a missing Finance connector ends the 1530ET auto-buy with an operator-visible review
list (`test_1530_with_finance_down_yields_no_auto_buy_and_a_manual_review_list`).
Suggested disposition: Monitoring.

## Rows closed on 2026-10-12 on the strength of a yfinance fallback

`market_data_unavailable`, `token_limit_exceeded`, `intraday_bars_unavailable`,
`output_size_limit` were marked Resolved citing a yfinance prefetch fallback. That fallback no
longer exists for scheduled runs (above). Their current protection is the Robinhood-primary
slot overrides plus fail-closed aborts. Suggested disposition: re-open as Monitoring unless the
operator confirms Robinhood-primary alone resolves them.

## Suggested Notion dispositions (operator to apply)

| Key | Suggested |
| --- | --- |
| classifier_block, transaction_block, real_world_transactions_denied, session_refusal | Monitoring (prompt-level stop-batch contract; cause is session-side) |
| cap_consumed, cap_breach | Resolved (sequential protections tested) |
| duplicate_fills, competing_executor | Monitoring; record the simultaneous-start risk as accepted |
| missing_connector, provider_error | Monitoring (no fallback by design; six slots Robinhood-primary) |
| market_data_unavailable, token_limit_exceeded, intraday_bars_unavailable, output_size_limit | Monitoring (fallback removed) |
