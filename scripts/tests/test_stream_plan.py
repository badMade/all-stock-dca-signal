"""Pending tests (TDD, approval pending): compute on the stream Gate 5 path, plus the sandbox bundle.

WHY
---
``backup_planner compute`` sizes from quotes alone, so ``finalize`` marks its plans SHADOW_ONLY: it skips
the stream generator's 60-minute trend and data-confidence gates (ADR gap 2). The backup therefore buys
nothing when the Mac is off. These tests pin the replacement:

1. ``scripts/stream_plan.py`` -- sizes a plan from ``scripts.stream_candidates.build_document`` (the same
   code the primary slots' Gate 5 runs), so a ticker whose trend cannot be validated is not planned.
2. ``backup_planner compute --source stream`` -- the CLI the prompts call. A plan it writes carries
   generator ``backup_stream``, which ``finalize`` may place; the old quote-only ``backup`` plan stays
   SHADOW_ONLY.
3. ``scripts/build_backup_bundle.py`` -- a deterministic, stdlib-only zipapp (``dist/backup_planner.pyz``)
   so the Perplexity sandbox, which has no repo, runs the SAME code when the Mac is off.

Scope: the six executor-class slots (0935, 1030, 1130, 1230, 1330, 1430). The 1530 slot uses
``near_close_dca.evaluate_source_signal`` (not a StreamSpec) and is out of scope: it must fail closed.

The market-data payload is the one every ``generate_*_candidates`` CLI takes
(``{"sources": {"finance": {...}}, ...}``); fixtures mirror ``test_generate_midday_candidates.py``.
"""

from __future__ import annotations

import importlib.util
import json
import os
import subprocess
import sys
import zipfile
from datetime import date, datetime, time, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest

missing = [name for name in ("scripts.build_backup_bundle", "scripts.stream_plan") if importlib.util.find_spec(name) is None]
if len(missing) == 2:
    pytest.skip(
        f"TDD implementation approval pending: {', '.join(missing)}",
        allow_module_level=True,
    )
if missing:
    raise ImportError(f"Missing TDD implementation modules: {', '.join(missing)}")

# Keep imports after the guard so missing TDD modules produce the explicit diagnostic above.
from scripts import backup_planner, build_backup_bundle, stream_plan  # noqa: E402
from scripts.generate_intraday_candidates import INTRADAY_SPECS  # noqa: E402
from scripts.generate_market_open_candidates import MARKET_OPEN_SPEC  # noqa: E402
from scripts.generate_midday_candidates import MIDDAY_SPEC  # noqa: E402
from scripts.generate_morning_candidates import MORNING_SPEC  # noqa: E402

ET = ZoneInfo("America/New_York")
REPO = Path(__file__).resolve().parents[2]
DAY = date(2026, 8, 5)
PREV = date(2026, 8, 4)
SLOT = "1230"
NOW = "2026-08-05T12:31:00-04:00"
ACCOUNT = "999000111"  # synthetic; conftest assigns the same value
STREAM_SLOTS = ("0935", "1030", "1130", "1230", "1330", "1430")
SLOT_SPECS = {
    "0935": MARKET_OPEN_SPEC,
    "1030": MORNING_SPEC,
    **INTRADAY_SPECS,
    "1230": MIDDAY_SPEC,
}


# --- fixtures ---------------------------------------------------------------------------------


def _bar(moment, price, low=None):
    return {"timestamp": moment.isoformat(), "open": price, "high": price + 0.01,
            "low": price if low is None else low, "close": price, "volume": 1000.0}


def _falling(early, late, *, start=time(9, 30)):
    bars, cur = [], datetime.combine(DAY, start, tzinfo=ET)
    stop, step = datetime.combine(DAY, time(12, 30), tzinfo=ET), datetime.combine(DAY, time(12, 0), tzinfo=ET)
    while cur <= stop:
        bars.append(_bar(cur, late if cur >= step else early))
        cur += timedelta(minutes=15)
    return bars


def _ticker(early, late, prev_close, prev_low, *, start=time(9, 30)):
    return {"previous_close_bar": _bar(datetime.combine(PREV, time(16, 0), tzinfo=ET), prev_close, prev_low),
            "intraday_bars": _falling(early, late, start=start)}


def _market(**per_ticker):
    return {"sources": {"finance": per_ticker}, "source_errors": [], "ticker_errors": {"finance": []}}


def _write(tmp_path, name, obj):
    path = tmp_path / name
    path.write_text(json.dumps(obj))
    return str(path)


def _pages(rows=()):
    return {"account_number": ACCOUNT, "trading_day": DAY.isoformat(), "pages": [{"orders": list(rows)}]}


def _compute(tmp_path, capsys, market, *, rows=(), bp="1000", deployed="0", slot=SLOT, source="stream"):
    argv = ["compute", "--source", source, "--trading-day", DAY.isoformat(), "--slot", slot,
            "--market-data", _write(tmp_path, "market.json", market),
            "--orders-pages", _write(tmp_path, "pages.json", _pages(rows)),
            "--buying-power", bp, "--now", NOW, "--deployed-last-7d-usd", deployed]
    code = backup_planner.main(argv)
    return code, json.loads(capsys.readouterr().out)


# A 3% drop with a validated falling trend: MU is in the live universe.
FALLING_MU = _ticker(early=102.0, late=98.0, prev_close=101.0, prev_low=100.0)
# Same drop but bars start at 12:00: the 60-minute trend window is not covered, so no auto-buy.
TRUNCATED_MU = _ticker(early=102.0, late=98.0, prev_close=101.0, prev_low=100.0, start=time(12, 0))
FLAT_MU = _ticker(early=101.0, late=101.0, prev_close=101.0, prev_low=100.0)


# --- 1. slot registry ---------------------------------------------------------------------------


@pytest.mark.parametrize("slot", STREAM_SLOTS)
def test_every_executor_class_slot_has_a_stream_spec(slot):
    assert stream_plan.spec_for_slot(slot) is SLOT_SPECS[slot]


@pytest.mark.parametrize("slot", ["1530", "0000", "1230ET", ""])
def test_the_final_slot_and_unknown_slots_fail_closed(slot):
    with pytest.raises(stream_plan.UnsupportedSlot):
        stream_plan.spec_for_slot(slot)


# --- 2. sizing comes from the stream Gate 5 path ------------------------------------------------


def test_a_validated_falling_ticker_is_planned_with_the_stream_generator_tag(tmp_path, capsys):
    code, report = _compute(tmp_path, capsys, _market(MU=FALLING_MU))
    assert (code, report["status"]) == (0, "VALID_PLAN")
    plan = report["plan"]
    assert plan["generator"] == "backup_stream"
    assert [o["symbol"] for o in plan["orders"]] == ["MU"]
    order = plan["orders"][0]
    assert order["ref_id"] == "dca-runner-1230-2026-08-05-MU"
    assert 0 < order["notional_usd"] <= 16.0


def test_plan_notional_equals_the_primary_gate5_cands_row(tmp_path, capsys):
    """No second implementation: the plan's amount is exactly what the primary's Gate 5 would carry."""
    document = stream_plan.gate5_document(slot=SLOT, trading_day=DAY, market_data=_market(MU=FALLING_MU),
                                          config_dir=REPO / "config", deployed_last_7d_usd=0.0)
    expected = {row["ticker"]: row["desired_usd"] for row in document["cands"]}
    code, report = _compute(tmp_path, capsys, _market(MU=FALLING_MU))
    assert {o["symbol"]: o["notional_usd"] for o in report["plan"]["orders"]} == expected


def test_a_ticker_whose_trend_cannot_be_validated_is_not_planned(tmp_path, capsys):
    code, report = _compute(tmp_path, capsys, _market(MU=TRUNCATED_MU))
    assert code == 0
    assert report["plan"]["orders"] == []  # quote-only compute would have planned MU; the stream path must not
    assert ["MU", "manual_review_candidate"] in [list(s) for s in report["skipped"]]


def test_a_flat_tape_plans_nothing_and_is_not_an_error(tmp_path, capsys):
    code, report = _compute(tmp_path, capsys, _market(MU=FLAT_MU))
    assert (code, report["status"], report["plan"]["orders"]) == (0, "VALID_PLAN", [])


def test_the_deployment_figure_is_required_on_slots_whose_gate5_requires_it(tmp_path, capsys):
    market = _write(tmp_path, "market.json", _market(MU=FALLING_MU))
    pages = _write(tmp_path, "pages.json", _pages())
    code = backup_planner.main(["compute", "--source", "stream", "--trading-day", DAY.isoformat(), "--slot", SLOT,
                                "--market-data", market, "--orders-pages", pages, "--buying-power", "1000",
                                "--now", NOW])
    report = json.loads(capsys.readouterr().out)
    assert code == 2 and report["plan"] is None
    assert any(e["code"] == "deployment_figure_required" for e in report["errors"])


def test_a_source_error_plans_nothing(tmp_path, capsys):
    market = _market(MU=FALLING_MU)
    market["source_errors"] = ["finance"]
    code, report = _compute(tmp_path, capsys, market)
    assert code == 2 and report["plan"] is None


def test_the_final_slot_is_refused_by_the_cli(tmp_path, capsys):
    code, report = _compute(tmp_path, capsys, _market(MU=FALLING_MU), slot="1530")
    assert code == 2 and report["plan"] is None
    assert any(e["code"] == "unsupported_slot" for e in report["errors"])


# --- 3. caps stay all-or-nothing, tickers bought today are excluded -----------------------------


def test_a_batch_that_would_breach_the_daily_cap_plans_nothing(tmp_path, capsys):
    spent = {"id": "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaa1", "ref_id": None, "symbol": "AAPL", "side": "buy",
             "type": "market", "state": "filled", "dollar_based_amount": {"amount": "179.900000", "currency_code": "USD"},
             "time_in_force": "gfd", "market_hours": "regular_hours", "trigger": "immediate",
             "placed_agent": "agentic", "created_at": f"{DAY.isoformat()}T14:00:00+00:00"}
    code, report = _compute(tmp_path, capsys, _market(MU=FALLING_MU), rows=[spent])
    assert code == 2 and report["plan"] is None
    assert any("cap" in e["code"] for e in report["errors"])


def test_insufficient_buying_power_plans_nothing(tmp_path, capsys):
    code, report = _compute(tmp_path, capsys, _market(MU=FALLING_MU), bp="0.50")
    assert code == 2 and report["plan"] is None


# --- 4. finalize may place a stream plan, but never the old quote-only plan ---------------------


def _finalize(tmp_path, capsys, plan):
    fresh = {o["symbol"]: o["decision_price"] for o in plan["orders"]}
    argv = ["finalize", "--trading-day", DAY.isoformat(), "--slot", SLOT,
            "--plan", _write(tmp_path, "plan.json", plan),
            "--orders-pages", _write(tmp_path, "pages2.json", _pages()),
            "--fresh-quotes", _write(tmp_path, "fresh.json", fresh),
            "--fresh-spreads", _write(tmp_path, "spreads.json", {s: {"bid": 99.9, "ask": 100.1} for s in fresh}),
            "--buying-power", "1000"]
    code = backup_planner.main(argv)
    return code, json.loads(capsys.readouterr().out)


def test_finalize_places_a_stream_plan(tmp_path, capsys):
    _, computed = _compute(tmp_path, capsys, _market(MU=FALLING_MU))
    code, report = _finalize(tmp_path, capsys, computed["plan"])
    assert (code, report["status"]) == (0, "PROCEED")
    assert [o["symbol"] for o in report["to_place"]] == ["MU"]


def test_finalize_still_keeps_the_quote_only_backup_plan_shadow_only(tmp_path, capsys):
    _, computed = _compute(tmp_path, capsys, _market(MU=FALLING_MU))
    legacy = dict(computed["plan"], generator="backup")
    code, report = _finalize(tmp_path, capsys, legacy)
    assert (code, report["status"], report["to_place"]) == (0, "SHADOW_ONLY", [])


def test_a_stream_plan_keeps_the_day_wide_ticker_exclusion(tmp_path, capsys):
    """A computed plan cannot resize, so it never tops up a ticker bought earlier today."""
    _, computed = _compute(tmp_path, capsys, _market(MU=FALLING_MU))
    earlier = {"id": "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaa2", "ref_id": None, "symbol": "MU", "side": "buy",
               "type": "market", "state": "filled", "dollar_based_amount": {"amount": "2.000000", "currency_code": "USD"},
               "time_in_force": "gfd", "market_hours": "regular_hours", "trigger": "immediate",
               "placed_agent": "agentic", "created_at": f"{DAY.isoformat()}T13:35:00+00:00"}
    plan = computed["plan"]
    argv = ["finalize", "--trading-day", DAY.isoformat(), "--slot", SLOT,
            "--plan", _write(tmp_path, "plan.json", plan),
            "--orders-pages", _write(tmp_path, "pages3.json", _pages([earlier])),
            "--fresh-quotes", _write(tmp_path, "fresh.json", {"MU": plan["orders"][0]["prior_close"] * 0.99}),
            "--fresh-spreads", _write(tmp_path, "spreads.json", {"MU": {"bid": 99.9, "ask": 100.1}}),
            "--buying-power", "1000"]
    backup_planner.main(argv)
    report = json.loads(capsys.readouterr().out)
    assert report["to_place"] == []


# --- 5. the sandbox bundle ----------------------------------------------------------------------


@pytest.fixture(scope="module")
def bundle(tmp_path_factory):
    out = tmp_path_factory.mktemp("bundle") / "backup_planner.pyz"
    build_backup_bundle.build(out)
    return out


def test_the_build_is_byte_deterministic(bundle, tmp_path):
    again = tmp_path / "again.pyz"
    build_backup_bundle.build(again)
    assert again.read_bytes() == bundle.read_bytes()


def test_the_bundle_holds_only_repo_python_and_config_and_no_secrets(bundle):
    with zipfile.ZipFile(bundle) as zf:
        names = zf.namelist()
        blob = b"".join(zf.read(n) for n in names)
    assert all(n.endswith((".py", ".json")) for n in names)
    assert not any(n.startswith(("tests/", ".env", "cron_tracking/")) or "/.env" in n for n in names)
    assert ACCOUNT.encode() not in blob
    assert bundle.stat().st_size < 6_000_000


def test_the_bundle_is_stamped_with_commit_and_content_hash(bundle):
    with zipfile.ZipFile(bundle) as zf:
        stamp = json.loads(zf.read("BUNDLE_STAMP.json"))
    assert set(stamp) >= {"source_commit", "content_sha256", "config_sha256"}
    assert len(stamp["content_sha256"]) == 64


def _run_bundle(bundle, tmp_path, market, pages):
    env = {k: v for k, v in os.environ.items() if k not in ("PYTHONPATH", "DCA_CONFIG_DIR", "DCA_CRON_TRACKING_ROOT")}
    env["DCA_DISABLE_DOTENV"] = "1"
    return subprocess.run(
        [sys.executable, "-I", str(bundle), "compute", "--source", "stream", "--trading-day", DAY.isoformat(),
         "--slot", SLOT, "--market-data", market, "--orders-pages", pages, "--buying-power", "1000",
         "--now", NOW, "--deployed-last-7d-usd", "0"],
        cwd=tmp_path, env=env, capture_output=True, text=True, timeout=120,
    )


def test_the_bundle_runs_without_the_repo_and_matches_the_repo_planner_byte_for_byte(bundle, tmp_path, capsys):
    market = _write(tmp_path, "market.json", _market(MU=FALLING_MU))
    pages = _write(tmp_path, "pages.json", _pages())
    proc = _run_bundle(bundle, tmp_path, market, pages)
    assert proc.returncode == 0, proc.stderr
    code, repo_report = _compute(tmp_path, capsys, _market(MU=FALLING_MU))
    assert code == 0
    assert json.loads(proc.stdout) == repo_report


def test_the_bundle_fails_closed_on_a_bad_payload_with_exit_2(bundle, tmp_path):
    market = _write(tmp_path, "market.json", {"sources": {}})
    pages = _write(tmp_path, "pages.json", _pages())
    proc = _run_bundle(bundle, tmp_path, market, pages)
    assert proc.returncode == 2
    assert json.loads(proc.stdout)["plan"] is None


def test_check_flags_drift_and_the_committed_bundle_is_current(bundle, tmp_path):
    assert build_backup_bundle.check(bundle) is False  # a fresh build has not drifted
    tampered = tmp_path / "tampered.pyz"
    with zipfile.ZipFile(bundle) as src, zipfile.ZipFile(tampered, "w") as dst:
        for info in src.infolist():
            data = src.read(info.filename)
            dst.writestr(info, data + (b"\n# drift" if info.filename == "dca_order_plan.py" else b""))
    assert build_backup_bundle.check(tampered) is True
    committed = REPO / "dist" / "backup_planner.pyz"
    assert committed.is_file() and build_backup_bundle.check(committed) is False
