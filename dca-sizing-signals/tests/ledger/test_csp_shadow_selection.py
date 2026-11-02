"""Candidate selection for the cash-secured put shadow log (ADR docs/adr-csp-shadow-log.md).

Decision 2 (candidates), decision 6 (tax-loss pause), decision 7 (fail closed) and open
question 4 (collateral limits). Every number is synthetic (see csp_rows.py) and the arithmetic
behind each expected value is written next to the assertion.
"""

from __future__ import annotations

import dataclasses
from datetime import date, datetime, timedelta

import pytest

from .csp_rows import AS_OF, ET, EXPIRY, QUOTE_AT, SESSION, account, chain_row, select, underlying


def _tickers(result) -> list[str]:
    return [s.ticker for s in result.suggestions]


def _reasons(result, ticker: str) -> set[str]:
    return {reason for t, reason in result.dropped if t == ticker}


# --- the happy path ------------------------------------------------------------------------


def test_default_contract_becomes_one_suggestion_with_hand_computed_fields():
    result = select([chain_row()])
    assert _tickers(result) == ["ZZZA"]
    s = result.suggestions[0]
    assert s.session == SESSION
    assert s.expiration_date == EXPIRY
    assert s.dte == 31  # 2026-03-02 -> 2026-04-02
    assert s.strike == 45.0
    assert s.bid == 0.95 and s.ask == 1.03
    assert s.mid == pytest.approx(0.99)  # (0.95 + 1.03) / 2
    assert s.spread_pct == pytest.approx(0.08 / 0.99)  # 8.08% of mid
    assert s.collateral == pytest.approx(4_500.0)  # 45 * 100
    assert s.underlying_price == 50.0
    assert s.ladder_buy_price == 48.0
    assert s.delta == -0.22 and s.implied_volatility == 0.35
    assert s.sector == "synthetic-a"
    assert s.account_alias.startswith("alias-")
    assert s.suggestion_id  # content-derived, stable


def test_suggestion_id_is_deterministic():
    a = select([chain_row()]).suggestions[0]
    b = select([chain_row()]).suggestions[0]
    assert a.suggestion_id == b.suggestion_id


def test_calls_are_ignored_not_suggested():
    assert select([chain_row(option_type="call")]).suggestions == ()


# --- filters -------------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("days", "kept"),
    [(24, False), (25, True), (45, True), (46, False)],  # window is 25..45 days inclusive
)
def test_days_to_expiry_window(days, kept):
    result = select([chain_row(expiration_date=SESSION + timedelta(days=days))])
    assert bool(result.suggestions) is kept


@pytest.mark.parametrize(
    ("delta", "kept"),
    # Window is -0.30 <= delta <= -0.15 inclusive. A positive delta is a call-shaped value on
    # a row labelled put: refused, not reinterpreted.
    [(-0.31, False), (-0.30, True), (-0.15, True), (-0.14, False), (0.22, False)],
)
def test_delta_window(delta, kept):
    assert bool(select([chain_row(delta=delta)]).suggestions) is kept


@pytest.mark.parametrize(
    ("bid", "ask", "kept"),
    [
        # mid 1.00, spread 0.10 -> exactly 10% of mid: kept (the cap is <=, float noise tolerated).
        (0.95, 1.05, True),
        # mid 1.005, spread 0.11 -> 10.95% of mid: dropped.
        (0.95, 1.06, False),
        # Crossed market (ask below bid): not a usable quote.
        (1.05, 0.95, False),
        # Zero bid: no premium to collect at the bid, so no shadow entry.
        (0.0, 0.05, False),
    ],
)
def test_spread_cap_and_quote_sanity(bid, ask, kept):
    assert bool(select([chain_row(bid=bid, ask=ask)]).suggestions) is kept


@pytest.mark.parametrize(("strike", "kept"), [(48.0, True), (49.0, False)])
def test_strike_must_not_exceed_the_ladder_buy_price(strike, kept):
    # The graded ladder would buy at $48.00. A $49 strike would commit to buying above that.
    # Strike 48 collateral is $4,800 (4.8% of the account), well inside the caps.
    assert bool(select([chain_row(strike=strike)]).suggestions) is kept


def test_ticker_without_a_ladder_price_gets_nothing():
    result = select([chain_row()], ladder={"ZZZB": 48.0})
    assert result.suggestions == ()
    assert "no_ladder_price" in _reasons(result, "ZZZA")


def test_inactive_ticker_gets_nothing():
    result = select([chain_row()], active=("ZZZB",))
    assert result.suggestions == ()


def test_one_suggestion_per_ticker_picks_the_best_annualised_bid_yield():
    # A: bid 0.95 / strike 45 over 31 days  -> 0.95/45 * 365/31 = 24.86% a year
    # B: bid 0.80 / strike 40 over 31 days  -> 0.80/40 * 365/31 = 23.55% a year
    #    (ask 0.86, mid 0.83, spread 7.2%: passes every filter on its own)
    rows = [chain_row(strike=40.0, bid=0.80, ask=0.86), chain_row()]
    result = select(rows)
    assert [(s.ticker, s.strike) for s in result.suggestions] == [("ZZZA", 45.0)]


# --- string numerics -----------------------------------------------------------------------


def test_string_numerics_are_cast():
    row = chain_row(strike="45.00", bid=" 0.95", ask="1.03", delta="-0.22", implied_volatility="0.35")
    result = select([row], quotes={"ZZZA": underlying(price="50.00")}, ladder={"ZZZA": "48"})
    s = result.suggestions[0]
    assert (s.strike, s.bid, s.ask, s.delta, s.underlying_price, s.ladder_buy_price) == (
        45.0, 0.95, 1.03, -0.22, 50.0, 48.0)
    assert isinstance(s.strike, float)


def test_iso_string_dates_and_timestamps_are_parsed():
    row = chain_row(expiration_date="2026-04-02", quote_at="2026-03-02T19:55:00+00:00")  # 14:55 ET
    assert _tickers(select([row])) == ["ZZZA"]


@pytest.mark.parametrize("bad", ["nan", "NaN", "inf", "-inf", "", "abc", None, True, float("nan"), float("inf")])
def test_non_finite_or_unparseable_numerics_drop_the_row(bad):
    result = select([chain_row(bid=bad)])
    assert result.suggestions == ()
    assert "invalid_row" in _reasons(result, "ZZZA")


def test_non_finite_underlying_price_drops_the_ticker():
    result = select([chain_row()], quotes={"ZZZA": underlying(price="inf")})
    assert result.suggestions == ()


# --- stale and missing quotes (fail closed) ---------------------------------------------------


def test_option_quote_from_an_earlier_session_is_dropped():
    friday = datetime(2026, 2, 27, 15, 55, tzinfo=ET)  # the session before 2026-03-02
    result = select([chain_row(quote_at=friday)])
    assert result.suggestions == ()
    assert "stale_option_quote" in _reasons(result, "ZZZA")


def test_option_quote_after_as_of_is_dropped_as_lookahead():
    later = AS_OF + timedelta(minutes=1)
    assert select([chain_row(quote_at=later)]).suggestions == ()


def test_naive_quote_timestamp_is_dropped():
    assert select([chain_row(quote_at=datetime(2026, 3, 2, 14, 55))]).suggestions == ()


def test_stale_underlying_quote_drops_the_ticker():
    friday = datetime(2026, 2, 27, 15, 55, tzinfo=ET)
    result = select([chain_row()], quotes={"ZZZA": underlying(quote_at=friday)})
    assert result.suggestions == ()
    assert "stale_underlying_quote" in _reasons(result, "ZZZA")


def test_missing_underlying_quote_drops_the_ticker_and_never_estimates():
    result = select([chain_row()], quotes={})
    assert result.suggestions == ()
    assert "no_underlying_quote" in _reasons(result, "ZZZA")


def test_duplicate_contract_rows_that_disagree_are_dropped_as_ambiguous():
    rows = [chain_row(bid=0.95), chain_row(bid=0.90)]  # same option_id, different bids
    assert select(rows).suggestions == ()


def test_garbage_input_never_raises():
    rows = [None, 7, "row", {"ticker": "ZZZA"}, chain_row(expiration_date="not-a-date")]
    result = select(rows)
    assert result.suggestions == ()


# --- tax-loss pause and open shadow puts ---------------------------------------------------


def test_ticker_in_tax_loss_pause_never_receives_a_suggestion():
    result = select([chain_row(), chain_row("ZZZB")], paused=("ZZZA",))
    assert _tickers(result) == ["ZZZB"]
    assert "tax_loss_pause" in _reasons(result, "ZZZA")


def test_ticker_with_an_open_shadow_put_gets_no_second_one():
    first = select([chain_row()]).suggestions[0]
    from market_data_ingest.ledger.csp_shadow import entry_from_suggestion

    result = select([chain_row()], open_positions=[entry_from_suggestion(first)])
    assert result.suggestions == ()
    assert "open_shadow_put" in _reasons(result, "ZZZA")


# --- collateral and sector caps --------------------------------------------------------------


def test_per_contract_collateral_cap_is_ten_percent_of_the_account():
    # $40,000 account -> per-contract cap $4,000. Strike 45 locks $4,500 (11.25%): dropped.
    # Strike 40 locks exactly $4,000 (10%): kept.
    small = account(value=40_000.0, holdings={"synthetic-a": 1_000.0})
    assert select([chain_row()], acct=small).suggestions == ()
    kept = select([chain_row(strike=40.0, bid=0.80, ask=0.86)], acct=small)
    assert [s.collateral for s in kept.suggestions] == [pytest.approx(4_000.0)]


def test_total_open_shadow_collateral_cap_is_twenty_five_percent():
    from market_data_ingest.ledger.csp_shadow import entry_from_suggestion

    # An open shadow book of $20,000: four $5,000 puts (strike 50 * 100) on other names.
    # Each: bid 1.00, ask 1.08, mid 1.04, spread 7.7%; ladder 52 >= strike 50.
    opens = []
    for name in ("ZZZD", "ZZZE", "ZZZF", "ZZZG"):
        s = select([chain_row(name, strike=50.0, bid=1.00, ask=1.08)], active=(name,),
                   quotes={name: underlying(price=55.0)}, ladder={name: 52.0},
                   acct=account(sectors={name: "synthetic-z"}, holdings={})).suggestions[0]
        opens.append(entry_from_suggestion(s))
    assert sum(o.collateral for o in opens) == pytest.approx(20_000.0)
    # Cap is $25,000. ZZZA (yield 0.95/45, higher) is taken first: 20,000 + 4,500 = 24,500 <= 25,000.
    # ZZZB (bid 0.90, ask 0.98, mid 0.94, spread 8.5%; yield 0.90/45, lower) would make 29,000: dropped.
    rows = [chain_row(), chain_row("ZZZB", bid=0.90, ask=0.98)]
    result = select(rows, open_positions=opens)
    assert _tickers(result) == ["ZZZA"]
    assert "total_collateral_cap" in _reasons(result, "ZZZB")


def test_open_positions_that_have_expired_do_not_count_toward_the_total_cap():
    from market_data_ingest.ledger.csp_shadow import entry_from_suggestion

    old = select([chain_row("ZZZD", strike=48.0)], active=("ZZZD",), ladder={"ZZZD": 48.0},
                 quotes={"ZZZD": underlying()}, acct=account(sectors={"ZZZD": "synthetic-z"}, holdings={}))
    entry = entry_from_suggestion(old.suggestions[0])
    expired = dataclasses.replace(entry, expiration_date=date(2026, 2, 27))
    # Expired before the session: neither blocks ZZZD by "open put" nor locks collateral.
    assert _tickers(select([chain_row()], open_positions=[expired] * 10)) == ["ZZZA"]


@pytest.mark.parametrize(("held", "kept"), [(30_000.0, True), (31_000.0, False)])
def test_no_candidates_in_a_sector_already_above_thirty_percent(held, kept):
    # $30,000 of $100,000 is exactly 30%: not ABOVE 30%, kept. $31,000 (31%) is above: dropped.
    acct = account(holdings={"synthetic-a": held})
    result = select([chain_row()], acct=acct)
    assert bool(result.suggestions) is kept
    if not kept:
        assert "sector_cap" in _reasons(result, "ZZZA")


def test_ticker_with_no_known_sector_is_dropped():
    acct = account(sectors={"ZZZB": "synthetic-b"})
    result = select([chain_row()], acct=acct)
    assert result.suggestions == ()
    assert "no_sector" in _reasons(result, "ZZZA")


def test_without_an_account_context_nothing_is_suggested():
    # The caps cannot be checked without the account value, so fail closed.
    assert select([chain_row()], acct=None).suggestions == ()


def test_account_context_refuses_a_raw_identifier():
    from market_data_ingest.ledger.csp_shadow import AccountContext
    from market_data_ingest.ledger.errors import AccountIdentifierError

    with pytest.raises(AccountIdentifierError):
        AccountContext(account_alias="not-an-alias", account_value=1.0, sector_by_ticker={},
                       sector_value_by_sector={})


def test_naive_as_of_is_refused():
    from market_data_ingest.ledger.errors import LedgerValidationError

    with pytest.raises(LedgerValidationError):
        select([chain_row()], as_of=datetime(2026, 3, 2, 15))


def test_quote_at_default_is_the_selection_session():
    # Guard against a fixture drift that would make every other test pass vacuously.
    assert QUOTE_AT.astimezone(ET).date() == SESSION
