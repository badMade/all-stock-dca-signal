"""End-to-end tests for the whole-share accumulation wiring through
near_close_dca._process_buy_candidate's dispatch to
_process_whole_share_candidate (see docs/whole_share_accumulation.md).

Mirrors test_inverse_martingale_wiring.py's structure: each test isolates
the ledger's on-disk state via $DCA_CRON_TRACKING_ROOT (the module always
constructs WholeShareAccumulationLedger() bare, with no root override, so
this env var is the only isolation lever).
"""

import csv
import inspect
from contextlib import contextmanager
from dataclasses import replace
from datetime import date, datetime, time, timedelta
from typing import Any, cast

import pytest

import near_close_dca as _near_close_dca
from dca_whole_share_accumulator import WholeShareAccumulationLedger
from near_close_dca import (
    EASTERN,
    MISSING_FILL_EXECUTION_GAP,
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
    _whole_share_reconciled_price,
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

    source: SourceName = SourceName.ROBINHOOD

    def get_previous_close_bar(self, ticker, as_of):
        return None

    def get_intraday_bars(self, ticker, start, end, interval_minutes):
        return []

    def get_bid_ask(self, ticker, as_of):
        return (9.99, 10.01)


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


def _blocked_stub(trading_day_iso, *, triggered_at_utc):
    """A minimal but genuinely valid blocked run_state stub -- carrying the
    SAME real ``run_classification`` a gated ``run_daily_compact`` call
    would stamp -- so ``dca_risk.is_afternoon_scheduled_evidence`` (and
    therefore ``_load_deployment_history_states``-based scans like
    ``_persist_gated_whole_share_recovery``'s cross-artifact netting)
    actually recognizes it, unlike a bare hand-written dict.
    """
    from scripts.run_classification import build_scheduled_classification

    return {
        "status": "blocked",
        "trading_day": trading_day_iso,
        "run_classification": build_scheduled_classification(trading_day_iso, triggered_at_utc),
        "buy_candidates": [],
    }


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


@pytest.mark.parametrize("working_state", ["new", "confirmed", "partially_filled"])
def test_ordinary_success_path_defers_settlement_for_a_still_live_order(
    tmp_path, monkeypatch, working_state
):
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
                return {
                    "id": "RH-STILL-LIVE-1",
                    "state": working_state,
                    # A partial cumulative fill is deliberately not final
                    # while any of these working states can still advance.
                    "cumulative_quantity": "1" if working_state == "partially_filled" else "0",
                }
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
    assert errors == 0
    assert running_deployed_7d == 10.0
    # An accepted-but-unfilled order still gets a real execution record, so
    # fill-coverage reporting doesn't treat it as though nothing happened.
    assert len(run_result.execution_records) == 1
    assert run_result.execution_records[0].status == "submitted"
    assert run_result.execution_records[0].quantity_filled is None
    assert run_result.loop_block_reasons["BYDDY"] == MISSING_FILL_EXECUTION_GAP

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
    assert errors == 0
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


def test_dry_run_preview_blocks_when_one_share_exceeds_the_ceiling(tmp_path, monkeypatch):
    monkeypatch.setenv("DCA_CRON_TRACKING_ROOT", str(tmp_path / "cron_tracking"))
    config = StrategyConfig(whole_share_tickers=frozenset({"BYDDY"}), dry_run=True)
    broker = DryRunBroker(cash=500.0, reference_prices={"BYDDY": 50.0})

    outcome, _, _, errors, _ = _process_buy_candidate(
        _decision(suggested_amount_usd=50.0, target_price=50.0),
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
    broker = DryRunBroker(cash=500.0, reference_prices={"BYDDY": 50.0})

    outcome, _, _, _, _ = _process_buy_candidate(
        _decision(suggested_amount_usd=50.0, target_price=50.0),
        ctx=_ctx(config, broker),
        running_deployed_7d=0.0,
    )

    assert outcome["status"] == "blocked"
    assert outcome["budget_blocked"] is True
    assert "exceeds max per-order" in outcome["skip_reason"]
    ledger = WholeShareAccumulationLedger()
    assert ledger.pending_usd("BYDDY") == 50.0
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

    class _WideSpreadData(_TightSpreadData):
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


def test_placement_failure_restores_credit_only_after_both_order_history_checks(tmp_path, monkeypatch):
    """A definite no-order result must precede restoration after placement raises.

    The live lookup catches an accepted order that is still working, while the
    any-state lookup catches one that filled or terminated before reconciliation.
    Restoring the claim before both exact-ref-id checks report no match could
    make already-spent credit available on a later trading day.
    """
    monkeypatch.setenv("DCA_CRON_TRACKING_ROOT", str(tmp_path / "cron_tracking"))
    config = StrategyConfig(whole_share_tickers=frozenset({"BYDDY"}), dry_run=False)
    calls = []

    class _RejectedBeforeAcceptanceBroker(DryRunBroker):
        def place_whole_share_buy(self, ticker, quantity, ref_id):
            calls.append(("place", ref_id))
            raise RuntimeError("rejected before acceptance")

        def same_day_strategy_order_exists_with_ref_id_prefix(self, ticker, trading_day, ref_id_prefixes):
            calls.append(("live", ref_id_prefixes))
            return None

        def same_day_order_with_ref_id_prefix_any_state(self, ticker, trading_day, ref_id_prefixes):
            calls.append(("any_state", ref_id_prefixes))
            return None

    broker = _RejectedBeforeAcceptanceBroker(cash=500.0, reference_prices={"BYDDY": 10.0})

    real_preserve_pending = WholeShareAccumulationLedger.preserve_pending

    def _spy_preserve_pending(self, ticker, trading_day, deducted_usd, **kwargs):
        calls.append(("preserve_pending", ticker))
        return real_preserve_pending(self, ticker, trading_day, deducted_usd, **kwargs)

    monkeypatch.setattr(WholeShareAccumulationLedger, "preserve_pending", _spy_preserve_pending)
    outcome, _, _, errors, _ = _process_buy_candidate(
        _decision(suggested_amount_usd=10.0, target_price=10.0),
        ctx=_ctx(config, broker),
        running_deployed_7d=0.0,
    )

    ref_id = "graded-dca-wholeshare-2026-08-16-BYDDY"
    assert calls == [
        ("place", ref_id),
        ("live", (ref_id,)),
        ("any_state", (ref_id,)),
        ("preserve_pending", "BYDDY"),
    ]
    assert outcome["status"] == "failed"
    assert errors == 1
    assert WholeShareAccumulationLedger().pending_usd("BYDDY") == 10.0


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
    assert outcome["whole_share_settlement_reconciliation_needed"] is True
    assert errors == 1
    ledger = WholeShareAccumulationLedger()
    # Neither restored (would free credit for an order that may be live) nor
    # settled (no order id was ever confirmed) -- left exactly as
    # record_contribution's own deduction left it.
    assert ledger.pending_usd("BYDDY") == 0.0
    assert ledger.shares_purchased_total("BYDDY") == 0


def test_ambiguous_placement_failure_rejects_a_negative_fractional_terminal_quantity(tmp_path, monkeypatch):
    """Codex review ("Check negativity before truncating terminal
    quantities"): this module's OTHER copy of the terminal-quantity parse
    (the placement-failure/terminal-inclusive reconciliation branch inside
    _process_whole_share_candidate, duplicated from
    _reconcile_unresolved_whole_share_claim per this module's own "three
    entry points"/duplicated-logic caution) has the identical truncation
    gap: a negative FRACTIONAL cumulative_quantity like "-0.5" truncates to
    0 under int(float(...)), indistinguishable from an ordinary explicit
    zero. Falling through to the zero-fill release here would restore the
    claim's entire reservation on malformed broker evidence. Uses
    WholeSharePlacementTerminalError's own carried order payload, which
    this branch reconciles WITHOUT a separate any-state lookup.
    """
    monkeypatch.setenv("DCA_CRON_TRACKING_ROOT", str(tmp_path / "cron_tracking"))
    config = StrategyConfig(whole_share_tickers=frozenset({"BYDDY"}), dry_run=False)

    class _NegativeFractionalTerminalBroker(DryRunBroker):
        def place_whole_share_buy(self, ticker, quantity, ref_id):
            raise WholeSharePlacementTerminalError(
                "rejected",
                {"id": "RH-NEG-FRAC-TERMINAL-1", "state": "rejected", "cumulative_quantity": "-0.5"},
            )

        def same_day_strategy_order_exists_with_ref_id_prefix(self, ticker, trading_day, ref_id_prefixes):
            return None

    broker = _NegativeFractionalTerminalBroker(cash=500.0, reference_prices={"BYDDY": 10.0})

    outcome, _, _, errors, _ = _process_buy_candidate(
        _decision(suggested_amount_usd=10.0, target_price=10.0),
        ctx=_ctx(config, broker),
        running_deployed_7d=0.0,
    )

    assert outcome["status"] == "blocked"
    # "-0.5" is BOTH negative and fractional -- the fractional-integrality
    # check now catches it first (as "unknown"), before the value is even
    # classified as negative; either malformed-data branch is correct here,
    # since both leave the claim reserved rather than releasing it.
    assert "negative" in outcome["skip_reason"] or "unknown" in outcome["skip_reason"]
    assert errors == 1
    ledger = WholeShareAccumulationLedger()
    # Left exactly as record_contribution's own deduction left it -- a
    # negative quantity is not zero-fill evidence, so the $10 stays
    # deducted (reserved), not restored back to pending.
    assert ledger.pending_usd("BYDDY") == 0.0
    assert ledger.shares_purchased_total("BYDDY") == 0


def test_ambiguous_placement_failure_rejects_a_positive_fractional_terminal_quantity(tmp_path, monkeypatch):
    """Codex review ("Reject fractional terminal quantities before
    truncation"): this module's OTHER copy of the terminal-quantity parse
    (the placement-failure/terminal-inclusive reconciliation branch, same
    site the negative-fractional sibling test above already covers) has
    the identical fractional-truncation gap: a POSITIVE fractional
    cumulative_quantity like "0.5" truncates to 0 under int(float(...)),
    wrongly reading as a confirmed zero fill and restoring the claim's
    entire reservation on malformed broker evidence.
    """
    monkeypatch.setenv("DCA_CRON_TRACKING_ROOT", str(tmp_path / "cron_tracking"))
    config = StrategyConfig(whole_share_tickers=frozenset({"BYDDY"}), dry_run=False)

    class _PositiveFractionalTerminalBroker(DryRunBroker):
        def place_whole_share_buy(self, ticker, quantity, ref_id):
            raise WholeSharePlacementTerminalError(
                "filled",
                {"id": "RH-POS-FRAC-TERMINAL-1", "state": "filled", "cumulative_quantity": "0.5"},
            )

        def same_day_strategy_order_exists_with_ref_id_prefix(self, ticker, trading_day, ref_id_prefixes):
            return None

    broker = _PositiveFractionalTerminalBroker(cash=500.0, reference_prices={"BYDDY": 10.0})

    outcome, _, _, errors, _ = _process_buy_candidate(
        _decision(suggested_amount_usd=10.0, target_price=10.0),
        ctx=_ctx(config, broker),
        running_deployed_7d=0.0,
    )

    assert outcome["status"] == "blocked"
    assert "unknown" in outcome["skip_reason"]
    assert errors == 1
    ledger = WholeShareAccumulationLedger()
    # Left exactly as record_contribution's own deduction left it -- a
    # fractional quantity is not zero-fill evidence, so the $10 stays
    # deducted (reserved), not restored back to pending.
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
        cast(Any, _RecordingLedger()),
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
    run_result = RunResult(trading_day=date(2026, 8, 16), decisions=[])
    ctx = _ctx(config, broker, run_result=run_result)

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
    assert ctx.state.day_notional(date(2026, 8, 15)) == 10.0
    assert running_deployed_7d == 10.0
    assert len(run_result.execution_records) == 1
    assert run_result.execution_records[0].order_id == "RH-YESTERDAY-1"
    assert run_result.execution_records[0].notional_usd == 10.0


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


@pytest.mark.parametrize("order_state", ["new", "confirmed", "partially_filled"])
def test_a_nonterminal_ambiguous_match_leaves_the_claim_reserved(tmp_path, monkeypatch, order_state):
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
            matched_order = {
                "id": "RH-STILL-WORKING-1",
                "state": order_state,
                "cumulative_quantity": "0",
            }
            if order_state == "partially_filled":
                # "0" is truthy and quantity is positive, but neither value
                # may settle a live order; the entire claim remains reserved
                # until the order reaches a terminal state.
                matched_order["quantity"] = "1"
            return matched_order

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
    # settle() restores only the three unfilled shares at the cached $4
    # claim price.  The filled share's $4 credit remains consumed even
    # though its actual execution notional ($4.25) is used for reporting.
    assert ledger.pending_usd("BYDDY") == 12.0
    assert ledger.unresolved_claim("BYDDY", "2026-08-16", "afternoon-BYDDY") is None
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


@pytest.mark.parametrize("working_state", ["new", "confirmed", "partially_filled"])
def test_immediate_placement_failure_books_a_still_live_order_found_via_terminal_lookup(
    tmp_path, monkeypatch, working_state
):
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
            return {"id": "RH-RACE-IMMEDIATE-1", "state": working_state, "cumulative_quantity": "1"}

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


def test_live_order_carried_by_placement_failure_envelope_is_booked_without_history_lookup(
    tmp_path, monkeypatch
):
    """A live inner order preserved by the adapter's failure-envelope error
    is authoritative acceptance evidence even when order history has not
    indexed it yet.

    The caller must book the attempted notional and retain the unresolved
    claim instead of reporting a blocked candidate that is invisible to the
    remaining batch risk gates.
    """
    monkeypatch.setenv("DCA_CRON_TRACKING_ROOT", str(tmp_path / "cron_tracking"))
    config = StrategyConfig(
        whole_share_tickers=frozenset({"BYDDY"}), dry_run=False, max_per_order_usd=10.0
    )

    class _LiveFailureEnvelopeBroker(DryRunBroker):
        def place_whole_share_buy(self, ticker, quantity, ref_id):
            raise WholeSharePlacementTerminalError(
                "outer failure carried an accepted inner order",
                order={
                    "id": "RH-LIVE-ENVELOPE-1",
                    "state": "partially_filled",
                    "cumulative_quantity": "1",
                },
            )

        def same_day_strategy_order_exists_with_ref_id_prefix(self, ticker, trading_day, ref_id_prefixes):
            raise AssertionError("the failure envelope already supplied the order evidence")

        def same_day_order_with_ref_id_prefix_any_state(self, ticker, trading_day, ref_id_prefixes):
            raise AssertionError("the failure envelope already supplied the order evidence")

    ctx = _ctx(config, _LiveFailureEnvelopeBroker(cash=500.0, reference_prices={"BYDDY": 4.0}))

    outcome, _, running_deployed_7d, errors, _ = _process_buy_candidate(
        _decision(suggested_amount_usd=16.0, target_price=4.0),
        ctx=ctx,
        running_deployed_7d=0.0,
    )

    assert outcome["status"] == "submitted"
    assert outcome["order_id"] == "RH-LIVE-ENVELOPE-1"
    assert outcome["submitted_amount_usd"] == pytest.approx(8.0)
    assert outcome["whole_share_settlement_reconciliation_needed"] is True
    assert running_deployed_7d == pytest.approx(8.0)
    assert errors == 1
    assert ctx.state.day_order_count(date(2026, 8, 16)) == 1
    assert ctx.state.day_notional(date(2026, 8, 16)) == pytest.approx(8.0)
    ledger = WholeShareAccumulationLedger()
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


@pytest.mark.parametrize("terminal_quantity", [None, "not-a-number"])
def test_immediate_placement_failure_leaves_the_claim_reserved_when_terminal_quantity_is_unknown(
    tmp_path, monkeypatch, terminal_quantity
):
    """Codex review ("Keep terminal claims reserved when fill quantity is
    absent"): the immediate placement-failure branch's own terminal-
    inclusive lookup used to default filled_shares to 0 whenever
    cumulative_quantity was absent -- indistinguishable from an EXPLICIT
    zero -- and then unconditionally restored the full planned notional.
    An absent or malformed quantity on a cancelled/rejected order proves
    nothing about what actually executed; the claim must stay reserved
    instead.
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
            order = {"id": "RH-NO-QTY-1", "state": "cancelled"}
            if terminal_quantity is not None:
                order["cumulative_quantity"] = terminal_quantity
            return order

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


def test_immediate_placement_failure_counts_a_confirmed_zero_fill_toward_the_attempt_cap(
    tmp_path, monkeypatch
):
    """Codex review ("Count confirmed zero-fill orders toward the attempt
    cap"): a confirmed terminal order with an EXPLICIT zero fill is a real
    broker attempt -- config.max_orders_per_day is defined as an attempt
    count where a cancelled/rejected order still consumes one of the
    shared daily slots. The old behavior restored the contribution and
    fell through to the generic failure outcome without ever calling
    record_buy() or retaining the broker's real order ID, leaving the
    in-memory attempt count one too low and the persisted artifact unable
    to reconstruct the missing attempt on a later run.
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
    assert running_deployed_7d == 0.0
    # The confirmed attempt is counted, even though nothing was spent.
    assert ctx.state.day_order_count(date(2026, 8, 16)) == 1
    assert ctx.state.day_notional(date(2026, 8, 16)) == 0.0
    ledger = WholeShareAccumulationLedger()
    assert ledger.shares_purchased_total("BYDDY") == 0
    assert ledger.unresolved_claim("BYDDY", "2026-08-16", "afternoon-BYDDY") is None


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
    assert outcome["whole_share_attempt_deferred"] is True
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


def test_pre_roll_live_order_recovery_queues_a_post_commit_state_booked_marker(
    tmp_path, monkeypatch
):
    """Codex review ("Mark committed ordinary live recoveries"): the
    ORDINARY (gate-passing) live_order_found publish tested above never
    sets state_booked itself, and its claim stays unresolved in the
    ledger exactly like a gated live_order_found row -- so a LATER gated
    reconciliation of the SAME still-live claim needs a way to know
    record_buy() above actually committed, or it double-books the same
    order every time. A marker callback must be queued on
    ``ctx.state.pending_durable_commit_callbacks`` (mirroring the gated
    path's own identical queuing), and once fired against the row this
    run eventually persists, must mark it state_booked=True.
    """
    monkeypatch.setenv("DCA_CRON_TRACKING_ROOT", str(tmp_path / "cron_tracking"))
    ledger = WholeShareAccumulationLedger()
    ledger.record_contribution(
        "BYDDY", "2026-08-15", 10.0, 10.0, stream="afternoon",
        idempotency_key="afternoon-BYDDY", recorded_at="2026-08-15T15:30:00-04:00",
    )

    class _StillLiveBroker(DryRunBroker):
        def same_day_strategy_order_exists_with_ref_id_prefix(self, ticker, trading_day, ref_id_prefixes):
            return {"id": "RH-QUEUED-MARKER-ORDINARY", "state": "confirmed", "cumulative_quantity": "0"}

    artifacts_dir = tmp_path / "artifacts"
    artifacts_dir.mkdir()
    state = StrategyState()
    ctx = ProcessCandidateContext(
        config=StrategyConfig(whole_share_tickers=frozenset({"BYDDY"}), dry_run=False),
        state=state,
        broker=_StillLiveBroker(cash=500.0, reference_prices={"BYDDY": 10.0}),
        robinhood_data=None,
        now=datetime(2026, 8, 16, 15, 45, tzinfo=EASTERN),
        trading_day=date(2026, 8, 16),
        guard_active=False,
        prior_same_day_allocations={},
        scheduled=True,
        run_result=None,
        artifacts_dir=str(artifacts_dir),
    )
    _near_close_dca._IN_FLIGHT_CANDIDATE_OUTCOMES["outcomes"] = {}

    _reconcile_cross_day_whole_share_claims_for_batch(ctx, 0.0)

    assert len(state.pending_durable_commit_callbacks) == 1

    # Simulate what run_pipeline()'s own reporting would have flushed this
    # in-flight outcome into by the time state_transaction()'s callback
    # actually fires (well after run_pipeline() itself has returned).
    _near_close_dca.atomic_write_text(
        artifacts_dir / "run_state.json",
        _near_close_dca.json.dumps({
            "buy_candidates": [{
                "ticker": "BYDDY",
                "status": "submitted",
                "order_id": "RH-QUEUED-MARKER-ORDINARY",
                "whole_share_settlement_reconciliation_needed": True,
                "whole_share_claim_breakdown": [{"trading_day": "2026-08-15", "amount_usd": 10.0}],
            }]
        }),
    )
    for callback in state.pending_durable_commit_callbacks:
        callback()

    persisted = _near_close_dca.json.loads((artifacts_dir / "run_state.json").read_bytes())
    assert persisted["buy_candidates"][0]["state_booked"] is True


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


def test_pre_roll_live_order_recovery_rejects_a_negative_submitted_quantity(tmp_path, monkeypatch):
    """Codex review ("Reject negative live-order quantities before
    booking"): a still-live matched order's own "quantity" field can be
    malformed (e.g. "-1", or a fractional "-0.5" that int() truncates to an
    indistinguishable 0). Booking a negative or zero-from-truncation
    planned_notional_usd here would create false cap/kill-switch headroom
    for later candidates in the batch even though a real, unresolved
    broker order exists. Must fall back to the claim's own unclamped
    shares_to_buy, same as an absent/unparseable quantity already does.
    """
    monkeypatch.setenv("DCA_CRON_TRACKING_ROOT", str(tmp_path / "cron_tracking"))
    ledger = WholeShareAccumulationLedger()
    # $30 pending at a cached $10.00/share price plans 3 shares.
    ledger.record_contribution(
        "BYDDY", "2026-08-15", 30.0, 10.0, stream="afternoon",
        idempotency_key="afternoon-BYDDY", recorded_at="2026-08-15T15:30:00-04:00",
    )

    class _NegativeQuantityStillLiveBroker(DryRunBroker):
        def same_day_strategy_order_exists_with_ref_id_prefix(self, ticker, trading_day, ref_id_prefixes):
            return {"id": "RH-NEG-QTY-LIVE", "state": "confirmed", "quantity": "-0.5", "cumulative_quantity": "0"}

    ctx = _ctx(
        StrategyConfig(whole_share_tickers=frozenset({"BYDDY"}), dry_run=False),
        _NegativeQuantityStillLiveBroker(cash=500.0, reference_prices={"BYDDY": 10.0}),
    )
    _near_close_dca._IN_FLIGHT_CANDIDATE_OUTCOMES["outcomes"] = {}

    deployed = _reconcile_cross_day_whole_share_claims_for_batch(ctx, 0.0)

    # Falls back to the claim's own unclamped 3-share/$30 plan, never a
    # negative or zero notional from the malformed quantity.
    assert deployed == pytest.approx(30.0)
    outcome = _near_close_dca._IN_FLIGHT_CANDIDATE_OUTCOMES["outcomes"]["BYDDY"]
    assert outcome["submitted_amount_usd"] == pytest.approx(30.0)
    assert outcome["whole_share_quantity"] == 3
    assert ledger.unresolved_claim("BYDDY", "2026-08-15", "afternoon-BYDDY") is not None


def test_pre_roll_live_order_recovery_rejects_a_positive_fractional_submitted_quantity(
    tmp_path, monkeypatch
):
    """Codex review ("Reject fractional live-order quantities before
    truncation"): this path places WHOLE-share orders only, so a positive
    but FRACTIONAL submitted quantity like "0.5" is just as malformed as a
    negative one -- int() truncates it to 0, silently understating the
    booked notional to zero for a real, still-live broker order. Must fall
    back to the claim's own unclamped shares_to_buy, same as a negative or
    unparseable quantity already does.
    """
    monkeypatch.setenv("DCA_CRON_TRACKING_ROOT", str(tmp_path / "cron_tracking"))
    ledger = WholeShareAccumulationLedger()
    # $30 pending at a cached $10.00/share price plans 3 shares.
    ledger.record_contribution(
        "BYDDY", "2026-08-15", 30.0, 10.0, stream="afternoon",
        idempotency_key="afternoon-BYDDY", recorded_at="2026-08-15T15:30:00-04:00",
    )

    class _FractionalQuantityStillLiveBroker(DryRunBroker):
        def same_day_strategy_order_exists_with_ref_id_prefix(self, ticker, trading_day, ref_id_prefixes):
            return {"id": "RH-FRAC-QTY-LIVE", "state": "confirmed", "quantity": "0.5", "cumulative_quantity": "0"}

    ctx = _ctx(
        StrategyConfig(whole_share_tickers=frozenset({"BYDDY"}), dry_run=False),
        _FractionalQuantityStillLiveBroker(cash=500.0, reference_prices={"BYDDY": 10.0}),
    )
    _near_close_dca._IN_FLIGHT_CANDIDATE_OUTCOMES["outcomes"] = {}

    deployed = _reconcile_cross_day_whole_share_claims_for_batch(ctx, 0.0)

    # Falls back to the claim's own unclamped 3-share/$30 plan, never a
    # zero notional from the truncated fractional quantity.
    assert deployed == pytest.approx(30.0)
    outcome = _near_close_dca._IN_FLIGHT_CANDIDATE_OUTCOMES["outcomes"]["BYDDY"]
    assert outcome["submitted_amount_usd"] == pytest.approx(30.0)
    assert outcome["whole_share_quantity"] == 3
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


def test_find_same_day_recorded_order_recognizes_filled_status(tmp_path):
    """Codex review ("Recognize filled gated rows during ordinary retry
    deduplication"): _persist_gated_whole_share_recovery persists a
    confirmed terminal fill with status="filled", not "submitted"
    ("Mark confirmed gated fills as filled"). _find_same_day_recorded_order
    (and _same_day_order_id_already_recorded, which wraps it) must still
    recognize that row as an already-recorded order -- otherwise, if the
    gated hook's own atomic write later fails after persisting evidence, a
    subsequent ordinary gate-passing retry re-finds this claim, sees no
    matching "submitted" row, and republishes the full notional a second
    time even though the gated artifact and StrategyState booking already
    survived.
    """
    from test_near_close_dca import _write_scheduled_afternoon_artifact

    history_root = str(tmp_path / "runs")
    trading_day = date(2026, 8, 17)
    _write_scheduled_afternoon_artifact(
        history_root,
        date(2026, 8, 16),
        [{"ticker": "BYDDY", "status": "filled", "order_id": "RH-FILLED-GATED-1", "planned_amount_usd": 10.0}],
    )

    found = _near_close_dca._find_same_day_recorded_order(
        "RH-FILLED-GATED-1", "BYDDY", trading_day, history_root
    )
    assert found is not None
    assert found["planned_amount_usd"] == pytest.approx(10.0)
    assert _near_close_dca._same_day_order_id_already_recorded(
        "RH-FILLED-GATED-1", "BYDDY", trading_day, history_root
    )


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
            # One persisted row can aggregate multiple broker orders.  Both
            # claims deliberately share a date, so reconciliation must use
            # the order_id carried by each breakdown entry rather than
            # attributing the row total (or the first same-date entry) to the
            # order that just settled.
            "planned_amount_usd": 30.0,
            "whole_share_order_ids": ["RH-OTHER-ORDER", "RH-DELTA-FILL-1"],
            "whole_share_claim_breakdown": [
                {"trading_day": "2026-08-15", "order_id": "RH-OTHER-ORDER", "amount_usd": 20.0},
                {"trading_day": "2026-08-15", "order_id": "RH-DELTA-FILL-1", "amount_usd": 10.0},
            ],
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


def test_an_ambiguous_legacy_breakdown_skips_the_reconciliation_delta_instead_of_assuming_zero(
    tmp_path, monkeypatch
):
    """discussion_r3838771875: a legacy artifact's breakdown can aggregate
    TWO orders for the same claim date with NEITHER entry carrying an
    order_id (written before order_id was added to breakdown entries) --
    the settling order's own order_id then matches nothing, and the
    trading-day-only fallback is itself ambiguous (two entries share that
    date). Treating that ambiguity as "nothing was recorded" (the prior
    behavior: recorded_amount stays 0.0) would publish the ENTIRE fill as
    a fresh reconciliation delta, double-counting whichever of the two
    legacy entries actually already covered this order's own provisional
    amount. The conservative outcome is to skip the adjustment entirely.
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
            "ticker": "BYDDY", "status": "submitted", "order_id": "RH-DELTA-FILL-1",
            "planned_amount_usd": 30.0,
            "whole_share_order_ids": ["RH-OTHER-ORDER", "RH-DELTA-FILL-1"],
            # Legacy shape: neither breakdown entry carries an order_id, and
            # both share the SAME claim date -- ambiguous by construction.
            "whole_share_claim_breakdown": [
                {"trading_day": "2026-08-15", "amount_usd": 20.0},
                {"trading_day": "2026-08-15", "amount_usd": 10.0},
            ],
        }],
    )

    class _FilledAboveCachedPriceBroker(DryRunBroker):
        def same_day_strategy_order_exists_with_ref_id_prefix(self, ticker, trading_day, ref_id_prefixes):
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
    # No adjustment is published -- neither the full $12 (which would
    # double-count whichever legacy entry already covered this order) nor
    # any other guessed delta.
    assert deployed == 0.0
    assert "BYDDY" not in _near_close_dca._IN_FLIGHT_CANDIDATE_OUTCOMES["outcomes"]


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
    assert unresolved is not None

    def _raising_preserve_pending(*args, **kwargs):
        raise RuntimeError("ledger write failed")

    ledger.preserve_pending_if_claimed = _raising_preserve_pending

    class _ZeroFillBroker:
        def same_day_strategy_order_exists_with_ref_id_prefix(self, ticker, trading_day, ref_id_prefixes):
            return None

        def same_day_order_with_ref_id_prefix_any_state(self, ticker, trading_day, ref_id_prefixes):
            return {"id": "RH-ZERO-RELEASE-FAILS", "state": "rejected", "cumulative_quantity": "0"}

    reconciled = _near_close_dca._reconcile_unresolved_whole_share_claim(
        ledger, cast(Any, _ZeroFillBroker()), "BYDDY", "afternoon", "afternoon-BYDDY", "2026-08-15", unresolved
    )

    assert reconciled is None
    # Claim remains exactly as reserved as before -- the release failed.
    assert ledger.unresolved_claim("BYDDY", "2026-08-15", "afternoon-BYDDY") is not None


def test_reconcile_rejects_a_negative_terminal_fill_quantity(tmp_path, monkeypatch):
    """Codex review ("Reject negative terminal fill quantities"): a
    parseable but NEGATIVE ``cumulative_quantity`` (e.g. ``"-1"``) on a
    terminal order found via the any-state fallback is malformed broker
    data, not evidence that nothing executed. Falling through to the
    explicit-zero-fill branch on a negative value would restore the
    claim's ENTIRE reservation and resolve it as a confirmed zero fill --
    making credit backing an unknown (or genuinely real) fill reusable.
    Only an exact zero is trustworthy zero-fill evidence; a negative
    quantity must leave the claim exactly as reserved, for a later retry
    once better evidence is available.
    """
    monkeypatch.setenv("DCA_CRON_TRACKING_ROOT", str(tmp_path / "cron_tracking"))
    ledger = WholeShareAccumulationLedger()
    ledger.record_contribution(
        "BYDDY", "2026-08-15", 10.0, 10.0, stream="afternoon",
        idempotency_key="afternoon-BYDDY", recorded_at="2026-08-15T15:30:00-04:00",
    )
    unresolved = ledger.unresolved_claim("BYDDY", "2026-08-15", "afternoon-BYDDY")
    assert unresolved is not None

    class _NegativeQuantityBroker:
        def same_day_strategy_order_exists_with_ref_id_prefix(self, ticker, trading_day, ref_id_prefixes):
            return None

        def same_day_order_with_ref_id_prefix_any_state(self, ticker, trading_day, ref_id_prefixes):
            return {"id": "RH-NEGATIVE-QTY-1", "state": "rejected", "cumulative_quantity": "-1"}

    reconciled = _near_close_dca._reconcile_unresolved_whole_share_claim(
        ledger, cast(Any, _NegativeQuantityBroker()), "BYDDY", "afternoon", "afternoon-BYDDY", "2026-08-15",
        unresolved,
    )

    assert reconciled is None
    # Left exactly as reserved -- a negative quantity is not zero-fill evidence.
    assert ledger.unresolved_claim("BYDDY", "2026-08-15", "afternoon-BYDDY") is not None


def test_reconcile_rejects_a_negative_fractional_terminal_fill_quantity(tmp_path, monkeypatch):
    """Codex review ("Check negativity before truncating terminal
    quantities"): a negative FRACTIONAL cumulative_quantity such as
    ``"-0.5"`` truncates to ``0`` under ``int(float(raw_terminal_quantity))``
    -- indistinguishable from an ordinary explicit zero once truncated, so a
    ``filled_shares < 0`` check performed AFTER truncation never catches it.
    That let this malformed value slip through to the explicit-zero-fill
    branch, restoring the claim's entire reservation on the strength of a
    value that was never actually zero. The sign must be checked on the RAW
    parsed float, before truncation.
    """
    monkeypatch.setenv("DCA_CRON_TRACKING_ROOT", str(tmp_path / "cron_tracking"))
    ledger = WholeShareAccumulationLedger()
    ledger.record_contribution(
        "BYDDY", "2026-08-15", 10.0, 10.0, stream="afternoon",
        idempotency_key="afternoon-BYDDY", recorded_at="2026-08-15T15:30:00-04:00",
    )
    unresolved = ledger.unresolved_claim("BYDDY", "2026-08-15", "afternoon-BYDDY")
    assert unresolved is not None

    class _NegativeFractionalQuantityBroker:
        def same_day_strategy_order_exists_with_ref_id_prefix(self, ticker, trading_day, ref_id_prefixes):
            return None

        def same_day_order_with_ref_id_prefix_any_state(self, ticker, trading_day, ref_id_prefixes):
            return {"id": "RH-NEGATIVE-FRACTIONAL-QTY-1", "state": "rejected", "cumulative_quantity": "-0.5"}

    reconciled = _near_close_dca._reconcile_unresolved_whole_share_claim(
        ledger, cast(Any, _NegativeFractionalQuantityBroker()), "BYDDY", "afternoon", "afternoon-BYDDY",
        "2026-08-15", unresolved,
    )

    assert reconciled is None
    # Left exactly as reserved -- a negative quantity is not zero-fill evidence,
    # even after truncating toward zero would otherwise hide the sign.
    assert ledger.unresolved_claim("BYDDY", "2026-08-15", "afternoon-BYDDY") is not None


def test_reconcile_rejects_a_positive_fractional_terminal_fill_quantity(tmp_path, monkeypatch):
    """Codex review ("Reject fractional terminal quantities before
    truncation"): a POSITIVE fractional cumulative_quantity such as
    ``"0.5"`` on a terminal "filled" order truncates to ``0`` under
    ``int(float(...))``, wrongly reading as a confirmed zero fill and
    restoring the claim's entire reservation on the strength of a value
    that was never actually zero. Only a genuine whole number is
    trustworthy evidence; a fractional value must leave the claim exactly
    as reserved, for a later retry once better evidence is available.
    """
    monkeypatch.setenv("DCA_CRON_TRACKING_ROOT", str(tmp_path / "cron_tracking"))
    ledger = WholeShareAccumulationLedger()
    ledger.record_contribution(
        "BYDDY", "2026-08-15", 10.0, 10.0, stream="afternoon",
        idempotency_key="afternoon-BYDDY", recorded_at="2026-08-15T15:30:00-04:00",
    )
    unresolved = ledger.unresolved_claim("BYDDY", "2026-08-15", "afternoon-BYDDY")
    assert unresolved is not None

    class _PositiveFractionalQuantityBroker:
        def same_day_strategy_order_exists_with_ref_id_prefix(self, ticker, trading_day, ref_id_prefixes):
            return None

        def same_day_order_with_ref_id_prefix_any_state(self, ticker, trading_day, ref_id_prefixes):
            return {"id": "RH-POSITIVE-FRACTIONAL-QTY-1", "state": "filled", "cumulative_quantity": "0.5"}

    reconciled = _near_close_dca._reconcile_unresolved_whole_share_claim(
        ledger, cast(Any, _PositiveFractionalQuantityBroker()), "BYDDY", "afternoon", "afternoon-BYDDY",
        "2026-08-15", unresolved,
    )

    assert reconciled is None
    # Left exactly as reserved -- a fractional quantity is not confirmed
    # zero-fill evidence, even after truncating toward zero would
    # otherwise hide it.
    assert ledger.unresolved_claim("BYDDY", "2026-08-15", "afternoon-BYDDY") is not None


def test_reconcile_falls_back_to_submitted_quantity_when_cumulative_quantity_is_unparseable(
    tmp_path, monkeypatch
):
    """Codex review ("Treat unusable cumulative quantities as unknown"): a
    filled payload can carry a falsey-but-non-null ``cumulative_quantity``
    such as ``""`` -- present, so the "field is absent" fallback never
    triggers, but not parseable as a number either. Silently keeping the
    unclamped ``shares_to_buy`` default in that case overstates the fill
    whenever a per-order ceiling clamped the actually-submitted quantity
    down from what the claim originally planned (2 shares planned, only 1
    actually submitted and filled here) -- it must instead fall back to the
    order's own submitted ``quantity``, the same value the caller already
    trusts for planning around a live/non-terminal match.
    """
    monkeypatch.setenv("DCA_CRON_TRACKING_ROOT", str(tmp_path / "cron_tracking"))
    ledger = WholeShareAccumulationLedger()
    ledger.record_contribution(
        "BYDDY", "2026-08-15", 20.0, 10.0, stream="afternoon",
        idempotency_key="afternoon-BYDDY", recorded_at="2026-08-15T15:30:00-04:00",
    )
    unresolved = ledger.unresolved_claim("BYDDY", "2026-08-15", "afternoon-BYDDY")
    assert unresolved["shares_to_buy"] == 2

    class _UnparseableCumulativeQuantityBroker:
        def same_day_strategy_order_exists_with_ref_id_prefix(self, ticker, trading_day, ref_id_prefixes):
            return {
                "id": "RH-CLAMPED-1",
                "state": "filled",
                "cumulative_quantity": "",
                "quantity": "1",
                "average_price": "10.00",
            }

    reconciled = _near_close_dca._reconcile_unresolved_whole_share_claim(
        ledger, _UnparseableCumulativeQuantityBroker(), "BYDDY", "afternoon", "afternoon-BYDDY", "2026-08-15",
        unresolved,
    )

    assert reconciled is not None
    assert reconciled["shares_purchased"] == 1


def test_reconcile_rejects_a_negative_fractional_fill_from_the_live_match_branch(tmp_path, monkeypatch):
    """Codex review ("Reject negative fractional fills before integer
    conversion"): the DIRECT matched_order/"filled" branch (the primary
    live-order lookup, distinct from the any-state-fallback and
    placement-failure duplicate this same negative-quantity bug was already
    fixed in) parses cumulative_quantity with the identical
    int(float(...)) truncation. A negative fractional value like "-0.5"
    truncates to 0, which -- unlike the other two sites -- doesn't just get
    rejected outright here: shares_purchased=0 is a legitimate "confirmed
    zero fill" shape to _settle_whole_share_claim, so this would restore the
    claim's FULL reservation and resolve it on malformed data. A negative
    parsed value must be treated the same as absent/unparseable -- falling
    back to the order's own submitted quantity (1, clamped from the 2-share
    claim), not settling as a zero fill.
    """
    monkeypatch.setenv("DCA_CRON_TRACKING_ROOT", str(tmp_path / "cron_tracking"))
    ledger = WholeShareAccumulationLedger()
    ledger.record_contribution(
        "BYDDY", "2026-08-15", 20.0, 10.0, stream="afternoon",
        idempotency_key="afternoon-BYDDY", recorded_at="2026-08-15T15:30:00-04:00",
    )
    unresolved = ledger.unresolved_claim("BYDDY", "2026-08-15", "afternoon-BYDDY")
    assert unresolved["shares_to_buy"] == 2

    class _NegativeFractionalLiveMatchBroker:
        def same_day_strategy_order_exists_with_ref_id_prefix(self, ticker, trading_day, ref_id_prefixes):
            return {
                "id": "RH-NEG-FRAC-LIVE-1",
                "state": "filled",
                "cumulative_quantity": "-0.5",
                "quantity": "1",
                "average_price": "10.00",
            }

    reconciled = _near_close_dca._reconcile_unresolved_whole_share_claim(
        ledger, _NegativeFractionalLiveMatchBroker(), "BYDDY", "afternoon", "afternoon-BYDDY", "2026-08-15",
        unresolved,
    )

    assert reconciled is not None
    assert reconciled["shares_purchased"] == 1


def test_reconcile_rejects_a_positive_fractional_fill_from_the_live_match_branch(tmp_path, monkeypatch):
    """Codex review ("Reject fractional terminal quantities before
    truncation"): the DIRECT matched_order/"filled" branch's negative-only
    guard let a POSITIVE fractional cumulative_quantity like "0.5" through
    -- int() truncates it to 0, which _settle_whole_share_claim() reads as
    a legitimate confirmed zero fill, restoring the claim's FULL
    reservation on malformed data despite nonzero fill evidence. Must fall
    back to the order's own submitted quantity (1, clamped from the
    2-share claim), same as a negative or unparseable value already does.
    """
    monkeypatch.setenv("DCA_CRON_TRACKING_ROOT", str(tmp_path / "cron_tracking"))
    ledger = WholeShareAccumulationLedger()
    ledger.record_contribution(
        "BYDDY", "2026-08-15", 20.0, 10.0, stream="afternoon",
        idempotency_key="afternoon-BYDDY", recorded_at="2026-08-15T15:30:00-04:00",
    )
    unresolved = ledger.unresolved_claim("BYDDY", "2026-08-15", "afternoon-BYDDY")
    assert unresolved["shares_to_buy"] == 2

    class _PositiveFractionalLiveMatchBroker:
        def same_day_strategy_order_exists_with_ref_id_prefix(self, ticker, trading_day, ref_id_prefixes):
            return {
                "id": "RH-POS-FRAC-LIVE-1",
                "state": "filled",
                "cumulative_quantity": "0.5",
                "quantity": "1",
                "average_price": "10.00",
            }

    reconciled = _near_close_dca._reconcile_unresolved_whole_share_claim(
        ledger, _PositiveFractionalLiveMatchBroker(), "BYDDY", "afternoon", "afternoon-BYDDY", "2026-08-15",
        unresolved,
    )

    assert reconciled is not None
    assert reconciled["shares_purchased"] == 1


def test_reconcile_rejects_a_fractional_submitted_quantity_fallback(tmp_path, monkeypatch):
    """Codex review ("Validate submitted-quantity fallbacks before
    truncation"): when a terminal "filled" match has no usable
    cumulative_quantity, the DIRECT matched_order branch falls back to the
    order's own "quantity" field -- but that fallback lacked the same
    finite/nonnegative/whole-number guard already applied to
    cumulative_quantity. A malformed positive fractional "quantity" like
    "0.5" would truncate to 0, settling the claim as a confirmed zero fill
    (restoring the FULL reservation) despite nonzero fill evidence. Must
    fall back to the claim's own already-validated shares_to_buy instead.
    """
    monkeypatch.setenv("DCA_CRON_TRACKING_ROOT", str(tmp_path / "cron_tracking"))
    ledger = WholeShareAccumulationLedger()
    ledger.record_contribution(
        "BYDDY", "2026-08-15", 20.0, 10.0, stream="afternoon",
        idempotency_key="afternoon-BYDDY", recorded_at="2026-08-15T15:30:00-04:00",
    )
    unresolved = ledger.unresolved_claim("BYDDY", "2026-08-15", "afternoon-BYDDY")
    assert unresolved["shares_to_buy"] == 2

    class _FractionalSubmittedQuantityBroker:
        def same_day_strategy_order_exists_with_ref_id_prefix(self, ticker, trading_day, ref_id_prefixes):
            return {
                "id": "RH-FRAC-SUBMITTED-1",
                "state": "filled",
                "cumulative_quantity": None,
                "quantity": "0.5",
                "average_price": "10.00",
            }

    reconciled = _near_close_dca._reconcile_unresolved_whole_share_claim(
        ledger, _FractionalSubmittedQuantityBroker(), "BYDDY", "afternoon", "afternoon-BYDDY", "2026-08-15",
        unresolved,
    )

    assert reconciled is not None
    # Falls back to the claim's own shares_to_buy (2), NOT truncated to 0.
    assert reconciled["shares_purchased"] == 2


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


@pytest.mark.parametrize("fresh_status", ["submitted", "failed"])
def test_merge_whole_share_outcomes_preserves_fresh_attempt_after_cross_day_zero_fill(fresh_status):
    recovered = {
        "status": "failed",
        "order_id": "old-zero-fill",
        "whole_share_zero_fill_attempt": True,
        "whole_share_claim_breakdown": [{"trading_day": "2026-08-15", "amount_usd": 0.0}],
    }
    fresh = {
        "status": fresh_status,
        "order_id": "fresh-order",
        "submitted_amount_usd": 10.0 if fresh_status == "submitted" else 0.0,
        "whole_share_quantity": 1 if fresh_status == "submitted" else 0,
    }

    merged = _near_close_dca._merge_whole_share_submission_outcomes(recovered, fresh)

    assert merged["status"] == fresh_status
    assert merged["whole_share_order_ids"] == ["old-zero-fill", "fresh-order"]
    assert merged["whole_share_attempt_breakdown"] == [
        {"trading_day": "2026-08-15"},
        {"trading_day": None},
    ]


def test_merge_whole_share_outcomes_excludes_failed_requested_notional():
    recovered = {
        "status": "submitted",
        "order_id": "recovered-fill",
        "submitted_amount_usd": 25.0,
        "whole_share_quantity": 2,
    }
    failed = {
        "status": "failed",
        "submitted_amount_usd": 40.0,
        "whole_share_quantity": 0,
    }

    merged = _near_close_dca._merge_whole_share_submission_outcomes(recovered, failed)

    assert merged["status"] == "submitted"
    assert merged["submitted_amount_usd"] == pytest.approx(25.0)
    assert merged["whole_share_attempt_breakdown"] == [
        {"trading_day": None},
        {"trading_day": None},
    ]


def test_merge_whole_share_outcomes_retains_recovered_order_id_after_no_id_failure():
    """Codex review ("Retain the recovered order ID after a no-ID failure"):
    ``merged = dict(new)`` keeps only NEW's own top-level ``order_id`` -- if
    today's fresh placement fails without ever obtaining a broker order id,
    that scalar silently becomes None even though the recovered side's real
    order id is still present. build_usage_proxy only counts a submitted
    outcome toward orders-placed via this top-level field, so a merged row
    would otherwise report zero orders for a run that in fact recovered one.
    """
    recovered = {
        "status": "submitted",
        "order_id": "recovered-fill",
        "submitted_amount_usd": 25.0,
        "whole_share_quantity": 2,
    }
    failed_no_id = {
        "status": "failed",
        "order_id": None,
        "submitted_amount_usd": 40.0,
        "whole_share_quantity": 0,
    }

    merged = _near_close_dca._merge_whole_share_submission_outcomes(recovered, failed_no_id)

    assert merged["status"] == "submitted"
    assert merged["order_id"] == "recovered-fill"
    assert merged["whole_share_order_ids"] == ["recovered-fill"]


def test_merge_whole_share_outcomes_keeps_deployed_order_id_on_mixed_failure_with_own_id():
    """discussion_r3838771868: unlike the no-ID case above, a FAILED
    attempt can still carry its OWN broker order id (e.g. a confirmed
    rejected/zero-fill order) -- the prior fallback only fired when the
    new side's scalar id was falsy, so it left the top-level ``order_id``
    at the failed order's id even though ``status``/``submitted_amount_usd``
    describe the recovered (existing) side's real deployment. Terminal-
    order-id filtering keys off that top-level id, so leaving it on the
    failed order would make load_prior_same_day_allocations() skip the
    whole aggregate and erase the recovered spend along with it.
    """
    recovered = {
        "status": "submitted",
        "order_id": "recovered-fill",
        "submitted_amount_usd": 25.0,
        "whole_share_quantity": 2,
    }
    failed_with_own_id = {
        "status": "failed",
        "order_id": "rejected-order",
        "submitted_amount_usd": 40.0,
        "whole_share_quantity": 0,
    }

    merged = _near_close_dca._merge_whole_share_submission_outcomes(recovered, failed_with_own_id)

    assert merged["status"] == "submitted"
    assert merged["submitted_amount_usd"] == pytest.approx(25.0)
    # The scalar id must track the order whose notional was actually
    # retained -- the recovered/existing side, not the failed newcomer.
    assert merged["order_id"] == "recovered-fill"
    # Both broker orders are still individually discoverable for audit.
    assert set(merged["whole_share_order_ids"]) == {"recovered-fill", "rejected-order"}


def test_merge_whole_share_outcomes_excludes_settlement_adjustment_attempt():
    adjustment = {
        "status": "submitted",
        "submitted_amount_usd": 2.0,
        "whole_share_settlement_adjustment": True,
        "whole_share_claim_breakdown": [{"trading_day": "2026-08-20", "amount_usd": 2.0}],
    }
    fresh = {
        "status": "submitted",
        "order_id": "fresh-order",
        "submitted_amount_usd": 10.0,
        "whole_share_quantity": 1,
    }

    merged = _near_close_dca._merge_whole_share_submission_outcomes(adjustment, fresh)

    assert merged["whole_share_attempt_breakdown"] == [{"trading_day": None}]


def test_merge_whole_share_outcomes_clears_adjustment_marker_when_aggregating_a_real_attempt():
    """Codex review ("Clear the adjustment marker when aggregating real
    attempts"): ``merged = dict(new)`` keeps only NEW's own top-level
    ``whole_share_settlement_adjustment`` marker. When NEW is itself a
    settlement adjustment but EXISTING contributed a real physical attempt
    (e.g. a live recovery merged with its own later terminal correction),
    that scalar must not stay True -- load_prior_same_day_allocations()
    gates the row's ENTIRE attempt count on this one field, so a stale
    True would suppress the real attempt existing's own breakdown entry
    represents, even though the merged whole_share_attempt_breakdown
    correctly carries it.
    """
    real_attempt = {
        "status": "submitted",
        "order_id": "live-order",
        "submitted_amount_usd": 10.0,
        "whole_share_quantity": 1,
        "whole_share_claim_breakdown": [{"trading_day": "2026-08-20", "amount_usd": 10.0}],
    }
    terminal_adjustment = {
        "status": "submitted",
        "submitted_amount_usd": 2.0,
        "whole_share_settlement_adjustment": True,
        "whole_share_claim_breakdown": [{"trading_day": "2026-08-20", "amount_usd": 2.0}],
    }

    merged = _near_close_dca._merge_whole_share_submission_outcomes(real_attempt, terminal_adjustment)

    # The real attempt survives in the breakdown...
    assert merged["whole_share_attempt_breakdown"] == [{"trading_day": "2026-08-20"}]
    # ...so the merged row must NOT be marked adjustment-only, or that real
    # attempt gets excluded from attempt_count entirely.
    assert merged["whole_share_settlement_adjustment"] is False


def test_merge_whole_share_outcomes_marks_adjustment_only_when_both_sides_are_adjustments():
    """Companion to the test above: when EVERY contributing side is itself
    a settlement adjustment (no physical attempt anywhere), the merged row
    correctly stays marked adjustment-only.
    """
    first_adjustment = {
        "status": "submitted",
        "submitted_amount_usd": 2.0,
        "whole_share_settlement_adjustment": True,
        "whole_share_claim_breakdown": [{"trading_day": "2026-08-20", "amount_usd": 2.0}],
    }
    second_adjustment = {
        "status": "submitted",
        "submitted_amount_usd": 1.0,
        "whole_share_settlement_adjustment": True,
        "whole_share_claim_breakdown": [{"trading_day": "2026-08-21", "amount_usd": 1.0}],
    }

    merged = _near_close_dca._merge_whole_share_submission_outcomes(first_adjustment, second_adjustment)

    assert merged["whole_share_attempt_breakdown"] == []
    assert merged["whole_share_settlement_adjustment"] is True


def test_merge_whole_share_outcomes_defers_unreleased_zero_fill_attempt():
    recovered = {
        "status": "submitted",
        "order_id": "recovered-order",
        "submitted_amount_usd": 10.0,
        "whole_share_quantity": 1,
    }
    unreleased_zero_fill = {
        "status": "failed",
        "order_id": "deferred-zero-fill",
        "whole_share_attempt_deferred": True,
    }

    merged = _near_close_dca._merge_whole_share_submission_outcomes(recovered, unreleased_zero_fill)

    assert merged["whole_share_attempt_breakdown"] == [{"trading_day": None}]


def test_publish_or_merge_whole_share_outcome_merges_a_prior_zero_fill_recovery():
    """Codex review ("Merge zero-fill evidence before later submitted
    recoveries"): a confirmed zero-fill recovery publishes "status":
    "failed" (see _book_reconciled_whole_share_order's own
    zero_fill_attempt branch -- nothing was purchased), so the merge
    gate's old ``existing.status == "submitted"`` check alone missed it.
    When the SAME cross-day pre-pass then reconciles a LATER, separate
    claim for the SAME ticker as genuinely submitted,
    _publish_or_merge_whole_share_outcome must MERGE with that zero-fill
    evidence, not silently overwrite and lose it -- its order id and
    attempt-breakdown entry represent a real attempt already counted via
    record_buy(..., 0.0).
    """
    _near_close_dca._IN_FLIGHT_CANDIDATE_OUTCOMES["outcomes"] = {
        "BYDDY": {
            "status": "failed",
            "order_id": "RH-ZERO-FILL-1",
            "whole_share_zero_fill_attempt": True,
            "whole_share_claim_breakdown": [
                {"trading_day": "2026-08-15", "order_id": "RH-ZERO-FILL-1", "amount_usd": 0.0}
            ],
        }
    }

    _near_close_dca._publish_or_merge_whole_share_outcome(
        "BYDDY",
        {
            "status": "submitted",
            "order_id": "RH-SUBMITTED-1",
            "submitted_amount_usd": 10.0,
            "whole_share_quantity": 1,
            "whole_share_claim_breakdown": [
                {"trading_day": "2026-08-16", "order_id": "RH-SUBMITTED-1", "amount_usd": 10.0}
            ],
        },
    )

    merged = _near_close_dca._IN_FLIGHT_CANDIDATE_OUTCOMES["outcomes"]["BYDDY"]
    assert merged["status"] == "submitted"
    # The zero-fill order's id survives the merge, not just the new one's.
    assert set(merged["whole_share_order_ids"]) == {"RH-ZERO-FILL-1", "RH-SUBMITTED-1"}
    # Both physical attempts are represented in the breakdown -- the
    # zero-fill's own real attempt must not be lost.
    assert len(merged["whole_share_attempt_breakdown"]) == 2


def test_persisted_whole_share_attempt_breakdown_marks_standalone_deferred_as_non_countable():
    """Codex review ("Persist standalone deferred attempts as non-countable"):
    a standalone whole_share_attempt_deferred outcome never passes through
    _merge_whole_share_submission_outcomes when there is no recovered
    outcome to merge with, so it carries no whole_share_attempt_breakdown
    of its own. Persisting that absence as None makes
    load_prior_same_day_allocations fall back to counting one attempt today,
    even though the physical attempt is deliberately left for reconciliation
    to book later -- double-counting the same rejected order.
    """
    deferred_only = {
        "status": "failed",
        "order_id": "deferred-zero-fill",
        "whole_share_attempt_deferred": True,
    }

    assert _near_close_dca._persisted_whole_share_attempt_breakdown(deferred_only) == []


def test_persisted_whole_share_attempt_breakdown_preserves_existing_breakdown():
    outcome = {
        "status": "submitted",
        "whole_share_attempt_breakdown": [{"trading_day": "2026-08-15"}],
    }

    assert _near_close_dca._persisted_whole_share_attempt_breakdown(outcome) == [
        {"trading_day": "2026-08-15"}
    ]


def test_persisted_whole_share_attempt_breakdown_leaves_ordinary_outcome_unset():
    outcome = {"status": "submitted"}

    assert _near_close_dca._persisted_whole_share_attempt_breakdown(outcome) is None


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


def test_whole_share_submission_merge_preserves_ids_from_both_aggregates():
    """Nested aggregation must not discard order IDs already carried by the newer outcome."""
    existing = {
        "status": "submitted",
        "order_id": "RH-CLAIM-2",
        "whole_share_order_ids": ["RH-CLAIM-1", "RH-CLAIM-2"],
        "submitted_amount_usd": 20.0,
        "whole_share_quantity": 2,
    }
    new = {
        "status": "submitted",
        "order_id": "RH-CLAIM-4",
        "whole_share_order_ids": ["RH-CLAIM-3", "RH-CLAIM-4"],
        "submitted_amount_usd": 20.0,
        "whole_share_quantity": 2,
    }

    merged = _near_close_dca._merge_whole_share_submission_outcomes(existing, new)

    assert merged["submitted_amount_usd"] == 40.0
    assert merged["whole_share_quantity"] == 4
    assert merged["order_id"] == "RH-CLAIM-4"
    assert merged["whole_share_order_ids"] == [
        "RH-CLAIM-1",
        "RH-CLAIM-2",
        "RH-CLAIM-3",
        "RH-CLAIM-4",
    ]


def test_batch_pre_pass_reconciles_cross_day_claims_before_any_candidate_places(tmp_path, monkeypatch):
    """Codex review ("Reconcile recovered spend before starting the
    candidate batch"): cross-day reconciliation used to run only once the
    whole-share ticker itself reached _process_whole_share_candidate in
    the sorted batch loop -- a recovered order that pushes deployment
    across the kill-switch threshold was then invisible to any candidate
    sorted BEFORE the whole-share ticker. _reconcile_cross_day_whole_share_
    claims_for_batch runs this reconciliation as a pre-pass over every
    configured whole-share ticker, so every call into the candidate processor
    already receives the recovered deployment, regardless of sort order.
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

    whole_share_decision = _decision(action=Action.NO_ACTION)
    deployments_seen = []

    def _capture_candidate_deployment(decision, ctx, running_deployed_7d):
        deployments_seen.append((decision.ticker, running_deployed_7d))
        return ({"status": "skipped", "skip_reason": "test"}, None, running_deployed_7d, 0, 0)

    monkeypatch.setattr(_near_close_dca, "_process_buy_candidate", _capture_candidate_deployment)
    _process_buy_candidates_batch([other_decision, whole_share_decision], ctx)

    # The cross-day claim is settled and its $10 notional is already
    # reflected in the running figure BEFORE the first (alphabetically earlier)
    # candidate is handed to placement logic. This exercises the batch boundary,
    # rather than merely proving that the reconciliation helper returns $10.
    assert ledger.shares_purchased_total("BYDDY") == 1
    assert ledger.unresolved_claims_any_day("BYDDY", "afternoon-BYDDY") == []
    assert deployments_seen == [("AAPL", 10.0), ("BYDDY", 10.0)]


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
    _whole_share_reconciled_price() returns None and the ticker
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
        {
            "trading_day": "2026-08-15",
            "order_id": "RH-CROSS-DAY-ZERO-FILL-1",
            "amount_usd": 0.0,
        }
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


def test_per_candidate_recovery_is_merged_with_todays_fresh_submission(tmp_path, monkeypatch):
    """A mid-call recovery and a later fresh order must both reach artifacts.

    This covers the identity-guard variant of the mid-call recovery race: the
    whole-share success path publishes today's outcome before returning, so
    the outer batch observes that published object as ``c_outcome``.  The
    recovery therefore has to be merged at that inner publication point; it
    cannot be recovered by comparing the post-call published object with the
    returned object.
    """
    monkeypatch.setenv("DCA_CRON_TRACKING_ROOT", str(tmp_path / "cron_tracking"))
    ledger = WholeShareAccumulationLedger()
    claim_trading_day = date(2026, 8, 15)
    ledger.record_contribution(
        "BYDDY", claim_trading_day.isoformat(), 10.0, 10.0, stream="afternoon",
        idempotency_key="afternoon-BYDDY", recorded_at="2026-08-15T15:30:00-04:00",
    )

    config = StrategyConfig(whole_share_tickers=frozenset({"BYDDY"}), dry_run=False)
    old_claim_lookups = {"count": 0}

    class _RecoveryThenFreshSubmissionBroker(DryRunBroker):
        def same_day_strategy_order_exists(self, ticker, trading_day):
            return False

        def place_whole_share_buy(self, ticker, quantity, ref_id):
            return "RH-FRESH-TODAY"

        def same_day_strategy_order_exists_with_ref_id_prefix(self, ticker, trading_day, ref_id_prefixes):
            if ref_id_prefixes == ("graded-dca-wholeshare-2026-08-15-BYDDY",):
                old_claim_lookups["count"] += 1
                if old_claim_lookups["count"] == 1:
                    return None
                return {
                    "id": "RH-RECOVERED-OLD-CLAIM",
                    "state": "filled",
                    "quantity": "1",
                    "average_price": "10.00",
                }
            return {
                "id": "RH-FRESH-TODAY",
                "state": "filled",
                "quantity": "1",
                "average_price": "10.00",
            }

    broker = _RecoveryThenFreshSubmissionBroker(cash=500.0, reference_prices={"BYDDY": 10.0})
    _near_close_dca._IN_FLIGHT_CANDIDATE_OUTCOMES["outcomes"] = {}

    candidate_outcomes, _, running_deployed_7d, errors, _ = _process_buy_candidates_batch(
        [_decision(suggested_amount_usd=10.0, target_price=10.0)], _ctx(config, broker)
    )

    assert old_claim_lookups["count"] == 2
    assert errors == 0
    assert running_deployed_7d == 20.0
    outcome = candidate_outcomes["BYDDY"]
    assert outcome["status"] == "submitted"
    assert outcome["order_id"] == "RH-FRESH-TODAY"
    assert outcome["whole_share_order_ids"] == ["RH-RECOVERED-OLD-CLAIM", "RH-FRESH-TODAY"]
    assert outcome["submitted_amount_usd"] == 20.0
    assert outcome["whole_share_quantity"] == 2
    assert ledger.shares_purchased_total("BYDDY") == 2


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


def test_same_call_capture_recognizes_recovered_zero_fill_attempt():
    """The same-call recovery merge must recognize countable zero fills.

    Keep this source-level guard alongside the end-to-end batch regression:
    self-publishing candidate paths can replace the in-flight entry before
    the outer batch loop gets a chance to inspect it.
    """
    source = inspect.getsource(_near_close_dca._process_whole_share_candidate)

    capture_start = source.index("_candidate_from_reconciliation is not _pre_reconciliation_outcome")
    capture_end = source.index("_recovered_before_todays_processing = _candidate_from_reconciliation")
    capture_guard = source[capture_start:capture_end]

    assert 'get("status") == "submitted"' in capture_guard
    assert 'get("whole_share_zero_fill_attempt")' in capture_guard


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
        options=CompactRunOptions(
            artifacts_dir=str(tmp_path / "artifacts"),
            # Match run_pipeline's normal layout: the current artifact
            # directory is nested under the deployment-history scan root.
            deployment_history_root=tmp_path,
        ),
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

    # Companion artifacts must be refreshed to match, not left at the
    # zero-order stub _write_blocked_run_stub() wrote before the recovery
    # was persisted -- scripts/drift_detection.py and
    # scripts/purchase_history.py read submitted_orders.csv, not
    # run_state.json's own buy_candidates (Codex review: "Refresh companion
    # artifacts after gated recoveries").
    artifacts_dir = tmp_path / "artifacts"
    summary = _near_close_dca.json.loads((artifacts_dir / "run_summary.json").read_bytes())
    assert summary["orders_submitted_count"] == 1

    import csv

    with (artifacts_dir / "submitted_orders.csv").open(newline="", encoding="utf-8") as fh:
        order_rows = list(csv.DictReader(fh))
    assert len(order_rows) == 1
    assert order_rows[0]["ticker"] == "BYDDY"
    assert order_rows[0]["order_id"] == "RH-GATED-1"
    assert order_rows[0]["submitted_amount_usd"] == "10.0"


def test_gated_reconciliation_refuses_a_mismatched_broker_account(tmp_path, monkeypatch):
    """Codex review ("Pin the broker account before gated reconciliation"):
    _check_execution_gates() returns False (reaching this gated branch)
    BEFORE it ever calls _check_account_pinning() -- the date/holiday gate
    short-circuits first. The gated whole-share reconciliation loop
    previously checked only config.dry_run before querying the broker and
    mutating the ledger, so a stale or miswired adapter pointed at the
    WRONG account could release a reservation for a claim that the
    INTENDED account actually still holds as a live/filled order --
    letting that spent credit be reused once the misconfiguration is
    corrected. The claim must stay untouched when the account is
    mismatched.
    """
    monkeypatch.setenv("DCA_CRON_TRACKING_ROOT", str(tmp_path / "cron_tracking"))
    ledger = WholeShareAccumulationLedger()
    claim_trading_day = date(2026, 8, 15)  # the final approved trading date
    ledger.record_contribution(
        "BYDDY", claim_trading_day.isoformat(), 10.0, 10.0, stream="afternoon",
        idempotency_key="afternoon-BYDDY", recorded_at="2026-08-15T15:30:00-04:00",
    )

    now = datetime(2026, 8, 16, 15, 45, tzinfo=EASTERN)
    config = StrategyConfig(
        dry_run=False,
        tickers=(),
        whole_share_tickers=frozenset({"BYDDY"}),
        approved_test_dates=frozenset({claim_trading_day}),
        target_account_number="999000111",
    )
    state = StrategyState()

    class _MismatchedAccountBroker(DryRunBroker):
        def same_day_strategy_order_exists(self, ticker, trading_day):
            return False

        def same_day_strategy_order_exists_with_ref_id_prefix(self, ticker, trading_day, ref_id_prefixes):
            # If reached, this would incorrectly release BYDDY's reservation
            # -- the assertions below check it is NEVER reached.
            if ref_id_prefixes == ("graded-dca-wholeshare-2026-08-15-BYDDY",):
                return {"id": "RH-GATED-1", "state": "filled", "quantity": "1"}
            return None

    # A stale/miswired adapter pointed at a DIFFERENT account than configured.
    broker = _MismatchedAccountBroker(cash=500.0, account="123456789", reference_prices={"BYDDY": 10.0})
    data = _FakeSignalData(SourceName.FINANCE, close_price=9.90, prev_close=10.00)
    rh_data = _FakeSignalData(SourceName.ROBINHOOD, close_price=9.90, prev_close=10.00)

    with pytest.raises(RuntimeError, match="Refusing to trade account"):
        run_daily_compact(
            now,
            config,
            state,
            data,
            rh_data,
            broker,
            options=CompactRunOptions(artifacts_dir=str(tmp_path / "artifacts")),
        )

    # The claim must stay exactly as it was -- untouched by a broker lookup
    # against the wrong account.
    assert ledger.shares_purchased_total("BYDDY") == 0
    assert ledger.unresolved_claim("BYDDY", claim_trading_day.isoformat(), "afternoon-BYDDY") is not None
    assert state.day_notional(claim_trading_day) == pytest.approx(0.0)


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

    # Reconciliation evidence belongs to the older afternoon claim even when
    # this blocked retry happens before the ordinary 14:30 ET evidence floor.
    now = datetime(2026, 8, 16, 10, 0, tzinfo=EASTERN)
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

    # dca_runner.state_transaction() -- not exercised by run_daily_compact
    # itself -- is what actually fires these once its own save_strategy_state()
    # call durably commits state.record_buy()'s mutation above; simulate that
    # commit here the same way production does (Codex review: "Mark recovery
    # only after committing StrategyState").
    for callback in state.pending_durable_commit_callbacks:
        callback()
    state.pending_durable_commit_callbacks = []

    # The blocked invocation must also leave durable deployment evidence.
    # StrategyState is process-local and the resolved ledger claim cannot be
    # rediscovered, so only this artifact can seed a later process's 7-day
    # kill-switch total.
    recovered_state = _near_close_dca.json.loads(
        (tmp_path / "artifacts" / "run_state.json").read_bytes()
    )
    assert recovered_state["status"] == "blocked"
    assert recovered_state["recovered_deployment_evidence"] is True
    assert recovered_state["buy_candidates"] == [
        {
            "ticker": "BYDDY",
            "status": "filled",
            "order_id": "RH-ORPHANED-GATED-1",
            "buy_multiplier": None,
            "planned_amount_usd": 10.0,
            "whole_share_quantity": 1,
            "whole_share_claim_breakdown": [
                {"trading_day": claim_trading_day.isoformat(), "amount_usd": 10.0}
            ],
            "recovered_on_gated_path": True,
            "state_booked": True,
        }
    ]
    later_config = _near_close_dca._apply_deployment_history(
        config, date(2026, 8, 17), tmp_path
    )
    assert later_config.deployed_last_7d_usd == pytest.approx(10.0)

    # If that provisionally recorded order later settles at a different
    # notional, persist only its correction rather than counting the physical
    # broker order for a second time.
    adjustment_dir = tmp_path / "adjustment"
    adjustment_dir.mkdir()
    adjustment_state = dict(recovered_state)
    adjustment_state["buy_candidates"] = []
    (adjustment_dir / "run_state.json").write_text(
        _near_close_dca.json.dumps(adjustment_state), encoding="utf-8"
    )
    _near_close_dca._persist_gated_whole_share_recovery(
        adjustment_dir,
        "BYDDY",
        claim_trading_day.isoformat(),
        {"order_id": "RH-ORPHANED-GATED-1", "shares_purchased": 1, "order_notional_usd": 12.0},
        date(2026, 8, 17),
        tmp_path,
    )
    adjustment = _near_close_dca.json.loads(
        (adjustment_dir / "run_state.json").read_text(encoding="utf-8")
    )["buy_candidates"][0]
    assert adjustment["planned_amount_usd"] == pytest.approx(2.0)
    assert adjustment["whole_share_settlement_adjustment"] is True


def test_whole_share_gated_recovery_keeps_claim_when_evidence_write_fails(tmp_path, monkeypatch):
    """A failed pre-settlement artifact write must leave the claim retryable."""
    monkeypatch.setenv("DCA_CRON_TRACKING_ROOT", str(tmp_path / "cron_tracking"))
    ledger = WholeShareAccumulationLedger()
    claim_day = date(2026, 8, 15)
    ledger.record_contribution(
        "BYDDY", claim_day.isoformat(), 10.0, 10.0, stream="afternoon",
        idempotency_key="afternoon-BYDDY", recorded_at="2026-08-15T15:30:00-04:00",
    )
    claim = ledger.unresolved_claim("BYDDY", claim_day.isoformat(), "afternoon-BYDDY")

    class _FilledBroker(DryRunBroker):
        def same_day_strategy_order_exists_with_ref_id_prefix(self, ticker, trading_day, ref_id_prefixes):
            return {"id": "RH-WRITE-FAIL", "state": "filled", "quantity": "1"}

    with pytest.raises(OSError, match="disk full"):
        _near_close_dca._reconcile_unresolved_whole_share_claim(
            ledger,
            _FilledBroker(cash=500.0, reference_prices={"BYDDY": 10.0}),
            "BYDDY",
            "afternoon",
            "afternoon-BYDDY",
            claim_day.isoformat(),
            claim,
            before_settle=lambda _recovered: (_ for _ in ()).throw(OSError("disk full")),
        )

    assert ledger.unresolved_claim("BYDDY", claim_day.isoformat(), "afternoon-BYDDY") is not None
    assert ledger.shares_purchased_total("BYDDY") == 0


def test_settle_evidence_hook_fires_only_for_the_claim_winner(tmp_path, monkeypatch):
    """Codex review ("Fence evidence writes to the winning reconciler"): two
    concurrent reconcilers can both discover the SAME unresolved claim and
    both call ledger.settle(require_claim_ownership=True) for it, but only
    one of them actually wins ownership. A durable-evidence hook fired
    unconditionally before settle() (rather than gated on actually winning)
    would let the LOSER also persist evidence for an order it never
    resolved, double-counting it in deployment history even though the
    ledger's own totals are correctly only mutated once.
    """
    monkeypatch.setenv("DCA_CRON_TRACKING_ROOT", str(tmp_path / "cron_tracking"))
    ledger = WholeShareAccumulationLedger()
    claim_day = date(2026, 8, 15)
    ledger.record_contribution(
        "BYDDY", claim_day.isoformat(), 10.0, 10.0, stream="afternoon",
        idempotency_key="afternoon-BYDDY", recorded_at="2026-08-15T15:30:00-04:00",
    )
    claim_token = ledger.unresolved_claim("BYDDY", claim_day.isoformat(), "afternoon-BYDDY")["claim_token"]
    fired = []

    def _settle_once():
        return ledger.settle(
            "BYDDY", claim_day.isoformat(), 1, 10.0,
            stream="afternoon", recorded_at="2026-08-15T15:30:00-04:00",
            idempotency_key="afternoon-BYDDY", claim_token=claim_token, require_claim_ownership=True,
            on_claim_won=lambda: fired.append(True),
        )

    first = _settle_once()
    second = _settle_once()

    assert first is True
    assert second is False
    # The hook must fire exactly once -- for the winner only.
    assert fired == [True]
    assert ledger.shares_purchased_total("BYDDY") == 1


def test_settle_evidence_hook_failure_leaves_the_claim_unresolved(tmp_path, monkeypatch):
    """A raised on_claim_won hook must abort BEFORE settle()'s own disk
    write, leaving the claim exactly as unresolved/retryable as before this
    call -- mirrors the existing before_settle contract, now enforced by
    settle() itself rather than by the caller ordering its own call before
    settle().
    """
    monkeypatch.setenv("DCA_CRON_TRACKING_ROOT", str(tmp_path / "cron_tracking"))
    ledger = WholeShareAccumulationLedger()
    claim_day = date(2026, 8, 15)
    ledger.record_contribution(
        "BYDDY", claim_day.isoformat(), 10.0, 10.0, stream="afternoon",
        idempotency_key="afternoon-BYDDY", recorded_at="2026-08-15T15:30:00-04:00",
    )
    claim_token = ledger.unresolved_claim("BYDDY", claim_day.isoformat(), "afternoon-BYDDY")["claim_token"]

    with pytest.raises(OSError, match="disk full"):
        ledger.settle(
            "BYDDY", claim_day.isoformat(), 1, 10.0,
            stream="afternoon", recorded_at="2026-08-15T15:30:00-04:00",
            idempotency_key="afternoon-BYDDY", claim_token=claim_token, require_claim_ownership=True,
            on_claim_won=lambda: (_ for _ in ()).throw(OSError("disk full")),
        )

    assert ledger.unresolved_claim("BYDDY", claim_day.isoformat(), "afternoon-BYDDY") is not None
    assert ledger.shares_purchased_total("BYDDY") == 0


def test_gated_recovery_reverses_provisional_evidence_for_a_zero_fill_terminal(tmp_path, monkeypatch):
    """Codex review ("Reverse provisional evidence for zero-fill
    terminals"): an order persisted by an earlier gated run as
    live_order_found (full planned notional) that later reconciles as a
    confirmed zero fill must have its provisional notional reversed, not
    left to overcount deployment history forever -- preserve_pending() has
    already resolved the ledger claim by the time this is called, so no
    later run can ever revisit it.
    """
    monkeypatch.setenv("DCA_CRON_TRACKING_ROOT", str(tmp_path / "cron_tracking"))
    ledger = WholeShareAccumulationLedger()
    claim_trading_day = date(2026, 8, 15)
    ledger.record_contribution(
        "BYDDY", claim_trading_day.isoformat(), 10.0, 10.0, stream="afternoon",
        idempotency_key="afternoon-BYDDY", recorded_at="2026-08-15T15:30:00-04:00",
    )
    now = datetime(2026, 8, 16, 10, 0, tzinfo=EASTERN)
    config = StrategyConfig(
        dry_run=False,
        tickers=(),
        whole_share_tickers=frozenset({"BYDDY"}),
        approved_test_dates=frozenset({claim_trading_day}),
    )
    state = StrategyState()

    class _LiveGatedBroker(DryRunBroker):
        def same_day_strategy_order_exists_with_ref_id_prefix(self, ticker, trading_day, ref_id_prefixes):
            if ref_id_prefixes == ("graded-dca-wholeshare-2026-08-15-BYDDY",):
                return {"id": "RH-ZF-1", "state": "new", "quantity": "1"}
            return None

    broker = _LiveGatedBroker(cash=500.0, reference_prices={"BYDDY": 10.0})
    data = _FakeSignalData(SourceName.FINANCE, close_price=9.90, prev_close=10.00)
    rh_data = _FakeSignalData(SourceName.ROBINHOOD, close_price=9.90, prev_close=10.00)

    # First blocked run: the order is only LIVE, so a full-notional
    # provisional recovery row is persisted under this run's own artifact.
    first_dir = tmp_path / "first"
    run_daily_compact(
        now, config, state, data, rh_data, broker,
        options=CompactRunOptions(artifacts_dir=str(first_dir)),
    )
    first_state = _near_close_dca.json.loads((first_dir / "run_state.json").read_bytes())
    assert first_state["buy_candidates"][0]["planned_amount_usd"] == pytest.approx(10.0)

    second_dir = tmp_path / "second"
    second_dir.mkdir()
    _near_close_dca.atomic_write_text(
        second_dir / "run_state.json",
        _near_close_dca.json.dumps(_blocked_stub("2026-08-17", triggered_at_utc="2026-08-17T19:35:00Z")),
    )
    _near_close_dca._persist_gated_whole_share_recovery(
        second_dir,
        "BYDDY",
        "2026-08-15",
        {"zero_fill_attempt": True, "order_id": "RH-ZF-1"},
        date(2026, 8, 17),
        tmp_path,
    )
    second_state = _near_close_dca.json.loads((second_dir / "run_state.json").read_bytes())
    reversal = second_state["buy_candidates"][0]
    assert reversal["planned_amount_usd"] == pytest.approx(-10.0)
    assert reversal["whole_share_settlement_adjustment"] is True

    # A zero fill with NO prior provisional record deploys no capital, but
    # it still needs a durable zero-dollar identity row -- not a bare
    # no-op -- so the queued state_booked marker callback has something to
    # attach to (Codex review: "Persist a booking marker for first-seen
    # zero-fill attempts"); otherwise a ledger-write failure AFTER the
    # zero-dollar attempt is already durably booked into StrategyState
    # would leave nothing on disk for a retry to recognize as booked,
    # double-counting the attempt.
    third_dir = tmp_path / "third"
    third_dir.mkdir()
    _near_close_dca.atomic_write_text(
        third_dir / "run_state.json",
        _near_close_dca.json.dumps(_blocked_stub("2026-08-17", triggered_at_utc="2026-08-17T19:35:00Z")),
    )
    _near_close_dca._persist_gated_whole_share_recovery(
        third_dir,
        "GOOG",
        "2026-08-15",
        {"zero_fill_attempt": True, "order_id": "RH-ZF-NEVER-RECORDED"},
        date(2026, 8, 17),
        tmp_path,
    )
    third_state = _near_close_dca.json.loads((third_dir / "run_state.json").read_bytes())
    assert len(third_state["buy_candidates"]) == 1
    zero_fill_row = third_state["buy_candidates"][0]
    assert zero_fill_row["planned_amount_usd"] == pytest.approx(0.0)
    assert zero_fill_row["order_id"] == "RH-ZF-NEVER-RECORDED"
    assert zero_fill_row.get("whole_share_settlement_adjustment") is not True


def test_persist_gated_whole_share_recovery_corrects_id_less_terminal_result(tmp_path):
    """Codex review ("Reconcile ID-less terminal results against
    provisional rows"): an id-less live recovery that later reconciles as
    terminal (at a DIFFERENT amount, still with no order id) must still
    receive its settlement correction -- (ticker, claim_trading_day_iso)
    alone is a safe identity to net against, since the ledger can only
    ever hold ONE unresolved claim per (ticker, trading_day,
    idempotency_key).
    """
    first_dir = tmp_path / "first"
    first_dir.mkdir()
    _near_close_dca.atomic_write_text(
        first_dir / "run_state.json",
        _near_close_dca.json.dumps(_blocked_stub("2026-08-16", triggered_at_utc="2026-08-16T19:35:00Z")),
    )
    _near_close_dca._persist_gated_whole_share_recovery(
        first_dir, "BYDDY", "2026-08-15",
        {"live_order_found": True, "order_id": None, "planned_notional_usd": 10.0},
        date(2026, 8, 16), tmp_path,
    )
    second_dir = tmp_path / "second"
    second_dir.mkdir()
    _near_close_dca.atomic_write_text(
        second_dir / "run_state.json",
        _near_close_dca.json.dumps(_blocked_stub("2026-08-17", triggered_at_utc="2026-08-17T19:35:00Z")),
    )
    _near_close_dca._persist_gated_whole_share_recovery(
        second_dir, "BYDDY", "2026-08-15",
        {"order_id": None, "shares_purchased": 1, "order_notional_usd": 12.0},
        date(2026, 8, 17), tmp_path,
    )
    second_state = _near_close_dca.json.loads((second_dir / "run_state.json").read_bytes())
    correction = second_state["buy_candidates"][0]
    assert correction["planned_amount_usd"] == pytest.approx(2.0)
    assert correction["whole_share_settlement_adjustment"] is True


def test_persist_gated_whole_share_recovery_excludes_adjustment_rows_from_submitted_counts(tmp_path):
    """Codex review ("Exclude settlement adjustments from submitted-order
    counts"): when a provisional recovery and its later settlement
    adjustment land in the SAME artifacts_dir (both rows in one
    run_state.json, each carrying "status": "submitted"), the companion-
    artifact refresh must count only ONE physical broker attempt --
    matching the exact same "not another physical attempt" contract
    load_prior_same_day_allocations already applies via this identical
    whole_share_settlement_adjustment marker. A naive count of every
    status=="submitted" row would report two orders and write two CSV
    rows for one real order.
    """
    artifacts_dir = tmp_path / "recovery"
    artifacts_dir.mkdir()
    _near_close_dca.atomic_write_text(
        artifacts_dir / "run_state.json",
        _near_close_dca.json.dumps(_blocked_stub("2026-08-15", triggered_at_utc="2026-08-15T19:35:00Z")),
    )
    # First: a live order found provisionally recorded at $10.
    _near_close_dca._persist_gated_whole_share_recovery(
        artifacts_dir, "BYDDY", "2026-08-15",
        {"live_order_found": True, "order_id": "RH-ADJ-1", "planned_notional_usd": 10.0, "planned_quantity": 1},
        date(2026, 8, 15), None,
    )
    # Second: the SAME order settles at $12 -- a $2 adjustment row is
    # appended to the SAME run_state.json alongside the original $10 row.
    _near_close_dca._persist_gated_whole_share_recovery(
        artifacts_dir, "BYDDY", "2026-08-15",
        {"order_id": "RH-ADJ-1", "shares_purchased": 1, "order_notional_usd": 12.0},
        date(2026, 8, 15), None,
    )

    run_state = _near_close_dca.json.loads((artifacts_dir / "run_state.json").read_bytes())
    assert len(run_state["buy_candidates"]) == 2  # the original row plus its adjustment
    # The original row flips to "filled" once the terminal settlement that
    # confirms it lands (Codex review: "Preserve terminal fill evidence on
    # settlement corrections") -- the adjustment row alone stays "submitted".
    assert sum(1 for c in run_state["buy_candidates"] if c.get("status") in ("submitted", "filled")) == 2

    # But only ONE of those is a real physical attempt.
    assert run_state["orders_submitted_count"] == 1
    assert run_state["counts"]["submitted"] == 1

    summary = _near_close_dca.json.loads((artifacts_dir / "run_summary.json").read_bytes())
    assert summary["orders_submitted_count"] == 1

    import csv

    with (artifacts_dir / "submitted_orders.csv").open(newline="", encoding="utf-8") as fh:
        order_rows = list(csv.DictReader(fh))
    assert len(order_rows) == 1
    assert order_rows[0]["order_id"] == "RH-ADJ-1"


def test_persist_gated_whole_share_recovery_does_not_dedupe_absent_order_ids(tmp_path):
    """Codex review ("Avoid deduplicating absent order IDs"): a broker match
    that omits both "id" and "order_id" normalizes to order_id=None. Two
    DISTINCT claims recovered into the same blocked stub must not have the
    second one silently dropped just because it also lacks an order id.
    """
    artifacts_dir = tmp_path / "artifacts"
    artifacts_dir.mkdir()
    _near_close_dca.atomic_write_text(
        artifacts_dir / "run_state.json",
        _near_close_dca.json.dumps(_blocked_stub("2026-08-16", triggered_at_utc="2026-08-16T19:35:00Z")),
    )
    _near_close_dca._persist_gated_whole_share_recovery(
        artifacts_dir,
        "BYDDY",
        "2026-08-15",
        {"order_id": None, "shares_purchased": 1, "order_notional_usd": 10.0},
        date(2026, 8, 16),
        tmp_path,
    )
    _near_close_dca._persist_gated_whole_share_recovery(
        artifacts_dir,
        "BYDDY",
        "2026-08-14",  # a genuinely DIFFERENT claim day, same ticker
        {"order_id": None, "shares_purchased": 1, "order_notional_usd": 5.0},
        date(2026, 8, 16),
        tmp_path,
    )
    run_state = _near_close_dca.json.loads((artifacts_dir / "run_state.json").read_bytes())
    amounts = sorted(c["planned_amount_usd"] for c in run_state["buy_candidates"])
    assert amounts == [5.0, 10.0]


def test_gated_recovery_already_recorded_detects_id_less_live_claims_across_artifacts(tmp_path):
    """Codex review ("Deduplicate ID-less live recoveries before state
    booking"): _gated_whole_share_recovery_already_recorded's live_order_
    found branch only ever checked an order-id scan, which always returns
    False for an empty id -- _persist_and_book_gated_recovery's precomputed
    already_recorded decision must ALSO recognize a prior id-less recovery
    made into a DIFFERENT artifacts_dir, or every gated retry re-books
    StrategyState for the same still-unresolved order.
    """
    first_dir = tmp_path / "first"
    first_dir.mkdir()
    _near_close_dca.atomic_write_text(
        first_dir / "run_state.json",
        _near_close_dca.json.dumps(_blocked_stub("2026-08-16", triggered_at_utc="2026-08-16T19:35:00Z")),
    )
    _near_close_dca._persist_gated_whole_share_recovery(
        first_dir, "BYDDY", "2026-08-15",
        {"live_order_found": True, "order_id": None, "planned_notional_usd": 10.0},
        date(2026, 8, 16), tmp_path,
    )
    # Simulate the booking step that normally follows persist -- already_
    # recorded depends on state_booked, not merely on evidence existing
    # (Codex review: "Track state booking separately from artifact
    # deduplication").
    _near_close_dca._mark_gated_whole_share_recovery_state_booked(
        first_dir, "BYDDY", "2026-08-15", None, date(2026, 8, 16),
    )
    reconciled = {"live_order_found": True, "order_id": None, "planned_notional_usd": 10.0}
    already_recorded = _near_close_dca._gated_whole_share_recovery_already_recorded(
        "BYDDY", "2026-08-15", reconciled, date(2026, 8, 17), tmp_path,
    )
    assert already_recorded is True

    # A ticker with NO prior recording at all is correctly NOT flagged.
    fresh = _near_close_dca._gated_whole_share_recovery_already_recorded(
        "GOOG", "2026-08-15", reconciled, date(2026, 8, 17), tmp_path,
    )
    assert fresh is False


def test_gated_recovery_already_recorded_detects_id_less_terminal_claims(tmp_path):
    """Codex review ("Deduplicate ID-less terminal recoveries in strategy
    state"): the NON-live branch (a terminal/zero-fill result) must ALSO
    recognize a prior id-less recovery -- the old exact-artifact-day check
    only matched a row whose OWN trading_day equaled the claim's day, which
    a gated recovery (persisted under a LATER run's own trading_day) never
    satisfies.
    """
    first_dir = tmp_path / "first"
    first_dir.mkdir()
    _near_close_dca.atomic_write_text(
        first_dir / "run_state.json",
        _near_close_dca.json.dumps(_blocked_stub("2026-08-16", triggered_at_utc="2026-08-16T19:35:00Z")),
    )
    _near_close_dca._persist_gated_whole_share_recovery(
        first_dir, "BYDDY", "2026-08-15",
        {"live_order_found": True, "order_id": None, "planned_notional_usd": 10.0},
        date(2026, 8, 16), tmp_path,
    )
    _near_close_dca._mark_gated_whole_share_recovery_state_booked(
        first_dir, "BYDDY", "2026-08-15", None, date(2026, 8, 16),
    )
    terminal_reconciled = {"order_id": None, "shares_purchased": 1, "order_notional_usd": 10.0}
    already_recorded = _near_close_dca._gated_whole_share_recovery_already_recorded(
        "BYDDY", "2026-08-15", terminal_reconciled, date(2026, 8, 17), tmp_path,
    )
    assert already_recorded is True


def test_total_recorded_whole_share_claim_amount_nets_across_id_availability_change(tmp_path):
    """Codex review ("Net recoveries when broker order-ID availability
    changes"): a provisional row persisted WITHOUT an order id and a later
    lookup WITH one (or vice versa) must still be recognized as the SAME
    ledger claim via (ticker, claim_trading_day_iso) -- the ledger can hold
    at most one unresolved claim per (ticker, trading_day, idempotency_key).
    """
    # Provisional row has NO id; later lookup DOES have one.
    no_id_row = [
        {
            "ticker": "BYDDY",
            "status": "submitted",
            "order_id": None,
            "whole_share_claim_breakdown": [{"trading_day": "2026-08-15", "amount_usd": 10.0}],
        }
    ]
    total, found, _, _, _ = _near_close_dca._total_recorded_whole_share_claim_amount(
        "RH-LATER-ID", "BYDDY", "2026-08-15", date(2026, 8, 17), None, no_id_row,
    )
    assert found is True
    assert total == pytest.approx(10.0)

    # Provisional row HAS an id; later lookup has NONE.
    with_id_row = [
        {
            "ticker": "BYDDY",
            "status": "submitted",
            "order_id": "RH-EARLIER-ID",
            "whole_share_claim_breakdown": [{"trading_day": "2026-08-15", "amount_usd": 10.0}],
        }
    ]
    total2, found2, _, _, _ = _near_close_dca._total_recorded_whole_share_claim_amount(
        None, "BYDDY", "2026-08-15", date(2026, 8, 17), None, with_id_row,
    )
    assert found2 is True
    assert total2 == pytest.approx(10.0)

    # Two DIFFERENT stable ids never match, even for the same ticker/day.
    total3, found3, _, _, _ = _near_close_dca._total_recorded_whole_share_claim_amount(
        "RH-UNRELATED", "BYDDY", "2026-08-15", date(2026, 8, 17), None, with_id_row,
    )
    assert found3 is False
    assert total3 == pytest.approx(0.0)


def test_persist_gated_whole_share_recovery_takes_an_artifact_level_lock(tmp_path, monkeypatch):
    """Codex review ("Serialize recovery artifact updates across tickers"):
    the read-modify-write of ``artifacts_dir``'s ``run_state.json`` must be
    guarded by a lock scoped to the ARTIFACT itself -- distinct from each
    ticker's own per-ticker ``ledger.lock`` -- so two different tickers'
    recoveries into the SAME stub can never race each other's read/write.
    Verified directly against the real ``cross_process_lock`` (rather than
    a simulated thread race, which cannot be made both deterministic and
    non-flaky here): the lock path used must be a sidecar of
    ``run_state.json`` itself, and it must be taken on every call.
    """
    import scripts.file_lock as file_lock_module

    artifacts_dir = tmp_path / "artifacts"
    artifacts_dir.mkdir()
    _near_close_dca.atomic_write_text(
        artifacts_dir / "run_state.json",
        _near_close_dca.json.dumps(_blocked_stub("2026-08-16", triggered_at_utc="2026-08-16T19:35:00Z")),
    )

    real_cross_process_lock = file_lock_module.cross_process_lock
    calls = []

    @contextmanager
    def _tracking_lock(lock_path, **kwargs):
        calls.append(lock_path)
        with real_cross_process_lock(lock_path, **kwargs):
            yield

    monkeypatch.setattr(file_lock_module, "cross_process_lock", _tracking_lock)

    _near_close_dca._persist_gated_whole_share_recovery(
        artifacts_dir, "BYDDY", "2026-08-15",
        {"order_id": "RH-LOCK-1", "shares_purchased": 1, "order_notional_usd": 10.0},
        date(2026, 8, 16), tmp_path,
    )
    _near_close_dca._persist_gated_whole_share_recovery(
        artifacts_dir, "GOOG", "2026-08-15",
        {"order_id": "RH-LOCK-2", "shares_purchased": 1, "order_notional_usd": 5.0},
        date(2026, 8, 16), tmp_path,
    )

    assert len(calls) == 2
    # Both tickers' recoveries lock the SAME sidecar path -- the artifact
    # itself, not a per-ticker path -- so a real concurrent pair contends
    # on one lock and cannot interleave their reads and writes. The lock
    # lives OUTSIDE artifacts_dir (Codex review: "Lock blocked-stub creation
    # with recovery updates" -- a lock file left inside artifacts_dir itself
    # would violate the blocked-run four-file artifact-set contract).
    assert calls[0].resolve() == calls[1].resolve()
    assert artifacts_dir.resolve() not in calls[0].resolve().parents
    run_state = _near_close_dca.json.loads((artifacts_dir / "run_state.json").read_bytes())
    tickers = sorted(c["ticker"] for c in run_state["buy_candidates"])
    assert tickers == ["BYDDY", "GOOG"]


def test_persist_gated_whole_share_recovery_rejects_a_foreign_stream_stub(tmp_path):
    """Codex review ("Reject blocked stubs that cannot carry afternoon
    evidence"): a preserved blocked state from another stream (or one with
    no valid scheduled run_classification at all) must not silently accept
    a recovery row that dca_risk.is_afternoon_scheduled_evidence will later
    reject wholesale -- the caller's pre-commit hook must see this failure
    BEFORE the ledger call it guards resolves the claim irreversibly.
    """
    artifacts_dir = tmp_path / "artifacts"
    artifacts_dir.mkdir()
    _near_close_dca.atomic_write_text(
        artifacts_dir / "run_state.json",
        _near_close_dca.json.dumps({"status": "blocked", "buy_candidates": []}),
    )
    with pytest.raises(RuntimeError, match="cannot carry afternoon deployment evidence"):
        _near_close_dca._persist_gated_whole_share_recovery(
            artifacts_dir, "BYDDY", "2026-08-15",
            {"live_order_found": True, "order_id": "RH-FOREIGN-1", "planned_notional_usd": 10.0},
            date(2026, 8, 16), tmp_path,
        )
    # Nothing was written -- the artifact is untouched.
    run_state = _near_close_dca.json.loads((artifacts_dir / "run_state.json").read_bytes())
    assert run_state["buy_candidates"] == []


def test_gated_recovery_survives_a_crash_between_evidence_and_state_booking(tmp_path):
    """Codex review ("Track state booking separately from artifact
    deduplication"): a process killed after evidence is durably persisted
    but before StrategyState is booked must not leave a later retry
    mistaking "evidence exists" for "state was booked" -- the retry must
    still book state (and must be able to durably record having done so).
    """
    artifacts_dir = tmp_path / "artifacts"
    artifacts_dir.mkdir()
    _near_close_dca.atomic_write_text(
        artifacts_dir / "run_state.json",
        _near_close_dca.json.dumps(_blocked_stub("2026-08-16", triggered_at_utc="2026-08-16T19:35:00Z")),
    )
    reconciled = {"live_order_found": True, "order_id": "RH-CRASH-1", "planned_notional_usd": 10.0}

    # Step 1 (evidence persisted) happened; step 2 (state booked) did not --
    # simulating the process being killed in between.
    _near_close_dca._persist_gated_whole_share_recovery(
        artifacts_dir, "BYDDY", "2026-08-15", reconciled, date(2026, 8, 16), tmp_path,
    )

    # A retry's dedup decision must come back False -- state was NOT booked,
    # even though evidence was.
    already_recorded = _near_close_dca._gated_whole_share_recovery_already_recorded(
        "BYDDY", "2026-08-15", reconciled, date(2026, 8, 16), tmp_path,
    )
    assert already_recorded is False

    # The retry books state and marks it.
    state = StrategyState()
    _near_close_dca._book_reconciled_whole_share_order_durably(
        state, "BYDDY", "2026-08-15", reconciled, date(2026, 8, 16), tmp_path,
        already_recorded=already_recorded,
    )
    assert state.day_notional(date(2026, 8, 15)) == pytest.approx(10.0)
    _near_close_dca._mark_gated_whole_share_recovery_state_booked(
        artifacts_dir, "BYDDY", "2026-08-15", "RH-CRASH-1", date(2026, 8, 16),
    )

    # A THIRD attempt now correctly sees it as fully recorded and does not
    # re-book.
    final_check = _near_close_dca._gated_whole_share_recovery_already_recorded(
        "BYDDY", "2026-08-15", reconciled, date(2026, 8, 16), tmp_path,
    )
    assert final_check is True


def test_write_blocked_run_stub_locks_against_recovery_updates(tmp_path, monkeypatch):
    """Codex review ("Lock blocked-stub creation with recovery updates"):
    _write_blocked_run_stub's check-then-write of run_state.json must
    contend on the SAME sidecar lock _persist_gated_whole_share_recovery
    uses, so two overlapping gated invocations sharing an artifacts_dir
    can never have one's stub-creation clobber the other's already-
    persisted recovery evidence.
    """
    import scripts.file_lock as file_lock_module

    real_cross_process_lock = file_lock_module.cross_process_lock
    calls = []

    @contextmanager
    def _tracking_lock(lock_path, **kwargs):
        calls.append(lock_path)
        with real_cross_process_lock(lock_path, **kwargs):
            yield

    monkeypatch.setattr(file_lock_module, "cross_process_lock", _tracking_lock)

    artifacts_dir = tmp_path / "artifacts"
    _near_close_dca._write_blocked_run_stub(date(2026, 8, 16), artifacts_dir)

    assert len(calls) == 1
    expected_lock_path = _near_close_dca._gated_recovery_lock_path(artifacts_dir / "run_state.json")
    assert calls[0].resolve() == expected_lock_path.resolve()
    # The lock itself must NOT live inside artifacts_dir -- the blocked-run
    # four-file artifact set is a documented, asserted-exact contract, and a
    # fifth file left in that same directory would silently violate it for
    # every consumer that lists the directory.
    assert artifacts_dir.resolve() not in calls[0].resolve().parents
    run_state = _near_close_dca.json.loads((artifacts_dir / "run_state.json").read_bytes())
    assert run_state["status"] == "blocked"
    assert {p.name for p in artifacts_dir.iterdir()} == {
        "run_state.json", "run_summary.json", "signals.csv", "submitted_orders.csv",
    }


def test_write_blocked_run_stub_keeps_companion_writes_inside_the_recovery_lock(tmp_path, monkeypatch):
    """Codex review ("Keep companion writes inside the recovery lock"): the
    companion-file writes (run_summary.json/signals.csv/submitted_orders.csv)
    must happen WHILE the recovery lock is still held, not after it releases.
    Releasing the lock right after the run_state.json write left a window
    where this stub writer's own "not exists" companion checks/writes could
    run concurrently with -- and be overtaken by -- a full
    _persist_gated_whole_share_recovery() pass sharing the SAME lock,
    letting the stub clobber a freshly-refreshed run_summary.json/
    submitted_orders.csv with its own zero-order versions.
    """
    import scripts.file_lock as file_lock_module
    import scripts.scheduled_artifacts as scheduled_artifacts_module

    real_cross_process_lock = file_lock_module.cross_process_lock
    lock_held = {"value": False}

    @contextmanager
    def _tracking_lock(lock_path, **kwargs):
        with real_cross_process_lock(lock_path, **kwargs):
            lock_held["value"] = True
            try:
                yield
            finally:
                lock_held["value"] = False

    real_write_submitted_orders_csv = scheduled_artifacts_module.write_submitted_orders_csv
    observed_lock_held_during_companion_write = []

    def _tracking_write_submitted_orders_csv(*args, **kwargs):
        observed_lock_held_during_companion_write.append(lock_held["value"])
        return real_write_submitted_orders_csv(*args, **kwargs)

    monkeypatch.setattr(file_lock_module, "cross_process_lock", _tracking_lock)
    monkeypatch.setattr(
        scheduled_artifacts_module, "write_submitted_orders_csv", _tracking_write_submitted_orders_csv
    )

    artifacts_dir = tmp_path / "artifacts"
    _near_close_dca._write_blocked_run_stub(date(2026, 8, 16), artifacts_dir)

    assert observed_lock_held_during_companion_write == [True]


def test_persist_gated_whole_share_recovery_dedupes_id_less_live_recoveries_across_artifacts(
    tmp_path,
):
    """Codex review ("Deduplicate ID-less live recoveries across
    artifacts"): a live_order_found claim stays unresolved and is
    reconciled again on EVERY later run. When the broker's match omits an
    order id, a LATER gated invocation using a DIFFERENT artifacts_dir must
    still recognize the SAME ticker/claim-day recovery already recorded by
    an EARLIER one, not just the current stub's own candidates.
    """
    first_dir = tmp_path / "first"
    first_dir.mkdir()
    _near_close_dca.atomic_write_text(
        first_dir / "run_state.json",
        _near_close_dca.json.dumps(_blocked_stub("2026-08-16", triggered_at_utc="2026-08-16T19:35:00Z")),
    )
    _near_close_dca._persist_gated_whole_share_recovery(
        first_dir, "BYDDY", "2026-08-15",
        {"live_order_found": True, "order_id": None, "planned_notional_usd": 10.0},
        date(2026, 8, 16), tmp_path,
    )
    first_state = _near_close_dca.json.loads((first_dir / "run_state.json").read_bytes())
    assert len(first_state["buy_candidates"]) == 1
    # Simulate the booking step completing for the first recovery -- with
    # no state_booked=True anywhere, a later persist call would (correctly)
    # append its own zero-amount marker row rather than silently no-op
    # (Codex review: "Track state booking separately from artifact
    # deduplication").
    _near_close_dca._mark_gated_whole_share_recovery_state_booked(
        first_dir, "BYDDY", "2026-08-15", None, date(2026, 8, 16),
    )

    # A LATER gated run, still live, still no id, writing into a DIFFERENT
    # artifacts_dir -- must recognize the prior recording and no-op.
    second_dir = tmp_path / "second"
    second_dir.mkdir()
    _near_close_dca.atomic_write_text(
        second_dir / "run_state.json",
        _near_close_dca.json.dumps(_blocked_stub("2026-08-17", triggered_at_utc="2026-08-17T19:35:00Z")),
    )
    _near_close_dca._persist_gated_whole_share_recovery(
        second_dir, "BYDDY", "2026-08-15",
        {"live_order_found": True, "order_id": None, "planned_notional_usd": 10.0},
        date(2026, 8, 17), tmp_path,
    )
    second_state = _near_close_dca.json.loads((second_dir / "run_state.json").read_bytes())
    assert second_state["buy_candidates"] == []

    # A genuinely DIFFERENT claim day is still recorded normally.
    third_dir = tmp_path / "third"
    third_dir.mkdir()
    _near_close_dca.atomic_write_text(
        third_dir / "run_state.json",
        _near_close_dca.json.dumps(_blocked_stub("2026-08-18", triggered_at_utc="2026-08-18T19:35:00Z")),
    )
    _near_close_dca._persist_gated_whole_share_recovery(
        third_dir, "BYDDY", "2026-08-14",
        {"live_order_found": True, "order_id": None, "planned_notional_usd": 7.0},
        date(2026, 8, 18), tmp_path,
    )
    third_state = _near_close_dca.json.loads((third_dir / "run_state.json").read_bytes())
    assert len(third_state["buy_candidates"]) == 1
    assert third_state["buy_candidates"][0]["planned_amount_usd"] == pytest.approx(7.0)


def test_total_recorded_whole_share_claim_amount_matches_aggregated_order_ids(tmp_path):
    """Codex review ("Match aggregate order IDs while netting settlements"):
    a normal run can merge several reconciled whole-share orders for one
    ticker into a single candidate row via
    _merge_whole_share_submission_outcomes, leaving an OLDER order id
    reachable only through whole_share_order_ids while order_id itself
    holds the NEWEST -- the settlement-adjustment lookup must still find
    it, mirroring _find_same_day_recorded_order's own identical matching.
    """
    extra_candidates = [
        {
            "ticker": "BYDDY",
            "status": "submitted",
            "order_id": "RH-NEWEST",
            "whole_share_order_ids": ["RH-OLDEST", "RH-NEWEST"],
            "whole_share_claim_breakdown": [
                {"trading_day": "2026-08-15", "amount_usd": 10.0}
            ],
        }
    ]
    total, found, _, _, _ = _near_close_dca._total_recorded_whole_share_claim_amount(
        "RH-OLDEST", "BYDDY", "2026-08-15", date(2026, 8, 16), None, extra_candidates,
    )
    assert found is True
    assert total == pytest.approx(10.0)


def test_total_recorded_whole_share_claim_amount_nets_only_the_matching_orders_entry(tmp_path):
    """Codex review ("Net only the matching order's breakdown amount"): a
    row matched by order_id can be an AGGREGATE covering more than one
    physical order for the same ticker/day -- here order A ($10) and order
    B ($20), both merged into one row's whole_share_claim_breakdown, each
    carrying its OWN order_id. Reconciling A must net against A's own $10
    entry alone, not the row's combined $30 -- summing every same-day entry
    regardless of its own order_id would erase B's real, unrelated $20
    deployment from the computed adjustment.
    """
    extra_candidates = [
        {
            "ticker": "BYDDY",
            "status": "submitted",
            "order_id": "RH-ORDER-B",
            "whole_share_order_ids": ["RH-ORDER-A", "RH-ORDER-B"],
            "whole_share_claim_breakdown": [
                {"trading_day": "2026-08-15", "order_id": "RH-ORDER-A", "amount_usd": 10.0},
                {"trading_day": "2026-08-15", "order_id": "RH-ORDER-B", "amount_usd": 20.0},
            ],
        }
    ]
    total, found, _, _, _ = _near_close_dca._total_recorded_whole_share_claim_amount(
        "RH-ORDER-A", "BYDDY", "2026-08-15", date(2026, 8, 16), None, extra_candidates,
    )
    assert found is True
    assert total == pytest.approx(10.0)


def test_total_recorded_whole_share_claim_amount_rejects_ambiguous_id_less_aggregate_match(tmp_path):
    """Codex review ("Reject ambiguous ID-less aggregate matches"): the
    SAME aggregate as the "nets only the matching order's entry" test
    above (order A=$10, order B=$20, each with its own per-entry
    order_id) -- but this time the terminal broker response for A omits
    its OWN order id (order_id=None passed in). _candidate_row_matches_
    whole_share_claim then matches this row via the id-less
    trading-day-only fallback, which used to assume "no id anywhere means
    exactly one claim" and summed BOTH entries ($30), computing a -$18
    correction that erases B's real $20 deployment. With no order_id to
    pick the right entry, this must be treated as ambiguous instead.
    """
    extra_candidates = [
        {
            "ticker": "BYDDY",
            "status": "submitted",
            "order_id": "RH-ORDER-B",
            "whole_share_order_ids": ["RH-ORDER-A", "RH-ORDER-B"],
            "whole_share_claim_breakdown": [
                {"trading_day": "2026-08-15", "order_id": "RH-ORDER-A", "amount_usd": 10.0},
                {"trading_day": "2026-08-15", "order_id": "RH-ORDER-B", "amount_usd": 20.0},
            ],
        }
    ]
    total, found, _, _, ambiguous = _near_close_dca._total_recorded_whole_share_claim_amount(
        None, "BYDDY", "2026-08-15", date(2026, 8, 16), None, extra_candidates,
    )
    assert ambiguous is True
    assert found is False
    assert total == pytest.approx(0.0)


def test_total_recorded_whole_share_claim_amount_skips_ambiguous_id_less_legacy_entries(tmp_path):
    """Codex review ("Skip ambiguous ID-less entries when netting a known
    order"): a legacy aggregate can have MULTIPLE same-day breakdown
    entries that carry no order_id of their own, while the row's top-level
    whole_share_order_ids still lets it match a known reconciled order. Here
    order A ($10) and order B ($20) are both id-less entries for the same
    day, and the row matches order A via whole_share_order_ids. Neither
    entry can be told apart from the other, so reconciling A must find
    NOTHING recorded (not A's $10 alone, and not the combined $30) --
    counting either id-less entry would be a guess, and guessing $10 would
    still be wrong the moment the two entries' amounts differ from what this
    call assumes.
    """
    extra_candidates = [
        {
            "ticker": "BYDDY",
            "status": "submitted",
            "order_id": "RH-ORDER-B",
            "whole_share_order_ids": ["RH-ORDER-A", "RH-ORDER-B"],
            "whole_share_claim_breakdown": [
                {"trading_day": "2026-08-15", "amount_usd": 10.0},
                {"trading_day": "2026-08-15", "amount_usd": 20.0},
            ],
        }
    ]
    total, found, _, _, _ = _near_close_dca._total_recorded_whole_share_claim_amount(
        "RH-ORDER-A", "BYDDY", "2026-08-15", date(2026, 8, 16), None, extra_candidates,
    )
    assert found is False
    assert total == pytest.approx(0.0)


def test_persist_gated_whole_share_recovery_marks_a_confirmed_fill_as_filled(tmp_path):
    """Codex review ("Mark confirmed gated fills as filled"): a CONFIRMED
    terminal fill (a genuine settled purchase -- reconciled carries
    shares_purchased/order_notional_usd, not live_order_found or
    zero_fill_attempt) must be recorded with status="filled", not
    "submitted". The four-column submitted_orders.csv this row also feeds
    is read by scripts/purchase_history.py's _orders_csv_fills(), which
    only counts a row toward confirmed cost history when its status/state
    actually says so -- a "submitted" status here has neither that nor any
    executed-quantity evidence, so the recovered purchase would silently
    never appear in cost history.

    A genuinely still-live order (live_order_found) must still be recorded
    as "submitted" -- its final quantity is not yet known, so it is not
    actually confirmed fill evidence.
    """
    artifacts_dir = tmp_path / "artifacts"
    artifacts_dir.mkdir()
    _near_close_dca.atomic_write_text(
        artifacts_dir / "run_state.json",
        _near_close_dca.json.dumps(_blocked_stub("2026-08-16", triggered_at_utc="2026-08-16T19:35:00Z")),
    )
    _near_close_dca._persist_gated_whole_share_recovery(
        artifacts_dir,
        "BYDDY",
        "2026-08-15",
        {"order_id": "RH-CONFIRMED-FILL-1", "shares_purchased": 1, "order_notional_usd": 10.0},
        date(2026, 8, 16),
        tmp_path,
    )
    run_state = _near_close_dca.json.loads((artifacts_dir / "run_state.json").read_bytes())
    rows = run_state["buy_candidates"]
    assert len(rows) == 1
    assert rows[0]["status"] == "filled"

    submitted_orders = (artifacts_dir / "submitted_orders.csv").read_text(encoding="utf-8")
    assert "filled" in submitted_orders

    # A still-live order (final quantity unknown) stays "submitted".
    live_artifacts_dir = tmp_path / "live-artifacts"
    live_artifacts_dir.mkdir()
    _near_close_dca.atomic_write_text(
        live_artifacts_dir / "run_state.json",
        _near_close_dca.json.dumps(_blocked_stub("2026-08-16", triggered_at_utc="2026-08-16T19:35:00Z")),
    )
    _near_close_dca._persist_gated_whole_share_recovery(
        live_artifacts_dir,
        "BYDDY",
        "2026-08-15",
        {"live_order_found": True, "order_id": "RH-STILL-LIVE-1", "planned_notional_usd": 10.0},
        date(2026, 8, 16),
        tmp_path,
    )
    live_run_state = _near_close_dca.json.loads((live_artifacts_dir / "run_state.json").read_bytes())
    assert live_run_state["buy_candidates"][0]["status"] == "submitted"


def test_persist_gated_whole_share_recovery_corrects_terminal_result_in_same_stub(tmp_path):
    """Codex review ("Reconcile terminal results already present in the
    current stub"): a retry that reuses the SAME per-day artifacts_dir
    after a provisionally-recorded live order goes terminal must still
    apply the terminal correction, not exit early just because the order
    id is already present in that stub. Uses a genuinely valid
    (discoverable) blocked stub -- Codex review ("Exclude the current stub
    from the history scan") -- so the SAME already-persisted row is not
    ALSO picked up by the deployment-history disk scan on top of the
    in-memory ``extra_candidates`` pass, which would otherwise double-count
    it and turn the expected +$2 correction into a spurious -$8 one.
    """
    artifacts_dir = tmp_path / "artifacts"
    artifacts_dir.mkdir()
    _near_close_dca.atomic_write_text(
        artifacts_dir / "run_state.json",
        _near_close_dca.json.dumps(_blocked_stub("2026-08-16", triggered_at_utc="2026-08-16T19:35:00Z")),
    )
    _near_close_dca._persist_gated_whole_share_recovery(
        artifacts_dir,
        "BYDDY",
        "2026-08-15",
        {"live_order_found": True, "order_id": "RH-SAME-STUB-1", "planned_notional_usd": 10.0},
        date(2026, 8, 16),
        tmp_path,
    )
    # The SAME retry (same artifacts_dir) later discovers the order is now
    # terminally filled at a DIFFERENT amount.
    _near_close_dca._persist_gated_whole_share_recovery(
        artifacts_dir,
        "BYDDY",
        "2026-08-15",
        {"order_id": "RH-SAME-STUB-1", "shares_purchased": 1, "order_notional_usd": 12.0},
        date(2026, 8, 16),
        tmp_path,
    )
    run_state = _near_close_dca.json.loads((artifacts_dir / "run_state.json").read_bytes())
    rows = run_state["buy_candidates"]
    assert len(rows) == 2
    assert rows[0]["planned_amount_usd"] == pytest.approx(10.0)
    assert rows[1]["planned_amount_usd"] == pytest.approx(2.0)
    assert rows[1]["whole_share_settlement_adjustment"] is True
    assert sum(r["planned_amount_usd"] for r in rows) == pytest.approx(12.0)
    # Codex review ("Preserve terminal fill evidence on settlement
    # corrections"): the ORIGINAL physical row (a provisional
    # live_order_found) must flip to "filled" once the terminal fill that
    # confirms it is persisted, not stay stuck at "submitted" forever with
    # only an excluded adjustment row to show for the real settlement.
    assert rows[0]["status"] == "filled"

    # Codex review ("Apply settlement corrections to the canonical order
    # row"): the adjustment row is excluded from submitted_orders.csv (one
    # physical attempt, not two rows), but the CSV's single row for this
    # order must carry the NET settled $12, not the stale $10 the base row
    # was originally provisioned at.
    csv_text = (artifacts_dir / "submitted_orders.csv").read_text(encoding="utf-8")
    csv_rows = list(csv.DictReader(csv_text.splitlines()))
    assert len(csv_rows) == 1
    assert csv_rows[0]["order_id"] == "RH-SAME-STUB-1"
    assert float(csv_rows[0]["submitted_amount_usd"]) == pytest.approx(12.0)
    assert csv_rows[0]["status"] == "filled"


def test_persist_gated_whole_share_recovery_csv_nets_across_an_order_id_availability_change(tmp_path):
    """Codex review ("Apply corrections when order ID availability
    changes"): the provisional and terminal broker responses can differ in
    whether they carry an order id at all -- a shape
    _candidate_row_matches_whole_share_claim already treats as normal. Here
    the base row carries an order id ("RH-ID-BASE-1") but the terminal
    settlement's own reconciled result is id-less; the adjustment row it
    produces is therefore keyed by (ticker, trading_day), not order_id. The
    canonical CSV's single row for this order must still net the
    correction into its amount, not keep reporting the stale original --
    checking only the order-id map for an id-bearing base row (an earlier
    version of this fix did) never finds an id-less adjustment.
    """
    artifacts_dir = tmp_path / "artifacts"
    artifacts_dir.mkdir()
    _near_close_dca.atomic_write_text(
        artifacts_dir / "run_state.json",
        _near_close_dca.json.dumps(_blocked_stub("2026-08-16", triggered_at_utc="2026-08-16T19:35:00Z")),
    )
    _near_close_dca._persist_gated_whole_share_recovery(
        artifacts_dir,
        "BYDDY",
        "2026-08-15",
        {"order_id": "RH-ID-BASE-1", "shares_purchased": 1, "order_notional_usd": 10.0},
        date(2026, 8, 16),
        tmp_path,
    )
    # The terminal settlement's own reconciled result is id-less this time.
    _near_close_dca._persist_gated_whole_share_recovery(
        artifacts_dir,
        "BYDDY",
        "2026-08-15",
        {"shares_purchased": 1, "order_notional_usd": 12.0},
        date(2026, 8, 16),
        tmp_path,
    )
    run_state = _near_close_dca.json.loads((artifacts_dir / "run_state.json").read_bytes())
    rows = run_state["buy_candidates"]
    assert len(rows) == 2
    assert rows[1]["whole_share_settlement_adjustment"] is True
    assert rows[1]["order_id"] is None

    csv_text = (artifacts_dir / "submitted_orders.csv").read_text(encoding="utf-8")
    csv_rows = list(csv.DictReader(csv_text.splitlines()))
    assert len(csv_rows) == 1
    assert csv_rows[0]["order_id"] == "RH-ID-BASE-1"
    assert float(csv_rows[0]["submitted_amount_usd"]) == pytest.approx(12.0)


def test_persist_gated_whole_share_recovery_csv_does_not_apply_corrections_to_sibling_orders(tmp_path):
    """Codex review ("Avoid applying corrections to sibling orders"): the
    SAME gated stub holds TWO distinct physical orders for one ticker/day
    -- A=$10 (order_id RH-ORDER-A) and B=$20 (order_id RH-ORDER-B). A's
    own +$2 settlement adjustment is correctly keyed to A's order_id in
    the direct order-id map. But the ticker/trading-day fallback map is
    ALSO populated for every adjustment regardless of whether its direct
    order-id match already resolved it -- B's own order_id genuinely
    differs from A's, so B also misses the direct id lookup and would
    (without the ambiguity guard) ALSO match the shared ticker/day key,
    wrongly absorbing A's +$2 correction too and reporting B as $22.
    """
    artifacts_dir = tmp_path / "artifacts"
    artifacts_dir.mkdir()
    _near_close_dca.atomic_write_text(
        artifacts_dir / "run_state.json",
        _near_close_dca.json.dumps(_blocked_stub("2026-08-16", triggered_at_utc="2026-08-16T19:35:00Z")),
    )
    _near_close_dca._persist_gated_whole_share_recovery(
        artifacts_dir,
        "BYDDY",
        "2026-08-15",
        {"order_id": "RH-ORDER-A", "shares_purchased": 1, "order_notional_usd": 10.0},
        date(2026, 8, 16),
        tmp_path,
    )
    _near_close_dca._persist_gated_whole_share_recovery(
        artifacts_dir,
        "BYDDY",
        "2026-08-15",
        {"order_id": "RH-ORDER-B", "shares_purchased": 1, "order_notional_usd": 20.0},
        date(2026, 8, 16),
        tmp_path,
    )
    # A's own terminal settlement corrects it upward by $2.
    _near_close_dca._persist_gated_whole_share_recovery(
        artifacts_dir,
        "BYDDY",
        "2026-08-15",
        {"order_id": "RH-ORDER-A", "shares_purchased": 1, "order_notional_usd": 12.0},
        date(2026, 8, 16),
        tmp_path,
    )

    csv_text = (artifacts_dir / "submitted_orders.csv").read_text(encoding="utf-8")
    csv_rows = {row["order_id"]: float(row["submitted_amount_usd"]) for row in csv.DictReader(csv_text.splitlines())}
    assert csv_rows["RH-ORDER-A"] == pytest.approx(12.0)
    assert csv_rows["RH-ORDER-B"] == pytest.approx(20.0)


def test_persist_gated_whole_share_recovery_csv_nets_when_the_adjustment_gains_an_id(tmp_path):
    """Codex review ("Index ID-bearing adjustments by the fallback
    identity"): the "vice versa" direction of the order-id-availability
    change -- here the PROVISIONAL base row is id-less, but the terminal
    settlement's own reconciled result picks up a fresh order id. The
    resulting adjustment row is then keyed by order_id in
    ``adjustment_totals_by_order_id`` alone; the id-less base row can only
    ever query ``adjustment_totals_by_ticker_day``, which stayed empty for
    an id-bearing adjustment, so the CSV kept reporting the stale original
    $10 instead of the real net $12 (an earlier version of this fix indexed
    an id-bearing adjustment ONLY by its order_id and returned early).
    """
    artifacts_dir = tmp_path / "artifacts"
    artifacts_dir.mkdir()
    _near_close_dca.atomic_write_text(
        artifacts_dir / "run_state.json",
        _near_close_dca.json.dumps(_blocked_stub("2026-08-16", triggered_at_utc="2026-08-16T19:35:00Z")),
    )
    # The provisional live recovery is id-less.
    _near_close_dca._persist_gated_whole_share_recovery(
        artifacts_dir,
        "BYDDY",
        "2026-08-15",
        {"live_order_found": True, "order_id": None, "planned_notional_usd": 10.0},
        date(2026, 8, 16),
        tmp_path,
    )
    # The terminal settlement's own reconciled result gains an order id.
    _near_close_dca._persist_gated_whole_share_recovery(
        artifacts_dir,
        "BYDDY",
        "2026-08-15",
        {"order_id": "RH-GAINED-ID-1", "shares_purchased": 1, "order_notional_usd": 12.0},
        date(2026, 8, 16),
        tmp_path,
    )
    run_state = _near_close_dca.json.loads((artifacts_dir / "run_state.json").read_bytes())
    rows = run_state["buy_candidates"]
    assert len(rows) == 2
    assert rows[1]["whole_share_settlement_adjustment"] is True
    assert rows[1]["order_id"] == "RH-GAINED-ID-1"

    csv_text = (artifacts_dir / "submitted_orders.csv").read_text(encoding="utf-8")
    csv_rows = list(csv.DictReader(csv_text.splitlines()))
    assert len(csv_rows) == 1
    assert float(csv_rows[0]["submitted_amount_usd"]) == pytest.approx(12.0)


def test_persist_gated_whole_share_recovery_refreshes_the_buy_candidates_count(tmp_path):
    """Codex review ("Refresh the recovered candidate count"): appending a
    recovered row to a blocked stub's ``buy_candidates`` list must also
    refresh ``counts["buy_candidates"]`` -- dca_weekly_dashboard's
    build_weekly_overview()/build_run_summary() both total candidates from
    that mirrored counts field, not from ``len(buy_candidates)`` directly,
    so leaving it at the blocked stub's original (zero) value would make
    the recovered order disappear from weekly/summary candidate metrics
    even though the same artifacts report its submitted order.
    """
    artifacts_dir = tmp_path / "recovery"
    artifacts_dir.mkdir()
    _near_close_dca.atomic_write_text(
        artifacts_dir / "run_state.json",
        _near_close_dca.json.dumps(_blocked_stub("2026-08-15", triggered_at_utc="2026-08-15T19:35:00Z")),
    )
    _near_close_dca._persist_gated_whole_share_recovery(
        artifacts_dir, "BYDDY", "2026-08-15",
        {"order_id": "RH-COUNT-1", "shares_purchased": 1, "order_notional_usd": 10.0},
        date(2026, 8, 15), None,
    )
    run_state = _near_close_dca.json.loads((artifacts_dir / "run_state.json").read_bytes())
    assert len(run_state["buy_candidates"]) == 1
    assert run_state["counts"]["buy_candidates"] == 1


def test_persist_gated_whole_share_recovery_excludes_adjustments_from_buy_candidates_count(tmp_path):
    """Codex review ("Exclude adjustments from recovered candidate
    counts"): a whole_share_settlement_adjustment row is a bookkeeping-only
    dollar correction for the SAME physical candidate already counted by
    the original row -- it must not be counted as a second buy candidate
    in counts["buy_candidates"], mirroring submitted_rows' own identical
    exclusion.
    """
    artifacts_dir = tmp_path / "recovery"
    artifacts_dir.mkdir()
    _near_close_dca.atomic_write_text(
        artifacts_dir / "run_state.json",
        _near_close_dca.json.dumps(_blocked_stub("2026-08-15", triggered_at_utc="2026-08-15T19:35:00Z")),
    )
    _near_close_dca._persist_gated_whole_share_recovery(
        artifacts_dir, "BYDDY", "2026-08-15",
        {"live_order_found": True, "order_id": "RH-ADJ-COUNT-1", "planned_notional_usd": 10.0},
        date(2026, 8, 15), None,
    )
    _near_close_dca._persist_gated_whole_share_recovery(
        artifacts_dir, "BYDDY", "2026-08-15",
        {"order_id": "RH-ADJ-COUNT-1", "shares_purchased": 1, "order_notional_usd": 12.0},
        date(2026, 8, 15), None,
    )
    run_state = _near_close_dca.json.loads((artifacts_dir / "run_state.json").read_bytes())
    assert len(run_state["buy_candidates"]) == 2  # original row + adjustment row
    assert run_state["counts"]["buy_candidates"] == 1  # one physical candidate


def test_persist_gated_whole_share_recovery_refuses_ambiguous_legacy_matches(tmp_path):
    """Codex review ("Preserve ambiguous matches instead of treating them
    as new"): a legacy aggregate row matches this order via
    ``whole_share_order_ids``, but has TWO id-less same-day breakdown
    entries (A=$10, B=$20) that cannot be told apart --
    ``_total_recorded_whole_share_claim_amount`` correctly reports nothing
    safe to net (``found`` stays False), but that must NOT be mistaken for
    "no matching row exists": appending order A's $12 terminal fill as a
    brand-new row on top of the un-netted $30 would double-count real
    deployment. This must fail closed instead of silently republishing.
    """
    artifacts_dir = tmp_path / "artifacts"
    artifacts_dir.mkdir()
    stub = _blocked_stub("2026-08-16", triggered_at_utc="2026-08-16T19:35:00Z")
    stub["buy_candidates"] = [
        {
            "ticker": "BYDDY",
            "status": "submitted",
            "order_id": "RH-ORDER-B",
            "whole_share_order_ids": ["RH-ORDER-A", "RH-ORDER-B"],
            "whole_share_claim_breakdown": [
                {"trading_day": "2026-08-15", "amount_usd": 10.0},
                {"trading_day": "2026-08-15", "amount_usd": 20.0},
            ],
        }
    ]
    _near_close_dca.atomic_write_text(artifacts_dir / "run_state.json", _near_close_dca.json.dumps(stub))

    with pytest.raises(RuntimeError, match="ambiguous"):
        _near_close_dca._persist_gated_whole_share_recovery(
            artifacts_dir,
            "BYDDY",
            "2026-08-15",
            {"order_id": "RH-ORDER-A", "shares_purchased": 1, "order_notional_usd": 12.0},
            date(2026, 8, 16),
            None,
        )
    # The pre-existing (un-netted) rows must be left completely untouched.
    run_state = _near_close_dca.json.loads((artifacts_dir / "run_state.json").read_bytes())
    assert len(run_state["buy_candidates"]) == 1


def test_persist_gated_whole_share_recovery_nets_adjustments_across_retries(tmp_path):
    """Codex review ("Make settlement adjustments idempotent across
    retries"): if an EARLIER retry already persisted a partial adjustment
    into its OWN artifacts_dir (e.g. because the ledger's own disk write
    then failed, leaving the claim unresolved for a later retry), a LATER
    retry recomputing the SAME terminal correction must net against the
    full recorded total across every artifact -- not just the first
    matching row -- or it republishes the same delta again and overstates
    deployment.
    """
    order_id = "RH-NET-RETRY-1"
    first_dir = tmp_path / "first"
    first_dir.mkdir()
    _near_close_dca.atomic_write_text(
        first_dir / "run_state.json",
        _near_close_dca.json.dumps(_blocked_stub("2026-08-16", triggered_at_utc="2026-08-16T19:35:00Z")),
    )
    _near_close_dca._persist_gated_whole_share_recovery(
        first_dir, "BYDDY", "2026-08-15",
        {"live_order_found": True, "order_id": order_id, "planned_notional_usd": 10.0},
        date(2026, 8, 16), tmp_path,
    )
    # Simulate the base row's own booking step completing too -- fully_
    # committed is an AND across every matching row (not just the latest),
    # so a later zero-delta retry only true-no-ops once EVERY row for this
    # claim, base included, is marked (Codex review: "Keep unbooked
    # settlement adjustments visible").
    _near_close_dca._mark_gated_whole_share_recovery_state_booked(
        first_dir, "BYDDY", "2026-08-15", order_id, date(2026, 8, 16),
    )
    # First retry: terminal fill at $12 -- persists a +$2 adjustment.
    second_dir = tmp_path / "second"
    second_dir.mkdir()
    _near_close_dca.atomic_write_text(
        second_dir / "run_state.json",
        _near_close_dca.json.dumps(_blocked_stub("2026-08-17", triggered_at_utc="2026-08-17T19:35:00Z")),
    )
    _near_close_dca._persist_gated_whole_share_recovery(
        second_dir, "BYDDY", "2026-08-15",
        {"order_id": order_id, "shares_purchased": 1, "order_notional_usd": 12.0},
        date(2026, 8, 17), tmp_path,
    )
    second_state = _near_close_dca.json.loads((second_dir / "run_state.json").read_bytes())
    assert second_state["buy_candidates"][0]["planned_amount_usd"] == pytest.approx(2.0)
    # Simulate this retry's own booking step completing -- otherwise a
    # THIRD retry recomputing the identical $12 correction would (correctly)
    # append its own zero-amount state_booked marker rather than silently
    # no-op (Codex review: "Track state booking separately from artifact
    # deduplication").
    _near_close_dca._mark_gated_whole_share_recovery_state_booked(
        second_dir, "BYDDY", "2026-08-15", order_id, date(2026, 8, 17),
    )

    # A LATER retry recomputes the SAME $12 terminal correction (e.g. the
    # ledger's own settle() write failed after this same hook ran once
    # already). Netting against BOTH prior artifacts (10 + 2 == 12) must
    # produce a zero delta -- nothing further published.
    third_dir = tmp_path / "third"
    third_dir.mkdir()
    _near_close_dca.atomic_write_text(
        third_dir / "run_state.json",
        _near_close_dca.json.dumps(_blocked_stub("2026-08-18", triggered_at_utc="2026-08-18T19:35:00Z")),
    )
    _near_close_dca._persist_gated_whole_share_recovery(
        third_dir, "BYDDY", "2026-08-15",
        {"order_id": order_id, "shares_purchased": 1, "order_notional_usd": 12.0},
        date(2026, 8, 18), tmp_path,
    )
    third_state = _near_close_dca.json.loads((third_dir / "run_state.json").read_bytes())
    assert third_state["buy_candidates"] == []


def test_persist_gated_whole_share_recovery_zeros_the_unchanged_marker_amount(tmp_path):
    """Codex review ("Zero the unchanged recovery marker adjustment"): when
    a retry recomputes the SAME already-fully-recorded amount (delta == 0)
    but no matching row was ever marked state_booked, the marker row this
    function appends so state_booked has somewhere to live must itself
    carry a ZERO amount -- not the full original notional -- or it
    silently adds the whole order's notional to deployment history a
    second time, permanently (the claim resolves right after, so nothing
    can ever reverse it).
    """
    order_id = "RH-ZERO-MARKER-1"
    first_dir = tmp_path / "first"
    first_dir.mkdir()
    _near_close_dca.atomic_write_text(
        first_dir / "run_state.json",
        _near_close_dca.json.dumps(_blocked_stub("2026-08-16", triggered_at_utc="2026-08-16T19:35:00Z")),
    )
    _near_close_dca._persist_gated_whole_share_recovery(
        first_dir, "BYDDY", "2026-08-15",
        {"order_id": order_id, "shares_purchased": 1, "order_notional_usd": 10.0},
        date(2026, 8, 16), tmp_path,
    )
    # NOT marked state_booked -- simulates a crash between persisting
    # evidence and durably booking StrategyState.

    # A retry observes the SAME $10 terminal fill again -- delta is 0, but
    # state was never marked booked, so a marker row must still be
    # appended for state_booked to attach to. That marker must carry
    # amount 0.0, not the full $10.
    second_dir = tmp_path / "second"
    second_dir.mkdir()
    _near_close_dca.atomic_write_text(
        second_dir / "run_state.json",
        _near_close_dca.json.dumps(_blocked_stub("2026-08-17", triggered_at_utc="2026-08-17T19:35:00Z")),
    )
    _near_close_dca._persist_gated_whole_share_recovery(
        second_dir, "BYDDY", "2026-08-15",
        {"order_id": order_id, "shares_purchased": 1, "order_notional_usd": 10.0},
        date(2026, 8, 17), tmp_path,
    )
    second_state = _near_close_dca.json.loads((second_dir / "run_state.json").read_bytes())
    marker_row = second_state["buy_candidates"][0]
    assert marker_row["planned_amount_usd"] == pytest.approx(0.0)
    assert marker_row["whole_share_claim_breakdown"][0]["amount_usd"] == pytest.approx(0.0)
    assert marker_row["whole_share_settlement_adjustment"] is True


def test_persist_gated_whole_share_recovery_keeps_a_booked_base_from_masking_an_unbooked_adjustment(
    tmp_path,
):
    """Codex review ("Keep unbooked settlement adjustments visible"): a
    claim's BASE row (e.g. the original live_order_found booking) can be
    state_booked=True while a LATER settlement-adjustment row for the
    SAME claim is still state_booked=False -- a crash between persisting
    that adjustment and durably booking its own StrategyState correction.
    The old OR-across-rows signal treated the base row's True as proof
    the WHOLE claim (adjustment included) was settled, so a retry
    recomputing the identical zero delta silently no-op'd and the
    adjustment's own gap was never surfaced again. A retry must instead
    keep publishing a marker until EVERY matching row, not just the
    oldest one, is booked.
    """
    order_id = "RH-MASK-UNBOOKED-ADJ-1"
    first_dir = tmp_path / "first"
    first_dir.mkdir()
    _near_close_dca.atomic_write_text(
        first_dir / "run_state.json",
        _near_close_dca.json.dumps(_blocked_stub("2026-08-16", triggered_at_utc="2026-08-16T19:35:00Z")),
    )
    _near_close_dca._persist_gated_whole_share_recovery(
        first_dir, "BYDDY", "2026-08-15",
        {"live_order_found": True, "order_id": order_id, "planned_notional_usd": 10.0},
        date(2026, 8, 16), tmp_path,
    )
    # The BASE row IS booked...
    _near_close_dca._mark_gated_whole_share_recovery_state_booked(
        first_dir, "BYDDY", "2026-08-15", order_id, date(2026, 8, 16),
    )

    # ...but the settlement correction to $15 is persisted and then a
    # crash prevents its own StrategyState booking from ever happening.
    second_dir = tmp_path / "second"
    second_dir.mkdir()
    _near_close_dca.atomic_write_text(
        second_dir / "run_state.json",
        _near_close_dca.json.dumps(_blocked_stub("2026-08-17", triggered_at_utc="2026-08-17T19:35:00Z")),
    )
    _near_close_dca._persist_gated_whole_share_recovery(
        second_dir, "BYDDY", "2026-08-15",
        {"order_id": order_id, "shares_purchased": 1, "order_notional_usd": 15.0},
        date(2026, 8, 17), tmp_path,
    )
    # NOT marked -- simulates the crash. The claim-wide OR would (wrongly)
    # already report state_booked=True here because of the base row alone.
    _, _, state_booked_or, fully_committed, _ = _near_close_dca._total_recorded_whole_share_claim_amount(
        order_id, "BYDDY", "2026-08-15", date(2026, 8, 18), tmp_path, [],
    )
    assert state_booked_or is True
    assert fully_committed is False

    # A retry recomputing the SAME $15 settlement (delta == 0 against the
    # net total) must NOT true-no-op -- it must keep publishing a marker,
    # since the adjustment's own booking is still outstanding.
    third_dir = tmp_path / "third"
    third_dir.mkdir()
    _near_close_dca.atomic_write_text(
        third_dir / "run_state.json",
        _near_close_dca.json.dumps(_blocked_stub("2026-08-18", triggered_at_utc="2026-08-18T19:35:00Z")),
    )
    _near_close_dca._persist_gated_whole_share_recovery(
        third_dir, "BYDDY", "2026-08-15",
        {"order_id": order_id, "shares_purchased": 1, "order_notional_usd": 15.0},
        date(2026, 8, 18), tmp_path,
    )
    third_state = _near_close_dca.json.loads((third_dir / "run_state.json").read_bytes())
    assert len(third_state["buy_candidates"]) == 1
    assert third_state["buy_candidates"][0]["planned_amount_usd"] == pytest.approx(0.0)

    # The NET total across all three artifacts is still exactly $15 (base
    # $10 + adjustment $5) -- the retry published a zero-amount marker,
    # not another dollar-moving correction.
    total, _, _, _, _ = _near_close_dca._total_recorded_whole_share_claim_amount(
        order_id, "BYDDY", "2026-08-15", date(2026, 8, 19), tmp_path, [],
    )
    assert total == pytest.approx(15.0)


def test_stale_no_match_release_reverses_provisional_evidence(tmp_path, monkeypatch):
    """Codex review ("Reverse live evidence on stale no-match releases"):
    a PRIOR gated run can have persisted this claim provisionally as
    live_order_found (the full planned notional). If a LATER reconciliation
    gets a clean no-match from the broker and the claim is stale,
    ledger.preserve_pending() restores the FULL reserved credit -- as if
    nothing was ever spent -- and releases the claim for good. Without a
    before_release correction here too, the stale provisional notional
    stays counted in deployment history forever, since no later run can
    ever revisit a resolved claim to reverse it.
    """
    monkeypatch.setenv("DCA_CRON_TRACKING_ROOT", str(tmp_path / "cron_tracking"))
    from dca_whole_share_accumulator import WholeShareAccumulationLedger

    ledger = WholeShareAccumulationLedger()
    claim_trading_day = date(2026, 8, 15)
    ledger.record_contribution(
        "BYDDY", claim_trading_day.isoformat(), 10.0, 10.0, stream="afternoon",
        idempotency_key="afternoon-BYDDY", recorded_at="2026-08-15T15:30:00-04:00",
    )
    # A PRIOR gated run already persisted the full $10 provisionally.
    artifacts_dir = tmp_path / "artifacts"
    artifacts_dir.mkdir()
    _near_close_dca.atomic_write_text(
        artifacts_dir / "run_state.json",
        _near_close_dca.json.dumps(_blocked_stub("2026-08-16", triggered_at_utc="2026-08-16T19:35:00Z")),
    )
    _near_close_dca._persist_gated_whole_share_recovery(
        artifacts_dir, "BYDDY", "2026-08-15",
        {"live_order_found": True, "order_id": "RH-STALE-1", "planned_notional_usd": 10.0},
        date(2026, 8, 16), tmp_path,
    )

    class _NoMatchBroker(DryRunBroker):
        def same_day_strategy_order_exists_with_ref_id_prefix(self, ticker, trading_day, ref_id_prefixes):
            return None

    broker = _NoMatchBroker(cash=500.0, reference_prices={"BYDDY": 10.0})
    claim = ledger.unresolved_claim("BYDDY", claim_trading_day.isoformat(), "afternoon-BYDDY")
    assert claim is not None
    # This test's own narrative is a broker call that WAS actually made (a
    # prior gated run found and persisted a live order) -- mirror the
    # "placed" marker execute_claimed() would have durably set for that
    # real attempt, since (Codex review: "Avoid inventing attempts for
    # stale no-match claims") a stale-release reversal is only booked as
    # an attempt when real placement evidence exists.
    claim["placed"] = True

    released = []

    def _before_release(result):
        released.append(result)
        _near_close_dca._persist_gated_whole_share_recovery(
            artifacts_dir, "BYDDY", "2026-08-15", result, date(2026, 8, 20), tmp_path,
        )

    # Far enough past claimed_at to be stale.
    stale_now_iso = "2026-08-20T19:35:00-04:00"
    result = _near_close_dca._reconcile_unresolved_whole_share_claim(
        ledger, broker, "BYDDY", "afternoon", "afternoon-BYDDY",
        "2026-08-15", claim, now_iso=stale_now_iso, before_release=_before_release,
    )
    assert result is None  # nothing to book -- this is a release, not a settlement
    assert released, "expected the stale no-match release to invoke before_release"
    assert ledger.unresolved_claim("BYDDY", claim_trading_day.isoformat(), "afternoon-BYDDY") is None

    reversed_state = _near_close_dca.json.loads((artifacts_dir / "run_state.json").read_bytes())
    total_recorded = sum(
        entry.get("amount_usd", 0.0)
        for row in reversed_state["buy_candidates"]
        for entry in row.get("whole_share_claim_breakdown") or ()
        if entry.get("trading_day") == "2026-08-15"
    )
    assert total_recorded == pytest.approx(0.0)


def test_stale_no_match_release_does_not_invent_an_attempt_before_placement(tmp_path, monkeypatch):
    """Codex review ("Avoid inventing attempts for stale no-match claims"):
    when the process dies AFTER record_contribution() reserves a fire but
    BEFORE execute_claimed() ever attempts the broker call -- neither
    "placing" nor "placed" was ever set -- a later stale no-match lookup
    correctly finds no broker order, because none was ever requested.
    Booking this as a zero_fill_attempt (as the unconditional version of
    this fix did) fabricates a broker attempt that never happened: on the
    gated path, before_release persists a "submitted" recovery row and
    calls record_buy(..., 0.0), inflating max_orders_per_day accounting
    and durable state for a request that was never sent. The claim must
    still be released (the reservation must not stay stranded forever),
    just without fabricating booking evidence for it.
    """
    monkeypatch.setenv("DCA_CRON_TRACKING_ROOT", str(tmp_path / "cron_tracking"))
    from dca_whole_share_accumulator import WholeShareAccumulationLedger

    ledger = WholeShareAccumulationLedger()
    claim_trading_day = date(2026, 8, 15)
    ledger.record_contribution(
        "BYDDY", claim_trading_day.isoformat(), 10.0, 10.0, stream="afternoon",
        idempotency_key="afternoon-BYDDY", recorded_at="2026-08-15T15:30:00-04:00",
    )

    class _NoMatchBroker(DryRunBroker):
        def same_day_strategy_order_exists_with_ref_id_prefix(self, ticker, trading_day, ref_id_prefixes):
            return None

    broker = _NoMatchBroker(cash=500.0, reference_prices={"BYDDY": 10.0})
    claim = ledger.unresolved_claim("BYDDY", claim_trading_day.isoformat(), "afternoon-BYDDY")
    assert claim is not None
    # Never placed -- record_contribution() sets neither "placing" nor
    # "placed"; execute_claimed() never got a chance to run.
    assert not claim.get("placing")
    assert not claim.get("placed")

    released = []

    def _before_release(result):
        released.append(result)

    stale_now_iso = "2026-08-20T19:35:00-04:00"
    result = _near_close_dca._reconcile_unresolved_whole_share_claim(
        ledger, broker, "BYDDY", "afternoon", "afternoon-BYDDY",
        "2026-08-15", claim, now_iso=stale_now_iso, before_release=_before_release,
    )
    assert result is None
    # before_release must NOT fire -- there is no real attempt to book.
    assert not released
    # The claim is still released -- the reservation is not stranded.
    assert ledger.unresolved_claim("BYDDY", claim_trading_day.isoformat(), "afternoon-BYDDY") is None
    assert ledger.pending_usd("BYDDY") == 10.0


def test_persist_gated_whole_share_recovery_rows_are_schema_valid(tmp_path):
    """A fresh recovery row and a settlement-adjustment row (including a
    NEGATIVE one reversing a zero-fill terminal) must both validate against
    schemas/run_state.schema.json -- planned_amount_usd's negative
    exception is scoped there to whole_share_settlement_adjustment=True
    rows (Codex review: "Keep persisted recovery candidates within the
    run-state schema").
    """
    jsonschema = pytest.importorskip("jsonschema")
    run_state_schema = _near_close_dca.json.loads(
        (_near_close_dca.Path(__file__).resolve().parent.parent / "schemas" / "run_state.schema.json").read_bytes()
    )
    candidate_schema = run_state_schema["properties"]["buy_candidates"]["items"]

    first_dir = tmp_path / "first"
    first_dir.mkdir()
    _near_close_dca.atomic_write_text(
        first_dir / "run_state.json",
        _near_close_dca.json.dumps(_blocked_stub("2026-08-16", triggered_at_utc="2026-08-16T19:35:00Z")),
    )
    _near_close_dca._persist_gated_whole_share_recovery(
        first_dir, "BYDDY", "2026-08-15",
        {"live_order_found": True, "order_id": "RH-SCHEMA-1", "planned_notional_usd": 10.0, "planned_quantity": 1},
        date(2026, 8, 16), tmp_path,
    )
    fresh_row = _near_close_dca.json.loads((first_dir / "run_state.json").read_bytes())["buy_candidates"][0]
    jsonschema.validate(fresh_row, candidate_schema)

    second_dir = tmp_path / "second"
    second_dir.mkdir()
    _near_close_dca.atomic_write_text(
        second_dir / "run_state.json",
        _near_close_dca.json.dumps(_blocked_stub("2026-08-17", triggered_at_utc="2026-08-17T19:35:00Z")),
    )
    _near_close_dca._persist_gated_whole_share_recovery(
        second_dir, "BYDDY", "2026-08-15",
        {"zero_fill_attempt": True, "order_id": "RH-SCHEMA-1"},
        date(2026, 8, 17), tmp_path,
    )
    reversal_row = _near_close_dca.json.loads((second_dir / "run_state.json").read_bytes())["buy_candidates"][0]
    assert reversal_row["planned_amount_usd"] < 0
    jsonschema.validate(reversal_row, candidate_schema)

    # The negative exception is scoped to whole_share_settlement_adjustment
    # =True rows -- an ordinary submitted candidate reporting a negative
    # amount (a malformed row from anywhere else in the codebase) must
    # still fail schema validation, or dca_risk.deployed_usd() could be
    # silently understated by it (Codex review: "Keep negative notionals
    # scoped to settlement adjustments").
    malformed_row = dict(reversal_row)
    malformed_row.pop("whole_share_settlement_adjustment")
    with pytest.raises(jsonschema.ValidationError):
        jsonschema.validate(malformed_row, candidate_schema)


def test_whole_share_zero_fill_release_hook_fires_before_ledger_commit(tmp_path, monkeypatch):
    """Codex review ("Keep zero-fill claims retryable until reversal
    persists"): before_release must fire -- and be able to persist durable
    evidence -- BEFORE preserve_pending_if_claimed() commits the release to
    disk, exactly like before_settle does for a positive terminal fill.
    """
    monkeypatch.setenv("DCA_CRON_TRACKING_ROOT", str(tmp_path / "cron_tracking"))
    ledger = WholeShareAccumulationLedger()
    claim_day = date(2026, 8, 15)
    ledger.record_contribution(
        "BYDDY", claim_day.isoformat(), 10.0, 10.0, stream="afternoon",
        idempotency_key="afternoon-BYDDY", recorded_at="2026-08-15T15:30:00-04:00",
    )
    claim = ledger.unresolved_claim("BYDDY", claim_day.isoformat(), "afternoon-BYDDY")

    class _ZeroFillBroker(DryRunBroker):
        def same_day_strategy_order_exists_with_ref_id_prefix(self, ticker, trading_day, ref_id_prefixes):
            return None

        def same_day_order_with_ref_id_prefix_any_state(self, ticker, trading_day, ref_id_prefixes):
            return {"id": "RH-ZF-HOOK-1", "state": "rejected", "cumulative_quantity": "0"}

    fired = []
    result = _near_close_dca._reconcile_unresolved_whole_share_claim(
        ledger,
        _ZeroFillBroker(cash=500.0, reference_prices={"BYDDY": 10.0}),
        "BYDDY",
        "afternoon",
        "afternoon-BYDDY",
        claim_day.isoformat(),
        claim,
        before_release=lambda recovered: fired.append(dict(recovered)),
    )

    assert result == {"zero_fill_attempt": True, "order_id": "RH-ZF-HOOK-1"}
    # The hook received the SAME result the function returns, and fired
    # exactly once before the claim was released.
    assert fired == [result]
    assert ledger.unresolved_claim("BYDDY", claim_day.isoformat(), "afternoon-BYDDY") is None


def test_whole_share_zero_fill_release_hook_failure_leaves_claim_unresolved(tmp_path, monkeypatch):
    """A raised before_release hook must abort BEFORE
    preserve_pending_if_claimed()'s own disk write, leaving the claim
    exactly as unresolved/retryable as before this call.
    """
    monkeypatch.setenv("DCA_CRON_TRACKING_ROOT", str(tmp_path / "cron_tracking"))
    ledger = WholeShareAccumulationLedger()
    claim_day = date(2026, 8, 15)
    ledger.record_contribution(
        "BYDDY", claim_day.isoformat(), 10.0, 10.0, stream="afternoon",
        idempotency_key="afternoon-BYDDY", recorded_at="2026-08-15T15:30:00-04:00",
    )
    claim = ledger.unresolved_claim("BYDDY", claim_day.isoformat(), "afternoon-BYDDY")

    class _ZeroFillBroker(DryRunBroker):
        def same_day_strategy_order_exists_with_ref_id_prefix(self, ticker, trading_day, ref_id_prefixes):
            return None

        def same_day_order_with_ref_id_prefix_any_state(self, ticker, trading_day, ref_id_prefixes):
            return {"id": "RH-ZF-HOOK-FAIL-1", "state": "rejected", "cumulative_quantity": "0"}

    result = _near_close_dca._reconcile_unresolved_whole_share_claim(
        ledger,
        _ZeroFillBroker(cash=500.0, reference_prices={"BYDDY": 10.0}),
        "BYDDY",
        "afternoon",
        "afternoon-BYDDY",
        claim_day.isoformat(),
        claim,
        before_release=lambda _recovered: (_ for _ in ()).throw(OSError("disk full")),
    )

    assert result is None
    assert ledger.unresolved_claim("BYDDY", claim_day.isoformat(), "afternoon-BYDDY") is not None


def test_gated_recovery_persists_live_evidence_before_booking_durable_state(tmp_path, monkeypatch):
    """Codex review ("Avoid rebooking live recoveries after artifact-write
    failures"): for a live_order_found result, durable evidence
    (_persist_gated_whole_share_recovery) must be written BEFORE
    StrategyState is mutated (_book_reconciled_whole_share_order_durably) --
    state.record_buy() survives a later raise via state_transaction()'s
    save-in-finally, but the "live_order_found" claim is reconciled again on
    EVERY later run since it never resolves in the ledger. If state were
    booked first and the evidence write then failed, a later retry would
    find nothing on disk to prevent it from re-booking the SAME still-live
    order into StrategyState a second time.
    """
    monkeypatch.setenv("DCA_CRON_TRACKING_ROOT", str(tmp_path / "cron_tracking"))
    ledger = WholeShareAccumulationLedger()
    claim_trading_day = date(2026, 8, 15)
    ledger.record_contribution(
        "BYDDY", claim_trading_day.isoformat(), 10.0, 10.0, stream="afternoon",
        idempotency_key="afternoon-BYDDY", recorded_at="2026-08-15T15:30:00-04:00",
    )
    now = datetime(2026, 8, 16, 10, 0, tzinfo=EASTERN)
    config = StrategyConfig(
        dry_run=False,
        tickers=(),
        whole_share_tickers=frozenset({"BYDDY"}),
        approved_test_dates=frozenset({claim_trading_day}),
    )
    state = StrategyState()

    class _LiveGatedBroker(DryRunBroker):
        def same_day_strategy_order_exists_with_ref_id_prefix(self, ticker, trading_day, ref_id_prefixes):
            if ref_id_prefixes == ("graded-dca-wholeshare-2026-08-15-BYDDY",):
                return {"id": "RH-LIVE-GATED-1", "state": "new", "quantity": "1"}
            return None

    broker = _LiveGatedBroker(cash=500.0, reference_prices={"BYDDY": 10.0})
    data = _FakeSignalData(SourceName.FINANCE, close_price=9.90, prev_close=10.00)
    rh_data = _FakeSignalData(SourceName.ROBINHOOD, close_price=9.90, prev_close=10.00)

    call_order = []
    real_persist = _near_close_dca._persist_gated_whole_share_recovery
    real_book = _near_close_dca._book_reconciled_whole_share_order_durably

    def _tracked_persist(*args, **kwargs):
        call_order.append("persist")
        return real_persist(*args, **kwargs)

    def _tracked_book(*args, **kwargs):
        call_order.append("book")
        return real_book(*args, **kwargs)

    monkeypatch.setattr(_near_close_dca, "_persist_gated_whole_share_recovery", _tracked_persist)
    monkeypatch.setattr(_near_close_dca, "_book_reconciled_whole_share_order_durably", _tracked_book)

    result = run_daily_compact(
        now, config, state, data, rh_data, broker,
        options=CompactRunOptions(artifacts_dir=str(tmp_path / "artifacts")),
    )

    assert result["blocked"] is True
    assert call_order == ["persist", "book"]
    assert state.own_orders_recorded == {("BYDDY", claim_trading_day)}

    # A failed evidence write must leave the durable-state mutation
    # unreached entirely.
    monkeypatch.setattr(
        _near_close_dca,
        "_persist_gated_whole_share_recovery",
        lambda *a, **k: (_ for _ in ()).throw(RuntimeError("disk full")),
    )
    state2 = StrategyState()
    with pytest.raises(RuntimeError, match="disk full"):
        run_daily_compact(
            now, config, state2, data, rh_data, broker,
            options=CompactRunOptions(artifacts_dir=str(tmp_path / "artifacts2")),
        )
    assert state2.own_orders_recorded == set()


def test_gated_live_recovery_locks_the_tickers_own_ledger_lock(tmp_path, monkeypatch):
    """Codex review ("Serialize live recovery evidence across run
    directories"): a live_order_found result -- unlike settled/zero-fill
    shapes -- has no ledger-level ownership fence, since the claim stays
    unresolved and is reconciled again on every later run. Two concurrent
    gated invocations writing to DIFFERENT artifacts_dir files would not
    otherwise contend on anything; the caller must explicitly take the
    SAME per-ticker lock the ledger itself uses for this ticker.
    """
    monkeypatch.setenv("DCA_CRON_TRACKING_ROOT", str(tmp_path / "cron_tracking"))
    ledger = WholeShareAccumulationLedger()
    claim_trading_day = date(2026, 8, 15)
    ledger.record_contribution(
        "BYDDY", claim_trading_day.isoformat(), 10.0, 10.0, stream="afternoon",
        idempotency_key="afternoon-BYDDY", recorded_at="2026-08-15T15:30:00-04:00",
    )
    now = datetime(2026, 8, 16, 10, 0, tzinfo=EASTERN)
    config = StrategyConfig(
        dry_run=False,
        tickers=(),
        whole_share_tickers=frozenset({"BYDDY"}),
        approved_test_dates=frozenset({claim_trading_day}),
    )
    state = StrategyState()

    class _LiveGatedBroker(DryRunBroker):
        def same_day_strategy_order_exists_with_ref_id_prefix(self, ticker, trading_day, ref_id_prefixes):
            if ref_id_prefixes == ("graded-dca-wholeshare-2026-08-15-BYDDY",):
                return {"id": "RH-LIVE-LOCK-1", "state": "new", "quantity": "1"}
            return None

    broker = _LiveGatedBroker(cash=500.0, reference_prices={"BYDDY": 10.0})
    data = _FakeSignalData(SourceName.FINANCE, close_price=9.90, prev_close=10.00)
    rh_data = _FakeSignalData(SourceName.ROBINHOOD, close_price=9.90, prev_close=10.00)

    import scripts.file_lock as file_lock_module

    real_cross_process_lock = file_lock_module.cross_process_lock
    calls = []

    @contextmanager
    def _tracking_lock(lock_path, **kwargs):
        calls.append(lock_path)
        with real_cross_process_lock(lock_path, **kwargs):
            yield

    monkeypatch.setattr(file_lock_module, "cross_process_lock", _tracking_lock)

    run_daily_compact(
        now, config, state, data, rh_data, broker,
        options=CompactRunOptions(artifacts_dir=str(tmp_path / "artifacts")),
    )

    ledger_lock_calls = [p for p in calls if p.name == "ledger.lock" and p.parent.name == "BYDDY"]
    assert ledger_lock_calls, "expected the ticker's own ledger.lock to be taken for the live recovery"


def test_book_reconciled_whole_share_order_durably_corrects_already_booked_notional(tmp_path):
    """Codex review ("Reconcile provisional notional in StrategyState"): a
    claim already durably booked (state_booked=True) at its full planned
    live-order notional, whose fill later settles LOWER, must have
    StrategyState's own day/week notional corrected by the same delta the
    artifact was corrected by -- not stay frozen at the original (higher)
    figure. The correction must NOT bump the attempt/order-count counters,
    since the attempt was already counted the first time this claim was
    booked.
    """
    order_id = "RH-NOTIONAL-CORRECT-1"
    claim_day = date(2026, 8, 15)
    trading_day = date(2026, 8, 16)

    # First gated run: books the claim as live_order_found at its full $10
    # planned notional, and the marker gets committed (simulating the
    # enclosing state_transaction() saving it).
    first_dir = tmp_path / "first"
    first_dir.mkdir()
    _near_close_dca.atomic_write_text(
        first_dir / "run_state.json",
        _near_close_dca.json.dumps(_blocked_stub("2026-08-16", triggered_at_utc="2026-08-16T19:35:00Z")),
    )
    state = StrategyState()
    live_result = {"live_order_found": True, "order_id": order_id, "planned_notional_usd": 10.0}
    _near_close_dca._persist_gated_whole_share_recovery(
        first_dir, "BYDDY", claim_day.isoformat(), live_result, trading_day, tmp_path,
    )
    _near_close_dca._book_reconciled_whole_share_order_durably(
        state, "BYDDY", claim_day.isoformat(), live_result, trading_day, tmp_path,
        already_recorded=False,
    )
    _near_close_dca._mark_gated_whole_share_recovery_state_booked(
        first_dir, "BYDDY", claim_day.isoformat(), order_id, trading_day,
    )
    assert state.day_notional(claim_day) == pytest.approx(10.0)
    assert state.buys_today("BYDDY", claim_day) == 1

    # Second gated run (fresh StrategyState reload, exactly like a real new
    # invocation): the SAME order settled at $4, not $10.
    second_dir = tmp_path / "second"
    second_dir.mkdir()
    _near_close_dca.atomic_write_text(
        second_dir / "run_state.json",
        _near_close_dca.json.dumps(_blocked_stub("2026-08-17", triggered_at_utc="2026-08-17T19:35:00Z")),
    )
    terminal_result = {"order_id": order_id, "shares_purchased": 1, "order_notional_usd": 4.0}
    already_recorded = _near_close_dca._gated_whole_share_recovery_already_recorded(
        "BYDDY", claim_day.isoformat(), terminal_result, trading_day, tmp_path,
    )
    assert already_recorded is True
    delta = _near_close_dca._persist_gated_whole_share_recovery(
        second_dir, "BYDDY", claim_day.isoformat(), terminal_result, trading_day, tmp_path,
    )
    assert delta == pytest.approx(-6.0)
    _near_close_dca._book_reconciled_whole_share_order_durably(
        state, "BYDDY", claim_day.isoformat(), terminal_result, trading_day, tmp_path,
        already_recorded=already_recorded, notional_correction_usd=delta,
    )

    assert state.day_notional(claim_day) == pytest.approx(4.0)
    # No second attempt counted -- this is a correction, not a new booking.
    assert state.buys_today("BYDDY", claim_day) == 1


def test_gated_recovery_notional_correction_clears_a_stale_earlier_week_reserve(tmp_path):
    """Codex review ("Invalidate the reserve after correcting prior-week
    spend"): a correction that changes an EARLIER ISO week's own
    submitted_notional_by_week must clear reserve_week_key when it no
    longer matches that earlier week -- otherwise roll_weekly_reserve()
    keeps returning early on every later call, using a reserve computed
    from the STALE pre-correction prior-week figure and authorizing excess
    stress-tier spend.
    """
    order_id = "RH-RESERVE-INVALIDATE-1"
    claim_day = date(2026, 8, 10)  # an earlier ISO week than trading_day below
    trading_day = date(2026, 8, 17)  # a later ISO week

    first_dir = tmp_path / "first"
    first_dir.mkdir()
    _near_close_dca.atomic_write_text(
        first_dir / "run_state.json",
        _near_close_dca.json.dumps(_blocked_stub("2026-08-17", triggered_at_utc="2026-08-17T19:35:00Z")),
    )
    state = StrategyState()
    live_result = {"live_order_found": True, "order_id": order_id, "planned_notional_usd": 10.0}
    _near_close_dca._persist_gated_whole_share_recovery(
        first_dir, "BYDDY", claim_day.isoformat(), live_result, trading_day, tmp_path,
    )
    _near_close_dca._book_reconciled_whole_share_order_durably(
        state, "BYDDY", claim_day.isoformat(), live_result, trading_day, tmp_path,
        already_recorded=False,
    )
    # Fresh booking for an earlier-week claim must already clear the key.
    assert state.reserve_week_key is None
    _near_close_dca._mark_gated_whole_share_recovery_state_booked(
        first_dir, "BYDDY", claim_day.isoformat(), order_id, trading_day,
    )

    # Simulate roll_weekly_reserve() having already computed THIS week's
    # reserve before the correction below arrives.
    from near_close_dca import iso_week_key

    state.reserve_week_key = iso_week_key(trading_day)

    second_dir = tmp_path / "second"
    second_dir.mkdir()
    _near_close_dca.atomic_write_text(
        second_dir / "run_state.json",
        _near_close_dca.json.dumps(_blocked_stub("2026-08-18", triggered_at_utc="2026-08-18T19:35:00Z")),
    )
    terminal_result = {"order_id": order_id, "shares_purchased": 1, "order_notional_usd": 15.0}
    already_recorded = _near_close_dca._gated_whole_share_recovery_already_recorded(
        "BYDDY", claim_day.isoformat(), terminal_result, trading_day, tmp_path,
    )
    delta = _near_close_dca._persist_gated_whole_share_recovery(
        second_dir, "BYDDY", claim_day.isoformat(), terminal_result, trading_day, tmp_path,
    )
    _near_close_dca._book_reconciled_whole_share_order_durably(
        state, "BYDDY", claim_day.isoformat(), terminal_result, trading_day, tmp_path,
        already_recorded=already_recorded, notional_correction_usd=delta,
    )

    assert state.reserve_week_key is None


def test_gated_dedup_finds_state_booked_evidence_beyond_the_kill_switch_window(tmp_path, monkeypatch):
    """Codex review ("Search beyond the kill-switch window for booking
    deduplication"): state-booking dedup must find a matching
    state_booked=True row even when its artifact is OLDER than the
    kill-switch trailing window -- a whole-share claim can stay unresolved
    in the ledger far longer than that window, and deployment-risk
    aggregation being window-bounded is a SEPARATE concern from claim-
    identity dedup.
    """
    monkeypatch.setenv("DCA_CRON_TRACKING_ROOT", str(tmp_path / "cron_tracking"))
    from dca_risk import KILL_SWITCH_WINDOW_DAYS

    order_id = "RH-STALE-DEDUP-1"
    claim_day = date(2026, 7, 1)
    old_trading_day = claim_day + timedelta(days=1)
    stale_dir = tmp_path / "stale"
    stale_dir.mkdir()
    _near_close_dca.atomic_write_text(
        stale_dir / "run_state.json",
        _near_close_dca.json.dumps(
            _blocked_stub(old_trading_day.isoformat(), triggered_at_utc=f"{old_trading_day.isoformat()}T19:35:00Z")
        ),
    )
    live_result = {"live_order_found": True, "order_id": order_id, "planned_notional_usd": 10.0}
    _near_close_dca._persist_gated_whole_share_recovery(
        stale_dir, "BYDDY", claim_day.isoformat(), live_result, old_trading_day, tmp_path,
    )
    _near_close_dca._mark_gated_whole_share_recovery_state_booked(
        stale_dir, "BYDDY", claim_day.isoformat(), order_id, old_trading_day,
    )

    # A retry FAR beyond the kill-switch window (the stale artifact's own
    # day is well outside it) must still find the state_booked evidence.
    much_later = claim_day + timedelta(days=KILL_SWITCH_WINDOW_DAYS + 30)
    assert (much_later - old_trading_day).days > KILL_SWITCH_WINDOW_DAYS
    already_recorded = _near_close_dca._gated_whole_share_recovery_already_recorded(
        "BYDDY", claim_day.isoformat(), live_result, much_later, tmp_path,
    )
    assert already_recorded is True


def test_ordinary_live_order_row_is_not_trusted_as_state_booked(tmp_path):
    """Codex review ("Do not infer a StrategyState commit from ordinary
    rows"): an ORDINARY (gate-passing) row carrying
    whole_share_settlement_reconciliation_needed=True is
    _book_reconciled_whole_share_order()'s own live_order_found publish --
    its claim stays UNRESOLVED in the ledger, exactly like a gated
    live_order_found row, so its own run_state.json write and the
    StrategyState save that must vouch for it are still two separate
    durability events. Row existence alone must NOT prove booking, unlike
    an ordinary TERMINAL row (whose claim is already resolved and can
    never be rediscovered regardless).
    """
    live_ordinary_row = {
        "ticker": "BYDDY",
        "status": "submitted",
        "order_id": "RH-ORDINARY-LIVE-1",
        "whole_share_claim_breakdown": [{"trading_day": "2026-08-15", "amount_usd": 10.0}],
        "whole_share_settlement_reconciliation_needed": True,
        # No recovered_on_gated_path, no state_booked -- exactly what the
        # ordinary live_order_found publish writes.
    }
    _, found, state_booked, _, _ = _near_close_dca._total_recorded_whole_share_claim_amount(
        "RH-ORDINARY-LIVE-1", "BYDDY", "2026-08-15", date(2026, 8, 16), None, [live_ordinary_row],
    )
    assert found is True
    assert state_booked is False

    # An ordinary TERMINAL row (no reconciliation-needed flag) is still
    # trusted -- its claim is already resolved and can never be
    # rediscovered by a later gated retry regardless.
    terminal_ordinary_row = {
        "ticker": "BYDDY",
        "status": "submitted",
        "order_id": "RH-ORDINARY-TERMINAL-1",
        "whole_share_claim_breakdown": [{"trading_day": "2026-08-15", "amount_usd": 10.0}],
    }
    _, _, terminal_state_booked, _, _ = _near_close_dca._total_recorded_whole_share_claim_amount(
        "RH-ORDINARY-TERMINAL-1", "BYDDY", "2026-08-15", date(2026, 8, 16), None, [terminal_ordinary_row],
    )
    assert terminal_state_booked is True


def test_gated_dedup_finds_current_stub_evidence_without_a_history_root(tmp_path):
    """Codex review ("Include the current stub in gated booking
    deduplication"): run_daily_compact()'s documented default
    deployment_history_root=None means _total_recorded_whole_share_claim_
    amount's own history scan runs nothing at all -- but the CURRENT
    run's own artifacts_dir/run_state.json can already carry a
    state_booked=True row (e.g. from an earlier ticker's reconciliation in
    this same gated pass). That row must still be found even with no
    deployment_history_root.
    """
    order_id = "RH-CURRENT-STUB-DEDUP-1"
    artifacts_dir = tmp_path / "artifacts"
    artifacts_dir.mkdir()
    stub = _blocked_stub("2026-08-16", triggered_at_utc="2026-08-16T19:35:00Z")
    stub["buy_candidates"] = [
        {
            "ticker": "BYDDY",
            "status": "submitted",
            "order_id": order_id,
            "whole_share_claim_breakdown": [{"trading_day": "2026-08-15", "amount_usd": 10.0}],
            "recovered_on_gated_path": True,
            "state_booked": True,
        }
    ]
    _near_close_dca.atomic_write_text(artifacts_dir / "run_state.json", _near_close_dca.json.dumps(stub))

    reconciled = {"order_id": order_id, "shares_purchased": 1, "order_notional_usd": 10.0}
    already_recorded = _near_close_dca._gated_whole_share_recovery_already_recorded(
        "BYDDY", "2026-08-15", reconciled, date(2026, 8, 16), None, artifacts_dir,
    )
    assert already_recorded is True

    # Without the current-stub fallback (no artifacts_dir passed), the
    # lookup has nothing to scan at all with deployment_history_root=None.
    still_missing = _near_close_dca._gated_whole_share_recovery_already_recorded(
        "BYDDY", "2026-08-15", reconciled, date(2026, 8, 16), None,
    )
    assert still_missing is False


def test_gated_dedup_does_not_double_count_the_current_stub(tmp_path):
    """The current-stub fallback above must not double-count when
    deployment_history_root ALSO covers the same artifacts_dir (run_
    pipeline's own default is Path(artifacts_dir).parent) -- the row would
    otherwise be found once via the direct stub read and again via the
    history scan.
    """
    order_id = "RH-NO-DOUBLE-COUNT-1"
    artifacts_dir = tmp_path / "artifacts"
    artifacts_dir.mkdir()
    stub = _blocked_stub("2026-08-16", triggered_at_utc="2026-08-16T19:35:00Z")
    stub["buy_candidates"] = [
        {
            "ticker": "BYDDY",
            "status": "submitted",
            "order_id": order_id,
            "whole_share_claim_breakdown": [{"trading_day": "2026-08-15", "amount_usd": 10.0}],
            "recovered_on_gated_path": True,
            "state_booked": True,
        }
    ]
    _near_close_dca.atomic_write_text(artifacts_dir / "run_state.json", _near_close_dca.json.dumps(stub))

    total, found, state_booked, _, _ = _near_close_dca._total_recorded_whole_share_claim_amount(
        order_id, "BYDDY", "2026-08-15", date(2026, 8, 16), tmp_path,
        _near_close_dca._current_gated_stub_candidates(artifacts_dir),
        exclude_path=artifacts_dir / "run_state.json",
    )
    assert found is True
    assert state_booked is True
    assert total == pytest.approx(10.0)


def test_gated_live_recovery_fails_closed_with_no_artifacts_dir(tmp_path):
    """Codex review ("Fail the settlement hook when no artifact directory
    exists"): run_and_report()'s RunContext omits artifacts_dir entirely
    (stays None). _persist_gated_whole_share_recovery, invoked as the
    before_settle/before_release hook, must raise rather than silently
    report success -- reporting success there lets the ledger claim
    resolve irreversibly with NO recovery row published anywhere.
    """
    with pytest.raises(RuntimeError, match="no artifacts_dir"):
        _near_close_dca._persist_gated_whole_share_recovery(
            None, "BYDDY", "2026-08-15",
            {"order_id": "RH-NO-DIR-1", "shares_purchased": 1, "order_notional_usd": 10.0},
            date(2026, 8, 16), tmp_path,
        )


def test_still_claimed_already_locked_avoids_self_deadlock(tmp_path, monkeypatch):
    """Codex review ("Revalidate the claim after acquiring the
    live-recovery lock"): a caller already holding a ticker's own
    ledger.lock must be able to revalidate via still_claimed(
    already_locked=True) without re-acquiring that SAME non-reentrant
    lock -- the normal (locking) call path would self-deadlock.
    """
    monkeypatch.setenv("DCA_CRON_TRACKING_ROOT", str(tmp_path / "cron_tracking"))
    from dca_whole_share_accumulator import WholeShareAccumulationLedger
    from scripts.file_lock import cross_process_lock

    ledger = WholeShareAccumulationLedger()
    decision = ledger.record_contribution(
        "BYDDY", "2026-08-15", 10.0, 10.0, stream="afternoon",
        idempotency_key="afternoon-BYDDY", recorded_at="2026-08-15T15:30:00-04:00",
    )

    with cross_process_lock(ledger.root / "BYDDY" / "ledger.lock"):
        # This would hang (and eventually deadlock-timeout) without
        # already_locked=True.
        result = ledger.still_claimed(
            "BYDDY", "2026-08-15", "afternoon-BYDDY", "2026-08-15T15:30:00-04:00",
            claim_token=decision.claim_token,
            already_locked=True,
        )
    assert result is True


def test_gated_recovery_marker_is_queued_not_written_immediately(tmp_path, monkeypatch):
    """Codex review ("Mark recovery only after committing StrategyState"):
    state.record_buy() for a gated recovery is only in-memory until the
    ENCLOSING dca_runner.state_transaction() durably saves it, long after
    run_daily_compact/_run_once_impl return. Writing the "state_booked"
    artifact marker synchronously would let a crash in that window leave a
    marker proving a booking that never reached disk, permanently losing
    the claim's counters. The marker must instead be queued on
    ``state.pending_durable_commit_callbacks`` and fire only once flushed.
    """
    monkeypatch.setenv("DCA_CRON_TRACKING_ROOT", str(tmp_path / "cron_tracking"))
    ledger = WholeShareAccumulationLedger()
    claim_trading_day = date(2026, 8, 15)
    ledger.record_contribution(
        "BYDDY", claim_trading_day.isoformat(), 10.0, 10.0, stream="afternoon",
        idempotency_key="afternoon-BYDDY", recorded_at="2026-08-15T15:30:00-04:00",
    )
    now = datetime(2026, 8, 16, 10, 0, tzinfo=EASTERN)
    config = StrategyConfig(
        dry_run=False,
        tickers=(),
        whole_share_tickers=frozenset(),
        approved_test_dates=frozenset({claim_trading_day}),
    )
    state = StrategyState()

    class _QueuedMarkerBroker(DryRunBroker):
        def same_day_strategy_order_exists(self, ticker, trading_day):
            return False

        def same_day_strategy_order_exists_with_ref_id_prefix(self, ticker, trading_day, ref_id_prefixes):
            if ref_id_prefixes == ("graded-dca-wholeshare-2026-08-15-BYDDY",):
                return {"id": "RH-QUEUED-MARKER-1", "state": "filled", "quantity": "1"}
            return None

    broker = _QueuedMarkerBroker(cash=500.0, reference_prices={"BYDDY": 10.0})
    data = _FakeSignalData(SourceName.FINANCE, close_price=9.90, prev_close=10.00)
    rh_data = _FakeSignalData(SourceName.ROBINHOOD, close_price=9.90, prev_close=10.00)
    artifacts_dir = tmp_path / "artifacts"

    run_daily_compact(
        now, config, state, data, rh_data, broker,
        options=CompactRunOptions(artifacts_dir=str(artifacts_dir)),
    )

    # state.record_buy() already happened (in-memory), but the artifact
    # marker must NOT be written yet -- only queued.
    assert state.day_notional(claim_trading_day) == pytest.approx(10.0)
    assert len(state.pending_durable_commit_callbacks) == 1
    recovered_state = _near_close_dca.json.loads((artifacts_dir / "run_state.json").read_bytes())
    assert recovered_state["buy_candidates"][0]["state_booked"] is False

    # Simulating a crash before the enclosing state_transaction() ever saves
    # state: the callback is simply dropped, never invoked. A later retry
    # must still see already_recorded=False and be able to re-book -- the
    # queued-but-never-fired marker must not have leaked onto disk.
    reconciled = {"order_id": "RH-QUEUED-MARKER-1", "order_notional_usd": 10.0, "shares_purchased": 1}
    still_unbooked = _near_close_dca._gated_whole_share_recovery_already_recorded(
        "BYDDY", claim_trading_day.isoformat(), reconciled, date(2026, 8, 16), tmp_path,
    )
    assert still_unbooked is False

    # Now simulate the commit actually happening (what dca_runner.
    # state_transaction() does after save_strategy_state() succeeds): flush
    # the queue, and the marker becomes durable.
    for callback in state.pending_durable_commit_callbacks:
        callback()
    state.pending_durable_commit_callbacks = []
    recovered_state = _near_close_dca.json.loads((artifacts_dir / "run_state.json").read_bytes())
    assert recovered_state["buy_candidates"][0]["state_booked"] is True


def test_state_transaction_marks_gated_recovery_only_after_saving(tmp_path, monkeypatch):
    """End-to-end version of the queuing behavior above, through the real
    dca_runner.state_transaction() a live caller actually uses: the queued
    marker callback must fire only after save_strategy_state() returns, and
    must not fire at all if the save raises.
    """
    import dca_runner

    monkeypatch.setenv("DCA_CRON_TRACKING_ROOT", str(tmp_path / "cron_tracking"))
    state_path = tmp_path / "strategy_state.json"

    calls = []
    with dca_runner.state_transaction(path=state_path) as state:
        state.pending_durable_commit_callbacks.append(lambda: calls.append("committed"))
        assert calls == []  # not fired yet -- save hasn't happened

    assert calls == ["committed"]
    assert state.pending_durable_commit_callbacks == []

    # A save failure must not fire the queued callback: nothing was
    # actually committed, so nothing should claim it was.
    failing_calls = []

    def _boom(*args, **kwargs):
        raise RuntimeError("simulated durable-save failure")

    monkeypatch.setattr(dca_runner, "save_strategy_state", _boom)
    with pytest.raises(RuntimeError, match="simulated durable-save failure"):
        with dca_runner.state_transaction(path=state_path) as state:
            state.pending_durable_commit_callbacks.append(lambda: failing_calls.append("committed"))

    assert failing_calls == []


def test_state_transaction_runs_every_commit_callback_even_after_one_fails(tmp_path, monkeypatch):
    """Codex review ("Run later commit callbacks after one marker fails"):
    when a run recovers MULTIPLE claims in one invocation, it queues one
    marker callback per claim. save_strategy_state() commits ALL of their
    StrategyState mutations together in a single write -- so if the FIRST
    queued callback raises (e.g. its own artifact write fails), the
    SECOND and THIRD claims' callbacks must still run: their own
    StrategyState bookings are already durably saved regardless, and
    skipping their marker writes would make them look unbooked to a later
    retry, which then double-books them even though nothing was wrong
    with them.
    """
    import dca_runner

    monkeypatch.setenv("DCA_CRON_TRACKING_ROOT", str(tmp_path / "cron_tracking"))
    state_path = tmp_path / "strategy_state.json"

    ran = []

    def _failing_first():
        ran.append("first")
        raise RuntimeError("first marker write failed")

    def _ok_second():
        ran.append("second")

    def _ok_third():
        ran.append("third")

    with pytest.raises(RuntimeError, match="first marker write failed"):
        with dca_runner.state_transaction(path=state_path) as state:
            state.pending_durable_commit_callbacks.append(_failing_first)
            state.pending_durable_commit_callbacks.append(_ok_second)
            state.pending_durable_commit_callbacks.append(_ok_third)

    # ALL three ran -- the second and third weren't skipped just because
    # the first raised.
    assert ran == ["first", "second", "third"]


def test_gated_marker_write_failure_raises_instead_of_silently_swallowing(tmp_path):
    """Codex review ("Make the post-save booking marker failure-safe"):
    the state_booked marker callback now runs AFTER StrategyState is
    already durably saved -- a read/write failure here can no longer be
    silently swallowed (as an earlier revision did), since that would
    leave the already-committed record_buy() permanently unmarked and let
    every later retry double-book the same order. It must raise instead,
    surfacing the anomaly immediately.
    """
    artifacts_dir = tmp_path / "artifacts"
    artifacts_dir.mkdir()
    (artifacts_dir / "run_state.json").write_bytes(b"not valid json")

    with pytest.raises(RuntimeError, match="could not mark gated whole-share recovery state-booked"):
        _near_close_dca._mark_gated_whole_share_recovery_state_booked(
            artifacts_dir, "BYDDY", "2026-08-15", "RH-1", date(2026, 8, 16),
        )


def test_ordinary_recovery_rows_are_treated_as_already_state_booked(tmp_path):
    """Codex review ("Treat ordinary recovery rows as already
    state-booked"): a row published by the NORMAL (gate-passing)
    _book_reconciled_whole_share_order() -- not the gated path -- never
    sets state_booked at all, because its state.record_buy() and its own
    candidate-row publish happen together in the SAME run, saved by that
    SAME state_transaction() commit. Finding such a row at all already
    proves booking happened; treating the merely-absent field as
    state_booked=False would make a later gated retry of the same
    still-live claim call record_buy() again for an order StrategyState
    already durably has.
    """
    ordinary_row = {
        "ticker": "BYDDY",
        "status": "submitted",
        "order_id": "RH-ORDINARY-1",
        "whole_share_claim_breakdown": [{"trading_day": "2026-08-15", "amount_usd": 10.0}],
        # No "recovered_on_gated_path" and no "state_booked" field at all --
        # exactly what the ordinary (non-gated) publish path writes.
    }
    total, found, state_booked, _, _ = _near_close_dca._total_recorded_whole_share_claim_amount(
        "RH-ORDINARY-1", "BYDDY", "2026-08-15", date(2026, 8, 16), None, [ordinary_row],
    )
    assert found is True
    assert state_booked is True
    assert total == pytest.approx(10.0)

    # A GATED row with the SAME shape but no state_booked=True must still
    # be treated as NOT booked -- this test must not weaken that guarantee.
    gated_row = {
        "ticker": "BYDDY",
        "status": "submitted",
        "order_id": "RH-GATED-1",
        "whole_share_claim_breakdown": [{"trading_day": "2026-08-15", "amount_usd": 10.0}],
        "recovered_on_gated_path": True,
        "state_booked": False,
    }
    _, _, gated_state_booked, _, _ = _near_close_dca._total_recorded_whole_share_claim_amount(
        "RH-GATED-1", "BYDDY", "2026-08-15", date(2026, 8, 16), None, [gated_row],
    )
    assert gated_state_booked is False


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
    from dca_risk import deployed_usd

    # Exercise the actual next-run consumer rather than only asserting the
    # artifact's intermediate shape: the recovered fill must remain visible
    # after this invocation's in-memory StrategyState is gone.
    assert deployed_usd(run_state) == pytest.approx(10.0)


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
        {"trading_day": "2026-08-01", "order_id": "RH-OUT-OF-WINDOW-1", "amount_usd": 10.0}
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


def test_terminal_partial_fill_does_not_rebook_after_losing_claim_ownership(tmp_path, monkeypatch):
    """The terminal-partial-fill sibling obeys the same ownership result.

    A terminal placement response is authoritative evidence of its partial
    fill, but it does not make this caller the owner of ledger settlement.
    When another reconciler wins that transition first, this path must not
    book or publish the same broker fill a second time.
    """
    monkeypatch.setenv("DCA_CRON_TRACKING_ROOT", str(tmp_path / "cron_tracking"))
    config = StrategyConfig(whole_share_tickers=frozenset({"BYDDY"}), dry_run=False)

    class _TerminalPartialFillBroker(DryRunBroker):
        def place_whole_share_buy(self, ticker, quantity, ref_id):
            raise WholeSharePlacementTerminalError(
                "order partially filled before cancellation",
                order={
                    "id": "RH-WON-BY-SOMEONE-ELSE-2",
                    "state": "cancelled",
                    "cumulative_quantity": "1",
                    "average_price": "10.25",
                },
            )

    broker = _TerminalPartialFillBroker(cash=500.0, reference_prices={"BYDDY": 10.0})

    import dca_whole_share_accumulator as wsa_module

    # A stub that unconditionally returns False would pass every assertion
    # below even if the terminal-partial-fill branch stopped passing
    # require_claim_ownership=True to settle() -- the real settle() only
    # skips its shares_purchased_total/audit-event mutations (and thus
    # returns False without double-booking the ledger) when that flag is
    # actually set. Capture the kwargs settle() was called with so this
    # test fails, rather than silently passing, if that argument regresses
    # (Codex review: "Exercise the real settlement ownership guard").
    settle_calls = []

    def _lost_the_race_settle(self, *args, **kwargs):
        settle_calls.append(kwargs)
        return False  # cleanly resolved elsewhere -- not an exception

    monkeypatch.setattr(wsa_module.WholeShareAccumulationLedger, "settle", _lost_the_race_settle)

    ctx = _ctx(config, broker)
    outcome, _, running_deployed_7d, errors, _ = _process_buy_candidate(
        _decision(suggested_amount_usd=20.0, target_price=10.0),
        ctx=ctx,
        running_deployed_7d=0.0,
    )

    assert outcome["status"] == "skipped"
    assert outcome["order_id"] == "RH-WON-BY-SOMEONE-ELSE-2"
    assert errors == 0
    assert running_deployed_7d == 0.0
    assert ctx.state.day_notional(date(2026, 8, 16)) == 0.0
    assert ("BYDDY", date(2026, 8, 16)) not in ctx.state.own_orders_recorded
    assert settle_calls, "the terminal-partial-fill branch never called settle()"
    assert settle_calls[-1].get("require_claim_ownership") is True


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
    broker = DryRunBroker(cash=500.0, reference_prices={"BYDDY": 50.0})

    decision = _decision(suggested_amount_usd=50.0, target_price=50.0)
    outcome, top_up_outcomes, _, _, _ = _process_buy_candidate(
        decision, ctx=_ctx(config, broker), running_deployed_7d=0.0
    )
    assert outcome["status"] == "blocked"
    assert outcome["whole_share_planned_notional_usd"] == 50.0

    rows = _build_buy_candidates([decision], {"BYDDY": outcome}, {})
    assert rows[0]["planned_amount_usd"] == 50.0


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
        "whole_share_attempt_breakdown": [
            {"trading_day": "2026-08-18"},
            {"trading_day": None},
        ],
    }

    rows = _build_buy_candidates([decision], {"BYDDY": outcome}, {})

    assert len(rows) == 1
    assert rows[0]["whole_share_pending_usd"] == 7.0
    assert rows[0]["whole_share_contribution_usd"] == 3.0
    assert rows[0]["whole_share_quantity"] is None  # not fired this run
    assert rows[0]["whole_share_attempt_breakdown"] == [
        {"trading_day": "2026-08-18"},
        {"trading_day": None},
    ]


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
    broker = DryRunBroker(cash=500.0, reference_prices={"BYDDY": 50.0})

    def _raising_preserve_pending(self, *args, **kwargs):
        raise RuntimeError("ledger lock timeout")

    monkeypatch.setattr(WholeShareAccumulationLedger, "preserve_pending", _raising_preserve_pending)

    outcome, _, _, errors, _ = _process_buy_candidate(
        _decision(suggested_amount_usd=50.0, target_price=50.0),
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
        "BYDDY", "2026-08-16", 50.0, 1_000_000.0, stream="seed", idempotency_key="seed"
    )
    assert ledger.pending_usd("BYDDY") == 50.0

    config = StrategyConfig(whole_share_tickers=frozenset({"BYDDY"}), dry_run=False)
    # $50 share price exceeds the default $10 max_per_order_usd ceiling --
    # blocked before any broker round-trip, reaching _preserve_and_block.
    broker = DryRunBroker(cash=500.0, reference_prices={"BYDDY": 50.0})

    def _raising_preserve_pending(self, *args, **kwargs):
        raise RuntimeError("ledger lock timeout")

    monkeypatch.setattr(WholeShareAccumulationLedger, "preserve_pending", _raising_preserve_pending)

    outcome, _, _, errors, _ = _process_buy_candidate(
        _decision(action=Action.NO_ACTION, suggested_amount_usd=0.0, target_price=50.0),
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

    assert _whole_share_reconciled_price(decision) == 10.09

    # Symmetric: whichever source happens to be higher wins, not "finance
    # unless robinhood is higher" or a fixed source preference.
    reversed_decision = ReconciledDecision(
        ticker="BYDDY",
        action=Action.NO_ACTION,
        reason="test",
        finance_signal=_source_signal(SourceName.FINANCE, quote_price=10.09, suggested_amount_usd=0.0),
        robinhood_signal=_source_signal(SourceName.ROBINHOOD, quote_price=9.90, suggested_amount_usd=0.0),
    )

    assert _whole_share_reconciled_price(reversed_decision) == 10.09

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

    assert _whole_share_reconciled_price(disagreeing_decision) is None


def test_higher_reconciled_quote_prevents_threshold_and_ceiling_overspend(tmp_path, monkeypatch):
    """A tolerated quote difference must not make the cheaper source executable.

    Finance's $9.90 quote is within two percent of Robinhood's $10.09 quote,
    but the order executes at Robinhood.  A $10 contribution therefore cannot
    fund one share and must remain pending rather than slipping past both the
    accumulator threshold and the default $10 per-order ceiling.
    """
    monkeypatch.setenv("DCA_CRON_TRACKING_ROOT", str(tmp_path / "cron_tracking"))
    config = StrategyConfig(whole_share_tickers=frozenset({"BYDDY"}), dry_run=False)
    broker = DryRunBroker(cash=500.0, reference_prices={"BYDDY": 10.09})
    decision = ReconciledDecision(
        ticker="BYDDY",
        action=Action.BUY,
        reason="finance and Robinhood agree on BUY",
        finance_signal=_source_signal(SourceName.FINANCE, quote_price=9.90, suggested_amount_usd=10.0),
        robinhood_signal=_source_signal(SourceName.ROBINHOOD, quote_price=10.09, suggested_amount_usd=10.0),
        suggested_amount_usd=10.0,
        selected_base_amount_usd=1.0,
        target_price=9.90,
        decision_bar_close=9.90,
    )

    outcome, _, _, errors, reviews = _process_buy_candidate(
        decision,
        ctx=_ctx(config, broker),
        running_deployed_7d=0.0,
    )

    assert outcome["status"] == "skipped"
    assert "threshold not yet reached" in outcome["skip_reason"]
    assert outcome["whole_share_pending_usd"] == pytest.approx(10.0)
    assert errors == 0
    assert reviews == 0
    ledger = WholeShareAccumulationLedger()
    assert ledger.pending_usd("BYDDY") == pytest.approx(10.0)
    assert ledger.shares_purchased_total("BYDDY") == 0


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
    _whole_share_reconciled_price returns (the HIGHER of the two
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
