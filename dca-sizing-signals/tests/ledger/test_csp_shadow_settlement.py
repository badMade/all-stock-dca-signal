"""Shadow entries, settlement and the open-put view (ADR docs/adr-csp-shadow-log.md, decision 3).

Entries are taken at the BID, an early close is costed at the ASK, never the mid. Settlement
uses later point-in-time prices; an unknown later price leaves the trade unscored, never
assumed. Synthetic numbers only (csp_rows.py); the arithmetic is next to each assertion.

The default shadow trade used throughout: sell the ZZZA 2026-04-02 $45 put on 2026-03-02 at
the $0.95 bid. Premium = 0.95 * 100 = $95.00; collateral = 45 * 100 = $4,500.
"""

from __future__ import annotations

from datetime import date, datetime, timedelta

import pytest

from .csp_rows import AS_OF, ET, EXPIRY, SESSION, account, chain_row, select, utc


def _entry(**chain_overrides):
    from market_data_ingest.ledger.csp_shadow import entry_from_suggestion

    return entry_from_suggestion(select([chain_row(**chain_overrides)]).suggestions[0])


def _price(close, quote_date=EXPIRY, ticker="ZZZA"):
    return {"ticker": ticker, "quote_date": quote_date, "close": close}


AFTER_EXPIRY = datetime(2026, 4, 3, 18, 0, tzinfo=ET)


def _settle(entries, prices, as_of=AFTER_EXPIRY, **kw):
    from market_data_ingest.ledger.csp_shadow import settle

    return settle(entries, prices, as_of, **kw)


# --- entry at the bid ----------------------------------------------------------------------


def test_entry_is_taken_at_the_bid_never_the_mid_or_ask():
    e = _entry()
    assert e.entry_price == 0.95  # the bid; mid would be 0.99, ask 1.03
    assert e.premium_dollars == pytest.approx(95.0)  # 0.95 * 100
    assert e.collateral == pytest.approx(4_500.0)
    assert e.entry_price != e.mid and e.entry_price != e.ask


# --- settlement at expiry ------------------------------------------------------------------


def test_out_of_the_money_at_expiry_expires_and_keeps_the_premium():
    (s,) = _settle([_entry()], [_price(46.0)])  # close 46 >= strike 45
    assert s.status == "expired_otm"
    assert s.assigned is False
    assert s.settled_on == EXPIRY
    assert s.pnl == pytest.approx(95.0)
    assert s.return_on_collateral == pytest.approx(95.0 / 4_500.0)  # 2.11%
    assert s.days_held == 31


def test_close_exactly_at_the_strike_is_not_assigned():
    # Auto-exercise needs the option at least $0.01 in the money; at the strike it is not.
    (s,) = _settle([_entry()], [_price(45.0)])
    assert s.status == "expired_otm"


def test_in_the_money_at_expiry_is_assigned_at_the_strike():
    (s,) = _settle([_entry()], [_price(42.0)])
    assert s.status == "assigned"
    assert s.assigned is True
    assert s.effective_cost_basis == pytest.approx(44.05)  # strike 45 - premium 0.95
    # Marked at the expiry close: (42.00 - 44.05) * 100 = -$205.00
    assert s.pnl == pytest.approx(-205.0)
    assert s.return_on_collateral == pytest.approx(-205.0 / 4_500.0)


def test_string_later_price_is_cast():
    (s,) = _settle([_entry()], [_price("46.00")])
    assert s.status == "expired_otm"


# --- early close at the ask ----------------------------------------------------------------


def test_early_close_is_costed_at_the_ask():
    e = _entry()
    # Two weeks in, the put is quoted 0.30 bid / 0.40 ask. Buying it back costs the ASK:
    # P&L = (0.95 - 0.40) * 100 = $55.00 (the mid, 0.35, would have said $60).
    close = {"entry_id": e.entry_id, "bid": 0.30, "ask": "0.40",
             "quote_at": datetime(2026, 3, 16, 15, 0, tzinfo=ET)}
    (s,) = _settle([e], [], as_of=datetime(2026, 3, 16, 16, 0, tzinfo=ET), early_closes=[close])
    assert s.status == "closed_early"
    assert s.exit_ask == 0.40
    assert s.pnl == pytest.approx(55.0)
    assert s.settled_on == date(2026, 3, 16)
    assert s.days_held == 14


def test_early_close_quoted_after_as_of_is_not_used():
    e = _entry()
    close = {"entry_id": e.entry_id, "bid": 0.30, "ask": 0.40,
             "quote_at": datetime(2026, 3, 17, 15, 0, tzinfo=ET)}
    (s,) = _settle([e], [], as_of=datetime(2026, 3, 16, 16, 0, tzinfo=ET), early_closes=[close])
    assert s.status == "open"


def test_non_finite_early_close_ask_is_unscored_not_guessed():
    e = _entry()
    close = {"entry_id": e.entry_id, "bid": 0.30, "ask": "nan",
             "quote_at": datetime(2026, 3, 16, 15, 0, tzinfo=ET)}
    (s,) = _settle([e], [], as_of=datetime(2026, 3, 16, 16, 0, tzinfo=ET), early_closes=[close])
    assert s.status == "unscored"
    assert s.pnl is None


# --- unknown later prices -------------------------------------------------------------------


def test_missing_expiry_price_is_unscored_never_assumed():
    (s,) = _settle([_entry()], [])
    assert s.status == "unscored"
    assert s.unscored_reason == "no_expiry_price"
    assert s.pnl is None and s.return_on_collateral is None


def test_a_price_from_before_expiry_is_not_used_as_the_expiry_price():
    (s,) = _settle([_entry()], [_price(46.0, quote_date=EXPIRY - timedelta(days=1))])
    assert s.status == "unscored"


@pytest.mark.parametrize("bad", ["nan", "inf", "", None, "x"])
def test_unparseable_expiry_price_is_unscored(bad):
    (s,) = _settle([_entry()], [_price(bad)])
    assert s.status == "unscored"


def test_before_expiry_the_trade_is_open_and_a_future_price_is_invisible():
    # as_of 2026-03-20: the expiry bar (dated 04-02) is after the as_of session, so even if a
    # caller passes it, it must not settle the trade (lookahead).
    (s,) = _settle([_entry()], [_price(46.0)], as_of=datetime(2026, 3, 20, 16, tzinfo=ET))
    assert s.status == "open"
    assert s.pnl is None


# --- early-assignment risk flags ------------------------------------------------------------


def test_ex_dividend_inside_the_holding_window_is_flagged():
    (s,) = _settle([_entry()], [_price(46.0)], ex_dividend_dates={"ZZZA": [date(2026, 3, 13)]})
    assert "ex_dividend_in_window" in s.flags


def test_ex_dividend_outside_the_window_is_not_flagged():
    (s,) = _settle([_entry()], [_price(46.0)], ex_dividend_dates={"ZZZA": [date(2026, 4, 10)]})
    assert "ex_dividend_in_window" not in s.flags


def test_deep_in_the_money_move_before_expiry_is_flagged():
    # Deep ITM default: underlying more than 10% below the strike -> below 45 * 0.90 = 40.50.
    prices = [_price(40.0, quote_date=date(2026, 3, 20)), _price(46.0)]
    (s,) = _settle([_entry()], prices)
    assert "deep_itm" in s.flags
    assert s.status == "expired_otm"  # the flag records the risk; settlement is still at expiry


def test_assignment_inside_a_tax_loss_pause_is_flagged():
    (s,) = _settle([_entry()], [_price(42.0)], paused_tickers=frozenset({"ZZZA"}), account=account())
    assert "assigned_in_tax_loss_pause" in s.flags


def test_assignment_that_would_breach_the_sector_cap_is_flagged():
    # Holdings $28,000 + $4,500 assigned = $32,500 of $100,000 = 32.5% > 30%.
    (s,) = _settle([_entry()], [_price(42.0)], account=account(holdings={"synthetic-a": 28_000.0}))
    assert "assignment_breaches_sector_cap" in s.flags


def test_assignment_with_no_account_context_is_flagged_unchecked():
    (s,) = _settle([_entry()], [_price(42.0)])
    assert "concentration_unchecked" in s.flags


# --- persistence and the open-put view ------------------------------------------------------


def test_schema_is_idempotent_and_carries_provenance(ledger):
    from market_data_ingest.ledger.csp_shadow import CSP_TABLES, ensure_csp_schema

    ensure_csp_schema(ledger.con)
    ensure_csp_schema(ledger.con)
    for table in CSP_TABLES:
        cols = set(ledger.table_columns(table))
        assert {"content_hash", "source", "as_of", "ingested_at", "run_id", "inputs_known_at"} <= cols


def test_record_entries_stores_the_bid_and_is_idempotent(ledger):
    from market_data_ingest.ledger.csp_shadow import ensure_csp_schema, record_shadow_entries, shadow_entries

    ensure_csp_schema(ledger.con)
    sugg = select([chain_row()]).suggestions
    assert record_shadow_entries(ledger.con, sugg, run_id="csp-1", as_of=AS_OF) == 1
    assert record_shadow_entries(ledger.con, sugg, run_id="csp-2", as_of=AS_OF + timedelta(hours=1)) == 0
    (row,) = shadow_entries(ledger.con)
    assert row["entry_price"] == 0.95 and row["premium_dollars"] == pytest.approx(95.0)
    assert row["source"] == "csp_shadow"


def test_record_entries_refuses_an_as_of_earlier_than_the_quotes(ledger):
    from market_data_ingest.ledger.csp_shadow import ensure_csp_schema, record_shadow_entries
    from market_data_ingest.ledger.errors import LedgerValidationError

    ensure_csp_schema(ledger.con)
    sugg = select([chain_row()]).suggestions
    with pytest.raises(LedgerValidationError):
        record_shadow_entries(ledger.con, sugg, run_id="csp-1", as_of=AS_OF - timedelta(hours=1))


def test_open_short_puts_and_tax_loss_blocking(ledger):
    from market_data_ingest.ledger.csp_shadow import (
        ensure_csp_schema,
        open_short_puts,
        record_settlements,
        record_shadow_entries,
        settle,
        tax_loss_blocked_tickers,
    )

    ensure_csp_schema(ledger.con)
    record_shadow_entries(ledger.con, select([chain_row()]).suggestions, run_id="csp-1", as_of=AS_OF)
    # Known only after it was recorded: invisible to a reader asking about an earlier moment.
    assert open_short_puts(ledger.con, AS_OF - timedelta(minutes=1)) == []
    assert [p["ticker"] for p in open_short_puts(ledger.con, datetime(2026, 3, 20, 12, tzinfo=ET))] == ["ZZZA"]
    # Expiry day itself is still open (assignment happens after the close).
    assert tax_loss_blocked_tickers(ledger.con, datetime(2026, 4, 2, 12, tzinfo=ET)) == {"ZZZA"}
    # The day after expiry it is no longer open.
    assert open_short_puts(ledger.con, AFTER_EXPIRY) == []

    # An early close recorded on 03-16 ends it from that session on.
    entry = _entry()
    close = {"entry_id": entry.entry_id, "bid": 0.30, "ask": 0.40,
             "quote_at": datetime(2026, 3, 16, 15, 0, tzinfo=ET)}
    when = datetime(2026, 3, 16, 16, 0, tzinfo=ET)
    settled = settle([entry], [], when, early_closes=[close])
    assert record_settlements(ledger.con, settled, run_id="settle-1", as_of=when) == 1
    assert open_short_puts(ledger.con, datetime(2026, 3, 13, 12, tzinfo=ET), known_at=when) != []
    assert open_short_puts(ledger.con, datetime(2026, 3, 17, 12, tzinfo=ET)) == []


def test_record_settlements_skips_open_rows_and_keeps_unscored(ledger):
    from market_data_ingest.ledger.csp_shadow import ensure_csp_schema, record_settlements, shadow_settlements

    ensure_csp_schema(ledger.con)
    open_row = _settle([_entry()], [], as_of=datetime(2026, 3, 20, 16, tzinfo=ET))
    assert record_settlements(ledger.con, open_row, run_id="s1", as_of=utc(2026, 3, 20, 21)) == 0
    unscored = _settle([_entry()], [])
    assert record_settlements(ledger.con, unscored, run_id="s2", as_of=AFTER_EXPIRY) == 1
    (row,) = shadow_settlements(ledger.con)
    assert row["status"] == "unscored" and row["pnl"] is None


def test_session_constant_matches_the_fixture():
    assert SESSION == date(2026, 3, 2)
