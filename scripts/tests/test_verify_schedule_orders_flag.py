"""Tests for ``verify_schedule --orders-json``: CLI access to slot attribution.

The Python API (``verify_day(..., order_ids=...)``) shipped in #1804. Verifier and
digest sessions drive the CLI, not the API, so they had to hand-write a
``python -c`` call. The flag must stay reporting-only (never change the exit
status) and fail closed on input it cannot trust.
"""

import io
import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from datetime import date  # noqa: E402

from scripts import verify_schedule  # noqa: E402
from scripts.verify_schedule import slot_run_dir_relative  # noqa: E402

DAY = date(2026, 9, 22)  # a Tuesday the calendar has open, long past
DAY_ARGS = ["--date", DAY.isoformat()]


def write_state(root: Path, slot_id: str, order_ids) -> None:
    run_dir = root / slot_run_dir_relative(slot_id, DAY)
    run_dir.mkdir(parents=True)
    (run_dir / "run_state.json").write_text(json.dumps({"buy_candidates": [{"order_id": oid} for oid in order_ids]}))


def write_orders(tmp_path: Path, payload) -> Path:
    path = tmp_path / "orders.json"
    path.write_text(payload if isinstance(payload, str) else json.dumps(payload))
    return path


def run(tmp_path, capsys, *extra):
    code = verify_schedule.main([*DAY_ARGS, "--root", str(tmp_path / "root"), *extra])
    return code, capsys.readouterr()


@pytest.fixture
def tree(tmp_path):
    write_state(tmp_path / "root", "1030", ["a1"])
    return tmp_path


def test_orders_json_adds_order_attribution_to_the_json_report(tree, capsys):
    orders = write_orders(tree, ["a1", "stray"])

    _, out = run(tree, capsys, "--json", "--orders-json", str(orders))

    attribution = json.loads(out.out)["order_attribution"]
    assert attribution == {"attributed": {"a1": "1030"}, "unattributed": ["stray"], "ambiguous": {}}


def test_without_the_flag_the_report_shape_is_unchanged(tree, capsys):
    _, out = run(tree, capsys, "--json")

    assert "order_attribution" not in json.loads(out.out)


def test_dash_reads_the_order_ids_from_stdin(tree, capsys, monkeypatch):
    monkeypatch.setattr(sys, "stdin", io.StringIO(json.dumps(["a1"])))

    _, out = run(tree, capsys, "--json", "--orders-json", "-")

    assert json.loads(out.out)["order_attribution"]["attributed"] == {"a1": "1030"}


def test_attribution_never_changes_the_exit_status(tree, capsys):
    plain_code, _ = run(tree, capsys)
    code, _ = run(tree, capsys, "--orders-json", str(write_orders(tree, ["a1", "stray"])))

    assert code == plain_code == 1  # the other six slots have no evidence on this root


def test_text_output_names_attributed_and_unattributed_ids(tree, capsys):
    _, out = run(tree, capsys, "--orders-json", str(write_orders(tree, ["a1", "stray"])))

    lines = out.out.splitlines()
    assert any("a1" in line and "1030" in line for line in lines)
    assert any("unattributed" in line.lower() and "stray" in line for line in lines)


@pytest.mark.parametrize("payload", [
    "{not json",
    json.dumps({"order_id": "a1"}),
    json.dumps(["a1", 7]),
    json.dumps("a1"),
    json.dumps(["a1", ""]),
])
def test_untrusted_orders_input_fails_closed_with_usage_error(tree, capsys, payload):
    with pytest.raises(SystemExit) as exc:
        run(tree, capsys, "--json", "--orders-json", str(write_orders(tree, payload)))

    captured = capsys.readouterr()
    assert exc.value.code == 2
    assert captured.out == ""  # no half-built report on stdout
    # Exit 2 alone would also come from argparse rejecting an unknown flag, so
    # require the flag to be recognised and its own validation to speak.
    assert "unrecognized arguments" not in captured.err
    assert "--orders-json" in captured.err


def test_missing_orders_file_fails_closed_with_usage_error(tree, capsys):
    with pytest.raises(SystemExit) as exc:
        run(tree, capsys, "--json", "--orders-json", str(tree / "nope.json"))

    assert exc.value.code == 2
    captured = capsys.readouterr()
    assert captured.out == ""
    assert "unrecognized arguments" not in captured.err
    assert "--orders-json" in captured.err
