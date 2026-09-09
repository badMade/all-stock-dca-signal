"""Actual placement must stay in the regular session, including manual runs."""

import json
from dataclasses import replace
from datetime import date, datetime, time, timedelta
from pathlib import Path

import pytest
from test_inverse_martingale_wiring import TEST_DATES, _base_config, _run_day
from test_morning_dca import DecliningBarsProvider, _all_tradable, _now_live
from test_whole_share_wiring import _ctx, _decision

import dca_runner
import near_close_dca
from dca_live_adapters import RobinhoodBroker
from dca_whole_share_accumulator import WholeShareAccumulationLedger
from morning_dca import MORNING_LIVE_MODE, MorningConfig, MorningDCAExecutor
from near_close_dca import EASTERN, SourceName, StrategyConfig, StrategyState, _process_buy_candidate


def _broker(placed, *, review=None, on_place=None):
    def place(**kwargs):
        placed.append(kwargs)
        if on_place:
            on_place()
        return {"order": {"id": f"fixture-order-{len(placed)}", "state": "confirmed"}}

    return RobinhoodBroker(
        account="999000111",
        enable_live_orders=True,
        place_fn=place,
        orders_fn=lambda **kwargs: {"orders": [], "next": None},
        buying_power_fn=lambda **kwargs: {"buying_power": "1000"},
        tradability_fn=_all_tradable,
        review_fn=review or (lambda **kwargs: {"approved": True}),
    )


def _freeze_clock(monkeypatch, now):
    # Freeze the clock source only: the calendar/session verdict must still run.
    monkeypatch.setattr(near_close_dca, "_live_placement_now", lambda: now, raising=False)


def test_compact_live_rejects_1700_placement(tmp_path, monkeypatch):
    now = datetime.combine(TEST_DATES[0], time(17), tzinfo=EASTERN)
    _freeze_clock(monkeypatch, now)
    placed = []
    _run_day(
        tmp_path, _base_config(), StrategyState(), now.date(), 90, "1700",
        now_time=now.time(), broker=_broker(placed),
    )
    assert placed == []


def test_manual_morning_live_rejects_1730_placement(tmp_path, monkeypatch):
    now = _now_live().replace(hour=17)
    _freeze_clock(monkeypatch, now)
    placed = []
    config = MorningConfig(
        tickers=("NVDA",),
        morning_run_state_root=str(tmp_path / "morning"),
        prior_allocation_roots=(), cap_seed_roots=(),
        approved_test_dates=frozenset({now.date()}),
    )
    result = MorningDCAExecutor(config, mode=MORNING_LIVE_MODE)._run_impl(
        now, DecliningBarsProvider(SourceName.ROBINHOOD, 100, {"NVDA": 0.10}),
        broker=_broker(placed), data_source="fixture", manual=True,
    )
    assert placed == []
    assert result["artifacts"]["buy_candidates"][0]["status"] == "blocked"


def _manual_morning(tmp_path, now, broker, tickers=("NVDA",)):
    config = MorningConfig(
        tickers=tickers, morning_run_state_root=str(tmp_path / "morning"),
        prior_allocation_roots=(), cap_seed_roots=(),
        approved_test_dates=frozenset({now.date()}),
    )
    executor = MorningDCAExecutor(config, mode=MORNING_LIVE_MODE)
    result = executor._run_impl(
        now, DecliningBarsProvider(SourceName.ROBINHOOD, 100, dict.fromkeys(tickers, 0.10)),
        broker=broker, data_source="fixture", manual=True,
    )
    return json.loads(Path(result["paths"]["run_state"]).read_text()), executor


@pytest.mark.parametrize("now", [
    datetime(2026, 7, 8, 9, 29, 59, tzinfo=EASTERN),
    datetime(2026, 7, 8, 16, tzinfo=EASTERN),
    datetime(2026, 7, 11, 12, tzinfo=EASTERN),
    datetime(2026, 9, 7, 12, tzinfo=EASTERN),
    datetime(2026, 11, 27, 13, tzinfo=EASTERN),
])
@pytest.mark.parametrize("path", ["compact", "morning", "whole_share"])
def test_live_placement_blocks_closed_sessions(tmp_path, monkeypatch, now, path):
    _freeze_clock(monkeypatch, now)
    placed = []
    broker = _broker(placed)
    if path == "morning":
        _, executor = _manual_morning(tmp_path, now, broker)
        assert executor.ledger.day_attempts(now.date()) == 0
    elif path == "compact":
        _run_day(
            tmp_path, _base_config(approved_test_dates=frozenset({now.date()})), StrategyState(),
            now.date(), 90, "closed", now_time=now.time(), broker=broker, scheduled=False,
        )
    else:
        ctx = replace(
            _ctx(StrategyConfig(whole_share_tickers=frozenset({"BYDDY"}), dry_run=False), broker),
            now=now, trading_day=now.date(),
        )
        outcome, _, _, _, _ = _process_buy_candidate(
            _decision(suggested_amount_usd=10), ctx=ctx, running_deployed_7d=0,
        )
        assert outcome["status"] == "blocked"
        assert "live session" in outcome["skip_reason"]
        assert WholeShareAccumulationLedger().pending_usd("BYDDY") == 10
        assert ctx.state.day_order_count(now.date()) == 0
    assert placed == []


@pytest.mark.parametrize("now", [
    datetime(2026, 7, 8, 12, tzinfo=EASTERN),
    datetime(2026, 11, 27, 12, 59, 59, tzinfo=EASTERN),
])
@pytest.mark.parametrize("path", ["compact", "morning", "whole_share"])
def test_manual_live_placement_allows_regular_session_outside_slot(tmp_path, monkeypatch, now, path):
    _freeze_clock(monkeypatch, now)
    placed = []
    broker = _broker(placed)
    if path == "morning":
        _manual_morning(tmp_path, now, broker)
    elif path == "compact":
        _run_day(
            tmp_path, _base_config(approved_test_dates=frozenset({now.date()})), StrategyState(),
            now.date(), 90, "manual", now_time=now.time(), broker=broker, scheduled=False,
        )
    else:
        ctx = replace(
            _ctx(StrategyConfig(whole_share_tickers=frozenset({"BYDDY"}), dry_run=False), broker, scheduled=False),
            now=now, trading_day=now.date(),
        )
        _process_buy_candidate(_decision(suggested_amount_usd=10), ctx=ctx, running_deployed_7d=0)
    assert len(placed) == 1


@pytest.mark.parametrize("path", ["compact", "morning"])
def test_close_between_candidates_preserves_first_submission(tmp_path, monkeypatch, path):
    now = _now_live().replace(hour=15, minute=59, second=59)
    clock = [now]
    monkeypatch.setattr(near_close_dca, "_live_placement_now", lambda: clock[0])
    placed = []
    broker = _broker(placed, on_place=lambda: clock.__setitem__(0, now + timedelta(seconds=1)))
    if path == "morning":
        artifact, executor = _manual_morning(tmp_path, now, broker, tickers=("AAPL", "MSFT"))
        assert executor.ledger.day_attempts(now.date()) == 1
    else:
        _, artifact, _ = _run_day(
            tmp_path, _base_config(tickers=("AAPL", "MSFT"), approved_test_dates=frozenset({now.date()})),
            StrategyState(), now.date(), 90, "cross-close", now_time=now.time(), broker=broker,
        )
    assert len(placed) == 1
    rows = {row["ticker"]: row for row in artifact["buy_candidates"]}
    assert rows["AAPL"]["order_id"] == "fixture-order-1"
    assert rows["MSFT"]["status"] == "blocked"
    reason = artifact["broker_outcomes"]["MSFT"]["skip_reason"] if path == "morning" else rows["MSFT"]["skip_reason"]
    assert "live session" in reason


@pytest.mark.parametrize("path", ["compact", "morning"])
def test_manual_review_cannot_carry_placement_past_close(tmp_path, monkeypatch, path):
    now = _now_live().replace(hour=15, minute=59, second=59)
    _freeze_clock(monkeypatch, now)
    placed = []

    def review(**kwargs):
        _freeze_clock(monkeypatch, now + timedelta(seconds=1))
        return {"approved": True}

    broker = _broker(placed, review=review)
    if path == "morning":
        _manual_morning(tmp_path, now, broker)
    else:
        _run_day(
            tmp_path, _base_config(approved_test_dates=frozenset({now.date()})), StrategyState(),
            now.date(), 90, "review", now_time=now.time(), broker=broker, scheduled=False,
        )
    assert placed == []


def test_whole_share_claim_wait_rechecks_clock_and_preserves_credit(tmp_path, monkeypatch):
    now = _now_live().replace(hour=15, minute=59, second=59)
    _freeze_clock(monkeypatch, now)
    original = WholeShareAccumulationLedger.execute_claimed

    def after_lock_wait(self, *args, **kwargs):
        _freeze_clock(monkeypatch, now + timedelta(seconds=1))
        return original(self, *args, **kwargs)

    monkeypatch.setattr(WholeShareAccumulationLedger, "execute_claimed", after_lock_wait)
    placed = []
    ctx = replace(
        _ctx(StrategyConfig(whole_share_tickers=frozenset({"BYDDY"}), dry_run=False), _broker(placed)),
        now=now, trading_day=now.date(),
    )
    outcome, _, _, _, _ = _process_buy_candidate(_decision(suggested_amount_usd=10), ctx=ctx, running_deployed_7d=0)
    assert placed == []
    assert outcome["status"] == "blocked"
    assert WholeShareAccumulationLedger().pending_usd("BYDDY") == 10


@pytest.mark.parametrize("clock_value", [None, "invalid", datetime(2026, 7, 8, 12),
    datetime(2026, 7, 9, 12, tzinfo=EASTERN)])
def test_invalid_or_wrong_day_clock_blocks_placement(tmp_path, monkeypatch, clock_value):
    _freeze_clock(monkeypatch, clock_value)
    placed = []
    artifact, _ = _manual_morning(tmp_path, _now_live(), _broker(placed))
    assert placed == []
    assert "live session" in artifact["broker_outcomes"]["NVDA"]["skip_reason"]


@pytest.mark.parametrize("failed_source", ["clock", "calendar"])
def test_unavailable_clock_or_calendar_fails_closed_without_error_details(tmp_path, monkeypatch, failed_source):
    _freeze_clock(monkeypatch, _now_live())

    def fail(*args):
        raise RuntimeError("sensitive fixture detail")

    if failed_source == "clock":
        monkeypatch.setattr(near_close_dca, "_live_placement_now", fail)
    else:
        monkeypatch.setattr(dca_runner, "market_day_status", fail)
    placed = []
    artifact, _ = _manual_morning(tmp_path, _now_live(), _broker(placed))
    assert placed == []
    reason = artifact["broker_outcomes"]["NVDA"]["skip_reason"]
    assert "live session" in reason
    assert "sensitive fixture detail" not in reason


def test_calendar_coverage_unknown_is_blocked():
    now = datetime(2029, 7, 9, 12, tzinfo=EASTERN)
    reason = near_close_dca._regular_session_failure(now, now.date())
    assert reason is not None and "CalendarCoverageError" in reason


def test_historical_dry_run_does_not_consult_live_clock(tmp_path, monkeypatch):
    def fail():
        pytest.fail("dry run must not consult the live placement clock")

    monkeypatch.setattr(near_close_dca, "_live_placement_now", fail)
    result, artifact, _ = _run_day(
        tmp_path, _base_config(dry_run=True), StrategyState(), TEST_DATES[0], 90, "dry", now_time=time(17),
    )
    assert not result["blocked"]
    assert artifact["buy_candidates"][0]["status"] == "dry_run"


def test_default_placement_clock_is_fresh_and_timezone_aware():
    before = datetime.now(EASTERN)
    observed = near_close_dca._live_placement_now()
    after = datetime.now(EASTERN)
    assert observed.utcoffset() is not None
    assert before <= observed <= after


def test_runner_fresh_clock_rechecks_early_close_without_history_timestamp():
    now = datetime(2026, 11, 27, 13, 30, tzinfo=EASTERN)
    _, failure = dca_runner._now_fn_clock_failure(
        lambda: now, date(2026, 11, 27), dca_runner.SLOTS["1330"], "1330", None,
    )
    assert failure == "live session: outside regular market hours"
