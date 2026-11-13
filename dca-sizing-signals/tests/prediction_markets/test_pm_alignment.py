"""Daily alignment to equities: last mid strictly before 16:00 ET, DST-correct (ADR §6; methods doc §4)."""

from __future__ import annotations

from datetime import date

import pytest

from .conftest import pm, quote, utc


@pytest.mark.parametrize(
    ("session_date", "close_utc_hour"),
    [
        pytest.param(date(2026, 3, 6), 21, id="EST-before-spring-forward"),
        pytest.param(date(2026, 3, 9), 20, id="EDT-after-spring-forward"),
        pytest.param(date(2026, 10, 30), 20, id="EDT-before-fall-back"),
        pytest.param(date(2026, 11, 2), 21, id="EST-after-fall-back"),
    ],
)
def test_daily_alignment_uses_last_mid_before_1600_et(session_date, close_utc_hour):
    d, h = session_date, close_utc_hour
    rows = [
        quote(ts_utc=utc(d.year, d.month, d.day, h - 1, 30), mid=0.21),
        quote(ts_utc=utc(d.year, d.month, d.day, h - 1, 59, 59), mid=0.22),  # 15:59:59 ET: the answer
        quote(ts_utc=utc(d.year, d.month, d.day, h, 0, 0), mid=0.90),  # exactly 16:00 ET: excluded
        quote(ts_utc=utc(d.year, d.month, d.day, h, 0, 1), mid=0.91),
        quote(ts_utc=utc(d.year, d.month, d.day, 23, 30), mid=0.92),  # same UTC date, after the close
    ]
    chosen = pm("alignment").daily_value(list(reversed(rows)), d)
    assert chosen.mid == pytest.approx(0.22)


def test_daily_alignment_uses_mid_not_last():
    d = date(2026, 11, 12)
    rows = [
        quote(ts_utc=utc(2026, 11, 12, 20, 0), mid=0.30, last=0.30),
        quote(ts_utc=utc(2026, 11, 12, 20, 50), mid=None, last=0.95),  # trade print, no two-sided quote
    ]
    chosen = pm("alignment").daily_value(rows, d)
    assert chosen.mid == pytest.approx(0.30)


def test_daily_alignment_ignores_utc_candle_close_and_previous_evening():
    """A row at 23:59 UTC the day before is not 'before the close' of this session's day."""
    d = date(2026, 11, 12)
    rows = [quote(ts_utc=utc(2026, 11, 12, 4, 59), mid=0.5)]  # 23:59 ET on Nov 11
    assert pm("alignment").daily_value(rows, d) is None


def test_daily_alignment_rejects_naive_timestamps():
    from datetime import datetime

    with pytest.raises(ValueError):
        pm("alignment").daily_value([quote(ts_utc=datetime(2026, 11, 12, 15))], date(2026, 11, 12))
