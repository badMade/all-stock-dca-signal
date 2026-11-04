"""A refused payload build raises a macOS notification (opt-in), so the operator learns of it when it happens.

2026-11-02: five of seven payload builds were refused (quiet cap, coverage floor) and each one only showed up in a
launchd log and in the slot's Gate 5 abort, long after the build. With the liquidity split (a liquid name with no bars
now counts against the coverage floor) a refusal is more likely, so the publisher says so out loud.

Contract pinned here, before the implementation exists:

* Only a *refusal* notifies (``publish`` returning 2 after ``_record_refusal``); a published payload never does.
* Opt-in: nothing is raised unless ``DCA_NOTIFY_REFUSAL=1``. The suite and every direct caller stay silent on a Mac;
  the launcher exports the variable on by default (``deploy/launchd/robinhood_payload.sh``).
* Best-effort like the marker: a notifier that is missing, fails or hangs never changes the exit status (2), never
  stops the marker from being written, and is reported on stderr without the message text.
* Reuses ``schedule_watchdog.notify_macos`` (text as argv, never interpolated into AppleScript); no second notifier.
"""

import json
import subprocess
from datetime import datetime
from pathlib import Path

import pytest
from test_robinhood_payload import universe_plan

from scripts import robinhood_payload as rp
from scripts import schedule_watchdog

SCRIPT = Path(__file__).resolve().parent.parent / "deploy/launchd/robinhood_payload.sh"
BUILT = datetime(2026, 10, 15, 14, 31, tzinfo=rp.ET)  # one minute after the 14:30 anchor, as launchd builds it


@pytest.fixture
def notified(monkeypatch):
    calls = []
    monkeypatch.setattr(schedule_watchdog, "notify_macos", lambda title, message: calls.append((title, message)))
    monkeypatch.setenv("DCA_NOTIFY_REFUSAL", "1")
    return calls


def refuse(tmp_path, *, errored=12, now=BUILT):
    """Publish a 90-ticker build with ``errored`` failures: 12 is under the 95% coverage floor (78/90)."""
    plan, _, chunk = universe_plan(tmp_path, errored=errored)
    payload, failed = rp.assemble(plan, [chunk])
    return plan, rp.publish(plan, payload, failed, min_coverage=rp.DEFAULT_MIN_COVERAGE, now=now)


def test_a_coverage_refusal_raises_one_notification_naming_the_slot_and_the_counts(tmp_path, notified):
    plan, code = refuse(tmp_path)

    assert code == 2
    assert len(notified) == 1
    title, message = notified[0]
    assert "1430" in title and "refused" in title.lower()
    assert "78/90" in message  # valid of universe, as the marker records them
    assert "coverage" in message  # the reason, not just the fact


def test_a_build_before_the_anchor_notifies_too(tmp_path, notified):
    early = datetime(2026, 10, 15, 14, 20, tzinfo=rp.ET)
    _, code = refuse(tmp_path, errored=0, now=early)

    assert code == 2
    assert len(notified) == 1
    assert "anchor" in notified[0][1]


def test_the_message_is_short_and_lists_at_most_a_handful_of_failed_tickers(tmp_path, notified):
    refuse(tmp_path, errored=40)

    _, message = notified[0]
    assert len(message) <= 250
    assert "more" in message  # 40 failures cannot all fit; the count of the rest does


def test_a_published_payload_never_notifies(tmp_path, notified):
    plan, code = refuse(tmp_path, errored=2)  # 88/90 = 97.8%, above the floor

    assert code == 0
    assert notified == []


@pytest.mark.parametrize("value", [None, "0", "", "no"])
def test_it_is_silent_unless_explicitly_enabled(tmp_path, monkeypatch, value):
    calls = []
    monkeypatch.setattr(schedule_watchdog, "notify_macos", lambda title, message: calls.append(title))
    if value is None:
        monkeypatch.delenv("DCA_NOTIFY_REFUSAL", raising=False)
    else:
        monkeypatch.setenv("DCA_NOTIFY_REFUSAL", value)

    _, code = refuse(tmp_path)

    assert code == 2
    assert calls == []


@pytest.mark.parametrize(
    "error",
    [FileNotFoundError("osascript"), subprocess.CalledProcessError(1, "osascript"), subprocess.TimeoutExpired("osascript", 10)],
    ids=lambda e: type(e).__name__,
)
def test_a_failing_notifier_changes_neither_the_exit_status_nor_the_marker(tmp_path, monkeypatch, capsys, error):
    def boom(title, message):
        raise error

    monkeypatch.setattr(schedule_watchdog, "notify_macos", boom)
    monkeypatch.setenv("DCA_NOTIFY_REFUSAL", "1")

    plan, code = refuse(tmp_path)

    assert code == 2
    marker = json.loads(rp._refusal_path(Path(plan["out"])).read_text(encoding="utf-8"))
    assert marker["valid"] == 78
    err = capsys.readouterr().err
    assert "could not raise the refusal notification" in err
    assert "coverage" not in err.split("could not raise the refusal notification")[1]  # no message text in the log note


def test_the_marker_is_written_before_the_notifier_runs(tmp_path, monkeypatch):
    seen = []

    def check_marker(title, message):
        seen.append(rp._refusal_path(Path(plan_holder["plan"]["out"])).exists())

    plan_holder = {}
    plan, _, chunk = universe_plan(tmp_path, errored=12)
    plan_holder["plan"] = plan
    monkeypatch.setattr(schedule_watchdog, "notify_macos", check_marker)
    monkeypatch.setenv("DCA_NOTIFY_REFUSAL", "1")
    payload, failed = rp.assemble(plan, [chunk])
    rp.publish(plan, payload, failed, min_coverage=rp.DEFAULT_MIN_COVERAGE, now=BUILT)

    assert seen == [True]


def test_the_launcher_turns_the_notification_on_by_default_but_lets_the_operator_turn_it_off():
    text = SCRIPT.read_text(encoding="utf-8")

    assert 'export DCA_NOTIFY_REFUSAL="${DCA_NOTIFY_REFUSAL:-1}"' in text
