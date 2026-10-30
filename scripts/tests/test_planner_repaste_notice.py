"""``scripts.planner_repaste_notice``: tell the operator, by GitHub issue, when a push to main changes the planner.

Why (2026-10-28): the 9:35 backup failed at step 0 because the prompt pasted into Perplexity carried the ``d5ad2719``
stamp while the attached ``backup_planner.pyz`` was ``6e3b2c3b``; ``main`` had moved on to ``f0b3fa93`` by then. Three
rebuilds landed in about a day and nothing told the operator to repaste. The Mac-side ``planner_stamp_check`` only sees
the attached file and only helps when the Mac is on, so this is the second layer: it runs on GitHub when the planner
changes, and the resulting issue reaches the operator wherever they are.

Contract pinned here, before the module exists:

* Unchanged ``content_sha256`` between the previous and current planner: no notice at all (a push that does not move
  the stamp needs no repaste).
* Changed, or the previous planner is missing or unreadable: a notice with a title stable per new hash, and a body
  that names the old and new stamps, the universe hash and symbol count, the commit and the repaste steps.
* An unreadable CURRENT planner is an error (exit 2, nothing written): a silent failure looks like "all good".
* The notice never carries a private value or a placeholder; it is posted to a repository issue.
* The workflow that runs it has no ``workflow_dispatch`` (the repo's trust-boundary rule), holds only
  ``contents: read`` and ``issues: write``, and triggers on pushes to main that touch ``backup_planner.pyz``.
"""

import sys
from pathlib import Path

import pytest
import yaml

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from scripts import planner_repaste_notice as notice_mod  # noqa: E402
from scripts.planner_stamp_check import StampError  # noqa: E402

REPO = Path(__file__).resolve().parents[2]
WORKFLOW = REPO / ".github" / "workflows" / "planner-repaste-notice.yml"

OLD = "d5ad271982ee5d9b854e03dfe6fdea957f3a10e32e48d5eb5188ccf55fce0558"
NEW = "f0b3fa93ddfdbcebea672dbec5e8805da4db21e86b9f98b5c17669cfa57310b3"
UNIVERSE = "d17c2b284aa1f0398798c4b1d29c586ebcb3dca26a766c93abe421126a173c2a"
COMMIT = "ae525faa774b15d1c94f7730b8f3f8bb7875d0f6"


@pytest.fixture
def stamps(monkeypatch):
    """Stand in for the planner files: the file NAME picks which stamp it reads as."""
    table = {"old.pyz": OLD, "new.pyz": NEW, "same.pyz": NEW}

    def fake_stamp(path):
        try:
            return table[Path(path).name]
        except KeyError:
            raise StampError("cannot read planner build metadata") from None

    monkeypatch.setattr(notice_mod, "pasted_stamp", fake_stamp)
    monkeypatch.setattr(notice_mod, "planner_facts", lambda path: (NEW, UNIVERSE, 91))
    return table


def test_an_unchanged_stamp_produces_no_notice(stamps, tmp_path):
    assert notice_mod.build_notice(tmp_path / "same.pyz", tmp_path / "new.pyz", commit=COMMIT) is None


def test_a_changed_stamp_names_old_new_universe_count_and_commit(stamps, tmp_path):
    notice = notice_mod.build_notice(tmp_path / "old.pyz", tmp_path / "new.pyz", commit=COMMIT)

    assert notice is not None
    assert NEW[:8] in notice.title
    for fact in (OLD, NEW, UNIVERSE[:8], "91", COMMIT[:7]):
        assert fact in notice.body
    assert "verify_perplexity_pair" in notice.body  # the check the operator runs before pasting
    assert "fill_perplexity_backup_prompts" in notice.body


def test_the_title_depends_only_on_the_new_stamp_so_reruns_find_the_same_issue(stamps, tmp_path):
    first = notice_mod.build_notice(tmp_path / "old.pyz", tmp_path / "new.pyz", commit=COMMIT)
    second = notice_mod.build_notice(None, tmp_path / "new.pyz", commit="b" * 40)

    assert first.title == second.title


@pytest.mark.parametrize("previous", [None, "absent.pyz"])
def test_a_missing_or_unreadable_previous_planner_still_notifies(stamps, tmp_path, previous):
    prev = None if previous is None else tmp_path / previous

    notice = notice_mod.build_notice(prev, tmp_path / "new.pyz", commit=COMMIT)

    assert notice is not None
    assert "unknown" in notice.body.lower()


def test_an_unreadable_current_planner_is_an_error_not_a_silent_skip(stamps, tmp_path):
    with pytest.raises(StampError):
        notice_mod.build_notice(tmp_path / "old.pyz", tmp_path / "absent.pyz", commit=COMMIT)


def test_the_notice_carries_no_private_value_or_unfilled_placeholder(stamps, tmp_path):
    notice = notice_mod.build_notice(tmp_path / "old.pyz", tmp_path / "new.pyz", commit=COMMIT)

    assert "@" not in notice.body and "@" not in notice.title  # no email address, no mention
    assert "${" not in notice.body
    assert "ROBINHOOD_AGENTIC_ACCOUNT_NUMBER" not in notice.body


def test_main_writes_title_and_body_only_when_the_stamp_changed(stamps, tmp_path, capsys):
    out = tmp_path / "out"

    code = notice_mod.main(["--previous", str(tmp_path / "old.pyz"), "--current", str(tmp_path / "new.pyz"),
                            "--commit", COMMIT, "--out-dir", str(out)])

    assert code == 0
    assert NEW[:8] in (out / "title.txt").read_text(encoding="utf-8")
    assert OLD in (out / "body.md").read_text(encoding="utf-8")

    quiet = tmp_path / "quiet"
    code = notice_mod.main(["--previous", str(tmp_path / "same.pyz"), "--current", str(tmp_path / "new.pyz"),
                            "--commit", COMMIT, "--out-dir", str(quiet)])

    assert code == 0
    assert not (quiet / "title.txt").exists() and not (quiet / "body.md").exists()


def test_main_exits_two_and_writes_nothing_when_the_current_planner_is_unreadable(stamps, tmp_path, capsys):
    out = tmp_path / "out"

    code = notice_mod.main(["--previous", str(tmp_path / "old.pyz"), "--current", str(tmp_path / "absent.pyz"),
                            "--commit", COMMIT, "--out-dir", str(out)])

    assert code == 2
    assert not (out / "title.txt").exists()
    assert str(tmp_path) not in capsys.readouterr().out  # never echo local paths


def test_the_committed_planner_yields_a_notice_when_there_is_no_previous():
    """Real planner, real facts: proves the helpers this module reuses still agree with it."""
    notice = notice_mod.build_notice(None, REPO / "backup_planner.pyz", commit=COMMIT)

    assert notice is not None
    assert "symbols" in notice.body


def _workflow():
    document = yaml.safe_load(WORKFLOW.read_text(encoding="utf-8"))
    # PyYAML reads the bare key `on` as the boolean True.
    document["on"] = document.pop(True, document.get("on"))
    return document


def test_the_workflow_triggers_only_on_pushes_to_main_that_touch_the_planner():
    triggers = _workflow()["on"]

    assert set(triggers) == {"push"}  # no workflow_dispatch: a dispatched ref could supply the code that runs
    assert triggers["push"]["branches"] == ["main"]
    assert triggers["push"]["paths"] == ["backup_planner.pyz"]


def test_the_workflow_holds_only_read_contents_and_write_issues():
    assert _workflow()["permissions"] == {"contents": "read", "issues": "write"}


def test_the_workflow_runs_the_notice_module_and_posts_through_an_issue():
    text = WORKFLOW.read_text(encoding="utf-8")

    assert "scripts.planner_repaste_notice" in text
    assert "gh issue" in text
    assert "planner-repaste" in text  # the label that finds the open issue


# ---------------------------------------------------------------------------------------------------------------------
# What changed inside the planner (2026-10-29). Of 29 hash changes since Oct 14, 25 touched code or JSON and 4 touched
# only comments or docstrings; the hash stays strict, but the notice says which kind of change it is so a
# comment-only one is marked optional. Contract:
#
# * Changed files are found by comparing the bundled files INSIDE the two planners (no git needed in CI).
# * A .py file whose AST is unchanged once docstrings are dropped is "comment-only"; JSON, bootstrap, added, removed
#   and unparsable files are real changes. Any real change makes the notice "needed"; otherwise "optional".
# * When the previous planner cannot be read the kind is "needed": unknown is never downgraded.
# * Only file names and kinds are listed, never contents, and a long list is cut to a bounded table.
# * An open "needed" issue is never retitled or rewritten by an "optional" notice (the workflow only comments).
# ---------------------------------------------------------------------------------------------------------------------

import json  # noqa: E402
import zipfile  # noqa: E402


def make_pyz(path, files, *, stamp, bootstrap="print('boot')\n"):
    """A minimal planner zipapp that passes planner_stamp_check.pasted_stamp's structural checks."""
    info = {"content_sha256": stamp, "files": sorted(files), "schema_version": 1}
    with zipfile.ZipFile(path, "w") as archive:
        archive.writestr("__main__.py", bootstrap)
        archive.writestr("BUILD_INFO.json", json.dumps(info))
        for name, text in files.items():
            archive.writestr(name, text)
    return path


@pytest.fixture
def facts(monkeypatch):
    monkeypatch.setattr(notice_mod, "planner_facts", lambda path: (NEW, UNIVERSE, 91))


def _pair(tmp_path, old_files, new_files, **kw):
    old = make_pyz(tmp_path / "old.pyz", old_files, stamp=OLD)
    new = make_pyz(tmp_path / "new.pyz", new_files, stamp=NEW, **kw)
    return old, new


def test_a_comment_or_docstring_only_change_is_optional(facts, tmp_path):
    old, new = _pair(
        tmp_path,
        {"a.py": '"""Old doc."""\n# old note\nVALUE = 1\n'},
        {"a.py": '"""New doc, longer."""\n# new note\nVALUE = 1\n'},
    )

    notice = notice_mod.build_notice(old, new, commit=COMMIT)

    assert notice.kind == "optional"
    assert notice.title.startswith("Repaste optional") and NEW[:8] in notice.title
    assert "a.py" in notice.body and "comment-only" in notice.body


def test_a_code_change_is_needed_and_names_the_file(facts, tmp_path):
    old, new = _pair(tmp_path, {"a.py": "VALUE = 1\n", "b.py": "X = 1\n"}, {"a.py": "VALUE = 2\n", "b.py": "X = 1\n"})

    notice = notice_mod.build_notice(old, new, commit=COMMIT)

    assert notice.kind == "needed" and notice.title.startswith("Repaste needed")
    assert "a.py" in notice.body and "code" in notice.body
    assert "b.py" not in notice.body  # unchanged files are not listed


def test_a_json_added_removed_or_unparsable_file_is_a_real_change(facts, tmp_path):
    cases = [
        ({"c.json": '{"a": 1}'}, {"c.json": '{"a": 2}'}),
        ({"a.py": "X = 1\n"}, {"a.py": "X = 1\n", "new.py": "Y = 1\n"}),
        ({"a.py": "X = 1\n", "old.py": "Y = 1\n"}, {"a.py": "X = 1\n"}),
        ({"a.py": "X = 1\n"}, {"a.py": "X = (\n"}),  # a syntax error cannot be proven comment-only
    ]
    for index, (before, after) in enumerate(cases):
        sub = tmp_path / str(index)
        sub.mkdir()
        old, new = _pair(sub, before, after)
        assert notice_mod.build_notice(old, new, commit=COMMIT).kind == "needed", (before, after)


def test_a_changed_bootstrap_is_a_real_change_even_when_every_file_is_identical(facts, tmp_path):
    files = {"a.py": "X = 1\n"}
    old = make_pyz(tmp_path / "old.pyz", files, stamp=OLD, bootstrap="print('one')\n")
    new = make_pyz(tmp_path / "new.pyz", files, stamp=NEW, bootstrap="print('two')\n")

    notice = notice_mod.build_notice(old, new, commit=COMMIT)

    assert notice.kind == "needed" and "bootstrap" in notice.body


def test_an_unreadable_previous_planner_is_never_downgraded_to_optional(facts, tmp_path):
    new = make_pyz(tmp_path / "new.pyz", {"a.py": "X = 1\n"}, stamp=NEW)

    for previous in (None, tmp_path / "absent.pyz"):
        notice = notice_mod.build_notice(previous, new, commit=COMMIT)
        assert notice.kind == "needed"


def test_a_long_list_of_changed_files_is_cut_to_a_bounded_table(facts, tmp_path):
    before = {f"m{i:02d}.py": "X = 1\n" for i in range(45)}
    after = {f"m{i:02d}.py": "X = 2\n" for i in range(45)}
    old, new = _pair(tmp_path, before, after)

    notice = notice_mod.build_notice(old, new, commit=COMMIT)

    assert notice.body.count("| `m") == notice_mod.MAX_LISTED_FILES
    assert "and 15 more" in notice.body


def test_file_contents_never_reach_the_notice(facts, tmp_path):
    old, new = _pair(tmp_path, {"a.py": "SECRET_TOKEN = 'abc'\n"}, {"a.py": "SECRET_TOKEN = 'xyz'\n"})

    notice = notice_mod.build_notice(old, new, commit=COMMIT)

    assert "abc" not in notice.body and "xyz" not in notice.body and "SECRET_TOKEN" not in notice.body


def test_main_writes_the_kind_beside_the_title_and_body(facts, tmp_path):
    old, new = _pair(tmp_path, {"a.py": "# one\nX = 1\n"}, {"a.py": "# two\nX = 1\n"})
    out = tmp_path / "out"

    code = notice_mod.main(["--previous", str(old), "--current", str(new), "--commit", COMMIT, "--out-dir", str(out)])

    assert code == 0
    assert (out / "kind.txt").read_text(encoding="utf-8") == "optional"


def test_the_workflow_never_rewrites_an_open_needed_issue_for_an_optional_notice():
    text = WORKFLOW.read_text(encoding="utf-8")

    assert "kind.txt" in text
    assert '"${kind}" = "needed"' in text  # the edit is guarded; the comment is not


def test_a_moved_stamp_with_no_differing_file_is_still_a_needed_repaste(facts, tmp_path):
    files = {"a.py": "X = 1\n"}
    old = make_pyz(tmp_path / "old.pyz", files, stamp=OLD)
    new = make_pyz(tmp_path / "new.pyz", files, stamp=NEW)

    notice = notice_mod.build_notice(old, new, commit=COMMIT)

    assert notice.kind == "needed" and "No bundled file differs" in notice.body


def test_an_oversized_bundled_file_makes_the_changes_unknown_not_an_expansion(facts, tmp_path, monkeypatch):
    monkeypatch.setattr(notice_mod, "_MAX_MEMBER_BYTES", 4)
    old, new = _pair(tmp_path, {"a.py": "X = 1\n"}, {"a.py": "X = 2\n"})

    notice = notice_mod.build_notice(old, new, commit=COMMIT)

    assert notice.kind == "needed" and "unknown" in notice.body.lower()
