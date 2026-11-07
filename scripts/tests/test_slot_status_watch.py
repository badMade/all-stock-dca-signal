"""The "running" signal (step 6 of the credit plan, 2026-11-05).

The Mac dispatcher checks a slot 15 minutes after it starts, and the cloud watcher 20 minutes after. On 2026-11-05 the
9:35 run was still placing orders at 9:50 and recorded ``succeeded`` only at 9:55:04, so both would have emailed
Perplexity while Claude was mid-run. The live runs never call local_heartbeat, so nothing said "running".

``scripts/slot_status_watch.py`` runs on the Mac in the publish job (every 2 minutes and on every change): a slot
whose dated run folder exists today but has no final status is recorded ``in_progress``, with ``activity_at`` = the
newest file time in that folder, and then the document is published. A final status is never overwritten. Kept out
of ``scripts/slot_status.py`` so the Perplexity planner, which bundles that module, does not change.
"""

from __future__ import annotations

import json
import os
from datetime import date, datetime
from pathlib import Path
from types import SimpleNamespace
from zoneinfo import ZoneInfo

import pytest

from scripts import slot_status as ss
from scripts import slot_status_watch as sw
from scripts.verify_schedule import slot_run_dir_relative

ET = ZoneInfo("America/New_York")
DAY = date(2026, 11, 5)
NOW = datetime(2026, 11, 5, 9, 50, tzinfo=ET)


@pytest.fixture(autouse=True)
def _state_root(tmp_path, monkeypatch):
    monkeypatch.setattr(ss, "state_dir", lambda: tmp_path / "slot_status")


def _run_dir(root: Path, slot: str, day: date = DAY, touched: datetime | None = None) -> Path:
    run_dir = root / slot_run_dir_relative(slot, day)
    run_dir.mkdir(parents=True, exist_ok=True)
    stamp = (touched or datetime(2026, 11, 5, 9, 47, tzinfo=ET)).timestamp()
    for name in ("payload_plan_0935.json", "place_AMD.json"):
        path = run_dir / name
        path.write_text("{}", encoding="utf-8")
        os.utime(path, (stamp, stamp))
    return run_dir


def _local():
    return json.loads(ss.state_path().read_text(encoding="utf-8"))


def test_a_run_folder_with_no_final_status_is_marked_in_progress_with_its_last_activity(tmp_path):
    _run_dir(tmp_path / "cron", "0935", touched=datetime(2026, 11, 5, 9, 48, 30, tzinfo=ET))
    marked = sw.mark_running(tmp_path / "cron", now=NOW)
    assert marked == ["0935"]
    entry = _local()["slots"]["0935"]
    assert entry["status"] == "in_progress"
    assert entry["activity_at"] == "2026-11-05T09:48:30-05:00"  # the newest file in the folder, in ET


@pytest.mark.parametrize("status", ["succeeded", "failed", "backup_requested"])
def test_a_final_status_is_never_overwritten(tmp_path, status):
    ss.record(DAY.isoformat(), "0935", status, now=NOW)
    _run_dir(tmp_path / "cron", "0935")
    assert sw.mark_running(tmp_path / "cron", now=NOW) == []
    assert _local()["slots"]["0935"]["status"] == status


def test_an_in_progress_slot_gets_its_activity_refreshed(tmp_path):
    _run_dir(tmp_path / "cron", "0935", touched=datetime(2026, 11, 5, 9, 40, tzinfo=ET))
    sw.mark_running(tmp_path / "cron", now=NOW)
    _run_dir(tmp_path / "cron", "0935", touched=datetime(2026, 11, 5, 9, 49, tzinfo=ET))
    sw.mark_running(tmp_path / "cron", now=NOW)
    assert _local()["slots"]["0935"]["activity_at"] == "2026-11-05T09:49:00-05:00"


def test_no_run_folder_means_nothing_is_recorded(tmp_path):
    (tmp_path / "cron").mkdir()
    assert sw.mark_running(tmp_path / "cron", now=NOW) == []
    assert not ss.state_path().exists()


def test_yesterdays_run_folder_does_not_mark_today(tmp_path):
    _run_dir(tmp_path / "cron", "0935", day=date(2026, 11, 4), touched=datetime(2026, 11, 4, 9, 47, tzinfo=ET))
    assert sw.mark_running(tmp_path / "cron", now=NOW) == []


def test_every_slot_with_a_folder_is_marked(tmp_path):
    for slot in ("0935", "1030"):
        _run_dir(tmp_path / "cron", slot)
    assert sorted(sw.mark_running(tmp_path / "cron", now=NOW)) == ["0935", "1030"]


def test_the_document_still_carries_no_trading_data(tmp_path):
    _run_dir(tmp_path / "cron", "0935")
    sw.mark_running(tmp_path / "cron", now=NOW)
    entry = _local()["slots"]["0935"]
    assert set(entry) == {"status", "stamped_at", "activity_at"}  # no tickers, amounts, order ids or file names


class RecordingPublisher:
    channel = "github"

    def __init__(self):
        self.documents = []

    def publish(self, document):
        self.documents.append(json.loads(json.dumps(document)))
        return SimpleNamespace(channel=self.channel, ok=True, detail="abc123")


def test_cli_marks_running_slots_then_publishes(tmp_path, monkeypatch, capsys):
    publisher = RecordingPublisher()
    monkeypatch.setattr(ss, "_configured_publisher", lambda: publisher)
    _run_dir(tmp_path / "cron", "0935")
    assert sw.main(["--root", str(tmp_path / "cron"), "--now", NOW.isoformat()]) == 0
    out = json.loads(capsys.readouterr().out)
    assert out["marked"] == ["0935"] and out["published"] is True
    assert publisher.documents[0]["slots"]["0935"]["status"] == "in_progress"


def test_cli_with_nothing_running_still_publishes_a_changed_document(tmp_path, monkeypatch, capsys):
    # The finalizer records "succeeded" from the VM; the watch must still push it (it replaces plain `publish`).
    publisher = RecordingPublisher()
    monkeypatch.setattr(ss, "_configured_publisher", lambda: publisher)
    (tmp_path / "cron").mkdir()
    ss.record(DAY.isoformat(), "0935", "succeeded", now=NOW)
    assert sw.main(["--root", str(tmp_path / "cron"), "--now", NOW.isoformat()]) == 0
    assert json.loads(capsys.readouterr().out)["marked"] == []
    assert publisher.documents[0]["slots"]["0935"]["status"] == "succeeded"


def test_cli_never_raises_on_an_unreadable_root(tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(ss, "_configured_publisher", lambda: RecordingPublisher())
    assert sw.main(["--root", str(tmp_path / "missing"), "--now", NOW.isoformat()]) in (0, 2)
    assert "Traceback" not in capsys.readouterr().err


def test_the_planner_does_not_bundle_the_watch():
    # scripts/slot_status.py is inside backup_planner.pyz; the watch must not be, or every change re-stamps it.
    from scripts import build_backup_planner as bbp

    repo = Path(__file__).resolve().parents[2]
    assert "scripts/slot_status_watch.py" not in bbp.collect_files(repo)


# -- a finished folder is never marked running (2026-11-05 review) -----------------------------------------------------
# The watch runs on the Mac; the finalizer records "succeeded" from inside the Cowork VM. Their shared flock lock is
# not seen across the VM boundary, so a watch write landing in the same instant could replace "succeeded" with
# "in_progress", and the slot would read as stalled 10 minutes later. The finalizer writes run_summary.json into the
# run folder BEFORE it records the status (finalize_run_evidence.main: finalize(), then _post_slot_signal), so a
# folder holding run_summary.json is finished: the watch leaves its slot alone whatever the document says.


def test_a_folder_with_a_run_summary_is_finished_and_never_marked(tmp_path):
    run_dir = _run_dir(tmp_path / "cron", "0935")
    (run_dir / "run_summary.json").write_text("{}", encoding="utf-8")
    assert sw.mark_running(tmp_path / "cron", now=NOW) == []
    assert not ss.state_path().exists()  # nothing written, so nothing can overwrite the finalizer's status


def test_a_finished_folder_never_turns_an_in_progress_entry_back_on(tmp_path):
    # The race itself: the watch saw the folder mid-run, the finalizer has since written run_summary.json, and the
    # document still says in_progress (the status write lost the race or is about to land). Do not refresh it.
    run_dir = _run_dir(tmp_path / "cron", "0935", touched=datetime(2026, 11, 5, 9, 40, tzinfo=ET))
    sw.mark_running(tmp_path / "cron", now=NOW)
    before = _local()["slots"]["0935"]
    (run_dir / "run_summary.json").write_text("{}", encoding="utf-8")
    assert sw.mark_running(tmp_path / "cron", now=NOW) == []
    assert _local()["slots"]["0935"] == before


def test_a_folder_without_a_run_summary_is_still_marked(tmp_path):
    run_dir = _run_dir(tmp_path / "cron", "0935")
    (run_dir / "run_state.json").write_text("{}", encoding="utf-8")  # an executor's early state is not the end
    assert sw.mark_running(tmp_path / "cron", now=NOW) == ["0935"]
