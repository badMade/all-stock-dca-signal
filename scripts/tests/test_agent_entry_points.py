"""Tests for scripts/agent_entry_points.py.

Runnable with either ``pytest scripts/tests`` or
``python scripts/tests/test_agent_entry_points.py``.

The entry-point index only earns trust if it never drifts stale, so every
entry's referenced path, doc, and symbol is checked for real: a listed
command that points at a file which no longer exists (or a symbol that was
renamed) is exactly the kind of silent rot that would make an agent trust
the index less than reading the source itself.
"""

import ast
import subprocess
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO_ROOT))

from scripts.agent_entry_points import ENTRY_POINTS, main  # noqa: E402


def _defined_names(path: Path) -> set[str]:
    """Every module-level function/class/constant name a module defines.

    AST-based rather than regex-based so a symbol rename is caught reliably.
    Restricting to module-level statements ensures `ENTRY_POINTS[*].symbols`
    reflect names callers can access as `module.<name>`.
    """
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    names: set[str] = set()
    for node in tree.body:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            names.add(node.name)
        elif isinstance(node, ast.Assign):
            for target in node.targets:
                names.update(sub.id for sub in ast.walk(target) if isinstance(sub, ast.Name))
        elif isinstance(node, ast.AnnAssign) and isinstance(node.target, ast.Name):
            names.add(node.target.id)
    return names


def test_entry_points_nonempty():
    assert len(ENTRY_POINTS) >= 1


def test_every_exists_path_resolves():
    for entry in ENTRY_POINTS:
        path = REPO_ROOT / entry.exists_path
        assert path.is_file(), f"{entry.need!r} points at missing file: {entry.exists_path}"


def test_every_doc_ref_resolves():
    for entry in ENTRY_POINTS:
        path = REPO_ROOT / entry.doc_ref
        assert path.is_file(), f"{entry.need!r} points at missing doc: {entry.doc_ref}"


def test_every_symbol_appears_in_its_file():
    for entry in ENTRY_POINTS:
        if not entry.symbols:
            continue
        path = REPO_ROOT / entry.exists_path
        assert path.suffix == ".py", f"{entry.need!r} lists symbols but {entry.exists_path} is not a Python file"
        defined = _defined_names(path)
        for sym in entry.symbols:
            assert sym in defined, f"{entry.need!r} references symbol {sym!r}, not found in {entry.exists_path}"


def test_no_duplicate_needs():
    needs = [entry.need for entry in ENTRY_POINTS]
    assert len(needs) == len(set(needs))


def test_cli_human_readable_output():
    assert main([]) == 0


def test_cli_json_output_is_valid_json():
    import io
    import json
    from contextlib import redirect_stdout

    buf = io.StringIO()
    with redirect_stdout(buf):
        exit_code = main(["--json"])
    assert exit_code == 0
    parsed = json.loads(buf.getvalue())
    assert len(parsed) == len(ENTRY_POINTS)


def test_cli_search_filters_and_can_be_empty():
    assert main(["--search", "same-day top-up"]) == 0
    assert main(["--search", "no-entry-point-mentions-this-string"]) == 1


def test_cli_json_search_with_no_matches_still_exits_zero():
    import io
    import json
    from contextlib import redirect_stdout

    buf = io.StringIO()
    with redirect_stdout(buf):
        exit_code = main(["--search", "no-entry-point-mentions-this-string", "--json"])
    assert exit_code == 0
    assert json.loads(buf.getvalue()) == []


def test_module_runs_as_subprocess():
    result = subprocess.run(
        [sys.executable, str(REPO_ROOT / "scripts" / "agent_entry_points.py")],
        capture_output=True,
        text=True,
        check=True,
        cwd=REPO_ROOT,
    )
    assert "Dry-run the full DCA pipeline" in result.stdout


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(pytest.main([__file__, "-q"]))


def _command_substitutions(text: str):
    """Yield (start, end) spans of every ``$(...)`` in ``text``, parens balanced.

    Written as a scan rather than a regex on purpose: the catalog's own
    substitution is ``$(python3 -c 'import dca_paths;
    print(dca_paths.cron_tracking_root())')``, whose body nests parens two deep.
    A regex allowing one level of nesting matched nothing here and the guard
    passed vacuously against a genuinely unquoted command -- caught only by
    reverting the fix and finding the test still green.
    """
    index = 0
    while True:
        start = text.find("$(", index)
        if start == -1:
            return
        depth = 0
        for pos in range(start + 1, len(text)):
            if text[pos] == "(":
                depth += 1
            elif text[pos] == ")":
                depth -= 1
                if depth == 0:
                    yield start, pos + 1
                    index = pos + 1
                    break
        else:  # unbalanced -- nothing further to check
            return


def test_command_substitutions_in_the_catalog_are_quoted():
    """Regression for "Quote the command-substituted state root".

    The catalog's commands are meant to be pasted into a shell, and this
    substitution resolves through dca_paths, which honours
    $DCA_CRON_TRACKING_ROOT -- an operator-set path this repo deliberately does
    not constrain (AGENTS.md item 4). Unquoted, a root containing whitespace
    word-splits into several argv entries and the documented command fails
    instead of producing its report.

    Asserted over every entry rather than the one a review named: the catalog
    grows, and this is a property of any command it publishes.
    """
    for entry in ENTRY_POINTS:
        text = " ".join(
            str(getattr(entry, name, "") or "") for name in ("command", "notes")
        )
        for start, end in _command_substitutions(text):
            assert text[start - 1 : start] == '"' and text[end : end + 1] == '"', (
                f"unquoted command substitution in {entry.need!r}: {text[start:end]}"
            )
