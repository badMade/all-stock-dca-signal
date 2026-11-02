"""Research-only tax-loss candidate report and wash-sale eligibility check.

Implements the tests for the PROPOSED ADR docs/adr-tax-loss-sale.md (PR #2099) on the Phase 0
ledger (PR #2101). Nothing under test places, previews or cancels an order: the module only
reads plain rows a caller already fetched and says "eligible" or "not eligible, because ...".

Every row here is SYNTHETIC: fake tickers (``ZZZA``...), round prices, and account aliases
built at runtime from descriptive strings (never digits), so tests/test_config_invariants.py
has nothing to flag. The arithmetic behind each expected value is written next to it.

Window conventions (proposed, conservative; ADR decision 3):

* Pre-sale lookback: any acquisition of the ticker dated ``sale_date - 31`` through
  ``sale_date`` inclusive (0..31 calendar days before) blocks the sale. The statute's window
  is 30 days; day 31 is a deliberate one-day margin, which is why "32 days before" is the
  first clean day in these tests.
* Post-sale pause: ``sale_date`` through ``sale_date + 31`` inclusive; the first buy allowed
  again is ``sale_date + 32``.
"""

from __future__ import annotations

import re
from dataclasses import replace
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

import pytest

from .conftest import TEST_SALT, utc

MODULE = Path(__file__).resolve().parents[2] / "market_data_ingest" / "ledger" / "tax_loss.py"

SALE = date(2026, 12, 1)
AT = utc(2026, 11, 30, 15)  # when the eligibility check runs (approval expiry is judged here)


def _alias(name: str) -> str:
    from market_data_ingest.ledger.accounts import account_alias

    # Descriptive strings, not digits: the alias is an HMAC, so the raw value never matters.
    return account_alias(f"synthetic-{name}", salt=TEST_SALT)


@pytest.fixture
def accts():
    return {"agentic": _alias("agentic"), "roth": _alias("roth-ira"), "managed": _alias("managed-individual")}


def _approval(**overrides):
    from market_data_ingest.ledger.tax_loss import Approval, lot_key

    fields = {
        "approval_id": "tlh_zzza_first",
        "ticker": "ZZZA",
        "lot_keys": (lot_key("lot-zzza-1"), lot_key("lot-zzza-2")),
        "max_shares": 15.0,
        "approved_at": utc(2026, 11, 29, 12),
        "expires_at": utc(2026, 12, 2, 0),
    }
    fields.update(overrides)
    return Approval(**fields)


def _clean(accts):
    """Inputs under which a ZZZA sale on SALE is eligible. Each test breaks exactly one thing."""
    from market_data_ingest.ledger.tax_loss import lot_key

    return {
        "ticker": "ZZZA",
        "sale_date": SALE,
        "acquisitions_by_account": {
            # 2026-10-30 is 32 days before 2026-12-01 (Oct 30 -> Nov 30 is 31, +1): outside.
            accts["agentic"]: [{"symbol": "ZZZA", "acquired_date": date(2026, 10, 30), "quantity": 1.0,
                                "kind": "buy"}],
            # A buy of a DIFFERENT ticker inside the window never blocks ZZZA.
            accts["roth"]: [{"symbol": "ZZZB", "acquired_date": date(2026, 11, 25), "quantity": 1.0, "kind": "buy"}],
            accts["managed"]: [],  # history read and empty: proven clean, not unknown
        },
        "open_option_positions": {
            # A long put and a short call do not acquire shares: neither blocks.
            accts["agentic"]: [
                {"underlying": "ZZZA", "option_type": "put", "side": "long", "quantity": 1.0,
                 "expiration_date": date(2027, 1, 15)},
                {"underlying": "ZZZA", "option_type": "call", "side": "short", "quantity": 1.0,
                 "expiration_date": date(2027, 1, 15)},
            ],
            accts["roth"]: [],
            accts["managed"]: [],
        },
        "drip_status": {accts["agentic"]: False, accts["roth"]: False, accts["managed"]: False},
        "approval": _approval(),
        "lot_keys": (lot_key("lot-zzza-1"),),
        "shares": 10.0,
        "tracked_accounts": tuple(accts.values()),
        "outside_accounts_attested": True,
        "at": AT,
    }


def _check(**kwargs):
    from market_data_ingest.ledger.tax_loss import eligibility

    return eligibility(**kwargs)


# --- eligibility: the happy path and each rule ---------------------------------------------------


def test_clean_inputs_are_eligible(accts):
    result = _check(**_clean(accts))
    assert result.eligible is True
    assert result.reasons == ()
    assert result.washing_acquisitions == ()


def test_ira_buy_ten_days_before_the_sale_blocks(accts):
    # 2026-11-21 is 10 days before 2026-12-01. An IRA purchase makes the loss permanently
    # disallowed (ADR context), so a buy in ANY tracked account blocks, not just the seller's.
    kw = _clean(accts)
    kw["acquisitions_by_account"][accts["roth"]].append(
        {"symbol": "ZZZA", "acquired_date": date(2026, 11, 21), "quantity": 0.5, "kind": "buy"})
    result = _check(**kw)
    assert result.eligible is False
    assert "acquisition_in_lookback" in result.reasons
    assert [(a["account_alias"], a["acquired_date"]) for a in result.washing_acquisitions] == [
        (accts["roth"], date(2026, 11, 21))]
    # Last buy 2026-11-21 -> first clean sale date is 32 days later: Nov 21 + 32 = Dec 23.
    assert result.earliest_clean_sale_date == date(2026, 12, 23)


def test_managed_account_buy_blocks(accts):
    # A Robinhood-managed portfolio buys on its own schedule; it still counts.
    kw = _clean(accts)
    kw["acquisitions_by_account"][accts["managed"]] = [
        {"symbol": "ZZZA", "acquired_date": date(2026, 11, 28), "quantity": 0.1, "kind": "managed_rebalance"}]
    assert "acquisition_in_lookback" in _check(**kw).reasons


def test_buy_thirty_two_days_before_does_not_block_but_thirty_one_does(accts):
    kw = _clean(accts)  # already holds a 32-days-before buy (2026-10-30) and is eligible
    assert _check(**kw).eligible is True
    # 2026-10-31 is 31 days before 2026-12-01: inside the conservative 31-day lookback.
    kw["acquisitions_by_account"][accts["agentic"]].append(
        {"symbol": "ZZZA", "acquired_date": date(2026, 10, 31), "quantity": 1.0, "kind": "buy"})
    assert _check(**kw).eligible is False


def test_buy_on_the_sale_date_blocks(accts):
    kw = _clean(accts)
    kw["acquisitions_by_account"][accts["agentic"]].append(
        {"symbol": "ZZZA", "acquired_date": SALE, "quantity": 1.0, "kind": "buy"})
    assert "acquisition_in_lookback" in _check(**kw).reasons


def test_dividend_reinvestment_counts_as_an_acquisition(accts):
    kw = _clean(accts)
    kw["acquisitions_by_account"][accts["agentic"]].append(
        {"symbol": "zzza", "acquired_date": date(2026, 11, 15), "quantity": 0.01, "kind": "drip"})  # case-folded
    assert "acquisition_in_lookback" in _check(**kw).reasons


def test_same_lot_nuance_conservative_pre_pause_is_intentional(accts):
    # IRS precision (ADR context, review of 2026-11-02): shares bought within 30 days BEFORE
    # the sale do not wash the loss when those same shares are part of the sale. So selling
    # the whole position -- including a lot bought 10 days ago -- is strictly allowed.
    # The ADR chooses the conservative rule anyway: ANY acquisition in the lookback blocks,
    # including the very lot being sold. This test pins that choice so nobody "fixes" it
    # without amending the ADR.
    from market_data_ingest.ledger.tax_loss import lot_key

    kw = _clean(accts)
    recent = lot_key("lot-zzza-2")
    kw["acquisitions_by_account"][accts["agentic"]].append(
        {"symbol": "ZZZA", "acquired_date": date(2026, 11, 21), "quantity": 5.0, "kind": "buy"})
    kw["lot_keys"] = (lot_key("lot-zzza-1"), recent)  # the recent lot IS in the sale
    kw["shares"] = 15.0
    result = _check(**kw)
    assert result.eligible is False
    assert "acquisition_in_lookback" in result.reasons


def test_missing_account_history_blocks(accts):
    kw = _clean(accts)
    del kw["acquisitions_by_account"][accts["managed"]]  # tracked, but no history supplied
    result = _check(**kw)
    assert result.eligible is False
    assert "account_history_missing" in result.reasons


def test_unknown_account_history_blocks(accts):
    kw = _clean(accts)
    kw["acquisitions_by_account"][accts["roth"]] = None  # a read that failed is unknown, not empty
    assert "account_history_missing" in _check(**kw).reasons


def test_no_tracked_accounts_blocks(accts):
    kw = _clean(accts)
    kw["tracked_accounts"] = ()
    assert "no_tracked_accounts" in _check(**kw).reasons


def test_untracked_account_buy_still_counts(accts):
    # Conservative: an acquisition handed in for an account outside tracked_accounts is still
    # evidence of a buy, so it blocks rather than being dropped.
    kw = _clean(accts)
    kw["acquisitions_by_account"][_alias("spare")] = [
        {"symbol": "ZZZA", "acquired_date": date(2026, 11, 20), "quantity": 1.0, "kind": "buy"}]
    assert "acquisition_in_lookback" in _check(**kw).reasons


def test_outside_accounts_must_be_attested(accts):
    # ADR open question 2 default: an operator attestation covers accounts outside Robinhood.
    for value in (None, False):
        kw = _clean(accts)
        kw["outside_accounts_attested"] = value
        assert "outside_accounts_not_attested" in _check(**kw).reasons


def test_malformed_acquisition_row_blocks(accts):
    kw = _clean(accts)
    kw["acquisitions_by_account"][accts["agentic"]].append({"symbol": "ZZZA", "acquired_date": "2026-11-20"})
    assert "malformed_acquisition" in _check(**kw).reasons


def test_acquisition_row_may_not_carry_an_order_id(accts):
    # Robinhood order ids are 15-digit numbers that would trip the account-number guard; the
    # wash-sale logic never needs them, so an order id field is refused outright, never stored.
    from market_data_ingest.ledger.errors import LedgerValidationError

    kw = _clean(accts)
    kw["acquisitions_by_account"][accts["agentic"]].append(
        {"symbol": "ZZZA", "acquired_date": date(2026, 11, 20), "quantity": 1.0, "kind": "buy",
         "order_id": "".join(str(i % 10) for i in range(15))})
    with pytest.raises(LedgerValidationError, match="order_id"):
        _check(**kw)


def test_raw_account_key_is_refused(accts):
    from market_data_ingest.ledger.errors import AccountIdentifierError

    kw = _clean(accts)
    raw = "3" * 9  # account-shaped, built at runtime
    kw["acquisitions_by_account"][raw] = []
    with pytest.raises(AccountIdentifierError) as err:
        _check(**kw)
    assert raw not in str(err.value)  # never echoed


# --- options ------------------------------------------------------------------------------------


def test_open_long_call_blocks(accts):
    kw = _clean(accts)
    kw["open_option_positions"][accts["roth"]] = [
        {"underlying": "ZZZA", "option_type": "call", "side": "long", "quantity": 1.0,
         "expiration_date": date(2027, 3, 19)}]
    result = _check(**kw)
    assert result.eligible is False
    assert "open_long_call" in result.reasons


def test_open_short_put_blocks(accts):
    # A put sold BEFORE the window can still be assigned INSIDE it (ADR decision 3), so any
    # open short put on the ticker blocks, whenever it was written.
    kw = _clean(accts)
    kw["open_option_positions"][accts["agentic"]].append(
        {"underlying": "ZZZA", "option_type": "put", "side": "short", "quantity": 1.0,
         "expiration_date": date(2026, 12, 18)})
    result = _check(**kw)
    assert result.eligible is False
    assert "open_short_put" in result.reasons


def test_options_on_another_underlying_do_not_block(accts):
    kw = _clean(accts)
    kw["open_option_positions"][accts["agentic"]].append(
        {"underlying": "ZZZB", "option_type": "call", "side": "long", "quantity": 1.0,
         "expiration_date": date(2026, 12, 18)})
    assert _check(**kw).eligible is True


def test_unknown_option_positions_block(accts):
    kw = _clean(accts)
    kw["open_option_positions"][accts["managed"]] = None
    assert "option_positions_unknown" in _check(**kw).reasons
    kw = _clean(accts)
    del kw["open_option_positions"][accts["roth"]]
    assert "option_positions_unknown" in _check(**kw).reasons


def test_unreadable_option_row_on_the_ticker_blocks(accts):
    kw = _clean(accts)
    kw["open_option_positions"][accts["agentic"]].append(
        {"underlying": "ZZZA", "option_type": "straddle", "side": "long", "quantity": 1.0,
         "expiration_date": date(2026, 12, 18)})
    assert "malformed_option_position" in _check(**kw).reasons


# --- DRIP ---------------------------------------------------------------------------------------


def test_drip_unknown_blocks(accts):
    kw = _clean(accts)
    kw["drip_status"][accts["managed"]] = None
    result = _check(**kw)
    assert result.eligible is False
    assert "drip_unknown" in result.reasons
    kw = _clean(accts)
    del kw["drip_status"][accts["roth"]]
    assert "drip_unknown" in _check(**kw).reasons


def test_drip_on_blocks(accts):
    kw = _clean(accts)
    kw["drip_status"][accts["agentic"]] = True
    assert "drip_on" in _check(**kw).reasons


def test_drip_flag_must_be_a_real_bool(accts):
    # "off" or 0 is not False; anything but a literal False is unknown (fail closed).
    kw = _clean(accts)
    kw["drip_status"][accts["agentic"]] = 0
    assert "drip_unknown" in _check(**kw).reasons


# --- approvals ----------------------------------------------------------------------------------


def test_missing_approval_blocks(accts):
    kw = _clean(accts)
    kw["approval"] = None
    assert "no_approval" in _check(**kw).reasons


def test_expired_approval_blocks(accts):
    kw = _clean(accts)
    # Expired 2026-11-30 12:00Z; the check runs at 15:00Z the same day.
    kw["approval"] = _approval(expires_at=utc(2026, 11, 30, 12))
    result = _check(**kw)
    assert result.eligible is False
    assert "approval_expired" in result.reasons


def test_approval_expiring_before_the_sale_date_blocks(accts):
    kw = _clean(accts)
    kw["at"] = utc(2026, 11, 28, 15)
    kw["approval"] = _approval(approved_at=utc(2026, 11, 28, 12), expires_at=utc(2026, 11, 30, 23))
    assert "approval_expires_before_sale" in _check(**kw).reasons


def test_approval_from_the_future_blocks(accts):
    # Point in time: an approval recorded after the moment of the check did not exist then.
    kw = _clean(accts)
    kw["approval"] = _approval(approved_at=utc(2026, 11, 30, 16))
    assert "approval_not_yet_valid" in _check(**kw).reasons


def test_approval_for_another_ticker_blocks(accts):
    kw = _clean(accts)
    kw["approval"] = _approval(ticker="ZZZB")
    assert "approval_ticker_mismatch" in _check(**kw).reasons


def test_lot_not_in_the_approval_blocks(accts):
    from market_data_ingest.ledger.tax_loss import lot_key

    kw = _clean(accts)
    kw["lot_keys"] = (lot_key("lot-zzza-1"), lot_key("lot-zzza-9"))
    assert "approval_lot_mismatch" in _check(**kw).reasons


def test_more_shares_than_approved_blocks(accts):
    kw = _clean(accts)
    kw["shares"] = 15.5  # approval max is 15.0
    assert "approval_shares_exceeded" in _check(**kw).reasons
    kw["shares"] = 15.0  # exactly the max is allowed
    assert _check(**kw).eligible is True


@pytest.mark.parametrize("shares", [0.0, -1.0, float("nan"), None])
def test_non_positive_or_unreadable_share_count_blocks(accts, shares):
    kw = _clean(accts)
    kw["shares"] = shares
    assert "invalid_share_count" in _check(**kw).reasons


def test_no_lots_blocks(accts):
    kw = _clean(accts)
    kw["lot_keys"] = ()
    assert "no_lots" in _check(**kw).reasons


def test_approval_validates_its_own_fields():
    from market_data_ingest.ledger.errors import AccountIdentifierError, LedgerValidationError

    with pytest.raises(LedgerValidationError):
        _approval(lot_keys=())
    with pytest.raises(LedgerValidationError):
        _approval(lot_keys=("not-a-lot-key",))
    with pytest.raises(LedgerValidationError):
        _approval(max_shares=0.0)
    with pytest.raises(LedgerValidationError):
        _approval(expires_at=utc(2026, 11, 29, 12))  # not after approved_at
    with pytest.raises(LedgerValidationError):
        _approval(approved_at=datetime(2026, 11, 29, 12))  # naive
    with pytest.raises(AccountIdentifierError):
        _approval(approval_id="tlh-" + "5" * 15)  # an order-id-shaped number trips the guard


def test_many_failures_report_every_reason_at_once(accts):
    kw = _clean(accts)
    kw["approval"] = None
    kw["drip_status"] = {}
    kw["open_option_positions"] = {}
    result = _check(**kw)
    assert {"no_approval", "drip_unknown", "option_positions_unknown"} <= set(result.reasons)
    assert list(result.reasons) == sorted(set(result.reasons))


# --- windows ------------------------------------------------------------------------------------


def test_post_sale_pause_window():
    from market_data_ingest.ledger.tax_loss import post_sale_pause_window

    # Inclusive: the sale day itself plus 31 calendar days after it. Dec 1 + 31 = Jan 1.
    assert post_sale_pause_window(date(2026, 12, 1)) == (date(2026, 12, 1), date(2027, 1, 1))
    # Year-end (ADR question 6): a Dec 31 sale pauses through Jan 31.
    assert post_sale_pause_window(date(2026, 12, 31)) == (date(2026, 12, 31), date(2027, 1, 31))
    with pytest.raises(TypeError):
        post_sale_pause_window(datetime(2026, 12, 1, tzinfo=timezone.utc))


def test_pre_sale_lookback_window():
    from market_data_ingest.ledger.tax_loss import pre_sale_lookback_window

    # Dec 1 - 31 = Oct 31. For a Dec 31 sale the pause must start by Dec 1 (ADR question 6:
    # the last buy may be Nov 29; Nov 29 + 32 = Dec 31).
    assert pre_sale_lookback_window(date(2026, 12, 1)) == (date(2026, 10, 31), date(2026, 12, 1))
    assert pre_sale_lookback_window(date(2026, 12, 31)) == (date(2026, 11, 30), date(2026, 12, 31))


# --- candidate report ---------------------------------------------------------------------------

AS_OF = utc(2026, 11, 2, 21)
QDAY = date(2026, 11, 2)


def _lot(alias, name, symbol="ZZZA", *, qty, basis, acquired=date(2026, 1, 15), snapshot=QDAY):
    from market_data_ingest.ledger.tax_loss import lot_key

    return {"account_alias": alias, "symbol": symbol, "lot_key": lot_key(name), "acquired_date": acquired,
            "snapshot_date": snapshot, "quantity": qty, "cost_basis_per_share": basis}


def _quotes(**prices):
    return {sym: {"price": p, "quote_date": QDAY} for sym, p in prices.items()}


def _kinds(accts):
    return {accts["agentic"]: "agentic", accts["roth"]: "roth_ira", accts["managed"]: "managed_individual"}


def _report(lots, quotes, accts, **kw):
    from market_data_ingest.ledger.tax_loss import candidate_report

    return candidate_report(lots, quotes, AS_OF, account_kinds=kw.pop("account_kinds", _kinds(accts)), **kw)


def test_default_thresholds_are_fifteen_percent_and_twenty_five_dollars():
    from market_data_ingest.ledger.tax_loss import DEFAULT_THRESHOLDS, MIN_LOSS_DOLLARS, MIN_LOSS_PCT

    assert (MIN_LOSS_PCT, MIN_LOSS_DOLLARS) == (0.15, 25.0)
    assert (DEFAULT_THRESHOLDS.min_loss_pct, DEFAULT_THRESHOLDS.min_loss_dollars) == (0.15, 25.0)


def test_report_applies_both_thresholds_and_taxable_only(accts):
    a, roth = accts["agentic"], accts["roth"]
    lots = [
        # 10 sh @ $20, quote $15: cost 200, value 150, loss $50 = 25%  -> candidate.
        _lot(a, "big", "ZZZA", qty=10, basis=20.0),
        # 10 sh @ $10, quote $8.50: loss $15 = 15% but under $25          -> excluded.
        _lot(a, "small-dollars", "ZZZB", qty=10, basis=10.0),
        # 100 sh @ $10, quote $9: loss $100 but only 10%                   -> excluded.
        _lot(a, "small-pct", "ZZZC", qty=100, basis=10.0),
        # 50 sh @ $10, quote $8.50: cost 500, value 425, loss $75 = 15.0% -> candidate (pct boundary).
        _lot(a, "pct-edge", "ZZZB", qty=50, basis=10.0),
        # 20 sh @ $5, quote $3.75 (ZZZD): cost 100, value 75, loss $25.00 = 25% -> candidate ($ boundary).
        _lot(a, "dollar-edge", "ZZZD", qty=20, basis=5.0, acquired=date(2025, 10, 1)),
        # Same loss as "big" but in a Roth IRA: a loss there is not deductible -> excluded.
        _lot(roth, "ira", "ZZZA", qty=10, basis=20.0),
        # A gain: 10 sh @ $10, quote $15                                 -> excluded.
        _lot(a, "gain", "ZZZE", qty=10, basis=10.0),
    ]
    quotes = _quotes(ZZZA=15.0, ZZZB=8.5, ZZZC=9.0, ZZZD=3.75, ZZZE=15.0)
    report = _report(lots, quotes, accts)
    from market_data_ingest.ledger.tax_loss import lot_key

    by_key = {c.lot_key: c for c in report.candidates}
    assert set(by_key) == {lot_key("big"), lot_key("pct-edge"), lot_key("dollar-edge")}
    big = by_key[lot_key("big")]
    assert big.estimated_loss == pytest.approx(50.0)
    assert big.loss_pct == pytest.approx(0.25)
    assert big.cost_basis_total == pytest.approx(200.0) and big.market_value == pytest.approx(150.0)
    assert big.account_kind == "agentic"
    assert by_key[lot_key("pct-edge")].estimated_loss == pytest.approx(75.0)
    assert by_key[lot_key("dollar-edge")].estimated_loss == pytest.approx(25.0)
    # Sorted largest estimated loss first: 75, 50, 25.
    assert [c.estimated_loss for c in report.candidates] == pytest.approx([75.0, 50.0, 25.0])


def test_report_term_short_versus_long(accts):
    a = accts["agentic"]
    lots = [
        # Acquired 2025-11-02, as_of 2026-11-02: held exactly one year, not MORE than one -> short.
        _lot(a, "one-year", qty=10, basis=20.0, acquired=date(2025, 11, 2)),
        # Acquired 2025-11-01: more than one year by as_of 2026-11-02 -> long.
        _lot(a, "year-and-a-day", qty=10, basis=20.0, acquired=date(2025, 11, 1)),
        _lot(a, "recent", qty=10, basis=20.0, acquired=date(2026, 6, 1)),
    ]
    report = _report(lots, _quotes(ZZZA=15.0), accts)
    from market_data_ingest.ledger.tax_loss import lot_key

    terms = {c.lot_key: c.term for c in report.candidates}
    assert terms == {lot_key("one-year"): "short", lot_key("year-and-a-day"): "long", lot_key("recent"): "short"}


def test_report_skips_unreadable_inputs_with_a_reason(accts):
    a = accts["agentic"]
    stranger = _alias("unknown-kind")
    lots = [
        _lot(a, "no-quote", "ZZZQ", qty=10, basis=20.0),
        _lot(a, "stale", "ZZZS", qty=10, basis=20.0),
        _lot(a, "future-quote", "ZZZF", qty=10, basis=20.0),
        _lot(stranger, "kindless", "ZZZA", qty=10, basis=20.0),
    ]
    quotes = {
        "ZZZS": {"price": 15.0, "quote_date": date(2026, 10, 27)},  # 6 days old > 4-day default
        "ZZZF": {"price": 15.0, "quote_date": date(2026, 11, 3)},  # after as_of: lookahead
        "ZZZA": {"price": 15.0, "quote_date": QDAY},
    }
    report = _report(lots, quotes, accts)
    from market_data_ingest.ledger.tax_loss import lot_key

    assert report.candidates == ()
    assert dict(report.skipped) == {
        lot_key("no-quote"): "no_quote",
        lot_key("stale"): "stale_quote",
        lot_key("future-quote"): "quote_after_as_of",
        lot_key("kindless"): "unknown_account_kind",
    }


def test_report_lot_quantity_uses_the_latest_snapshot_numbers(accts):
    a = accts["agentic"]
    lots = [
        _lot(a, "lot-1", qty=10, basis=20.0, snapshot=date(2026, 10, 1)),
        _lot(a, "lot-1", qty=4, basis=20.0, snapshot=date(2026, 11, 1)),  # 6 shares sold since Oct 1
        _lot(a, "lot-1", qty=1, basis=20.0, snapshot=date(2026, 11, 5)),  # after as_of: invisible (lookahead)
    ]
    # Latest snapshot on or before as_of is Nov 1. 4 sh @ $20, quote $10: cost 80, value 40, loss $40 = 50%.
    (cand,) = _report(lots, _quotes(ZZZA=10.0), accts).candidates
    assert (cand.quantity, cand.estimated_loss) == (4, pytest.approx(40.0))


def test_report_without_wash_sale_inputs_lists_every_blocking_reason(accts):
    report = _report([_lot(accts["agentic"], "big", qty=10, basis=20.0)], _quotes(ZZZA=15.0), accts)
    (cand,) = report.candidates
    assert cand.eligible is False
    assert {"account_history_missing", "option_positions_unknown", "drip_unknown", "no_approval",
            "outside_accounts_not_attested"} <= set(cand.blocking_reasons)


def test_report_with_a_recent_buy_lists_it_and_the_pause_still_needed(accts):
    a, roth, managed = accts["agentic"], accts["roth"], accts["managed"]
    from market_data_ingest.ledger.tax_loss import Approval, lot_key

    approval = Approval(approval_id="tlh_zzza_report", ticker="ZZZA", lot_keys=(lot_key("big"),), max_shares=10.0,
                        approved_at=utc(2026, 11, 2, 12), expires_at=utc(2026, 11, 9, 0))
    report = _report(
        [_lot(a, "big", qty=10, basis=20.0)], _quotes(ZZZA=15.0), accts,
        acquisitions_by_account={
            a: [{"symbol": "ZZZA", "acquired_date": date(2026, 10, 28), "quantity": 1.0, "kind": "buy"}],
            roth: [], managed: []},
        open_option_positions={a: [], roth: [], managed: []},
        drip_status={a: False, roth: False, managed: False},
        approvals=[approval],
        outside_accounts_attested=True,
    )
    (cand,) = report.candidates
    assert cand.blocking_reasons == ("acquisition_in_lookback",)
    assert [x["acquired_date"] for x in cand.washing_acquisitions] == [date(2026, 10, 28)]
    # Last buy Oct 28 -> first clean sale date Oct 28 + 32 = Nov 29; from Nov 2 that is 27 days.
    assert cand.earliest_clean_sale_date == date(2026, 11, 29)
    assert cand.pause_days_needed == 27


def test_report_with_clean_inputs_is_eligible(accts):
    a, roth, managed = accts["agentic"], accts["roth"], accts["managed"]
    from market_data_ingest.ledger.tax_loss import Approval, lot_key

    approval = Approval(approval_id="tlh_zzza_report", ticker="ZZZA", lot_keys=(lot_key("big"),), max_shares=10.0,
                        approved_at=utc(2026, 11, 2, 12), expires_at=utc(2026, 11, 9, 0))
    report = _report(
        [_lot(a, "big", qty=10, basis=20.0)], _quotes(ZZZA=15.0), accts,
        acquisitions_by_account={a: [], roth: [], managed: []},
        open_option_positions={a: [], roth: [], managed: []},
        drip_status={a: False, roth: False, managed: False},
        approvals=[approval, replace(approval, approval_id="tlh_other", ticker="ZZZB")],
        outside_accounts_attested=True,
    )
    (cand,) = report.candidates
    assert cand.eligible is True and cand.blocking_reasons == () and cand.pause_days_needed == 0


def test_report_is_pure_and_takes_no_connection(accts):
    import inspect

    from market_data_ingest.ledger.tax_loss import candidate_report, eligibility

    for fn in (candidate_report, eligibility):
        params = set(inspect.signature(fn).parameters)
        assert not params & {"con", "connection", "store", "ledger", "broker"}


# --- approval table ------------------------------------------------------------------------------


def test_approval_table_is_append_only_with_provenance(ledger):
    from market_data_ingest.ledger.tax_loss import APPROVALS_TABLE, ensure_tax_loss_schema, record_approval

    ensure_tax_loss_schema(ledger.con)
    ensure_tax_loss_schema(ledger.con)  # idempotent
    cols = set(ledger.table_columns(APPROVALS_TABLE))
    assert {"approval_id", "ticker", "lot_keys", "max_shares", "expires_at", "approved_at", "inputs_known_at",
            "content_hash", "source", "as_of", "ingested_at", "run_id"} <= cols
    appr = _approval()
    assert record_approval(ledger.con, appr, source="operator_chat", run_id="op-1", as_of=utc(2026, 11, 29, 13)) == 1
    # Identical re-record is dropped by the content hash.
    assert record_approval(ledger.con, appr, source="operator_chat", run_id="op-2", as_of=utc(2026, 11, 29, 14)) == 0
    assert ledger.row_count(APPROVALS_TABLE) == 1


def test_approvals_known_at_is_point_in_time(ledger):
    from market_data_ingest.ledger.tax_loss import approvals_known_at, ensure_tax_loss_schema, record_approval

    ensure_tax_loss_schema(ledger.con)
    first = _approval()
    record_approval(ledger.con, first, source="operator_chat", run_id="op-1", as_of=utc(2026, 11, 29, 13))
    # Operator narrows the same approval later (fewer shares): a new version, a new row.
    narrowed = replace(first, max_shares=5.0, approved_at=utc(2026, 11, 30, 9))
    record_approval(ledger.con, narrowed, source="operator_chat", run_id="op-2", as_of=utc(2026, 11, 30, 9))

    assert approvals_known_at(ledger.con, utc(2026, 11, 29, 12)) == []  # nothing recorded yet
    assert approvals_known_at(ledger.con, utc(2026, 11, 29, 20)) == [first]
    assert approvals_known_at(ledger.con, utc(2026, 11, 30, 10)) == [narrowed]


def test_record_approval_refuses_as_of_before_approved_at(ledger):
    from market_data_ingest.ledger.errors import LedgerValidationError
    from market_data_ingest.ledger.tax_loss import APPROVALS_TABLE, ensure_tax_loss_schema, record_approval

    ensure_tax_loss_schema(ledger.con)
    with pytest.raises(LedgerValidationError):
        record_approval(ledger.con, _approval(), source="operator_chat", run_id="op-1", as_of=utc(2026, 11, 29, 11))
    with pytest.raises(LedgerValidationError):
        record_approval(ledger.con, {"approval_id": "x"}, source="operator_chat", run_id="op-1",
                        as_of=utc(2026, 11, 29, 13))
    assert ledger.row_count(APPROVALS_TABLE) == 0


def test_lot_key_matches_the_tax_lot_ingest(ledger):
    from market_data_ingest.ledger.tax_loss import lot_key

    alias = _alias("agentic")
    ledger.ingest_tax_lots([{"account_alias": alias, "symbol": "ZZZA", "lot_id": "lot-zzza-1",
                             "acquired_date": date(2026, 1, 15), "snapshot_date": QDAY, "quantity": 10.0,
                             "cost_basis_per_share": 20.0}], source="t", run_id="r", as_of=AS_OF)
    (row,) = ledger.tax_lots()
    assert row["lot_key"] == lot_key("lot-zzza-1")


def test_report_accepts_rows_read_back_from_the_ledger(ledger, accts):
    # End to end on stored rows: tax_lots() output (with provenance columns) feeds the report.
    ledger.ingest_tax_lots([{"account_alias": accts["agentic"], "symbol": "ZZZA", "lot_id": "lot-zzza-1",
                             "acquired_date": date(2026, 1, 15), "snapshot_date": QDAY, "quantity": 10.0,
                             "cost_basis_per_share": 20.0}], source="t", run_id="r", as_of=AS_OF)
    (cand,) = _report(ledger.tax_lots(known_at=AS_OF), _quotes(ZZZA=15.0), accts).candidates
    assert cand.estimated_loss == pytest.approx(50.0)


# --- static guarantees ---------------------------------------------------------------------------


def test_module_names_no_broker_order_tool():
    # ADR scope: no code here places, previews or cancels an order. The broker's order tools
    # are all named place_*/review_*/cancel_* (preview_* contains review_), so none of those
    # fragments may appear anywhere in the module source, comments included.
    text = MODULE.read_text()
    offenders = [frag for frag in ("place_", "review_", "cancel_", "submit_order") if frag in text.lower()]
    assert not offenders, f"tax_loss.py mentions broker order tools: {offenders}"


def test_module_imports_nothing_broker_shaped():
    import ast

    tree = ast.parse(MODULE.read_text())
    imported = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imported |= {a.name for a in node.names}
        elif isinstance(node, ast.ImportFrom):
            imported.add(node.module or "")
    assert not {m for m in imported if re.search(r"robinhood|broker|alpaca|requests|urllib|http|socket", m)}


def test_only_record_approval_writes_approvals():
    # The system never creates approvals on its own: exactly one INSERT into the approvals
    # table exists, inside record_approval, which only the operator's own call reaches.
    import ast

    tree = ast.parse(MODULE.read_text())
    writers = [
        fn.name for fn in ast.walk(tree) if isinstance(fn, ast.FunctionDef)
        and "INSERT INTO" in ast.get_source_segment(MODULE.read_text(), fn)
    ]
    assert writers == ["record_approval"]


def test_public_api_has_no_sell_or_order_entry_point():
    import market_data_ingest.ledger.tax_loss as mod

    public = {n for n in dir(mod) if not n.startswith("_")}
    assert not {n for n in public if re.search(r"(?i)sell|order|place|submit|cancel|execute", n)}


def test_module_contains_no_account_shaped_literal():
    from market_data_ingest.ledger.accounts import looks_like_account_identifier

    bad = [n for n, line in enumerate(MODULE.read_text().splitlines(), 1) if looks_like_account_identifier(line)]
    assert not bad, f"account-shaped literal on lines {bad}"


def test_timedelta_sanity_for_the_hand_counts():
    # The hand counts above, checked once by the stdlib so a typo in a comment cannot hide.
    assert (SALE - date(2026, 10, 30)).days == 32
    assert (SALE - date(2026, 10, 31)).days == 31
    assert (SALE - date(2026, 11, 21)).days == 10
    assert date(2026, 10, 28) + timedelta(days=32) == date(2026, 11, 29)
