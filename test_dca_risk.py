"""Tests for the dynamic risk guard (dca_risk) and the sizing kill-switch."""

from __future__ import annotations

import dataclasses
import json
import sys
from datetime import date
from unittest import mock

import pytest

from dca_risk import (
    STREAM_MANUAL,
    STREAM_MIDDAY,
    STREAM_MORNING,
    STREAM_OTHER,
    STREAM_SCHEDULED,
    _trading_day,
    apply_risk_guard,
    build_risk_report,
    classify_stream,
    deployed_by_day,
    deployed_usd,
    deployed_usd_by_claim_date,
    is_afternoon_scheduled_evidence,
    kill_switch_active,
    main,
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

def test_load_states_keeps_contract_rejected_deployed_capital(tmp_path):
    # A contract-rejected state (status=blocked + artifact_contract_failure) with a
    # real submitted order must still contribute its deployed capital to the
    # kill-switch accounting; a pure date-gate blocked stub must not.
    import json as _json

    from dca_risk import _load_states

    root = tmp_path / "scheduled_runs"
    rejected = _scheduled_state("2026-07-06", 42.0)
    rejected["status"] = "blocked"
    rejected["artifact_contract_failure"] = {
        "failure": "artifact_contract_failure", "reason": "compute-only terminal state"
    }
    d1 = root / "20260706_1520ET"
    d1.mkdir(parents=True)
    (d1 / "run_state.json").write_text(_json.dumps(rejected), encoding="utf-8")
    d2 = root / "20260707_1520ET"
    d2.mkdir(parents=True)
    (d2 / "run_state.json").write_text(
        _json.dumps({"trading_day": "2026-07-07", "status": "blocked"}), encoding="utf-8"
    )

    states = _load_states(root)
    # the rejected-but-real state is kept; the pure date-gate stub is dropped
    assert any(s.get("artifact_contract_failure") for s in states)
    assert not any(
        s.get("status") == "blocked" and not s.get("artifact_contract_failure") for s in states
    )
    # its real submitted fill is counted in scheduled deployment accounting
    by_day = deployed_by_day(states)
    assert by_day.get(date(2026, 7, 6)) == 42.0


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
    # The current shared fallback ladder uses 1.5x at 2% and 2.5x at 5%.
    # A 2% drop sizes to 1.5x, a 5%+ drop sizes to 2.5x — unchanged ladder.
    assert graded_buy_multiplier(0.02, guarded) == 1.5
    assert graded_buy_multiplier(0.05, guarded) == 2.5


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


def _slot_state(trading_day: str, deployed: float, *, stream: str) -> dict:
    """A seven-slot-roster run_state (morning/midday) depositing ``deployed``."""
    slot = {
        "morning": dict(
            cron_id="42173154",
            scheduled_for_et="10:30",
            scheduled_window_start_et="10:25",
            scheduled_window_end_et="10:35",
            run_id=f"scheduled_{trading_day.replace('-', '')}_1030ET",
        ),
        "midday": dict(
            cron_id="a6239dae",
            scheduled_for_et="12:30",
            scheduled_window_start_et="12:25",
            scheduled_window_end_et="12:35",
            run_id=f"scheduled_{trading_day.replace('-', '')}_1230ET",
        ),
    }[stream]
    et_time = {"morning": "10:30:01", "midday": "12:30:01"}[stream]
    rc = build_scheduled_classification(
        trading_day,
        f"{trading_day}T{et_time}-04:00",
        stream=stream,
        supersedes_cron_id=None,
        valid_afternoon_scheduled_evidence=False,
        **slot,
    )
    return {
        "trading_day": trading_day,
        "generated_at": f"{trading_day}T{et_time}-04:00",
        "run_classification": rc,
        "buy_candidates": [
            {"ticker": "AAA", "planned_amount_usd": deployed, "status": "submitted"},
        ],
    }


def test_morning_midday_roster_deployment_has_its_own_bucket():
    # Codex review discussion_r3694627885 ("Keep non-afternoon runs out of the
    # afternoon risk stream"): a valid 10:30/12:30 roster run classifies as
    # STREAM_SCHEDULED (Codex review: "Mirror roster identities in the runtime
    # validator"), but its timestamp is always before the 2:30 PM afternoon
    # floor, so it must not count toward the afternoon kill-switch stream --
    # and it must not silently vanish either, now that it is no longer
    # STREAM_OTHER. It needs a bucket of its own.
    as_of = date(2026, 7, 8)
    states = [
        _scheduled_state("2026-07-07", 500.0),          # afternoon
        _slot_state("2026-07-07", 200.0, stream="morning"),
        _slot_state("2026-07-07", 300.0, stream="midday"),
    ]

    # All three are valid scheduled evidence...
    assert all(classify_stream(s) == STREAM_SCHEDULED for s in states)
    # ...but only the afternoon one counts as afternoon evidence, and only it
    # feeds the kill-switch-relevant scheduled bucket.
    scheduled_by_day = deployed_by_day(states, stream=STREAM_SCHEDULED)
    assert scheduled_by_day == {date(2026, 7, 7): 500.0}

    other_by_day = deployed_by_day(states, stream=STREAM_OTHER, require_afternoon_evidence=False)
    assert other_by_day == {}

    report = build_risk_report(states, as_of, _config())
    by_stream = report["deployed_last_7d_usd_by_stream"]
    assert by_stream[STREAM_SCHEDULED] == 500.0
    assert by_stream[STREAM_MORNING] == 200.0
    assert by_stream[STREAM_MIDDAY] == 300.0
    assert by_stream[STREAM_OTHER] == 0.0
    # The 1,000 total across all three slots must never wedge the afternoon
    # kill-switch: only the $500 afternoon slice counts toward it.
    assert report["scheduled_live"]["deployed_last_7d_usd"] == 500.0
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


def test_afternoon_evidence_excludes_only_morning_midday_not_every_other_stream(monkeypatch):
    """Codex review discussion_r3694703749 ("Preserve the documented 14:30
    risk-stream eligibility"): docs/risk_management.md documents a 2:30 PM
    intraday slot -- not yet wired into validate_classification() -- that is
    INTENDED to jointly fund the afternoon kill-switch alongside the 3:30 PM
    slot. is_afternoon_scheduled_evidence() must exclude ONLY the morning/
    midday streams this session actually wired in, not every stream other
    than exactly "afternoon" -- otherwise a future 2:30 PM identity (whatever
    non-"afternoon" stream value it turns out to use) would stay wrongly
    excluded even after validate_classification() is updated to accept it,
    perpetuating the documented undercount rather than only guarding against
    the two streams known today to be non-afternoon.

    validate_classification() doesn't recognize a hypothetical future
    "intraday" stream yet, so classify_stream() is patched to isolate the
    stream-exclusion check itself from that unrelated, not-yet-implemented
    gap."""
    import dca_risk

    monkeypatch.setattr(dca_risk, "classify_stream", lambda state: STREAM_SCHEDULED)

    future_intraday_state = {
        "trading_day": "2026-07-07",
        "generated_at": "2026-07-07T18:30:00Z",  # 14:30 ET
        "run_classification": {"stream": "intraday"},
    }
    assert dca_risk.is_afternoon_scheduled_evidence(future_intraday_state) is True

    morning_state = dict(future_intraday_state)
    morning_state["run_classification"] = {"stream": STREAM_MORNING}
    assert dca_risk.is_afternoon_scheduled_evidence(morning_state) is False

    midday_state = dict(future_intraday_state)
    midday_state["run_classification"] = {"stream": STREAM_MIDDAY}
    assert dca_risk.is_afternoon_scheduled_evidence(midday_state) is False


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


# --- classify_stream coverage ------------------------------------------------

def test_classify_stream_non_dict_state():
    assert classify_stream(None) == STREAM_OTHER
    assert classify_stream([]) == STREAM_OTHER


def test_classify_stream_accepts_the_seven_slot_rosters_morning_identity():
    # Codex review discussion_r3694864526 ("Mirror roster identities in the
    # runtime validator"): classify_stream() calls
    # scripts.run_classification.validate_classification() directly, which
    # previously only ever accepted the afternoon identity -- so every
    # 10:30/12:30 seven-slot-roster run_state (run_dca.run_slot()) classified
    # as STREAM_OTHER instead of STREAM_SCHEDULED, regardless of how correct
    # and complete its own identity fields were.
    rc = build_scheduled_classification(
        "2026-07-06",
        "2026-07-06T14:30:01Z",
        stream="morning",
        cron_id="42173154",
        supersedes_cron_id=None,
        scheduled_for_et="10:30",
        scheduled_window_start_et="10:25",
        scheduled_window_end_et="10:35",
        run_id="scheduled_20260706_1030ET",
        valid_afternoon_scheduled_evidence=False,
    )
    state = {"trading_day": "2026-07-06", "run_classification": rc, "buy_candidates": []}
    assert classify_stream(state) == STREAM_SCHEDULED

def test_classify_stream_scheduled_but_invalid_classification():
    state = _scheduled_state("2026-07-07", 500.0)
    # Break validation by stripping required fields
    state["run_classification"].pop("run_id", None)
    assert classify_stream(state) == STREAM_OTHER

def test_classify_stream_scheduled_missing_manual_flag():
    state = _scheduled_state("2026-07-07", 500.0)
    state["run_classification"].pop("manual_run", None)
    assert classify_stream(state) == STREAM_OTHER

def test_classify_stream_import_error():
    state = _scheduled_state("2026-07-07", 500.0)
    import scripts.run_classification

    # Simulating ImportError for scripts.run_classification by setting it to None in sys.modules
    with mock.patch.dict(sys.modules, {
        "scripts.run_classification": None,
        "run_classification": scripts.run_classification,
    }):
        assert classify_stream(state) == STREAM_SCHEDULED


def test_classify_stream_explicit_manual_origin():
    # If origin is "manual", it should return STREAM_MANUAL even if manual_run is not True
    state = _scheduled_state("2026-07-07", 500.0)
    state["run_classification"]["run_origin"] = "manual"
    state["run_classification"].pop("manual_run", None)
    assert classify_stream(state) == STREAM_MANUAL

def test_classify_stream_unknown_origin():
    state = _scheduled_state("2026-07-07", 500.0)
    state["run_classification"]["run_origin"] = "unknown"
    state["run_classification"]["manual_run"] = False
    assert classify_stream(state) == STREAM_OTHER

def test_run_timestamp_fallback_to_rc_triggered_at_utc():
    from dca_risk import run_timestamp_et
    # State with no generated_at or triggered_at_utc at top level
    state = {
        "run_classification": {
            "triggered_at_utc": "2026-07-07T19:20:00Z"
        }
    }
    dt = run_timestamp_et(state)
    assert dt is not None
    assert dt.hour == 15  # 19:20 UTC -> 15:20 ET

    # State completely lacking timestamps
    assert run_timestamp_et({}) is None
    assert run_timestamp_et({"run_classification": {}}) is None


def test_parse_iso_et_unparseable():
    from dca_risk import _parse_iso_et
    assert _parse_iso_et("") is None
    assert _parse_iso_et("   ") is None
    assert _parse_iso_et("not-a-timestamp") is None

def test_parse_iso_et_naive_datetime():
    from datetime import datetime

    from dca_risk import _parse_iso_et
    dt = datetime(2026, 7, 7, 15, 20)
    parsed = _parse_iso_et(dt)
    assert parsed == dt


def test_trading_day_invalid_date_returns_none():
    """An unparseable trading_day string returns None."""
    state = {"trading_day": "not-a-real-date"}
    assert _trading_day(state) is None

def test_trading_day_missing_or_wrong_type_returns_none():
    """Missing or non-string trading_day returns None."""
    assert _trading_day({}) is None
    assert _trading_day({"trading_day": 12345}) is None


def test_deployed_usd_with_submitted_candidates():
    state = {
        "buy_candidates": [
            {"status": "submitted", "planned_amount_usd": 100.5},
            {"status": "skipped", "planned_amount_usd": 50.0},
            {"status": "submitted", "planned_amount_usd": "200.25"},
            {"status": "submitted", "planned_amount_usd": None},
            {"status": "submitted", "planned_amount_usd": "invalid"},
        ]
    }
    assert deployed_usd(state) == 300.75


def test_deployed_usd_with_candidates_none_submitted():
    state = {
        "buy_candidates": [
            {"status": "skipped", "planned_amount_usd": 50.0},
            {"status": "failed", "planned_amount_usd": 20.0},
        ],
        "budget": {"day_notional_usd": 1000.0}
    }
    assert deployed_usd(state) == 0.0


def test_deployed_usd_fallback_to_budget():
    # buy_candidates must be absent (not an empty list) to fall back to
    # budget.day_notional_usd -- see
    # test_empty_buy_candidates_does_not_fall_back_to_budget_notional above.
    state = {
        "budget": {"day_notional_usd": 500.55}
    }
    assert deployed_usd(state) == 500.55

    state_none = {
        "buy_candidates": None,
        "budget": {"day_notional_usd": "100.5"}
    }
    assert deployed_usd(state_none) == 100.5


def test_deployed_usd_invalid_budget():
    state = {
        "budget": {"day_notional_usd": "invalid"}
    }
    assert deployed_usd(state) == 0.0

    state_no_budget = {}
    assert deployed_usd(state_no_budget) == 0.0


def test_deployed_usd_by_claim_date_attributes_a_reconciled_row_to_its_own_date():
    """Codex review ("Attribute reconciled deployment to the claim date"):
    a whole-share row recovered by cross-day reconciliation carries
    whole_share_claim_breakdown -- the claim's TRUE execution date, which
    can differ from this artifact's own trading_day by days or weeks. That
    row's dollars must bucket under the claim's own date, not today's.
    """
    state = {
        "trading_day": "2026-08-16",
        "buy_candidates": [
            {
                "status": "submitted",
                "planned_amount_usd": 10.0,
                "whole_share_claim_breakdown": [{"trading_day": "2026-08-01", "amount_usd": 10.0}],
            },
        ],
    }
    assert deployed_usd_by_claim_date(state) == {date(2026, 8, 1): 10.0}


def test_deployed_usd_by_claim_date_splits_an_aggregated_row_across_two_dates():
    """A single row can aggregate a reconciled claim from one day with
    today's own fresh contribution (or two reconciled claims from two
    different days) -- the breakdown may not account for the row's whole
    planned_amount_usd, and whatever remainder it leaves uncovered must
    still land on this artifact's own trading_day, exactly like an
    ordinary candidate with no breakdown at all.
    """
    state = {
        "trading_day": "2026-08-16",
        "buy_candidates": [
            {
                "status": "submitted",
                "planned_amount_usd": 25.0,
                "whole_share_claim_breakdown": [{"trading_day": "2026-08-01", "amount_usd": 10.0}],
            },
        ],
    }
    assert deployed_usd_by_claim_date(state) == {
        date(2026, 8, 1): 10.0,
        date(2026, 8, 16): 15.0,
    }


def test_deployed_usd_by_claim_date_falls_back_to_the_artifact_day_without_a_breakdown():
    state = {
        "trading_day": "2026-08-16",
        "buy_candidates": [
            {"status": "submitted", "planned_amount_usd": 40.0},
            {"status": "skipped", "planned_amount_usd": 999.0},
        ],
    }
    assert deployed_usd_by_claim_date(state) == {date(2026, 8, 16): 40.0}


def test_deployed_usd_by_claim_date_falls_back_to_deployed_usd_without_candidates():
    state = {"trading_day": "2026-08-16", "budget": {"day_notional_usd": 75.0}}
    assert deployed_usd_by_claim_date(state) == {date(2026, 8, 16): 75.0}


def test_deployed_by_day_buckets_a_reconciled_claim_under_its_own_trading_day():
    """Integration check: deployed_by_day (the function apply_risk_guard's
    kill-switch seeding actually calls) must reflect the SAME claim-date
    attribution deployed_usd_by_claim_date computes per-state.
    """
    old_claim_state = _scheduled_state("2026-08-16", 10.0)
    old_claim_state["buy_candidates"][0]["whole_share_claim_breakdown"] = [
        {"trading_day": "2026-08-01", "amount_usd": 10.0}
    ]
    per_day = deployed_by_day([old_claim_state], stream=STREAM_SCHEDULED)
    assert per_day == {date(2026, 8, 1): 10.0}
    # A claim dated 2026-08-01 must not appear as trailing 7-day deployment
    # for a kill-switch evaluation on 2026-08-16.
    assert seven_day_deployed(per_day, date(2026, 8, 16)) == 0.0


# --- CLI: main() -------------------------------------------------------------
#
# Every case above exercises the report/guard functions directly; nothing
# exercised the CLI entrypoint an operator or scheduled task actually runs.


def test_main_prints_report_for_a_populated_state_root(tmp_path, capsys):
    root = tmp_path / "scheduled_runs"
    run_dir = root / "20260707_1520ET"
    run_dir.mkdir(parents=True)
    (run_dir / "run_state.json").write_text(
        json.dumps(_scheduled_state("2026-07-07", 500.0)), encoding="utf-8"
    )

    rc = main(["--state-root", str(root), "--as-of", "2026-07-08"])

    assert rc == 0
    report = json.loads(capsys.readouterr().out)
    assert report["deployed_last_7d_usd_by_stream"][STREAM_SCHEDULED] == 500.0


def test_main_on_a_missing_state_root_reports_zero_deployment(tmp_path, capsys):
    """find_run_state_files treats a missing root as empty, not an error."""
    rc = main(["--state-root", str(tmp_path / "does-not-exist"), "--as-of", "2026-07-08"])

    assert rc == 0
    report = json.loads(capsys.readouterr().out)
    assert report["scheduled_live"]["kill_switch_engaged"] is False


def test_main_honors_monthly_allocation_and_kill_switch_fraction_overrides(tmp_path, capsys):
    root = tmp_path / "scheduled_runs"
    run_dir = root / "20260707_1520ET"
    run_dir.mkdir(parents=True)
    # $500 deployed; with a tiny override allocation and a low fraction, the
    # kill-switch must engage even though the default config would not trip.
    (run_dir / "run_state.json").write_text(
        json.dumps(_scheduled_state("2026-07-07", 500.0)), encoding="utf-8"
    )

    rc = main(
        [
            "--state-root",
            str(root),
            "--as-of",
            "2026-07-08",
            "--monthly-allocation-usd",
            "1000",
            "--kill-switch-fraction",
            "0.10",
        ]
    )

    assert rc == 0
    report = json.loads(capsys.readouterr().out)
    assert report["scheduled_live"]["kill_switch_engaged"] is True


def test_main_defaults_as_of_to_today(tmp_path, capsys):
    """Omitting --as-of must not crash; it defaults to today's date."""
    rc = main(["--state-root", str(tmp_path / "does-not-exist")])
    assert rc == 0
    report = json.loads(capsys.readouterr().out)
    assert report["scheduled_live"]["kill_switch_engaged"] is False
