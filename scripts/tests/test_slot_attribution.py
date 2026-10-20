"""Tests for scripts/slot_attribution.py: map broker order ids to slots from run artifacts.

Why (2026-10-20 triage note): a verifier run guessed which slot made which order
cluster from order times, which do not line up with slot times (scheduler jitter,
backup periods). Attribution must come from the slot's own ``run_state.json``.
"""

import json
import sys
from datetime import date, datetime
from pathlib import Path
from zoneinfo import ZoneInfo

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from scripts.slot_attribution import attribute_orders  # noqa: E402
from scripts.verify_schedule import slot_run_dir_relative, verify_day  # noqa: E402

ET = ZoneInfo("America/New_York")
DAY = date(2026, 10, 20)
AFTER_CLOSE = datetime(2026, 10, 20, 18, 0, tzinfo=ET)


def write_state(root: Path, slot_id: str, state) -> Path:
    run_dir = root / slot_run_dir_relative(slot_id, DAY)
    run_dir.mkdir(parents=True)
    path = run_dir / "run_state.json"
    path.write_text(state if isinstance(state, str) else json.dumps(state))
    return path


def candidates(*order_ids):
    return {"buy_candidates": [{"ticker": "T%d" % i, "order_id": oid} for i, oid in enumerate(order_ids)]}


def test_orders_map_to_the_slot_whose_run_state_names_them(tmp_path):
    write_state(tmp_path, "1030", candidates("a1", "a2"))
    write_state(tmp_path, "1130", candidates("b1"))
    write_state(tmp_path, "1330", {"orders": [{"broker_order_id": "c1"}]})

    result = attribute_orders(DAY, ["a1", "a2", "b1", "c1"], root=tmp_path)

    assert result["attributed"] == {"a1": "1030", "a2": "1030", "b1": "1130", "c1": "1330"}
    assert result["unattributed"] == []
    assert result["ambiguous"] == {}


def test_an_order_no_artifact_names_is_unattributed_never_guessed(tmp_path):
    write_state(tmp_path, "1030", candidates("a1"))

    result = attribute_orders(DAY, ["a1", "stray"], root=tmp_path)

    assert result["attributed"] == {"a1": "1030"}
    assert result["unattributed"] == ["stray"]


def test_an_order_claimed_by_two_slots_is_ambiguous_not_attributed(tmp_path):
    write_state(tmp_path, "1130", candidates("dup"))
    write_state(tmp_path, "1330", candidates("dup"))

    result = attribute_orders(DAY, ["dup"], root=tmp_path)

    assert "dup" not in result["attributed"]
    assert result["ambiguous"] == {"dup": ["1130", "1330"]}


def test_missing_and_malformed_run_state_are_skipped_without_raising(tmp_path):
    write_state(tmp_path, "1030", "{not json")
    write_state(tmp_path, "1230", [1, 2, 3])
    write_state(tmp_path, "1530", {"buy_candidates": 1})

    result = attribute_orders(DAY, ["x"], root=tmp_path)

    assert result["attributed"] == {}
    assert result["unattributed"] == ["x"]


def test_other_days_run_directories_never_attribute_todays_orders(tmp_path):
    other = date(2026, 10, 19)
    run_dir = tmp_path / slot_run_dir_relative("1030", other)
    run_dir.mkdir(parents=True)
    (run_dir / "run_state.json").write_text(json.dumps(candidates("old")))

    result = attribute_orders(DAY, ["old"], root=tmp_path)

    assert result["unattributed"] == ["old"]


def test_verify_day_reports_attribution_only_when_order_ids_are_given(tmp_path):
    write_state(tmp_path, "1030", candidates("a1"))

    plain = verify_day(DAY, AFTER_CLOSE, root=tmp_path)
    with_orders = verify_day(DAY, AFTER_CLOSE, root=tmp_path, order_ids=["a1", "stray"])

    assert "order_attribution" not in plain
    assert with_orders["order_attribution"]["attributed"] == {"a1": "1030"}
    assert with_orders["order_attribution"]["unattributed"] == ["stray"]
    # Attribution is reporting only: it must not change the audit verdict.
    assert with_orders["ok"] == plain["ok"]
    assert with_orders["failing_slots"] == plain["failing_slots"]


def test_duplicate_ids_in_input_and_in_one_slot_are_not_ambiguous(tmp_path):
    write_state(tmp_path, "1030", {
        **candidates("a1", "a1"),
        "orders": [{"order_id": "a1"}, {"order_id": "a2"}],
    })

    result = attribute_orders(DAY, ["a1", "a1", "a2", "stray", "stray"], root=tmp_path)

    assert result == {
        "attributed": {"a1": "1030", "a2": "1030"},
        "unattributed": ["stray"],
        "ambiguous": {},
    }


def test_default_root_uses_the_configured_cron_tracking_root(tmp_path, monkeypatch):
    monkeypatch.setattr("scripts.slot_attribution.cron_tracking_root", lambda: tmp_path)
    write_state(tmp_path, "1530", candidates("a1"))

    assert attribute_orders(DAY, ["a1"])["attributed"] == {"a1": "1530"}


def test_unreadable_state_does_not_abort_other_slots(tmp_path, monkeypatch):
    unreadable = write_state(tmp_path, "1030", candidates("a1"))
    write_state(tmp_path, "1130", candidates("b1"))
    read_bytes = Path.read_bytes

    def read(path):
        if path == unreadable:
            raise OSError("unreadable")
        return read_bytes(path)

    monkeypatch.setattr(Path, "read_bytes", read)

    result = attribute_orders(DAY, ["a1", "b1"], root=tmp_path)

    assert result["attributed"] == {"b1": "1130"}
    assert result["unattributed"] == ["a1"]


def test_explicit_empty_order_ids_add_an_empty_report_without_changing_verdict(tmp_path):
    plain = verify_day(DAY, AFTER_CLOSE, root=tmp_path)
    report = verify_day(DAY, AFTER_CLOSE, root=tmp_path, order_ids=[])

    assert report.pop("order_attribution") == {"attributed": {}, "unattributed": [], "ambiguous": {}}
    assert report == plain
