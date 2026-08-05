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


def _defined_names(path: Path) -> set:
    """Every function/class/constant name a module defines, at any nesting level.

    AST-based rather than regex-based so a symbol pointing at an ``async def``,
    an indented (nested/method) definition, or a module-level constant is
    verified correctly instead of silently never matching.
    """
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    names = set()
    for node in ast.walk(tree):
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
