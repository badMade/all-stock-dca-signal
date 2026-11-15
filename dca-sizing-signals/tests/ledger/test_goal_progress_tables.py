"""portfolio_snapshots and goal_progress: append-only ledger tables (Phase 2, ADR tests 19-21).

Written FIRST. Same store rules as csp_shadow and the rest of the ledger: every row carries
provenance, an identical re-ingest is a no-op, a correction is a new row, readers take the latest
version known at ``known_at``, one bad row rejects the batch, and unknown or account-named keys
are refused. All values are SYNTHETIC round numbers.
"""

from __future__ import annotations

import pytest
from market_data_ingest.ledger.errors import LedgerValidationError

from .conftest import utc

SNAP = {
    "snapshot_day": "2026-11-13",
    "symbol": "ZZZA",
    "quantity": 2.0,
    "close_day": "2026-11-12",
    "prior_close": 10.0,
    "market_value": 20.0,
}
PROGRESS = {
    "day": "2026-11-13",
    "goal_id": "growth-test",
    "goal_version": 1,
    "window_days": 9,
    "period_return": 0.10,
    "annualized_return": None,
    "mwr_annualized": None,
}


def test_both_tables_exist(ledger):
    assert "portfolio_snapshots" in ledger.tables()
    assert "goal_progress" in ledger.tables()


def test_snapshot_identical_reingest_is_a_no_op(ledger):
    assert ledger.ingest_portfolio_snapshots([SNAP], source="robinhood_readonly", run_id="snap-1",
                                             as_of=utc(2026, 11, 13, 12)) == 1
    assert ledger.ingest_portfolio_snapshots([SNAP], source="robinhood_readonly", run_id="snap-1",
                                             as_of=utc(2026, 11, 13, 12)) == 0
    assert ledger.row_count("portfolio_snapshots") == 1


def test_snapshot_correction_is_a_new_row_and_latest_is_read(ledger):
    ledger.ingest_portfolio_snapshots([SNAP], source="robinhood_readonly", run_id="snap-1", as_of=utc(2026, 11, 13, 12))
    fixed = {**SNAP, "prior_close": 11.0, "market_value": 22.0}
    ledger.ingest_portfolio_snapshots(
        [fixed], source="robinhood_readonly", run_id="snap-2", as_of=utc(2026, 11, 13, 12)
    )
    assert ledger.row_count("portfolio_snapshots") == 2
    latest = ledger.portfolio_snapshots(known_at=utc(2026, 11, 14))
    assert [r["market_value"] for r in latest] == [22.0]


def test_snapshot_one_bad_row_rejects_the_batch(ledger):
    bad = {**SNAP, "symbol": "ZZZB", "quantity": -1.0}
    with pytest.raises(LedgerValidationError):
        ledger.ingest_portfolio_snapshots([SNAP, bad], source="robinhood_readonly", run_id="snap-3",
                                          as_of=utc(2026, 11, 13, 12))
    assert ledger.row_count("portfolio_snapshots") == 0


def test_snapshot_refuses_account_named_and_unknown_keys(ledger):
    with pytest.raises(LedgerValidationError):
        ledger.ingest_portfolio_snapshots([{**SNAP, "account_number": "x"}], source="robinhood_readonly",
                                          run_id="snap-4", as_of=utc(2026, 11, 13, 12))
    with pytest.raises(LedgerValidationError):
        ledger.ingest_portfolio_snapshots([{**SNAP, "extra": 1}], source="robinhood_readonly",
                                          run_id="snap-5", as_of=utc(2026, 11, 13, 12))


def test_snapshot_refuses_naive_timestamps(ledger):
    with pytest.raises(LedgerValidationError):
        ledger.ingest_portfolio_snapshots([SNAP], source="robinhood_readonly", run_id="snap-6",
                                          as_of=utc(2026, 11, 13, 12).replace(tzinfo=None))


def test_goal_progress_appends_and_reads_latest_known(ledger):
    ledger.ingest_goal_progress([PROGRESS], source="goal_progress", run_id="gp-1", as_of=utc(2026, 11, 13, 12))
    corrected = {**PROGRESS, "period_return": 0.11}
    ledger.ingest_goal_progress([corrected], source="goal_progress", run_id="gp-2", as_of=utc(2026, 11, 13, 12))
    assert ledger.row_count("goal_progress") == 2
    assert [r["period_return"] for r in ledger.goal_progress(known_at=utc(2026, 11, 14))] == [0.11]


def test_goal_progress_annualized_is_null_under_one_year(ledger):
    ledger.ingest_goal_progress([PROGRESS], source="goal_progress", run_id="gp-3", as_of=utc(2026, 11, 13, 12))
    row = ledger.goal_progress(known_at=utc(2026, 11, 14))[0]
    assert row["window_days"] < 365
    assert row["annualized_return"] is None


def test_goal_progress_refuses_annualized_value_under_one_year(ledger):
    bad = {**PROGRESS, "annualized_return": 32.0}
    with pytest.raises(LedgerValidationError):
        ledger.ingest_goal_progress([bad], source="goal_progress", run_id="gp-4", as_of=utc(2026, 11, 13, 12))
    assert ledger.row_count("goal_progress") == 0


# ---- the no-holdings marker (regression: the ledger used to refuse it) -------------------------
# dca_goal_snapshot.capture_previous_close emits exactly this row for a trading day on which the
# account holds nothing (tests/test_goal_snapshot_capture.py). It must round-trip unchanged.
NO_HOLDINGS = {"snapshot_day": "2026-11-13", "symbol": None, "quantity": 0.0,
               "close_day": None, "prior_close": None, "market_value": 0.0}


def test_no_holdings_marker_is_accepted_and_reads_back_unchanged(ledger):
    assert ledger.ingest_portfolio_snapshots([NO_HOLDINGS], source="robinhood_readonly", run_id="snap-7",
                                             as_of=utc(2026, 11, 13, 12)) == 1
    assert ledger.ingest_portfolio_snapshots([NO_HOLDINGS], source="robinhood_readonly", run_id="snap-7",
                                             as_of=utc(2026, 11, 13, 12)) == 0
    [row] = ledger.portfolio_snapshots(known_at=utc(2026, 11, 14))
    assert {k: row[k] for k in NO_HOLDINGS} == {**NO_HOLDINGS, "snapshot_day": row["snapshot_day"]}
    assert row["snapshot_day"].isoformat() == "2026-11-13"
    assert row["symbol"] is None and row["close_day"] is None and row["prior_close"] is None


def test_no_holdings_marker_cannot_share_a_batch_with_positions(ledger):
    with pytest.raises(LedgerValidationError):
        ledger.ingest_portfolio_snapshots([SNAP, NO_HOLDINGS], source="robinhood_readonly", run_id="snap-8",
                                          as_of=utc(2026, 11, 13, 12))
    assert ledger.row_count("portfolio_snapshots") == 0


@pytest.mark.parametrize("field, value", [("quantity", 2.0), ("market_value", 20.0), ("prior_close", 10.0),
                                          ("close_day", "2026-11-12")])
def test_a_symbolless_row_that_is_not_the_exact_marker_is_refused(ledger, field, value):
    with pytest.raises(LedgerValidationError):
        ledger.ingest_portfolio_snapshots([{**NO_HOLDINGS, field: value}], source="robinhood_readonly",
                                          run_id="snap-9", as_of=utc(2026, 11, 13, 12))
    assert ledger.row_count("portfolio_snapshots") == 0


def test_the_marker_sentinel_is_not_accepted_as_a_real_symbol(ledger):
    with pytest.raises(LedgerValidationError):
        ledger.ingest_portfolio_snapshots([{**SNAP, "symbol": ledger.NO_HOLDINGS_SYMBOL}],
                                          source="robinhood_readonly", run_id="snap-10",
                                          as_of=utc(2026, 11, 13, 12))
