"""Tests for ``scripts/fill_perplexity_backup_prompts.py``.

The six Perplexity backup prompts committed to the repo carry three placeholders (the account's last four
digits, the run-state folder, the alert email) so the repo never holds those values. This script renders a
private, filled copy from values the operator keeps locally, for pasting into Perplexity.

Written from the required behavior, not from the implementation:

* Every placeholder is filled, per file, and nothing else in the prompt changes.
* The state folder is per task (each file's own task id), so two tasks never share a state record.
* Fail closed and all-or-nothing: a missing or malformed value, an unknown leftover placeholder or a prompt
  without a task header writes NOTHING.
* The private copies can never land in the repo checkout (where they could be committed), are owner-only, and
  the values are never echoed to the terminal.
* The real committed prompts fill cleanly, so a generator change that renames a placeholder fails here.
"""

from __future__ import annotations

import os
import stat
from pathlib import Path

import pytest

from scripts import fill_perplexity_backup_prompts as fill

ROOT = Path(__file__).resolve().parents[2]
REAL_PROMPTS = ROOT / "claude" / "dca-executor-prompts" / "perplexity"

LAST4 = "4321"
EMAIL = "ops@example.test"
ACCOUNT = "7" * 5 + LAST4  # synthetic; built so no account-shaped literal sits in this file
PROJECT_DIR = "/work/projects/dca-synthetic-0001/files"  # synthetic; the real folder is the operator's own

TEMPLATE = """STANDING BACKUP EXECUTOR - slot {slot}
Task {task}. Fires 10:00 ET on trading days.
2. ACCOUNT: the number must end in ${{ROBINHOOD_AGENTIC_ACCOUNT_LAST4}}.
Write ${{PPLX_RUN_STATE_DIR}}/run_state_<YYYYMMDD>_<HHMM>ET.json
Append to ${{PPLX_RUN_STATE_DIR}}/submitted_orders.csv
Send email to ${{DCA_ALERT_EMAIL_ADDRESS}}, never a hardcoded address.
"""


def _write_prompts(directory: Path, tasks=(("0935", "aaaa1111"), ("1030", "bbbb2222"))) -> Path:
    directory.mkdir(parents=True, exist_ok=True)
    for slot, task in tasks:
        (directory / f"{slot}-backup.txt").write_text(TEMPLATE.format(slot=slot, task=task), encoding="utf-8")
    return directory


@pytest.fixture
def src(tmp_path):
    return _write_prompts(tmp_path / "src")


@pytest.fixture
def out(tmp_path):
    return tmp_path / "out"


def _env(**over):
    base = {"DCA_ALERT_EMAIL_ADDRESS": EMAIL, "ROBINHOOD_AGENTIC_ACCOUNT_NUMBER": ACCOUNT, "DCA_DISABLE_DOTENV": "1",
            "PPLX_PROJECT_FILES_DIR": PROJECT_DIR}
    base.update(over)
    return {k: v for k, v in base.items() if v is not None}


def _run(src, out, env, *extra):
    return fill.main(["--src", str(src), "--out", str(out), *extra], environ=env)


# -- filling ---------------------------------------------------------------------------------------------------


def test_every_placeholder_is_filled_and_no_placeholder_is_left(src, out):
    assert _run(src, out, _env()) == 0
    for path in sorted(out.glob("*-backup.txt")):
        text = path.read_text(encoding="utf-8")
        assert "${" not in text, path.name
        assert f"must end in {LAST4}." in text
        assert f"email to {EMAIL}," in text


def test_the_state_folder_is_per_task_so_two_tasks_never_share_a_state_record(src, out):
    assert _run(src, out, _env()) == 0
    first = (out / "0935-backup.txt").read_text(encoding="utf-8")
    second = (out / "1030-backup.txt").read_text(encoding="utf-8")
    assert "cron_tracking/aaaa1111/run_state_" in first and "cron_tracking/aaaa1111/submitted_orders.csv" in first
    assert "cron_tracking/bbbb2222/run_state_" in second and "aaaa1111" not in second


def test_the_state_root_can_be_changed(src, out):
    assert _run(src, out, _env(), "--state-root", "projects/DCA/cron_tracking") == 0
    assert "projects/DCA/cron_tracking/aaaa1111/run_state_" in (out / "0935-backup.txt").read_text(encoding="utf-8")


def test_only_the_placeholders_change(src, out):
    assert _run(src, out, _env()) == 0
    original = (src / "0935-backup.txt").read_text(encoding="utf-8").splitlines()
    filled = (out / "0935-backup.txt").read_text(encoding="utf-8").splitlines()
    assert len(original) == len(filled)
    changed = [i for i, (a, b) in enumerate(zip(original, filled, strict=True)) if a != b]
    assert changed == [2, 3, 4, 5]


def test_the_source_prompts_are_left_untouched(src, out):
    before = {p.name: p.read_bytes() for p in src.glob("*.txt")}
    assert _run(src, out, _env()) == 0
    assert {p.name: p.read_bytes() for p in src.glob("*.txt")} == before


def test_a_folder_mapping_names_the_state_folder_per_task(src, out):
    assert _run(src, out, _env(), "--folder", "aaaa1111=uuid-one-0001", "--folder", "bbbb2222=uuid-two-0002") == 0
    first = (out / "0935-backup.txt").read_text(encoding="utf-8")
    assert "cron_tracking/uuid-one-0001/run_state_" in first and "aaaa1111/" not in first
    assert "cron_tracking/uuid-two-0002/submitted_orders.csv" in (out / "1030-backup.txt").read_text(encoding="utf-8")


def test_a_partial_folder_mapping_fails_closed(src, out):
    assert _run(src, out, _env(), "--folder", "aaaa1111=uuid-one-0001") == 2
    assert not out.exists() or not list(out.iterdir())


@pytest.mark.parametrize("bad", ["aaaa1111", "aaaa1111=", "zz=uuid-one-0001", "aaaa1111=../x", "aaaa1111=a b"])
def test_a_malformed_folder_argument_is_rejected(src, out, bad):
    assert _run(src, out, _env(), "--folder", bad) == 2
    assert not out.exists() or not list(out.iterdir())


def test_two_tasks_may_not_share_one_folder(src, out):
    assert _run(src, out, _env(), "--folder", "aaaa1111=same-folder-1", "--folder", "bbbb2222=same-folder-1") == 2
    assert not out.exists() or not list(out.iterdir())


def test_a_destination_that_is_a_directory_writes_nothing_and_exits_2(src, out, capsys):
    out.mkdir()
    (out / "1030-backup.txt").mkdir()
    assert _run(src, out, _env()) == 2
    assert not (out / "0935-backup.txt").exists()
    assert not list(out.glob("*.tmp"))
    assert "directory" in capsys.readouterr().err


def test_a_short_write_cannot_leave_a_truncated_prompt(src, out, monkeypatch):
    """The temp file is written through a file object, so a partial os.write can never be moved into place."""
    real_write = os.write
    monkeypatch.setattr(os, "write", lambda fd, data: real_write(fd, data[:5]))
    assert _run(src, out, _env()) == 0
    assert (out / "0935-backup.txt").read_text(encoding="utf-8") == (
        fill.render(
            (src / "0935-backup.txt").read_text(encoding="utf-8"),
            name="x", last4=LAST4, email=EMAIL, state_root="cron_tracking",
        )
    )


def test_a_task_mapped_twice_is_rejected(src, out):
    assert _run(src, out, _env(), "--folder", "aaaa1111=uuid-one-0001", "--folder", "aaaa1111=uuid-two-0002") == 2


# -- where the values come from ----------------------------------------------------------------------------------


def test_the_last_four_digits_come_from_the_account_number_the_repo_already_uses(src, out):
    assert _run(src, out, _env()) == 0
    assert f"must end in {LAST4}." in (out / "0935-backup.txt").read_text(encoding="utf-8")


def test_an_explicit_last_four_overrides_the_account_number(src, out):
    assert _run(src, out, _env(ROBINHOOD_AGENTIC_ACCOUNT_LAST4="9999")) == 0
    assert "must end in 9999." in (out / "0935-backup.txt").read_text(encoding="utf-8")


def test_an_explicit_last_four_alone_is_enough(src, out):
    assert _run(src, out, _env(ROBINHOOD_AGENTIC_ACCOUNT_NUMBER=None, ROBINHOOD_AGENTIC_ACCOUNT_LAST4="9999")) == 0


# -- fail closed, all or nothing -----------------------------------------------------------------------------------


@pytest.mark.parametrize("missing", ["DCA_ALERT_EMAIL_ADDRESS", "ROBINHOOD_AGENTIC_ACCOUNT_NUMBER"])
def test_a_missing_value_writes_nothing_and_fails(src, out, missing, capsys):
    assert _run(src, out, _env(**{missing: None})) == 2
    assert not out.exists() or not list(out.iterdir())
    assert "missing" in capsys.readouterr().err.lower()


@pytest.mark.parametrize("bad", ["123", "12345", "12a4", "", " 123", "12 4"])
def test_a_malformed_last_four_is_rejected(src, out, bad):
    assert _run(src, out, _env(ROBINHOOD_AGENTIC_ACCOUNT_LAST4=bad)) == 2
    assert not out.exists() or not list(out.iterdir())


@pytest.mark.parametrize("bad", ["not-an-email", "a b@c.test", "@c.test", "a@", "a@@c.test", "a@c.test\nBcc: x@y.test"])
def test_a_malformed_email_is_rejected(src, out, bad):
    assert _run(src, out, _env(DCA_ALERT_EMAIL_ADDRESS=bad)) == 2
    assert not out.exists() or not list(out.iterdir())


def test_a_short_or_non_numeric_account_number_is_rejected(src, out):
    assert _run(src, out, _env(ROBINHOOD_AGENTIC_ACCOUNT_NUMBER="12")) == 2
    assert _run(src, out, _env(ROBINHOOD_AGENTIC_ACCOUNT_NUMBER="abcdefgh")) == 2
    assert not out.exists() or not list(out.iterdir())


def test_an_unknown_leftover_placeholder_fails_closed(tmp_path, out):
    src = _write_prompts(tmp_path / "src2")
    with open(src / "0935-backup.txt", "a", encoding="utf-8") as handle:
        handle.write("Also ${SOME_NEW_SETTING} is required.\n")
    assert _run(src, out, _env()) == 2
    assert not out.exists() or not list(out.iterdir())


def test_a_prompt_without_a_task_header_fails_closed(tmp_path, out):
    src = _write_prompts(tmp_path / "src3")
    (src / "0935-backup.txt").write_text(TEMPLATE.replace("Task aaaa1111.", "No header here."), encoding="utf-8")
    assert _run(src, out, _env()) == 2
    assert not out.exists() or not list(out.iterdir())


def test_one_bad_prompt_means_none_are_written(tmp_path, out):
    src = _write_prompts(tmp_path / "src4")
    (src / "1030-backup.txt").write_text("Task bbbb2222.\nno placeholders at all\n", encoding="utf-8")
    assert _run(src, out, _env()) == 2
    assert not out.exists() or not list(out.iterdir())


def test_an_empty_source_directory_is_an_error(tmp_path, out):
    empty = tmp_path / "empty"
    empty.mkdir()
    assert _run(empty, out, _env()) == 2


# -- where the private copies may go ---------------------------------------------------------------------------------


def test_an_output_directory_inside_the_repo_checkout_is_refused(src, capsys):
    inside = ROOT / "filled-prompts-should-not-exist"
    assert _run(src, inside, _env()) == 2
    assert not inside.exists()
    assert "repo" in capsys.readouterr().err.lower()


def test_the_output_directory_cannot_be_the_source_directory(src):
    assert _run(src, src, _env()) == 2


@pytest.mark.skipif(os.name != "posix", reason="POSIX permissions")
def test_the_private_copies_are_owner_only(src, out):
    assert _run(src, out, _env()) == 0
    for path in out.glob("*-backup.txt"):
        assert stat.S_IMODE(path.stat().st_mode) == 0o600, path.name


def test_the_values_are_never_printed(src, out, capsys):
    assert _run(src, out, _env()) == 0
    printed = capsys.readouterr()
    assert LAST4 not in printed.out + printed.err
    assert EMAIL not in printed.out + printed.err
    assert ACCOUNT not in printed.out + printed.err


def test_a_failure_message_does_not_leak_a_rejected_value(src, out, capsys):
    secret = "x" * 3 + "@" + "y" * 3
    assert _run(src, out, _env(DCA_ALERT_EMAIL_ADDRESS=secret + " bad")) == 2
    printed = capsys.readouterr()
    assert secret not in printed.out + printed.err


# -- the real prompts --------------------------------------------------------------------------------------------------


def test_the_committed_prompts_fill_cleanly(out):
    assert _run(REAL_PROMPTS, out, _env()) == 0
    files = sorted(out.glob("*-backup.txt"))
    assert len(files) == 6
    for path in files:
        text = path.read_text(encoding="utf-8")
        assert "${" not in text, path.name
        assert LAST4 in text and EMAIL in text
        # the planner stamp the prompt checks is untouched by filling
        stamp = [line for line in (REAL_PROMPTS / path.name).read_text(encoding="utf-8").split() if len(line) == 64]
        assert all(s in text for s in stamp)


def test_the_committed_prompts_hold_no_private_values():
    for path in REAL_PROMPTS.glob("*-backup.txt"):
        text = path.read_text(encoding="utf-8")
        assert "@" not in text.replace("${DCA_ALERT_EMAIL_ADDRESS}", ""), path.name


# -- 2026-11-09: PPLX_PROJECT_FILES_DIR, the folder that holds backup_planner.pyz ------------------------------------------
# The run searched the whole disk (`find /`) for the planner file; the prompt now names the folder, filled from a
# local value so the Perplexity project id never lands in the repo.


def test_the_project_files_folder_is_filled_into_every_committed_prompt(out):
    assert _run(REAL_PROMPTS, out, _env()) == 0
    for path in sorted(out.glob("*-backup.txt")):
        text = path.read_text(encoding="utf-8")
        assert f"cd {PROJECT_DIR}" in text, path.name
        assert "${PPLX_PROJECT_FILES_DIR}" not in text, path.name


def test_a_prompt_that_names_the_folder_fails_closed_when_no_folder_is_given(out, capsys):
    assert _run(REAL_PROMPTS, out, _env(PPLX_PROJECT_FILES_DIR=None)) == 2
    assert not out.exists() or not list(out.iterdir())
    assert "PPLX_PROJECT_FILES_DIR" in capsys.readouterr().err


@pytest.mark.parametrize("bad", ["files", "./files", "/work/../etc", "/work/my files", "/work/$(x)", ""])
def test_a_malformed_project_files_folder_is_rejected_and_not_echoed(out, capsys, bad):
    assert _run(REAL_PROMPTS, out, _env(PPLX_PROJECT_FILES_DIR=bad)) == 2
    assert not out.exists() or not list(out.iterdir())
    printed = capsys.readouterr()
    if bad:
        assert bad not in printed.out + printed.err


def test_a_prompt_without_the_folder_placeholder_still_fills_when_a_folder_is_given(src, out):
    assert _run(src, out, _env()) == 0  # the old three-placeholder prompts keep working
