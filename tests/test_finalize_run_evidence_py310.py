"""The Gate 9 finalizer must stay importable on Python 3.10 (2026-10-20).

The 14:30/15:30 live sessions fall back to a bridge shell whose only Python is
3.10.12 when uv cannot install 3.11 (the 2026-10-13..16 aborts). Those aborts
wrote no run_state.json, so the 12:30 trailing-7-day afternoon check failed
closed for a week. The live-mirror prompts now run ``scripts.finalize_run_evidence``
on that 3.10 purely to write evidence; this test keeps its import closure free
of 3.11-only syntax and stdlib so that fallback keeps working. (Verified by hand
on CPython 3.10.20: both slots write and verify run_state.json.)
"""

import ast
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
ENTRY = "scripts/finalize_run_evidence.py"
LIVE_MIRROR = ROOT / "claude" / "dca-executor-prompts" / "live-mirror"
# stdlib names that only exist from 3.11 on.
_NEW_STDLIB_MODULES = {"tomllib"}
_NEW_FROM_IMPORTS = {
    "datetime": {"UTC"},
    "enum": {"StrEnum"},
    "typing": {"Self", "LiteralString", "Never", "assert_never", "NotRequired", "Required", "Unpack"},
}


def _import_closure() -> dict:
    stdlib = set(sys.stdlib_module_names)
    seen, todo = {}, [ENTRY]
    while todo:
        rel = todo.pop()
        if rel in seen:
            continue
        seen[rel] = (ROOT / rel).read_text(encoding="utf-8")
        for node in ast.walk(ast.parse(seen[rel])):
            names = []
            if isinstance(node, ast.Import):
                names = [a.name for a in node.names]
            elif isinstance(node, ast.ImportFrom) and node.module and node.level == 0:
                names = [node.module] + [f"{node.module}.{a.name}" for a in node.names]
            for name in names:
                candidate = name.replace(".", "/") + ".py"
                if name.split(".")[0] not in stdlib and (ROOT / candidate).exists():
                    todo.append(candidate)
    return seen


def test_finalizer_import_closure_has_no_py311_only_features():
    problems = []
    for rel, src in _import_closure().items():
        try:
            tree = ast.parse(src, feature_version=(3, 10))
        except SyntaxError as exc:
            problems.append(f"{rel}:{exc.lineno} {exc.msg}")
            continue
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                problems += [f"{rel}:{node.lineno} import {a.name}" for a in node.names
                             if a.name.split(".")[0] in _NEW_STDLIB_MODULES]
            elif isinstance(node, ast.ImportFrom) and node.level == 0 and node.module:
                if node.module.split(".")[0] in _NEW_STDLIB_MODULES:
                    problems.append(f"{rel}:{node.lineno} from {node.module}")
                bad = _NEW_FROM_IMPORTS.get(node.module, set())
                problems += [f"{rel}:{node.lineno} {node.module}.{a.name}" for a in node.names if a.name in bad]
    assert not problems, "Python 3.11-only features in the finalizer's import closure: " + "; ".join(problems)


def test_afternoon_prompts_finalize_evidence_without_python_311():
    for name in ("1430ET-230-pm-afternoon-buying-run.txt", "1530ET-330-final-buying-run.txt"):
        text = (LIVE_MIRROR / name).read_text(encoding="utf-8")
        assert "EVIDENCE STILL GETS WRITTEN" in text, name
        assert "GATE2_ABORT_NO_PYTHON311" in text, name
