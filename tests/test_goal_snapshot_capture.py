"""Read-only snapshot capture of the PREVIOUS trading day's close (Phase 2, ADR tests 22-24).

Written FIRST. Skipped until ``dca_goal_snapshot.py`` lands. The capture takes injected read-only
callables (positions, prior close) that the scheduled agent session wires to Robinhood read tools.
The repo itself makes no broker or network call.

* A missed trading day writes no row and raises ``missed_snapshot`` (any failure counts as missed).
* Weekends and configured holidays are exempt.
* Only read tools are used. The no-orders check is static, like the hedge and CSP tests.
"""

from __future__ import annotations

import ast
import re
from datetime import date
from pathlib import Path

import pytest

dca_goal_snapshot = pytest.importorskip(
    "dca_goal_snapshot", reason="dca_goal_snapshot.py lands with Phase 2 (ADR tests 22-24)"
)

REPO_ROOT = Path(__file__).resolve().parents[1]
SNAPSHOT_MODULE = REPO_ROOT / "dca_goal_snapshot.py"
READ_ONLY_TOOLS = {"get_equity_positions", "get_equity_historicals", "get_portfolio"}
ORDER_TOOL = re.compile(
    r"(?<![A-Za-z0-9_])(place|cancel|review|preview|exercise|decline|accept)_(equity|option|crypto|advanced|order|trade)"
)

FRIDAY = date(2026, 11, 13)  # previous trading day for a Monday 07:31 run
MONDAY = date(2026, 11, 16)


def _is_trading_day(day: date) -> bool:
    return day.weekday() < 5


def _positions(day):
    return [{"symbol": "ZZZA", "quantity": 2.0}]


def _prior_close(symbol, day):
    return 10.0


def _raises(*_):
    raise TimeoutError("read tool timed out")


# ---- capture: one row per day, or an alert ----------------------------------------------------

def test_capture_writes_one_row_per_position_for_a_trading_day(tmp_path):
    result = dca_goal_snapshot.capture_previous_close(
        FRIDAY, positions_fn=_positions, prior_close_fn=_prior_close, is_trading_day=_is_trading_day)
    assert len(result.rows) == 1
    row = result.rows[0]
    assert row["snapshot_day"] == FRIDAY.isoformat()
    assert row["prior_close"] == 10.0
    assert row["market_value"] == 20.0
    assert result.alerts == []


def test_a_failed_read_is_a_missed_snapshot_with_no_row():
    result = dca_goal_snapshot.capture_previous_close(
        FRIDAY, positions_fn=_raises, prior_close_fn=_prior_close, is_trading_day=_is_trading_day)
    assert result.rows == []
    assert [a.kind for a in result.alerts] == ["missed_snapshot"]


def test_a_failed_price_read_is_a_missed_snapshot_with_no_partial_rows():
    result = dca_goal_snapshot.capture_previous_close(
        FRIDAY, positions_fn=_positions, prior_close_fn=_raises, is_trading_day=_is_trading_day)
    assert result.rows == []
    assert [a.kind for a in result.alerts] == ["missed_snapshot"]


def test_an_empty_positions_read_on_a_trading_day_is_still_a_row_per_day_not_silence():
    # Zero holdings is a real state (a fully sold account), not a missed read.
    result = dca_goal_snapshot.capture_previous_close(
        FRIDAY, positions_fn=lambda day: [], prior_close_fn=_prior_close, is_trading_day=_is_trading_day)
    assert result.rows == [{"snapshot_day": FRIDAY.isoformat(), "symbol": None, "quantity": 0.0,
                            "close_day": None, "prior_close": None, "market_value": 0.0}]
    assert result.alerts == []


def test_a_weekend_or_holiday_with_no_read_is_exempt():
    result = dca_goal_snapshot.capture_previous_close(
        date(2026, 11, 14), positions_fn=_raises, prior_close_fn=_prior_close, is_trading_day=_is_trading_day)
    assert result.rows == []
    assert result.alerts == []


def test_snapshot_rows_carry_no_account_identifier():
    result = dca_goal_snapshot.capture_previous_close(
        FRIDAY, positions_fn=_positions, prior_close_fn=_prior_close, is_trading_day=_is_trading_day)
    for row in result.rows:
        assert not any("account" in key.lower() for key in row)


# ---- previous trading day ---------------------------------------------------------------------

def test_previous_trading_day_skips_weekends():
    assert dca_goal_snapshot.previous_trading_day(MONDAY, _is_trading_day) == FRIDAY


def test_previous_trading_day_skips_a_holiday_with_the_real_calendar():
    # 2026-11-26 is Thanksgiving (config/market_calendar.json). Friday 11-27 looks back to Wednesday 11-25.
    assert dca_goal_snapshot.previous_trading_day(date(2026, 11, 27), None) == date(2026, 11, 25)


# ---- read-only boundary -----------------------------------------------------------------------

def test_module_exists():
    assert SNAPSHOT_MODULE.exists()


def test_module_calls_only_the_read_only_robinhood_tools():
    text = SNAPSHOT_MODULE.read_text(encoding="utf-8")
    named = set(re.findall(r"\bget_[a-z_]+\b", text))
    assert named <= READ_ONLY_TOOLS, f"non-allowlisted read tools: {sorted(named - READ_ONLY_TOOLS)}"


def test_module_names_no_order_preview_or_review_tool():
    text = SNAPSHOT_MODULE.read_text(encoding="utf-8")
    assert [m.group(0) for m in ORDER_TOOL.finditer(text)] == []


def test_module_imports_no_broker_network_or_mcp_client():
    tree = ast.parse(SNAPSHOT_MODULE.read_text(encoding="utf-8"))
    names: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            names.update(a.name for a in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            names.add(node.module)
    bad = sorted(n for n in names if re.search(r"robinhood|broker|mcp|requests|urllib|http|socket|httpx", n, re.I))
    assert bad == []
