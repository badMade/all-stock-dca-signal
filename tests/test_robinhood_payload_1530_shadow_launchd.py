"""The shadow launchd agent that builds the 3:30 PM Robinhood payload for measurement only (2026-10-22).

Pins it next to ``com.all-stock-dca.robinhood-payload.plist.template`` (the six-slot job): same placeholder
convention and venv-Python launch, but a separate Label and log so loading it can never touch the live job, an
explicit ``1530`` slot (``auto`` never selects it), and a build deadline.
"""

from __future__ import annotations

import plistlib
import re
from pathlib import Path

from scripts import robinhood_payload as rp

DIR = Path(__file__).resolve().parent.parent / "deploy/launchd"
SHADOW = DIR / "com.all-stock-dca.robinhood-payload-1530-shadow.plist.template"
LIVE = DIR / "com.all-stock-dca.robinhood-payload.plist.template"


def _load(path: Path) -> dict:
    text = path.read_text(encoding="utf-8")
    for placeholder in ("REPLACE_WITH_REPO_PATH", "REPLACE_WITH_CRON_TRACKING_ROOT", "REPLACE_WITH_CODEX_PATH"):
        text = text.replace(placeholder, "/x")
    assert "REPLACE_WITH_" not in text, "unknown placeholder left in the template"
    return plistlib.loads(text.encode("utf-8"))


def test_shadow_agent_cannot_collide_with_the_live_six_slot_agent():
    shadow, live = _load(SHADOW), _load(LIVE)
    assert shadow["Label"] == "com.all-stock-dca.robinhood-payload-1530-shadow" != live["Label"]
    assert shadow["StandardOutPath"] != live["StandardOutPath"] and shadow["StandardOutPath"].startswith("/tmp/")
    assert shadow["StandardErrorPath"] == shadow["StandardOutPath"]


def test_shadow_agent_names_the_1530_slot_explicitly_never_auto():
    args = _load(SHADOW)["ProgramArguments"]
    assert args[0] == "/x/.venv/bin/python" and args[3] == "/x/deploy/launchd/robinhood_payload.sh"
    assert '"1530"' in args[2] and "auto" not in args[2]
    assert "1530" not in rp.SLOTS  # `auto` would never pick it, which is why the slot is named here
    assert '"auto"' in _load(LIVE)["ProgramArguments"][2]  # the live job is untouched


def test_shadow_agent_fires_one_minute_after_the_decision_time_on_weekdays_only():
    entries = _load(SHADOW)["StartCalendarInterval"]
    assert sorted(e["Weekday"] for e in entries) == [1, 2, 3, 4, 5]
    assert {(e["Hour"], e["Minute"]) for e in entries} == {(15, 31)}
    live = _load(LIVE)["StartCalendarInterval"]
    assert all((e["Hour"], e["Minute"]) != (15, 31) for e in live)  # the live job still never fires at 3:31


def test_shadow_agent_sets_a_build_deadline_that_fits_before_the_slot_window_closes():
    env = _load(SHADOW)["EnvironmentVariables"]
    deadline = int(env["DCA_BUILD_DEADLINE_SECONDS"])
    assert 240 <= deadline <= 600  # above a real 2-4 minute build, well inside the 15:25-15:55 window
    assert "DCA_BUILD_DEADLINE_SECONDS" not in _load(LIVE)["EnvironmentVariables"]
    assert env["DCA_CRON_TRACKING_ROOT"] == "/x"  # must match the live sessions


def test_shadow_template_holds_no_secret_or_account_shaped_literal():
    text = SHADOW.read_text(encoding="utf-8")
    assert not re.search(r"\d{8,}", text) and "ROBINHOOD_AGENTIC_ACCOUNT_NUMBER" not in text
