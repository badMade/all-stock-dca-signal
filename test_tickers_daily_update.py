"""Tests for the program-facing Claude ticker-history update wrapper."""

import json
from pathlib import Path

from scripts.run_tickers_daily_update import run_incremental_update


def _write_updater(path: Path, body: str) -> None:
    path.write_text(body, encoding="utf-8")


def test_wrapper_runs_until_the_external_updater_reports_complete(tmp_path):
    history_dir = tmp_path / "histories"
    history_dir.mkdir()
    state_path = tmp_path / "state.txt"
    ticker_config = tmp_path / "tickers.json"
    artifacts_dir = tmp_path / "events"
    ticker_config.write_text('{"active": []}', encoding="utf-8")
    updater = tmp_path / "incremental_update.py"
    _write_updater(
        updater,
        "from pathlib import Path\n"
        f"state = Path({str(state_path)!r})\n"
        "count = int(state.read_text() if state.exists() else '0') + 1\n"
        "state.write_text(str(count))\n"
        "print('ALL_DONE' if count == 2 else 'DONE_THIS_RUN updated=3 remaining=4')\n",
    )

    result = run_incremental_update(
        updater_script=updater,
        history_dir=history_dir,
        max_invocations=4,
        ticker_config=ticker_config,
        artifacts_dir=artifacts_dir,
    )

    assert result["status"] == "completed"
    assert result["invocations"] == 2
    assert result["updated_total"] == 3
    assert result["remaining"] == 0
    assert result["errors"] == []
    assert (artifacts_dir / "ticker_update_event.json").is_file()
    assert "status=completed invocations=2 updated=3 remaining=0 errors=[]" in (
        history_dir / "_daily_update_log.txt"
    ).read_text(encoding="utf-8")


def test_wrapper_stops_after_repeated_non_decreasing_progress(tmp_path):
    history_dir = tmp_path / "histories"
    history_dir.mkdir()
    updater = tmp_path / "incremental_update.py"
    _write_updater(updater, "print('DONE_THIS_RUN updated=0 remaining=4')\n")

    result = run_incremental_update(
        updater_script=updater,
        history_dir=history_dir,
        max_invocations=10,
        artifacts_dir=tmp_path / "events",
    )

    assert result["status"] == "partial"
    assert result["invocations"] == 4
    assert result["remaining"] == 4
    assert result["errors"] == [
        "remaining count did not decrease across four consecutive invocations"
    ]


def test_updated_active_ticker_is_published_for_calibration(tmp_path):
    history_dir = tmp_path / "histories"
    history_dir.mkdir()
    history = history_dir / "ABC.csv"
    history.write_text("Date,Close\n2026-08-01,10\n", encoding="utf-8")
    ticker_config = tmp_path / "tickers.json"
    ticker_config.write_text('{"active": ["ABC", "OTHER"]}', encoding="utf-8")
    updater = tmp_path / "incremental_update.py"
    _write_updater(
        updater,
        "from pathlib import Path\n"
        f"history = Path({str(history)!r})\n"
        "history.write_text('Date,Close\\n2026-08-02,11\\n')\n"
        "print('ALL_DONE')\n",
    )

    events_dir = tmp_path / "events"
    result = run_incremental_update(
        updater_script=updater,
        history_dir=history_dir,
        ticker_config=ticker_config,
        artifacts_dir=events_dir,
    )

    assert result["changed_tickers"] == ["ABC"]
    assert result["changed_active_tickers"] == ["ABC"]
    event = json.loads((events_dir / "ticker_update_event.json").read_text(encoding="utf-8"))
    assert event["changed_active_tickers"] == ["ABC"]


def test_wrapper_passes_the_shared_history_dir_to_the_external_updater(tmp_path):
    history_dir = tmp_path / "histories"
    history_dir.mkdir()
    updater = tmp_path / "incremental_update.py"
    _write_updater(
        updater,
        "import os\n"
        "from pathlib import Path\n"
        f"expected = {str(history_dir.resolve())!r}\n"
        "assert os.environ['TICKERS_DIR'] == expected\n"
        "print('ALL_DONE')\n",
    )

    result = run_incremental_update(
        updater_script=updater,
        history_dir=history_dir,
        artifacts_dir=tmp_path / "events",
    )

    assert result["status"] == "completed"


def test_relative_updater_script_still_resolves_after_chdir(tmp_path, monkeypatch):
    """A manifest-supplied relative script path must resolve against the
    process's cwd at call time, not silently double up under the
    subprocess's own cwd (its parent directory) (Codex review: "Resolve
    the updater path before switching directories")."""
    history_dir = tmp_path / "histories"
    history_dir.mkdir()
    scripts_dir = tmp_path / "scripts"
    scripts_dir.mkdir()
    updater = scripts_dir / "incremental_update.py"
    _write_updater(updater, "print('ALL_DONE')\n")

    monkeypatch.chdir(tmp_path)
    result = run_incremental_update(
        updater_script=Path("scripts/incremental_update.py"),
        history_dir=history_dir,
        artifacts_dir=tmp_path / "events",
    )

    assert result["status"] == "completed"
    assert result["errors"] == []


def test_all_done_from_a_nonzero_exit_is_never_accepted_as_complete(tmp_path):
    """ALL_DONE printed just before a nonzero exit must not be treated as a
    completed run (Codex review: "Reject ALL_DONE from a failed updater
    process")."""
    history_dir = tmp_path / "histories"
    history_dir.mkdir()
    updater = tmp_path / "incremental_update.py"
    _write_updater(
        updater,
        "import sys\nprint('ALL_DONE')\nsys.exit(1)\n",
    )

    result = run_incremental_update(
        updater_script=updater,
        history_dir=history_dir,
        artifacts_dir=tmp_path / "events",
    )

    assert result["status"] == "partial"
    assert result["errors"] and "exited 1" in result["errors"][0]


def test_a_hung_invocation_degrades_to_partial_instead_of_hanging_forever(tmp_path):
    """A subprocess that never returns must time out into a safe partial
    event rather than block the caller indefinitely (Codex review: "Bound
    each updater subprocess invocation")."""
    history_dir = tmp_path / "histories"
    history_dir.mkdir()
    updater = tmp_path / "incremental_update.py"
    _write_updater(updater, "import time\ntime.sleep(30)\n")

    result = run_incremental_update(
        updater_script=updater,
        history_dir=history_dir,
        artifacts_dir=tmp_path / "events",
        invocation_timeout_seconds=0.2,
    )

    assert result["status"] == "partial"
    assert result["errors"] and "timeout" in result["errors"][0]
