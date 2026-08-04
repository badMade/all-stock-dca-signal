# DCA Operational TODOs

This file tracks follow-up work from live DCA incidents so the same failure modes are not lost in chat history.

## 2026-08-03 follow-ups

| Priority | Area | Problem observed | Follow-up fix |
|---|---|---|---|
| High | 11:30 intraday scheduling | The active schedule did not contain the 11:30 Perplexity-primary buying slot when checked at 11:35 AM ET. | Add a pre-market schedule integrity check that verifies every primary buying slot for the current trading day: 9:35, 10:30, 11:30, 12:30, 1:30, 2:30, and 3:30. If a slot is missing, create or repair it before market open and write a compact schedule readiness artifact. |
| High | Latest pointer publishing | The 9:35 Perplexity-primary run wrote complete root artifacts and 25 filled orders, but the expected latest pointer was missing, which made pointer-based checks report no record. | Require each Perplexity-primary buying run to publish both its canonical stream pointer and the Perplexity-primary same-day alias pointer after root artifacts are written. Add a post-write assertion that fails the run artifact contract if the pointer is missing. |
| High | Direct path fallback | The 12:30 task needed to use 9:35 fills as top-up context even when the 9:35 latest pointer was missing. | Keep the direct date/window artifact fallback for same-day top-up context. Pointer absence should be recorded as `latest_pointer_missing_artifact_contract_failure`, but direct path discovery should still load completed same-day root artifacts. |
| High | Alias pointer fallback | The 11:30 catch-up orchestration can see `latest_pointer_missing_artifact_contract_failure` when the canonical pointer is absent even though the Perplexity-primary alias root has a valid same-day pointer and complete artifacts. | Implemented shared fallback support in `scripts/same_day_pointer.select_same_day_run_state()`: the helper now checks the dated Perplexity-primary alias pointer automatically after the canonical pointer fails, while still allowing caller-supplied fallback pointers. It validates same-day, stream, root containment, target run_state, and scheduled non-manual evidence before returning a state, so future pointer-missing events can self-heal without trusting arbitrary files. |
| Medium | Artifact completion timing | A raw-input-only directory was visible before the finalized 9:35 run_state and order CSV files appeared, causing an early false read of the run as incomplete. | Implemented as `scripts/scheduled_artifacts.await_finalized_run_artifacts()`: it re-runs the artifact contract (required root files plus a non-null `run_classification`) until the run_state also carries a completion marker — any non-empty field of `COMPLETION_MARKER_FIELDS` — or the grace period expires (default 20s, polled every 1s). The window can only delay a verdict, never invent one: once it closes the contract result is returned exactly as `validate_run_artifacts` computed it, so a contract-valid run that writes no marker still reports `ok=True` (with `finalized=False`) and a genuinely broken run still reports its real errors. The clock and sleep are injectable so callers and tests can drive the window deterministically. Use it for a run that fired MOMENTS ago; `validate_run_artifacts` alone stays correct once the writer has returned. |
| Medium | Broker minimum order size | At 10:30, AAPL qualified for a small same-day top-up, but Robinhood rejected it because the dollar amount was below the broker minimum. | Add a broker-minimum policy for incremental top-ups: either skip with `below_broker_minimum_order_size` and carry the delta forward, or round only if explicitly allowed by config. Do not retry blindly. |
| Medium | Slack and in-app diagnostics | Some failures were visible only after manual checks or background result messages. | Ensure every buying run notifies only on placed orders, blocking errors, broker mismatch, missing artifacts, drift, or meaningful exceptions. Include the artifact root, run_id, and whether top-up context was loaded from pointer or direct path. |
| Low | Cost control | Recovery and debug churn drove July usage above the expected band. | Prefer targeted repair passes for missing slots or failed tickers. Avoid full-universe manual reruns unless a scheduled run fails and drift detection confirms missing orders. |

## Definition of done for these follow-ups

- Schedule integrity check writes a same-day readiness artifact before the first buying slot.
- Every primary buying slot has a same-day task or an explicit disabled reason.
- Every buying run writes `run_state.json`, `run_summary.json`, `signals.csv`, and `submitted_orders.csv` at the root.
- Every buying run writes a non-null `run_classification`.
- Every buying run publishes a same-day latest pointer after root artifacts are written.
- Later same-day runs can load prior fills from both validated pointers and direct date/window artifact paths.
- Pointer-based orchestration checks the same-day Perplexity-primary alias pointer with its alias root before surfacing a missing canonical pointer as a terminal contract failure.
- Missing pointers are recorded as artifact-contract failures without erasing valid order evidence.
- A just-fired run is given a bounded finalization window before its artifacts are judged, and that window never converts a passing contract into a failure.
