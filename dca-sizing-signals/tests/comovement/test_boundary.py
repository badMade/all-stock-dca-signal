"""comovement_signals stays pure: stdlib plus dca_shared_interfaces only.

No core module (near_close_dca and every other repo-root module), no sibling suggester or
model package, no market_data_ingest, and no network-capable stdlib module. Prices reach
this package only as rows from the caller.
"""

from __future__ import annotations

import ast
import sys
from pathlib import Path

PACKAGE_ROOT = Path(__file__).resolve().parents[2]
PACKAGE = PACKAGE_ROOT / "comovement_signals"
REPO_ROOT = PACKAGE_ROOT.parent

NETWORK_STDLIB = {"socket", "ssl", "http", "urllib", "ftplib", "smtplib", "poplib", "imaplib",
                  "telnetlib", "xmlrpc", "asyncio", "subprocess"}
ALLOWED_OWN = {"comovement_signals", "dca_shared_interfaces", "__future__"}


def _top_level_imports(py_file: Path) -> set[str]:
    tree = ast.parse(py_file.read_text(), filename=str(py_file))
    names: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            names.update(a.name.split(".")[0] for a in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module and node.level == 0:
            names.add(node.module.split(".")[0])
    return names


def test_package_exists():
    assert PACKAGE.is_dir() and any(PACKAGE.glob("*.py"))


def test_imports_only_stdlib_and_shared_interfaces():
    allowed = (set(sys.stdlib_module_names) - NETWORK_STDLIB) | ALLOWED_OWN
    violations = []
    for py in sorted(PACKAGE.rglob("*.py")):
        bad = _top_level_imports(py) - allowed
        if bad:
            violations.append(f"{py.relative_to(PACKAGE_ROOT)}: {sorted(bad)}")
    assert not violations, "comovement_signals imports outside stdlib + dca_shared_interfaces:\n" + "\n".join(
        violations)


def test_never_imports_core_or_sibling_packages():
    core = {p.stem for p in REPO_ROOT.glob("*.py")} | {"near_close_dca"}
    forbidden = core | {"ml_forecaster", "wondrous_octopus", "connector_signals", "market_data_ingest"}
    hits = {py.name: _top_level_imports(py) & forbidden for py in PACKAGE.rglob("*.py")}
    assert not any(hits.values()), hits


def test_shared_interfaces_do_not_import_comovement():
    for py in (PACKAGE_ROOT / "dca_shared_interfaces").rglob("*.py"):
        assert "comovement_signals" not in _top_level_imports(py), py
