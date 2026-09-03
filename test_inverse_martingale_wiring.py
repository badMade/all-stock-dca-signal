"""End-to-end wiring tests: the opt-in inverse-Martingale accumulator through
run_daily_compact -> _run_once_impl -> _process_buy_candidate, and the
default-off regression guarantee.

Complements test_dca_inverse_martingale.py (pure decision-function/ledger unit
tests) and test_dca_config.py (loader tests) -- this file only exercises the
wiring itself.
"""

from __future__ import annotations

import csv
import json
from datetime import date, datetime, time, timedelta
from typing import Any, cast

import pytest

from dca_cumulative import CumulativeCreditLedger
from dca_inverse_martingale import (
    STATUS_DIPPING,
    STATUS_FIRING,
    STATUS_NONE,
    InverseMartingaleLedger,
    InverseMartingaleStep,
)
from near_close_dca import (
    EASTERN,
    Bar,
    CompactRunOptions,
    DryRunBroker,
    InverseMartingaleLeaseRecovery,
    PriorSameDayAllocation,
    SourceName,
    StrategyConfig,
    StrategyState,
    _book_recovered_lease_durably,
    _inverse_martingale_advance_age_for_missing_data,
    _inverse_martingale_reset_if_disabled_mid_cycle,
    run_daily_compact,
)

PCT_ENROLLMENT = {
    "basis": "pct",
    "tiers": ((1.0, 1.0), (2.0, 2.0), (4.0, 4.0), (8.0, 8.0)),
    "recovery_offset": 1.0,
}

TEST_DATES = (date(2026, 6, 29), date(2026, 6, 30), date(2026, 7, 1), date(2026, 7, 2))


class _FixedPriceData:
    """Minimal provider: a constant prior close, a caller-supplied decision
    price, a declining 60-minute trend (required by evaluate_source_signal's
    trend filter), and a tight bid/ask so the scheduled-run spread gate
    passes."""

    def __init__(self, prev_close: float, decision_price: float, source=SourceName.FINANCE):
        self._prev = prev_close
        self._price = decision_price
        self.source = source

    def get_previous_close_bar(self, ticker, as_of):
        ts = datetime.combine(as_of.date() - timedelta(days=1), time(16, 0), tzinfo=EASTERN)
        return Bar(ts, self._prev, self._prev, self._prev, self._prev, 1000)

    def get_intraday_bars(self, ticker, start, end, interval_minutes):
        # Mirrors test_near_close_dca.py's _FakeData: bars in the last 30
        # minutes settle at the decision close, earlier bars sit higher so
        # the 60-minute trend reads as down.
        bars = []
        cursor = start
        late_threshold = end - timedelta(minutes=30)
        while cursor <= end:
            price = self._price if cursor >= late_threshold else self._price + 2.0
            bars.append(Bar(cursor, price, price, price, price, 100))
            cursor += timedelta(minutes=interval_minutes)
        return bars

    def get_quote(self, ticker, as_of):
        return (self._price, as_of)

    def get_bid_ask(self, ticker, as_of):
        return (self._price - 0.01, self._price + 0.01)


def _base_config(**overrides):
    # dry_run=False by default: settlement of a fired accumulator balance
    # only ever happens on a REAL ("submitted") placement outcome, never a
    # dry-run simulation (see near_close_dca.py's deferred settlement block)
    # -- most of these tests need to observe that settlement, so they need
    # the real (DryRunBroker-simulated-as-real) placement path.
    kwargs = dict(
        dry_run=False,
        tickers=("AAPL",),
        approved_test_dates=frozenset(TEST_DATES),
        max_per_order_usd=10.0,
    )
    kwargs.update(overrides)
    return StrategyConfig(**kwargs)


def _run_day(
    tmp_path, config, state, trading_day, decision_price, artifacts_label, scheduled=True,
    robinhood_price=None, broker_cash=500.0, prior_same_day_allocations=None, stream="afternoon",
    now_time=time(15, 45), prev_close=100.0, broker=None, deployment_history_root=None,
    classification_overrides=None,
):
    now = datetime.combine(trading_day, now_time, tzinfo=EASTERN)
    fin = _FixedPriceData(prev_close=prev_close, decision_price=decision_price, source=SourceName.FINANCE)
    rh = _FixedPriceData(
        prev_close=prev_close,
        decision_price=robinhood_price if robinhood_price is not None else decision_price,
        source=SourceName.ROBINHOOD,
    )
    broker = cast(Any, broker) or DryRunBroker(cash=broker_cash, reference_prices={"AAPL": decision_price})
    result = run_daily_compact(
        now,
        config,
        state,
        fin,
        rh,
        broker,
        options=CompactRunOptions(
            artifacts_dir=str(tmp_path / f"runs/{artifacts_label}"),
            scheduled=scheduled,
            prior_same_day_allocations=prior_same_day_allocations,
            stream=stream,
            deployment_history_root=deployment_history_root,
            classification_overrides=classification_overrides,
        ),
    )
    with open(cast(str, result["run_state_path"]), encoding="utf-8") as handle:
        run_state = json.load(handle)
    return result, run_state, broker


def test_disabled_by_default_no_artifacts_no_ref_id_segment(tmp_path, monkeypatch):
    monkeypatch.setenv("DCA_CRON_TRACKING_ROOT", str(tmp_path / "cron_tracking"))
    config = _base_config()  # config.inverse_martingale defaults to {}
    state = StrategyState()
    result, run_state, _ = _run_day(tmp_path, config, state, TEST_DATES[0], 90.0, "day1")
    assert result["blocked"] is False
    # A 10% dip on an un-enrolled ticker still buys normally through the
    # ordinary ladder path -- the accumulator must not suppress it.
    assert run_state["buy_candidates"], "un-enrolled ticker should still buy normally"
    for candidate in run_state["buy_candidates"]:
        assert "imart" not in (candidate.get("order_id") or "")
    assert not (tmp_path / "cron_tracking" / "inverse_martingale_dca").exists()


def test_enrolled_ticker_accumulates_without_buying(tmp_path, monkeypatch):
    monkeypatch.setenv("DCA_CRON_TRACKING_ROOT", str(tmp_path / "cron_tracking"))
    config = _base_config(inverse_martingale={"AAPL": PCT_ENROLLMENT})
    state = StrategyState()
    # 1% down -> crosses the first tier ($1 accumulated), but must NOT place
    # a normal-ladder order (suppressed by graded_buy_multiplier) nor fire
    # (recovery not yet triggered).
    result, run_state, _ = _run_day(tmp_path, config, state, TEST_DATES[0], 99.0, "day1")
    assert result["blocked"] is False
    assert run_state["buy_candidates"] == []
    tracker_path = tmp_path / "cron_tracking" / "inverse_martingale_dca" / "AAPL" / "tracker.json"
    assert tracker_path.exists()
    tracker = json.loads(tracker_path.read_text())
    assert tracker["status"] == "dipping"
    assert tracker["accumulated_usd"] == 1.0


def test_enrolled_ticker_fires_a_single_order_on_recovery(tmp_path, monkeypatch):
    monkeypatch.setenv("DCA_CRON_TRACKING_ROOT", str(tmp_path / "cron_tracking"))
    config = _base_config(inverse_martingale={"AAPL": PCT_ENROLLMENT})
    state = StrategyState()

    # Day 1: -1% crosses tier 1 ($1). Day 2: -3% crosses tier 2 (+$2 -> $3
    # total), sets the cycle low at 3.0%. Day 3: recovers to -1.5% (1.5pt off
    # the 3.0% low, clears the 1.0pt offset) -> fires the full $3.00.
    r1, s1, _ = _run_day(tmp_path, config, state, TEST_DATES[0], 99.0, "day1")
    assert s1["buy_candidates"] == []

    r2, s2, _ = _run_day(tmp_path, config, state, TEST_DATES[1], 97.0, "day2")
    assert s2["buy_candidates"] == []

    r3, s3, broker3 = _run_day(tmp_path, config, state, TEST_DATES[2], 98.5, "day3")
    assert r3["blocked"] is False
    [candidate] = s3["buy_candidates"]
    assert candidate["ticker"] == "AAPL"
    assert "imart" in (candidate.get("order_id") or "")
    # The submitted candidate row is the REAL synthetic fire decision, not
    # AAPL's original (enrollment-suppressed) ladder decision -- reason and
    # status correctly identify it as an inverse-Martingale fire rather than
    # some other non-BUY classification (Codex review: "Preserve the
    # synthetic fire decision in run artifacts").
    assert candidate["reason"] == "inverse_martingale_fire"
    assert candidate["status"] == "submitted"

    # signals.csv's auto_buy_signal reconciliation reads the SAME decisions
    # list -- both source rows for AAPL must reflect the real fire too.
    with open(cast(str, r3["signals_path"]), encoding="utf-8") as handle:
        signal_rows = [row for row in csv.DictReader(handle) if row.get("ticker") == "AAPL"]
    assert len(signal_rows) == 2  # finance + robinhood
    assert all(row["auto_buy_signal"] == "True" for row in signal_rows)

    tracker_path = tmp_path / "cron_tracking" / "inverse_martingale_dca" / "AAPL" / "tracker.json"
    # Fully resolved, but a retry-idempotency marker is retained rather than
    # deleting the file outright (see test_dca_inverse_martingale.py's
    # test_same_day_retry_after_full_fire_is_a_no_op).
    tracker = json.loads(tracker_path.read_text())
    assert tracker["status"] == "none"
    assert tracker["last_evaluated_trading_day"] == TEST_DATES[2].isoformat()

    with open(cast(str, r3["orders_csv_path"]), encoding="utf-8") as handle:
        rows = list(csv.DictReader(handle))
    imart_rows = [row for row in rows if "imart" in (row.get("order_id") or "")]
    assert len(imart_rows) == 1
    assert float(imart_rows[0]["notional_usd"]) == pytest.approx(3.0, abs=0.01)
    # Regression test for "Populate execution targets on synthetic fire
    # decisions". Enrollment forces every source signal non-BUY, so
    # reconcile_signals() never took the branch that populates target_price/
    # target_price_source/decision_bar_close via _target_fields() -- left
    # unset, this row's target_price would be the ReconciledDecision
    # default (0.0), which build_execution_record()'s `if target_price:`
    # guard reads as "no target" and skips slippage_pct entirely even for a
    # filled order.
    assert float(imart_rows[0]["target_price"]) > 0.0
    assert imart_rows[0]["target_price_source"]
    assert float(imart_rows[0]["decision_bar_close"]) > 0.0


def test_enrolled_ticker_never_gets_a_normal_ladder_buy_even_on_deep_dip(tmp_path, monkeypatch):
    monkeypatch.setenv("DCA_CRON_TRACKING_ROOT", str(tmp_path / "cron_tracking"))
    config = _base_config(inverse_martingale={"AAPL": PCT_ENROLLMENT})
    state = StrategyState()
    # An 8.5% dip would be a 5x-tier buy on the normal ladder; enrollment
    # must suppress that entirely, only accumulating.
    result, run_state, _ = _run_day(tmp_path, config, state, TEST_DATES[0], 91.5, "day1")
    assert result["blocked"] is False
    assert run_state["buy_candidates"] == []


def test_manual_run_does_not_advance_the_accumulator(tmp_path, monkeypatch):
    monkeypatch.setenv("DCA_CRON_TRACKING_ROOT", str(tmp_path / "cron_tracking"))
    config = _base_config(inverse_martingale={"AAPL": PCT_ENROLLMENT})
    state = StrategyState()
    _run_day(tmp_path, config, state, TEST_DATES[0], 99.0, "manual", scheduled=False)
    assert not (tmp_path / "cron_tracking" / "inverse_martingale_dca").exists()


def test_kill_switch_caps_a_fire_to_the_unit_base_and_preserves_remainder(tmp_path, monkeypatch):
    """A synthetic fire must still respect the dynamic risk kill switch,
    capped to the accumulator's per-unit base (not the full accumulated
    amount) -- exactly like a normal ladder buy is capped to its own
    selected base when the kill switch is engaged."""
    monkeypatch.setenv("DCA_CRON_TRACKING_ROOT", str(tmp_path / "cron_tracking"))
    config = _base_config(
        inverse_martingale={"AAPL": PCT_ENROLLMENT},
        monthly_cash_allocation_usd=100.0,
        kill_switch_7d_deploy_fraction=0.40,
        deployed_last_7d_usd=50.0,  # $50 > 40% of $100 -> kill switch engaged
        dry_run=False,
    )
    state = StrategyState()

    r1, s1, _ = _run_day(tmp_path, config, state, TEST_DATES[0], 99.0, "day1")  # +$1
    r2, s2, _ = _run_day(tmp_path, config, state, TEST_DATES[1], 97.0, "day2")  # +$2 -> $3, low at 3%
    r3, s3, broker3 = _run_day(tmp_path, config, state, TEST_DATES[2], 98.5, "day3")  # recovers -> fires

    [candidate] = s3["buy_candidates"]
    assert candidate["ticker"] == "AAPL"
    assert candidate["status"] == "submitted"
    # Capped to the $1 unit base, not the full $3.00 accumulated -- also
    # visible in the DryRunBroker's own synthesized order_id amount.
    assert candidate["planned_amount_usd"] == pytest.approx(1.0, abs=0.01)
    assert "1.00" in candidate["order_id"]

    tracker_path = tmp_path / "cron_tracking" / "inverse_martingale_dca" / "AAPL" / "tracker.json"
    assert tracker_path.exists()  # NOT cleared -- $2.00 remains for a later run
    tracker = json.loads(tracker_path.read_text())
    assert tracker["status"] == "firing"
    assert tracker["accumulated_usd"] == pytest.approx(2.0, abs=0.01)


def test_kill_switch_capped_rollback_stamps_the_current_stream_not_a_stale_one(tmp_path, monkeypatch):
    """Regression test for "Stamp the settling stream on partial rollbacks".
    The kill-switch-capped rollback branch is built from im_pre_fire_state
    (the state read BEFORE this run's own claim), which carries whatever
    last_evaluated_stream the accumulation phase last stamped -- here,
    "morning". Left unstamped with THIS run's own stream ("midday"), a
    genuine same-stream retry of the midday slot would not match the
    STATUS_FIRING same-day retry guard's own `last_evaluated_stream in
    (None, stream)` check, be treated as a genuinely new continuation, and
    submit a SECOND real order for the same logical slot.
    """
    monkeypatch.setenv("DCA_CRON_TRACKING_ROOT", str(tmp_path / "cron_tracking"))
    config = _base_config(
        inverse_martingale={"AAPL": PCT_ENROLLMENT},
        monthly_cash_allocation_usd=100.0,
        kill_switch_7d_deploy_fraction=0.40,
        deployed_last_7d_usd=50.0,  # $50 > 40% of $100 -> kill switch engaged
        dry_run=False,
    )
    state = StrategyState()

    _run_day(tmp_path, config, state, TEST_DATES[0], 99.0, "day1", stream="morning")  # +$1
    _run_day(tmp_path, config, state, TEST_DATES[1], 97.0, "day2", stream="morning")  # +$2 -> $3

    # Day 3, MIDDAY slot: recovers -> the kill switch caps the fire to the
    # $1 unit base, leaving $2 as a rolled-back STATUS_FIRING continuation.
    r3, s3, _broker3 = _run_day(tmp_path, config, state, TEST_DATES[2], 98.5, "day3", stream="midday")
    [candidate] = s3["buy_candidates"]
    assert candidate["status"] == "submitted"
    assert candidate["planned_amount_usd"] == pytest.approx(1.0, abs=0.01)

    tracker_path = tmp_path / "cron_tracking" / "inverse_martingale_dca" / "AAPL" / "tracker.json"
    tracker = json.loads(tracker_path.read_text())
    assert tracker["status"] == "firing"
    assert tracker["accumulated_usd"] == pytest.approx(2.0, abs=0.01)
    assert tracker["fire_attempts"] == 1
    assert tracker["last_evaluated_trading_day"] == TEST_DATES[2].isoformat()
    # The bug: before the fix, this stayed "morning" (im_pre_fire_state's
    # own stale stamp from the accumulation phase) instead of reflecting
    # THIS settlement's own actual stream, "midday".
    assert tracker["last_evaluated_stream"] == "midday"

    # A genuine RETRY of the SAME midday slot, same day, must be a pure
    # no-op: no new order, fire_attempts unchanged.
    r3b, s3b, _ = _run_day(tmp_path, config, state, TEST_DATES[2], 98.5, "day3-midday-retry", stream="midday")
    assert s3b["buy_candidates"] == []
    after_retry = json.loads(tracker_path.read_text())
    assert after_retry["fire_attempts"] == 1  # unchanged -- not treated as a new continuation
    assert after_retry["accumulated_usd"] == pytest.approx(2.0, abs=0.01)


def test_kill_switch_capped_rollback_stamps_the_settlement_as_a_real_evaluation(tmp_path, monkeypatch):
    """Regression test for "Stamp rollback evaluations as real".

    Fresh evidence beyond the stream-stamp fix above: the kill-switch-capped
    rollback branch is built from im_pre_fire_state (the state read BEFORE
    this run's own claim), which can ALSO carry a stale
    last_evaluated_is_real=False -- left over from an earlier-in-the-day
    age-only bump on a different stream (see
    test_prior_same_day_allocation_defers_a_ready_to_fire_recovery, which
    stamps exactly that shape via a real run; reproduced here by writing the
    tracker directly, to isolate this fix from that unrelated guard's own
    same-day budget bookkeeping). Restamping last_evaluated_stream (the
    prior fix) without also restamping last_evaluated_is_real=True leaves
    the STATUS_FIRING same-stream retry guard's third condition
    (`tracker_state.get("last_evaluated_is_real", True)`) false, so a
    genuine same-stream retry of THIS settlement is misread as a fresh
    continuation -- incrementing fire_attempts (or submitting a second real
    order) for what is really the same logical slot.
    """
    monkeypatch.setenv("DCA_CRON_TRACKING_ROOT", str(tmp_path / "cron_tracking"))
    config = _base_config(
        inverse_martingale={"AAPL": PCT_ENROLLMENT},
        monthly_cash_allocation_usd=100.0,
        kill_switch_7d_deploy_fraction=0.40,
        deployed_last_7d_usd=50.0,  # $50 > 40% of $100 -> kill switch engaged
        dry_run=False,
    )
    state = StrategyState()

    _run_day(tmp_path, config, state, TEST_DATES[0], 99.0, "day1", stream="morning")  # +$1
    _run_day(tmp_path, config, state, TEST_DATES[1], 97.0, "day2", stream="morning")  # +$2 -> $3, low 3.0%

    # Simulate an age-only bump having already touched this cycle earlier
    # TODAY (day 3), on some other stream -- last_evaluated_trading_day is
    # stamped to today, last_evaluated_stream is left at its stale prior
    # value ("morning", carried from day 2's real evaluation), and
    # last_evaluated_is_real is stamped False, matching exactly what
    # _inverse_martingale_advance_age_for_missing_data / the guarded-fire
    # deferral path (near_close_dca.py) would have written.
    tracker_path = tmp_path / "cron_tracking" / "inverse_martingale_dca" / "AAPL" / "tracker.json"
    poisoned = json.loads(tracker_path.read_text())
    poisoned["last_evaluated_trading_day"] = TEST_DATES[2].isoformat()
    poisoned["last_evaluated_is_real"] = False
    tracker_path.write_text(json.dumps(poisoned), encoding="utf-8")

    # Day 3, MIDDAY slot: recovers -> the kill switch caps the fire to the
    # $1 unit base, leaving $2 as a rolled-back STATUS_FIRING continuation.
    # This is a genuine, real evaluation.
    r3b, s3b, _broker3b = _run_day(tmp_path, config, state, TEST_DATES[2], 98.5, "day3-midday", stream="midday")
    [candidate] = s3b["buy_candidates"]
    assert candidate["status"] == "submitted"

    settled = json.loads(tracker_path.read_text())
    assert settled["status"] == "firing"
    assert settled["accumulated_usd"] == pytest.approx(2.0, abs=0.01)
    assert settled["fire_attempts"] == 1
    assert settled["last_evaluated_stream"] == "midday"
    # The bug: before the fix, this stayed False (copied unchanged from
    # im_pre_fire_state's stale guarded-deferral stamp) instead of reflecting
    # that THIS settlement is itself a genuine, real evaluation.
    assert settled["last_evaluated_is_real"] is True

    # A genuine RETRY of the SAME midday slot, same day, must be a pure
    # no-op: no new order, fire_attempts unchanged. Before the fix, the
    # stale last_evaluated_is_real=False would have defeated the
    # STATUS_FIRING same-stream retry guard here, submitting a second order.
    r3c, s3c, _ = _run_day(tmp_path, config, state, TEST_DATES[2], 98.5, "day3-midday-retry", stream="midday")
    assert s3c["buy_candidates"] == []
    after_retry = json.loads(tracker_path.read_text())
    assert after_retry["fire_attempts"] == 1  # unchanged -- not treated as a new continuation
    assert after_retry["accumulated_usd"] == pytest.approx(2.0, abs=0.01)


def test_blocked_placement_preserves_the_balance_and_counts_the_attempt(tmp_path, monkeypatch):
    """A fire that never reaches placement (blocked by insufficient buying
    power) must not lose the accumulated balance -- the same amount is
    retried on a later run -- but it DOES count as a fire attempt (toward
    max_fire_attempts), since the ticker's cycle already committed to
    firing once recovery was detected (Codex review: "Count failed fire
    submissions toward the attempt limit")."""
    monkeypatch.setenv("DCA_CRON_TRACKING_ROOT", str(tmp_path / "cron_tracking"))
    config = _base_config(inverse_martingale={"AAPL": PCT_ENROLLMENT})
    state = StrategyState()

    _run_day(tmp_path, config, state, TEST_DATES[0], 99.0, "day1")  # +$1
    _run_day(tmp_path, config, state, TEST_DATES[1], 97.0, "day2")  # +$2 -> $3

    tracker_path = tmp_path / "cron_tracking" / "inverse_martingale_dca" / "AAPL" / "tracker.json"
    before = json.loads(tracker_path.read_text())
    assert before["accumulated_usd"] == pytest.approx(3.0, abs=0.01)
    assert before["status"] == "dipping"

    # Day 3: recovers -> would fire, but the broker has essentially no cash,
    # so the buying-power gate blocks placement before any order is placed.
    r3, s3, _ = _run_day(tmp_path, config, state, TEST_DATES[2], 98.5, "day3", broker_cash=0.0)
    # The blocked fire still shows up in the artifact's buy_candidates list,
    # correctly attributed -- reason/status/skip_reason all describe the
    # REAL attempted inverse-Martingale fire, not the ticker's original
    # (enrollment-suppressed, non-BUY) ladder decision (Codex review:
    # "Preserve the synthetic fire decision in run artifacts"). A blocked
    # attempt being invisible from the audit trail was the bug, not the
    # correct behavior.
    [candidate] = s3["buy_candidates"]
    assert candidate["ticker"] == "AAPL"
    assert candidate["reason"] == "inverse_martingale_fire"
    assert candidate["status"] == "blocked"
    assert candidate["planned_amount_usd"] == pytest.approx(3.0, abs=0.01)

    after = json.loads(tracker_path.read_text())
    assert after["accumulated_usd"] == pytest.approx(3.0, abs=0.01)  # nothing lost
    assert after["status"] == "firing"  # recovery already committed to firing
    assert after["fire_attempts"] == 1  # this blocked attempt counted


def test_cross_source_disagreement_defers_the_fire(tmp_path, monkeypatch):
    """A recovery reported by only one provider, while the other still shows
    the low (or a materially different price), must not fire -- the ladder's
    own finance/Robinhood disagreement safety bar applies here too."""
    monkeypatch.setenv("DCA_CRON_TRACKING_ROOT", str(tmp_path / "cron_tracking"))
    config = _base_config(inverse_martingale={"AAPL": PCT_ENROLLMENT})
    state = StrategyState()

    _run_day(tmp_path, config, state, TEST_DATES[0], 99.0, "day1")  # +$1
    _run_day(tmp_path, config, state, TEST_DATES[1], 97.0, "day2")  # +$2 -> $3

    tracker_path = tmp_path / "cron_tracking" / "inverse_martingale_dca" / "AAPL" / "tracker.json"
    before = json.loads(tracker_path.read_text())

    # Day 3: finance shows a full recovery to 98.5, but Robinhood still
    # reports something close to the original 90.0 reference-day price -- an
    # ~8.6% disagreement, well past the accumulator's 2% cross-source
    # tolerance -- so the fire must defer.
    r3, s3, _ = _run_day(tmp_path, config, state, TEST_DATES[2], 98.5, "day3", robinhood_price=90.0)
    assert s3["buy_candidates"] == []

    # The cycle itself is untouched (still $3 accumulated, same crossed
    # tiers) and retried fresh with agreeing sources later -- but
    # trading_days_active still advances for this session, and an audit
    # event is appended, so a ticker stuck disagreeing for a long stretch
    # still ages toward max_cycle_days abandonment instead of the age clock
    # silently freezing while data is unusable (Codex review: "Age cycles
    # across missing-data sessions").
    after = json.loads(tracker_path.read_text())
    assert after["accumulated_usd"] == before["accumulated_usd"]
    assert after["crossed_tier_thresholds"] == before["crossed_tier_thresholds"]
    assert after["status"] == before["status"]
    assert after["trading_days_active"] == before["trading_days_active"] + 1
    assert after["events"][-1]["reason"] == "data_unavailable_age_advanced"


def test_disabling_enrollment_mid_cycle_resets_the_active_tracker(tmp_path, monkeypatch):
    """Regression test for "Reset active cycles when enrollment is disabled".

    An operator flipping a ticker's enrollment off mid-cycle (removing its
    entry from config.inverse_martingale, or setting enabled: false) must
    abandon the active cycle outright -- otherwise the injection site skips
    the ticker entirely without touching its ledger, the frozen balance
    survives untouched, and a later re-enrollment under the SAME
    configuration wakes the old cycle back up: it can fire the balance
    accumulated before AND during the disabled interval on top of whatever
    the normal ladder (no longer suppressed while disabled) already bought
    for the same descent.
    """
    monkeypatch.setenv("DCA_CRON_TRACKING_ROOT", str(tmp_path / "cron_tracking"))
    enrolled_config = _base_config(inverse_martingale={"AAPL": PCT_ENROLLMENT})
    state = StrategyState()

    _run_day(tmp_path, enrolled_config, state, TEST_DATES[0], 95.0, "day1")  # -5%, crosses 1/2/4 -> $7
    tracker_path = tmp_path / "cron_tracking" / "inverse_martingale_dca" / "AAPL" / "tracker.json"
    before = json.loads(tracker_path.read_text())
    assert before["status"] == "dipping"
    assert before["accumulated_usd"] == 7.0

    # Operator disables JUST AAPL's enrollment: no "AAPL" entry in
    # config.inverse_martingale (the loader already drops an explicit
    # enabled: false the same way -- see _inverse_martingale_config's own
    # docstring). A placeholder "MSFT" entry keeps config.inverse_martingale
    # itself non-empty here -- the cleanup pass itself runs regardless (see
    # test_disabling_the_entire_mapping_still_resets_active_trackers below
    # for that case); this test just isolates the per-ticker path.
    disabled_config = _base_config(inverse_martingale={"MSFT": PCT_ENROLLMENT})
    _run_day(tmp_path, disabled_config, state, TEST_DATES[1], 96.0, "day2")

    after = json.loads(tracker_path.read_text())
    assert after["status"] == "none"
    assert after["last_evaluated_trading_day"] == TEST_DATES[1].isoformat()
    assert "accumulated_usd" not in after  # resolved_marker() carries no leftover cycle fields
    assert after["events"][-1]["reason"] == "cycle_reset_enrollment_disabled"

    # Re-enrolling under the SAME configuration on a later day starts a
    # genuinely fresh cycle -- the old $7 balance is gone, not resumed.
    _run_day(tmp_path, enrolled_config, state, TEST_DATES[2], 99.0, "day3")  # -1% -> crosses only tier 1.0
    resumed = json.loads(tracker_path.read_text())
    assert resumed["status"] == "dipping"
    assert resumed["accumulated_usd"] == 1.0


def test_removing_a_ticker_from_the_tradable_universe_resets_its_active_cycle(tmp_path, monkeypatch):
    """Regression test for "Reset cycles for tickers outside the tradable
    universe". A ticker can stay enrolled in config.inverse_martingale
    (enabled: true) while being removed from config.tickers or added to
    config.excluded_tickers -- e.g. an operator drops it from the live
    universe without remembering to also touch its separate accumulator
    enrollment. The tracking/firing loop only iterates tradable_tickers, so
    such a ticker can never be evaluated again; without this reset its
    balance and age would freeze indefinitely, bypassing max_cycle_days,
    and could resume as a stale obligation from a completely different
    market regime if the ticker is later restored to the universe.
    """
    monkeypatch.setenv("DCA_CRON_TRACKING_ROOT", str(tmp_path / "cron_tracking"))
    enrolled_config = _base_config(tickers=("AAPL",), inverse_martingale={"AAPL": PCT_ENROLLMENT})
    state = StrategyState()

    _run_day(tmp_path, enrolled_config, state, TEST_DATES[0], 95.0, "day1")  # -5%, crosses 1/2/4 -> $7
    tracker_path = tmp_path / "cron_tracking" / "inverse_martingale_dca" / "AAPL" / "tracker.json"
    before = json.loads(tracker_path.read_text())
    assert before["status"] == "dipping"
    assert before["accumulated_usd"] == 7.0

    # AAPL is removed from the tradable universe (config.tickers), but its
    # config.inverse_martingale entry is left untouched -- still "enrolled"
    # by that mapping alone.
    removed_from_universe_config = _base_config(
        tickers=("MSFT",), inverse_martingale={"AAPL": PCT_ENROLLMENT}
    )
    _run_day(tmp_path, removed_from_universe_config, state, TEST_DATES[1], 96.0, "day2")

    after = json.loads(tracker_path.read_text())
    assert after["status"] == "none"
    assert "accumulated_usd" not in after  # resolved_marker() carries no leftover cycle fields

    # Restoring AAPL to the tradable universe on a later day starts a
    # genuinely fresh cycle -- the old $7 balance is gone, not resumed.
    _run_day(tmp_path, enrolled_config, state, TEST_DATES[2], 99.0, "day3")  # -1% -> crosses only tier 1.0
    resumed = json.loads(tracker_path.read_text())
    assert resumed["status"] == "dipping"
    assert resumed["accumulated_usd"] == 1.0


def test_disabling_the_entire_mapping_still_resets_active_trackers(tmp_path, monkeypatch):
    """Regression test for "Run enrollment cleanup when the mapping becomes
    empty". Disabling the ONLY enrolled ticker (config.inverse_martingale
    becomes completely EMPTY, not just missing one entry among others) must
    still reset its active cycle -- the tracking/firing block below is
    gated on `config.inverse_martingale` being non-empty and would
    otherwise skip past the ticker entirely, leaving its balance frozen
    with no path back to reset until the whole feature is re-enrolled
    somewhere. The cleanup pass runs regardless of that guard, scanning the
    on-disk ledger tree directly instead of `tradable_tickers`.
    """
    monkeypatch.setenv("DCA_CRON_TRACKING_ROOT", str(tmp_path / "cron_tracking"))
    enrolled_config = _base_config(inverse_martingale={"AAPL": PCT_ENROLLMENT})
    state = StrategyState()

    _run_day(tmp_path, enrolled_config, state, TEST_DATES[0], 95.0, "day1")  # -5%, crosses 1/2/4 -> $7
    tracker_path = tmp_path / "cron_tracking" / "inverse_martingale_dca" / "AAPL" / "tracker.json"
    before = json.loads(tracker_path.read_text())
    assert before["status"] == "dipping"
    assert before["accumulated_usd"] == 7.0

    # Operator disables the WHOLE feature: config.inverse_martingale is
    # completely empty, not just missing AAPL's entry among others.
    disabled_config = _base_config(inverse_martingale={})
    _run_day(tmp_path, disabled_config, state, TEST_DATES[1], 96.0, "day2")

    after = json.loads(tracker_path.read_text())
    assert after["status"] == "none"
    assert after["last_evaluated_trading_day"] == TEST_DATES[1].isoformat()
    assert "accumulated_usd" not in after
    assert after["events"][-1]["reason"] == "cycle_reset_enrollment_disabled"


def _pending_tracker_state() -> dict:
    """A minimal, valid STATUS_FIRING tracker state mid-settlement -- the
    exact shape a partial-fire claim leaves on disk between its own claim
    and its own deferred settlement (see evaluate_inverse_martingale_step's
    settlement_pending guard). Carries cycle_reference_close/
    lowest_drop_seen -- a real "firing" tracker always does, inherited
    unchanged from the "dipping" phase that preceded it and never dropped
    (see _validate_tracker_payload's "Require the fire-floor metadata on
    firing trackers" completeness check) -- so this fixture stays valid
    against that check rather than incidentally exercising it.
    """
    return {
        "status": STATUS_FIRING,
        "accumulated_usd": 5.0,
        "cycle_start_trading_day": TEST_DATES[0].isoformat(),
        "last_evaluated_trading_day": TEST_DATES[0].isoformat(),
        "cycle_reference_close": 100.0,
        "lowest_drop_seen": 5.0,
        "fire_attempts": 1,
        "trading_days_active": 1,
        "settlement_pending": True,
        # The recoverable-lease metadata the claim site writes alongside the
        # flag. A pending lease is only valid when it carries enough to
        # reconcile the claim against broker order history -- see
        # _validate_tracker_payload's "Validate the newly persisted settlement
        # fields on read" block.
        # The real deterministic format claim creation writes -- no explicit
        # settlement_stream here, so recovery falls back to the historical
        # "afternoon" default (no stream token of its own).
        "settlement_ref_id": f"graded-dca-imart-{TEST_DATES[0].isoformat()}-AAPL",
        "settlement_trading_day": TEST_DATES[0].isoformat(),
        "settlement_claimed_usd": 5.0,
        "settlement_target_state": {
            "status": STATUS_NONE,
            "last_evaluated_trading_day": TEST_DATES[0].isoformat(),
            "last_evaluated_stream": "afternoon",
        },
        "settlement_live": True,
    }


def test_missing_data_age_advance_leaves_a_pending_settlement_lease_untouched(tmp_path):
    """Regression test for "Preserve pending settlement leases during
    age-only updates". A concurrent, overlapping process lacking usable
    source data for a ticker mid-settlement must not touch that ticker's
    ledger at all -- copying the pending state forward (even just to bump
    trading_days_active) writes a NEW on-disk state that still carries
    settlement_pending, but no longer matches the EXACT state the owning
    process's own deferred settlement expects: that settlement's
    compare-and-swap would then fail, silently discarding whatever it
    placed if it succeeded, or leaving settlement_pending stuck forever
    (nothing else would ever clear it) if it didn't.
    """
    ledger = InverseMartingaleLedger(root=tmp_path)
    pending_state = _pending_tracker_state()
    ledger.record_step(
        "AAPL", pending_state,
        InverseMartingaleStep(
            status_before=STATUS_FIRING, status_after=STATUS_FIRING,
            accumulated_usd_before=10.0, accumulated_usd_after=5.0,
            newly_crossed_tiers=(), fired=True, fire_amount_usd=5.0,
            reason="fire_continuation",
        ),
    )
    before = ledger.read("AAPL")
    assert before["settlement_pending"] is True

    _inverse_martingale_advance_age_for_missing_data(ledger, "AAPL", TEST_DATES[1])

    after = ledger.read("AAPL")
    assert after == before  # completely untouched -- not even the age fields


def test_disabled_enrollment_reset_leaves_a_pending_settlement_lease_untouched(tmp_path):
    """Same guarantee as the age-advance case above, but for the
    enrollment-disabled reset: overwriting a pending cycle here (even with
    a clean resolved_marker) would still move the on-disk state out from
    under the owning process's own settlement compare-and-swap."""
    ledger = InverseMartingaleLedger(root=tmp_path)
    pending_state = _pending_tracker_state()
    ledger.record_step(
        "AAPL", pending_state,
        InverseMartingaleStep(
            status_before=STATUS_FIRING, status_after=STATUS_FIRING,
            accumulated_usd_before=10.0, accumulated_usd_after=5.0,
            newly_crossed_tiers=(), fired=True, fire_amount_usd=5.0,
            reason="fire_continuation",
        ),
    )
    before = ledger.read("AAPL")
    assert before["settlement_pending"] is True

    _inverse_martingale_reset_if_disabled_mid_cycle(ledger, "AAPL", TEST_DATES[1], "afternoon")

    after = ledger.read("AAPL")
    assert after == before  # completely untouched, not reset


def test_morning_slot_ticker_gating_does_not_reset_the_cycle(tmp_path, monkeypatch):
    """Regression test for "Preserve cycles excluded only from the current
    slot". run_dca.run_slot("1030ET") temporarily narrows config.tickers to
    the 9:45 preflight-admitted subset for the "morning" stream only (see
    run_dca.py's own `slot_config = replace(config, tickers=slot_tickers,
    ...)`) -- "midday"/"afternoon" always see the full configured universe.
    A ticker merely gated out of THAT ONE morning slot -- simulated here by
    an empty config.tickers, standing in for run_slot's own preflight-
    narrowed subset -- is NOT genuinely removed from config/tickers.json,
    and must not have its accumulator cycle reset: it remains globally
    enrolled and simply returns in the midday/afternoon slots the same day.
    """
    monkeypatch.setenv("DCA_CRON_TRACKING_ROOT", str(tmp_path / "cron_tracking"))
    enrollment = {"AAPL": PCT_ENROLLMENT}
    state = StrategyState()

    # Day 1: AAPL dips, crosses tier 1 -> accumulates $1.
    config_full = _base_config(tickers=("AAPL",), inverse_martingale=enrollment)
    _run_day(tmp_path, config_full, state, TEST_DATES[0], 99.0, "day1", stream="afternoon")

    tracker_path = tmp_path / "cron_tracking" / "inverse_martingale_dca" / "AAPL" / "tracker.json"
    before = json.loads(tracker_path.read_text())
    assert before["status"] == "dipping"
    assert before["accumulated_usd"] == pytest.approx(1.0)

    # Day 2, MORNING slot: AAPL is gated out of THIS slot specifically --
    # config.tickers is empty, standing in for run_slot("1030ET")'s own
    # preflight-narrowed subset -- but AAPL stays enrolled in
    # inverse_martingale throughout, exactly as it would if the real
    # preflight gate merely excluded it from today's morning scan.
    config_morning_gated = _base_config(tickers=(), inverse_martingale=enrollment)
    _run_day(
        tmp_path, config_morning_gated, state, TEST_DATES[1], 99.0, "day2-morning", stream="morning",
    )

    after_morning = json.loads(tracker_path.read_text())
    assert after_morning["status"] == "dipping"  # NOT reset to "none"
    assert after_morning["accumulated_usd"] == pytest.approx(1.0)

    # Day 2, MIDDAY slot: the full universe is restored -- AAPL's cycle
    # continues normally (a new tier crosses at -3%), proving the balance
    # genuinely survived the morning gate-out rather than being silently
    # reset and re-started from zero.
    _run_day(tmp_path, config_full, state, TEST_DATES[1], 97.0, "day2-midday", stream="midday")
    after_midday = json.loads(tracker_path.read_text())
    assert after_midday["status"] == "dipping"
    assert after_midday["accumulated_usd"] == pytest.approx(3.0)  # existing $1 + newly-crossed 2% tier ($2)


def test_same_day_retry_after_full_fire_does_not_double_credit(tmp_path, monkeypatch):
    """A retry of the SAME scheduled slot on the SAME trading day, after a
    cycle already fully fired, must not treat the still-depressed price as a
    brand-new dip and silently accumulate a duplicate balance (Codex review:
    "Require a fresh dip before rearming a fired cycle")."""
    monkeypatch.setenv("DCA_CRON_TRACKING_ROOT", str(tmp_path / "cron_tracking"))
    config = _base_config(inverse_martingale={"AAPL": PCT_ENROLLMENT})
    state = StrategyState()

    _run_day(tmp_path, config, state, TEST_DATES[0], 95.0, "day1")  # -5%, crosses 1/2/4 -> $7
    r2, s2, _ = _run_day(tmp_path, config, state, TEST_DATES[1], 96.5, "day2")  # recovers -> fires $7
    [candidate] = s2["buy_candidates"]
    assert "imart" in (candidate.get("order_id") or "")

    tracker_path = tmp_path / "cron_tracking" / "inverse_martingale_dca" / "AAPL" / "tracker.json"
    resolved = json.loads(tracker_path.read_text())
    assert resolved["status"] == "none"

    # Retry the SAME slot on the SAME trading day (TEST_DATES[1]), at a price
    # that would look like a fresh 1% dip if the tracker were naively reset.
    r2b, s2b, _ = _run_day(tmp_path, config, state, TEST_DATES[1], 99.0, "day2-retry")
    assert s2b["buy_candidates"] == []  # no new fire, no re-accumulation
    after_retry = json.loads(tracker_path.read_text())
    # The audit trail (events) legitimately grows with a no-op entry; every
    # actual state field must stay unchanged by the retry.
    state_fields = {k: v for k, v in after_retry.items() if k != "events"}
    resolved_fields = {k: v for k, v in resolved.items() if k != "events"}
    assert state_fields == resolved_fields

    # A later day does not by itself make the still-open descent fresh --
    # prev_close (99) is genuinely below the reference on this NEW day (not
    # merely a repeated same-day observation), so the durable latch is
    # legitimately still tested and still armed.
    r3, s3, _ = _run_day(tmp_path, config, state, TEST_DATES[2], 99.0, "day3", prev_close=99.0)
    assert s3["buy_candidates"] == []  # accumulates only, no fire yet
    later = json.loads(tracker_path.read_text())
    assert later["status"] == "none"
    assert "accumulated_usd" not in later
    assert later["rearm_reference_close"] == pytest.approx(100.0)


def test_later_slot_the_same_day_still_blocked_by_the_fired_cycles_own_floor(tmp_path, monkeypatch):
    """Regression test for "Require a fresh descent before rearming in a
    later slot". Letting a DIFFERENT stream past the same-day retry guard
    (see test_later_slot_the_same_day_breaks_below_the_fired_floor below)
    reopened a narrower hole: a morning cycle can bottom at -5%, recover to
    -3.5% and fire, and a midday run at a price STILL below the original
    prior_close (but nowhere near as deep as -5%) would otherwise look like
    a brand-new dip to a fresh (status "none") evaluation -- re-crediting,
    and potentially re-firing, for the SAME underlying descent the morning
    fire already paid for. The fired cycle's own floor (its lowest_drop_seen
    at fire time) must block this until price genuinely breaks below it.
    """
    monkeypatch.setenv("DCA_CRON_TRACKING_ROOT", str(tmp_path / "cron_tracking"))
    config = _base_config(inverse_martingale={"AAPL": PCT_ENROLLMENT})
    state = StrategyState()

    # Day 1, morning slot: -5% dip, crosses tiers 1/2/4 -> accum $7.
    _run_day(tmp_path, config, state, TEST_DATES[0], 95.0, "day1-morning", stream="morning")

    # Day 2, morning slot: recovers -> fires the full $7, resolves the cycle.
    # The fired floor is the cycle's own low, -5% (drop_metric 5.0).
    r2, s2, _ = _run_day(tmp_path, config, state, TEST_DATES[1], 96.5, "day2-morning", stream="morning")
    [candidate] = s2["buy_candidates"]
    assert "imart" in (candidate.get("order_id") or "")

    tracker_path = tmp_path / "cron_tracking" / "inverse_martingale_dca" / "AAPL" / "tracker.json"
    resolved = json.loads(tracker_path.read_text())
    assert resolved["status"] == "none"
    assert resolved["last_evaluated_stream"] == "morning"
    assert resolved["fired_floor_drop_metric"] == pytest.approx(5.0)

    # SAME day, midday slot: a SHALLOW -1% dip -- still below the original
    # prior_close, but nowhere near the -5% floor the morning fire already
    # covered. Must NOT be treated as a fresh dip: no new cycle, no credit.
    r2b, s2b, _ = _run_day(tmp_path, config, state, TEST_DATES[1], 99.0, "day2-midday-shallow", stream="midday")
    assert s2b["buy_candidates"] == []
    after_shallow = json.loads(tracker_path.read_text())
    assert after_shallow["status"] == "none"
    assert "accumulated_usd" not in after_shallow

    # A genuine RETRY of that same midday slot, same day, is unaffected --
    # still blocked by the SAME floor, not a new/different mechanism.
    r2c, s2c, _ = _run_day(tmp_path, config, state, TEST_DATES[1], 99.0, "day2-midday-retry", stream="midday")
    assert s2c["buy_candidates"] == []
    after_retry = json.loads(tracker_path.read_text())
    state_fields = {k: v for k, v in after_retry.items() if k != "events"}
    prior_fields = {k: v for k, v in after_shallow.items() if k != "events"}
    assert state_fields == prior_fields

    # The durable rearm latch, unlike the same-day floor, remains effective
    # on later days until price exits the original descent -- prev_close
    # (99) is genuinely below the reference on this NEW day, not a repeated
    # same-day observation.
    r3, s3, _ = _run_day(tmp_path, config, state, TEST_DATES[2], 99.0, "day3", prev_close=99.0)
    assert s3["buy_candidates"] == []
    later = json.loads(tracker_path.read_text())
    assert later["status"] == "none"
    assert "accumulated_usd" not in later
    assert later["rearm_reference_close"] == pytest.approx(100.0)


def test_later_slot_deeper_price_remains_latched_until_dip_exit(tmp_path, monkeypatch):
    """A deeper print without an intervening recovery is still the same dip."""
    monkeypatch.setenv("DCA_CRON_TRACKING_ROOT", str(tmp_path / "cron_tracking"))
    config = _base_config(inverse_martingale={"AAPL": PCT_ENROLLMENT})
    state = StrategyState()

    # Day 1, morning slot: -5% dip, crosses tiers 1/2/4 -> accum $7.
    _run_day(tmp_path, config, state, TEST_DATES[0], 95.0, "day1-morning", stream="morning")
    # Day 2, morning slot: recovers -> fires the full $7. Floor = -5% (5.0).
    r2, s2, _ = _run_day(tmp_path, config, state, TEST_DATES[1], 96.5, "day2-morning", stream="morning")
    [candidate] = s2["buy_candidates"]
    assert "imart" in (candidate.get("order_id") or "")

    tracker_path = tmp_path / "cron_tracking" / "inverse_martingale_dca" / "AAPL" / "tracker.json"

    # SAME day, midday slot: even a deeper -5.5% print remains part of the
    # original descent until price first exits it.
    r2b, s2b, _ = _run_day(tmp_path, config, state, TEST_DATES[1], 94.5, "day2-midday-deep", stream="midday")
    assert s2b["buy_candidates"] == []
    after_midday = json.loads(tracker_path.read_text())
    assert after_midday["status"] == "none"
    assert "accumulated_usd" not in after_midday


def test_split_during_an_active_cycle_is_found_after_resolution(tmp_path, monkeypatch):
    """A verified split that lands WHILE a cycle is still active -- before
    the cycle resolves -- must still be found: the injection site seeds
    ``rearm_last_seen_day`` from the cycle's own start day
    (``cycle_start_trading_day``), not the day the cycle happened to
    resolve, so ``archived_split_factor``'s search window covers a split
    dated anywhere after the cycle started, not only after resolution."""
    monkeypatch.setenv("DCA_CRON_TRACKING_ROOT", str(tmp_path / "cron_tracking"))
    monkeypatch.setenv("DCA_TICKER_HISTORY_DIR", str(tmp_path / "ticker_data"))
    (tmp_path / "ticker_data").mkdir()
    # A 2-for-1 split dated while a capped fire continuation is still active,
    # strictly between cycle start and final resolution. Both the Stock Splits
    # and Dividends columns must be present -- archived_split_factor() treats
    # an archive missing either one as untrustworthy.
    (tmp_path / "ticker_data" / "AAPL.csv").write_text(
        "Date,Close,Adj Close,Stock Splits,Dividends\n"
        "2026-06-30,50,50,2.0,0.0\n",
        encoding="utf-8",
    )
    config = _base_config(inverse_martingale={"AAPL": PCT_ENROLLMENT})
    state = StrategyState()

    # Day 1 (2026-06-29): a -9% dip crosses all four tiers and banks $15.
    _run_day(tmp_path, config, state, TEST_DATES[0], 91.0, "day1")
    # Day 2 (2026-06-30): recovery starts firing, but the $10 per-order cap
    # leaves a $5 continuation active across the split day.
    r2, s2, _ = _run_day(tmp_path, config, state, TEST_DATES[1], 93.0, "day2")
    [candidate] = s2["buy_candidates"]
    assert "imart" in (candidate.get("order_id") or "")

    tracker_path = tmp_path / "cron_tracking" / "inverse_martingale_dca" / "AAPL" / "tracker.json"
    continuing = json.loads(tracker_path.read_text())
    assert continuing["status"] == "firing"

    # Day 3: the continuation settles and only now resolves the cycle. The
    # durable latch must retain day 1 as the boundary of its reference scale,
    # rather than recording this resolution day as the archive cursor.
    r3, s3, _ = _run_day(tmp_path, config, state, TEST_DATES[2], 49.0, "day3", prev_close=49.0)
    [candidate] = s3["buy_candidates"]
    assert "imart" in (candidate.get("order_id") or "")
    resolved = json.loads(tracker_path.read_text())
    assert resolved["rearm_reference_close"] == pytest.approx(100.0)
    assert resolved["rearm_last_seen_day"] == TEST_DATES[0].isoformat()

    # Day 4 (2026-07-02): the first evaluation after resolution. A price of
    # 49 is far below the OLD (pre-split) reference of 100, but the archive
    # already carries the verified split from day 2 -- the seeded search
    # window (since the cycle's start day) finds it, rebasing the durable
    # latch to 50 instead of leaving it latched against the stale, unsplit
    # reference indefinitely.
    r4, s4, _ = _run_day(tmp_path, config, state, TEST_DATES[3], 49.0, "day4", prev_close=49.0)
    assert s4["buy_candidates"] == []
    after_split = json.loads(tracker_path.read_text())
    assert after_split["rearm_reference_close"] == pytest.approx(50.0)
    # The cursor advances to the newest archive row actually EXAMINED (day
    # 2, the split's own row -- the only row present), not trading_day
    # itself, per archived_split_factor()'s own documented cursor contract.
    assert after_split["rearm_last_seen_day"] == TEST_DATES[1].isoformat()


class _AmbiguousFireBroker(DryRunBroker):
    """DryRunBroker double whose place_market_buy raises
    OrderPlacementAmbiguousError for a chosen ticker -- simulating a
    placement connector call that itself failed (e.g. a response timeout)
    before any broker response was observed.
    """

    def __init__(self, *args, raise_for_ticker, **kwargs):
        super().__init__(*args, **kwargs)
        self._raise_for_ticker = raise_for_ticker

    def place_market_buy(self, ticker, notional_usd, ref_id):
        if ticker == self._raise_for_ticker:
            from near_close_dca import OrderPlacementAmbiguousError

            raise OrderPlacementAmbiguousError(f"placement connector call for {ticker} timed out")
        return super().place_market_buy(ticker, notional_usd, ref_id)


def test_ambiguous_fire_keeps_the_claim_leased_and_resolved(tmp_path, monkeypatch):
    """Codex review, PR #1300 discussion_r3920826596 ("Keep inverse-
    Martingale claims leased after ambiguity"): mirrors
    test_dry_run_fire_does_not_settle_the_ledger above, but the fire's
    placement was AMBIGUOUS (OrderPlacementAmbiguousError) rather than a
    dry run. Unlike a dry run -- which never reaches a real broker at all
    and must be replayed -- an ambiguous placement may already be live at
    the broker, so it must be treated as consumed (settled, not rolled
    back) and its settlement_pending lease released, not left frozen or
    silently re-fired with the same accumulated amount by a later
    evaluation.
    """
    monkeypatch.setenv("DCA_CRON_TRACKING_ROOT", str(tmp_path / "cron_tracking"))
    config = _base_config(inverse_martingale={"AAPL": PCT_ENROLLMENT})
    state = StrategyState()

    _run_day(tmp_path, config, state, TEST_DATES[0], 95.0, "day1")  # -5%, crosses 1/2/4 -> $7
    broker = _AmbiguousFireBroker(cash=500.0, reference_prices={"AAPL": 96.5}, raise_for_ticker="AAPL")
    r2, s2, _ = _run_day(tmp_path, config, state, TEST_DATES[1], 96.5, "day2", broker=broker)
    [candidate] = s2["buy_candidates"]
    assert candidate["status"] == "potentially_submitted"

    tracker_path = tmp_path / "cron_tracking" / "inverse_martingale_dca" / "AAPL" / "tracker.json"
    tracker = json.loads(tracker_path.read_text())
    # Resolved and released, not left frozen or rolled back to "firing".
    assert tracker["status"] == "none"
    assert "settlement_pending" not in tracker

    # A later evaluation must NOT re-fire the same $7 -- the claim was
    # conservatively treated as consumed, not restored.
    r3, s3, _ = _run_day(tmp_path, config, state, TEST_DATES[2], 96.5, "day3")
    assert s3["buy_candidates"] == []


def test_dry_run_fire_does_not_settle_the_ledger(tmp_path, monkeypatch):
    """A dry-run invocation never places a real broker order, so it must
    not be treated as a genuine settlement -- the claimed balance stays
    intact (status "firing") for a later, real run to actually place
    (Codex review: "Preserve accumulator balances during dry runs")."""
    monkeypatch.setenv("DCA_CRON_TRACKING_ROOT", str(tmp_path / "cron_tracking"))
    config = _base_config(inverse_martingale={"AAPL": PCT_ENROLLMENT}, dry_run=True)
    state = StrategyState()

    _run_day(tmp_path, config, state, TEST_DATES[0], 95.0, "day1")  # -5%, crosses 1/2/4 -> $7
    r2, s2, _ = _run_day(tmp_path, config, state, TEST_DATES[1], 96.5, "day2")  # recovers -> claims $7
    [candidate] = s2["buy_candidates"]
    assert candidate["status"] == "dry_run"
    assert "imart" in (candidate.get("order_id") or "")

    tracker_path = tmp_path / "cron_tracking" / "inverse_martingale_dca" / "AAPL" / "tracker.json"
    tracker = json.loads(tracker_path.read_text())
    assert tracker["status"] == "firing"  # claimed, but never settled
    assert tracker["accumulated_usd"] == pytest.approx(7.0, abs=0.01)  # nothing debited

    # The dry-run claim must also release its settlement lease. Prove the
    # balance is not merely present but actually available to the next live
    # invocation: the same recovery submits the full deferred amount and
    # resolves the tracker.
    live_config = _base_config(inverse_martingale={"AAPL": PCT_ENROLLMENT}, dry_run=False)
    _, live_state, _ = _run_day(
        tmp_path,
        live_config,
        state,
        TEST_DATES[2],
        96.5,
        "day3-live",
    )
    [live_candidate] = live_state["buy_candidates"]
    assert live_candidate["status"] == "submitted"
    assert live_candidate["planned_amount_usd"] == pytest.approx(7.0, abs=0.01)
    assert "7.00" in live_candidate["order_id"]
    resolved = json.loads(tracker_path.read_text())
    assert resolved["status"] == "none"
    assert "settlement_pending" not in resolved


def test_dry_run_fire_attempts_do_not_consume_the_retry_budget(tmp_path, monkeypatch):
    """Regression test for "Do not count dry runs as fire attempts".

    A dry-run outcome never reaches a real broker -- im_placed_amount is
    unconditionally 0, identical to what a genuinely blocked live attempt
    reports -- but it must not consume the SAME max_fire_attempts retry
    budget a live blocked attempt does. With max_fire_attempts=2 and a
    per-order cap forcing a multi-run partial fire, THREE consecutive
    dry-run "fire" evaluations must not abandon the accumulated balance,
    or advance an existing pre-fire attempt count to the limit.
    """
    monkeypatch.setenv("DCA_CRON_TRACKING_ROOT", str(tmp_path / "cron_tracking"))
    extended_dates = (date(2026, 6, 29), date(2026, 6, 30), date(2026, 7, 1), date(2026, 7, 2), date(2026, 7, 6))
    config = _base_config(
        inverse_martingale={"AAPL": PCT_ENROLLMENT},
        max_per_order_usd=5.0,
        inverse_martingale_max_fire_attempts=2,
        dry_run=True,
        approved_test_dates=frozenset(extended_dates),
    )
    state = StrategyState()

    _run_day(tmp_path, config, state, extended_dates[0], 99.0, "day1")  # -1% -> +$1
    _run_day(tmp_path, config, state, extended_dates[1], 91.5, "day2")  # -8.5% -> +$14 -> $15 total

    tracker_path = tmp_path / "cron_tracking" / "inverse_martingale_dca" / "AAPL" / "tracker.json"
    pre_fire_tracker = json.loads(tracker_path.read_text())
    assert pre_fire_tracker["accumulated_usd"] == pytest.approx(15.0, abs=0.01)
    # Model a recovered cycle that has already spent one genuine production
    # attempt. Dry-run rehearsals must preserve this count rather than consume
    # its one remaining live retry.
    pre_fire_tracker["fire_attempts"] = 1
    tracker_path.write_text(json.dumps(pre_fire_tracker))

    # Three consecutive dry-run "fires" -- incrementing the existing count
    # would cause the next evaluation to abandon the cycle at the limit.
    _run_day(tmp_path, config, state, extended_dates[2], 97.0, "day3")  # recovers -> dry-run "fires" $5 (capped)
    _run_day(tmp_path, config, state, extended_dates[3], 97.0, "day4")  # continuation, dry-run "fires" $5 again
    r5, s5, _ = _run_day(tmp_path, config, state, extended_dates[4], 97.0, "day5")

    tracker = json.loads(tracker_path.read_text())
    assert tracker["status"] == "firing"  # NOT abandoned back to "none"
    assert tracker["accumulated_usd"] == pytest.approx(15.0, abs=0.01)  # nothing lost across 3 dry runs
    assert tracker["fire_attempts"] == 1  # pre-fire production attempt preserved

    [candidate] = s5["buy_candidates"]
    assert candidate["status"] == "dry_run"


def test_prior_same_day_allocation_defers_fire_but_still_tracks_the_dip(tmp_path, monkeypatch):
    """A ticker with a prior same-day allocation (bought earlier today by a
    DIFFERENT stream, e.g. MorningDCAExecutor, which enrollment does not
    suppress) must still have its dip TRACKED this run -- only firing is
    deferred. Previously the whole evaluation was skipped for a guarded
    ticker, so any dip deepening later the same guarded day was silently,
    permanently lost once the day rolled over to a new reference price
    (Codex review: "Continue tracking tiers despite a prior same-day
    buy")."""
    monkeypatch.setenv("DCA_CRON_TRACKING_ROOT", str(tmp_path / "cron_tracking"))
    config = _base_config(inverse_martingale={"AAPL": PCT_ENROLLMENT})
    state = StrategyState()
    prior = PriorSameDayAllocation(
        "AAPL", amount_usd=5.0, reference_price=100.0, run_id="morning",
        source="scheduled", selected_base_amount_usd=2.0,
    )

    # Day 1: guarded by the prior allocation, but the dip must still be
    # tracked (1% -> tier 1, $1 accumulated), not skipped entirely.
    r1, s1, _ = _run_day(
        tmp_path, config, state, TEST_DATES[0], 99.0, "day1",
        prior_same_day_allocations={"AAPL": prior},
    )
    assert s1["buy_candidates"] == []
    tracker_path = tmp_path / "cron_tracking" / "inverse_martingale_dca" / "AAPL" / "tracker.json"
    tracker = json.loads(tracker_path.read_text())
    assert tracker["status"] == "dipping"
    assert tracker["accumulated_usd"] == pytest.approx(1.0, abs=0.01)

    # Day 2: still guarded, dips further (crosses tier 2, +$2 -> $3) --
    # again tracked, not lost.
    r2, s2, _ = _run_day(
        tmp_path, config, state, TEST_DATES[1], 97.0, "day2",
        prior_same_day_allocations={"AAPL": prior},
    )
    assert s2["buy_candidates"] == []
    tracker = json.loads(tracker_path.read_text())
    assert tracker["accumulated_usd"] == pytest.approx(3.0, abs=0.01)


def test_discarded_sub_threshold_credit_still_tracks_the_dip(tmp_path, monkeypatch):
    """A final-pass credit below $0.50 is discarded without affecting tracking.

    The inverse-Martingale evaluation remains independent of the cumulative
    minimum-order ledger and must still record the ticker's dip.
    """
    monkeypatch.setenv("DCA_CRON_TRACKING_ROOT", str(tmp_path / "cron_tracking"))
    config = _base_config(inverse_martingale={"AAPL": PCT_ENROLLMENT})
    state = StrategyState()

    # Seed a small credit that the final-pass policy must forget rather than
    # turning into an order. guard_active requires a non-None allocations map.
    credit_ledger = CumulativeCreditLedger(root=tmp_path / "cron_tracking" / "cumulative_dca_credits")
    credit_ledger.record("AAPL", TEST_DATES[0].isoformat(), 0.20)

    r1, s1, _ = _run_day(
        tmp_path, config, state, TEST_DATES[0], 99.0, "day1",
        prior_same_day_allocations={},
    )
    assert s1["buy_candidates"] == []
    assert credit_ledger.minimum_order_pending_tickers(TEST_DATES[0].isoformat()) == {}

    tracker_path = tmp_path / "cron_tracking" / "inverse_martingale_dca" / "AAPL" / "tracker.json"
    tracker = json.loads(tracker_path.read_text())
    assert tracker["status"] == "dipping"
    assert tracker["accumulated_usd"] == pytest.approx(1.0, abs=0.01)


def test_prior_same_day_allocation_defers_a_ready_to_fire_recovery(tmp_path, monkeypatch):
    """When recovery is detected on a guarded day, the fire must be
    deferred (not claimed/lost) until an unguarded run can carry it. The
    cycle's balance/status must stay exactly as they were pre-evaluation
    (unclaimed), but its AGE must still advance -- otherwise a ticker that
    keeps recovering into a guarded slot every session would never age at
    all, and max_cycle_days could never abandon it (Codex review: "Advance
    cycle age when deferring a guarded recovery")."""
    monkeypatch.setenv("DCA_CRON_TRACKING_ROOT", str(tmp_path / "cron_tracking"))
    config = _base_config(inverse_martingale={"AAPL": PCT_ENROLLMENT})
    state = StrategyState()

    _run_day(tmp_path, config, state, TEST_DATES[0], 95.0, "day1")  # unguarded: -5%, accum=$7

    tracker_path = tmp_path / "cron_tracking" / "inverse_martingale_dca" / "AAPL" / "tracker.json"
    before = json.loads(tracker_path.read_text())
    assert before["status"] == "dipping"

    prior = PriorSameDayAllocation(
        "AAPL", amount_usd=5.0, reference_price=100.0, run_id="morning",
        source="scheduled", selected_base_amount_usd=2.0,
    )
    # Day 2: recovers -> would fire, but guarded by a prior same-day allocation.
    r2, s2, _ = _run_day(
        tmp_path, config, state, TEST_DATES[1], 96.5, "day2",
        prior_same_day_allocations={"AAPL": prior},
    )
    assert s2["buy_candidates"] == []  # deferred, not claimed
    after = json.loads(tracker_path.read_text())
    # Balance/status/tiers/reference close are untouched -- unclaimed,
    # retried fresh once unguarded -- but the age fields still advanced (and
    # the ledger's own event log, which every claim_fire() write appends to
    # regardless of what changed, gained one more entry for it).
    # last_evaluated_is_real also flips to False -- this is an age-only
    # bump, not a real evaluation by "day2"'s own stream (Codex review:
    # "Mark deferred-fire age updates as non-real") -- so it's excluded from
    # the untouched-fields comparison and asserted directly instead.
    _EXCLUDED = ("trading_days_active", "last_evaluated_trading_day", "last_evaluated_is_real", "events")
    unaged_before = {k: v for k, v in before.items() if k not in _EXCLUDED}
    unaged_after = {k: v for k, v in after.items() if k not in _EXCLUDED}
    assert unaged_after == unaged_before
    assert after["trading_days_active"] == before["trading_days_active"] + 1
    assert after["last_evaluated_trading_day"] == TEST_DATES[1].isoformat()
    assert after["last_evaluated_is_real"] is False
    assert len(after["events"]) == len(before["events"]) + 1
    assert after["events"][-1]["reason"] == "fire_deferred_same_day_guard"

    # Day 3: unguarded -> the same recovery now fires cleanly.
    r3, s3, _ = _run_day(tmp_path, config, state, TEST_DATES[2], 96.5, "day3")
    [candidate] = s3["buy_candidates"]
    assert "imart" in (candidate.get("order_id") or "")


def test_crash_after_fire_claim_keeps_balance_recoverable(tmp_path, monkeypatch):
    """A reservation made before broker placement must not debit the ledger."""
    monkeypatch.setenv("DCA_CRON_TRACKING_ROOT", str(tmp_path / "cron_tracking"))
    config = _base_config(inverse_martingale={"AAPL": PCT_ENROLLMENT})
    state = StrategyState()
    _run_day(tmp_path, config, state, TEST_DATES[0], 95.0, "day1")

    import near_close_dca as ncd

    def crash_before_placement(**kwargs):
        raise RuntimeError("simulated exit before broker placement")

    monkeypatch.setattr(ncd, "_process_buy_candidates_batch", crash_before_placement)
    with pytest.raises(RuntimeError, match="simulated exit"):
        _run_day(tmp_path, config, state, TEST_DATES[1], 96.5, "day2")

    tracker_path = tmp_path / "cron_tracking" / "inverse_martingale_dca" / "AAPL" / "tracker.json"
    claimed = json.loads(tracker_path.read_text())
    assert claimed["settlement_pending"] is True
    assert claimed["settlement_ref_id"].endswith("-2026-06-30-AAPL")
    # The balance stays on the tracker ITSELF, not in a nested side field: the
    # whole point of a recoverable lease is that a reader who knows nothing
    # about it still sees the real accumulated dollars.
    assert claimed["accumulated_usd"] == pytest.approx(7.0)
    assert claimed["settlement_claimed_usd"] == pytest.approx(7.0)


def test_next_day_crash_recovery_reconciles_terminal_partial_order(tmp_path, monkeypatch):
    """An old lease must query its claim day and retain only the unfilled remainder."""
    monkeypatch.setenv("DCA_CRON_TRACKING_ROOT", str(tmp_path / "cron_tracking"))
    config = _base_config(inverse_martingale={"AAPL": PCT_ENROLLMENT})
    state = StrategyState()
    _run_day(tmp_path, config, state, TEST_DATES[0], 95.0, "day1")

    import near_close_dca as ncd

    monkeypatch.setattr(
        ncd,
        "_process_buy_candidates_batch",
        lambda **kwargs: (_ for _ in ()).throw(RuntimeError("simulated exit")),
    )
    with pytest.raises(RuntimeError, match="simulated exit"):
        _run_day(tmp_path, config, state, TEST_DATES[1], 96.5, "day2")
    monkeypatch.undo()
    monkeypatch.setenv("DCA_CRON_TRACKING_ROOT", str(tmp_path / "cron_tracking"))

    tracker_path = tmp_path / "cron_tracking" / "inverse_martingale_dca" / "AAPL" / "tracker.json"
    claim = json.loads(tracker_path.read_text())

    class HistoricalBroker(DryRunBroker):
        def __init__(self):
            super().__init__(cash=500.0, reference_prices={"AAPL": 96.5})
            self.queried_days = []

        def strategy_order_with_ref_id(self, ticker, trading_day, ref_id):
            self.queried_days.append(trading_day)
            assert ref_id == claim["settlement_ref_id"]
            return {
                "state": "cancelled",
                "ref_id": ref_id,
                "dollar_amount": "7.00",
                "cumulative_quantity": "0.03",
                "average_price": "100.00",
            }

    broker = HistoricalBroker()
    _, run_state, _ = _run_day(
        tmp_path, config, state, TEST_DATES[2], 96.5, "day3", broker=broker
    )
    recovered = json.loads(tracker_path.read_text())
    assert broker.queried_days == [TEST_DATES[1]]
    # This lease was claimed YESTERDAY, so its $3 must NOT appear among today's
    # buy candidates: a run_state artifact carries one top-level trading day, so
    # load_prior_same_day_allocations would read the row as an allocation made
    # today and charge it against today's caps in every later slot (Codex
    # review: "Keep prior-day recoveries out of today's submission context").
    assert [row for row in run_state["buy_candidates"] if row["ticker"] == "AAPL"] == []
    assert recovered["status"] == STATUS_FIRING
    assert recovered["accumulated_usd"] == pytest.approx(4.0)
    assert recovered["fire_attempts"] == 1
    assert recovered["last_evaluated_trading_day"] == TEST_DATES[2].isoformat()
    assert recovered["last_evaluated_stream"] == "afternoon"
    assert "settlement_pending" not in recovered
    # The recovered $3 also consumed real budget on the CLAIM day, not today:
    # a lease recovered the morning after its crash spent yesterday's
    # daily/weekly headroom, and record_buy keys those buckets by day.
    assert state.day_notional(TEST_DATES[1]) == pytest.approx(3.0)
    assert state.buys_today("AAPL", TEST_DATES[1]) == 1


def test_an_expired_lease_release_folds_the_outage_into_trading_days_active(tmp_path):
    """Regression test for "Account for lease outage days before restoring
    the cycle".

    `_inverse_martingale_advance_age_for_missing_data` deliberately never
    touches a `settlement_pending` cycle's age fields (racing the owning
    process's own settlement write is unsafe), so while broker history stays
    unreachable and a lease sits pending, `trading_days_active` is frozen
    exactly as it was at claim time. Restoring the pre-claim state UNCHANGED
    once the lease finally resolves left the very next real evaluation
    crediting only a single `trading_days_active += 1` for the whole gap --
    however long broker history was actually unreachable -- letting a cycle
    already close to `max_cycle_days` at claim time resume and fire a stale
    balance well past its real age.

    Calls `_inverse_martingale_recover_pending_lease` directly rather than
    through the full `_run_day` harness, since the fixture's approved
    trading window is only a few days wide and this scenario needs an
    outage spanning many more than that.
    """
    import near_close_dca as ncd

    claim_day = date(2026, 6, 1)
    resolution_day = date(2026, 6, 20)  # a 19-calendar-day outage
    tracker_state = {
        "status": STATUS_FIRING,
        "accumulated_usd": 5.0,
        "cycle_start_trading_day": claim_day.isoformat(),
        "last_evaluated_trading_day": claim_day.isoformat(),
        "cycle_reference_close": 100.0,
        "lowest_drop_seen": 2.0,
        "fire_attempts": 1,
        "trading_days_active": 5,
        "settlement_pending": True,
        "settlement_ref_id": f"graded-dca-imart-{claim_day.isoformat()}-AAPL",
        "settlement_trading_day": claim_day.isoformat(),
        "settlement_claimed_usd": 5.0,
        "settlement_target_state": {
            "status": STATUS_NONE,
            "last_evaluated_trading_day": claim_day.isoformat(),
            "last_evaluated_stream": "afternoon",
        },
        "settlement_live": True,
    }

    class _NoOrderBroker:
        account_number = "mock"

        def strategy_order_with_ref_id(self, ticker, trading_day, ref_id):
            return None

    ledger = InverseMartingaleLedger(root=tmp_path)
    ledger.record_step(
        "AAPL", tracker_state,
        InverseMartingaleStep(
            status_before=STATUS_FIRING, status_after=STATUS_FIRING,
            accumulated_usd_before=10.0, accumulated_usd_after=5.0,
            newly_crossed_tiers=(), fired=True, fire_amount_usd=5.0,
            reason="fire_continuation",
        ),
    )
    persisted_tracker_state = ledger.read("AAPL")
    recovery = ncd._inverse_martingale_recover_pending_lease(
        ledger, "AAPL", persisted_tracker_state,
        broker=cast(Any, _NoOrderBroker()), dry_run=False, trading_day=resolution_day, stream="afternoon",
    )

    assert recovery is not None
    assert recovery.recovered_state["status"] == STATUS_FIRING
    # 5 (the age at claim time) + 19 (the calendar-day outage, a safe upper
    # bound on trading days actually missed) -- not 5 + 1, the single
    # increment a naive "restore unchanged, let the next evaluation advance
    # it" approach would produce regardless of how long the outage lasted.
    assert recovery.recovered_state["trading_days_active"] == 5 + 19


def test_an_exact_fill_reconciliation_ages_a_capped_continuation(tmp_path):
    """Regression test for "Age a fully reconciled capped continuation".

    When the claimed chunk fills EXACTLY (the amount abs-matches
    settlement_claimed_usd within half a cent), reconciliation restores
    ``settlement_target_state`` verbatim -- for a kill-switch-CAPPED fire,
    that target is itself a STATUS_FIRING continuation, computed at claim
    time with a correct-as-of-then trading_days_active. Restoring it
    unchanged after a long broker-reconciliation outage left the next real
    evaluation crediting only a single +1 for however long that outage
    actually lasted -- the same gap the expired-lease-release branch above
    already closes, just for the exact-fill branch instead.
    """
    import near_close_dca as ncd

    claim_day = date(2026, 6, 1)
    resolution_day = date(2026, 6, 20)  # a 19-calendar-day outage
    tracker_state = {
        "status": STATUS_FIRING,
        "accumulated_usd": 15.0,
        "cycle_start_trading_day": claim_day.isoformat(),
        "last_evaluated_trading_day": claim_day.isoformat(),
        "cycle_reference_close": 100.0,
        "lowest_drop_seen": 2.0,
        "fire_attempts": 1,
        "trading_days_active": 5,
        "settlement_pending": True,
        "settlement_ref_id": f"graded-dca-imart-{claim_day.isoformat()}-AAPL",
        "settlement_trading_day": claim_day.isoformat(),
        "settlement_claimed_usd": 5.0,
        # A capped partial fire's own target: still STATUS_FIRING, with the
        # remainder ($10) and the trading_days_active correct AS OF THE
        # CLAIM DAY.
        "settlement_target_state": {
            "status": STATUS_FIRING,
            "accumulated_usd": 10.0,
            "cycle_start_trading_day": claim_day.isoformat(),
            "cycle_reference_close": 100.0,
            "lowest_drop_seen": 2.0,
            "fire_attempts": 1,
            "trading_days_active": 5,
            "last_evaluated_trading_day": claim_day.isoformat(),
            "last_evaluated_stream": "afternoon",
            "last_evaluated_is_real": True,
        },
        "settlement_live": True,
    }

    class _ExactFillBroker:
        account_number = "mock"

        def strategy_order_with_ref_id(self, ticker, trading_day, ref_id):
            return {"state": "filled", "ref_id": ref_id, "executed_notional": "5.00"}

    ledger = InverseMartingaleLedger(root=tmp_path)
    ledger.record_step(
        "AAPL", tracker_state,
        InverseMartingaleStep(
            status_before=STATUS_FIRING, status_after=STATUS_FIRING,
            accumulated_usd_before=15.0, accumulated_usd_after=15.0,
            newly_crossed_tiers=(), fired=True, fire_amount_usd=5.0,
            reason="fire_continuation",
        ),
    )
    persisted_tracker_state = ledger.read("AAPL")
    recovery = ncd._inverse_martingale_recover_pending_lease(
        ledger, "AAPL", persisted_tracker_state,
        broker=cast(Any, _ExactFillBroker()), dry_run=False, trading_day=resolution_day, stream="afternoon",
    )

    assert recovery is not None
    assert recovery.recovered_state["status"] == STATUS_FIRING
    assert recovery.recovered_state["accumulated_usd"] == pytest.approx(10.0)
    # 5 (the age at claim time) + 19 (the calendar-day outage) -- not 5 + 1.
    assert recovery.recovered_state["trading_days_active"] == 5 + 19


def _crash_a_fire_claim(tmp_path, monkeypatch, config, state):
    """Run day 1 (accumulate) then day 2 (recover, claim, die before placement),
    returning the on-disk lease the crashed claim left behind."""
    import near_close_dca as ncd

    _run_day(tmp_path, config, state, TEST_DATES[0], 95.0, "day1")
    monkeypatch.setattr(
        ncd,
        "_process_buy_candidates_batch",
        lambda **kwargs: (_ for _ in ()).throw(RuntimeError("simulated exit")),
    )
    with pytest.raises(RuntimeError, match="simulated exit"):
        _run_day(tmp_path, config, state, TEST_DATES[1], 96.5, "day2")
    monkeypatch.undo()
    monkeypatch.setenv("DCA_CRON_TRACKING_ROOT", str(tmp_path / "cron_tracking"))
    tracker_path = tmp_path / "cron_tracking" / "inverse_martingale_dca" / "AAPL" / "tracker.json"
    return tracker_path, json.loads(tracker_path.read_text())


class _HistoryBroker(DryRunBroker):
    """A broker that reports one historical order for the claimed ref."""

    def __init__(self, order, price=96.5):
        super().__init__(cash=500.0, reference_prices={"AAPL": price})
        self._order = order
        self.queried = []

    def strategy_order_with_ref_id(self, ticker, trading_day, ref_id):
        self.queried.append((ticker, trading_day, ref_id))
        if self._order is None:
            return None
        return {"ref_id": ref_id, **self._order}


@pytest.mark.parametrize("terminal_state", ["cancelled", "canceled", "voided", "rejected", "failed"])
def test_zero_fill_terminal_order_restores_the_whole_claimed_balance(
    tmp_path, monkeypatch, terminal_state
):
    """Regression test for "Recognize every terminal spelling before settling
    the claim". Robinhood reports both "cancelled" and "canceled" (and
    "voided"). Reading any of them as nonterminal made reconciliation
    substitute the order's REQUESTED dollar_amount for its executed amount --
    which matches the claim exactly -- and permanently discard an accumulator
    balance no shares were ever bought with.
    """
    monkeypatch.setenv("DCA_CRON_TRACKING_ROOT", str(tmp_path / "cron_tracking"))
    config = _base_config(inverse_martingale={"AAPL": PCT_ENROLLMENT})
    state = StrategyState()
    tracker_path, claim = _crash_a_fire_claim(tmp_path, monkeypatch, config, state)
    assert claim["settlement_claimed_usd"] == pytest.approx(7.0)

    broker = _HistoryBroker(
        {"state": terminal_state, "dollar_amount": "7.00", "cumulative_quantity": "0", "average_price": "0"}
    )
    _, run_state, _ = _run_day(tmp_path, config, state, TEST_DATES[2], 96.5, "day3", broker=broker)
    recovered = json.loads(tracker_path.read_text())
    assert "settlement_pending" not in recovered
    # No shares were ever bought, so nothing is booked against the claim day's
    # budget for a purchase that never happened...
    assert state.day_notional(TEST_DATES[1]) == pytest.approx(0.0)
    # ...but a real order DID reach the broker, and config/budgets.json counts a
    # cancelled or rejected buy as an attempt against max_orders_per_day, so the
    # roster-wide order count for the claim day must still register it (Codex
    # review: "Count recovered zero-fill orders toward the daily cap").
    assert state.day_order_count(TEST_DATES[1]) == 1
    assert state.buys_today("AAPL", TEST_DATES[1]) == 1
    # ...and the whole $7 survives rather than being written off as spent.
    assert recovered["accumulated_usd"] == pytest.approx(7.0)
    # A real order WAS submitted and came back terminal with no fill, so this
    # counts as a failed fire attempt -- otherwise a repeating crash-then-
    # terminal-failure cycle would never reach max_fire_attempts.
    assert recovered["status"] == STATUS_FIRING
    assert recovered["fire_attempts"] == 1
    assert recovered["last_evaluated_stream"] == "afternoon"
    # Stamping this stream also stops a second attempt inside this same run.
    assert [r for r in run_state["buy_candidates"] if r["ticker"] == "AAPL"] == []
    assert state.day_notional(TEST_DATES[2]) == pytest.approx(0.0)


def test_finite_nonnegative_float_catches_overflow_instead_of_raising():
    """Regression test for "Catch integer overflow while parsing broker
    amounts". json.loads() parses an arbitrarily large JSON integer into an
    unbounded Python int, and float() on one too large to represent RAISES
    OverflowError rather than returning inf. Uncaught, that escaped
    _finite_nonnegative_float and _recovered_broker_notional entirely,
    aborting the whole scheduled run over one malformed broker-history
    response instead of treating that ticker's settlement as indeterminate
    and preserving its lease.
    """
    import near_close_dca as ncd

    assert ncd._finite_nonnegative_float(10**400) is None
    assert ncd._finite_nonnegative_float({"amount": 10**400}) is None
    # An order carrying an unparseable-by-overflow executed_notional must be
    # treated as indeterminate (preserve the lease), not crash the caller.
    assert ncd._recovered_broker_notional(
        {"state": "filled", "executed_notional": 10**400}
    ) is None


def test_recovered_broker_notional_rejects_an_overflowed_product():
    """Regression test for "Reject overflowed reconstructed notionals". Even
    when cumulative_quantity and average_price are each individually finite,
    their PRODUCT can overflow to inf (e.g. two ~1e308 broker fields).
    Recovery must not treat that infinity as real executed capital -- it
    would clear the lease and pass it into ledger events and
    StrategyState.record_buy(), corrupting budget totals.
    """
    import near_close_dca as ncd

    huge = 1e308
    assert ncd._recovered_broker_notional(
        {"state": "filled", "cumulative_quantity": huge, "average_price": huge}
    ) is None
    # A genuinely representable product is unaffected.
    assert ncd._recovered_broker_notional(
        {"state": "filled", "cumulative_quantity": 0.07, "average_price": 100.0}
    ) == pytest.approx(7.0)


@pytest.mark.parametrize(
    "live_state", ["partially_filled", "new", "queued", "confirmed", "unconfirmed"]
)
def test_live_order_keeps_the_lease_until_it_is_terminal(tmp_path, monkeypatch, live_state):
    """Regression test for "Keep live partial fills leased until the order is
    terminal". A working order can still fill its own remainder. Rebuilding an
    unleased continuation for the currently-unfilled amount let a later
    scheduled stream fire that same remainder again -- inverse-Martingale
    candidates bypass the ordinary duplicate-order guard, so nothing else would
    have stopped it.
    """
    monkeypatch.setenv("DCA_CRON_TRACKING_ROOT", str(tmp_path / "cron_tracking"))
    config = _base_config(inverse_martingale={"AAPL": PCT_ENROLLMENT})
    state = StrategyState()
    tracker_path, claim = _crash_a_fire_claim(tmp_path, monkeypatch, config, state)

    broker = _HistoryBroker(
        {
            "state": live_state,
            "dollar_amount": "7.00",
            "cumulative_quantity": "0.03",
            "average_price": "100.00",
        }
    )
    _, run_state, _ = _run_day(tmp_path, config, state, TEST_DATES[2], 96.5, "day3", broker=broker)

    assert json.loads(tracker_path.read_text()) == claim  # untouched
    assert [row for row in run_state["buy_candidates"] if row["ticker"] == "AAPL"] == []


@pytest.mark.parametrize(
    "bad_fields",
    [
        {"cumulative_quantity": "corrupt"},
        {"average_price": "corrupt"},
        {"cumulative_quantity": "NaN"},
        {"average_price": "Infinity"},
        {"cumulative_quantity": "-1"},
    ],
)
def test_unparseable_broker_notional_keeps_the_lease(tmp_path, monkeypatch, bad_fields):
    """Regression test for "Preserve the lease when broker notional is
    unparseable". Reading an unusable execution field as $0 made a genuinely
    live or filled order look like an unplaced claim, releasing the whole
    balance for a duplicate purchase; an `Infinity` would instead have been
    booked into strategy state as real spend.
    """
    monkeypatch.setenv("DCA_CRON_TRACKING_ROOT", str(tmp_path / "cron_tracking"))
    config = _base_config(inverse_martingale={"AAPL": PCT_ENROLLMENT})
    state = StrategyState()
    tracker_path, claim = _crash_a_fire_claim(tmp_path, monkeypatch, config, state)

    order = {
        "state": "filled",
        "dollar_amount": "7.00",
        "cumulative_quantity": "0.07",
        "average_price": "100.00",
    }
    order.update(bad_fields)
    broker = _HistoryBroker(order)
    _run_day(tmp_path, config, state, TEST_DATES[2], 96.5, "day3", broker=broker)

    assert json.loads(tracker_path.read_text()) == claim  # untouched
    assert state.day_notional(TEST_DATES[1]) == pytest.approx(0.0)
    assert state.day_notional(TEST_DATES[2]) == pytest.approx(0.0)


def test_recovery_against_a_different_broker_account_keeps_the_lease(tmp_path, monkeypatch):
    """Regression test for "Verify the claiming broker account before releasing
    a lease". Broker history is scoped to one account, so an empty result from a
    DIFFERENT account says nothing about whether the claim was submitted --
    releasing on that silence frees a balance whose real order sits in the
    original account.
    """
    monkeypatch.setenv("DCA_CRON_TRACKING_ROOT", str(tmp_path / "cron_tracking"))
    # Built from repeated digits rather than written out, so no account-shaped
    # literal appears in this file (see test_config_invariants.py's standing
    # grep for the secrets policy).
    claiming_account = "1" * 9
    other_account = "2" * 9

    class _AccountBroker(_HistoryBroker):
        def __init__(self, account, order=None):
            super().__init__(order)
            self._account = account

        def account_number(self):
            return self._account

    config = _base_config(
        inverse_martingale={"AAPL": PCT_ENROLLMENT}, target_account_number=claiming_account
    )
    state = StrategyState()
    claiming = _AccountBroker(claiming_account)
    _run_day(tmp_path, config, state, TEST_DATES[0], 95.0, "day1", broker=claiming)
    import near_close_dca as ncd

    monkeypatch.setattr(
        ncd,
        "_process_buy_candidates_batch",
        lambda **kwargs: (_ for _ in ()).throw(RuntimeError("simulated exit")),
    )
    with pytest.raises(RuntimeError, match="simulated exit"):
        _run_day(tmp_path, config, state, TEST_DATES[1], 96.5, "day2", broker=claiming)
    monkeypatch.undo()
    monkeypatch.setenv("DCA_CRON_TRACKING_ROOT", str(tmp_path / "cron_tracking"))

    tracker_path = tmp_path / "cron_tracking" / "inverse_martingale_dca" / "AAPL" / "tracker.json"
    claim = json.loads(tracker_path.read_text())
    # A non-secret fingerprint, never the account number itself.
    assert claim["settlement_account"] and claiming_account not in json.dumps(claim)

    # A later run wired to a different account must not release the lease on the
    # strength of that account's (necessarily empty) history.
    other_config = _base_config(
        inverse_martingale={"AAPL": PCT_ENROLLMENT}, target_account_number=other_account
    )
    other = _AccountBroker(other_account, order=None)
    _run_day(tmp_path, other_config, state, TEST_DATES[2], 96.5, "day3", broker=other)
    # The lookup still runs -- a positive match would be trustworthy however the
    # accounts line up, since finding the order proves it exists. What is
    # withheld is the RELEASE: this account's empty history says nothing about
    # an order submitted into the other one.
    assert json.loads(tracker_path.read_text()) == claim


def test_executed_notional_values_a_fill_with_no_quantity_or_price(tmp_path, monkeypatch):
    """Regression test for "Honor authoritative executed notional during
    recovery". `executed_notional` is the broker's own cent-precise settled
    figure and the field `scripts/broker_reconciliation._executed_dollars`
    prefers. Ignoring it meant a valid supported row that omits quantity or
    price reconstructed as $0.00, which recovery read as a confirmed zero-fill
    and released the entire claim for a second purchase.
    """
    monkeypatch.setenv("DCA_CRON_TRACKING_ROOT", str(tmp_path / "cron_tracking"))
    config = _base_config(inverse_martingale={"AAPL": PCT_ENROLLMENT})
    state = StrategyState()
    tracker_path, claim = _crash_a_fire_claim(tmp_path, monkeypatch, config, state)
    assert claim["settlement_claimed_usd"] == pytest.approx(7.0)

    broker = _HistoryBroker({"state": "filled", "executed_notional": "7.00"})
    _run_day(tmp_path, config, state, TEST_DATES[2], 96.5, "day3", broker=broker)

    recovered = json.loads(tracker_path.read_text())
    # Recognized as a full fill: the claim settles to its target and the $7 is
    # booked against the claim day, instead of the balance being released as
    # unplaced and bought a second time today.
    assert "settlement_pending" not in recovered
    assert state.day_notional(TEST_DATES[1]) == pytest.approx(7.0)
    assert state.day_notional(TEST_DATES[2]) == pytest.approx(0.0)


def test_terminal_fill_with_no_usable_price_keeps_the_lease(tmp_path, monkeypatch):
    """A reported fill nobody can value is indeterminate, not a confirmed zero
    -- the distinction `broker_reconciliation` tracks as `has_unvalued_fill`."""
    monkeypatch.setenv("DCA_CRON_TRACKING_ROOT", str(tmp_path / "cron_tracking"))
    config = _base_config(inverse_martingale={"AAPL": PCT_ENROLLMENT})
    state = StrategyState()
    tracker_path, claim = _crash_a_fire_claim(tmp_path, monkeypatch, config, state)

    # A real fill (quantity > 0) with no price and no executed_notional.
    broker = _HistoryBroker({"state": "cancelled", "cumulative_quantity": "0.03"})
    _run_day(tmp_path, config, state, TEST_DATES[2], 96.5, "day3", broker=broker)
    assert json.loads(tracker_path.read_text()) == claim


@pytest.mark.parametrize(
    "order",
    [
        {"state": "filled"},  # no valuation fields at all
        {"state": "filled", "executed_notional": "0"},
        {"state": "filled", "cumulative_quantity": "0", "average_price": "0"},
    ],
)
def test_a_filled_order_with_no_positive_valuation_keeps_the_lease(tmp_path, monkeypatch, order):
    """Regression test for "Treat filled orders without positive notional as
    indeterminate".

    A `state="filled"` row with no positive valuation from any field fell
    through to 0.0, so recovery read it as a confirmed zero-fill, restored the
    entire accumulator balance and allowed it to be submitted again -- even
    though the broker is saying the original order executed. Zero is only ever
    defensible for a terminal FAILURE state, where "nothing executed" is what
    the state itself means.
    """
    monkeypatch.setenv("DCA_CRON_TRACKING_ROOT", str(tmp_path / "cron_tracking"))
    config = _base_config(inverse_martingale={"AAPL": PCT_ENROLLMENT})
    state = StrategyState()
    tracker_path, claim = _crash_a_fire_claim(tmp_path, monkeypatch, config, state)

    broker = _HistoryBroker(order)
    _run_day(tmp_path, config, state, TEST_DATES[2], 96.5, "day3", broker=broker)
    # Indeterminate: the lease survives exactly as the crash left it, and no
    # balance is released for a second purchase.
    assert json.loads(tracker_path.read_text()) == claim
    assert state.day_notional(TEST_DATES[1]) == pytest.approx(0.0)


def test_live_run_releases_an_expired_dry_run_lease(tmp_path, monkeypatch):
    """Regression test for "Let live runs release leases created by dry runs".
    `settlement_live=False` proves no broker order could exist, so no account
    holds one and account identity is irrelevant. Comparing anyway froze such a
    lease forever once the configured account value changed — exactly what
    happens when a dry run fingerprints a placeholder and a live run later
    supplies the real value.
    """
    monkeypatch.setenv("DCA_CRON_TRACKING_ROOT", str(tmp_path / "cron_tracking"))
    dry = _base_config(inverse_martingale={"AAPL": PCT_ENROLLMENT}, dry_run=True)
    state = StrategyState()
    tracker_path, claim = _crash_a_fire_claim(tmp_path, monkeypatch, dry, state)
    assert claim["settlement_live"] is False
    # A dry-run claim pins no account: there is no order for one to be found in.
    assert claim.get("settlement_account") is None

    live = _base_config(inverse_martingale={"AAPL": PCT_ENROLLMENT})
    broker = _HistoryBroker(None)
    _run_day(tmp_path, live, StrategyState(), TEST_DATES[2], 96.5, "day3", broker=broker)

    released = json.loads(tracker_path.read_text())
    assert "settlement_pending" not in released
    # The balance was never spent, so it survives the release intact.
    assert released.get("accumulated_usd", 0.0) > 0 or released["status"] == STATUS_NONE


def test_same_day_recovery_of_a_removed_ticker_is_still_published(tmp_path, monkeypatch):
    """Regression test for "Emit recovered orders even when the ticker has no
    decision". `_build_buy_candidates` iterates decisions, and the ledger-wide
    recovery pass deliberately reaches tickers that have none — an un-enrolled
    or removed ticker. Its recovered purchase was therefore dropped from
    `buy_candidates` and `orders_placed_count` in exactly the scenario the pass
    exists to support.
    """
    monkeypatch.setenv("DCA_CRON_TRACKING_ROOT", str(tmp_path / "cron_tracking"))
    config = _base_config(inverse_martingale={"AAPL": PCT_ENROLLMENT})
    state = StrategyState()
    tracker_path, claim = _crash_a_fire_claim(tmp_path, monkeypatch, config, state)

    # SAME trading day as the claim, but the ticker is gone from the universe,
    # so this run produces no decision for it at all.
    removed = _base_config(inverse_martingale={}, tickers=("MSFT",))
    broker = _HistoryBroker(
        {"state": "filled", "executed_notional": "7.00"}, price=96.5
    )
    _, run_state, _ = _run_day(
        tmp_path, removed, state, TEST_DATES[1], 96.5, "day2b", broker=broker
    )

    assert "settlement_pending" not in json.loads(tracker_path.read_text())
    rows = [row for row in run_state["buy_candidates"] if row["ticker"] == "AAPL"]
    assert len(rows) == 1
    assert rows[0]["reason"] == "recovered_inverse_martingale_lease"
    assert rows[0]["status"] == "submitted"
    assert float(rows[0]["planned_amount_usd"]) == pytest.approx(7.0)
    # Signal-derived fields are absent rather than fabricated: this row records a
    # purchase reconstructed from broker history, not a decision this run made.
    assert rows[0]["target_price"] is None
    # Per-ticker, not day_notional: this run also buys MSFT through the ordinary
    # ladder, and the day aggregate sums every ticker.
    assert state.buys_today("AAPL", TEST_DATES[1]) == 1


def test_live_claim_without_a_fingerprint_is_never_released_on_absence(tmp_path, monkeypatch):
    """Regression test for "Refuse live claims without an account fingerprint".
    When the identity cannot be computed -- an unwritable key, a refused probe --
    persisting no account at all made the lease indistinguishable from a legacy
    one, which recovery exempts from account matching. A later run wired to a
    different account could then release the balance on that account's empty
    history.
    """
    import near_close_dca as ncd

    monkeypatch.setenv("DCA_CRON_TRACKING_ROOT", str(tmp_path / "cron_tracking"))
    # Simulate a fingerprint that cannot be computed at claim time.
    monkeypatch.setattr(ncd, "_broker_account_fingerprint", lambda *a, **k: None)
    config = _base_config(inverse_martingale={"AAPL": PCT_ENROLLMENT})
    state = StrategyState()
    tracker_path, claim = _crash_a_fire_claim(tmp_path, monkeypatch, config, state)
    # Recorded explicitly as unknown, NOT omitted -- omission reads as "legacy
    # lease, nothing to contradict" and would permit the release below.
    assert claim["settlement_live"] is True
    assert claim["settlement_account"] == ncd._LEASE_ACCOUNT_UNKNOWN

    monkeypatch.setattr(ncd, "_broker_account_fingerprint", lambda *a, **k: None)
    broker = _HistoryBroker(None)
    _run_day(tmp_path, config, state, TEST_DATES[2], 96.5, "day3", broker=broker)
    assert json.loads(tracker_path.read_text()) == claim  # preserved, not released


def test_mapping_shaped_executed_notional_is_valued(tmp_path, monkeypatch):
    """Regression test for "Parse mapping-shaped executed notionals". A Robinhood
    money value is a scalar OR a {"amount": ...} mapping (see
    scripts/broker_reconciliation._money). Rejecting the mapping made a
    perfectly valuable fill read as unreadable and froze its lease.
    """
    monkeypatch.setenv("DCA_CRON_TRACKING_ROOT", str(tmp_path / "cron_tracking"))
    config = _base_config(inverse_martingale={"AAPL": PCT_ENROLLMENT})
    state = StrategyState()
    tracker_path, claim = _crash_a_fire_claim(tmp_path, monkeypatch, config, state)

    broker = _HistoryBroker({"state": "filled", "executed_notional": {"amount": "7.00"}})
    _run_day(tmp_path, config, state, TEST_DATES[2], 96.5, "day3", broker=broker)

    assert "settlement_pending" not in json.loads(tracker_path.read_text())
    assert state.day_notional(TEST_DATES[1]) == pytest.approx(7.0)


def test_dry_run_lease_expires_without_a_broker_lookup(tmp_path, monkeypatch):
    """Regression test for "Skip broker lookup for dry-run-origin leases".
    settlement_live=False proves no order was ever submitted, so no history can
    contain it. Requiring the lookup anyway froze such a lease behind an adapter
    that cannot query client ids at all.
    """
    monkeypatch.setenv("DCA_CRON_TRACKING_ROOT", str(tmp_path / "cron_tracking"))
    dry = _base_config(inverse_martingale={"AAPL": PCT_ENROLLMENT}, dry_run=True)
    state = StrategyState()
    tracker_path, claim = _crash_a_fire_claim(tmp_path, monkeypatch, dry, state)
    assert claim["settlement_live"] is False

    class _NoHistoryBroker(DryRunBroker):
        """An adapter that cannot reconcile by client id at all (AlpacaBroker-like)."""

        def __init__(self):
            super().__init__(cash=500.0, reference_prices={"AAPL": 96.5})

        def strategy_order_with_ref_id(self, ticker: str, trading_day: date, ref_id: str) -> Any:
            return None

    live = _base_config(inverse_martingale={"AAPL": PCT_ENROLLMENT})
    _run_day(
        tmp_path, live, StrategyState(), TEST_DATES[2], 96.5, "day3", broker=_NoHistoryBroker()
    )
    assert "settlement_pending" not in json.loads(tracker_path.read_text())


def test_partial_fill_that_exhausts_the_balance_resolves_the_cycle(tmp_path, monkeypatch):
    """Regression test for "Resolve partial fills that exhaust the accumulator".
    A recovered amount below the claim but at or above the pre-fire balance left
    STATUS_FIRING with accumulated_usd=0 -- which the validator rejects as
    corruption on the very next read, permanently skipping the ticker.
    """
    monkeypatch.setenv("DCA_CRON_TRACKING_ROOT", str(tmp_path / "cron_tracking"))
    config = _base_config(inverse_martingale={"AAPL": PCT_ENROLLMENT})
    state = StrategyState()
    tracker_path, claim = _crash_a_fire_claim(tmp_path, monkeypatch, config, state)
    assert claim["accumulated_usd"] == pytest.approx(7.0)

    # Differs from the $7 claim, but consumes the whole $7 balance.
    broker = _HistoryBroker({"state": "cancelled", "executed_notional": "7.50"})
    _run_day(tmp_path, config, state, TEST_DATES[2], 96.5, "day3", broker=broker)

    recovered = json.loads(tracker_path.read_text())
    assert "settlement_pending" not in recovered
    # The paid floor PRICE is persisted alongside the metric, exactly as every
    # ordinary fire-resolution path does. With only the metric, the same-day
    # re-arm guard compares it in whatever reference frame a later stream has,
    # and a price still above the already-paid floor can read as a fresh deeper
    # descent and re-credit tiers that were already bought (Codex review:
    # "Preserve the paid floor price on recovered resolution").
    assert recovered.get("fired_floor_price") is not None
    assert recovered["fired_floor_price"] > 0
    # A resolved marker, never an active cycle holding a zero balance.
    assert not (
        recovered.get("status") == STATUS_FIRING
        and float(recovered.get("accumulated_usd", 0.0)) <= 0
    )
    # And it must still be readable -- the corruption this guards against showed
    # up only on the NEXT load.
    InverseMartingaleLedger(
        root=tmp_path / "cron_tracking" / "inverse_martingale_dca"
    ).read("AAPL")
    # The claim's own rearm_last_seen_day must survive into the recovered
    # marker (Codex review: "Preserve the pre-split close in recovered
    # resolution"). Without it, the split-rebase lookup on the next
    # evaluation never runs -- isinstance(rearm_last_seen_day, str) is its
    # gate -- so a real corporate action between this claim and the next
    # evaluation can never be detected.
    assert recovered["rearm_last_seen_day"] == claim["settlement_target_state"]["rearm_last_seen_day"]


def test_stale_recovery_is_not_added_to_the_seven_day_deployment(tmp_path, monkeypatch):
    """Regression test for "Exclude stale recoveries from the seven-day deployment
    total". _load_deployment_history_states sums only the trailing kill-switch
    window, so a claim day outside it is no longer part of the configured
    exposure and must not inflate the runtime figure."""
    from datetime import date as _date

    from near_close_dca import _within_kill_switch_window

    today = _date(2026, 6, 30)
    assert _within_kill_switch_window(today, today)
    assert _within_kill_switch_window(today - timedelta(days=6), today)
    assert not _within_kill_switch_window(today - timedelta(days=7), today)
    assert not _within_kill_switch_window(today - timedelta(days=30), today)
    # A future claim day is not in the trailing window either.
    assert not _within_kill_switch_window(today + timedelta(days=1), today)


def test_filled_order_without_cumulative_quantity_uses_quantity(tmp_path, monkeypatch):
    """Regression test for "Fall back to quantity when valuing filled orders".
    scripts/broker_reconciliation._normalize_orders falls back to `quantity`
    when `cumulative_quantity` is genuinely absent; without the same fallback a
    real fill valued as $0.00 and the whole claim was released for a duplicate
    purchase.
    """
    monkeypatch.setenv("DCA_CRON_TRACKING_ROOT", str(tmp_path / "cron_tracking"))
    config = _base_config(inverse_martingale={"AAPL": PCT_ENROLLMENT})
    state = StrategyState()
    tracker_path, claim = _crash_a_fire_claim(tmp_path, monkeypatch, config, state)

    broker = _HistoryBroker({"state": "filled", "quantity": "0.07", "average_price": "100.00"})
    _run_day(tmp_path, config, state, TEST_DATES[2], 96.5, "day3", broker=broker)

    assert "settlement_pending" not in json.loads(tracker_path.read_text())
    assert state.day_notional(TEST_DATES[1]) == pytest.approx(7.0)


def test_filled_order_with_empty_string_cumulative_quantity_falls_back_to_quantity(
    tmp_path, monkeypatch
):
    """Regression test (Antigravity review: "Empty String Check for
    cumulative_quantity"). A broker payload with `cumulative_quantity: ""`
    is not None, so the plain `is not None` check picked "cumulative_quantity"
    as the field to read, parsed it to None, and never fell back to
    "quantity" for a genuinely filled order -- valuing a real fill at $0
    instead of using the fallback the "genuinely absent" case already gets.
    """
    monkeypatch.setenv("DCA_CRON_TRACKING_ROOT", str(tmp_path / "cron_tracking"))
    config = _base_config(inverse_martingale={"AAPL": PCT_ENROLLMENT})
    state = StrategyState()
    tracker_path, claim = _crash_a_fire_claim(tmp_path, monkeypatch, config, state)

    broker = _HistoryBroker(
        {"state": "filled", "cumulative_quantity": "", "quantity": "0.07", "average_price": "100.00"}
    )
    _run_day(tmp_path, config, state, TEST_DATES[2], 96.5, "day3", broker=broker)

    assert "settlement_pending" not in json.loads(tracker_path.read_text())
    assert state.day_notional(TEST_DATES[1]) == pytest.approx(7.0)


def test_filled_order_with_whitespace_only_cumulative_quantity_falls_back_to_quantity(
    tmp_path, monkeypatch
):
    """Regression test (Codex review: "Treat whitespace-only cumulative
    quantities as absent"). A broker payload with `cumulative_quantity: " "`
    (whitespace, not empty) is not caught by an empty-string check either --
    it still gets selected as the field to read, parses to None, and
    preserves the lease indefinitely instead of valuing the fill.
    """
    monkeypatch.setenv("DCA_CRON_TRACKING_ROOT", str(tmp_path / "cron_tracking"))
    config = _base_config(inverse_martingale={"AAPL": PCT_ENROLLMENT})
    state = StrategyState()
    tracker_path, claim = _crash_a_fire_claim(tmp_path, monkeypatch, config, state)

    broker = _HistoryBroker(
        {"state": "filled", "cumulative_quantity": "  ", "quantity": "0.07", "average_price": "100.00"}
    )
    _run_day(tmp_path, config, state, TEST_DATES[2], 96.5, "day3", broker=broker)

    assert "settlement_pending" not in json.loads(tracker_path.read_text())
    assert state.day_notional(TEST_DATES[1]) == pytest.approx(7.0)


def test_cancelled_order_never_treats_requested_quantity_as_executed(tmp_path, monkeypatch):
    """The fallback is scoped to "filled": on a cancelled order `quantity` is
    what was REQUESTED, so using it would write off a balance no shares bought."""
    monkeypatch.setenv("DCA_CRON_TRACKING_ROOT", str(tmp_path / "cron_tracking"))
    config = _base_config(inverse_martingale={"AAPL": PCT_ENROLLMENT})
    state = StrategyState()
    tracker_path, claim = _crash_a_fire_claim(tmp_path, monkeypatch, config, state)

    broker = _HistoryBroker({"state": "cancelled", "quantity": "0.07", "average_price": "100.00"})
    _run_day(tmp_path, config, state, TEST_DATES[2], 96.5, "day3", broker=broker)
    # Zero executed -> nothing is booked, and the balance survives intact as a
    # failed fire attempt rather than being written off.
    recovered = json.loads(tracker_path.read_text())
    assert state.day_notional(TEST_DATES[1]) == pytest.approx(0.0)
    assert state.day_notional(TEST_DATES[2]) == pytest.approx(0.0)
    assert recovered["accumulated_usd"] == pytest.approx(7.0)


def test_same_day_recovery_of_an_enrolled_ticker_is_not_a_credit_flush(tmp_path, monkeypatch):
    """Regression test for "Mark covered recovery rows as recoveries". An
    enrolled ticker still has a (non-BUY) decision, so a recovered outcome
    matched the pending-credit-flush shape and was published with that
    decision's reason and signal fields — a credit flush that never happened.
    """
    monkeypatch.setenv("DCA_CRON_TRACKING_ROOT", str(tmp_path / "cron_tracking"))
    config = _base_config(inverse_martingale={"AAPL": PCT_ENROLLMENT})
    state = StrategyState()
    tracker_path, claim = _crash_a_fire_claim(tmp_path, monkeypatch, config, state)

    # SAME trading day as the claim, ticker still enrolled -> it has a decision.
    broker = _HistoryBroker({"state": "filled", "executed_notional": "7.00"}, price=96.5)
    _, run_state, _ = _run_day(
        tmp_path, config, state, TEST_DATES[1], 96.5, "day2b", broker=broker
    )
    rows = [r for r in run_state["buy_candidates"] if r["ticker"] == "AAPL"]
    assert len(rows) == 1
    assert rows[0]["reason"] == "recovered_inverse_martingale_lease"
    assert rows[0]["is_pending_credit_flush"] is False
    assert float(rows[0]["planned_amount_usd"]) == pytest.approx(7.0)


def test_recovery_does_not_rebook_spend_already_on_disk(tmp_path, monkeypatch):
    """Regression test for "Avoid rebooking recovery spend already persisted in
    state". own_orders_recorded is run-scoped and deliberately not persisted,
    while StrategyState is -- so a prior run that booked the buy, persisted, and
    wrote its artifact before dying would have its spend counted twice.
    """
    import near_close_dca as ncd

    monkeypatch.setenv("DCA_CRON_TRACKING_ROOT", str(tmp_path / "cron_tracking"))
    config = _base_config(inverse_martingale={"AAPL": PCT_ENROLLMENT})
    state = StrategyState()
    tracker_path, claim = _crash_a_fire_claim(tmp_path, monkeypatch, config, state)

    # Model the durable evidence: an on-disk artifact already records this
    # ticker/day submission, exactly as the failure handler would have written.
    monkeypatch.setattr(
        ncd, "_same_day_order_recorded_in_any_stream", lambda *a, **k: True
    )
    # A fresh StrategyState carrying the already-persisted spend, and an EMPTY
    # own_orders_recorded -- the post-restart shape.
    restored = StrategyState()
    restored.record_buy("AAPL", TEST_DATES[1], 7.0)
    assert restored.day_notional(TEST_DATES[1]) == pytest.approx(7.0)

    broker = _HistoryBroker({"state": "filled", "executed_notional": "7.00"})
    _run_day(
        tmp_path, config, restored, TEST_DATES[2], 96.5, "day3", broker=broker,
        deployment_history_root=tmp_path / "runs",
    )

    # Still $7, not $14.
    assert restored.day_notional(TEST_DATES[1]) == pytest.approx(7.0)
    assert restored.buys_today("AAPL", TEST_DATES[1]) == 1


def test_prior_week_recovery_forces_the_rollover_reserve_to_recompute(tmp_path, monkeypatch):
    """Regression test for "Recompute rollover reserve after prior-week
    recovery". The reserve is computed once per week and keyed by
    reserve_week_key, so a run earlier in the week may have rolled it from a
    prior-week total that omitted the pending order; roll_weekly_reserve() would
    then early-return and leave it overstated forever.
    """
    import near_close_dca as ncd

    monkeypatch.setenv("DCA_CRON_TRACKING_ROOT", str(tmp_path / "cron_tracking"))
    config = _base_config(inverse_martingale={"AAPL": PCT_ENROLLMENT})
    state = StrategyState()
    tracker_path, claim = _crash_a_fire_claim(tmp_path, monkeypatch, config, state)
    claim_day = date.fromisoformat(claim["settlement_trading_day"])

    # Recover a full week later, so the claim day sits in an earlier ISO week.
    later = claim_day + timedelta(days=7)
    monkeypatch.setattr(ncd, "_within_kill_switch_window", lambda *a, **k: True)
    config = _base_config(
        inverse_martingale={"AAPL": PCT_ENROLLMENT},
        approved_test_dates=frozenset({*TEST_DATES, later}),
    )
    fresh = StrategyState()
    # An earlier run this week already rolled the reserve for this week.
    fresh.reserve_week_key = ncd.iso_week_key(later)

    broker = _HistoryBroker({"state": "filled", "executed_notional": "7.00"})
    _run_day(tmp_path, config, fresh, later, 96.5, "wk2", broker=broker)

    # The stale key was cleared so the roll recomputed from the corrected total.
    assert fresh.reserve_week_key == ncd.iso_week_key(later)
    assert fresh.day_notional(claim_day) == pytest.approx(7.0)


def test_expired_order_is_settled_not_left_indeterminate(tmp_path, monkeypatch):
    """Regression test for "Treat expired broker orders as settled". The repo's
    canonical terminal set (scripts/broker_reconciliation._TERMINAL_ORDER_STATES)
    includes "expired"; omitting it here made an expired order look
    indeterminate, so its lease was preserved forever even though the order can
    no longer execute.
    """
    monkeypatch.setenv("DCA_CRON_TRACKING_ROOT", str(tmp_path / "cron_tracking"))
    config = _base_config(inverse_martingale={"AAPL": PCT_ENROLLMENT})
    state = StrategyState()
    tracker_path, claim = _crash_a_fire_claim(tmp_path, monkeypatch, config, state)

    # Expired with a real partial fill: it must settle, not hang.
    broker = _HistoryBroker({"state": "expired", "executed_notional": "3.00"})
    _run_day(tmp_path, config, state, TEST_DATES[2], 96.5, "day3", broker=broker)

    recovered = json.loads(tracker_path.read_text())
    assert "settlement_pending" not in recovered
    assert state.day_notional(TEST_DATES[1]) == pytest.approx(3.0)


def test_lease_fingerprint_key_is_published_atomically(tmp_path):
    """Regression test for "Wait for the fingerprint key to be initialized".
    Creating the real path with O_EXCL and writing afterwards left a window
    where a concurrent first-time process saw the file exist, read it EMPTY and
    gave up -- persisting a live claim as settlement_account="unknown", which
    recovery then refuses to release forever.
    """
    import threading

    from near_close_dca import _LEASE_FINGERPRINT_KEY_FILENAME, _lease_fingerprint_key

    key_dir = tmp_path / "keys"
    results = []
    barrier = threading.Barrier(8)

    def _race():
        barrier.wait()
        results.append(_lease_fingerprint_key(key_dir))

    threads = [threading.Thread(target=_race) for _ in range(8)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    # Every racer gets the SAME non-empty key -- never None, never a private one.
    assert None not in results
    assert len(set(results)) == 1
    assert results[0] == (key_dir / _LEASE_FINGERPRINT_KEY_FILENAME).read_bytes().strip()
    # No temp files left behind. The publish lock's own file is expected: both
    # publication paths now serialize on it, so it is created on the way to the
    # key rather than being leftover scratch.
    leftover = sorted(
        p.name for p in key_dir.iterdir()
        if p.name != f"{_LEASE_FINGERPRINT_KEY_FILENAME}.lock"
    )
    assert leftover == [_LEASE_FINGERPRINT_KEY_FILENAME]


def test_lease_fingerprint_key_survives_a_mixed_link_and_replace_race(tmp_path, monkeypatch):
    """Regression test for "Avoid overwriting a concurrently published
    fingerprint key".

    ``os.link`` is no-clobber; ``os.replace`` is not. Only the replace path used
    to take the lock, so a process whose link failed could check ``_published()``,
    see nothing, and then have its replace overwrite a key a LINK-path process
    published in between. Any claim already fingerprinted under the overwritten
    key could never match the surviving one, and recovery refuses to release a
    lease whose account it cannot confirm -- the accumulator freezes permanently.

    The two paths are not as mutually exclusive as "the filesystem either
    supports hard links or it does not" suggests: a transient EIO/ENOSPC, an LSM
    denial hitting one process, or two mounts of the same tree with different
    link support all put one racer on each path. That mix is what this models.

    The interleaving is FORCED rather than raced for, so this fails
    deterministically against the old code instead of depending on scheduling.
    """
    import errno
    import threading

    import near_close_dca as ncd
    from near_close_dca import _LEASE_FINGERPRINT_KEY_FILENAME, _lease_fingerprint_key

    real_link = ncd.os.link
    real_replace = ncd.os.replace
    at_replace = threading.Event()
    release_replace = threading.Event()

    def _split_link(src, dst, **kwargs):
        """The replace-path thread's link fails transiently; the link-path
        thread's succeeds."""
        if threading.current_thread().name == "replace-path":
            raise OSError(errno.EIO, "transient I/O error")
        return real_link(src, dst, **kwargs)

    def _blocking_replace(src, dst, **kwargs):
        """Pin the replace-path thread at the exact moment after its
        _published() check saw nothing, and hold it there until the driver says
        go."""
        at_replace.set()
        release_replace.wait(timeout=10.0)
        return real_replace(src, dst, **kwargs)

    monkeypatch.setattr(ncd.os, "link", _split_link)
    monkeypatch.setattr(ncd.os, "replace", _blocking_replace)

    key_dir = tmp_path / "keys"
    results: dict[str, object] = {}

    def _publish():
        results[threading.current_thread().name] = _lease_fingerprint_key(key_dir)

    replace_thread = threading.Thread(target=_publish, name="replace-path")
    link_thread = threading.Thread(target=_publish, name="link-path")

    replace_thread.start()
    assert at_replace.wait(timeout=10.0), "replace path never reached os.replace"
    # Release the link path only now -- after the replace path is past its
    # _published() check. That ordering IS the bug.
    link_thread.start()
    # Let the link path RETURN before the replace lands. Returning is what makes
    # the clobber harmful: the caller goes on to fingerprint live claims with
    # the key it was handed, and _lease_fingerprint_key re-reads the file on
    # every call, so a clobber after that point orphans those claims forever.
    #
    # Under the OLD code the link path takes no lock, so it completes here.
    # Under the FIXED code it is waiting on the same lock the replace path
    # holds, so this join times out -- and the replace is released below, which
    # is what lets it finish.
    link_thread.join(timeout=2.0)
    release_replace.set()
    replace_thread.join(timeout=20.0)
    link_thread.join(timeout=20.0)
    assert not replace_thread.is_alive() and not link_thread.is_alive()

    # Every caller must still hold the key that is on disk at the end. The
    # clobber's signature is precisely a caller whose returned key stopped
    # matching the surviving file -- its already-fingerprinted leases could
    # never be confirmed again, and recovery refuses to release those.
    on_disk = (key_dir / _LEASE_FINGERPRINT_KEY_FILENAME).read_bytes().strip()
    assert None not in results.values()
    assert set(results.values()) == {on_disk}


def test_lease_fingerprint_key_falls_back_when_hard_links_are_unavailable(tmp_path, monkeypatch):
    """Regression test for "Provide a fallback when hard-link publication is
    unavailable".

    On a filesystem that does not support or permit hard links (a bind-mounted
    share, some container overlays, exFAT/FAT), ``os.link`` raises OSError, the
    outer handler returned ``_published()`` as None, and every live claim was
    persisted with ``settlement_account="unknown"`` -- which recovery then
    refuses to release, freezing the accumulator permanently on that host.
    """
    import errno
    import threading

    import near_close_dca as ncd
    from near_close_dca import _LEASE_FINGERPRINT_KEY_FILENAME, _lease_fingerprint_key

    def _no_hard_links(src, dst, **kwargs):
        raise OSError(errno.EPERM, "hard links not permitted")

    monkeypatch.setattr(ncd.os, "link", _no_hard_links)

    key_dir = tmp_path / "keys"
    results = []
    barrier = threading.Barrier(8)

    def _race():
        barrier.wait()
        results.append(_lease_fingerprint_key(key_dir))

    threads = [threading.Thread(target=_race) for _ in range(8)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    # The lock-serialized rename gives the same guarantee the link publish did:
    # a real key for everyone, and exactly ONE of them -- never None, so no
    # claim is written as settlement_account="unknown" on these hosts.
    assert None not in results
    assert len(set(results)) == 1
    assert results[0] == (key_dir / _LEASE_FINGERPRINT_KEY_FILENAME).read_bytes().strip()
    # No temp files left behind; the sidecar lock file is expected.
    leftovers = {p.name for p in key_dir.iterdir()} - {
        _LEASE_FINGERPRINT_KEY_FILENAME, f"{_LEASE_FINGERPRINT_KEY_FILENAME}.lock"
    }
    assert leftovers == set()


def test_lease_fingerprint_key_treats_undecodable_bytes_as_unavailable(tmp_path):
    """Regression test for "Treat an undecodable fingerprint key as
    unavailable". A partially restored or corrupted key file can contain
    non-UTF-8 bytes; ``Path.read_text()`` raises ``UnicodeDecodeError`` on
    those, which is NOT an ``OSError`` and previously propagated straight out
    of ``_lease_fingerprint_key`` (and therefore out of
    ``_broker_account_fingerprint``), aborting the entire scheduled run
    instead of failing closed to "record no account identity" like every
    other unusable-key case here.
    """
    from near_close_dca import _LEASE_FINGERPRINT_KEY_FILENAME, _lease_fingerprint_key

    key_dir = tmp_path / "keys"
    key_dir.mkdir(parents=True)
    (key_dir / _LEASE_FINGERPRINT_KEY_FILENAME).write_bytes(b"\xff\xfe\x00corrupted")

    # Must not raise. A corrupted key that already occupies the path can't
    # be overwritten by the ordinary "create if absent" publish path either
    # (that would reintroduce the exact empty-file race the atomic os.link
    # publish exists to close), so this fails closed to None -- "record no
    # account identity" -- exactly like every other unusable-key case here,
    # instead of aborting the caller with an uncaught UnicodeDecodeError.
    assert _lease_fingerprint_key(key_dir) is None


def test_lease_fingerprint_key_rejects_material_that_is_not_full_length_hex(tmp_path):
    """Regression test for "Validate persisted fingerprint key material
    before use". A partially restored or truncated key file can hold valid
    UTF-8 that is still not the 64-lowercase-hex shape ``secrets.token_hex(32)``
    always publishes -- a one-character or truncated hex string, say.
    Trusting it as-is keyed new fingerprints with far weaker material than
    the documented 256 bits, and made every existing lease's fingerprint
    permanently unmatchable once a correct key was eventually regenerated.
    """
    from near_close_dca import _LEASE_FINGERPRINT_KEY_FILENAME, _lease_fingerprint_key

    key_dir = tmp_path / "keys"
    key_dir.mkdir(parents=True)
    (key_dir / _LEASE_FINGERPRINT_KEY_FILENAME).write_text("ab", encoding="utf-8")

    # Must not accept the truncated material as the key -- same fail-closed
    # "record no account identity" outcome as every other unusable-key case.
    assert _lease_fingerprint_key(key_dir) is None


def test_lease_fingerprint_key_directory_is_fsynced_after_publishing(tmp_path, monkeypatch):
    """Regression test for "Fsync the key directory after publishing".

    ``os.link``/``os.replace`` are already atomic, so the key is never
    observed half-written -- but the directory ENTRY they create can still be
    lost on power loss before the parent directory's own metadata reaches
    stable storage. A next process that lost the entry would generate
    different key material, permanently unable to match the account
    fingerprint a pending lease already recorded.
    """
    import near_close_dca as ncd
    from near_close_dca import _lease_fingerprint_key

    fsynced_dirs = []
    real_fsync_directory = ncd._fsync_directory

    def _spy(path):
        fsynced_dirs.append(path)
        real_fsync_directory(path)

    monkeypatch.setattr(ncd, "_fsync_directory", _spy)

    key_dir = tmp_path / "keys"
    key = _lease_fingerprint_key(key_dir)

    assert key is not None
    assert fsynced_dirs == [key_dir]


def test_lease_fingerprint_key_directory_is_fsynced_on_the_fallback_path(tmp_path, monkeypatch):
    """The same durability guarantee applies to the lock-serialized os.replace
    fallback used when the filesystem does not support hard links."""
    import errno

    import near_close_dca as ncd
    from near_close_dca import _lease_fingerprint_key

    def _no_hard_links(src, dst, **kwargs):
        raise OSError(errno.EPERM, "hard links not permitted")

    monkeypatch.setattr(ncd.os, "link", _no_hard_links)

    fsynced_dirs = []
    real_fsync_directory = ncd._fsync_directory

    def _spy(path):
        fsynced_dirs.append(path)
        real_fsync_directory(path)

    monkeypatch.setattr(ncd, "_fsync_directory", _spy)

    key_dir = tmp_path / "keys"
    key = _lease_fingerprint_key(key_dir)

    assert key is not None
    assert fsynced_dirs == [key_dir]


def test_lease_fingerprint_key_adopts_a_publish_that_landed_before_the_lock(tmp_path, monkeypatch):
    """Regression test for "Avoid overwriting a concurrently published
    fingerprint key".

    The fallback branch only runs after an ``os.link`` call has failed for a
    non-EEXIST reason. That failure can be a one-off transient error rather than
    "this filesystem has no hard links at all" -- so a concurrent caller can
    still land a real ``os.link`` publish around the same time. The old fallback
    did a plain ``_published() is None`` check and then an UNCONDITIONAL
    ``os.replace``, a non-atomic check-then-act pair: a publish landing in that
    window was silently overwritten with different material, handing two callers
    two different keys for what must be one durable fingerprint secret.

    Both publication paths now take the same lock, so the only way another
    publish can exist by the time this one looks is if it completed BEFORE the
    lock was acquired -- which is exactly what this simulates. The winner's
    material must be adopted, never replaced.

    See test_lease_fingerprint_key_survives_a_mixed_link_and_replace_race for
    the concurrent, two-thread version that forces the interleaving itself.
    """
    import contextlib
    import errno

    import near_close_dca as ncd
    import scripts.file_lock as file_lock_module
    from near_close_dca import _LEASE_FINGERPRINT_KEY_FILENAME, _lease_fingerprint_key

    # Must be the real 64-lowercase-hex shape _lease_fingerprint_key
    # publishes and validates -- see "Validate persisted fingerprint key
    # material before use" -- or this concurrent-publisher simulation gets
    # rejected by that same validation instead of exercising the race.
    _CONCURRENT_PUBLISHER_MATERIAL = "c0" * 32

    key_dir = tmp_path / "keys"
    key_path = key_dir / _LEASE_FINGERPRINT_KEY_FILENAME

    def _transient_link_failure(src, dst, **kwargs):
        raise OSError(errno.EIO, "transient link failure")

    real_lock = file_lock_module.cross_process_lock

    @contextlib.contextmanager
    def _lock_that_loses_the_race(*args, **kwargs):
        """Acquire normally, but have a concurrent publisher win just before we
        do -- the one ordering that can still put another key on disk now that
        every publisher is serialized on this lock."""
        with real_lock(*args, **kwargs):
            key_path.write_text(_CONCURRENT_PUBLISHER_MATERIAL, encoding="utf-8")
            yield

    monkeypatch.setattr(ncd.os, "link", _transient_link_failure)
    monkeypatch.setattr(file_lock_module, "cross_process_lock", _lock_that_loses_the_race)

    key = _lease_fingerprint_key(key_dir)

    # The winner's material survives untouched, and is what this caller is
    # handed -- not this call's own (different) random material.
    assert key == _CONCURRENT_PUBLISHER_MATERIAL.encode("utf-8")
    assert key_path.read_text(encoding="utf-8") == _CONCURRENT_PUBLISHER_MATERIAL


def _write_morning_deployment_artifact(root, trading_day, order_id):
    """A real, VALID morning-stream scheduled run_state carrying one submitted order.

    Built from scripts.run_classification's own morning-slot constants rather
    than hand-written strings: validate_classification() requires the whole
    morning identity tuple to be coherent (cron id, window, run id, no
    afternoon lineage), and a block that fails it classifies as STREAM_OTHER --
    which every deployment predicate ignores, so the artifact would prove
    nothing about the stream filtering under test.
    """
    from scripts.run_classification import (
        MORNING_SLOT_CRON_ID,
        MORNING_SLOT_SCHEDULED_FOR_ET,
        MORNING_SLOT_SCHEDULED_WINDOW_END_ET,
        MORNING_SLOT_SCHEDULED_WINDOW_START_ET,
        MORNING_SLOT_STREAM,
        build_scheduled_classification,
        validate_classification,
    )

    day = trading_day.isoformat()
    generated_at = f"{day}T10:30:00Z"
    classification = build_scheduled_classification(
        day,
        generated_at,
        stream=MORNING_SLOT_STREAM,
        cron_id=MORNING_SLOT_CRON_ID,
        supersedes_cron_id=None,
        scheduled_for_et=MORNING_SLOT_SCHEDULED_FOR_ET,
        scheduled_window_start_et=MORNING_SLOT_SCHEDULED_WINDOW_START_ET,
        scheduled_window_end_et=MORNING_SLOT_SCHEDULED_WINDOW_END_ET,
        run_id=f"scheduled_{day.replace('-', '')}_1030ET",
        valid_afternoon_scheduled_evidence=False,
    )
    verdict = validate_classification(classification)
    assert verdict.ok, verdict

    run_dir = root / f"{day}-morning"
    run_dir.mkdir(parents=True, exist_ok=True)
    (run_dir / "run_state.json").write_text(
        json.dumps(
            {
                "run_id": classification["run_id"],
                "generated_at": generated_at,
                "trading_day": day,
                "broker": "robinhood",
                "budget": {"budget_tier": "normal", "daily_cap_usd": 180, "weekly_cap_usd": 900},
                "counts": {"submitted": 1},
                "run_classification": classification,
                "buy_candidates": [
                    {
                        "ticker": "AAPL", "buy_multiplier": 1.0, "planned_amount_usd": 7.0,
                        "status": "submitted", "order_id": order_id,
                    }
                ],
            }
        ),
        encoding="utf-8",
    )
    return run_dir


def test_recovery_idempotency_sees_a_morning_artifact_the_kill_switch_ignores(tmp_path):
    """Regression test for "Check every roster stream before rebooking a
    recovery".

    The order-identity predicate introduced for recovery idempotency filtered
    through `is_afternoon_scheduled_evidence`, which explicitly rejects morning
    and midday artifacts. A morning or midday worker that persisted BOTH
    `record_buy()` and its submitted-order artifact and then crashed before
    deferred settlement therefore looked un-booked to the afternoon recovery
    run, which recorded the same dollars and attempt a second time and
    republished the order, overstating the shared daily and weekly caps.

    "Has this order already been booked?" is a question about every stream;
    only "does it count toward the afternoon kill-switch total?" is
    afternoon-scoped. This pins both halves against a REAL on-disk artifact
    rather than a monkeypatched predicate.
    """
    from near_close_dca import (
        _same_day_order_deployment_already_recorded,
        _same_day_order_recorded_in_any_stream,
    )

    root = tmp_path / "runs"
    trading_day = TEST_DATES[1]
    _write_morning_deployment_artifact(root, trading_day, "MORNING-ORDER-1")
    wanted = {"MORNING-ORDER-1"}

    # Booking idempotency must SEE the morning artifact...
    assert _same_day_order_recorded_in_any_stream(wanted, trading_day, root) is True
    # ...while kill-switch accounting must still ignore it, because morning
    # capital is deliberately excluded from that stream.
    assert _same_day_order_deployment_already_recorded(wanted, trading_day, root) is False

    # An unrelated order id matches nothing under either question.
    assert _same_day_order_recorded_in_any_stream({"OTHER"}, trading_day, root) is False


def test_a_morning_claim_recovered_by_the_afternoon_stays_out_of_that_stream(tmp_path, monkeypatch):
    """Regression test for "Preserve the claiming stream when booking recovered
    spend".

    A lease outlives the run that claimed it, so the afternoon slot routinely
    recovers a MORNING claim. Deployment is accounted per stream and never
    conflated -- dca_risk's kill-switch figure counts only scheduled-afternoon
    capital, and `is_afternoon_scheduled_evidence` explicitly rejects morning
    and midday runs. Adding a morning claim's dollars to the runtime
    seven-day figure, and publishing its row inside an afternoon artifact, made
    every future `deployed_by_day()` scan read morning capital as afternoon
    deployment -- permanently, since the artifact is durable -- inflating the
    kill-switch total and spuriously capping later candidates.
    """
    monkeypatch.setenv("DCA_CRON_TRACKING_ROOT", str(tmp_path / "cron_tracking"))
    config = _base_config(inverse_martingale={"AAPL": PCT_ENROLLMENT})
    state = StrategyState()

    # Claim on the MORNING stream, then crash before placement.
    import near_close_dca as ncd

    _run_day(tmp_path, config, state, TEST_DATES[0], 95.0, "day1", stream="morning")
    monkeypatch.setattr(
        ncd,
        "_process_buy_candidates_batch",
        lambda **kwargs: (_ for _ in ()).throw(RuntimeError("simulated exit")),
    )
    with pytest.raises(RuntimeError, match="simulated exit"):
        _run_day(tmp_path, config, state, TEST_DATES[1], 96.5, "day2", stream="morning")
    monkeypatch.undo()
    monkeypatch.setenv("DCA_CRON_TRACKING_ROOT", str(tmp_path / "cron_tracking"))

    tracker_path = tmp_path / "cron_tracking" / "inverse_martingale_dca" / "AAPL" / "tracker.json"
    claim = json.loads(tracker_path.read_text())
    # The lease records where the money came from, so a later stream can tell.
    assert claim["settlement_stream"] == "morning"

    # The AFTERNOON slot recovers it the same day.
    baseline_7d = config.deployed_last_7d_usd
    broker = _HistoryBroker({"state": "filled", "executed_notional": "7.00"})
    result, run_state, _ = _run_day(
        tmp_path, config, state, TEST_DATES[1], 96.5, "day2-afternoon", broker=broker,
        stream="afternoon", deployment_history_root=tmp_path / "runs",
    )

    assert "settlement_pending" not in json.loads(tracker_path.read_text())
    # The shared daily/weekly budget IS charged -- those caps span the roster.
    assert state.day_notional(TEST_DATES[1]) == pytest.approx(7.0)
    # But the afternoon-only kill-switch figure is untouched, and no row lands
    # in this afternoon artifact to be re-read as afternoon deployment later.
    assert config.deployed_last_7d_usd == pytest.approx(baseline_7d)
    assert [
        r for r in run_state["buy_candidates"]
        if r["reason"] == "recovered_inverse_martingale_lease"
    ] == []
    # Regression assertion for "Persist claim metadata before suppressing
    # cross-stream recovery rows": since no run artifact carries this
    # recovery, the tracker itself must durably record which claim it
    # resolved -- the ledger's own generic step-event log carries no date,
    # stream, order id, or claim ref at all.
    last_recovery = json.loads(tracker_path.read_text())["last_recovery"]
    assert last_recovery["claim_trading_day"] == claim["settlement_trading_day"]
    assert last_recovery["claim_stream"] == "morning"
    assert last_recovery["claim_ref"] == claim["settlement_ref_id"]
    assert last_recovery["recovered_amount_usd"] == pytest.approx(7.0)
    assert last_recovery["reconciled_trading_day"] == TEST_DATES[1].isoformat()
    assert last_recovery["reconciled_stream"] == "afternoon"

    # Regression assertion for "Persist recovery attribution in the audit
    # event": the top-level last_recovery field above is overwritten
    # wholesale the next time this cycle resolves (fires, is abandoned, or
    # is reset) -- resolved_marker() carries no such field forward. The
    # SAME attribution must also live in the ledger's own append-only event
    # log, which survives that replacement.
    events = json.loads(tracker_path.read_text())["events"]
    recovery_events = [e for e in events if e.get("recovery_metadata")]
    assert len(recovery_events) == 1
    recovery_event_metadata = recovery_events[0]["recovery_metadata"]
    assert recovery_event_metadata == last_recovery


def test_a_same_day_zero_fill_recovery_still_publishes_an_attempt_row(tmp_path, monkeypatch):
    """Regression test for "Persist recovered zero-fill attempts in artifacts".

    The artifact loop filtered on notional, so a terminal cancelled/rejected
    order recovered on its own claim day was omitted from buy_candidates
    entirely. `record_buy(..., 0.0)` only protects the recovering invocation's
    in-memory count: every later roster slot reconstructs the shared
    max_orders_per_day attempt count from artifact rows, so a genuine broker
    attempt vanished from the record and a later slot could be granted an
    extra order.
    """
    monkeypatch.setenv("DCA_CRON_TRACKING_ROOT", str(tmp_path / "cron_tracking"))
    config = _base_config(inverse_martingale={"AAPL": PCT_ENROLLMENT})
    state = StrategyState()
    tracker_path, claim = _crash_a_fire_claim(tmp_path, monkeypatch, config, state)
    assert claim["settlement_claimed_usd"] == pytest.approx(7.0)

    # Recovered on the SAME day it was claimed -- a matched terminal row proves
    # submission happened and can never fill, so it resolves immediately.
    broker = _HistoryBroker(
        {"state": "cancelled", "dollar_amount": "7.00", "cumulative_quantity": "0", "average_price": "0"}
    )
    _, run_state, _ = _run_day(
        tmp_path, config, state, TEST_DATES[1], 96.5, "day2-recover", broker=broker
    )

    assert "settlement_pending" not in json.loads(tracker_path.read_text())
    recoveries = [
        r for r in run_state["buy_candidates"]
        if r["reason"] == "recovered_inverse_martingale_lease"
    ]
    # The attempt is preserved as a ZERO-notional row: a real order reached the
    # broker (so it carries its id) but bought nothing.
    assert len(recoveries) == 1
    assert recoveries[0]["ticker"] == "AAPL"
    assert float(recoveries[0]["planned_amount_usd"]) == pytest.approx(0.0)
    assert recoveries[0]["order_id"]


def test_a_recovery_already_recorded_on_disk_is_not_republished(tmp_path, monkeypatch):
    """Regression test for "Avoid republishing recovery spend already on disk".

    When the original worker persisted its submitted-order artifact but crashed
    before clearing the lease, `im_already_on_disk` correctly keeps the order
    out of StrategyState and the runtime seven-day total -- but the artifact
    publication was unconditional, putting a SECOND artifact carrying the same
    broker order on disk. `dca_risk.deployed_by_day` sums per artifact with no
    order-level deduplication, so the next invocation counted that one order
    twice against the kill-switch threshold and could suppress valid later
    purchases.
    """
    import near_close_dca as ncd

    monkeypatch.setenv("DCA_CRON_TRACKING_ROOT", str(tmp_path / "cron_tracking"))
    config = _base_config(inverse_martingale={"AAPL": PCT_ENROLLMENT})
    state = StrategyState()
    tracker_path, _ = _crash_a_fire_claim(tmp_path, monkeypatch, config, state)

    # The crashed worker's own artifact already records this exact order.
    monkeypatch.setattr(
        ncd, "_same_day_order_recorded_in_any_stream", lambda *a, **k: True
    )
    broker = _HistoryBroker({"state": "filled", "executed_notional": "7.00"})
    _, run_state, _ = _run_day(
        tmp_path, config, state, TEST_DATES[1], 96.5, "day2-recover", broker=broker,
        deployment_history_root=tmp_path / "runs",
    )

    # The lease is still resolved -- reconciliation is the point of the pass...
    assert "settlement_pending" not in json.loads(tracker_path.read_text())
    # ...but nothing is re-booked into state...
    assert state.day_notional(TEST_DATES[1]) == pytest.approx(0.0)
    # ...and no second artifact row carries the already-audited order.
    assert [
        r for r in run_state["buy_candidates"]
        if r["reason"] == "recovered_inverse_martingale_lease"
    ] == []


def test_recovery_is_logged_in_the_execution_record(tmp_path, monkeypatch):
    """Regression test for "Record recovered fills in the execution log".

    A recovered fire used to create only a candidate outcome, never an
    execution record -- so run_daily_compact's orders.csv (written solely
    from run_result.execution_records) omitted a real, broker-filled order
    that buy_candidates.csv/submitted_orders.csv reported as submitted,
    undercounting execution reporting.
    """
    monkeypatch.setenv("DCA_CRON_TRACKING_ROOT", str(tmp_path / "cron_tracking"))
    config = _base_config(inverse_martingale={"AAPL": PCT_ENROLLMENT})
    state = StrategyState()
    tracker_path, claim = _crash_a_fire_claim(tmp_path, monkeypatch, config, state)
    assert claim["settlement_claimed_usd"] == pytest.approx(7.0)

    broker = _HistoryBroker({"state": "filled", "executed_notional": "7.00"})
    result, run_state, _ = _run_day(
        tmp_path, config, state, TEST_DATES[1], 96.5, "day2-recover", broker=broker
    )
    assert "settlement_pending" not in json.loads(tracker_path.read_text())
    [recovery] = [
        r for r in run_state["buy_candidates"]
        if r["reason"] == "recovered_inverse_martingale_lease"
    ]

    with open(result["orders_csv_path"], encoding="utf-8") as handle:
        rows = list(csv.DictReader(handle))
    matching = [r for r in rows if r.get("order_id") == recovery["order_id"]]
    assert len(matching) == 1, "the recovered fill must be durably logged in orders.csv"
    assert float(matching[0]["notional_usd"]) == pytest.approx(7.0)


def test_recovery_terminal_zero_fill_does_not_fabricate_a_fill(tmp_path, monkeypatch):
    """Regression test for "Avoid treating requested quantity as a recovered
    fill". A terminal cancelled/rejected/expired order that omits
    cumulative_quantity carries its REQUESTED quantity (not executed) in
    plain "quantity". Combining that with average_price fabricated a fill
    that never happened -- marking a $0 recovery (correctly booked as such
    by _recovered_broker_notional) as "filled" with the entire requested
    notional, even though no shares were ever bought.
    """
    monkeypatch.setenv("DCA_CRON_TRACKING_ROOT", str(tmp_path / "cron_tracking"))
    config = _base_config(inverse_martingale={"AAPL": PCT_ENROLLMENT})
    state = StrategyState()
    tracker_path, claim = _crash_a_fire_claim(tmp_path, monkeypatch, config, state)
    assert claim["settlement_claimed_usd"] == pytest.approx(7.0)

    broker = _HistoryBroker(
        {
            "state": "cancelled",
            "dollar_amount": "7.00",
            "quantity": "0.0725389",  # requested, never executed
            "average_price": "96.50",
        }
    )
    result, run_state, _ = _run_day(
        tmp_path, config, state, TEST_DATES[1], 96.5, "day2-zero-fill", broker=broker
    )
    recovered = json.loads(tracker_path.read_text())
    assert "settlement_pending" not in recovered
    # Nothing executed, so nothing is booked...
    assert state.day_notional(TEST_DATES[1]) == pytest.approx(0.0)
    # ...and buy_candidates.csv correctly shows a zero-notional attempt, not a
    # fabricated purchase.
    [recovery] = [
        r for r in run_state["buy_candidates"]
        if r["reason"] == "recovered_inverse_martingale_lease"
    ]
    assert float(recovery["planned_amount_usd"]) == pytest.approx(0.0)

    with open(result["orders_csv_path"], encoding="utf-8") as handle:
        rows = list(csv.DictReader(handle))
    matching = [r for r in rows if r.get("order_id") == recovery["order_id"]]
    assert len(matching) == 1
    assert matching[0]["status"] != "filled"
    assert matching[0]["avg_fill_price"] in ("", "-")
    assert matching[0]["quantity_filled"] in ("", "-")


def test_orders_csv_preserves_recovered_fill_across_a_successful_retry(tmp_path, monkeypatch):
    """Regression test for "Preserve recovered fills through successful
    retries". run_daily_compact's own success-path write used to be an
    unconditional overwrite (write_execution_csv). A subsequent SUCCESSFUL
    run of the same scheduled slot -- a real retry, after an earlier failed
    attempt had persisted a recovery's row into orders.csv via
    _write_afternoon_failure_artifacts -- would silently truncate that row
    back out. Nothing will ever reproduce it: the recovery's lease is
    already durably cleared.
    """
    import near_close_dca as ncd

    monkeypatch.setenv("DCA_CRON_TRACKING_ROOT", str(tmp_path / "cron_tracking"))
    config = _base_config(inverse_martingale={"AAPL": PCT_ENROLLMENT})
    state = StrategyState()
    tracker_path, claim = _crash_a_fire_claim(tmp_path, monkeypatch, config, state)
    assert claim["settlement_claimed_usd"] == pytest.approx(7.0)

    reopened = _base_config(inverse_martingale={}, tickers=("AAPL", "MSFT"))
    broker = _HistoryBroker({"state": "filled", "executed_notional": "7.00"})

    original_process = ncd._process_buy_candidate

    def _boom_for_msft(*, decision, ctx, running_deployed_7d):
        if decision.ticker == "MSFT":
            raise RuntimeError("simulated failure for MSFT")
        return original_process(decision=decision, ctx=ctx, running_deployed_7d=running_deployed_7d)

    monkeypatch.setattr(ncd, "_process_buy_candidate", _boom_for_msft)
    with pytest.raises(RuntimeError, match="simulated failure for MSFT"):
        _run_day(tmp_path, reopened, state, TEST_DATES[1], 96.5, "day2-retry", broker=broker)
    monkeypatch.setattr(ncd, "_process_buy_candidate", original_process)

    orders_csv = tmp_path / "runs" / "day2-retry" / "orders.csv"
    with open(orders_csv, encoding="utf-8") as handle:
        rows_after_failure = list(csv.DictReader(handle))
    assert any(r.get("ticker") == "AAPL" for r in rows_after_failure)

    broker2 = DryRunBroker(cash=500.0, reference_prices={"AAPL": 96.5, "MSFT": 96.5})
    _run_day(tmp_path, reopened, state, TEST_DATES[1], 96.5, "day2-retry", broker=broker2)

    with open(orders_csv, encoding="utf-8") as handle:
        rows_after_retry = list(csv.DictReader(handle))
    assert any(r.get("ticker") == "AAPL" for r in rows_after_retry), (
        "the recovered AAPL fill must survive a later successful retry"
    )


def test_recovered_candidate_survives_a_successful_retry_in_run_state(tmp_path, monkeypatch):
    """Regression test for "Preserve recovered candidates through successful
    retries". After an earlier failed attempt at this slot already
    reconciled and cleared the crash lease, a later SUCCESSFUL retry's
    recovery-publish loop never re-evaluates it (there is nothing pending
    left to recover) -- so run_once()'s own fresh run_state.json, built
    solely from this run's own candidates, omits the recovered AAPL entry
    entirely, even though its execution row survives in orders.csv.
    dca_risk.deployed_usd() sums buy_candidates, not orders.csv, so the
    recovered spend would silently drop out of the kill-switch total.
    """
    import near_close_dca as ncd

    monkeypatch.setenv("DCA_CRON_TRACKING_ROOT", str(tmp_path / "cron_tracking"))
    config = _base_config(inverse_martingale={"AAPL": PCT_ENROLLMENT})
    state = StrategyState()
    tracker_path, claim = _crash_a_fire_claim(tmp_path, monkeypatch, config, state)
    assert claim["settlement_claimed_usd"] == pytest.approx(7.0)

    reopened = _base_config(inverse_martingale={}, tickers=("AAPL", "MSFT"))
    broker = _HistoryBroker({"state": "filled", "executed_notional": "7.00"})

    original_process = ncd._process_buy_candidate

    def _boom_for_msft(*, decision, ctx, running_deployed_7d):
        if decision.ticker == "MSFT":
            raise RuntimeError("simulated failure for MSFT")
        return original_process(decision=decision, ctx=ctx, running_deployed_7d=running_deployed_7d)

    monkeypatch.setattr(ncd, "_process_buy_candidate", _boom_for_msft)
    with pytest.raises(RuntimeError, match="simulated failure for MSFT"):
        _run_day(
            tmp_path, reopened, state, TEST_DATES[1], 96.5, "day2-retry-state", broker=broker
        )
    monkeypatch.setattr(ncd, "_process_buy_candidate", original_process)

    broker2 = DryRunBroker(cash=500.0, reference_prices={"AAPL": 96.5, "MSFT": 96.5})
    _, run_state_after_retry, _ = _run_day(
        tmp_path, reopened, state, TEST_DATES[1], 96.5, "day2-retry-state", broker=broker2
    )

    assert any(
        c.get("ticker") == "AAPL" and c.get("status") == "submitted"
        for c in run_state_after_retry["buy_candidates"]
    ), "the recovered AAPL candidate must survive into the retry's own run_state.json"

    # Regression for "Persist reinjected recoveries for the next retry": a
    # SECOND successful retry of the same slot must ALSO see the recovered
    # candidate -- it re-derives its own "prior recovered outcomes" from
    # broker_outcomes in the state the FIRST retry just wrote, so that write
    # must have persisted the outcome into broker_outcomes too, not just
    # buy_candidates.
    broker3 = DryRunBroker(cash=500.0, reference_prices={"AAPL": 96.5, "MSFT": 96.5})
    _, run_state_after_second_retry, _ = _run_day(
        tmp_path, reopened, state, TEST_DATES[1], 96.5, "day2-retry-state", broker=broker3
    )
    assert any(
        c.get("ticker") == "AAPL" and c.get("status") == "submitted"
        for c in run_state_after_second_retry["buy_candidates"]
    ), "the recovered AAPL candidate must also survive a SECOND successful retry"


def test_recovered_candidate_reaches_companion_artifacts_across_a_retry(tmp_path, monkeypatch):
    """Regression test for "Rebuild companion artifacts after reinjecting
    recoveries". Reinjecting a recovered candidate into a successful retry's
    run_state.json alone left buy_candidates.csv, submitted_orders.csv, and
    run_summary.json as run_once() originally wrote them -- without the
    recovery. A risk reader of run_state.json would count the recovered
    order while a CSV/report reader of the untouched companions would not
    see it at all.
    """
    import near_close_dca as ncd

    monkeypatch.setenv("DCA_CRON_TRACKING_ROOT", str(tmp_path / "cron_tracking"))
    config = _base_config(inverse_martingale={"AAPL": PCT_ENROLLMENT})
    state = StrategyState()
    tracker_path, claim = _crash_a_fire_claim(tmp_path, monkeypatch, config, state)
    assert claim["settlement_claimed_usd"] == pytest.approx(7.0)

    reopened = _base_config(inverse_martingale={}, tickers=("AAPL", "MSFT"))
    broker = _HistoryBroker({"state": "filled", "executed_notional": "7.00"})

    original_process = ncd._process_buy_candidate

    def _boom_for_msft(*, decision, ctx, running_deployed_7d):
        if decision.ticker == "MSFT":
            raise RuntimeError("simulated failure for MSFT")
        return original_process(decision=decision, ctx=ctx, running_deployed_7d=running_deployed_7d)

    monkeypatch.setattr(ncd, "_process_buy_candidate", _boom_for_msft)
    with pytest.raises(RuntimeError, match="simulated failure for MSFT"):
        _run_day(
            tmp_path, reopened, state, TEST_DATES[1], 96.5, "day2-retry-companions", broker=broker
        )
    monkeypatch.setattr(ncd, "_process_buy_candidate", original_process)

    broker2 = DryRunBroker(cash=500.0, reference_prices={"AAPL": 96.5, "MSFT": 96.5})
    _run_day(
        tmp_path, reopened, state, TEST_DATES[1], 96.5, "day2-retry-companions", broker=broker2
    )

    run_dir = tmp_path / "runs" / "day2-retry-companions"
    with open(run_dir / "buy_candidates.csv", encoding="utf-8") as handle:
        candidate_rows = list(csv.DictReader(handle))
    assert any(r.get("ticker") == "AAPL" for r in candidate_rows), (
        "buy_candidates.csv must reflect the reinjected recovery"
    )

    with open(run_dir / "submitted_orders.csv", encoding="utf-8") as handle:
        order_rows = list(csv.DictReader(handle))
    assert any(r.get("ticker") == "AAPL" for r in order_rows), (
        "submitted_orders.csv must reflect the reinjected recovery"
    )

    summary = json.loads((run_dir / "run_summary.json").read_text(encoding="utf-8"))
    assert summary["orders_submitted_count"] >= 1

    # Regression for "Refresh all derived counts after reinjection":
    # counts.buy_candidates and the usage-proxy's buy_candidates_written/
    # orders_placed_count must grow with the reinjected candidate too, not
    # just orders_submitted_count/counts.submitted.
    run_state = json.loads((run_dir / "run_state.json").read_text(encoding="utf-8"))
    assert run_state["counts"]["buy_candidates"] == len(run_state["buy_candidates"])
    assert run_state["usage_proxy"]["buy_candidates_written"] == len(run_state["buy_candidates"])
    assert run_state["usage_proxy"]["orders_placed_count"] >= 1


def test_reinjection_recomputes_classification_after_a_zero_order_retry(tmp_path, monkeypatch):
    """Regression test for "Recompute classification after reinjecting
    submissions". When the SUCCESSFUL retry itself submits nothing (no
    pending lease left to recover, no ordinary candidate qualifies),
    run_once() stamps run_classification_outcome="completed_no_orders" plus
    a no-order completion_reason. Reinjecting the PRIOR attempt's recovered
    submission afterward must clear that stale no-order classification --
    otherwise the state is simultaneously "submitted" (order_submit_status)
    and "completed with no orders" (run_classification_outcome).
    """
    import near_close_dca as ncd

    monkeypatch.setenv("DCA_CRON_TRACKING_ROOT", str(tmp_path / "cron_tracking"))
    config = _base_config(inverse_martingale={"AAPL": PCT_ENROLLMENT})
    state = StrategyState()
    tracker_path, claim = _crash_a_fire_claim(tmp_path, monkeypatch, config, state)
    assert claim["settlement_claimed_usd"] == pytest.approx(7.0)

    # First attempt AT THIS SAME directory: AAPL's pending lease resolves and
    # settles (recovered_inverse_martingale_lease row written), but MSFT's
    # own placement raises, failing the whole run and leaving that recovered
    # row in the failure artifact at "day2-reclass".
    reopened = _base_config(inverse_martingale={}, tickers=("AAPL", "MSFT"))
    broker = _HistoryBroker({"state": "filled", "executed_notional": "7.00"})

    original_process = ncd._process_buy_candidate

    def _boom_for_msft(*, decision, ctx, running_deployed_7d):
        if decision.ticker == "MSFT":
            raise RuntimeError("simulated failure for MSFT")
        return original_process(decision=decision, ctx=ctx, running_deployed_7d=running_deployed_7d)

    monkeypatch.setattr(ncd, "_process_buy_candidate", _boom_for_msft)
    with pytest.raises(RuntimeError, match="simulated failure for MSFT"):
        _run_day(
            tmp_path, reopened, state, TEST_DATES[1], 96.5, "day2-reclass", broker=broker
        )
    monkeypatch.setattr(ncd, "_process_buy_candidate", original_process)

    # Retry with AAPL as the ONLY ticker, unenrolled, and no price drop -- the
    # lease is already resolved (nothing pending to recover) and no ordinary
    # candidate qualifies, so run_once() itself submits ZERO orders and
    # stamps a no-order classification.
    retry_config = _base_config(inverse_martingale={}, tickers=("AAPL",))
    broker2 = DryRunBroker(cash=500.0, reference_prices={"AAPL": 100.0})
    _, run_state_after_retry, _ = _run_day(
        tmp_path, retry_config, state, TEST_DATES[1], 100.0, "day2-reclass", broker=broker2
    )

    assert any(
        c.get("ticker") == "AAPL" and c.get("status") == "submitted"
        for c in run_state_after_retry["buy_candidates"]
    ), "the recovered AAPL candidate must be reinjected into the retry's run_state.json"
    assert run_state_after_retry["orders_submitted_count"] >= 1
    assert run_state_after_retry["order_submit_status"] == "submitted"
    assert run_state_after_retry.get("run_classification_outcome") != "completed_no_orders"
    assert not run_state_after_retry.get("completion_reason")

    summary = json.loads(
        (tmp_path / "runs" / "day2-reclass" / "run_summary.json").read_text(encoding="utf-8")
    )
    assert summary.get("run_classification_outcome") != "completed_no_orders"


def test_a_malformed_prior_broker_outcomes_does_not_abort_a_successful_retry(tmp_path, monkeypatch):
    """Regression test for "Ignore malformed prior broker outcomes during
    retry". The reinjection block reads the PRIOR (failed) attempt's
    run_state.json to find any recovered lease that needs carrying forward
    into the successful retry's fresh state. If that prior state's
    broker_outcomes field is not a mapping -- e.g. corrupted into a list --
    an unconditional .items() call there must not raise AFTER run_once() has
    already completed and placed live orders: it should be treated as
    carrying no reinjectable recoveries, and the orders.csv merge/usage
    backfill that follow must still run.
    """
    import near_close_dca as ncd

    monkeypatch.setenv("DCA_CRON_TRACKING_ROOT", str(tmp_path / "cron_tracking"))
    config = _base_config(inverse_martingale={"AAPL": PCT_ENROLLMENT})
    state = StrategyState()
    tracker_path, claim = _crash_a_fire_claim(tmp_path, monkeypatch, config, state)
    assert claim["settlement_claimed_usd"] == pytest.approx(7.0)

    reopened = _base_config(inverse_martingale={}, tickers=("AAPL", "MSFT"))
    broker = _HistoryBroker({"state": "filled", "executed_notional": "7.00"})

    original_process = ncd._process_buy_candidate

    def _boom_for_msft(*, decision, ctx, running_deployed_7d):
        if decision.ticker == "MSFT":
            raise RuntimeError("simulated failure for MSFT")
        return original_process(decision=decision, ctx=ctx, running_deployed_7d=running_deployed_7d)

    monkeypatch.setattr(ncd, "_process_buy_candidate", _boom_for_msft)
    with pytest.raises(RuntimeError, match="simulated failure for MSFT"):
        _run_day(
            tmp_path, reopened, state, TEST_DATES[1], 96.5, "day2-malformed-prior", broker=broker
        )
    monkeypatch.setattr(ncd, "_process_buy_candidate", original_process)

    run_dir = tmp_path / "runs" / "day2-malformed-prior"
    prior_state = json.loads((run_dir / "run_state.json").read_text(encoding="utf-8"))
    prior_state["broker_outcomes"] = ["not", "a", "mapping"]
    (run_dir / "run_state.json").write_text(json.dumps(prior_state), encoding="utf-8")

    broker2 = DryRunBroker(cash=500.0, reference_prices={"AAPL": 96.5, "MSFT": 96.5})
    # Must not raise -- the malformed prior broker_outcomes must be ignored,
    # not crash the reinjection block after orders were already placed.
    _run_day(
        tmp_path, reopened, state, TEST_DATES[1], 96.5, "day2-malformed-prior", broker=broker2
    )

    with open(run_dir / "submitted_orders.csv", encoding="utf-8") as handle:
        order_rows = list(csv.DictReader(handle))
    assert any(r.get("ticker") == "MSFT" for r in order_rows), (
        "orders.csv merge/usage backfill after the reinjection block must "
        "still run despite the malformed prior state"
    )


def test_recovery_is_rebooked_when_already_on_disk_in_this_same_directory(tmp_path, monkeypatch):
    """Regression test for "Republish recoveries before overwriting their
    owning state". `im_recovery_already_on_disk` skipping the recovery's
    candidate-outcome booking is only safe when the on-disk copy lives in a
    DIFFERENT directory that survives untouched. When the on-disk copy is in
    THIS exact artifacts_dir (`_order_recorded_in_this_artifacts_dir` --
    im_recovery_on_disk_here), run_once is about to overwrite that
    directory's own run_state.json with a fresh one; skipping the rebooking
    there silently drops the recovery from the new state's buy_candidates,
    even though its execution row survives in orders.csv.
    dca_risk.deployed_usd() reads from buy_candidates, not orders.csv.
    """
    import near_close_dca as ncd

    monkeypatch.setenv("DCA_CRON_TRACKING_ROOT", str(tmp_path / "cron_tracking"))
    config = _base_config(inverse_martingale={"AAPL": PCT_ENROLLMENT})
    state = StrategyState()
    tracker_path, claim = _crash_a_fire_claim(tmp_path, monkeypatch, config, state)
    assert claim["settlement_claimed_usd"] == pytest.approx(7.0)
    order_id = claim["settlement_ref_id"]

    # Simulate an earlier crashed attempt at THIS exact slot having already
    # persisted a submitted row for this order in THIS directory --
    # _order_recorded_in_this_artifacts_dir reads exactly this shape.
    artifacts_dir = tmp_path / "runs" / "day2-recover-here"
    artifacts_dir.mkdir(parents=True)
    (artifacts_dir / "run_state.json").write_text(
        json.dumps(
            {
                "run_id": "earlier-attempt",
                "trading_day": TEST_DATES[1].isoformat(),
                "run_classification": {"stream": "afternoon"},
                "buy_candidates": [
                    {"ticker": "AAPL", "status": "submitted", "order_id": order_id}
                ],
                "broker_outcomes": {},
            }
        ),
        encoding="utf-8",
    )
    # Force im_already_on_disk=True regardless of directory -- the real
    # signal under test is narrowing that down to "on disk HERE" via
    # _order_recorded_in_this_artifacts_dir, exercised by the pre-seeded
    # state above.
    monkeypatch.setattr(ncd, "_same_day_order_recorded_in_any_stream", lambda *a, **k: True)

    broker = _HistoryBroker({"state": "filled", "executed_notional": "7.00"})
    _, run_state, _ = _run_day(
        tmp_path, config, state, TEST_DATES[1], 96.5, "day2-recover-here", broker=broker
    )
    assert "settlement_pending" not in json.loads(tracker_path.read_text())
    assert any(
        r["reason"] == "recovered_inverse_martingale_lease" and r.get("order_id") == order_id
        for r in run_state["buy_candidates"]
    ), "the recovery must still be booked into this directory's own new run_state.json"


def test_synthesized_failure_state_drops_stale_execution_rows(tmp_path):
    """Regression test for "Drop stale execution rows for synthesized failure
    states". When run_state.json is missing/unreadable (state_is_synthesized),
    every other companion artifact is fully REPLACED rather than merged with
    whatever a stale or foreign prior attempt left behind. orders.csv must
    get the same treatment -- merging it instead attributes that unrelated
    attempt's executions to the freshly synthesized state.
    """
    import near_close_dca as ncd

    artifacts_dir = tmp_path / "runs" / "stale"
    artifacts_dir.mkdir(parents=True)
    stale_csv = artifacts_dir / "orders.csv"
    stale_csv.write_text(
        "ticker,status,order_id,notional_usd,action_reason,target_price,"
        "target_price_source,target_quote_time,decision_bar_close,"
        "order_submitted_at,order_filled_at,avg_fill_price,quantity_filled,"
        "slippage_abs,slippage_pct,slippage_cost,latency_seconds,execution_quality\n"
        "MSFT,filled,stale-order,50.0,,,,,,,,,,,,,,\n",
        encoding="utf-8",
    )
    # run_state.json is absent entirely -> state_is_synthesized is True.

    new_record = ncd.build_execution_record(
        ticker="AAPL",
        target_price=0.0,
        target_price_source="",
        decision_bar_close=0.0,
        notional_usd=7.0,
        order_id="fresh-order",
    )

    ncd._write_afternoon_failure_artifacts(
        artifacts_dir,
        datetime(2026, 7, 1, 15, 45, tzinfo=EASTERN),
        RuntimeError("boom"),
        execution_records=[new_record],
    )

    with open(stale_csv, encoding="utf-8") as handle:
        rows = list(csv.DictReader(handle))
    assert [r["order_id"] for r in rows] == ["fresh-order"]


def test_synthesized_failure_state_removes_stale_execution_rows_even_when_empty(tmp_path):
    """Regression test for "Replace stale execution CSVs even when the new
    list is empty". When THIS failure invocation recorded no execution
    records of its own, the prior gate skipped calling the merge helper
    entirely -- leaving a stale orders.csv from an invalid/foreign prior
    attempt sitting there, still attributed to the freshly synthesized
    state.
    """
    import near_close_dca as ncd

    artifacts_dir = tmp_path / "runs" / "stale-empty"
    artifacts_dir.mkdir(parents=True)
    stale_csv = artifacts_dir / "orders.csv"
    stale_csv.write_text(
        "ticker,status,order_id,notional_usd,action_reason,target_price,"
        "target_price_source,target_quote_time,decision_bar_close,"
        "order_submitted_at,order_filled_at,avg_fill_price,quantity_filled,"
        "slippage_abs,slippage_pct,slippage_cost,latency_seconds,execution_quality\n"
        "MSFT,filled,stale-order,50.0,,,,,,,,,,,,,,\n",
        encoding="utf-8",
    )
    # run_state.json is absent entirely -> state_is_synthesized is True.

    ncd._write_afternoon_failure_artifacts(
        artifacts_dir,
        datetime(2026, 7, 1, 15, 45, tzinfo=EASTERN),
        RuntimeError("boom"),
        execution_records=None,
    )

    assert not stale_csv.exists(), "a stale orders.csv must not survive a synthesized failure state"


def test_afternoon_failure_replaces_execution_rows_when_the_prior_state_is_foreign(
    tmp_path,
):
    """Regression test for "Replace execution rows when the failure directory
    is foreign". A readable existing run_state.json from a DIFFERENT trading
    day or stream is not evidence of a genuine retry -- state_is_synthesized
    was only True for an unreadable file, so this failure path merged the
    new execution record into a foreign run's orders.csv, inflating that
    other run's metrics.
    """
    import near_close_dca as ncd

    artifacts_dir = tmp_path / "runs" / "reused-foreign"
    artifacts_dir.mkdir(parents=True)
    state_path = artifacts_dir / "run_state.json"
    state_path.write_text(
        json.dumps(
            {
                "run_id": "prior-run",
                "trading_day": "2026-06-29",
                "status": "failed",
                "run_classification": {"stream": "morning"},
                "broker_outcomes": {},
                "buy_candidates": [],
            }
        ),
        encoding="utf-8",
    )
    stale_csv = artifacts_dir / "orders.csv"
    stale_csv.write_text(
        "ticker,status,order_id,notional_usd,action_reason,target_price,"
        "target_price_source,target_quote_time,decision_bar_close,"
        "order_submitted_at,order_filled_at,avg_fill_price,quantity_filled,"
        "slippage_abs,slippage_pct,slippage_cost,latency_seconds,execution_quality\n"
        "MSFT,filled,foreign-order,50.0,,,,,,,,,,,,,,\n",
        encoding="utf-8",
    )

    new_record = ncd.build_execution_record(
        ticker="AAPL",
        target_price=0.0,
        target_price_source="",
        decision_bar_close=0.0,
        notional_usd=7.0,
        order_id="fresh-order",
    )

    # This invocation is for trading_day 2026-07-01, stream "afternoon" (the
    # default) -- neither matches the foreign prior state's 2026-06-29/morning.
    ncd._write_afternoon_failure_artifacts(
        artifacts_dir,
        datetime(2026, 7, 1, 15, 45, tzinfo=EASTERN),
        RuntimeError("boom"),
        execution_records=[new_record],
    )

    with open(stale_csv, encoding="utf-8") as handle:
        rows = list(csv.DictReader(handle))
    assert [r["order_id"] for r in rows] == ["fresh-order"]

    # Regression for "Replace the foreign run state along with its execution
    # log": a foreign run_state.json must be fully synthesized for THIS
    # invocation, not merely have its orders.csv swapped out -- otherwise the
    # artifact still carries the foreign trading_day/stream/run_id while its
    # execution log describes the current failure.
    written_state = json.loads(state_path.read_text())
    assert written_state["trading_day"] == "2026-07-01"
    assert written_state["run_classification"]["stream"] == "afternoon"
    assert written_state["run_id"] != "prior-run"


def test_afternoon_failure_artifacts_replaces_execution_log_for_a_foreign_stream_same_day(tmp_path):
    """Regression test for "Replace execution logs for foreign failure
    states". A PARSEABLE existing run_state.json is not necessarily this
    invocation's own -- artifacts_dir is caller-supplied and can be reused
    across streams/slots/dates. state_is_synthesized alone only catches a
    missing/unreadable file, so a foreign-but-valid run_state.json (a
    different stream, but the SAME trading_day) previously left the merge
    path free to combine its orders.csv with this run's own fills,
    misattributing them.
    """
    import near_close_dca as ncd

    artifacts_dir = tmp_path / "runs" / "foreign-same-day"
    artifacts_dir.mkdir(parents=True)
    # A parseable run_state.json belonging to a DIFFERENT stream than this
    # invocation (default "afternoon") on the SAME trading_day.
    (artifacts_dir / "run_state.json").write_text(
        json.dumps({
            "trading_day": "2026-07-01",
            "run_classification": {
                "stream": "morning", "manual_run": False, "scheduled_for_et": "10:30",
            },
        }),
        encoding="utf-8",
    )
    stale_csv = artifacts_dir / "orders.csv"
    stale_csv.write_text(
        "ticker,status,order_id,notional_usd,action_reason,target_price,"
        "target_price_source,target_quote_time,decision_bar_close,"
        "order_submitted_at,order_filled_at,avg_fill_price,quantity_filled,"
        "slippage_abs,slippage_pct,slippage_cost,latency_seconds,execution_quality\n"
        "MSFT,filled,foreign-order,50.0,,,,,,,,,,,,,,\n",
        encoding="utf-8",
    )

    new_record = ncd.build_execution_record(
        ticker="AAPL",
        target_price=0.0,
        target_price_source="",
        decision_bar_close=0.0,
        notional_usd=7.0,
        order_id="fresh-order",
    )

    ncd._write_afternoon_failure_artifacts(
        artifacts_dir,
        datetime(2026, 7, 1, 15, 45, tzinfo=EASTERN),
        RuntimeError("boom"),
        execution_records=[new_record],
    )

    with open(stale_csv, encoding="utf-8") as handle:
        rows = list(csv.DictReader(handle))
    assert [r["order_id"] for r in rows] == ["fresh-order"], (
        "the foreign stream's stale row must not survive alongside this run's own fill"
    )


def test_orders_csv_is_replaced_when_artifacts_dir_is_reused_for_a_different_day(
    tmp_path, monkeypatch
):
    """Regression test for "Merge executions only when retrying the same
    run". Reusing artifacts_dir for an UNRELATED trading day must not keep
    the previous day's execution rows -- they belong to a different run
    entirely, and merging them in would misattribute their fills/usage
    counts to this one.
    """
    monkeypatch.setenv("DCA_CRON_TRACKING_ROOT", str(tmp_path / "cron_tracking"))
    config = _base_config()
    state = StrategyState()

    result1, _run_state1, _ = _run_day(tmp_path, config, state, TEST_DATES[0], 90.0, "reused-dir")
    with open(result1["orders_csv_path"], encoding="utf-8") as handle:
        rows1 = list(csv.DictReader(handle))
    assert rows1

    state2 = StrategyState()
    result2, _run_state2, _ = _run_day(tmp_path, config, state2, TEST_DATES[1], 90.0, "reused-dir")
    with open(result2["orders_csv_path"], encoding="utf-8") as handle:
        rows2 = list(csv.DictReader(handle))
    assert rows2
    assert {r["order_id"] for r in rows1}.isdisjoint({r["order_id"] for r in rows2}), (
        "the earlier day's order must not survive into the reused directory's new run"
    )


def test_orders_csv_is_replaced_when_a_different_stream_reuses_the_same_day_dir(
    tmp_path, monkeypatch
):
    """Regression test for "Match the prior artifact's slot before merging
    executions". A trading_day match alone is not proof of a retry: two
    DIFFERENT scheduled slots on the SAME day (e.g. a stale morning artifact
    reused by a later midday run) sharing a directory must not be treated as
    a retry of each other just because the date matches -- that inflated
    usage/slippage reporting by attributing one slot's fills to another's
    run_state.
    """
    monkeypatch.setenv("DCA_CRON_TRACKING_ROOT", str(tmp_path / "cron_tracking"))
    config = _base_config()
    state = StrategyState()

    # classification_overrides mirrors `stream` here exactly as this repo's
    # own real call site (run_dca._slot_classification_overrides) always
    # does -- CompactRunOptions.stream alone does not stamp
    # run_classification.stream; see
    # test_orders_csv_survives_a_retry_when_stream_is_not_mirrored_into_classification
    # for the case where a caller does NOT keep the two in sync.
    result1, _run_state1, _ = _run_day(
        tmp_path, config, state, TEST_DATES[0], 90.0, "shared-dir", stream="morning",
        classification_overrides={"stream": "morning"},
    )
    with open(result1["orders_csv_path"], encoding="utf-8") as handle:
        rows1 = list(csv.DictReader(handle))
    assert rows1

    state2 = StrategyState()
    result2, _run_state2, _ = _run_day(
        tmp_path, config, state2, TEST_DATES[0], 90.0, "shared-dir", stream="midday",
        classification_overrides={"stream": "midday"},
    )
    with open(result2["orders_csv_path"], encoding="utf-8") as handle:
        rows2 = list(csv.DictReader(handle))
    assert rows2
    assert {r["order_id"] for r in rows1}.isdisjoint({r["order_id"] for r in rows2}), (
        "a different scheduled slot's order must not survive into another slot's run"
    )


def test_orders_csv_is_replaced_when_a_manual_run_reuses_a_scheduled_dir(
    tmp_path, monkeypatch
):
    """Regression test for "Separate manual runs from scheduled retries".
    A manual run (options.scheduled=False) invoked into a directory holding a
    same-day SCHEDULED afternoon artifact must not be treated as a retry of
    it just because both compare equal on trading_day/stream/scheduled_for_et
    -- build_run_artifacts stamps a manual run's classification with
    run_origin="manual" and neither stream nor scheduled_for_et set, so
    matching on those alone (with the shared afternoon/15:30 DEFAULTS) wiped
    that distinction.
    """
    monkeypatch.setenv("DCA_CRON_TRACKING_ROOT", str(tmp_path / "cron_tracking"))
    config = _base_config()
    state = StrategyState()

    result1, _run_state1, _ = _run_day(
        tmp_path, config, state, TEST_DATES[0], 90.0, "manual-vs-scheduled-dir",
        classification_overrides={"stream": "afternoon"},
    )
    with open(result1["orders_csv_path"], encoding="utf-8") as handle:
        rows1 = list(csv.DictReader(handle))
    assert rows1

    state2 = StrategyState()
    result2, run_state2, _ = _run_day(
        tmp_path, config, state2, TEST_DATES[0], 90.0, "manual-vs-scheduled-dir",
        scheduled=False,
    )
    assert run_state2["run_classification"]["run_origin"] == "manual"
    with open(result2["orders_csv_path"], encoding="utf-8") as handle:
        rows2 = list(csv.DictReader(handle))
    assert rows2
    assert {r["order_id"] for r in rows1}.isdisjoint({r["order_id"] for r in rows2}), (
        "a manual run must not be treated as a retry of a prior scheduled run "
        "sharing the same directory"
    )


def test_orders_csv_is_replaced_when_a_different_slot_shares_the_same_stream(
    tmp_path, monkeypatch
):
    """Regression test for "Include the scheduled slot when identifying
    retries". A shared stream is not proof of a retry either: the intraday
    stream in particular covers three distinct fire times (11:30/13:30/14:30
    ET) under one stream name (scripts/run_classification.py's
    INTRADAY_SLOT_IDENTITIES), distinguished only by scheduled_for_et/run_id.
    Comparing only trading_day+stream would treat a later slot reusing the
    directory as a retry of an earlier, unrelated slot.
    """
    monkeypatch.setenv("DCA_CRON_TRACKING_ROOT", str(tmp_path / "cron_tracking"))
    state = StrategyState()

    # Different tickers per slot -- the DryRunBroker's synthetic order id
    # does not encode scheduled_for_et, only the ticker/stream/day, so two
    # DIFFERENT fire times of the SAME stream would otherwise generate the
    # SAME order id and make an order-id-disjointness assertion meaningless.
    result1, _run_state1, _ = _run_day(
        tmp_path, _base_config(tickers=("AAPL",)), state, TEST_DATES[0], 90.0,
        "shared-slot-dir", stream="intraday",
        classification_overrides={"stream": "intraday", "scheduled_for_et": "11:30"},
    )
    with open(result1["orders_csv_path"], encoding="utf-8") as handle:
        rows1 = list(csv.DictReader(handle))
    assert rows1
    assert {r["ticker"] for r in rows1} == {"AAPL"}

    state2 = StrategyState()
    result2, _run_state2, _ = _run_day(
        tmp_path, _base_config(tickers=("MSFT",)), state2, TEST_DATES[0], 90.0,
        "shared-slot-dir", stream="intraday",
        classification_overrides={"stream": "intraday", "scheduled_for_et": "13:30"},
    )
    with open(result2["orders_csv_path"], encoding="utf-8") as handle:
        rows2 = list(csv.DictReader(handle))
    assert rows2
    assert {r["ticker"] for r in rows2} == {"MSFT"}, (
        "a different fire time sharing the same stream must not survive into "
        "another slot's run"
    )


def test_orders_csv_survives_a_retry_when_stream_is_not_mirrored_into_classification(
    tmp_path, monkeypatch
):
    """Regression test for "Compare retries against the persisted
    classification stream". CompactRunOptions.stream and
    classification_overrides["stream"] are two SEPARATE fields -- this
    repo's own real call site (run_dca._slot_classification_overrides) always
    keeps them in sync, but a caller of this public API that sets
    options.stream="midday" without also putting "stream": "midday" into
    classification_overrides gets a persisted run_classification.stream that
    still defaults to "afternoon". Comparing the prior run's persisted stream
    against options.stream directly then NEVER recognizes its own genuine
    retry, wiping an already-recovered execution row on every successful
    retry.
    """
    monkeypatch.setenv("DCA_CRON_TRACKING_ROOT", str(tmp_path / "cron_tracking"))
    config = _base_config()

    def _run(state, artifacts_label):
        now = datetime.combine(TEST_DATES[0], time(15, 45), tzinfo=EASTERN)
        fin = _FixedPriceData(prev_close=100.0, decision_price=90.0, source=SourceName.FINANCE)
        rh = _FixedPriceData(prev_close=100.0, decision_price=90.0, source=SourceName.ROBINHOOD)
        broker = DryRunBroker(cash=500.0, reference_prices={"AAPL": 90.0})
        return run_daily_compact(
            now,
            config,
            state,
            fin,
            rh,
            broker,
            options=CompactRunOptions(
                artifacts_dir=str(tmp_path / f"runs/{artifacts_label}"),
                scheduled=True,
                stream="midday",  # NOT mirrored into classification_overrides below.
            ),
        )

    state1 = StrategyState()
    result1 = _run(state1, "midday-not-mirrored")
    with open(result1["orders_csv_path"], encoding="utf-8") as handle:
        rows1 = list(csv.DictReader(handle))
    assert rows1

    state2 = StrategyState()
    result2 = _run(state2, "midday-not-mirrored")
    with open(result2["orders_csv_path"], encoding="utf-8") as handle:
        rows2 = list(csv.DictReader(handle))
    assert {r["order_id"] for r in rows1} <= {r["order_id"] for r in rows2}, (
        "a genuine retry at the same slot must preserve the earlier attempt's order"
    )


def test_merge_execution_csv_upgrades_a_submitted_row_to_filled(tmp_path):
    """Regression test for "Upgrade persisted submissions when recovery
    confirms a fill". An order logged as "submitted" (its fill not yet
    visible when it was first recorded, e.g. by a crashed attempt) must be
    upgraded IN PLACE once a later invocation's execution record confirms it
    filled -- not silently kept as "submitted" forever because the merge
    always preferred whichever row came first.
    """
    import near_close_dca as ncd

    path = tmp_path / "orders.csv"
    path.write_text(
        "ticker,status,order_id,notional_usd,action_reason,target_price,"
        "target_price_source,target_quote_time,decision_bar_close,"
        "order_submitted_at,order_filled_at,avg_fill_price,quantity_filled,"
        "slippage_abs,slippage_pct,slippage_cost,latency_seconds,execution_quality\n"
        "AAPL,submitted,order-1,7.0,,,,,,,,,,,,,,\n",
        encoding="utf-8",
    )
    filled_record = ncd.build_execution_record(
        ticker="AAPL",
        target_price=0.0,
        target_price_source="",
        decision_bar_close=0.0,
        notional_usd=7.0,
        order_id="order-1",
        avg_fill_price=96.5,
        quantity_filled=0.0725,
    )
    ncd._merge_execution_csv(path, [filled_record])

    with open(path, encoding="utf-8") as handle:
        rows = list(csv.DictReader(handle))
    assert len(rows) == 1
    assert rows[0]["status"] == "filled"
    assert float(rows[0]["avg_fill_price"]) == pytest.approx(96.5)


def test_merge_execution_csv_tolerates_extra_columns_in_existing_rows(tmp_path):
    """Regression test for "Normalize preserved CSV rows before rewriting
    them". An existing orders.csv carrying a column outside the current
    _CSV_FIELDS set (a newer schema, or a manually extended log) must not
    turn an otherwise successful merge into a ValueError.
    """
    import near_close_dca as ncd

    path = tmp_path / "orders.csv"
    path.write_text(
        "ticker,status,order_id,notional_usd,action_reason,target_price,"
        "target_price_source,target_quote_time,decision_bar_close,"
        "order_submitted_at,order_filled_at,avg_fill_price,quantity_filled,"
        "slippage_abs,slippage_pct,slippage_cost,latency_seconds,execution_quality,"
        "extra_future_column\n"
        "MSFT,filled,old-order,50.0,,,,,,,,,,,,,,,extra-value\n",
        encoding="utf-8",
    )
    new_record = ncd.build_execution_record(
        ticker="AAPL",
        target_price=0.0,
        target_price_source="",
        decision_bar_close=0.0,
        notional_usd=7.0,
        order_id="new-order",
    )
    ncd._merge_execution_csv(path, [new_record])  # must not raise

    with open(path, encoding="utf-8") as handle:
        rows = list(csv.DictReader(handle))
    assert {r["order_id"] for r in rows} == {"old-order", "new-order"}


def test_merge_execution_csv_replaces_a_stale_empty_log_when_always_write_is_set(tmp_path):
    """Regression test for "Honor always_write when replacing an empty
    execution log" / "Preserve the header-only execution artifact after
    replacement". replace_existing=True with no new records and an
    existing stale file (run_daily_compact's own call when a directory is
    reused by an unrelated run that submitted nothing) must not just unlink
    the stale file and stop -- always_write=True promises callers a
    (possibly header-only) file always exists at this path, matching
    write_execution_csv's prior unconditional-create contract. Leaving it
    deleted breaks a caller that already advertised orders_csv_path in its
    result dict.
    """
    import near_close_dca as ncd

    path = tmp_path / "orders.csv"
    path.write_text(
        "ticker,status,order_id,notional_usd,action_reason,target_price,"
        "target_price_source,target_quote_time,decision_bar_close,"
        "order_submitted_at,order_filled_at,avg_fill_price,quantity_filled,"
        "slippage_abs,slippage_pct,slippage_cost,latency_seconds,execution_quality\n"
        "MSFT,filled,stale-order,50.0,,,,,,,,,,,,,,\n",
        encoding="utf-8",
    )
    ncd._merge_execution_csv(path, [], replace_existing=True, always_write=True)

    assert path.exists(), "always_write=True must leave a file behind, not just delete the stale one"
    with open(path, encoding="utf-8") as handle:
        rows = list(csv.DictReader(handle))
    assert rows == [], "the stale row must not survive the replace"


def test_merge_execution_csv_persists_new_records_when_the_old_file_is_unreadable(tmp_path):
    """Regression test for "Persist new executions when an old CSV is
    unreadable" / "Do not drop new executions when the old CSV is
    unreadable". A genuine same-slot retry (replace_existing=False) whose
    own orders.csv is truncated/corrupted has nothing salvageable to merge
    with, but this run's own new records are still real, accepted orders --
    discarding them (the previous behavior) left the damaged file in place
    with the retry's fills silently absent from execution reporting, even
    though the caller reports the run as complete.
    """
    import near_close_dca as ncd

    path = tmp_path / "orders.csv"
    path.write_bytes(b"\xff\xfe not utf-8 \xff")

    new_record = ncd.build_execution_record(
        ticker="AAPL",
        target_price=0.0,
        target_price_source="",
        decision_bar_close=0.0,
        notional_usd=7.0,
        order_id="new-order",
    )

    ncd._merge_execution_csv(path, [new_record])  # replace_existing=False: a same-slot retry

    with open(path, encoding="utf-8") as handle:
        rows = list(csv.DictReader(handle))
    assert {r["order_id"] for r in rows} == {"new-order"}, (
        "this run's own new record must survive even though the old file was unreadable"
    )


def test_merge_execution_csv_preserves_target_metadata_when_upgrading(tmp_path):
    """Regression test for "Preserve target metadata when upgrading
    execution rows". Replacing an existing row wholesale with a recovery's
    execution record (target_price=0.0, no real submission metadata)
    discarded the original placement's real target price/source/decision
    close/submission time, leaving a "filled" row with no computable
    slippage.
    """
    import near_close_dca as ncd

    path = tmp_path / "orders.csv"
    path.write_text(
        "ticker,status,order_id,notional_usd,action_reason,target_price,"
        "target_price_source,target_quote_time,decision_bar_close,"
        "order_submitted_at,order_filled_at,avg_fill_price,quantity_filled,"
        "slippage_abs,slippage_pct,slippage_cost,latency_seconds,execution_quality\n"
        "AAPL,submitted,order-1,7.0,inverse_martingale_fire,100.0,"
        "finance:signal_quote,2026-07-01T15:30:00+00:00,100.0,"
        "2026-07-01T15:44:00+00:00,,,,,,,,submitted\n",
        encoding="utf-8",
    )
    recovery_record = ncd.build_execution_record(
        ticker="AAPL",
        target_price=0.0,
        target_price_source="",
        decision_bar_close=0.0,
        notional_usd=7.0,
        order_id="order-1",
        avg_fill_price=96.5,
        quantity_filled=0.0725,
    )
    ncd._merge_execution_csv(path, [recovery_record])

    with open(path, encoding="utf-8") as handle:
        [row] = list(csv.DictReader(handle))
    assert row["status"] == "filled"
    assert float(row["target_price"]) == pytest.approx(100.0)
    assert row["target_price_source"] == "finance:signal_quote"
    assert row["action_reason"] == "inverse_martingale_fire"
    assert float(row["avg_fill_price"]) == pytest.approx(96.5)
    assert float(row["slippage_abs"]) == pytest.approx(96.5 - 100.0)
    assert row["execution_quality"] != "unfilled"


def test_merge_execution_csv_upgrades_a_partial_row_from_a_notional_only_recovery(tmp_path):
    """Regression test for "Upgrade partial rows from notional-only terminal
    recovery". A terminal recovery valued only through executed_notional
    (force_filled, no avg_fill_price/quantity_filled breakdown) reports
    _row_fill_quantity()==0, so the plain quantity comparison alone never
    lets its authoritative terminal status/notional win over a stale
    positive-quantity partial fill logged earlier.
    """
    import near_close_dca as ncd

    path = tmp_path / "orders.csv"
    path.write_text(
        "ticker,status,order_id,notional_usd,action_reason,target_price,"
        "target_price_source,target_quote_time,decision_bar_close,"
        "order_submitted_at,order_filled_at,avg_fill_price,quantity_filled,"
        "slippage_abs,slippage_pct,slippage_cost,latency_seconds,execution_quality\n"
        "AAPL,filled,order-1,7.0,inverse_martingale_fire,100.0,"
        "finance:signal_quote,2026-07-01T15:30:00+00:00,100.0,"
        "2026-07-01T15:44:00+00:00,2026-07-01T15:44:30+00:00,96.5,0.01,"
        "-3.5,-3.5,-0.035,30,fair\n",
        encoding="utf-8",
    )
    recovery_record = ncd.build_execution_record(
        ticker="AAPL",
        target_price=0.0,
        target_price_source="",
        decision_bar_close=0.0,
        notional_usd=7.0,
        order_id="order-1",
        force_filled=True,
    )
    ncd._merge_execution_csv(path, [recovery_record])

    with open(path, encoding="utf-8") as handle:
        [row] = list(csv.DictReader(handle))
    assert row["status"] == "filled"
    assert float(row["notional_usd"]) == pytest.approx(7.0)
    # The existing row's STALE partial quantity/price must NOT survive as if
    # they were this terminal notional-only confirmation's own complete
    # breakdown (Codex review: "Clear stale granular data on notional-only
    # upgrades") -- a downstream consumer reading both fields as present
    # would otherwise treat the old partial as the complete fill.
    assert row["quantity_filled"] in ("", "-")
    assert row["avg_fill_price"] in ("", "-")
    # Regression for "Preserve the recovery marker through CSV upgrades":
    # the existing row's original action_reason ("inverse_martingale_fire")
    # must not survive the notional-only upgrade -- run_dca.
    # _refresh_fills_from_broker() identifies a terminal notional-only
    # recovery by action_reason == "recovered_inverse_martingale_lease" to
    # skip re-querying it, and would otherwise fail to recognize this
    # merged row and accept a stale follow-up lookup as its terminal detail.
    assert row["action_reason"] == "recovered_inverse_martingale_lease"
    # Regression for "Clear stale partial timestamps during CSV recovery
    # upgrades": the existing row's order_filled_at (2026-07-01T15:44:30+00:00,
    # from the earlier premature partial-fill log) must not survive either --
    # the recovery record here supplies no order_filled_at of its own, so
    # keeping the stale one would let latency_seconds be recomputed for a
    # fill this row no longer even claims a quantity for.
    assert row["order_filled_at"] in ("", "-")
    assert row["latency_seconds"] in ("", "-")


def test_merge_execution_csv_preserves_a_complete_fill_from_a_notional_only_recovery(tmp_path):
    """Regression test for "Preserve confirmed granular fills during notional
    recovery". When the existing row's own avg_fill_price * quantity_filled
    already accounts for the terminal recovery's executed_notional -- i.e. it
    is a genuine, previously logged COMPLETE fill, not a stale premature
    partial -- a later notional-only confirmation of the same order must not
    wipe that real granular breakdown down to blank just because the
    recovery itself carries no avg_fill_price/quantity_filled of its own.
    """
    import near_close_dca as ncd

    path = tmp_path / "orders.csv"
    path.write_text(
        "ticker,status,order_id,notional_usd,action_reason,target_price,"
        "target_price_source,target_quote_time,decision_bar_close,"
        "order_submitted_at,order_filled_at,avg_fill_price,quantity_filled,"
        "slippage_abs,slippage_pct,slippage_cost,latency_seconds,execution_quality\n"
        "AAPL,filled,order-1,7.0,inverse_martingale_fire,100.0,"
        "finance:signal_quote,2026-07-01T15:30:00+00:00,100.0,"
        "2026-07-01T15:44:00+00:00,2026-07-01T15:44:30+00:00,100.0,0.07,"
        "0.0,0.0,0.0,30,fair\n",
        encoding="utf-8",
    )
    recovery_record = ncd.build_execution_record(
        ticker="AAPL",
        target_price=0.0,
        target_price_source="",
        decision_bar_close=0.0,
        notional_usd=7.0,
        order_id="order-1",
        force_filled=True,
    )
    ncd._merge_execution_csv(path, [recovery_record])

    with open(path, encoding="utf-8") as handle:
        [row] = list(csv.DictReader(handle))
    assert row["status"] == "filled"
    assert float(row["notional_usd"]) == pytest.approx(7.0)
    # The existing row's real, already-complete fill breakdown (0.07 shares
    # at $100.0 == the recovery's own $7.0 executed notional) must survive
    # untouched -- it is not a stale partial this notional-only confirmation
    # is upgrading.
    assert float(row["quantity_filled"]) == pytest.approx(0.07)
    assert float(row["avg_fill_price"]) == pytest.approx(100.0)
    assert row["order_filled_at"] == "2026-07-01T15:44:30+00:00"
    assert row["action_reason"] == "inverse_martingale_fire"


def test_merge_execution_csv_tolerates_mixed_timezone_awareness_timestamps(tmp_path):
    """Regression test for "Handle mixed timezone awareness while merging
    timestamps". An existing row's naive submission timestamp merged with a
    recovery's timezone-aware fill timestamp parses fine on both sides, but
    subtracting an aware datetime from a naive one raises TypeError, not
    ValueError -- aborting the merge (and losing the recovered fill) instead
    of just leaving latency_seconds unavailable.
    """
    import near_close_dca as ncd

    path = tmp_path / "orders.csv"
    path.write_text(
        "ticker,status,order_id,notional_usd,action_reason,target_price,"
        "target_price_source,target_quote_time,decision_bar_close,"
        "order_submitted_at,order_filled_at,avg_fill_price,quantity_filled,"
        "slippage_abs,slippage_pct,slippage_cost,latency_seconds,execution_quality\n"
        "AAPL,submitted,order-1,7.0,inverse_martingale_fire,100.0,"
        "finance:signal_quote,2026-07-01T15:30:00+00:00,100.0,"
        "2026-07-01T15:44:00,,,,,,,,submitted\n",
        encoding="utf-8",
    )
    recovery_record = ncd.build_execution_record(
        ticker="AAPL",
        target_price=0.0,
        target_price_source="",
        decision_bar_close=0.0,
        notional_usd=7.0,
        order_id="order-1",
        avg_fill_price=96.5,
        quantity_filled=0.0725,
        order_filled_at=datetime.fromisoformat("2026-07-01T15:45:00+00:00"),
    )
    ncd._merge_execution_csv(path, [recovery_record])

    with open(path, encoding="utf-8") as handle:
        [row] = list(csv.DictReader(handle))
    assert row["status"] == "filled"
    assert float(row["avg_fill_price"]) == pytest.approx(96.5)
    assert row["latency_seconds"] == ""


def test_get_order_fill_does_not_fabricate_a_cancelled_orders_quantity(tmp_path):
    """Regression test for "Do not turn confirmed zero-fill orders into
    fills". A cancelled order that omits cumulative_quantity but carries its
    REQUESTED quantity and an average_price must not be read back as a fill
    -- "quantity" there is what was requested, not what executed.
    """
    from dca_live_adapters import RobinhoodBroker

    def orders_fn(**kwargs):
        return {
            "data": {
                "order": {
                    "id": "cancelled-order",
                    "state": "cancelled",
                    "quantity": "0.0725",
                    "average_price": "96.50",
                }
            }
        }

    broker = RobinhoodBroker(order_fill_fn=orders_fn)
    assert broker.get_order_fill("cancelled-order") is None


def test_recovery_execution_row_keeps_history_fill_details_without_a_second_lookup(
    tmp_path, monkeypatch
):
    """Regression test for "Preserve known fill details during recovery
    logging". The broker's optional get_order_fill() can return None (unwired,
    or a transient follow-up failure) even though the SAME history lookup that
    reconciled the lease already established a terminal fill's price and
    quantity. Relying solely on that second, independent call left the
    execution row with no price/quantity at all -- undercounting
    filled_order_count/total_filled_notional for a fill history had already
    proven. _HistoryBroker (a bare DryRunBroker) never populates its own
    _fills map for a synthetic recovered order, so get_order_fill() returns
    None here exactly like the unwired/failed case.
    """
    monkeypatch.setenv("DCA_CRON_TRACKING_ROOT", str(tmp_path / "cron_tracking"))
    config = _base_config(inverse_martingale={"AAPL": PCT_ENROLLMENT})
    state = StrategyState()
    tracker_path, claim = _crash_a_fire_claim(tmp_path, monkeypatch, config, state)
    assert claim["settlement_claimed_usd"] == pytest.approx(7.0)

    broker = _HistoryBroker(
        {
            "state": "filled",
            "executed_notional": "7.00",
            "average_price": "96.50",
            "cumulative_quantity": "0.0725389",
        }
    )
    assert broker.get_order_fill("anything") is None  # the scenario under test

    result, run_state, _ = _run_day(
        tmp_path, config, state, TEST_DATES[1], 96.5, "day2-recover", broker=broker
    )
    assert "settlement_pending" not in json.loads(tracker_path.read_text())
    [recovery] = [
        r for r in run_state["buy_candidates"]
        if r["reason"] == "recovered_inverse_martingale_lease"
    ]

    with open(result["orders_csv_path"], encoding="utf-8") as handle:
        rows = list(csv.DictReader(handle))
    [row] = [r for r in rows if r.get("order_id") == recovery["order_id"]]
    assert float(row["avg_fill_price"]) == pytest.approx(96.50)
    assert float(row["quantity_filled"]) == pytest.approx(0.0725389)


def test_recovery_prefers_the_terminal_history_quantity_over_a_stale_follow_up(
    tmp_path, monkeypatch
):
    """Regression test for "Prefer the terminal recovery quantity over the
    follow-up lookup". get_order_fill() can be eventually consistent: it can
    return a real (non-None) OrderFill that still reflects an earlier PARTIAL
    fill, even though the SAME history lookup that reconciled the lease
    already established the TERMINAL quantity. Preferring that stale
    follow-up over the retained terminal value left the execution row
    permanently partial.
    """
    import near_close_dca as ncd

    class _StalePartialFollowUpBroker(_HistoryBroker):
        def get_order_fill(self, order_id):
            return ncd.OrderFill(avg_fill_price=96.5, quantity_filled=0.01)

    monkeypatch.setenv("DCA_CRON_TRACKING_ROOT", str(tmp_path / "cron_tracking"))
    config = _base_config(inverse_martingale={"AAPL": PCT_ENROLLMENT})
    state = StrategyState()
    tracker_path, claim = _crash_a_fire_claim(tmp_path, monkeypatch, config, state)
    assert claim["settlement_claimed_usd"] == pytest.approx(7.0)

    broker = _StalePartialFollowUpBroker(
        {
            "state": "filled",
            "executed_notional": "7.00",
            "average_price": "96.50",
            "cumulative_quantity": "0.0725389",
        }
    )

    result, run_state, _ = _run_day(
        tmp_path, config, state, TEST_DATES[1], 96.5, "day2-recover-stale", broker=broker
    )
    assert "settlement_pending" not in json.loads(tracker_path.read_text())
    [recovery] = [
        r for r in run_state["buy_candidates"]
        if r["reason"] == "recovered_inverse_martingale_lease"
    ]

    with open(result["orders_csv_path"], encoding="utf-8") as handle:
        rows = list(csv.DictReader(handle))
    [row] = [r for r in rows if r.get("order_id") == recovery["order_id"]]
    # The terminal history quantity (0.0725389), not the stale follow-up
    # lookup's partial quantity (0.01).
    assert float(row["quantity_filled"]) == pytest.approx(0.0725389)


def test_recovery_rejects_stale_partial_timestamps_that_disagree_with_terminal_quantity(
    tmp_path, monkeypatch
):
    """Regression test for "Reject stale partial timestamps during terminal
    recovery". get_order_fill() can return a real OrderFill reflecting an
    EARLIER PARTIAL fill even though the terminal history quantity is
    already known and preferred over its price/quantity. Copying that
    lookup's timestamps unconditionally stamped the partial transaction's
    submit/fill time onto the terminal execution record, corrupting
    latency_seconds for a fill it never actually timed. The timestamps must
    only be accepted when the lookup's own quantity agrees with the
    quantity actually reported.
    """
    import near_close_dca as ncd

    stale_submitted_at = datetime(2026, 6, 29, 15, 30, tzinfo=EASTERN)
    stale_filled_at = datetime(2026, 6, 29, 15, 30, 5, tzinfo=EASTERN)

    class _StalePartialWithTimestampsBroker(_HistoryBroker):
        def get_order_fill(self, order_id):
            return ncd.OrderFill(
                avg_fill_price=96.5,
                quantity_filled=0.01,
                order_submitted_at=stale_submitted_at,
                order_filled_at=stale_filled_at,
            )

    monkeypatch.setenv("DCA_CRON_TRACKING_ROOT", str(tmp_path / "cron_tracking"))
    config = _base_config(inverse_martingale={"AAPL": PCT_ENROLLMENT})
    state = StrategyState()
    tracker_path, claim = _crash_a_fire_claim(tmp_path, monkeypatch, config, state)
    assert claim["settlement_claimed_usd"] == pytest.approx(7.0)

    broker = _StalePartialWithTimestampsBroker(
        {
            "state": "filled",
            "executed_notional": "7.00",
            "average_price": "96.50",
            "cumulative_quantity": "0.0725389",
        }
    )

    result, run_state, _ = _run_day(
        tmp_path, config, state, TEST_DATES[1], 96.5, "day2-recover-stale-ts", broker=broker
    )
    assert "settlement_pending" not in json.loads(tracker_path.read_text())
    [recovery] = [
        r for r in run_state["buy_candidates"]
        if r["reason"] == "recovered_inverse_martingale_lease"
    ]

    with open(result["orders_csv_path"], encoding="utf-8") as handle:
        rows = list(csv.DictReader(handle))
    [row] = [r for r in rows if r.get("order_id") == recovery["order_id"]]
    # The terminal history quantity, not the stale follow-up's partial quantity.
    assert float(row["quantity_filled"]) == pytest.approx(0.0725389)
    # The stale lookup's timestamps -- which disagree with the reported
    # quantity -- must not appear.
    assert row["order_submitted_at"] in ("", "-")
    assert row["order_filled_at"] in ("", "-")


def test_recovery_rejects_partial_timestamps_for_a_notional_only_recovery(tmp_path, monkeypatch):
    """Regression test for "Reject partial timestamps for notional-only
    recoveries". When terminal history confirms a fill only through
    executed_notional (im_recovery.fill_price/fill_quantity both None), the
    prior timestamp-agreement fix's `is None` branch accepted timestamps from
    ANY follow-up lookup unconditionally -- including one that returns a
    real POSITIVE-quantity partial fill for the same order. The lookup's own
    quantity must be equally absent (0/None) for its timestamps to be
    accepted; a positive quantity means it describes a real transaction this
    notional-only record isn't reporting.
    """
    import near_close_dca as ncd

    partial_submitted_at = datetime(2026, 6, 29, 15, 30, tzinfo=EASTERN)
    partial_filled_at = datetime(2026, 6, 29, 15, 30, 5, tzinfo=EASTERN)

    class _PositiveQuantityPartialBroker(_HistoryBroker):
        def get_order_fill(self, order_id):
            return ncd.OrderFill(
                avg_fill_price=50.0,
                quantity_filled=0.001,
                order_submitted_at=partial_submitted_at,
                order_filled_at=partial_filled_at,
            )

    monkeypatch.setenv("DCA_CRON_TRACKING_ROOT", str(tmp_path / "cron_tracking"))
    config = _base_config(inverse_martingale={"AAPL": PCT_ENROLLMENT})
    state = StrategyState()
    tracker_path, claim = _crash_a_fire_claim(tmp_path, monkeypatch, config, state)
    assert claim["settlement_claimed_usd"] == pytest.approx(7.0)

    # executed_notional only -- no cumulative_quantity/average_price breakdown,
    # so im_recovery.fill_price/fill_quantity are both None.
    broker = _PositiveQuantityPartialBroker({"state": "filled", "executed_notional": "7.00"})

    result, run_state, _ = _run_day(
        tmp_path, config, state, TEST_DATES[1], 96.5, "day2-recover-notional-ts", broker=broker
    )
    assert "settlement_pending" not in json.loads(tracker_path.read_text())
    [recovery] = [
        r for r in run_state["buy_candidates"]
        if r["reason"] == "recovered_inverse_martingale_lease"
    ]

    with open(result["orders_csv_path"], encoding="utf-8") as handle:
        rows = list(csv.DictReader(handle))
    [row] = [r for r in rows if r.get("order_id") == recovery["order_id"]]
    assert row["status"] == "filled"
    assert float(row["notional_usd"]) == pytest.approx(7.0)
    # The positive-quantity partial lookup's price/quantity must not appear ...
    assert row["avg_fill_price"] in ("", "-")
    assert row["quantity_filled"] in ("", "-")
    # ... and neither must its timestamps.
    assert row["order_submitted_at"] in ("", "-")
    assert row["order_filled_at"] in ("", "-")


def test_recovery_logging_survives_a_raising_fill_lookup(tmp_path, monkeypatch):
    """Regression test for "Isolate the optional fill lookup from recovery
    settlement". A get_order_fill() call that raises (e.g. a transient
    broker-adapter outage) must not abort the recovery-publish loop after the
    lease was already durably claim_fire()'d clear -- the recovered outcome
    and execution record must still be published from the retained history
    values.
    """

    class _RaisingFollowUpBroker(_HistoryBroker):
        def get_order_fill(self, order_id):
            raise RuntimeError("simulated transient broker outage")

    monkeypatch.setenv("DCA_CRON_TRACKING_ROOT", str(tmp_path / "cron_tracking"))
    config = _base_config(inverse_martingale={"AAPL": PCT_ENROLLMENT})
    state = StrategyState()
    tracker_path, claim = _crash_a_fire_claim(tmp_path, monkeypatch, config, state)
    assert claim["settlement_claimed_usd"] == pytest.approx(7.0)

    broker = _RaisingFollowUpBroker(
        {
            "state": "filled",
            "executed_notional": "7.00",
            "average_price": "96.50",
            "cumulative_quantity": "0.0725389",
        }
    )

    result, run_state, _ = _run_day(
        tmp_path, config, state, TEST_DATES[1], 96.5, "day2-recover-raise", broker=broker
    )
    assert "settlement_pending" not in json.loads(tracker_path.read_text())
    [recovery] = [
        r for r in run_state["buy_candidates"]
        if r["reason"] == "recovered_inverse_martingale_lease"
    ]
    with open(result["orders_csv_path"], encoding="utf-8") as handle:
        rows = list(csv.DictReader(handle))
    [row] = [r for r in rows if r.get("order_id") == recovery["order_id"]]
    assert float(row["avg_fill_price"]) == pytest.approx(96.50)
    assert float(row["quantity_filled"]) == pytest.approx(0.0725389)


def test_recovery_ignores_a_stale_fill_lookup_when_history_had_no_granular_breakdown(
    tmp_path, monkeypatch
):
    """Regression test for "Preserve terminal notional over a stale fill
    lookup". When terminal history confirms a fill only through
    executed_notional (im_recovery.fill_price/fill_quantity both None), a
    follow-up get_order_fill() call can still return a real (non-None)
    OrderFill -- e.g. an eventually-consistent partial fill from before the
    order went terminal. Accepting its price/quantity here made the
    execution record look like a genuine granular fill, so build_report()
    used avg_fill_price * quantity_filled (the stale partial) instead of
    falling back to the authoritative notional_usd, underreporting the
    recovery.
    """
    import near_close_dca as ncd

    class _StalePartialFollowUpBroker(_HistoryBroker):
        def get_order_fill(self, order_id):
            return ncd.OrderFill(avg_fill_price=50.0, quantity_filled=0.001)

    monkeypatch.setenv("DCA_CRON_TRACKING_ROOT", str(tmp_path / "cron_tracking"))
    config = _base_config(inverse_martingale={"AAPL": PCT_ENROLLMENT})
    state = StrategyState()
    tracker_path, claim = _crash_a_fire_claim(tmp_path, monkeypatch, config, state)
    assert claim["settlement_claimed_usd"] == pytest.approx(7.0)

    # executed_notional only -- no cumulative_quantity/average_price breakdown,
    # so im_recovery.fill_price/fill_quantity are both None.
    broker = _StalePartialFollowUpBroker({"state": "filled", "executed_notional": "7.00"})

    result, run_state, _ = _run_day(
        tmp_path, config, state, TEST_DATES[1], 96.5, "day2-recover-notional-only", broker=broker
    )
    assert "settlement_pending" not in json.loads(tracker_path.read_text())
    [recovery] = [
        r for r in run_state["buy_candidates"]
        if r["reason"] == "recovered_inverse_martingale_lease"
    ]

    with open(result["orders_csv_path"], encoding="utf-8") as handle:
        rows = list(csv.DictReader(handle))
    [row] = [r for r in rows if r.get("order_id") == recovery["order_id"]]
    assert row["status"] == "filled"
    assert float(row["notional_usd"]) == pytest.approx(7.0)
    # The stale partial from the follow-up lookup must NOT appear.
    assert row["avg_fill_price"] in ("", "-")
    assert row["quantity_filled"] in ("", "-")


def test_recovered_execution_row_persists_when_a_later_candidate_raises(tmp_path, monkeypatch):
    """Regression test for "Persist recovered execution rows when the batch
    fails". run_daily_compact's own write_execution_csv call is only reached
    on the success path; run_once's exception handler writes orders.csv on
    failure. Without that, a recovered fire's execution row -- appended to
    run_result.execution_records earlier in the SAME raising invocation --
    was lost the moment the process unwound, and could never be recovered on
    a later retry because the order is already "on disk" via
    submitted_orders.csv/run_state.json.
    """
    import near_close_dca as ncd

    monkeypatch.setenv("DCA_CRON_TRACKING_ROOT", str(tmp_path / "cron_tracking"))
    config = _base_config(inverse_martingale={"AAPL": PCT_ENROLLMENT})
    state = StrategyState()
    tracker_path, claim = _crash_a_fire_claim(tmp_path, monkeypatch, config, state)
    assert claim["settlement_claimed_usd"] == pytest.approx(7.0)

    reopened = _base_config(inverse_martingale={}, tickers=("AAPL", "MSFT"))
    broker = _HistoryBroker({"state": "filled", "executed_notional": "7.00"})

    original_process = ncd._process_buy_candidate

    def _boom_for_msft(*, decision, ctx, running_deployed_7d):
        if decision.ticker == "MSFT":
            raise RuntimeError("simulated failure for MSFT")
        return original_process(decision=decision, ctx=ctx, running_deployed_7d=running_deployed_7d)

    monkeypatch.setattr(ncd, "_process_buy_candidate", _boom_for_msft)

    with pytest.raises(RuntimeError, match="simulated failure for MSFT"):
        _run_day(
            tmp_path, reopened, state, TEST_DATES[1], 96.5, "day2-recover-fail",
            broker=broker,
        )

    assert "settlement_pending" not in json.loads(tracker_path.read_text())
    orders_csv = tmp_path / "runs" / "day2-recover-fail" / "orders.csv"
    assert orders_csv.exists(), "orders.csv must be written even though the run raised"
    with open(orders_csv, encoding="utf-8") as handle:
        rows = list(csv.DictReader(handle))
    matching = [r for r in rows if r.get("ticker") == "AAPL"]
    assert len(matching) == 1
    assert float(matching[0]["notional_usd"]) == pytest.approx(7.0)


def test_recovered_outcomes_survive_a_later_candidate_raising_mid_batch(monkeypatch):
    """Regression test for "Publish recovered outcomes before processing new
    candidates". _IN_FLIGHT_CANDIDATE_OUTCOMES is what a failure handler reads
    when a later candidate raises before _process_buy_candidates_batch
    returns. A same-day crash-lease recovery used to be merged into
    candidate_outcomes only AFTER the batch call returned -- so a raise
    partway through the batch meant that merge point was never reached, and
    an already-reconciled, already-lease-cleared recovery vanished from the
    failure artifact with no way for a later retry to reconstruct it.
    Recovered outcomes are now seeded into the batch's own dict before any
    candidate is processed, so they survive regardless of where in the batch
    the raise happens.
    """
    import near_close_dca as ncd

    recovered_outcomes = {
        "AAPL#imart-recovery": {
            "status": "submitted",
            "order_id": "imart-AAPL-recovered",
            "submitted_amount_usd": 7.0,
            "recovered_inverse_martingale_lease": True,
            "ticker": "AAPL",
        }
    }

    def _boom(**kwargs):
        raise RuntimeError("simulated failure mid-batch")

    monkeypatch.setattr(ncd, "_process_buy_candidate", _boom)

    decision = ncd.ReconciledDecision(
        ticker="MSFT",
        action=ncd.Action.BUY,
        reason="test",
        finance_signal=None,
        robinhood_signal=None,
    )
    ctx = ncd.ProcessCandidateContext(
        config=_base_config(tickers=("MSFT",)),
        state=StrategyState(),
        broker=DryRunBroker(cash=500.0, reference_prices={"MSFT": 90.0}),
        robinhood_data=None,
        now=datetime.combine(TEST_DATES[0], time(15, 45), tzinfo=EASTERN),
        trading_day=TEST_DATES[0],
        guard_active=False,
        prior_same_day_allocations={},
        scheduled=True,
        run_result=None,
    )

    with pytest.raises(RuntimeError, match="simulated failure mid-batch"):
        ncd._process_buy_candidates_batch(
            buy_candidates=[decision],
            ctx=ctx,
            initial_candidate_outcomes=recovered_outcomes,
        )

    in_flight = ncd._IN_FLIGHT_CANDIDATE_OUTCOMES.get("outcomes")
    assert in_flight is not None
    assert in_flight["AAPL#imart-recovery"] == recovered_outcomes["AAPL#imart-recovery"]


def test_recovery_row_survives_alongside_a_same_ticker_candidate(tmp_path, monkeypatch):
    """Regression test for "Preserve recovery rows alongside current ticker
    outcomes". candidate_outcomes is keyed by ticker, so keying a recovery by
    its ticker made the two events collide and one was silently dropped --
    under-reporting real capital to every later same-day and deployment-history
    reader.
    """
    monkeypatch.setenv("DCA_CRON_TRACKING_ROOT", str(tmp_path / "cron_tracking"))
    config = _base_config(inverse_martingale={"AAPL": PCT_ENROLLMENT})
    state = StrategyState()
    tracker_path, claim = _crash_a_fire_claim(tmp_path, monkeypatch, config, state)

    # Same trading day as the claim, enrollment now disabled -> AAPL produces an
    # ordinary ladder candidate of its own AND carries a recovery.
    reopened = _base_config(inverse_martingale={}, tickers=("AAPL",))
    broker = _HistoryBroker({"state": "filled", "executed_notional": "7.00"}, price=96.5)
    _, run_state, _ = _run_day(
        tmp_path, reopened, state, TEST_DATES[1], 90.0, "day2b", broker=broker
    )

    assert "settlement_pending" not in json.loads(tracker_path.read_text())
    rows = [r for r in run_state["buy_candidates"] if r["ticker"] == "AAPL"]
    recoveries = [r for r in rows if r["reason"] == "recovered_inverse_martingale_lease"]
    # The recovery is present in its own right, not displaced by the ordinary
    # candidate the same ticker produced this run.
    assert len(recoveries) == 1
    assert float(recoveries[0]["planned_amount_usd"]) == pytest.approx(7.0)
    assert len(rows) > len(recoveries), "the ordinary candidate must survive too"


def test_another_same_ticker_order_does_not_suppress_recovery_booking(tmp_path, monkeypatch):
    """Regression test for "Require the recovered order itself in deployment
    history". The old ticker-level predicate answered a weaker question: ANY
    submitted order for the ticker that day. An ordinary ladder buy from another
    stream therefore suppressed booking for a recovered order with no artifact
    at all, and the batch computed its headroom without that spend.
    """
    import near_close_dca as ncd

    monkeypatch.setenv("DCA_CRON_TRACKING_ROOT", str(tmp_path / "cron_tracking"))
    config = _base_config(inverse_martingale={"AAPL": PCT_ENROLLMENT})
    state = StrategyState()
    tracker_path, claim = _crash_a_fire_claim(tmp_path, monkeypatch, config, state)

    # The TICKER has a submitted order on the claim day, but not THIS order.
    monkeypatch.setattr(
        ncd, "_same_day_ticker_deployment_already_recorded", lambda *a, **k: True
    )
    broker = _HistoryBroker({"state": "filled", "executed_notional": "7.00"})
    _run_day(
        tmp_path, config, state, TEST_DATES[2], 96.5, "day3", broker=broker,
        deployment_history_root=tmp_path / "runs",
    )

    assert "settlement_pending" not in json.loads(tracker_path.read_text())
    # Booked: the ticker-level evidence is about a different order.
    assert state.day_notional(TEST_DATES[1]) == pytest.approx(7.0)


def test_recovery_booking_still_seeds_a_separate_prior_allocation(tmp_path, monkeypatch):
    """Regression test for "Reconcile separate prior allocations after booking
    a recovery".

    A same-day recovered order used to be recorded in ``own_orders_recorded``
    under the bare ``(ticker, trading_day)`` key that ``_seed_prior_allocations``
    reads as "this stream already bought this ticker today, its aggregate is
    accounted for". That inference is exactly wrong for a recovery: the
    recovered order died before its artifacts were written, so it is absent from
    the scan the aggregate is built from, and the aggregate describes some OTHER
    order -- a cross-stream or manual buy made after the crash. Suppressing it
    dropped that order's dollars AND its attempt from StrategyState, so
    unrelated candidates could run past the daily, weekly and shared
    order-count caps.
    """
    monkeypatch.setenv("DCA_CRON_TRACKING_ROOT", str(tmp_path / "cron_tracking"))
    config = _base_config(inverse_martingale={"AAPL": PCT_ENROLLMENT})
    state = StrategyState()
    tracker_path, claim = _crash_a_fire_claim(tmp_path, monkeypatch, config, state)
    assert claim["settlement_claimed_usd"] == pytest.approx(7.0)

    # A genuinely separate $5 order for the same ticker, placed by another
    # stream after the crash -- and therefore present in the artifact scan the
    # recovered order is missing from.
    separate = PriorSameDayAllocation(
        "AAPL", amount_usd=5.0, reference_price=100.0, run_id="morning",
        source="scheduled", selected_base_amount_usd=2.0, attempt_count=1,
    )
    # Resolved on the SAME day it was claimed (a matched, filled order settles
    # immediately) so the recovery and the seeding pass address the same day --
    # the only arrangement in which the old key could collide at all.
    broker = _HistoryBroker({"state": "filled", "executed_notional": "7.00"})
    _run_day(
        tmp_path, config, state, TEST_DATES[1], 96.5, "day2-recover", broker=broker,
        prior_same_day_allocations={"AAPL": separate},
    )

    assert "settlement_pending" not in json.loads(tracker_path.read_text())
    # BOTH orders are accounted for: the recovered $7 and the separate $5.
    assert state.day_notional(TEST_DATES[1]) == pytest.approx(12.0)
    # And both consumed one of the shared max_orders_per_day slots.
    assert state.day_order_count(TEST_DATES[1]) == 2


def test_book_recovered_lease_durably_keys_idempotency_by_the_individual_claim():
    """Regression test for "Key recovery idempotency by the individual
    claim". Two DISTINCT recovered leases for the same ticker and claim day
    -- e.g. a morning partial-fire claim recovered before an afternoon
    continuation also crashes -- previously mapped to the identical
    ``(ticker, claim_trading_day)`` key in ``state.own_orders_recorded``.
    Once the first recovery's key landed there, the second recovery's own
    idempotency check short-circuited and its ``record_buy`` was skipped
    entirely, so a batch that recovered both leases only ever booked one of
    them into the durable daily/weekly totals.
    """
    state = StrategyState()
    day = date(2026, 7, 1)
    morning = InverseMartingaleLeaseRecovery(
        ticker="AAPL", claim_trading_day=day, recovered_amount_usd=7.0,
        recovered_state={}, order_id="order-morning", claim_ref="ref-morning",
        claim_stream="morning",
    )
    afternoon = InverseMartingaleLeaseRecovery(
        ticker="AAPL", claim_trading_day=day, recovered_amount_usd=5.0,
        recovered_state={}, order_id="order-afternoon", claim_ref="ref-afternoon",
        claim_stream="afternoon",
    )

    _book_recovered_lease_durably(morning, state, day, deployment_history_root=None)
    _book_recovered_lease_durably(afternoon, state, day, deployment_history_root=None)

    # Both orders' dollars are booked -- neither was silently skipped as an
    # apparent re-run of the other.
    assert state.day_notional(day) == pytest.approx(12.0)

    # Replaying the SAME recovery again (the actual in-process-retry case
    # this idempotency guard exists for) must still be a no-op.
    _book_recovered_lease_durably(morning, state, day, deployment_history_root=None)
    assert state.day_notional(day) == pytest.approx(12.0)


def test_account_mismatch_refuses_a_positive_match_too(tmp_path, monkeypatch):
    """Regression test for "Refuse positive matches from a different broker
    account". The claim ref is built from stream + trading day + ticker and
    carries no account, so two accounts mint the identical ref: a match in the
    wrong account is some other account's order, and settling on it would clear
    the original lease and book a fill that never happened here.
    """
    import near_close_dca as ncd

    monkeypatch.setenv("DCA_CRON_TRACKING_ROOT", str(tmp_path / "cron_tracking"))
    config = _base_config(inverse_martingale={"AAPL": PCT_ENROLLMENT})
    state = StrategyState()
    tracker_path, claim = _crash_a_fire_claim(tmp_path, monkeypatch, config, state)
    assert claim["settlement_account"]

    # This run computes a DIFFERENT fingerprint than the claim recorded.
    monkeypatch.setattr(ncd, "_broker_account_fingerprint", lambda *a, **k: "0" * 32)
    # ...and the broker would happily return a matching ref for its own account.
    broker = _HistoryBroker({"state": "filled", "executed_notional": "7.00"})
    _run_day(tmp_path, config, state, TEST_DATES[2], 96.5, "day3", broker=broker)

    # Neither settled nor released: nothing about this claim is knowable here.
    assert json.loads(tracker_path.read_text()) == claim
    assert broker.queried == []
    assert state.day_notional(TEST_DATES[1]) == pytest.approx(0.0)


def test_matched_terminal_zero_fill_resolves_on_the_claim_day(tmp_path, monkeypatch):
    """Regression test for "Resolve matched terminal zero-fill orders on the
    claim day". The next-day wait exists because an ABSENT order might still be
    in flight; a matched terminal row proves submission happened and can never
    fill, so waiting only blocked later same-day streams from retrying.
    """
    monkeypatch.setenv("DCA_CRON_TRACKING_ROOT", str(tmp_path / "cron_tracking"))
    config = _base_config(inverse_martingale={"AAPL": PCT_ENROLLMENT})
    state = StrategyState()
    tracker_path, claim = _crash_a_fire_claim(tmp_path, monkeypatch, config, state)
    claim_day = date.fromisoformat(claim["settlement_trading_day"])

    broker = _HistoryBroker(
        {"state": "cancelled", "cumulative_quantity": "0", "average_price": "0"}, price=96.5
    )
    # SAME trading day as the claim -- a later stream on the crash day.
    _run_day(tmp_path, config, state, claim_day, 96.5, "day2b", broker=broker)

    recovered = json.loads(tracker_path.read_text())
    assert "settlement_pending" not in recovered
    assert recovered["accumulated_usd"] == pytest.approx(7.0)
    assert recovered["fire_attempts"] == 1


def test_absent_order_still_waits_for_the_claim_day_to_end(tmp_path, monkeypatch):
    """The counterpart: with NO matching order the next-day requirement stands,
    because the owning worker may still be between claim and submit."""
    monkeypatch.setenv("DCA_CRON_TRACKING_ROOT", str(tmp_path / "cron_tracking"))
    config = _base_config(inverse_martingale={"AAPL": PCT_ENROLLMENT})
    state = StrategyState()
    tracker_path, claim = _crash_a_fire_claim(tmp_path, monkeypatch, config, state)
    claim_day = date.fromisoformat(claim["settlement_trading_day"])

    broker = _HistoryBroker(None, price=96.5)
    _run_day(tmp_path, config, state, claim_day, 96.5, "day2b", broker=broker)
    assert json.loads(tracker_path.read_text()) == claim  # still leased


def test_lease_recovery_runs_even_after_enrollment_is_disabled(tmp_path, monkeypatch):
    """Regression test for "Reconcile leases before enrollment and market-data
    gates". Broker-ref reconciliation needs no current price and no active
    enrollment. Reached only from inside the per-ticker evaluation loop, a
    crashed lease became permanently unrecoverable the moment an operator
    disabled inverse-Martingale for that ticker: the cleanup helper
    deliberately leaves settlement_pending untouched and the evaluation loop
    never reaches an unenrolled ticker at all.
    """
    monkeypatch.setenv("DCA_CRON_TRACKING_ROOT", str(tmp_path / "cron_tracking"))
    config = _base_config(inverse_martingale={"AAPL": PCT_ENROLLMENT})
    state = StrategyState()
    tracker_path, claim = _crash_a_fire_claim(tmp_path, monkeypatch, config, state)
    assert claim["settlement_pending"] is True

    # The operator disables enrollment entirely before the recovery run.
    disabled = _base_config(inverse_martingale={})
    broker = _HistoryBroker(
        {"state": "filled", "dollar_amount": "7.00", "cumulative_quantity": "0.07", "average_price": "100.00"}
    )
    _run_day(tmp_path, disabled, state, TEST_DATES[2], 96.5, "day3", broker=broker)

    assert broker.queried == [("AAPL", TEST_DATES[1], claim["settlement_ref_id"])]
    recovered = json.loads(tracker_path.read_text())
    assert "settlement_pending" not in recovered
    # The claimed fire landed in full, so the lease settles to its target
    # rather than sitting frozen forever, and the $7 is booked as real spend.
    assert recovered["status"] == STATUS_NONE
    assert state.day_notional(TEST_DATES[1]) == pytest.approx(7.0)


def test_lease_recovery_runs_even_when_the_date_gate_blocks_the_run(tmp_path, monkeypatch):
    """Regression test for "Reconcile leases before the date-gate return".
    Before this fix, every scheduled invocation on or after a date outside
    config.approved_test_dates returned from _check_execution_gates before
    the ordinary lease-recovery pass further down ever ran -- so a fire
    claimed on the final approved trading date crashed into a lease no LATER
    run could ever reach, permanently freezing the balance until an operator
    manually widened the trading window.
    """
    monkeypatch.setenv("DCA_CRON_TRACKING_ROOT", str(tmp_path / "cron_tracking"))
    config = _base_config(inverse_martingale={"AAPL": PCT_ENROLLMENT})
    state = StrategyState()
    tracker_path, claim = _crash_a_fire_claim(tmp_path, monkeypatch, config, state)
    assert claim["settlement_pending"] is True

    # The trading window closed before a retry ever ran: TEST_DATES[2] is not
    # in this config's approved_test_dates, so _check_execution_gates blocks
    # the whole run.
    gated = _base_config(
        inverse_martingale={"AAPL": PCT_ENROLLMENT},
        approved_test_dates=frozenset(TEST_DATES[:2]),
    )
    broker = _HistoryBroker(
        {"state": "filled", "dollar_amount": "7.00", "cumulative_quantity": "0.07", "average_price": "100.00"}
    )
    result, run_state, _ = _run_day(tmp_path, gated, state, TEST_DATES[2], 96.5, "day3", broker=broker)

    assert result["blocked"] is True
    assert run_state["buy_candidates"] == []
    # The lease was still reconciled against the broker despite the run
    # returning immediately after the date gate.
    assert broker.queried == [("AAPL", TEST_DATES[1], claim["settlement_ref_id"])]
    recovered = json.loads(tracker_path.read_text())
    assert "settlement_pending" not in recovered
    assert recovered["status"] == STATUS_NONE
    # Regression assertion for "Book recovered spend before clearing gated
    # leases": the DURABLE half of booking (state.record_buy against the
    # claim day) must still happen even though the gate blocks this run from
    # evaluating any candidates -- once this pass clears settlement_pending,
    # no later run can ever rediscover the lease to book it retroactively,
    # and the next allowed run would otherwise compute headroom as if the
    # money were never spent.
    assert state.day_notional(TEST_DATES[1]) == pytest.approx(7.0)


def test_lease_recovery_books_spend_when_a_market_holiday_blocks_the_run(tmp_path, monkeypatch):
    """A holiday-gated reconciliation must not clear a filled lease without
    recording its spend against the original claim day.

    The approved-date regression above exercises the other half of the shared
    date/holiday gate.  Keep a holiday-specific case as well: holiday retries
    are a routine way for a final weekday submission to be reconciled after
    its worker has crashed, and the returned recovery must reach the durable
    booking path before the early return.
    """
    monkeypatch.setenv("DCA_CRON_TRACKING_ROOT", str(tmp_path / "cron_tracking"))
    config = _base_config(inverse_martingale={"AAPL": PCT_ENROLLMENT})
    state = StrategyState()
    tracker_path, claim = _crash_a_fire_claim(tmp_path, monkeypatch, config, state)

    holiday = date(2026, 7, 3)  # NYSE-observed Independence Day holiday.
    gated = _base_config(
        inverse_martingale={"AAPL": PCT_ENROLLMENT},
        approved_test_dates=frozenset((*TEST_DATES, holiday)),
    )
    broker = _HistoryBroker(
        {"state": "filled", "dollar_amount": "7.00", "cumulative_quantity": "0.07", "average_price": "100.00"}
    )

    result, run_state, _ = _run_day(tmp_path, gated, state, holiday, 96.5, "holiday", broker=broker)

    assert result["blocked"] is True
    assert run_state["buy_candidates"] == []
    assert broker.queried == [("AAPL", TEST_DATES[1], claim["settlement_ref_id"])]
    assert "settlement_pending" not in json.loads(tracker_path.read_text())
    assert state.day_notional(TEST_DATES[1]) == pytest.approx(7.0)
    assert state.week_notional(TEST_DATES[1]) == pytest.approx(7.0)


def test_dry_run_recovery_never_releases_a_live_lease(tmp_path, monkeypatch):
    """Regression test for "Reconcile live leases even when the recovery run is
    dry-run". A dry-run invocation has no broker order history, so an absent
    order proves nothing. Gating the lookup on the CURRENT run's mode alone
    released the whole pre-fire balance as "unplaced" once the claim day
    passed -- even though the live order existed -- and a later live run could
    then buy that balance a second time.
    """
    monkeypatch.setenv("DCA_CRON_TRACKING_ROOT", str(tmp_path / "cron_tracking"))
    config = _base_config(inverse_martingale={"AAPL": PCT_ENROLLMENT})
    state = StrategyState()
    tracker_path, claim = _crash_a_fire_claim(tmp_path, monkeypatch, config, state)
    assert claim["settlement_live"] is True

    dry = _base_config(inverse_martingale={"AAPL": PCT_ENROLLMENT}, dry_run=True)
    broker = _HistoryBroker(None)
    _run_day(tmp_path, dry, StrategyState(), TEST_DATES[2], 96.5, "day3", broker=broker)

    preserved = json.loads(tracker_path.read_text())
    assert broker.queried == []  # never even asked -- a dry run cannot answer
    assert preserved == claim  # the lease is left exactly as the crash left it


def test_sub_dollar_balance_is_deferred_not_rounded_up_to_the_broker_minimum(tmp_path, monkeypatch):
    """A schema-valid unit_usd below $1 combined with a single-unit first tier
    must never reach the broker at all -- not as a sub-dollar order, and not
    rounded UP into an order several times the tracked balance (Codex review:
    "Defer sub-dollar balances instead of rounding them up"). The accumulator's
    contract is that it defers a descent it cannot act on; buying more than it
    tracked breaks that in the expensive direction.
    """
    monkeypatch.setenv("DCA_CRON_TRACKING_ROOT", str(tmp_path / "cron_tracking"))
    small_unit_enrollment = {
        "basis": "pct",
        "tiers": ((1.0, 1.0),),  # a single 1-unit tier
        "recovery_offset": 1.0,
    }
    config = _base_config(inverse_martingale={"AAPL": small_unit_enrollment}, inverse_martingale_unit_usd=0.3)
    state = StrategyState()

    _run_day(tmp_path, config, state, TEST_DATES[0], 99.0, "day1")  # -1%, crosses tier 1 -> $0.30
    tracker_path = tmp_path / "cron_tracking" / "inverse_martingale_dca" / "AAPL" / "tracker.json"
    assert json.loads(tracker_path.read_text())["accumulated_usd"] == pytest.approx(0.3, abs=0.01)

    _, s2, _ = _run_day(tmp_path, config, state, TEST_DATES[1], 100.0, "day2")  # recovers
    # Nothing is placed: not $0.30 (below the broker minimum) and emphatically
    # not $1.00 (more than three times what the ladder deferred).
    assert s2["buy_candidates"] == []
    assert state.day_notional(TEST_DATES[1]) == pytest.approx(0.0)

    tracker = json.loads(tracker_path.read_text())
    # The cycle stays active with its balance intact, so a deeper later dip can
    # still grow it past the minimum; max_cycle_days remains the backstop.
    assert tracker["status"] == STATUS_DIPPING
    assert tracker["accumulated_usd"] == pytest.approx(0.3, abs=0.01)
    assert not [e for e in tracker["events"] if e["fired"]]


def test_the_broker_minimum_floor_never_places_past_a_tighter_per_order_cap(tmp_path, monkeypatch):
    """The broker-minimum floor raises a fire back to $1.00 whenever the cycle
    genuinely holds that much (see test_dca_inverse_martingale.py's
    test_the_broker_minimum_floor_still_beats_a_sub_dollar_per_order_cap), but
    it must never do so by spending PAST an operator's own per-order cap. With
    max_per_order_usd below the broker minimum the two constraints are
    irreconcilable, and the fail-closed outcome is to place nothing at all
    rather than to let the floor quietly override the tighter limit.

    The ledger's accounting is the part that matters here: a blocked placement
    is money that was never spent, so the claim must roll all the way back and
    leave the full $3.00 balance intact for a later evaluation to fire once the
    cap allows it.
    """
    monkeypatch.setenv("DCA_CRON_TRACKING_ROOT", str(tmp_path / "cron_tracking"))
    config = _base_config(
        inverse_martingale={"AAPL": PCT_ENROLLMENT},
        max_per_order_usd=0.4,  # below the broker minimum -- irreconcilable
    )
    state = StrategyState()

    _run_day(tmp_path, config, state, TEST_DATES[0], 99.0, "day1")  # +$1
    _run_day(tmp_path, config, state, TEST_DATES[1], 97.0, "day2")  # +$2 -> $3 total
    tracker_path = tmp_path / "cron_tracking" / "inverse_martingale_dca" / "AAPL" / "tracker.json"
    assert json.loads(tracker_path.read_text())["accumulated_usd"] == pytest.approx(3.0, abs=0.01)

    _, s3, _ = _run_day(tmp_path, config, state, TEST_DATES[2], 100.0, "day3")  # recovers
    [candidate] = s3["buy_candidates"]
    assert candidate["status"] == "blocked"
    assert state.day_notional(TEST_DATES[2]) == pytest.approx(0.0)

    # Nothing was placed, so the rollback must restore the balance in full --
    # the cycle keeps carrying its $3.00 rather than silently losing the
    # claimed slice to a placement that never happened.
    tracker = json.loads(tracker_path.read_text())
    assert tracker["accumulated_usd"] == pytest.approx(3.0, abs=0.01)


def test_settlement_skips_rather_than_overwrites_a_concurrently_advanced_ledger(tmp_path, monkeypatch):
    """Regression test for "Serialize partial-fire claims through settlement".

    Deferred settlement writes back via InverseMartingaleLedger.claim_fire()
    (a compare-and-swap), not the older unconditional record_step() -- so if
    a fully independent second process reads the SAME just-claimed remainder
    state this run wrote at the injection site and successfully advances the
    ledger on its own before this run reaches settlement, this run's own
    settlement write must be REFUSED rather than silently clobbering the
    other process's state.

    Simulated by wrapping InverseMartingaleLedger.claim_fire: right after the
    real injection-site claim succeeds (call #1, this run's own), a second,
    independent ledger instance immediately performs its own successful
    claim_fire() against that exact just-written state -- exactly what a
    genuinely concurrent process would do. The settlement call that follows
    (call #2) must then find the on-disk state no longer matches what this
    run's own claim wrote, and its own CAS must fail.
    """
    monkeypatch.setenv("DCA_CRON_TRACKING_ROOT", str(tmp_path / "cron_tracking"))
    config = _base_config(
        inverse_martingale={"AAPL": PCT_ENROLLMENT},
        max_per_order_usd=1.0,  # forces a partial fire, so injection claims only part of the $3 balance
    )
    state = StrategyState()

    _run_day(tmp_path, config, state, TEST_DATES[0], 99.0, "day1")  # +$1
    _run_day(tmp_path, config, state, TEST_DATES[1], 97.0, "day2")  # +$2 -> $3 total

    import near_close_dca as ncd

    real_claim_fire = ncd.InverseMartingaleLedger.claim_fire
    call_count = {"n": 0}
    intruder_marker = {
        "status": "dipping", "accumulated_usd": 42.0, "intruder": True,
        # A realistic intruder write still carries the fields a real
        # evaluate_inverse_martingale_step() call always produces for an
        # active "dipping" cycle -- see "Require complete snapshots for
        # active trackers" and "Require cycle-start metadata for active
        # trackers" -- so this fixture is a genuinely valid (if
        # unexpected) state, not one _validate_tracker_payload itself
        # would reject for an unrelated reason.
        "crossed_tier_thresholds": [1.0], "cycle_reference_close": 100.0, "lowest_drop_seen": 1.0,
        "cycle_start_trading_day": "2026-06-25", "trading_days_active": 1,
    }

    def spying_claim_fire(self, ticker, *, expected_prior_state, new_state, step):
        call_count["n"] += 1
        ok = real_claim_fire(self, ticker, expected_prior_state=expected_prior_state, new_state=new_state, step=step)
        if call_count["n"] == 1 and ok:
            # A fully independent second process, racing in right after this
            # run's own injection-site claim, successfully advances the
            # ledger on its own -- called directly against the UNPATCHED
            # original method so this simulated intrusion is not itself
            # counted as one of this run's own claim_fire calls.
            intruder = ncd.InverseMartingaleLedger()
            real_claim_fire(intruder, ticker, expected_prior_state=new_state, new_state=intruder_marker, step=step)
        return ok

    monkeypatch.setattr(ncd.InverseMartingaleLedger, "claim_fire", spying_claim_fire)

    _run_day(tmp_path, config, state, TEST_DATES[2], 98.5, "day3")  # recovers -> fires, races, settles

    assert call_count["n"] == 2  # the injection-site claim, then settlement's own CAS attempt

    tracker_path = tmp_path / "cron_tracking" / "inverse_martingale_dca" / "AAPL" / "tracker.json"
    on_disk = json.loads(tracker_path.read_text())
    # The intruding process's state survives untouched -- settlement's CAS
    # detected the mismatch and skipped writing rather than overwriting it.
    assert on_disk.get("intruder") is True
    assert on_disk["accumulated_usd"] == pytest.approx(42.0)


def test_non_fire_transition_is_also_compare_and_swap_protected(tmp_path, monkeypatch):
    """Regression test for "Compare-and-swap every accumulator transition".

    The NON-fire branch (tier crossed, awaiting recovery -- no order
    attempted) used to write via an unconditional record_step(). Two
    overlapping processes evaluating the same ticker from the same starting
    snapshot but different prices could then have one successfully
    claim_fire() a real order while the other, reaching the non-fire branch,
    unconditionally overwrote that claim with its own stale state -- the
    fire would still be submitted, but its later settlement CAS would then
    fail against the overwritten state, stranding the already-spent balance
    in the ledger to fire again later. Simulated here by racing an
    independent "intruder" write in between this run's own read and its
    write of a plain tier-crossing (non-fire) transition.
    """
    monkeypatch.setenv("DCA_CRON_TRACKING_ROOT", str(tmp_path / "cron_tracking"))
    config = _base_config(inverse_martingale={"AAPL": PCT_ENROLLMENT})
    state = StrategyState()

    import near_close_dca as ncd

    real_compare_and_swap = ncd.InverseMartingaleLedger.compare_and_swap_transition
    call_count = {"n": 0}
    intruder_marker = {
        "status": "dipping", "accumulated_usd": 42.0, "intruder": True,
        # A realistic intruder write still carries the fields a real
        # evaluate_inverse_martingale_step() call always produces for an
        # active "dipping" cycle -- see "Require complete snapshots for
        # active trackers" and "Require cycle-start metadata for active
        # trackers" -- so this fixture is a genuinely valid (if
        # unexpected) state, not one _validate_tracker_payload itself
        # would reject for an unrelated reason.
        "crossed_tier_thresholds": [1.0], "cycle_reference_close": 100.0, "lowest_drop_seen": 1.0,
        "cycle_start_trading_day": "2026-06-25", "trading_days_active": 1,
    }

    def spying_compare_and_swap(self, ticker, *, expected_prior_state, new_state, step):
        call_count["n"] += 1
        if call_count["n"] == 1:
            # A fully independent second process races in and successfully
            # advances the ledger from the SAME starting state this run is
            # about to write from -- called directly against the UNPATCHED
            # original method so it is not itself counted as one of this
            # run's own transition writes.
            intruder = ncd.InverseMartingaleLedger()
            real_compare_and_swap(
                intruder, ticker, expected_prior_state=expected_prior_state, new_state=intruder_marker, step=step
            )
        return real_compare_and_swap(
            self, ticker, expected_prior_state=expected_prior_state, new_state=new_state, step=step
        )

    monkeypatch.setattr(ncd.InverseMartingaleLedger, "compare_and_swap_transition", spying_compare_and_swap)

    # Day 1: a 1% dip crosses tier 1 -- a plain, non-fire tier-crossing
    # transition, exactly the code path under test (no cycle exists yet, so
    # expected_prior_state is {} -- the intruder's own claim against that
    # same {} succeeds first).
    result, run_state, _ = _run_day(tmp_path, config, state, TEST_DATES[0], 99.0, "day1")

    assert result["blocked"] is False
    assert run_state["buy_candidates"] == []  # never fires this run
    assert call_count["n"] == 1  # this run's own single non-fire evaluation

    tracker_path = tmp_path / "cron_tracking" / "inverse_martingale_dca" / "AAPL" / "tracker.json"
    on_disk = json.loads(tracker_path.read_text())
    # The intruding process's state survives -- this run's own non-fire
    # write lost its compare-and-swap (the on-disk state no longer matched
    # expected_prior_state after the intruder's write) and was skipped,
    # rather than clobbering it.
    assert on_disk.get("intruder") is True
    assert on_disk["accumulated_usd"] == pytest.approx(42.0)


def test_corrupted_tracker_file_is_skipped_not_silently_reset(tmp_path, monkeypatch, caplog):
    """Regression test for "Refuse unreadable accumulator state instead of
    resetting it". A truncated/malformed tracker.json must not be treated
    the same as a genuinely absent one -- evaluating from a fabricated {}
    would start a brand-new cycle and this ticker's own compare-and-swap
    write would overwrite the damaged file, permanently discarding whatever
    dollar balance it held. The run itself must still complete normally;
    only this one ticker's evaluation is skipped.
    """
    monkeypatch.setenv("DCA_CRON_TRACKING_ROOT", str(tmp_path / "cron_tracking"))
    config = _base_config(inverse_martingale={"AAPL": PCT_ENROLLMENT})
    state = StrategyState()

    tracker_dir = tmp_path / "cron_tracking" / "inverse_martingale_dca" / "AAPL"
    tracker_dir.mkdir(parents=True)
    corrupted_content = "{not valid json"
    (tracker_dir / "tracker.json").write_text(corrupted_content, encoding="utf-8")

    with caplog.at_level("ERROR"):
        result, run_state, _ = _run_day(tmp_path, config, state, TEST_DATES[0], 99.0, "day1")

    assert result["blocked"] is False  # the run itself completes normally
    assert run_state["buy_candidates"] == []  # AAPL's evaluation was skipped, not restarted fresh
    # The damaged file is left completely untouched -- NOT overwritten with
    # a fresh {"status": "dipping", ...} cycle starting from a fabricated {}.
    assert (tracker_dir / "tracker.json").read_text(encoding="utf-8") == corrupted_content
    assert any("unreadable" in record.message for record in caplog.records)


def test_lease_account_fingerprint_is_keyed_not_a_bare_digest(tmp_path):
    """Regression test for "Replace the reversible account-number fingerprint".

    A Robinhood account is 6-12 decimal digits, so a plain ``sha256(account)``
    persisted in a tracker is not a secret at all: anyone holding the digest can
    enumerate the whole space -- a million candidates at the short end -- and
    recover the number exactly. The fingerprint must be a KEYED HMAC whose key
    lives outside the tracker payload.
    """
    import hashlib

    from near_close_dca import _LEASE_FINGERPRINT_KEY_FILENAME, _broker_account_fingerprint

    class _Broker:
        available_buying_power = 1000.0

        def __init__(self, account):
            self._account = account

        def account_number(self):
            return self._account

    account = "1" * 9
    first = _broker_account_fingerprint(cast(Any, _Broker(account)), tmp_path / "keys_a")
    assert first
    # Not derivable from the account alone.
    assert first != hashlib.sha256(account.encode()).hexdigest()[: len(first)]
    # Stable for the same account under the same key...
    assert _broker_account_fingerprint(cast(Any, _Broker(account)), tmp_path / "keys_a") == first
    # ...and unlinkable across key domains, so a copied tracker reveals nothing.
    assert _broker_account_fingerprint(cast(Any, _Broker(account)), tmp_path / "keys_b") != first
    # A different account under the same key still differs.
    assert _broker_account_fingerprint(cast(Any, _Broker("2" * 9)), tmp_path / "keys_a") != first

    key_path = tmp_path / "keys_a" / _LEASE_FINGERPRINT_KEY_FILENAME
    assert key_path.is_file()
    assert key_path.stat().st_mode & 0o077 == 0  # owner-only


def test_a_settlement_overpay_that_exhausts_the_balance_resolves_cleanly(tmp_path, monkeypatch):
    """Regression test for "Pass the resolved marker's supported date keyword".

    When settlement reports an amount differing from the claimed fire by at
    least half a cent but still covering the accumulated balance, deferred
    settlement takes its rollback branch and writes a `resolved_marker()`. That
    call passed a keyword the marker's signature no longer accepted, raising
    TypeError *after* placement processing -- aborting the scheduled run and
    leaving the fire's `settlement_pending` claim uncleared, so the balance
    stayed frozen behind a lease nothing would ever settle.

    Nothing else in this suite reaches that branch, which is exactly why a
    signature change could break it silently: the whole test suite stayed green.
    """
    import near_close_dca as ncd

    monkeypatch.setenv("DCA_CRON_TRACKING_ROOT", str(tmp_path / "cron_tracking"))
    config = _base_config(inverse_martingale={"AAPL": PCT_ENROLLMENT})
    state = StrategyState()

    # Day 1: -5% dip accumulates $7. Day 2: recovery fires the whole balance.
    _run_day(tmp_path, config, state, TEST_DATES[0], 95.0, "day1")

    # Report a placement a cent ABOVE the claim: far enough from the claimed
    # amount to miss the "matched exactly" branch, and large enough that the
    # rollback's remainder lands at or below zero.
    real_batch = ncd._process_buy_candidates_batch

    def overpaying_batch(**kwargs):
        outcomes, top_ups, deployed, errs, revs = real_batch(**kwargs)
        for outcome in outcomes.values():
            if outcome.get("status") == "submitted" and outcome.get("submitted_amount_usd"):
                outcome["submitted_amount_usd"] = round(outcome["submitted_amount_usd"] + 0.01, 2)
        return outcomes, top_ups, deployed, errs, revs

    monkeypatch.setattr(ncd, "_process_buy_candidates_batch", overpaying_batch)
    _run_day(tmp_path, config, state, TEST_DATES[1], 96.5, "day2")

    tracker_path = tmp_path / "cron_tracking" / "inverse_martingale_dca" / "AAPL" / "tracker.json"
    resolved = json.loads(tracker_path.read_text())
    # The run completed and the lease is cleared rather than stranded.
    assert "settlement_pending" not in resolved
    assert resolved["status"] == "none"
    # The marker carries the split-window cursor under its supported keyword,
    # seeded from the cycle-start day (TEST_DATES[0]) -- the day
    # cycle_reference_close's price scale was actually captured on -- not
    # today's settlement day (Codex review: "Seed the overpay latch cursor
    # from the cycle reference day").
    assert resolved["rearm_last_seen_day"] == TEST_DATES[0].isoformat()
    # Regression assertion for "Preserve the absolute fire floor during
    # overpay settlement": the marker must carry fired_floor_price alongside
    # fired_floor_drop_metric, exactly like every other fire-resolution path
    # -- without it, the same-day re-arm guard falls back to comparing the
    # metric in whatever reference frame a later stream happens to have, and
    # a price still above the already-paid floor can re-credit tiers that
    # were already bought.
    assert resolved.get("fired_floor_price") is not None
    assert resolved["fired_floor_price"] > 0
