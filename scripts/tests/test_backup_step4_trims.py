"""Step 4 of the 2026-10-30 credit plan: trim the fires that get past STEP A, and measure the change.

After step 3 a normal-day fire ends at STEP A. What still costs credits is a fire that gets past it: a holiday the
morning ``market-day`` run did not mark, a fire outside its window, or a real backup. Three trims, plus the ledger
comparison that shows whether each change paid off:

* every planner command prints one compact JSON line (the agent re-reads tool output on every later turn, and
  ``indent=2`` roughly doubled the 91-symbol ``universe`` list);
* a gates result of ``skipped_market_closed`` or ``skipped_outside_window`` ends the run inside part 0, with the
  state record and no procedure part, device listing or notification (before, a compliant agent fetched all three
  parts, about 22 KB, to write one record);
* ``skipped_outside_window`` joins the no-notification statuses, like ``skipped_market_closed``;
* ``backup_cost_ledger report --split D`` compares credits per fire and per day before and after a change.
"""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pytest

from scripts import backup_cost_ledger as ledger
from scripts import backup_procedure

REPO = Path(__file__).resolve().parents[2]
SKIPS = ("skipped_market_closed", "skipped_outside_window")


def _planner(*args: str) -> subprocess.CompletedProcess:
    return subprocess.run([sys.executable, "-m", "scripts.backup_planner", *args], cwd=REPO,
                          capture_output=True, text=True, check=False)


# -- compact planner output -------------------------------------------------------------------------------------------


@pytest.mark.parametrize("args", [
    ("universe",),
    ("gates", "--task", "4412c3c4", "--now", "2026-10-29T10:00:00-04:00"),   # PROCEED
    ("gates", "--task", "4412c3c4", "--now", "2026-10-31T10:00:00-04:00"),   # Saturday: skipped_market_closed
    ("gates", "--task", "deadbeef", "--now", "2026-10-29T10:00:00-04:00"),   # refused: exit 2
])
def test_every_planner_json_output_is_one_compact_line(args):
    done = _planner(*args)
    text = done.stdout.rstrip("\n")
    assert "\n" not in text, "one line, so the agent re-reads as little as possible"
    document = json.loads(text)
    assert text == json.dumps(document, separators=(",", ":"), sort_keys=False) or text == json.dumps(
        document, separators=(",", ":"), sort_keys=True)


def test_compact_universe_output_is_much_smaller_than_the_pretty_one():
    text = _planner("universe").stdout.strip()
    document = json.loads(text)
    assert len(text) < 0.75 * len(json.dumps(document, indent=2))
    assert document["status"] == "OK" and document["count"] == len(document["symbols"])


# -- skipped gates end inside part 0 ---------------------------------------------------------------------------------


def _gate_a(task_id: str) -> str:
    part0 = backup_procedure.render_part(task_id, 0)
    gate = part0.split("\nSTAND-DOWN GATE", 1)[1]
    return gate.split("\nb. ", 1)[0]


@pytest.mark.parametrize("backup", backup_procedure.BACKUPS, ids=lambda b: b.task_id)
def test_a_skipped_gates_result_ends_the_run_inside_part_0(backup):
    step = _gate_a(backup.task_id)
    for status in SKIPS:
        assert status in step, status
    for phrase in ("stop with exactly that status", "request no procedure part", "make no device listing",
                   "send no notification", "${PPLX_RUN_STATE_DIR}/run_state_"):
        assert phrase in step, phrase
    assert "no account lookup" in step and "no broker call" in step


@pytest.mark.parametrize("backup", backup_procedure.BACKUPS, ids=lambda b: b.task_id)
def test_an_unusable_gates_result_fails_closed_inside_part_0(backup):
    step = _gate_a(backup.task_id)
    assert "Any other gates status, a non-zero exit, or output that is not that JSON document" in step
    assert "place NOTHING, record failed_unexpected_error, notify, and stop" in step


@pytest.mark.parametrize("backup", backup_procedure.BACKUPS, ids=lambda b: b.task_id)
def test_only_proceed_reaches_the_mac_probe_and_the_procedure(backup):
    part0 = backup_procedure.render_part(backup.task_id, 0)
    gate_e = part0.split("\ne. ", 1)[1].split("\n", 1)[0]
    assert "a gates status other than PROCEED" not in gate_e
    assert "continue with the PROCEDURE below" in gate_e


def test_the_skipped_record_keeps_the_stand_down_record_shape():
    step = _gate_a(backup_procedure.BACKUPS[0].task_id)
    for key in ("trading_day", "slot", "task_id", "fired_at_et", "planner_build", "universe_hash", "sizing_version",
                "status", "run_origin", "account_last4", "plan_source", "orders_placed", "errors"):
        assert key in step, key
    assert "scheduled_backup" in step
    assert "Never include the account number" in step


# -- no notification for a fire outside its window -------------------------------------------------------------------


def test_skipped_outside_window_joins_the_no_notification_statuses():
    step9 = backup_procedure.PROCEDURE_TEMPLATE.split("STEP 9 - NOTIFY", 1)[1]
    exempt = step9.split("for every status except", 1)[1].split(".", 1)[0]
    for status in ("placed", "nothing_to_place", "shadow_only", "stood_down_local_active", *SKIPS):
        assert status in exempt, status
    assert "failed_unexpected_error" not in exempt and "stood_down_local_unknown" not in exempt


# -- before / after comparison in the ledger --------------------------------------------------------------------------


@pytest.fixture
def _cron_root(tmp_path, monkeypatch):
    monkeypatch.setenv("DCA_CRON_TRACKING_ROOT", str(tmp_path))


def _add(day, task, credits, status=None):
    argv = ["add", "--day", day, "--task", task, "--credits", str(credits)]
    if status:
        argv += ["--status", status]
    assert ledger.main(argv) == 0


def test_report_split_compares_before_and_after_a_change(_cron_root, capsys):
    _add("2026-10-28", "4412c3c4", 300, "stood_down_local_active")
    _add("2026-10-28", "f83b8d62", 400, "stood_down_local_active")
    _add("2026-10-29", "4412c3c4", 200, "stood_down_local_active")
    _add("2026-11-02", "4412c3c4", 20)
    _add("2026-11-02", "f83b8d62", 30)
    _add("2026-11-03", "4412c3c4", 10)
    capsys.readouterr()
    assert ledger.main(["report", "--split", "2026-11-02"]) == 0
    out = json.loads(capsys.readouterr().out)
    assert out["split"] == "2026-11-02"
    assert out["before"] == {"days": 2, "fires": 3, "mean_credits_per_fire": 300.0, "mean_credits_per_day": 450.0}
    assert out["after"] == {"days": 2, "fires": 3, "mean_credits_per_fire": 20.0, "mean_credits_per_day": 30.0}
    assert out["after_under_target"] is True


def test_report_split_with_no_rows_on_one_side_reports_nulls(_cron_root, capsys):
    _add("2026-10-28", "4412c3c4", 300)
    capsys.readouterr()
    assert ledger.main(["report", "--split", "2026-11-02"]) == 0
    out = json.loads(capsys.readouterr().out)
    assert out["after"] == {"days": 0, "fires": 0, "mean_credits_per_fire": None, "mean_credits_per_day": None}
    assert out["after_under_target"] is None


def test_report_split_rejects_a_malformed_day(_cron_root, capsys):
    assert ledger.main(["report", "--split", "next week"]) == 2
    assert json.loads(capsys.readouterr().out)["error"] == "bad_day"
