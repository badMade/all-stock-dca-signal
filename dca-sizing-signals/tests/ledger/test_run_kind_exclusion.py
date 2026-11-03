"""Manual-run fills never enter scheduled metrics (docs/run_classification.md).

The repo's vocabulary is ``run_origin`` = ``scheduled`` | ``manual``; the ledger stores it
as ``run_kind`` with exactly those two values. Reporting excludes manual runs AND manual
order IDs, so an order id that shows up tagged manual anywhere is excluded even if some
other row tags it scheduled (fail closed: an ambiguous order is not scheduled evidence).

Fixture (synthetic): three ZZZA fills on D0 at 10.00, t+5 close 11.00 (+10%):
  ord-s1  scheduled, $100            -> counted
  ord-m1  manual,    $40             -> excluded (manual run)
  ord-x1  scheduled, $60 AND a second row for the same order tagged manual -> excluded
"""

from __future__ import annotations

import pytest

from .conftest import FIRST_SEEN, LATER, SESSIONS, bar, fill, ingest_bars, ingest_calendar, utc

KNOW_ALL = utc(2026, 6, 1)


def _setup(ledger, staging_dir):
    ingest_calendar(ledger)
    ingest_bars(ledger, staging_dir, [bar("ZZZA", SESSIONS[5], 11.00), bar("ZZZA", SESSIONS[20], 12.00)])
    ledger.ingest_fills(
        [
            fill("ord-s1", notional=100.0),
            fill("ord-m1", run_kind="manual", notional=40.0),
            fill("ord-x1", notional=60.0),
        ],
        source="run_artifact",
        run_id="f1",
        as_of=FIRST_SEEN,
    )
    ledger.ingest_fills(
        [fill("ord-x1", run_kind="manual", notional=60.0)], source="broker_order_history", run_id="f2", as_of=LATER
    )
    ledger.record_outcomes(known_at=KNOW_ALL, run_id="score", as_of=KNOW_ALL)


def test_run_kind_vocabulary_matches_run_classification():
    from market_data_ingest.ledger import defaults

    assert defaults.RUN_KINDS == ("scheduled", "manual")


def test_unknown_run_kind_is_rejected(ledger):
    from market_data_ingest.ledger.errors import LedgerValidationError

    with pytest.raises(LedgerValidationError, match="run_kind"):
        ledger.ingest_fills([fill("ord-1", run_kind="adhoc")], source="run_artifact", run_id="r", as_of=FIRST_SEEN)


def test_scheduled_outcomes_exclude_manual_runs_and_manual_order_ids(ledger, staging_dir):
    _setup(ledger, staging_dir)
    ids = {r["subject_id"] for r in ledger.scheduled_outcomes(known_at=KNOW_ALL)}
    assert ids == {"ord-s1"}
    # The excluded rows are still in the ledger (nothing deleted), just not in the metric.
    all_ids = {r["subject_id"] for r in ledger.outcomes(known_at=KNOW_ALL)}
    assert {"ord-s1", "ord-m1", "ord-x1"} <= all_ids


def test_scheduled_summary_counts_only_scheduled_dollars(ledger, staging_dir):
    _setup(ledger, staging_dir)
    summary = {
        (r["subject_kind"], r["horizon_sessions"]): r for r in ledger.scheduled_outcome_summary(known_at=KNOW_ALL)
    }
    t5 = summary[("fill", 5)]
    # Only ord-s1: n=1, $100 notional, +10% -> $10.00 P&L, notional-weighted return 0.10.
    # Had the manual $40 or the ambiguous $60 leaked in, notional would read 140 or 200.
    assert t5["n_subjects"] == 1
    assert t5["n_scored"] == 1
    assert t5["notional"] == pytest.approx(100.0)
    assert t5["dollar_pnl"] == pytest.approx(10.0)
    assert t5["weighted_return"] == pytest.approx(0.10)
    # t+20 close 12.00 -> +20% -> $20.00.
    assert summary[("fill", 20)]["dollar_pnl"] == pytest.approx(20.0)


def test_unscored_rows_count_as_subjects_but_never_as_zero_return(ledger, staging_dir):
    # Calendar ends before t+20: the t+20 row exists, is unscored, and contributes no P&L
    # rather than a 0% return.
    ingest_calendar(ledger, sessions=SESSIONS[:11])
    ingest_bars(ledger, staging_dir, [bar("ZZZA", SESSIONS[5], 11.00)])
    ledger.ingest_fills([fill("ord-s1", notional=100.0)], source="run_artifact", run_id="f1", as_of=FIRST_SEEN)
    ledger.record_outcomes(known_at=KNOW_ALL, run_id="score", as_of=KNOW_ALL)
    t20 = {(r["subject_kind"], r["horizon_sessions"]): r for r in ledger.scheduled_outcome_summary(known_at=KNOW_ALL)}[
        ("fill", 20)
    ]
    assert t20["n_subjects"] == 1
    assert t20["n_scored"] == 0
    assert t20["dollar_pnl"] is None
    assert t20["weighted_return"] is None
