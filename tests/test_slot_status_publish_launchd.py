"""The launchd agent that pushes the slot-status document from the Mac (2026-11-04).

2026-11-03: the live runs recorded every slot's status, but they run in a Cowork bridge VM with no GitHub
credentials, so no push reached the public signal repository. This agent runs ``scripts.slot_status publish`` on the
Mac itself: when the document's folder changes (a run just finalized), and every few minutes as a fallback. Same
placeholder convention and venv-Python launch as the other agents in deploy/launchd/.
"""

from __future__ import annotations

import plistlib
import re
from pathlib import Path

TEMPLATE = (
    Path(__file__).resolve().parent.parent / "deploy/launchd/com.all-stock-dca.slot-status-publish.plist.template"
)


def _load() -> dict:
    text = TEMPLATE.read_text(encoding="utf-8")
    text = text.replace("REPLACE_WITH_REPO_PATH", "/repo").replace("REPLACE_WITH_CRON_TRACKING_ROOT", "/cron")
    assert "REPLACE_WITH_" not in text, "unknown placeholder left in the template"
    return plistlib.loads(text.encode("utf-8"))


def test_label_and_command_publish_from_the_checkout():
    agent = _load()
    assert agent["Label"] == "com.all-stock-dca.slot-status-publish"
    assert agent["ProgramArguments"] == ["/repo/.venv/bin/python", "-m", "scripts.slot_status", "publish"]
    assert agent["WorkingDirectory"] == "/repo"


def test_it_wakes_when_a_run_writes_the_document_and_on_a_fallback_timer():
    agent = _load()
    # The folder, not the file: the document is replaced atomically (a rename), which a file watch can miss.
    assert agent["WatchPaths"] == ["/cron/slot_status"]
    assert 60 <= agent["StartInterval"] <= 600


def test_it_reads_the_same_cron_tree_as_the_live_runs_and_never_waits_for_a_password():
    env = _load()["EnvironmentVariables"]
    assert env["DCA_CRON_TRACKING_ROOT"] == "/cron"
    assert env["GIT_TERMINAL_PROMPT"] == "0"  # no terminal under launchd: fail, never hang on a credential prompt


def test_template_holds_no_secret_or_account_shaped_literal():
    text = TEMPLATE.read_text(encoding="utf-8")
    assert not re.search(r"\d{8,}", text)
    assert "ROBINHOOD_AGENTIC_ACCOUNT_NUMBER" not in text
    assert "ghp_" not in text and "x-access-token" not in text


def test_logs_go_to_tmp_like_the_other_agents():
    agent = _load()
    assert agent["StandardOutPath"].startswith("/tmp/") and agent["StandardErrorPath"].startswith("/tmp/")
