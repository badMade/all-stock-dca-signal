"""Research-only tax-loss candidate report and wash-sale eligibility check.

Implements the tests for the PROPOSED ADR docs/adr-tax-loss-sale.md (PR #2099) on the Phase 0
ledger (PR #2101). Nothing under test places, previews or cancels an order: the module only
reads plain rows a caller already fetched, plus approvals the operator recorded in the
ledger, and says "eligible" or "not eligible, because ...".

Every row here is SYNTHETIC: fake tickers (``ZZZA``...), round prices, and account aliases
built at runtime from descriptive strings (never digits), so tests/test_config_invariants.py
has nothing to flag. The arithmetic behind each expected value is written next to it.

Window conventions (proposed, conservative; ADR decision 3):

* Pre-sale lookback: any acquisition of the ticker dated ``sale_date - 31`` through
  ``sale_date`` inclusive (0..31 calendar days before) blocks the sale. The statute's window
  is 30 days; day 31 is a deliberate one-day margin, which is why "32 days before" is the
  first clean day in these tests.
* Post-sale pause: ``sale_date`` through ``sale_date + 31`` inclusive.
* A long call or short put on the ticker held at ANY time overlapping either window blocks,
  even if it was closed before the sale (review of #2104: open positions alone missed a call
  bought on day -10 and sold on day -2).

History inputs (review of #2104): every account's acquisition and option history is a dict
``{"covers_from": date, "rows": [...]}``. An empty ``rows`` list is clean only when
``covers_from`` reaches back to the start of the lookback; otherwise it is incomplete.
"""

from __future__ import annotations

import re
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

import pytest

from .conftest import TEST_SALT, utc

MODULE = Path(__file__).resolve().parents[2] / "market_data_ingest" / "ledger" / "tax_loss.py"

SALE = date(2026, 12, 1)
AT = utc(2026, 11, 30, 15)  # when the eligibility check runs (approval expiry is judged here)
COVERS = date(2026, 9, 1)  # history start: before every lookback used below


def _alias(name: str) -> str:
    from market_data_ingest.ledger.accounts import account_alias

    # Descriptive strings, not digits: the alias is an HMAC, so the raw value never matters.
    return account_alias(f"synthetic-{name}", salt=TEST_SALT)


def _hist(rows=None, covers_from=COVERS):
    return {"covers_from": covers_from, "rows": list(rows or [])}


def _buy(when, symbol="ZZZA", qty=1.0, kind="buy"):
    return {"symbol": symbol, "acquired_date": when, "quantity": qty, "kind": kind}


def _opt(option_type, side, opened, closed=None, *, underlying="ZZZA", expiration=date(2027, 1, 15)):
    return {"underlying": underlying, "option_type": option_type, "side": side, "quantity": 1.0,
            "opened_on": opened, "closed_on": closed, "expiration_date": expiration}


@pytest.fixture
def accts():
    return {"agentic": _alias("agentic"), "roth": _alias("roth-ira"), "managed": _alias("managed-individual")}


def _approval(**overrides):
    from market_data_ingest.ledger.tax_loss import Approval, lot_key

    fields = {
        "approval_id": "tlh_zzza_first",
        "ticker": "ZZZA",
        "sale_date": SALE,
        "lot_keys": (lot_key("lot-zzza-1"), lot_key("lot-zzza-2")),
        "max_shares": 15.0,
        "approved_at": utc(2026, 11, 29, 12),
        # 2026-12-02 00:00Z is 2026-12-01 19:00 ET: after the sale session's 16:00 ET close.
        "expires_at": utc(2026, 12, 2, 0),
    }
    fields.update(overrides)
    return Approval(**fields)


def _record(ledger, *, as_of=None, **overrides) -> str:
    from market_data_ingest.ledger.tax_loss import record_approval

    approval = _approval(**overrides)
    record_approval(ledger.con, approval, source="operator_chat", run_id="op",
                    as_of=as_of or approval.approved_at + timedelta(minutes=1))
    return approval.approval_id


def _clean(ledger, accts):
    """Inputs under which a ZZZA sale on SALE is eligible. Each test breaks exactly one thing."""
    from market_data_ingest.ledger.tax_loss import lot_key

    return {
        "ticker": "ZZZA",
        "sale_date": SALE,
        "acquisitions_by_account": {
            # 2026-10-30 is 32 days before 2026-12-01 (Oct 30 -> Nov 30 is 31, +1): outside.
            accts["agentic"]: _hist([_buy(date(2026, 10, 30))]),
            # A buy of a DIFFERENT ticker inside the window never blocks ZZZA.
            accts["roth"]: _hist([_buy(date(2026, 11, 25), symbol="ZZZB")]),
            accts["managed"]: _hist(),  # read, covers the whole lookback, and empty: proven clean
        },
        "option_history_by_account": {
            # A long put and a short call do not acquire shares: neither blocks.
            accts["agentic"]: _hist([_opt("put", "long", date(2026, 11, 2)), _opt("call", "short", date(2026, 11, 2))]),
            accts["roth"]: _hist(),
            accts["managed"]: _hist(),
        },
        "drip_status": {accts["agentic"]: False, accts["roth"]: False, accts["managed"]: False},
        "approval_id": _record(ledger),
        "con": ledger.con,
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


def test_clean_inputs_are_eligible(ledger, accts):
    result = _check(**_clean(ledger, accts))
    assert result.eligible is True
    assert result.reasons == ()
    assert result.washing_acquisitions == ()
    # Only acquisitions INSIDE the windows count toward the clean date (review of #2104): the
    # Oct 30 buy is outside, so there is nothing to wait out.
    assert result.earliest_clean_sale_date is None


def test_ira_buy_ten_days_before_the_sale_blocks(ledger, accts):
    # 2026-11-21 is 10 days before 2026-12-01. An IRA purchase makes the loss permanently
    # disallowed (ADR context), so a buy in ANY tracked account blocks, not just the seller's.
    kw = _clean(ledger, accts)
    kw["acquisitions_by_account"][accts["roth"]]["rows"].append(_buy(date(2026, 11, 21), qty=0.5))
    result = _check(**kw)
    assert result.eligible is False
    assert "acquisition_in_lookback" in result.reasons
    assert [(a["account_alias"], a["acquired_date"]) for a in result.washing_acquisitions] == [
        (accts["roth"], date(2026, 11, 21))]
    # Last in-window buy 2026-11-21 -> first clean sale date is 32 days later: Nov 21 + 32 = Dec 23.
    assert result.earliest_clean_sale_date == date(2026, 12, 23)


def test_managed_account_buy_blocks(ledger, accts):
    # A Robinhood-managed portfolio buys on its own schedule; it still counts.
    kw = _clean(ledger, accts)
    kw["acquisitions_by_account"][accts["managed"]] = _hist([_buy(date(2026, 11, 28), qty=0.1,
                                                                  kind="managed_rebalance")])
    assert "acquisition_in_lookback" in _check(**kw).reasons


def test_buy_thirty_two_days_before_does_not_block_but_thirty_one_does(ledger, accts):
    kw = _clean(ledger, accts)  # already holds a 32-days-before buy (2026-10-30) and is eligible
    assert _check(**kw).eligible is True
    # 2026-10-31 is 31 days before 2026-12-01: inside the conservative 31-day lookback.
    kw["acquisitions_by_account"][accts["agentic"]]["rows"].append(_buy(date(2026, 10, 31)))
    assert _check(**kw).eligible is False


def test_buy_on_the_check_day_blocks(ledger, accts):
    kw = _clean(ledger, accts)
    kw["acquisitions_by_account"][accts["agentic"]]["rows"].append(_buy(date(2026, 11, 30)))
    assert "acquisition_in_lookback" in _check(**kw).reasons


def test_acquisition_dated_after_the_check_blocks(ledger, accts):
    # A row dated after `at` cannot be real history (lookahead or a bad feed): fail closed,
    # and never let it move the earliest clean date (review of #2104).
    kw = _clean(ledger, accts)
    kw["acquisitions_by_account"][accts["agentic"]]["rows"].append(_buy(date(2026, 12, 5)))
    result = _check(**kw)
    assert "acquisition_dated_after_check" in result.reasons
    assert result.earliest_clean_sale_date is None


def test_dividend_reinvestment_counts_as_an_acquisition(ledger, accts):
    kw = _clean(ledger, accts)
    kw["acquisitions_by_account"][accts["agentic"]]["rows"].append(
        _buy(date(2026, 11, 15), symbol="zzza", qty=0.01, kind="drip"))  # case-folded
    assert "acquisition_in_lookback" in _check(**kw).reasons


def test_same_lot_nuance_conservative_pre_pause_is_intentional(ledger, accts):
    # IRS precision (ADR context, review of 2026-11-02): shares bought within 30 days BEFORE
    # the sale do not wash the loss when those same shares are part of the sale. So selling
    # the whole position -- including a lot bought 10 days ago -- is strictly allowed.
    # The ADR chooses the conservative rule anyway: ANY acquisition in the lookback blocks,
    # including the very lot being sold. This test pins that choice so nobody "fixes" it
    # without amending the ADR.
    from market_data_ingest.ledger.tax_loss import lot_key

    kw = _clean(ledger, accts)
    kw["acquisitions_by_account"][accts["agentic"]]["rows"].append(_buy(date(2026, 11, 21), qty=5.0))
    kw["lot_keys"] = (lot_key("lot-zzza-1"), lot_key("lot-zzza-2"))  # the recent lot IS in the sale
    kw["shares"] = 15.0
    result = _check(**kw)
    assert result.eligible is False
    assert "acquisition_in_lookback" in result.reasons


def test_missing_account_history_blocks(ledger, accts):
    kw = _clean(ledger, accts)
    del kw["acquisitions_by_account"][accts["managed"]]  # tracked, but no history supplied
    result = _check(**kw)
    assert result.eligible is False
    assert "account_history_missing" in result.reasons


def test_unknown_account_history_blocks(ledger, accts):
    kw = _clean(ledger, accts)
    kw["acquisitions_by_account"][accts["roth"]] = None  # a read that failed is unknown, not empty
    assert "account_history_missing" in _check(**kw).reasons


def test_empty_history_that_does_not_cover_the_lookback_blocks(ledger, accts):
    # Review of #2104: an empty list from a read that only went back to Nov 5 proves nothing
    # about Oct 31 - Nov 4. covers_from must be on or before Oct 31 (Dec 1 - 31).
    kw = _clean(ledger, accts)
    kw["acquisitions_by_account"][accts["managed"]] = _hist(covers_from=date(2026, 11, 5))
    assert "account_history_incomplete" in _check(**kw).reasons
    kw["acquisitions_by_account"][accts["managed"]] = _hist(covers_from=date(2026, 10, 31))  # exactly enough
    assert _check(**kw).eligible is True


def test_history_without_covers_from_blocks(ledger, accts):
    kw = _clean(ledger, accts)
    kw["acquisitions_by_account"][accts["managed"]] = {"rows": []}
    assert "account_history_incomplete" in _check(**kw).reasons
    kw = _clean(ledger, accts)
    kw["acquisitions_by_account"][accts["managed"]] = []  # a bare list: no coverage stated
    assert "account_history_missing" in _check(**kw).reasons


def test_no_tracked_accounts_blocks(ledger, accts):
    kw = _clean(ledger, accts)
    kw["tracked_accounts"] = ()
    assert "no_tracked_accounts" in _check(**kw).reasons


def test_untracked_account_buy_still_counts(ledger, accts):
    # Conservative: an acquisition handed in for an account outside tracked_accounts is still
    # evidence of a buy, so it blocks rather than being dropped.
    kw = _clean(ledger, accts)
    kw["acquisitions_by_account"][_alias("spare")] = _hist([_buy(date(2026, 11, 20))])
    assert "acquisition_in_lookback" in _check(**kw).reasons


def test_outside_accounts_must_be_attested(ledger, accts):
    # ADR open question 2 default: an operator attestation covers accounts outside Robinhood.
    for value in (None, False):
        kw = _clean(ledger, accts)
        kw["outside_accounts_attested"] = value
        assert "outside_accounts_not_attested" in _check(**kw).reasons


def test_malformed_acquisition_row_blocks(ledger, accts):
    kw = _clean(ledger, accts)
    kw["acquisitions_by_account"][accts["agentic"]]["rows"].append({"symbol": "ZZZA", "acquired_date": "2026-11-20"})
    assert "malformed_acquisition" in _check(**kw).reasons


def test_acquisition_row_may_not_carry_an_order_id(ledger, accts):
    # Robinhood order ids are numeric and, next to a label like "rh", trip the account-number
    # guard; the wash-sale logic never needs them, so an order id field is refused outright.
    from market_data_ingest.ledger.errors import LedgerValidationError

    kw = _clean(ledger, accts)
    kw["acquisitions_by_account"][accts["agentic"]]["rows"].append(
        {**_buy(date(2026, 11, 20)), "order_id": "".join(str(i % 10) for i in range(15))})
    with pytest.raises(LedgerValidationError, match="order_id"):
        _check(**kw)


def test_raw_account_key_is_refused(ledger, accts):
    from market_data_ingest.ledger.errors import AccountIdentifierError

    kw = _clean(ledger, accts)
    raw = "3" * 9  # account-shaped, built at runtime
    kw["acquisitions_by_account"][raw] = _hist()
    with pytest.raises(AccountIdentifierError) as err:
        _check(**kw)
    assert raw not in str(err.value)  # never echoed


def test_sale_date_before_the_check_blocks(ledger, accts):
    kw = _clean(ledger, accts)
    kw["sale_date"] = date(2026, 11, 29)  # the check runs on Nov 30 ET
    assert "sale_date_before_check" in _check(**kw).reasons


# --- options: history, not just open positions (review of #2104) --------------------------------


def test_long_call_bought_and_sold_inside_the_window_blocks(ledger, accts):
    # The #2104 blocker: a call bought day -10 (Nov 21) and sold day -2 (Nov 29) leaves nothing
    # open at the sale, but it was "a contract or option so to acquire" inside the window.
    kw = _clean(ledger, accts)
    kw["option_history_by_account"][accts["roth"]] = _hist([_opt("call", "long", date(2026, 11, 21),
                                                                 date(2026, 11, 29))])
    result = _check(**kw)
    assert result.eligible is False
    assert "long_call_in_window" in result.reasons


def test_short_put_written_and_bought_back_inside_the_window_blocks(ledger, accts):
    # ADR decision 3: writing a put inside the window is treated as blocking (fail closed).
    kw = _clean(ledger, accts)
    kw["option_history_by_account"][accts["agentic"]]["rows"].append(
        _opt("put", "short", date(2026, 11, 15), date(2026, 11, 20)))
    assert "short_put_in_window" in _check(**kw).reasons


def test_open_short_put_written_before_the_window_blocks(ledger, accts):
    # A put sold BEFORE the window can still be assigned INSIDE it (ADR decision 3).
    kw = _clean(ledger, accts)
    kw["option_history_by_account"][accts["agentic"]]["rows"].append(
        _opt("put", "short", date(2026, 9, 10), expiration=date(2026, 12, 18)))
    assert "short_put_in_window" in _check(**kw).reasons


def test_open_long_call_blocks(ledger, accts):
    kw = _clean(ledger, accts)
    kw["option_history_by_account"][accts["managed"]] = _hist([_opt("call", "long", date(2026, 9, 2))])
    assert "long_call_in_window" in _check(**kw).reasons


def test_options_entirely_before_the_window_do_not_block(ledger, accts):
    # Lookback starts Oct 31. A call closed Sep 20 and an unclosed call that expired Oct 17
    # both ended before it.
    kw = _clean(ledger, accts)
    kw["option_history_by_account"][accts["agentic"]]["rows"] += [
        _opt("call", "long", date(2026, 9, 5), date(2026, 9, 20)),
        _opt("call", "long", date(2026, 9, 5), expiration=date(2026, 10, 17)),
    ]
    assert _check(**kw).eligible is True
    # An option that ended ON the first lookback day (Oct 31) overlaps it.
    kw["option_history_by_account"][accts["agentic"]]["rows"].append(
        _opt("call", "long", date(2026, 9, 5), date(2026, 10, 31)))
    assert "long_call_in_window" in _check(**kw).reasons


def test_options_on_another_underlying_do_not_block(ledger, accts):
    kw = _clean(ledger, accts)
    kw["option_history_by_account"][accts["agentic"]]["rows"].append(
        _opt("call", "long", date(2026, 11, 20), underlying="ZZZB"))
    assert _check(**kw).eligible is True


def test_missing_or_short_option_history_blocks(ledger, accts):
    kw = _clean(ledger, accts)
    kw["option_history_by_account"][accts["managed"]] = None
    assert "option_history_missing" in _check(**kw).reasons
    kw = _clean(ledger, accts)
    del kw["option_history_by_account"][accts["roth"]]
    assert "option_history_missing" in _check(**kw).reasons
    kw = _clean(ledger, accts)
    kw["option_history_by_account"][accts["roth"]] = _hist(covers_from=date(2026, 11, 1))
    assert "option_history_incomplete" in _check(**kw).reasons


def test_unreadable_option_row_on_the_ticker_blocks(ledger, accts):
    kw = _clean(ledger, accts)
    kw["option_history_by_account"][accts["agentic"]]["rows"].append(_opt("straddle", "long", date(2026, 11, 2)))
    assert "malformed_option_history" in _check(**kw).reasons
    kw = _clean(ledger, accts)
    kw["option_history_by_account"][accts["agentic"]]["rows"].append(
        _opt("call", "long", date(2026, 11, 2), date(2026, 11, 1)))  # closed before it opened
    assert "malformed_option_history" in _check(**kw).reasons
    kw = _clean(ledger, accts)
    kw["option_history_by_account"][accts["agentic"]]["rows"].append(
        _opt("call", "short", date(2026, 12, 3)))  # opened after the check: lookahead
    assert "malformed_option_history" in _check(**kw).reasons


def test_option_row_may_not_carry_an_order_id(ledger, accts):
    from market_data_ingest.ledger.errors import LedgerValidationError

    kw = _clean(ledger, accts)
    kw["option_history_by_account"][accts["agentic"]]["rows"].append(
        {**_opt("put", "long", date(2026, 11, 2)), "order_id": "x"})
    with pytest.raises(LedgerValidationError, match="order_id"):
        _check(**kw)


# --- DRIP ---------------------------------------------------------------------------------------


def test_drip_unknown_blocks(ledger, accts):
    kw = _clean(ledger, accts)
    kw["drip_status"][accts["managed"]] = None
    result = _check(**kw)
    assert result.eligible is False
    assert "drip_unknown" in result.reasons
    kw = _clean(ledger, accts)
    del kw["drip_status"][accts["roth"]]
    assert "drip_unknown" in _check(**kw).reasons


def test_drip_on_blocks(ledger, accts):
    kw = _clean(ledger, accts)
    kw["drip_status"][accts["agentic"]] = True
    assert "drip_on" in _check(**kw).reasons


def test_drip_flag_must_be_a_real_bool(ledger, accts):
    # "off" or 0 is not False; anything but a literal False is unknown (fail closed).
    kw = _clean(ledger, accts)
    kw["drip_status"][accts["agentic"]] = 0
    assert "drip_unknown" in _check(**kw).reasons


# --- approvals: only ones recorded in the ledger count (review of #2104) ------------------------


def test_missing_approval_blocks(ledger, accts):
    kw = _clean(ledger, accts)
    kw["approval_id"] = None
    assert "no_approval" in _check(**kw).reasons


def test_unrecorded_approval_id_blocks(ledger, accts):
    # An approval that exists only in memory, or was never recorded, is no approval at all.
    kw = _clean(ledger, accts)
    kw["approval_id"] = "tlh_never_recorded"
    assert "no_approval" in _check(**kw).reasons


def test_eligibility_does_not_accept_an_in_memory_approval(ledger, accts):
    import inspect

    from market_data_ingest.ledger.tax_loss import eligibility

    assert "approval" not in inspect.signature(eligibility).parameters
    kw = _clean(ledger, accts)
    with pytest.raises(TypeError):
        _check(**{**kw, "approval": _approval()})


def test_approval_recorded_after_the_check_blocks(ledger, accts):
    # Point in time: recorded (as_of) at 16:00Z, after the 15:00Z check, so it did not exist yet.
    kw = _clean(ledger, accts)
    kw["approval_id"] = _record(ledger, approval_id="tlh_zzza_late", approved_at=utc(2026, 11, 30, 16))
    assert "no_approval" in _check(**kw).reasons


def test_expired_approval_blocks(ledger, accts):
    kw = _clean(ledger, accts)
    # Expired 2026-11-30 12:00Z; the check runs at 15:00Z the same day.
    kw["approval_id"] = _record(ledger, approval_id="tlh_zzza_expired", expires_at=utc(2026, 11, 30, 12))
    result = _check(**kw)
    assert result.eligible is False
    assert "approval_expired" in result.reasons


@pytest.mark.parametrize("expires", [utc(2026, 12, 1, 5, 1), utc(2026, 12, 1, 20, 59)])
def test_approval_expiring_before_the_sale_session_closes_blocks(ledger, accts, expires):
    # 05:01Z = 00:01 ET on the sale day; 20:59Z = 15:59 ET. Both end before the 16:00 ET close.
    kw = _clean(ledger, accts)
    kw["approval_id"] = _record(ledger, approval_id="tlh_zzza_short", expires_at=expires)
    assert "approval_expires_before_sale" in _check(**kw).reasons


def test_approval_expiring_after_the_close_is_fine(ledger, accts):
    kw = _clean(ledger, accts)
    kw["approval_id"] = _record(ledger, approval_id="tlh_zzza_close", expires_at=utc(2026, 12, 1, 21, 1))  # 16:01 ET
    assert _check(**kw).eligible is True


def test_approval_for_another_sale_date_blocks(ledger, accts):
    # ADR decision 1: approved "for that ticker, that date and that share count".
    kw = _clean(ledger, accts)
    kw["approval_id"] = _record(ledger, approval_id="tlh_zzza_other_day", sale_date=date(2026, 12, 2),
                                expires_at=utc(2026, 12, 3, 0))
    assert "approval_sale_date_mismatch" in _check(**kw).reasons


def test_approval_for_another_ticker_blocks(ledger, accts):
    kw = _clean(ledger, accts)
    kw["approval_id"] = _record(ledger, approval_id="tlh_zzzb", ticker="ZZZB")
    assert "approval_ticker_mismatch" in _check(**kw).reasons


def test_lot_not_in_the_approval_blocks(ledger, accts):
    from market_data_ingest.ledger.tax_loss import lot_key

    kw = _clean(ledger, accts)
    kw["lot_keys"] = (lot_key("lot-zzza-1"), lot_key("lot-zzza-9"))
    assert "approval_lot_mismatch" in _check(**kw).reasons


def test_more_shares_than_approved_blocks(ledger, accts):
    kw = _clean(ledger, accts)
    kw["shares"] = 15.5  # approval max is 15.0
    assert "approval_shares_exceeded" in _check(**kw).reasons
    kw["shares"] = 15.0  # exactly the max is allowed
    assert _check(**kw).eligible is True


def test_narrowed_approval_version_wins(ledger, accts):
    # The operator re-records the same approval id with fewer shares; the newer decision rules.
    kw = _clean(ledger, accts)
    _record(ledger, max_shares=5.0, approved_at=utc(2026, 11, 30, 9))
    assert "approval_shares_exceeded" in _check(**kw).reasons  # 10 shares > 5


@pytest.mark.parametrize("shares", [0.0, -1.0, float("nan"), None])
def test_non_positive_or_unreadable_share_count_blocks(ledger, accts, shares):
    kw = _clean(ledger, accts)
    kw["shares"] = shares
    assert "invalid_share_count" in _check(**kw).reasons


def test_no_lots_blocks(ledger, accts):
    kw = _clean(ledger, accts)
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
    with pytest.raises(TypeError):
        _approval(sale_date=datetime(2026, 12, 1, tzinfo=timezone.utc))  # a datetime is not a date
    with pytest.raises(AccountIdentifierError):
        _approval(approval_id="tlh-" + "5" * 15)  # an order-id-shaped number


def test_many_failures_report_every_reason_at_once(ledger, accts):
    kw = _clean(ledger, accts)
    kw["approval_id"] = None
    kw["drip_status"] = {}
    kw["option_history_by_account"] = {}
    result = _check(**kw)
    assert {"no_approval", "drip_unknown", "option_history_missing"} <= set(result.reasons)
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

AS_OF = utc(2026, 11, 2, 21)  # 16:00 ET, Nov 2
QDAY = date(2026, 11, 2)


def _lot(alias, name, symbol="ZZZA", *, qty, basis, acquired=date(2026, 1, 15), snapshot=QDAY):
    from market_data_ingest.ledger.tax_loss import lot_key

    return {"account_alias": alias, "symbol": symbol, "lot_key": lot_key(name), "acquired_date": acquired,
            "snapshot_date": snapshot, "quantity": qty, "cost_basis_per_share": basis}


def _quotes(**prices):
    return {sym: {"price": p, "quote_date": QDAY} for sym, p in prices.items()}


def _kinds(accts):
    return {accts["agentic"]: "agentic", accts["roth"]: "roth_ira", accts["managed"]: "managed_individual"}


def _report(ledger, lots, quotes, accts, **kw):
    from market_data_ingest.ledger.tax_loss import candidate_report

    return candidate_report(lots, quotes, AS_OF, con=ledger.con,
                            account_kinds=kw.pop("account_kinds", _kinds(accts)), **kw)


def _clean_report_inputs(accts):
    a, roth, managed = accts["agentic"], accts["roth"], accts["managed"]
    return {
        "acquisitions_by_account": {a: _hist(), roth: _hist(), managed: _hist()},
        "option_history_by_account": {a: _hist(), roth: _hist(), managed: _hist()},
        "drip_status": {a: False, roth: False, managed: False},
        "outside_accounts_attested": True,
    }


def _record_report_approval(ledger, **overrides):
    from market_data_ingest.ledger.tax_loss import lot_key

    fields = {"approval_id": "tlh_zzza_report", "sale_date": QDAY, "lot_keys": (lot_key("big"),),
              "max_shares": 10.0, "approved_at": utc(2026, 11, 2, 12), "expires_at": utc(2026, 11, 3, 0)}
    fields.update(overrides)
    return _record(ledger, **fields)


def test_default_thresholds_are_fifteen_percent_and_twenty_five_dollars():
    from market_data_ingest.ledger.tax_loss import DEFAULT_THRESHOLDS, MIN_LOSS_DOLLARS, MIN_LOSS_PCT

    assert (MIN_LOSS_PCT, MIN_LOSS_DOLLARS) == (0.15, 25.0)
    assert (DEFAULT_THRESHOLDS.min_loss_pct, DEFAULT_THRESHOLDS.min_loss_dollars) == (0.15, 25.0)


def test_report_applies_both_thresholds_and_taxable_only(ledger, accts):
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
    report = _report(ledger, lots, quotes, accts)
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


def test_report_term_short_versus_long(ledger, accts):
    a = accts["agentic"]
    lots = [
        # Acquired 2025-11-02, as_of 2026-11-02: held exactly one year, not MORE than one -> short.
        _lot(a, "one-year", qty=10, basis=20.0, acquired=date(2025, 11, 2)),
        # Acquired 2025-11-01: more than one year by as_of 2026-11-02 -> long.
        _lot(a, "year-and-a-day", qty=10, basis=20.0, acquired=date(2025, 11, 1)),
        _lot(a, "recent", qty=10, basis=20.0, acquired=date(2026, 6, 1)),
    ]
    report = _report(ledger, lots, _quotes(ZZZA=15.0), accts)
    from market_data_ingest.ledger.tax_loss import lot_key

    terms = {c.lot_key: c.term for c in report.candidates}
    assert terms == {lot_key("one-year"): "short", lot_key("year-and-a-day"): "long", lot_key("recent"): "short"}


def test_report_skips_unreadable_inputs_with_a_reason(ledger, accts):
    a = accts["agentic"]
    stranger = _alias("unknown-kind")
    bad_key = {**_lot(a, "bad-key", qty=10, basis=20.0), "lot_key": "not-a-lot-key"}
    lots = [
        _lot(a, "no-quote", "ZZZQ", qty=10, basis=20.0),
        _lot(a, "stale", "ZZZS", qty=10, basis=20.0),
        _lot(a, "future-quote", "ZZZF", qty=10, basis=20.0),
        _lot(stranger, "kindless", "ZZZA", qty=10, basis=20.0),
        bad_key,  # review of #2104: a lot key that is not a lot_key() value is malformed, not judged
    ]
    quotes = {
        "ZZZS": {"price": 15.0, "quote_date": date(2026, 10, 27)},  # 6 days old > 4-day default
        "ZZZF": {"price": 15.0, "quote_date": date(2026, 11, 3)},  # after as_of: lookahead
        "ZZZA": {"price": 15.0, "quote_date": QDAY},
    }
    report = _report(ledger, lots, quotes, accts)
    from market_data_ingest.ledger.tax_loss import lot_key

    assert report.candidates == ()
    assert dict(report.skipped) == {
        lot_key("no-quote"): "no_quote",
        lot_key("stale"): "stale_quote",
        lot_key("future-quote"): "quote_after_as_of",
        lot_key("kindless"): "unknown_account_kind",
        "lots[4]": "malformed_lot",
    }


def test_report_uses_each_accounts_newest_snapshot_only(ledger, accts):
    # Review of #2104: a lot sold since (absent from the account's newest snapshot) must not
    # stay a candidate just because an older snapshot still lists it.
    a, managed = accts["agentic"], accts["managed"]
    lots = [
        _lot(a, "lot-1", qty=10, basis=20.0, snapshot=date(2026, 10, 1)),
        _lot(a, "lot-sold", qty=10, basis=20.0, snapshot=date(2026, 10, 1)),  # gone by Nov 1
        _lot(a, "lot-1", qty=4, basis=20.0, snapshot=date(2026, 11, 1)),  # 6 shares sold since Oct 1
        _lot(a, "lot-1", qty=1, basis=20.0, snapshot=date(2026, 11, 5)),  # after as_of: invisible (lookahead)
        # Another account's newest snapshot is older; its own lots still count.
        _lot(managed, "managed-1", qty=10, basis=20.0, snapshot=date(2026, 10, 15)),
    ]
    # lot-1: 4 sh @ $20, quote $10: cost 80, value 40, loss $40 = 50%.
    # managed-1: 10 sh @ $20, quote $10: cost 200, value 100, loss $100 = 50%.
    report = _report(ledger, lots, _quotes(ZZZA=10.0), accts)
    from market_data_ingest.ledger.tax_loss import lot_key

    got = {c.lot_key: (c.quantity, c.estimated_loss) for c in report.candidates}
    assert got == {lot_key("lot-1"): (4, pytest.approx(40.0)), lot_key("managed-1"): (10, pytest.approx(100.0))}


def test_report_lot_without_snapshot_date_is_malformed(ledger, accts):
    row = _lot(accts["agentic"], "undated", qty=10, basis=20.0)
    del row["snapshot_date"]
    report = _report(ledger, [row], _quotes(ZZZA=15.0), accts)
    from market_data_ingest.ledger.tax_loss import lot_key

    assert report.candidates == () and dict(report.skipped) == {lot_key("undated"): "malformed_lot"}


def test_report_without_wash_sale_inputs_lists_every_blocking_reason(ledger, accts):
    report = _report(ledger, [_lot(accts["agentic"], "big", qty=10, basis=20.0)], _quotes(ZZZA=15.0), accts)
    (cand,) = report.candidates
    assert cand.eligible is False
    assert {"account_history_missing", "option_history_missing", "drip_unknown", "no_approval",
            "outside_accounts_not_attested"} <= set(cand.blocking_reasons)


def test_report_with_a_recent_buy_lists_it_and_the_pause_still_needed(ledger, accts):
    a = accts["agentic"]
    _record_report_approval(ledger)
    inputs = _clean_report_inputs(accts)
    inputs["acquisitions_by_account"][a] = _hist([_buy(date(2026, 10, 28))], covers_from=date(2026, 9, 1))
    report = _report(ledger, [_lot(a, "big", qty=10, basis=20.0)], _quotes(ZZZA=15.0), accts, **inputs)
    (cand,) = report.candidates
    assert cand.blocking_reasons == ("acquisition_in_lookback",)
    assert [x["acquired_date"] for x in cand.washing_acquisitions] == [date(2026, 10, 28)]
    # Last buy Oct 28 -> first clean sale date Oct 28 + 32 = Nov 29; from Nov 2 that is 27 days.
    assert cand.earliest_clean_sale_date == date(2026, 11, 29)
    assert cand.pause_days_needed == 27


def test_report_with_clean_inputs_and_a_recorded_approval_is_eligible(ledger, accts):
    _record_report_approval(ledger)
    _record_report_approval(ledger, approval_id="tlh_other", ticker="ZZZB")
    report = _report(ledger, [_lot(accts["agentic"], "big", qty=10, basis=20.0)], _quotes(ZZZA=15.0), accts,
                     **_clean_report_inputs(accts))
    (cand,) = report.candidates
    assert cand.eligible is True and cand.blocking_reasons == () and cand.pause_days_needed == 0


def test_report_ignores_approvals_for_another_sale_date(ledger, accts):
    _record_report_approval(ledger, sale_date=date(2026, 11, 3), expires_at=utc(2026, 11, 4, 0))
    report = _report(ledger, [_lot(accts["agentic"], "big", qty=10, basis=20.0)], _quotes(ZZZA=15.0), accts,
                     **_clean_report_inputs(accts))
    (cand,) = report.candidates
    assert cand.blocking_reasons == ("no_approval",)


def test_report_and_eligibility_never_write(ledger, accts):
    # They READ approvals from the ledger and write nothing -- not even the approvals table
    # when it does not exist yet (the read path reports "no approval" instead).
    from market_data_ingest.ledger.tax_loss import APPROVALS_TABLE

    before = set(ledger.tables())
    report = _report(ledger, [_lot(accts["agentic"], "big", qty=10, basis=20.0)], _quotes(ZZZA=15.0), accts,
                     **_clean_report_inputs(accts))
    assert report.candidates[0].blocking_reasons == ("no_approval",)
    assert set(ledger.tables()) == before and APPROVALS_TABLE not in before


def test_report_and_eligibility_take_no_broker(ledger):
    import inspect

    from market_data_ingest.ledger.tax_loss import candidate_report, eligibility

    for fn in (candidate_report, eligibility):
        params = set(inspect.signature(fn).parameters)
        assert not params & {"broker", "client", "session", "approval", "approvals"}


# --- approval table ------------------------------------------------------------------------------


def test_approval_table_is_append_only_with_provenance(ledger):
    from market_data_ingest.ledger.tax_loss import APPROVALS_TABLE, ensure_tax_loss_schema, record_approval

    ensure_tax_loss_schema(ledger.con)
    ensure_tax_loss_schema(ledger.con)  # idempotent
    cols = set(ledger.table_columns(APPROVALS_TABLE))
    assert {"approval_id", "ticker", "sale_date", "lot_keys", "max_shares", "expires_at", "approved_at",
            "inputs_known_at", "content_hash", "source", "as_of", "ingested_at", "run_id"} <= cols
    appr = _approval()
    assert record_approval(ledger.con, appr, source="operator_chat", run_id="op-1", as_of=utc(2026, 11, 29, 13)) == 1
    # Identical re-record is dropped by the content hash.
    assert record_approval(ledger.con, appr, source="operator_chat", run_id="op-2", as_of=utc(2026, 11, 29, 14)) == 0
    assert ledger.row_count(APPROVALS_TABLE) == 1


def test_approvals_known_at_is_point_in_time(ledger):
    from dataclasses import replace

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


def test_approvals_known_at_on_a_fresh_ledger_is_empty_and_writes_nothing(ledger):
    from market_data_ingest.ledger.tax_loss import APPROVALS_TABLE, approvals_known_at

    assert approvals_known_at(ledger.con, AT) == []
    assert APPROVALS_TABLE not in ledger.tables()


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
    (cand,) = _report(ledger, ledger.tax_lots(known_at=AS_OF), _quotes(ZZZA=15.0), accts).candidates
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

    source = MODULE.read_text()
    tree = ast.parse(source)
    writers = [
        fn.name for fn in ast.walk(tree) if isinstance(fn, ast.FunctionDef)
        and "INSERT INTO" in ast.get_source_segment(source, fn)
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
