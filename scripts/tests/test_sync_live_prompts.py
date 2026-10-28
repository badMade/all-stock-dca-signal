"""Tests for scripts/sync_live_prompts.py (written BEFORE the script exists; TDD red step).

Motivating incident (2026-10-26): ``check_live_prompt_drift`` reported 7 of 7 live Cowork task bodies behind their
repo mirrors. The live bodies lacked, among other things, the 2026-10-22 log-and-continue history rule, the #1839 ``uv``
error capture, and the placement check that does not compare the broker's ``ref_id`` (a prompt that does would halt
after its first order, because the broker never echoes the id). The README says a repaste must not be a blind copy:
a mirror drops the live file's YAML frontmatter and carries ``${ROBINHOOD_AGENTIC_ACCOUNT_NUMBER}`` where the live
body carries the account literal. A hand edit of eight paragraphs in seven files is where mistakes would happen, so the
sync is a script, and these tests pin what it must do and what it must refuse:

* the default is a dry run that writes nothing;
* ``--write`` backs the live file up first, then replaces ONLY the body: frontmatter kept byte for byte, the mirror's
  ``#`` preamble left out, the account placeholder filled with the live account;
* after a write the drift checker reads 0 drifted slots, and a second write changes nothing;
* it is all or nothing: one slot it cannot handle means no slot is written;
* it never repoints a task to another account, never prints the account, and never touches other files.

The account value below is the suite's synthetic one (``conftest.py`` assigns it unconditionally).
"""

from __future__ import annotations

from pathlib import Path

import pytest

# Imported before anything sets $DCA_CONFIG_DIR, as in test_check_live_prompt_drift.py.
import dca_config  # noqa: F401
from scripts import check_live_prompt_drift as drift
from scripts import sync_live_prompts as sync

ACCOUNT = "999000111"
OTHER_ACCOUNT = "123456789"
PLACEHOLDER = "${ROBINHOOD_AGENTIC_ACCOUNT_NUMBER}"

MIRROR_BODY = (
    "GATE 5 - BUY LIST\n"
    "NO HAND-REIMPLEMENTED SIGNALS: Never recompute signals, sizes or candidates by hand.\n"
    "HISTORY COMPLETENESS IS LOG-AND-CONTINUE (operator, 2026-10-22): log it and continue.\n"
    f"Run python3 -m scripts.same_day_top_up_gate --account {PLACEHOLDER} --trading-day <YYYY-MM-DD>\n"
    'Save the order history as {"account_number": ..., "trading_day": ..., "pages": [...]}.\n'
    "VALIDATE THE PLACEMENT RESPONSE: the ref_id is NOT compared; scripts.validate_native_placement controls.\n"
    "GATE 9 - REPORT\n"
    "Run python3 -m scripts.finalize_run_evidence --slot 1530 on every outcome.\n"
)
OLD_BODY = (
    "GATE 5 - BUY LIST\n"
    f"Run python3 -m scripts.same_day_top_up_gate --account {ACCOUNT} --trading-day <YYYY-MM-DD>\n"
    "VALIDATE THE PLACEMENT RESPONSE: halt unless the returned ref_id matches.\n"
    "GATE 9 - REPORT\n"
    "Run python3 -m scripts.finalize_run_evidence --slot 1530 on every outcome.\n"
)
FRONTMATTER = "---\nname: 330-final-buying-run\ndescription: 3:30 PM final buying run\n---\n"
PREAMBLE = "# MIRROR of the LIVE scheduled task `{task}` ({slot} slot)\n# Read-only copy.\n\n"


def _setup(tmp_path: Path, *, live_body: str = OLD_BODY, frontmatter: str = FRONTMATTER):
    """Seven mirrors (one per roster slot) and seven live task folders, each with its own frontmatter."""
    mirror_dir = tmp_path / "mirror"
    scheduled = tmp_path / "Scheduled"
    mirror_dir.mkdir()
    for slot in drift.EXPECTED_SLOTS:
        task = f"task-{slot.lower()}"
        (mirror_dir / f"{slot}-{task}.txt").write_text(
            PREAMBLE.format(task=task, slot=slot) + MIRROR_BODY, encoding="utf-8")
        (scheduled / task).mkdir(parents=True)
        (scheduled / task / "SKILL.md").write_text(
            frontmatter.replace("330-final-buying-run", task) + live_body, encoding="utf-8")
    return mirror_dir, scheduled


def _live_path(scheduled: Path, slot: str = "1530ET") -> Path:
    return scheduled / f"task-{slot.lower()}" / "SKILL.md"


def _run(mirror_dir: Path, scheduled: Path, *extra: str) -> int:
    return sync.main(["--mirror-dir", str(mirror_dir), "--scheduled-dir", str(scheduled), *extra])


def _snapshot(scheduled: Path) -> dict:
    return {str(p.relative_to(scheduled)): p.read_bytes() for p in sorted(scheduled.rglob("*")) if p.is_file()}


@pytest.fixture(autouse=True)
def synthetic_account(monkeypatch):
    monkeypatch.setenv("ROBINHOOD_AGENTIC_ACCOUNT_NUMBER", ACCOUNT)


def test_the_default_is_a_dry_run_that_writes_nothing(tmp_path, capsys):
    mirror_dir, scheduled = _setup(tmp_path)
    before = _snapshot(scheduled)

    assert _run(mirror_dir, scheduled) == 0

    assert _snapshot(scheduled) == before
    out = capsys.readouterr().out
    assert "7 of 7" in out and "dry run" in out.lower()


def test_write_replaces_the_body_and_keeps_the_frontmatter_byte_for_byte(tmp_path):
    mirror_dir, scheduled = _setup(tmp_path)
    frontmatter = FRONTMATTER.replace("330-final-buying-run", "task-1530et")

    assert _run(mirror_dir, scheduled, "--write") == 0

    new = _live_path(scheduled).read_text(encoding="utf-8")
    assert new.startswith(frontmatter)
    assert "HISTORY COMPLETENESS IS LOG-AND-CONTINUE" in new
    assert "halt unless the returned ref_id matches" not in new


def test_the_mirror_preamble_is_not_copied_into_the_live_body(tmp_path):
    mirror_dir, scheduled = _setup(tmp_path)

    _run(mirror_dir, scheduled, "--write")

    assert "MIRROR of the LIVE scheduled task" not in _live_path(scheduled).read_text(encoding="utf-8")


def test_the_account_placeholder_is_filled_with_the_live_account(tmp_path):
    mirror_dir, scheduled = _setup(tmp_path)

    _run(mirror_dir, scheduled, "--write")

    new = _live_path(scheduled).read_text(encoding="utf-8")
    assert PLACEHOLDER not in new
    assert f"--account {ACCOUNT} --trading-day" in new


def test_after_a_write_the_drift_checker_reads_zero_drifted_slots(tmp_path):
    mirror_dir, scheduled = _setup(tmp_path)
    _run(mirror_dir, scheduled, "--write")

    report = drift.check(mirror_dir=mirror_dir, scheduled_dir=scheduled, window_end_date="2027-11-05",
                         account_number=ACCOUNT)

    assert report["drifted_slots"] == 0
    assert all(slot["status"] == "match" for slot in report["slots"])


def test_every_live_file_is_backed_up_with_its_exact_old_bytes_before_it_is_replaced(tmp_path):
    mirror_dir, scheduled = _setup(tmp_path)
    old = {slot: _live_path(scheduled, slot).read_bytes() for slot in drift.EXPECTED_SLOTS}

    _run(mirror_dir, scheduled, "--write")

    for slot in drift.EXPECTED_SLOTS:
        backups = sorted(_live_path(scheduled, slot).parent.glob("SKILL.md.bak-*"))
        assert len(backups) == 1, slot
        assert backups[0].read_bytes() == old[slot]


def test_a_second_write_changes_nothing_and_makes_no_new_backup(tmp_path):
    mirror_dir, scheduled = _setup(tmp_path)
    _run(mirror_dir, scheduled, "--write")
    after_first = _snapshot(scheduled)

    assert _run(mirror_dir, scheduled, "--write") == 0

    assert _snapshot(scheduled) == after_first


def test_the_live_file_keeps_its_permissions_and_the_backup_is_no_more_open_than_it(tmp_path):
    # Writing through a temp file and renaming it over SKILL.md would reset an owner-only (0600) live task to the
    # umask default (usually 0644), quietly widening who can read a file that carries the account number.
    mirror_dir, scheduled = _setup(tmp_path)
    live = _live_path(scheduled)
    live.chmod(0o600)

    _run(mirror_dir, scheduled, "--write")

    assert live.stat().st_mode & 0o777 == 0o600
    [backup] = live.parent.glob("SKILL.md.bak-*")
    assert backup.stat().st_mode & 0o777 == 0o600


def test_a_live_body_that_already_matches_is_not_rewritten_or_backed_up(tmp_path):
    mirror_dir, scheduled = _setup(tmp_path)
    one = _live_path(scheduled, "1030ET")
    one.write_text(FRONTMATTER.replace("330-final-buying-run", "task-1030et")
                   + MIRROR_BODY.replace(PLACEHOLDER, ACCOUNT), encoding="utf-8")
    before = one.read_bytes()

    _run(mirror_dir, scheduled, "--write")

    assert one.read_bytes() == before
    assert not list(one.parent.glob("SKILL.md.bak-*"))
    assert len(list(_live_path(scheduled, "0935ET").parent.glob("SKILL.md.bak-*"))) == 1


def test_a_live_file_with_no_frontmatter_gets_none_invented(tmp_path):
    mirror_dir, scheduled = _setup(tmp_path, frontmatter="")

    _run(mirror_dir, scheduled, "--write")

    assert not _live_path(scheduled).read_text(encoding="utf-8").startswith("---")


def test_one_slot_it_cannot_handle_means_no_slot_is_written(tmp_path, capsys):
    mirror_dir, scheduled = _setup(tmp_path)
    _live_path(scheduled, "1330ET").unlink()  # a live task folder with no SKILL.md
    before = _snapshot(scheduled)

    assert _run(mirror_dir, scheduled, "--write") == 2

    assert _snapshot(scheduled) == before
    assert "task-1330et" in capsys.readouterr().err


def test_a_staging_failure_leaves_live_files_untouched_and_cleans_staged_files(tmp_path, monkeypatch, capsys):
    mirror_dir, scheduled = _setup(tmp_path)
    old_live = {slot: _live_path(scheduled, slot).read_bytes() for slot in drift.EXPECTED_SLOTS}
    stage = sync._stage
    calls = 0

    def fail_second_stage(path, data):
        nonlocal calls
        calls += 1
        if calls == 2:
            raise OSError("simulated staging failure")
        return stage(path, data)

    monkeypatch.setattr(sync, "_stage", fail_second_stage)

    assert _run(mirror_dir, scheduled, "--write") == 2

    assert {slot: _live_path(scheduled, slot).read_bytes() for slot in drift.EXPECTED_SLOTS} == old_live
    assert not list(scheduled.rglob("SKILL.md.tmp"))
    captured = capsys.readouterr()
    assert "updated" not in captured.out
    assert "could not stage" in captured.err


def test_a_mid_batch_replacement_failure_restores_prior_files_and_returns_2(tmp_path, monkeypatch, capsys):
    mirror_dir, scheduled = _setup(tmp_path)
    old_live = {slot: _live_path(scheduled, slot).read_bytes() for slot in drift.EXPECTED_SLOTS}
    replace = sync._replace
    calls = 0

    def fail_second_replace(path, staged):
        nonlocal calls
        calls += 1
        if calls == 2:
            raise OSError("simulated replacement failure")
        replace(path, staged)

    monkeypatch.setattr(sync, "_replace", fail_second_replace)

    assert _run(mirror_dir, scheduled, "--write") == 2

    assert {slot: _live_path(scheduled, slot).read_bytes() for slot in drift.EXPECTED_SLOTS} == old_live
    captured = capsys.readouterr()
    assert "updated" not in captured.out
    assert "prior replacements were restored" in captured.err


def test_it_refuses_when_the_account_cannot_be_determined(tmp_path, monkeypatch, capsys):
    monkeypatch.delenv("ROBINHOOD_AGENTIC_ACCOUNT_NUMBER", raising=False)
    mirror_dir, scheduled = _setup(tmp_path, live_body=OLD_BODY.replace(ACCOUNT, "<none>"))
    before = _snapshot(scheduled)

    assert _run(mirror_dir, scheduled, "--write") == 2

    assert _snapshot(scheduled) == before
    assert "account" in capsys.readouterr().err.lower()


def test_it_uses_the_one_account_every_live_slot_carries_when_the_env_var_is_unset(tmp_path, monkeypatch):
    monkeypatch.delenv("ROBINHOOD_AGENTIC_ACCOUNT_NUMBER", raising=False)
    mirror_dir, scheduled = _setup(tmp_path)
    live = _live_path(scheduled, "1230ET")
    live.write_text(live.read_text(encoding="utf-8") + "Unrelated numeric value: 180000\n", encoding="utf-8")

    assert _run(mirror_dir, scheduled, "--write") == 0

    assert f"--account {ACCOUNT} --trading-day" in _live_path(scheduled).read_text(encoding="utf-8")


def test_it_never_repoints_a_live_task_to_a_different_account(tmp_path, capsys):
    mirror_dir, scheduled = _setup(tmp_path)
    wrong = _live_path(scheduled, "1230ET")
    wrong.write_text(wrong.read_text(encoding="utf-8").replace(ACCOUNT, OTHER_ACCOUNT), encoding="utf-8")
    before = _snapshot(scheduled)

    assert _run(mirror_dir, scheduled, "--write") == 2

    assert _snapshot(scheduled) == before
    assert "task-1230et" in capsys.readouterr().err


def test_it_refuses_a_live_slot_with_conflicting_account_references(tmp_path, capsys):
    mirror_dir, scheduled = _setup(tmp_path)
    live = _live_path(scheduled, "1230ET")
    live.write_text(
        live.read_text(encoding="utf-8")
        + f"Unrelated numeric value: 180000\nUse --account {OTHER_ACCOUNT} for this operation.\n",
        encoding="utf-8",
    )
    before = _snapshot(scheduled)

    assert _run(mirror_dir, scheduled, "--write") == 2

    assert _snapshot(scheduled) == before
    assert "task-1230et" in capsys.readouterr().err


def test_fallback_refuses_a_slot_with_conflicting_account_references(tmp_path, monkeypatch, capsys):
    monkeypatch.delenv("ROBINHOOD_AGENTIC_ACCOUNT_NUMBER", raising=False)
    mirror_dir, scheduled = _setup(tmp_path)
    live = _live_path(scheduled, "1230ET")
    live.write_text(
        live.read_text(encoding="utf-8")
        + f"Unrelated numeric value: 180000\nUse --account {OTHER_ACCOUNT} for this operation.\n",
        encoding="utf-8",
    )
    before = _snapshot(scheduled)

    assert _run(mirror_dir, scheduled, "--write") == 2

    assert _snapshot(scheduled) == before
    assert "account" in capsys.readouterr().err.lower()


def test_output_never_carries_an_account_shaped_number(tmp_path, capsys):
    mirror_dir, scheduled = _setup(tmp_path)

    _run(mirror_dir, scheduled)
    _run(mirror_dir, scheduled, "--write")

    captured = capsys.readouterr()
    assert ACCOUNT not in captured.out + captured.err
    assert OTHER_ACCOUNT not in captured.out + captured.err


def test_it_touches_nothing_in_a_task_folder_except_skill_md_and_its_backup(tmp_path):
    mirror_dir, scheduled = _setup(tmp_path)
    side = _live_path(scheduled).parent / "notes.txt"
    side.write_text("keep me", encoding="utf-8")

    _run(mirror_dir, scheduled, "--write")

    assert side.read_text(encoding="utf-8") == "keep me"
    names = {p.name for p in _live_path(scheduled).parent.iterdir()}
    assert names == {"SKILL.md", "notes.txt"} | {n for n in names if n.startswith("SKILL.md.bak-")}


def test_an_incomplete_mirror_set_exits_2_and_writes_nothing(tmp_path):
    mirror_dir, scheduled = _setup(tmp_path)
    next(mirror_dir.glob("1330ET-*.txt")).unlink()
    before = _snapshot(scheduled)

    assert _run(mirror_dir, scheduled, "--write") == 2

    assert _snapshot(scheduled) == before


def test_a_missing_scheduled_dir_exits_2(tmp_path):
    mirror_dir, _ = _setup(tmp_path)

    assert _run(mirror_dir, tmp_path / "nope", "--write") == 2


def test_the_committed_mirrors_round_trip_without_the_env_var(tmp_path, monkeypatch):
    """Account inference uses explicit references, not unrelated numbers in the real prompt bodies."""
    monkeypatch.delenv("ROBINHOOD_AGENTIC_ACCOUNT_NUMBER", raising=False)
    mirror_dir = drift.DEFAULT_MIRROR_DIR
    scheduled = tmp_path / "Scheduled"
    for mirror in sorted(mirror_dir.glob("*.txt")):
        if mirror.name.startswith("."):
            continue
        task = drift.task_id_for(mirror)
        body = "\n".join(drift._mirror_body(mirror.read_text(encoding="utf-8")))
        body = body.replace(PLACEHOLDER, ACCOUNT) + "\nstale body line\n"
        (scheduled / task).mkdir(parents=True)
        (scheduled / task / "SKILL.md").write_text(f"---\nname: {task}\n---\n{body}", encoding="utf-8")

    assert _run(mirror_dir, scheduled, "--write") == 0

    report = drift.check(
        mirror_dir=mirror_dir,
        scheduled_dir=scheduled,
        window_end_date="2027-11-05",
        account_number=drift.shared_live_account(mirror_dir, scheduled),
    )
    assert [s["status"] for s in report["slots"]] == ["match"] * 7


# --- Copilot review of PR #1949 (2026-10-26): four findings, each verified against the real mirrors first. ---

SHARED_NUMBER = "180000"  # every committed mirror carries this one (a timeout_ms value), so it is NOT an account


def _real_mirrors_with_stale_live(tmp_path: Path, live_tail: str = f"old body --account {ACCOUNT}\n") -> tuple[Path, Path]:
    mirror_dir = drift.DEFAULT_MIRROR_DIR
    scheduled = tmp_path / "Scheduled"
    for mirror in sorted(mirror_dir.glob("*.txt")):
        if mirror.name.startswith("."):
            continue
        task = drift.task_id_for(mirror)
        (scheduled / task).mkdir(parents=True)
        (scheduled / task / "SKILL.md").write_text(f"---\nname: {task}\n---\n{live_tail}", encoding="utf-8")
    return mirror_dir, scheduled


def test_a_live_body_carrying_the_account_and_a_second_account_is_refused(tmp_path, capsys):
    # The old check only asked "is the selected account somewhere in the file", so a body that also named another
    # account was accepted and the other reference silently disappeared under the replacement.
    mirror_dir, scheduled = _setup(tmp_path)
    mixed = _live_path(scheduled, "1130ET")
    mixed.write_text(mixed.read_text(encoding="utf-8") + f"also check account {OTHER_ACCOUNT}\n", encoding="utf-8")
    before = _snapshot(scheduled)

    assert _run(mirror_dir, scheduled, "--write") == 2

    assert _snapshot(scheduled) == before
    captured = capsys.readouterr()
    assert "task-1130et" in captured.err and OTHER_ACCOUNT not in captured.out + captured.err


def test_a_number_the_mirror_itself_contains_is_not_mistaken_for_a_second_account(tmp_path):
    mirror_dir, scheduled = _setup(tmp_path)
    for mirror in mirror_dir.glob("*.txt"):
        mirror.write_text(mirror.read_text(encoding="utf-8") + f"pass timeout_ms at most {SHARED_NUMBER}.\n",
                          encoding="utf-8")
    for slot in drift.EXPECTED_SLOTS:
        live = _live_path(scheduled, slot)
        live.write_text(live.read_text(encoding="utf-8") + f"pass timeout_ms at most {SHARED_NUMBER}.\n",
                        encoding="utf-8")

    assert _run(mirror_dir, scheduled, "--write") == 0


@pytest.mark.parametrize("body", ["", "\n\n   \n"], ids=["preamble-only", "blank-lines-only"])
def test_a_mirror_with_no_body_is_refused_rather_than_blanking_the_live_prompt(tmp_path, capsys, body):
    mirror_dir, scheduled = _setup(tmp_path)
    broken = next(mirror_dir.glob("1330ET-*.txt"))
    broken.write_text(PREAMBLE.format(task="task-1330et", slot="1330ET") + body, encoding="utf-8")
    before = _snapshot(scheduled)

    assert _run(mirror_dir, scheduled, "--write") == 2

    assert _snapshot(scheduled) == before
    assert "task-1330et" in capsys.readouterr().err


def test_the_fallback_account_survives_a_deployed_state_where_every_slot_also_carries_a_mirror_number(
    tmp_path, monkeypatch,
):
    # check_live_prompt_drift.shared_live_account() needs EXACTLY ONE number common to every live slot. Once the
    # real mirrors are deployed each slot carries the account AND 180000, so it returns None and a second run
    # without the environment variable exited 2. The fallback must look at account-bearing numbers only.
    monkeypatch.delenv("ROBINHOOD_AGENTIC_ACCOUNT_NUMBER", raising=False)
    mirror_dir, scheduled = _real_mirrors_with_stale_live(tmp_path)

    assert _run(mirror_dir, scheduled, "--write") == 0  # first run: stale bodies, account is the only number
    assert _run(mirror_dir, scheduled, "--write") == 0  # second run: deployed bodies now also carry 180000
    assert _run(mirror_dir, scheduled) == 0


def test_a_failure_while_replacing_leaves_every_live_file_as_it_was_and_says_so(tmp_path, monkeypatch, capsys):
    mirror_dir, scheduled = _setup(tmp_path)
    before = {slot: _live_path(scheduled, slot).read_bytes() for slot in drift.EXPECTED_SLOTS}
    real_replace = sync.os.replace
    calls = {"n": 0}

    def flaky(src, dst, *args, **kwargs):
        calls["n"] += 1
        if calls["n"] == 3:  # the third live file fails to land after two already did
            raise OSError("disk went away")
        return real_replace(src, dst, *args, **kwargs)

    monkeypatch.setattr(sync.os, "replace", flaky)

    assert _run(mirror_dir, scheduled, "--write") == 2

    monkeypatch.setattr(sync.os, "replace", real_replace)
    for slot in drift.EXPECTED_SLOTS:
        assert _live_path(scheduled, slot).read_bytes() == before[slot], slot
    captured = capsys.readouterr()
    assert "updated" not in captured.out  # no success line for a batch that did not complete
    assert "restored" in captured.err.lower()


def test_a_write_that_cannot_be_staged_touches_no_live_file(tmp_path):
    # A leftover SKILL.md.tmp that is a directory cannot be written. Every file must be staged before the first
    # live file is touched, so the failure is found while nothing has changed yet.
    mirror_dir, scheduled = _setup(tmp_path)
    (_live_path(scheduled, "1430ET").parent / "SKILL.md.tmp").mkdir()
    before = {slot: _live_path(scheduled, slot).read_bytes() for slot in drift.EXPECTED_SLOTS}

    assert _run(mirror_dir, scheduled, "--write") == 2

    for slot in drift.EXPECTED_SLOTS:
        assert _live_path(scheduled, slot).read_bytes() == before[slot], slot
