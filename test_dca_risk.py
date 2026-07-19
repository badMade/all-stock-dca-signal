"""Tests for the dynamic risk guard (dca_risk) and the sizing kill-switch."""

from __future__ import annotations

import dataclasses
from datetime import date

import pytest

from dca_risk import (
    STREAM_MANUAL,
    STREAM_OTHER,
    STREAM_SCHEDULED,
    _trading_day,
    apply_risk_guard,
    build_risk_report,
    classify_stream,
    deployed_by_day,
    deployed_usd,
    is_afternoon_scheduled_evidence,
    kill_switch_active,
    rolling_cash_burn,
    seven_day_deployed,
)
from near_close_dca import StrategyConfig, graded_buy_multiplier, kill_switch_engaged
from scripts.run_classification import build_manual_classification, build_scheduled_classification

# Monthly allocation used across tests: 40% threshold => $1,440.
ALLOCATION = 3600.0
THRESHOLD = 0.40 * ALLOCATION  # 1440.0


def _scheduled_state(trading_day: str, deployed: float, *, et_time: str = "15:20:00") -> dict:
    """A scheduled-live run_state depositing ``deployed`` via one submitted candidate."""
    return {
        "trading_day": trading_day,
        "generated_at": f"{trading_day}T{et_time}-04:00",
        "run_classification": build_scheduled_classification(trading_day),
        "buy_candidates": [
            {"ticker": "AAA", "planned_amount_usd": deployed, "status": "submitted"},
        ],
    }


def _manual_state(trading_day: str, deployed: float) -> dict:
    return {
        "trading_day": trading_day,
        "generated_at": f"{trading_day}T15:22:00-04:00",
        "run_classification": build_manual_classification(),
        "buy_candidates": [
            {"ticker": "BBB", "planned_amount_usd": deployed, "status": "submitted"},
        ],
    }


def _config(**over) -> StrategyConfig:
    base = dict(monthly_cash_allocation_usd=ALLOCATION, kill_switch_7d_deploy_fraction=0.40)
    base.update(over)
    return dataclasses.replace(StrategyConfig(), **base)


# --- Case 1: normal case, no kill-switch -------------------------------------

def test_normal_case_no_kill_switch_preserves_ladder():
    """Deployment below 40% of monthly leaves the full graded ladder intact."""
    as_of = date(2026, 7, 8)
    # $600 deployed over the trailing week — well under the $1,440 threshold.
    states = [
        _scheduled_state("2026-07-06", 300.0),
        _scheduled_state("2026-07-07", 300.0),
    ]
    by_day = deployed_by_day(states)
    guarded = apply_risk_guard(_config(), by_day, as_of)

    assert guarded.deployed_last_7d_usd == 600.0
    assert kill_switch_engaged(guarded) is False
    # A 2% drop still sizes to 2x, a 5%+ drop still to 3.5x — unchanged ladder.
    assert graded_buy_multiplier(0.02, guarded) == 2.0
    assert graded_buy_multiplier(0.05, guarded) == 3.5


# --- Case 2: 7-day deployed > 40% monthly triggers cap to 1x -----------------

def test_kill_switch_caps_multiplier_to_one():
    """7-day deployment above 40% of monthly caps every positive multiplier at 1x."""
    as_of = date(2026, 7, 8)
    # $1,500 in the trailing 7 days -> above the $1,440 threshold.
    states = [
        _scheduled_state("2026-07-06", 800.0),
        _scheduled_state("2026-07-07", 700.0),
    ]
    by_day = deployed_by_day(states)
    guarded = apply_risk_guard(_config(), by_day, as_of)

    assert guarded.deployed_last_7d_usd == 1500.0
    assert seven_day_deployed(by_day, as_of) == 1500.0
    assert kill_switch_active(1500.0, ALLOCATION, 0.40) is True
    assert kill_switch_engaged(guarded) is True

    # Everything that would scale up is capped to 1x...
    assert graded_buy_multiplier(0.02, guarded) == 1.0   # would be 2x
    assert graded_buy_multiplier(0.08, guarded) == 1.0   # would be 5x
    assert graded_buy_multiplier(0.005, guarded) == 1.0  # already 1x
    # ...but a no-buy stays a no-buy (the guard never forces participation).
    assert graded_buy_multiplier(0.0005, guarded) == 0.0


def test_kill_switch_boundary_is_strictly_greater_than_threshold():
    """Exactly at the threshold does not engage; a cent over does."""
    assert kill_switch_active(THRESHOLD, ALLOCATION, 0.40) is False
    assert kill_switch_active(THRESHOLD + 0.01, ALLOCATION, 0.40) is True


# --- Case 3: zero / no deployment history ------------------------------------

def test_zero_deployment_history():
    """No artifacts -> zero burn, guard disengaged, full ladder."""
    as_of = date(2026, 7, 8)
    by_day = deployed_by_day([])
    assert by_day == {}
    assert seven_day_deployed(by_day, as_of) == 0.0

    burn = rolling_cash_burn(by_day, as_of)
    assert burn["total_deployed_usd"] == 0.0
    assert burn["daily_burn_rate_usd"] == 0.0
    assert burn["active_deployment_days"] == 0

    guarded = apply_risk_guard(_config(), by_day, as_of)
    assert guarded.deployed_last_7d_usd == 0.0
    assert kill_switch_engaged(guarded) is False
    assert graded_buy_multiplier(0.08, guarded) == 5.0  # full ladder intact


def test_candidates_present_but_none_submitted_deploy_zero():
    state = _scheduled_state("2026-07-07", 500.0)
    state["buy_candidates"][0]["status"] = "skipped"
    assert deployed_usd(state) == 0.0


def test_empty_buy_candidates_does_not_fall_back_to_budget_notional():
    # Codex review #9 on PR #22: when the top-up guard seeds cross-stream
    # (morning/midday) prior allocations into the afternoon StrategyState but
    # the afternoon run itself has no BUY candidates, the artifact's
    # buy_candidates list is explicitly empty ([]), not absent. An empty list
    # means "nothing was deployed this run" -- deployed_usd() must not fall
    # back to budget.day_notional_usd, which can reflect the seeded
    # cross-stream capital rather than an actual afternoon-submitted order,
    # double-counting a morning/midday spend as afternoon deployment.
    state = _scheduled_state("2026-07-07", 0.0)
    state["buy_candidates"] = []
    state["budget"] = {"day_notional_usd": 300.0}
    assert deployed_usd(state) == 0.0


def test_non_positive_allocation_disables_guard():
    """A mis-/unconfigured allocation must never wedge the ladder to 1x."""
    guarded = dataclasses.replace(_config(monthly_cash_allocation_usd=0.0), deployed_last_7d_usd=9999.0)
    assert kill_switch_engaged(guarded) is False
    assert graded_buy_multiplier(0.08, guarded) == 5.0


# --- Case 4: streams / run-classification not conflated ----------------------

def test_streams_not_conflated():
    """Manual and other streams never count toward scheduled-live deployment."""
    as_of = date(2026, 7, 8)
    states = [
        _scheduled_state("2026-07-07", 500.0),  # scheduled
        _manual_state("2026-07-07", 900.0),     # manual — must be excluded
    ]

    scheduled_by_day = deployed_by_day(states, stream=STREAM_SCHEDULED)
    manual_by_day = deployed_by_day(states, stream=STREAM_MANUAL)

    assert scheduled_by_day == {date(2026, 7, 7): 500.0}
    assert manual_by_day == {date(2026, 7, 7): 900.0}

    guarded = apply_risk_guard(_config(), scheduled_by_day, as_of)
    # Scheduled-only 7-day deployment is $500 (< $1,440); the $900 manual run is
    # NOT folded in, so the guard stays disengaged.
    assert guarded.deployed_last_7d_usd == 500.0
    assert kill_switch_engaged(guarded) is False

    report = build_risk_report(states, as_of, _config())
    assert report["deployed_last_7d_usd_by_stream"][STREAM_SCHEDULED] == 500.0
    assert report["deployed_last_7d_usd_by_stream"][STREAM_MANUAL] == 900.0
    assert report["scheduled_live"]["kill_switch_engaged"] is False


def test_manual_flag_wins_over_scheduled_origin():
    """A state marked scheduled but flagged manual is treated as manual, not scheduled."""
    state = _scheduled_state("2026-07-07", 500.0)
    state["run_classification"]["manual_run"] = True
    assert classify_stream(state) == STREAM_MANUAL
    assert deployed_by_day([state], stream=STREAM_SCHEDULED) == {}


def test_afternoon_evidence_before_230pm_rejected():
    """Scheduled evidence timestamped before 2:30 PM ET is not counted as afternoon."""
    early = _scheduled_state("2026-07-07", 900.0, et_time="09:45:00")  # morning
    late = _scheduled_state("2026-07-08", 900.0, et_time="15:20:00")   # afternoon

    assert is_afternoon_scheduled_evidence(early) is False
    assert is_afternoon_scheduled_evidence(late) is True

    by_day = deployed_by_day([early, late], stream=STREAM_SCHEDULED)
    # Only the afternoon artifact contributes; the pre-2:30 PM one is dropped.
    assert by_day == {date(2026, 7, 8): 900.0}


def test_utc_timestamp_converted_to_et_for_floor():
    """A 19:20Z scheduled run is 15:20 ET -> valid afternoon evidence."""
    state = {
        "trading_day": "2026-07-07",
        "generated_at": "2026-07-07T19:20:00Z",
        "run_classification": build_scheduled_classification("2026-07-07"),
        "buy_candidates": [{"ticker": "AAA", "planned_amount_usd": 10.0, "status": "submitted"}],
    }
    assert is_afternoon_scheduled_evidence(state) is True


def test_unclassified_state_is_other_stream():
    state = {"trading_day": "2026-07-07", "buy_candidates": []}
    assert classify_stream(state) == STREAM_OTHER


# --- 30-day rolling burn rate ------------------------------------------------

def test_rolling_30day_burn_rate_window_and_average():
    as_of = date(2026, 7, 30)
    states = [
        _scheduled_state("2026-07-02", 300.0),  # inside window
        _scheduled_state("2026-07-15", 600.0),  # inside window
        _scheduled_state("2026-06-25", 999.0),  # 35 days before -> excluded
    ]
    by_day = deployed_by_day(states)
    burn = rolling_cash_burn(by_day, as_of)
    assert burn["window_days"] == 30
    assert burn["total_deployed_usd"] == 900.0
    assert burn["daily_burn_rate_usd"] == pytest.approx(30.0)  # 900 / 30
    assert burn["active_deployment_days"] == 2

def test_trading_day_invalid_date_returns_none():
    """An unparseable trading_day string returns None."""
    state = {"trading_day": "not-a-real-date"}
    assert _trading_day(state) is None

def test_trading_day_missing_or_wrong_type_returns_none():
    """Missing or non-string trading_day returns None."""
    assert _trading_day({}) is None
    assert _trading_day({"trading_day": 12345}) is None
