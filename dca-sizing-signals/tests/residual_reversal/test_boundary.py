"""residual_reversal stays pure: stdlib plus dca_shared_interfaces only, no network-capable
stdlib module, no core module and no sibling package. Prices arrive only as caller rows."""

from __future__ import annotations

import ast
import sys
from pathlib import Path

PACKAGE_ROOT = Path(__file__).resolve().parents[2]
PACKAGE = PACKAGE_ROOT / "residual_reversal"
REPO_ROOT = PACKAGE_ROOT.parent

NETWORK_STDLIB = {"socket", "ssl", "http", "urllib", "ftplib", "smtplib", "poplib", "imaplib",
                  "telnetlib", "xmlrpc", "asyncio", "subprocess"}
ALLOWED_OWN = {"residual_reversal", "dca_shared_interfaces", "__future__"}


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


def test_imports_only_stdlib_and_shared_interfaces():
    allowed = (set(sys.stdlib_module_names) - NETWORK_STDLIB) | ALLOWED_OWN
    bad = {py.name: sorted(_imports(py) - allowed) for py in PACKAGE.rglob("*.py") if _imports(py) - allowed}
    assert not bad, bad


def test_never_imports_core_or_sibling_packages():
    core = {p.stem for p in REPO_ROOT.glob("*.py")} | {"near_close_dca"}
    forbidden = core | {"ml_forecaster", "wondrous_octopus", "connector_signals", "market_data_ingest",
                        "comovement_signals", "overnight_futures"}
    hits = {py.name: _imports(py) & forbidden for py in PACKAGE.rglob("*.py")}
    assert not any(hits.values()), hits


def test_no_other_package_imports_residual_reversal():
    for pkg in ("dca_shared_interfaces", "connector_signals", "market_data_ingest", "comovement_signals",
                "overnight_futures"):
        for py in (PACKAGE_ROOT / pkg).rglob("*.py"):
            assert "residual_reversal" not in _imports(py), py


def test_live_side_never_imports_residual_reversal():
    """Not wired into any live path: no repo-root module (near_close_dca, the three entry
    points, sizing_adjustment_reader.py) imports it. Wiring needs its own ADR."""
    hits = [p.name for p in REPO_ROOT.glob("*.py") if "residual_reversal" in _imports(p)]
    assert not hits, hits


def test_is_packaged():
    assert '"residual_reversal*"' in (PACKAGE_ROOT / "pyproject.toml").read_text()
