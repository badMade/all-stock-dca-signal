"""The docs catalog: a closed, listing-derived allowlist and honest registration status."""

import io
import json
import os
from pathlib import Path

import pytest
from test_dca_console import request, running_server

from dca_console import REPO, AuthFailureLog, ConsoleAccess
from dca_console_docs import MAX_DOC_BYTES, allowed_docs, docs_index, read_doc


def make_repo(tmp_path):
    (tmp_path / "docs").mkdir()
    (tmp_path / "claude" / "dca-executor-prompts" / "live-mirror").mkdir(parents=True)
    (tmp_path / "scripts").mkdir()
    (tmp_path / "config").mkdir()
    (tmp_path / "docs" / "strategy.md").write_text("# Strategy overview\n\nbody\n")
    (tmp_path / "docs" / "orphan.md").write_text("no heading here\n")
    (tmp_path / "docs" / "README.md").write_text("# Docs\n\n| [strategy.md](strategy.md) | Strategy |\n")
    (tmp_path / "docs" / "notes.txt").write_text("not a markdown page")
    (tmp_path / "CLAUDE.md").write_text("# CLAUDE\n\n| `docs/strategy.md` | Strategy |\n")
    (tmp_path / "README.md").write_text("# Root readme\n")
    (tmp_path / "scripts" / "README.md").write_text("# Scripts\n")
    (tmp_path / "claude" / "SECOND-EXECUTOR-FINDING.md").write_text("# Second executor\n")
    (tmp_path / "claude" / "dca-executor-prompts" / "live-mirror" / "1030ET-run.txt").write_text("prompt mirror")
    (tmp_path / "config" / "budgets.json").write_text("{}")
    (tmp_path / "AGENTS.md").write_text("# Agents\n")
    return tmp_path


def test_allowed_docs_lists_only_the_four_fixed_locations(tmp_path):
    repo = make_repo(tmp_path)
    catalog = allowed_docs(repo)
    assert set(catalog) == {
        "README.md", "CLAUDE.md", "AGENTS.md", "scripts/README.md",
        "docs/strategy.md", "docs/orphan.md", "docs/README.md",
        "claude/SECOND-EXECUTOR-FINDING.md",
        "claude/dca-executor-prompts/live-mirror/1030ET-run.txt",
    }
    assert catalog["claude/dca-executor-prompts/live-mirror/1030ET-run.txt"].mirror is True
    assert catalog["docs/strategy.md"].group == "docs" and catalog["README.md"].group == "root"
    assert "docs/notes.txt" not in catalog and "config/budgets.json" not in catalog


@pytest.mark.skipif(os.name != "posix", reason="symlink confinement needs POSIX symlinks")
def test_read_doc_refuses_unlisted_escaping_and_oversized_pages(tmp_path):
    repo = make_repo(tmp_path)
    outside = tmp_path.parent / f"{tmp_path.name}-outside.md"
    outside.write_text("# outside\n")
    (repo / "docs" / "link.md").symlink_to(outside)
    (repo / "docs" / "huge.md").write_bytes(b"x" * (MAX_DOC_BYTES + 1))
    assert read_doc(repo, "docs/strategy.md") == b"# Strategy overview\n\nbody\n"
    assert read_doc(repo, "config/budgets.json") is None
    assert read_doc(repo, "../outside.md") is None
    assert read_doc(repo, "docs/../config/budgets.json") is None
    assert read_doc(repo, "docs/link.md") is None
    assert read_doc(repo, "docs/huge.md") is None
    assert read_doc(repo, "") is None


def test_docs_index_reports_titles_groups_and_registration(tmp_path):
    repo = make_repo(tmp_path)
    index = docs_index(repo, {"docs/strategy.md"})
    rows = {row["path"]: row for row in index["docs"]}
    assert rows["docs/strategy.md"]["title"] == "Strategy overview"
    assert rows["docs/strategy.md"]["in_readme"] is True
    assert rows["docs/strategy.md"]["in_claude_md"] is True
    assert rows["docs/strategy.md"]["in_catalog"] is True
    assert rows["docs/orphan.md"]["title"] == "docs/orphan.md"
    assert rows["docs/orphan.md"]["in_readme"] is False and rows["docs/orphan.md"]["in_catalog"] is False
    assert rows["README.md"]["in_readme"] is None and rows["README.md"]["group"] == "root"
    assert rows["claude/dca-executor-prompts/live-mirror/1030ET-run.txt"]["mirror"] is True
    assert index["unregistered"] == ["docs/orphan.md"]
    assert index["warnings"] == ["1 docs page(s) are missing from the docs/README.md index."]
    assert rows["docs/README.md"]["in_readme"] is True
    assert str(tmp_path) not in json.dumps(index)


def test_real_repository_catalog_covers_the_docs_corpus_and_every_catalog_ref():
    from dca_console import module_catalog

    catalog = allowed_docs(REPO)
    # The registration triangle is closed for the real corpus: every docs/
    # page is linked from the docs index (GUI expansion Phase 5).
    unregistered = docs_index(REPO, set())["unregistered"]
    assert unregistered == [], f"add a row for each of these to the table in docs/README.md: {unregistered}"
    assert {name for name in catalog if name.startswith("docs/")} == {
        f"docs/{path.name}" for path in (REPO / "docs").glob("*.md")
    }, "every docs/*.md page needs a row in docs/README.md's table (add the missing page's row there)"
    refs = {row["docs"] for row in module_catalog("python3") if isinstance(row.get("docs"), str)}
    missing = sorted(ref for ref in refs if ref.endswith((".md", ".txt")) and ref not in catalog)
    assert missing == [], f"program directory doc_refs outside the docs catalog: {missing}"


def test_docs_routes_serve_only_catalog_pages_and_the_index(app):
    with running_server(app, ConsoleAccess(), AuthFailureLog(interval=0, stream=io.StringIO())) as server:
        status, headers, body = request(server, "/api/docs?path=docs/newcomer_guide.md")
        assert status == 200 and "text/plain" in headers["Content-Type"]
        assert body.startswith(b"#")
        for bad in ("/api/docs?path=config/budgets.json", "/api/docs?path=../CLAUDE.md",
                    "/api/docs?path=docs/../pyproject.toml", "/api/docs?path=viz/vendor/three.min.js", "/api/docs"):
            assert request(server, bad)[0] == 404, bad
        assert request(server, "/api/docs?path=x&extra=1")[0] == 400
        status, _, body = request(server, "/api/docs/index")
        assert status == 200
        index = json.loads(body)
        assert index["count"] > 30 and any(row["path"] == "docs/local_console.md" for row in index["docs"])
        assert request(server, "/api/docs/index?x=1")[0] == 400


def test_docs_routes_redact_sensitive_values(app, monkeypatch):
    import dca_console_docs
    from conftest import FIXTURE_ACCOUNT_NUMBER

    fake_doc = b"# Guide\nAccount: " + FIXTURE_ACCOUNT_NUMBER.encode() + b"\nUser path: " + str(Path.home()).encode() + b"/project"
    monkeypatch.setattr(dca_console_docs, "read_doc", lambda repo, name: fake_doc)

    with running_server(app, ConsoleAccess(), AuthFailureLog(interval=0, stream=io.StringIO())) as server:
        status, headers, body = request(server, "/api/docs?path=docs/newcomer_guide.md")
        assert status == 200
        text = body.decode("utf-8")
        assert FIXTURE_ACCOUNT_NUMBER not in text
        assert "[redacted]" in text
        assert str(Path.home()) not in text


@pytest.fixture
def app(tmp_path, monkeypatch):
    import sys
    from datetime import date

    import dca_console
    from dca_console import ConsoleApp, Settings

    monkeypatch.setattr(dca_console, "python_packages", lambda _: {})
    application = ConsoleApp(Settings(
        "local", tmp_path / "artifacts", tmp_path / "output", date(2026, 9, 8), sys.executable, sys.executable,
    ))
    yield application
    application.jobs.close()
