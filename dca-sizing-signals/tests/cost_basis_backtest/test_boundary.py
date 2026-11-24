"""cost_basis_backtest stays an offline research tool: stdlib only, no network-capable
stdlib module, no DuckDB, no suggester, no core module,
and nothing -- no sibling package, no repo-root module -- imports it. It cannot reach live
sizing because nothing live can reach it."""

from __future__ import annotations

import ast
import sys
from pathlib import Path

PACKAGE_ROOT = Path(__file__).resolve().parents[2]
PACKAGE = PACKAGE_ROOT / "cost_basis_backtest"
REPO_ROOT = PACKAGE_ROOT.parent

NETWORK_STDLIB = {"socket", "ssl", "http", "urllib", "ftplib", "smtplib", "poplib", "imaplib",
                  "telnetlib", "xmlrpc", "asyncio", "subprocess", "sqlite3"}
ALLOWED_OWN = {"cost_basis_backtest", "__future__"}
SIBLINGS = ("dca_shared_interfaces", "connector_signals", "market_data_ingest", "comovement_signals",
            "overnight_futures", "residual_reversal")


def _imports(py_file: Path) -> set[str]:
    names: set[str] = set()
    for node in ast.walk(ast.parse(py_file.read_text(), filename=str(py_file))):
        if isinstance(node, ast.Import):
            names.update(a.name.split(".")[0] for a in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module and node.level == 0:
            names.add(node.module.split(".")[0])
    return names


def test_package_exists():
    assert (PACKAGE / "__init__.py").is_file()


def test_imports_only_stdlib():
    allowed = (set(sys.stdlib_module_names) - NETWORK_STDLIB) | ALLOWED_OWN
    bad = {py.name: sorted(_imports(py) - allowed) for py in PACKAGE.rglob("*.py") if _imports(py) - allowed}
    assert not bad, bad


def test_never_imports_core_or_sibling_packages():
    core = {p.stem for p in REPO_ROOT.glob("*.py")} | {"near_close_dca", "duckdb"}
    forbidden = core | set(SIBLINGS) | {"ml_forecaster", "wondrous_octopus"}
    hits = {py.name: _imports(py) & forbidden for py in PACKAGE.rglob("*.py")}
    assert not any(hits.values()), hits


def test_no_sibling_package_imports_it():
    for pkg in SIBLINGS:
        for py in (PACKAGE_ROOT / pkg).rglob("*.py"):
            assert "cost_basis_backtest" not in _imports(py), py


def test_live_side_never_imports_it():
    hits = [p.name for p in REPO_ROOT.glob("*.py") if "cost_basis_backtest" in _imports(p)]
    assert not hits, hits


def test_writes_nothing():
    """No file or directory writes anywhere in the package: results go back to the caller."""
    banned_attrs = {"write_text", "write_bytes", "mkdir", "unlink", "rename", "replace", "touch"}
    hits = []
    for py in PACKAGE.rglob("*.py"):
        for node in ast.walk(ast.parse(py.read_text(), filename=str(py))):
            if isinstance(node, ast.Call) and isinstance(node.func, ast.Name) and node.func.id == "open":
                hits.append(f"{py.name}:{node.lineno} open()")
            if isinstance(node, ast.Attribute) and node.attr in banned_attrs:
                hits.append(f"{py.name}:{node.lineno} .{node.attr}")
    assert not hits, hits


def test_is_packaged():
    assert '"cost_basis_backtest*"' in (PACKAGE_ROOT / "pyproject.toml").read_text()
