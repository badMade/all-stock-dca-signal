"""Tests for the unified reason-code vocabulary."""

from __future__ import annotations

from scripts import reason_codes as rc


def test_all_reasons_are_valid_and_unknown_is_not():
    for code in rc.ALL_REASONS:
        assert rc.is_valid_reason(code)
    assert not rc.is_valid_reason("not_a_real_code")


def test_from_top_up_guard_maps_every_known_guard_result():
    assert rc.from_top_up_guard("no_prior_run") == rc.FIRST_ALLOCATION
    assert rc.from_top_up_guard("incremental_top_up") == rc.EFFECTIVE_TOP_UP
    assert rc.from_top_up_guard("price_not_below_prior_run") == rc.NO_LOWER_PRICE
    assert rc.from_top_up_guard("no_incremental_difference") == rc.ALREADY_MET_OR_OVERFILLED
    assert rc.from_top_up_guard("missing_prior_price") == rc.MISSING_PRIOR_PRICE
    assert rc.from_top_up_guard("missing_prior_base") == rc.MISSING_PRIOR_PRICE


def test_from_top_up_guard_passes_unknown_through_unchanged():
    assert rc.from_top_up_guard("some_new_code") == "some_new_code"


def test_fail_closed_classification():
    assert rc.is_fail_closed(rc.BROKER_HISTORY_INCOMPLETE)
    assert rc.is_fail_closed(rc.SAFETY_CAP)
    # A benign no-buy is not a fail-closed block.
    assert not rc.is_fail_closed(rc.NO_LOWER_PRICE)
    assert not rc.is_fail_closed(rc.FIRST_ALLOCATION)


def test_no_buy_reasons_exclude_the_buy_outcomes():
    assert rc.FIRST_ALLOCATION not in rc.NO_BUY_REASONS
    assert rc.EFFECTIVE_TOP_UP not in rc.NO_BUY_REASONS
    assert rc.SUBMITTED not in rc.NO_BUY_REASONS
    assert rc.NO_LOWER_PRICE in rc.NO_BUY_REASONS
    assert rc.BROKER_HISTORY_INCOMPLETE in rc.NO_BUY_REASONS


def test_dry_run_is_a_no_buy_reason():
    assert rc.DRY_RUN in rc.NO_BUY_REASONS
    # But it is not a fail-closed safety block.
    assert not rc.is_fail_closed(rc.DRY_RUN)
