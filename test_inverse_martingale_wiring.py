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

import pytest

from dca_cumulative import CumulativeCreditLedger
from dca_inverse_martingale import STATUS_FIRING, InverseMartingaleLedger, InverseMartingaleStep
from near_close_dca import (
    EASTERN,
    Bar,
    CompactRunOptions,
    DryRunBroker,
    PriorSameDayAllocation,
    SourceName,
    StrategyConfig,
    StrategyState,
    _inverse_martingale_advance_age_for_missing_data,
    _inverse_martingale_reset_if_disabled_mid_cycle,
    run_daily_compact,
)

PCT_ENROLLMENT = {
    "basis": "pct",
    "tiers": ((1.0, 1.0), (2.0, 2.0), (4.0, 4.0), (8.0, 8.0)),
    "recovery_offset": 1.0,
}

TEST_DATES = (date(2026, 6, 29), date(2026, 6, 30), date(2026, 7, 1))


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
    now_time=time(15, 45),
):
    now = datetime.combine(trading_day, now_time, tzinfo=EASTERN)
    fin = _FixedPriceData(prev_close=100.0, decision_price=decision_price, source=SourceName.FINANCE)
    rh = _FixedPriceData(
        prev_close=100.0,
        decision_price=robinhood_price if robinhood_price is not None else decision_price,
        source=SourceName.ROBINHOOD,
    )
    broker = DryRunBroker(cash=broker_cash, reference_prices={"AAPL": decision_price})
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
        ),
    )
    with open(result["run_state_path"], encoding="utf-8") as handle:
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
    with open(r3["signals_path"], encoding="utf-8") as handle:
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

    with open(r3["orders_csv_path"], encoding="utf-8") as handle:
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

    # A genuinely LATER day's fresh dip still works normally.
    r3, s3, _ = _run_day(tmp_path, config, state, TEST_DATES[2], 99.0, "day3")
    assert s3["buy_candidates"] == []  # accumulates only, no fire yet
    later = json.loads(tracker_path.read_text())
    assert later["status"] == "dipping"
    assert later["accumulated_usd"] == pytest.approx(1.0, abs=0.01)


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

    # A genuinely LATER day's shallow dip is unaffected by yesterday's floor
    # -- it only ever applies on the SAME trading_day it was recorded.
    r3, s3, _ = _run_day(tmp_path, config, state, TEST_DATES[2], 99.0, "day3")
    assert s3["buy_candidates"] == []
    later = json.loads(tracker_path.read_text())
    assert later["status"] == "dipping"
    assert later["accumulated_usd"] == pytest.approx(1.0, abs=0.01)


def test_later_slot_the_same_day_breaks_below_the_fired_floor(tmp_path, monkeypatch):
    """A later same-day slot's dip that genuinely falls BELOW the fired
    cycle's own floor is a real, new descent and must still be tracked --
    the floor blocks a shallow re-arm, not every same-day re-arm (Codex
    review: "Distinguish later slots from retries before suppressing
    rearming" combined with "Require a fresh descent before rearming in a
    later slot")."""
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

    # SAME day, midday slot: a genuinely DEEPER -5.5% dip -- past the -5%
    # floor, a real new descent, not a re-visit of the morning's range.
    r2b, s2b, _ = _run_day(tmp_path, config, state, TEST_DATES[1], 94.5, "day2-midday-deep", stream="midday")
    assert s2b["buy_candidates"] == []  # accumulates only, no fire yet
    after_midday = json.loads(tracker_path.read_text())
    assert after_midday["status"] == "dipping"
    assert after_midday["accumulated_usd"] == pytest.approx(7.0, abs=0.01)  # crosses tiers 1/2/4 again


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


def test_dry_run_fire_attempts_do_not_consume_the_retry_budget(tmp_path, monkeypatch):
    """Regression test for "Do not count dry runs as fire attempts".

    A dry-run outcome never reaches a real broker -- im_placed_amount is
    unconditionally 0, identical to what a genuinely blocked live attempt
    reports -- but it must not consume the SAME max_fire_attempts retry
    budget a live blocked attempt does. With max_fire_attempts=1 and a
    per-order cap forcing a multi-run partial fire, THREE consecutive
    dry-run "fire" evaluations must not abandon the accumulated balance,
    even though a single live blocked attempt already would with the same
    budget (see test_blocked_placement_preserves_the_balance_and_counts_the_attempt).
    """
    monkeypatch.setenv("DCA_CRON_TRACKING_ROOT", str(tmp_path / "cron_tracking"))
    extended_dates = (date(2026, 6, 29), date(2026, 6, 30), date(2026, 7, 1), date(2026, 7, 2), date(2026, 7, 6))
    config = _base_config(
        inverse_martingale={"AAPL": PCT_ENROLLMENT},
        max_per_order_usd=5.0,
        inverse_martingale_max_fire_attempts=1,
        dry_run=True,
        approved_test_dates=frozenset(extended_dates),
    )
    state = StrategyState()

    _run_day(tmp_path, config, state, extended_dates[0], 99.0, "day1")  # -1% -> +$1
    _run_day(tmp_path, config, state, extended_dates[1], 91.5, "day2")  # -8.5% -> +$14 -> $15 total

    tracker_path = tmp_path / "cron_tracking" / "inverse_martingale_dca" / "AAPL" / "tracker.json"
    assert json.loads(tracker_path.read_text())["accumulated_usd"] == pytest.approx(15.0, abs=0.01)

    # Three consecutive dry-run "fires" -- max_fire_attempts=1 would abandon
    # the cycle after just the first one if dry runs incorrectly consumed
    # the same budget a live blocked attempt does.
    _run_day(tmp_path, config, state, extended_dates[2], 97.0, "day3")  # recovers -> dry-run "fires" $5 (capped)
    _run_day(tmp_path, config, state, extended_dates[3], 97.0, "day4")  # continuation, dry-run "fires" $5 again
    r5, s5, _ = _run_day(tmp_path, config, state, extended_dates[4], 97.0, "day5")

    tracker = json.loads(tracker_path.read_text())
    assert tracker["status"] == "firing"  # NOT abandoned back to "none"
    assert tracker["accumulated_usd"] == pytest.approx(15.0, abs=0.01)  # nothing lost across 3 dry runs
    assert tracker["fire_attempts"] == 0  # dry runs never counted

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


def test_pending_credit_flush_candidate_defers_fire_but_still_tracks_the_dip(tmp_path, monkeypatch):
    """Regression test for "Track tiers despite a pending-credit flush
    candidate". A ticker with a same-day pending cumulative credit (an
    unrelated, older accumulation mechanism -- see dca_cumulative.py) gets
    its own non-BUY decision appended to buy_candidates so that balance can
    flush, which makes it appear in `already_included` for a completely
    unrelated reason. The inverse-Martingale evaluation must still TRACK
    this ticker's dip -- only fire-injection defers, exactly like a prior
    same-day allocation (guard_active) already does. Previously the whole
    evaluation was skipped whenever already_included matched, so a tier
    crossing on the ticker's own final scheduled slot was silently,
    permanently lost.
    """
    monkeypatch.setenv("DCA_CRON_TRACKING_ROOT", str(tmp_path / "cron_tracking"))
    config = _base_config(inverse_martingale={"AAPL": PCT_ENROLLMENT})
    state = StrategyState()

    # Seed a small (well under the $0.50 flush threshold) pending credit for
    # AAPL on day 1, so the credit-ledger flush block appends AAPL's non-BUY
    # decision to buy_candidates this run -- guard_active requires a non-None
    # prior_same_day_allocations, passed as {} here (present, but with no
    # entry for AAPL) so this test isolates the batch-occupied reason from
    # the separate prior-same-day-allocation guard tested above.
    credit_ledger = CumulativeCreditLedger(root=tmp_path / "cron_tracking" / "cumulative_dca_credits")
    credit_ledger.record("AAPL", TEST_DATES[0].isoformat(), 0.20)

    r1, s1, _ = _run_day(
        tmp_path, config, state, TEST_DATES[0], 99.0, "day1",
        prior_same_day_allocations={},
    )
    # The flush candidate itself is a real (non-BUY, non-imart) entry --
    # confirms already_included genuinely matched AAPL this run, not that
    # the credit ledger silently failed to engage.
    assert len(s1["buy_candidates"]) == 1
    assert "imart" not in (s1["buy_candidates"][0].get("order_id") or "")

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


def test_sub_dollar_fire_is_floored_to_the_broker_minimum(tmp_path, monkeypatch):
    """A schema-valid unit_usd below $1 combined with a single-unit first
    tier must not reach the broker as a sub-dollar order (Codex review:
    "Prevent sub-dollar accumulator fires")."""
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

    r2, s2, _ = _run_day(tmp_path, config, state, TEST_DATES[1], 100.0, "day2")  # fully recovers -> fires
    [candidate] = s2["buy_candidates"]
    assert candidate["status"] == "submitted"
    assert candidate["planned_amount_usd"] == pytest.approx(1.0, abs=0.01)  # floored, not $0.30

    # Regression test for "Keep sub-dollar fires in the accumulator": the
    # ledger's own settlement must agree that the FULL $1.00 broker minimum
    # was claimed/settled (not just $0.30), since that is what was actually
    # placed -- the earlier fix floored only the candidate's order amount,
    # leaving the step's own fire_amount_usd (what settlement compares
    # against) understated at $0.30, so settlement saw "more placed than
    # accumulated" and silently absorbed the extra $0.70 as a clean
    # resolution instead of it ever being properly claimed from the ledger.
    tracker = json.loads(tracker_path.read_text())
    assert tracker["status"] == "none"  # cleanly resolved, no phantom remainder
    # Both the injection-site claim and settlement's confirmation append
    # their own audit event (claim, then confirm) -- both must agree the
    # ledger claimed/settled the FULL floored $1.00, not the raw $0.30.
    fire_events = [e for e in tracker["events"] if e["fired"]]
    assert fire_events
    for event in fire_events:
        assert event["fire_amount_usd"] == pytest.approx(1.0, abs=0.01)
        assert event["accumulated_usd_after"] == pytest.approx(0.0, abs=0.01)


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
        if call_count["n"] == 1:
            # A fully independent second process races in and successfully
            # advances the ledger from the SAME starting state this run is
            # about to write from -- called directly against the UNPATCHED
            # original method so it is not itself counted as one of this
            # run's own claim_fire calls.
            intruder = ncd.InverseMartingaleLedger()
            real_claim_fire(
                intruder, ticker, expected_prior_state=expected_prior_state, new_state=intruder_marker, step=step
            )
        return real_claim_fire(self, ticker, expected_prior_state=expected_prior_state, new_state=new_state, step=step)

    monkeypatch.setattr(ncd.InverseMartingaleLedger, "claim_fire", spying_claim_fire)

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
