"""Tests for the dynamic risk guard (dca_risk) and the sizing kill-switch."""

from __future__ import annotations

import dataclasses
import json
import sys
from datetime import date
from typing import Any
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
    has_unusable_deployed_notional,
    is_afternoon_scheduled_evidence,
    kill_switch_active,
    main,
    rolling_cash_burn,
    seven_day_deployed,
    unusable_deployed_notional_dates,
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


# --- Evidence accounting -----------------------------------------------------
#
# Regression for "Reject missing risk-history evidence". Every figure in the
# report is a sum over the states that loaded, so a zero is ambiguous between
# "nothing deployed" and "nothing read" -- and a consumer that takes the second
# for the first leaves the kill switch disengaged.


def test_evidence_reports_a_missing_state_root(tmp_path):
    """find_run_state_files returns [] for a root that does not exist, so the
    report is all zeroes and exits 0. The evidence block is what makes that
    distinguishable from a genuinely quiet week."""
    from dca_risk import _load_states_with_evidence

    states, evidence = _load_states_with_evidence(tmp_path / "nope")
    assert states == []
    assert evidence["state_root_exists"] is False
    assert evidence["run_state_files_found"] == 0
    assert evidence["run_states_loaded"] == 0


def test_evidence_counts_an_unreadable_state(tmp_path):
    """An unreadable state is deliberately skipped rather than failing the whole
    report -- so it has to be COUNTED, or a corrupted recent afternoon state
    silently lowers the deployed figure."""
    import json as _json

    from dca_risk import _load_states_with_evidence

    root = tmp_path / "scheduled_runs"
    good = root / "20260707_1520ET"
    good.mkdir(parents=True)
    (good / "run_state.json").write_text(
        _json.dumps(_scheduled_state("2026-07-07", 500.0)), encoding="utf-8"
    )
    bad = root / "20260708_1520ET"
    bad.mkdir(parents=True)
    (bad / "run_state.json").write_text("{ not json", encoding="utf-8")

    states, evidence = _load_states_with_evidence(root)
    assert len(states) == 1
    assert evidence["run_state_files_found"] == 2
    assert evidence["run_states_loaded"] == 1
    assert evidence["run_states_unreadable"] == 1
    assert evidence["unreadable_paths"] == [str(bad / "run_state.json")]


def test_evidence_is_clean_for_a_fully_readable_tree(tmp_path):
    """The other side: a real zero-spend week must not look like missing
    evidence, or the consumer aborts every quiet week."""
    import json as _json

    from dca_risk import _load_states_with_evidence

    root = tmp_path / "scheduled_runs"
    day = root / "20260707_1520ET"
    day.mkdir(parents=True)
    (day / "run_state.json").write_text(
        _json.dumps(_scheduled_state("2026-07-07", 0.0)), encoding="utf-8"
    )

    _states, evidence = _load_states_with_evidence(root)
    assert evidence["state_root_exists"] is True
    assert evidence["run_states_loaded"] == 1
    assert evidence["run_states_unreadable"] == 0
    assert evidence["unreadable_paths"] == []


def test_evidence_separates_a_parsed_but_uninformative_state(tmp_path):
    """Regression for "Validate loaded run states before counting evidence".

    `{}` is valid JSON: it parses, is a dict, is not blocked, and appends -- so
    it counted as loaded with nothing unreadable, while classify_stream can only
    return STREAM_OTHER for it. A consumer told "1 loaded, 0 unreadable" would
    treat a zero from that as proven.
    """
    from dca_risk import _load_states_with_evidence

    root = tmp_path / "scheduled_runs"
    day = root / "20260707_1520ET"
    day.mkdir(parents=True)
    (day / "run_state.json").write_text("{}", encoding="utf-8")

    states, evidence = _load_states_with_evidence(root)
    assert states == [{}]
    assert evidence["run_states_loaded"] == 1
    assert evidence["run_states_unreadable"] == 0
    assert evidence["run_states_unclassifiable"] == 1
    assert evidence["afternoon_states_seen"] == 0


def test_evidence_counts_a_manual_only_tree_as_no_afternoon_history(tmp_path):
    """A tree of perfectly valid manual runs loads cleanly and proves nothing
    about afternoon deployment -- the only gate that funds the kill switch."""
    import json as _json

    from dca_risk import _load_states_with_evidence

    root = tmp_path / "manual_runs"
    day = root / "20260707_1522ET"
    day.mkdir(parents=True)
    (day / "run_state.json").write_text(
        _json.dumps(_manual_state("2026-07-07", 900.0)), encoding="utf-8"
    )

    _states, evidence = _load_states_with_evidence(root)
    assert evidence["run_states_loaded"] == 1
    assert evidence["run_states_unclassifiable"] == 0
    assert evidence["afternoon_states_seen"] == 0


@pytest.mark.parametrize("stream", ["morning", "midday"])
def test_evidence_counts_a_roster_only_tree_as_no_afternoon_history(tmp_path, stream):
    """Regression for "Count only afternoon states as kill-switch evidence".

    The seven-slot roster's own 10:30/12:30 artifacts ARE valid STREAM_SCHEDULED
    evidence -- build_risk_report buckets them under STREAM_MORNING/STREAM_MIDDAY
    -- but is_afternoon_scheduled_evidence excludes them by design, so
    deployed_by_day counts none of them toward the scheduled-live figure.
    Counting the broader stream made such a tree report positive evidence for a
    figure it contributes nothing to.
    """
    import json as _json

    from dca_risk import _load_states_with_evidence

    state = _slot_state("2026-07-07", 200.0, stream=stream)
    # Precondition: this genuinely IS scheduled evidence, just not afternoon.
    assert classify_stream(state) == STREAM_SCHEDULED
    assert is_afternoon_scheduled_evidence(state) is False

    root = tmp_path / f"{stream}_dca"
    day = root / "20260707_run"
    day.mkdir(parents=True)
    (day / "run_state.json").write_text(_json.dumps(state), encoding="utf-8")

    _states, evidence = _load_states_with_evidence(root)
    assert evidence["run_states_loaded"] == 1
    assert evidence["run_states_unclassifiable"] == 0
    assert evidence["afternoon_states_seen"] == 0


def test_evidence_sees_a_real_scheduled_state(tmp_path):
    """The other side, and the one that matters most: a genuine scheduled tree
    must satisfy every clause, or the prompt aborts on a healthy install."""
    import json as _json

    from dca_risk import _load_states_with_evidence

    root = tmp_path / "scheduled_runs"
    day = root / "20260707_1520ET"
    day.mkdir(parents=True)
    (day / "run_state.json").write_text(
        _json.dumps(_scheduled_state("2026-07-07", 500.0)), encoding="utf-8"
    )

    _states, evidence = _load_states_with_evidence(root)
    assert evidence["state_root_exists"] is True
    assert evidence["run_states_loaded"] == 1
    assert evidence["run_states_unreadable"] == 0
    assert evidence["run_states_unclassifiable"] == 0
    assert evidence["afternoon_states_seen"] == 1


def test_main_publishes_the_evidence_block(tmp_path, capsys):
    """The CLI is what the executor prompt actually reads, so the block has to
    reach its stdout -- not just the helper's return value."""
    import json as _json

    from dca_risk import main

    root = tmp_path / "scheduled_runs"
    day = root / "20260707_1520ET"
    day.mkdir(parents=True)
    (day / "run_state.json").write_text(
        _json.dumps(_scheduled_state("2026-07-07", 500.0)), encoding="utf-8"
    )

    assert main(["--state-root", str(root), "--as-of", "2026-07-07"]) == 0
    report = _json.loads(capsys.readouterr().out)
    assert report["evidence"]["run_states_loaded"] == 1
    assert report["evidence"]["run_states_unreadable"] == 0
    # Additive only: the pre-existing fields keep their shape.
    assert "scheduled_live" in report and "deployed_last_7d_usd" in report["scheduled_live"]


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


def test_load_states_keeps_recovered_deployment_evidence(tmp_path):
    """Codex review ("Load blocked recovery evidence in the risk CLI"): a
    blocked stub marked ``recovered_deployment_evidence=True`` (written by
    ``_persist_gated_whole_share_recovery`` when a blocked invocation
    reconciles an order belonging to an older, valid afternoon whole-share
    claim) must be kept, mirroring
    ``is_afternoon_scheduled_evidence``'s own identical exception -- a pure
    date-gate blocked stub with neither marker must still be dropped.
    """
    import json as _json

    from dca_risk import _load_states

    root = tmp_path / "scheduled_runs"
    recovered = _scheduled_state("2026-07-06", 25.0)
    recovered["status"] = "blocked"
    recovered["recovered_deployment_evidence"] = True
    d1 = root / "20260706_1520ET"
    d1.mkdir(parents=True)
    (d1 / "run_state.json").write_text(_json.dumps(recovered), encoding="utf-8")
    d2 = root / "20260707_1520ET"
    d2.mkdir(parents=True)
    (d2 / "run_state.json").write_text(
        _json.dumps({"trading_day": "2026-07-07", "status": "blocked"}), encoding="utf-8"
    )

    states = _load_states(root)
    # the recovery-marked state is kept; the pure date-gate stub is dropped
    assert any(s.get("recovered_deployment_evidence") for s in states)
    assert not any(
        s.get("status") == "blocked" and not s.get("recovered_deployment_evidence") for s in states
    )
    # its recovered notional is counted in scheduled deployment accounting
    by_day = deployed_by_day(states)
    assert by_day.get(date(2026, 7, 6)) == 25.0


def test_load_states_keeps_partially_blocked_states_with_placed_evidence(tmp_path):
    # Codex review, PR #1300 discussion_r3920912125 ("Keep partial blocked
    # states in risk scans"): raw persistence failing AFTER an order was
    # already placed writes a fully populated status:"blocked" artifact for
    # that real candidate, with NO artifact_contract_failure marker at all
    # (unlike the compute-only-terminal-state case the sibling test above
    # covers). Dropping it here understated deployed_last_7d_usd_by_stream
    # for that morning/midday stream. A pure date-gate stub with no order
    # evidence must still be dropped.
    import json as _json

    from dca_risk import _load_states

    root = tmp_path / "scheduled_runs"
    partial = _scheduled_state("2026-07-06", 42.0)
    partial["status"] = "blocked"  # no artifact_contract_failure marker
    d1 = root / "20260706_1520ET"
    d1.mkdir(parents=True)
    (d1 / "run_state.json").write_text(_json.dumps(partial), encoding="utf-8")
    d2 = root / "20260707_1520ET"
    d2.mkdir(parents=True)
    (d2 / "run_state.json").write_text(
        _json.dumps({"trading_day": "2026-07-07", "status": "blocked"}), encoding="utf-8"
    )

    states = _load_states(root)
    assert any(s.get("trading_day") == "2026-07-06" for s in states)
    assert not any(s.get("trading_day") == "2026-07-07" for s in states)
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


@pytest.mark.parametrize("status", ["filled", "pending", "unconfirmed"])
def test_restored_broker_order_statuses_count_as_deployed(status):
    state = _scheduled_state("2026-07-07", 500.0)
    state["buy_candidates"][0]["status"] = status
    state["buy_candidates"][0]["order_id"] = "broker-order-1"

    assert deployed_usd(state) == 500.0


@pytest.mark.parametrize("status", ["filled", "pending", "unconfirmed"])
def test_restored_broker_order_statuses_reach_the_kill_switch(status):
    # Codex review, P1 ("Apply restored statuses in claim-date accounting"):
    # production aggregation never calls deployed_usd() directly -- the
    # rolling burn and the kill switch both go through deployed_by_day() ->
    # deployed_usd_by_claim_date(). A claim-date loop that still accepted only
    # "submitted" would report $0 for exactly these restored orders and could
    # authorize further purchases on top of capital already at the broker.
    state = _scheduled_state("2026-07-07", 500.0)
    state["buy_candidates"][0]["status"] = status
    state["buy_candidates"][0]["order_id"] = "broker-order-1"

    assert deployed_usd_by_claim_date(state) == {date(2026, 7, 7): 500.0}
    by_day = deployed_by_day([state])
    assert by_day == {date(2026, 7, 7): 500.0}
    assert seven_day_deployed(by_day, date(2026, 7, 7)) == 500.0


def test_restored_status_row_still_honours_its_claim_date_breakdown():
    # A restored row can also be a cross-day reconciled whole-share row: the
    # widened status set must not bypass the per-claim-date split, or an
    # out-of-window claim would land in today's kill-switch bucket.
    state = _scheduled_state("2026-08-03", 10.0)
    candidate = state["buy_candidates"][0]
    candidate["status"] = "filled"
    candidate["planned_amount_usd"] = 10.0
    candidate["whole_share_claim_breakdown"] = [
        {"trading_day": "2026-08-01", "amount_usd": 4.0},
    ]

    assert deployed_usd_by_claim_date(state) == {
        date(2026, 8, 1): 4.0,
        date(2026, 8, 3): 6.0,
    }


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
    state_none: Any = None
    state_list: Any = []
    assert classify_stream(state_none) == STREAM_OTHER
    assert classify_stream(state_list) == STREAM_OTHER


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


def test_deployed_usd_counts_restored_in_flight_statuses():
    """Regression test for "Count restored in-flight spend in deployment
    risk". A same-slot retry can restore a prior attempt's "filled"/
    "pending"/"unconfirmed" candidate unchanged (near_close_dca.
    run_daily_compact's merge) -- deployed_usd() must recognize those as
    real deployed capital too, not just the exact "submitted" status, or
    the 7-day kill switch values a real in-flight/completed order at $0.
    "not_submitted_confirmation_required" never reached the broker and must
    still be excluded.
    """
    state = {
        "buy_candidates": [
            {"status": "filled", "planned_amount_usd": 10.0},
            {"status": "pending", "planned_amount_usd": 20.0},
            {"status": "unconfirmed", "planned_amount_usd": 30.0},
            {"status": "not_submitted_confirmation_required", "planned_amount_usd": 1000.0},
            {"status": "skipped", "planned_amount_usd": 1000.0},
        ]
    }
    assert deployed_usd(state) == 60.0


def test_deployed_usd_counts_potentially_submitted_without_the_legacy_flag():
    # Codex review, PR #1299 (dca_risk.py:93): "potentially_submitted" is a
    # valid schema status on its own -- the legacy ambiguous_placement_
    # exception boolean flag is not required to accompany it. A recovered/
    # imported run state carrying only the status (no flag) must still count
    # as deployed capital, or deployed_usd()/deployed_usd_by_claim_date()
    # understate rolling deployment and let a later live run reuse
    # kill-switch headroom for an order that may still exist at the broker.
    state = {
        "buy_candidates": [
            {"status": "potentially_submitted", "planned_amount_usd": 12.0},
        ]
    }
    assert deployed_usd(state) == 12.0


def test_deployed_usd_by_claim_date_counts_potentially_submitted_without_the_legacy_flag():
    state = {
        "trading_day": "2026-06-29",
        "buy_candidates": [
            {"status": "potentially_submitted", "planned_amount_usd": 12.0},
        ],
    }
    assert deployed_usd_by_claim_date(state) == {date(2026, 6, 29): 12.0}


def test_deployed_usd_by_claim_date_counts_restored_in_flight_statuses():
    state = {
        "trading_day": "2026-06-29",
        "buy_candidates": [
            {"status": "filled", "planned_amount_usd": 15.0},
            {"status": "pending", "planned_amount_usd": 25.0},
            {"status": "not_submitted_confirmation_required", "planned_amount_usd": 999.0},
        ],
    }
    assert deployed_usd_by_claim_date(state) == {date(2026, 6, 29): 40.0}


def test_deployed_usd_ignores_an_unhashable_candidate_status():
    # Codex review ("Validate statuses before set membership"): widening the
    # status check from `== "submitted"` to set membership made it partial.
    # `_load_states()` admits any valid-JSON dict, so a corrupt artifact can
    # carry `"status": []` or `{}`; hashing those to test set membership raises
    # TypeError. The equality comparison it replaced was total -- a non-string
    # status was simply not-submitted. That property has to survive.
    state = {
        "buy_candidates": [
            {"status": [], "planned_amount_usd": 999.0},
            {"status": {}, "planned_amount_usd": 999.0},
            {"status": 7, "planned_amount_usd": 999.0},
            {"status": None, "planned_amount_usd": 999.0},
            {"status": "submitted", "planned_amount_usd": 10.0},
        ]
    }
    assert deployed_usd(state) == 10.0


def test_deployed_usd_ignores_non_finite_committed_notionals():
    state = {
        "trading_day": "2026-07-06",
        "buy_candidates": [
            {"status": "filled", "planned_amount_usd": "NaN"},
            {"status": "pending", "planned_amount_usd": "Infinity"},
            {"status": "unconfirmed", "planned_amount_usd": 10.0},
        ],
    }
    assert deployed_usd(state) == 10.0


def test_deployed_usd_by_claim_date_ignores_non_finite_breakdown_amounts():
    state = {
        "trading_day": "2026-07-06",
        "buy_candidates": [
            {
                "status": "filled",
                "planned_amount_usd": 10.0,
                "whole_share_claim_breakdown": [
                    {"trading_day": "2026-07-05", "amount_usd": "NaN"},
                    {"trading_day": "2026-07-06", "amount_usd": 10.0},
                ],
            }
        ],
    }
    assert deployed_usd_by_claim_date(state) == {date(2026, 7, 6): 10.0}


def test_unusable_notional_keeps_artifact_day_for_unclaimed_remainder():
    """An uncertain row's claim-date remainder is uncertain on its artifact day."""
    state = {
        "trading_day": "2026-07-10",
        "buy_candidates": [
            {
                "status": "submitted",
                "planned_amount_usd": 20.0,
                "deployed_notional_uncertain": True,
                "whole_share_claim_breakdown": [
                    {"trading_day": "2026-06-30", "amount_usd": 10.0},
                ],
            }
        ],
    }

    assert deployed_usd_by_claim_date(state) == {
        date(2026, 6, 30): 10.0,
        date(2026, 7, 10): 10.0,
    }
    assert unusable_deployed_notional_dates(state) == {
        date(2026, 6, 30),
        date(2026, 7, 10),
    }


def test_deployed_usd_by_claim_date_ignores_an_unhashable_candidate_status():
    # The identical expression in deployed_usd_by_claim_date() needs the same
    # guard -- it is reached by the per-claim-date rollup, not by deployed_usd.
    state = {
        "trading_day": "2026-07-06",
        "buy_candidates": [
            {"status": [], "planned_amount_usd": 999.0},
            {"status": "filled", "planned_amount_usd": 25.0},
        ],
    }
    per_date = deployed_usd_by_claim_date(state)
    assert sum(per_date.values()) == 25.0


def test_one_corrupt_artifact_does_not_abort_the_whole_risk_summary():
    # The consequence that makes it more than cosmetic: deployed_by_day() wraps
    # no per-artifact boundary, so a single malformed candidate would take down
    # the rolling kill-switch calculation for every other run alongside it.
    corrupt = _scheduled_state("2026-07-06", 0.0)
    corrupt["buy_candidates"] = [{"status": [], "planned_amount_usd": 999.0}]
    healthy = _scheduled_state("2026-07-06", 40.0)

    by_day = deployed_by_day([corrupt, healthy])
    assert by_day.get(date(2026, 7, 6)) == 40.0


def test_deployed_usd_with_candidates_none_submitted():
    state = {
        "buy_candidates": [
            {"status": "skipped", "planned_amount_usd": 50.0},
            {"status": "failed", "planned_amount_usd": 20.0},
        ],
        "budget": {"day_notional_usd": 1000.0}
    }
    assert deployed_usd(state) == 0.0


@pytest.mark.parametrize("planned_amount", [None, "invalid"])
def test_ambiguous_failed_candidate_has_unusable_deployed_notional(planned_amount):
    """Ambiguous placement is deployed evidence even when status is failed."""
    state = {
        "trading_day": "2026-07-07",
        "buy_candidates": [
            {
                "status": "failed",
                "ambiguous_placement_exception": True,
                "planned_amount_usd": planned_amount,
            },
        ],
    }

    assert deployed_usd(state) == 0.0
    assert has_unusable_deployed_notional(state) is True


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


def test_deployed_by_day_rejects_partially_attributable_scheduled_state():
    """A dated sibling must not conceal an undated deployed candidate.

    This deliberately uses valid scheduled/afternoon metadata so the production
    aggregation path reaches claim-date bucketing.  The malformed artifact day
    leaves the second placed candidate unfiled; keeping the first candidate's
    dated $50 bucket would understate the state's actual broker exposure.
    """
    state = _scheduled_state("2026-08-16", 0.0)
    state["trading_day"] = "not-a-date"
    state["buy_candidates"] = [
        {
            "ticker": "AAPL",
            "status": "filled",
            "planned_amount_usd": 50.0,
            "whole_share_claim_breakdown": [
                {"trading_day": "2026-08-01", "amount_usd": 50.0},
            ],
        },
        {
            "ticker": "BYDDY",
            "status": "filled",
            "planned_amount_usd": 50.0,
        },
    ]

    assert deployed_usd_by_claim_date(state) == {}
    assert deployed_by_day([state]) == {}


def test_deployed_usd_by_claim_date_falls_back_to_the_artifact_day_without_a_breakdown():
    state = {
        "trading_day": "2026-08-16",
        "buy_candidates": [
            {"status": "submitted", "planned_amount_usd": 40.0},
            {"status": "skipped", "planned_amount_usd": 999.0},
        ],
    }
    assert deployed_usd_by_claim_date(state) == {date(2026, 8, 16): 40.0}


def test_deployed_usd_by_claim_date_counts_ambiguous_placement_reservations():
    state = {
        "trading_day": "2026-08-16",
        "buy_candidates": [
            {
                "status": "failed",
                "ambiguous_placement_exception": True,
                "planned_amount_usd": 40.0,
            },
        ],
    }
    assert deployed_usd(state) == 40.0
    assert deployed_usd_by_claim_date(state) == {date(2026, 8, 16): 40.0}


def test_deployed_usd_by_claim_date_falls_back_to_deployed_usd_without_candidates():
    state = {"trading_day": "2026-08-16", "budget": {"day_notional_usd": 75.0}}
    assert deployed_usd_by_claim_date(state) == {date(2026, 8, 16): 75.0}


def test_deployed_usd_by_claim_date_dedupes_a_repeated_order_id():
    """codex review discussion_r3887931343 ("Deduplicate recovered copies in
    deployment accounting"): duplicate historical artifacts for the same
    accepted order share one order_id. seen_order_ids, when threaded in,
    must skip a second occurrence of an already-seen order_id entirely."""
    state = {
        "trading_day": "2026-08-16",
        "buy_candidates": [
            {"status": "submitted", "planned_amount_usd": 10.0, "order_id": "ORD-DUP-1"},
            {"status": "submitted", "planned_amount_usd": 10.0, "order_id": "ORD-DUP-1"},
        ],
    }
    seen: set = set()
    assert deployed_usd_by_claim_date(state, seen_order_ids=seen) == {date(2026, 8, 16): 10.0}
    assert seen == {"ORD-DUP-1"}

    # No seen_order_ids given: unchanged, backward-compatible behavior sums
    # both rows.
    assert deployed_usd_by_claim_date(state) == {date(2026, 8, 16): 20.0}


def test_deployed_usd_by_claim_date_never_dedupes_rows_without_an_order_id():
    """Legacy evidence with no order_id at all must never be deduplicated
    against another such row -- there is no identity to compare."""
    state = {
        "trading_day": "2026-08-16",
        "buy_candidates": [
            {"status": "submitted", "planned_amount_usd": 10.0},
            {"status": "submitted", "planned_amount_usd": 10.0},
        ],
    }
    seen: set = set()
    assert deployed_usd_by_claim_date(state, seen_order_ids=seen) == {date(2026, 8, 16): 20.0}


def test_deployed_by_day_counts_a_duplicated_order_id_once():
    """Integration check: deployed_by_day (the function apply_risk_guard's
    kill-switch seeding actually calls) threads ONE seen_order_ids set
    across its whole sweep over states, so the SAME broker order recorded
    in TWO durable artifacts (e.g. duplicate historical copies of one
    fractional placement's evidence) is summed once, not once per copy."""
    state_a = _scheduled_state("2026-08-16", 10.0)
    state_a["buy_candidates"][0]["order_id"] = "ORD-DUP-1"
    state_b = _scheduled_state("2026-08-16", 10.0)
    state_b["buy_candidates"][0]["order_id"] = "ORD-DUP-1"

    per_day = deployed_by_day([state_a, state_b], stream=STREAM_SCHEDULED)
    assert per_day == {date(2026, 8, 16): 10.0}

    # Distinct order ids are never deduplicated against each other.
    state_c = _scheduled_state("2026-08-16", 10.0)
    state_c["buy_candidates"][0]["order_id"] = "ORD-DISTINCT-2"
    per_day_distinct = deployed_by_day([state_a, state_c], stream=STREAM_SCHEDULED)
    assert per_day_distinct == {date(2026, 8, 16): 20.0}

    # Legacy evidence with no order_id at all is never deduplicated.
    state_d = _scheduled_state("2026-08-16", 10.0)
    state_e = _scheduled_state("2026-08-16", 10.0)
    per_day_legacy = deployed_by_day([state_d, state_e], stream=STREAM_SCHEDULED)
    assert per_day_legacy == {date(2026, 8, 16): 20.0}


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


def _corrupt_state_at(root, run_dir: str) -> None:
    """An unreadable run_state under a dated run directory."""
    d = root / "scheduled_runs" / run_dir
    d.mkdir(parents=True, exist_ok=True)
    (d / "run_state.json").write_text("{ not json", encoding="utf-8")


def test_unusable_states_outside_the_window_do_not_block(tmp_path):
    """Regression for "Scope unusable-state counts to relevant risk history".

    deployed_by_day feeds seven_day_deployed, which sums only the trailing
    KILL_SWITCH_WINDOW_DAYS ending at as_of -- so an artifact dated outside that
    window cannot move the total however corrupt it is. Counting it blocked a
    consumer on evidence irrelevant by construction, and permanently: a legacy
    corrupt file never heals, so one of them wedged Gate 5 forever.
    """
    from dca_risk import _load_states_with_evidence

    as_of = date(2026, 8, 26)
    _corrupt_state_at(tmp_path, "20260101_1530ET")  # ~8 months stale

    _states, evidence = _load_states_with_evidence(tmp_path, as_of=as_of)

    assert evidence["run_states_unreadable"] == 0
    assert evidence["unreadable_paths"] == []
    # Reported, not hidden -- the archive is still rotting and an operator
    # should be able to see that.
    assert evidence["unusable_states_outside_window"] == 1
    assert len(evidence["outside_window_paths"]) == 1


def test_unusable_states_inside_the_window_still_block(tmp_path):
    """The fail-closed half: a corrupt artifact that COULD move the figure counts.

    This is the case the whole evidence block exists for, and the scoping must
    not weaken it.
    """
    from dca_risk import _load_states_with_evidence

    as_of = date(2026, 8, 26)
    _corrupt_state_at(tmp_path, "20260824_1530ET")  # two days back, in window

    _states, evidence = _load_states_with_evidence(tmp_path, as_of=as_of)

    assert evidence["run_states_unreadable"] == 1
    assert evidence["unusable_states_outside_window"] == 0


def test_an_undated_unusable_path_is_treated_as_in_window(tmp_path):
    """Unknown means possibly relevant, never "old".

    The date comes from the run directory name; a path that does not carry one
    yields no evidence either way, and a fail-closed check must not resolve that
    ambiguity in the direction that stops counting.
    """
    from dca_risk import _load_states_with_evidence

    as_of = date(2026, 8, 26)
    d = tmp_path / "scheduled_runs" / "no_date_here"
    d.mkdir(parents=True)
    (d / "run_state.json").write_text("{ not json", encoding="utf-8")

    _states, evidence = _load_states_with_evidence(tmp_path, as_of=as_of)

    assert evidence["run_states_unreadable"] == 1
    assert evidence["unusable_states_outside_window"] == 0


def test_omitting_as_of_keeps_the_counts_archive_wide(tmp_path):
    """_load_states' own callers pass no window and must see every unusable file.

    Guards the scoping against leaking into the un-windowed path, where there is
    no figure to be relevant to.
    """
    from dca_risk import _load_states_with_evidence

    _corrupt_state_at(tmp_path, "20260101_1530ET")

    _states, evidence = _load_states_with_evidence(tmp_path)

    assert evidence["run_states_unreadable"] == 1
    assert evidence["unusable_states_outside_window"] == 0


def test_a_future_dated_unusable_state_still_blocks(tmp_path):
    """Regression for "Keep future reconciliation artifacts relevant to past windows".

    The window scoping above is one-sided on purpose. deployed_usd_by_claim_date
    files a reconciled whole-share row under the trading_day its claim breakdown
    records -- an EARLIER day than the artifact that recovered it -- so a run
    dated after as_of can still carry in-window deployment. Excluding it by its
    own date let a corrupted one hide that capital behind
    run_states_unreadable == 0, which is exactly the zero the evidence block
    exists to make unmistakable.
    """
    from dca_risk import _load_states_with_evidence

    as_of = date(2026, 8, 26)
    _corrupt_state_at(tmp_path, "20260830_1530ET")  # four days AFTER the report date

    _states, evidence = _load_states_with_evidence(tmp_path, as_of=as_of)

    assert evidence["run_states_unreadable"] == 1
    assert evidence["unusable_states_outside_window"] == 0


def test_a_future_artifact_really_can_carry_in_window_deployment(tmp_path):
    """The premise of the test above, proven rather than asserted.

    If a later-dated artifact could not reach back into the window, excluding a
    corrupted one would be harmless and the one-sided bound would be arbitrary.
    This shows the readable form of the same artifact moving the very figure the
    corrupted form would have silently zeroed.
    """
    import json as _json

    from dca_risk import _load_states_with_evidence, deployed_by_day, seven_day_deployed

    root = tmp_path / "scheduled_runs"
    day = root / "20260830_1530ET"
    day.mkdir(parents=True)
    state = _scheduled_state("2026-08-30", 0.0)
    state["buy_candidates"] = [
        {
            "ticker": "AAA",
            "planned_amount_usd": 120.0,
            "status": "submitted",
            # Recovered on Aug 30, but the capital moved on Aug 24.
            "whole_share_claim_breakdown": [
                {"trading_day": "2026-08-24", "amount_usd": 120.0}
            ],
        },
    ]
    (day / "run_state.json").write_text(_json.dumps(state), encoding="utf-8")

    states, _evidence = _load_states_with_evidence(root, as_of=date(2026, 8, 26))
    assert seven_day_deployed(deployed_by_day(states), date(2026, 8, 26)) == 120.0


def test_an_afternoon_state_without_a_trading_day_is_not_counted_as_evidence(tmp_path):
    """Regression for "Reject afternoon states without a usable trading day".

    Nothing in is_afternoon_scheduled_evidence reads trading_day -- it checks the
    classification and the 2:30 PM floor -- so a state missing it sailed into
    afternoon_states_seen while deployed_usd_by_claim_date dropped every one of
    its candidates for want of a date to file them under. The consumer then read
    "1 afternoon state seen, 0 deployed" and left the kill switch disengaged on a
    zero the loader guaranteed.
    """
    import json as _json

    from dca_risk import _load_states_with_evidence

    root = tmp_path / "scheduled_runs"
    day = root / "20260707_1520ET"
    day.mkdir(parents=True)
    state = _scheduled_state("2026-07-07", 500.0)
    del state["trading_day"]
    (day / "run_state.json").write_text(_json.dumps(state), encoding="utf-8")

    _states, evidence = _load_states_with_evidence(root)

    assert evidence["run_states_loaded"] == 1
    assert evidence["run_states_unclassifiable"] == 0
    assert evidence["afternoon_states_seen"] == 0
    assert evidence["afternoon_states_undated"] == 1


def test_a_malformed_trading_day_is_treated_the_same_as_a_missing_one(tmp_path):
    """_trading_day returns None for both, and so must the evidence count."""
    import json as _json

    from dca_risk import _load_states_with_evidence

    root = tmp_path / "scheduled_runs"
    day = root / "20260707_1520ET"
    day.mkdir(parents=True)
    state = _scheduled_state("2026-07-07", 500.0)
    state["trading_day"] = "not-a-date"
    (day / "run_state.json").write_text(_json.dumps(state), encoding="utf-8")

    _states, evidence = _load_states_with_evidence(root)

    assert evidence["afternoon_states_seen"] == 0
    assert evidence["afternoon_states_undated"] == 1


def test_an_undated_state_whose_claims_carry_their_own_dates_still_counts(tmp_path):
    """The half that must NOT be excluded, or the fix trades one silent zero for
    a permanent false abort.

    A reconciliation artifact whose every submitted candidate carries a dated
    whole_share_claim_breakdown never consults the artifact's own trading_day --
    the amounts land on the claim dates. Checking trading_day alone would have
    rejected a state that contributes in full.
    """
    import json as _json

    from dca_risk import _load_states_with_evidence

    root = tmp_path / "scheduled_runs"
    day = root / "20260707_1520ET"
    day.mkdir(parents=True)
    state = _scheduled_state("2026-07-07", 0.0)
    del state["trading_day"]
    state["buy_candidates"] = [
        {
            "ticker": "AAA",
            "planned_amount_usd": 80.0,
            "status": "submitted",
            "whole_share_claim_breakdown": [
                {"trading_day": "2026-07-06", "amount_usd": 80.0}
            ],
        },
    ]
    (day / "run_state.json").write_text(_json.dumps(state), encoding="utf-8")

    _states, evidence = _load_states_with_evidence(root)

    assert evidence["afternoon_states_seen"] == 1
    assert evidence["afternoon_states_undated"] == 0


def test_a_healthy_afternoon_state_reports_no_undated_evidence(tmp_path):
    """The ordinary case keeps its shape: the new field is additive and zero."""
    import json as _json

    from dca_risk import _load_states_with_evidence

    root = tmp_path / "scheduled_runs"
    day = root / "20260707_1520ET"
    day.mkdir(parents=True)
    (day / "run_state.json").write_text(
        _json.dumps(_scheduled_state("2026-07-07", 500.0)), encoding="utf-8"
    )

    _states, evidence = _load_states_with_evidence(root)

    assert evidence["afternoon_states_seen"] == 1
    assert evidence["afternoon_states_undated"] == 0


def test_a_corrupt_state_inside_the_burn_window_still_blocks(tmp_path):
    """Regression for "Track corrupt states across the 30-day burn window".

    build_risk_report publishes rolling_cash_burn over BURN_WINDOW_DAYS as well
    as the kill switch's own 7-day figure, so scoping the evidence counts to 7
    days let an 8-30 day old corrupt state read clean while it understated the
    30-day total and daily burn rate.
    """
    from dca_risk import _load_states_with_evidence

    as_of = date(2026, 8, 26)
    _corrupt_state_at(tmp_path, "20260810_1530ET")  # 16 days back: past 7, inside 30

    _states, evidence = _load_states_with_evidence(tmp_path, as_of=as_of)

    assert evidence["run_states_unreadable"] == 1
    assert evidence["unusable_states_outside_window"] == 0


def test_a_corrupt_state_beyond_the_burn_window_does_not_block(tmp_path):
    """The other half: past every published figure's horizon, it still cannot
    matter, and the permanent-wedge case round 22 raised stays fixed."""
    from dca_risk import _load_states_with_evidence

    as_of = date(2026, 8, 26)
    _corrupt_state_at(tmp_path, "20260101_1530ET")

    _states, evidence = _load_states_with_evidence(tmp_path, as_of=as_of)

    assert evidence["run_states_unreadable"] == 0
    assert evidence["unusable_states_outside_window"] == 1


def test_the_evidence_window_covers_every_published_figure():
    """The bound is derived, not written down twice.

    Adding a figure over a longer window must widen this automatically rather
    than silently leaving the evidence counts scoped to a shorter one.
    """
    from dca_risk import BURN_WINDOW_DAYS, EVIDENCE_WINDOW_DAYS, KILL_SWITCH_WINDOW_DAYS

    assert EVIDENCE_WINDOW_DAYS >= BURN_WINDOW_DAYS
    assert EVIDENCE_WINDOW_DAYS >= KILL_SWITCH_WINDOW_DAYS


def test_a_misfiled_tree_stops_trusting_its_own_path_dates(tmp_path):
    """Regression for "Validate path dates before discarding corrupt states".

    A directory date is a naming convention, not an invariant -- dca_paths.
    stream_root joins whatever component its caller hands it. So a state can sit
    under a stale directory while attributing capital to the current window, and
    once that file is corrupt there are no readable dates left to check it by.

    What CAN still be checked is whether the tree honours the convention at all.
    Here one readable state under a January directory books August capital; that
    proves the path dates lie in this tree, so the equally-stale corrupt file is
    counted rather than waved past.
    """
    import json as _json

    from dca_risk import _load_states_with_evidence

    root = tmp_path / "scheduled_runs"
    misfiled = root / "20260102_1530ET"
    misfiled.mkdir(parents=True)
    (misfiled / "run_state.json").write_text(
        _json.dumps(_scheduled_state("2026-08-25", 500.0)), encoding="utf-8"
    )
    _corrupt_state_at(tmp_path, "20260101_1530ET")

    _states, evidence = _load_states_with_evidence(root.parent, as_of=date(2026, 8, 26))

    assert evidence["run_states_unreadable"] == 1
    assert evidence["unusable_states_outside_window"] == 0


def test_a_well_filed_tree_keeps_the_path_date_exclusion(tmp_path):
    """The guard must not fire on the ordinary case, or round 22's fix is lost.

    A reconciled row attributing capital to a day EARLIER than its own directory
    is the normal cross-day recovery shape, not a contradiction -- only a date
    later than the directory's own proves the convention broken.
    """
    import json as _json

    from dca_risk import _load_states_with_evidence

    root = tmp_path / "scheduled_runs"
    recent = root / "20260825_1530ET"
    recent.mkdir(parents=True)
    state = _scheduled_state("2026-08-25", 0.0)
    state["buy_candidates"] = [
        {
            "ticker": "AAA",
            "planned_amount_usd": 60.0,
            "status": "submitted",
            "whole_share_claim_breakdown": [
                {"trading_day": "2026-08-19", "amount_usd": 60.0}
            ],
        },
    ]
    (recent / "run_state.json").write_text(_json.dumps(state), encoding="utf-8")
    _corrupt_state_at(tmp_path, "20260101_1530ET")

    _states, evidence = _load_states_with_evidence(root.parent, as_of=date(2026, 8, 26))

    assert evidence["run_states_unreadable"] == 0
    assert evidence["unusable_states_outside_window"] == 1


def test_one_healthy_afternoon_state_cannot_vouch_for_an_undated_one(tmp_path):
    """Regression for "Require zero undated states before sizing".

    The earlier fix excluded undated states from afternoon_states_seen and
    claimed that was enough to make the documented gate fail closed. It is not:
    a single healthy afternoon state keeps afternoon_states_seen > 0, so every
    documented check passed while the undated state's real submitted notional
    was dropped from the total. afternoon_states_undated must be asserted in its
    own right, which is what docs/midday_dca.md and the entry-point catalog now
    require.
    """
    import json as _json

    from dca_risk import _load_states_with_evidence

    root = tmp_path / "scheduled_runs"
    healthy = root / "20260825_1520ET"
    healthy.mkdir(parents=True)
    (healthy / "run_state.json").write_text(
        _json.dumps(_scheduled_state("2026-08-25", 100.0)), encoding="utf-8"
    )
    undated = root / "20260826_1520ET"
    undated.mkdir(parents=True)
    state = _scheduled_state("2026-08-26", 900.0)
    del state["trading_day"]
    (undated / "run_state.json").write_text(_json.dumps(state), encoding="utf-8")

    _states, evidence = _load_states_with_evidence(root)

    # The pre-existing clauses all still pass -- that is the point.
    assert evidence["run_states_unreadable"] == 0
    assert evidence["run_states_unclassifiable"] == 0
    assert evidence["afternoon_states_seen"] == 1
    # Only this one catches the $900 that deployed_usd_by_claim_date drops.
    assert evidence["afternoon_states_undated"] == 1


def test_the_documented_evidence_gate_names_the_undated_count():
    """The published contract has to carry the clause, not just the code.

    A consumer follows docs/midday_dca.md and scripts/agent_entry_points.py; a
    count nothing tells them to check protects nobody.
    """
    from pathlib import Path

    from scripts.agent_entry_points import ENTRY_POINTS

    doc = Path("docs/midday_dca.md").read_text(encoding="utf-8")
    assert "`afternoon_states_undated` | `== 0`" in doc

    midday = [e for e in ENTRY_POINTS if "1230ET" in e.need]
    assert midday, "the 1230ET row must exist"
    assert any("afternoon_states_undated == 0" in e.notes for e in midday)


def test_overfilled_breakdown_with_an_undated_claim_retains_artifact_uncertainty():
    """An undated overfill cannot be safely attributed only to dated siblings."""
    from dca_risk import unusable_deployed_notional_dates

    state = {
        "trading_day": "2026-08-26",
        "buy_candidates": [
            {
                "ticker": "AAPL",
                "status": "submitted",
                "planned_amount_usd": 10.0,
                "whole_share_claim_breakdown": [
                    {"trading_day": "2026-08-10", "amount_usd": 8.0},
                    {"amount_usd": 8.0},
                ],
            }
        ],
    }

    assert unusable_deployed_notional_dates(state) == {date(2026, 8, 10), date(2026, 8, 26)}


def test_a_malformed_sibling_entry_does_not_discard_a_valid_claim_date():
    """Codex review ("Preserve valid claim dates when a sibling entry is
    malformed"): the row_unusable early exit used to discard claim_dates
    accumulated from OTHER, valid breakdown entries, keeping only the
    artifact-day fallback the malformed sibling itself added. During a
    historical replay where that artifact day is outside the trailing
    evidence window (e.g. a reconciliation artifact written after the
    fact), the known historical claim contributed neither deployment nor
    uncertainty and could incorrectly leave the live cap open.
    """
    from dca_risk import unusable_deployed_notional_dates

    state = {
        "trading_day": "2026-08-26",
        "buy_candidates": [
            {
                "ticker": "AAPL",
                "status": "submitted",
                "planned_amount_usd": "NaN",  # unusable top-level notional
                "whole_share_claim_breakdown": [
                    {"trading_day": "2026-08-10", "amount_usd": 8.0},  # valid
                    {"amount_usd": "NaN"},  # malformed sibling: unusable amount
                ],
            }
        ],
    }

    dates = unusable_deployed_notional_dates(state)
    assert date(2026, 8, 10) in dates, (
        "the valid historical claim date must survive a malformed sibling "
        "entry elsewhere in the same breakdown"
    )
    assert date(2026, 8, 26) in dates


def test_malformed_sibling_entry_does_not_discard_a_valid_siblings_claim_date():
    # Codex review: "Preserve valid claim dates when a sibling entry is
    # malformed". An unusable top-level notional with a valid, dated
    # breakdown entry AND a malformed (non-dict) sibling entry must retain
    # the valid entry's own claim date, not just the malformed sibling's
    # artifact-day fallback -- otherwise a known historical claim
    # contributes neither deployment nor uncertainty for its own day and
    # the live cap can stay open when it should be reserving that capital.
    from dca_risk import unusable_deployed_notional_dates

    state = {
        "trading_day": "2026-08-26",
        "buy_candidates": [
            {
                "ticker": "AAPL",
                "status": "submitted",
                "planned_amount_usd": "unknown",
                "whole_share_claim_breakdown": [
                    {"trading_day": "2026-08-10", "amount_usd": 5.0},
                    "not-a-dict",
                ],
            }
        ],
    }

    assert unusable_deployed_notional_dates(state) == {date(2026, 8, 10), date(2026, 8, 26)}


def test_unparseable_row_total_keeps_artifact_day_uncertain():
    """A row whose top-level notional cannot be parsed as a number at all
    (not merely negative/non-finite) has no total to compare accounted_usd
    against, so completeness of a fully-dated breakdown can never be
    established -- the artifact day must stay uncertain rather than being
    cleared just because every breakdown entry happened to be well-formed
    (Codex review: "Preserve uncertainty for an unaccounted breakdown
    remainder")."""
    from dca_risk import unusable_deployed_notional_dates

    state = {
        "trading_day": "2026-08-31",
        "buy_candidates": [
            {
                "ticker": "AAPL",
                "status": "submitted",
                "planned_amount_usd": "NaN",
                "whole_share_claim_breakdown": [
                    {"trading_day": "2026-08-28", "amount_usd": 50.0},
                ],
            }
        ],
    }

    assert unusable_deployed_notional_dates(state) == {date(2026, 8, 28), date(2026, 8, 31)}
    # deployed_usd_by_claim_date() agrees this row is entirely unreadable --
    # it drops the whole thing, including the $50 breakdown entry -- so both
    # dates genuinely need to stay flagged as uncertain evidence.
    assert deployed_usd_by_claim_date(state) == {}


def test_row_total_that_exactly_matches_a_readable_breakdown_clears_artifact_day():
    """Regression guard: when the top-level total IS a readable, non-negative
    number (so ``planned`` is not None) and a fully-dated breakdown accounts
    for every dollar of it, the artifact day must NOT be marked uncertain --
    only a genuinely un-parseable total (the test above) or a real
    unaccounted remainder should keep it in the set. Uses
    ``deployed_notional_uncertain`` to make the row unusable without an
    unparseable amount, so ``planned`` stays a real, comparable number."""
    from dca_risk import unusable_deployed_notional_dates

    state = {
        "trading_day": "2026-08-31",
        "buy_candidates": [
            {
                "ticker": "AAPL",
                "status": "submitted",
                "planned_amount_usd": 50.0,
                "deployed_notional_uncertain": True,
                "whole_share_claim_breakdown": [
                    {"trading_day": "2026-08-28", "amount_usd": 50.0},
                ],
            }
        ],
    }

    assert unusable_deployed_notional_dates(state) == {date(2026, 8, 28)}


def test_malformed_breakdown_sibling_does_not_drop_a_valid_claim_date():
    """A malformed or unusable SIBLING entry in the same breakdown must not
    erase an already-parsed valid claim date collected earlier in the same
    loop -- deployed_usd_by_claim_date() drops the WHOLE row the moment any
    part of it is unreadable, so the valid claim date is one of the
    surviving pieces of evidence that day's deployment is uncertain (Codex
    review: "Retain valid claim dates when a sibling entry is malformed").
    The artifact day is ALSO uncertain here: row_unusable means at least one
    entry's own amount could not be read, so accounted_usd is necessarily
    incomplete and completeness can never be established (Codex review:
    "Preserve artifact-day uncertainty in malformed breakdowns")."""
    from dca_risk import unusable_deployed_notional_dates

    state = {
        "trading_day": "2026-08-31",
        "buy_candidates": [
            {
                "ticker": "AAPL",
                "status": "submitted",
                "planned_amount_usd": "NaN",
                "whole_share_claim_breakdown": [
                    {"trading_day": "2026-08-29", "amount_usd": 50.0},
                    {"trading_day": "2026-09-15", "amount_usd": "garbage"},
                ],
            }
        ],
    }

    assert unusable_deployed_notional_dates(state) == {
        date(2026, 8, 29),
        date(2026, 9, 15),
        date(2026, 8, 31),
    }
    assert deployed_usd_by_claim_date(state) == {}


def test_unreadable_breakdown_with_no_other_entries_still_flags_artifact_day():
    """A row whose top-level total is unparseable and whose ONLY breakdown
    entry is also malformed must not lose artifact-day uncertainty just
    because there happens to be no other, valid sibling to fall back on --
    deployed_usd_by_claim_date() still drops the whole row, and a fresh,
    entirely unaccounted remainder could belong to the artifact day (Codex
    review: "Preserve artifact-day uncertainty in malformed breakdowns")."""
    from dca_risk import unusable_deployed_notional_dates

    state = {
        "trading_day": "2026-08-31",
        "buy_candidates": [
            {
                "ticker": "AAPL",
                "status": "submitted",
                "planned_amount_usd": "NaN",
                "whole_share_claim_breakdown": [
                    {"trading_day": "2026-01-01", "amount_usd": "garbage"},
                ],
            }
        ],
    }

    assert unusable_deployed_notional_dates(state) == {date(2026, 1, 1), date(2026, 8, 31)}
    assert deployed_usd_by_claim_date(state) == {}


def test_zero_dollar_breakdown_entry_is_not_propagated_as_uncertain():
    """A breakdown entry with a genuine, VERIFIED $0.00 amount is real
    evidence of no deployment on that date -- near_close_dca.py emits these
    for recovered zero-fill attempts. Sweeping its date into the
    row_notional_unusable propagation alongside a genuinely malformed
    sibling would flag a date whose evidence was never actually missing
    (Codex review: "Exclude zero-dollar claims from sibling
    propagation")."""
    from dca_risk import unusable_deployed_notional_dates

    state = {
        "trading_day": "2026-08-31",
        "buy_candidates": [
            {
                "ticker": "AAPL",
                "status": "submitted",
                "planned_amount_usd": "NaN",
                "whole_share_claim_breakdown": [
                    {"trading_day": "2026-08-29", "amount_usd": 0.0},
                    {"trading_day": "2026-01-01", "amount_usd": "garbage"},
                ],
            }
        ],
    }

    assert unusable_deployed_notional_dates(state) == {date(2026, 1, 1), date(2026, 8, 31)}
    assert date(2026, 8, 29) not in unusable_deployed_notional_dates(state)


def test_overfilled_row_stays_uncertain_even_with_a_malformed_sibling():
    """A row whose top-level total IS readable but whose GOOD breakdown
    entries alone already exceed it is overfilled -- deployed_usd_by_claim_date()
    caps that entry's attribution at the row's own total, so the current
    attribution is only an artifact of which entries happened to be
    readable, not a verified fact. The overfill check must still run even
    when a DIFFERENT sibling entry is separately malformed -- accounted_usd
    only ever sums independently-readable entries, so the overfill is real
    regardless -- rather than short-circuiting on row_unusable and leaving
    only the malformed entry's own date flagged (Codex review: "Retain
    dates for overfilled readable rows")."""
    from dca_risk import unusable_deployed_notional_dates

    state = {
        "trading_day": "2026-08-31",
        "buy_candidates": [
            {
                "ticker": "AAPL",
                "status": "submitted",
                "planned_amount_usd": 10.0,
                "whole_share_claim_breakdown": [
                    {"trading_day": "2026-08-31", "amount_usd": 20.0},
                    {"trading_day": "2026-01-01", "amount_usd": "garbage"},
                ],
            }
        ],
    }

    assert unusable_deployed_notional_dates(state) == {date(2026, 1, 1), date(2026, 8, 31)}
    # deployed_usd_by_claim_date() caps the $20 claim at the row's own $10
    # total and books it all to the artifact day -- an attribution the
    # overfill logic above correctly refuses to trust on its own.
    assert deployed_usd_by_claim_date(state) == {date(2026, 8, 31): 10.0}


def test_overfill_reconstruction_excludes_a_zero_dollar_dated_sibling():
    """The overfill branch rebuilds claim_dates from scratch to distrust
    individual per-entry attribution, but a dated entry with a genuine,
    verified $0.00 amount contributes nothing to either accounted_usd or
    deployed_usd_by_claim_date() -- it must not be swept into that
    reconstruction just because its date happens to be readable (Codex
    review: "Filter zero claims in the newly reachable overfill path")."""
    from dca_risk import unusable_deployed_notional_dates

    state = {
        "trading_day": "2026-08-31",
        "buy_candidates": [
            {
                "ticker": "AAPL",
                "status": "submitted",
                "planned_amount_usd": 10.0,
                "whole_share_claim_breakdown": [
                    {"trading_day": "2026-08-31", "amount_usd": 20.0},
                    {"trading_day": "2026-01-01", "amount_usd": "garbage"},
                    {"trading_day": "2026-08-30", "amount_usd": 0.0},
                ],
            }
        ],
    }

    assert unusable_deployed_notional_dates(state) == {date(2026, 1, 1), date(2026, 8, 31)}
    assert date(2026, 8, 30) not in unusable_deployed_notional_dates(state)
    assert deployed_usd_by_claim_date(state) == {date(2026, 8, 31): 10.0}


def test_valid_row_total_does_not_propagate_sibling_dates_on_a_bad_entry():
    """Regression guard for the fix above: sibling-date propagation on
    row_unusable must be scoped to row_notional_unusable. When the row's OWN
    top-level total IS a usable number, deployed_usd_by_claim_date() does
    NOT drop the whole row just because one breakdown entry is malformed --
    it still books every valid entry under its own date and the remainder
    under the artifact day. Propagating every sibling claim date here in
    that case would flag a date whose evidence was never actually lost, only
    the malformed entry's own date (already added directly, independent of
    claim_dates) is genuinely uncertain (Codex review: "Restrict sibling
    claim propagation to unusable row totals")."""
    from dca_risk import unusable_deployed_notional_dates

    state = {
        "trading_day": "2026-08-31",
        "buy_candidates": [
            {
                "ticker": "AAPL",
                "status": "submitted",
                "planned_amount_usd": 100.0,
                "whole_share_claim_breakdown": [
                    {"trading_day": "2026-08-29", "amount_usd": 50.0},
                    {"trading_day": "2026-06-01", "amount_usd": "garbage"},
                ],
            }
        ],
    }

    assert unusable_deployed_notional_dates(state) == {date(2026, 6, 1)}
    # deployed_usd_by_claim_date() agrees the valid entry and the remainder
    # are both fully accounted for -- nothing was silently dropped for
    # 2026-08-29 or the artifact day, so neither belongs in the result above.
    assert deployed_usd_by_claim_date(state) == {
        date(2026, 8, 29): 50.0,
        date(2026, 8, 31): 50.0,
    }


def test_deployed_notional_reader_rejects_an_oversized_json_integer():
    """A syntactically valid JSON integer can still overflow float()."""
    from dca_risk import has_unusable_deployed_notional

    state = {
        "trading_day": "2026-08-26",
        "buy_candidates": [
            {"ticker": "AAPL", "status": "submitted", "planned_amount_usd": 10**10000}
        ],
    }

    assert has_unusable_deployed_notional(state) is True


def test_nonnumeric_row_total_retains_artifact_day_even_with_a_fully_dated_breakdown():
    # Codex review: "Retain artifact-day uncertainty when the row total is
    # unknown". A whole_share_claim_breakdown is allowed to cover only part
    # of a row -- any remainder belongs to the artifact day. When the row's
    # own planned_amount_usd is nonnumeric (not merely uncertain-but-numeric),
    # there is no known total to compare the breakdown sum against, so we can
    # never prove the breakdown is exhaustive. Every claim-date entry here is
    # validly dated (no has_unusable_claim_date), which previously caused the
    # artifact day to be dropped entirely -- silently hiding an unbounded
    # omitted remainder from the trailing evidence window.
    from dca_risk import unusable_deployed_notional_dates

    state = {
        "trading_day": "2026-08-26",
        "buy_candidates": [
            {
                "ticker": "AAPL",
                "status": "submitted",
                "planned_amount_usd": "unknown",
                "whole_share_claim_breakdown": [
                    {"trading_day": "2026-08-10", "amount_usd": 5.0},
                ],
            }
        ],
    }

    assert unusable_deployed_notional_dates(state) == {date(2026, 8, 10), date(2026, 8, 26)}
