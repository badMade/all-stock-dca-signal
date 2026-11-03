"""Scorecard and pass bar for the cash-secured put shadow log (ADR docs/adr-csp-shadow-log.md,
decisions 4 and 5). Synthetic settlements only; arithmetic next to each assertion.

The pass bar can only be reached after at least 6 months AND at least 20 settled trades; until
then ``passes`` is False whatever the other numbers say.
"""

from __future__ import annotations

import math
from datetime import date, datetime, timedelta

import pytest

from .csp_rows import ET


def _settled(i: int, entry_session: date, *, pnl: float = 95.0, status: str = "expired_otm", days: int = 31,
             collateral: float = 4_500.0, premium: float = 95.0, flags=()):
    return {
        "entry_id": f"e{i:03d}",
        "ticker": "ZZZA",
        "entry_session": entry_session,
        "expiration_date": entry_session + timedelta(days=days),
        "settled_on": entry_session + timedelta(days=days),
        "status": status,
        "assigned": status == "assigned",
        "pnl": pnl if status != "unscored" else None,
        "collateral": collateral,
        "premium_dollars": premium,
        "days_held": days,
        "flags": tuple(flags),
    }


def _card(settled, *, as_of, dca=(), apy=0.036):
    from market_data_ingest.ledger.csp_shadow import scorecard

    return scorecard(settled, apy, dca, as_of=as_of)


def test_one_trade_vs_the_cash_sweep_hand_computed():
    s = [_settled(1, date(2026, 3, 2))]
    card = _card(s, as_of=datetime(2026, 4, 3, 18, tzinfo=ET))
    # Sweep on the same $4,500 for 31 days at 3.60% APY:
    #   4500 * (1.036 ** (31/365) - 1) = 4500 * 0.0030083 = $13.54
    sweep = 4_500.0 * (1.036 ** (31 / 365) - 1)
    assert sweep == pytest.approx(13.537, abs=0.01)
    assert card.sweep_pnl == pytest.approx(sweep)
    assert card.net_pnl == pytest.approx(95.0)
    assert card.excess_vs_sweep == pytest.approx(95.0 - sweep)
    # Annualised return on locked collateral: 95 / (4500 * 31/365) = 24.86% a year.
    assert card.annualised_return_on_collateral == pytest.approx(95.0 / (4_500.0 * 31 / 365))
    assert card.beats_sweep is True
    assert card.n_settled == 1 and card.n_assigned == 0


def test_vs_plain_dca_uses_the_benchmark_rows():
    s = [_settled(1, date(2026, 3, 2))]
    # Plain DCA of the same $4,500 in ZZZA over the same 31 days returned +1% -> $45.
    card = _card(s, as_of=datetime(2026, 4, 3, tzinfo=ET), dca=[{"entry_id": "e001", "dca_return_pct": "0.01"}])
    assert card.dca_pnl == pytest.approx(45.0)
    assert card.excess_vs_dca == pytest.approx(50.0)  # 95 - 45
    assert card.beats_dca is True


def test_missing_dca_benchmark_leaves_the_comparison_unknown():
    card = _card([_settled(1, date(2026, 3, 2))], as_of=datetime(2026, 4, 3, tzinfo=ET))
    assert card.dca_pnl is None and card.beats_dca is None


def test_worst_loss_vs_trailing_twelve_months_premium_and_assignment_count():
    s = [
        _settled(1, date(2026, 3, 2)),  # +95
        _settled(2, date(2026, 4, 6), status="assigned", pnl=-205.0),  # -205, premium 95
    ]
    card = _card(s, as_of=datetime(2026, 6, 1, tzinfo=ET))
    assert card.worst_trade_loss == pytest.approx(205.0)
    assert card.trailing_12m_premium == pytest.approx(190.0)  # 95 + 95
    assert card.worst_loss_below_premium is False  # 205 is not smaller than 190
    assert card.n_assigned == 1
    assert card.net_pnl == pytest.approx(-110.0)


def test_premium_older_than_twelve_months_is_not_counted():
    s = [_settled(1, date(2025, 3, 2)), _settled(2, date(2026, 3, 2))]
    card = _card(s, as_of=datetime(2026, 4, 3, tzinfo=ET))
    assert card.trailing_12m_premium == pytest.approx(95.0)  # only the 2026 entry


def test_unscored_and_open_rows_are_not_settled_trades():
    s = [_settled(1, date(2026, 3, 2)), _settled(2, date(2026, 3, 9), status="unscored"),
         _settled(3, date(2026, 3, 9), status="open")]
    card = _card(s, as_of=datetime(2026, 4, 30, tzinfo=ET))
    assert card.n_settled == 1
    assert card.n_unscored == 1


# --- the pass bar -------------------------------------------------------------------------


def _book(n: int, spacing_days: int):
    """``n`` winning trades, one every ``spacing_days`` from 2026-01-05, each +$95 on $4,500."""
    start = date(2026, 1, 5)
    rows = [_settled(i, start + timedelta(days=i * spacing_days)) for i in range(n)]
    dca = [{"entry_id": r["entry_id"], "dca_return_pct": 0.0} for r in rows]
    return rows, dca


def test_twenty_trades_over_six_months_can_pass():
    # Spacing 9 days: last entry 2026-01-05 + 19*9 = 2026-06-25, settled 31 days later on
    # 2026-07-26, which is on/after 2026-07-05 (first entry + 6 months).
    rows, dca = _book(20, 9)
    card = _card(rows, as_of=datetime(2026, 7, 27, tzinfo=ET), dca=dca)
    assert card.has_min_months is True
    assert card.has_min_trades is True
    assert card.beats_sweep and card.worst_loss_below_premium and card.drawdown_ok and card.no_breaches
    assert card.passes is True


def test_nineteen_trades_never_pass():
    rows, dca = _book(19, 10)  # long enough in time, one trade short
    card = _card(rows, as_of=datetime(2026, 8, 30, tzinfo=ET), dca=dca)
    assert card.has_min_months is True
    assert card.has_min_trades is False
    assert card.passes is False


def test_twenty_trades_in_under_six_months_never_pass():
    # Spacing 5 days: last entry 2026-01-05 + 95 = 2026-04-10, settled 2026-05-11 < 2026-07-05.
    rows, dca = _book(20, 5)
    card = _card(rows, as_of=datetime(2026, 5, 12, tzinfo=ET), dca=dca)
    assert card.has_min_trades is True
    assert card.has_min_months is False
    assert card.beats_sweep is True  # good numbers do not override the minimums
    assert card.passes is False


def test_a_breach_flag_fails_the_bar():
    rows, dca = _book(20, 9)
    rows[3] = {**rows[3], "status": "assigned", "assigned": True, "pnl": 10.0,
               "flags": ("assignment_breaches_sector_cap",)}
    card = _card(rows, as_of=datetime(2026, 7, 27, tzinfo=ET), dca=dca)
    assert card.no_breaches is False
    assert card.passes is False


def test_unchecked_concentration_fails_the_bar_closed():
    rows, dca = _book(20, 9)
    rows[0] = {**rows[0], "status": "assigned", "assigned": True, "flags": ("concentration_unchecked",)}
    assert _card(rows, as_of=datetime(2026, 7, 27, tzinfo=ET), dca=dca).no_breaches is False


def test_missing_benchmark_fails_the_drawdown_bar_closed():
    rows, _ = _book(20, 9)
    card = _card(rows, as_of=datetime(2026, 7, 27, tzinfo=ET), dca=())
    assert card.drawdown_ok is False
    assert card.passes is False


def test_drawdown_compares_the_overlay_with_the_benchmark():
    # Overlay cumulative P&L: +95, -110 (after -205), -15 -> peak 95, trough -110: drawdown $205.
    # Benchmark (plain DCA, 4,500 * return): +45, -405 (after -450), -360 -> drawdown $450.
    rows = [_settled(1, date(2026, 3, 2)), _settled(2, date(2026, 4, 6), status="assigned", pnl=-205.0),
            _settled(3, date(2026, 5, 11))]
    dca = [{"entry_id": "e001", "dca_return_pct": 0.01}, {"entry_id": "e002", "dca_return_pct": -0.10},
           {"entry_id": "e003", "dca_return_pct": 0.01}]
    card = _card(rows, as_of=datetime(2026, 7, 1, tzinfo=ET), dca=dca)
    assert card.overlay_max_drawdown == pytest.approx(205.0)
    assert card.benchmark_max_drawdown == pytest.approx(450.0)
    assert card.drawdown_ok is True


def test_empty_book_is_all_false_not_an_error():
    card = _card([], as_of=datetime(2026, 7, 1, tzinfo=ET))
    assert card.n_settled == 0 and card.passes is False and card.beats_sweep is False
    assert card.annualised_return_on_collateral is None
    assert not math.isnan(card.net_pnl)
