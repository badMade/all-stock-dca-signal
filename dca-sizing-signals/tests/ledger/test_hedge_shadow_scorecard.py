"""The hedge scorecard judges down days, not just correlation (docs/adr-goals-and-loss-hedge-shadow.md, test 12).

Written FIRST: fails until ``hedge_shadow.down_day_scorecard`` exists.

Why this test exists: in 2026 a long-window BTC to S&P 500 correlation turned negative while
the two still fell together on many of the same days. A scorecard that reported correlation
alone would call that a hedge. This one must report what the hedge did on the days the
equity portfolio fell.

The return series below are SYNTHETIC arithmetic inputs (the ledger conftest's convention), not
market data: six hand-picked daily returns whose results can be checked with pencil and paper.
They test the scorecard's arithmetic, which no real capture could pin as cleanly. Real shadow
history replaces them only as an end-to-end check once the shadow log has run.
"""

from __future__ import annotations

import statistics
from datetime import date

import pytest

hedge_shadow = pytest.importorskip(
    "market_data_ingest.ledger.hedge_shadow",
    reason="loss-hedge shadow implementation is deferred pending ADR approval",
)

DAYS = [date(2026, 3, d) for d in (2, 3, 4, 5, 6, 9)]  # six weekdays (synthetic calendar)
EQUITY = dict(zip(DAYS, [-0.02, -0.01, 0.01, 0.02, -0.03, 0.01], strict=True))
# Looks like a hedge by correlation (about -0.10) but FALLS on all three equity down days.
FALSE_HEDGE = dict(zip(DAYS, [-0.01, -0.02, 0.03, -0.04, -0.01, -0.02], strict=True))
# Rises on every equity down day: what a real hedge looks like.
TRUE_HEDGE = dict(zip(DAYS, [0.01, 0.005, -0.01, -0.01, 0.02, 0.0], strict=True))


def test_12_a_low_correlation_does_not_hide_falling_with_equity():
    card = hedge_shadow.down_day_scorecard(EQUITY, FALSE_HEDGE)
    # Full-period correlation is still reported, and it looks reassuring...
    assert card["correlation"] == pytest.approx(statistics.correlation(
        [EQUITY[d] for d in DAYS], [FALSE_HEDGE[d] for d in DAYS]))
    assert card["correlation"] < 0
    # ...but the down-day facts expose it. Equity fell on Mar 2, 3 and 6.
    assert card["days"] == 6
    assert card["equity_down_days"] == 3
    # The hedge also fell on all 3 of them: 3 / 3 = 1.0.
    assert card["hedge_fell_with_equity_share"] == pytest.approx(1.0)
    # Mean hedge return on those days: (-0.01 - 0.02 - 0.01) / 3 = -0.013333...
    assert card["hedge_mean_on_down_days"] == pytest.approx(-0.04 / 3)


def test_12_a_real_hedge_scores_well_on_down_days():
    card = hedge_shadow.down_day_scorecard(EQUITY, TRUE_HEDGE)
    assert card["equity_down_days"] == 3
    assert card["hedge_fell_with_equity_share"] == pytest.approx(0.0)
    # (0.01 + 0.005 + 0.02) / 3 = 0.011666...
    assert card["hedge_mean_on_down_days"] == pytest.approx(0.035 / 3)


def test_12_only_dates_present_in_both_series_count():
    hedge = {d: r for d, r in TRUE_HEDGE.items() if d != DAYS[0]}  # hedge missing Mar 2
    card = hedge_shadow.down_day_scorecard(EQUITY, hedge)
    assert card["days"] == 5
    assert card["equity_down_days"] == 2  # Mar 3 and Mar 6


def test_12_no_down_days_reports_none_not_zero():
    up = {d: abs(r) + 0.001 for d, r in EQUITY.items()}
    card = hedge_shadow.down_day_scorecard(up, TRUE_HEDGE)
    assert card["equity_down_days"] == 0
    # "No evidence" must not read as "never fell with equity".
    assert card["hedge_fell_with_equity_share"] is None
    assert card["hedge_mean_on_down_days"] is None


def test_12_too_few_days_or_non_finite_returns_give_no_card():
    two = {d: EQUITY[d] for d in DAYS[:2]}
    assert hedge_shadow.down_day_scorecard(two, TRUE_HEDGE) is None
    bad = dict(TRUE_HEDGE)
    bad[DAYS[3]] = float("nan")
    assert hedge_shadow.down_day_scorecard(EQUITY, bad) is None
