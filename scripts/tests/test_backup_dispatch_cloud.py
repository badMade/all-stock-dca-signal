"""Step 6, Mac-down cover: a GitHub Actions watcher (operator choice 2026-10-31).

If the Mac is down, nothing on it can email Perplexity, and Claude cannot run either. A scheduled GitHub workflow
reads the public slot-status document 20 minutes after each slot. It sends the same trigger email only when today's
document has NO entry for the slot (the Mac never started it) or there is no document for today at all (the Mac is
down). Any entry, ``failed`` included, means the Mac is alive and its own dispatcher (at +15 minutes) owns the decision;
``backup_requested`` means it already sent. That keeps the two senders from both emailing for one slot.
"""

from __future__ import annotations

from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest

from scripts import backup_dispatch as bd
from scripts import slot_status

ET = ZoneInfo("America/New_York")
DAY = "2026-10-29"
CALENDAR = {"years_covered": [2026], "holidays_closed": [], "early_close_1300": [], "early_close_time_et": "13:00"}
REPO = Path(__file__).resolve().parents[2]


def _now(hhmm: str, day: str = DAY) -> datetime:
    hour, minute = hhmm.split(":")
    return datetime.fromisoformat(day).replace(hour=int(hour), minute=int(minute), tzinfo=ET)


def _doc(day=DAY, market="open", **slots):
    return {"schema": slot_status.SCHEMA, "trading_day": day, "market": market, "updated_at": "x",
            "slots": {slot: {"status": status, "stamped_at": "x"} for slot, status in slots.items()}}


def _decide(document, now="09:56", day=DAY):
    return bd.cloud_decide(document, day=day, slot="0935", now=_now(now, day), calendar=CALENDAR)


def test_no_document_for_today_means_the_mac_is_down_so_send():
    assert _decide(None)["action"] == "send"
    assert _decide(_doc(day="2026-10-28", **{"0935": "succeeded"}))["action"] == "send"


def test_no_entry_for_the_slot_means_it_never_started_so_send():
    assert _decide(_doc(**{"1530": "succeeded"}))["action"] == "send"


@pytest.mark.parametrize("status", ["succeeded", "in_progress", "failed", "backup_requested"])
def test_any_entry_leaves_the_decision_to_the_mac(status):
    out = _decide(_doc(**{"0935": status}))
    assert out == {"action": "skip", "reason": f"mac_reported_{status}"}


def test_a_closed_market_sends_nothing():
    assert _decide(_doc(market="closed")) == {"action": "skip", "reason": "market_closed"}
    assert bd.cloud_decide(None, day="2026-10-31", slot="0935", now=_now("09:56", "2026-10-31"),
                           calendar=CALENDAR) == {"action": "skip", "reason": "market_closed"}


@pytest.mark.parametrize("now, reason", [("09:54", "too_early"), ("10:30", "too_late")])
def test_the_cloud_window_opens_20_minutes_after_the_slot(now, reason):
    assert _decide(None, now=now) == {"action": "skip", "reason": reason}


def test_a_malformed_document_counts_as_no_document():
    assert _decide({"schema": "other"})["action"] == "send"
    assert _decide("not a dict")["action"] == "send"


def test_the_workflow_runs_after_each_slot_and_reads_its_secrets_by_name():
    workflow = (REPO / ".github" / "workflows" / "backup-dispatch-watch.yml").read_text(encoding="utf-8")
    assert "schedule:" in workflow and "workflow_dispatch:" in workflow
    assert "python -m scripts.backup_dispatch cloud" in workflow
    for secret in ("DCA_PERPLEXITY_SENDER", "DCA_SMTP_HOST", "DCA_SMTP_USER", "DCA_SMTP_PASSWORD"):
        assert "secrets." + secret in workflow, secret
    assert "1-5" in workflow  # weekdays only


def test_the_workflow_can_push_the_backup_requested_mark_to_the_public_signal_repository():
    # GITHUB_TOKEN cannot push to another repository, so the mark goes through a fine-grained token secret scoped
    # to the signal repository only.
    workflow = (REPO / ".github" / "workflows" / "backup-dispatch-watch.yml").read_text(encoding="utf-8")
    assert "secrets.DCA_SIGNAL_PUSH_TOKEN" in workflow
    assert "git remote add signal" in workflow
    assert "badMade/all-stock-dca-signal" in workflow


def test_the_cloud_command_reads_the_signal_from_the_public_repository(monkeypatch, capsys):
    import json

    seen = []
    monkeypatch.setattr(bd, "_calendar", lambda: CALENDAR)
    monkeypatch.setattr(bd, "_fetch_document", lambda url: seen.append(url) or None)
    assert bd.main(["cloud", "--now", "2026-10-29T09:56:00-04:00", "--dry-run"]) == 0
    assert seen == ["https://raw.githubusercontent.com/badMade/all-stock-dca-signal/slot-status/slot_status.json"]
    assert json.loads(capsys.readouterr().out)["action"] == "would_send"


# -- the schedule: few runs, every slot covered (2026-10-31) ----------------------------------------------------------
# GitHub Actions had just stopped on a spending limit. Every 5 minutes in market hours was ~108 runs a day; the watcher
# only needs one run inside each slot's cloud window, in both EDT and EST, since GitHub cron is UTC.

import re as _re  # noqa: E402
from datetime import timezone as _utc_tz  # noqa: E402


def _crons():
    workflow = (REPO / ".github" / "workflows" / "backup-dispatch-watch.yml").read_text(encoding="utf-8")
    return _re.findall(r'cron:\s*"([^"]+)"', workflow)


def _expand(field, low, high):
    values = set()
    for part in field.split(","):
        if part == "*":
            values.update(range(low, high + 1))
        elif "-" in part:
            a, b = map(int, part.split("-"))
            values.update(range(a, b + 1))
        else:
            values.add(int(part))
    return values


def _fires_utc(day):
    """Every (hour, minute) UTC the schedule fires on ``day`` (weekday checks done by the caller)."""
    fires = set()
    for cron in _crons():
        minute, hour, dom, month, dow = cron.split()
        assert (dom, month, dow) == ("*", "*", "1-5"), cron  # weekdays, every month
        for h in _expand(hour, 0, 23):
            for m in _expand(minute, 0, 59):
                fires.add((h, m))
    return fires


def test_the_watcher_runs_at_most_nine_times_a_day():
    assert len(_fires_utc(None)) <= 9


@pytest.mark.parametrize("day", ["2026-10-29", "2026-12-03"], ids=["EDT", "EST"])
def test_every_slot_gets_exactly_one_run_inside_its_cloud_window_in_summer_and_winter(day):
    covered = []
    for hour, minute in sorted(_fires_utc(day)):
        when = datetime.fromisoformat(day).replace(hour=hour, minute=minute, tzinfo=_utc_tz.utc)
        slot = bd.due_slot(when, bd.CLOUD_CHECK_AFTER_MINUTES)
        if slot:
            covered.append(slot)
    assert sorted(covered) == ["0935", "1030", "1130", "1230", "1330", "1430", "1530"]


# -- a Mac that went silent mid-run (2026-11-05) -----------------------------------------------------------------------
# Any entry still leaves the decision to the Mac, with one exception: an in_progress slot whose activity_at is more
# than CLOUD_STALE_MINUTES old means the Mac stopped publishing mid-run (asleep or down), so nobody else will send.


def _running(minutes_ago: int, now="10:20"):
    when = _now(now).timestamp() - minutes_ago * 60
    stamp = datetime.fromtimestamp(when, tz=ET).isoformat()
    doc = _doc()
    doc["slots"]["0935"] = {"status": "in_progress", "stamped_at": "x", "activity_at": stamp}
    return bd.cloud_decide(doc, day=DAY, slot="0935", now=_now(now), calendar=CALENDAR)


def test_a_running_slot_with_recent_activity_is_left_to_the_mac():
    assert _running(5) == {"action": "skip", "reason": "mac_reported_in_progress"}


def test_a_running_slot_silent_past_the_cloud_limit_sends():
    assert _running(bd.CLOUD_STALE_MINUTES + 1) == {"action": "send", "reason": "mac_stalled"}


def test_the_cloud_stale_limit_is_twenty_minutes():
    assert bd.CLOUD_STALE_MINUTES == 20


# -- security hardening for outbound fetch and config parsing ---------------------------------------------------------


@pytest.mark.parametrize(
    "url",
    [
        "file:///etc/passwd",
        "http://raw.githubusercontent.com/badMade/all-stock-dca-signal/slot-status/slot_status.json",
        "ftp://raw.githubusercontent.com/badMade/all-stock-dca-signal/slot-status/slot_status.json",
        "https://127.0.0.1/slot_status.json",
        "https://169.254.169.254/latest/meta-data",
        "https://10.0.0.1/slot_status.json",
    ],
)
def test_fetch_document_blocks_unsafe_schemes_and_private_ssrf_targets(monkeypatch, url):
    called = []
    monkeypatch.setattr(bd.urllib.request, "urlopen", lambda *a, **kw: called.append(a))
    monkeypatch.setattr(bd.urllib.request, "build_opener", lambda *a, **kw: called.append(a))
    assert bd._fetch_document(url) is None
    assert called == []


def test_fetch_document_uses_hardened_opener_and_enforces_size_and_duplicate_key_guards(monkeypatch):
    import io
    import urllib.error

    from dca_net_utils import BlockedFileHandler, BlockedFTPHandler, PinnedHTTPHandler, PinnedHTTPSHandler

    seen_handlers = []

    class _FakeResponse:
        def __init__(self, body: bytes):
            self._buf = io.BytesIO(body)

        def read(self, n: int = -1) -> bytes:
            return self._buf.read(n)

        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False

    def _make_opener(body: bytes):
        def _build(*handlers):
            seen_handlers.clear()
            seen_handlers.extend(handlers)

            class _Opener:
                def open(self, req, timeout=None):
                    return _FakeResponse(body)

            return _Opener()

        return _build

    monkeypatch.setattr(bd.dca_net_utils, "assert_safe_destination", lambda url: None)
    url = "https://raw.githubusercontent.com/badMade/all-stock-dca-signal/slot-status/slot_status.json"

    # Valid payload succeeds and wires all SSRF-hardened handlers.
    monkeypatch.setattr(bd.urllib.request, "build_opener", _make_opener(b'{"schema":"slot_status/1"}'))
    assert bd._fetch_document(url) == {"schema": "slot_status/1"}
    assert {PinnedHTTPHandler, PinnedHTTPSHandler, BlockedFileHandler, BlockedFTPHandler}.issubset(set(seen_handlers))
    redirect_handlers = [
        h for h in seen_handlers if isinstance(h, type) and issubclass(h, bd.urllib.request.HTTPRedirectHandler)
    ]
    assert redirect_handlers and redirect_handlers[0]().redirect_request(None, None, 302, "Found", {}, "https://x") is None

    # Oversized payload fails closed.
    oversized = b'{"x":"' + (b"a" * (bd._MAX_STATUS_DOCUMENT_BYTES + 16)) + b'"}'
    monkeypatch.setattr(bd.urllib.request, "build_opener", _make_opener(oversized))
    assert bd._fetch_document(url) is None

    # Duplicate JSON keys fail closed.
    monkeypatch.setattr(bd.urllib.request, "build_opener", _make_opener(b'{"market":"closed","market":"open"}'))
    assert bd._fetch_document(url) is None

    # Redirect / HTTP error fails closed cleanly.
    def _raise_redirect(*handlers):
        class _Opener:
            def open(self, req, timeout=None):
                raise urllib.error.HTTPError(url, 302, "Found", {}, None)

        return _Opener()

    monkeypatch.setattr(bd.urllib.request, "build_opener", _raise_redirect)
    assert bd._fetch_document(url) is None


def test_load_settings_rejects_duplicate_json_keys(tmp_path):
    (tmp_path / "backup_dispatch.json").write_text(
        '{"mode": "shadow", "mode": "live", "check_after_minutes": 15}', encoding="utf-8"
    )
    with pytest.raises(ValueError, match="duplicate"):
        bd.load_settings(tmp_path)


def test_main_rejects_boolean_check_after_minutes(monkeypatch, capsys):
    import json

    monkeypatch.setattr(bd, "load_settings", lambda: {"mode": "shadow", "check_after_minutes": True})
    assert bd.main(["run", "--now", "2026-10-29T09:51:00-04:00"]) == 2
    assert json.loads(capsys.readouterr().out) == {"action": "error", "detail": "ValueError"}
