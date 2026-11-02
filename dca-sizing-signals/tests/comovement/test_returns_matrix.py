"""returns_matrix(): point-in-time daily log returns from caller-supplied close rows.

ADR rule 1 (clusters are point-in-time): only closes known before as_of count, a ticker
with too little history becomes a singleton, and Robinhood's interpolated pre-listing
bars are not real prices.

Review round 1 (PR #2106): a bar dated ON as_of is excluded unless the caller marks it
``final`` -- at an intraday slot today's daily bar is still forming and is not a close.
So most tests below put as_of one day after the last toy close.
"""

from __future__ import annotations

import math

import pytest

from comovement_signals import ComovementInputError, returns_matrix

from ._toy import day, rows_from_closes


def test_log_returns_basic():
    # 100 -> 110 -> 99: ln(110/100) = ln(1.1) and ln(99/110) = ln(0.9). as_of day(3) is
    # the next day, so the day(2) close is final by date.
    rows = rows_from_closes("A", [100.0, 110.0, 99.0])
    m = returns_matrix(rows, as_of=day(3), window_sessions=252, min_sessions=2)
    assert m.sessions == (day(1), day(2))
    assert m.returns["A"][day(1)] == pytest.approx(math.log(1.1))
    assert m.returns["A"][day(2)] == pytest.approx(math.log(0.9))
    assert m.eligible == ("A",)
    assert m.singletons == ()
    assert m.latest_price_date == day(2)


def test_iso_string_dates_accepted():
    rows = [{"ticker": "A", "date": "2026-01-01", "close": 100.0},
            {"ticker": "A", "date": "2026-01-02", "close": 110.0}]
    m = returns_matrix(rows, as_of=day(2), min_sessions=1)
    assert m.returns["A"][day(1)] == pytest.approx(math.log(1.1))


def test_row_after_as_of_changes_nothing():
    # No lookahead: a wild close dated after as_of, and a ticker that only exists after
    # as_of, must leave the matrix exactly as it was.
    base = rows_from_closes("A", [100.0, 110.0, 99.0]) + rows_from_closes("B", [50.0, 51.0, 52.0])
    future = [{"ticker": "A", "date": day(4), "close": 1000.0},
              {"ticker": "Z", "date": day(4), "close": 5.0},
              {"ticker": "Z", "date": day(5), "close": 6.0, "final": True}]
    without = returns_matrix(base, as_of=day(3), min_sessions=2)
    with_future = returns_matrix(base + future, as_of=day(3), min_sessions=2)
    assert with_future == without
    assert "Z" not in with_future.returns


def test_bar_on_as_of_is_excluded_unless_final():
    # A forming intraday bar on as_of (day(2), close 99) is not a close yet: without the
    # final flag only the day(1) return ln(1.1) exists. Marked final, ln(0.9) appears.
    forming = rows_from_closes("A", [100.0, 110.0, 99.0])
    m = returns_matrix(forming, as_of=day(2), min_sessions=1)
    assert m.sessions == (day(1),)
    assert m.latest_price_date == day(1)
    final = rows_from_closes("A", [100.0, 110.0]) + [{"ticker": "A", "date": day(2), "close": 99.0, "final": True}]
    m2 = returns_matrix(final, as_of=day(2), min_sessions=1)
    assert m2.returns["A"][day(2)] == pytest.approx(math.log(0.9))


def test_final_flag_must_be_boolean():
    rows = [{"ticker": "A", "date": day(0), "close": 1.0, "final": "yes"}]
    with pytest.raises(ComovementInputError):
        returns_matrix(rows, as_of=day(0))


def test_interpolated_rows_ignored():
    # B's first two bars are interpolated pre-listing fill (close 999). Its first real close
    # is day(2) = 50, then 55 on day(3): one real return, ln(55/50) = ln(1.1), on day(3).
    # If the interpolated bars were used, B would also get returns on day(1) and day(2).
    rows = (rows_from_closes("A", [100.0, 101.0, 102.0, 103.0])
            + rows_from_closes("B", [999.0, 999.0], interpolated=True)
            + rows_from_closes("B", [50.0, 55.0], start=2, interpolated=False))
    m = returns_matrix(rows, as_of=day(4), min_sessions=1)
    assert m.returns["B"] == {day(3): pytest.approx(math.log(1.1))}


def test_interpolated_row_with_null_close_is_skipped():
    # Review round 1: interpolated rows are ignored anyway, so a null or junk close on one
    # must not fail the whole run. The same junk on a real row still fails closed.
    rows = rows_from_closes("A", [100.0, 110.0]) + [
        {"ticker": "B", "date": day(0), "close": None, "interpolated": True},
        {"ticker": "B", "date": day(1), "close": "n/a", "interpolated": True},
    ]
    m = returns_matrix(rows, as_of=day(2), min_sessions=1)
    assert "B" not in m.returns
    with pytest.raises(ComovementInputError):
        returns_matrix(rows + [{"ticker": "C", "date": day(0), "close": None}], as_of=day(2))


def test_interpolated_flag_must_be_boolean():
    rows = [{"ticker": "A", "date": day(0), "close": 1.0, "interpolated": "false"}]
    with pytest.raises(ComovementInputError):
        returns_matrix(rows, as_of=day(1))


def test_window_keeps_only_last_n_sessions():
    # Six closes give five returns on day(1)..day(5); a 3-session window keeps day(3)..day(5).
    rows = rows_from_closes("A", [100.0, 101.0, 102.0, 103.0, 104.0, 105.0])
    m = returns_matrix(rows, as_of=day(6), window_sessions=3, min_sessions=1)
    assert m.sessions == (day(3), day(4), day(5))
    assert sorted(m.returns["A"]) == [day(3), day(4), day(5)]
    assert m.returns["A"][day(3)] == pytest.approx(math.log(103.0 / 102.0))


def test_short_history_becomes_singleton():
    # A: 6 closes -> 5 returns. B lists on day(3): 3 closes -> 2 returns. With a 3-return
    # minimum A is eligible for clustering and B is a singleton.
    rows = rows_from_closes("A", [100.0, 101.0, 102.0, 103.0, 104.0, 105.0]) + rows_from_closes(
        "B", [10.0, 11.0, 12.0], start=3)
    m = returns_matrix(rows, as_of=day(6), min_sessions=3)
    assert m.eligible == ("A",)
    assert m.singletons == ("B",)


def test_gap_does_not_produce_multi_day_return():
    # A has no bar on day(2), while B trades every day. A's returns: day(1) = ln(110/100);
    # day(2) missing (no close); day(3) missing (no close on the prior session day(2));
    # day(4) = ln(133.1/121) = ln(1.1). A 3-session jump 110 -> 121 is never reported.
    rows = ([{"ticker": "A", "date": day(i), "close": c} for i, c in ((0, 100.0), (1, 110.0), (3, 121.0), (4, 133.1))]
            + rows_from_closes("B", [1.0, 1.0, 1.0, 1.0, 1.0]))
    m = returns_matrix(rows, as_of=day(5), min_sessions=1)
    assert sorted(m.returns["A"]) == [day(1), day(4)]
    assert m.returns["A"][day(4)] == pytest.approx(math.log(1.1))


def test_stray_date_for_one_ticker_does_not_break_everyone():
    # Review round 1: A, B and C trade on even days 0, 2, 4, 6; C alone also has a stray
    # row on day(3). Only 1 of the 3 listed tickers has a close on day(3) (< half), so it
    # is not a session. A and B keep their returns on day(2), day(4) and day(6); a naive
    # union calendar would have dropped day(4) for both. C's stray row is ignored, so C's
    # day(4) return is ln(104/102), not ln(104/stray).
    rows = []
    for t in ("A", "B", "C"):
        rows += [{"ticker": t, "date": day(i), "close": 100.0 + i} for i in (0, 2, 4, 6)]
    rows.append({"ticker": "C", "date": day(3), "close": 500.0})
    m = returns_matrix(rows, as_of=day(7), min_sessions=1)
    assert m.sessions == (day(2), day(4), day(6))
    for t in ("A", "B", "C"):
        assert sorted(m.returns[t]) == [day(2), day(4), day(6)]
    assert m.returns["C"][day(4)] == pytest.approx(math.log(104.0 / 102.0))


def test_new_listing_dates_still_sessions():
    # Before B lists, A is the only listed ticker, so A's own dates stay sessions (the
    # share is taken over tickers listed on that date, not over the whole universe).
    rows = rows_from_closes("A", [100.0, 101.0, 102.0, 103.0]) + rows_from_closes("B", [1.0, 2.0], start=2)
    m = returns_matrix(rows, as_of=day(4), min_sessions=1)
    assert m.sessions == (day(1), day(2), day(3))


@pytest.mark.parametrize("bad_close", [0.0, -1.0, float("nan"), float("inf"), None])
def test_non_positive_or_non_finite_close_fails_closed(bad_close):
    rows = [{"ticker": "A", "date": day(0), "close": 100.0}, {"ticker": "A", "date": day(1), "close": bad_close}]
    with pytest.raises(ComovementInputError):
        returns_matrix(rows, as_of=day(2))


def test_duplicate_ticker_date_fails_closed():
    rows = [{"ticker": "A", "date": day(0), "close": 100.0}, {"ticker": "A", "date": day(0), "close": 100.0}]
    with pytest.raises(ComovementInputError):
        returns_matrix(rows, as_of=day(1))


def test_missing_field_fails_closed():
    with pytest.raises(ComovementInputError):
        returns_matrix([{"ticker": "A", "date": day(0)}], as_of=day(1))
