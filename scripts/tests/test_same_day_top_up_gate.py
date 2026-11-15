#!/usr/bin/env python3
"""Tests for the broker-sourced, fail-closed same-day top-up gate.

Every case here is derived from the DOCUMENTED policy, not from any
implementation:

  config/scheduled_tasks.json -> same_day_top_up_policy.summary
    "Each later run buys only the incremental difference
     max(0, desired_amount - prior_same_day_amount), and only when its current
     decision price is strictly below the prior same-day reference price;
     otherwise it buys $0."

  config/scheduled_tasks.json -> same_day_top_up_policy.reference_price_preference
    "A missing reference price (none available) means NO automatic top-up: the
     candidate is classified execution_status='manual_review_candidate'
     (top_up_manual_review=true) at $0."

  config/run_classification.json -> final_buy_behavior.same_day_top_up_guard
    "A missing prior reference => no auto-top-up (conservative $0 /
     manual_review_candidate)."

The policy PERMITS a same-ticker repeat buy on the same day. These tests must
never assert that a repeat is forbidden; they assert it is correctly SIZED and
correctly GATED on a strictly-lower price, and that an undeterminable prior
picture aborts instead of proceeding as if the slate were clean.

The gate is stateless and broker-sourced: its only input about prior same-day
buying is replayed ``get_equity_orders`` pages. No filesystem run-state is
consulted, so every one of the seven slots is visible automatically.
"""

from __future__ import annotations

import json

import pytest

gate = pytest.importorskip(
    "scripts.same_day_top_up_gate",
    reason="scripts/same_day_top_up_gate.py not present",
)

from scripts.reason_codes import (  # noqa: E402
    ALREADY_MET_OR_OVERFILLED,
    BELOW_BROKER_MINIMUM_ORDER_SIZE,
    BROKER_HISTORY_INCOMPLETE,
    EFFECTIVE_TOP_UP,
    FIRST_ALLOCATION,
    MISSING_CURRENT_PRICE,
    MISSING_PRIOR_PRICE,
    NO_LOWER_PRICE,
    UNLOGGED_REFERENCE_PRICE,
)

ACCOUNT = "999000111"  # synthetic; conftest assigns the same value. The CLI now expands and shape-checks --account,
# so a literal ${...} placeholder or a fake like ACC1 is no longer a usable account.
# Derived, never written as a literal: tests/test_config_invariants.py takes no account-shaped literal other than
# the synthetic ACCOUNT (use conftest.FIXTURE_ACCOUNT_NUMBER or keep a number to 5 digits or fewer).
OTHER_ACCOUNT = str(int(ACCOUNT) + 1)
DAY = "2026-07-30"


# --- builders ----------------------------------------------------------------


def _fill(order_id, symbol, usd, price, created_at, *, side="buy"):
    """One filled dollar-based buy, in the shape get_equity_orders returns."""
    qty = usd / price
    return {
        "id": order_id,
        "symbol": symbol,
        "side": side,
        "state": "filled",
        "quantity": f"{qty:.6f}",
        "cumulative_quantity": f"{qty:.6f}",
        "average_price": f"{price:.6f}",
        "dollar_based_amount": {"amount": f"{usd:.6f}", "currency_code": "USD"},
        "created_at": created_at,
        "placed_agent": "agentic",
    }


def _page(orders, *, next_cursor=None):
    body = {"orders": orders}
    if next_cursor is not None:
        body["next"] = next_cursor
    return {"data": body}


def _run(pages, candidates, *, account=ACCOUNT, day=DAY, minimum_order_usd=None, config_dir=None):
    return gate.evaluate_slot(
        pages=pages,
        candidates=candidates,
        account_number=account,
        trading_day=day,
        minimum_order_usd=minimum_order_usd,
        config_dir=config_dir,
    )


def _by_ticker(result):
    return {d["ticker"]: d for d in result["decisions"]}


# --- the policy's four sizing outcomes ---------------------------------------


def test_no_prior_same_day_buy_authorizes_the_full_desired_amount():
    """First touch of a ticker today is a first allocation, not a top-up.

    same_day_top_up_policy sizes a top-up as desired - prior; with prior $0 the
    full desired amount is authorized.
    """
    r = _run([_page([])], [{"ticker": "SPCX", "desired_usd": 1.00, "current_price": 112.10}])
    assert r["verdict"] == "PROCEED"
    d = _by_ticker(r)["SPCX"]
    assert d["buy_usd"] == pytest.approx(1.00)
    assert d["reason"] == FIRST_ALLOCATION
    # No qualifying same-day fill exists to attribute a reference price to, so
    # reference_source must not falsely claim broker_fill provenance (Codex
    # review: "Report no source when no reference exists").
    assert d["reference_price"] is None
    assert d["reference_source"] is None


def test_a_lower_price_and_a_larger_desired_amount_without_a_logged_reference_needs_manual_review():
    """The policy's core arithmetic is buy max(0, desired - prior), nothing
    more -- but without a caller-supplied, order-id-bound logged reference,
    this gate's only reference price is the average FILL price, which a
    market order commonly fills above its own decision price. Authorizing a
    top-up off that unproven, possibly-inflated reference risks over-buying
    (Codex review: "Compare top-ups against the configured reference price"),
    so a would-be top-up with no bound reference goes to manual review at $0
    instead of auto-firing. See test_a_logged_reference_price_still_permits_a_genuine_topup
    for the same arithmetic WITH a bound reference, which still auto-fires.
    """
    pages = [_page([_fill("o1", "TRIN", 1.00, 17.5599, "2026-07-30T13:58:14Z")])]
    r = _run(pages, [{"ticker": "TRIN", "desired_usd": 4.00, "current_price": 17.3199}])
    assert r["verdict"] == "PROCEED"
    d = _by_ticker(r)["TRIN"]
    assert d["buy_usd"] == pytest.approx(0.0)
    assert d["effective_prior_usd"] == pytest.approx(1.00, abs=0.01)
    assert d["reason"] == UNLOGGED_REFERENCE_PRICE
    assert d["manual_review"] is True


def test_a_price_equal_to_the_reference_is_not_strictly_below_and_buys_zero():
    """'strictly below' is strict. Equal is not below.

    This is today's 10:31 NFLX case: same fill price as the 09:58 buy, so the
    top-up invariant blocks it even though dollars remained desired.
    """
    pages = [_page([_fill("o1", "NFLX", 5.00, 70.7899, "2026-07-30T13:58:44Z")])]
    r = _run(pages, [{"ticker": "NFLX", "desired_usd": 9.00, "current_price": 70.7899}])
    d = _by_ticker(r)["NFLX"]
    assert d["buy_usd"] == pytest.approx(0.0)
    assert d["reason"] == NO_LOWER_PRICE


def test_a_desired_amount_at_or_below_the_prior_buys_zero_even_when_price_fell():
    """A lower price does not by itself authorize more dollars.

    Today's 10:31 TRIN and AAPL and 11:32 GOOGL: price was strictly lower, but
    the slot wanted no more than was already deployed, so the incremental is
    zero or negative and the policy buys $0.
    """
    pages = [_page([_fill("o1", "AAPL", 3.00, 331.88, "2026-07-30T13:58:21Z")])]
    r = _run(pages, [{"ticker": "AAPL", "desired_usd": 1.00, "current_price": 330.80}])
    d = _by_ticker(r)["AAPL"]
    assert d["buy_usd"] == pytest.approx(0.0)
    assert d["reason"] == ALREADY_MET_OR_OVERFILLED


def test_top_up_difference_below_broker_minimum_is_skipped_and_recorded():
    """A positive top-up delta that is below the broker minimum order size is skipped.

    When desired ($2.50) exceeds prior ($2.00) at a strictly lower price, the
    computed incremental delta is $0.50. Under a $1.00 broker minimum, this
    un-placeable order must produce buy_usd=0.0 and canonical reason
    BELOW_BROKER_MINIMUM_ORDER_SIZE instead of failing at the broker.
    """
    pages = [_page([_fill("o1", "TRIN", 2.00, 17.5599, "2026-07-30T13:58:14Z")])]
    r = _run(pages, [{"ticker": "TRIN", "desired_usd": 2.50, "current_price": 17.3199}], minimum_order_usd=1.0)
    assert r["verdict"] == "PROCEED"
    d = _by_ticker(r)["TRIN"]
    assert d["buy_usd"] == pytest.approx(0.0)
    assert d["reason"] == BELOW_BROKER_MINIMUM_ORDER_SIZE

    # Under a smaller minimum (e.g. $0.25), the same $0.50 delta is
    # authorized. Bound to the broker's own latest fill (o1, unchanged
    # reference value) so this exercises the minimum-boundary arithmetic
    # itself rather than the separate unbound-reference manual-review path
    # (see test_a_lower_price_and_a_larger_desired_amount_without_a_logged_reference_needs_manual_review).
    r_small = _run(
        pages,
        [
            {
                "ticker": "TRIN",
                "desired_usd": 2.50,
                "current_price": 17.3199,
                "prior_logged_reference_price": 17.5599,
                "prior_logged_reference_order_id": "o1",
            }
        ],
        minimum_order_usd=0.25,
    )
    assert r_small["verdict"] == "PROCEED"
    d_small = _by_ticker(r_small)["TRIN"]
    assert d_small["buy_usd"] == pytest.approx(0.50, abs=0.01)
    assert d_small["reason"] == EFFECTIVE_TOP_UP


def test_top_up_with_float_subtraction_at_exact_minimum_boundary():
    """Cent-quantization prevents float subtraction from skipping an exact minimum buy.

    desired ($2.30) - prior ($1.30) produces 0.9999999999999998 in raw float
    arithmetic. Cent-quantized comparison must treat this delta as exactly
    $1.00 and authorize it. Bound to the broker's own latest fill (o1,
    unchanged reference value) so this exercises the cent-quantization
    boundary itself rather than the separate unbound-reference manual-review
    path (see test_a_lower_price_and_a_larger_desired_amount_without_a_logged_reference_needs_manual_review).
    """
    pages = [_page([_fill("o1", "TRIN", 1.30, 17.5599, "2026-07-30T13:58:14Z")])]
    r = _run(
        pages,
        [
            {
                "ticker": "TRIN",
                "desired_usd": 2.30,
                "current_price": 17.3199,
                "prior_logged_reference_price": 17.5599,
                "prior_logged_reference_order_id": "o1",
            }
        ],
        minimum_order_usd=1.0,
    )
    assert r["verdict"] == "PROCEED"
    d = _by_ticker(r)["TRIN"]
    assert d["buy_usd"] == pytest.approx(1.00, abs=0.01)
    assert d["reason"] == EFFECTIVE_TOP_UP


def test_evaluate_slot_validates_minimum_order_usd():
    """Non-positive and boolean minimum_order_usd must fail fast."""
    pages = [_page([])]
    cands = [{"ticker": "SPCX", "desired_usd": 1.00, "current_price": 112.10}]
    with pytest.raises(ValueError, match="minimum_order_usd must be a positive number"):
        _run(pages, cands, minimum_order_usd=0.0)
    with pytest.raises(ValueError, match="minimum_order_usd must be a positive number"):
        _run(pages, cands, minimum_order_usd=-1.0)
    with pytest.raises(ValueError, match="minimum_order_usd must be a positive number"):
        _run(pages, cands, minimum_order_usd=True)
    with pytest.raises(ValueError, match="minimum_order_usd must be a positive number"):
        _run(pages, cands, minimum_order_usd=False)


def test_evaluate_slot_resolves_minimum_order_usd_from_config(tmp_path):
    """When minimum_order_usd is omitted, load the configured value from config_dir."""
    config_file = tmp_path / "per_ticker_sizing.json"
    config_file.write_text(
        json.dumps(
            {
                "status": "active",
                "minimum_order_usd": 2.0,
                "tickers": {
                    "TRIN": {
                        "base_amount_usd": 1.0,
                        "tiers": [{"min_drop_pct": 0.1, "multiplier": 1.0}],
                    }
                },
            }
        ),
        encoding="utf-8",
    )
    pages = [_page([_fill("o1", "TRIN", 2.00, 17.5599, "2026-07-30T13:58:14Z")])]
    # delta is $1.50, which is < configured $2.00 minimum -> skip
    r = _run(
        pages,
        [{"ticker": "TRIN", "desired_usd": 3.50, "current_price": 17.3199}],
        config_dir=tmp_path,
    )
    assert r["verdict"] == "PROCEED"
    d = _by_ticker(r)["TRIN"]
    assert d["buy_usd"] == pytest.approx(0.0)
    assert d["reason"] == BELOW_BROKER_MINIMUM_ORDER_SIZE


# --- the seven-slot picture --------------------------------------------------


def test_prior_exposure_accumulates_across_every_slot_not_just_the_last_one():
    """Prior is the day's CUMULATIVE exposure, so all seven slots are visible.

    The filesystem guard only ever folded in three legacy streams; a
    broker-sourced guard sees every order the account placed today regardless of
    which slot placed it. Three $1.00 TRIN buys must present as a $3.00 prior.
    """
    pages = [
        _page(
            [
                _fill("o1", "TRIN", 1.00, 17.5599, "2026-07-30T13:58:14Z"),
                _fill("o2", "TRIN", 1.00, 17.4887, "2026-07-30T14:31:47Z"),
                _fill("o3", "TRIN", 1.00, 17.3199, "2026-07-30T15:32:47Z"),
            ]
        )
    ]
    # Bound to the broker's own latest fill (o3, unchanged reference value) so
    # this exercises the cent-accumulation arithmetic itself rather than the
    # separate unbound-reference manual-review path (see
    # test_a_lower_price_and_a_larger_desired_amount_without_a_logged_reference_needs_manual_review
    # for that case).
    r = _run(
        pages,
        [
            {
                "ticker": "TRIN",
                "desired_usd": 5.00,
                "current_price": 17.10,
                "prior_logged_reference_price": 17.3199,
                "prior_logged_reference_order_id": "o3",
            }
        ],
    )
    d = _by_ticker(r)["TRIN"]
    # Exact, not approximate: effective_prior_usd is normalized to cents before
    # the subtraction below, so three genuine $1.00 fills must present as
    # exactly $3.00 -- not $3.0000041175, the raw cumulative_quantity *
    # average_price reconstruction -- and the top-up as exactly $2.00, not
    # $1.99 (Codex review: "Preserve exact cents when rounding the top-up
    # delta").
    assert d["effective_prior_usd"] == 3.00
    assert d["buy_usd"] == 2.00
    assert d["reason"] == EFFECTIVE_TOP_UP


def test_the_reference_price_is_the_lowest_same_day_fill_not_the_most_recent():
    """Beating only the most recent fill is not enough to beat the day.

    With fills at 17.56 and 17.32, a 17.40 decision price is below the FIRST
    fill but above the lowest. Comparing against the lowest is the conservative
    reading of "the prior same-day reference price" and is what blocks a
    dip-chasing top-up placed above a better price already achieved today.
    """
    pages = [
        _page(
            [
                _fill("o1", "TRIN", 1.00, 17.5599, "2026-07-30T13:58:14Z"),
                _fill("o2", "TRIN", 1.00, 17.3199, "2026-07-30T15:32:47Z"),
            ]
        )
    ]
    r = _run(pages, [{"ticker": "TRIN", "desired_usd": 5.00, "current_price": 17.40}])
    d = _by_ticker(r)["TRIN"]
    assert d["reference_price"] == pytest.approx(17.3199)
    assert d["buy_usd"] == pytest.approx(0.0)
    assert d["reason"] == NO_LOWER_PRICE


# --- preferring the caller's logged target/decision price over the fill price -

# config/scheduled_tasks.json -> same_day_top_up_policy.reference_price_preference:
# "The prior same-day reference price is resolved in order: logged
# target_price, then decision_price, then average_fill_price." This gate has
# no artifact access of its own, so BrokerOrder.reference_price is always the
# average FILL price -- which, on positive slippage, can be HIGHER than the
# true logged reference and wrongly authorize a top-up above it (Codex
# review: "Compare top-ups against the configured reference price").


def test_without_a_logged_reference_a_would_be_topup_goes_to_manual_review():
    """Fixed KNOWN LIMITATION: a prior decision at $100 that filled at $101
    leaves this gate no way to tell that a new $100.50 decision is actually
    above the true $100 reference. Without a bound logged reference, this
    used to authorize a top-up a target/decision-price comparison would have
    blocked; it now goes to manual review at $0 instead of auto-firing off
    the unproven, possibly-inflated fill-price reference. A caller that has
    the logged reference should supply prior_logged_reference_price to
    auto-fire correctly (see the next test)."""
    pages = [_page([_fill("o1", "TRIN", 1.00, 101.00, "2026-07-30T13:58:14Z")])]
    r = _run(pages, [{"ticker": "TRIN", "desired_usd": 4.00, "current_price": 100.50}])
    d = _by_ticker(r)["TRIN"]
    assert d["reference_price"] == pytest.approx(101.00)
    assert d["reason"] == UNLOGGED_REFERENCE_PRICE
    assert d["buy_usd"] == 0.0
    assert d["manual_review"] is True


def test_a_logged_reference_price_blocks_a_topup_the_fill_price_would_permit():
    """The fix: when the caller supplies the ticker's actual logged
    target_price/decision_price from the prior buy, BOUND to that same
    order's order_id, this gate compares against THAT instead of the
    inflated fill price, correctly blocking a new decision that is above the
    true reference even though it is below the fill price."""
    pages = [_page([_fill("o1", "TRIN", 1.00, 101.00, "2026-07-30T13:58:14Z")])]
    r = _run(
        pages,
        [
            {
                "ticker": "TRIN",
                "desired_usd": 4.00,
                "current_price": 100.50,
                "prior_logged_reference_price": 100.00,
                "prior_logged_reference_order_id": "o1",
            }
        ],
    )
    d = _by_ticker(r)["TRIN"]
    assert d["reference_price"] == pytest.approx(100.00)
    assert d["reference_order_id"] == "o1"
    assert d["reference_source"] == "logged"
    assert d["logged_reference_bound"] is True
    assert d["buy_usd"] == pytest.approx(0.0)
    assert d["reason"] == NO_LOWER_PRICE


def test_a_logged_reference_price_still_permits_a_genuine_topup():
    """The override is a straight substitution, not an extra restriction: a
    decision genuinely below the logged reference is still authorized, when
    bound to the order it describes."""
    pages = [_page([_fill("o1", "TRIN", 1.00, 101.00, "2026-07-30T13:58:14Z")])]
    r = _run(
        pages,
        [
            {
                "ticker": "TRIN",
                "desired_usd": 4.00,
                "current_price": 99.00,
                "prior_logged_reference_price": 100.00,
                "prior_logged_reference_order_id": "o1",
            }
        ],
    )
    d = _by_ticker(r)["TRIN"]
    assert d["reference_price"] == pytest.approx(100.00)
    assert d["buy_usd"] == pytest.approx(3.00, abs=0.01)
    assert d["reason"] == EFFECTIVE_TOP_UP


def test_a_logged_reference_price_unbound_to_the_latest_order_is_ignored():
    """Codex review: 'Bind logged references to the latest broker order'.

    A logged reference with NO order_id, or one that names an order OTHER
    than the broker's own latest qualifying fill, must not override broker
    history -- it could describe an older, stale buy (e.g. a $100 first
    fill) while a more recent, lower fill ($90) is what the policy actually
    requires comparing against. Falling back to the broker-observed
    reference is the safe (harder-to-authorize) direction.
    """
    pages = [
        _page(
            [
                _fill("o1", "TRIN", 1.00, 100.00, "2026-07-30T13:00:00Z"),
                _fill("o2", "TRIN", 1.00, 90.00, "2026-07-30T14:00:00Z"),
            ]
        )
    ]
    # No order_id at all: falls back to the broker's own latest reference ($90).
    r_no_id = _run(
        pages,
        [
            {
                "ticker": "TRIN",
                "desired_usd": 4.00,
                "current_price": 95.00,
                "prior_logged_reference_price": 100.00,
            }
        ],
    )
    d_no_id = _by_ticker(r_no_id)["TRIN"]
    assert d_no_id["reference_price"] == pytest.approx(90.00)
    assert d_no_id["reason"] == NO_LOWER_PRICE
    assert d_no_id["buy_usd"] == pytest.approx(0.0)

    # order_id names the STALE $100 fill, not the latest $90 one: still ignored.
    r_stale_id = _run(
        pages,
        [
            {
                "ticker": "TRIN",
                "desired_usd": 4.00,
                "current_price": 95.00,
                "prior_logged_reference_price": 100.00,
                "prior_logged_reference_order_id": "o1",
            }
        ],
    )
    d_stale_id = _by_ticker(r_stale_id)["TRIN"]
    assert d_stale_id["reference_price"] == pytest.approx(90.00)
    assert d_stale_id["reference_order_id"] == "o2"
    assert d_stale_id["reference_source"] == "broker_fill"
    assert d_stale_id["logged_reference_bound"] is False
    assert d_stale_id["reason"] == NO_LOWER_PRICE
    assert d_stale_id["buy_usd"] == pytest.approx(0.0)


@pytest.mark.parametrize("bad_value", [0, -5.0, "not-a-number", None, float("nan")])
def test_an_invalid_logged_reference_price_falls_back_to_the_fill_price(bad_value):
    """A non-positive, non-numeric, or non-finite override must not silently
    poison or corrupt the comparison -- it is simply ignored, falling back
    to this gate's own broker-fill-price reference. That fallback reference
    is unproven (never the true logged decision price), so the resulting
    would-be top-up goes to manual review rather than auto-firing."""
    pages = [_page([_fill("o1", "TRIN", 1.00, 17.5599, "2026-07-30T13:58:14Z")])]
    r = _run(
        pages,
        [
            {
                "ticker": "TRIN",
                "desired_usd": 4.00,
                "current_price": 17.3199,
                "prior_logged_reference_price": bad_value,
            }
        ],
    )
    d = _by_ticker(r)["TRIN"]
    assert d["reference_price"] == pytest.approx(17.5599)
    assert d["buy_usd"] == 0.0
    assert d["reason"] == UNLOGGED_REFERENCE_PRICE
    assert d["manual_review"] is True


def test_goog_and_googl_are_scored_as_independent_tickers():
    """Share-class equivalence is an OPEN portfolio-construction decision.

    This test pins today's behavior (distinct tickers, independent priors) so
    that adopting an equivalence list is a deliberate, visible change rather
    than something that drifts in. It is NOT an assertion that treating them
    separately is correct.
    """
    pages = [_page([_fill("o1", "GOOGL", 1.50, 332.0699, "2026-07-30T13:58:27Z")])]
    r = _run(pages, [{"ticker": "GOOG", "desired_usd": 1.50, "current_price": 331.5199}])
    d = _by_ticker(r)["GOOG"]
    assert d["effective_prior_usd"] == pytest.approx(0.0)
    assert d["reason"] == FIRST_ALLOCATION


# --- fail closed -------------------------------------------------------------


def test_an_unfollowed_pagination_cursor_aborts_the_whole_slot():
    """Unproven-complete history must abort, never authorize a full buy.

    A final page still advertising ``next`` means orders exist that were never
    read. The failure mode being closed here is precisely the one that let
    today's repeats through: an empty-looking prior picture read as "nothing
    bought yet".
    """
    pages = [_page([], next_cursor="cursor-page-2")]
    r = _run(pages, [{"ticker": "TRIN", "desired_usd": 4.00, "current_price": 17.32}])
    assert r["verdict"] == "ABORT"
    assert r["reason"] == BROKER_HISTORY_INCOMPLETE
    assert all(d["buy_usd"] == 0.0 for d in r["decisions"])


def test_an_error_shaped_order_response_aborts_the_whole_slot():
    """An error page is not an empty history."""
    r = _run(
        [{"error": "upstream timeout"}],
        [{"ticker": "TRIN", "desired_usd": 4.00, "current_price": 17.32}],
    )
    assert r["verdict"] == "ABORT"
    assert r["reason"] == BROKER_HISTORY_INCOMPLETE
    assert all(d["buy_usd"] == 0.0 for d in r["decisions"])


def test_zero_saved_pages_aborts_rather_than_reading_as_a_clean_slate():
    """No pages at all is missing evidence, not proof of no prior buying.

    This is the fail-open shape that a missing filesystem state root produced.
    """
    r = _run([], [{"ticker": "TRIN", "desired_usd": 4.00, "current_price": 17.32}])
    assert r["verdict"] == "ABORT"
    assert r["reason"] == BROKER_HISTORY_INCOMPLETE


def test_a_working_unfilled_order_poisons_the_reference_and_buys_zero():
    """An order whose fill price is not yet knowable cannot be compared against.

    reference_price_preference: a missing reference means $0 and manual review,
    never a full-size buy.
    """
    working = {
        "id": "o1",
        "symbol": "TRIN",
        "side": "buy",
        "state": "confirmed",
        "dollar_based_amount": {"amount": "1.000000", "currency_code": "USD"},
        "created_at": "2026-07-30T13:58:14Z",
        "placed_agent": "agentic",
    }
    r = _run([_page([working])], [{"ticker": "TRIN", "desired_usd": 4.00, "current_price": 17.10}])
    d = _by_ticker(r)["TRIN"]
    assert d["buy_usd"] == pytest.approx(0.0)
    assert d["reason"] == MISSING_PRIOR_PRICE
    assert d["manual_review"] is True


def test_a_candidate_with_no_current_price_buys_zero_when_a_prior_exists():
    """Without a decision price the strictly-below invariant cannot be evaluated."""
    pages = [_page([_fill("o1", "TRIN", 1.00, 17.5599, "2026-07-30T13:58:14Z")])]
    r = _run(pages, [{"ticker": "TRIN", "desired_usd": 4.00, "current_price": None}])
    d = _by_ticker(r)["TRIN"]
    assert d["buy_usd"] == pytest.approx(0.0)
    assert d["manual_review"] is True


# --- which orders count as prior exposure ------------------------------------


def test_a_cancelled_order_with_no_fill_is_not_prior_exposure():
    """A rejected or cancelled buy that never filled deployed no dollars.

    It must not suppress a legitimate first allocation.
    """
    dead = {
        "id": "o1",
        "symbol": "TRIN",
        "side": "buy",
        "state": "cancelled",
        "cumulative_quantity": "0.000000",
        "dollar_based_amount": {"amount": "1.000000", "currency_code": "USD"},
        "created_at": "2026-07-30T13:58:14Z",
    }
    r = _run([_page([dead])], [{"ticker": "TRIN", "desired_usd": 4.00, "current_price": 17.10}])
    d = _by_ticker(r)["TRIN"]
    assert d["effective_prior_usd"] == pytest.approx(0.0)
    assert d["buy_usd"] == pytest.approx(4.00)


def test_a_cancelled_order_that_partially_filled_counts_and_blocks_pending_review():
    """Dollars that actually settled count as prior exposure, whatever the state.

    A cancelled-with-partial-fill order carries real deployed dollars but no
    CONFIRMED full-fill price, so there is no trustworthy reference to compare
    against. The policy's ``missing_prior_price`` branch applies: $0 and manual
    review, not a top-up sized against a price that was never achieved.
    """
    partial = {
        "id": "o1",
        "symbol": "TRIN",
        "side": "buy",
        "state": "cancelled",
        "cumulative_quantity": "0.100000",
        "average_price": "17.000000",
        "dollar_based_amount": {"amount": "4.000000", "currency_code": "USD"},
        "created_at": "2026-07-30T13:58:14Z",
    }
    r = _run([_page([partial])], [{"ticker": "TRIN", "desired_usd": 4.00, "current_price": 16.50}])
    d = _by_ticker(r)["TRIN"]
    assert d["effective_prior_usd"] == pytest.approx(1.70)
    assert d["buy_usd"] == pytest.approx(0.0)
    assert d["reason"] == MISSING_PRIOR_PRICE
    assert d["manual_review"] is True


def test_a_same_day_sell_is_not_counted_as_prior_buy_exposure():
    """This is an accumulate-only strategy; a sell is not a prior allocation."""
    pages = [_page([_fill("o1", "TRIN", 1.00, 17.5599, "2026-07-30T13:58:14Z", side="sell")])]
    r = _run(pages, [{"ticker": "TRIN", "desired_usd": 4.00, "current_price": 17.10}])
    d = _by_ticker(r)["TRIN"]
    assert d["effective_prior_usd"] == pytest.approx(0.0)
    assert d["buy_usd"] == pytest.approx(4.00)


def test_the_account_number_must_be_supplied():
    """Account pinning: a snapshot of the wrong account proves nothing."""
    with pytest.raises(ValueError):
        _run([_page([])], [{"ticker": "TRIN", "desired_usd": 1.0, "current_price": 17.0}], account="")


def test_the_trading_day_must_be_supplied():
    """An empty trading_day would let same-day reconciliation scope its
    created_at_gte filter to nothing, treating an unbounded history window
    as proof no buy occurred today (Codex review: "Reject an empty trading
    day before replaying pages")."""
    with pytest.raises(ValueError):
        _run([_page([])], [{"ticker": "TRIN", "desired_usd": 1.0, "current_price": 17.0}], day="")


# --- the 2026-07-30 regression -----------------------------------------------

# The twelve orders the seven live slots actually placed on 2026-07-30, in
# submission order, as (order_id, symbol, usd, avg_fill_price, created_at UTC).
_JUL30_FILLS = [
    ("6a6b5876", "TRIN", 1.00, 17.5599, "2026-07-30T13:58:14Z"),
    ("6a6b587c", "AAPL", 3.00, 331.8800, "2026-07-30T13:58:21Z"),
    ("6a6b5883", "GOOGL", 1.50, 332.0699, "2026-07-30T13:58:27Z"),
    ("6a6b588d", "GOOG", 1.50, 331.5199, "2026-07-30T13:58:37Z"),
    ("6a6b5894", "NFLX", 5.00, 70.7899, "2026-07-30T13:58:44Z"),
    ("6a6b589a", "IBM", 4.00, 221.9299, "2026-07-30T13:58:50Z"),
    ("6a6b6053", "TRIN", 1.00, 17.4887, "2026-07-30T14:31:47Z"),
    ("6a6b605b", "AAPL", 1.00, 330.8000, "2026-07-30T14:31:55Z"),
    ("6a6b6061", "NFLX", 1.00, 70.7899, "2026-07-30T14:32:01Z"),
    ("6a6b6e9f", "TRIN", 1.00, 17.3199, "2026-07-30T15:32:47Z"),
    ("6a6b6ea9", "GOOGL", 1.50, 331.7699, "2026-07-30T15:32:57Z"),
    ("6a6b98f0", "SPCX", 1.00, 112.0999, "2026-07-30T18:33:20Z"),
]

# Each slot's candidates, reconstructed from what it actually placed: because
# the guard failed open, every slot placed its full desired amount, so the
# placed notional IS that slot's desired_usd and the achieved fill price is the
# best available proxy for its decision price.
# (slot label, half-open index range into _JUL30_FILLS) in firing order.
_JUL30_SLOTS = [
    ("0935", 0, 6),   # fired 09:43, placed 09:58 ET
    ("1030", 6, 9),   # placed 10:31 ET
    ("1130", 9, 11),  # placed 11:32 ET
    ("1430", 11, 12),  # placed 14:33 ET
]


def _replay_jul30():
    """Replay 2026-07-30 slot by slot, feeding each slot the real prior fills.

    Each slot sees exactly the orders submitted before it -- which is what a
    broker-sourced guard reads from get_equity_orders at that moment.
    """
    placed, total = [], 0.0
    for _slot, start, stop in _JUL30_SLOTS:
        prior_rows = [_fill(oid, sym, usd, px, ts) for oid, sym, usd, px, ts in _JUL30_FILLS[:start]]
        candidates = [
            {"ticker": sym, "desired_usd": usd, "current_price": px}
            for _oid, sym, usd, px, _ts in _JUL30_FILLS[start:stop]
        ]
        r = _run([_page(prior_rows)], candidates)
        assert r["verdict"] == "PROCEED"
        for d in r["decisions"]:
            if d["buy_usd"] > 0:
                placed.append((d["ticker"], round(d["buy_usd"], 2)))
                total += d["buy_usd"]
    return placed, total


def test_jul30_replay_permits_every_first_touch_unchanged():
    """The six 09:58 first-touch buys and the 14:33 SPCX buy stay exactly as placed.

    The fix must not suppress legitimate DCA; it must only correct repeats.
    """
    placed, _ = _replay_jul30()
    assert placed == [
        ("TRIN", 1.00),
        ("AAPL", 3.00),
        ("GOOGL", 1.50),
        ("GOOG", 1.50),
        ("NFLX", 5.00),
        ("IBM", 4.00),
        ("SPCX", 1.00),
    ]


def test_jul30_replay_skips_all_five_repeat_buys_and_spends_17_dollars():
    """$22.50 across 12 orders becomes $17.00 across 7 under the working guard.

    None of the five repeats is resized to a nonzero partial: every later slot
    wanted no more than was already deployed for that ticker, so the policy's
    delta arithmetic lands on $0 rather than a haircut.
    """
    placed, total = _replay_jul30()
    assert len(placed) == 7
    assert total == pytest.approx(17.00)


# --- reject candidates with no usable decision price --------------------------
#
# config/scheduled_tasks.json -> same_day_top_up_policy.reference_price_preference:
# "A missing reference price (none available) means NO automatic top-up."
# Gate 6.5 cannot run at all without ITS OWN decision price, which matters even
# on a FIRST allocation (no prior same-day buy): incremental_top_up_amount only
# checks current_price AFTER finding a prior, so a malformed candidate for a
# never-bought-today ticker must be caught here, before it ever reaches that
# function, or it would receive the full desired amount unchecked.


def test_a_missing_current_price_buys_zero_even_on_a_first_allocation():
    """No prior buy today for SPCX, and no decision price either -- must NOT
    be treated as a clean first allocation."""
    r = _run([_page([])], [{"ticker": "SPCX", "desired_usd": 1.00, "current_price": None}])
    assert r["verdict"] == "PROCEED"
    d = _by_ticker(r)["SPCX"]
    assert d["buy_usd"] == 0.0
    assert d["reason"] == MISSING_CURRENT_PRICE
    assert d["manual_review"] is True


@pytest.mark.parametrize("bad_price", [float("nan"), float("inf"), float("-inf"), "not-a-number"])
def test_a_non_finite_or_nonnumeric_current_price_is_treated_as_missing(bad_price):
    """inf/nan parse successfully through a bare float() call; they must not
    silently survive as a comparable decision price."""
    r = _run([_page([])], [{"ticker": "SPCX", "desired_usd": 1.00, "current_price": bad_price}])
    d = _by_ticker(r)["SPCX"]
    assert d["buy_usd"] == 0.0
    assert d["reason"] == MISSING_CURRENT_PRICE
    assert d["manual_review"] is True


# --- deduplicate replayed orders before reconciliation -------------------------


def test_the_same_order_repeated_across_a_page_boundary_is_not_double_counted():
    """Pagination can repeat an order at a page boundary; a saved response
    array can also contain a duplicated page. Either way, the SAME order id
    must count once, not once per appearance. Bound to that same order
    (unchanged reference value) so this exercises the dedup itself rather
    than the separate unbound-reference manual-review path."""
    fill = _fill("o1", "TRIN", 1.00, 17.5599, "2026-07-30T13:58:14Z")
    pages = [
        {"data": {"orders": [fill], "next_cursor": "page-2"}},
        {"data": {"orders": [fill]}},
    ]
    r = _run(
        pages,
        [
            {
                "ticker": "TRIN",
                "desired_usd": 4.00,
                "current_price": 17.0,
                "prior_logged_reference_price": 17.5599,
                "prior_logged_reference_order_id": "o1",
            }
        ],
    )
    assert r["verdict"] == "PROCEED"
    assert r["orders_in_snapshot"] == 1
    d = _by_ticker(r)["TRIN"]
    assert d["effective_prior_usd"] == pytest.approx(1.00, abs=0.01)
    assert d["buy_usd"] == pytest.approx(3.00, abs=0.01)


# --- reject unconsumed saved order pages ---------------------------------------


def test_an_unconsumed_trailing_saved_page_aborts_the_whole_slot():
    """A well-formed, EMPTY first page (no continuation pointer) makes
    pagination stop and report complete=True -- but a second saved page still
    sitting in the array proves the saved evidence is malformed, not that the
    account bought nothing today. Silently discarding it would let an
    out-of-order or duplicated ``--orders-pages`` array produce a clean-looking
    empty history and a full first allocation."""
    pages = [
        _page([]),
        _page([_fill("o1", "TRIN", 1.00, 17.5599, "2026-07-30T13:58:14Z")]),
    ]
    r = _run(pages, [{"ticker": "TRIN", "desired_usd": 4.00, "current_price": 17.0}])
    assert r["verdict"] == "ABORT"
    assert r["reason"] == BROKER_HISTORY_INCOMPLETE
    assert all(d["buy_usd"] == 0.0 for d in r["decisions"])


def test_every_saved_page_consumed_still_proceeds_normally():
    """Sanity check for the check above: a properly paginated, fully-consumed
    two-page history must still PROCEED."""
    fill1 = _fill("o1", "TRIN", 1.00, 17.5599, "2026-07-30T13:58:14Z")
    fill2 = _fill("o2", "AAPL", 1.00, 331.88, "2026-07-30T14:31:00Z")
    pages = [
        {"data": {"orders": [fill1], "next_cursor": "page-2"}},
        {"data": {"orders": [fill2]}},
    ]
    r = _run(pages, [{"ticker": "TRIN", "desired_usd": 4.00, "current_price": 17.0}])
    assert r["verdict"] == "PROCEED"
    assert r["orders_in_snapshot"] == 2


# --- compare against the most recent valid reference price, not the minimum ---
#
# config/scheduled_tasks.json -> same_day_top_up_policy.final_override_top_up:
# "the comparison reference is that ticker's most recent valid same-day buy
# reference (ordered by generated_at)."


def test_reference_price_is_the_most_recent_fill_not_the_minimum():
    """Fills at $90 then $100 (in that order): a later $95 decision is below
    the MOST RECENT $100 reference and must top up, even though $90 -- the
    minimum of the two -- is also below $95. Comparing against the minimum
    would incorrectly block this and change the documented experiment
    strategy (Codex review: "Compare against the most recent valid reference
    price"). Bound to the broker's own latest fill (o2, unchanged reference
    value) so this exercises the most-recent-vs-minimum selection itself
    rather than the separate unbound-reference manual-review path."""
    pages = [
        _page(
            [
                _fill("o1", "TRIN", 1.00, 90.00, "2026-07-31T13:00:00Z"),
                _fill("o2", "TRIN", 1.00, 100.00, "2026-07-31T14:00:00Z"),
            ]
        )
    ]
    r = _run(
        pages,
        [
            {
                "ticker": "TRIN",
                "desired_usd": 5.00,
                "current_price": 95.00,
                "prior_logged_reference_price": 100.00,
                "prior_logged_reference_order_id": "o2",
            }
        ],
        day="2026-07-31",
    )
    d = _by_ticker(r)["TRIN"]
    assert d["reference_price"] == pytest.approx(100.00)
    assert d["reason"] == EFFECTIVE_TOP_UP
    assert d["buy_usd"] == pytest.approx(3.00)


def test_reference_price_ignores_fill_order_in_the_saved_array_and_uses_created_at():
    """The most-recent selection is by created_at, not by array position: the
    $100 fill is listed FIRST here and the $90 fill SECOND, yet $90 (later by
    time) is still the correct reference."""
    pages = [
        _page(
            [
                _fill("o1", "TRIN", 1.00, 100.00, "2026-07-31T09:00:00Z"),
                _fill("o2", "TRIN", 1.00, 90.00, "2026-07-31T15:00:00Z"),
            ]
        )
    ]
    r = _run(pages, [{"ticker": "TRIN", "desired_usd": 5.00, "current_price": 95.00}], day="2026-07-31")
    d = _by_ticker(r)["TRIN"]
    assert d["reference_price"] == pytest.approx(90.00)
    assert d["reason"] == NO_LOWER_PRICE
    assert d["buy_usd"] == 0.0


def test_a_pending_order_still_poisons_the_most_recent_reference():
    """The most-recent override must not weaken the existing fail-closed
    poisoning rule: a still-working (unfilled) order's eventual price is not
    yet knowable, so the reference must stay None regardless of any earlier
    confirmed fill."""
    confirmed = _fill("o1", "TRIN", 1.00, 90.00, "2026-07-31T13:00:00Z")
    working = {
        "id": "o2",
        "symbol": "TRIN",
        "side": "buy",
        "state": "confirmed",
        "dollar_based_amount": {"amount": "1.000000", "currency_code": "USD"},
        "created_at": "2026-07-31T14:00:00Z",
        "placed_agent": "agentic",
    }
    r = _run([_page([confirmed, working])], [{"ticker": "TRIN", "desired_usd": 5.00, "current_price": 80.00}], day="2026-07-31")
    d = _by_ticker(r)["TRIN"]
    assert d["reference_price"] is None
    assert d["reason"] == MISSING_PRIOR_PRICE
    assert d["manual_review"] is True


# --- bind replayed pages to the requested account and day (main()'s CLI) ------
#
# evaluate_slot() itself trusts account_number/trading_day as arguments its
# caller already asserts; main() is where a FILE (potentially saved from a
# different account or an earlier day) actually enters the system, so the
# binding check belongs at that boundary.


def _write_json(path, payload):
    path.write_text(json.dumps(payload), encoding="utf-8")
    return str(path)


def _pinned_pages(pages, *, account=ACCOUNT, day=DAY):
    """The current --orders-pages contract: query metadata bound to the
    saved evidence (see _bind_saved_pages / "Bind replayed pages to the
    requested account and day"). A bare array is the pre-pinning shape and
    is rejected -- see test_main_rejects_the_pre_pinning_bare_array_shape."""
    return {"account_number": account, "trading_day": day, "pages": pages}


# --- CLI: the exit code IS the safety contract --------------------------------
#
# evaluate_slot() is well covered above; nothing exercised main() itself before
# this section, which means the part an executor prompt actually depends on --
# "Exit status is 0 on PROCEED and 2 on ABORT" (module docstring) -- had no
# regression protection. A `return 0` slipping into the ABORT branch would pass
# every test above and still authorize a run that thinks the gate blocked it.


def test_cli_exits_zero_and_prints_proceed_on_clean_history(tmp_path, capsys):
    pages_path = _write_json(tmp_path / "pages.json", _pinned_pages([_page([])]))
    candidates_path = _write_json(
        tmp_path / "candidates.json", [{"ticker": "SPCX", "desired_usd": 1.00, "current_price": 112.10}]
    )
    rc = gate.main(
        [
            "--account",
            ACCOUNT,
            "--trading-day",
            DAY,
            "--orders-pages",
            pages_path,
            "--candidates",
            candidates_path,
        ]
    )
    assert rc == 0
    out = json.loads(capsys.readouterr().out)
    assert out["verdict"] == "PROCEED"


def test_cli_exits_two_and_prints_abort_on_incomplete_history(tmp_path, capsys):
    pages_path = _write_json(tmp_path / "pages.json", _pinned_pages([]))
    candidates_path = _write_json(
        tmp_path / "candidates.json", [{"ticker": "TRIN", "desired_usd": 4.00, "current_price": 17.32}]
    )
    rc = gate.main(
        [
            "--account",
            ACCOUNT,
            "--trading-day",
            DAY,
            "--orders-pages",
            pages_path,
            "--candidates",
            candidates_path,
        ]
    )
    assert rc == 2
    out = json.loads(capsys.readouterr().out)
    assert out["verdict"] == "ABORT"
    assert out["reason"] == BROKER_HISTORY_INCOMPLETE
    assert all(d["buy_usd"] == 0.0 for d in out["decisions"])


def test_cli_exits_two_on_unreadable_orders_pages_file(tmp_path, capsys):
    """A missing/unparsable input file must abort, not crash or read as empty."""
    candidates_path = _write_json(
        tmp_path / "candidates.json", [{"ticker": "TRIN", "desired_usd": 4.00, "current_price": 17.32}]
    )
    rc = gate.main(
        [
            "--account",
            ACCOUNT,
            "--trading-day",
            DAY,
            "--orders-pages",
            str(tmp_path / "does-not-exist.json"),
            "--candidates",
            candidates_path,
        ]
    )
    assert rc == 2
    out = json.loads(capsys.readouterr().out)
    assert out["verdict"] == "ABORT"
    assert out["reason"] == BROKER_HISTORY_INCOMPLETE
    assert out["decisions"] == []


def test_cli_exits_two_on_malformed_json_input(tmp_path, capsys):
    pages_path = tmp_path / "pages.json"
    pages_path.write_text("{not valid json", encoding="utf-8")
    candidates_path = _write_json(
        tmp_path / "candidates.json", [{"ticker": "TRIN", "desired_usd": 4.00, "current_price": 17.32}]
    )
    rc = gate.main(
        [
            "--account",
            ACCOUNT,
            "--trading-day",
            DAY,
            "--orders-pages",
            str(pages_path),
            "--candidates",
            candidates_path,
        ]
    )
    assert rc == 2
    out = json.loads(capsys.readouterr().out)
    assert out["verdict"] == "ABORT"
    assert out["reason"] == BROKER_HISTORY_INCOMPLETE


def test_cli_exits_two_when_orders_pages_object_has_no_pages_array(tmp_path, capsys):
    """A well-formed JSON object with none of the expected keys (no "pages",
    no account/day metadata) must abort via the same binding check as a bare
    array (test_main_rejects_the_pre_pinning_bare_array_shape) -- it just
    fails a different isinstance check inside _bind_saved_pages."""
    pages_path = _write_json(tmp_path / "pages.json", {"not": "the expected shape"})
    candidates_path = _write_json(
        tmp_path / "candidates.json", [{"ticker": "TRIN", "desired_usd": 4.00, "current_price": 17.32}]
    )
    rc = gate.main(
        [
            "--account",
            ACCOUNT,
            "--trading-day",
            DAY,
            "--orders-pages",
            pages_path,
            "--candidates",
            candidates_path,
        ]
    )
    assert rc == 2
    out = json.loads(capsys.readouterr().out)
    assert out["verdict"] == "ABORT"
    assert out["reason"] == BROKER_HISTORY_INCOMPLETE


def test_cli_exits_two_when_candidates_is_not_a_json_array(tmp_path, capsys):
    pages_path = _write_json(tmp_path / "pages.json", _pinned_pages([_page([])]))
    candidates_path = _write_json(tmp_path / "candidates.json", {"not": "a list"})
    rc = gate.main(
        [
            "--account",
            ACCOUNT,
            "--trading-day",
            DAY,
            "--orders-pages",
            pages_path,
            "--candidates",
            candidates_path,
        ]
    )
    assert rc == 2
    out = json.loads(capsys.readouterr().out)
    assert out["verdict"] == "ABORT"
    assert out["reason"] == BROKER_HISTORY_INCOMPLETE


def test_main_proceeds_when_the_saved_metadata_matches(tmp_path, capsys):
    orders_path = tmp_path / "orders.json"
    candidates_path = tmp_path / "candidates.json"
    _write_json(orders_path, {"account_number": ACCOUNT, "trading_day": "2026-07-31", "pages": [_page([])]})
    _write_json(candidates_path, [{"ticker": "SPCX", "desired_usd": 1.0, "current_price": 100.0}])
    exit_code = gate.main(
        [
            "--account", ACCOUNT,
            "--trading-day", "2026-07-31",
            "--orders-pages", str(orders_path),
            "--candidates", str(candidates_path),
        ]
    )
    assert exit_code == 0
    assert json.loads(capsys.readouterr().out)["verdict"] == "PROCEED"


def test_main_rejects_orders_pages_saved_for_a_different_account(tmp_path, capsys):
    """A well-formed, complete-looking snapshot proves nothing about the
    account being traded unless it is actually pinned to it (Codex review:
    "Bind replayed pages to the requested account and day")."""
    orders_path = tmp_path / "orders.json"
    candidates_path = tmp_path / "candidates.json"
    _write_json(orders_path, {"account_number": OTHER_ACCOUNT, "trading_day": "2026-07-31", "pages": [_page([])]})
    _write_json(candidates_path, [{"ticker": "SPCX", "desired_usd": 1.0, "current_price": 100.0}])
    exit_code = gate.main(
        [
            "--account", ACCOUNT,
            "--trading-day", "2026-07-31",
            "--orders-pages", str(orders_path),
            "--candidates", str(candidates_path),
        ]
    )
    assert exit_code == 2
    result = json.loads(capsys.readouterr().out)
    assert result["verdict"] == "ABORT"
    assert result["reason"] == BROKER_HISTORY_INCOMPLETE


def test_main_rejects_orders_pages_saved_for_a_different_trading_day(tmp_path, capsys):
    orders_path = tmp_path / "orders.json"
    candidates_path = tmp_path / "candidates.json"
    _write_json(orders_path, {"account_number": ACCOUNT, "trading_day": "2026-07-30", "pages": [_page([])]})
    _write_json(candidates_path, [{"ticker": "SPCX", "desired_usd": 1.0, "current_price": 100.0}])
    exit_code = gate.main(
        [
            "--account", ACCOUNT,
            "--trading-day", "2026-07-31",
            "--orders-pages", str(orders_path),
            "--candidates", str(candidates_path),
        ]
    )
    assert exit_code == 2
    assert json.loads(capsys.readouterr().out)["verdict"] == "ABORT"


def test_main_rejects_the_pre_pinning_bare_array_shape(tmp_path, capsys):
    """The old ``--orders-pages`` contract (a bare JSON array with no query
    metadata) must not be silently trusted -- there is nothing to check it
    against, which is exactly the gap being closed."""
    orders_path = tmp_path / "orders.json"
    candidates_path = tmp_path / "candidates.json"
    _write_json(orders_path, [_page([])])
    _write_json(candidates_path, [{"ticker": "SPCX", "desired_usd": 1.0, "current_price": 100.0}])
    exit_code = gate.main(
        [
            "--account", ACCOUNT,
            "--trading-day", "2026-07-31",
            "--orders-pages", str(orders_path),
            "--candidates", str(candidates_path),
        ]
    )
    assert exit_code == 2
    assert json.loads(capsys.readouterr().out)["verdict"] == "ABORT"


def test_a_visible_prior_fill_without_average_price_is_not_missing_prior_price():
    """2026-09-22 2:30 PM: MA was bought earlier that day and the fill was in
    the saved history, yet the gate reported MISSING_PRIOR_PRICE. A fill whose
    price is derivable (here: executed_notional / cumulative_quantity, with no
    average_price field) must yield a reference, so the decision is the
    policy's own answer -- here NO_LOWER_PRICE, since the current price is
    above the $500 fill -- not a fail-closed "unknown"."""
    fill = {
        "id": "ma-935",
        "symbol": "MA",
        "side": "buy",
        "state": "filled",
        "cumulative_quantity": "0.008000",
        "executed_notional": {"amount": "4.00", "currency_code": "USD"},
        "dollar_based_amount": {"amount": "4.00", "currency_code": "USD"},
        "created_at": "2026-07-30T13:35:10Z",
    }
    r = _run([_page([fill])], [{"ticker": "MA", "desired_usd": 4.00, "current_price": 505.00}])
    assert r["verdict"] == "PROCEED"
    d = _by_ticker(r)["MA"]
    assert d["reason"] != MISSING_PRIOR_PRICE
    assert d["reason"] in (ALREADY_MET_OR_OVERFILLED, NO_LOWER_PRICE)
    assert d["reference_price"] == pytest.approx(500.0)
    assert d["buy_usd"] == pytest.approx(0.0)
