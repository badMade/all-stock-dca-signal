"""The shadow log can never trade (ADR docs/adr-csp-shadow-log.md, Verification bullet 1).

Static checks over the module source: no broker order tool name (place_, preview_, review_,
cancel_ -- word-bounded so ordinary words such as ``replace`` do not trip it), no MCP tool
reference, and no import of anything that could reach a network or a broker. The repo's
append-only rule (no row-mutating SQL) is enforced package-wide by test_ledger_schema.py,
which already scans this module too.
"""

from __future__ import annotations

import ast
import re
from pathlib import Path

MODULE = Path(__file__).resolve().parents[2] / "market_data_ingest" / "ledger" / "csp_shadow.py"

# Word-bounded at the start so "replace_..." or "displace_..." never matches "place_".
_BROKER_TOOL = re.compile(r"\b(place|preview|review|cancel)_\w*", re.IGNORECASE)
_MCP = re.compile(r"mcp__|robinhood\.|place_equity_order|option_order", re.IGNORECASE)
_ALLOWED_TOP_LEVEL = {"__future__", "dataclasses", "datetime", "hashlib", "json", "math", "collections", "typing",
                      "zoneinfo", "duckdb", "market_data_ingest"}


def test_module_exists():
    assert MODULE.exists(), "market_data_ingest/ledger/csp_shadow.py not found"


def test_module_names_no_broker_order_tool():
    offenders = [f"{n}: {line.strip()}" for n, line in enumerate(MODULE.read_text().splitlines(), 1)
                 if _BROKER_TOOL.search(line) or _MCP.search(line)]
    assert not offenders, "the CSP shadow log must never place, preview, review or cancel an order:\n" + "\n".join(
        offenders)


def test_the_grep_itself_catches_tool_names():
    # Guard the guard: the pattern must flag the real tool names and spare ordinary words.
    for name in ("place_equity_order", "review_option_order", "cancel_equity_order", "preview_scan"):
        assert _BROKER_TOOL.search(name)
    for word in ("replace_all", "str.replace(", "displace_x", "workplace"):
        assert not _BROKER_TOOL.search(word)


def test_module_imports_nothing_that_can_reach_a_network_or_broker():
    tree = ast.parse(MODULE.read_text())
    tops: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            tops.update(a.name.split(".")[0] for a in node.names)
        elif isinstance(node, ast.ImportFrom):
            if node.level:  # relative import inside market_data_ingest.ledger
                tops.add("market_data_ingest")
            elif node.module:
                tops.add(node.module.split(".")[0])
    assert tops <= _ALLOWED_TOP_LEVEL, f"unexpected imports: {sorted(tops - _ALLOWED_TOP_LEVEL)}"


def test_module_has_no_dynamic_import_or_eval():
    text = MODULE.read_text()
    for banned in ("__import__", "importlib", "eval(", "exec(", "getattr(", "subprocess", "socket", "urllib"):
        assert banned not in text, banned
