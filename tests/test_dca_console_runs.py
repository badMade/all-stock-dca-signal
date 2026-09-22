"""Runs-page reader regressions: bounded, label-only, lock-free, and honest about gaps."""

import json
import os
from datetime import date, timedelta
from pathlib import Path

import pytest

import dca_runner
import scripts.file_lock
from dca_console_reader import MAX_BYTES, RootSpec
from dca_console_routes import walk_redact
from dca_console_runs import (
    artifact_tree_map,
    build_run_detail,
    build_run_index,
    pointer_strip,
    run_identity,
)
from scripts.run_classification import build_scheduled_classification

DAY = date(2026, 9, 8)
CONFIG = Path(__file__).resolve().parents[1] / "config"
ACCOUNT = "999000111"


def write_state(root, relative="morning_dca/20260908_1030ET", name="run_state.json", **updates):
    folder = root / relative
    folder.mkdir(parents=True, exist_ok=True)
    state = {
        "trading_day": DAY.isoformat(), "generated_at": DAY.isoformat() + "T10:30:00-04:00",
        "status": "completed", "dry_run": False, "counts": {"buy_candidates": 1, "submitted": 1},
        "run_classification": {"stream": "morning", "run_origin": "scheduled", "manual_run": False,
                               "cron_id": "42173154", "run_id": "scheduled_20260908_1030ET"},
    }
    state.update(updates)
    (folder / name).write_text(json.dumps(state), encoding="utf-8")
    return folder


def afternoon_run(root, relative="ce16ba6b/scheduled_runs/20260908_1530ET", **updates):
    """A 3:30 PM run with every artifact the detail builder knows how to read.

    The classification block is the canonical one the live writer emits, so
    ``dca_risk.classify_stream`` (which insists on ``validate_classification``
    passing) counts the run as scheduled afternoon evidence; a hand-rolled
    three-key block is STREAM_OTHER and the evidence-gap diagnostic would
    report it as excluded by stream.
    """
    state = {
        "account_number": ACCOUNT, "order_submit_status": "submitted", "run_id": "scheduled_20260908_1530ET",
        "generated_at": DAY.isoformat() + "T15:30:00-04:00",
        "run_classification": build_scheduled_classification(DAY.isoformat(), "2026-09-08T19:30:00Z"),
        "buy_candidates": [{"ticker": "ABC", "planned_amount_usd": 20.0, "target_price": 10.0,
                            "buy_multiplier": 2.0, "order_id": "ORDER-SECRET-1"}],
        "top_up_note": f"seen under {root} for {ACCOUNT}",
        "risk_guard": {"kill_switch_engaged": False, "deployed_last_7d_usd": 40.0},
    }
    state.update(updates)
    folder = write_state(root, relative, **state)
    (folder / "signals.csv").write_text(
        "ticker,source,action,reason,decision_bar_close,previous_close,lookback_price,buy_multiplier\n"
        f"ABC,robinhood,buy,drop acct {ACCOUNT} at {root},9.0,10.0,9.5,2.0\n"
        "XYZ,robinhood,hold,no_drop,10.0,10.0,10.0,0\n", encoding="utf-8")
    (folder / "buy_candidates.csv").write_text(
        "ticker,planned_amount_usd,target_price,order_id,status\nABC,20.0,10.0,ORDER-SECRET-1,submitted\n",
        encoding="utf-8")
    (folder / "submitted_orders.csv").write_text(
        "ticker,order_id,submitted_amount_usd,status\nABC,ORDER-SECRET-1,20.0,submitted\n", encoding="utf-8")
    (folder / "market_data_1530ET.json").write_text("{}", encoding="utf-8")
    raw = folder / "raw"
    raw.mkdir()
    (raw / "orders_after_place.json").write_text(json.dumps({"orders": [
        {"id": "ORDER-SECRET-1", "symbol": "ABC", "state": "filled", "average_price": "10.5",
         "executions": [{"quantity": "2", "price": "10.5"}]},
    ]}), encoding="utf-8")
    (raw / "quotes_batch1.json").write_text(json.dumps({"results": []}), encoding="utf-8")
    return folder


def only_run(root, **filters):
    runs = build_run_index([root], **filters)["runs"]
    assert len(runs) == 1, runs
    return runs[0]


def test_tree_map_absent_root_is_absent_not_empty(tmp_path):
    result = artifact_tree_map([RootSpec.of(tmp_path / "missing")])
    root = result["roots"][0]
    assert root["exists"] is False and root["at_risk"] is False
    assert {entry["present"] for entry in root["directories"]} == {False}
    assert {entry["newest"] for entry in root["directories"]} == {None}
    assert {entry["name"] for entry in root["directories"]} >= {
        "morning_dca", "ce16ba6b", "ce16ba6b/scheduled_runs", "morning_dca/manual_runs", "inverse_martingale_dca",
        "wondrous_octopus/runs", "attempt_log"}


def test_tree_map_counts_entries_and_flags_secondary_root(tmp_path):
    (tmp_path / "intraday_dca" / "20260908_1130ET").mkdir(parents=True)
    (tmp_path / "intraday_dca" / "20260908_1330ET").mkdir()
    result = artifact_tree_map([RootSpec.of(tmp_path, "secondary")])
    root = result["roots"][0]
    assert root["exists"] is True and root["at_risk"] is True
    intraday = next(entry for entry in root["directories"] if entry["name"] == "intraday_dca")
    assert intraday == {"name": "intraday_dca", "present": True, "entries": 2, "newest": intraday["newest"]}
    assert intraday["newest"].startswith("20")
    assert str(tmp_path) not in json.dumps(result)


def test_index_empty_or_absent_root_reports_nothing(tmp_path):
    (tmp_path / "empty").mkdir()
    absent = build_run_index([RootSpec.of(tmp_path / "missing")])
    assert absent["runs"] == [] and absent["recent_days"] == []
    assert any("does not exist" in warning for warning in absent["warnings"])
    assert build_run_index([RootSpec.of(tmp_path / "empty")]) == {"runs": [], "recent_days": [], "warnings": []}


def test_index_row_shape_slot_and_stable_identity(tmp_path):
    write_state(tmp_path)
    root = RootSpec.of(tmp_path)
    row = only_run(root)
    assert row["id"] == run_identity(root.label, "morning_dca/20260908_1030ET")
    assert row["id"] == only_run(root)["id"]
    assert row["stream"] == "morning" and row["slot"] == "1030ET" and row["day"] == "2026-09-08"
    assert row["status"] == "completed" and row["mode"] == "live" and row["origin"] == "scheduled"
    assert row["candidates"] == 1 and row["submitted"] == 1
    assert row["incomplete"] is False and row["at_risk"] is False
    assert row["root_label"] == root.label and row["relative_dir"] == "morning_dca/20260908_1030ET"
    assert "confirmed_spend_usd" not in row
    assert build_run_index([root])["recent_days"] == ["2026-09-08"]


def test_index_manual_runs_are_flagged_and_missing_counts_stay_null(tmp_path):
    write_state(tmp_path, "morning_dca/manual_runs/20260908_1101ET", counts={},
                run_classification={"stream": "morning", "run_origin": "scheduled", "manual_run": True})
    row = only_run(RootSpec.of(tmp_path))
    assert row["origin"] == "manual"
    assert row["candidates"] is None and row["submitted"] is None


def test_index_reports_incomplete_directories_not_failures(tmp_path):
    slot = tmp_path / "intraday_dca" / "20260909_1130ET"
    slot.mkdir(parents=True)
    (slot / "market_data_1130ET.json").write_text("{}", encoding="utf-8")
    (tmp_path / "2026-09-10").mkdir()
    (tmp_path / "intraday_dca" / "notes").mkdir()
    runs = build_run_index([RootSpec.of(tmp_path, "secondary")])["runs"]
    assert [row["relative_dir"] for row in runs] == ["2026-09-10", "intraday_dca/20260909_1130ET"]
    intraday, dated = runs[1], runs[0]
    assert intraday["incomplete"] is True and intraday["status"] == "incomplete"
    assert intraday["stream"] == "intraday" and intraday["slot"] == "1130ET"
    assert intraday["time"].startswith("2026-09-09T11:30:00") and intraday["day"] == "2026-09-09"
    assert intraday["files"] == ["market_data_1130ET.json"]
    assert intraday["candidates"] is None and intraday["at_risk"] is True
    assert dated["stream"] == "unknown" and dated["time"] is None and dated["files"] == []
    assert all(row["status"] != "failed" for row in runs)


def test_index_nested_and_malformed_states_are_not_incomplete(tmp_path):
    write_state(tmp_path, "scheduled_runs/20260908_1530ET/artifacts",
                run_classification={"stream": "afternoon", "run_origin": "scheduled", "manual_run": False})
    broken = tmp_path / "midday_dca" / "20260908_1230ET"
    broken.mkdir(parents=True)
    (broken / "run_state.json").write_text("{not json", encoding="utf-8")
    result = build_run_index([RootSpec.of(tmp_path)])
    assert [row["relative_dir"] for row in result["runs"]] == ["scheduled_runs/20260908_1530ET/artifacts"]
    assert result["runs"][0]["stream"] == "afternoon" and result["runs"][0]["incomplete"] is False
    assert any("malformed" in warning for warning in result["warnings"])


def test_index_collapses_variants_filters_and_excludes(tmp_path):
    write_state(tmp_path, generated_at="2026-09-08T10:30:00-04:00")
    write_state(tmp_path, name="run_state_final.json", generated_at="2026-09-08T10:31:00-04:00", status="failed")
    write_state(tmp_path, "midday_dca/20260810_1230ET", trading_day="2026-08-10", generated_at=None)
    write_state(tmp_path, "console/demo/20260901_1030ET", trading_day="2026-09-01")
    root = RootSpec.of(tmp_path)
    result = build_run_index([root], excluded_roots=[tmp_path / "console"])
    assert [row["day"] for row in result["runs"]] == ["2026-09-08", "2026-08-10"]
    assert result["runs"][0]["status"] == "failed" and result["runs"][0]["time"].startswith("2026-09-08T10:31")
    assert result["recent_days"] == ["2026-09-08", "2026-08-10"]
    assert [row["day"] for row in build_run_index([root], day=date(2026, 8, 10))["runs"]] == ["2026-08-10"]
    # The demo archive is only hidden when the caller excludes it; the day
    # filter is the state's trading_day, so the demo run is a September row.
    assert [row["day"] for row in build_run_index([root], month="2026-09")["runs"]] == ["2026-09-08", "2026-09-01"]
    excluded = build_run_index([root], month="2026-09", excluded_roots=[tmp_path / "console"])
    assert [row["day"] for row in excluded["runs"]] == ["2026-09-08"]
    assert build_run_index([root], day=date(2026, 1, 1))["runs"] == []


def test_index_caps_rows_with_warning(tmp_path):
    for offset in range(501):
        (tmp_path / (date(2020, 1, 1) + timedelta(days=offset)).isoformat()).mkdir()
    result = build_run_index([RootSpec.of(tmp_path)])
    assert len(result["runs"]) == 500 and result["runs"][0]["day"] == "2021-05-15"
    assert any("newest 500" in warning for warning in result["warnings"])


def test_detail_rejects_malformed_and_unknown_ids(tmp_path):
    write_state(tmp_path)
    root = RootSpec.of(tmp_path)
    assert build_run_detail([root], "../etc", config_dir=CONFIG)["found"] is False
    unknown = build_run_detail([root], "run-0123456789ab", config_dir=CONFIG)
    assert unknown["found"] is False and any("No run" in warning for warning in unknown["warnings"])


def test_detail_sections_from_a_full_afternoon_run(tmp_path):
    afternoon_run(tmp_path)
    root = RootSpec.of(tmp_path)
    detail = build_run_detail([root], only_run(root)["id"], config_dir=CONFIG)
    assert detail["found"] is True and detail["run"]["stream"] == "afternoon"
    identity = detail["identity"]
    assert identity["order_submit_status"] == "submitted" and identity["risk_guard"]["deployed_last_7d_usd"] == 40.0
    classification = identity["run_classification"]
    assert classification["stream"] == "afternoon" and classification["run_origin"] == "scheduled"
    assert classification["manual_run"] is False and classification["cron_id"] == "10ce362c"
    # Only the allowlisted sub-keys survive: the run_id, trigger time and
    # window fields the live writer stamps are dropped with everything else.
    assert set(classification) <= {"stream", "run_origin", "manual_run", "run_type", "cron_id", "schema",
                                   "state_schema_version"}
    assert "account_number" not in identity and "buy_candidates" not in identity and "run_id" not in identity
    assert "top_up_note" in identity
    assert detail["contract"]["ok"] is True and detail["contract"]["errors"] == []
    assert detail["drift"]["drift_detected"] is False and "run_dir" not in detail["drift"]
    tables = detail["tables"]
    assert set(tables) == {"signals.csv", "buy_candidates.csv", "submitted_orders.csv"}
    assert tables["signals.csv"]["columns"][:2] == ["ticker", "source"]
    assert tables["submitted_orders.csv"]["columns"] == ["ticker", "submitted_amount_usd", "status"]
    assert tables["buy_candidates.csv"]["rows"] == [{"ticker": "ABC", "planned_amount_usd": "20.0",
                                                    "target_price": "10.0", "status": "submitted"}]
    post = detail["post_trade"]
    assert post["available"] is True and post["summary"]["coverage"]["fills"] == 1
    assert post["slippage"][0]["ticker"] == "ABC" and post["slippage"][0]["slippage_pct"] == 5.0
    assert "order_id" not in post["slippage"][0] and post["missing_fills"] == []
    window = detail["close_window"]
    assert window["available"] is True and window["multiplier_label"] == "reporting cap 3x"
    assert window["would_have_fired_tickers"] == ["ABC"] and window["raw_quote_files"] == 1
    assert detail["inputs"] == [{"name": "market_data_1530ET.json", "bytes": 2}]
    assert detail["abort_explanation"] is None
    assert "ORDER-SECRET-1" not in json.dumps(detail)


def test_detail_incomplete_run_has_empty_identity_and_unavailable_sections(tmp_path):
    folder = tmp_path / "intraday_dca" / "20260908_1430ET"
    folder.mkdir(parents=True)
    (folder / "cands_2026-09-08_1430.json").write_text("[]", encoding="utf-8")
    root = RootSpec.of(tmp_path)
    detail = build_run_detail([root], only_run(root)["id"], config_dir=CONFIG)
    assert detail["found"] is True and detail["identity"] == {} and detail["run"]["incomplete"] is True
    assert detail["contract"]["ok"] is False
    assert detail["post_trade"]["available"] is False and detail["close_window"]["available"] is False
    assert detail["inputs"] == [{"name": "cands_2026-09-08_1430.json", "bytes": 2}]
    assert any("incomplete, not failed" in warning for warning in detail["warnings"])


def test_detail_explains_afternoon_evidence_gap_with_labels_only(tmp_path):
    afternoon_run(tmp_path, order_submit_status="blocked_kill_switch_evidence_gap",
                  risk_guard={"kill_switch_engaged": True})
    root = RootSpec.of(tmp_path)
    detail = build_run_detail([root], only_run(root)["id"], config_dir=CONFIG)
    explanation = detail["abort_explanation"]
    # afternoon_run() emits the canonical classification block (see its
    # docstring), so classify_stream counts the run as real scheduled afternoon
    # evidence and the diagnostic reports the tree as healthy -- the gap this
    # run aborted on is elsewhere. The "excluded_by_stream" reading belongs to
    # the older fixture that hand-rolled a three-key classification.
    assert explanation["likely_cause"] == "afternoon_evidence_healthy"
    assert explanation["as_of"] == "2026-09-08" and "cron_tracking_root" not in explanation
    assert all(entry["root"].startswith(f"<{root.label}>") for entry in explanation["roots"])
    assert str(tmp_path) not in json.dumps(detail)


def test_detail_opens_runs_the_capped_index_cannot_list(tmp_path):
    """The 500-row cap is presentation only; a clicked id must resolve past it (review: detail 404)."""
    for offset in range(501):
        (tmp_path / (date(2020, 1, 1) + timedelta(days=offset)).isoformat()).mkdir()
    root = RootSpec.of(tmp_path)
    oldest = run_identity(root.label, "2020-01-01")
    assert oldest not in {row["id"] for row in build_run_index([root])["runs"]}
    detail = build_run_detail([root], oldest, config_dir=CONFIG)
    assert detail["found"] is True and detail["run"]["day"] == "2020-01-01"
    assert not any("newest 500" in warning for warning in detail["warnings"])


def test_detail_honours_the_same_exclusions_as_the_index(tmp_path):
    write_state(tmp_path, "console/demo/20260901_1030ET", trading_day="2026-09-01")
    root = RootSpec.of(tmp_path)
    demo = run_identity(root.label, "console/demo/20260901_1030ET")
    assert build_run_detail([root], demo, config_dir=CONFIG)["found"] is True
    hidden = build_run_detail([root], demo, config_dir=CONFIG, excluded_roots=[tmp_path / "console"])
    assert hidden["found"] is False and any("No run" in warning for warning in hidden["warnings"])


def test_symlinked_root_scans_and_reads_the_same_tree(tmp_path):
    """A root reached through a symlink must index, open and validate like a plain one (review: resolved base)."""
    real = tmp_path / "real"
    folder = afternoon_run(real)
    link = tmp_path / "link"
    os.symlink(real, link, target_is_directory=True)
    # The documented afternoon pointer lives at the ce16ba6b root itself
    # (scripts.run_classification.latest_scheduled_run_path), and here it
    # spells the target through the symlink, as a pointer written by a
    # process that was itself started on the symlinked path would.
    (real / "ce16ba6b" / "latest_scheduled_run.json").write_text(json.dumps({
        "run_id": "scheduled_20260908_1530ET", "run_state_path": str(link / folder.relative_to(real) / "run_state.json"),
        "trading_day": DAY.isoformat(), "stream": "afternoon", "pointer_status": "valid"}), encoding="utf-8")
    root = RootSpec.of(link)
    index = build_run_index([root])
    assert [row["relative_dir"] for row in index["runs"]] == ["ce16ba6b/scheduled_runs/20260908_1530ET"]
    assert index["runs"][0]["id"] == run_identity(root.label, "ce16ba6b/scheduled_runs/20260908_1530ET")
    assert not any("malformed" in warning for warning in index["warnings"])
    detail = build_run_detail([root], index["runs"][0]["id"], config_dir=CONFIG)
    assert detail["found"] is True and detail["contract"]["ok"] is True and detail["drift"] is not None
    assert set(detail["tables"]) == {"signals.csv", "buy_candidates.csv", "submitted_orders.csv"}
    assert detail["post_trade"]["available"] is True and detail["close_window"]["raw_quote_files"] == 1
    tree = artifact_tree_map([root])["roots"][0]
    assert next(entry for entry in tree["directories"] if entry["name"] == "ce16ba6b/scheduled_runs")["entries"] == 1
    pointer = next(row for row in pointer_strip([root], DAY, config_dir=CONFIG)["streams"]
                   if row["stream"] == "afternoon" and row["status"] != "missing")
    assert pointer["status"] == "valid" and pointer["run_state_present"] is True
    assert pointer["order_submit_status"] == "submitted"
    for payload in (index, detail, tree, pointer):
        assert str(link) not in json.dumps(payload) and str(real) not in json.dumps(payload)


def test_detail_skips_outside_helpers_for_oversized_artifacts(tmp_path):
    """Contract and drift open files themselves; an artifact over the ceiling vetoes them (review: unbounded reads)."""
    folder = afternoon_run(tmp_path)
    # A sparse file reports the logical size without writing 32 MiB to disk;
    # errors.csv is one of the names the drift loader would otherwise open.
    with (folder / "errors.csv").open("wb") as handle:
        handle.truncate(MAX_BYTES + 1)
    root = RootSpec.of(tmp_path)
    detail = build_run_detail([root], only_run(root)["id"], config_dir=CONFIG)
    assert detail["found"] is True and detail["contract"] is None and detail["drift"] is None
    assert any("exceeds the 32 MiB read limit" in warning for warning in detail["warnings"])
    # The reader-backed sections are unaffected: the tables and post-trade
    # summary still come from their own bounded reads.
    assert detail["tables"]["signals.csv"]["rows"] and detail["post_trade"]["available"] is True


def test_detail_never_follows_artifacts_that_escape_the_root(tmp_path):
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "quotes.json").write_text(json.dumps({"results": [{"symbol": "ABC"}]}), encoding="utf-8")
    (outside / "errors.csv").write_text("ticker,error\nABC,boom\n", encoding="utf-8")
    root_dir = tmp_path / "root"
    folder = afternoon_run(root_dir)
    os.symlink(outside / "quotes.json", folder / "raw" / "quotes_escape.json")
    root = RootSpec.of(root_dir)
    detail = build_run_detail([root], only_run(root)["id"], config_dir=CONFIG)
    # The escaping quote file is not counted; the run directory itself is
    # still clean so the contract and drift helpers run.
    assert detail["close_window"]["raw_quote_files"] == 1 and detail["contract"]["ok"] is True
    os.symlink(outside / "errors.csv", folder / "errors.csv")
    detail = build_run_detail([root], only_run(root)["id"], config_dir=CONFIG)
    assert detail["contract"] is None and detail["drift"] is None
    assert any("resolves outside the root" in warning for warning in detail["warnings"])
    assert str(outside) not in json.dumps(detail)


def test_evidence_gap_diagnostic_is_vetoed_by_an_oversized_run_state(tmp_path):
    afternoon_run(tmp_path, order_submit_status="blocked_kill_switch_evidence_gap",
                  risk_guard={"kill_switch_engaged": True})
    other = tmp_path / "intraday_dca" / "20260908_1430ET"
    other.mkdir(parents=True)
    with (other / "run_state.json").open("wb") as handle:
        handle.truncate(MAX_BYTES + 1)
    root = RootSpec.of(tmp_path)
    afternoon = next(row for row in build_run_index([root])["runs"] if row["stream"] == "afternoon")
    detail = build_run_detail([root], afternoon["id"], config_dir=CONFIG)
    assert detail["abort_explanation"] is None
    assert any("evidence-gap diagnostic was skipped" in warning for warning in detail["warnings"])


def test_pointer_strip_statuses_evidence_and_alias(tmp_path):
    morning = write_state(tmp_path)
    (tmp_path / "morning_dca" / "latest_morning_run.json").write_text(json.dumps({
        "run_id": "scheduled_20260908_1030ET", "run_state_path": str(morning / "run_state.json"),
        "trading_day": DAY.isoformat(), "stream": "morning", "pointer_status": "valid"}), encoding="utf-8")
    (tmp_path / "morning_dca" / "latest_morning_live.json").write_text(json.dumps({
        "run_id": "x", "run_state_path": str(tmp_path / "morning_dca" / "gone" / "run_state.json"),
        "trading_day": DAY.isoformat(), "stream": "morning"}), encoding="utf-8")
    (tmp_path / "midday_dca").mkdir()
    (tmp_path / "midday_dca" / "latest_midday_run.json").write_text(json.dumps({
        "run_id": "x", "run_state_path": str(tmp_path / "midday_dca" / "20260907_1230ET" / "run_state.json"),
        "trading_day": "2026-09-07", "stream": "midday"}), encoding="utf-8")
    rows = pointer_strip([RootSpec.of(tmp_path, "secondary")], DAY, config_dir=CONFIG)["streams"]
    by_key = {(row["stream"], row["alias"]): row for row in rows}
    valid = by_key[("morning", False)]
    assert valid["status"] == "valid" and valid["evidence"] is True and valid["evidence_label"] == "scheduled evidence"
    assert valid["run_state_present"] is True and valid["order_submit_status"] is None and valid["at_risk"] is True
    assert valid["pointer_file"] == "morning_dca/latest_morning_run.json"
    alias = by_key[("morning", True)]
    assert alias["status"] == "pending" and alias["evidence"] is False and alias["evidence_label"] == "unverified"
    assert alias["run_state_present"] is False
    stale = by_key[("midday", False)]
    assert stale["status"] == "prior_date" and stale["evidence"] is False and stale["evidence_label"] == "rejected"
    assert stale["trading_day"] == "2026-09-07" and stale["reasons"]
    for stream in ("market_open", "intraday", "afternoon"):
        assert by_key[(stream, False)]["status"] == "missing" and by_key[(stream, False)]["evidence"] is False
    assert str(tmp_path) not in json.dumps(rows)


def test_pointer_strip_outside_root_is_reported_without_reading(tmp_path):
    outside = tmp_path / "elsewhere" / "run_state.json"
    outside.parent.mkdir()
    outside.write_text(json.dumps({"trading_day": DAY.isoformat(), "order_submit_status": "submitted"}))
    (tmp_path / "root" / "morning_dca").mkdir(parents=True)
    (tmp_path / "root" / "morning_dca" / "latest_morning_run.json").write_text(json.dumps({
        "run_id": "x", "run_state_path": str(outside), "trading_day": DAY.isoformat(), "stream": "morning"}))
    rows = pointer_strip([RootSpec.of(tmp_path / "root")], DAY, config_dir=CONFIG)["streams"]
    row = next(item for item in rows if item["stream"] == "morning")
    assert row["status"] == "outside_root" and row["run_state_present"] is False
    assert row["order_submit_status"] is None and row["evidence_label"] == "rejected"
    assert str(tmp_path) not in json.dumps(rows) and "<path>" in row["reasons"][0]


def test_outputs_never_carry_paths_or_account_numbers(tmp_path):
    afternoon_run(tmp_path, error_class=f"blocked {ACCOUNT}")
    (tmp_path / "morning_dca").mkdir()
    (tmp_path / "morning_dca" / "latest_morning_run.json").write_text(json.dumps({
        "run_id": "x", "run_state_path": "/elsewhere/run_state.json", "trading_day": DAY.isoformat(),
        "stream": "morning"}), encoding="utf-8")
    roots = [RootSpec.of(tmp_path, "secondary")]
    results = [artifact_tree_map(roots), build_run_index(roots),
               build_run_detail(roots, only_run(roots[0])["id"], config_dir=CONFIG),
               pointer_strip(roots, DAY, config_dir=CONFIG)]
    for result in results:
        assert str(tmp_path) not in json.dumps(result)
        assert ACCOUNT not in json.dumps(walk_redact(result))


def test_builders_never_take_a_lock(tmp_path, monkeypatch):
    def refuse(*args, **kwargs):
        raise AssertionError("a console reader took a lock")

    monkeypatch.setattr(scripts.file_lock, "cross_process_lock", refuse)
    monkeypatch.setattr(dca_runner, "state_transaction", refuse)
    afternoon_run(tmp_path, risk_guard={"kill_switch_engaged": True})
    write_state(tmp_path)
    roots = [RootSpec.of(tmp_path)]
    artifact_tree_map(roots)
    index = build_run_index(roots)
    for row in index["runs"]:
        assert build_run_detail(roots, row["id"], config_dir=CONFIG)["found"] is True
    pointer_strip(roots, DAY, config_dir=CONFIG)


@pytest.mark.parametrize("run_id", ["run-", "run-ABCDEF012345", "run-0123456789abc", ""])
def test_run_id_regex_is_strict(tmp_path, run_id):
    assert build_run_detail([RootSpec.of(tmp_path)], run_id, config_dir=CONFIG)["found"] is False
