"""The loss-hedge shadow log can never trade (docs/adr-goals-and-loss-hedge-shadow.md, tests 6 and 10).

Written FIRST: fails until ``market_data_ingest/ledger/hedge_shadow.py``, ``dca_goals.py`` and
``dca_console_goals.py`` exist. Modelled on ``test_csp_shadow_no_orders.py``: static checks over
module source, so no later edit can quietly add an order path.

* Test 6: no module in the goal or hedge path names an equity or options order tool, or imports
  a broker, MCP or network client.
* Test 10: the same modules name no crypto order tool either.
"""

from __future__ import annotations

import ast
import re
from pathlib import Path

PACKAGE_ROOT = Path(__file__).resolve().parents[2]
REPO_ROOT = PACKAGE_ROOT.parent

HEDGE_MODULE = PACKAGE_ROOT / "market_data_ingest" / "ledger" / "hedge_shadow.py"
GOAL_MODULES = [REPO_ROOT / "dca_goals.py", REPO_ROOT / "dca_console_goals.py"]
ALL_MODULES = [HEDGE_MODULE, *GOAL_MODULES]

# Word-bounded at the start so "replace_" or "workplace" never match "place_" (same rule as the CSP test).
_BROKER_TOOL = re.compile(r"\b(place|preview|review|cancel|submit)_\w*order\w*|\b(place|preview|review|cancel)_\w*",
                          re.IGNORECASE)
_MCP = re.compile(r"mcp__|robinhood\.|equity_order|option_order|RobinhoodBroker|build_broker", re.IGNORECASE)
# Crypto order tool shapes: place_crypto_order, crypto_order, order_crypto, crypto_trade, buy_crypto, sell_crypto.
_CRYPTO_ORDER = re.compile(r"crypto_order|order_crypto|crypto_trade|\b(buy|sell)_crypto", re.IGNORECASE)

_HEDGE_ALLOWED_IMPORTS = {"__future__", "dataclasses", "datetime", "decimal", "hashlib", "json", "math",
                          "collections", "statistics", "typing", "zoneinfo", "duckdb", "market_data_ingest"}
_FORBIDDEN_ANYWHERE = {"socket", "urllib", "http", "requests", "httpx", "aiohttp", "dca_live_adapters",
                       "near_close_dca", "run_dca", "dca_runner", "robin_stocks"}


def _imports(path: Path) -> set[str]:
    tops: set[str] = set()
    for node in ast.walk(ast.parse(path.read_text())):
        if isinstance(node, ast.Import):
            tops.update(a.name.split(".")[0] for a in node.names)
        elif isinstance(node, ast.ImportFrom):
            if node.level:
                tops.add("market_data_ingest")
            elif node.module:
                tops.add(node.module.split(".")[0])
    return tops


def _offending_lines(path: Path, *patterns: re.Pattern[str]) -> list[str]:
    return [f"{path.name}:{n}: {line.strip()}" for n, line in enumerate(path.read_text().splitlines(), 1)
            if any(p.search(line) for p in patterns)]


def test_every_module_exists():
    missing = [str(p.relative_to(REPO_ROOT)) for p in ALL_MODULES if not p.exists()]
    assert not missing, f"not found: {missing}"


# --------------------------------------------------------------------------------------------
# Test 6: no equity or options order path, no broker or network import.
# --------------------------------------------------------------------------------------------

def test_6_no_module_names_a_broker_order_tool_or_mcp():
    offenders = [line for path in ALL_MODULES for line in _offending_lines(path, _BROKER_TOOL, _MCP)]
    assert not offenders, "the goal and hedge path must never place, preview, review or cancel an order:\n" + \
        "\n".join(offenders)


def test_6_the_hedge_module_imports_only_the_csp_shadow_allowlist():
    unexpected = _imports(HEDGE_MODULE) - _HEDGE_ALLOWED_IMPORTS
    assert not unexpected, f"unexpected imports: {sorted(unexpected)}"


def test_6_no_module_imports_a_network_or_broker_client():
    for path in ALL_MODULES:
        bad = _imports(path) & _FORBIDDEN_ANYWHERE
        assert not bad, f"{path.name} imports {sorted(bad)}"


def test_6_no_dynamic_import_or_eval():
    for path in ALL_MODULES:
        text = path.read_text()
        for banned in ("__import__", "importlib", "eval(", "exec(", "subprocess", "socket", "urllib"):
            assert banned not in text, f"{path.name}: {banned}"


def test_6_the_grep_catches_real_tool_names_and_spares_ordinary_words():
    for name in ("place_equity_order", "review_option_order", "cancel_equity_order", "preview_scan",
                 "submit_order"):
        assert _BROKER_TOOL.search(name), name
    for word in ("replace_all", "str.replace(", "displace_x", "workplace", "reviewer"):
        assert not _BROKER_TOOL.search(word), word


# --------------------------------------------------------------------------------------------
# Test 10: no crypto order path.
# --------------------------------------------------------------------------------------------

def test_10_no_module_names_a_crypto_order_tool():
    offenders = [line for path in ALL_MODULES for line in _offending_lines(path, _CRYPTO_ORDER)]
    assert not offenders, "the crypto hedge is shadow-only:\n" + "\n".join(offenders)


def test_10_the_crypto_grep_catches_order_shapes_and_spares_quote_reads():
    for name in ("place_crypto_order", "crypto_order", "order_crypto", "buy_crypto", "sell_crypto"):
        assert _CRYPTO_ORDER.search(name), name
    # Reading quotes is the shadow log's job; it must stay allowed.
    for word in ("get_crypto_quotes", "crypto_price_at", "crypto_allocation"):
        assert not _CRYPTO_ORDER.search(word), word
