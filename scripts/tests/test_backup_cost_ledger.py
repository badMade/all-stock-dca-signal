"""scripts/backup_cost_ledger.py: the operator's local log of Perplexity credits per backup fire.

Perplexity credits are not exposed to this repo (docs/cost_tracking.md), so the operator reads each fire's credits off
Perplexity's usage page and records them here. The ledger is the baseline the stand-down work is measured against: a
gitignored CSV under the cron root, never committed, because billing figures do not belong in a public repo.
"""

import json

import pytest

from scripts import backup_cost_ledger as ledger


@pytest.fixture(autouse=True)
def _cron_root(tmp_path, monkeypatch):
    monkeypatch.setenv("DCA_CRON_TRACKING_ROOT", str(tmp_path))
    return tmp_path


def _run(capsys, *argv):
    code = ledger.main(list(argv))
    return code, json.loads(capsys.readouterr().out)


def test_ledger_lives_under_the_cron_root_not_the_checkout(tmp_path):
    assert ledger.ledger_path() == tmp_path / "backup_cost_ledger.csv"


def test_add_appends_one_row_and_creates_the_header_once(tmp_path, capsys):
    assert _run(capsys, "add", "--day", "2026-10-29", "--task", "4412c3c4", "--credits", "312")[0] == 0
    assert _run(capsys, "add", "--day", "2026-10-29", "--task", "f83b8d62", "--credits", "40.5",
                "--status", "stood_down_local_active")[0] == 0
    lines = (tmp_path / "backup_cost_ledger.csv").read_text(encoding="utf-8").splitlines()
    assert lines[0].split(",")[:4] == ["trading_day", "task_id", "credits", "status"]
    assert len(lines) == 3


def test_add_echoes_the_row_but_never_a_path(tmp_path, capsys):
    code, out = _run(capsys, "add", "--day", "2026-10-29", "--task", "4412c3c4", "--credits", "312")
    assert code == 0 and out["ok"] is True
    assert out["row"]["credits"] == 312.0
    assert str(tmp_path) not in json.dumps(out)


@pytest.mark.parametrize("credits", ["-1", "abc", "nan", "inf", ""])
def test_add_rejects_credits_that_are_not_a_finite_non_negative_number(capsys, credits):
    code, out = _run(capsys, "add", "--day", "2026-10-29", "--task", "4412c3c4", "--credits", credits)
    assert code == 2 and out["ok"] is False
    assert not ledger.ledger_path().exists()


def test_add_rejects_a_task_that_is_not_a_backup_task(capsys):
    code, out = _run(capsys, "add", "--day", "2026-10-29", "--task", "2ba19526", "--credits", "10")
    assert code == 2 and out["error"] == "unknown_task"


@pytest.mark.parametrize("day", ["2026-13-01", "10/29/2026", "yesterday"])
def test_add_rejects_a_malformed_day(capsys, day):
    code, out = _run(capsys, "add", "--day", day, "--task", "4412c3c4", "--credits", "10")
    assert code == 2 and out["error"] == "bad_day"


def test_add_rejects_a_status_outside_the_state_record_set(capsys):
    code, out = _run(capsys, "add", "--day", "2026-10-29", "--task", "4412c3c4", "--credits", "10",
                     "--status", "whatever")
    assert code == 2 and out["error"] == "unknown_status"


def test_status_set_matches_the_procedure_state_record_statuses():
    # One vocabulary: the ledger groups by exactly the statuses STEP 8 of the served procedure can record.
    from scripts import backup_procedure

    for status in ledger.STATUSES:
        assert status in backup_procedure.PROCEDURE_TEMPLATE


def test_report_on_an_empty_ledger_is_ok_and_empty(capsys):
    code, out = _run(capsys, "report")
    assert code == 0
    assert out["days"] == [] and out["by_status"] == {} and out["target_per_day"] == 200


def test_report_totals_each_day_against_the_target_and_averages_by_status_and_task(capsys):
    rows = [
        ("2026-10-28", "4412c3c4", "300", "stood_down_local_active"),
        ("2026-10-28", "f83b8d62", "100", "stood_down_local_active"),
        ("2026-10-29", "4412c3c4", "50", "stood_down_local_active"),
        ("2026-10-29", "f83b8d62", "120", "placed"),
    ]
    for day, task, credits, status in rows:
        _run(capsys, "add", "--day", day, "--task", task, "--credits", credits, "--status", status)
    code, out = _run(capsys, "report")
    assert code == 0
    assert out["days"] == [
        {"trading_day": "2026-10-28", "credits": 400.0, "fires": 2, "over_target": True},
        {"trading_day": "2026-10-29", "credits": 170.0, "fires": 2, "over_target": False},
    ]
    assert out["by_status"]["stood_down_local_active"] == {"fires": 3, "mean_credits": 150.0}
    assert out["by_status"]["placed"] == {"fires": 1, "mean_credits": 120.0}
    assert out["by_task"]["4412c3c4"] == {"fires": 2, "mean_credits": 175.0}


def test_report_since_drops_earlier_days(capsys):
    _run(capsys, "add", "--day", "2026-10-27", "--task", "4412c3c4", "--credits", "300")
    _run(capsys, "add", "--day", "2026-10-29", "--task", "4412c3c4", "--credits", "20")
    _, out = _run(capsys, "report", "--since", "2026-10-28")
    assert [day["trading_day"] for day in out["days"]] == ["2026-10-29"]


def test_a_row_with_no_status_reports_under_unrecorded(capsys):
    _run(capsys, "add", "--day", "2026-10-29", "--task", "4412c3c4", "--credits", "20")
    _, out = _run(capsys, "report")
    assert out["by_status"] == {"unrecorded": {"fires": 1, "mean_credits": 20.0}}


def test_a_corrupt_ledger_fails_closed_without_echoing_its_content(capsys):
    ledger.ledger_path().write_text("trading_day,task_id,credits,status,recorded_at\nsecret-ish,row\n",
                                    encoding="utf-8")
    code, out = _run(capsys, "report")
    assert code == 2 and out["error"] == "ledger_corrupt"
    assert "secret-ish" not in json.dumps(out)


def test_the_ledger_file_is_gitignored():
    # Billing figures stay off the public repo: the default location is inside the gitignored cron_tracking/ tree.
    import subprocess
    from pathlib import Path

    repo = Path(__file__).resolve().parents[2]
    result = subprocess.run(["git", "-C", str(repo), "check-ignore", "-q", "cron_tracking/backup_cost_ledger.csv"],
                            check=False)
    assert result.returncode == 0
