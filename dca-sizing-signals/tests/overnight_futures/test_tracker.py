"""FuturesOvernightTracker (docs/adr-overnight-futures-tracker.md, D4 and D9).

Expected behaviour comes from the ADR's definitions, not from an implementation:

* Window: 16:00 ET on the prior NYSE session (13:00 ET on a half-day) to 09:29 ET on
  the session date, built with the tz database (America/New_York), never a fixed offset.
* Overnight move (fair-value adjusted): ln(F_pre / F_anchor) - ln(FV_pre / FV_anchor),
  on ONE contract for the whole window.
* Implied open gap: exp(adjusted move) - 1, relative to the prior index close.
* Fail closed (D9): a splice, a bad price, a window touching the 09:30 open, or missing
  fair value yields no adjusted move and a reason code. Nothing raises.
"""

from __future__ import annotations

import math
from datetime import date, datetime, time, timezone

import pytest

from overnight_futures.tracker import OvernightInputs, compute_overnight_session, session_window_utc

UTC = timezone.utc


def _inputs(**overrides) -> OvernightInputs:
    """A plain weekday night (Tue 2026-11-10 16:00 ET -> Wed 2026-11-11 09:29 ET, EST)."""
    base = {
        "root": "ES",
        "session_date": date(2026, 11, 11),
        "anchor_contract_id": "ESZ26",
        "pre_open_contract_id": "ESZ26",
        "anchor_close_utc": datetime(2026, 11, 10, 21, 0, tzinfo=UTC),
        "pre_open_utc": datetime(2026, 11, 11, 14, 29, tzinfo=UTC),
        "f_anchor": 6000.0,
        "f_pre_open": 6030.0,
        "fair_value_anchor": 6010.0,
        "fair_value_pre_open": 6010.0,
        "is_half_day_anchor": False,
    }
    base.update(overrides)
    return OvernightInputs(**base)


# --- the window: tz database, DST, weekends, half-days -----------------------------------


def test_weekday_window_in_standard_time() -> None:
    anchor, pre_open = session_window_utc(prior_session=date(2026, 11, 10), session=date(2026, 11, 11))
    assert anchor == datetime(2026, 11, 10, 21, 0, tzinfo=UTC)
    assert pre_open == datetime(2026, 11, 11, 14, 29, tzinfo=UTC)


def test_weekday_window_in_daylight_time() -> None:
    anchor, pre_open = session_window_utc(prior_session=date(2026, 7, 14), session=date(2026, 7, 15))
    assert anchor == datetime(2026, 7, 14, 20, 0, tzinfo=UTC)
    assert pre_open == datetime(2026, 7, 15, 13, 29, tzinfo=UTC)


def test_window_across_autumn_dst_change() -> None:
    """US DST ended Sun 2026-11-01: anchor is EDT (UTC-4), pre-open is EST (UTC-5)."""
    anchor, pre_open = session_window_utc(prior_session=date(2026, 10, 30), session=date(2026, 11, 2))
    assert anchor == datetime(2026, 10, 30, 20, 0, tzinfo=UTC)
    assert pre_open == datetime(2026, 11, 2, 14, 29, tzinfo=UTC)


def test_window_across_spring_dst_change() -> None:
    """US DST began Sun 2026-03-08: anchor is EST (UTC-5), pre-open is EDT (UTC-4)."""
    anchor, pre_open = session_window_utc(prior_session=date(2026, 3, 6), session=date(2026, 3, 9))
    assert anchor == datetime(2026, 3, 6, 21, 0, tzinfo=UTC)
    assert pre_open == datetime(2026, 3, 9, 13, 29, tzinfo=UTC)


def test_half_day_anchor_is_1300_et() -> None:
    """Day after Thanksgiving 2026 (Fri 11-27) closes at 13:00 ET."""
    anchor, _ = session_window_utc(
        prior_session=date(2026, 11, 27), session=date(2026, 11, 30), anchor_close_et=time(13, 0)
    )
    assert anchor == datetime(2026, 11, 27, 18, 0, tzinfo=UTC)


def test_window_rejects_session_not_after_prior() -> None:
    with pytest.raises(ValueError):
        session_window_utc(prior_session=date(2026, 11, 11), session=date(2026, 11, 11))


@pytest.mark.parametrize(
    ("prior", "session", "expected_hours"),
    [
        (date(2026, 11, 10), date(2026, 11, 11), 17 + 29 / 60),  # weekday night
        (date(2026, 11, 6), date(2026, 11, 9), 65 + 29 / 60),  # weekend
        (date(2026, 10, 30), date(2026, 11, 2), 66 + 29 / 60),  # weekend that gains an hour
        (date(2026, 3, 6), date(2026, 3, 9), 64 + 29 / 60),  # weekend that loses an hour
    ],
    ids=["weekday", "weekend", "weekend-dst-end", "weekend-dst-start"],
)
def test_hours_in_window_is_real_elapsed_time(prior: date, session: date, expected_hours: float) -> None:
    anchor, pre_open = session_window_utc(prior_session=prior, session=session)
    result = compute_overnight_session(_inputs(session_date=session, anchor_close_utc=anchor, pre_open_utc=pre_open))
    assert result.hours_in_window == pytest.approx(expected_hours)


# --- the move -------------------------------------------------------------------------------


def test_adjusted_move_equals_raw_move_when_fair_value_is_flat() -> None:
    result = compute_overnight_session(_inputs())
    assert result.status == "ok"
    assert result.fv_quality == "ok"
    assert result.overnight_ret_raw == pytest.approx(math.log(6030.0 / 6000.0))
    assert result.overnight_ret_fv_adj == pytest.approx(math.log(6030.0 / 6000.0))
    assert result.implied_gap == pytest.approx(6030.0 / 6000.0 - 1)


def test_fair_value_change_is_removed_from_the_move() -> None:
    """An ex-dividend night: futures fall by exactly the fair-value drop. Raw says 'down',
    adjusted says 'nothing happened'. This is the sawtooth bias D4 exists to remove."""
    result = compute_overnight_session(
        _inputs(f_anchor=6000.0, f_pre_open=5994.0, fair_value_anchor=6010.0, fair_value_pre_open=6004.0)
    )
    assert result.overnight_ret_raw < 0
    assert result.overnight_ret_fv_adj == pytest.approx(
        math.log(5994.0 / 6000.0) - math.log(6004.0 / 6010.0)
    )
    assert abs(result.overnight_ret_fv_adj) < abs(result.overnight_ret_raw) / 10
    assert result.implied_gap == pytest.approx(math.exp(result.overnight_ret_fv_adj) - 1)


def test_result_is_deterministic() -> None:
    assert compute_overnight_session(_inputs()) == compute_overnight_session(_inputs())


# --- fail closed (D9) ----------------------------------------------------------------------


def test_contract_splice_is_refused() -> None:
    """Roll night with different contracts at the two ends: never splice (D4)."""
    result = compute_overnight_session(_inputs(anchor_contract_id="ESZ26", pre_open_contract_id="ESH27"))
    assert result.status == "rejected_contract_splice"
    assert result.overnight_ret_raw is None
    assert result.overnight_ret_fv_adj is None
    assert result.implied_gap is None


@pytest.mark.parametrize("field", ["f_anchor", "f_pre_open"])
@pytest.mark.parametrize("bad", [None, 0.0, -1.0, math.nan, math.inf])
def test_bad_futures_price_is_refused(field: str, bad) -> None:
    result = compute_overnight_session(_inputs(**{field: bad}))
    assert result.status == "rejected_invalid_price"
    assert result.overnight_ret_raw is None
    assert result.overnight_ret_fv_adj is None


def test_pre_open_at_or_after_cash_open_is_refused() -> None:
    """09:30 ET or later would read a print at or after the open: lookahead."""
    at_open = datetime(2026, 11, 11, 14, 30, tzinfo=UTC)  # 09:30 EST
    result = compute_overnight_session(_inputs(pre_open_utc=at_open))
    assert result.status == "rejected_lookahead"
    assert result.overnight_ret_fv_adj is None


def test_anchor_not_before_pre_open_is_refused() -> None:
    result = compute_overnight_session(
        _inputs(anchor_close_utc=datetime(2026, 11, 11, 14, 29, tzinfo=UTC))
    )
    assert result.status == "rejected_window"
    assert result.overnight_ret_fv_adj is None


def test_naive_timestamps_are_refused() -> None:
    """Without a timezone the window is ambiguous; a fixed-offset guess is exactly the bug D4 bans."""
    result = compute_overnight_session(_inputs(anchor_close_utc=datetime(2026, 11, 10, 21, 0)))
    assert result.status == "rejected_window"


@pytest.mark.parametrize("field", ["fair_value_anchor", "fair_value_pre_open"])
def test_missing_fair_value_keeps_raw_but_withholds_adjusted(field: str) -> None:
    """D5/D9: the row is stored with fv_quality 'missing', but nothing is forecast from it."""
    result = compute_overnight_session(_inputs(**{field: None}))
    assert result.status == "ok"
    assert result.fv_quality == "missing"
    assert result.overnight_ret_raw == pytest.approx(math.log(6030.0 / 6000.0))
    assert result.overnight_ret_fv_adj is None
    assert result.implied_gap is None


def test_half_day_anchor_is_computed_but_excluded_from_evaluation() -> None:
    result = compute_overnight_session(_inputs(is_half_day_anchor=True))
    assert result.status == "ok"
    assert result.overnight_ret_fv_adj is not None
    assert result.exclude_from_evaluation is True


def test_full_session_is_not_excluded() -> None:
    assert compute_overnight_session(_inputs()).exclude_from_evaluation is False


def test_never_raises_on_garbage() -> None:
    """D9: whatever arrives, the tracker answers with a status, not an exception."""
    result = compute_overnight_session(
        _inputs(f_anchor="6000", fair_value_anchor="x", anchor_close_utc="yesterday")  # type: ignore[arg-type]
    )
    assert result.status.startswith("rejected_")
