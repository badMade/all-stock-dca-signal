"""The launchd agent that runs ticker-advisor/daily_universe_job.py once each weekday evening (step 7b-2).

Written BEFORE the template exists (TDD). The agent applies the daily buy list to this checkout, so it must pass
``--apply`` explicitly (the job is propose-only without it) and carry no secret.
"""

from __future__ import annotations

import plistlib
import re
from pathlib import Path

TEMPLATE = Path(__file__).resolve().parent.parent / "deploy/launchd/com.all-stock-dca.universe-daily-ranking.plist.template"
PLACEHOLDERS = {
    "REPLACE_WITH_REPO_PATH": "/repo",
    "REPLACE_WITH_SNAPSHOT_FILE": "/data/fundamentals_snapshot.json",
    "REPLACE_WITH_PRICE_ARCHIVE_CSV": "/data/prices.csv",
    "REPLACE_WITH_EXCHANGES_FILE": "/data/exchanges.json",
    "REPLACE_WITH_OUT_DIR": "/data/universe_out",
}


def _load() -> dict:
    text = TEMPLATE.read_text(encoding="utf-8")
    for placeholder, value in PLACEHOLDERS.items():
        text = text.replace(placeholder, value)
    assert "REPLACE_WITH_" not in text, "the template names a placeholder this test does not know"
    return plistlib.loads(text.encode("utf-8"))


def test_runs_the_job_through_the_checkout_venv_and_applies_to_this_checkouts_files():
    agent = _load()
    assert agent["Label"] == "com.all-stock-dca.universe-daily-ranking"
    args = agent["ProgramArguments"]
    assert args[:2] == ["/repo/.venv/bin/python", "/repo/ticker-advisor/daily_universe_job.py"]
    assert "--apply" in args
    option = {args[i]: args[i + 1] for i, a in enumerate(args) if a.startswith("--") and i + 1 < len(args)}
    assert option["--tickers-config"] == "/repo/config/tickers.json"
    assert option["--history"] == "/repo/config/ticker_history.csv"
    assert option["--snapshot"] == "/data/fundamentals_snapshot.json"
    assert option["--prices"] == "/data/prices.csv" and option["--out-dir"] == "/data/universe_out"


def test_works_from_the_ticker_advisor_directory_and_logs_to_tmp():
    agent = _load()
    assert agent["WorkingDirectory"] == "/repo/ticker-advisor"
    assert agent["StandardOutPath"].startswith("/tmp/") and agent["StandardErrorPath"].startswith("/tmp/")


def test_fires_each_weekday_after_the_close_and_the_data_refreshes():
    entries = _load()["StartCalendarInterval"]
    assert sorted(e["Weekday"] for e in entries) == [1, 2, 3, 4, 5]
    assert all(e["Hour"] * 60 + e["Minute"] >= 19 * 60 for e in entries)  # after the 18:30 archive and snapshot jobs


def test_the_template_says_the_job_changes_the_buy_list_only_and_sells_nothing():
    text = TEMPLATE.read_text(encoding="utf-8").lower()
    assert "buy list" in text and "never sells" in text


def test_holds_no_secret_literal():
    text = TEMPLATE.read_text(encoding="utf-8")
    assert not re.search(r"\d{6,}", text)
    assert "ROBINHOOD_AGENTIC_ACCOUNT_NUMBER" not in text and "SEC_EDGAR_USER_AGENT" not in text
