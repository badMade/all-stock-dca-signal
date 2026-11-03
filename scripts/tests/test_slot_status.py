"""scripts/slot_status.py: the "slot finished" signal a Perplexity backup reads before doing anything else.

Each Perplexity backup fire used to pay for a Mac sub-agent session (list_devices + run_local_subagent) just to learn
that Claude's slot had already succeeded (docs/cost_tracking.md, "Perplexity backup target band"). The primary now
publishes one small public document per trading day, and the backup reads it once: a succeeded slot (or a closed
market) ends the fire at once. Anything else (missing, stale, malformed, failed, in progress) falls through to the
existing full check, so a bad signal can only cost credits, never skip a safety check or add a buy.
"""

import json
from datetime import datetime
from types import SimpleNamespace
from zoneinfo import ZoneInfo

import pytest

from scripts import slot_status as ss

ET = ZoneInfo("America/New_York")
DAY = "2026-10-29"
NOW = datetime(2026, 10, 29, 9, 41, tzinfo=ET)


@pytest.fixture(autouse=True)
def _state_root(tmp_path, monkeypatch):
    monkeypatch.setattr(ss, "state_dir", lambda: tmp_path / "slot_status")


# --- document --------------------------------------------------------------------------------


def test_slots_are_the_seven_canonical_slots():
    assert ss.SLOTS == ("0935", "1030", "1130", "1230", "1330", "1430", "1530")


def test_record_starts_the_day_with_an_open_market_and_one_slot():
    doc = ss.record(DAY, "0935", "in_progress", now=NOW)
    assert doc["schema"] == ss.SCHEMA
    assert doc["trading_day"] == DAY and doc["market"] == "open"
    assert doc["slots"] == {"0935": {"status": "in_progress", "stamped_at": NOW.isoformat(timespec="seconds")}}


def test_record_merges_slots_and_a_later_status_replaces_an_earlier_one():
    ss.record(DAY, "0935", "in_progress", now=NOW)
    ss.record(DAY, "0935", "succeeded", now=NOW.replace(minute=44))
    doc = ss.record(DAY, "1030", "in_progress", now=NOW.replace(hour=10, minute=31))
    assert doc["slots"]["0935"]["status"] == "succeeded"
    assert doc["slots"]["1030"]["status"] == "in_progress"


def test_a_new_trading_day_starts_from_an_empty_document():
    ss.record(DAY, "1530", "succeeded", now=NOW)
    doc = ss.record("2026-10-30", "0935", "in_progress", now=datetime(2026, 10, 30, 9, 36, tzinfo=ET))
    assert doc["trading_day"] == "2026-10-30" and list(doc["slots"]) == ["0935"]


def test_the_document_carries_no_trading_or_account_data():
    ss.mark_market(DAY, "open", now=NOW)
    doc = ss.record(DAY, "0935", "succeeded", now=NOW)
    assert set(doc) == {"schema", "trading_day", "market", "updated_at", "slots"}
    assert set(doc["slots"]["0935"]) == {"status", "stamped_at"}
    text = json.dumps(doc).lower()
    for word in ("ticker", "usd", "amount", "account", "order", "price", "ref_id", "symbol"):
        assert word not in text


@pytest.mark.parametrize("slot", ["0930", "1545", "", "../x"])
def test_record_refuses_an_unknown_slot(slot):
    with pytest.raises(ValueError):
        ss.record(DAY, slot, "succeeded", now=NOW)


@pytest.mark.parametrize("status", ["off", "errored", "done", "SUCCEEDED"])
def test_record_refuses_an_unknown_status(status):
    with pytest.raises(ValueError):
        ss.record(DAY, "0935", status, now=NOW)


def test_record_refuses_a_naive_clock():
    with pytest.raises(ValueError):
        ss.record(DAY, "0935", "succeeded", now=datetime(2026, 10, 29, 9, 41))


def test_a_corrupt_local_state_file_is_replaced_not_trusted(tmp_path):
    path = ss.state_path()
    path.parent.mkdir(parents=True)
    path.write_text("{not json", encoding="utf-8")
    doc = ss.record(DAY, "0935", "in_progress", now=NOW)
    assert doc["slots"] == {"0935": {"status": "in_progress", "stamped_at": NOW.isoformat(timespec="seconds")}}


# --- market day ------------------------------------------------------------------------------


CALENDAR = {"years_covered": [2026], "holidays_closed": ["2026-11-26"], "early_close_1300": ["2026-11-27"],
            "early_close_time_et": "13:00"}


@pytest.mark.parametrize("day, expected", [
    ("2026-10-29", "open"),       # Thursday
    ("2026-10-31", "closed"),     # Saturday
    ("2026-11-26", "closed"),     # Thanksgiving
    ("2026-11-27", "open"),       # early close: the planner's gates still judge each window
])
def test_market_for_day_uses_the_repo_calendar(day, expected):
    assert ss.market_for_day(day, CALENDAR) == expected


def test_market_for_day_fails_closed_outside_the_calendar_years():
    with pytest.raises(ValueError):
        ss.market_for_day("2031-01-02", CALENDAR)


# --- the reader's contract (what the short Perplexity prompt does by hand) --------------------


def _doc(**slots):
    return {"schema": ss.SCHEMA, "trading_day": DAY, "market": "open", "updated_at": NOW.isoformat(),
            "slots": {slot: {"status": status, "stamped_at": NOW.isoformat()} for slot, status in slots.items()}}


def test_a_succeeded_slot_today_means_stand_down():
    assert ss.is_done(_doc(**{"0935": "succeeded"}), trading_day=DAY, slot="0935") is True


def test_a_closed_market_today_means_stand_down_for_every_slot():
    doc = {**_doc(), "market": "closed"}
    assert all(ss.is_done(doc, trading_day=DAY, slot=slot) for slot in ss.SLOTS)


@pytest.mark.parametrize("status", ["in_progress", "failed"])
def test_in_progress_or_failed_falls_through_to_the_full_check(status):
    # in_progress 20 minutes after the slot may be a crashed run: the existing Mac probe judges it, not this file.
    assert ss.is_done(_doc(**{"0935": status}), trading_day=DAY, slot="0935") is False


@pytest.mark.parametrize("document", [
    None,
    "not a dict",
    {},
    {**_doc(**{"0935": "succeeded"}), "trading_day": "2026-10-28"},   # yesterday's file
    {**_doc(**{"0935": "succeeded"}), "schema": "slot_status/999"},    # unknown shape
    {**_doc(**{"0935": "succeeded"}), "slots": "0935"},
    {**_doc(**{"0935": "succeeded"}), "market": "maybe"},
    _doc(**{"1030": "succeeded"}),                                     # another slot finished, not this one
    {**_doc(), "slots": {"0935": {"status": "succeeded"}}},             # no stamp
    {**_doc(), "slots": {"0935": "succeeded"}},
])
def test_anything_unexpected_falls_through_to_the_full_check(document):
    assert ss.is_done(document, trading_day=DAY, slot="0935") is False


# --- publishing ------------------------------------------------------------------------------


class RecordingPublisher:
    channel = "github_branch"

    def __init__(self, ok=True, raises=False):
        self.ok, self.raises, self.documents = ok, raises, []

    def publish(self, document):
        if self.raises:
            raise RuntimeError("boom https://user:ghp_TOKEN@github.com")
        self.documents.append(json.loads(json.dumps(document)))
        return SimpleNamespace(channel=self.channel, ok=self.ok, detail="abc123" if self.ok else "git push failed")


def test_update_records_then_publishes_the_whole_day_document():
    publisher = RecordingPublisher()
    out = ss.update(DAY, "0935", "succeeded", now=NOW, publisher=publisher)
    assert out == {"recorded": True, "published": True, "detail": "abc123"}
    assert publisher.documents[0]["slots"]["0935"]["status"] == "succeeded"


def test_update_never_raises_when_the_publisher_fails_or_crashes():
    assert ss.update(DAY, "0935", "succeeded", now=NOW, publisher=RecordingPublisher(ok=False))["published"] is False
    out = ss.update(DAY, "0935", "succeeded", now=NOW, publisher=RecordingPublisher(raises=True))
    assert out["recorded"] is True and out["published"] is False
    assert "ghp_TOKEN" not in json.dumps(out) and "github.com" not in json.dumps(out)


def test_update_never_raises_when_the_local_state_cannot_be_written(monkeypatch, tmp_path):
    blocker = tmp_path / "blocker"
    blocker.write_text("a file, not a directory", encoding="utf-8")
    monkeypatch.setattr(ss, "state_dir", lambda: blocker / "slot_status")
    publisher = RecordingPublisher()
    out = ss.update(DAY, "0935", "succeeded", now=NOW, publisher=publisher)
    assert out["recorded"] is False and out["published"] is False
    assert publisher.documents == []  # never publish a document that was not recorded


def test_update_with_no_publisher_only_records():
    out = ss.update(DAY, "0935", "succeeded", now=NOW, publisher=None)
    assert out == {"recorded": True, "published": False, "detail": "publishing disabled"}


# --- settings --------------------------------------------------------------------------------


def test_settings_build_a_branch_publisher_for_slot_status_json(tmp_path):
    publisher = ss.publisher_from_settings({"enabled": True, "branch": "slot-status", "remote": "origin"},
                                           repo_dir=tmp_path)
    assert publisher.branch == "slot-status" and publisher.filename == "slot_status.json"


def test_disabled_settings_mean_no_publisher(tmp_path):
    assert ss.publisher_from_settings({"enabled": False, "branch": "slot-status", "remote": "origin"},
                                      repo_dir=tmp_path) is None


@pytest.mark.parametrize("branch", ["recommendations", "main", "claude/x"])
def test_settings_refuse_a_branch_that_would_clobber_or_trigger_ci(tmp_path, branch):
    # The branch publisher force-pushes a one-file tree: sharing the recommendations branch would erase
    # recommendations.json, and main / claude/** trigger CI.
    with pytest.raises(ValueError):
        ss.publisher_from_settings({"enabled": True, "branch": branch, "remote": "origin"}, repo_dir=tmp_path)


def test_the_checked_in_config_names_the_slot_status_branch():
    settings = ss.load_settings()
    assert settings["branch"] == "slot-status"
    assert isinstance(settings["enabled"], bool)


def test_raw_url_points_at_the_public_signal_repository():
    # 2026-10-31: badMade/all-stock-dca is PRIVATE, so a raw.githubusercontent.com read of it returns 404 to
    # Perplexity and every fire took the full path. The signal lives in a separate public data-only repository.
    assert ss.raw_url("slot-status", "badMade/all-stock-dca-signal") == (
        "https://raw.githubusercontent.com/badMade/all-stock-dca-signal/slot-status/slot_status.json")


@pytest.mark.parametrize("repository", ["", "badMade", "badMade/x/y", "bad Made/x", "../x/y"])
def test_raw_url_refuses_a_malformed_repository(repository):
    with pytest.raises(ValueError):
        ss.raw_url("slot-status", repository)


def test_the_checked_in_config_publishes_to_the_public_signal_repository():
    settings = ss.load_settings()
    assert settings["repository"] == "badMade/all-stock-dca-signal"
    assert settings["repository"] != "badMade/all-stock-dca"  # private: Perplexity cannot read it
    assert settings["remote"] == "signal"  # a git remote the Mac adds for the public repository


# --- CLI -------------------------------------------------------------------------------------


def test_cli_market_day_publishes_the_closed_flag(monkeypatch, capsys):
    publisher = RecordingPublisher()
    monkeypatch.setattr(ss, "_configured_publisher", lambda: publisher)
    monkeypatch.setattr(ss, "_calendar", lambda: CALENDAR)
    assert ss.main(["market-day", "--trading-day", "2026-11-26", "--now", "2026-11-26T08:00:00-05:00"]) == 0
    out = json.loads(capsys.readouterr().out)
    assert out["market"] == "closed" and out["published"] is True
    assert publisher.documents[0]["market"] == "closed"


def test_cli_show_prints_the_local_document(capsys):
    ss.record(DAY, "0935", "succeeded", now=NOW)
    assert ss.main(["show"]) == 0
    assert json.loads(capsys.readouterr().out)["slots"]["0935"]["status"] == "succeeded"


def test_the_suite_never_publishes_for_real(monkeypatch):
    # conftest.py sets the disable switch for every test, so local_heartbeat start/finish stay local.
    assert ss._configured_publisher() is None
    monkeypatch.delenv(ss.DISABLE_ENV)
    assert ss._configured_publisher() is not None  # the checked-in config enables publishing on the operator's Mac
