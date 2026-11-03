"""Regression tests for the independent review of PR #2105 (CSP shadow log).

1. A later ``unscored`` settlement must never supersede an earlier scored one (a feed outage
   would otherwise drop an assigned loser from the scorecard).
2. ``scorecard(as_of)`` must not count a trade settled after the as_of session (lookahead).
3. ``passes`` requires zero unscored trades: missing-price trades are often the losers, so
   letting them drop out silently biases toward a pass.
4. Several early-close quotes for one entry: the EARLIEST buy-back quote is used, whatever the
   row order; two quotes at the same moment that disagree are ambiguous -> unscored.
5. Nits: early-assignment flags are counted in the scorecard; ``deep_itm`` ignores the as_of
   session's bar before the 16:00 ET close; a sector missing from ``sector_value_by_sector``
   fails closed; sector exposure accumulates across picks in one run (and open shadow puts);
   ``paused_tickers=None`` means "pause list unknown" -> no candidates.

Synthetic numbers only (csp_rows.py). Default trade: ZZZA 2026-04-02 $45 put sold 2026-03-02
at the $0.95 bid; premium $95, collateral $4,500.
"""

from __future__ import annotations

from datetime import date, datetime, timedelta

import pytest

from .csp_rows import ET, EXPIRY, account, chain_row, select

AFTER_EXPIRY = datetime(2026, 4, 3, 18, 0, tzinfo=ET)


def _entry():
    from market_data_ingest.ledger.csp_shadow import entry_from_suggestion

    return entry_from_suggestion(select([chain_row()]).suggestions[0])


def _price(close, quote_date=EXPIRY, ticker="ZZZA"):
    return {"ticker": ticker, "quote_date": quote_date, "close": close}


def _reasons(result, ticker):
    return {r for t, r in result.dropped if t == ticker}


# --- 1. scored settlements are terminal ------------------------------------------------------


def test_later_unscored_settlement_never_supersedes_a_scored_one(ledger):
    from market_data_ingest.ledger.csp_shadow import ensure_csp_schema, record_settlements, settle, shadow_settlements

    ensure_csp_schema(ledger.con)
    e = _entry()
    # 04-03: the expiry close (42.00) is known -> assigned, (42.00 - 44.05) * 100 = -$205.
    scored = settle([e], [_price(42.0)], AFTER_EXPIRY)
    assert record_settlements(ledger.con, scored, run_id="s1", as_of=AFTER_EXPIRY) == 1
    # 04-10: a re-run during a price-feed outage sees no expiry bar -> unscored, newer inputs.
    later = datetime(2026, 4, 10, 18, 0, tzinfo=ET)
    outage = settle([e], [], later)
    assert outage[0].status == "unscored"
    record_settlements(ledger.con, outage, run_id="s2", as_of=later)
    (row,) = shadow_settlements(ledger.con)
    assert row["status"] == "assigned"
    assert row["pnl"] == pytest.approx(-205.0)


def test_a_later_scored_settlement_still_replaces_an_unscored_one(ledger):
    from market_data_ingest.ledger.csp_shadow import ensure_csp_schema, record_settlements, settle, shadow_settlements

    ensure_csp_schema(ledger.con)
    e = _entry()
    record_settlements(ledger.con, settle([e], [], AFTER_EXPIRY), run_id="s1", as_of=AFTER_EXPIRY)
    later = datetime(2026, 4, 10, 18, 0, tzinfo=ET)
    record_settlements(ledger.con, settle([e], [_price(46.0)], later), run_id="s2", as_of=later)
    (row,) = shadow_settlements(ledger.con)
    assert row["status"] == "expired_otm"


# --- 2 and 3. scorecard lookahead and unscored trades ---------------------------------------


def _settled(i, start, *, status="expired_otm", pnl=95.0, days=31, flags=()):
    return {"entry_id": f"e{i:03d}", "ticker": "ZZZA", "entry_session": start,
            "expiration_date": start + timedelta(days=days), "settled_on": start + timedelta(days=days),
            "status": status, "assigned": status == "assigned", "pnl": None if status == "unscored" else pnl,
            "collateral": 4_500.0, "premium_dollars": 95.0, "days_held": days, "flags": tuple(flags)}


def _card(rows, as_of, dca=()):
    from market_data_ingest.ledger.csp_shadow import scorecard

    return scorecard(rows, 0.036, dca, as_of=as_of)


def test_scorecard_ignores_trades_settled_after_the_as_of_session():
    rows = [_settled(1, date(2026, 3, 2))]  # settles 2026-04-02
    card = _card(rows, datetime(2026, 3, 10, 16, tzinfo=ET))
    assert card.n_settled == 0
    assert card.net_pnl == 0.0
    assert _card(rows, datetime(2026, 4, 2, 17, tzinfo=ET)).n_settled == 1


def _passing_book():
    # 20 winners, one every 9 days from 2026-01-05: last settles 2026-07-26 >= 2026-07-05.
    rows = [_settled(i, date(2026, 1, 5) + timedelta(days=9 * i)) for i in range(20)]
    dca = [{"entry_id": r["entry_id"], "dca_return_pct": 0.0} for r in rows]
    return rows, dca


def test_an_unscored_trade_blocks_the_pass():
    rows, dca = _passing_book()
    assert _card(rows, datetime(2026, 7, 27, tzinfo=ET), dca).passes is True
    rows.append(_settled(99, date(2026, 6, 1), status="unscored"))  # expired 07-02, no price
    card = _card(rows, datetime(2026, 7, 27, tzinfo=ET), dca)
    assert card.n_unscored == 1
    assert card.no_unscored is False
    assert card.passes is False


def test_an_unscored_trade_not_yet_expired_at_as_of_does_not_count():
    rows, dca = _passing_book()
    rows.append(_settled(99, date(2026, 7, 20), status="unscored"))  # would expire 08-20
    card = _card(rows, datetime(2026, 7, 27, tzinfo=ET), dca)
    assert card.n_unscored == 0 and card.passes is True


# --- 4. duplicate early-close quotes --------------------------------------------------------


@pytest.mark.parametrize("order", [0, 1])
def test_earliest_early_close_quote_wins_regardless_of_row_order(order):
    from market_data_ingest.ledger.csp_shadow import settle

    e = _entry()
    rows = [
        {"entry_id": e.entry_id, "bid": 2.90, "ask": 3.00, "quote_at": datetime(2026, 3, 17, 15, tzinfo=ET)},
        {"entry_id": e.entry_id, "bid": 0.05, "ask": 0.10, "quote_at": datetime(2026, 3, 16, 15, tzinfo=ET)},
    ]
    if order:
        rows.reverse()
    (s,) = settle([e], [], datetime(2026, 3, 18, 16, tzinfo=ET), early_closes=rows)
    # Earliest buy-back (03-16 at the 0.10 ask): (0.95 - 0.10) * 100 = $85.00.
    assert s.status == "closed_early"
    assert s.exit_ask == 0.10
    assert s.pnl == pytest.approx(85.0)
    assert s.settled_on == date(2026, 3, 16)


def test_two_disagreeing_early_close_quotes_at_the_same_moment_are_unscored():
    from market_data_ingest.ledger.csp_shadow import settle

    e = _entry()
    at = datetime(2026, 3, 16, 15, tzinfo=ET)
    rows = [{"entry_id": e.entry_id, "bid": 0.30, "ask": 0.40, "quote_at": at},
            {"entry_id": e.entry_id, "bid": 0.30, "ask": 0.45, "quote_at": at}]
    (s,) = settle([e], [], datetime(2026, 3, 18, 16, tzinfo=ET), early_closes=rows)
    assert s.status == "unscored" and s.pnl is None


# --- 5. nits --------------------------------------------------------------------------------


def test_scorecard_counts_early_assignment_risk_flags():
    rows = [_settled(1, date(2026, 3, 2), flags=("ex_dividend_in_window",)),
            _settled(2, date(2026, 3, 9), flags=("deep_itm", "ex_dividend_in_window")),
            _settled(3, date(2026, 3, 16))]
    card = _card(rows, datetime(2026, 6, 1, tzinfo=ET))
    assert card.n_ex_dividend_risk == 2
    assert card.n_deep_itm_risk == 1
    assert card.n_early_assignment_risk == 2  # trades with at least one of the two flags


def test_deep_itm_ignores_the_as_of_sessions_bar_before_the_close():
    from market_data_ingest.ledger.csp_shadow import settle

    e = _entry()
    bar = [_price(40.0, quote_date=date(2026, 3, 20))]  # below 45 * 0.90 = 40.50
    (midday,) = settle([e], bar, datetime(2026, 3, 20, 12, tzinfo=ET))
    assert "deep_itm" not in midday.flags  # a 03-20 close cannot exist at 12:00 ET on 03-20
    (evening,) = settle([e], bar, datetime(2026, 3, 20, 17, tzinfo=ET))
    assert "deep_itm" in evening.flags


def test_sector_missing_from_holdings_fails_closed():
    acct = account(holdings={"synthetic-b": 0.0})  # synthetic-a (ZZZA's sector) absent
    result = select([chain_row()], acct=acct)
    assert result.suggestions == ()
    assert "no_sector_value" in _reasons(result, "ZZZA")


@pytest.mark.parametrize(("held", "zzzb_kept"), [(25_000.0, True), (27_000.0, False)])
def test_sector_exposure_accumulates_across_picks_in_one_run(held, zzzb_kept):
    # ZZZA and ZZZB share synthetic-a. ZZZA (yield 0.95/45) is picked first and adds $4,500.
    #   held 25,000 -> 29,500 (29.5%), not above 30%: ZZZB is still allowed.
    #   held 27,000 -> 31,500 (31.5%), above 30%: ZZZB is dropped.
    acct = account(sectors={"ZZZA": "synthetic-a", "ZZZB": "synthetic-a"}, holdings={"synthetic-a": held})
    result = select([chain_row(), chain_row("ZZZB", bid=0.90, ask=0.98)], acct=acct)
    assert ("ZZZB" in [s.ticker for s in result.suggestions]) is zzzb_kept
    assert "ZZZA" in [s.ticker for s in result.suggestions]
    if not zzzb_kept:
        assert "sector_cap" in _reasons(result, "ZZZB")


def test_open_shadow_puts_count_toward_their_sectors_exposure():
    from market_data_ingest.ledger.csp_shadow import entry_from_suggestion

    # An open $4,800 put on ZZZD in synthetic-a, plus $26,000 held = $30,800 (30.8%): above 30%.
    acct_d = account(sectors={"ZZZD": "synthetic-a"}, holdings={"synthetic-a": 26_000.0})
    opened = select([chain_row("ZZZD", strike=48.0)], active=("ZZZD",), ladder={"ZZZD": 48.0},
                    quotes={"ZZZD": {"price": 50.0, "quote_at": datetime(2026, 3, 2, 14, 55, tzinfo=ET)}},
                    acct=acct_d).suggestions[0]
    acct = account(sectors={"ZZZA": "synthetic-a"}, holdings={"synthetic-a": 26_000.0})
    result = select([chain_row()], acct=acct, open_positions=[entry_from_suggestion(opened)])
    assert result.suggestions == ()
    assert "sector_cap" in _reasons(result, "ZZZA")


def test_unknown_pause_list_means_no_candidates():
    result = select([chain_row()], paused=None)
    assert result.suggestions == ()
    assert ("*", "no_pause_list") in result.dropped
