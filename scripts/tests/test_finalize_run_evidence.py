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
    return fre.finalize(
        slot_id,
        kwargs.pop("start", started(slot_id)),
        outcome=kwargs.pop("outcome", "NO_ORDERS"),
        order_submit_status=status,
        root=tmp_path,
        now=NOW,
        **kwargs,
    )


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
    state = json.loads((run_dir / "run_state.json").read_bytes())
    rc = state["run_classification"]
    assert rc["run_origin"] == "scheduled" and rc["manual_run"] is False and rc["valid_scheduled_evidence"] is True
    assert state["finalized_by"] == "scripts.finalize_run_evidence"
    # The CSV contract: header-only files exist even with zero rows.
    assert (run_dir / "submitted_orders.csv").read_text().startswith("ticker,order_id")
    assert json.loads((run_dir / "run_summary.json").read_bytes())["run_classification"] == rc
    slots = {row["slot_id"]: row["status"] for row in verify_day(DAY, NOW, root=tmp_path)["slots"]}
    assert slots[slot_id] == "verified"


def test_live_classification_is_the_executors_own(tmp_path):
    import intraday_dca

    run(tmp_path, "1330")
    state = json.loads((tmp_path / "intraday_dca/20260922_1330ET/run_state.json").read_bytes())
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
    assert run(tmp_path, "0935", start=datetime(2026, 9, 22, 9, 35, tzinfo=ET))["verified"] is True
    # 0935 never fires before 9:35: no early-start allowance, unlike the other slots.
    with pytest.raises(fre.FinalizeRefused, match="acceptance window"):
        run(tmp_path, "0935", start=datetime(2026, 9, 22, 9, 30, tzinfo=ET))
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


@pytest.mark.parametrize(
    "status, orders, match",
    [
        ("submitted", None, "needs at least one order row"),
        ("no_orders_submitted", [ORDER], "use 'submitted'"),
        ("dry_run", None, "is not one of"),
        ("submitted", [{**ORDER, "status": "rejected"}], "not a placed or in-flight order"),
        ("submitted", [{**ORDER, "order_id": ""}], "no broker order_id"),
        ("submitted", [{**ORDER, "ticker": None}], "no ticker"),
        ("submitted", [{**ORDER, "submitted_amount_usd": 0}], "not a positive number"),
        ("submitted", [{**ORDER, "submitted_amount_usd": "2.5"}], "not a positive number"),
        ("submitted", [ORDER, {**ORDER, "ticker": "ANET"}], "appears more than once"),
    ],
)
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
    state = json.loads((tmp_path / "intraday_dca/20260922_1430ET/run_state.json").read_bytes())
    assert state["budget"] == {**BUDGET, "budget_tier": "normal"} and state["counts"] == {"submitted": 1}
    run(tmp_path, "1130")
    early = json.loads((tmp_path / "intraday_dca/20260922_1130ET/run_state.json").read_bytes())
    assert early["budget"] == {"budget_evaluated": False} and early["counts"] == {"submitted": 0}
    schema = json.loads((Path(__file__).resolve().parents[2] / "schemas/run_state.schema.json").read_bytes())
    assert all(key in state and key in early for key in schema["required"])
    jsonschema = pytest.importorskip("jsonschema")
    referencing = pytest.importorskip("referencing")
    schemas = Path(__file__).resolve().parents[2] / "schemas"
    registry = referencing.Registry().with_resources(
        (path.name, referencing.Resource.from_contents(json.loads(path.read_bytes())))
        for path in schemas.glob("*.schema.json")
    )
    validator = jsonschema.Draft7Validator(schema, registry=registry)
    # Everything outside run_classification must validate. The nested block is
    # the executor's own (test_live_classification_is_the_executors_own), and
    # run_classification.schema.json only admits the run_slot/3:30 identities,
    # so the executors' classifications fail it whoever writes them -- that is
    # a pre-existing schema gap, not this command's.
    for candidate in (state, early):
        assert [
            e.message for e in validator.iter_errors(candidate) if not e.path or e.path[0] != "run_classification"
        ] == []
    run(tmp_path, "1530")
    afternoon = json.loads((tmp_path / "ce16ba6b/scheduled_runs/20260922_1530ET/run_state.json").read_bytes())
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
    state = json.loads((run_dir / "run_state.json").read_bytes())
    assert (
        state["orders_submitted_count"] == 1 and state["gate9_report"] == report and state["status"] == "ORDERS_PLACED"
    )
    assert [row["order_id"] for row in read_csv(run_dir / "submitted_orders.csv")] == ["ord-1"]


def test_retry_keeps_the_first_attempts_budget(tmp_path):
    run(tmp_path, status="submitted", orders=[ORDER])
    run(tmp_path, status="no_orders_submitted")  # relabelled submitted; budget carried forward
    state = json.loads((tmp_path / "intraday_dca/20260922_1430ET/run_state.json").read_bytes())
    assert state["budget"]["budget_tier"] == "normal"


def test_retry_merges_orders_by_order_id_and_stays_submitted(tmp_path):
    run(tmp_path, status="submitted", orders=[ORDER])
    second = {**ORDER, "ticker": "ANET", "order_id": "ord-2"}
    run(tmp_path, status="submitted", orders=[ORDER, second])
    run_dir = tmp_path / "intraday_dca/20260922_1430ET"
    assert [row["order_id"] for row in read_csv(run_dir / "submitted_orders.csv")] == ["ord-1", "ord-2"]
    # A later retry that placed nothing does not relabel the slot as order-free.
    result = run(tmp_path, status="no_orders_submitted")
    state = json.loads((run_dir / "run_state.json").read_bytes())
    assert state["order_submit_status"] == "submitted" and state["orders_submitted_count"] == 2
    assert any("already recorded" in warning for warning in result["warnings"])


def test_existing_executor_run_state_is_kept_and_only_the_pointer_refreshed(tmp_path):
    run(tmp_path, "1030")
    run_dir = tmp_path / "morning_dca/20260922_1030ET"
    state = json.loads((run_dir / "run_state.json").read_bytes())
    state.pop("finalized_by")
    state["executor_payload"] = "kept"
    (run_dir / "run_state.json").write_text(json.dumps(state))
    (tmp_path / "morning_dca/latest_morning_run.json").unlink()
    result = run(tmp_path, "1030", status="submitted", orders=[ORDER])
    assert result["run_state_written"] is False and result["verified"] is True
    assert json.loads((run_dir / "run_state.json").read_bytes())["executor_payload"] == "kept"
    assert any("NOT recorded" in warning for warning in result["warnings"])
    assert (tmp_path / "morning_dca/latest_morning_run.json").is_file()


def test_missing_companions_of_an_executor_state_are_repaired_never_overwritten(tmp_path):
    run(tmp_path, "1030")
    run_dir = tmp_path / "morning_dca/20260922_1030ET"
    state = json.loads((run_dir / "run_state.json").read_bytes())
    state.pop("finalized_by")
    (run_dir / "run_state.json").write_text(json.dumps(state))
    (run_dir / "run_summary.json").unlink()
    (run_dir / "submitted_orders.csv").unlink()
    (run_dir / "morning_signals.csv").write_text("ticker,grade\nMA,2x\n")
    # Interrupted executor: the audit must not call this verified.
    assert {r["slot_id"]: r["status"] for r in verify_day(DAY, NOW, root=tmp_path)["slots"]}[
        "1030"
    ] == "evidence_incomplete"
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
    assert (
        fre.main(["--slot", "1230", "--started-at", "2026-09-22T12:31:00", "--orders-json", str(missing), *base]) == 2
    )


# --- zero-order finalizes must show Gate 5 actually evaluated data -----------
# 2026-09-18: the 12:30 and 3:30 sessions lost market data, hand-wrote an empty
# candidates file and ended silently as a "quiet tape" (Notion DCA Issues:
# fabricated_empty_candidates, logged_as_quiet_tape).


def rows_for(evaluated, provider_error, no_data):
    """Candidate rows the way the generators emit them: a provider-error ticker is
    itself a NO_DATA row, then plain NO_DATA rows, then evaluated-and-quiet rows."""
    rows = [
        {"ticker": f"T{i}", "action": "NO_DATA", "provider_error": i < provider_error}
        for i in range(max(provider_error, no_data))
    ]
    rows += [{"ticker": f"T{i}", "action": "NO_ACTION", "provider_error": False} for i in range(len(rows), evaluated)]
    return rows


def gate5_doc(slot_id="1430", **overrides):
    counts = {"evaluated_count": 64, "provider_error_count": 1, "no_data_count": 1}
    counts.update({k: overrides.pop(k) for k in list(overrides) if k in counts})
    doc = {
        "slot": f"{slot_id}ET",
        "runner_slot_id": slot_id,
        "trading_day": DAY.isoformat(),
        "buy_count": 0,
        "already_covered_tickers": [],
        **counts,
        "candidates": rows_for(counts["evaluated_count"], counts["provider_error_count"], counts["no_data_count"]),
    }
    doc.update(overrides)
    return doc


def put(tmp_path, slot_id, name, payload):
    run_dir = tmp_path / slot_run_dir_relative(slot_id, DAY)
    run_dir.mkdir(parents=True, exist_ok=True)
    (run_dir / name).write_text(json.dumps(payload))
    return run_dir


@pytest.mark.parametrize("payload", [[], {"candidates": []}, {"candidates": [], "buy_count": 0}])
def test_hand_written_empty_candidates_file_is_refused(tmp_path, payload):
    run_dir = put(tmp_path, "1230", "candidates_2026-09-22_1230.json", payload)
    with pytest.raises(fre.FinalizeRefused, match="none is a Gate 5 generator document.*market_data_unavailable"):
        run(tmp_path, "1230", outcome="END_SILENTLY")
    assert not (run_dir / "run_state.json").exists()


def test_no_candidates_status_needs_the_generator_document(tmp_path):
    with pytest.raises(fre.FinalizeRefused, match="needs this slot's Gate 5 generator document"):
        run(tmp_path, "1430", status="not_submitted_no_candidates")


@pytest.mark.parametrize(
    "counts",
    [
        {"evaluated_count": 0},
        {"evaluated_count": 64, "provider_error_count": 64},
        {"evaluated_count": 64, "provider_error_count": 4, "no_data_count": 64},
    ],
)
def test_zero_candidates_from_zero_usable_tickers_is_an_outage(tmp_path, counts):
    put(tmp_path, "1430", "gate5_document.json", gate5_doc(**counts))
    with pytest.raises(fre.FinalizeRefused, match="data outage, not a quiet tape"):
        run(tmp_path, "1430", status="not_submitted_no_candidates")


def test_genuinely_quiet_tape_verifies_and_records_its_evidence(tmp_path):
    # One provider error is routine in real documents; it must not block.
    run_dir = put(tmp_path, "1430", "gate5_document.json", gate5_doc(no_data_count=40))
    result = run(tmp_path, "1430", status="not_submitted_no_candidates")
    assert result["verified"] is True, result["errors"]
    evidence = json.loads((run_dir / "run_state.json").read_bytes())["gate5_evidence"]
    # The provider-error ticker is one of the 40 NO_DATA rows, not subtracted twice.
    assert evidence == {
        "document": "gate5_document.json",
        "evaluated_count": 64,
        "usable_count": 24,
        "buy_count": 0,
        "provider_error_count": 1,
        "no_data_count": 40,
    }


def test_fully_covered_universe_is_served_not_an_outage(tmp_path):
    put(tmp_path, "1430", "gate5_document.json", gate5_doc(evaluated_count=0, already_covered_tickers=["MA"]))
    assert run(tmp_path, "1430", status="not_submitted_no_candidates")["verified"] is True


def test_gate6_5_zeroing_real_buys_passes(tmp_path):
    put(tmp_path, "1430", "gate5_document.json", gate5_doc(buy_count=3, provider_error_count=64))
    assert run(tmp_path, "1430", outcome="NO_ORDERS_GATE6_5_ZEROED")["verified"] is True


def test_cands_extract_beside_a_real_document_is_not_a_stray(tmp_path):
    put(tmp_path, "1030", "cands.json", [])
    put(tmp_path, "1030", "cands_output.json", gate5_doc("1030"))
    assert run(tmp_path, "1030", outcome="END_SILENTLY")["verified"] is True


def test_a_document_for_another_slot_or_day_is_not_this_runs_evidence(tmp_path):
    put(tmp_path, "1430", "candidates.json", gate5_doc("1330"))
    put(tmp_path, "1430", "candidates_old.json", gate5_doc(trading_day="2026-09-21"))
    with pytest.raises(fre.FinalizeRefused, match="look like candidate files"):
        run(tmp_path, "1430", outcome="END_SILENTLY")


def test_declared_failure_is_not_second_guessed(tmp_path):
    put(tmp_path, "1230", "candidates.json", [])
    result = run(
        tmp_path,
        "1230",
        status="not_submitted_error",
        error_class="market_data_unavailable",
        error_detail="provider returned nothing",
    )
    assert result["run_state_written"] is True


def test_cli_maps_an_outage_dressed_as_quiet_tape_to_exit_2(tmp_path, capsys):
    put(tmp_path, "1530", "candidates.json", {"candidates": []})
    argv = [
        "--slot",
        "1530",
        "--started-at",
        "2026-09-22T15:31:00-04:00",
        "--root",
        str(tmp_path),
        "--outcome",
        "END_SILENTLY",
        "--order-submit-status",
        "no_orders_submitted",
    ]
    assert fre.main(argv) == 2
    assert "market_data_unavailable" in json.loads(capsys.readouterr().out)["refused"]


# --- Copilot review, PR #1519 ------------------------------------------------


def test_all_no_data_1530_document_without_no_data_count_is_an_outage(tmp_path):
    """generate_buy_candidates' 15:30 document publishes no no_data_count; its rows say it all."""
    doc = gate5_doc("1530", provider_error_count=0, no_data_count=64)
    del doc["no_data_count"]
    put(tmp_path, "1530", "candidates_2026-09-22_1530.json", doc)
    with pytest.raises(fre.FinalizeRefused, match="no data 64.*data outage"):
        run(tmp_path, "1530", status="not_submitted_no_candidates")


def test_document_without_rows_or_no_data_count_is_not_evidence(tmp_path):
    doc = gate5_doc()
    del doc["no_data_count"], doc["candidates"]
    put(tmp_path, "1430", "gate5_document.json", doc)
    with pytest.raises(fre.FinalizeRefused, match="needs this slot's Gate 5 generator document"):
        run(tmp_path, "1430", status="not_submitted_no_candidates")


def test_counts_only_document_falls_back_to_its_counts(tmp_path):
    doc = gate5_doc(no_data_count=10)
    del doc["candidates"]
    put(tmp_path, "1430", "gate5_document.json", doc)
    assert run(tmp_path, "1430", status="not_submitted_no_candidates")["verified"] is True


def test_duplicate_json_keys_make_a_file_invalid_evidence(tmp_path):
    run_dir = tmp_path / slot_run_dir_relative("1430", DAY)
    run_dir.mkdir(parents=True)
    body = json.dumps(gate5_doc())[:-1] + ', "evaluated_count": 0}'
    (run_dir / "gate5_document.json").write_text(body)
    with pytest.raises(fre.FinalizeRefused, match="gate5_document.json look like candidate files.*duplicate JSON keys"):
        run(tmp_path, "1430", outcome="END_SILENTLY")


def executor_state(run_dir, **fields):
    run_dir.mkdir(parents=True, exist_ok=True)
    state = {
        "trading_day": DAY.isoformat(),
        "stream": "intraday",
        "order_submit_status": "no_orders_submitted",
        **fields,
    }
    (run_dir / "run_state.json").write_text(json.dumps(state))
    return state


def test_executor_state_recording_an_outage_is_refused_and_left_untouched(tmp_path):
    run_dir = tmp_path / slot_run_dir_relative("1430", DAY)
    executor_state(run_dir, counts={"evaluated": 64, "no_data": 64, "buy_candidates": 0})
    before = sorted(p.name for p in run_dir.iterdir())
    with pytest.raises(fre.FinalizeRefused, match="not written by this command.*data outage"):
        run(tmp_path, "1430", outcome="END_SILENTLY")
    assert sorted(p.name for p in run_dir.iterdir()) == before


def test_hand_written_state_beside_a_fabricated_candidates_file_is_refused(tmp_path):
    run_dir = tmp_path / slot_run_dir_relative("1230", DAY)
    executor_state(run_dir, stream="midday")
    (run_dir / "candidates.json").write_text("[]")
    with pytest.raises(fre.FinalizeRefused, match="candidates.json look like candidate files"):
        run(tmp_path, "1230", outcome="END_SILENTLY")


def test_executor_state_with_usable_data_is_not_second_guessed(tmp_path):
    run_dir = tmp_path / slot_run_dir_relative("1430", DAY)
    executor_state(run_dir, counts={"evaluated": 64, "no_data": 1, "buy_candidates": 0})
    fre.check_existing_state_gate5_evidence(run_dir, "1430", DAY, json.loads((run_dir / "run_state.json").read_bytes()))


# --- code_version: which release produced this evidence (cloud-clone plan, step 5) ---------------------------
# A cloud run trades from a pinned checkout. The run's evidence has to say which commit that was, or
# nobody can tell afterwards what code placed an order. The field is optional so every existing caller
# (the seven prompts before they are repasted, the executors' own artifacts) is unchanged.

CODE_VERSION = {"git_sha": "a" * 40, "tag": "live-sep30-1", "pinned_by": "DCA_PINNED_SHA"}


def test_code_version_is_recorded_in_run_state_and_run_summary(tmp_path):
    result = run(tmp_path, "1330", code_version=CODE_VERSION)

    assert result["verified"] is True, result["errors"]
    run_dir = tmp_path / slot_run_dir_relative("1330", DAY)
    assert json.loads((run_dir / "run_state.json").read_bytes())["code_version"] == CODE_VERSION
    assert json.loads((run_dir / "run_summary.json").read_bytes())["code_version"] == CODE_VERSION


def test_code_version_is_absent_when_the_caller_gives_none(tmp_path):
    run(tmp_path, "1330")

    run_dir = tmp_path / slot_run_dir_relative("1330", DAY)
    assert "code_version" not in json.loads((run_dir / "run_state.json").read_bytes())
    assert "code_version" not in json.loads((run_dir / "run_summary.json").read_bytes())


@pytest.mark.parametrize(
    "bad",
    [
        {"tag": "live-1"},
        {"git_sha": "abc123"},
        {"git_sha": "G" * 40},
        {"git_sha": "a" * 40, "unexpected": 1},
        "a" * 40,
    ],
)
def test_a_malformed_code_version_is_refused_before_anything_is_written(tmp_path, bad):
    with pytest.raises(ValueError):
        run(tmp_path, "1330", code_version=bad)

    assert not (tmp_path / slot_run_dir_relative("1330", DAY)).exists()


def test_the_cli_accepts_code_version_json(tmp_path):
    exit_code = fre.main([
        "--slot", "1330", "--started-at", "2026-09-22T13:32:00-04:00",
        "--outcome", "NO_ORDERS", "--order-submit-status", "no_orders_submitted",
        "--code-version-json", json.dumps(CODE_VERSION), "--root", str(tmp_path),
    ])

    assert exit_code == 0
    run_dir = tmp_path / slot_run_dir_relative("1330", DAY)
    assert json.loads((run_dir / "run_state.json").read_bytes())["code_version"] == CODE_VERSION


# --- code_version review follow-ups (Copilot review, PR #1571) ------------------------------------------------


def as_executor_state(tmp_path, slot="1030", **fields):
    """Turn a state this command wrote into one an executor's own run() wrote."""
    run(tmp_path, slot)
    run_dir = tmp_path / slot_run_dir_relative(slot, DAY)
    state = json.loads((run_dir / "run_state.json").read_bytes())
    state.pop("finalized_by")
    state.update(fields)
    (run_dir / "run_state.json").write_text(json.dumps(state))
    return run_dir


def test_code_version_is_merged_into_an_executor_written_run_state_and_summary(tmp_path):
    run_dir = as_executor_state(tmp_path)

    result = run(tmp_path, "1030", code_version=CODE_VERSION)

    assert result["run_state_written"] is False and result["verified"] is True, result["errors"]
    assert json.loads((run_dir / "run_state.json").read_bytes())["code_version"] == CODE_VERSION
    assert json.loads((run_dir / "run_summary.json").read_bytes())["code_version"] == CODE_VERSION
    assert not any("code_version" in warning and "NOT recorded" in warning for warning in result["warnings"])


def test_an_executor_state_keeps_its_other_fields_when_code_version_is_merged(tmp_path):
    run_dir = as_executor_state(tmp_path, executor_payload="kept")

    run(tmp_path, "1030", code_version=CODE_VERSION)

    assert json.loads((run_dir / "run_state.json").read_bytes())["executor_payload"] == "kept"


def test_a_conflicting_code_version_is_refused_and_the_state_is_left_as_it_was(tmp_path):
    run_dir = as_executor_state(tmp_path, code_version={"git_sha": "b" * 40})
    before = (run_dir / "run_state.json").read_text()

    with pytest.raises(fre.FinalizeRefused, match="code_version"):
        run(tmp_path, "1030", code_version=CODE_VERSION)

    assert (run_dir / "run_state.json").read_text() == before


def test_a_retry_that_omits_code_version_keeps_the_first_attempts_release(tmp_path):
    run(tmp_path, "1330", code_version=CODE_VERSION)

    run(tmp_path, "1330")

    run_dir = tmp_path / slot_run_dir_relative("1330", DAY)
    assert json.loads((run_dir / "run_state.json").read_bytes())["code_version"] == CODE_VERSION


def cli_args(tmp_path, code_version_json):
    return ["--slot", "1330", "--started-at", "2026-09-22T13:32:00-04:00",
            "--outcome", "NO_ORDERS", "--order-submit-status", "no_orders_submitted",
            "--code-version-json", code_version_json, "--root", str(tmp_path)]


DUPLICATE_SHA_JSON = '{"git_sha": "%s", "git_sha": "%s"}' % ("a" * 40, "b" * 40)


def test_the_cli_refuses_duplicate_git_sha_keys_given_inline(tmp_path, capsys):
    assert fre.main(cli_args(tmp_path, DUPLICATE_SHA_JSON)) == 2

    assert "duplicate" in json.loads(capsys.readouterr().out)["refused"]
    assert not (tmp_path / slot_run_dir_relative("1330", DAY)).exists()


def test_the_cli_refuses_duplicate_git_sha_keys_given_as_a_file(tmp_path, capsys):
    path = tmp_path / "code_version.json"
    path.write_text(DUPLICATE_SHA_JSON, encoding="utf-8")

    assert fre.main(cli_args(tmp_path / "root", str(path))) == 2

    assert "duplicate" in json.loads(capsys.readouterr().out)["refused"]


def test_the_cli_refuses_a_git_sha_that_is_not_the_pinned_release(tmp_path, capsys, monkeypatch):
    monkeypatch.setenv("DCA_PINNED_SHA", "b" * 40)

    assert fre.main(cli_args(tmp_path, json.dumps(CODE_VERSION))) == 2

    assert "DCA_PINNED_SHA" in json.loads(capsys.readouterr().out)["refused"]
    assert not (tmp_path / slot_run_dir_relative("1330", DAY)).exists()


def test_the_cli_accepts_a_git_sha_equal_to_the_pinned_release(tmp_path, monkeypatch):
    monkeypatch.setenv("DCA_PINNED_SHA", CODE_VERSION["git_sha"])

    assert fre.main(cli_args(tmp_path, json.dumps(CODE_VERSION))) == 0


# --- Group C: a platform refusal is its own recorded outcome (session_refusal,
# real_world_transactions_denied; claude/triage-routine/GROUP-C-DECISION-2026-10-17.md).


def test_platform_refusal_reason_code_is_a_registered_no_buy_reason():
    from scripts import reason_codes

    assert reason_codes.PLATFORM_REFUSAL == "PLATFORM_REFUSAL"
    assert reason_codes.PLATFORM_REFUSAL in reason_codes.ALL_REASONS
    assert reason_codes.PLATFORM_REFUSAL in reason_codes.NO_BUY_REASONS
    # Not a safety/gate failure: nothing in this repo blocked it.
    assert reason_codes.PLATFORM_REFUSAL not in reason_codes.FAIL_CLOSED_REASONS


def test_platform_refusal_finalizes_as_not_submitted_error_with_distinct_reason_and_alert(tmp_path):
    result = run(
        tmp_path,
        "1530",
        outcome="PLATFORM_REFUSAL",
        status="not_submitted_error",
        error_class=fre.PLATFORM_REFUSAL_ERROR_CLASS,
        error_detail="place_equity_order refused before the broker saw it",
    )
    assert result["run_state_written"] is True
    state = json.loads(next(tmp_path.rglob("run_state.json")).read_text(encoding="utf-8"))
    assert state["order_submit_status"] == "not_submitted_error"
    assert state["orders_submitted_count"] == 0
    assert state["reason_code"] == "PLATFORM_REFUSAL"
    assert state["alert_required"] is True
    # A generic error must NOT carry the refusal marker.
    other = run(
        tmp_path, "1430", status="not_submitted_error", error_class="market_data_unavailable", error_detail="x"
    )
    assert other["run_state_written"] is True
    other_state = json.loads((tmp_path / "intraday_dca/20260922_1430ET/run_state.json").read_text(encoding="utf-8"))
    assert "reason_code" not in other_state


def test_platform_refusal_cannot_claim_a_quiet_status(tmp_path):
    with pytest.raises(fre.FinalizeRefused, match="platform refusal"):
        run(
            tmp_path,
            "1530",
            outcome="END_SILENTLY",
            status="no_orders_submitted",
            error_class=fre.PLATFORM_REFUSAL_ERROR_CLASS,
            error_detail="refused",
        )
    assert not any(tmp_path.iterdir())


def test_platform_refusal_after_earlier_fills_stays_submitted_and_keeps_the_marker(tmp_path):
    result = run(
        tmp_path,
        "1530",
        outcome="PLATFORM_REFUSAL",
        status="submitted",
        orders=[ORDER],
        error_class=fre.PLATFORM_REFUSAL_ERROR_CLASS,
        error_detail="second order refused",
    )
    state = json.loads(next(tmp_path.rglob("run_state.json")).read_text(encoding="utf-8"))
    assert result["run_state_written"] is True
    assert state["order_submit_status"] == "submitted"
    assert state["reason_code"] == "PLATFORM_REFUSAL"
    assert state["alert_required"] is True
