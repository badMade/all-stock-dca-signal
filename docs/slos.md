# Service level objectives (proposed)

**Status: proposed targets, written 2026-11-13 so a weekly readiness check and any SRE-style
review have something to measure against.** Nothing here changes runtime behaviour. Each number is a starting point to be tightened
or loosened after four weeks of measured data; edit this file when that happens and note the date.

A single-operator, single-account system does not need five nines. The objectives below protect the three
things that cost money or trust when they fail: a slot that never fires, a buy that is not reconciled, and an
alert nobody sees.

## Objectives

| # | Objective | Indicator (SLI) | Target (SLO) | Window | Where to measure it |
|---|---|---|---|---|---|
| 1 | Scheduled slots fire | Fraction of scheduled live slots on trading days that leave Gate 9 evidence | ≥ 98% | rolling 30 days | `scripts/verify_schedule.py` ([schedule_verification.md](schedule_verification.md)) |
| 2 | Orders reconcile | Fraction of placed orders whose post-run broker snapshot reconciles to the run artifact with no unexplained difference | 100% of placed orders; any miss is an incident | rolling 30 days | `scripts/broker_reconciliation.py`, run artifacts ([reliability_spec.md](reliability_spec.md)) |
| 3 | No over-deployment | Days where deployed USD exceeded the daily, weekly, or per-order cap | 0 | rolling 90 days | `dca_risk.py` report, cap checks in [safeguards.md](safeguards.md) |
| 4 | Alerts are delivered | Fraction of Gate 9 alerts whose delivery channel was on and received | ≥ 99% | rolling 30 days | `scripts/alert_digest.py` ([alert_digest.md](alert_digest.md)) |
| 5 | Missed fires are detected | Time from a missed fire to an alert | ≤ 30 minutes after the slot's grace window | per incident | per-stream heartbeat checks (`DCA_HEARTBEAT_URL_<STREAM>`) |
| 6 | Evidence is complete | Live slots with a finalized evidence file | 100% | rolling 30 days | `scripts/finalize_run_evidence.py` |
| 7 | Change safety | Merges to `main` with green required CI | 100% (no red-merge) | rolling 30 days | GitHub Actions history, `local-ci` status ([local_ci.md](local_ci.md)) |

## Error budget and what to do when it is spent

- **SLOs 2 and 3 have a zero budget.** One miss is an incident: stop adding features, follow
  [emergency_stop.md](emergency_stop.md) if money may still be at risk, write an incident note in `docs/`, and add a
  regression test before resuming.
- **SLOs 1, 4, 5, 6 have a small budget** (about 1 to 2 events a month). Spending it means the next change
  window goes to reliability work (Phase 2 in [`PRODUCTION_READINESS.md`](../PRODUCTION_READINESS.md)) before new
  features.
- **SLO 7** is a hygiene objective. A red merge is fixed forward within one trading day.

## Known measurement limits

- Live authority sits in scheduler task text outside this repo (`PRODUCTION_READINESS.md` gap 1), so SLOs 1 and 5
  can only be measured from the artifacts and heartbeats the runs leave behind, not from the scheduler's own log.
- Until heartbeat URLs are configured for every stream, SLO 5 is unmeasured, not met.
