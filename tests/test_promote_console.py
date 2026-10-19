"""Promotion gates: merged, versioned, logged, digest-clean, and human-approved."""

import io
import subprocess

import pytest

from dca_console_release import digest_files
from scripts import promote_console as pc


def run(repo, *args):
    subprocess.run(["git", "-C", str(repo), *args], check=True, capture_output=True)


def make_repo(tmp_path, version="1.1.0", changelog_top=None, tamper=False):
    repo = tmp_path / "repo"
    repo.mkdir()
    run(repo, "init", "-q", "-b", "main")
    run(repo, "config", "user.email", "t@t")
    run(repo, "config", "user.name", "t")
    (repo / "console").mkdir()
    (repo / "console" / "app.js").write_text("a")
    digest = digest_files({"app.js": b"a"})
    (repo / "dca_console_release.py").write_text(f'CONSOLE_VERSION = "{version}"\nASSET_DIGEST = "{digest}"\n')
    (repo / "docs").mkdir()
    (repo / "docs" / "console_changelog.md").write_text(f"## {changelog_top or version}\n- x\n")
    if tamper:
        (repo / "console" / "app.js").write_text("b")
    run(repo, "add", "-A")
    run(repo, "commit", "-qm", "c")
    return repo


def test_all_gates_pass(tmp_path):
    repo = make_repo(tmp_path)
    assert pc.check_gates(repo, "HEAD", "main", "1.0.0")["version"] == "1.1.0"


@pytest.mark.parametrize("kwargs,stable,message", [
    ({"tamper": True}, "1.0.0", "ASSET_DIGEST"),
    ({"changelog_top": "1.0.0"}, "1.0.0", "changelog"),
    ({}, "1.1.0", "not newer"),
])
def test_gate_failures(tmp_path, kwargs, stable, message):
    repo = make_repo(tmp_path, **kwargs)
    with pytest.raises(pc.PromotionError, match=message):
        pc.check_gates(repo, "HEAD", "main", stable)


def test_unmerged_ref_is_refused(tmp_path):
    repo = make_repo(tmp_path)
    run(repo, "checkout", "-q", "-b", "sandbox")
    (repo / "x").write_text("1")
    run(repo, "add", "-A")
    run(repo, "commit", "-qm", "wip")
    with pytest.raises(pc.PromotionError, match="not merged"):
        pc.check_gates(repo, "sandbox", "main", "1.0.0")


def test_approval_requires_tty_and_exact_phrase():
    class Pipe(io.StringIO):
        def isatty(self):
            return False

    class Tty(io.StringIO):
        def isatty(self):
            return True

    with pytest.raises(pc.PromotionError, match="interactive"):
        pc.confirm("1.1.0", stdin=Pipe())
    assert pc.confirm("1.1.0", stdin=Tty(), prompt=lambda _: "promote 1.1.0")
    assert not pc.confirm("1.1.0", stdin=Tty(), prompt=lambda _: "yes")
