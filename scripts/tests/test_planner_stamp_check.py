"""``scripts.planner_stamp_check``: tell the operator when the planner they pasted into Perplexity is stale.

Why (2026-10-21): the planner's ``content_sha256`` is demanded by step 0 of every backup prompt, and it moves
whenever any file inside the planner changes (it moved from ``2d4cadaf`` to ``c1d28d01`` to ``00ecf680`` to
``d274f82e`` in one afternoon, for reasons unrelated to the change being shipped). A stale planner costs a missed
slot, and nothing told anyone. A cloud routine cannot do this check (no repo or GitHub credentials in a scheduled
session, confirmed 2026-10-21), so it runs on the operator's Mac, next to the checkout and the ``backup_planner.pyz``
that was actually attached.

Contract pinned here, before any of it exists:

* The baseline is the stamp INSIDE the operator's own ``backup_planner.pyz``; nothing is stored anywhere else.
* The fresh stamp comes from ``origin/main`` WITHOUT touching the checkout's working tree.
* 0 = unchanged (silent), 1 = stale (notify), 2 = could not check (notify: a silent check is the worst failure).
* Output is one JSON document that never carries a path outside the repo or any private value.
"""

import json
import shutil
import subprocess
import sys
import zipfile
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from scripts import build_backup_planner, planner_stamp_check  # noqa: E402

REPO = Path(__file__).resolve().parents[2]
SAME = "d274f82e" + "0" * 56
NEWER = "ab12cd34" + "1" * 56


@pytest.fixture
def notified(monkeypatch):
    calls = []
    monkeypatch.setattr(planner_stamp_check, "notify_macos", lambda title, message: calls.append((title, message)))
    return calls


@pytest.fixture
def pasted(tmp_path):
    """A real planner built from this checkout, standing in for the operator's attached file."""
    out = tmp_path / "backup_planner.pyz"
    info = build_backup_planner.build(out, repo_root=REPO, source_commit="0" * 40)
    return out, info["content_sha256"]


def test_pasted_stamp_reads_the_content_hash_stored_inside_the_planner(pasted):
    path, expected = pasted
    assert planner_stamp_check.pasted_stamp(path) == expected


def test_a_missing_or_corrupt_planner_raises_a_clear_error_not_a_traceback(tmp_path):
    with pytest.raises(planner_stamp_check.StampError):
        planner_stamp_check.pasted_stamp(tmp_path / "absent.pyz")
    bad = tmp_path / "bad.pyz"
    bad.write_bytes(b"not a zip")
    with pytest.raises(planner_stamp_check.StampError):
        planner_stamp_check.pasted_stamp(bad)


def test_unchanged_stamp_exits_zero_prints_ok_and_stays_silent(pasted, monkeypatch, notified, capsys):
    path, stamp = pasted
    monkeypatch.setattr(planner_stamp_check, "fresh_stamp", lambda repo, ref: (stamp, "a" * 40))

    code = planner_stamp_check.main(["--planner", str(path), "--notify"])

    assert code == 0
    assert notified == []  # nothing to say
    report = json.loads(capsys.readouterr().out)
    assert report["status"] == "UP_TO_DATE"
    assert report["pasted"] == stamp and report["latest"] == stamp


def test_a_changed_stamp_exits_one_and_notifies_with_both_short_hashes(pasted, monkeypatch, notified, capsys):
    path, _ = pasted
    monkeypatch.setattr(planner_stamp_check, "fresh_stamp", lambda repo, ref: (NEWER, "b" * 40))

    code = planner_stamp_check.main(["--planner", str(path), "--notify"])

    assert code == 1
    assert len(notified) == 1
    title, message = notified[0]
    assert "planner" in title.lower()
    assert NEWER[:8] in message  # the new stamp, so the operator can compare after rebuilding
    report = json.loads(capsys.readouterr().out)
    assert report["status"] == "STALE"
    assert report["latest_commit"] == "b" * 40


def test_without_notify_a_stale_planner_still_exits_one_but_raises_nothing(pasted, monkeypatch, notified):
    path, _ = pasted
    monkeypatch.setattr(planner_stamp_check, "fresh_stamp", lambda repo, ref: (NEWER, "b" * 40))

    assert planner_stamp_check.main(["--planner", str(path)]) == 1
    assert notified == []


def test_a_check_that_cannot_run_exits_two_and_notifies(tmp_path, monkeypatch, notified, capsys):
    code = planner_stamp_check.main(["--planner", str(tmp_path / "absent.pyz"), "--notify"])

    assert code == 2
    assert len(notified) == 1  # a silent failed check would look exactly like "all good"
    assert json.loads(capsys.readouterr().out)["status"] == "ERROR"


def test_a_git_failure_is_an_error_not_a_stale_verdict(pasted, monkeypatch, notified):
    path, _ = pasted

    def boom(repo, ref):
        raise planner_stamp_check.StampError("git fetch failed")

    monkeypatch.setattr(planner_stamp_check, "fresh_stamp", boom)

    assert planner_stamp_check.main(["--planner", str(path), "--notify"]) == 2
    assert len(notified) == 1


def test_the_fresh_stamp_is_built_from_the_ref_without_touching_the_working_tree(tmp_path):
    """Real git, tiny repo: the working tree keeps its uncommitted edit and its checked-out branch."""
    import subprocess

    repo = tmp_path / "repo"
    subprocess.run(["git", "init", "-q", "-b", "main", str(repo)], check=True)
    for name in ("user.email", "user.name"):
        subprocess.run(["git", "-C", str(repo), "config", name, "t@example.invalid"], check=True)
    marker = repo / "marker.txt"
    marker.write_text("committed\n")
    subprocess.run(["git", "-C", str(repo), "add", "."], check=True)
    subprocess.run(["git", "-C", str(repo), "commit", "-q", "-m", "c1"], check=True)
    marker.write_text("uncommitted edit\n")  # must survive the check

    archived = planner_stamp_check.export_ref(repo, "HEAD", tmp_path / "export")

    assert (archived / "marker.txt").read_text() == "committed\n"  # the ref's content, not the edit
    assert marker.read_text() == "uncommitted edit\n"  # working tree untouched


def test_report_never_contains_a_home_directory_or_account_shaped_value(pasted, monkeypatch, capsys):
    path, stamp = pasted
    monkeypatch.setattr(planner_stamp_check, "fresh_stamp", lambda repo, ref: (stamp, "a" * 40))
    planner_stamp_check.main(["--planner", str(path)])
    out = capsys.readouterr().out
    assert str(Path.home()) not in out
    assert "ROBINHOOD" not in out


@pytest.mark.parametrize("metadata", [
    "null", "[]", '""', "{}", '{"content_sha256":true}', '{"content_sha256":null}',
    '{"content_sha256":"short"}', '{"content_sha256":"' + SAME.upper() + '"}',
    '{"content_sha256":"' + SAME + '","content_sha256":"' + NEWER + '"}',
])
def test_invalid_or_ambiguous_metadata_is_unavailable(tmp_path, metadata):
    planner = tmp_path / "planner.pyz"
    with zipfile.ZipFile(planner, "w") as archive:
        archive.writestr("BUILD_INFO.json", metadata)
    with pytest.raises(planner_stamp_check.StampError):
        planner_stamp_check.pasted_stamp(planner)


def test_readable_stamp_in_an_incomplete_planner_is_unavailable(pasted, tmp_path):
    path, _ = pasted
    incomplete = tmp_path / "incomplete.pyz"
    with zipfile.ZipFile(path) as source, zipfile.ZipFile(incomplete, "w") as destination:
        for name in source.namelist():
            if name != "__main__.py":
                destination.writestr(name, source.read(name))
    with pytest.raises(planner_stamp_check.StampError):
        planner_stamp_check.pasted_stamp(incomplete)


def test_git_errors_do_not_leak_private_details(pasted, monkeypatch, capsys, notified):
    path, _ = pasted

    def fail(*args, **kwargs):
        raise subprocess.CalledProcessError(1, ["git"], stderr=b"private remote credentials")

    monkeypatch.setattr(planner_stamp_check.subprocess, "run", fail)
    assert planner_stamp_check.main(["--planner", str(path), "--notify"]) == 2
    output = capsys.readouterr()
    assert "private" not in output.out + output.err
    assert len(notified) == 1


def test_notification_failure_is_reported_without_a_traceback(pasted, monkeypatch, capsys):
    path, _ = pasted
    monkeypatch.setattr(planner_stamp_check, "fresh_stamp", lambda repo, ref: (NEWER, "b" * 40))

    def fail(*args):
        raise OSError("private local path")

    monkeypatch.setattr(planner_stamp_check, "notify_macos", fail)
    assert planner_stamp_check.main(["--planner", str(path), "--notify"]) == 2
    output = capsys.readouterr()
    assert json.loads(output.out)["status"] == "ERROR"
    assert "private" not in output.out + output.err


def test_fresh_stamp_fetches_main_and_uses_its_builder_without_changing_checkout(tmp_path):
    repo = tmp_path / "repo"
    subprocess.run(["git", "init", "-q", "-b", "main", str(repo)], check=True)
    for name in ("user.email", "user.name"):
        subprocess.run(["git", "-C", str(repo), "config", name, "t@example.invalid"], check=True)
    for name in [*build_backup_planner.collect_files(REPO), "scripts/build_backup_planner.py"]:
        destination = repo / name
        destination.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(REPO / name, destination)
    # A changed bootstrap must change the fresh stamp even if the checker itself is not updated.
    builder = repo / "scripts/build_backup_planner.py"
    builder.write_text(builder.read_text().replace("MIN_PYTHON = %(min_python)r",
                                                  "# Fresh bootstrap\nMIN_PYTHON = %(min_python)r"))
    subprocess.run(["git", "-C", str(repo), "add", "."], check=True)
    subprocess.run(["git", "-C", str(repo), "commit", "-q", "-m", "planner"], check=True)
    commit = subprocess.check_output(["git", "-C", str(repo), "rev-parse", "HEAD"], text=True).strip()
    subprocess.run(["git", "-C", str(repo), "remote", "add", "origin", str(repo)], check=True)
    subprocess.run(["git", "-C", str(repo), "switch", "-q", "-c", "work"], check=True)
    source = repo / "dca_risk.py"
    source.write_text("uncommitted edit\n")
    before = subprocess.check_output(["git", "-C", str(repo), "status", "--porcelain"])

    stamp, latest_commit = planner_stamp_check.fresh_stamp(repo, "origin/main")

    expected = tmp_path / "expected.pyz"
    exported = planner_stamp_check.export_ref(repo, commit, tmp_path / "expected-source")
    subprocess.run(
        [sys.executable, str(exported / "scripts/build_backup_planner.py"),
         "--out", str(expected), "--source-commit", commit],
        check=True, capture_output=True,
    )
    assert latest_commit == commit
    assert stamp == planner_stamp_check.pasted_stamp(expected)
    assert stamp != build_backup_planner.content_sha256(REPO)
    assert len(stamp) == 64
    assert source.read_text() == "uncommitted edit\n"
    assert subprocess.check_output(["git", "-C", str(repo), "status", "--porcelain"]) == before
    assert subprocess.check_output(["git", "-C", str(repo), "branch", "--show-current"], text=True).strip() == "work"


def test_fresh_stamp_fetch_failure_and_invalid_ref_fail_closed(tmp_path):
    repo = tmp_path / "repo"
    subprocess.run(["git", "init", "-q", str(repo)], check=True)
    with pytest.raises(planner_stamp_check.StampError):
        planner_stamp_check.fresh_stamp(repo, "origin/main")
    with pytest.raises(planner_stamp_check.StampError):
        planner_stamp_check.fresh_stamp(repo, "HEAD")


def test_export_rejects_traversal_and_skips_symlinks(tmp_path, monkeypatch):
    def archive_with_path(repo, *args, stdout):
        with zipfile.ZipFile(stdout, "w") as archive:
            archive.writestr("../outside", "unsafe")

    monkeypatch.setattr(planner_stamp_check, "_git", archive_with_path)
    with pytest.raises(planner_stamp_check.StampError):
        planner_stamp_check.export_ref(tmp_path, "HEAD", tmp_path / "unsafe")
    assert not (tmp_path / "outside").exists()

    def archive_with_symlink(repo, *args, stdout):
        with zipfile.ZipFile(stdout, "w") as archive:
            link = zipfile.ZipInfo("link")
            link.external_attr = 0o120777 << 16
            archive.writestr(link, "../outside")
            archive.writestr("marker", "safe")

    monkeypatch.setattr(planner_stamp_check, "_git", archive_with_symlink)
    exported = planner_stamp_check.export_ref(tmp_path, "HEAD", tmp_path / "safe")
    assert not (exported / "link").exists()
    assert (exported / "marker").read_text() == "safe"
