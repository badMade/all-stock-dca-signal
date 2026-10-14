"""Tests for ``scripts/emit_order_plan.py`` and its wiring into the seven live-mirror prompts.

Written test-first (Phase 4 of docs/adr-plan-first-backup-executor.md); every test here must
fail red until the script and the prompt paragraph exist.

What the script is for: the Claude slot's resolved buy list (every Gate 6.5/6.7 survivor with
``buy_usd > 0``) is written to a plan file BEFORE Gate 7 places anything, so the backup executor
can find a plan even when placement fails midway. It is an emit step only:

* it never places, cancels or modifies an order and imports no broker code;
* a failed emit writes NOTHING and reports a structured error, exit 2;
* the prompts treat a failed emit as non-blocking, because the primary's own placement never
  depends on the plan (only the backup does).
"""

from __future__ import annotations

import json
import re
from datetime import date
from pathlib import Path

import pytest

import dca_config
import dca_order_plan as dop
import dca_paths
import dca_runner
from scripts import emit_order_plan

REPO = Path(__file__).resolve().parents[2]
MIRRORS = REPO / "claude" / "dca-executor-prompts" / "live-mirror"
DAY = "2026-10-07"
SLOT = "1130"
NOW = "2026-10-07T11:29:40-04:00"


def _row(ticker="MSFT", buy_usd=4.0, multiplier=2.0, drop_pct=1.2, **over):
    row = {
        "ticker": ticker,
        "buy_usd": buy_usd,
        "multiplier": multiplier,
        "drop_pct": drop_pct,
        "current_price": 100.0,
        "prior_close": 101.2,
        "prior_low": 100.5,
        "unknown_prior_low": False,
        "selected_base_amount_usd": 2.0,
    }
    row.update(over)
    return row


@pytest.fixture
def ctx():
    config = dca_config.load_strategy_config(dca_config.get_config_dir())
    return {
        "trading_day": date.fromisoformat(DAY),
        "slot": SLOT,
        "ladder_version": dop.current_ladder_version(),
        "active_tickers": tuple(config.tickers),
        "excluded_tickers": frozenset(config.excluded_tickers),
        "max_per_order_usd": dca_runner.MAX_PER_ORDER_USD,
    }


def _run(tmp_path, capsys, rows, *, out=None, extra=(), candidates_text=None, slot=SLOT, day=DAY):
    cands = tmp_path / "buys.json"
    cands.write_text(candidates_text if candidates_text is not None else json.dumps(rows))
    argv = ["--trading-day", day, "--slot", slot, "--candidates", str(cands), "--now", NOW, *extra]
    if out is not None:
        argv += ["--out", str(out)]
    code = emit_order_plan.main(argv)
    return code, json.loads(capsys.readouterr().out)


# ---------------------------------------------------------------------------
# Behavior
# ---------------------------------------------------------------------------


def test_emit_writes_a_plan_that_the_backup_side_loader_accepts(tmp_path, capsys, ctx):
    out = tmp_path / "plans" / "order_plan_1130.json"
    code, report = _run(tmp_path, capsys, [_row("MSFT", 4.0), _row("AAPL", 2.0, 1.0, 0.7)], out=out)
    assert code == 0
    assert report["status"] == "EMITTED"
    assert report["order_count"] == 2
    assert report["path"] == str(out)
    loaded = dop.load_plan(out, **ctx)
    assert loaded.errors == ()
    assert [o["symbol"] for o in loaded.orders] == ["MSFT", "AAPL"]
    assert loaded.plan["generator"] == "claude"


def test_default_path_is_the_order_plans_tree_under_the_cron_root(tmp_path, capsys, monkeypatch):
    root = tmp_path / "cron"
    monkeypatch.setattr(dca_paths, "cron_tracking_root", lambda: str(root))
    code, report = _run(tmp_path, capsys, [_row()])
    expected = root / "order_plans" / DAY / "order_plan_1130.json"
    assert code == 0
    assert report["path"] == str(expected)
    assert expected.is_file()


def test_zero_survivors_emit_a_valid_empty_plan(tmp_path, capsys, ctx):
    out = tmp_path / "order_plan_1130.json"
    code, report = _run(tmp_path, capsys, [_row("MSFT", 0.0)], out=out)
    assert (code, report["status"], report["order_count"]) == (0, "EMITTED", 0)
    assert dop.load_plan(out, **ctx).errors == ()


def test_rerun_overwrites_and_identical_inputs_give_identical_bytes(tmp_path, capsys):
    out = tmp_path / "order_plan_1130.json"
    _run(tmp_path, capsys, [_row("MSFT", 4.0)], out=out)
    first = out.read_bytes()
    _run(tmp_path, capsys, [_row("MSFT", 4.0)], out=out)
    assert out.read_bytes() == first
    _run(tmp_path, capsys, [_row("AAPL", 2.0, 1.0, 0.7)], out=out)
    assert json.loads(out.read_bytes())["orders"][0]["symbol"] == "AAPL"
    assert sorted(p.name for p in tmp_path.iterdir() if p.name != "buys.json") == ["order_plan_1130.json"]


@pytest.mark.parametrize(
    "rows",
    [[_row("QBUT")], [_row("ZZZZ")], [_row("MSFT", 99.0)], [_row("MSFT"), _row("MSFT")], [_row("MSFT", prior_close=None)]],
)
def test_a_bad_row_writes_nothing_and_exits_two(tmp_path, capsys, rows):
    out = tmp_path / "order_plan_1130.json"
    code, report = _run(tmp_path, capsys, rows, out=out)
    assert code == 2
    assert report["status"] == "FAILED"
    assert report["errors"] and {"code", "detail"} <= set(report["errors"][0])
    assert not out.exists()


@pytest.mark.parametrize("text", ["", "{oops", "{}", '"x"', "null", '{"a": 1, "a": 2}'])
def test_corrupt_input_writes_nothing_and_exits_two(tmp_path, capsys, text):
    out = tmp_path / "order_plan_1130.json"
    code, report = _run(tmp_path, capsys, None, out=out, candidates_text=text)
    assert (code, report["status"]) == (2, "FAILED")
    assert not out.exists()


def test_a_missing_input_file_writes_nothing_and_exits_two(tmp_path, capsys):
    out = tmp_path / "order_plan_1130.json"
    code = emit_order_plan.main(
        ["--trading-day", DAY, "--slot", SLOT, "--candidates", str(tmp_path / "nope.json"), "--now", NOW,
         "--out", str(out)]
    )
    assert code == 2
    assert json.loads(capsys.readouterr().out)["status"] == "FAILED"
    assert not out.exists()


@pytest.mark.parametrize(("slot", "day"), [("9999", DAY), ("1130ET", DAY), ("", DAY), (SLOT, "10/07/2026"), (SLOT, "")])
def test_a_bad_slot_or_day_fails_closed_without_raising(tmp_path, capsys, slot, day):
    out = tmp_path / "order_plan_1130.json"
    code, report = _run(tmp_path, capsys, [_row()], out=out, slot=slot, day=day)
    assert (code, report["status"]) == (2, "FAILED")
    assert not out.exists()


def test_a_failed_emit_leaves_an_earlier_plan_untouched(tmp_path, capsys):
    out = tmp_path / "order_plan_1130.json"
    _run(tmp_path, capsys, [_row("MSFT", 4.0)], out=out)
    before = out.read_bytes()
    code, _ = _run(tmp_path, capsys, [_row("QBUT")], out=out)
    assert code == 2
    assert out.read_bytes() == before


def test_the_report_and_plan_never_contain_the_account_number(tmp_path, capsys, monkeypatch):
    monkeypatch.setenv("ROBINHOOD_AGENTIC_ACCOUNT_NUMBER", "999000111")
    out = tmp_path / "order_plan_1130.json"
    code = emit_order_plan.main(
        ["--trading-day", DAY, "--slot", SLOT, "--candidates", str(_write(tmp_path, [_row()])), "--now", NOW,
         "--out", str(out)]
    )
    assert code == 0
    assert "999000111" not in capsys.readouterr().out
    assert "999000111" not in out.read_text()


def _write(tmp_path, rows):
    path = tmp_path / "buys.json"
    path.write_text(json.dumps(rows))
    return path


def test_the_script_is_emit_only_and_carries_no_broker_or_placement_code():
    for name in ("execute_plan", "RobinhoodBroker", "place_equity_order", "place_order"):
        assert not hasattr(emit_order_plan, name), name
    source = (REPO / "scripts" / "emit_order_plan.py").read_text()
    assert "place_equity_order" not in source.replace("never places", "")
    assert "dca_live_adapters" not in source


# ---------------------------------------------------------------------------
# Wiring: every live-mirror prompt emits the plan as the FIRST thing in Gate 7
# ---------------------------------------------------------------------------

_MIRRORS = sorted(MIRRORS.glob("*ET-*.txt"))


def _gate7(text: str) -> str:
    start = text.index("GATE 7 - PLACE THE ORDERS\n")
    return text[start : text.index("GATE 8 - CONFIRM", start)]


def test_all_seven_mirrors_are_covered():
    assert len(_MIRRORS) == 7


@pytest.mark.parametrize("path", _MIRRORS, ids=lambda p: p.name[:4])
def test_gate7_opens_with_the_plan_emit_step(path):
    slot = path.name[:4]
    section = _gate7(path.read_text())
    assert section.startswith("GATE 7 - PLACE THE ORDERS\nEMIT THE ORDER PLAN"), path.name
    first_paragraph = section.split("\n")[1]
    assert "python3 -m scripts.emit_order_plan" in first_paragraph
    assert f"--slot {slot}" in first_paragraph
    assert "--trading-day <YYYY-MM-DD>" in first_paragraph
    assert re.search(r"--candidates \$ROOT/\S+", first_paragraph)


@pytest.mark.parametrize("path", _MIRRORS, ids=lambda p: p.name[:4])
def test_a_failed_emit_never_blocks_placement_and_is_reported(path):
    first_paragraph = _gate7(path.read_text()).split("\n")[1]
    assert "does NOT block" in first_paragraph
    assert "Gate 9" in first_paragraph
    assert "never evidence" in first_paragraph  # the plan is input to the backup, not proof of any purchase


@pytest.mark.parametrize("path", _MIRRORS, ids=lambda p: p.name[:4])
def test_the_emit_input_is_rejoined_from_the_cands_file_by_ticker(path):
    """Copilot review on PR 1619: the 12:30 and 15:30 survivor files omit current_price,
    multiplier and drop_pct, so every non-empty emit would fail. Each prompt must say where
    the emitter's fields come from instead of assuming the survivor row carries them."""
    slot = path.name[:4]
    first_paragraph = _gate7(path.read_text()).split("\n")[1]
    assert f"$ROOT/cands_<YYYY-MM-DD>_{slot}.json, matched BY TICKER" in first_paragraph
    for field in ("multiplier", "drop_pct", "current_price", "prior_close", "prior_low"):
        assert field in first_paragraph
    assert "never filled in by guess" in first_paragraph
