"""Release gate: console UI assets cannot change without a version bump + changelog entry."""

import sys

import pytest

import dca_bootstrap
import dca_console
from dca_console_release import (
    ASSET_DIGEST,
    CHANGELOG_PATH,
    CONSOLE_VERSION,
    asset_digest,
    changelog_versions,
    release_info,
)


def test_assets_unchanged_without_version_bump():
    assert asset_digest() == ASSET_DIGEST, (
        "console/ changed. Release process: bump CONSOLE_VERSION, add a '## <version>' entry to "
        "docs/console_changelog.md, then set ASSET_DIGEST to `python -m dca_console_release`."
    )


def test_changelog_has_current_version_first():
    versions = changelog_versions()
    assert versions and versions[0] == CONSOLE_VERSION, CHANGELOG_PATH


def test_digest_tracks_content(tmp_path):
    (tmp_path / "a.js").write_text("x")
    first = asset_digest(tmp_path)
    (tmp_path / "a.js").write_text("y")
    assert asset_digest(tmp_path) != first


def test_ui_shows_version_and_sandbox_badge():
    html = (dca_console.REPO / "console" / "index.html").read_text(encoding="utf-8")
    js = (dca_console.REPO / "console" / "app.js").read_text(encoding="utf-8")
    assert 'id="console-version"' in html and 'id="channel-badge"' in html
    assert "SANDBOX" in js and "release.version" in js


def _app(tmp_path, monkeypatch, **kw):
    monkeypatch.setattr(dca_console, "python_packages", kw.get("probe", lambda _: {}))
    settings = dca_console.Settings("demo", tmp_path / "r", tmp_path / "o", None, sys.executable, sys.executable,
                                    channel=kw.get("channel", "stable"))
    return dca_console.ConsoleApp(settings)


def test_release_info_in_snapshot_and_sandbox_channel(tmp_path, monkeypatch):
    assert release_info("sandbox") == {"version": CONSOLE_VERSION, "channel": "sandbox", "assets": ASSET_DIGEST}
    app = _app(tmp_path, monkeypatch, channel="sandbox")
    try:
        assert app.settings.channel == "sandbox"
    finally:
        app.jobs.close()


def test_packages_are_reprobed_not_frozen(tmp_path, monkeypatch):
    state = {"numpy": False, "calls": 0}

    def probe(_):
        state["calls"] += 1
        return {"numpy": state["numpy"]}

    app = _app(tmp_path, monkeypatch, probe=probe)
    try:
        assert app.packages["numpy"] is False
        state["numpy"] = True  # operator installs the package while the console runs
        calls = state["calls"]
        assert app.packages["numpy"] is False and state["calls"] == calls  # within TTL: cached
        app.reprobe_packages()  # what the Environment page does on every load
        assert app.packages["numpy"] is True
    finally:
        app.jobs.close()


def test_sandbox_flag_is_demo_only_and_rejects_real_roots(tmp_path, capsys):
    with pytest.raises(SystemExit):
        dca_console.main(["--sandbox", "--root", str(tmp_path), "--check"])
    assert "synthetic" in capsys.readouterr().err


def test_linked_worktree_resolves_main_venv(tmp_path):
    main = tmp_path / "main"
    (main / ".git" / "worktrees" / "wt").mkdir(parents=True)
    wt = tmp_path / "wt"
    wt.mkdir()
    (wt / ".git").write_text(f"gitdir: {main}/.git/worktrees/wt\n")
    assert dca_bootstrap.linked_worktree_main_checkout(wt) == main
    assert dca_bootstrap.resolve_venv_dir(wt) == main / ".venv"
    assert dca_bootstrap.linked_worktree_main_checkout(main) is None  # .git is a directory


def test_ensure_venv_refuses_to_create_when_told(tmp_path):
    with pytest.raises(SystemExit, match="Refusing to create"):
        dca_bootstrap.ensure_venv(venv_dir=tmp_path / ".venv", create_if_missing=False)
