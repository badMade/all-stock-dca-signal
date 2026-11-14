"""Goal progress can never trade (docs/adr-goals-and-loss-hedge-shadow.md, Phase 2).

Written FIRST. Static checks over module source, modelled on the hedge no-orders test: the
Phase 2 modules may read Robinhood positions and quotes, but no later edit can quietly add an
order, preview or review path. Fails until ``dca_goal_progress.py`` exists.
"""

from __future__ import annotations

import ast
import re
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
PROGRESS_MODULE = REPO_ROOT / "dca_goal_progress.py"

# Word-bounded at the start so "replace_order" never matches "place_order" (same rule as the hedge test).
ORDER_TOOL = re.compile(
    r"(?<![A-Za-z0-9_])(place|cancel|review|preview|exercise|decline|accept)_(equity|option|crypto|advanced|order|trade)"
)
BROKER_IMPORT = re.compile(r"(robinhood|broker|mcp|requests|urllib|http|socket|httpx|aiohttp)", re.IGNORECASE)


def _imports(path: Path) -> set[str]:
    tree = ast.parse(path.read_text(encoding="utf-8"))
    names: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            names.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            names.add(node.module)
    return names


def test_progress_module_exists():
    assert PROGRESS_MODULE.exists(), "dca_goal_progress.py must land with Phase 2"


def test_progress_module_names_no_order_preview_or_review_tool():
    text = PROGRESS_MODULE.read_text(encoding="utf-8")
    hits = [m.group(0) for m in ORDER_TOOL.finditer(text)]
    assert hits == [], f"order-shaped tool names found: {hits}"


def test_progress_module_imports_no_broker_network_or_mcp_client():
    offending = sorted(name for name in _imports(PROGRESS_MODULE) if BROKER_IMPORT.search(name))
    assert offending == [], f"broker/network imports found: {offending}"


def test_progress_module_has_no_dynamic_import_or_eval():
    text = PROGRESS_MODULE.read_text(encoding="utf-8")
    for pattern in (r"\beval\s*\(", r"\bexec\s*\(", r"__import__\s*\(", r"importlib\.import_module"):
        assert not re.search(pattern, text), f"dynamic execution: {pattern}"


def test_order_pattern_catches_real_shapes_and_spares_ordinary_words():
    for shape in ("place_equity_order", "cancel_option_order", "review_crypto_order", "preview_crypto_order"):
        assert ORDER_TOOL.search(shape), shape
    for ordinary in ("replace_order_book", "workplace_equity", "get_equity_positions", "get_portfolio"):
        assert not ORDER_TOOL.search(ordinary), ordinary
