"""inventory_signals stays pure: stdlib plus dca_shared_interfaces only.

No core module, no sibling suggester or model package, no market_data_ingest, no yfinance,
and no network-capable stdlib module. Inputs reach this package only from the caller.
"""

from __future__ import annotations

import ast
import sys
from pathlib import Path

PACKAGE_ROOT = Path(__file__).resolve().parents[2]
PACKAGE = PACKAGE_ROOT / "inventory_signals"
REPO_ROOT = PACKAGE_ROOT.parent

NETWORK_STDLIB = {"socket", "ssl", "http", "urllib", "ftplib", "smtplib", "poplib", "imaplib",
                  "telnetlib", "xmlrpc", "asyncio", "subprocess"}
ALLOWED_OWN = {"inventory_signals", "dca_shared_interfaces", "__future__"}


def _top_level_imports(py_file: Path) -> set[str]:
    tree = ast.parse(py_file.read_text(), filename=str(py_file))
    names: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            names.update(a.name.split(".")[0] for a in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module and node.level == 0:
            names.add(node.module.split(".")[0])
    return names


def test_package_exists_and_is_packaged():
    assert PACKAGE.is_dir() and (PACKAGE / "__init__.py").is_file()
    assert '"inventory_signals*"' in (PACKAGE_ROOT / "pyproject.toml").read_text()


def test_imports_only_stdlib_and_shared_interfaces():
    allowed = (set(sys.stdlib_module_names) - NETWORK_STDLIB) | ALLOWED_OWN
    violations = {py.name: sorted(_top_level_imports(py) - allowed) for py in PACKAGE.rglob("*.py")}
    assert not any(violations.values()), violations


def test_never_imports_core_or_sibling_packages():
    core = {p.stem for p in REPO_ROOT.glob("*.py")} | {"near_close_dca"}
    forbidden = core | {"ml_forecaster", "wondrous_octopus", "connector_signals", "market_data_ingest",
                        "comovement_signals", "overnight_futures", "yfinance"}
    hits = {py.name: _top_level_imports(py) & forbidden for py in PACKAGE.rglob("*.py")}
    assert not any(hits.values()), hits


def test_live_side_never_imports_inventory_signals():
    # Shadow mode: no repo-root module (the core, the three entry points, the reader) reads it yet.
    hits = [p.name for p in REPO_ROOT.glob("*.py") if "inventory_signals" in _top_level_imports(p)]
    assert not hits, hits


def test_other_packages_do_not_import_inventory_signals():
    for pkg in ("dca_shared_interfaces", "comovement_signals", "connector_signals", "overnight_futures",
                "market_data_ingest"):
        for py in (PACKAGE_ROOT / pkg).rglob("*.py"):
            assert "inventory_signals" not in _top_level_imports(py), py
