"""The plan-first half of scripts/robinhood_payload.py (2026-10-26): bid/ask, the broker worker's prompt and reply, ingest,
and the launcher step that runs them after the payload merge. Replaces the retired Claude courier's duties."""

import json
import os
import subprocess
import sys
from datetime import datetime, timedelta
from pathlib import Path

import pytest

from scripts import robinhood_payload as rp
from test_robinhood_payload import DAY, good_ticker, kw, plan_for, write_chunk

REPO = Path(__file__).resolve().parents[1]
LAUNCHER = REPO / "deploy/launchd/robinhood_payload.sh"


# -- bid / ask on the quote ---------------------------------------------------------------------------------------------


def test_bid_and_ask_are_kept_when_present_and_sane():
    raw = good_ticker()
    raw["quote"].update(bid_price=10.09, ask_price=10.11)
    quote = rp.validate_ticker(raw, **kw())["quote"]
    assert (quote["bid_price"], quote["ask_price"]) == (10.09, 10.11)


def test_a_quote_without_bid_and_ask_is_unchanged():
    quote = rp.validate_ticker(good_ticker(), **kw())["quote"]
    assert set(quote) == {"price", "as_of"}


def test_an_ask_below_its_bid_fails_the_ticker():
    raw = good_ticker()
    raw["quote"].update(bid_price=10.2, ask_price=10.1)
    with pytest.raises(rp.PayloadError, match="below its bid"):
        rp.validate_ticker(raw, **kw())


def test_only_one_side_present_fails_the_ticker_rather_than_being_dropped():
    raw = good_ticker()
    raw["quote"].update(bid_price=10.09)
    with pytest.raises(rp.PayloadError):
        rp.validate_ticker(raw, **kw())


# -- the broker worker's prompt -----------------------------------------------------------------------------------------


def test_broker_prompt_names_only_the_two_read_tools_and_the_account(tmp_path):
    prompt = rp.build_broker_prompt(plan_for(tmp_path, ["AAA"]), "999000111")
    assert "get_equity_orders" in prompt and "get_portfolio" in prompt
    assert "Never call any order, cancel" in prompt
    assert '"999000111"' in prompt


def test_broker_prompt_account_comes_from_the_environment_never_a_literal(monkeypatch):
    monkeypatch.setenv("ROBINHOOD_AGENTIC_ACCOUNT_NUMBER", "999000111")
    assert rp._account_number() == "999000111"
    source = Path(rp.__file__).read_text(encoding="utf-8")
    assert "999000111" not in source


# -- the broker worker's reply -------------------------------------------------------------------------------------------


def test_an_error_reply_is_refused(tmp_path):
    path = write_chunk(tmp_path, "broker.json", {"error": "tool failed"})
    with pytest.raises(rp.PayloadError, match="broker worker reported"):
        rp.load_broker_reply(path)


def test_a_reply_with_no_order_pages_is_refused(tmp_path):
    path = write_chunk(tmp_path, "broker.json", {"order_pages": [], "portfolio": {}})
    with pytest.raises(rp.PayloadError, match="no order pages"):
        rp.load_broker_reply(path)


def test_a_good_reply_returns_normalised_pages_and_portfolio(tmp_path):
    path = write_chunk(tmp_path, "broker.json", {"order_pages": [{"orders": []}], "portfolio": {"equity": "1"}})
    pages, portfolio = rp.load_broker_reply(path)
    assert len(pages) == 1 and isinstance(pages[0], dict) and isinstance(portfolio, dict)


# -- ingest --------------------------------------------------------------------------------------------------------------


def _published(tmp_path, *, built_minutes_ago=1):
    """A real published payload for the 14:30 slot, plus a fresh broker reply file beside it."""
    tickers = [f"T{i}" for i in range(10)]
    plan = plan_for(tmp_path, tickers)
    chunk = write_chunk(tmp_path, "chunk_0.json", {t: good_ticker() for t in tickers})
    payload, failed = rp.assemble(plan, [chunk])
    assert rp.publish(plan, payload, failed, min_coverage=0.95) == 0
    reply = write_chunk(tmp_path, "broker.json", {"order_pages": [{"orders": []}], "portfolio": {}})
    return plan, reply


def test_ingest_refuses_a_missing_payload(tmp_path):
    plan = plan_for(tmp_path, ["AAA"])
    reply = write_chunk(tmp_path, "broker.json", {"order_pages": [{"orders": []}], "portfolio": {}})
    assert rp.ingest(plan, reply) != 0


def test_ingest_exits_6_for_a_stale_payload(tmp_path, monkeypatch):
    plan, reply = _published(tmp_path)
    monkeypatch.setenv("ROBINHOOD_AGENTIC_ACCOUNT_NUMBER", "999000111")
    later = datetime.now(rp.ET) + timedelta(hours=3)
    assert rp.ingest(plan, reply, now=later, max_age_minutes=15) == rp.EXIT_STALE


def test_ingest_refuses_a_broker_reply_far_from_now(tmp_path, monkeypatch):
    plan, reply = _published(tmp_path)
    monkeypatch.setenv("ROBINHOOD_AGENTIC_ACCOUNT_NUMBER", "999000111")
    old = datetime.now().timestamp() - 3600
    os.utime(reply, (old, old))
    assert rp.ingest(plan, reply, max_age_minutes=600) == 2


def test_ingest_exits_3_when_another_ingest_holds_the_lease(tmp_path, monkeypatch):
    from scripts.scheduler_lease import SchedulerLease, window_key

    plan, reply = _published(tmp_path)
    monkeypatch.setenv("ROBINHOOD_AGENTIC_ACCOUNT_NUMBER", "999000111")
    monkeypatch.setenv("DCA_CRON_TRACKING_ROOT", str(tmp_path / "ct"))
    import importlib
    import dca_paths

    importlib.reload(dca_paths)
    lease = SchedulerLease(
        Path(dca_paths.cron_tracking_root()) / "scheduler_leases", window_key(DAY.isoformat(), "plan_first", "ingest")
    )
    assert getattr(lease.acquire("someone-else"), "acquired", True)
    assert rp.ingest(plan, reply, max_age_minutes=600) == 3


# -- the launcher step ---------------------------------------------------------------------------------------------------


def test_launcher_runs_no_broker_step_unless_opted_in():
    text = LAUNCHER.read_text(encoding="utf-8")
    assert 'DCA_PLAN_FIRST_INGEST' in text
    assert 'broker-prompt' in text and ' ingest ' in text


def test_a_failing_broker_or_ingest_step_never_changes_the_launchers_exit_status():
    """The payload's own merge status is the launcher's; the plan-first step is best-effort after it."""
    text = LAUNCHER.read_text(encoding="utf-8")
    assert text.rstrip().endswith('exit "$MERGE_STATUS"')
    step = text[text.index("DCA_PLAN_FIRST_INGEST"):]
    assert "|| echo" in step or "|| true" in step


def test_no_courier_schedule_or_snapshot_cron_remains():
    templates = sorted(p.name for p in (REPO / "deploy/launchd").glob("*.plist.template"))
    assert not any("recommendations-publish" in name for name in templates)
    assert (REPO / "deploy/launchd/com.all-stock-dca.robinhood-payload.plist.template").exists()
