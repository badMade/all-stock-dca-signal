# Group C decision document (2026-10-17)

Docs only. No code, config or live-prompt change until the operator picks options in chat.
Keys covered: `real_world_transactions_denied`, `session_refusal`, `missing_connector`, `provider_error`.
(`classifier_block` / `transaction_block` stay Monitoring per the operator; `hand_reimplemented` is handled elsewhere.)

## What the code and prompts actually do today (verified)

1. **Platform refusal is already specified in the 1530ET live-mirror prompt** (Gate 7, "A PLATFORM REFUSAL IS NOT A BROKER REJECTION"): it stops the batch, is not a gate/safety failure, consumes no placement attempt, and the alert must say so. Nothing in the repo (Python, `config/`, `scripts/finalize_run_evidence.py`) has a distinct code or test for it; `not_submitted_error` is the generic status in `config/run_classification.json`.
2. **Conflict with the operator rule "never retry a permission denial":** that same paragraph says "Retry the FIRST refusal once and no more". The live-mirror is off-limits for this session, so this is only flagged. It is the one place the prompt contradicts the standing rule.
3. **Evidence-doc correction for 1530ET.** `REMAINING-ROWS-EVIDENCE-2026-10-17.md` says `scripts/generate_buy_candidates.py` "aborts without its primary". It does not. Its docstring (line ~254) and `test_a_down_finance_source_reconciles_on_robinhood_alone` show that when Finance is down it scores Finance `NO_DATA`/`provider_error=True` and reconciles off Robinhood alone. A would-be BUY then becomes `MANUAL_REVIEW` (never an auto-buy), and a non-BUY is surfaced via `provider_error_count` (`test_provider_error_without_manual_review_is_surfaced`). So a missing Finance connector at 1530ET means **no automatic final buy, plus an operator-visible manual-review list**, not a silent abort. The six other slots are Robinhood-primary via `market_data_source_policy.slot_overrides`; `fallback_source` is `null`.
4. `FinanceDataProvider` (`dca_live_adapters.py`) is built from injected callables; there is nothing in-repo that decides whether a connector exists. Connecting Finance is session/infrastructure, not code.

## Rows 1-2: `real_world_transactions_denied`, `session_refusal` (Permission layer)

| Option | What it is | Cost / risk |
| --- | --- | --- |
| **A. Accept stop-batch + pin it with a distinct reason code and test (recommended)** | Keep the prompt contract. Add a small constant (e.g. `PLATFORM_REFUSAL`) and an evidence/alert path so a refusal finalizes as `order_submit_status=not_submitted_error` with that reason, and the alert carries the resolved buy list. Tests-first. | Low. Repo-only, no live-prompt edit needed unless the operator wants the retry sentence changed. |
| B. Route placement through a context with stable trade authorization | Infrastructure: run placement in a session/account configuration whose permission layer does not refuse. | Not a code change; cannot be verified here; effect is unpredictable per the row. Complementary to A, not a replacement. |
| C. Mark Rejected / Monitoring only | No change. | Leaves refusals indistinguishable from other aborts in evidence. |

**Recommendation:** A now (it is the only part this repo can own), B as a separate operator infrastructure task. Proposed first test: a finalizer/evidence test where a Gate 7 run records a platform refusal with zero orders submitted and asserts (i) `order_submit_status == "not_submitted_error"`, (ii) the distinct reason code, (iii) the refusal is not counted toward `max_orders_per_day`, (iv) an alert is emitted listing the unplaced buy list. Never retries or rephrases a denial.

**Decision needed on the retry sentence:** leave the live-mirror "retry once" as is (separate Group A/prompt session), or tell that session to remove it to match the no-retry rule.

## Rows 3-4: `missing_connector`, `provider_error` (Finance connector)

| Option | What it is | Cost / risk |
| --- | --- | --- |
| **A. Keep fail-closed; six slots Robinhood-primary; 1530ET degrades to manual review (recommended; = status quo)** | Document the true 1530ET behavior (finding 3), add a regression test that the 1530ET path with Finance absent/errored emits zero BUY candidates and a non-zero `manual_review_count`/`provider_error_count`, and correct the evidence doc. Mark Monitoring. | Lowest. No source-policy change; an outage costs the final auto-buy for that day only, with the operator alerted. |
| B. Connect a Finance data source | Infrastructure: provide a Finance MCP/connector to the executor sessions. | Fixes root cause; outside the repo; connector availability has already failed live on 2026-09-09. |
| C. Labeled single-source (Robinhood-only) mode for 1530ET | Add an explicit, labeled mode (config flag + candidate/artifact label) allowing Robinhood-only BUYs at 1530ET, like the other six slots. | MEDIUM: scoring-policy change to the final buy; needs `market_data_source_policy.slot_overrides["1530ET"]`, generator change, tests and operator acceptance run. Not a yfinance fallback. |
| D. Reintroduce yfinance fallback | Retired 2026-10-12. | Excluded unless the operator explicitly decides otherwise. |

**Recommendation:** A. If the operator wants the final buy to survive a Finance outage, C is the sanctioned route (mirrors the six Robinhood-primary slots) rather than D.

## Decisions requested

1. Rows 1-2: A (distinct code + test), plus B as an infrastructure follow-up? Retry sentence: leave or remove?
2. Rows 3-4: A, B, or C? (D only on explicit approval.)

## After you decide

Implement the chosen option tests-first, run `ruff check .` and targeted pytest on touched files, open a **draft** PR naming the Keys in the body.

## Operator decision (2026-10-17, in chat) and what shipped

- Rows 1-2: option A. `PLATFORM_REFUSAL` reason code (`scripts/reason_codes.py`) and `--error-class platform_refusal` in `scripts/finalize_run_evidence.py` (run_state gets `reason_code` + `alert_required`; refuses a quiet status). B (stable-authorization placement context) is a separate operator infrastructure task.
- Retry sentence: the 1530ET live-mirror "Retry the FIRST refusal once" contradicts the no-retry rule. FLAGGED for removal in the prompt/live-mirror session; not edited here.
- Rows 3-4: option A. Behavior pinned by a regression test; evidence doc corrected.
- Follow-up for the prompt session: have live prompts pass `--error-class platform_refusal` on a refusal.

## Operator decision (2026-10-21, in chat): rows 3-4 switch to option C

The operator chose option C after the 2026-10-21 15:30 run recorded `source_errors: ["finance"]` and all 26 Robinhood
BUYs were downgraded to MANUAL_REVIEW (0 orders; none of the 13 recorded 1530ET runs since 2026-09-23 placed an order, most of them Gate 5 market-data aborts that this change does not touch).
Shipped as the "labeled single-source mode" this document describes:

- `config/scheduled_tasks.json` -> `market_data_source_policy.slot_overrides["1530ET"].finance_outage_fallback =
  "robinhood_only"` (1530ET stays Finance-primary; the key is the off-switch -- delete it to restore option A).
- `scripts/generate_buy_candidates.py`: only a WHOLE-source Finance outage (`source_errors` lists finance, or
  `sources.finance` absent) is scored as the benign "finance unavailable" shape, so `reconcile_signals` returns its
  existing "Robinhood-only BUY because finance source is unavailable". A per-ticker `ticker_errors.finance`, an
  incomplete Finance payload, or any Robinhood error still forces MANUAL_REVIEW. The output document carries
  `finance_outage_fallback_enabled`, `finance_outage_robinhood_only` and `finance_outage_robinhood_only_buy_count` so the
  run is visibly single-source.
- Still required: the operator acceptance run this document's option C calls for. The 1530ET live-mirror prompt
  (line 72) already says the generator falls back to Robinhood-only on a Finance outage; it was not edited here.
- Not a yfinance fallback (option D stays retired).

