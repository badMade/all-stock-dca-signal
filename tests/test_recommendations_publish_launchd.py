"""The launchd agent that runs ``scripts.snapshot_cycle publish --notify`` on the Mac every 5 minutes.

Pinned to sit beside the other agents in ``deploy/launchd/``: same placeholder convention, same venv-Python launch,
logs under /tmp. It exists because the courier session cannot push the ``recommendations`` branch (no GitHub
credentials in a scheduled session; the branch was a 404 on 2026-10-26), so the push moved to the operator's login.
"""

from __future__ import annotations

import plistlib
import re
from pathlib import Path

TEMPLATE = (
    Path(__file__).resolve().parent.parent
    / "deploy/launchd/com.all-stock-dca.recommendations-publish.plist.template"
)


def _load() -> dict:
    text = TEMPLATE.read_text(encoding="utf-8")
    text = text.replace("REPLACE_WITH_REPO_PATH", "/repo").replace("REPLACE_WITH_CRON_TRACKING_ROOT", "/cron")
    assert "REPLACE_WITH_" not in text, "unknown placeholder left in the template"
    return plistlib.loads(text.encode("utf-8"))


def test_label_and_command_run_publish_with_notification_from_the_checkout():
    agent = _load()
    assert agent["Label"] == "com.all-stock-dca.recommendations-publish"
    assert agent["ProgramArguments"] == ["/repo/.venv/bin/python", "-m", "scripts.snapshot_cycle", "publish", "--notify"]
    assert agent["WorkingDirectory"] == "/repo"


def test_reads_the_same_store_root_the_courier_writes():
    assert _load()["EnvironmentVariables"]["DCA_CRON_TRACKING_ROOT"] == "/cron"


def test_runs_at_the_couriers_cadence():
    agent = _load()
    assert agent["StartInterval"] == 300
    assert agent["RunAtLoad"] is True


def test_template_holds_no_secret_or_account_shaped_literal():
    text = TEMPLATE.read_text(encoding="utf-8")
    assert not re.search(r"\d{8,}", text)
    assert "ROBINHOOD_AGENTIC_ACCOUNT_NUMBER" not in text


def test_logs_go_to_tmp_like_the_other_agents():
    agent = _load()
    assert agent["StandardOutPath"].startswith("/tmp/")
    assert agent["StandardErrorPath"] == agent["StandardOutPath"]
