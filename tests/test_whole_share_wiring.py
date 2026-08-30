"""End-to-end tests for the whole-share accumulation wiring through
near_close_dca._process_buy_candidate's dispatch to
_process_whole_share_candidate (see docs/whole_share_accumulation.md).

Mirrors test_inverse_martingale_wiring.py's structure: each test isolates
the ledger's on-disk state via $DCA_CRON_TRACKING_ROOT (the module always
constructs WholeShareAccumulationLedger() bare, with no root override, so
this env var is the only isolation lever).
"""

import logging
from dataclasses import replace
from datetime import date, datetime, time, timedelta

import pytest

import near_close_dca as _near_close_dca
from dca_whole_share_accumulator import WholeShareAccumulationLedger
from near_close_dca import (
    EASTERN,
    NORMAL_WEEKLY_CAP_USD,
    Action,
    Bar,
    CompactRunOptions,
    DryRunBroker,
    OrderConfirmationRequiredError,
    OrderFill,
    ProcessCandidateContext,
    ReconciledDecision,
    RunContext,
    RunResult,
    SourceName,
    SourceSignal,
    StrategyConfig,
    StrategyState,
    WholeSharePlacementTerminalError,
    _build_buy_candidates,
    _process_buy_candidate,
    _process_buy_candidates_batch,
    _reconcile_cross_day_whole_share_claims_for_batch,
    _whole_share_redispatch_reconciled_price,
    run_daily_compact,
    run_once,
)


class _FakeSignalData:
    """Minimal MarketDataProvider double producing a controlled small drop
    with a stable bid/ask -- mirrors test_near_close_dca.py's own
    _FakeData/_FakeDataWithBidAsk, kept local so this file has no
    intra-test-file coupling."""

    def __init__(self, source, close_price, prev_close, bid_ask=(9.98, 10.00)):
        self.source = source
        self._close = close_price
        self._prev = prev_close
        self._bid_ask = bid_ask

    def get_previous_close_bar(self, ticker, as_of):
        ts = datetime.combine(as_of.date() - timedelta(days=1), time(16, 0), tzinfo=EASTERN)
        return Bar(ts, self._prev, self._prev, self._prev, self._prev, 1000)

    def get_intraday_bars(self, ticker, start, end, interval_minutes):
        bars = []
        cursor = start
        late_threshold = end - timedelta(minutes=30)
        while cursor <= end:
            price = self._close if cursor >= late_threshold else self._close + 2.0
            bars.append(Bar(cursor, price, price, price, price, 100))
            cursor += timedelta(minutes=interval_minutes)
        return bars

    def get_quote(self, ticker, as_of):
        return (self._close, as_of)

    def get_bid_ask(self, ticker, as_of):
        return self._bid_ask


class _TightSpreadData:
    """Minimal MarketDataProvider double exposing a tight bid/ask spread,
    so tests reach past the scheduled-run spread gate (see
    near_close_dca.quote_spread_pct/_quote_bid_ask)."""
    source = SourceName.ROBINHOOD

    def get_bid_ask(self, ticker, as_of):
        return (9.99, 10.01)

    def get_previous_close_bar(self, ticker, as_of):
        return None


_UNSET = object()


def _ctx(config, broker, *, scheduled=True, run_result=None, robinhood_data: Any = _UNSET):
    return ProcessCandidateContext(
        config=config,
        state=StrategyState(),
        broker=broker,
        robinhood_data=_TightSpreadData() if robinhood_data is _UNSET else robinhood_data,
        now=datetime(2026, 8, 16, 15, 45, tzinfo=EASTERN),
        trading_day=date(2026, 8, 16),
        guard_active=False,
        prior_same_day_allocations={},
        scheduled=scheduled,
        run_result=run_result,
    )


def _decision(*, action=Action.BUY, suggested_amount_usd=3.0, target_price=10.0):
    return ReconciledDecision(
        ticker="BYDDY",
        action=action,
        reason="test",
        finance_signal=None,
        robinhood_signal=None,
        buy_multiplier=1.0 if action == Action.BUY else 0.0,
        suggested_amount_usd=suggested_amount_usd,
        selected_base_amount_usd=1.0,
        target_price=target_price,
    )


def test_below_threshold_contribution_accumulates_without_buying(tmp_path, monkeypatch):
    monkeypatch.setenv("DCA_CRON_TRACKING_ROOT", str(tmp_path / "cron_tracking"))
    config = StrategyConfig(whole_share_tickers=frozenset({"BYDDY"}), dry_run=False)
    broker = DryRunBroker(cash=500.0)

    outcome, top_up, _, errors, _ = _process_buy_candidate(
        _decision(suggested_amount_usd=3.0, target_price=10.0),
        ctx=_ctx(config, broker),
        running_deployed_7d=0.0,
    )

    # "accumulating" isn't in schemas/run_state.schema.json's candidate
    # status enum (Codex review: "Use a schema-supported status while
    # accumulating") -- "skipped" (already enum-valid, already used
    # elsewhere for benign no-action outcomes) carries the same meaning.
    assert outcome["status"] == "skipped"
    assert outcome["whole_share_pending_usd"] == 3.0
    assert top_up is None
    assert errors == 0
    ledger = WholeShareAccumulationLedger()
    assert ledger.pending_usd("BYDDY") == 3.0
    assert ledger.shares_purchased_total("BYDDY") == 0


def test_contribution_crossing_share_price_places_a_live_whole_share_order(tmp_path, monkeypatch):
    monkeypatch.setenv("DCA_CRON_TRACKING_ROOT", str(tmp_path / "cron_tracking"))
    config = StrategyConfig(whole_share_tickers=frozenset({"BYDDY"}), dry_run=False)
    broker = DryRunBroker(cash=500.0, reference_prices={"BYDDY": 10.0})

    outcome, top_up, running_deployed_7d, errors, _ = _process_buy_candidate(
        _decision(suggested_amount_usd=10.0, target_price=10.0),
        ctx=_ctx(config, broker),
        running_deployed_7d=0.0,
    )

    assert outcome["status"] == "submitted"
    assert outcome["whole_share_quantity"] == 1
    assert outcome["submitted_amount_usd"] == 10.0
    assert outcome["order_id"]
    assert top_up is None
    assert errors == 0
    assert running_deployed_7d == 10.0

    ledger = WholeShareAccumulationLedger()
    assert ledger.pending_usd("BYDDY") == 0.0


def test_whole_share_ledger_settles_only_the_confirmed_fill(tmp_path, monkeypatch):
    """The post-placement reconciliation path settles using the broker's own
    CONFIRMED terminal order state (an exact-ref_id
    ``same_day_strategy_order_exists_with_ref_id_prefix`` match reporting
    ``cumulative_quantity``), not a separate, possibly-stale
    ``get_order_fill()`` re-query -- see ``_process_whole_share_candidate``'s
    own "place_whole_share_buy returning without raising only means the
    broker ACCEPTED the order" comment. Whole shares are always integer
    counts (Robinhood has no fractional whole-share unit), so a genuine
    "partial fill" here means fewer WHOLE shares landed than requested (a
    2-share order that only fills 1) -- ``settle()``'s own shortfall math
    restores the unbought share's credit to ``pending_usd``, mirroring
    ``_VerifiedFillAboveEstimateBroker``'s identical override pattern above.
    """
    monkeypatch.setenv("DCA_CRON_TRACKING_ROOT", str(tmp_path / "cron_tracking"))

    class PartialFillBroker(DryRunBroker):
        def same_day_strategy_order_exists_with_ref_id_prefix(self, ticker, trading_day, ref_id_prefixes):
            if ref_id_prefixes == ("graded-dca-wholeshare-2026-08-16-BYDDY",):
                return {"id": "RH-PARTIAL-FILL-1", "state": "filled", "cumulative_quantity": "1", "average_price": "10"}
            return None

    broker = PartialFillBroker(cash=500.0, reference_prices={"BYDDY": 10.0})
    # $20 at a $10 share price plans a 2-share order; only 1 share actually
    # fills (per PartialFillBroker's confirmed terminal state above).
    outcome, _, _, _, _ = _process_buy_candidate(
        _decision(suggested_amount_usd=20.0, target_price=10.0),
        ctx=_ctx(StrategyConfig(whole_share_tickers=frozenset({"BYDDY"}), dry_run=False), broker),
        running_deployed_7d=0.0,
    )

    ledger = WholeShareAccumulationLedger()
    assert outcome["status"] == "submitted"
    assert ledger.shares_purchased_total("BYDDY") == 1
    # The second, unfilled share's $10 credit is restored to pending_usd.
    assert ledger.pending_usd("BYDDY") == 10.0


def test_ordinary_success_path_defers_settlement_for_a_still_live_order(tmp_path, monkeypatch):
    """Codex review ("Keep ordinary nonterminal submissions unresolved" and
    "Record accepted nonterminal orders as submitted"): when
    place_whole_share_buy returns WITHOUT raising, that only means the
    broker ACCEPTED the order -- not that it filled. A nonterminal state
    (new/queued/confirmed/partially_filled) means the final executed
    quantity is not yet known, exactly the same ambiguity the placement-
    EXCEPTION reconciliation branch already guards against, so the whole-
    share LEDGER's own claim must stay reserved (settle() must not be
    called) until a later reconciliation attempt finds it terminal. But the
    broker DID accept a real, live order -- reporting it as "blocked"
    (an earlier version of this fix did) hid it from
    run_result.execution_records/submitted_orders.csv/fill-coverage
    reporting entirely, contrary to what an accepted-but-unfilled
    whole-share order should look like. The candidate outcome is
    "submitted" (using the locally-planned quantity/notional, since the
    real fill isn't known yet) while the ledger claim stays unresolved.
    """
    monkeypatch.setenv("DCA_CRON_TRACKING_ROOT", str(tmp_path / "cron_tracking"))
    config = StrategyConfig(whole_share_tickers=frozenset({"BYDDY"}), dry_run=False)

    class _StillLiveAfterPlacementBroker(DryRunBroker):
        def same_day_strategy_order_exists_with_ref_id_prefix(self, ticker, trading_day, ref_id_prefixes):
            if ref_id_prefixes == ("graded-dca-wholeshare-2026-08-16-BYDDY",):
                return {"id": "RH-STILL-LIVE-1", "state": "confirmed", "cumulative_quantity": "0"}
            return None

    broker = _StillLiveAfterPlacementBroker(cash=500.0, reference_prices={"BYDDY": 10.0})
    run_result = RunResult(trading_day=date(2026, 8, 16), decisions=[])

    outcome, top_up, running_deployed_7d, errors, _ = _process_buy_candidate(
        _decision(suggested_amount_usd=10.0, target_price=10.0),
        ctx=_ctx(config, broker, run_result=run_result),
        running_deployed_7d=0.0,
    )

    assert outcome["status"] == "submitted"
    assert outcome["whole_share_settlement_reconciliation_needed"] is True
    assert top_up is None
    assert running_deployed_7d == 10.0
    # An accepted-but-unfilled order still gets a real execution record, so
    # fill-coverage reporting doesn't treat it as though nothing happened.
    assert len(run_result.execution_records) == 1

    ledger = WholeShareAccumulationLedger()
    # Still deducted (a real order was placed) but NOT settled -- the claim
    # stays unresolved for a later reconciliation attempt once the order
    # actually terminates.
    assert ledger.pending_usd("BYDDY") == 0.0
    assert ledger.shares_purchased_total("BYDDY") == 0
    assert ledger.unresolved_claim("BYDDY", "2026-08-16", "afternoon-BYDDY") is not None


def test_ordinary_success_path_defers_settlement_when_order_not_yet_visible(tmp_path, monkeypatch):
    """Codex review ("Keep claims reserved when accepted orders are not yet
    visible" and "Record accepted nonterminal orders as submitted"): a
    clean `None` from the post-placement exact-ref lookup is NOT proof the
    order failed -- Robinhood's own order-history endpoint is eventually
    consistent, so a just-accepted order may simply not be indexed yet
    while still genuinely working. The ledger's own claim stays reserved
    exactly like the nonterminal-match case, but the outcome is still
    reported "submitted" (a real order genuinely was placed), not
    "blocked".
    """
    monkeypatch.setenv("DCA_CRON_TRACKING_ROOT", str(tmp_path / "cron_tracking"))
    config = StrategyConfig(whole_share_tickers=frozenset({"BYDDY"}), dry_run=False)

    class _NotYetVisibleAfterPlacementBroker(DryRunBroker):
        def same_day_strategy_order_exists_with_ref_id_prefix(self, ticker, trading_day, ref_id_prefixes):
            return None  # eventual-consistency gap -- not a failure signal

    broker = _NotYetVisibleAfterPlacementBroker(cash=500.0, reference_prices={"BYDDY": 10.0})

    outcome, top_up, running_deployed_7d, errors, _ = _process_buy_candidate(
        _decision(suggested_amount_usd=10.0, target_price=10.0),
        ctx=_ctx(config, broker),
        running_deployed_7d=0.0,
    )

    assert outcome["status"] == "submitted"
    assert outcome["whole_share_settlement_reconciliation_needed"] is True
    assert top_up is None
    assert running_deployed_7d == 10.0

    ledger = WholeShareAccumulationLedger()
    assert ledger.pending_usd("BYDDY") == 0.0
    assert ledger.shares_purchased_total("BYDDY") == 0
    assert ledger.unresolved_claim("BYDDY", "2026-08-16", "afternoon-BYDDY") is not None


def test_ordinary_success_path_books_the_accepted_order_when_state_verification_raises(
    tmp_path, monkeypatch
):
    """Codex review ("Book accepted orders when state verification
    raises"): place_whole_share_buy() already returned a real, live
    order_id before this post-placement state-verification lookup even
    runs -- the broker genuinely accepted and is holding this order. A
    RAISED lookup here (network/API outage) correctly leaves the ledger
    claim itself unresolved (its true terminal state genuinely could not
    be verified), but reporting the candidate as "blocked" is misleading
    and has a real cost: a "blocked" outcome never calls record_buy() or
    bumps running_deployed_7d, and prior-allocation loading excludes
    "blocked" rows entirely, so later candidates in the same batch (or a
    future run reconstructing the day's attempts from the persisted
    artifact) could exceed the daily attempt count, daily/weekly notional
    caps, or the kill switch while this accepted order remains live and
    uncounted. Must be accounted for and reported exactly like the clean
    "not yet visible"/nonterminal branch.
    """
    monkeypatch.setenv("DCA_CRON_TRACKING_ROOT", str(tmp_path / "cron_tracking"))
    config = StrategyConfig(whole_share_tickers=frozenset({"BYDDY"}), dry_run=False)

    class _StateVerificationRaisesBroker(DryRunBroker):
        def same_day_strategy_order_exists_with_ref_id_prefix(self, ticker, trading_day, ref_id_prefixes):
            raise RuntimeError("order history temporarily unavailable")

    broker = _StateVerificationRaisesBroker(cash=500.0, reference_prices={"BYDDY": 10.0})

    outcome, top_up, running_deployed_7d, errors, _ = _process_buy_candidate(
        _decision(suggested_amount_usd=10.0, target_price=10.0),
        ctx=_ctx(config, broker),
        running_deployed_7d=0.0,
    )

    assert outcome["status"] == "submitted"
    assert outcome["order_id"] is not None
    assert outcome["whole_share_settlement_reconciliation_needed"] is True
    assert top_up is None
    assert errors == 1
    # The accepted order's notional is counted toward this run's own
    # deployment tracking, even though its true terminal state is unknown.
    assert running_deployed_7d == 10.0

    ledger = WholeShareAccumulationLedger()
    # The ledger claim is genuinely left unresolved for a later
    # reconciliation attempt -- its true state was never confirmed.
    assert ledger.unresolved_claim("BYDDY", "2026-08-16", "afternoon-BYDDY") is not None


def test_ordinary_success_path_books_the_verified_fill_price(tmp_path, monkeypatch):
    """Codex review ("Book the verified fill price on ordinary successful
    orders"): when the post-placement lookup confirms a filled
    quantity-based market order whose average_price differs from the
    signal-time quote this candidate was sized against, the booked
    notional/state must reflect the REAL verified fill, not the locally
    estimated one -- otherwise budget state, artifacts, and the kill switch
    report the wrong amount actually deployed.
    """
    monkeypatch.setenv("DCA_CRON_TRACKING_ROOT", str(tmp_path / "cron_tracking"))
    config = StrategyConfig(whole_share_tickers=frozenset({"BYDDY"}), dry_run=False)

    class _VerifiedFillAboveEstimateBroker(DryRunBroker):
        def same_day_strategy_order_exists_with_ref_id_prefix(self, ticker, trading_day, ref_id_prefixes):
            if ref_id_prefixes == ("graded-dca-wholeshare-2026-08-16-BYDDY",):
                return {"id": "RH-VERIFIED-FILL-1", "state": "filled", "cumulative_quantity": "1", "average_price": "10.35"}
            return None

    broker = _VerifiedFillAboveEstimateBroker(cash=500.0, reference_prices={"BYDDY": 10.0})

    outcome, _, running_deployed_7d, errors, _ = _process_buy_candidate(
        _decision(suggested_amount_usd=10.0, target_price=10.0),
        ctx=_ctx(config, broker),
        running_deployed_7d=0.0,
    )

    assert outcome["status"] == "submitted"
    # Booked using the REAL $10.35 fill price, not the $10.00 estimate.
    assert outcome["submitted_amount_usd"] == 10.35
    assert errors == 0
    assert running_deployed_7d == 10.35
    ledger = WholeShareAccumulationLedger()
    assert ledger.shares_purchased_total("BYDDY") == 1


def test_an_explicit_zero_fill_is_not_replaced_by_the_requested_quantity(tmp_path, monkeypatch):
    """Codex review, PR #786 ("Reject invalid cumulative quantities before
    settling"): the ordinary-success branch read `cumulative_quantity` via
    `... or ... .get("quantity")`, so an explicit `cumulative_quantity: 0`
    -- falsy, exactly like an absent key -- fell through to the order's own
    REQUESTED `quantity` field. A confirmed `{"state": "filled",
    "cumulative_quantity": 0, "quantity": 1}` then booked a full-share spend
    for a fill the broker explicitly reported as zero.
    """
    monkeypatch.setenv("DCA_CRON_TRACKING_ROOT", str(tmp_path / "cron_tracking"))
    config = StrategyConfig(whole_share_tickers=frozenset({"BYDDY"}), dry_run=False)

    class _ExplicitZeroFillBroker(DryRunBroker):
        def same_day_strategy_order_exists_with_ref_id_prefix(self, ticker, trading_day, ref_id_prefixes):
            if ref_id_prefixes == ("graded-dca-wholeshare-2026-08-16-BYDDY",):
                return {
                    "id": "RH-ZERO-FILL-1", "state": "filled",
                    "cumulative_quantity": 0, "quantity": 1, "average_price": "10.00",
                }
            return None

    broker = _ExplicitZeroFillBroker(cash=500.0, reference_prices={"BYDDY": 10.0})

    outcome, _, running_deployed_7d, errors, _ = _process_buy_candidate(
        _decision(suggested_amount_usd=10.0, target_price=10.0),
        ctx=_ctx(config, broker),
        running_deployed_7d=0.0,
    )

    assert outcome["status"] == "submitted"
    # Booked using the explicit ZERO fill, not the requested 1-share quantity.
    assert outcome["submitted_amount_usd"] == 0.0
    assert outcome["whole_share_quantity"] == 0
    assert running_deployed_7d == 0.0
    ledger = WholeShareAccumulationLedger()
    assert ledger.shares_purchased_total("BYDDY") == 0


def test_a_negative_cumulative_quantity_is_rejected_not_booked(tmp_path, monkeypatch):
    """Sibling case to the zero-fill fix above: a NEGATIVE
    `cumulative_quantity` is truthy, so the same broken `or` accepted it
    outright, and multiplying a negative quantity into `order_notional`
    could reduce cap/deployment accounting rather than merely mis-booking a
    single order. `_finite_nonnegative_float` rejects it, falling back to
    the locally-planned quantity exactly as an absent or unreadable figure
    already does.
    """
    monkeypatch.setenv("DCA_CRON_TRACKING_ROOT", str(tmp_path / "cron_tracking"))
    config = StrategyConfig(whole_share_tickers=frozenset({"BYDDY"}), dry_run=False)

    class _NegativeFillBroker(DryRunBroker):
        def same_day_strategy_order_exists_with_ref_id_prefix(self, ticker, trading_day, ref_id_prefixes):
            if ref_id_prefixes == ("graded-dca-wholeshare-2026-08-16-BYDDY",):
                return {
                    "id": "RH-NEGATIVE-FILL-1", "state": "filled",
                    "cumulative_quantity": -1, "quantity": 1, "average_price": "10.00",
                }
            return None

    broker = _NegativeFillBroker(cash=500.0, reference_prices={"BYDDY": 10.0})

    outcome, _, running_deployed_7d, errors, _ = _process_buy_candidate(
        _decision(suggested_amount_usd=10.0, target_price=10.0),
        ctx=_ctx(config, broker),
        running_deployed_7d=0.0,
    )

    assert outcome["status"] == "submitted"
    # A negative fill is rejected, not booked -- falls back to the
    # locally-planned 1-share estimate at the confirmed $10.00 fill price.
    assert outcome["submitted_amount_usd"] == 10.00
    assert outcome["whole_share_quantity"] == 1
    # Codex review, PR #786 ("Keep invalid filled quantities unresolved"):
    # a PRESENT but unreadable/negative quantity is contradictory broker
    # evidence, not silence -- the claim must be flagged for reconciliation
    # rather than silently settled against the locally-planned guess.
    assert outcome["whole_share_settlement_reconciliation_needed"] is True
    ledger = WholeShareAccumulationLedger()
    assert ledger.shares_purchased_total("BYDDY") == 0
    unresolved = ledger.unresolved_claim("BYDDY", "2026-08-16", "afternoon-BYDDY")
    assert unresolved is not None and unresolved["resolved"] is False


def test_an_unreadable_filled_quantity_leaves_the_claim_unresolved(tmp_path, monkeypatch):
    """Codex review, PR #786 ("Keep invalid filled quantities unresolved").

    `_finite_nonnegative_float` rejecting a supplied `cumulative_quantity`
    did not change control flow: `quantity` retained the locally-requested
    share count and the code settled the ledger and booked that full
    notional anyway, exactly as if the quantity had been genuinely absent.
    A PRESENT invalid execution quantity ("unknown", NaN, a container) is
    CONTRADICTORY broker evidence on a confirmed-filled order, not silence
    about the fill -- settling against an unverified guess can misstate the
    ledger by a real amount nobody can currently prove. The claim must be
    left unresolved (neither settled nor restored) for manual reconciliation.
    """
    monkeypatch.setenv("DCA_CRON_TRACKING_ROOT", str(tmp_path / "cron_tracking"))
    config = StrategyConfig(whole_share_tickers=frozenset({"BYDDY"}), dry_run=False)

    class _UnreadableFillBroker(DryRunBroker):
        def same_day_strategy_order_exists_with_ref_id_prefix(self, ticker, trading_day, ref_id_prefixes):
            if ref_id_prefixes == ("graded-dca-wholeshare-2026-08-16-BYDDY",):
                return {
                    "id": "RH-UNREADABLE-FILL-1", "state": "filled",
                    "cumulative_quantity": "unknown", "average_price": "10.00",
                }
            return None

    broker = _UnreadableFillBroker(cash=500.0, reference_prices={"BYDDY": 10.0})

    outcome, _, running_deployed_7d, errors, _ = _process_buy_candidate(
        _decision(suggested_amount_usd=10.0, target_price=10.0),
        ctx=_ctx(config, broker),
        running_deployed_7d=0.0,
    )

    assert outcome["status"] == "submitted"
    assert outcome["whole_share_settlement_reconciliation_needed"] is True
    ledger = WholeShareAccumulationLedger()
    assert ledger.shares_purchased_total("BYDDY") == 0
    unresolved = ledger.unresolved_claim("BYDDY", "2026-08-16", "afternoon-BYDDY")
    assert unresolved is not None and unresolved["resolved"] is False


def test_an_explicit_null_filled_quantity_leaves_the_claim_unresolved(tmp_path, monkeypatch):
    """Codex review, PR #786 ("Treat explicit null fill quantities as
    unresolved").

    An explicit JSON `cumulative_quantity: null` is Python `None` -- exactly
    the same value `raw_filled_quantity` holds when the key is genuinely
    ABSENT. Reading `raw_filled_quantity is not None` to decide whether the
    quantity evidence is unreadable therefore let a `state: "filled"` order
    supplying that exact null bypass the round-67 guard entirely: `quantity`
    kept the locally-planned estimate and settle() ran anyway, exactly the
    bug round 67 was meant to close. The fix must track PRESENCE of the key
    separately from the truthiness of its value.
    """
    monkeypatch.setenv("DCA_CRON_TRACKING_ROOT", str(tmp_path / "cron_tracking"))
    config = StrategyConfig(whole_share_tickers=frozenset({"BYDDY"}), dry_run=False)

    class _NullFillBroker(DryRunBroker):
        def same_day_strategy_order_exists_with_ref_id_prefix(self, ticker, trading_day, ref_id_prefixes):
            if ref_id_prefixes == ("graded-dca-wholeshare-2026-08-16-BYDDY",):
                return {
                    "id": "RH-NULL-FILL-1", "state": "filled",
                    "cumulative_quantity": None, "average_price": "10.00",
                }
            return None

    broker = _NullFillBroker(cash=500.0, reference_prices={"BYDDY": 10.0})

    outcome, _, running_deployed_7d, errors, _ = _process_buy_candidate(
        _decision(suggested_amount_usd=10.0, target_price=10.0),
        ctx=_ctx(config, broker),
        running_deployed_7d=0.0,
    )

    assert outcome["status"] == "submitted"
    assert outcome["whole_share_settlement_reconciliation_needed"] is True
    ledger = WholeShareAccumulationLedger()
    assert ledger.shares_purchased_total("BYDDY") == 0
    unresolved = ledger.unresolved_claim("BYDDY", "2026-08-16", "afternoon-BYDDY")
    assert unresolved is not None and unresolved["resolved"] is False


def test_an_absent_filled_quantity_still_settles_normally(tmp_path, monkeypatch):
    """An ordinary ABSENT quantity is a DIFFERENT answer from a present but
    unreadable one, and unaffected by the fix above: silence about the fill
    still falls back to the locally-planned estimate and settles normally.
    """
    monkeypatch.setenv("DCA_CRON_TRACKING_ROOT", str(tmp_path / "cron_tracking"))
    config = StrategyConfig(whole_share_tickers=frozenset({"BYDDY"}), dry_run=False)

    class _SilentFillBroker(DryRunBroker):
        def same_day_strategy_order_exists_with_ref_id_prefix(self, ticker, trading_day, ref_id_prefixes):
            if ref_id_prefixes == ("graded-dca-wholeshare-2026-08-16-BYDDY",):
                return {"id": "RH-SILENT-FILL-1", "state": "filled", "average_price": "10.00"}
            return None

    broker = _SilentFillBroker(cash=500.0, reference_prices={"BYDDY": 10.0})
    outcome, _, _, _, _ = _process_buy_candidate(
        _decision(suggested_amount_usd=10.0, target_price=10.0),
        ctx=_ctx(config, broker),
        running_deployed_7d=0.0,
    )
    assert outcome["status"] == "submitted"
    assert outcome["whole_share_settlement_reconciliation_needed"] is False
    ledger = WholeShareAccumulationLedger()
    assert ledger.shares_purchased_total("BYDDY") == 1


def test_execution_record_is_logged_on_success(tmp_path, monkeypatch):
    monkeypatch.setenv("DCA_CRON_TRACKING_ROOT", str(tmp_path / "cron_tracking"))
    from near_close_dca import RunResult

    config = StrategyConfig(whole_share_tickers=frozenset({"BYDDY"}), dry_run=False)
    broker = DryRunBroker(cash=500.0, reference_prices={"BYDDY": 10.0})
    run_result = RunResult(trading_day=date(2026, 8, 16), decisions=[])

    outcome, _, _, _, _ = _process_buy_candidate(
        _decision(suggested_amount_usd=10.0, target_price=10.0),
        ctx=_ctx(config, broker, run_result=run_result),
        running_deployed_7d=0.0,
    )

    assert outcome["status"] == "submitted"
    # Codex review ("Record successful whole-share orders in execution
    # reports"): a submitted whole-share order must appear in
    # RunResult.execution_records like any other placed order, or
    # write_execution_csv/slippage reporting silently omits it.
    assert len(run_result.execution_records) == 1
    assert run_result.execution_records[0].ticker == "BYDDY"


def test_execution_record_uses_the_known_fill_not_a_broker_requery(tmp_path, monkeypatch):
    """Codex review ("Pass the verified fill into execution logging"): the
    ordinary post-placement success path already reads the broker's own
    fill price/quantity off the confirmed order (placed_order) to compute
    order_notional -- but was then letting _log_execution re-query
    broker.get_order_fill() for the SAME order, an eventually-consistent
    lookup that can disagree with (or simply miss) evidence this function
    already has. Passing known_fill= makes the execution record use the
    SAME authoritative fill this function already verified, matching every
    other whole-share reconciliation branch's identical known_fill pattern.
    """
    monkeypatch.setenv("DCA_CRON_TRACKING_ROOT", str(tmp_path / "cron_tracking"))
    from near_close_dca import RunResult

    config = StrategyConfig(whole_share_tickers=frozenset({"BYDDY"}), dry_run=False)

    class _StaleRequeryBroker(DryRunBroker):
        def get_order_fill(self, order_id):
            # A re-query would report a DIFFERENT (stale/wrong) price than
            # what the confirmed order (placed_order) actually carries --
            # proves the execution record used known_fill instead.
            return OrderFill(avg_fill_price=1.0, quantity_filled=999.0)

    broker = _StaleRequeryBroker(cash=500.0, reference_prices={"BYDDY": 10.0})
    run_result = RunResult(trading_day=date(2026, 8, 16), decisions=[])

    outcome, _, _, _, _ = _process_buy_candidate(
        _decision(suggested_amount_usd=10.0, target_price=10.0),
        ctx=_ctx(config, broker, run_result=run_result),
        running_deployed_7d=0.0,
    )

    assert outcome["status"] == "submitted"
    assert len(run_result.execution_records) == 1
    record = run_result.execution_records[0]
    # The real fill (from placed_order's own average_price/quantity, $10.0
    # for 1 share), NOT the broker's stale/wrong re-query response.
    assert record.avg_fill_price == pytest.approx(10.0)
    assert record.quantity_filled == pytest.approx(1.0)


def test_dry_run_never_places_and_never_mutates_the_ledger(tmp_path, monkeypatch):
    monkeypatch.setenv("DCA_CRON_TRACKING_ROOT", str(tmp_path / "cron_tracking"))
    config = StrategyConfig(whole_share_tickers=frozenset({"BYDDY"}), dry_run=True)
    broker = DryRunBroker(cash=500.0, reference_prices={"BYDDY": 10.0})

    outcome, _, _, _, _ = _process_buy_candidate(
        _decision(suggested_amount_usd=10.0, target_price=10.0),
        ctx=_ctx(config, broker),
        running_deployed_7d=0.0,
    )

    assert outcome["status"] == "dry_run"
    assert outcome["whole_share_quantity"] == 1
    # Codex review ("Avoid mutating the accumulator during dry runs"): a
    # dry run must never touch the real durable ledger at all -- not
    # write-then-restore, just never write. Starting from an empty ledger,
    # it stays empty.
    ledger = WholeShareAccumulationLedger()
    assert ledger.pending_usd("BYDDY") == 0.0
    assert ledger.shares_purchased_total("BYDDY") == 0


def test_dry_run_never_reconciles_a_cross_day_unresolved_claim(tmp_path, monkeypatch):
    """Codex review ("Keep cross-day reconciliation read-only during dry
    runs"): a dry-run/shadow invocation must never durably resolve a REAL
    unresolved claim from an earlier trading day, even when the broker
    could confirm an exact ref-id match for it -- settling would change
    the claim's pending balance, bump shares_purchased_total, and append
    an event, all violating the dry-run path's own non-mutation guarantee
    and letting a demo alter what a LATER live run would see.
    """
    monkeypatch.setenv("DCA_CRON_TRACKING_ROOT", str(tmp_path / "cron_tracking"))
    ledger = WholeShareAccumulationLedger()
    ledger.record_contribution(
        "BYDDY",
        "2026-08-15",
        10.0,
        10.0,
        stream="afternoon",
        idempotency_key="afternoon-BYDDY",
        recorded_at="2026-08-15T15:30:00-04:00",
    )
    assert len(ledger.unresolved_claims_any_day("BYDDY", "afternoon-BYDDY")) == 1

    config = StrategyConfig(whole_share_tickers=frozenset({"BYDDY"}), dry_run=True)

    class _CrossDayConfirmingBroker(DryRunBroker):
        def same_day_strategy_order_exists_with_ref_id_prefix(self, ticker, trading_day, ref_id_prefixes):
            if ref_id_prefixes == ("graded-dca-wholeshare-2026-08-15-BYDDY",):
                return {"id": "RH-YESTERDAY-1", "state": "filled", "quantity": "1"}
            return None

    broker = _CrossDayConfirmingBroker(cash=500.0, reference_prices={"BYDDY": 10.0})

    _process_buy_candidate(
        _decision(suggested_amount_usd=3.0, target_price=10.0),
        ctx=_ctx(config, broker),
        running_deployed_7d=0.0,
    )

    # Yesterday's claim is untouched -- still unresolved, no shares booked.
    assert len(ledger.unresolved_claims_any_day("BYDDY", "afternoon-BYDDY")) == 1
    assert ledger.shares_purchased_total("BYDDY") == 0


def test_dry_run_success_persists_the_simulated_carry_forward_balance(tmp_path, monkeypatch):
    """Codex review ("Persist accumulator details for successful dry-run
    fires"): a dry run that crosses the share threshold and fires must
    report whole_share_pending_usd/whole_share_contribution_usd, exactly
    like the below-threshold preview and the live success path both
    already do -- otherwise _build_buy_candidates writes both as null even
    though the SAME preview decision already carries the simulated
    carry-forward balance and contribution.
    """
    monkeypatch.setenv("DCA_CRON_TRACKING_ROOT", str(tmp_path / "cron_tracking"))
    config = StrategyConfig(whole_share_tickers=frozenset({"BYDDY"}), dry_run=True)
    broker = DryRunBroker(cash=500.0, reference_prices={"BYDDY": 10.0})

    outcome, _, _, _, _ = _process_buy_candidate(
        _decision(suggested_amount_usd=16.0, target_price=10.0),
        ctx=_ctx(config, broker),
        running_deployed_7d=0.0,
    )

    assert outcome["status"] == "dry_run"
    assert outcome["whole_share_quantity"] == 1
    assert outcome["whole_share_contribution_usd"] == 16.0
    assert outcome["whole_share_pending_usd"] == 6.0
    ledger = WholeShareAccumulationLedger()
    assert ledger.pending_usd("BYDDY") == 0.0  # dry run never writes


def test_dry_run_below_threshold_is_a_pure_readonly_preview(tmp_path, monkeypatch):
    monkeypatch.setenv("DCA_CRON_TRACKING_ROOT", str(tmp_path / "cron_tracking"))
    config = StrategyConfig(whole_share_tickers=frozenset({"BYDDY"}), dry_run=True)
    broker = DryRunBroker(cash=500.0)

    outcome, _, _, _, _ = _process_buy_candidate(
        _decision(suggested_amount_usd=3.0, target_price=10.0),
        ctx=_ctx(config, broker),
        running_deployed_7d=0.0,
    )

    assert outcome["status"] == "skipped"
    assert outcome["whole_share_pending_usd"] == 3.0
    ledger = WholeShareAccumulationLedger()
    assert ledger.pending_usd("BYDDY") == 0.0  # never written


def test_dry_run_does_not_block_a_later_real_scheduled_run(tmp_path, monkeypatch):
    """A repeated --demo/dry-run invocation must never durably resolve the
    day's idempotency key -- a later REAL scheduled run for the same
    ticker/stream/day must still be able to record its own contribution
    and, if it crosses the threshold, fire for real.
    """
    monkeypatch.setenv("DCA_CRON_TRACKING_ROOT", str(tmp_path / "cron_tracking"))
    dry_config = StrategyConfig(whole_share_tickers=frozenset({"BYDDY"}), dry_run=True)
    dry_broker = DryRunBroker(cash=500.0, reference_prices={"BYDDY": 10.0})
    _process_buy_candidate(
        _decision(suggested_amount_usd=10.0, target_price=10.0),
        ctx=_ctx(dry_config, dry_broker),
        running_deployed_7d=0.0,
    )

    real_config = StrategyConfig(whole_share_tickers=frozenset({"BYDDY"}), dry_run=False)
    real_broker = DryRunBroker(cash=500.0, reference_prices={"BYDDY": 10.0})
    outcome, _, _, _, _ = _process_buy_candidate(
        _decision(suggested_amount_usd=10.0, target_price=10.0),
        ctx=_ctx(real_config, real_broker),
        running_deployed_7d=0.0,
    )

    assert outcome["status"] == "submitted"
    ledger = WholeShareAccumulationLedger()
    assert ledger.shares_purchased_total("BYDDY") == 1


def test_dry_run_preview_is_clamped_by_the_max_per_order_ceiling(tmp_path, monkeypatch):
    # Codex review: "Apply safety gates to whole-share dry-run previews" --
    # a dry run must never report a full-quantity order the identical live
    # run would clamp. $16 at $4/share previews 4 shares, but the $10
    # ceiling only allows 2.
    monkeypatch.setenv("DCA_CRON_TRACKING_ROOT", str(tmp_path / "cron_tracking"))
    config = StrategyConfig(
        whole_share_tickers=frozenset({"BYDDY"}), dry_run=True, max_per_order_usd=10.0
    )
    broker = DryRunBroker(cash=500.0, reference_prices={"BYDDY": 4.0})

    outcome, _, _, _, _ = _process_buy_candidate(
        _decision(suggested_amount_usd=16.0, target_price=4.0),
        ctx=_ctx(config, broker),
        running_deployed_7d=0.0,
    )

    assert outcome["status"] == "dry_run"
    assert outcome["whole_share_quantity"] == 2
    assert outcome["submitted_amount_usd"] == 8.0
    # Codex review ("Restore clamped credit in dry-run pending previews"):
    # the pre-clamp preview was 4 shares/$0 remainder; the ceiling clamped
    # it to 2 shares/$8, so the unsubmitted $8 must be added back onto the
    # reported simulated carry-forward balance -- the equivalent live path
    # restores that exact shortfall during settle().
    assert outcome["whole_share_pending_usd"] == 8.0
    ledger = WholeShareAccumulationLedger()
    assert ledger.pending_usd("BYDDY") == 0.0  # dry run never writes


def test_dry_run_success_counts_the_broker_review_in_usage_metrics(tmp_path, monkeypatch):
    """Codex review ("Count whole-share dry-run reviews in usage metrics"):
    a dry-run preview that reaches the share threshold performs a real
    broker review_whole_share_buy() call, unconditionally (regardless of
    ctx.scheduled) -- the normal candidate path increments
    order_reviews_attempted whenever it calls review_market_buy, so this
    call must be counted too, or cost-control telemetry underreports actual
    broker calls made during a scheduled demo.
    """
    monkeypatch.setenv("DCA_CRON_TRACKING_ROOT", str(tmp_path / "cron_tracking"))
    config = StrategyConfig(whole_share_tickers=frozenset({"BYDDY"}), dry_run=True)
    broker = DryRunBroker(cash=500.0, reference_prices={"BYDDY": 10.0})

    outcome, _, _, _, order_reviews_attempted = _process_buy_candidate(
        _decision(suggested_amount_usd=10.0, target_price=10.0),
        ctx=_ctx(config, broker, scheduled=True),
        running_deployed_7d=0.0,
    )

    assert outcome["status"] == "dry_run"
    assert order_reviews_attempted == 1


def test_a_share_above_max_per_order_fires_under_the_whole_share_allowance(tmp_path, monkeypatch):
    """The RVII case (PR #786 review: "Allow RVII's share price through the
    whole-share ceiling").

    A whole-share ticker has no purchasable unit smaller than one share, so
    max_per_order_usd's $10 -- sized for fractional dollar orders -- reads as
    "never buy this ticker at all" for anything priced above it. RVII (~$23.36)
    would have accumulated in the ledger forever and never fired. The separate
    whole_share_max_per_order_usd allowance authorizes exactly that one share.
    """
    monkeypatch.setenv("DCA_CRON_TRACKING_ROOT", str(tmp_path / "cron_tracking"))
    config = StrategyConfig(
        whole_share_tickers=frozenset({"BYDDY"}),
        dry_run=False,
        max_per_order_usd=10.0,
        whole_share_max_per_order_usd=50.0,
    )
    broker = DryRunBroker(cash=500.0, reference_prices={"BYDDY": 23.36})

    outcome, _, _, _, _ = _process_buy_candidate(
        _decision(suggested_amount_usd=23.36, target_price=23.36),
        ctx=_ctx(config, broker),
        running_deployed_7d=0.0,
    )

    assert outcome["status"] == "submitted"
    assert outcome["whole_share_quantity"] == 1
    assert outcome["submitted_amount_usd"] == pytest.approx(23.36)


def test_the_allowance_never_enlarges_a_multi_share_fire(tmp_path, monkeypatch):
    """Deliberately narrow: how many shares go into ONE order is still
    max_per_order_usd's decision. BYDDY at ~$10 keeps buying 1 share (~$10),
    not 5 shares ($50), even though the allowance is $50 -- the allowance only
    ever engages when the ordinary ceiling cannot fit a single share."""
    monkeypatch.setenv("DCA_CRON_TRACKING_ROOT", str(tmp_path / "cron_tracking"))
    config = StrategyConfig(
        whole_share_tickers=frozenset({"BYDDY"}),
        dry_run=False,
        max_per_order_usd=10.0,
        whole_share_max_per_order_usd=50.0,
    )
    broker = DryRunBroker(cash=500.0, reference_prices={"BYDDY": 10.0})
    ledger = WholeShareAccumulationLedger()
    # Enough accumulated balance to fund five shares if the ceiling allowed it.
    ledger.record_contribution(
        "BYDDY", "2026-08-15", 50.0, 10.0, stream="seed", idempotency_key="seed",
        recorded_at="2026-08-15T15:30:00-04:00",
    )

    outcome, _, _, _, _ = _process_buy_candidate(
        _decision(suggested_amount_usd=10.0, target_price=10.0),
        ctx=_ctx(config, broker),
        running_deployed_7d=0.0,
    )

    assert outcome["status"] == "submitted"
    assert outcome["whole_share_quantity"] == 1, "max_per_order_usd still caps the share count"
    assert outcome["submitted_amount_usd"] == pytest.approx(10.0)


def test_a_share_above_the_allowance_is_still_blocked(tmp_path, monkeypatch):
    """The allowance is a bound, not a removal of the bound."""
    monkeypatch.setenv("DCA_CRON_TRACKING_ROOT", str(tmp_path / "cron_tracking"))
    config = StrategyConfig(
        whole_share_tickers=frozenset({"BYDDY"}),
        dry_run=False,
        max_per_order_usd=10.0,
        whole_share_max_per_order_usd=50.0,
    )
    broker = DryRunBroker(cash=500.0, reference_prices={"BYDDY": 50.01})

    outcome, _, _, _, _ = _process_buy_candidate(
        _decision(suggested_amount_usd=50.01, target_price=50.01),
        ctx=_ctx(config, broker),
        running_deployed_7d=0.0,
    )

    assert outcome["status"] == "blocked"
    assert outcome["budget_blocked"] is True
    assert "whole-share single-share allowance $50.00" in outcome["skip_reason"]


def test_the_allowance_does_not_rescue_a_kill_switch_capped_fire(tmp_path, monkeypatch):
    """ceiling_override_usd is the deployment kill switch capping the
    EXECUTABLE fire. Rescuing a share through it would defeat exactly the block
    it exists to impose, so the allowance is skipped whenever an override is in
    play -- the fire stays blocked and deferred until the kill switch
    disengages."""
    monkeypatch.setenv("DCA_CRON_TRACKING_ROOT", str(tmp_path / "cron_tracking"))
    config = StrategyConfig(
        whole_share_tickers=frozenset({"BYDDY"}),
        dry_run=False,
        max_per_order_usd=10.0,
        whole_share_max_per_order_usd=50.0,
    )
    broker = DryRunBroker(cash=500.0, reference_prices={"BYDDY": 23.36})
    ctx = _ctx(config, broker)

    block, quantity, _notional, _spread, is_error = _near_close_dca._whole_share_risk_gates(
        "BYDDY", 1, 23.36, 23.36, ctx, ceiling_override_usd=0.0
    )

    assert block is not None
    assert block["status"] == "blocked"
    assert "allowance" not in block["skip_reason"]
    assert quantity == 1
    assert is_error is False


def test_dry_run_preview_blocks_when_one_share_exceeds_the_ceiling(tmp_path, monkeypatch):
    monkeypatch.setenv("DCA_CRON_TRACKING_ROOT", str(tmp_path / "cron_tracking"))
    config = StrategyConfig(whole_share_tickers=frozenset({"BYDDY"}), dry_run=True)
    broker = DryRunBroker(cash=500.0, reference_prices={"BYDDY": 60.0})

    outcome, _, _, errors, _ = _process_buy_candidate(
        _decision(suggested_amount_usd=60.0, target_price=60.0),
        ctx=_ctx(config, broker),
        running_deployed_7d=0.0,
    )

    assert outcome["status"] == "blocked"
    assert outcome["budget_blocked"] is True
    assert "exceeds max per-order" in outcome["skip_reason"]
    assert errors == 0
    ledger = WholeShareAccumulationLedger()
    assert ledger.pending_usd("BYDDY") == 0.0  # dry run never writes


def test_dry_run_preview_blocks_on_missing_spread_data_for_a_scheduled_run(tmp_path, monkeypatch):
    monkeypatch.setenv("DCA_CRON_TRACKING_ROOT", str(tmp_path / "cron_tracking"))
    config = StrategyConfig(whole_share_tickers=frozenset({"BYDDY"}), dry_run=True)
    broker = DryRunBroker(cash=500.0, reference_prices={"BYDDY": 10.0})

    outcome, _, _, _, _ = _process_buy_candidate(
        _decision(suggested_amount_usd=10.0, target_price=10.0),
        ctx=_ctx(config, broker, robinhood_data=None),
        running_deployed_7d=0.0,
    )

    assert outcome["status"] == "blocked"
    assert outcome["skip_reason"] == "whole-share: bid/ask spread unavailable for scheduled run"


def test_insufficient_buying_power_blocks_and_preserves_pending(tmp_path, monkeypatch):
    monkeypatch.setenv("DCA_CRON_TRACKING_ROOT", str(tmp_path / "cron_tracking"))
    config = StrategyConfig(whole_share_tickers=frozenset({"BYDDY"}), dry_run=False)
    broker = DryRunBroker(cash=5.0, reference_prices={"BYDDY": 10.0})  # cash < order_notional

    outcome, _, _, _, _ = _process_buy_candidate(
        _decision(suggested_amount_usd=10.0, target_price=10.0),
        ctx=_ctx(config, broker),
        running_deployed_7d=0.0,
    )

    assert outcome["status"] == "blocked"
    assert outcome["budget_blocked"] is True
    ledger = WholeShareAccumulationLedger()
    assert ledger.pending_usd("BYDDY") == 10.0
    assert ledger.shares_purchased_total("BYDDY") == 0


def test_max_per_order_ceiling_blocks_when_one_share_exceeds_it(tmp_path, monkeypatch):
    monkeypatch.setenv("DCA_CRON_TRACKING_ROOT", str(tmp_path / "cron_tracking"))
    # Default max_per_order_usd is $10; a $50 share price means not even one
    # share can ever be bought this run (Codex review: "Apply the normal
    # risk gates before whole-share dispatch").
    config = StrategyConfig(whole_share_tickers=frozenset({"BYDDY"}), dry_run=False)
    broker = DryRunBroker(cash=500.0, reference_prices={"BYDDY": 60.0})

    outcome, _, _, _, _ = _process_buy_candidate(
        _decision(suggested_amount_usd=60.0, target_price=60.0),
        ctx=_ctx(config, broker),
        running_deployed_7d=0.0,
    )

    assert outcome["status"] == "blocked"
    assert outcome["budget_blocked"] is True
    assert "exceeds max per-order" in outcome["skip_reason"]
    ledger = WholeShareAccumulationLedger()
    assert ledger.pending_usd("BYDDY") == 60.0
    assert ledger.shares_purchased_total("BYDDY") == 0


def test_max_per_order_ceiling_clamps_quantity_and_settles_the_shortfall(tmp_path, monkeypatch):
    monkeypatch.setenv("DCA_CRON_TRACKING_ROOT", str(tmp_path / "cron_tracking"))
    config = StrategyConfig(
        whole_share_tickers=frozenset({"BYDDY"}), dry_run=False, max_per_order_usd=10.0
    )
    broker = DryRunBroker(cash=500.0, reference_prices={"BYDDY": 4.0})

    # A $16 accumulated contribution at $4/share qualifies for 4 shares
    # ($16), but the $10 ceiling only allows 2 shares ($8) this run.
    outcome, _, _, _, _ = _process_buy_candidate(
        _decision(suggested_amount_usd=16.0, target_price=4.0),
        ctx=_ctx(config, broker),
        running_deployed_7d=0.0,
    )

    assert outcome["status"] == "submitted"
    assert outcome["whole_share_quantity"] == 2
    assert outcome["submitted_amount_usd"] == 8.0
    ledger = WholeShareAccumulationLedger()
    assert ledger.shares_purchased_total("BYDDY") == 2
    # The unbought $8 worth (2 shares' worth) is restored to pending by
    # settle()'s own shortfall reconciliation, not lost.
    assert ledger.pending_usd("BYDDY") == 8.0


def test_post_settlement_read_failure_fallback_preserves_the_restored_shortfall(tmp_path, monkeypatch):
    """Codex review ("Preserve restored shortfalls in the read-failure
    fallback"): when a multi-share fire is clamped and settle() SUCCEEDS
    (restoring the unsubmitted shares' credit), a failure in the SEPARATE
    pending_usd() read immediately after it must not silently discard that
    restoration. Falling back to the bare pre-settle
    wsa_decision.pending_after_usd would understate available accumulated
    credit precisely during a transient post-settlement read failure -- the
    fallback must add back the SAME shortfall settle() itself just
    restored: planned_order_notional - order_notional.
    """
    monkeypatch.setenv("DCA_CRON_TRACKING_ROOT", str(tmp_path / "cron_tracking"))
    config = StrategyConfig(
        whole_share_tickers=frozenset({"BYDDY"}), dry_run=False, max_per_order_usd=10.0
    )
    broker = DryRunBroker(cash=500.0, reference_prices={"BYDDY": 4.0})

    def _raising_pending_usd(self, ticker):
        raise RuntimeError("ledger read unavailable")

    monkeypatch.setattr(WholeShareAccumulationLedger, "pending_usd", _raising_pending_usd)

    # Same $16-contribution/$4-share/$10-ceiling clamp as the test above:
    # 2 shares ($8) submitted, an $8 shortfall settle() itself restores.
    outcome, _, _, _, _ = _process_buy_candidate(
        _decision(suggested_amount_usd=16.0, target_price=4.0),
        ctx=_ctx(config, broker),
        running_deployed_7d=0.0,
    )

    assert outcome["status"] == "submitted"
    assert outcome["whole_share_quantity"] == 2
    # wsa_decision.pending_after_usd alone is $0 (the pre-settle, pre-clamp
    # snapshot); the restored $8 shortfall must still be reflected here.
    assert outcome["whole_share_pending_usd"] == 8.0


def test_settle_failure_after_a_clamp_surfaces_the_shortfall_durably(tmp_path, monkeypatch):
    """Codex review ("Preserve clamped shortfalls when settlement fails"):
    when the per-order ceiling clamps a multi-share fire and the broker
    accepts the smaller order, settle() is the ONLY thing that restores the
    unbought shares' reserved credit back to pending_usd. If settle() itself
    fails (a lock timeout or unwritable ledger), that shortfall stays
    stranded -- same-day retries exit at the broker-duplicate check before
    ever touching the claim again, and the next trading day's dedup key
    never revisits it either. The outcome must stay "submitted" (a real
    order genuinely was placed), but the shortfall must be surfaced onto
    the outcome itself -- not just a log line -- so it survives into
    buy_candidates.csv/run_state.json for manual reconciliation.
    """
    monkeypatch.setenv("DCA_CRON_TRACKING_ROOT", str(tmp_path / "cron_tracking"))
    config = StrategyConfig(
        whole_share_tickers=frozenset({"BYDDY"}), dry_run=False, max_per_order_usd=10.0
    )
    broker = DryRunBroker(cash=500.0, reference_prices={"BYDDY": 4.0})

    def _raising_settle(self, *args, **kwargs):
        raise RuntimeError("ledger lock timeout")

    monkeypatch.setattr(WholeShareAccumulationLedger, "settle", _raising_settle)

    # A $16 accumulated contribution at $4/share qualifies for 4 shares
    # ($16), but the $10 ceiling only allows 2 shares ($8) this run -- an
    # $8 shortfall settle() would normally restore.
    outcome, _, _, _, _ = _process_buy_candidate(
        _decision(suggested_amount_usd=16.0, target_price=4.0),
        ctx=_ctx(config, broker),
        running_deployed_7d=0.0,
    )

    assert outcome["status"] == "submitted"
    assert outcome["whole_share_quantity"] == 2
    assert outcome["whole_share_settlement_reconciliation_needed"] is True
    assert outcome["whole_share_settlement_shortfall_usd"] == 8.0


def test_daily_cap_blocks_and_preserves_pending(tmp_path, monkeypatch):
    monkeypatch.setenv("DCA_CRON_TRACKING_ROOT", str(tmp_path / "cron_tracking"))
    config = StrategyConfig(
        whole_share_tickers=frozenset({"BYDDY"}), dry_run=False, max_orders_per_day=0
    )
    broker = DryRunBroker(cash=500.0, reference_prices={"BYDDY": 10.0})

    outcome, _, _, _, _ = _process_buy_candidate(
        _decision(suggested_amount_usd=10.0, target_price=10.0),
        ctx=_ctx(config, broker),
        running_deployed_7d=0.0,
    )

    assert outcome["status"] == "blocked"
    assert outcome["skip_reason"] == "whole-share: cap would be exceeded"
    ledger = WholeShareAccumulationLedger()
    assert ledger.pending_usd("BYDDY") == 10.0


def test_scheduled_run_blocks_on_missing_spread_data(tmp_path, monkeypatch):
    monkeypatch.setenv("DCA_CRON_TRACKING_ROOT", str(tmp_path / "cron_tracking"))
    config = StrategyConfig(whole_share_tickers=frozenset({"BYDDY"}), dry_run=False)
    broker = DryRunBroker(cash=500.0, reference_prices={"BYDDY": 10.0})

    outcome, _, _, _, _ = _process_buy_candidate(
        _decision(suggested_amount_usd=10.0, target_price=10.0),
        ctx=_ctx(config, broker, robinhood_data=None),  # no bid/ask provider
        running_deployed_7d=0.0,
    )

    assert outcome["status"] == "blocked"
    assert "spread unavailable" in outcome["skip_reason"]
    ledger = WholeShareAccumulationLedger()
    assert ledger.pending_usd("BYDDY") == 10.0


def test_wide_spread_blocks_a_scheduled_run(tmp_path, monkeypatch):
    monkeypatch.setenv("DCA_CRON_TRACKING_ROOT", str(tmp_path / "cron_tracking"))

    class _WideSpreadData:
        def get_bid_ask(self, ticker, as_of):
            return (5.0, 15.0)  # ~66% spread, well past the default limit

    config = StrategyConfig(whole_share_tickers=frozenset({"BYDDY"}), dry_run=False)
    broker = DryRunBroker(cash=500.0, reference_prices={"BYDDY": 10.0})

    outcome, _, _, _, _ = _process_buy_candidate(
        _decision(suggested_amount_usd=10.0, target_price=10.0),
        ctx=_ctx(config, broker, robinhood_data=_WideSpreadData()),
        running_deployed_7d=0.0,
    )

    assert outcome["status"] == "blocked"
    assert "spread" in outcome["skip_reason"]
    ledger = WholeShareAccumulationLedger()
    assert ledger.pending_usd("BYDDY") == 10.0


def test_same_day_broker_order_exists_skips_before_touching_the_ledger(tmp_path, monkeypatch):
    monkeypatch.setenv("DCA_CRON_TRACKING_ROOT", str(tmp_path / "cron_tracking"))

    class _AlreadyOrderedBroker(DryRunBroker):
        def same_day_strategy_order_exists(self, ticker, trading_day):
            return True

    config = StrategyConfig(whole_share_tickers=frozenset({"BYDDY"}))
    broker = _AlreadyOrderedBroker(cash=500.0, reference_prices={"BYDDY": 10.0})

    outcome, _, _, _, _ = _process_buy_candidate(
        _decision(suggested_amount_usd=10.0, target_price=10.0),
        ctx=_ctx(config, broker),
        running_deployed_7d=0.0,
    )

    assert outcome["status"] == "skipped"
    assert outcome["skip_reason"] == "same-day broker order exists"
    ledger = WholeShareAccumulationLedger()
    assert ledger.pending_usd("BYDDY") == 0.0  # never recorded


def test_review_blocked_preserves_pending_for_a_manual_run(tmp_path, monkeypatch):
    monkeypatch.setenv("DCA_CRON_TRACKING_ROOT", str(tmp_path / "cron_tracking"))

    class _RejectingBroker(DryRunBroker):
        def review_whole_share_buy(self, ticker, quantity):
            return False, "manual review required"

    config = StrategyConfig(whole_share_tickers=frozenset({"BYDDY"}), dry_run=False)
    broker = _RejectingBroker(cash=500.0, reference_prices={"BYDDY": 10.0})

    outcome, _, _, _, order_reviews_attempted = _process_buy_candidate(
        _decision(suggested_amount_usd=10.0, target_price=10.0),
        ctx=_ctx(config, broker, scheduled=False),  # non-scheduled -> review path runs
        running_deployed_7d=0.0,
    )

    assert outcome["status"] == "blocked"
    assert "review blocked" in outcome["skip_reason"]
    assert order_reviews_attempted == 1
    ledger = WholeShareAccumulationLedger()
    assert ledger.pending_usd("BYDDY") == 10.0


def test_review_passed_on_a_manual_run_places_and_records_the_review(tmp_path, monkeypatch):
    monkeypatch.setenv("DCA_CRON_TRACKING_ROOT", str(tmp_path / "cron_tracking"))
    config = StrategyConfig(whole_share_tickers=frozenset({"BYDDY"}), dry_run=False)
    broker = DryRunBroker(cash=500.0, reference_prices={"BYDDY": 10.0})

    outcome, _, _, _, order_reviews_attempted = _process_buy_candidate(
        _decision(suggested_amount_usd=10.0, target_price=10.0),
        ctx=_ctx(config, broker, scheduled=False),
        running_deployed_7d=0.0,
    )

    assert outcome["status"] == "submitted"
    assert order_reviews_attempted == 1
    assert outcome["reviewed_order"] == {
        "quantity": 1,
        "side": "buy",
        "order_type": "market",
        "time_in_force": None,
        "review_passed": True,
    }
    ledger = WholeShareAccumulationLedger()
    assert ledger.shares_purchased_total("BYDDY") == 1


def test_a_same_day_order_appearing_mid_flight_blocks_the_late_placement(tmp_path, monkeypatch):
    """Codex review ("Hold the claim through the broker placement"): a
    fresh same_day_strategy_order_exists() recheck immediately before
    placement narrows the reclaim race -- if a reclaiming attempt's own
    order has already landed at the broker by the time this caller reaches
    placement, this caller must abort rather than submit a second live
    order for the same fire, even though it still (nominally) held the
    claim moments earlier. Simulated by a broker whose
    same_day_strategy_order_exists() answers False on its first call (the
    function's own top-of-function check) and True on every call after --
    standing in for "a reclaiming attempt placed its order in between."
    """
    monkeypatch.setenv("DCA_CRON_TRACKING_ROOT", str(tmp_path / "cron_tracking"))

    class _LateOrderAppearsBroker(DryRunBroker):
        def __init__(self, *args, **kwargs):
            super().__init__(*args, **kwargs)
            self._same_day_calls = 0

        def same_day_strategy_order_exists(self, ticker, trading_day):
            self._same_day_calls += 1
            return self._same_day_calls > 1

        def same_day_strategy_order_exists_with_ref_id_prefix(self, ticker, trading_day, ref_id_prefixes):
            # Confirms the broad match above really is THIS claim's own
            # reclaimed order (an exact ref_id match), not an unrelated one
            # (Codex review: "Preserve the claim when a late duplicate is
            # unrelated") -- the verification lookup this second check now
            # performs before deciding to skip.
            return {"id": "RH-RECLAIMED-1", "state": "filled", "quantity": "1"}

    config = StrategyConfig(whole_share_tickers=frozenset({"BYDDY"}), dry_run=False)
    broker = _LateOrderAppearsBroker(cash=500.0, reference_prices={"BYDDY": 10.0})

    outcome, _, _, _, _ = _process_buy_candidate(
        _decision(suggested_amount_usd=10.0, target_price=10.0),
        ctx=_ctx(config, broker, scheduled=True),
        running_deployed_7d=0.0,
    )

    assert outcome["status"] == "skipped"
    assert "same-day broker order now exists" in outcome["skip_reason"]
    ledger = WholeShareAccumulationLedger()
    assert ledger.shares_purchased_total("BYDDY") == 0


def test_scheduled_run_blocks_on_a_non_tradable_whole_share_ticker(tmp_path, monkeypatch):
    """Codex review ("Verify whole-share tradability before scheduled
    placement"): review_whole_share_buy is only attempted on a MANUAL run
    (ctx.scheduled=False), so a SCHEDULED run had no pre-placement
    tradability check at all -- is_tradable_for_whole_share_buy must be
    checked unconditionally, or a halted/inactive BYDDY with quote data
    still available could reach place_whole_share_buy on the scheduled path.
    """
    monkeypatch.setenv("DCA_CRON_TRACKING_ROOT", str(tmp_path / "cron_tracking"))

    class _NonTradableBroker(DryRunBroker):
        def is_tradable_for_whole_share_buy(self, ticker):
            return False

    config = StrategyConfig(whole_share_tickers=frozenset({"BYDDY"}), dry_run=False)
    broker = _NonTradableBroker(cash=500.0, reference_prices={"BYDDY": 10.0})

    outcome, _, _, _, _ = _process_buy_candidate(
        _decision(suggested_amount_usd=10.0, target_price=10.0),
        ctx=_ctx(config, broker, scheduled=True),
        running_deployed_7d=0.0,
    )

    assert outcome["status"] == "blocked"
    assert "not tradable" in outcome["skip_reason"]
    ledger = WholeShareAccumulationLedger()
    assert ledger.pending_usd("BYDDY") == 10.0  # preserved, not lost
    assert ledger.shares_purchased_total("BYDDY") == 0


def test_placement_failure_preserves_pending_and_counts_an_error(tmp_path, monkeypatch):
    monkeypatch.setenv("DCA_CRON_TRACKING_ROOT", str(tmp_path / "cron_tracking"))

    class _FailingBroker(DryRunBroker):
        def place_whole_share_buy(self, ticker, quantity, ref_id):
            raise RuntimeError("broker unavailable")

    config = StrategyConfig(whole_share_tickers=frozenset({"BYDDY"}), dry_run=False)
    broker = _FailingBroker(cash=500.0, reference_prices={"BYDDY": 10.0})

    outcome, _, _, errors, _ = _process_buy_candidate(
        _decision(suggested_amount_usd=10.0, target_price=10.0),
        ctx=_ctx(config, broker),
        running_deployed_7d=0.0,
    )

    assert outcome["status"] == "failed"
    assert errors == 1
    ledger = WholeShareAccumulationLedger()
    assert ledger.pending_usd("BYDDY") == 10.0


def test_ambiguous_placement_failure_reconciles_via_ref_id_instead_of_restoring_credit(tmp_path, monkeypatch):
    """Codex review ("Reconcile ambiguous placement failures before
    restoring credit"): place_whole_share_buy() can raise AFTER the broker
    has already accepted the order (e.g. a response timeout relaying the
    accepted order back) -- indistinguishable, from the exception alone,
    from a genuine rejection. Blindly restoring the deducted pending credit
    in that case would let a later trading day spend the same dollars
    again while the broker-side order already consumed them. The fix looks
    up this deterministic ref_id against the broker's own same-day order
    history before deciding whether to preserve or settle; when the broker
    reports the order as already accepted, the claim must be settled (not
    restored) and the outcome must report the broker's real order id.
    """
    monkeypatch.setenv("DCA_CRON_TRACKING_ROOT", str(tmp_path / "cron_tracking"))
    config = StrategyConfig(whole_share_tickers=frozenset({"BYDDY"}), dry_run=False)

    class _AmbiguousFailureBroker(DryRunBroker):
        def place_whole_share_buy(self, ticker, quantity, ref_id):
            raise RuntimeError("response timeout")

        def same_day_strategy_order_exists_with_ref_id_prefix(self, ticker, trading_day, ref_id_prefixes):
            assert ref_id_prefixes == ("graded-dca-wholeshare-2026-08-16-BYDDY",)
            return {"id": "RH-ALREADY-ACCEPTED-123", "state": "filled", "quantity": "1"}

    broker = _AmbiguousFailureBroker(cash=500.0, reference_prices={"BYDDY": 10.0})

    outcome, _, _, errors, _ = _process_buy_candidate(
        _decision(suggested_amount_usd=10.0, target_price=10.0),
        ctx=_ctx(config, broker),
        running_deployed_7d=0.0,
    )

    assert outcome["status"] == "submitted"
    assert outcome["order_id"] == "RH-ALREADY-ACCEPTED-123"
    assert errors == 0
    ledger = WholeShareAccumulationLedger()
    # Settled, not restored: the $10 contribution bought exactly one
    # $10 share, so the pending balance stays at $0 -- restoring it to
    # $10 (as the conservative preserve_pending branch would) would let a
    # later day spend it again on top of the order the broker already
    # placed.
    assert ledger.pending_usd("BYDDY") == 0.0
    assert ledger.shares_purchased_total("BYDDY") == 1


def test_ambiguous_placement_failure_with_a_failed_reconciliation_lookup_leaves_the_claim_reserved(
    tmp_path, monkeypatch
):
    """Codex review ("Keep ambiguous claims reserved when reconciliation
    fails"): a response timeout combined with a transient order-history
    outage must NOT fall through to the conservative preserve_pending
    branch -- that would restore credit even though the order may actually
    be live at the broker, letting a later trading day spend it again. When
    the reconciliation lookup itself raises, the claim must be left exactly
    as record_contribution left it (neither settled nor restored) and the
    candidate must fail closed with a data error.
    """
    monkeypatch.setenv("DCA_CRON_TRACKING_ROOT", str(tmp_path / "cron_tracking"))
    config = StrategyConfig(whole_share_tickers=frozenset({"BYDDY"}), dry_run=False)

    class _DoublyAmbiguousBroker(DryRunBroker):
        def place_whole_share_buy(self, ticker, quantity, ref_id):
            raise RuntimeError("response timeout")

        def same_day_strategy_order_exists_with_ref_id_prefix(self, ticker, trading_day, ref_id_prefixes):
            raise RuntimeError("order history lookup unavailable")

    broker = _DoublyAmbiguousBroker(cash=500.0, reference_prices={"BYDDY": 10.0})

    outcome, _, _, errors, _ = _process_buy_candidate(
        _decision(suggested_amount_usd=10.0, target_price=10.0),
        ctx=_ctx(config, broker),
        running_deployed_7d=0.0,
    )

    assert outcome["status"] == "blocked"
    assert "reconciliation lookup also failed" in outcome["skip_reason"]
    assert outcome["skip_reason"] == (
        "whole-share placement failed: RuntimeError; reconciliation lookup also failed: "
        "RuntimeError; claim left reserved pending manual reconciliation"
    )
    assert "response timeout" not in outcome["skip_reason"]
    assert "order history lookup unavailable" not in outcome["skip_reason"]
    assert errors == 1
    ledger = WholeShareAccumulationLedger()
    # Neither restored (would free credit for an order that may be live) nor
    # settled (no order id was ever confirmed) -- left exactly as
    # record_contribution's own deduction left it.
    assert ledger.pending_usd("BYDDY") == 0.0
    assert ledger.shares_purchased_total("BYDDY") == 0


def test_a_same_day_retry_reconciles_an_unresolved_claim_at_the_duplicate_check(tmp_path, monkeypatch):
    """Codex review ("Reconcile reserved claims before the duplicate early
    return"): an earlier attempt today left this exact idempotency slot
    deliberately unresolved (placement raised, and its ref-id reconciliation
    lookup ALSO raised). A same-day retry that reaches the BROAD, ref-id-
    agnostic same_day_strategy_order_exists check finds strong evidence the
    order actually landed and must reconcile (settle) that unresolved claim
    before returning its deliberate "skipped" outcome -- otherwise the
    claim's reservation (and any clamped shortfall) is stranded forever,
    since every future retry hits the same early return.
    """
    monkeypatch.setenv("DCA_CRON_TRACKING_ROOT", str(tmp_path / "cron_tracking"))
    config = StrategyConfig(whole_share_tickers=frozenset({"BYDDY"}), dry_run=False)

    class _DoublyAmbiguousBroker(DryRunBroker):
        def place_whole_share_buy(self, ticker, quantity, ref_id):
            raise RuntimeError("response timeout")

        def same_day_strategy_order_exists_with_ref_id_prefix(self, ticker, trading_day, ref_id_prefixes):
            raise RuntimeError("order history lookup unavailable")

    first_broker = _DoublyAmbiguousBroker(cash=500.0, reference_prices={"BYDDY": 10.0})
    _process_buy_candidate(
        _decision(suggested_amount_usd=10.0, target_price=10.0),
        ctx=_ctx(config, first_broker),
        running_deployed_7d=0.0,
    )
    ledger = WholeShareAccumulationLedger()
    assert ledger.pending_usd("BYDDY") == 0.0
    assert ledger.shares_purchased_total("BYDDY") == 0  # unresolved, not yet settled

    class _RetryBroker(DryRunBroker):
        def same_day_strategy_order_exists(self, ticker, trading_day):
            return True  # the order history outage cleared; the order did land

        def same_day_strategy_order_exists_with_ref_id_prefix(self, ticker, trading_day, ref_id_prefixes):
            return {"id": "RH-CONFIRMED-789", "quantity": "1", "state": "filled"}

    retry_broker = _RetryBroker(cash=500.0, reference_prices={"BYDDY": 10.0})
    outcome, _, _, errors, _ = _process_buy_candidate(
        _decision(suggested_amount_usd=10.0, target_price=10.0),
        ctx=_ctx(config, retry_broker),
        running_deployed_7d=0.0,
    )

    assert outcome["status"] == "skipped"
    assert errors == 0
    # The unresolved $10/1-share claim is now settled: 1 share purchased,
    # nothing left to restore (1 planned == 1 actually placed).
    assert ledger.shares_purchased_total("BYDDY") == 1
    assert ledger.pending_usd("BYDDY") == 0.0


def test_reconciliation_settlement_forwards_the_claim_ownership_token():
    """Every recovered fill must be settled by the owner that read it.

    Keep this assertion at the shared reconciliation helper boundary so both
    of its callers (filled orders and terminal partial fills) remain fenced
    if their claim is reclaimed during the broker lookup.
    """
    claim_token = "claim-owner-token"
    claim = {
        "claimed_at": "2026-08-16T15:45:00-04:00",
        "claim_token": claim_token,
        "current_share_price_usd": 10.0,
    }
    settle_kwargs = {}

    class _RecordingLedger:
        def settle(self, *args, **kwargs):
            settle_kwargs.update(kwargs)
            return True

    result = _near_close_dca._settle_whole_share_claim(
        _RecordingLedger(),
        "BYDDY",
        claim,
        "2026-08-16",
        1,
        "afternoon",
        "afternoon-BYDDY",
        order_id="RH-CLAIM-TOKEN",
    )

    assert result is not None
    assert settle_kwargs["claim_token"] == claim_token
    assert settle_kwargs["require_claim_ownership"] is True


def test_the_second_predisplacement_duplicate_check_leaves_a_reclaimed_claim_reserved_when_verification_fails(
    tmp_path, monkeypatch
):
    """Codex review ("Keep reclaimed claims reserved when exact lookup
    fails"): the SECOND, pre-placement same_day_strategy_order_exists
    recheck (immediately before place_whole_share_buy) can find that a
    concurrent reclaiming attempt's order landed at the broker in the
    window since the earlier checks ran. UNLIKE a failed BROAD lookup
    (which proves nothing lives at the broker at all, so restoring the
    deduction is always safe), a failed EXACT-ref verification lookup
    here cannot distinguish "our own reclaimed order" from "unrelated" --
    and the broad check that gated entry into this branch already proved
    SOME live order exists. Restoring the deduction here would risk
    double-crediting a claim a concurrent reclaimer may have already
    spent, if that order later fills.
    """
    monkeypatch.setenv("DCA_CRON_TRACKING_ROOT", str(tmp_path / "cron_tracking"))
    ledger = WholeShareAccumulationLedger()
    config = StrategyConfig(whole_share_tickers=frozenset({"BYDDY"}), dry_run=False)
    broad_check_calls = {"count": 0}

    class _ReclaimedDuringPlacementBroker(DryRunBroker):
        def same_day_strategy_order_exists(self, ticker, trading_day):
            broad_check_calls["count"] += 1
            # False on the initial (top-of-function) check -- False on
            # the SECOND, pre-placement recheck too until this counter
            # reaches its assertion point; here the reclaim happens
            # between the two, so the second call is True.
            return broad_check_calls["count"] >= 2

        def same_day_strategy_order_exists_with_ref_id_prefix(self, ticker, trading_day, ref_id_prefixes):
            raise RuntimeError("order history temporarily unavailable")

    broker = _ReclaimedDuringPlacementBroker(cash=500.0, reference_prices={"BYDDY": 10.0})

    outcome, _, running_deployed_7d, errors, _ = _process_buy_candidate(
        _decision(suggested_amount_usd=10.0, target_price=10.0),
        ctx=_ctx(config, broker),
        running_deployed_7d=0.0,
    )

    assert broad_check_calls["count"] == 2  # the initial check, then the pre-placement recheck
    assert outcome["status"] == "blocked"
    assert "duplicate ref_id verification failed" in outcome["skip_reason"]
    assert errors == 1
    # NOT restored -- the deduction is left exactly as record_contribution
    # left it, since the broad check already proved a live order exists
    # and this candidate cannot rule out that it's its own reclaimed fire.
    assert ledger.pending_usd("BYDDY") == 0.0
    assert ledger.shares_purchased_total("BYDDY") == 0
    assert ledger.unresolved_claim("BYDDY", "2026-08-16", "afternoon-BYDDY") is not None


def test_a_later_trading_day_reconciles_an_unresolved_claim_from_an_earlier_day(tmp_path, monkeypatch):
    """Codex review ("Reconcile unresolved claims across trading days"): an
    ambiguous placement failure whose ref-id reconciliation lookup ALSO
    failed can remain unresolved past midnight. A LATER trading day's run
    (this test's `_ctx()` fixture is fixed at 2026-08-16) must still be
    able to discover and reconcile a claim left unresolved on an EARLIER
    day (2026-08-15) against its ORIGINAL trading day/ref_id -- not just
    today's.
    """
    monkeypatch.setenv("DCA_CRON_TRACKING_ROOT", str(tmp_path / "cron_tracking"))
    ledger = WholeShareAccumulationLedger()
    # Simulates YESTERDAY's ambiguous, doubly-failed placement: claimed but
    # never settled or restored.
    ledger.record_contribution(
        "BYDDY",
        "2026-08-15",
        10.0,
        10.0,
        stream="afternoon",
        idempotency_key="afternoon-BYDDY",
        recorded_at="2026-08-15T15:30:00-04:00",
    )
    assert len(ledger.unresolved_claims_any_day("BYDDY", "afternoon-BYDDY")) == 1

    config = StrategyConfig(whole_share_tickers=frozenset({"BYDDY"}), dry_run=False)

    class _CrossDayConfirmingBroker(DryRunBroker):
        def same_day_strategy_order_exists(self, ticker, trading_day):
            return False  # nothing new today

        def same_day_strategy_order_exists_with_ref_id_prefix(self, ticker, trading_day, ref_id_prefixes):
            if ref_id_prefixes == ("graded-dca-wholeshare-2026-08-15-BYDDY",):
                return {"id": "RH-YESTERDAY-1", "state": "filled", "quantity": "1"}
            return None

    broker = _CrossDayConfirmingBroker(cash=500.0, reference_prices={"BYDDY": 10.0})
    ctx = _ctx(config, broker)

    # TODAY's own candidate is a fresh, below-threshold accumulation --
    # unrelated to yesterday's stranded claim.
    outcome, _, running_deployed_7d, errors, _ = _process_buy_candidate(
        _decision(suggested_amount_usd=3.0, target_price=10.0),
        ctx=ctx,
        running_deployed_7d=0.0,
    )

    assert errors == 0
    # Yesterday's claim is now settled: 1 share purchased for the $10 it
    # deducted (no shortfall -- 1 planned share == 1 actually placed).
    assert ledger.shares_purchased_total("BYDDY") == 1
    assert ledger.unresolved_claims_any_day("BYDDY", "afternoon-BYDDY") == []
    # Codex review ("Book reconciled orders into the strategy budget
    # state"): the reconciled $10 purchase must be booked into
    # ctx.state/running_deployed_7d, not just the whole-share ledger.
    assert ("BYDDY", date(2026, 8, 15)) in ctx.state.own_orders_recorded
    assert running_deployed_7d == 10.0


def test_a_reconciled_order_outside_the_7day_window_is_not_booked_as_current_deployment(tmp_path, monkeypatch):
    """Codex review ("Exclude old reconciliations from the trailing
    deployment total"): cross-day recovery can discover a claim from ANY
    earlier trading day, including one outside the trailing
    7-calendar-day window `running_deployed_7d` is otherwise seeded from
    (`dca_risk.seven_day_deployed`). Booking an old reconciled notional
    into `running_deployed_7d` unconditionally would treat a purchase
    from weeks ago as CURRENT deployment and could spuriously engage the
    kill switch for later candidates in the same batch.
    """
    monkeypatch.setenv("DCA_CRON_TRACKING_ROOT", str(tmp_path / "cron_tracking"))
    ledger = WholeShareAccumulationLedger()
    old_trading_day = "2026-07-20"  # well over 7 calendar days before 2026-08-16
    ledger.record_contribution(
        "BYDDY",
        old_trading_day,
        10.0,
        10.0,
        stream="afternoon",
        idempotency_key="afternoon-BYDDY",
        recorded_at="2026-07-20T15:30:00-04:00",
    )

    config = StrategyConfig(whole_share_tickers=frozenset({"BYDDY"}), dry_run=False)

    class _OldReconcilingBroker(DryRunBroker):
        def same_day_strategy_order_exists(self, ticker, trading_day):
            return False

        def same_day_strategy_order_exists_with_ref_id_prefix(self, ticker, trading_day, ref_id_prefixes):
            if ref_id_prefixes == (f"graded-dca-wholeshare-{old_trading_day}-BYDDY",):
                return {"id": "RH-OLD-1", "state": "filled", "quantity": "1"}
            return None

    broker = _OldReconcilingBroker(cash=500.0, reference_prices={"BYDDY": 10.0})
    outcome, _, running_deployed_7d, errors, _ = _process_buy_candidate(
        _decision(suggested_amount_usd=3.0, target_price=10.0),
        ctx=_ctx(config, broker),
        running_deployed_7d=0.0,
    )

    assert errors == 0
    # The old claim is still settled (real money, still tracked in the
    # ledger's own audit trail)...
    assert ledger.shares_purchased_total("BYDDY") == 1
    # ...but its notional is NOT added to running_deployed_7d, since it
    # falls outside the trailing 7-calendar-day window that figure feeds
    # the kill switch from.
    assert running_deployed_7d == 0.0


def test_a_failing_claims_lookup_does_not_starve_a_later_claims_reconciliation(tmp_path, monkeypatch):
    """Codex review ("Isolate failures for each unresolved claim"): a
    ticker can accumulate more than one unresolved cross-day claim. If the
    OLDEST claim's own broker history lookup consistently raises, that
    must not stop a LATER claim (still in the returned list) from being
    examined and reconciled -- one bad lookup must not starve the rest.
    """
    monkeypatch.setenv("DCA_CRON_TRACKING_ROOT", str(tmp_path / "cron_tracking"))
    ledger = WholeShareAccumulationLedger()
    ledger.record_contribution(
        "BYDDY", "2026-08-10", 10.0, 10.0, stream="afternoon", idempotency_key="afternoon-BYDDY",
        recorded_at="2026-08-10T15:30:00-04:00",
    )
    ledger.record_contribution(
        "BYDDY", "2026-08-15", 10.0, 10.0, stream="afternoon", idempotency_key="afternoon-BYDDY",
        recorded_at="2026-08-15T15:30:00-04:00",
    )
    assert len(ledger.unresolved_claims_any_day("BYDDY", "afternoon-BYDDY")) == 2

    config = StrategyConfig(whole_share_tickers=frozenset({"BYDDY"}), dry_run=False)

    class _PartiallyFailingBroker(DryRunBroker):
        def same_day_strategy_order_exists(self, ticker, trading_day):
            return False

        def same_day_strategy_order_exists_with_ref_id_prefix(self, ticker, trading_day, ref_id_prefixes):
            if ref_id_prefixes == ("graded-dca-wholeshare-2026-08-10-BYDDY",):
                raise RuntimeError("that date's order history is no longer available")
            if ref_id_prefixes == ("graded-dca-wholeshare-2026-08-15-BYDDY",):
                return {"id": "RH-LATER-1", "state": "filled", "quantity": "1"}
            return None

    broker = _PartiallyFailingBroker(cash=500.0, reference_prices={"BYDDY": 10.0})
    outcome, _, _, errors, _ = _process_buy_candidate(
        _decision(suggested_amount_usd=3.0, target_price=10.0),
        ctx=_ctx(config, broker),
        running_deployed_7d=0.0,
    )

    assert errors == 0
    # The oldest (2026-08-10) claim's failed lookup left it unresolved, but
    # the LATER (2026-08-15) claim was still reconciled despite the earlier
    # failure.
    assert ledger.shares_purchased_total("BYDDY") == 1
    remaining = ledger.unresolved_claims_any_day("BYDDY", "afternoon-BYDDY")
    assert [trading_day for trading_day, _ in remaining] == ["2026-08-10"]


def test_a_stale_confirmed_rejected_claim_is_released(tmp_path, monkeypatch):
    """Codex review ("Restore old claims after a confirmed no-match"): an
    ambiguous placement that was actually REJECTED, whose OWN immediate
    ref-id lookup ALSO failed at the time, leaves the claim unresolved.
    Once the claim is STALE and a LATER lookup cleanly confirms no live
    order exists under this ref_id, the deducted credit must be released
    back to pending_usd rather than stranded forever.
    """
    monkeypatch.setenv("DCA_CRON_TRACKING_ROOT", str(tmp_path / "cron_tracking"))
    ledger = WholeShareAccumulationLedger()
    # Claimed well over UNRESOLVED_CLAIM_STALE_SECONDS (5 minutes) before
    # this test's fixed `now` (2026-08-16T15:45 ET, from _ctx()).
    ledger.record_contribution(
        "BYDDY", "2026-08-16", 10.0, 10.0, stream="afternoon", idempotency_key="afternoon-BYDDY",
        recorded_at="2026-08-16T15:00:00-04:00",
    )
    assert ledger.pending_usd("BYDDY") == 0.0  # deducted

    config = StrategyConfig(whole_share_tickers=frozenset({"BYDDY"}), dry_run=False)

    class _ConfirmedRejectedBroker(DryRunBroker):
        def same_day_strategy_order_exists(self, ticker, trading_day):
            return True  # broad check still true; nothing distinguishes it

        def same_day_strategy_order_exists_with_ref_id_prefix(self, ticker, trading_day, ref_id_prefixes):
            return None  # cleanly confirmed: no live order under this ref_id

    broker = _ConfirmedRejectedBroker(cash=500.0, reference_prices={"BYDDY": 10.0})
    outcome, _, _, errors, _ = _process_buy_candidate(
        _decision(suggested_amount_usd=3.0, target_price=10.0),
        ctx=_ctx(config, broker),
        running_deployed_7d=0.0,
    )

    assert errors == 0
    ledger2 = WholeShareAccumulationLedger()
    # The stale, confirmed-rejected claim's $10 was released back to
    # pending_usd rather than staying permanently deducted.
    assert ledger2.pending_usd("BYDDY") == 10.0
    assert ledger2.unresolved_claim("BYDDY", "2026-08-16", "afternoon-BYDDY") is None


def test_a_nonterminal_ambiguous_match_leaves_the_claim_reserved(tmp_path, monkeypatch):
    """Codex review ("Keep nonterminal ambiguous orders reserved", "Book
    matched live orders before continuing"): the immediate placement-error
    branch's own ref-id reconciliation lookup can find a match that is
    still LIVE and working (new/queued/confirmed/partially_filled) -- not
    yet terminal. Settling now (as an earlier version of this fix did for
    ANY truthy match) would mark the claim resolved before the order's
    final filled quantity is actually known: if the working remainder is
    later cancelled or rejected, the ledger permanently overstates
    purchased shares and withholds the unfilled credit forever. The
    whole-share LEDGER claim must be left reserved -- neither settled nor
    restored -- until a later reconciliation attempt finds it terminal.
    But the accepted order itself must still be BOOKED as submitted/
    unfilled (using the locally-clamped quantity/notional actually
    attempted) so later candidates in the same batch aren't blind to it --
    reporting "blocked" (an earlier version of this fix did) left the
    accepted order completely uncounted toward daily/weekly caps and the
    kill switch.
    """
    monkeypatch.setenv("DCA_CRON_TRACKING_ROOT", str(tmp_path / "cron_tracking"))
    config = StrategyConfig(whole_share_tickers=frozenset({"BYDDY"}), dry_run=False)

    class _NonterminalMatchBroker(DryRunBroker):
        def place_whole_share_buy(self, ticker, quantity, ref_id):
            raise RuntimeError("response timeout")

        def same_day_strategy_order_exists_with_ref_id_prefix(self, ticker, trading_day, ref_id_prefixes):
            return {"id": "RH-STILL-WORKING-1", "state": "confirmed"}

    broker = _NonterminalMatchBroker(cash=500.0, reference_prices={"BYDDY": 10.0})

    outcome, _, running_deployed_7d, errors, _ = _process_buy_candidate(
        _decision(suggested_amount_usd=10.0, target_price=10.0),
        ctx=_ctx(config, broker),
        running_deployed_7d=0.0,
    )

    assert outcome["status"] == "submitted"
    assert outcome["order_id"] == "RH-STILL-WORKING-1"
    assert outcome["submitted_amount_usd"] == pytest.approx(10.0)
    assert running_deployed_7d == pytest.approx(10.0)
    assert errors == 1
    ledger = WholeShareAccumulationLedger()
    # Neither settled (shares_purchased_total stays 0) nor restored
    # (pending_usd stays at 0, the deducted amount) -- the claim is left
    # exactly as record_contribution left it.
    assert ledger.shares_purchased_total("BYDDY") == 0
    assert ledger.pending_usd("BYDDY") == 0.0
    assert ledger.unresolved_claim("BYDDY", "2026-08-16", "afternoon-BYDDY") is not None


def test_immediate_placement_failure_settles_a_partial_fill_found_via_terminal_lookup(tmp_path, monkeypatch):
    """Codex review ("Check terminal orders before restoring failed
    placements"): the immediate placement-failure branch's own ref-id
    reconciliation previously used only the LIVE-only lookup, which
    excludes a genuinely terminal (cancelled/rejected) order by design. If
    the order partially filled and was THEN cancelled before this lookup
    ran, the live-only lookup reports a clean "no match" and the branch
    would fall straight through to preserve_pending, restoring the FULL
    planned notional and silently discarding the shares that actually
    executed. The terminal-inclusive lookup must be checked too: a
    positive filled quantity is settled for real, using the broker's own
    average_price for the booked notional.
    """
    monkeypatch.setenv("DCA_CRON_TRACKING_ROOT", str(tmp_path / "cron_tracking"))
    settle_claim_tokens = []
    original_settle = WholeShareAccumulationLedger.settle

    def _record_settle_claim_token(self, *args, **kwargs):
        settle_claim_tokens.append(kwargs.get("claim_token"))
        return original_settle(self, *args, **kwargs)

    monkeypatch.setattr(WholeShareAccumulationLedger, "settle", _record_settle_claim_token)
    # $16 at $4/share previews 4 shares, clamped to 2 by the $10 ceiling.
    config = StrategyConfig(
        whole_share_tickers=frozenset({"BYDDY"}), dry_run=False, max_per_order_usd=10.0
    )

    class _PartialFillThenCanceledBroker(DryRunBroker):
        def place_whole_share_buy(self, ticker, quantity, ref_id):
            raise RuntimeError("response timeout")

        def same_day_strategy_order_exists_with_ref_id_prefix(self, ticker, trading_day, ref_id_prefixes):
            return None  # terminal orders are excluded from the live-only lookup

        def same_day_order_with_ref_id_prefix_any_state(self, ticker, trading_day, ref_id_prefixes):
            # Only 1 of the clamped 2 shares filled before cancellation, at
            # a real fill price ABOVE the $4.00 cached signal-time price.
            return {
                "id": "RH-PARTIAL-THEN-CANCELED-IMMEDIATE-1",
                "state": "cancelled",
                "cumulative_quantity": "1",
                "average_price": "4.25",
            }

    broker = _PartialFillThenCanceledBroker(cash=500.0, reference_prices={"BYDDY": 4.0})

    outcome, _, running_deployed_7d, errors, _ = _process_buy_candidate(
        _decision(suggested_amount_usd=16.0, target_price=4.0),
        ctx=_ctx(config, broker),
        running_deployed_7d=0.0,
    )

    assert outcome["status"] == "submitted"
    assert outcome["whole_share_quantity"] == 1
    # Booked using the REAL $4.25 fill price, not the stale $4.00 cached one.
    assert outcome["submitted_amount_usd"] == 4.25
    assert errors == 0
    ledger = WholeShareAccumulationLedger()
    assert ledger.shares_purchased_total("BYDDY") == 1
    assert running_deployed_7d == 4.25
    # Ownership enforcement is only meaningful when this terminal-inclusive
    # recovery passes the actionable decision's rotated claim token.
    assert len(settle_claim_tokens) == 1
    assert settle_claim_tokens[0]


def test_terminal_placement_error_settles_from_its_own_payload_when_both_lookups_miss(tmp_path, monkeypatch):
    """Codex review ("Reconcile terminal fills from the placement
    response"): when place_whole_share_buy raises
    WholeSharePlacementTerminalError, its own order payload is
    authoritative terminal evidence -- even if BOTH the live-only and
    terminal-inclusive lookups then report a clean "no match" (the
    broker's order-history endpoint is eventually consistent), the caller
    must still settle from the exception's own cumulative_quantity/
    average_price rather than treating the lookup misses as proof nothing
    executed and restoring the full planned notional.
    """
    monkeypatch.setenv("DCA_CRON_TRACKING_ROOT", str(tmp_path / "cron_tracking"))
    config = StrategyConfig(
        whole_share_tickers=frozenset({"BYDDY"}), dry_run=False, max_per_order_usd=10.0
    )

    class _TerminalErrorBothLookupsMissBroker(DryRunBroker):
        def place_whole_share_buy(self, ticker, quantity, ref_id):
            raise WholeSharePlacementTerminalError(
                "live whole-share placement came back in terminal state cancelled",
                order={
                    "id": "RH-TERMINAL-PAYLOAD-1",
                    "state": "cancelled",
                    "cumulative_quantity": "1",
                    "average_price": "4.25",
                },
            )

        def same_day_strategy_order_exists_with_ref_id_prefix(self, ticker, trading_day, ref_id_prefixes):
            return None  # eventually-consistent order-history miss

        def same_day_order_with_ref_id_prefix_any_state(self, ticker, trading_day, ref_id_prefixes):
            # If this were called, it would ALSO miss -- but it must not be
            # called at all, since the exception's own payload already
            # answers the question this lookup exists to answer.
            raise AssertionError("terminal-inclusive lookup must not be called for a terminal placement error")

    broker = _TerminalErrorBothLookupsMissBroker(cash=500.0, reference_prices={"BYDDY": 4.0})

    outcome, _, running_deployed_7d, errors, _ = _process_buy_candidate(
        _decision(suggested_amount_usd=16.0, target_price=4.0),
        ctx=_ctx(config, broker),
        running_deployed_7d=0.0,
    )

    assert outcome["status"] == "submitted"
    assert outcome["whole_share_quantity"] == 1
    assert outcome["submitted_amount_usd"] == 4.25
    assert errors == 0
    ledger = WholeShareAccumulationLedger()
    assert ledger.shares_purchased_total("BYDDY") == 1
    assert running_deployed_7d == 4.25


def test_terminal_placement_error_logs_execution_from_its_own_payload_without_requerying(tmp_path, monkeypatch):
    """Codex review ("Log terminal-response fills without re-querying
    history"): once a WholeSharePlacementTerminalError's own payload settles
    the claim (see the sibling test above), _log_execution used to STILL
    re-query broker.get_order_fill() for the durable execution record --
    the same separate, eventually-consistent endpoint that motivated
    skipping the terminal-inclusive lookup for this exact exception type in
    the first place. A clean miss there would record the proven partial
    fill as "submitted"/unfilled instead of "filled", corrupting
    filled_orders.csv/slippage even though this call already had the real
    fill price and quantity in hand.
    """
    monkeypatch.setenv("DCA_CRON_TRACKING_ROOT", str(tmp_path / "cron_tracking"))
    config = StrategyConfig(
        whole_share_tickers=frozenset({"BYDDY"}), dry_run=False, max_per_order_usd=10.0
    )

    class _TerminalErrorFillQueryMissesBroker(DryRunBroker):
        def place_whole_share_buy(self, ticker, quantity, ref_id):
            raise WholeSharePlacementTerminalError(
                "live whole-share placement came back in terminal state cancelled",
                order={
                    "id": "RH-TERMINAL-PAYLOAD-2",
                    "state": "cancelled",
                    "cumulative_quantity": "1",
                    "average_price": "4.25",
                },
            )

        def same_day_strategy_order_exists_with_ref_id_prefix(self, ticker, trading_day, ref_id_prefixes):
            return None

        def get_order_fill(self, order_id):
            # The separate history endpoint has not caught up -- a clean
            # miss, exactly like the eventually-consistent lookups above.
            return None

    broker = _TerminalErrorFillQueryMissesBroker(cash=500.0, reference_prices={"BYDDY": 4.0})
    run_result = RunResult(trading_day=date(2026, 8, 16), decisions=[])

    outcome, _, running_deployed_7d, errors, _ = _process_buy_candidate(
        _decision(suggested_amount_usd=16.0, target_price=4.0),
        ctx=_ctx(config, broker, run_result=run_result),
        running_deployed_7d=0.0,
    )

    assert outcome["status"] == "submitted"
    assert errors == 0
    assert len(run_result.execution_records) == 1
    record = run_result.execution_records[0]
    # Logged as filled, from the exception's own payload -- not "submitted"/
    # unfilled, despite get_order_fill() returning None.
    assert record.status == "filled"
    assert record.avg_fill_price == 4.25
    assert record.quantity_filled == 1.0


def test_immediate_placement_failure_books_a_still_live_order_found_via_terminal_lookup(tmp_path, monkeypatch):
    """Codex review ("Verify terminal state after the any-state fallback",
    "Book live failure-envelope orders before continuing"): the immediate
    placement-failure branch's terminal-inclusive fallback carries no state
    filter of its own, so it can surface a LIVE order that only became
    visible in the moment between the live-only lookup (which found
    nothing) and the any-state lookup. Settling that order's
    cumulative_quantity now (even a genuine partial fill so far) would
    resolve the claim before the order is actually done -- the fallback
    must be verified terminal (or "filled") before its quantity is trusted,
    exactly like the sibling reconciliation helper's own identical guard.
    The accepted order must still be BOOKED as submitted/unfilled (using
    the locally-clamped quantity/notional actually attempted), not
    reported as "blocked" -- an earlier version of this fix left later
    candidates in the same batch blind to this accepted, uncounted order.
    """
    monkeypatch.setenv("DCA_CRON_TRACKING_ROOT", str(tmp_path / "cron_tracking"))
    config = StrategyConfig(
        whole_share_tickers=frozenset({"BYDDY"}), dry_run=False, max_per_order_usd=10.0
    )

    class _RaceConditionStillLiveBroker(DryRunBroker):
        def place_whole_share_buy(self, ticker, quantity, ref_id):
            raise RuntimeError("response timeout")

        def same_day_strategy_order_exists_with_ref_id_prefix(self, ticker, trading_day, ref_id_prefixes):
            return None  # live-only lookup ran before the broker indexed the order

        def same_day_order_with_ref_id_prefix_any_state(self, ticker, trading_day, ref_id_prefixes):
            # The SAME order is now visible, still working -- not terminal.
            return {"id": "RH-RACE-IMMEDIATE-1", "state": "confirmed", "cumulative_quantity": "1"}

    broker = _RaceConditionStillLiveBroker(cash=500.0, reference_prices={"BYDDY": 4.0})

    outcome, _, running_deployed_7d, errors, _ = _process_buy_candidate(
        _decision(suggested_amount_usd=16.0, target_price=4.0),
        ctx=_ctx(config, broker),
        running_deployed_7d=0.0,
    )

    # $16 at $4/share plans 4 shares, but the $10 max_per_order_usd ceiling
    # clamps this to 2 shares/$8 -- the locally-clamped figure actually
    # attempted at placement, not the raw suggested amount.
    assert outcome["status"] == "submitted"
    assert outcome["order_id"] == "RH-RACE-IMMEDIATE-1"
    assert outcome["submitted_amount_usd"] == pytest.approx(8.0)
    assert running_deployed_7d == pytest.approx(8.0)
    ledger = WholeShareAccumulationLedger()
    # Still deducted (a real order was placed) but NOT settled -- the claim
    # stays unresolved for a later reconciliation attempt.
    assert ledger.shares_purchased_total("BYDDY") == 0
    assert ledger.unresolved_claim("BYDDY", "2026-08-16", "afternoon-BYDDY") is not None


def test_immediate_placement_failure_books_a_still_live_order_found_via_live_only_lookup(tmp_path, monkeypatch):
    """Codex review ("Book matched live orders before continuing"): the
    immediate placement-failure branch's FIRST reconciliation attempt --
    the live-only lookup (same_day_strategy_order_exists_with_ref_id_prefix)
    -- can itself find a live/working match. An earlier version of this fix
    reported this as "blocked" without booking it, leaving later candidates
    in the same batch blind to a real, uncounted broker order (the same
    hazard already fixed for the sibling terminal-inclusive fallback's
    identical branch). The accepted order must be booked as
    submitted/unfilled using the locally-clamped quantity/notional, while
    the whole-share ledger claim itself stays reserved for a later
    reconciliation attempt.
    """
    monkeypatch.setenv("DCA_CRON_TRACKING_ROOT", str(tmp_path / "cron_tracking"))
    config = StrategyConfig(
        whole_share_tickers=frozenset({"BYDDY"}), dry_run=False, max_per_order_usd=10.0
    )

    class _LiveOnlyMatchBroker(DryRunBroker):
        def place_whole_share_buy(self, ticker, quantity, ref_id):
            raise RuntimeError("response timeout")

        def same_day_strategy_order_exists_with_ref_id_prefix(self, ticker, trading_day, ref_id_prefixes):
            return {"id": "RH-LIVE-ONLY-1", "state": "confirmed", "cumulative_quantity": "1"}

    broker = _LiveOnlyMatchBroker(cash=500.0, reference_prices={"BYDDY": 4.0})

    outcome, _, running_deployed_7d, errors, _ = _process_buy_candidate(
        _decision(suggested_amount_usd=16.0, target_price=4.0),
        ctx=_ctx(config, broker),
        running_deployed_7d=0.0,
    )

    # $16 at $4/share plans 4 shares, but the $10 max_per_order_usd ceiling
    # clamps this to 2 shares/$8 -- the locally-clamped figure actually
    # attempted at placement, not the raw suggested amount.
    assert outcome["status"] == "submitted"
    assert outcome["order_id"] == "RH-LIVE-ONLY-1"
    assert outcome["submitted_amount_usd"] == pytest.approx(8.0)
    assert running_deployed_7d == pytest.approx(8.0)
    ledger = WholeShareAccumulationLedger()
    assert ledger.shares_purchased_total("BYDDY") == 0
    assert ledger.unresolved_claim("BYDDY", "2026-08-16", "afternoon-BYDDY") is not None


def test_immediate_placement_failure_leaves_the_claim_reserved_when_terminal_quantity_is_absent(
    tmp_path, monkeypatch
):
    """Codex review ("Keep terminal claims reserved when fill quantity is
    absent"): the immediate placement-failure branch's own terminal-
    inclusive lookup used to default filled_shares to 0 whenever
    cumulative_quantity was absent -- indistinguishable from an EXPLICIT
    zero -- and then unconditionally restored the full planned notional.
    An absent quantity on a cancelled/rejected order proves nothing about
    what actually executed; the claim must stay reserved instead.
    """
    monkeypatch.setenv("DCA_CRON_TRACKING_ROOT", str(tmp_path / "cron_tracking"))
    config = StrategyConfig(
        whole_share_tickers=frozenset({"BYDDY"}), dry_run=False, max_per_order_usd=10.0
    )

    class _TerminalNoQuantityBroker(DryRunBroker):
        def place_whole_share_buy(self, ticker, quantity, ref_id):
            raise RuntimeError("response timeout")

        def same_day_strategy_order_exists_with_ref_id_prefix(self, ticker, trading_day, ref_id_prefixes):
            return None

        def same_day_order_with_ref_id_prefix_any_state(self, ticker, trading_day, ref_id_prefixes):
            return {"id": "RH-NO-QTY-1", "state": "cancelled"}  # no cumulative_quantity field

    broker = _TerminalNoQuantityBroker(cash=500.0, reference_prices={"BYDDY": 4.0})

    outcome, _, running_deployed_7d, errors, _ = _process_buy_candidate(
        _decision(suggested_amount_usd=16.0, target_price=4.0),
        ctx=_ctx(config, broker),
        running_deployed_7d=0.0,
    )

    assert outcome["status"] == "blocked"
    assert running_deployed_7d == 0.0
    ledger = WholeShareAccumulationLedger()
    assert ledger.shares_purchased_total("BYDDY") == 0
    # NOT restored -- left reserved for later reconciliation.
    assert ledger.unresolved_claim("BYDDY", "2026-08-16", "afternoon-BYDDY") is not None


def test_immediate_placement_failure_marks_broker_placement_failed_for_a_confirmed_zero_fill(
    tmp_path, monkeypatch
):
    """Codex review ("Count confirmed zero-fill orders toward the attempt
    cap"): a confirmed terminal order with an EXPLICIT zero fill is a real
    broker attempt -- config.max_orders_per_day is defined as an attempt
    count where a cancelled/rejected order still consumes one of the
    shared daily slots.

    `_process_buy_candidate` no longer books that attempt itself, though --
    see `test_a_confirmed_zero_fill_consumes_exactly_one_attempt_through_the_batch_funnel`
    below for why, and for the count assertion this test used to make. What
    THIS function is still responsible for, called on its own, is the
    outcome carrying `broker_placement_failed` and the broker's real order
    ID; the batch loop's one funnel reads that marker to do the counting.
    """
    monkeypatch.setenv("DCA_CRON_TRACKING_ROOT", str(tmp_path / "cron_tracking"))
    config = StrategyConfig(
        whole_share_tickers=frozenset({"BYDDY"}), dry_run=False, max_per_order_usd=10.0
    )

    class _ConfirmedZeroFillBroker(DryRunBroker):
        def place_whole_share_buy(self, ticker, quantity, ref_id):
            raise RuntimeError("response timeout")

        def same_day_strategy_order_exists_with_ref_id_prefix(self, ticker, trading_day, ref_id_prefixes):
            return None

        def same_day_order_with_ref_id_prefix_any_state(self, ticker, trading_day, ref_id_prefixes):
            return {"id": "RH-ZERO-FILL-1", "state": "rejected", "cumulative_quantity": "0"}

    broker = _ConfirmedZeroFillBroker(cash=500.0, reference_prices={"BYDDY": 4.0})
    ctx = _ctx(config, broker)

    outcome, _, running_deployed_7d, errors, _ = _process_buy_candidate(
        _decision(suggested_amount_usd=16.0, target_price=4.0),
        ctx=ctx,
        running_deployed_7d=0.0,
    )

    assert outcome["status"] == "failed"
    assert outcome["order_id"] == "RH-ZERO-FILL-1"
    assert outcome["broker_placement_failed"] is True
    assert running_deployed_7d == 0.0
    ledger = WholeShareAccumulationLedger()
    assert ledger.shares_purchased_total("BYDDY") == 0
    assert ledger.unresolved_claim("BYDDY", "2026-08-16", "afternoon-BYDDY") is None


def test_a_confirmed_zero_fill_consumes_exactly_one_attempt_through_the_batch_funnel(
    tmp_path, monkeypatch
):
    """`_process_whole_share_candidate`'s raise-then-confirmed-zero-fill
    branch used to call `record_buy(ticker, trading_day, 0.0)` itself AND
    return an outcome carrying `broker_placement_failed` -- and
    `_process_buy_candidates_batch`'s one funnel already calls a
    zero-notional `record_buy` for every candidate whose outcome carries
    that marker. `_process_buy_candidate` is called ONLY from that batch
    loop in production (docs/live_wiring.md), so both charges land on the
    real path together: one raise, one confirmed broker attempt, but TWO
    `max_orders_per_day` slots consumed, which can skip a later valid
    candidate near the daily cap (Codex review, PR #786: "Avoid charging
    zero-fill whole-share attempts twice").

    Measured through the actual caller rather than the direct call the
    prior version of this test used, which is what let the double charge go
    unnoticed: calling `_process_buy_candidate` alone never reached the
    funnel at all.
    """
    monkeypatch.setenv("DCA_CRON_TRACKING_ROOT", str(tmp_path / "cron_tracking"))
    # This module-level cache is not test-scoped, so a prior test's own
    # seeded outcome for "BYDDY" would otherwise leak in here -- every other
    # test in this file that calls _process_buy_candidates_batch resets it
    # first for the same reason.
    _near_close_dca._IN_FLIGHT_CANDIDATE_OUTCOMES["outcomes"] = {}
    config = StrategyConfig(
        whole_share_tickers=frozenset({"BYDDY"}), dry_run=False, max_per_order_usd=10.0
    )

    class _ConfirmedZeroFillBroker(DryRunBroker):
        def place_whole_share_buy(self, ticker, quantity, ref_id):
            raise RuntimeError("response timeout")

        def same_day_strategy_order_exists_with_ref_id_prefix(self, ticker, trading_day, ref_id_prefixes):
            return None

        def same_day_order_with_ref_id_prefix_any_state(self, ticker, trading_day, ref_id_prefixes):
            return {"id": "RH-ZERO-FILL-1", "state": "rejected", "cumulative_quantity": "0"}

    broker = _ConfirmedZeroFillBroker(cash=500.0, reference_prices={"BYDDY": 4.0})
    ctx = _ctx(config, broker)
    decision = _decision(suggested_amount_usd=16.0, target_price=4.0)

    candidate_outcomes, _, running_deployed_7d, errors, _ = _process_buy_candidates_batch(
        [decision], ctx
    )

    outcome = candidate_outcomes["BYDDY"]
    assert outcome["status"] == "failed"
    assert outcome["order_id"] == "RH-ZERO-FILL-1"
    assert running_deployed_7d == 0.0
    # The confirmed attempt is counted exactly ONCE, even though nothing was
    # spent -- not twice, which is what the internal record_buy call plus
    # the funnel's own charge produced before the fix.
    assert ctx.state.day_order_count(date(2026, 8, 16)) == 1
    assert ctx.state.day_notional(date(2026, 8, 16)) == 0.0
    ledger = WholeShareAccumulationLedger()
    assert ledger.shares_purchased_total("BYDDY") == 0
    assert ledger.unresolved_claim("BYDDY", "2026-08-16", "afternoon-BYDDY") is None


def test_a_confirmation_hold_merged_with_a_recovered_fill_does_not_consume_an_attempt(
    tmp_path, monkeypatch
):
    """Codex review, PR #786 ("Check the pre-merge status before charging
    confirmation holds").

    When an earlier recovered fill for a ticker merges with TODAY's own
    `not_submitted_confirmation_required` outcome, `_merge_whole_share_
    submission_outcomes` forces the merged row's status to "submitted" (a
    real order really was recovered) -- but `broker_placement_failed`
    survives the merge unchanged from today's own outcome, and the
    attempt-charge exemption read only the (now-overwritten) `status`
    field. That let a confirmation hold that made no placement attempt of
    its own consume a `max_orders_per_day` slot anyway, purely because it
    happened to share a ticker with an unrelated recovered order this same
    run. Fixed by also checking `merged_confirmation_required`, the marker
    `_merge_whole_share_submission_outcomes` sets for exactly this case.
    """
    monkeypatch.setenv("DCA_CRON_TRACKING_ROOT", str(tmp_path / "cron_tracking"))
    _near_close_dca._IN_FLIGHT_CANDIDATE_OUTCOMES["outcomes"] = {
        "BYDDY": {
            "status": "submitted",
            "order_id": "RH-RECOVERED-CONFIRMATION-HOLD",
            "skip_reason": None,
            "submitted_amount_usd": 10.0,
            "whole_share_quantity": 1,
        }
    }
    config = StrategyConfig(whole_share_tickers=frozenset({"BYDDY"}), dry_run=False)

    class _ConfirmationRequiredBroker(DryRunBroker):
        def place_whole_share_buy(self, ticker, quantity, ref_id):
            raise OrderConfirmationRequiredError("broker requires step-up confirmation")

    broker = _ConfirmationRequiredBroker(cash=500.0, reference_prices={"BYDDY": 10.0})
    decision = _decision(suggested_amount_usd=10.0, target_price=10.0)
    ctx = _ctx(config, broker)

    candidate_outcomes, _, _, _, _ = _process_buy_candidates_batch([decision], ctx)

    outcome = candidate_outcomes["BYDDY"]
    # The merge really did force "submitted" -- confirming the scenario the
    # finding describes actually occurs.
    assert outcome["status"] == "submitted"
    assert outcome["merged_confirmation_required"] is True
    # Today's confirmation hold made no placement attempt of its own, so it
    # must not consume a max_orders_per_day slot.
    assert ctx.state.day_order_count(date(2026, 8, 16)) == 0


def test_immediate_placement_failure_does_not_count_a_zero_fill_attempt_when_release_fails(
    tmp_path, monkeypatch
):
    """Codex review ("Deduplicate zero-fill attempts when claim release
    fails"): mirrors the cross-day reconciliation helper's own identical
    fix, but for the IMMEDIATE (in-line) placement-failure path. The
    attempt was previously recorded UNCONDITIONALLY, before
    preserve_pending() even ran -- if that release itself raises, the
    claim is STILL unresolved, so a LATER reconciliation attempt (once
    release finally succeeds) would record the identical attempt a
    second time for one real cancelled/rejected order.
    """
    monkeypatch.setenv("DCA_CRON_TRACKING_ROOT", str(tmp_path / "cron_tracking"))
    config = StrategyConfig(
        whole_share_tickers=frozenset({"BYDDY"}), dry_run=False, max_per_order_usd=10.0
    )

    class _ConfirmedZeroFillBroker(DryRunBroker):
        def place_whole_share_buy(self, ticker, quantity, ref_id):
            raise RuntimeError("response timeout")

        def same_day_strategy_order_exists_with_ref_id_prefix(self, ticker, trading_day, ref_id_prefixes):
            return None

        def same_day_order_with_ref_id_prefix_any_state(self, ticker, trading_day, ref_id_prefixes):
            return {"id": "RH-ZERO-FILL-RELEASE-FAILS", "state": "rejected", "cumulative_quantity": "0"}

    def _raising_preserve_pending(self, *args, **kwargs):
        raise RuntimeError("ledger write failed")

    monkeypatch.setattr(WholeShareAccumulationLedger, "preserve_pending_if_claimed", _raising_preserve_pending)

    broker = _ConfirmedZeroFillBroker(cash=500.0, reference_prices={"BYDDY": 4.0})
    ctx = _ctx(config, broker)

    outcome, _, running_deployed_7d, errors, _ = _process_buy_candidate(
        _decision(suggested_amount_usd=16.0, target_price=4.0),
        ctx=ctx,
        running_deployed_7d=0.0,
    )

    assert outcome["status"] == "failed"
    assert outcome["order_id"] == "RH-ZERO-FILL-RELEASE-FAILS"
    # The release failed, so nothing is counted -- a LATER reconciliation
    # attempt, once release succeeds, is the one that should book it.
    assert ctx.state.day_order_count(date(2026, 8, 16)) == 0


def test_pre_roll_zero_fill_recovery_counts_toward_the_attempt_cap(tmp_path, monkeypatch):
    """A terminal zero fill discovered by the batch pre-pass is an attempt."""
    monkeypatch.setenv("DCA_CRON_TRACKING_ROOT", str(tmp_path / "cron_tracking"))
    ledger = WholeShareAccumulationLedger()
    ledger.record_contribution(
        "BYDDY", "2026-08-15", 10.0, 10.0, stream="afternoon",
        idempotency_key="afternoon-BYDDY", recorded_at="2026-08-15T15:30:00-04:00",
    )

    class _PreRollZeroFillBroker(DryRunBroker):
        def same_day_strategy_order_exists_with_ref_id_prefix(self, ticker, trading_day, ref_id_prefixes):
            return None

        def same_day_order_with_ref_id_prefix_any_state(self, ticker, trading_day, ref_id_prefixes):
            return {"id": "RH-PRE-ROLL-ZERO", "state": "rejected", "cumulative_quantity": "0"}

    ctx = _ctx(
        StrategyConfig(whole_share_tickers=frozenset({"BYDDY"}), dry_run=False),
        _PreRollZeroFillBroker(cash=500.0, reference_prices={"BYDDY": 10.0}),
    )
    _near_close_dca._IN_FLIGHT_CANDIDATE_OUTCOMES["outcomes"] = {}

    deployed = _reconcile_cross_day_whole_share_claims_for_batch(ctx, 0.0)

    assert deployed == 0.0
    assert ctx.state.day_order_count(date(2026, 8, 15)) == 1
    assert ctx.state.day_notional(date(2026, 8, 15)) == 0.0
    assert _near_close_dca._IN_FLIGHT_CANDIDATE_OUTCOMES["outcomes"]["BYDDY"]["order_id"] == "RH-PRE-ROLL-ZERO"
    assert ledger.unresolved_claim("BYDDY", "2026-08-15", "afternoon-BYDDY") is None


def test_pre_roll_claim_reconciliation_failure_for_one_ticker_does_not_stop_the_others(
    tmp_path, monkeypatch, caplog
):
    """The pre-pass runs BEFORE any candidate is processed, so one symbol's
    unreadable claim must not end the run before a single ticker is evaluated.
    That isolation lives one level down, per CLAIM, inside
    _reconcile_cross_day_whole_share_claims -- this pins that it covers the
    whole-ticker case too."""
    monkeypatch.setenv("DCA_CRON_TRACKING_ROOT", str(tmp_path / "cron_tracking"))
    ledger = WholeShareAccumulationLedger()
    for symbol in ("BYDDY", "RVII"):
        ledger.record_contribution(
            symbol, "2026-08-15", 10.0, 10.0, stream="afternoon",
            idempotency_key=f"afternoon-{symbol}", recorded_at="2026-08-15T15:30:00-04:00",
        )

    class _RecoveringBroker(DryRunBroker):
        def same_day_strategy_order_exists_with_ref_id_prefix(self, ticker, trading_day, ref_id_prefixes):
            return None

        def same_day_order_with_ref_id_prefix_any_state(self, ticker, trading_day, ref_id_prefixes):
            return {"id": f"RH-{ticker}-RECOVERED", "state": "filled", "cumulative_quantity": "1"}

    ctx = _ctx(
        StrategyConfig(whole_share_tickers=frozenset({"BYDDY", "RVII"}), dry_run=False),
        _RecoveringBroker(cash=500.0, reference_prices={"BYDDY": 10.0, "RVII": 10.0}),
    )
    _near_close_dca._IN_FLIGHT_CANDIDATE_OUTCOMES["outcomes"] = {}

    original = _near_close_dca._reconcile_unresolved_whole_share_claim

    def _boom_for_byddy(ledger_, broker, ticker, *args, **kwargs):
        if ticker == "BYDDY":
            raise RuntimeError("BYDDY ledger is unreadable")
        return original(ledger_, broker, ticker, *args, **kwargs)

    monkeypatch.setattr(
        _near_close_dca, "_reconcile_unresolved_whole_share_claim", _boom_for_byddy
    )

    with caplog.at_level(logging.WARNING, logger="near_close_dca"):
        _reconcile_cross_day_whole_share_claims_for_batch(ctx, 0.0)

    # RVII is sorted after BYDDY: it was still reconciled.
    assert _near_close_dca._IN_FLIGHT_CANDIDATE_OUTCOMES["outcomes"]["RVII"]["order_id"] == (
        "RH-RVII-RECOVERED"
    )
    assert "BYDDY" not in _near_close_dca._IN_FLIGHT_CANDIDATE_OUTCOMES["outcomes"]
    # Nothing is lost: BYDDY's claim is still there for the next run.
    assert ledger.unresolved_claim("BYDDY", "2026-08-15", "afternoon-BYDDY") is not None
    assert "BYDDY ledger is unreadable" in caplog.text


def test_a_swallowed_pre_roll_failure_is_reported_to_the_error_sink(tmp_path, monkeypatch):
    """A best-effort failure here leaves a REAL claim unreconciled, and every
    other per-ticker net in this module feeds errors_count -- and through it
    usage_proxy.manual_recovery_required. This pass only logged, so a run that
    could not read the whole-share ledger at all still reported itself healthy
    (Antigravity review, PR #786: "Error tracking in
    _reconcile_cross_day_whole_share_claims_for_batch")."""
    monkeypatch.setenv("DCA_CRON_TRACKING_ROOT", str(tmp_path / "cron_tracking"))
    ledger = WholeShareAccumulationLedger()
    ledger.record_contribution(
        "BYDDY", "2026-08-15", 10.0, 10.0, stream="afternoon",
        idempotency_key="afternoon-BYDDY", recorded_at="2026-08-15T15:30:00-04:00",
    )

    class _RecoveringBroker(DryRunBroker):
        def same_day_strategy_order_exists_with_ref_id_prefix(self, ticker, trading_day, ref_id_prefixes):
            return None

        def same_day_order_with_ref_id_prefix_any_state(self, ticker, trading_day, ref_id_prefixes):
            return {"id": "RH-BYDDY-RECOVERED", "state": "filled", "cumulative_quantity": "1"}

    ctx = _ctx(
        StrategyConfig(whole_share_tickers=frozenset({"BYDDY"}), dry_run=False),
        _RecoveringBroker(cash=500.0, reference_prices={"BYDDY": 10.0}),
    )
    _near_close_dca._IN_FLIGHT_CANDIDATE_OUTCOMES["outcomes"] = {}

    def _boom(*args, **kwargs):
        raise RuntimeError("ledger unreadable: account 999000111")

    monkeypatch.setattr(
        _near_close_dca, "_reconcile_unresolved_whole_share_claim", _boom
    )

    errors: list = []
    _reconcile_cross_day_whole_share_claims_for_batch(ctx, 0.0, error_sink=errors)

    assert len(errors) == 1, errors
    assert "BYDDY" in errors[0]
    assert "RuntimeError" in errors[0]
    # Type only, never the message -- this string reaches run evidence, and a
    # ledger/broker exception can carry an account identifier.
    assert "999000111" not in errors[0]

    # The sink is optional: every existing caller that only wants the
    # deployment figure keeps working, and omitting it swallows exactly as
    # before.
    _reconcile_cross_day_whole_share_claims_for_batch(ctx, 0.0)


def test_a_swallowed_same_day_existence_lookup_reaches_the_error_sink(tmp_path, monkeypatch):
    """The same-day branch has two more swallowed broker lookups, and they were
    left out of the sink. Both `continue` past a claim they could not resolve,
    so a run whose broker could not answer either question still reported
    itself clean -- no errors_count, no manual_recovery_required (Codex review,
    PR #786: "Count broker lookup failures in pre-roll health")."""
    monkeypatch.setenv("DCA_CRON_TRACKING_ROOT", str(tmp_path / "cron_tracking"))
    ledger = WholeShareAccumulationLedger()
    # TODAY's own claim: what reaches the same-day branch at all.
    ledger.record_contribution(
        "BYDDY", "2026-08-16", 10.0, 10.0, stream="afternoon",
        idempotency_key="afternoon-BYDDY", recorded_at="2026-08-16T15:30:00-04:00",
    )

    class _ExistenceCheckDown(DryRunBroker):
        def same_day_strategy_order_exists(self, ticker, trading_day):
            raise RuntimeError("broker unavailable: account 999000111")

    ctx = _ctx(
        StrategyConfig(whole_share_tickers=frozenset({"BYDDY"}), dry_run=False),
        _ExistenceCheckDown(cash=500.0, reference_prices={"BYDDY": 10.0}),
    )
    _near_close_dca._IN_FLIGHT_CANDIDATE_OUTCOMES["outcomes"] = {}

    errors: list = []
    _reconcile_cross_day_whole_share_claims_for_batch(ctx, 0.0, error_sink=errors)

    assert len(errors) == 1, errors
    assert "BYDDY" in errors[0]
    assert "RuntimeError" in errors[0]
    # Type only, never the message -- a broker error can carry an account
    # identifier and this string reaches persisted run evidence.
    assert "999000111" not in errors[0]

    # Still optional, and still swallowed exactly as before when omitted.
    _reconcile_cross_day_whole_share_claims_for_batch(ctx, 0.0)


def test_a_swallowed_same_day_terminal_lookup_reaches_the_error_sink(tmp_path, monkeypatch):
    """The higher-stakes of the two: this is the lookup that catches an order
    that partially filled and was THEN cancelled today, so a swallowed failure
    can end with record_contribution() reclaiming the claim and re-submitting
    the full quantity -- excess shares bought against reused credit."""
    monkeypatch.setenv("DCA_CRON_TRACKING_ROOT", str(tmp_path / "cron_tracking"))
    ledger = WholeShareAccumulationLedger()
    ledger.record_contribution(
        "BYDDY", "2026-08-16", 10.0, 10.0, stream="afternoon",
        idempotency_key="afternoon-BYDDY", recorded_at="2026-08-16T15:30:00-04:00",
    )

    class _TerminalHistoryDown(DryRunBroker):
        def same_day_strategy_order_exists(self, ticker, trading_day):
            # Nothing LIVE today, which is what sends the claim on to the
            # terminal-inclusive lookup below.
            return False

        def same_day_order_with_ref_id_prefix_any_state(self, ticker, trading_day, ref_id_prefixes):
            raise TimeoutError("history unavailable: account 999000111")

    ctx = _ctx(
        StrategyConfig(whole_share_tickers=frozenset({"BYDDY"}), dry_run=False),
        _TerminalHistoryDown(cash=500.0, reference_prices={"BYDDY": 10.0}),
    )
    _near_close_dca._IN_FLIGHT_CANDIDATE_OUTCOMES["outcomes"] = {}

    errors: list = []
    _reconcile_cross_day_whole_share_claims_for_batch(ctx, 0.0, error_sink=errors)

    assert len(errors) == 1, errors
    assert "BYDDY" in errors[0]
    assert "TimeoutError" in errors[0]
    assert "999000111" not in errors[0]


def test_a_booking_failure_after_a_durable_settle_stops_the_run(tmp_path, monkeypatch):
    """The one reconciliation failure that must NOT be isolated.

    _book_reconciled_whole_share_order runs AFTER the claim has already been
    durably settled, so its failure is not "left unresolved for the next run"
    -- that recovered order is real spend the ledger will never surface again.
    Swallowing it would let every later candidate in the batch place against
    an understated running_deployed_7d and miss a kill-switch transition the
    recovered order should have caused (Codex review, PR #786: "Stop
    placements when recovered spend cannot be booked"). Failing the run is the
    fail-closed answer: nothing is placed against a total known to be wrong.
    """
    monkeypatch.setenv("DCA_CRON_TRACKING_ROOT", str(tmp_path / "cron_tracking"))
    ledger = WholeShareAccumulationLedger()
    ledger.record_contribution(
        "BYDDY", "2026-08-15", 10.0, 10.0, stream="afternoon",
        idempotency_key="afternoon-BYDDY", recorded_at="2026-08-15T15:30:00-04:00",
    )

    class _RecoveringBroker(DryRunBroker):
        def same_day_strategy_order_exists_with_ref_id_prefix(self, ticker, trading_day, ref_id_prefixes):
            return None

        def same_day_order_with_ref_id_prefix_any_state(self, ticker, trading_day, ref_id_prefixes):
            return {"id": "RH-BYDDY-RECOVERED", "state": "filled", "cumulative_quantity": "1"}

    ctx = _ctx(
        StrategyConfig(whole_share_tickers=frozenset({"BYDDY"}), dry_run=False),
        _RecoveringBroker(cash=500.0, reference_prices={"BYDDY": 10.0}),
    )
    _near_close_dca._IN_FLIGHT_CANDIDATE_OUTCOMES["outcomes"] = {}

    def _boom(*args, **kwargs):
        raise RuntimeError("deployment history write failed")

    monkeypatch.setattr(_near_close_dca, "_book_reconciled_whole_share_order", _boom)

    with pytest.raises(RuntimeError, match="deployment history write failed"):
        _reconcile_cross_day_whole_share_claims_for_batch(ctx, 0.0)


def test_pre_roll_live_order_recovery_books_the_planned_notional_and_leaves_the_claim_reserved(
    tmp_path, monkeypatch
):
    """Codex review ("Book live orders discovered during claim recovery"):
    the batch pre-pass finds a still-LIVE (not yet terminal) match for an
    unresolved cross-day claim. The ledger claim itself must stay reserved
    -- the real filled quantity isn't known yet -- but the accepted order
    must still be booked as submitted/unfilled using the claim's own
    planned notional/quantity, so later candidates in the same batch see
    it in running_deployed_7d/the daily attempt count instead of treating
    it as if nothing were attempted.
    """
    monkeypatch.setenv("DCA_CRON_TRACKING_ROOT", str(tmp_path / "cron_tracking"))
    ledger = WholeShareAccumulationLedger()
    ledger.record_contribution(
        "BYDDY", "2026-08-15", 10.0, 10.0, stream="afternoon",
        idempotency_key="afternoon-BYDDY", recorded_at="2026-08-15T15:30:00-04:00",
    )

    class _StillLiveBroker(DryRunBroker):
        def same_day_strategy_order_exists_with_ref_id_prefix(self, ticker, trading_day, ref_id_prefixes):
            return {"id": "RH-PRE-ROLL-LIVE", "state": "confirmed", "cumulative_quantity": "0"}

    ctx = _ctx(
        StrategyConfig(whole_share_tickers=frozenset({"BYDDY"}), dry_run=False),
        _StillLiveBroker(cash=500.0, reference_prices={"BYDDY": 10.0}),
    )
    _near_close_dca._IN_FLIGHT_CANDIDATE_OUTCOMES["outcomes"] = {}

    deployed = _reconcile_cross_day_whole_share_claims_for_batch(ctx, 0.0)

    assert deployed == pytest.approx(10.0)
    assert ctx.state.day_order_count(date(2026, 8, 15)) == 1
    assert ctx.state.day_notional(date(2026, 8, 15)) == pytest.approx(10.0)
    outcome = _near_close_dca._IN_FLIGHT_CANDIDATE_OUTCOMES["outcomes"]["BYDDY"]
    assert outcome["status"] == "submitted"
    assert outcome["order_id"] == "RH-PRE-ROLL-LIVE"
    assert outcome["submitted_amount_usd"] == pytest.approx(10.0)
    # The ledger claim itself is still unresolved -- nothing was settled.
    assert ledger.unresolved_claim("BYDDY", "2026-08-15", "afternoon-BYDDY") is not None


def test_pre_roll_live_order_recovery_books_the_actual_submitted_quantity_when_clamped(
    tmp_path, monkeypatch
):
    """Codex review ("Book only the executable quantity for recovered live
    orders"): the unresolved claim's own shares_to_buy/order_notional_usd
    are the PRE-CLAMP planned figures. When the actual placement was
    clamped (by the per-order ceiling or the kill switch) to fewer shares
    than planned, the still-live matched order's own "quantity" field
    reflects what was REALLY submitted -- e.g. a 3-share $30 claim actually
    placed as a 1-share $10 order. Booking the claim's unclamped figure
    here would overstate this run's own batch caps and trailing
    deployment enough to spuriously block unrelated candidates.
    """
    monkeypatch.setenv("DCA_CRON_TRACKING_ROOT", str(tmp_path / "cron_tracking"))
    ledger = WholeShareAccumulationLedger()
    # $30 pending at a cached $10.00/share price plans 3 shares.
    ledger.record_contribution(
        "BYDDY", "2026-08-15", 30.0, 10.0, stream="afternoon",
        idempotency_key="afternoon-BYDDY", recorded_at="2026-08-15T15:30:00-04:00",
    )

    class _ClampedStillLiveBroker(DryRunBroker):
        def same_day_strategy_order_exists_with_ref_id_prefix(self, ticker, trading_day, ref_id_prefixes):
            # Only 1 of the planned 3 shares was actually submitted.
            return {"id": "RH-CLAMPED-LIVE", "state": "confirmed", "quantity": "1", "cumulative_quantity": "0"}

    ctx = _ctx(
        StrategyConfig(whole_share_tickers=frozenset({"BYDDY"}), dry_run=False),
        _ClampedStillLiveBroker(cash=500.0, reference_prices={"BYDDY": 10.0}),
    )
    _near_close_dca._IN_FLIGHT_CANDIDATE_OUTCOMES["outcomes"] = {}

    deployed = _reconcile_cross_day_whole_share_claims_for_batch(ctx, 0.0)

    assert deployed == pytest.approx(10.0)
    assert ctx.state.day_notional(date(2026, 8, 15)) == pytest.approx(10.0)
    outcome = _near_close_dca._IN_FLIGHT_CANDIDATE_OUTCOMES["outcomes"]["BYDDY"]
    assert outcome["submitted_amount_usd"] == pytest.approx(10.0)
    assert outcome["whole_share_quantity"] == 1
    assert ledger.unresolved_claim("BYDDY", "2026-08-15", "afternoon-BYDDY") is not None


def test_a_still_live_recovered_order_already_recorded_on_a_later_day_is_not_rebooked(
    tmp_path, monkeypatch
):
    """Codex review ("Avoid rebooking a recovered live order on every
    later run"): a "live_order_found" claim stays deliberately unresolved
    while its order remains live, so it is reconciled and (without this
    fix) rebooked on EVERY subsequent run. The FIRST recovery's own
    artifact is written under THAT run's own trading_day, never the
    claim's original trading_day -- an exact-trading_day dedup match can
    never find it. This test simulates exactly that: a prior run (one day
    after the claim) already recorded the recovered order on disk, and a
    SECOND, later run's pre-pass must not book it again.
    """
    from test_near_close_dca import _write_scheduled_afternoon_artifact

    monkeypatch.setenv("DCA_CRON_TRACKING_ROOT", str(tmp_path / "cron_tracking"))
    ledger = WholeShareAccumulationLedger()
    claim_trading_day = date(2026, 8, 15)
    ledger.record_contribution(
        "BYDDY", claim_trading_day.isoformat(), 10.0, 10.0, stream="afternoon",
        idempotency_key="afternoon-BYDDY", recorded_at="2026-08-15T15:30:00-04:00",
    )

    # Simulates the FIRST recovery run (2026-08-16): it found the same
    # still-live order and published a "submitted" outcome under ITS OWN
    # trading_day, not the claim's original 2026-08-15.
    history_root = str(tmp_path / "runs")
    _write_scheduled_afternoon_artifact(
        history_root,
        date(2026, 8, 16),
        [{"ticker": "BYDDY", "status": "submitted", "order_id": "RH-STILL-LIVE-1", "planned_amount_usd": 10.0}],
    )

    class _StillLiveBroker(DryRunBroker):
        def same_day_strategy_order_exists_with_ref_id_prefix(self, ticker, trading_day, ref_id_prefixes):
            return {"id": "RH-STILL-LIVE-1", "state": "confirmed", "cumulative_quantity": "0"}

    config = StrategyConfig(whole_share_tickers=frozenset({"BYDDY"}), dry_run=False)
    ctx = ProcessCandidateContext(
        config=config,
        state=StrategyState(),
        broker=_StillLiveBroker(cash=500.0, reference_prices={"BYDDY": 10.0}),
        robinhood_data=None,
        now=datetime(2026, 8, 17, 15, 45, tzinfo=EASTERN),
        trading_day=date(2026, 8, 17),
        guard_active=False,
        prior_same_day_allocations={},
        scheduled=True,
        run_result=None,
        deployment_history_root=history_root,
    )
    _near_close_dca._IN_FLIGHT_CANDIDATE_OUTCOMES["outcomes"] = {}

    deployed = _reconcile_cross_day_whole_share_claims_for_batch(ctx, 0.0)

    # Not rebooked -- the SECOND run's own state/deployment total sees
    # nothing new, since the FIRST run's artifact already accounts for it.
    assert deployed == 0.0
    assert ctx.state.day_order_count(date(2026, 8, 15)) == 0
    assert "BYDDY" not in _near_close_dca._IN_FLIGHT_CANDIDATE_OUTCOMES["outcomes"]
    # The ledger claim itself is STILL unresolved -- the order hasn't
    # terminated yet, only the double-booking is prevented.
    assert ledger.unresolved_claim("BYDDY", "2026-08-15", "afternoon-BYDDY") is not None


def test_a_terminally_filled_recovery_is_not_rebooked_after_an_earlier_live_provisional_booking(
    tmp_path, monkeypatch
):
    """Codex review ("Deduplicate terminal recoveries by broker order
    ID"): an earlier run provisionally booked this exact order as
    "live_order_found" under ITS OWN trading_day (not the claim's
    original day). A LATER run that finds the SAME order now terminally
    FILLED, settled for exactly the SAME amount that was already
    provisionally recorded, must not book and publish it a second time --
    the ticker/day dedup check alone (anchored at the claim's original
    day) can never see that earlier provisional artifact, so it must also
    fall back to an order-id match.
    """
    from test_near_close_dca import _write_scheduled_afternoon_artifact

    monkeypatch.setenv("DCA_CRON_TRACKING_ROOT", str(tmp_path / "cron_tracking"))
    ledger = WholeShareAccumulationLedger()
    claim_trading_day = date(2026, 8, 15)
    ledger.record_contribution(
        "BYDDY", claim_trading_day.isoformat(), 10.0, 10.0, stream="afternoon",
        idempotency_key="afternoon-BYDDY", recorded_at="2026-08-15T15:30:00-04:00",
    )

    # Simulates the FIRST recovery run (2026-08-16): found the order still
    # live and provisionally booked it under ITS OWN trading_day, with the
    # SAME shape real production outcomes publish -- the top-level dollar
    # field is persisted as "planned_amount_usd" (not "submitted_amount_usd",
    # which never survives serialization to disk), and the PER-CLAIM
    # provisional amount lives in whole_share_claim_breakdown, keyed by
    # the claim's own trading_day.
    history_root = str(tmp_path / "runs")
    _write_scheduled_afternoon_artifact(
        history_root,
        date(2026, 8, 16),
        [{
            "ticker": "BYDDY", "status": "submitted", "order_id": "RH-NOW-FILLED-1",
            "planned_amount_usd": 10.0,
            "whole_share_claim_breakdown": [{"trading_day": "2026-08-15", "amount_usd": 10.0}],
        }],
    )

    class _NowFilledBroker(DryRunBroker):
        def same_day_strategy_order_exists_with_ref_id_prefix(self, ticker, trading_day, ref_id_prefixes):
            return {"id": "RH-NOW-FILLED-1", "state": "filled", "quantity": "1"}

    config = StrategyConfig(whole_share_tickers=frozenset({"BYDDY"}), dry_run=False)
    ctx = ProcessCandidateContext(
        config=config,
        state=StrategyState(),
        broker=_NowFilledBroker(cash=500.0, reference_prices={"BYDDY": 10.0}),
        robinhood_data=None,
        now=datetime(2026, 8, 17, 15, 45, tzinfo=EASTERN),
        trading_day=date(2026, 8, 17),
        guard_active=False,
        prior_same_day_allocations={},
        scheduled=True,
        run_result=None,
        deployment_history_root=history_root,
    )
    _near_close_dca._IN_FLIGHT_CANDIDATE_OUTCOMES["outcomes"] = {}

    deployed = _reconcile_cross_day_whole_share_claims_for_batch(ctx, 0.0)

    # The ledger DOES settle the claim for real (that part is legitimate
    # and correct -- the order really did fill)...
    assert ledger.unresolved_claim("BYDDY", "2026-08-15", "afternoon-BYDDY") is None
    assert ledger.shares_purchased_total("BYDDY") == 1
    # ...but this run's OWN state/deployment total must not double-book it,
    # since the first recovery run's artifact already accounts for the
    # SAME $10 amount (no reconciliation delta).
    assert deployed == 0.0
    assert ctx.state.day_order_count(claim_trading_day) == 0
    assert "BYDDY" not in _near_close_dca._IN_FLIGHT_CANDIDATE_OUTCOMES["outcomes"]


def test_a_terminally_filled_recovery_publishes_the_delta_from_an_earlier_provisional_amount(
    tmp_path, monkeypatch
):
    """Companion to the exact-amount dedup test above: when the terminal
    settlement's real amount DIFFERS from what an earlier run's
    provisional "live_order_found" booking already recorded (a
    provisional booking has no real fill price yet, so it can differ from
    the eventual settlement), the difference must still be reconciled --
    silently skipping it forever (as an earlier version of this fix did)
    would leave the provisional artifact's stale figure permanently
    uncorrected in dca_risk.deployed_usd_by_claim_date()'s own sum across
    artifacts.
    """
    from test_near_close_dca import _write_scheduled_afternoon_artifact

    monkeypatch.setenv("DCA_CRON_TRACKING_ROOT", str(tmp_path / "cron_tracking"))
    ledger = WholeShareAccumulationLedger()
    claim_trading_day = date(2026, 8, 15)
    ledger.record_contribution(
        "BYDDY", claim_trading_day.isoformat(), 10.0, 10.0, stream="afternoon",
        idempotency_key="afternoon-BYDDY", recorded_at="2026-08-15T15:30:00-04:00",
    )

    # The FIRST recovery run provisionally recorded $10 (1 share at the
    # claim's own cached $10.00 price -- the real fill price wasn't known
    # yet while the order was still live). The persisted PER-CLAIM amount
    # lives in whole_share_claim_breakdown, not a top-level dollar field.
    history_root = str(tmp_path / "runs")
    _write_scheduled_afternoon_artifact(
        history_root,
        date(2026, 8, 16),
        [{
            "ticker": "BYDDY", "status": "submitted", "order_id": "RH-DELTA-FILL-1",
            "planned_amount_usd": 10.0,
            "whole_share_claim_breakdown": [{"trading_day": "2026-08-15", "amount_usd": 10.0}],
        }],
    )

    class _FilledAboveCachedPriceBroker(DryRunBroker):
        def same_day_strategy_order_exists_with_ref_id_prefix(self, ticker, trading_day, ref_id_prefixes):
            # The order actually filled at $12.00/share, ABOVE the cached
            # $10.00 signal-time price the provisional booking used.
            return {"id": "RH-DELTA-FILL-1", "state": "filled", "quantity": "1", "average_price": "12.00"}

    config = StrategyConfig(whole_share_tickers=frozenset({"BYDDY"}), dry_run=False)
    ctx = ProcessCandidateContext(
        config=config,
        state=StrategyState(),
        broker=_FilledAboveCachedPriceBroker(cash=500.0, reference_prices={"BYDDY": 10.0}),
        robinhood_data=None,
        now=datetime(2026, 8, 17, 15, 45, tzinfo=EASTERN),
        trading_day=date(2026, 8, 17),
        guard_active=False,
        prior_same_day_allocations={},
        scheduled=True,
        run_result=None,
        deployment_history_root=history_root,
    )
    _near_close_dca._IN_FLIGHT_CANDIDATE_OUTCOMES["outcomes"] = {}

    deployed = _reconcile_cross_day_whole_share_claims_for_batch(ctx, 0.0)

    assert ledger.unresolved_claim("BYDDY", "2026-08-15", "afternoon-BYDDY") is None
    # Only the $2.00 DELTA (the real $12 fill minus the $10 already
    # provisionally recorded) is booked/published -- not the full $12
    # again, which would double-count the first $10.
    assert deployed == pytest.approx(2.0)
    outcome = _near_close_dca._IN_FLIGHT_CANDIDATE_OUTCOMES["outcomes"]["BYDDY"]
    assert outcome["submitted_amount_usd"] == pytest.approx(2.0)
    # Marked so a later run's load_prior_same_day_allocations never counts
    # this row as a fresh attempt (Codex review: "Exclude same-day
    # settlement deltas from attempt counts").
    assert outcome["whole_share_settlement_adjustment"] is True
    # record_buy() is deliberately NOT called for a settlement-adjustment
    # delta -- the provisional booking already counted this attempt once
    # (Codex review: "Avoid counting a settlement adjustment as another
    # order attempt"), so claim_trading_day's own day_order_count must
    # stay at whatever the (separate, earlier) provisional booking left
    # it -- 0 here, since that booking happened in a DIFFERENT run/ctx.state.
    assert ctx.state.day_order_count(claim_trading_day) == 0


def test_a_terminally_filled_recovery_below_the_provisional_amount_is_not_republished(
    tmp_path, monkeypatch
):
    """Companion to the upward-delta test above: when the real settlement
    is BELOW what was already provisionally recorded (a downward
    correction), nothing is republished -- a negative submitted_amount_usd
    would violate schemas/run_state.schema's non-negative constraint and
    would also be silently discarded outright by
    load_prior_same_day_allocations's own `amount <= 0: continue` guard
    (Codex review: "Represent negative reconciliation deltas outside order
    amounts"). The existing provisional record stands as a deliberately
    conservative, upper-bound estimate instead.
    """
    from test_near_close_dca import _write_scheduled_afternoon_artifact

    monkeypatch.setenv("DCA_CRON_TRACKING_ROOT", str(tmp_path / "cron_tracking"))
    ledger = WholeShareAccumulationLedger()
    claim_trading_day = date(2026, 8, 15)
    ledger.record_contribution(
        "BYDDY", claim_trading_day.isoformat(), 10.0, 10.0, stream="afternoon",
        idempotency_key="afternoon-BYDDY", recorded_at="2026-08-15T15:30:00-04:00",
    )

    history_root = str(tmp_path / "runs")
    _write_scheduled_afternoon_artifact(
        history_root,
        date(2026, 8, 16),
        [{
            "ticker": "BYDDY", "status": "submitted", "order_id": "RH-DELTA-BELOW-1",
            "planned_amount_usd": 10.0,
            "whole_share_claim_breakdown": [{"trading_day": "2026-08-15", "amount_usd": 10.0}],
        }],
    )

    class _FilledBelowCachedPriceBroker(DryRunBroker):
        def same_day_strategy_order_exists_with_ref_id_prefix(self, ticker, trading_day, ref_id_prefixes):
            # Filled at $6.00/share, BELOW the cached $10.00 already recorded.
            return {"id": "RH-DELTA-BELOW-1", "state": "filled", "quantity": "1", "average_price": "6.00"}

    config = StrategyConfig(whole_share_tickers=frozenset({"BYDDY"}), dry_run=False)
    ctx = ProcessCandidateContext(
        config=config,
        state=StrategyState(),
        broker=_FilledBelowCachedPriceBroker(cash=500.0, reference_prices={"BYDDY": 10.0}),
        robinhood_data=None,
        now=datetime(2026, 8, 17, 15, 45, tzinfo=EASTERN),
        trading_day=date(2026, 8, 17),
        guard_active=False,
        prior_same_day_allocations={},
        scheduled=True,
        run_result=None,
        deployment_history_root=history_root,
    )
    _near_close_dca._IN_FLIGHT_CANDIDATE_OUTCOMES["outcomes"] = {}

    deployed = _reconcile_cross_day_whole_share_claims_for_batch(ctx, 0.0)

    # The ledger settles for real, at the true (lower) fill amount...
    assert ledger.unresolved_claim("BYDDY", "2026-08-15", "afternoon-BYDDY") is None
    # ...but nothing is republished for the downward correction.
    assert deployed == 0.0
    assert "BYDDY" not in _near_close_dca._IN_FLIGHT_CANDIDATE_OUTCOMES["outcomes"]


def test_a_terminally_filled_recovery_uses_its_own_breakdown_entry_not_the_aggregate_row_total(
    tmp_path, monkeypatch
):
    """Codex review ("Associate provisional notionals with individual
    order IDs"): the recorded provisional row can be an AGGREGATE of
    several separate orders' notionals merged by
    _merge_whole_share_submission_outcomes (e.g. two different claim days'
    orders both still live, recovered in the same pre-roll pass and
    published as one $20 row with two order ids). Treating that row's
    whole $20 total as the provisional amount of just the ONE order now
    settling would misattribute the OTHER, still-live order's $10 into
    the delta computation, corrupting deployment accounting for a
    completely separate, unrelated order.
    """
    from test_near_close_dca import _write_scheduled_afternoon_artifact

    monkeypatch.setenv("DCA_CRON_TRACKING_ROOT", str(tmp_path / "cron_tracking"))
    ledger = WholeShareAccumulationLedger()
    ledger.record_contribution(
        "BYDDY", "2026-08-14", 10.0, 10.0, stream="afternoon",
        idempotency_key="afternoon-BYDDY", recorded_at="2026-08-14T15:30:00-04:00",
    )
    ledger.record_contribution(
        "BYDDY", "2026-08-15", 10.0, 10.0, stream="afternoon",
        idempotency_key="afternoon-BYDDY", recorded_at="2026-08-15T15:30:00-04:00",
    )

    # An earlier run aggregated BOTH still-live orders into one $20 row
    # with two order ids and two separate breakdown entries.
    history_root = str(tmp_path / "runs")
    _write_scheduled_afternoon_artifact(
        history_root,
        date(2026, 8, 16),
        [{
            "ticker": "BYDDY", "status": "submitted",
            "order_id": "RH-CLAIM-15",
            "whole_share_order_ids": ["RH-CLAIM-14", "RH-CLAIM-15"],
            "planned_amount_usd": 20.0,
            "whole_share_claim_breakdown": [
                {"trading_day": "2026-08-14", "amount_usd": 10.0},
                {"trading_day": "2026-08-15", "amount_usd": 10.0},
            ],
        }],
    )

    class _OnlyOneNowFilledBroker(DryRunBroker):
        def same_day_strategy_order_exists_with_ref_id_prefix(self, ticker, trading_day, ref_id_prefixes):
            if ref_id_prefixes == ("graded-dca-wholeshare-2026-08-14-BYDDY",):
                # Still live -- unchanged from the earlier recovery.
                return {"id": "RH-CLAIM-14", "state": "confirmed", "cumulative_quantity": "0"}
            if ref_id_prefixes == ("graded-dca-wholeshare-2026-08-15-BYDDY",):
                # NOW filled, at the SAME $10.00 price already recorded.
                return {"id": "RH-CLAIM-15", "state": "filled", "quantity": "1", "average_price": "10.00"}
            return None

    config = StrategyConfig(whole_share_tickers=frozenset({"BYDDY"}), dry_run=False)
    ctx = ProcessCandidateContext(
        config=config,
        state=StrategyState(),
        broker=_OnlyOneNowFilledBroker(cash=500.0, reference_prices={"BYDDY": 10.0}),
        robinhood_data=None,
        now=datetime(2026, 8, 17, 15, 45, tzinfo=EASTERN),
        trading_day=date(2026, 8, 17),
        guard_active=False,
        prior_same_day_allocations={},
        scheduled=True,
        run_result=None,
        deployment_history_root=history_root,
    )
    _near_close_dca._IN_FLIGHT_CANDIDATE_OUTCOMES["outcomes"] = {}

    deployed = _reconcile_cross_day_whole_share_claims_for_batch(ctx, 0.0)

    # The 08-15 claim settled for real, at exactly the amount already
    # recorded for IT specifically ($10) -- zero delta, no adjustment
    # published, and critically NOT a -$10 adjustment from misreading the
    # aggregate row's $20 total as this one order's own provisional amount.
    assert ledger.unresolved_claim("BYDDY", "2026-08-15", "afternoon-BYDDY") is None
    assert deployed == 0.0
    assert "BYDDY" not in _near_close_dca._IN_FLIGHT_CANDIDATE_OUTCOMES["outcomes"]
    # The OTHER (08-14) claim is untouched -- still live, still unresolved.
    assert ledger.unresolved_claim("BYDDY", "2026-08-14", "afternoon-BYDDY") is not None


def test_reconcile_unresolved_whole_share_claim_does_not_book_a_zero_fill_attempt_when_release_fails():
    """Codex review ("Book zero-fill attempts only after releasing the
    claim"): when ledger.preserve_pending() itself raises while releasing
    a confirmed zero-fill order's claim, the claim is STILL unresolved --
    returning the zero_fill_attempt result anyway would tell the caller to
    book a real attempt (and publish this order id) even though nothing
    was actually released, and a LATER reconciliation pass encountering
    the same still-unresolved claim would then record the identical
    attempt again. Must return None instead, exactly like a failed lookup
    elsewhere in this function.
    """
    ledger = WholeShareAccumulationLedger()
    ledger.record_contribution(
        "BYDDY", "2026-08-15", 10.0, 10.0, stream="afternoon",
        idempotency_key="afternoon-BYDDY", recorded_at="2026-08-15T15:30:00-04:00",
    )
    unresolved = ledger.unresolved_claim("BYDDY", "2026-08-15", "afternoon-BYDDY")

    def _raising_preserve_pending(*args, **kwargs):
        raise RuntimeError("ledger write failed")

    ledger.preserve_pending_if_claimed = _raising_preserve_pending

    class _ZeroFillBroker:
        def account_number(self) -> str:
            return "test-account"

        def same_day_strategy_order_exists_with_ref_id_prefix(self, ticker, trading_day, ref_id_prefixes):
            return None

        def same_day_order_with_ref_id_prefix_any_state(self, ticker, trading_day, ref_id_prefixes):
            return {"id": "RH-ZERO-RELEASE-FAILS", "state": "rejected", "cumulative_quantity": "0"}

    assert unresolved is not None
    reconciled = _near_close_dca._reconcile_unresolved_whole_share_claim(
        ledger, _ZeroFillBroker(), "BYDDY", "afternoon", "afternoon-BYDDY", "2026-08-15", unresolved
    )

    assert reconciled is None
    # Claim remains exactly as reserved as before -- the release failed.
    assert ledger.unresolved_claim("BYDDY", "2026-08-15", "afternoon-BYDDY") is not None


def test_merge_whole_share_submission_outcomes_unions_ids_from_both_pre_aggregated_sides():
    """Codex review ("Merge every ID from pre-aggregated outcomes"): ``new``
    can itself already be a previously-aggregated outcome carrying its own
    whole_share_order_ids list (e.g. the outer batch loop merging a seeded
    pre-roll recovery with an already mid-call-merged recovery). Consuming
    only new["order_id"] silently drops every id but the newest from that
    list -- since reconcile_foreground_orders validates exactly the ids
    present here, the dropped broker order would never be reconciled.
    """
    existing = {
        "status": "submitted",
        "order_id": "RH-A",
        "submitted_amount_usd": 10.0,
        "whole_share_quantity": 1,
    }
    new = {
        "status": "submitted",
        "order_id": "RH-C",
        "whole_share_order_ids": ["RH-B", "RH-C"],
        "submitted_amount_usd": 20.0,
        "whole_share_quantity": 2,
    }

    merged = _near_close_dca._merge_whole_share_submission_outcomes(existing, new)

    assert set(merged["whole_share_order_ids"]) == {"RH-A", "RH-B", "RH-C"}
    assert merged["submitted_amount_usd"] == pytest.approx(30.0)
    assert merged["whole_share_quantity"] == 3


def test_merge_whole_share_submission_outcomes_preserves_settlement_state():
    """Codex review ("Preserve settlement state when aggregating
    outcomes"): ``merged = dict(new)`` keeps only NEW's own settlement
    fields -- if ``existing`` is a recovered live order still awaiting
    ledger settlement (whole_share_settlement_reconciliation_needed=True,
    with a nonzero whole_share_settlement_shortfall_usd from a clamped
    fire whose settle() call itself failed) and this ticker then submits
    a fresh, cleanly-settled order in the same run, the fresh outcome's
    own False/0.0 settlement fields must not silently overwrite and
    discard the unresolved recovery's marker and outstanding shortfall.
    """
    existing = {
        "status": "submitted",
        "order_id": "RH-A",
        "submitted_amount_usd": 10.0,
        "whole_share_quantity": 1,
        "whole_share_settlement_reconciliation_needed": True,
        "whole_share_settlement_shortfall_usd": 4.0,
    }
    new = {
        "status": "submitted",
        "order_id": "RH-B",
        "submitted_amount_usd": 20.0,
        "whole_share_quantity": 2,
        "whole_share_settlement_reconciliation_needed": False,
        "whole_share_settlement_shortfall_usd": 0.0,
    }

    merged = _near_close_dca._merge_whole_share_submission_outcomes(existing, new)

    assert merged["whole_share_settlement_reconciliation_needed"] is True
    assert merged["whole_share_settlement_shortfall_usd"] == pytest.approx(4.0)


def test_batch_keeps_submission_published_during_candidate_processing(monkeypatch):
    """A late reconciliation publication must beat a returned accumulating outcome."""
    config = StrategyConfig(whole_share_tickers=frozenset({"BYDDY"}), dry_run=False)
    ctx = _ctx(config, DryRunBroker(cash=500.0, reference_prices={"BYDDY": 10.0}))
    _near_close_dca._IN_FLIGHT_CANDIDATE_OUTCOMES["outcomes"] = {}

    def _publish_recovery_then_return_accumulating(*, decision, ctx, running_deployed_7d):
        _near_close_dca._publish_in_flight_outcome(
            decision.ticker,
            {
                "status": "submitted",
                "order_id": "RH-LATE-RECOVERY",
                "submitted_amount_usd": 10.0,
                "whole_share_quantity": 1,
            },
        )
        return ({"status": "accumulating", "whole_share_pending_usd": 2.0}, None, running_deployed_7d, 0, 0)

    monkeypatch.setattr(_near_close_dca, "_process_buy_candidate", _publish_recovery_then_return_accumulating)
    monkeypatch.setattr(
        _near_close_dca,
        "_reconcile_cross_day_whole_share_claims_for_batch",
        lambda ctx, running_deployed_7d, decisions_by_ticker=None: running_deployed_7d,
    )

    outcomes, _, _, _, _ = _process_buy_candidates_batch([_decision()], ctx)

    assert outcomes["BYDDY"]["status"] == "submitted"
    assert outcomes["BYDDY"]["order_id"] == "RH-LATE-RECOVERY"


def test_batch_pre_pass_aggregates_two_recovered_claims_for_the_same_ticker(tmp_path, monkeypatch):
    """Codex review ("Aggregate all recovered claims for the same ticker"):
    _reconcile_cross_day_whole_share_claims's own per-ticker loop can settle
    MULTIPLE separate unresolved claims for the SAME ticker in one call
    (e.g. two different earlier trading days each left one). Each settled
    claim calls _book_reconciled_whole_share_order independently -- a plain
    overwrite of the published outcome left only the LAST claim's notional/
    quantity visible to _build_buy_candidates, silently dropping the first
    from dca_risk.deployed_usd()'s next-run kill-switch seeding even though
    BOTH claims genuinely settled in the ledger.
    """
    monkeypatch.setenv("DCA_CRON_TRACKING_ROOT", str(tmp_path / "cron_tracking"))
    ledger = WholeShareAccumulationLedger()
    ledger.record_contribution(
        "BYDDY", "2026-08-14", 10.0, 10.0, stream="afternoon",
        idempotency_key="afternoon-BYDDY", recorded_at="2026-08-14T15:30:00-04:00",
    )
    ledger.record_contribution(
        "BYDDY", "2026-08-15", 10.0, 10.0, stream="afternoon",
        idempotency_key="afternoon-BYDDY", recorded_at="2026-08-15T15:30:00-04:00",
    )
    assert len(ledger.unresolved_claims_any_day("BYDDY", "afternoon-BYDDY")) == 2

    config = StrategyConfig(whole_share_tickers=frozenset({"BYDDY"}), dry_run=False)

    class _TwoClaimsConfirmingBroker(DryRunBroker):
        def same_day_strategy_order_exists(self, ticker, trading_day):
            return False

        def same_day_strategy_order_exists_with_ref_id_prefix(self, ticker, trading_day, ref_id_prefixes):
            if ref_id_prefixes == ("graded-dca-wholeshare-2026-08-14-BYDDY",):
                return {"id": "RH-CLAIM-14", "state": "filled", "quantity": "1"}
            if ref_id_prefixes == ("graded-dca-wholeshare-2026-08-15-BYDDY",):
                return {"id": "RH-CLAIM-15", "state": "filled", "quantity": "1"}
            return None

    broker = _TwoClaimsConfirmingBroker(cash=500.0, reference_prices={"BYDDY": 10.0})
    ctx = _ctx(config, broker)
    _near_close_dca._IN_FLIGHT_CANDIDATE_OUTCOMES["outcomes"] = {}

    running_deployed_7d = _reconcile_cross_day_whole_share_claims_for_batch(ctx, running_deployed_7d=0.0)

    assert ledger.shares_purchased_total("BYDDY") == 2
    assert running_deployed_7d == pytest.approx(20.0)

    outcome = _near_close_dca._IN_FLIGHT_CANDIDATE_OUTCOMES["outcomes"]["BYDDY"]
    assert outcome["status"] == "submitted"
    assert outcome["submitted_amount_usd"] == pytest.approx(20.0)
    assert outcome["whole_share_quantity"] == 2
    assert set(outcome["whole_share_order_ids"]) == {"RH-CLAIM-14", "RH-CLAIM-15"}


def test_batch_pre_pass_reconciles_cross_day_claims_before_any_candidate_places(tmp_path, monkeypatch):
    """Codex review ("Reconcile recovered spend before starting the
    candidate batch"): cross-day reconciliation used to run only once the
    whole-share ticker itself reached _process_whole_share_candidate in
    the sorted batch loop -- a recovered order that pushes deployment
    across the kill-switch threshold was then invisible to any candidate
    sorted BEFORE the whole-share ticker. _reconcile_cross_day_whole_share_
    claims_for_batch runs this reconciliation as a pre-pass over every
    whole-share ticker in buy_candidates, so the returned running_deployed_7d
    already reflects recovered spend before the main batch loop starts,
    regardless of sort order.
    """
    monkeypatch.setenv("DCA_CRON_TRACKING_ROOT", str(tmp_path / "cron_tracking"))
    ledger = WholeShareAccumulationLedger()
    claim_trading_day = date(2026, 8, 15)
    ledger.record_contribution(
        "BYDDY", claim_trading_day.isoformat(), 10.0, 10.0, stream="afternoon",
        idempotency_key="afternoon-BYDDY", recorded_at="2026-08-15T15:30:00-04:00",
    )
    assert len(ledger.unresolved_claims_any_day("BYDDY", "afternoon-BYDDY")) == 1

    config = StrategyConfig(whole_share_tickers=frozenset({"BYDDY"}), dry_run=False)

    class _CrossDayConfirmingBroker(DryRunBroker):
        def same_day_strategy_order_exists(self, ticker, trading_day):
            return False

        def same_day_strategy_order_exists_with_ref_id_prefix(self, ticker, trading_day, ref_id_prefixes):
            if ref_id_prefixes == ("graded-dca-wholeshare-2026-08-15-BYDDY",):
                return {"id": "RH-PREPASS-1", "state": "filled", "quantity": "1"}
            return None

    broker = _CrossDayConfirmingBroker(cash=500.0, reference_prices={"BYDDY": 10.0})
    ctx = _ctx(config, broker)

    # A non-whole-share candidate sorted BEFORE BYDDY in the batch --
    # the pre-pass must still reconcile BYDDY's cross-day claim first, so
    # running_deployed_7d reflects the recovered $10 spend by the time ANY
    # candidate (including this earlier-sorted one) is processed. The
    # pre-pass now scans ctx.config.whole_share_tickers directly rather
    # than buy_candidates, so it reconciles BYDDY even without an explicit
    # decision for it in decisions_by_ticker.
    other_decision = _decision(action=Action.BUY, suggested_amount_usd=3.0, target_price=10.0)
    other_decision = replace(other_decision, ticker="AAPL")

    running_deployed_7d = _reconcile_cross_day_whole_share_claims_for_batch(
        ctx, running_deployed_7d=0.0, decisions_by_ticker={"AAPL": other_decision}
    )

    # The cross-day claim is settled and its $10 notional is already
    # reflected in the returned figure, BEFORE any candidate has placed.
    assert ledger.shares_purchased_total("BYDDY") == 1
    assert ledger.unresolved_claims_any_day("BYDDY", "afternoon-BYDDY") == []
    assert running_deployed_7d == 10.0


def test_batch_pre_pass_reconciles_a_ticker_removed_from_whole_share_config(tmp_path, monkeypatch):
    """Codex review ("Reconcile claims for tickers removed from
    configuration"): an operator can remove a ticker from
    config.whole_share_tickers entirely (delisted, or moved to
    excluded_tickers) while a real unresolved claim is still pending. The
    pre-pass previously only iterated the CURRENT config's own ticker
    set, so such a claim could never be reconciled again by any later
    run, permanently stranding its accumulated balance. The pre-pass now
    also discovers any ticker with an existing ledger directory that
    isn't in the current whole_share_tickers set at all.
    """
    monkeypatch.setenv("DCA_CRON_TRACKING_ROOT", str(tmp_path / "cron_tracking"))
    ledger = WholeShareAccumulationLedger()
    claim_trading_day = date(2026, 8, 15)
    ledger.record_contribution(
        "BYDDY", claim_trading_day.isoformat(), 10.0, 10.0, stream="afternoon",
        idempotency_key="afternoon-BYDDY", recorded_at="2026-08-15T15:30:00-04:00",
    )

    # BYDDY is no longer enrolled in whole_share_tickers at all -- an
    # operator removed it (e.g. delisted, or moved to excluded_tickers).
    config = StrategyConfig(whole_share_tickers=frozenset(), dry_run=False)

    class _OrphanedTickerConfirmingBroker(DryRunBroker):
        def same_day_strategy_order_exists_with_ref_id_prefix(self, ticker, trading_day, ref_id_prefixes):
            if ref_id_prefixes == ("graded-dca-wholeshare-2026-08-15-BYDDY",):
                return {"id": "RH-ORPHANED-1", "state": "filled", "quantity": "1"}
            return None

    broker = _OrphanedTickerConfirmingBroker(cash=500.0, reference_prices={"BYDDY": 10.0})
    ctx = _ctx(config, broker)

    running_deployed_7d = _reconcile_cross_day_whole_share_claims_for_batch(ctx, running_deployed_7d=0.0)

    assert ledger.shares_purchased_total("BYDDY") == 1
    assert ledger.unresolved_claims_any_day("BYDDY", "afternoon-BYDDY") == []
    assert running_deployed_7d == pytest.approx(10.0)


def test_batch_pre_pass_reconciles_a_whole_share_ticker_absent_from_buy_candidates(tmp_path, monkeypatch):
    """Codex review ("Scan unresolved tickers independently of buy
    candidates"): a whole-share ticker with a genuine cross-day unresolved
    claim can be ABSENT from buy_candidates entirely -- e.g. today's own
    NO_ACTION redispatch quotes are missing or disagree, so
    _whole_share_redispatch_reconciled_price() returns None and the ticker
    never gets spliced into decisions/buy_candidates at all. Reconciling
    the STORED claim against broker history needs no current quote, so the
    pre-pass must still recover it even with no decisions_by_ticker entry
    for BYDDY whatsoever.
    """
    monkeypatch.setenv("DCA_CRON_TRACKING_ROOT", str(tmp_path / "cron_tracking"))
    ledger = WholeShareAccumulationLedger()
    claim_trading_day = date(2026, 8, 15)
    ledger.record_contribution(
        "BYDDY", claim_trading_day.isoformat(), 10.0, 10.0, stream="afternoon",
        idempotency_key="afternoon-BYDDY", recorded_at="2026-08-15T15:30:00-04:00",
    )

    config = StrategyConfig(whole_share_tickers=frozenset({"BYDDY"}), dry_run=False)

    class _CrossDayConfirmingBroker(DryRunBroker):
        def same_day_strategy_order_exists(self, ticker, trading_day):
            return False

        def same_day_strategy_order_exists_with_ref_id_prefix(self, ticker, trading_day, ref_id_prefixes):
            if ref_id_prefixes == ("graded-dca-wholeshare-2026-08-15-BYDDY",):
                return {"id": "RH-NO-DECISION-1", "state": "filled", "quantity": "1"}
            return None

    broker = _CrossDayConfirmingBroker(cash=500.0, reference_prices={"BYDDY": 10.0})
    ctx = _ctx(config, broker)

    # No decisions_by_ticker entry for BYDDY at all -- the pre-pass must
    # still find and reconcile it via a synthetic decision.
    running_deployed_7d = _reconcile_cross_day_whole_share_claims_for_batch(
        ctx, running_deployed_7d=0.0
    )

    assert ledger.shares_purchased_total("BYDDY") == 1
    assert running_deployed_7d == 10.0


def test_batch_pre_pass_logs_execution_against_the_reconciled_price_not_zero(tmp_path, monkeypatch):
    """Codex review ("Supply a real target for recovered execution
    records"): the batch pre-pass's synthetic NO_ACTION decision (built for
    a ticker with no real decisions_by_ticker entry) leaves target_price at
    its dataclass default of 0.0. build_execution_record() computes
    slippage as avg_fill_price - target_price -- with target_price=0.0 the
    ENTIRE fill price reads as adverse slippage and the entire notional as
    slippage cost, misreporting a normal reconciled fill as catastrophic
    execution quality. The reconciled order's own booked price-per-share
    must be used as the target instead.
    """
    monkeypatch.setenv("DCA_CRON_TRACKING_ROOT", str(tmp_path / "cron_tracking"))
    ledger = WholeShareAccumulationLedger()
    claim_trading_day = date(2026, 8, 15)
    ledger.record_contribution(
        "BYDDY", claim_trading_day.isoformat(), 10.0, 10.0, stream="afternoon",
        idempotency_key="afternoon-BYDDY", recorded_at="2026-08-15T15:30:00-04:00",
    )

    config = StrategyConfig(whole_share_tickers=frozenset({"BYDDY"}), dry_run=False)

    class _CrossDayConfirmingBroker(DryRunBroker):
        def same_day_strategy_order_exists(self, ticker, trading_day):
            return False

        def same_day_strategy_order_exists_with_ref_id_prefix(self, ticker, trading_day, ref_id_prefixes):
            if ref_id_prefixes == ("graded-dca-wholeshare-2026-08-15-BYDDY",):
                return {"id": "RH-EXEC-LOG-1", "state": "filled", "quantity": "1"}
            return None

        def get_order_fill(self, order_id):
            if order_id == "RH-EXEC-LOG-1":
                return OrderFill(avg_fill_price=10.0, quantity_filled=1.0)
            return None

    broker = _CrossDayConfirmingBroker(cash=500.0, reference_prices={"BYDDY": 10.0})
    run_result = RunResult(trading_day=date(2026, 8, 16), decisions=[])
    ctx = _ctx(config, broker, run_result=run_result)

    # No decisions_by_ticker entry for BYDDY -- forces the synthetic decision.
    running_deployed_7d = _reconcile_cross_day_whole_share_claims_for_batch(
        ctx, running_deployed_7d=0.0
    )

    assert ledger.shares_purchased_total("BYDDY") == 1
    assert running_deployed_7d == 10.0
    assert len(run_result.execution_records) == 1
    record = run_result.execution_records[0]
    # The booked price ($10/share) was used as the target, not 0.0 -- fill
    # price ($10.00) exactly matches it, so slippage is zero, not the
    # entire $10 notional.
    assert record.slippage_abs == 0.0
    assert record.slippage_cost == 0.0


def test_batch_pre_pass_reconciles_a_same_day_claim_when_an_order_already_exists(tmp_path, monkeypatch):
    """Codex review ("Include same-day claims in the batch pre-pass"): on a
    same-day retry after an ambiguous placement, an unresolved claim for
    TODAY's own trading day can already have a real broker order behind
    it. The pre-pass must reconcile that recovered fill too -- gated on
    the SAME same_day_strategy_order_exists signal the initial
    duplicate-check branch itself uses -- so candidates sorted before this
    ticker still see the recovered spend, not just candidates sorted
    after it once _process_whole_share_candidate itself runs.
    """
    monkeypatch.setenv("DCA_CRON_TRACKING_ROOT", str(tmp_path / "cron_tracking"))
    ledger = WholeShareAccumulationLedger()
    today = date(2026, 8, 16)
    ledger.record_contribution(
        "BYDDY", today.isoformat(), 10.0, 10.0, stream="afternoon",
        idempotency_key="afternoon-BYDDY", recorded_at="2026-08-16T15:30:00-04:00",
    )

    config = StrategyConfig(whole_share_tickers=frozenset({"BYDDY"}), dry_run=False)

    class _SameDayOrderExistsBroker(DryRunBroker):
        def same_day_strategy_order_exists(self, ticker, trading_day):
            return True  # an order already exists today -- an ambiguous retry's own

        def same_day_strategy_order_exists_with_ref_id_prefix(self, ticker, trading_day, ref_id_prefixes):
            if ref_id_prefixes == ("graded-dca-wholeshare-2026-08-16-BYDDY",):
                return {"id": "RH-SAME-DAY-1", "state": "filled", "quantity": "1"}
            return None

    broker = _SameDayOrderExistsBroker(cash=500.0, reference_prices={"BYDDY": 10.0})
    ctx = _ctx(config, broker)

    running_deployed_7d = _reconcile_cross_day_whole_share_claims_for_batch(
        ctx, running_deployed_7d=0.0
    )

    assert ledger.shares_purchased_total("BYDDY") == 1
    assert running_deployed_7d == 10.0


def test_batch_pre_pass_does_not_release_a_same_day_stale_claim_with_no_placement_attempt(tmp_path, monkeypatch):
    """Companion to the test above: a same-day claim that is merely STALE,
    with NO placement ever attempted (same_day_strategy_order_exists is
    False), must NOT be reconciled via this function's general
    staleness-release fallback -- record_contribution()'s own
    reclaim-and-reprice logic is what owns that scenario, and releasing it
    here via preserve_pending would flatten a reclaimable, repriceable
    fire into a plain released balance, discarding that behavior.
    """
    monkeypatch.setenv("DCA_CRON_TRACKING_ROOT", str(tmp_path / "cron_tracking"))
    ledger = WholeShareAccumulationLedger()
    today = date(2026, 8, 16)
    ledger.record_contribution(
        "BYDDY", today.isoformat(), 10.0, 10.0, stream="afternoon",
        idempotency_key="afternoon-BYDDY", recorded_at="2026-08-16T15:00:00-04:00",  # stale by _ctx()'s now
    )
    assert ledger.pending_usd("BYDDY") == 0.0  # deducted

    config = StrategyConfig(whole_share_tickers=frozenset({"BYDDY"}), dry_run=False)

    class _NoOrderAttemptedBroker(DryRunBroker):
        def same_day_strategy_order_exists(self, ticker, trading_day):
            return False  # nothing has ever been placed today

    broker = _NoOrderAttemptedBroker(cash=500.0, reference_prices={"BYDDY": 10.0})
    ctx = _ctx(config, broker)

    _reconcile_cross_day_whole_share_claims_for_batch(ctx, running_deployed_7d=0.0)

    # Left completely untouched -- not released via preserve_pending.
    assert ledger.pending_usd("BYDDY") == 0.0
    assert ledger.unresolved_claim("BYDDY", "2026-08-16", "afternoon-BYDDY") is not None


def test_batch_pre_pass_reconciles_a_same_day_terminal_partial_fill_before_reclaim(tmp_path, monkeypatch):
    """Codex review ("Reconcile terminal same-day fills before reclaiming
    the claim"): the broad same_day_strategy_order_exists check the batch
    pre-pass gates on is LIVE-only, so it misses a genuinely TERMINAL order
    (a partial fill that was THEN cancelled) an earlier placement attempt
    today left behind. Without also checking the exact ref-id
    terminal-inclusive lookup, this same-day claim falls straight through
    to the "nothing attempted" branch and is left for
    record_contribution()'s own reclaim-and-reprice logic -- which, once
    the claim goes stale, would reclaim and submit the FULL planned
    quantity again, never booking the earlier partial fill.
    """
    monkeypatch.setenv("DCA_CRON_TRACKING_ROOT", str(tmp_path / "cron_tracking"))
    ledger = WholeShareAccumulationLedger()
    today = date(2026, 8, 16)
    ledger.record_contribution(
        "BYDDY", today.isoformat(), 20.0, 10.0, stream="afternoon",
        idempotency_key="afternoon-BYDDY", recorded_at="2026-08-16T15:30:00-04:00",
    )

    config = StrategyConfig(whole_share_tickers=frozenset({"BYDDY"}), dry_run=False)

    class _SameDayTerminalPartialFillBroker(DryRunBroker):
        def same_day_strategy_order_exists(self, ticker, trading_day):
            return False  # the earlier attempt's order already cancelled -- not live

        def same_day_strategy_order_exists_with_ref_id_prefix(self, ticker, trading_day, ref_id_prefixes):
            return None  # terminal orders are excluded from the live-only lookup

        def same_day_order_with_ref_id_prefix_any_state(self, ticker, trading_day, ref_id_prefixes):
            if ref_id_prefixes == ("graded-dca-wholeshare-2026-08-16-BYDDY",):
                return {"id": "RH-SAME-DAY-TERMINAL-1", "state": "cancelled", "cumulative_quantity": "1"}
            return None

    broker = _SameDayTerminalPartialFillBroker(cash=500.0, reference_prices={"BYDDY": 10.0})
    ctx = _ctx(config, broker)

    running_deployed_7d = _reconcile_cross_day_whole_share_claims_for_batch(ctx, running_deployed_7d=0.0)

    # The 1 filled share is settled for real -- not left for a stale reclaim
    # to resubmit the full 2-share planned quantity.
    assert ledger.shares_purchased_total("BYDDY") == 1
    assert ledger.unresolved_claim("BYDDY", "2026-08-16", "afternoon-BYDDY") is None
    assert running_deployed_7d == 10.0


def test_batch_pre_pass_counts_a_confirmed_zero_fill_toward_the_attempt_cap(tmp_path, monkeypatch):
    """Codex review ("Count pre-roll zero-fill orders toward the attempt
    cap"): the cross-day/pre-roll reconciliation path releases a claim
    whose terminal order confirms an EXPLICIT zero fill, but that is still
    a REAL broker attempt -- config.max_orders_per_day counts a cancelled/
    rejected order the same as any other. The old behavior returned None
    from _reconcile_unresolved_whole_share_claim for this case, so the
    caller never called record_buy() and the attempt was invisible to the
    daily ceiling, matching the immediate placement-error sibling's own
    already-fixed bug.
    """
    monkeypatch.setenv("DCA_CRON_TRACKING_ROOT", str(tmp_path / "cron_tracking"))
    ledger = WholeShareAccumulationLedger()
    claim_trading_day = date(2026, 8, 15)
    ledger.record_contribution(
        "BYDDY", claim_trading_day.isoformat(), 10.0, 10.0, stream="afternoon",
        idempotency_key="afternoon-BYDDY", recorded_at="2026-08-15T15:30:00-04:00",
    )

    config = StrategyConfig(whole_share_tickers=frozenset({"BYDDY"}), dry_run=False)

    class _CrossDayZeroFillBroker(DryRunBroker):
        def same_day_strategy_order_exists(self, ticker, trading_day):
            return False

        def same_day_strategy_order_exists_with_ref_id_prefix(self, ticker, trading_day, ref_id_prefixes):
            return None

        def same_day_order_with_ref_id_prefix_any_state(self, ticker, trading_day, ref_id_prefixes):
            if ref_id_prefixes == ("graded-dca-wholeshare-2026-08-15-BYDDY",):
                return {"id": "RH-CROSS-DAY-ZERO-FILL-1", "state": "rejected", "cumulative_quantity": "0"}
            return None

    broker = _CrossDayZeroFillBroker(cash=500.0, reference_prices={"BYDDY": 10.0})
    ctx = _ctx(config, broker)
    import near_close_dca as _n

    _n._IN_FLIGHT_CANDIDATE_OUTCOMES["outcomes"] = {}
    running_deployed_7d = _reconcile_cross_day_whole_share_claims_for_batch(ctx, running_deployed_7d=0.0)

    assert running_deployed_7d == 0.0  # nothing purchased
    assert ledger.shares_purchased_total("BYDDY") == 0
    assert ledger.unresolved_claim("BYDDY", claim_trading_day.isoformat(), "afternoon-BYDDY") is None
    # The confirmed attempt is counted, even though nothing was spent.
    assert ctx.state.day_order_count(claim_trading_day) == 1
    assert ctx.state.day_notional(claim_trading_day) == 0.0
    # Codex review ("Keep recovered zero-fill attempts on the claim day"):
    # the published outcome must carry a machine-readable
    # whole_share_claim_breakdown pointing at the CLAIM's own trading day
    # (2026-08-15), not just prose inside skip_reason -- otherwise a later
    # run reloading this artifact would misattribute the attempt to ITS
    # OWN trading day instead of excluding it as an already-counted
    # cross-day recovery.
    published = _n._IN_FLIGHT_CANDIDATE_OUTCOMES["outcomes"]["BYDDY"]
    assert published["whole_share_claim_breakdown"] == [
        {"trading_day": "2026-08-15", "amount_usd": 0.0}
    ]


def test_per_candidate_reconciliation_recovery_survives_todays_own_non_submission(tmp_path, monkeypatch):
    """Codex review ("Merge recoveries published during candidate
    processing"): the batch pre-pass's own lookup can miss a claim that
    only becomes visible to the broker's eventually-consistent order
    history a moment later -- by the time
    _process_whole_share_candidate's own per-candidate safety-net
    reconciliation (run at the very start of THAT function, per-candidate,
    inside the batch loop) retries the SAME lookup, it succeeds and
    publishes a "submitted" outcome for the recovered claim. If TODAY's own
    signal for the SAME ticker doesn't reach the whole-share firing
    threshold (e.g. still accumulating), that return path never re-touches
    candidate_outcomes -- so the recovery survives internally -- but the
    OUTER batch loop's own pre-call `seeded_outcome` snapshot (captured
    BEFORE _process_buy_candidate even started, hence before the pre-pass's
    retry inside it) never saw it either, and unconditionally overwriting
    candidate_outcomes[ticker] with today's own "skipped" c_outcome would
    silently erase the recovered fill.
    """
    monkeypatch.setenv("DCA_CRON_TRACKING_ROOT", str(tmp_path / "cron_tracking"))
    ledger = WholeShareAccumulationLedger()
    claim_trading_day = date(2026, 8, 15)
    ledger.record_contribution(
        "BYDDY", claim_trading_day.isoformat(), 10.0, 10.0, stream="afternoon",
        idempotency_key="afternoon-BYDDY", recorded_at="2026-08-15T15:30:00-04:00",
    )

    config = StrategyConfig(whole_share_tickers=frozenset({"BYDDY"}), dry_run=False)
    lookup_calls = {"count": 0}

    class _EventuallyConsistentBroker(DryRunBroker):
        def same_day_strategy_order_exists(self, ticker, trading_day):
            return False

        def same_day_strategy_order_exists_with_ref_id_prefix(self, ticker, trading_day, ref_id_prefixes):
            if ref_id_prefixes != ("graded-dca-wholeshare-2026-08-15-BYDDY",):
                return None
            lookup_calls["count"] += 1
            # The batch pre-pass's own call (the first one) misses -- the
            # broker's order-history endpoint has not indexed the order
            # yet. The per-candidate safety-net's own retry (the second
            # call, moments later inside the same batch) succeeds.
            if lookup_calls["count"] == 1:
                return None
            return {"id": "RH-EVENTUALLY-CONSISTENT-1", "state": "filled", "quantity": "1"}

    broker = _EventuallyConsistentBroker(cash=500.0, reference_prices={"BYDDY": 10.0})
    # Today's own contribution ($3 at $10/share) is well below the firing
    # threshold, so this candidate's own outcome is a non-submission
    # ("skipped: still accumulating").
    decision = _decision(suggested_amount_usd=3.0, target_price=10.0)
    # _process_buy_candidates_batch's own candidate_outcomes seeds itself
    # from whatever _IN_FLIGHT_CANDIDATE_OUTCOMES["outcomes"] already holds
    # (to carry forward a real pre-roll pass's own recoveries) -- reset it
    # so a leftover entry from an unrelated earlier test can't be picked up
    # as this test's own seeded_outcome.
    _near_close_dca._IN_FLIGHT_CANDIDATE_OUTCOMES["outcomes"] = {}

    candidate_outcomes, _, running_deployed_7d, errors, _ = _process_buy_candidates_batch(
        [decision], _ctx(config, broker)
    )

    assert lookup_calls["count"] == 2  # pre-pass miss, then the per-candidate retry hit
    assert errors == 0
    outcome = candidate_outcomes["BYDDY"]
    # The recovered fill, not today's own "skipped" (still-accumulating)
    # outcome, is what survives.
    assert outcome["status"] == "submitted"
    assert outcome["order_id"] == "RH-EVENTUALLY-CONSISTENT-1"
    assert outcome["whole_share_quantity"] == 1
    assert ledger.shares_purchased_total("BYDDY") == 1


def test_batch_preserves_a_recovered_zero_fill_attempt_over_todays_own_non_submission(tmp_path, monkeypatch):
    """Codex review ("Preserve recovered zero-fill attempts through the
    batch"): the pre-pass's own zero_fill_attempt recovery (see
    _book_reconciled_whole_share_order) deliberately reports "status":
    "failed" -- a REAL, countable broker attempt, just not a purchase.
    The outer batch loop's own seeded/mid-call recovery preservation only
    ever looked for "status" == "submitted", so this recovery was silently
    overwritten by today's own processing of the SAME ticker (here, "still
    accumulating") the instant that candidate ran -- erasing the attempt
    from the run artifact entirely, even though ctx.state.record_buy()
    already counted it toward max_orders_per_day.
    """
    monkeypatch.setenv("DCA_CRON_TRACKING_ROOT", str(tmp_path / "cron_tracking"))
    ledger = WholeShareAccumulationLedger()
    claim_trading_day = date(2026, 8, 15)
    ledger.record_contribution(
        "BYDDY", claim_trading_day.isoformat(), 10.0, 10.0, stream="afternoon",
        idempotency_key="afternoon-BYDDY", recorded_at="2026-08-15T15:30:00-04:00",
    )

    config = StrategyConfig(whole_share_tickers=frozenset({"BYDDY"}), dry_run=False)

    class _CrossDayZeroFillBroker(DryRunBroker):
        def same_day_strategy_order_exists(self, ticker, trading_day):
            return False

        def same_day_strategy_order_exists_with_ref_id_prefix(self, ticker, trading_day, ref_id_prefixes):
            return None

        def same_day_order_with_ref_id_prefix_any_state(self, ticker, trading_day, ref_id_prefixes):
            if ref_id_prefixes == ("graded-dca-wholeshare-2026-08-15-BYDDY",):
                return {"id": "RH-BATCH-ZERO-FILL-1", "state": "rejected", "cumulative_quantity": "0"}
            return None

    # No buying power at all -- releasing the OLDER (2026-08-15) claim as a
    # confirmed zero fill restores its $10 back to pending, which alone now
    # covers a full share and would otherwise let today's own processing
    # legitimately re-fire a FRESH order for it. Zero cash instead blocks
    # that fresh attempt on the ordinary buying-power gate, so today's own
    # outcome for this SAME ticker is a genuine non-submission ("blocked"),
    # distinct from -- and, before this fix, silently overwriting -- the
    # recovered zero-fill attempt.
    broker = _CrossDayZeroFillBroker(cash=0.0, reference_prices={"BYDDY": 10.0})
    decision = _decision(suggested_amount_usd=3.0, target_price=10.0)
    _near_close_dca._IN_FLIGHT_CANDIDATE_OUTCOMES["outcomes"] = {}
    ctx = _ctx(config, broker)

    candidate_outcomes, _, running_deployed_7d, errors, _ = _process_buy_candidates_batch(
        [decision], ctx
    )

    assert running_deployed_7d == 0.0  # nothing purchased
    ledger = WholeShareAccumulationLedger()
    assert ledger.shares_purchased_total("BYDDY") == 0
    assert ledger.unresolved_claim("BYDDY", claim_trading_day.isoformat(), "afternoon-BYDDY") is None
    # The confirmed zero-fill attempt is counted toward the daily cap...
    assert ctx.state.day_order_count(claim_trading_day) == 1
    # ...and survives in the run's own outcome artifact instead of being
    # silently overwritten by today's own "still accumulating" outcome.
    outcome = candidate_outcomes["BYDDY"]
    assert outcome["status"] == "failed"
    assert outcome["order_id"] == "RH-BATCH-ZERO-FILL-1"
    assert outcome.get("whole_share_zero_fill_attempt") is True


def test_pre_roll_recovered_spend_is_carried_into_the_candidate_batch(tmp_path, monkeypatch):
    """Codex review ("Preserve pre-roll recovered spend for the candidate
    batch"): the pre-roll cross-day reconciliation pass runs before
    _process_buy_candidates_batch, which seeds its own running_deployed_7d
    from config.deployed_last_7d_usd. An earlier version of this fix
    discarded the pre-roll pass's own returned running_deployed_7d instead
    of folding it back into config.deployed_last_7d_usd -- since
    settle() is idempotent, _process_buy_candidates_batch's own later
    pre-pass can never rediscover a claim the pre-roll pass already
    resolved, so a recovered fill that should have engaged the kill
    switch was silently omitted from the run's risk_guard reporting.
    """
    monkeypatch.setenv("DCA_CRON_TRACKING_ROOT", str(tmp_path / "cron_tracking"))
    ledger = WholeShareAccumulationLedger()
    claim_trading_day = date(2026, 8, 15)  # within the trailing 7-day window
    ledger.record_contribution(
        "BYDDY", claim_trading_day.isoformat(), 10.0, 10.0, stream="afternoon",
        idempotency_key="afternoon-BYDDY", recorded_at="2026-08-15T15:30:00-04:00",
    )

    now = datetime(2026, 8, 16, 15, 45, tzinfo=EASTERN)
    config = StrategyConfig(
        dry_run=False,
        tickers=(),
        whole_share_tickers=frozenset({"BYDDY"}),
        approved_test_dates=frozenset({date(2026, 8, 16)}),
    )
    state = StrategyState()

    class _PreRollRecoveryBroker(DryRunBroker):
        def same_day_strategy_order_exists(self, ticker, trading_day):
            return False

        def same_day_strategy_order_exists_with_ref_id_prefix(self, ticker, trading_day, ref_id_prefixes):
            if ref_id_prefixes == ("graded-dca-wholeshare-2026-08-15-BYDDY",):
                return {"id": "RH-PRE-ROLL-1", "state": "filled", "quantity": "1"}
            return None

    broker = _PreRollRecoveryBroker(cash=500.0, reference_prices={"BYDDY": 10.0})
    data = _FakeSignalData(SourceName.FINANCE, close_price=9.90, prev_close=10.00)
    rh_data = _FakeSignalData(SourceName.ROBINHOOD, close_price=9.90, prev_close=10.00)

    result = run_daily_compact(
        now,
        config,
        state,
        data,
        rh_data,
        broker,
        options=CompactRunOptions(artifacts_dir=str(tmp_path / "artifacts")),
    )

    assert result["blocked"] is False
    import json
    from pathlib import Path

    run_state = json.loads(Path(result["run_state_path"]).read_bytes())
    assert ledger.shares_purchased_total("BYDDY") == 1
    # The reconciled $10 is reflected in the run's own risk_guard reporting,
    # not silently dropped.
    assert run_state["risk_guard"]["deployed_last_7d_usd"] == pytest.approx(10.0)


def test_whole_share_claim_is_reconciled_even_when_the_date_gate_blocks_the_run(tmp_path, monkeypatch):
    """Codex review ("Reconcile whole-share claims before date-gated
    returns"): _run_once_impl's own inverse-Martingale lease recovery
    already runs on the date/holiday-gated early-return path (see
    _book_recovered_lease_durably), but the analogous whole-share claim
    reconciliation did not -- so an ambiguous whole-share placement claimed
    on the FINAL approved trading date left an unresolved claim that every
    SUBSEQUENT scheduled invocation could never reach: each one returns
    from the date gate before the ordinary reconciliation pass further
    down in _run_once_impl even runs, permanently stranding the ticker's
    balance until an operator manually reopens the trading window.
    """
    monkeypatch.setenv("DCA_CRON_TRACKING_ROOT", str(tmp_path / "cron_tracking"))
    ledger = WholeShareAccumulationLedger()
    claim_trading_day = date(2026, 8, 15)  # the final approved trading date
    ledger.record_contribution(
        "BYDDY", claim_trading_day.isoformat(), 10.0, 10.0, stream="afternoon",
        idempotency_key="afternoon-BYDDY", recorded_at="2026-08-15T15:30:00-04:00",
    )

    # The trading window closed before a retry ever ran -- 2026-08-16 is not
    # in this config's approved_test_dates, so _check_execution_gates blocks
    # the whole run.
    now = datetime(2026, 8, 16, 15, 45, tzinfo=EASTERN)
    config = StrategyConfig(
        dry_run=False,
        tickers=(),
        whole_share_tickers=frozenset({"BYDDY"}),
        approved_test_dates=frozenset({claim_trading_day}),
    )
    state = StrategyState()

    class _GatedRecoveryBroker(DryRunBroker):
        def same_day_strategy_order_exists(self, ticker, trading_day):
            return False

        def same_day_strategy_order_exists_with_ref_id_prefix(self, ticker, trading_day, ref_id_prefixes):
            if ref_id_prefixes == ("graded-dca-wholeshare-2026-08-15-BYDDY",):
                return {"id": "RH-GATED-1", "state": "filled", "quantity": "1"}
            return None

    broker = _GatedRecoveryBroker(cash=500.0, reference_prices={"BYDDY": 10.0})
    data = _FakeSignalData(SourceName.FINANCE, close_price=9.90, prev_close=10.00)
    rh_data = _FakeSignalData(SourceName.ROBINHOOD, close_price=9.90, prev_close=10.00)

    result = run_daily_compact(
        now,
        config,
        state,
        data,
        rh_data,
        broker,
        options=CompactRunOptions(artifacts_dir=str(tmp_path / "artifacts")),
    )

    assert result["blocked"] is True
    # The claim was still reconciled against the broker despite the run
    # returning immediately after the date gate.
    assert ledger.shares_purchased_total("BYDDY") == 1
    assert ledger.unresolved_claim("BYDDY", claim_trading_day.isoformat(), "afternoon-BYDDY") is None
    # The DURABLE half of booking (state.record_buy against the claim day)
    # must still happen even though the gate blocks this run from
    # evaluating any candidates -- once this pass resolves the claim, no
    # later run can ever rediscover it to book it retroactively, and the
    # next allowed run would otherwise compute headroom as if the money
    # were never spent.
    assert state.day_notional(claim_trading_day) == pytest.approx(10.0)


def test_whole_share_claim_is_reconciled_on_the_gated_path_for_a_ticker_removed_from_config(
    tmp_path, monkeypatch
):
    """Codex review ("Discover orphaned ledgers before the gated
    return"): the gated-path whole-share reconciliation loop, like the
    normal (gate-passing) pre-pass, previously only iterated the CURRENT
    config.whole_share_tickers set. If a ticker with an unresolved claim
    is removed from configuration and every future invocation lands
    beyond the final approved date, every run returns through THIS gated
    branch before ever reaching the discovery-aware normal pre-pass, so
    the claim (and any real fill) remains permanently unreconciled.
    """
    monkeypatch.setenv("DCA_CRON_TRACKING_ROOT", str(tmp_path / "cron_tracking"))
    ledger = WholeShareAccumulationLedger()
    claim_trading_day = date(2026, 8, 15)  # the final approved trading date
    ledger.record_contribution(
        "BYDDY", claim_trading_day.isoformat(), 10.0, 10.0, stream="afternoon",
        idempotency_key="afternoon-BYDDY", recorded_at="2026-08-15T15:30:00-04:00",
    )

    now = datetime(2026, 8, 16, 15, 45, tzinfo=EASTERN)
    # BYDDY is no longer enrolled in whole_share_tickers at all -- an
    # operator removed it after the claim was recorded.
    config = StrategyConfig(
        dry_run=False,
        tickers=(),
        whole_share_tickers=frozenset(),
        approved_test_dates=frozenset({claim_trading_day}),
    )
    state = StrategyState()

    class _OrphanedGatedRecoveryBroker(DryRunBroker):
        def same_day_strategy_order_exists(self, ticker, trading_day):
            return False

        def same_day_strategy_order_exists_with_ref_id_prefix(self, ticker, trading_day, ref_id_prefixes):
            if ref_id_prefixes == ("graded-dca-wholeshare-2026-08-15-BYDDY",):
                return {"id": "RH-ORPHANED-GATED-1", "state": "filled", "quantity": "1"}
            return None

    broker = _OrphanedGatedRecoveryBroker(cash=500.0, reference_prices={"BYDDY": 10.0})
    data = _FakeSignalData(SourceName.FINANCE, close_price=9.90, prev_close=10.00)
    rh_data = _FakeSignalData(SourceName.ROBINHOOD, close_price=9.90, prev_close=10.00)

    result = run_daily_compact(
        now,
        config,
        state,
        data,
        rh_data,
        broker,
        options=CompactRunOptions(artifacts_dir=str(tmp_path / "artifacts")),
    )

    assert result["blocked"] is True
    assert ledger.shares_purchased_total("BYDDY") == 1
    assert ledger.unresolved_claim("BYDDY", claim_trading_day.isoformat(), "afternoon-BYDDY") is None
    assert state.day_notional(claim_trading_day) == pytest.approx(10.0)


def test_pre_roll_recovered_fill_is_persisted_into_buy_candidates(tmp_path, monkeypatch):
    """Codex review ("Persist fills recovered by the pre-roll reconciliation
    pass"): the pre-roll pass runs BEFORE _process_buy_candidates_batch
    installs _IN_FLIGHT_CANDIDATE_OUTCOMES["outcomes"], so
    _book_reconciled_whole_share_order's own _publish_in_flight_outcome
    call was a no-op -- the recovered fill was booked into ctx.state/
    running_deployed_7d for THIS run only, but never reached this run's own
    buy_candidates.csv/run_state.json. Since dca_risk.deployed_usd() (which
    a FUTURE run's own kill-switch seeding reads) sums only submitted
    buy_candidates rows, that recovered spend would vanish from trailing
    deployment accounting the moment this run ends. BYDDY's own signal
    today is deliberately NOT a buy (close above the prior close), so the
    only way BYDDY can appear in buy_candidates at all is via the pre-roll
    pass's own recovered outcome.
    """
    monkeypatch.setenv("DCA_CRON_TRACKING_ROOT", str(tmp_path / "cron_tracking"))
    ledger = WholeShareAccumulationLedger()
    claim_trading_day = date(2026, 8, 15)
    ledger.record_contribution(
        "BYDDY", claim_trading_day.isoformat(), 10.0, 10.0, stream="afternoon",
        idempotency_key="afternoon-BYDDY", recorded_at="2026-08-15T15:30:00-04:00",
    )

    now = datetime(2026, 8, 16, 15, 45, tzinfo=EASTERN)
    config = StrategyConfig(
        dry_run=False,
        tickers=(),
        whole_share_tickers=frozenset({"BYDDY"}),
        approved_test_dates=frozenset({date(2026, 8, 16)}),
    )
    state = StrategyState()

    class _PreRollRecoveryBroker(DryRunBroker):
        def same_day_strategy_order_exists(self, ticker, trading_day):
            return False

        def same_day_strategy_order_exists_with_ref_id_prefix(self, ticker, trading_day, ref_id_prefixes):
            if ref_id_prefixes == ("graded-dca-wholeshare-2026-08-15-BYDDY",):
                return {"id": "RH-PRE-ROLL-PERSIST-1", "state": "filled", "quantity": "1"}
            return None

    broker = _PreRollRecoveryBroker(cash=500.0, reference_prices={"BYDDY": 10.0})
    # ABOVE the prior close -- BYDDY's own signal today is NOT a buy.
    data = _FakeSignalData(SourceName.FINANCE, close_price=10.10, prev_close=10.00)
    rh_data = _FakeSignalData(SourceName.ROBINHOOD, close_price=10.10, prev_close=10.00)

    result = run_daily_compact(
        now,
        config,
        state,
        data,
        rh_data,
        broker,
        options=CompactRunOptions(artifacts_dir=str(tmp_path / "artifacts")),
    )

    assert result["blocked"] is False
    import json
    from pathlib import Path

    run_state = json.loads(Path(result["run_state_path"]).read_bytes())
    assert ledger.shares_purchased_total("BYDDY") == 1

    byddy_rows = [c for c in run_state["buy_candidates"] if c.get("ticker") == "BYDDY"]
    assert len(byddy_rows) == 1
    assert byddy_rows[0]["status"] == "submitted"
    assert byddy_rows[0]["order_id"] == "RH-PRE-ROLL-PERSIST-1"
    # deployed_usd() (dca_risk.py) sums exactly this field over submitted
    # buy_candidates -- a future run's kill-switch seeding depends on it
    # being present and correct here.
    assert byddy_rows[0]["planned_amount_usd"] == pytest.approx(10.0)


def test_batch_aggregates_a_recovered_fill_with_todays_own_submission(tmp_path, monkeypatch):
    """Codex review ("Preserve recovered fills when processing today's
    candidate"): a recovered fill from an EARLIER trading day's claim
    (seeded into candidate_outcomes by _run_once_impl's pre-roll pass) and
    TODAY's own, independent processing of the SAME ticker are two
    genuinely different orders. Unconditionally overwriting the seeded
    entry with today's own outcome (the pre-fix behavior) silently erased
    the recovered fill whenever today's own outcome was a real submission
    too -- dca_risk.deployed_usd() sums exactly ONE row's planned_amount_usd
    per ticker, so only the later of the two ever counted. The batch loop
    must aggregate both into one row instead.
    """
    monkeypatch.setenv("DCA_CRON_TRACKING_ROOT", str(tmp_path / "cron_tracking"))
    config = StrategyConfig(whole_share_tickers=frozenset({"BYDDY"}), dry_run=False)
    broker = DryRunBroker(cash=500.0, reference_prices={"BYDDY": 10.0})

    # Seed the pre-roll pass's own recovered outcome for BYDDY, exactly as
    # _run_once_impl would have installed it before this function starts.
    _near_close_dca._IN_FLIGHT_CANDIDATE_OUTCOMES["outcomes"] = {
        "BYDDY": {
            "status": "submitted",
            "order_id": "RH-RECOVERED-1",
            "skip_reason": None,
            "submitted_amount_usd": 10.0,
            "whole_share_quantity": 1,
        }
    }

    decision = _decision(suggested_amount_usd=10.0, target_price=10.0)
    candidate_outcomes, _, running_deployed_7d, _, _ = _process_buy_candidates_batch(
        [decision], _ctx(config, broker)
    )

    outcome = candidate_outcomes["BYDDY"]
    assert outcome["status"] == "submitted"
    # $10 recovered + $10 placed today == $20 aggregated, not just $10 from
    # whichever of the two happened to be written last.
    assert outcome["submitted_amount_usd"] == pytest.approx(20.0)
    assert outcome["whole_share_quantity"] == 2
    # order_id stays a SINGLE, exact broker order id (today's own, the
    # newer of the two) -- not a synthetic joined string no exact-ID
    # consumer could ever match (Codex review: "Preserve each broker order
    # ID when aggregating outcomes"). Both distinct ids are still recorded.
    assert outcome["order_id"] == "DRY-RUN-WHOLESHARE-BYDDY-1-graded-dca-wholeshare-2026-08-16-BYDDY"
    assert outcome["whole_share_order_ids"] == [
        "RH-RECOVERED-1",
        "DRY-RUN-WHOLESHARE-BYDDY-1-graded-dca-wholeshare-2026-08-16-BYDDY",
    ]


def test_batch_keeps_a_recovered_fill_when_todays_own_outcome_is_not_a_submission(tmp_path, monkeypatch):
    """Companion to the aggregation test above: when today's own processing
    of the ticker does NOT result in a real placement (e.g. still
    accumulating below the share threshold), the recovered fill must be
    kept exactly as seeded -- not replaced by a non-submission outcome.
    """
    monkeypatch.setenv("DCA_CRON_TRACKING_ROOT", str(tmp_path / "cron_tracking"))
    config = StrategyConfig(whole_share_tickers=frozenset({"BYDDY"}), dry_run=False)
    broker = DryRunBroker(cash=500.0, reference_prices={"BYDDY": 10.0})

    _near_close_dca._IN_FLIGHT_CANDIDATE_OUTCOMES["outcomes"] = {
        "BYDDY": {
            "status": "submitted",
            "order_id": "RH-RECOVERED-2",
            "skip_reason": None,
            "submitted_amount_usd": 10.0,
            "whole_share_quantity": 1,
        }
    }

    # $3 contribution against a $10 share price never crosses the threshold.
    decision = _decision(suggested_amount_usd=3.0, target_price=10.0)
    candidate_outcomes, _, _, _, _ = _process_buy_candidates_batch([decision], _ctx(config, broker))

    outcome = candidate_outcomes["BYDDY"]
    assert outcome["status"] == "submitted"
    assert outcome["order_id"] == "RH-RECOVERED-2"
    assert outcome["submitted_amount_usd"] == pytest.approx(10.0)
    assert outcome["whole_share_quantity"] == 1


def test_out_of_window_reconciliation_is_published_with_its_true_claim_date(tmp_path, monkeypatch):
    """Codex review ("Attribute reconciled deployment to the claim date",
    "Preserve the claim date for in-window recoveries"): dca_risk's
    deployment accounting used to bucket a submitted buy_candidates row
    under the ARTIFACT's own trading_day (today), not the claim's true
    execution date. An earlier version of this fix responded by skipping
    publication entirely for an out-of-window claim -- but that just traded
    one hazard (misattribution) for another (the recovered order vanishing
    from buy_candidates.csv/run_state.json entirely). The row is now
    published unconditionally, tagged with whole_share_claim_breakdown
    (this module's own trading-day/amount pairing), so
    dca_risk.deployed_usd_by_claim_date() can attribute it correctly no
    matter how old the claim is -- the in-memory running_deployed_7d total
    still correctly excludes it (that total only ever covers the trailing
    kill-switch window this SAME run cares about).
    """
    monkeypatch.setenv("DCA_CRON_TRACKING_ROOT", str(tmp_path / "cron_tracking"))
    ledger = WholeShareAccumulationLedger()
    claim_trading_day = date(2026, 8, 1)  # well outside the 7-day window ending 2026-08-16
    ledger.record_contribution(
        "BYDDY", claim_trading_day.isoformat(), 10.0, 10.0, stream="afternoon",
        idempotency_key="afternoon-BYDDY", recorded_at="2026-08-01T15:30:00-04:00",
    )

    config = StrategyConfig(whole_share_tickers=frozenset({"BYDDY"}), dry_run=False)

    class _OutOfWindowConfirmingBroker(DryRunBroker):
        def same_day_strategy_order_exists(self, ticker, trading_day):
            return False

        def same_day_strategy_order_exists_with_ref_id_prefix(self, ticker, trading_day, ref_id_prefixes):
            if ref_id_prefixes == ("graded-dca-wholeshare-2026-08-01-BYDDY",):
                return {"id": "RH-OUT-OF-WINDOW-1", "state": "filled", "quantity": "1"}
            return None

    broker = _OutOfWindowConfirmingBroker(cash=500.0, reference_prices={"BYDDY": 10.0})
    ctx = _ctx(config, broker)  # trading_day is 2026-08-16
    _near_close_dca._IN_FLIGHT_CANDIDATE_OUTCOMES["outcomes"] = {}

    running_deployed_7d = _reconcile_cross_day_whole_share_claims_for_batch(ctx, running_deployed_7d=0.0)

    assert ledger.shares_purchased_total("BYDDY") == 1
    assert ledger.unresolved_claim("BYDDY", "2026-08-01", "afternoon-BYDDY") is None
    # Excluded from the in-memory kill-switch total -- outside the trailing
    # 7-day window ending on THIS run's own trading_day (2026-08-16).
    assert running_deployed_7d == 0.0

    outcome = _near_close_dca._IN_FLIGHT_CANDIDATE_OUTCOMES["outcomes"]["BYDDY"]
    assert outcome["status"] == "submitted"
    assert outcome["submitted_amount_usd"] == pytest.approx(10.0)
    assert outcome["whole_share_claim_breakdown"] == [
        {"trading_day": "2026-08-01", "amount_usd": 10.0}
    ]


def test_reconciling_a_prior_week_claim_before_rolling_reserve_reduces_the_rollover(tmp_path, monkeypatch):
    """Codex review ("Reconcile prior-week claims before rolling
    reserve"): roll_weekly_reserve() bases the rolled-forward reserve on
    `NORMAL_WEEKLY_CAP_USD - state.week_notional(last_week)`. If a
    whole-share claim from the PRECEDING ISO week is reconciled AFTER the
    reserve has already been rolled (reserve_week_key now matches the
    CURRENT week), that reconciled spend can never reduce the reserve --
    it was already frozen using the too-high, not-yet-reconciled prior-week
    notional. Reconciling first must reduce the rolled-forward reserve by
    exactly the reconciled amount.
    """
    monkeypatch.setenv("DCA_CRON_TRACKING_ROOT", str(tmp_path / "cron_tracking"))
    ledger = WholeShareAccumulationLedger()
    claim_trading_day = date(2026, 8, 10)  # a Monday, the ISO week before trading_day below
    ledger.record_contribution(
        "BYDDY", claim_trading_day.isoformat(), 10.0, 10.0, stream="afternoon",
        idempotency_key="afternoon-BYDDY", recorded_at="2026-08-10T15:30:00-04:00",
    )

    config = StrategyConfig(whole_share_tickers=frozenset({"BYDDY"}), dry_run=False)

    class _PriorWeekConfirmingBroker(DryRunBroker):
        def same_day_strategy_order_exists(self, ticker, trading_day):
            return False

        def same_day_strategy_order_exists_with_ref_id_prefix(self, ticker, trading_day, ref_id_prefixes):
            if ref_id_prefixes == ("graded-dca-wholeshare-2026-08-10-BYDDY",):
                return {"id": "RH-PRIOR-WEEK-1", "state": "filled", "quantity": "1"}
            return None

    broker = _PriorWeekConfirmingBroker(cash=500.0, reference_prices={"BYDDY": 10.0})
    trading_day = date(2026, 8, 17)  # the following Monday -- a new ISO week
    state = StrategyState()
    state.finalized_budget_days.add(claim_trading_day)  # last week saw activity
    ctx = ProcessCandidateContext(
        config=config,
        state=state,
        broker=broker,
        robinhood_data=None,
        now=datetime(2026, 8, 17, 15, 45, tzinfo=EASTERN),
        trading_day=trading_day,
        guard_active=False,
        prior_same_day_allocations={},
        scheduled=True,
        run_result=None,
    )

    # Reconcile the prior-week claim FIRST (mirrors _run_once_impl's own
    # ordering fix), then roll the reserve.
    _reconcile_cross_day_whole_share_claims_for_batch(ctx, running_deployed_7d=0.0)
    assert ledger.shares_purchased_total("BYDDY") == 1
    state.roll_weekly_reserve(trading_day, config)

    # The reconciled $10 reduced the prior week's unused Normal-cap
    # capacity, so the rolled-forward reserve is $10 below the full cap --
    # not the full, un-reconciled NORMAL_WEEKLY_CAP_USD.
    assert state.weekly_rollover_reserve_usd == pytest.approx(NORMAL_WEEKLY_CAP_USD - 10.0)


def test_pre_roll_reconciliation_runs_before_tier_planning_kill_switch_preview(tmp_path, monkeypatch):
    """Codex review ("Reconcile recovered spend before planning the budget
    tier"): the planned_spend loop's whole-share preview reads
    `kill_switch_engaged(config)` directly to decide whether to clamp a
    candidate's preview notional to its `selected_base_amount_usd`. An
    earlier ordering ran the pre-roll cross-day reconciliation pass AFTER
    that loop, so a claim reconciled by that pass -- pushing
    `deployed_last_7d_usd` over the kill-switch threshold -- was invisible
    to the loop's own read: tier selection summed the candidate's UNCAPPED
    preview notional even though execution (which re-evaluates the kill
    switch against the now-updated config) actually caps it, letting the
    batch select a needlessly high tier. Reconciliation must run before the
    loop so both reads see the same, already-updated config.
    """
    monkeypatch.setenv("DCA_CRON_TRACKING_ROOT", str(tmp_path / "cron_tracking"))
    ledger = WholeShareAccumulationLedger()
    # A prior-day claim whose reconciled $10 notional, once folded into
    # deployed_last_7d_usd, pushes it past this test's deliberately tiny
    # kill-switch threshold ($20 * 0.4 = $8).
    claim_trading_day = date(2026, 8, 15)
    ledger.record_contribution(
        "BYDDY", claim_trading_day.isoformat(), 10.0, 10.0, stream="afternoon",
        idempotency_key="afternoon-BYDDY", recorded_at="2026-08-15T15:30:00-04:00",
    )
    # BYDDY's own current pending balance is large enough that an UNCAPPED
    # preview (ceiling = max_per_order_usd, raised here so it is not the
    # binding constraint) crosses the Normal tier's $129 planned-spend
    # threshold, but a kill-switch-CAPPED preview (ceiling clamped to
    # today's $2.00 selected_base_amount_usd, far below the $10 share
    # price) contributes $0.
    ledger.preserve_pending("BYDDY", "2026-08-16", 140.0, stream="afternoon", recorded_at="2026-08-16T15:45:00-04:00")

    now = datetime(2026, 8, 16, 15, 45, tzinfo=EASTERN)
    config = StrategyConfig(
        dry_run=False,
        tickers=(),
        whole_share_tickers=frozenset({"BYDDY"}),
        approved_test_dates=frozenset({date(2026, 8, 16)}),
        monthly_cash_allocation_usd=20.0,
        kill_switch_7d_deploy_fraction=0.4,
        max_per_order_usd=150.0,
    )
    state = StrategyState()

    class _PreRollKillSwitchBroker(DryRunBroker):
        def same_day_strategy_order_exists(self, ticker, trading_day):
            return False

        def same_day_strategy_order_exists_with_ref_id_prefix(self, ticker, trading_day, ref_id_prefixes):
            if ref_id_prefixes == ("graded-dca-wholeshare-2026-08-15-BYDDY",):
                return {"id": "RH-PRE-ROLL-KS-1", "state": "filled", "quantity": "1"}
            return None

    broker = _PreRollKillSwitchBroker(cash=500.0, reference_prices={"BYDDY": 10.0})
    # Below BOTH the prior close and prior low ($10.00) -- a BUY with the
    # full $2.00 selected_base_amount_usd.
    data = _FakeSignalData(SourceName.FINANCE, close_price=9.90, prev_close=10.00)
    rh_data = _FakeSignalData(SourceName.ROBINHOOD, close_price=9.90, prev_close=10.00)

    result = run_daily_compact(
        now,
        config,
        state,
        data,
        rh_data,
        broker,
        options=CompactRunOptions(artifacts_dir=str(tmp_path / "artifacts")),
    )

    assert result["blocked"] is False
    # The reconciled claim engaged the kill switch BEFORE tier planning ran,
    # so BYDDY's whole-share preview contributed ~$0 (clamped to the $2.00
    # base amount, below the $10 share price) instead of the ~$140
    # uncapped preview -- keeping planned spend under the Normal tier's
    # $129 threshold rather than escalating to Elevated or higher.
    assert state.selected_tier is not None
    assert state.selected_tier.name == "normal"


def test_a_reconciled_claim_already_recorded_on_disk_is_not_rebooked(tmp_path, monkeypatch):
    """Codex review ("Avoid rebooking orders after a settlement-only
    retry"): the ambiguous-placement-recovery branch (and the ordinary
    in-line placement path) both book ctx.state/running_deployed_7d and
    write a "submitted" execution artifact for a confirmed broker order
    even when the ledger's OWN settle() call then raises and leaves the
    claim unresolved. A LATER run's cross-day reconciliation can find that
    same still-unresolved claim, settle it successfully this time, and
    reach _book_reconciled_whole_share_order -- which must NOT rebook an
    order that a prior run's artifact already recorded on disk, or the
    order gets double-counted in both this run's own caps and the
    persisted deployment history a future run's kill switch sums from.
    """
    from test_near_close_dca import _write_scheduled_afternoon_artifact

    monkeypatch.setenv("DCA_CRON_TRACKING_ROOT", str(tmp_path / "cron_tracking"))
    ledger = WholeShareAccumulationLedger()
    claim_trading_day = date(2026, 8, 15)
    ledger.record_contribution(
        "BYDDY", claim_trading_day.isoformat(), 10.0, 10.0, stream="afternoon",
        idempotency_key="afternoon-BYDDY", recorded_at="2026-08-15T15:30:00-04:00",
    )
    assert len(ledger.unresolved_claims_any_day("BYDDY", "afternoon-BYDDY")) == 1

    # Simulates the earlier run whose broker order was confirmed and
    # already written to disk for claim_trading_day (the ledger's own
    # settle() call for that run raised, which is why the claim above is
    # still unresolved).
    history_root = str(tmp_path / "runs")
    _write_scheduled_afternoon_artifact(
        history_root,
        claim_trading_day,
        [{"ticker": "BYDDY", "status": "submitted", "planned_amount_usd": 10.0}],
    )

    config = StrategyConfig(whole_share_tickers=frozenset({"BYDDY"}), dry_run=False)

    class _CrossDayConfirmingBroker(DryRunBroker):
        def same_day_strategy_order_exists(self, ticker, trading_day):
            return False

        def same_day_strategy_order_exists_with_ref_id_prefix(self, ticker, trading_day, ref_id_prefixes):
            if ref_id_prefixes == ("graded-dca-wholeshare-2026-08-15-BYDDY",):
                return {"id": "RH-ALREADY-BOOKED-1", "state": "filled", "quantity": "1"}
            return None

    broker = _CrossDayConfirmingBroker(cash=500.0, reference_prices={"BYDDY": 10.0})
    ctx = ProcessCandidateContext(
        config=config,
        state=StrategyState(),
        broker=broker,
        robinhood_data=None,
        now=datetime(2026, 8, 16, 15, 45, tzinfo=EASTERN),
        trading_day=date(2026, 8, 16),
        guard_active=False,
        prior_same_day_allocations={},
        scheduled=True,
        run_result=None,
        deployment_history_root=history_root,
    )

    outcome, _, running_deployed_7d, errors, _ = _process_buy_candidate(
        _decision(suggested_amount_usd=3.0, target_price=10.0),
        ctx=ctx,
        running_deployed_7d=0.0,
    )

    assert errors == 0
    # The claim is settled in the ledger's own audit trail (real money,
    # genuinely purchased)...
    assert ledger.shares_purchased_total("BYDDY") == 1
    assert ledger.unresolved_claims_any_day("BYDDY", "afternoon-BYDDY") == []
    # ...but this run's state/deployment total must NOT double-count it,
    # since a prior run's artifact already recorded it on disk.
    assert ("BYDDY", claim_trading_day) not in ctx.state.own_orders_recorded
    assert running_deployed_7d == 0.0


def test_ambiguous_placement_recovery_reports_the_post_settlement_balance(tmp_path, monkeypatch):
    """Codex review ("Report the restored balance after ambiguous placement
    recovery"): when a multi-share fire is clamped by the per-order ceiling
    and placement raises after the broker accepted the smaller order, the
    ref-id reconciliation path's settle() call restores the unsubmitted
    shares' credit -- the reported whole_share_pending_usd must reflect
    that AUTHORITATIVE post-settlement balance, not the pre-settlement
    wsa_decision.pending_after_usd snapshot, mirroring the ordinary success
    path's own identical read.
    """
    monkeypatch.setenv("DCA_CRON_TRACKING_ROOT", str(tmp_path / "cron_tracking"))
    # $16 at $4/share previews 4 shares, but the $10 ceiling clamps to 2 --
    # settle() restores the 2 unsubmitted shares' $8 back into pending_usd.
    config = StrategyConfig(
        whole_share_tickers=frozenset({"BYDDY"}), dry_run=False, max_per_order_usd=10.0
    )

    class _AmbiguousClampedFailureBroker(DryRunBroker):
        def place_whole_share_buy(self, ticker, quantity, ref_id):
            raise RuntimeError("response timeout")

        def same_day_strategy_order_exists_with_ref_id_prefix(self, ticker, trading_day, ref_id_prefixes):
            return {"id": "RH-CLAMPED-ACCEPTED-456", "state": "filled", "quantity": "2"}

    broker = _AmbiguousClampedFailureBroker(cash=500.0, reference_prices={"BYDDY": 4.0})

    outcome, _, _, errors, _ = _process_buy_candidate(
        _decision(suggested_amount_usd=16.0, target_price=4.0),
        ctx=_ctx(config, broker),
        running_deployed_7d=0.0,
    )

    assert outcome["status"] == "submitted"
    assert outcome["whole_share_quantity"] == 2
    assert outcome["submitted_amount_usd"] == 8.0
    # The 2 unsubmitted shares' $8 was restored by settle() -- the
    # pre-settlement snapshot would have reported $0 here instead.
    assert outcome["whole_share_pending_usd"] == 8.0
    ledger = WholeShareAccumulationLedger()
    assert ledger.pending_usd("BYDDY") == 8.0


def test_ambiguous_placement_recovery_books_the_real_fill_price_not_the_signal_price(tmp_path, monkeypatch):
    """Codex review ("Book the actual fill notional after immediate
    recovery"): the immediate placement-error branch's own "if
    matched_order:" (live-lookup, terminal "filled") success path
    previously booked the locally-computed, signal-time order_notional
    rather than the matched order's own average_price. A market order can
    fill at a different price than the signal quote this candidate was
    planned against.
    """
    monkeypatch.setenv("DCA_CRON_TRACKING_ROOT", str(tmp_path / "cron_tracking"))
    config = StrategyConfig(whole_share_tickers=frozenset({"BYDDY"}), dry_run=False)

    class _RealFillPriceBroker(DryRunBroker):
        def place_whole_share_buy(self, ticker, quantity, ref_id):
            raise RuntimeError("response timeout")

        def same_day_strategy_order_exists_with_ref_id_prefix(self, ticker, trading_day, ref_id_prefixes):
            # Filled at $10.75, ABOVE the $10.00 cached signal-time price.
            return {"id": "RH-REAL-FILL-PRICE-1", "state": "filled", "quantity": "1", "average_price": "10.75"}

    broker = _RealFillPriceBroker(cash=500.0, reference_prices={"BYDDY": 10.0})

    outcome, _, running_deployed_7d, errors, _ = _process_buy_candidate(
        _decision(suggested_amount_usd=10.0, target_price=10.0),
        ctx=_ctx(config, broker),
        running_deployed_7d=0.0,
    )

    assert outcome["status"] == "submitted"
    assert errors == 0
    # Booked using the REAL $10.75 fill price, not the stale $10.00 cached
    # signal-time price.
    assert outcome["submitted_amount_usd"] == 10.75
    assert running_deployed_7d == 10.75
    ledger = WholeShareAccumulationLedger()
    assert ledger.shares_purchased_total("BYDDY") == 1


def test_ambiguous_placement_recovery_rejects_an_explicit_zero_fill(tmp_path, monkeypatch):
    """Codex review, PR #786 ("Reject invalid cumulative quantities before
    settling"): the immediate placement-error recovery's own "if
    matched_order:" branch has the same `... or ... .get("quantity")`
    construction as the ordinary-success path -- a matched order reporting
    `{"state": "filled", "cumulative_quantity": 0, "quantity": 1}` booked a
    full-share spend for a fill the broker explicitly reported as zero.
    """
    monkeypatch.setenv("DCA_CRON_TRACKING_ROOT", str(tmp_path / "cron_tracking"))
    config = StrategyConfig(whole_share_tickers=frozenset({"BYDDY"}), dry_run=False)

    class _RecoveredExplicitZeroFillBroker(DryRunBroker):
        def place_whole_share_buy(self, ticker, quantity, ref_id):
            raise RuntimeError("response timeout")

        def same_day_strategy_order_exists_with_ref_id_prefix(self, ticker, trading_day, ref_id_prefixes):
            return {
                "id": "RH-RECOVERED-ZERO-FILL-1", "state": "filled",
                "cumulative_quantity": 0, "quantity": 1, "average_price": "10.00",
            }

    broker = _RecoveredExplicitZeroFillBroker(cash=500.0, reference_prices={"BYDDY": 10.0})

    outcome, _, running_deployed_7d, errors, _ = _process_buy_candidate(
        _decision(suggested_amount_usd=10.0, target_price=10.0),
        ctx=_ctx(config, broker),
        running_deployed_7d=0.0,
    )

    assert outcome["status"] == "submitted"
    # Booked using the explicit ZERO fill, not the requested 1-share quantity.
    assert outcome["submitted_amount_usd"] == 0.0
    assert outcome["whole_share_quantity"] == 0
    assert running_deployed_7d == 0.0


def test_ambiguous_placement_recovery_leaves_an_unreadable_fill_unresolved(tmp_path, monkeypatch):
    """Codex review, PR #786 ("Keep invalid filled quantities unresolved").

    The immediate placement-error recovery's "if matched_order:" branch has
    the same contradictory-evidence problem as the ordinary-success path: a
    matched order reporting a PRESENT but unreadable/negative
    `cumulative_quantity` on a `state: "filled"` order settled the ledger
    against the locally-planned share count as if it were verified. The
    claim must be left unresolved for manual reconciliation instead.
    """
    monkeypatch.setenv("DCA_CRON_TRACKING_ROOT", str(tmp_path / "cron_tracking"))
    config = StrategyConfig(whole_share_tickers=frozenset({"BYDDY"}), dry_run=False)

    class _RecoveredUnreadableFillBroker(DryRunBroker):
        def place_whole_share_buy(self, ticker, quantity, ref_id):
            raise RuntimeError("response timeout")

        def same_day_strategy_order_exists_with_ref_id_prefix(self, ticker, trading_day, ref_id_prefixes):
            return {
                "id": "RH-RECOVERED-UNREADABLE-FILL-1", "state": "filled",
                "cumulative_quantity": -1, "average_price": "10.00",
            }

    broker = _RecoveredUnreadableFillBroker(cash=500.0, reference_prices={"BYDDY": 10.0})

    outcome, _, running_deployed_7d, errors, _ = _process_buy_candidate(
        _decision(suggested_amount_usd=10.0, target_price=10.0),
        ctx=_ctx(config, broker),
        running_deployed_7d=0.0,
    )

    assert outcome["status"] == "submitted"
    assert outcome["whole_share_settlement_reconciliation_needed"] is True
    ledger = WholeShareAccumulationLedger()
    assert ledger.shares_purchased_total("BYDDY") == 0
    unresolved = ledger.unresolved_claim("BYDDY", "2026-08-16", "afternoon-BYDDY")
    assert unresolved is not None and unresolved["resolved"] is False


def test_ambiguous_placement_recovery_leaves_an_explicit_null_fill_unresolved(tmp_path, monkeypatch):
    """Codex review, PR #786 ("Treat explicit null fill quantities as
    unresolved").

    The immediate placement-error recovery branch has the same null-vs-
    absent blindness as the ordinary-success path: an explicit JSON
    `cumulative_quantity: null` is Python `None`, identical to the absent-
    key case, so `raw_filled_quantity is not None` read it as silence
    rather than contradictory evidence and let settlement proceed against
    the locally-planned share count.
    """
    monkeypatch.setenv("DCA_CRON_TRACKING_ROOT", str(tmp_path / "cron_tracking"))
    config = StrategyConfig(whole_share_tickers=frozenset({"BYDDY"}), dry_run=False)

    class _RecoveredNullFillBroker(DryRunBroker):
        def place_whole_share_buy(self, ticker, quantity, ref_id):
            raise RuntimeError("response timeout")

        def same_day_strategy_order_exists_with_ref_id_prefix(self, ticker, trading_day, ref_id_prefixes):
            return {
                "id": "RH-RECOVERED-NULL-FILL-1", "state": "filled",
                "cumulative_quantity": None, "average_price": "10.00",
            }

    broker = _RecoveredNullFillBroker(cash=500.0, reference_prices={"BYDDY": 10.0})

    outcome, _, running_deployed_7d, errors, _ = _process_buy_candidate(
        _decision(suggested_amount_usd=10.0, target_price=10.0),
        ctx=_ctx(config, broker),
        running_deployed_7d=0.0,
    )

    assert outcome["status"] == "submitted"
    assert outcome["whole_share_settlement_reconciliation_needed"] is True
    ledger = WholeShareAccumulationLedger()
    assert ledger.shares_purchased_total("BYDDY") == 0
    unresolved = ledger.unresolved_claim("BYDDY", "2026-08-16", "afternoon-BYDDY")
    assert unresolved is not None and unresolved["resolved"] is False


def test_ambiguous_placement_recovery_shortfall_uses_the_cached_price_not_the_fill_price(tmp_path, monkeypatch):
    """Codex review ("Compute partial-fill shortfalls from the cached share
    price"): when a terminal partial fill executes away from the cached
    planning price AND ledger settlement itself then raises, the reported
    shortfall must be computed from the cached claim price (matching
    settle()'s own internal shortfall math), not from
    planned_order_notional minus the real, differently-priced fill
    notional. A two-share $20 claim (at a $10.00 cached price) filled for
    just 1 share at a real $12.00 fill price needs the unfilled share's
    $10.00 restored -- not $20.00 - $12.00 = $8.00.
    """
    monkeypatch.setenv("DCA_CRON_TRACKING_ROOT", str(tmp_path / "cron_tracking"))
    config = StrategyConfig(
        whole_share_tickers=frozenset({"BYDDY"}), dry_run=False, max_per_order_usd=10_000.0
    )

    class _SettleFailsAfterRealFillBroker(DryRunBroker):
        def place_whole_share_buy(self, ticker, quantity, ref_id):
            raise RuntimeError("response timeout")

        def same_day_strategy_order_exists_with_ref_id_prefix(self, ticker, trading_day, ref_id_prefixes):
            # Only 1 of the 2 planned shares filled, at a real $12.00 price
            # ABOVE the $10.00 cached signal-time price.
            return {"id": "RH-PARTIAL-FILL-SETTLE-FAILS-1", "state": "filled", "quantity": "1", "average_price": "12.00"}

    broker = _SettleFailsAfterRealFillBroker(cash=500.0, reference_prices={"BYDDY": 10.0})

    import dca_whole_share_accumulator as wsa_module

    original_settle = wsa_module.WholeShareAccumulationLedger.settle

    def _failing_settle(self, *args, **kwargs):
        raise RuntimeError("simulated settle failure")

    monkeypatch.setattr(wsa_module.WholeShareAccumulationLedger, "settle", _failing_settle)

    outcome, _, _, errors, _ = _process_buy_candidate(
        _decision(suggested_amount_usd=20.0, target_price=10.0),
        ctx=_ctx(config, broker),
        running_deployed_7d=0.0,
    )

    monkeypatch.setattr(wsa_module.WholeShareAccumulationLedger, "settle", original_settle)

    assert outcome["status"] == "submitted"
    assert outcome["whole_share_settlement_reconciliation_needed"] is True
    # $20.00 planned (2 shares * $10.00 cached) minus 1 filled share at the
    # CACHED $10.00 price = $10.00 -- not $20.00 - $12.00 = $8.00.
    assert outcome["whole_share_settlement_shortfall_usd"] == 10.0


def test_immediate_recovery_does_not_rebook_after_losing_claim_ownership(tmp_path, monkeypatch):
    """Codex review ("Skip immediate booking after losing claim
    ownership"): settle() is called with require_claim_ownership=True, so
    a clean `False` return (no exception) means a CONCURRENT reconciler
    already won this exact claim and already booked it. The immediate
    placement-error branch previously ignored that return value and
    unconditionally recorded the buy / incremented running_deployed_7d /
    published another "submitted" execution anyway, double-booking the
    SAME physical broker order.
    """
    monkeypatch.setenv("DCA_CRON_TRACKING_ROOT", str(tmp_path / "cron_tracking"))
    config = StrategyConfig(whole_share_tickers=frozenset({"BYDDY"}), dry_run=False)

    class _AlreadyResolvedByConcurrentReconcilerBroker(DryRunBroker):
        def place_whole_share_buy(self, ticker, quantity, ref_id):
            raise RuntimeError("response timeout")

        def same_day_strategy_order_exists_with_ref_id_prefix(self, ticker, trading_day, ref_id_prefixes):
            return {"id": "RH-WON-BY-SOMEONE-ELSE-1", "state": "filled", "quantity": "1"}

    broker = _AlreadyResolvedByConcurrentReconcilerBroker(cash=500.0, reference_prices={"BYDDY": 10.0})

    import dca_whole_share_accumulator as wsa_module

    original_settle = wsa_module.WholeShareAccumulationLedger.settle

    def _lost_the_race_settle(self, *args, **kwargs):
        return False  # cleanly resolved elsewhere -- not an exception

    monkeypatch.setattr(wsa_module.WholeShareAccumulationLedger, "settle", _lost_the_race_settle)

    ctx = _ctx(config, broker)
    outcome, _, running_deployed_7d, errors, _ = _process_buy_candidate(
        _decision(suggested_amount_usd=10.0, target_price=10.0),
        ctx=ctx,
        running_deployed_7d=0.0,
    )

    monkeypatch.setattr(wsa_module.WholeShareAccumulationLedger, "settle", original_settle)

    assert outcome["status"] == "skipped"
    assert errors == 0
    # Nothing was double-booked into THIS run's own state/deployment total.
    assert running_deployed_7d == 0.0
    assert ("BYDDY", date(2026, 8, 16)) not in ctx.state.own_orders_recorded


def test_confirmation_required_whole_share_placement_is_not_classified_as_failed(tmp_path, monkeypatch):
    """Codex review ("Handle confirmation-required whole-share placements
    distinctly"): mirrors the normal buy path's own OrderConfirmationRequiredError
    handling -- review passed but the broker requires a separate
    post-review confirmation step. This is an expected hold, not a
    placement failure: it must not count toward errors_count_inc or be
    classified "failed" (which could trigger failure recovery/escalation
    for an expected, non-error condition), and the deducted pending balance
    must be restored since no order was actually placed.
    """
    monkeypatch.setenv("DCA_CRON_TRACKING_ROOT", str(tmp_path / "cron_tracking"))

    class _ConfirmationRequiredBroker(DryRunBroker):
        def place_whole_share_buy(self, ticker, quantity, ref_id):
            raise OrderConfirmationRequiredError("confirmation required")

    config = StrategyConfig(whole_share_tickers=frozenset({"BYDDY"}), dry_run=False)
    broker = _ConfirmationRequiredBroker(cash=500.0, reference_prices={"BYDDY": 10.0})

    outcome, _, _, errors, _ = _process_buy_candidate(
        _decision(suggested_amount_usd=10.0, target_price=10.0),
        ctx=_ctx(config, broker),
        running_deployed_7d=0.0,
    )

    assert outcome["status"] == "not_submitted_confirmation_required"
    assert errors == 0
    ledger = WholeShareAccumulationLedger()
    assert ledger.pending_usd("BYDDY") == 10.0


def test_confirmation_required_redispatch_outcome_retains_a_whole_share_marker(tmp_path, monkeypatch):
    """Codex review ("Preserve confirmation-required redispatch outcomes"):
    a manual preserved-balance redispatch (decision.action is NO_ACTION)
    whose broker requires confirmation must still carry a "whole_share_"
    marker -- otherwise _build_buy_candidates's non-BUY filter drops it,
    and if it were the only candidate this run, the run would report
    no_candidates instead of confirmation_required, silently omitting the
    hold from the candidate artifacts entirely.
    """
    monkeypatch.setenv("DCA_CRON_TRACKING_ROOT", str(tmp_path / "cron_tracking"))
    ledger = WholeShareAccumulationLedger()
    # Seeded at a deliberately huge price so the seed call itself buys 0
    # shares, leaving the full $10 pending (rather than immediately firing).
    ledger.record_contribution(
        "BYDDY", "2026-08-16", 10.0, 1_000_000.0, stream="seed", idempotency_key="seed"
    )
    assert ledger.pending_usd("BYDDY") == 10.0

    class _ConfirmationRequiredBroker(DryRunBroker):
        def place_whole_share_buy(self, ticker, quantity, ref_id):
            raise OrderConfirmationRequiredError("confirmation required")

    config = StrategyConfig(whole_share_tickers=frozenset({"BYDDY"}), dry_run=False)
    broker = _ConfirmationRequiredBroker(cash=500.0, reference_prices={"BYDDY": 10.0})

    outcome, _, _, errors, _ = _process_buy_candidate(
        _decision(action=Action.NO_ACTION, suggested_amount_usd=0.0, target_price=10.0),
        ctx=_ctx(config, broker, scheduled=False),
        running_deployed_7d=0.0,
    )

    assert outcome["status"] == "not_submitted_confirmation_required"
    assert errors == 0
    assert any(key.startswith("whole_share_") for key in outcome)


def test_confirmation_required_restoration_failure_is_isolated(tmp_path, monkeypatch):
    """Codex review ("Isolate restoration failures in the confirmation-
    required path"): the OrderConfirmationRequiredError branch's own
    preserve_pending() call can itself raise (a lock timeout, corrupt
    ledger, or write error) exactly like the generic placement-failure
    branch's identical call -- without an adjacent boundary here, the
    exception escapes _process_buy_candidates_batch entirely, aborting
    every other, unrelated candidate still to be processed. The claim must
    be left exactly as record_contribution left it, handled as a
    per-candidate data error.
    """
    monkeypatch.setenv("DCA_CRON_TRACKING_ROOT", str(tmp_path / "cron_tracking"))

    class _ConfirmationRequiredBroker(DryRunBroker):
        def place_whole_share_buy(self, ticker, quantity, ref_id):
            raise OrderConfirmationRequiredError("confirmation required")

    config = StrategyConfig(whole_share_tickers=frozenset({"BYDDY"}), dry_run=False)
    broker = _ConfirmationRequiredBroker(cash=500.0, reference_prices={"BYDDY": 10.0})

    def _raising_preserve_pending(self, *args, **kwargs):
        raise RuntimeError("ledger lock timeout")

    monkeypatch.setattr(WholeShareAccumulationLedger, "preserve_pending", _raising_preserve_pending)

    outcome, _, _, errors, _ = _process_buy_candidate(
        _decision(suggested_amount_usd=10.0, target_price=10.0),
        ctx=_ctx(config, broker),
        running_deployed_7d=0.0,
    )

    assert outcome["status"] == "blocked"
    assert "restoration also failed" in outcome["skip_reason"]
    assert errors == 1


def test_ledger_settlement_failure_still_publishes_the_submitted_outcome(tmp_path, monkeypatch):
    """Codex review ("Preserve accepted orders when ledger settlement
    fails"): the broker has ALREADY accepted the order by the time
    settle() runs -- a settlement exception must not make the caller
    believe no order was placed.
    """
    monkeypatch.setenv("DCA_CRON_TRACKING_ROOT", str(tmp_path / "cron_tracking"))
    import dca_whole_share_accumulator

    def _boom(self, *args, **kwargs):
        raise RuntimeError("ledger unwritable")

    monkeypatch.setattr(dca_whole_share_accumulator.WholeShareAccumulationLedger, "settle", _boom)

    config = StrategyConfig(whole_share_tickers=frozenset({"BYDDY"}), dry_run=False)
    broker = DryRunBroker(cash=500.0, reference_prices={"BYDDY": 10.0})

    outcome, _, _, errors, _ = _process_buy_candidate(
        _decision(suggested_amount_usd=10.0, target_price=10.0),
        ctx=_ctx(config, broker),
        running_deployed_7d=0.0,
    )

    assert outcome["status"] == "submitted"
    assert outcome["order_id"]
    assert errors == 0  # a settlement failure is logged, not counted as a run error


def test_non_whole_share_ticker_is_unaffected_by_the_dispatch(tmp_path, monkeypatch):
    monkeypatch.setenv("DCA_CRON_TRACKING_ROOT", str(tmp_path / "cron_tracking"))
    config = StrategyConfig(whole_share_tickers=frozenset({"BYDDY"}))
    broker = DryRunBroker(cash=500.0, reference_prices={"AAPL": 98.0})
    decision = ReconciledDecision(
        ticker="AAPL",
        action=Action.BUY,
        reason="test",
        finance_signal=None,
        robinhood_signal=None,
        buy_multiplier=1.0,
        suggested_amount_usd=2.0,
        selected_base_amount_usd=1.0,
        target_price=98.0,
    )

    outcome, _, _, _, _ = _process_buy_candidate(
        decision, ctx=_ctx(config, broker, scheduled=False), running_deployed_7d=0.0
    )

    # Normal dollar-amount path -- never touches the whole-share ledger.
    assert outcome["status"] in {"submitted", "dry_run"}
    assert "whole_share_quantity" not in outcome


def test_no_usable_price_blocks_before_touching_the_ledger(tmp_path, monkeypatch):
    monkeypatch.setenv("DCA_CRON_TRACKING_ROOT", str(tmp_path / "cron_tracking"))
    config = StrategyConfig(whole_share_tickers=frozenset({"BYDDY"}))
    broker = DryRunBroker(cash=500.0)

    outcome, _, _, _, _ = _process_buy_candidate(
        _decision(action=Action.NO_ACTION, suggested_amount_usd=0.0, target_price=0.0),
        ctx=_ctx(config, broker),
        running_deployed_7d=0.0,
    )

    assert outcome["status"] == "blocked"
    assert "no usable current price" in outcome["skip_reason"]
    ledger = WholeShareAccumulationLedger()
    assert ledger.pending_usd("BYDDY") == 0.0


def test_kill_switch_caps_the_contribution_to_the_selected_base(tmp_path, monkeypatch):
    """Codex review ("Apply the deployment kill switch to whole-share
    orders"): when the rolling 7-day deployment threshold is engaged, a
    whole-share candidate's contribution must be capped to its own selected
    base ($1/$2), exactly like the normal buy path's own kill-switch cap --
    not the full ladder-multiplied suggested_amount_usd.
    """
    monkeypatch.setenv("DCA_CRON_TRACKING_ROOT", str(tmp_path / "cron_tracking"))
    # Default monthly_cash_allocation_usd=$3600, kill_switch_7d_deploy_fraction=0.40
    # -> threshold $1440; running_deployed_7d above that engages the switch.
    config = StrategyConfig(whole_share_tickers=frozenset({"BYDDY"}), dry_run=False)
    broker = DryRunBroker(cash=500.0)
    decision = ReconciledDecision(
        ticker="BYDDY",
        action=Action.BUY,
        reason="test",
        finance_signal=None,
        robinhood_signal=None,
        buy_multiplier=5.0,
        suggested_amount_usd=10.0,  # $2 base * 5x ladder multiplier
        selected_base_amount_usd=2.0,
        target_price=10.0,
    )

    outcome, _, _, _, _ = _process_buy_candidate(
        decision, ctx=_ctx(config, broker), running_deployed_7d=2000.0
    )

    assert outcome["status"] == "skipped"
    # Capped to the $2 base, not the $10 ladder-multiplied suggestion.
    assert outcome["whole_share_pending_usd"] == 2.0
    ledger = WholeShareAccumulationLedger()
    assert ledger.pending_usd("BYDDY") == 2.0


def test_kill_switch_blocks_accumulated_credit_above_the_selected_base(tmp_path, monkeypatch):
    """An engaged switch caps the order, not only today's contribution."""
    monkeypatch.setenv("DCA_CRON_TRACKING_ROOT", str(tmp_path / "cron_tracking"))
    config = StrategyConfig(whole_share_tickers=frozenset({"BYDDY"}), dry_run=False)
    broker = DryRunBroker(cash=500.0, reference_prices={"BYDDY": 10.0})

    WholeShareAccumulationLedger().record_contribution(
        "BYDDY",
        "2026-08-15",
        8.0,
        10.0,
        recorded_at="2026-08-15T15:45:00+00:00",
        idempotency_key="afternoon-BYDDY",
    )

    decision = ReconciledDecision(
        ticker="BYDDY",
        action=Action.BUY,
        reason="test",
        finance_signal=None,
        robinhood_signal=None,
        buy_multiplier=5.0,
        suggested_amount_usd=10.0,
        selected_base_amount_usd=2.0,
        target_price=10.0,
    )
    outcome, _, running_deployed_7d, errors, _ = _process_buy_candidate(
        decision,
        ctx=_ctx(config, broker),
        # This value can include orders placed earlier in the same batch.
        running_deployed_7d=2000.0,
    )

    assert outcome["status"] == "blocked"
    assert outcome["budget_blocked"] is True
    assert "exceeds max per-order $2.00" in outcome["skip_reason"]
    assert running_deployed_7d == 2000.0
    assert errors == 0
    ledger = WholeShareAccumulationLedger()
    assert ledger.pending_usd("BYDDY") == 10.0
    assert ledger.shares_purchased_total("BYDDY") == 0


def test_kill_switch_does_not_cap_below_the_selected_base_when_disengaged(tmp_path, monkeypatch):
    monkeypatch.setenv("DCA_CRON_TRACKING_ROOT", str(tmp_path / "cron_tracking"))
    config = StrategyConfig(whole_share_tickers=frozenset({"BYDDY"}), dry_run=False)
    broker = DryRunBroker(cash=500.0, reference_prices={"BYDDY": 10.0})
    decision = ReconciledDecision(
        ticker="BYDDY",
        action=Action.BUY,
        reason="test",
        finance_signal=None,
        robinhood_signal=None,
        buy_multiplier=5.0,
        suggested_amount_usd=10.0,
        selected_base_amount_usd=2.0,
        target_price=10.0,
    )

    outcome, _, _, _, _ = _process_buy_candidate(
        decision, ctx=_ctx(config, broker), running_deployed_7d=0.0
    )

    assert outcome["status"] == "submitted"
    assert outcome["whole_share_quantity"] == 1
    ledger = WholeShareAccumulationLedger()
    # A confirmed terminal "filled" order at the reference price (DryRunBroker
    # simulates a fill since reference_prices are supplied) settles the full
    # $10 contribution -- kill switch is not engaged, so nothing is capped.
    assert ledger.pending_usd("BYDDY") == 0.0
    assert ledger.shares_purchased_total("BYDDY") == 1


def test_a_reclaimed_fire_is_not_placed_by_the_fenced_out_caller(tmp_path, monkeypatch):
    """Codex review ("Fence the previous owner before reclaiming stale
    fires"): a stale-claim reclaim by ANOTHER overlapping attempt must stop
    THIS caller from placing the order it no longer exclusively owns, even
    though it already passed every earlier gate. Simulated by having the
    broker's own review call -- which runs between record_contribution and
    placement on a manual (non-scheduled) run -- reclaim the SAME fire with
    a recorded_at far enough in the future to look abandoned.
    """
    monkeypatch.setenv("DCA_CRON_TRACKING_ROOT", str(tmp_path / "cron_tracking"))

    class _ReclaimingBroker(DryRunBroker):
        def review_whole_share_buy(self, ticker, quantity):
            WholeShareAccumulationLedger().record_contribution(
                ticker,
                "2026-08-16",
                10.0,
                10.0,
                stream="afternoon",
                idempotency_key="afternoon-BYDDY",
                # Far enough past the original call's recorded_at to read
                # as an abandoned, reclaimable claim.
                recorded_at="2026-08-16T16:00:00-04:00",
            )
            return True, "reclaimed by another attempt"

    config = StrategyConfig(whole_share_tickers=frozenset({"BYDDY"}), dry_run=False)
    broker = _ReclaimingBroker(cash=500.0, reference_prices={"BYDDY": 10.0})

    outcome, _, _, _, _ = _process_buy_candidate(
        _decision(suggested_amount_usd=10.0, target_price=10.0),
        ctx=_ctx(config, broker, scheduled=False),  # scheduled=False -> review runs
        running_deployed_7d=0.0,
    )

    assert outcome["status"] == "skipped"
    assert "lost the fire claim" in outcome["skip_reason"]
    ledger = WholeShareAccumulationLedger()
    # No placement happened and no shares were recorded as purchased by the
    # fenced-out caller.
    assert ledger.shares_purchased_total("BYDDY") == 0


def test_dry_run_preview_rejects_a_broker_without_whole_share_support(tmp_path, monkeypatch):
    """Codex review ("Reject unsupported brokers in whole-share previews"):
    a dry-run preview must not fabricate a synthetic order_id for a broker
    that cannot actually place a whole-share order -- AlpacaBroker raises
    RuntimeError from place_whole_share_buy, so a dry_run "success" here
    would misrepresent what the identical live run would do.

    AlpacaBroker.is_tradable_for_whole_share_buy() (Codex review: "Verify
    whole-share tradability before scheduled placement") now catches this
    UNCONDITIONALLY, before review_whole_share_buy even runs -- both exist
    for the same reason (AlpacaBroker never actually supports whole-share),
    so either message is a correct block; this asserts the actual (earlier)
    gate.
    """
    monkeypatch.setenv("DCA_CRON_TRACKING_ROOT", str(tmp_path / "cron_tracking"))
    from near_close_dca import AlpacaBroker, AlpacaConfig

    config = StrategyConfig(whole_share_tickers=frozenset({"BYDDY"}), dry_run=True)
    broker = AlpacaBroker(AlpacaConfig())

    outcome, _, _, _, _ = _process_buy_candidate(
        _decision(suggested_amount_usd=10.0, target_price=10.0),
        ctx=_ctx(config, broker),
        running_deployed_7d=0.0,
    )

    assert outcome["status"] == "blocked"
    assert "not tradable" in outcome["skip_reason"]


def test_dry_run_preview_blocks_on_insufficient_buying_power(tmp_path, monkeypatch):
    """Codex review ("Check buying power in whole-share dry-run previews"):
    a dry-run preview must not report success for an order the SAME config
    in live mode would block for insufficient funds.
    """
    monkeypatch.setenv("DCA_CRON_TRACKING_ROOT", str(tmp_path / "cron_tracking"))
    config = StrategyConfig(whole_share_tickers=frozenset({"BYDDY"}), dry_run=True)
    broker = DryRunBroker(cash=5.0, reference_prices={"BYDDY": 10.0})  # cash < order_notional

    outcome, _, _, _, _ = _process_buy_candidate(
        _decision(suggested_amount_usd=10.0, target_price=10.0),
        ctx=_ctx(config, broker),
        running_deployed_7d=0.0,
    )

    assert outcome["status"] == "blocked"
    assert outcome["budget_blocked"] is True
    assert "insufficient buying power" in outcome["skip_reason"]


def test_accepted_order_is_published_before_a_fallible_fill_lookup(tmp_path, monkeypatch):
    """Codex review ("Publish the accepted order before fill lookup"):
    mirrors the normal buy path's identical fix -- a raise from
    get_order_fill (inside _log_execution, called AFTER the broker has
    already accepted the order) must not lose the accepted outcome. The
    order must already be visible via the in-flight publish mechanism the
    batch/failure-handling loop reads from.

    Since the "Pass the verified fill into execution logging" fix (see
    docs/whole_share_accumulation.md), _log_execution no longer queries
    get_order_fill AT ALL when the confirmed order itself carries a usable
    average_price -- so this broker deliberately omits it from the
    confirmed-order payload, forcing the known_fill fallback path (and
    thus this raise) to actually execute.
    """
    monkeypatch.setenv("DCA_CRON_TRACKING_ROOT", str(tmp_path / "cron_tracking"))
    import near_close_dca as _n

    class _RaisingFillBroker(DryRunBroker):
        def same_day_strategy_order_exists_with_ref_id_prefix(self, ticker, trading_day, ref_id_prefixes):
            confirmed = super().same_day_strategy_order_exists_with_ref_id_prefix(
                ticker, trading_day, ref_id_prefixes
            )
            if confirmed is not None:
                confirmed = dict(confirmed)
                confirmed.pop("average_price", None)
            return confirmed

        def get_order_fill(self, order_id):
            raise RuntimeError("fill lookup unavailable")

    config = StrategyConfig(whole_share_tickers=frozenset({"BYDDY"}), dry_run=False)
    broker = _RaisingFillBroker(cash=500.0, reference_prices={"BYDDY": 10.0})
    run_result = _n.RunResult(trading_day=date(2026, 8, 16), decisions=[])

    _n._IN_FLIGHT_CANDIDATE_OUTCOMES["outcomes"] = {}
    try:
        with pytest.raises(RuntimeError):
            _process_buy_candidate(
                _decision(suggested_amount_usd=10.0, target_price=10.0),
                ctx=_ctx(config, broker, run_result=run_result),
                running_deployed_7d=0.0,
            )
        published = dict(_n._IN_FLIGHT_CANDIDATE_OUTCOMES["outcomes"])
    finally:
        _n._IN_FLIGHT_CANDIDATE_OUTCOMES.pop("outcomes", None)

    assert published.get("BYDDY", {}).get("status") == "submitted"
    assert published["BYDDY"]["whole_share_quantity"] == 1


def test_planned_amount_reflects_the_accumulated_notional_when_a_fire_is_blocked(tmp_path, monkeypatch):
    """Codex review ("Persist the actual whole-share planned notional"): a
    fire blocked by a safety gate (here, the max_per_order_usd ceiling)
    must persist the full accumulated order_notional_usd it was about to
    attempt, not the day's own raw contribution.
    """
    monkeypatch.setenv("DCA_CRON_TRACKING_ROOT", str(tmp_path / "cron_tracking"))
    config = StrategyConfig(whole_share_tickers=frozenset({"BYDDY"}), dry_run=False)
    broker = DryRunBroker(cash=500.0, reference_prices={"BYDDY": 60.0})

    decision = _decision(suggested_amount_usd=60.0, target_price=60.0)
    outcome, top_up_outcomes, _, _, _ = _process_buy_candidate(
        decision, ctx=_ctx(config, broker), running_deployed_7d=0.0
    )
    assert outcome["status"] == "blocked"
    assert outcome["whole_share_planned_notional_usd"] == 60.0

    rows = _build_buy_candidates([decision], {"BYDDY": outcome}, {})
    assert rows[0]["planned_amount_usd"] == 60.0


def test_planned_amount_is_zero_for_an_accumulating_only_skip(tmp_path, monkeypatch):
    monkeypatch.setenv("DCA_CRON_TRACKING_ROOT", str(tmp_path / "cron_tracking"))
    config = StrategyConfig(whole_share_tickers=frozenset({"BYDDY"}), dry_run=False)
    broker = DryRunBroker(cash=500.0)

    decision = _decision(suggested_amount_usd=3.0, target_price=10.0)
    outcome, _, _, _, _ = _process_buy_candidate(decision, ctx=_ctx(config, broker), running_deployed_7d=0.0)
    assert outcome["status"] == "skipped"

    rows = _build_buy_candidates([decision], {"BYDDY": outcome}, {})
    assert rows[0]["planned_amount_usd"] == 0.0


def test_build_buy_candidates_persists_whole_share_accumulator_fields():
    """Codex review ("Persist whole-share details in candidate artifacts"):
    _build_buy_candidates must carry a whole-share outcome's accumulator
    detail into the persisted row, or run_state.json/the candidate CSV
    cannot report the accumulated balance or submitted quantity.
    """
    decision = _decision(suggested_amount_usd=3.0, target_price=10.0)
    outcome = {
        "status": "skipped",
        "skip_reason": "whole-share: contribution accumulated, share threshold not yet reached",
        "deliberate_skip": True,
        "whole_share_pending_usd": 7.0,
        "whole_share_contribution_usd": 3.0,
    }

    rows = _build_buy_candidates([decision], {"BYDDY": outcome}, {})

    assert len(rows) == 1
    assert rows[0]["whole_share_pending_usd"] == 7.0
    assert rows[0]["whole_share_contribution_usd"] == 3.0
    assert rows[0]["whole_share_quantity"] is None  # not fired this run


def test_build_buy_candidates_persists_whole_share_quantity_on_fire():
    decision = _decision(suggested_amount_usd=10.0, target_price=10.0)
    outcome = {
        "status": "submitted",
        "order_id": "ORD-1",
        "skip_reason": None,
        "submitted_amount_usd": 10.0,
        "whole_share_quantity": 1,
    }

    rows = _build_buy_candidates([decision], {"BYDDY": outcome}, {})

    assert rows[0]["whole_share_quantity"] == 1


def test_build_buy_candidates_leaves_a_normal_ticker_row_unaffected():
    decision = ReconciledDecision(
        ticker="AAPL",
        action=Action.BUY,
        reason="test",
        finance_signal=None,
        robinhood_signal=None,
        buy_multiplier=1.0,
        suggested_amount_usd=2.0,
        selected_base_amount_usd=1.0,
        target_price=98.0,
    )
    outcome = {"status": "submitted", "order_id": "ORD-2", "skip_reason": None}

    rows = _build_buy_candidates([decision], {"AAPL": outcome}, {})

    # Always present (so the CSV writer's header stays uniform across
    # every row -- see the comment in _build_buy_candidates), but None for
    # a ticker whose outcome never carries whole-share detail.
    assert rows[0]["whole_share_pending_usd"] is None
    assert rows[0]["whole_share_contribution_usd"] is None
    assert rows[0]["whole_share_quantity"] is None


def test_planned_spend_previews_the_accumulated_order_not_the_days_contribution(tmp_path, monkeypatch):
    """Codex review ("Preview accumulated notional before selecting the
    budget tier"): a whole-share candidate's tier-planning estimate must
    reflect what this run will actually try to spend (the accumulated
    order, once it crosses a share's price), not just today's own
    contribution -- otherwise near-boundary runs select the wrong budget
    tier.

    Seeds an $8.00 pending balance, then a 1%-drop BUY signal (no configured
    per-ticker sizing -> the default $2 base * 1x ladder tier = $2
    contribution) at a ~$9.90 share price: the $2.00 contribution alone
    would under-report planned spend versus the ~$9.90 order this run
    actually submits once the accumulated total crosses the share price.
    """
    monkeypatch.setenv("DCA_CRON_TRACKING_ROOT", str(tmp_path / "cron_tracking"))
    ledger = WholeShareAccumulationLedger()
    ledger.record_contribution(
        "BYDDY",
        "2026-08-15",
        8.00,
        1_000_000.0,  # absurdly high price -> 0 shares bought, just seeds pending
        stream="seed",
        recorded_at="2026-08-15T15:30:00-04:00",
        idempotency_key="seed-2026-08-15",
    )
    assert ledger.pending_usd("BYDDY") == 8.00

    now = datetime(2026, 8, 16, 15, 45, tzinfo=EASTERN)
    config = StrategyConfig(
        dry_run=False,
        tickers=(),
        whole_share_tickers=frozenset({"BYDDY"}),
        approved_test_dates=frozenset({date(2026, 8, 16)}),
    )
    state = StrategyState()
    broker = DryRunBroker(cash=500.0, reference_prices={"BYDDY": 9.90})
    data = _FakeSignalData(SourceName.FINANCE, close_price=9.90, prev_close=10.00)
    rh_data = _FakeSignalData(SourceName.ROBINHOOD, close_price=9.90, prev_close=10.00)

    result = run_daily_compact(
        now,
        config,
        state,
        data,
        rh_data,
        broker,
        options=CompactRunOptions(artifacts_dir=str(tmp_path / "artifacts")),
    )

    assert result["blocked"] is False
    import json
    from pathlib import Path

    run_state = json.loads(Path(result["run_state_path"]).read_bytes())
    [candidate] = run_state["buy_candidates"]
    assert candidate["status"] == "submitted"
    assert candidate["whole_share_quantity"] == 1
    # planned_spend_usd must reflect the ~$9.90 order actually attempted,
    # not the day's own $2.00 contribution alone.
    assert run_state["budget"]["planned_spend_usd"] == pytest.approx(9.90, abs=0.01)


def test_planned_spend_preview_is_clamped_to_the_max_per_order_ceiling(tmp_path, monkeypatch):
    """Codex review ("Clamp whole-share previews to executable notional"):
    execution never spends more than max_per_order_usd in one run, however
    large the accumulated balance -- the tier-planning preview must reflect
    that ceiling too, or a large preserved balance can select a needlessly
    high budget tier for spend the whole-share path will never submit.

    $50 pending + a $2 contribution at $4/share previews 13 shares ($52),
    but the default $10 per-order ceiling allows only 2 shares ($8) this
    run.
    """
    monkeypatch.setenv("DCA_CRON_TRACKING_ROOT", str(tmp_path / "cron_tracking"))
    ledger = WholeShareAccumulationLedger()
    ledger.record_contribution(
        "BYDDY",
        "2026-08-15",
        50.00,
        1_000_000.0,
        stream="seed",
        recorded_at="2026-08-15T15:30:00-04:00",
        idempotency_key="seed-2026-08-15",
    )
    assert ledger.pending_usd("BYDDY") == 50.00

    now = datetime(2026, 8, 16, 15, 45, tzinfo=EASTERN)
    config = StrategyConfig(
        dry_run=False,
        tickers=(),
        whole_share_tickers=frozenset({"BYDDY"}),
        approved_test_dates=frozenset({date(2026, 8, 16)}),
    )
    state = StrategyState()
    broker = DryRunBroker(cash=500.0, reference_prices={"BYDDY": 4.00})
    data = _FakeSignalData(SourceName.FINANCE, close_price=3.96, prev_close=4.00)
    rh_data = _FakeSignalData(SourceName.ROBINHOOD, close_price=3.96, prev_close=4.00)

    result = run_daily_compact(
        now,
        config,
        state,
        data,
        rh_data,
        broker,
        options=CompactRunOptions(artifacts_dir=str(tmp_path / "artifacts")),
    )

    assert result["blocked"] is False
    import json
    from pathlib import Path

    run_state = json.loads(Path(result["run_state_path"]).read_bytes())
    # The preview must never exceed what the $10 ceiling allows (2 shares @
    # ~$3.96-$4.00), never the full ~$52 accumulated total.
    assert run_state["budget"]["planned_spend_usd"] <= 10.0


def test_planned_spend_preview_clamps_with_the_ledger_rounded_price(tmp_path, monkeypatch):
    """Codex review ("Use the ledger-rounded price in tier planning"): at a
    fractional-cent share price, the preview's max_shares_by_ceiling must
    clamp using apply_contribution's OWN cent-rounded price, not the raw
    signal price -- execution clamps with the SAME rounded price
    (wsa_decision.current_share_price_usd), so previewing with the raw price
    can understate the actionable plan and select the wrong budget tier.

    At $3.334 raw (rounds to $3.33) with an $8.00 pending balance and a
    $2.00 contribution ($10.00 total): the raw price only fits 2 shares
    under the $10 ceiling (2 * 3.334 = $6.668), but the rounded $3.33 price
    fits 3 ($9.99) -- which is what execution will actually submit.
    """
    monkeypatch.setenv("DCA_CRON_TRACKING_ROOT", str(tmp_path / "cron_tracking"))
    ledger = WholeShareAccumulationLedger()
    ledger.record_contribution(
        "BYDDY",
        "2026-08-15",
        8.00,
        1_000_000.0,
        stream="seed",
        recorded_at="2026-08-15T15:30:00-04:00",
        idempotency_key="seed-2026-08-15",
    )
    assert ledger.pending_usd("BYDDY") == 8.00

    now = datetime(2026, 8, 16, 15, 45, tzinfo=EASTERN)
    config = StrategyConfig(
        dry_run=False,
        tickers=(),
        whole_share_tickers=frozenset({"BYDDY"}),
        approved_test_dates=frozenset({date(2026, 8, 16)}),
    )
    state = StrategyState()
    broker = DryRunBroker(cash=500.0, reference_prices={"BYDDY": 3.334})
    data = _FakeSignalData(SourceName.FINANCE, close_price=3.334, prev_close=3.3677)
    rh_data = _FakeSignalData(SourceName.ROBINHOOD, close_price=3.334, prev_close=3.3677)

    result = run_daily_compact(
        now,
        config,
        state,
        data,
        rh_data,
        broker,
        options=CompactRunOptions(artifacts_dir=str(tmp_path / "artifacts")),
    )

    assert result["blocked"] is False
    import json
    from pathlib import Path

    run_state = json.loads(Path(result["run_state_path"]).read_bytes())
    [candidate] = run_state["buy_candidates"]
    assert candidate["status"] == "submitted"
    assert candidate["whole_share_quantity"] == 3
    # The buggy raw-price clamp would have previewed ~$6.67 (2 shares); the
    # fix previews the actual ~$9.99 (3 shares) execution submits.
    assert run_state["budget"]["planned_spend_usd"] == pytest.approx(9.99, abs=0.01)


def test_planned_spend_preview_does_not_double_count_an_already_recorded_slot(tmp_path, monkeypatch):
    """Codex review ("Make tier previews honor resolved ledger
    contributions"): on a same-day retry (e.g. a scheduler re-invocation)
    after a below-threshold contribution was already recorded for THIS
    exact (trading_day, stream, ticker) slot, pending_usd already reflects
    that contribution -- re-adding suggested_amount_usd on top overstates
    the preview and can select the wrong budget tier, unlike the real
    (authoritative) record_contribution() call, which is idempotent per
    slot and correctly returns a zero-shares decision for the retry.

    Seeds pending to $7, then records TODAY's $2 contribution under the
    real (stream="afternoon", idempotency_key="afternoon-BYDDY") key the
    run itself will use -- simulating that this exact slot already ran once
    this run and is now being retried. The buggy preview would re-add $2 on
    top of the already-updated $9 pending balance (previewing ~$11 -- an
    order); the fix recognizes the slot as already recorded and previews
    the current $9 balance alone (still $0 -- no order, matching the real
    decision this retry will get from record_contribution()).
    """
    monkeypatch.setenv("DCA_CRON_TRACKING_ROOT", str(tmp_path / "cron_tracking"))
    ledger = WholeShareAccumulationLedger()
    ledger.record_contribution(
        "BYDDY",
        "2026-08-15",
        7.00,
        1_000_000.0,
        stream="seed",
        recorded_at="2026-08-15T15:30:00-04:00",
        idempotency_key="seed-2026-08-15",
    )
    assert ledger.pending_usd("BYDDY") == 7.00

    now = datetime(2026, 8, 16, 15, 45, tzinfo=EASTERN)
    # Records TODAY's own $2 contribution under the exact key run_daily_compact's
    # tier-planning loop and _process_whole_share_candidate will both use --
    # simulating an earlier attempt this same day already ran and left the
    # slot recorded (resolved, since 0 shares qualify).
    ledger.record_contribution(
        "BYDDY",
        "2026-08-16",
        2.00,
        10.0,
        stream="afternoon",
        recorded_at="2026-08-16T15:30:00-04:00",
        idempotency_key="afternoon-BYDDY",
    )
    assert ledger.pending_usd("BYDDY") == 9.00

    config = StrategyConfig(
        dry_run=False,
        tickers=(),
        whole_share_tickers=frozenset({"BYDDY"}),
        approved_test_dates=frozenset({date(2026, 8, 16)}),
    )
    state = StrategyState()
    broker = DryRunBroker(cash=500.0, reference_prices={"BYDDY": 10.0})
    data = _FakeSignalData(SourceName.FINANCE, close_price=9.90, prev_close=10.00)
    rh_data = _FakeSignalData(SourceName.ROBINHOOD, close_price=9.90, prev_close=10.00)

    result = run_daily_compact(
        now,
        config,
        state,
        data,
        rh_data,
        broker,
        options=CompactRunOptions(artifacts_dir=str(tmp_path / "artifacts")),
    )

    assert result["blocked"] is False
    import json
    from pathlib import Path

    run_state = json.loads(Path(result["run_state_path"]).read_bytes())
    [candidate] = run_state["buy_candidates"]
    assert candidate["status"] == "skipped"
    # The buggy preview would have shown ~$11 (double-counted); the fix
    # shows $0 -- no order, matching what record_contribution() actually
    # returns for this already-recorded retry.
    assert run_state["budget"]["planned_spend_usd"] == 0.0


def test_planned_spend_preview_does_not_recompute_a_preserved_already_recorded_slot(tmp_path, monkeypatch):
    """Codex review ("Mirror cached ledger outcomes in tier previews"): when
    an already-recorded slot's fire was blocked and its full deduction
    restored via preserve_pending (not just resolved-as-below-threshold),
    the restored pending balance can now be big enough to cover a share on
    its own. The real record_contribution() call still returns a
    zero-shares decision for this exact, already-resolved key regardless of
    the current balance -- previewing via apply_contribution(pending, 0,
    price) instead of short-circuiting entirely would show a phantom
    positive order the real retry never actually submits.
    """
    monkeypatch.setenv("DCA_CRON_TRACKING_ROOT", str(tmp_path / "cron_tracking"))
    ledger = WholeShareAccumulationLedger()
    # A fire recorded and then blocked/restored in full -- the SAME slot
    # (stream="afternoon", idempotency_key="afternoon-BYDDY") the run itself
    # will use, already resolved by the time tier planning previews it.
    decision = ledger.record_contribution(
        "BYDDY",
        "2026-08-16",
        10.00,
        10.0,
        stream="afternoon",
        recorded_at="2026-08-16T15:30:00-04:00",
        idempotency_key="afternoon-BYDDY",
    )
    ledger.preserve_pending(
        "BYDDY",
        "2026-08-16",
        10.00,
        stream="afternoon",
        recorded_at="2026-08-16T15:30:00-04:00",
        idempotency_key="afternoon-BYDDY",
        claim_token=decision.claim_token,
    )
    # The restored balance alone now covers (and exceeds) a full share.
    assert ledger.pending_usd("BYDDY") == 10.00

    now = datetime(2026, 8, 16, 15, 45, tzinfo=EASTERN)
    config = StrategyConfig(
        dry_run=False,
        tickers=(),
        whole_share_tickers=frozenset({"BYDDY"}),
        approved_test_dates=frozenset({date(2026, 8, 16)}),
    )
    state = StrategyState()
    broker = DryRunBroker(cash=500.0, reference_prices={"BYDDY": 10.0})
    data = _FakeSignalData(SourceName.FINANCE, close_price=9.90, prev_close=10.00)
    rh_data = _FakeSignalData(SourceName.ROBINHOOD, close_price=9.90, prev_close=10.00)

    result = run_daily_compact(
        now,
        config,
        state,
        data,
        rh_data,
        broker,
        options=CompactRunOptions(artifacts_dir=str(tmp_path / "artifacts")),
    )

    assert result["blocked"] is False
    import json
    from pathlib import Path

    run_state = json.loads(Path(result["run_state_path"]).read_bytes())
    # The buggy preview would have shown a full $9.90-ish order (10.00
    # restored pending / $10 price = 1 share); the fix previews $0, matching
    # record_contribution()'s zero-shares decision for this resolved key.
    assert run_state["budget"]["planned_spend_usd"] == 0.0


def test_planned_spend_preview_reflects_an_actionable_stale_claim(tmp_path, monkeypatch):
    """Codex review ("Account for actionable stale claims in tier
    planning"): an UNRESOLVED fire older than UNRESOLVED_CLAIM_STALE_SECONDS
    is reclaimable -- record_contribution() would RECLAIM it and return its
    cached POSITIVE decision, not zero. The already_recorded()-only preview
    flattened this to $0, understating what this run's own
    _process_whole_share_candidate call will actually attempt (and may need
    daily/weekly cap headroom for) once it reclaims and submits the SAME
    fire.
    """
    monkeypatch.setenv("DCA_CRON_TRACKING_ROOT", str(tmp_path / "cron_tracking"))
    ledger = WholeShareAccumulationLedger()
    # Recorded, unresolved, and stale (>5 minutes before the run's "now")
    # under the SAME slot _process_whole_share_candidate will use.
    ledger.record_contribution(
        "BYDDY",
        "2026-08-16",
        10.00,
        10.0,
        stream="afternoon",
        recorded_at="2026-08-16T15:30:00-04:00",
        idempotency_key="afternoon-BYDDY",
    )
    assert ledger.pending_usd("BYDDY") == 0.0  # deducted at record time

    now = datetime(2026, 8, 16, 15, 45, tzinfo=EASTERN)  # 15 min later -> stale
    config = StrategyConfig(
        dry_run=False,
        tickers=(),
        whole_share_tickers=frozenset({"BYDDY"}),
        approved_test_dates=frozenset({date(2026, 8, 16)}),
    )
    state = StrategyState()
    # Reference price matches the CURRENT $9.90 quote, not the original
    # $10.00 claim price -- a real broker fills at the current market price,
    # not the stale signal-time price the claim was originally sized
    # against, and the ordinary success path now books that VERIFIED fill
    # price/quantity rather than the locally-estimated notional (see
    # near_close_dca._process_whole_share_candidate's "Book the verified
    # fill price on ordinary successful orders" fix).
    broker = DryRunBroker(cash=500.0, reference_prices={"BYDDY": 9.90})
    data = _FakeSignalData(SourceName.FINANCE, close_price=9.90, prev_close=10.00)
    rh_data = _FakeSignalData(SourceName.ROBINHOOD, close_price=9.90, prev_close=10.00)

    result = run_daily_compact(
        now,
        config,
        state,
        data,
        rh_data,
        broker,
        options=CompactRunOptions(artifacts_dir=str(tmp_path / "artifacts")),
    )

    assert result["blocked"] is False
    import json
    from pathlib import Path

    run_state = json.loads(Path(result["run_state_path"]).read_bytes())
    [candidate] = run_state["buy_candidates"]
    # The real call reclaims the stale fire and submits it for real, repriced
    # against the CURRENT $9.90 quote (Codex review: "Reprice reclaimed stale
    # fires before placement") rather than the original $10.00 claim price --
    # still 1 share, since $10 total still covers a $9.90 share.
    assert candidate["status"] == "submitted"
    assert candidate["whole_share_quantity"] == 1
    # The preview must have anticipated this ~$9.90 order, not flattened it to $0.
    assert run_state["budget"]["planned_spend_usd"] == pytest.approx(9.90, abs=0.01)


def test_dry_run_preview_honors_an_already_recorded_slot(tmp_path, monkeypatch):
    """Codex review ("Honor slot idempotency in dry-run previews"): a
    dry-run preview for a slot that already recorded a REAL contribution
    (e.g. a --demo re-run after a genuine scheduled run) must not re-apply
    contribution_usd on top of pending_usd -- record_contribution() is
    idempotent per (trading_day, idempotency_key) and would return a
    zero-shares decision for this exact retry regardless of the current
    balance, so the dry-run artifact must match that, not preview a
    plausible order the identical live retry never actually submits.
    """
    monkeypatch.setenv("DCA_CRON_TRACKING_ROOT", str(tmp_path / "cron_tracking"))
    ledger = WholeShareAccumulationLedger()
    ledger.record_contribution(
        "BYDDY",
        "2026-08-16",
        9.00,
        10.0,
        stream="afternoon",
        recorded_at="2026-08-16T15:30:00-04:00",
        idempotency_key="afternoon-BYDDY",
    )
    assert ledger.pending_usd("BYDDY") == 9.00

    config = StrategyConfig(whole_share_tickers=frozenset({"BYDDY"}), dry_run=True)
    broker = DryRunBroker(cash=500.0, reference_prices={"BYDDY": 10.0})

    outcome, _, _, _, _ = _process_buy_candidate(
        _decision(suggested_amount_usd=2.0, target_price=10.0),
        ctx=_ctx(config, broker),
        running_deployed_7d=0.0,
    )

    # The buggy preview would add the $2.0 contribution on top of the
    # already-recorded $9.00 (previewing an $11 order, 1 share); the fix
    # recognizes the slot as already recorded and reports the unchanged
    # $9.00 balance with no order, matching what record_contribution()
    # itself would return for this exact retry.
    assert outcome["status"] == "skipped"
    assert outcome["whole_share_pending_usd"] == 9.00
    assert ledger.pending_usd("BYDDY") == 9.00  # dry run still never mutates


def test_tradability_lookup_failure_restores_pending_and_counts_an_error(tmp_path, monkeypatch):
    """Codex review ("Restore the deduction when tradability lookup
    raises"): record_contribution() has already deducted the full planned
    notional by the time is_tradable_for_whole_share_buy() runs -- a raise
    from that lookup (e.g. a broker API/network failure) must not escape
    without restoring it, or the batch aborts with no order submitted AND
    the accumulator permanently short by the unplaced amount.
    """
    monkeypatch.setenv("DCA_CRON_TRACKING_ROOT", str(tmp_path / "cron_tracking"))

    class _RaisingTradabilityBroker(DryRunBroker):
        def is_tradable_for_whole_share_buy(self, ticker):
            raise RuntimeError("tradability lookup unavailable")

    config = StrategyConfig(whole_share_tickers=frozenset({"BYDDY"}), dry_run=False)
    broker = _RaisingTradabilityBroker(cash=500.0, reference_prices={"BYDDY": 10.0})

    outcome, _, _, errors, _ = _process_buy_candidate(
        _decision(suggested_amount_usd=10.0, target_price=10.0),
        ctx=_ctx(config, broker),
        running_deployed_7d=0.0,
    )

    assert outcome["status"] == "blocked"
    assert "tradability lookup failed" in outcome["skip_reason"]
    assert errors == 1
    ledger = WholeShareAccumulationLedger()
    assert ledger.pending_usd("BYDDY") == 10.0


def test_buying_power_lookup_failure_restores_pending_and_counts_an_error(tmp_path, monkeypatch):
    """Codex review ("Restore pending credit when buying-power lookup
    fails"): record_contribution() has already deducted the full planned
    notional by the time available_buying_power() runs -- a raise from that
    lookup (broker/API outage) must not escape without restoring it, the
    same way the tradability-lookup failure just above does.
    """
    monkeypatch.setenv("DCA_CRON_TRACKING_ROOT", str(tmp_path / "cron_tracking"))

    class _RaisingBuyingPowerBroker(DryRunBroker):
        def available_buying_power(self):
            raise RuntimeError("buying power lookup unavailable")

    config = StrategyConfig(whole_share_tickers=frozenset({"BYDDY"}), dry_run=False)
    broker = _RaisingBuyingPowerBroker(cash=500.0, reference_prices={"BYDDY": 10.0})

    outcome, _, _, errors, _ = _process_buy_candidate(
        _decision(suggested_amount_usd=10.0, target_price=10.0),
        ctx=_ctx(config, broker),
        running_deployed_7d=0.0,
    )

    assert outcome["status"] == "blocked"
    assert "buying power lookup failed" in outcome["skip_reason"]
    assert errors == 1
    ledger = WholeShareAccumulationLedger()
    assert ledger.pending_usd("BYDDY") == 10.0


def test_max_shares_by_ceiling_uses_exact_cent_arithmetic(tmp_path, monkeypatch):
    """Codex review ("Compute the share ceiling with exact cent
    arithmetic"): a cent-rounded price like $0.10 is frequently not
    representable exactly as a binary float, so `10.0 // 0.10` evaluates to
    99.0 in raw Python float division, not 100.0 -- unnecessarily clamping a
    fully-affordable fire by one share. $50 pending at $0.10/share should
    fire all 100 shares under a $10 per-order ceiling in one run (clamped to
    the ceiling, not 99).
    """
    monkeypatch.setenv("DCA_CRON_TRACKING_ROOT", str(tmp_path / "cron_tracking"))
    config = StrategyConfig(
        whole_share_tickers=frozenset({"BYDDY"}), dry_run=False, max_per_order_usd=10.0
    )
    broker = DryRunBroker(cash=500.0, reference_prices={"BYDDY": 0.10})

    outcome, _, _, _, _ = _process_buy_candidate(
        _decision(suggested_amount_usd=50.0, target_price=0.10),
        ctx=_ctx(config, broker),
        running_deployed_7d=0.0,
    )

    assert outcome["status"] == "submitted"
    assert outcome["whole_share_quantity"] == 100
    assert outcome["submitted_amount_usd"] == 10.00


def test_a_corrupt_whole_share_ledger_does_not_abort_planning_for_other_tickers(tmp_path, monkeypatch):
    """Codex review ("Isolate ledger preview failures to the whole-share
    ticker"): a corrupt/unreadable BYDDY ledger must not raise out of the
    batch-wide tier-planning loop and abort every other, independent
    active-ticker candidate -- only BYDDY's own preview should degrade (to a
    $0 planned-spend contribution, matching what
    _process_whole_share_candidate's own identical ledger-error handling
    does for the real, authoritative decision).
    """
    tracking_root = tmp_path / "cron_tracking"
    monkeypatch.setenv("DCA_CRON_TRACKING_ROOT", str(tracking_root))
    ledger_path = tracking_root / "whole_share_accumulation" / "BYDDY" / "ledger.json"
    ledger_path.parent.mkdir(parents=True, exist_ok=True)
    ledger_path.write_text("not valid json{{{")

    now = datetime(2026, 8, 16, 15, 45, tzinfo=EASTERN)
    config = StrategyConfig(
        dry_run=False,
        tickers=("AAA",),
        whole_share_tickers=frozenset({"BYDDY"}),
        approved_test_dates=frozenset({date(2026, 8, 16)}),
    )
    state = StrategyState()
    broker = DryRunBroker(cash=500.0, reference_prices={"AAA": 9.90, "BYDDY": 9.90})
    data = _FakeSignalData(SourceName.FINANCE, close_price=9.90, prev_close=10.00)
    rh_data = _FakeSignalData(SourceName.ROBINHOOD, close_price=9.90, prev_close=10.00)

    result = run_daily_compact(
        now,
        config,
        state,
        data,
        rh_data,
        broker,
        options=CompactRunOptions(artifacts_dir=str(tmp_path / "artifacts")),
    )

    assert result["blocked"] is False
    import json
    from pathlib import Path

    run_state = json.loads(Path(result["run_state_path"]).read_bytes())
    statuses = {c["ticker"]: c["status"] for c in run_state["buy_candidates"]}
    assert statuses.get("AAA") == "submitted"


def test_whole_share_evaluated_tickers_are_in_the_coverage_universe(tmp_path, monkeypatch):
    """Codex review ("Include unfilled whole-share orders in coverage
    reporting"): build_report()'s missing_fills loop walks only
    run_result.active_universe, so a whole-share ticker's accepted-but-
    unfilled order would silently disappear from coverage unless
    active_universe includes it too, not just the normal tradable universe.
    """
    monkeypatch.setenv("DCA_CRON_TRACKING_ROOT", str(tmp_path / "cron_tracking"))
    now = datetime(2026, 8, 16, 15, 45, tzinfo=EASTERN)
    config = StrategyConfig(
        dry_run=True,
        tickers=("AAA",),
        whole_share_tickers=frozenset({"BYDDY"}),
        approved_test_dates=frozenset({date(2026, 8, 16)}),
    )
    state = StrategyState()
    broker = DryRunBroker(cash=500.0, reference_prices={"AAA": 9.90, "BYDDY": 9.90})
    data = _FakeSignalData(SourceName.FINANCE, close_price=9.90, prev_close=10.00)
    rh_data = _FakeSignalData(SourceName.ROBINHOOD, close_price=9.90, prev_close=10.00)

    run_result = RunResult(trading_day=date(2026, 8, 16), decisions=[])
    ctx = RunContext(
        now=now,
        config=config,
        state=state,
        finance_data=data,
        robinhood_data=rh_data,
        broker=broker,
        artifacts_dir=str(tmp_path / "artifacts"),
        run_result=run_result,
        stream="afternoon",
    )
    run_once(ctx)

    assert "AAA" in run_result.active_universe
    assert "BYDDY" in run_result.active_universe


@pytest.mark.parametrize("stream", ["morning", "midday"])
def test_whole_share_tickers_are_not_evaluated_outside_afternoon(stream, tmp_path, monkeypatch):
    """Morning and midday slots must not contribute to the once-daily ledger."""
    monkeypatch.setenv("DCA_CRON_TRACKING_ROOT", str(tmp_path / "cron_tracking"))
    now = datetime(2026, 8, 16, 15, 45, tzinfo=EASTERN)
    config = StrategyConfig(
        dry_run=False,
        tickers=(),
        whole_share_tickers=frozenset({"BYDDY"}),
        approved_test_dates=frozenset({date(2026, 8, 16)}),
        target_account_number="999000111",
    )
    run_result = RunResult(trading_day=date(2026, 8, 16), decisions=[])
    ctx = RunContext(
        now=now,
        config=config,
        state=StrategyState(),
        finance_data=_FakeSignalData(SourceName.FINANCE, close_price=9.90, prev_close=10.00),
        robinhood_data=_FakeSignalData(SourceName.ROBINHOOD, close_price=9.90, prev_close=10.00),
        broker=DryRunBroker(cash=500.0, reference_prices={"BYDDY": 9.90}),
        artifacts_dir=str(tmp_path / "artifacts"),
        run_result=run_result,
        stream=stream,
    )

    run_once(ctx)

    assert "BYDDY" not in run_result.active_universe
    assert all(decision.ticker != "BYDDY" for decision in run_result.decisions)
    assert WholeShareAccumulationLedger().pending_usd("BYDDY") == 0.0


def test_accumulator_only_run_is_not_misclassified_as_budget_exhausted(tmp_path, monkeypatch):
    """Codex review ("Classify accumulator-only runs without a budget
    failure"): a run where every BUY candidate is a whole-share ticker still
    below its share threshold must not be reported as
    'not_submitted_budget_exhausted' -- nothing was actually budget-blocked,
    the contribution was cleanly accumulated. near_close_dca's own
    _apply_no_order_classification (called right after
    _derive_order_submit_status, using each candidate outcome's
    'budget_blocked' flag -- never set on an accumulating whole-share skip)
    already reclassifies this to 'no_orders_submitted' /
    is_trading_failure=False; this test locks that behavior in.
    """
    monkeypatch.setenv("DCA_CRON_TRACKING_ROOT", str(tmp_path / "cron_tracking"))
    now = datetime(2026, 8, 16, 15, 45, tzinfo=EASTERN)
    config = StrategyConfig(
        dry_run=False,
        tickers=(),
        whole_share_tickers=frozenset({"BYDDY"}),
        approved_test_dates=frozenset({date(2026, 8, 16)}),
        target_account_number="999000111",
    )
    state = StrategyState()
    broker = DryRunBroker(cash=500.0, reference_prices={"BYDDY": 9.90})
    data = _FakeSignalData(SourceName.FINANCE, close_price=9.90, prev_close=10.00)
    rh_data = _FakeSignalData(SourceName.ROBINHOOD, close_price=9.90, prev_close=10.00)

    # $9.00 already pending; today's $2.00 contribution is still short of the
    # $9.90 share price, so this run only accumulates -- no order fires.
    ledger = WholeShareAccumulationLedger()
    ledger.record_contribution(
        "BYDDY",
        "2026-08-15",
        1.00,
        1_000_000.0,
        stream="seed",
        recorded_at="2026-08-15T15:30:00-04:00",
        idempotency_key="seed-2026-08-15",
    )

    result = run_daily_compact(
        now,
        config,
        state,
        data,
        rh_data,
        broker,
        options=CompactRunOptions(artifacts_dir=str(tmp_path / "artifacts")),
    )

    assert result["blocked"] is False
    import json
    from pathlib import Path

    run_state = json.loads(Path(result["run_state_path"]).read_bytes())
    [candidate] = run_state["buy_candidates"]
    assert candidate["status"] == "skipped"
    assert run_state["order_submit_status"] == "no_orders_submitted"
    assert run_state["is_trading_failure"] is False


def test_review_failure_restores_pending_and_counts_an_error(tmp_path, monkeypatch):
    """Codex review ("Restore the ledger when manual review raises"): a
    manual run's review_whole_share_buy() call itself can raise (a transient
    broker API outage) -- record_contribution() has already deducted the
    full planned notional by this point, so a raise here must not escape
    without restoring it, the same way the tradability/buying-power lookup
    failures do.
    """
    monkeypatch.setenv("DCA_CRON_TRACKING_ROOT", str(tmp_path / "cron_tracking"))

    class _RaisingReviewBroker(DryRunBroker):
        def review_whole_share_buy(self, ticker, quantity):
            raise RuntimeError("review lookup unavailable")

    config = StrategyConfig(whole_share_tickers=frozenset({"BYDDY"}), dry_run=False)
    broker = _RaisingReviewBroker(cash=500.0, reference_prices={"BYDDY": 10.0})

    outcome, _, _, errors, order_reviews_attempted = _process_buy_candidate(
        _decision(suggested_amount_usd=10.0, target_price=10.0),
        ctx=_ctx(config, broker, scheduled=False),  # non-scheduled -> review path runs
        running_deployed_7d=0.0,
    )

    assert outcome["status"] == "blocked"
    assert "review failed" in outcome["skip_reason"]
    assert errors == 1
    assert order_reviews_attempted == 1
    ledger = WholeShareAccumulationLedger()
    assert ledger.pending_usd("BYDDY") == 10.0


def test_duplicate_lookup_failure_before_placement_restores_pending_and_counts_an_error(tmp_path, monkeypatch):
    """Codex review ("Restore the fire when the final duplicate lookup
    raises"): the SECOND, pre-placement same_day_strategy_order_exists()
    recheck (immediately before place_whole_share_buy) can itself raise --
    record_contribution() has already deducted the full planned notional by
    this point, so this is a known pre-placement failure (no order placed
    by anyone) and must always be safe to preserve, unlike a successful
    duplicate MATCH (a genuine reclaim, where preserving would be wrong).
    """
    monkeypatch.setenv("DCA_CRON_TRACKING_ROOT", str(tmp_path / "cron_tracking"))

    class _RaisingOnSecondCallBroker(DryRunBroker):
        def __init__(self, *args, **kwargs):
            super().__init__(*args, **kwargs)
            self._same_day_calls = 0

        def same_day_strategy_order_exists(self, ticker, trading_day):
            self._same_day_calls += 1
            if self._same_day_calls > 1:
                raise RuntimeError("duplicate order lookup unavailable")
            return False

    config = StrategyConfig(whole_share_tickers=frozenset({"BYDDY"}), dry_run=False)
    broker = _RaisingOnSecondCallBroker(cash=500.0, reference_prices={"BYDDY": 10.0})

    outcome, _, _, errors, _ = _process_buy_candidate(
        _decision(suggested_amount_usd=10.0, target_price=10.0),
        ctx=_ctx(config, broker, scheduled=True),
        running_deployed_7d=0.0,
    )

    assert outcome["status"] == "blocked"
    assert "same-day duplicate order lookup failed" in outcome["skip_reason"]
    assert errors == 1
    ledger = WholeShareAccumulationLedger()
    assert ledger.pending_usd("BYDDY") == 10.0


def test_initial_duplicate_lookup_failure_is_isolated_and_touches_no_ledger_state(tmp_path, monkeypatch):
    """Codex review ("Isolate the initial duplicate-order lookup failure"):
    the FIRST, top-of-function same_day_strategy_order_exists() check -- run
    BEFORE record_contribution ever touches the ledger -- can itself raise
    (a transient Robinhood/API outage). Without an equivalent boundary here,
    the exception would escape _process_buy_candidates_batch entirely,
    aborting the WHOLE run and preventing outcomes/artifacts for every
    other, independent candidate still to be processed.
    """
    monkeypatch.setenv("DCA_CRON_TRACKING_ROOT", str(tmp_path / "cron_tracking"))

    class _RaisingOnFirstCallBroker(DryRunBroker):
        def same_day_strategy_order_exists(self, ticker, trading_day):
            raise RuntimeError("duplicate order lookup unavailable")

    config = StrategyConfig(whole_share_tickers=frozenset({"BYDDY"}), dry_run=False)
    broker = _RaisingOnFirstCallBroker(cash=500.0, reference_prices={"BYDDY": 10.0})

    outcome, _, _, errors, _ = _process_buy_candidate(
        _decision(suggested_amount_usd=10.0, target_price=10.0),
        ctx=_ctx(config, broker, scheduled=True),
        running_deployed_7d=0.0,
    )

    assert outcome["status"] == "blocked"
    assert "same-day duplicate order lookup failed" in outcome["skip_reason"]
    assert errors == 1
    # No contribution was ever recorded -- this check runs before the ledger
    # is touched at all.
    ledger = WholeShareAccumulationLedger()
    assert ledger.pending_usd("BYDDY") == 0.0
    assert ledger.shares_purchased_total("BYDDY") == 0


def test_claim_verification_failure_is_isolated_without_mutating_the_ledger(tmp_path, monkeypatch):
    """Codex review ("Isolate claim-verification ledger failures"): the
    still_claimed() recheck immediately before placement can itself raise
    (a corrupt/unreadable ledger or a lock timeout) -- record_contribution
    has already deducted the planned fire by this point. Unlike the OTHER
    ledger-error boundaries in this function, this failure must NOT call
    preserve_pending(): if a reclaiming caller actually owns the claim now,
    preserving here would incorrectly restore money that caller may yet
    spend. The candidate must simply fail closed, leaving the claim's
    resolution to whoever can actually verify it.
    """
    monkeypatch.setenv("DCA_CRON_TRACKING_ROOT", str(tmp_path / "cron_tracking"))
    config = StrategyConfig(whole_share_tickers=frozenset({"BYDDY"}), dry_run=False)
    broker = DryRunBroker(cash=500.0, reference_prices={"BYDDY": 10.0})

    def _raising_still_claimed(self, ticker, trading_day, idempotency_key, now):
        raise RuntimeError("ledger lock timeout")

    monkeypatch.setattr(WholeShareAccumulationLedger, "still_claimed", _raising_still_claimed)

    outcome, _, _, errors, _ = _process_buy_candidate(
        _decision(suggested_amount_usd=10.0, target_price=10.0),
        ctx=_ctx(config, broker, scheduled=True),
        running_deployed_7d=0.0,
    )

    assert outcome["status"] == "blocked"
    assert "claim verification failed" in outcome["skip_reason"]
    assert errors == 1
    # The deducted fire is left exactly as record_contribution left it --
    # neither settled nor restored, since ownership of the claim could not
    # be established. pending_usd()/shares_purchased_total() don't call
    # still_claimed(), so reading them below is unaffected by the patch
    # still being in place (monkeypatch reverts it at test teardown).
    ledger = WholeShareAccumulationLedger()
    assert ledger.pending_usd("BYDDY") == 0.0
    assert ledger.shares_purchased_total("BYDDY") == 0


def test_preserve_pending_failure_while_restoring_a_blocked_fire_is_isolated(tmp_path, monkeypatch):
    """Codex review ("Isolate failures while restoring blocked fires"): when
    a normal pre-placement gate (here, the max_per_order_usd ceiling) blocks
    a claimed fire, _preserve_and_block's own ledger.preserve_pending() call
    can itself raise (a lock timeout, unreadable ledger, or write failure) --
    the same failure-isolation problem already handled for still_claimed().
    Without this boundary, the exception would escape
    _process_buy_candidates_batch entirely, aborting unrelated candidates
    while this ticker gets no data-error outcome and its deducted fire stays
    unresolved with no record of why.
    """
    monkeypatch.setenv("DCA_CRON_TRACKING_ROOT", str(tmp_path / "cron_tracking"))
    # $50 share price exceeds the default $10 max_per_order_usd ceiling --
    # blocked before any broker round-trip, reaching _preserve_and_block.
    config = StrategyConfig(whole_share_tickers=frozenset({"BYDDY"}), dry_run=False)
    broker = DryRunBroker(cash=500.0, reference_prices={"BYDDY": 60.0})

    def _raising_preserve_pending(self, *args, **kwargs):
        raise RuntimeError("ledger lock timeout")

    monkeypatch.setattr(WholeShareAccumulationLedger, "preserve_pending", _raising_preserve_pending)

    outcome, _, _, errors, _ = _process_buy_candidate(
        _decision(suggested_amount_usd=60.0, target_price=60.0),
        ctx=_ctx(config, broker),
        running_deployed_7d=0.0,
    )

    assert outcome["status"] == "blocked"
    assert "failed to restore pending balance" in outcome["skip_reason"]
    assert errors == 1


def test_preserve_pending_failure_outcome_retains_a_whole_share_marker_for_redispatch(tmp_path, monkeypatch):
    """Codex review ("Retain the whole-share marker when restoration
    fails"): when a NO_ACTION preserved-fire redispatch (see
    docs/whole_share_accumulation.md's redispatch section) reaches a safety
    gate and preserve_pending() itself raises, the replacement outcome
    _preserve_and_block returns must still carry a "whole_share_"-prefixed
    field -- otherwise _build_buy_candidates's non-BUY filter (only a
    "whole_share_"-marked or pending-credit-flush outcome survives a non-BUY
    decision) drops this candidate entirely from buy_candidates.csv/
    run_state.json, losing the restoration error and planned notional even
    though errors_count_inc correctly counted the failure.
    """
    monkeypatch.setenv("DCA_CRON_TRACKING_ROOT", str(tmp_path / "cron_tracking"))
    # Seed an existing $50 pending balance so a NO_ACTION redispatch (today's
    # own contribution is $0.0 for a non-BUY decision) still has an
    # actionable fire to dispatch.
    ledger = WholeShareAccumulationLedger()
    # Seeded at a deliberately huge price so the seed call itself buys 0
    # shares, leaving the full $50 pending (rather than immediately firing).
    ledger.record_contribution(
        "BYDDY", "2026-08-16", 60.0, 1_000_000.0, stream="seed", idempotency_key="seed"
    )
    assert ledger.pending_usd("BYDDY") == 60.0

    config = StrategyConfig(whole_share_tickers=frozenset({"BYDDY"}), dry_run=False)
    # $50 share price exceeds the default $10 max_per_order_usd ceiling --
    # blocked before any broker round-trip, reaching _preserve_and_block.
    broker = DryRunBroker(cash=500.0, reference_prices={"BYDDY": 60.0})

    def _raising_preserve_pending(self, *args, **kwargs):
        raise RuntimeError("ledger lock timeout")

    monkeypatch.setattr(WholeShareAccumulationLedger, "preserve_pending", _raising_preserve_pending)

    outcome, _, _, errors, _ = _process_buy_candidate(
        _decision(action=Action.NO_ACTION, suggested_amount_usd=0.0, target_price=60.0),
        ctx=_ctx(config, broker),
        running_deployed_7d=0.0,
    )

    assert outcome["status"] == "blocked"
    assert "failed to restore pending balance" in outcome["skip_reason"]
    assert errors == 1
    assert any(key.startswith("whole_share_") for key in outcome)


def test_placement_failure_restoration_failure_is_isolated(tmp_path, monkeypatch):
    """Codex review ("Isolate restoration errors after placement failures"):
    when place_whole_share_buy() raises AND the subsequent
    ledger.preserve_pending() restoration also fails (a lock timeout,
    corrupt ledger, or write error), the second exception must not replace
    the original broker failure and escape _process_buy_candidates_batch --
    it must be handled the same way _preserve_and_block's own restoration
    failure is, returning a "failed" outcome that names BOTH failures
    without aborting the batch.
    """
    monkeypatch.setenv("DCA_CRON_TRACKING_ROOT", str(tmp_path / "cron_tracking"))
    config = StrategyConfig(whole_share_tickers=frozenset({"BYDDY"}), dry_run=False)

    class _RaisingPlacementBroker(DryRunBroker):
        def place_whole_share_buy(self, ticker, quantity, ref_id):
            raise RuntimeError("broker placement unavailable")

    broker = _RaisingPlacementBroker(cash=500.0, reference_prices={"BYDDY": 10.0})

    def _raising_preserve_pending(self, *args, **kwargs):
        raise RuntimeError("ledger lock timeout")

    monkeypatch.setattr(WholeShareAccumulationLedger, "preserve_pending", _raising_preserve_pending)

    outcome, _, _, errors, _ = _process_buy_candidate(
        _decision(suggested_amount_usd=10.0, target_price=10.0),
        ctx=_ctx(config, broker),
        running_deployed_7d=0.0,
    )

    assert outcome["status"] == "failed"
    assert "placement failed" in outcome["skip_reason"]
    assert "restoration also failed" in outcome["skip_reason"]
    assert errors == 1


def test_dry_run_preview_persists_spread_fields_on_every_subsequent_outcome(tmp_path, monkeypatch):
    """Codex review ("Preserve spread verdicts in whole-share dry runs"):
    once the shared risk gates pass, the spread fields they computed
    (spread_pct/spread_status/etc.) must be carried onto every subsequent
    dry-run outcome -- buying-power block, tradability block, review block,
    and the final 'dry_run' success -- exactly like the live placement path
    already does. Without this, _build_buy_candidates persists
    spread_status="not_evaluated" even though the gate was actually
    evaluated and passed.
    """
    monkeypatch.setenv("DCA_CRON_TRACKING_ROOT", str(tmp_path / "cron_tracking"))
    config = StrategyConfig(whole_share_tickers=frozenset({"BYDDY"}), dry_run=True)
    broker = DryRunBroker(cash=500.0, reference_prices={"BYDDY": 10.0})

    outcome, _, _, _, _ = _process_buy_candidate(
        _decision(suggested_amount_usd=10.0, target_price=10.0),
        ctx=_ctx(config, broker),
        running_deployed_7d=0.0,
    )

    assert outcome["status"] == "dry_run"
    assert outcome.get("spread_status") not in (None, "not_evaluated")


def test_successful_fire_persists_the_post_settlement_pending_balance(tmp_path, monkeypatch):
    """Codex review ("Persist the post-fire accumulator balance"): a
    successful fire that leaves a remainder (e.g. $15 pending at a $10 share
    price buys one share and carries $5 forward) must record the
    AUTHORITATIVE post-settlement pending/contribution fields on the
    'submitted' outcome, or _build_buy_candidates writes both as None and
    run artifacts can't show the cross-day balance the purchase produced.
    """
    monkeypatch.setenv("DCA_CRON_TRACKING_ROOT", str(tmp_path / "cron_tracking"))
    config = StrategyConfig(whole_share_tickers=frozenset({"BYDDY"}), dry_run=False)
    broker = DryRunBroker(cash=500.0, reference_prices={"BYDDY": 10.0})

    outcome, _, _, _, _ = _process_buy_candidate(
        _decision(suggested_amount_usd=15.0, target_price=10.0),
        ctx=_ctx(config, broker),
        running_deployed_7d=0.0,
    )

    assert outcome["status"] == "submitted"
    assert outcome["whole_share_quantity"] == 1
    assert outcome["whole_share_pending_usd"] == 5.0
    assert outcome["whole_share_contribution_usd"] == 15.0
    ledger = WholeShareAccumulationLedger()
    assert ledger.pending_usd("BYDDY") == 5.0


def test_a_preserved_fire_is_redispatched_without_a_fresh_buy_signal(tmp_path, monkeypatch):
    """Codex review ("Re-dispatch preserved fires without requiring a new
    BUY"): a whole-share ticker's pending balance already at or above one
    share's price -- left there by an EARLIER blocked fire attempt -- must
    still be re-evaluated today even when today's own fresh signal is
    NO_ACTION (e.g. the price recovered), or the already-funded fire would
    sit stranded indefinitely with no bound on how long until a fresh BUY
    signal happens to recur.

    Seeds a $15 pending balance (well above the $9.90 share price used
    below), then runs with a FLAT signal (close == prev_close -> NO_ACTION,
    not a fresh BUY) -- near_close_dca._run_once_impl's buy_candidates
    construction must still splice this ticker in from its current ledger
    balance alone.
    """
    monkeypatch.setenv("DCA_CRON_TRACKING_ROOT", str(tmp_path / "cron_tracking"))
    ledger = WholeShareAccumulationLedger()
    ledger.record_contribution(
        "BYDDY",
        "2026-08-15",
        15.00,
        1_000_000.0,  # absurdly high price -> 0 shares bought, just seeds pending
        stream="seed",
        recorded_at="2026-08-15T15:30:00-04:00",
        idempotency_key="seed-2026-08-15",
    )
    assert ledger.pending_usd("BYDDY") == 15.00

    now = datetime(2026, 8, 16, 15, 45, tzinfo=EASTERN)
    config = StrategyConfig(
        dry_run=False,
        tickers=(),
        whole_share_tickers=frozenset({"BYDDY"}),
        approved_test_dates=frozenset({date(2026, 8, 16)}),
    )
    state = StrategyState()
    broker = DryRunBroker(cash=500.0, reference_prices={"BYDDY": 9.90})
    # Flat close == prev_close -> no drop -> NO_ACTION, not a fresh BUY.
    data = _FakeSignalData(SourceName.FINANCE, close_price=9.90, prev_close=9.90)
    rh_data = _FakeSignalData(SourceName.ROBINHOOD, close_price=9.90, prev_close=9.90)

    result = run_daily_compact(
        now,
        config,
        state,
        data,
        rh_data,
        broker,
        options=CompactRunOptions(artifacts_dir=str(tmp_path / "artifacts")),
    )

    assert result["blocked"] is False
    import json
    from pathlib import Path

    run_state = json.loads(Path(result["run_state_path"]).read_bytes())
    [candidate] = run_state["buy_candidates"]
    assert candidate["status"] == "submitted"
    assert candidate["whole_share_quantity"] == 1

    ledger = WholeShareAccumulationLedger()
    assert ledger.shares_purchased_total("BYDDY") == 1
    assert ledger.pending_usd("BYDDY") == pytest.approx(15.00 - 9.90, abs=0.01)


def test_a_preserved_fire_is_not_redispatched_on_a_manual_review_decision(tmp_path, monkeypatch):
    """Codex review ("Do not redispatch manual-review whole-share
    decisions"): reconcile_signals returns MANUAL_REVIEW -- an explicit
    "do not auto-buy" verdict -- when finance and Robinhood disagree (or one
    provider errors while the other says BUY). The preserved-fire
    re-dispatch check must be restricted to a genuinely safe NO_ACTION
    decision, never MANUAL_REVIEW, or it would bypass that verdict entirely
    and fire the accumulated balance anyway.

    Seeds a $15 pending balance (well above the $9.90 share price), then
    gives finance a fresh BUY signal (a real drop) while Robinhood shows a
    flat, no-drop signal -- reconcile_signals's own
    "finance BUY but Robinhood is NO_ACTION" branch reconciles this to
    MANUAL_REVIEW, not NO_ACTION. The already-funded fire must stay
    untouched.
    """
    monkeypatch.setenv("DCA_CRON_TRACKING_ROOT", str(tmp_path / "cron_tracking"))
    ledger = WholeShareAccumulationLedger()
    ledger.record_contribution(
        "BYDDY",
        "2026-08-15",
        15.00,
        1_000_000.0,
        stream="seed",
        recorded_at="2026-08-15T15:30:00-04:00",
        idempotency_key="seed-2026-08-15",
    )
    assert ledger.pending_usd("BYDDY") == 15.00

    now = datetime(2026, 8, 16, 15, 45, tzinfo=EASTERN)
    config = StrategyConfig(
        dry_run=False,
        tickers=(),
        whole_share_tickers=frozenset({"BYDDY"}),
        approved_test_dates=frozenset({date(2026, 8, 16)}),
    )
    state = StrategyState()
    broker = DryRunBroker(cash=500.0, reference_prices={"BYDDY": 9.90})
    # Finance sees a real drop (-> BUY); Robinhood is flat (-> NO_ACTION).
    # reconcile_signals reconciles this disagreement to MANUAL_REVIEW.
    data = _FakeSignalData(SourceName.FINANCE, close_price=9.90, prev_close=10.00)
    rh_data = _FakeSignalData(SourceName.ROBINHOOD, close_price=9.90, prev_close=9.90)

    result = run_daily_compact(
        now,
        config,
        state,
        data,
        rh_data,
        broker,
        options=CompactRunOptions(artifacts_dir=str(tmp_path / "artifacts")),
    )

    assert result["blocked"] is False
    import json
    from pathlib import Path

    run_state = json.loads(Path(result["run_state_path"]).read_bytes())
    # No submitted/blocked whole-share row -- MANUAL_REVIEW never reaches
    # the whole-share dispatch at all, exactly like an ordinary MANUAL_REVIEW
    # ticker.
    assert run_state["buy_candidates"] == []

    ledger = WholeShareAccumulationLedger()
    assert ledger.shares_purchased_total("BYDDY") == 0
    assert ledger.pending_usd("BYDDY") == 15.00


def test_redispatch_eligibility_uses_cent_rounded_comparison(tmp_path, monkeypatch):
    """Codex review ("Compare redispatch thresholds using cent-rounded
    prices"): a raw float comparison between pending_usd and a fractional-
    cent quote can disagree with apply_contribution's own cent-rounded
    arithmetic. $10.00 pending against a $10.004 quote compares as
    insufficient under a raw float `>=`, while apply_contribution rounds the
    quote to $10.00 and fires one share -- the SAME calculation the real,
    authoritative record_contribution call would use. The re-dispatch
    eligibility check must use that same rounding, not a raw comparison.
    """
    monkeypatch.setenv("DCA_CRON_TRACKING_ROOT", str(tmp_path / "cron_tracking"))
    ledger = WholeShareAccumulationLedger()
    ledger.record_contribution(
        "BYDDY",
        "2026-08-15",
        10.00,
        1_000_000.0,
        stream="seed",
        recorded_at="2026-08-15T15:30:00-04:00",
        idempotency_key="seed-2026-08-15",
    )
    assert ledger.pending_usd("BYDDY") == 10.00

    now = datetime(2026, 8, 16, 15, 45, tzinfo=EASTERN)
    config = StrategyConfig(
        dry_run=False,
        tickers=(),
        whole_share_tickers=frozenset({"BYDDY"}),
        approved_test_dates=frozenset({date(2026, 8, 16)}),
    )
    state = StrategyState()
    broker = DryRunBroker(cash=500.0, reference_prices={"BYDDY": 10.004})
    # Flat -> NO_ACTION; the fractional-cent quote (10.004) is what the
    # re-dispatch eligibility check must compare against, cent-rounded.
    data = _FakeSignalData(SourceName.FINANCE, close_price=10.004, prev_close=10.004)
    rh_data = _FakeSignalData(SourceName.ROBINHOOD, close_price=10.004, prev_close=10.004)

    result = run_daily_compact(
        now,
        config,
        state,
        data,
        rh_data,
        broker,
        options=CompactRunOptions(artifacts_dir=str(tmp_path / "artifacts")),
    )

    assert result["blocked"] is False
    import json
    from pathlib import Path

    run_state = json.loads(Path(result["run_state_path"]).read_bytes())
    [candidate] = run_state["buy_candidates"]
    assert candidate["status"] == "submitted"
    assert candidate["whole_share_quantity"] == 1

    ledger = WholeShareAccumulationLedger()
    assert ledger.shares_purchased_total("BYDDY") == 1


def test_redispatch_ledger_read_failure_dispatches_for_proper_error_classification(tmp_path, monkeypatch):
    """Codex review ("Surface ledger failures during preserved-fire
    redispatch"): a corrupt/unreadable ledger during the re-dispatch
    eligibility check must not silently drop the ticker with no trace.
    Dispatching it anyway lets _process_whole_share_candidate's own
    record_contribution call hit the SAME failure and classify it properly
    (errors_count -- MISSING_FILL_DATA_ERROR), rather than this eligibility
    check swallowing it as a quiet no-op that leaves the run looking clean.
    """
    monkeypatch.setenv("DCA_CRON_TRACKING_ROOT", str(tmp_path / "cron_tracking"))
    ledger_dir = tmp_path / "cron_tracking" / "whole_share_accumulation" / "BYDDY"
    ledger_dir.mkdir(parents=True)
    (ledger_dir / "ledger.json").write_text("{not valid json")

    now = datetime(2026, 8, 16, 15, 45, tzinfo=EASTERN)
    config = StrategyConfig(
        dry_run=False,
        tickers=(),
        whole_share_tickers=frozenset({"BYDDY"}),
        approved_test_dates=frozenset({date(2026, 8, 16)}),
    )
    state = StrategyState()
    broker = DryRunBroker(cash=500.0, reference_prices={"BYDDY": 9.90})
    data = _FakeSignalData(SourceName.FINANCE, close_price=9.90, prev_close=9.90)
    rh_data = _FakeSignalData(SourceName.ROBINHOOD, close_price=9.90, prev_close=9.90)

    result = run_daily_compact(
        now,
        config,
        state,
        data,
        rh_data,
        broker,
        options=CompactRunOptions(artifacts_dir=str(tmp_path / "artifacts")),
    )

    assert result["blocked"] is False
    import json
    from pathlib import Path

    run_state = json.loads(Path(result["run_state_path"]).read_bytes())
    assert run_state["usage_proxy"]["errors_count"] >= 1
    [candidate] = run_state["buy_candidates"]
    assert candidate["status"] == "blocked"
    assert "ledger error" in candidate["skip_reason"]


def test_blocked_redispatch_outcome_is_persisted_in_buy_candidates(tmp_path, monkeypatch):
    """Codex review ("Persist blocked preserved-fire redispatch outcomes"):
    a re-dispatched NO_ACTION whole-share candidate that reaches a safety
    gate (here: insufficient buying power) must still show up in
    buy_candidates.csv/run_state.json with its block reason and planned
    notional -- not silently vanish because _build_buy_candidates's
    BUY-only filter used to drop every non-BUY, non-submitted/dry_run
    outcome regardless of origin.
    """
    monkeypatch.setenv("DCA_CRON_TRACKING_ROOT", str(tmp_path / "cron_tracking"))
    ledger = WholeShareAccumulationLedger()
    ledger.record_contribution(
        "BYDDY",
        "2026-08-15",
        15.00,
        1_000_000.0,
        stream="seed",
        recorded_at="2026-08-15T15:30:00-04:00",
        idempotency_key="seed-2026-08-15",
    )
    assert ledger.pending_usd("BYDDY") == 15.00

    now = datetime(2026, 8, 16, 15, 45, tzinfo=EASTERN)
    config = StrategyConfig(
        dry_run=False,
        tickers=(),
        whole_share_tickers=frozenset({"BYDDY"}),
        approved_test_dates=frozenset({date(2026, 8, 16)}),
    )
    state = StrategyState()
    # Cash well below the $9.90 share price -> insufficient buying power block.
    broker = DryRunBroker(cash=1.0, reference_prices={"BYDDY": 9.90})
    data = _FakeSignalData(SourceName.FINANCE, close_price=9.90, prev_close=9.90)
    rh_data = _FakeSignalData(SourceName.ROBINHOOD, close_price=9.90, prev_close=9.90)

    result = run_daily_compact(
        now,
        config,
        state,
        data,
        rh_data,
        broker,
        options=CompactRunOptions(artifacts_dir=str(tmp_path / "artifacts")),
    )

    assert result["blocked"] is False
    import json
    from pathlib import Path

    run_state = json.loads(Path(result["run_state_path"]).read_bytes())
    [candidate] = run_state["buy_candidates"]
    assert candidate["status"] == "blocked"
    assert "insufficient buying power" in candidate["skip_reason"]
    assert candidate["planned_amount_usd"] == pytest.approx(9.90, abs=0.01)

    ledger = WholeShareAccumulationLedger()
    assert ledger.pending_usd("BYDDY") == 15.00


def test_redispatch_outcome_with_no_whole_share_marker_is_still_persisted(tmp_path, monkeypatch):
    """Codex review ("Retain every whole-share redispatch outcome"): the
    OLD marker-scanning predicate in _build_buy_candidates (any
    "whole_share_"-prefixed key on the outcome) missed several
    _process_whole_share_candidate exit branches entirely -- e.g. the
    top-of-function same-day-duplicate-order SKIP, which sets no
    "whole_share_" field at all. This is a non-error, "already handled
    elsewhere" outcome, not a failure, so it must still show up in
    buy_candidates.csv/run_state.json rather than silently vanishing (and
    potentially misreporting the run as no_candidates if it were the only
    candidate). The fix identifies a redispatch from decision.synthetic_source
    instead of scanning outcome keys, so it can never miss a future exit
    branch the way the marker scan could.
    """
    monkeypatch.setenv("DCA_CRON_TRACKING_ROOT", str(tmp_path / "cron_tracking"))
    ledger = WholeShareAccumulationLedger()
    ledger.record_contribution(
        "BYDDY",
        "2026-08-15",
        15.00,
        1_000_000.0,
        stream="seed",
        recorded_at="2026-08-15T15:30:00-04:00",
        idempotency_key="seed-2026-08-15",
    )
    assert ledger.pending_usd("BYDDY") == 15.00

    now = datetime(2026, 8, 16, 15, 45, tzinfo=EASTERN)
    config = StrategyConfig(
        dry_run=False,
        tickers=(),
        whole_share_tickers=frozenset({"BYDDY"}),
        approved_test_dates=frozenset({date(2026, 8, 16)}),
    )
    state = StrategyState()

    class _AlreadyOrderedBroker(DryRunBroker):
        def same_day_strategy_order_exists(self, ticker, trading_day):
            return True

    broker = _AlreadyOrderedBroker(cash=500.0, reference_prices={"BYDDY": 9.90})
    data = _FakeSignalData(SourceName.FINANCE, close_price=9.90, prev_close=9.90)
    rh_data = _FakeSignalData(SourceName.ROBINHOOD, close_price=9.90, prev_close=9.90)

    result = run_daily_compact(
        now,
        config,
        state,
        data,
        rh_data,
        broker,
        options=CompactRunOptions(artifacts_dir=str(tmp_path / "artifacts")),
    )

    assert result["blocked"] is False
    import json
    from pathlib import Path

    run_state = json.loads(Path(result["run_state_path"]).read_bytes())
    [candidate] = run_state["buy_candidates"]
    assert candidate["status"] == "skipped"
    assert "same-day broker order exists" in candidate["skip_reason"]


def test_redispatch_abstains_when_sources_disagree_on_price(tmp_path, monkeypatch):
    """Codex review ("Reconcile prices before redispatching preserved
    fires"): reconcile_signals's NO_ACTION fallthrough branch (used when
    BOTH sources independently report no fresh signal) performs NO
    price-agreement check of its own -- it only compares actions, not
    prices. Selecting one source's quote_price unconditionally (finance
    preferred) could therefore size and approve an order against a price
    the OTHER source actively disagrees with, bypassing max_per_order_usd
    at the disagreeing price. Finance reports $9.50, Robinhood reports
    $20.00 -- both flat (NO_ACTION), but wildly disagreeing on price. The
    redispatch must abstain entirely rather than fire against either quote.
    """
    monkeypatch.setenv("DCA_CRON_TRACKING_ROOT", str(tmp_path / "cron_tracking"))
    ledger = WholeShareAccumulationLedger()
    ledger.record_contribution(
        "BYDDY",
        "2026-08-15",
        10.00,
        1_000_000.0,
        stream="seed",
        recorded_at="2026-08-15T15:30:00-04:00",
        idempotency_key="seed-2026-08-15",
    )
    assert ledger.pending_usd("BYDDY") == 10.00

    now = datetime(2026, 8, 16, 15, 45, tzinfo=EASTERN)
    config = StrategyConfig(
        dry_run=False,
        tickers=(),
        whole_share_tickers=frozenset({"BYDDY"}),
        approved_test_dates=frozenset({date(2026, 8, 16)}),
    )
    state = StrategyState()
    broker = DryRunBroker(cash=500.0, reference_prices={"BYDDY": 9.50})
    # Both flat (NO_ACTION on each source individually), but wildly
    # disagreeing on the actual price.
    data = _FakeSignalData(SourceName.FINANCE, close_price=9.50, prev_close=9.50)
    rh_data = _FakeSignalData(SourceName.ROBINHOOD, close_price=20.00, prev_close=20.00)

    result = run_daily_compact(
        now,
        config,
        state,
        data,
        rh_data,
        broker,
        options=CompactRunOptions(artifacts_dir=str(tmp_path / "artifacts")),
    )

    assert result["blocked"] is False
    import json
    from pathlib import Path

    run_state = json.loads(Path(result["run_state_path"]).read_bytes())
    assert run_state["buy_candidates"] == []

    ledger = WholeShareAccumulationLedger()
    assert ledger.shares_purchased_total("BYDDY") == 0
    assert ledger.pending_usd("BYDDY") == 10.00


def _source_signal(source, *, quote_price, suggested_amount_usd):
    return SourceSignal(
        ticker="BYDDY",
        source=source,
        action=Action.BUY,
        reason="test",
        suggested_amount_usd=suggested_amount_usd,
        selected_base_amount_usd=1.0,
        quote_price=quote_price,
        decision_bar_close=quote_price,
    )


def test_reconciled_price_uses_the_higher_of_two_agreeing_quotes():
    """Codex review ("Size whole-share orders using the higher reconciled
    quote"): actual execution happens on Robinhood (place_whole_share_buy
    is Robinhood-only), so sizing/threshold/ceiling math against the LOWER
    of two in-tolerance quotes can authorize spending beyond what the
    broker will actually fill at. With the documented Finance=$9.90/
    Robinhood=$10.09 boundary case, unconditionally using Finance's lower
    price let a $10 pending balance and the default $10 ceiling both look
    sufficient/compliant even though the broker was already quoting above
    both figures. The reconciled price must be the HIGHER of the two
    agreeing quotes, not unconditionally Finance's.
    """
    decision = ReconciledDecision(
        ticker="BYDDY",
        action=Action.NO_ACTION,
        reason="test",
        finance_signal=_source_signal(SourceName.FINANCE, quote_price=9.90, suggested_amount_usd=0.0),
        robinhood_signal=_source_signal(SourceName.ROBINHOOD, quote_price=10.09, suggested_amount_usd=0.0),
    )

    assert _whole_share_redispatch_reconciled_price(decision) == 10.09

    # Symmetric: whichever source happens to be higher wins, not "finance
    # unless robinhood is higher" or a fixed source preference.
    reversed_decision = ReconciledDecision(
        ticker="BYDDY",
        action=Action.NO_ACTION,
        reason="test",
        finance_signal=_source_signal(SourceName.FINANCE, quote_price=10.09, suggested_amount_usd=0.0),
        robinhood_signal=_source_signal(SourceName.ROBINHOOD, quote_price=9.90, suggested_amount_usd=0.0),
    )

    assert _whole_share_redispatch_reconciled_price(reversed_decision) == 10.09

    # Disagreement beyond tolerance is still refused entirely -- the fix
    # only changes WHICH of two agreeing quotes wins, never widens what
    # counts as agreement.
    disagreeing_decision = ReconciledDecision(
        ticker="BYDDY",
        action=Action.NO_ACTION,
        reason="test",
        finance_signal=_source_signal(SourceName.FINANCE, quote_price=9.50, suggested_amount_usd=0.0),
        robinhood_signal=_source_signal(SourceName.ROBINHOOD, quote_price=20.00, suggested_amount_usd=0.0),
    )

    assert _whole_share_redispatch_reconciled_price(disagreeing_decision) is None


def test_fresh_buy_blocks_when_sources_disagree_on_execution_quote(tmp_path, monkeypatch):
    """Codex review ("Reconcile fresh whole-share BUY prices before
    sizing"): reconcile_signals's "finance and Robinhood agree on BUY"
    branch selects ONE winning signal's quote_price purely by whichever
    produced the smaller suggested_amount_usd -- it never checks that
    winning quote against the OTHER source's own quote for agreement. For a
    whole-share order, the execution price directly sizes the share count
    and the max_per_order_usd ceiling check, so finance quoting $9.50 and
    Robinhood quoting $20 could let a $10 ceiling approve one share as a
    $9.50 order right before Robinhood actually executes it near $20. The
    candidate must instead block, mirroring the redispatch path's own
    disagreement abstention.
    """
    monkeypatch.setenv("DCA_CRON_TRACKING_ROOT", str(tmp_path / "cron_tracking"))
    config = StrategyConfig(whole_share_tickers=frozenset({"BYDDY"}), dry_run=False)
    broker = DryRunBroker(cash=500.0, reference_prices={"BYDDY": 9.50})

    finance_signal = _source_signal(SourceName.FINANCE, quote_price=9.50, suggested_amount_usd=1.0)
    robinhood_signal = _source_signal(SourceName.ROBINHOOD, quote_price=20.00, suggested_amount_usd=2.0)
    decision = ReconciledDecision(
        ticker="BYDDY",
        action=Action.BUY,
        reason="finance and Robinhood agree on BUY",
        finance_signal=finance_signal,
        robinhood_signal=robinhood_signal,
        suggested_amount_usd=1.0,
        selected_base_amount_usd=1.0,
        target_price=9.50,
        decision_bar_close=9.50,
    )

    outcome, _, _, errors, _ = _process_buy_candidate(
        decision,
        ctx=_ctx(config, broker),
        running_deployed_7d=0.0,
    )

    assert outcome["status"] == "blocked"
    assert "disagree" in outcome["skip_reason"]
    assert errors == 0
    ledger = WholeShareAccumulationLedger()
    assert ledger.pending_usd("BYDDY") == 0.0


def test_fresh_buy_with_agreeing_sources_still_fires(tmp_path, monkeypatch):
    """Companion to the disagreement test above: when both sources agree
    (within tolerance) on the execution quote, the reconciled-price check
    must not block a genuinely fireable candidate. Sized against the
    HIGHER of the two agreeing quotes (Codex review: "Size whole-share
    orders using the higher reconciled quote") -- actual execution happens
    on Robinhood, so $11 contribution comfortably covers even the higher
    $10.01 Robinhood quote.
    """
    monkeypatch.setenv("DCA_CRON_TRACKING_ROOT", str(tmp_path / "cron_tracking"))
    config = StrategyConfig(
        whole_share_tickers=frozenset({"BYDDY"}), dry_run=False, max_per_order_usd=15.0
    )
    broker = DryRunBroker(cash=500.0, reference_prices={"BYDDY": 10.01})

    finance_signal = _source_signal(SourceName.FINANCE, quote_price=10.0, suggested_amount_usd=11.0)
    robinhood_signal = _source_signal(SourceName.ROBINHOOD, quote_price=10.01, suggested_amount_usd=12.0)
    decision = ReconciledDecision(
        ticker="BYDDY",
        action=Action.BUY,
        reason="finance and Robinhood agree on BUY",
        finance_signal=finance_signal,
        robinhood_signal=robinhood_signal,
        suggested_amount_usd=11.0,
        selected_base_amount_usd=1.0,
        target_price=10.0,
        decision_bar_close=10.0,
    )

    outcome, _, _, errors, _ = _process_buy_candidate(
        decision,
        ctx=_ctx(config, broker),
        running_deployed_7d=0.0,
    )

    assert outcome["status"] == "submitted"
    assert errors == 0
    # Sized against the higher $10.01 quote, not the lower $10.00 one.
    assert outcome["submitted_amount_usd"] == pytest.approx(10.01, abs=0.01)


def test_initial_duplicate_lookup_failure_outcome_retains_a_whole_share_marker(tmp_path, monkeypatch):
    """Codex review ("Retain early redispatch lookup failures in candidate
    artifacts"): the FIRST, top-of-function same_day_strategy_order_exists()
    check's own exception outcome (fixed in an earlier round to no longer
    escape the batch) must ALSO carry a "whole_share_"-prefixed field for a
    NO_ACTION preserved-balance redispatch -- this check runs before the
    ledger and bypasses every other whole-share-marking helper, so without
    one, _build_buy_candidates's non-BUY filter silently drops the outcome
    even though errors_count_inc correctly counted the failure.
    """
    monkeypatch.setenv("DCA_CRON_TRACKING_ROOT", str(tmp_path / "cron_tracking"))
    config = StrategyConfig(whole_share_tickers=frozenset({"BYDDY"}), dry_run=False)

    class _RaisingOnFirstCallBroker(DryRunBroker):
        def same_day_strategy_order_exists(self, ticker, trading_day):
            raise RuntimeError("duplicate order lookup unavailable")

    broker = _RaisingOnFirstCallBroker(cash=500.0, reference_prices={"BYDDY": 10.0})

    outcome, _, _, errors, _ = _process_buy_candidate(
        _decision(action=Action.NO_ACTION, suggested_amount_usd=0.0, target_price=10.0),
        ctx=_ctx(config, broker),
        running_deployed_7d=0.0,
    )

    assert outcome["status"] == "blocked"
    assert errors == 1
    assert any(key.startswith("whole_share_") for key in outcome)


def test_redispatch_caps_the_executable_fire_to_the_kill_switch_base(tmp_path, monkeypatch):
    """Codex review ("Apply the kill switch to the executable whole-share
    fire"): capping only TODAY's contribution does nothing for a
    preserved-fire redispatch, since its contribution is already $0 -- the
    fire is funded entirely by an earlier day's accumulated balance. With
    the kill switch engaged, the EXECUTABLE order itself must be capped to
    decision.selected_base_amount_usd -- which a redispatched NO_ACTION
    decision never populates (it stays at its bare 0.0 default, since only a
    fresh BUY sets it), so the effective ceiling collapses to $0 and the
    fire is blocked outright rather than executing at full size. Seeds a
    $15 pending balance (enough for one $9.90 share, unclamped) and engages
    the kill switch via a saturated 7-day deployment history -- the
    redispatch must be blocked, never fire the full share while the switch
    is engaged.
    """
    monkeypatch.setenv("DCA_CRON_TRACKING_ROOT", str(tmp_path / "cron_tracking"))
    ledger = WholeShareAccumulationLedger()
    ledger.record_contribution(
        "BYDDY",
        "2026-08-15",
        15.00,
        1_000_000.0,
        stream="seed",
        recorded_at="2026-08-15T15:30:00-04:00",
        idempotency_key="seed-2026-08-15",
    )
    assert ledger.pending_usd("BYDDY") == 15.00

    now = datetime(2026, 8, 16, 15, 45, tzinfo=EASTERN)
    config = StrategyConfig(
        dry_run=False,
        tickers=(),
        whole_share_tickers=frozenset({"BYDDY"}),
        approved_test_dates=frozenset({date(2026, 8, 16)}),
        # Saturates the rolling 7-day deployment threshold so
        # kill_switch_engaged() is True for this run.
        deployed_last_7d_usd=1_000_000.0,
    )
    state = StrategyState()
    broker = DryRunBroker(cash=500.0, reference_prices={"BYDDY": 9.90})
    data = _FakeSignalData(SourceName.FINANCE, close_price=9.90, prev_close=9.90)
    rh_data = _FakeSignalData(SourceName.ROBINHOOD, close_price=9.90, prev_close=9.90)

    result = run_daily_compact(
        now,
        config,
        state,
        data,
        rh_data,
        broker,
        options=CompactRunOptions(artifacts_dir=str(tmp_path / "artifacts")),
    )

    assert result["blocked"] is False
    import json
    from pathlib import Path

    run_state = json.loads(Path(result["run_state_path"]).read_bytes())
    [candidate] = run_state["buy_candidates"]
    # A single $9.90 share exceeds the $1 kill-switch ceiling entirely, so
    # the redispatch is blocked outright rather than firing at full size --
    # the deployment-risk reduction the kill switch exists for.
    assert candidate["status"] == "blocked"
    assert "exceeds max per-order" in candidate["skip_reason"]

    ledger = WholeShareAccumulationLedger()
    assert ledger.shares_purchased_total("BYDDY") == 0
    assert ledger.pending_usd("BYDDY") == 15.00


def test_dry_run_review_failure_is_isolated_to_this_candidate(tmp_path, monkeypatch):
    """Codex review ("Catch whole-share dry-run review failures"): the
    dry-run preview branch's own review_whole_share_buy() call can raise
    (a transient broker/API outage) exactly like the real manual-review
    branch's identical call -- without an exception boundary here, the
    raise would escape _process_buy_candidates_batch entirely, aborting the
    WHOLE demo run and preventing artifacts for every other, independent
    candidate still to be processed. No ledger restoration is needed (this
    branch is read-only, no record_contribution call ran).
    """
    monkeypatch.setenv("DCA_CRON_TRACKING_ROOT", str(tmp_path / "cron_tracking"))

    class _RaisingReviewBroker(DryRunBroker):
        def review_whole_share_buy(self, ticker, quantity):
            raise RuntimeError("review lookup unavailable")

    config = StrategyConfig(whole_share_tickers=frozenset({"BYDDY"}), dry_run=True)
    broker = _RaisingReviewBroker(cash=500.0, reference_prices={"BYDDY": 10.0})

    outcome, _, _, errors, order_reviews_attempted = _process_buy_candidate(
        _decision(suggested_amount_usd=10.0, target_price=10.0),
        ctx=_ctx(config, broker),
        running_deployed_7d=0.0,
    )

    assert outcome["status"] == "blocked"
    assert "dry-run review failed" in outcome["skip_reason"]
    assert errors == 1
    assert order_reviews_attempted == 1
    ledger = WholeShareAccumulationLedger()
    assert ledger.pending_usd("BYDDY") == 0.0
    assert ledger.shares_purchased_total("BYDDY") == 0


def test_dry_run_tradability_failure_is_isolated_to_this_candidate(tmp_path, monkeypatch):
    """Codex review ("Catch tradability failures in dry-run previews"): the
    dry-run preview branch's own is_tradable_for_whole_share_buy() call can
    raise (a transient Robinhood/API outage) exactly like the real
    placement path's identical call further below (already wrapped in
    try/except, Codex review: "Restore the deduction when tradability
    lookup raises") -- without an equivalent boundary here, the raise would
    escape _process_buy_candidates_batch entirely, aborting the WHOLE demo
    run and preventing artifacts for every other, independent candidate
    still to be processed. No ledger restoration is needed (this branch is
    read-only, no record_contribution call ran).
    """
    monkeypatch.setenv("DCA_CRON_TRACKING_ROOT", str(tmp_path / "cron_tracking"))

    class _RaisingTradabilityBroker(DryRunBroker):
        def is_tradable_for_whole_share_buy(self, ticker):
            raise RuntimeError("tradability lookup unavailable")

    config = StrategyConfig(whole_share_tickers=frozenset({"BYDDY"}), dry_run=True)
    broker = _RaisingTradabilityBroker(cash=500.0, reference_prices={"BYDDY": 10.0})

    outcome, _, _, errors, _ = _process_buy_candidate(
        _decision(suggested_amount_usd=10.0, target_price=10.0),
        ctx=_ctx(config, broker),
        running_deployed_7d=0.0,
    )

    assert outcome["status"] == "blocked"
    assert "tradability lookup failed" in outcome["skip_reason"]
    assert errors == 1
    ledger = WholeShareAccumulationLedger()
    assert ledger.pending_usd("BYDDY") == 0.0
    assert ledger.shares_purchased_total("BYDDY") == 0


def test_dry_run_buying_power_failure_is_isolated_to_this_candidate(tmp_path, monkeypatch):
    """Codex review ("Catch buying-power failures in dry-run previews"): the
    dry-run preview branch's own available_buying_power() call can raise (a
    transient Robinhood/API outage) exactly like the real placement path's
    identical call further below (already wrapped in try/except) -- without
    an equivalent boundary here, the raise would escape
    _process_buy_candidates_batch entirely, aborting the WHOLE demo run and
    preventing artifacts for every other, independent candidate still to be
    processed. No ledger restoration is needed (this branch is read-only, no
    record_contribution call ran).
    """
    monkeypatch.setenv("DCA_CRON_TRACKING_ROOT", str(tmp_path / "cron_tracking"))

    class _RaisingBuyingPowerBroker(DryRunBroker):
        def available_buying_power(self):
            raise RuntimeError("buying power lookup unavailable")

    config = StrategyConfig(whole_share_tickers=frozenset({"BYDDY"}), dry_run=True)
    broker = _RaisingBuyingPowerBroker(cash=500.0, reference_prices={"BYDDY": 10.0})

    outcome, _, _, errors, _ = _process_buy_candidate(
        _decision(suggested_amount_usd=10.0, target_price=10.0),
        ctx=_ctx(config, broker),
        running_deployed_7d=0.0,
    )

    assert outcome["status"] == "blocked"
    assert "buying power lookup failed" in outcome["skip_reason"]
    assert errors == 1
    ledger = WholeShareAccumulationLedger()
    assert ledger.pending_usd("BYDDY") == 0.0
    assert ledger.shares_purchased_total("BYDDY") == 0


def test_planned_spend_preview_applies_the_kill_switch_ceiling(tmp_path, monkeypatch):
    """Codex review ("Apply the kill-switch ceiling in tier planning"): the
    tier-planning preview must clamp to the SAME effective ceiling execution
    actually uses -- when the kill switch is engaged, execution's
    _whole_share_risk_gates call clamps against
    decision.selected_base_amount_usd (via ceiling_override_usd), not just
    config.max_per_order_usd. Without matching that here, the preview could
    plan for up to the full accumulated notional while execution actually
    clamps to the $2 selected base (or $0 for a redispatch), overstating
    planned spend enough to select a needlessly high budget tier and
    authorize additional, unrelated same-run orders the real (capped) spend
    never justifies.

    Verified at the actual decision point: _run_once_impl feeds this loop's
    accumulated planned_spend into state.select_daily_budget(), which calls
    the module-level select_budget_tier() (see that method's own body) --
    NOT the SEPARATE, later recompute build_run_artifacts() performs purely
    for the persisted run_state.json report (using each outcome's raw,
    pre-clamp whole_share_planned_notional_usd, an intentionally different
    "what was attempted" summary unrelated to this finding). Monkeypatching
    the module-level select_budget_tier() intercepts the FIRST (live,
    caps-governing) call and captures its argument directly.

    Seeds a $150 pending balance -- at $9.90/share, an UNCAPPED preview would
    compute a ~$148.50 notional, comfortably past the $129 tier-escalation
    threshold -- then engages the kill switch. With the fix, the preview
    clamps to the $2 kill-switch ceiling (0 shares fit under it), so the
    live tier-selection call must see approximately $0, not ~$148.50.
    """
    monkeypatch.setenv("DCA_CRON_TRACKING_ROOT", str(tmp_path / "cron_tracking"))
    ledger = WholeShareAccumulationLedger()
    ledger.record_contribution(
        "BYDDY",
        "2026-08-15",
        150.00,
        1_000_000.0,
        stream="seed",
        recorded_at="2026-08-15T15:30:00-04:00",
        idempotency_key="seed-2026-08-15",
    )
    assert ledger.pending_usd("BYDDY") == 150.00

    from near_close_dca import select_budget_tier as _real_select_budget_tier

    calls = []

    def _spy_select_budget_tier(planned_spend_usd, high_multiplier_signal_count):
        calls.append(planned_spend_usd)
        return _real_select_budget_tier(planned_spend_usd, high_multiplier_signal_count)

    monkeypatch.setattr("near_close_dca.select_budget_tier", _spy_select_budget_tier)

    now = datetime(2026, 8, 16, 15, 45, tzinfo=EASTERN)
    config = StrategyConfig(
        dry_run=False,
        tickers=(),
        whole_share_tickers=frozenset({"BYDDY"}),
        approved_test_dates=frozenset({date(2026, 8, 16)}),
        deployed_last_7d_usd=1_000_000.0,  # saturates kill_switch_engaged()
    )
    state = StrategyState()
    broker = DryRunBroker(cash=500.0, reference_prices={"BYDDY": 9.90})
    # Both sources agree on a real drop -> a fresh BUY signal ($2 base * 1x
    # ladder tier = $2 today's contribution), reaching the tier-planning
    # loop's whole-share branch the ordinary way (not via redispatch).
    data = _FakeSignalData(SourceName.FINANCE, close_price=9.90, prev_close=10.00)
    rh_data = _FakeSignalData(SourceName.ROBINHOOD, close_price=9.90, prev_close=10.00)

    # RunContext directly, not run_daily_compact -- run_daily_compact defaults
    # a None deployment_history_root to a real directory and re-derives
    # config.deployed_last_7d_usd from any run-state history found there
    # (empty here, so it would silently zero out the deployed_last_7d_usd
    # this test sets directly). RunContext's own deployment_history_root
    # default (None) skips that re-derivation entirely, keeping this test's
    # config authoritative -- exactly like
    # test_whole_share_evaluated_tickers_are_in_the_coverage_universe above.
    run_result = RunResult(trading_day=date(2026, 8, 16), decisions=[])
    ctx = RunContext(
        now=now,
        config=config,
        state=state,
        finance_data=data,
        robinhood_data=rh_data,
        broker=broker,
        artifacts_dir=str(tmp_path / "artifacts"),
        run_result=run_result,
        stream="afternoon",
    )
    run_once(ctx)

    assert calls, "select_budget_tier was never called"
    # The FIRST call is the live, caps-governing tier selection this finding
    # is about; a later call (build_run_artifacts's own reporting recompute)
    # is a separate, unrelated concern this fix does not touch.
    assert calls[0] == pytest.approx(0.0, abs=0.01)

    # The fire was blocked outright at the kill-switch ceiling and fully
    # preserved, including today's own $2 contribution -- nothing was spent.
    ledger = WholeShareAccumulationLedger()
    assert ledger.pending_usd("BYDDY") == 152.00


def test_tier_planning_reconciles_the_price_for_a_fresh_whole_share_buy(tmp_path, monkeypatch):
    """Codex review ("Reconcile the tier-planning whole-share price"): the
    tier-planning preview previously used d.target_price -- the quote from
    whichever source WON reconcile_signals's "agree on BUY" selection (by
    smaller suggested_amount_usd) -- while _process_whole_share_candidate
    now executes using the reconciled price
    _whole_share_redispatch_reconciled_price returns (the HIGHER of the two
    in-tolerance quotes, since execution happens on Robinhood -- Codex
    review: "Size whole-share orders using the higher reconciled quote").
    When Finance wins the selection (smaller suggested amount) with the
    LOWER quote, the preview using d.target_price (Finance's, unreconciled)
    can see a fire the reconciled, HIGHER execution price would actually
    refuse -- overstating planned spend right at a budget-tier threshold.

    Bypasses the full signal-evaluation stack (drop %, ladder tiers, base
    selection) by monkeypatching _evaluate_all_signals directly with a
    hand-built ReconciledDecision: Finance's $9.90 suggested_amount_usd
    wins over Robinhood's $15 (so target_price is Finance's own $9.90
    quote), but the reconciled price execution actually uses is the higher
    $10.09 Robinhood quote. A $0 pending balance buys exactly 1 share at
    the unreconciled $9.90 preview price but 0 shares at the reconciled
    $10.09 execution price.
    """
    monkeypatch.setenv("DCA_CRON_TRACKING_ROOT", str(tmp_path / "cron_tracking"))
    ledger = WholeShareAccumulationLedger()
    assert ledger.pending_usd("BYDDY") == 0.0

    finance_signal = _source_signal(SourceName.FINANCE, quote_price=9.90, suggested_amount_usd=9.90)
    robinhood_signal = _source_signal(SourceName.ROBINHOOD, quote_price=10.09, suggested_amount_usd=15.0)
    decision = ReconciledDecision(
        ticker="BYDDY",
        action=Action.BUY,
        reason="finance and Robinhood agree on BUY",
        finance_signal=finance_signal,
        robinhood_signal=robinhood_signal,
        suggested_amount_usd=9.90,
        selected_base_amount_usd=1.0,
        target_price=9.90,
        decision_bar_close=9.90,
    )

    monkeypatch.setattr(
        "near_close_dca._evaluate_all_signals", lambda *args, **kwargs: ([decision], 0)
    )

    from near_close_dca import select_budget_tier as _real_select_budget_tier

    calls = []

    def _spy_select_budget_tier(planned_spend_usd, high_multiplier_signal_count):
        calls.append(planned_spend_usd)
        return _real_select_budget_tier(planned_spend_usd, high_multiplier_signal_count)

    monkeypatch.setattr("near_close_dca.select_budget_tier", _spy_select_budget_tier)

    now = datetime(2026, 8, 16, 15, 45, tzinfo=EASTERN)
    config = StrategyConfig(
        dry_run=False,
        tickers=(),
        whole_share_tickers=frozenset({"BYDDY"}),
        approved_test_dates=frozenset({date(2026, 8, 16)}),
    )
    state = StrategyState()
    broker = DryRunBroker(cash=500.0, reference_prices={"BYDDY": 9.90})

    run_result = RunResult(trading_day=date(2026, 8, 16), decisions=[])
    ctx = RunContext(
        now=now,
        config=config,
        state=state,
        finance_data=_FakeSignalData(SourceName.FINANCE, close_price=9.90, prev_close=10.00),
        robinhood_data=_FakeSignalData(SourceName.ROBINHOOD, close_price=10.09, prev_close=10.00),
        broker=broker,
        artifacts_dir=str(tmp_path / "artifacts"),
        run_result=run_result,
        stream="afternoon",
    )
    run_once(ctx)

    assert calls, "select_budget_tier was never called"
    # With the fix, the preview reconciles to the HIGHER $10.09 quote (same
    # as execution) -- a $0 pending balance plus the $9.90 contribution
    # ($9.90 total) does not cover a $10.09 share, so the real plan is $0.
    # Without the fix, the preview would use Finance's unreconciled $9.90
    # quote directly (0 shares fit == the SAME price as the total, so it
    # actually WOULD fire at exactly $9.90) and overstate planned spend.
    assert calls[0] == pytest.approx(0.0, abs=0.01)


def test_whole_share_tickers_are_evaluated_in_a_stable_sorted_order(tmp_path, monkeypatch):
    """Codex review, PR #786 ("Sort whole-share tickers before evaluation"):
    the evaluation list is built from a ``frozenset``, so with a SECOND
    whole-share ticker its order became ``PYTHONHASHSEED``-dependent.

    That list decides the order tickers are evaluated, sized and PLACED in --
    ``_evaluate_all_signals`` appends decisions in input order and the
    placement loop follows it -- so on a day when the remaining daily/order
    cap fits only one fire, or the consecutive-failure breaker trips between
    them, two runs on IDENTICAL inputs bought different tickers. With one
    whole-share ticker the order was trivially stable; this PR adding RVII is
    what made it reachable.

    Asserted two ways on purpose. The captured order alone would only fail on
    the seeds that happen to reverse the pair (measured: roughly half), so the
    source is checked too -- the regression is "``sorted`` was dropped", and
    that is deterministic.
    """
    monkeypatch.setenv("DCA_CRON_TRACKING_ROOT", str(tmp_path / "cron_tracking"))

    seen: list = []

    def _capture(now, config, tradable_tickers, finance_data, robinhood_data):
        seen.append(list(tradable_tickers))
        return [], 0

    monkeypatch.setattr("near_close_dca._evaluate_all_signals", _capture)

    now = datetime(2026, 8, 16, 15, 45, tzinfo=EASTERN)
    # Deliberately NOT in sorted order in the literal, and with one excluded,
    # so the assertion cannot pass by accident.
    config = StrategyConfig(
        dry_run=False,
        tickers=(),
        whole_share_tickers=frozenset({"RVII", "BYDDY", "ZZZZ"}),
        excluded_tickers=frozenset({"ZZZZ"}),
        approved_test_dates=frozenset({date(2026, 8, 16)}),
    )
    ctx = RunContext(
        now=now,
        config=config,
        state=StrategyState(),
        finance_data=_FakeSignalData(SourceName.FINANCE, close_price=9.90, prev_close=10.00),
        robinhood_data=_FakeSignalData(SourceName.ROBINHOOD, close_price=10.09, prev_close=10.00),
        broker=DryRunBroker(cash=500.0, reference_prices={"BYDDY": 9.90, "RVII": 23.36}),
        artifacts_dir=str(tmp_path / "artifacts"),
        run_result=RunResult(trading_day=date(2026, 8, 16), decisions=[]),
        stream="afternoon",
    )
    run_once(ctx)

    assert seen, "_evaluate_all_signals was never called"
    assert seen[0] == ["BYDDY", "RVII"], seen[0]

    import inspect

    source = inspect.getsource(_near_close_dca._run_once_impl)
    assert "whole_share_evaluated_tickers = (\n        sorted(" in source, (
        "the whole-share evaluation list must stay sorted -- a frozenset's "
        "iteration order decides placement order otherwise"
    )
