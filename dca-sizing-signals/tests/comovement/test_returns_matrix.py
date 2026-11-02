"""returns_matrix(): point-in-time daily log returns from caller-supplied close rows.

ADR rule 1 (clusters are point-in-time): only rows dated on or before as_of count, a
ticker with too little history becomes a singleton, and Robinhood's interpolated
pre-listing bars are not real prices.
"""

from __future__ import annotations

import math

import pytest

from comovement_signals import ComovementInputError, returns_matrix

from ._toy import day, rows_from_closes


def test_log_returns_basic():
    # 100 -> 110 -> 99: ln(110/100) = ln(1.1) and ln(99/110) = ln(0.9).
    rows = rows_from_closes("A", [100.0, 110.0, 99.0])
    m = returns_matrix(rows, as_of=day(2), window_sessions=252, min_sessions=2)
    assert m.sessions == (day(1), day(2))
    assert m.returns["A"][day(1)] == pytest.approx(math.log(1.1))
    assert m.returns["A"][day(2)] == pytest.approx(math.log(0.9))
    assert m.eligible == ("A",)
    assert m.singletons == ()
    assert m.latest_price_date == day(2)


def test_iso_string_dates_accepted():
    rows = [{"ticker": "A", "date": "2026-01-01", "close": 100.0},
            {"ticker": "A", "date": "2026-01-02", "close": 110.0}]
    m = returns_matrix(rows, as_of=day(1), min_sessions=1)
    assert m.returns["A"][day(1)] == pytest.approx(math.log(1.1))


def test_row_after_as_of_changes_nothing():
    # No lookahead: a wild close dated after as_of, and a ticker that only exists after
    # as_of, must leave the matrix exactly as it was.
    base = rows_from_closes("A", [100.0, 110.0, 99.0]) + rows_from_closes("B", [50.0, 51.0, 52.0])
    future = [{"ticker": "A", "date": day(3), "close": 1000.0},
              {"ticker": "Z", "date": day(3), "close": 5.0},
              {"ticker": "Z", "date": day(4), "close": 6.0}]
    without = returns_matrix(base, as_of=day(2), min_sessions=2)
    with_future = returns_matrix(base + future, as_of=day(2), min_sessions=2)
    assert with_future == without
    assert "Z" not in with_future.returns


def test_interpolated_rows_ignored():
    # B's first two bars are interpolated pre-listing fill (close 999). Its first real close
    # is day(2) = 50, then 55 on day(3): one real return, ln(55/50) = ln(1.1), on day(3).
    # If the interpolated bars were used, B would also get returns on day(1) and day(2).
    rows = (rows_from_closes("A", [100.0, 101.0, 102.0, 103.0])
            + rows_from_closes("B", [999.0, 999.0], interpolated=True)
            + rows_from_closes("B", [50.0, 55.0], start=2, interpolated=False))
    m = returns_matrix(rows, as_of=day(3), min_sessions=1)
    assert m.returns["B"] == {day(3): pytest.approx(math.log(1.1))}


def test_interpolated_flag_must_be_boolean():
    rows = [{"ticker": "A", "date": day(0), "close": 1.0, "interpolated": "false"}]
    with pytest.raises(ComovementInputError):
        returns_matrix(rows, as_of=day(0))


def test_window_keeps_only_last_n_sessions():
    # Six closes give five returns on day(1)..day(5); a 3-session window keeps day(3)..day(5).
    rows = rows_from_closes("A", [100.0, 101.0, 102.0, 103.0, 104.0, 105.0])
    m = returns_matrix(rows, as_of=day(5), window_sessions=3, min_sessions=1)
    assert m.sessions == (day(3), day(4), day(5))
    assert sorted(m.returns["A"]) == [day(3), day(4), day(5)]
    assert m.returns["A"][day(3)] == pytest.approx(math.log(103.0 / 102.0))


def test_short_history_becomes_singleton():
    # A: 6 closes -> 5 returns. B lists on day(3): 3 closes -> 2 returns. With a 3-return
    # minimum A is eligible for clustering and B is a singleton.
    rows = rows_from_closes("A", [100.0, 101.0, 102.0, 103.0, 104.0, 105.0]) + rows_from_closes(
        "B", [10.0, 11.0, 12.0], start=3)
    m = returns_matrix(rows, as_of=day(5), min_sessions=3)
    assert m.eligible == ("A",)
    assert m.singletons == ("B",)


def test_gap_does_not_produce_multi_day_return():
    # A has no bar on day(2), while B trades every day. A's returns: day(1) = ln(110/100);
    # day(2) missing (no close); day(3) missing (no close on the prior session day(2));
    # day(4) = ln(133.1/121) = ln(1.1). A 3-session jump 110 -> 121 is never reported.
    rows = ([{"ticker": "A", "date": day(i), "close": c} for i, c in ((0, 100.0), (1, 110.0), (3, 121.0), (4, 133.1))]
            + rows_from_closes("B", [1.0, 1.0, 1.0, 1.0, 1.0]))
    m = returns_matrix(rows, as_of=day(4), min_sessions=1)
    assert sorted(m.returns["A"]) == [day(1), day(4)]
    assert m.returns["A"][day(4)] == pytest.approx(math.log(1.1))


@pytest.mark.parametrize("bad_close", [0.0, -1.0, float("nan"), float("inf")])
def test_non_positive_or_non_finite_close_fails_closed(bad_close):
    rows = [{"ticker": "A", "date": day(0), "close": 100.0}, {"ticker": "A", "date": day(1), "close": bad_close}]
    with pytest.raises(ComovementInputError):
        returns_matrix(rows, as_of=day(1))


def test_duplicate_ticker_date_fails_closed():
    rows = [{"ticker": "A", "date": day(0), "close": 100.0}, {"ticker": "A", "date": day(0), "close": 100.0}]
    with pytest.raises(ComovementInputError):
        returns_matrix(rows, as_of=day(0))


def test_missing_field_fails_closed():
    with pytest.raises(ComovementInputError):
        returns_matrix([{"ticker": "A", "date": day(0)}], as_of=day(0))
