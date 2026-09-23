"""Tests for scripts/finalize_run_evidence.py: the one Gate 9 command every live prompt calls.

Every slot must come out verified by both ``same_day_pointer.validate_pointer``
and ``verify_schedule``; off-window starts and inconsistent order claims must be
refused before anything is written; an executor-written run_state is never
overwritten; a retry of the same firing merges orders instead of dropping them.
"""

import csv
import json
import sys
from datetime import date, datetime
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from scripts import finalize_run_evidence as fre  # noqa: E402
from scripts.same_day_pointer import validate_pointer  # noqa: E402
from scripts.verify_schedule import SLOT_EVIDENCE_LAYOUT, slot_run_dir_relative, verify_day  # noqa: E402

ET = ZoneInfo("America/New_York")
DAY = date(2026, 9, 22)
NOW = datetime(2026, 9, 22, 17, 0, tzinfo=ET)
ORDER = {"ticker": "MA", "order_id": "ord-1", "submitted_amount_usd": 2.5, "status": "filled"}
BUDGET = {"budget_tier": "Normal", "daily_cap_usd": 180, "weekly_cap_usd": 900}


@pytest.fixture(autouse=True)
def synthetic_account(monkeypatch):
    monkeypatch.setenv("ROBINHOOD_AGENTIC_ACCOUNT_NUMBER", "999000111")


def started(slot_id, minute=2):
    return datetime(2026, 9, 22, int(slot_id[:2]), int(slot_id[2:]) + minute, tzinfo=ET)


def run(tmp_path, slot_id="1430", status="no_orders_submitted", **kwargs):
    if status == "submitted":
        kwargs.setdefault("budget", BUDGET)
    return fre.finalize(slot_id, kwargs.pop("start", started(slot_id)), outcome=kwargs.pop("outcome", "NO_ORDERS"),
                        order_submit_status=status, root=tmp_path, now=NOW, **kwargs)


def read_csv(path):
    with path.open(newline="", encoding="utf-8") as handle:
        return list(csv.DictReader(handle))


def test_pointer_names_match_the_executor_classes():
    import intraday_dca
    import market_open_dca
    import midday_dca
    import morning_dca

    assert fre.POINTER_NAMES == {
        "market_open": market_open_dca.MarketOpenDCAExecutor.latest_pointer_name,
        "morning": morning_dca.MorningDCAExecutor.latest_pointer_name,
        "midday": midday_dca.MiddayDCAExecutor.latest_pointer_name,
        "intraday": intraday_dca.IntradayDCAExecutor.latest_pointer_name,
    }


@pytest.mark.parametrize("slot_id", sorted(SLOT_EVIDENCE_LAYOUT))
def test_every_slot_finalizes_to_verified_evidence(tmp_path, slot_id):
    result = run(tmp_path, slot_id)
    assert result["verified"] is True, result["errors"]
    assert result["pointer_status"] == "valid" and result["slot_status"] == "verified"
    stream, _ = SLOT_EVIDENCE_LAYOUT[slot_id]
    pointer = tmp_path / result["pointer"]
    check = validate_pointer(pointer, expected_stream=stream, expected_root=pointer.parent, today=DAY.isoformat())
    assert check.is_valid
    run_dir = tmp_path / slot_run_dir_relative(slot_id, DAY)
    state = json.loads((run_dir / "run_state.json").read_text())
    rc = state["run_classification"]
    assert rc["run_origin"] == "scheduled" and rc["manual_run"] is False and rc["valid_scheduled_evidence"] is True
    assert state["finalized_by"] == "scripts.finalize_run_evidence"
    # The CSV contract: header-only files exist even with zero rows.
    assert (run_dir / "submitted_orders.csv").read_text().startswith("ticker,order_id")
    assert json.loads((run_dir / "run_summary.json").read_text())["run_classification"] == rc
    slots = {row["slot_id"]: row["status"] for row in verify_day(DAY, NOW, root=tmp_path)["slots"]}
    assert slots[slot_id] == "verified"


def test_live_classification_is_the_executors_own(tmp_path):
    import intraday_dca

    run(tmp_path, "1330")
    state = json.loads((tmp_path / "intraday_dca/20260922_1330ET/run_state.json").read_text())
    expected = intraday_dca.IntradayDCAExecutor(mode="live", slot="1330").run_classification(started("1330"))
    expected["valid_scheduled_evidence"] = True
    # The pinned account never reaches the artifact (Copilot review, PR #1514).
    assert expected.pop("account_number") == "999000111"
    expected["account_number_redacted"] = True
    assert state["run_classification"] == expected
    assert "999000111" not in (tmp_path / "intraday_dca/20260922_1330ET/run_state.json").read_text()


def test_window_accepts_the_prompts_early_gate1_start_but_nothing_earlier(tmp_path):
    # The live 2:30 prompt's Gate 1 accepts 2:25-2:55; dca_runner's window opens at 2:30.
    assert run(tmp_path, "1430", start=datetime(2026, 9, 22, 14, 25, tzinfo=ET))["verified"] is True
    assert run(tmp_path, "0935", start=datetime(2026, 9, 22, 9, 30, tzinfo=ET))["verified"] is True
    with pytest.raises(fre.FinalizeRefused, match="acceptance window"):
        run(tmp_path, "1030", start=datetime(2026, 9, 22, 10, 24, tzinfo=ET))
    assert fre.acceptance_window("1230", DAY)[1].strftime("%H:%M") == "13:00"
    # The end is exclusive, as dca_runner.gate_time_sanity checks it.
    with pytest.raises(fre.FinalizeRefused, match="acceptance window"):
        run(tmp_path, "1230", start=datetime(2026, 9, 22, 13, 0, tzinfo=ET))


def test_off_window_start_is_refused_and_writes_nothing(tmp_path):
    with pytest.raises(fre.FinalizeRefused, match="acceptance window"):
        run(tmp_path, "1430", start=datetime(2026, 9, 22, 15, 40, tzinfo=ET))
    assert not any(tmp_path.iterdir())


@pytest.mark.parametrize("status, orders, match", [
    ("submitted", None, "needs at least one order row"),
    ("no_orders_submitted", [ORDER], "use 'submitted'"),
    ("dry_run", None, "is not one of"),
    ("submitted", [{**ORDER, "status": "rejected"}], "not a placed or in-flight order"),
    ("submitted", [{**ORDER, "order_id": ""}], "no broker order_id"),
    ("submitted", [{**ORDER, "ticker": None}], "no ticker"),
    ("submitted", [{**ORDER, "submitted_amount_usd": 0}], "not a positive number"),
    ("submitted", [{**ORDER, "submitted_amount_usd": "2.5"}], "not a positive number"),
    ("submitted", [ORDER, {**ORDER, "ticker": "ANET"}], "appears more than once"),
])
def test_inconsistent_order_claims_are_refused(tmp_path, status, orders, match):
    with pytest.raises(fre.FinalizeRefused, match=match):
        run(tmp_path, status=status, orders=orders)
    assert not (tmp_path / "intraday_dca/20260922_1430ET").exists()


def test_placed_orders_need_the_gate6_budget(tmp_path):
    with pytest.raises(fre.FinalizeRefused, match="--budget-json"):
        run(tmp_path, status="submitted", orders=[ORDER], budget=None)
    with pytest.raises(fre.FinalizeRefused, match="budget_tier"):
        run(tmp_path, status="submitted", orders=[ORDER], budget={**BUDGET, "budget_tier": "huge"})


def test_run_state_carries_the_schemas_required_budget_and_counts(tmp_path):
    run(tmp_path, status="submitted", orders=[ORDER])
    state = json.loads((tmp_path / "intraday_dca/20260922_1430ET/run_state.json").read_text())
    assert state["budget"] == {**BUDGET, "budget_tier": "normal"} and state["counts"] == {"submitted": 1}
    run(tmp_path, "1130")
    early = json.loads((tmp_path / "intraday_dca/20260922_1130ET/run_state.json").read_text())
    assert early["budget"] == {"budget_evaluated": False} and early["counts"] == {"submitted": 0}
    schema = json.loads((Path(__file__).resolve().parents[2] / "schemas/run_state.schema.json").read_text())
    assert all(key in state and key in early for key in schema["required"])
    jsonschema = pytest.importorskip("jsonschema")
    referencing = pytest.importorskip("referencing")
    schemas = Path(__file__).resolve().parents[2] / "schemas"
    registry = referencing.Registry().with_resources(
        (path.name, referencing.Resource.from_contents(json.loads(path.read_bytes())))
        for path in schemas.glob("*.schema.json"))
    validator = jsonschema.Draft7Validator(schema, registry=registry)
    # Everything outside run_classification must validate. The nested block is
    # the executor's own (test_live_classification_is_the_executors_own), and
    # run_classification.schema.json only admits the run_slot/3:30 identities,
    # so the executors' classifications fail it whoever writes them -- that is
    # a pre-existing schema gap, not this command's.
    for candidate in (state, early):
        assert [e.message for e in validator.iter_errors(candidate) if not e.path or e.path[0] != "run_classification"] == []
    run(tmp_path, "1530")
    afternoon = json.loads((tmp_path / "ce16ba6b/scheduled_runs/20260922_1530ET/run_state.json").read_text())
    assert [e.message for e in validator.iter_errors(afternoon)] == []


def test_the_lock_sits_beside_the_run_directory_not_inside_it(tmp_path):
    run(tmp_path)
    assert (tmp_path / "intraday_dca/.20260922_1430ET.finalize.lock").exists()
    assert not any(path.name.endswith(".lock") for path in (tmp_path / "intraday_dca/20260922_1430ET").iterdir())


def test_orders_and_report_are_recorded(tmp_path):
    report = {"outcome": "ORDERS_PLACED", "gate7": {"orders_placed": 1}}
    result = run(tmp_path, status="submitted", orders=[ORDER], report=report, outcome="ORDERS_PLACED")
    assert result["verified"] is True
    run_dir = tmp_path / "intraday_dca/20260922_1430ET"
    state = json.loads((run_dir / "run_state.json").read_text())
    assert state["orders_submitted_count"] == 1 and state["gate9_report"] == report and state["status"] == "ORDERS_PLACED"
    assert [row["order_id"] for row in read_csv(run_dir / "submitted_orders.csv")] == ["ord-1"]


def test_retry_keeps_the_first_attempts_budget(tmp_path):
    run(tmp_path, status="submitted", orders=[ORDER])
    run(tmp_path, status="no_orders_submitted")  # relabelled submitted; budget carried forward
    state = json.loads((tmp_path / "intraday_dca/20260922_1430ET/run_state.json").read_text())
    assert state["budget"]["budget_tier"] == "normal"


def test_retry_merges_orders_by_order_id_and_stays_submitted(tmp_path):
    run(tmp_path, status="submitted", orders=[ORDER])
    second = {**ORDER, "ticker": "ANET", "order_id": "ord-2"}
    run(tmp_path, status="submitted", orders=[ORDER, second])
    run_dir = tmp_path / "intraday_dca/20260922_1430ET"
    assert [row["order_id"] for row in read_csv(run_dir / "submitted_orders.csv")] == ["ord-1", "ord-2"]
    # A later retry that placed nothing does not relabel the slot as order-free.
    result = run(tmp_path, status="no_orders_submitted")
    state = json.loads((run_dir / "run_state.json").read_text())
    assert state["order_submit_status"] == "submitted" and state["orders_submitted_count"] == 2
    assert any("already recorded" in warning for warning in result["warnings"])


def test_existing_executor_run_state_is_kept_and_only_the_pointer_refreshed(tmp_path):
    run(tmp_path, "1030")
    run_dir = tmp_path / "morning_dca/20260922_1030ET"
    state = json.loads((run_dir / "run_state.json").read_text())
    state.pop("finalized_by")
    state["executor_payload"] = "kept"
    (run_dir / "run_state.json").write_text(json.dumps(state))
    (tmp_path / "morning_dca/latest_morning_run.json").unlink()
    result = run(tmp_path, "1030", status="submitted", orders=[ORDER])
    assert result["run_state_written"] is False and result["verified"] is True
    assert json.loads((run_dir / "run_state.json").read_text())["executor_payload"] == "kept"
    assert any("NOT recorded" in warning for warning in result["warnings"])
    assert (tmp_path / "morning_dca/latest_morning_run.json").is_file()


def test_missing_companions_of_an_executor_state_are_repaired_never_overwritten(tmp_path):
    run(tmp_path, "1030")
    run_dir = tmp_path / "morning_dca/20260922_1030ET"
    state = json.loads((run_dir / "run_state.json").read_text())
    state.pop("finalized_by")
    (run_dir / "run_state.json").write_text(json.dumps(state))
    (run_dir / "run_summary.json").unlink()
    (run_dir / "submitted_orders.csv").unlink()
    (run_dir / "morning_signals.csv").write_text("ticker,grade\nMA,2x\n")
    # Interrupted executor: the audit must not call this verified.
    assert {r["slot_id"]: r["status"] for r in verify_day(DAY, NOW, root=tmp_path)["slots"]}["1030"] \
        == "evidence_incomplete"
    result = run(tmp_path, "1030")
    assert result["verified"] is True
    assert any("run_summary.json" in warning and "submitted_orders.csv" in warning for warning in result["warnings"])
    assert (run_dir / "morning_signals.csv").read_text() == "ticker,grade\nMA,2x\n"


def test_malformed_existing_run_state_is_refused(tmp_path):
    run_dir = tmp_path / "midday_dca/20260922_1230ET"
    run_dir.mkdir(parents=True)
    (run_dir / "run_state.json").write_text("[]")
    with pytest.raises(fre.FinalizeRefused, match="refusing to overwrite"):
        run(tmp_path, "1230")


def test_existing_signals_csv_is_preserved(tmp_path):
    run_dir = tmp_path / "market_open_dca/20260922_0935ET"
    run_dir.mkdir(parents=True)
    (run_dir / "market_open_signals.csv").write_text("ticker,grade\nMA,2x\n")
    assert run(tmp_path, "0935")["verified"] is True
    assert (run_dir / "market_open_signals.csv").read_text() == "ticker,grade\nMA,2x\n"


def test_cli_exit_codes(tmp_path, capsys):
    base = ["--root", str(tmp_path), "--outcome", "END_SILENTLY", "--order-submit-status", "no_orders_submitted"]
    assert fre.main(["--slot", "1230ET", "--started-at", "2026-09-22T12:31:00", *base]) == 0
    assert json.loads(capsys.readouterr().out)["verified"] is True
    assert fre.main(["--slot", "1230", "--started-at", "2026-09-22T09:00:00-04:00", *base]) == 2
    assert "acceptance window" in json.loads(capsys.readouterr().out)["refused"]
    assert fre.main(["--slot", "0900", "--started-at", "2026-09-22T09:00:00", *base]) == 2
    missing = tmp_path / "nope.json"
    assert fre.main(["--slot", "1230", "--started-at", "2026-09-22T12:31:00", "--orders-json", str(missing), *base]) == 2
