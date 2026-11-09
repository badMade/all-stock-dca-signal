"""Step 6 of the 2026-10-30 credit plan: Perplexity runs ONLY when a Claude slot fails (operator goal: zero backup
credits on a normal day).

``scripts/backup_dispatch.py`` runs on the Mac (launchd), uses no LLM and no Perplexity credits. About 15 minutes after
each of the seven slots it asks the local heartbeat whether the slot may need a backup. Only when it does (the slot is
off or errored, with no confirmed backup buy and no live claim) does it email Perplexity Computer
(computer@perplexity.com, from the operator's Perplexity address) one message: that slot's filled backup prompt with
``backup_planner.pyz`` attached. Every safety check stays inside the backup procedure itself, so a wrong trigger
costs credits but cannot add a buy.

The sending channel is injected: the operator's 2026-10-31 test email decides whether email is the channel. Nothing
here sends in a test, and nothing prints a secret.
"""

from __future__ import annotations

import json
from datetime import datetime
from email.message import EmailMessage
from zoneinfo import ZoneInfo

import pytest

from scripts import backup_dispatch as bd
from scripts import slot_status

ET = ZoneInfo("America/New_York")
DAY = "2026-10-29"  # a Thursday
CALENDAR = {"years_covered": [2026], "holidays_closed": ["2026-11-26"], "early_close_1300": ["2026-11-27"],
            "early_close_time_et": "13:00"}
PROMPT = "STANDING BACKUP EXECUTOR - slot 0935 (market-open)\nTask 4412c3c4. ...filled prompt text...\n"


def _at(hhmm: str, day: str = DAY) -> datetime:
    hour, minute = hhmm.split(":")
    return datetime.fromisoformat(day).replace(hour=int(hour), minute=int(minute), tzinfo=ET)


def _probe(local_status: str, backup_may_act: bool):
    calls = []

    def probe(day, slot):
        calls.append((day, slot))
        return {"local_status": local_status, "backup_may_act": backup_may_act}

    probe.calls = calls
    return probe


@pytest.fixture(autouse=True)
def _cron_root(tmp_path, monkeypatch):
    monkeypatch.setenv("DCA_CRON_TRACKING_ROOT", str(tmp_path))
    monkeypatch.setattr(slot_status, "state_dir", lambda: tmp_path / "slot_status")


# -- which task and window each slot uses ----------------------------------------------------------------------------


@pytest.mark.parametrize("slot, task_id", [
    ("0935", "4412c3c4"), ("1030", "e028361e"), ("1130", "e3590042"), ("1230", "062605c6"),
    ("1330", "54ab24d2"), ("1430", "54ab24d2"), ("1530", "f83b8d62"),
])
def test_every_slot_maps_to_its_backup_task(slot, task_id):
    assert bd.task_for_slot(slot).task_id == task_id


@pytest.mark.parametrize("slot, start, end", [
    ("0935", "09:50", "10:30"), ("1230", "12:45", "13:30"),
    # 2026-11-09: the final slot's window opens at 15:40 (10 minutes after the slot), not 15:45, because its backup
    # fires at 15:40 and only has until 16:00; every other slot keeps 15 minutes.
    ("1530", "15:40", "16:00"),
])
def test_the_check_window_opens_15_minutes_after_the_slot_except_the_final_slot_and_closes_at_its_latest_placement(
        slot, start, end):
    assert bd.check_window(slot) == (start, end)


def test_an_unknown_slot_is_refused():
    with pytest.raises(ValueError):
        bd.task_for_slot("0930")


# -- the decision ------------------------------------------------------------------------------------------------------


def _decide(local_status="off", may_act=True, now="09:51", day=DAY, sent=False):
    return bd.decide(day=day, slot="0935", now=_at(now, day), calendar=CALENDAR,
                     probe=_probe(local_status, may_act), already_sent=sent)


@pytest.mark.parametrize("local_status", ["off", "errored"])
def test_a_slot_that_failed_or_never_started_sends(local_status):
    assert _decide(local_status, True) == {"action": "send", "reason": local_status}


@pytest.mark.parametrize("local_status", ["succeeded", "in_progress"])
def test_a_slot_that_succeeded_or_is_running_sends_nothing(local_status):
    assert _decide(local_status, False) == {"action": "skip", "reason": local_status}


def test_an_unreadable_heartbeat_sends_nothing_but_asks_for_the_operator():
    out = _decide("unknown", False)
    assert out["action"] == "skip" and out["reason"] == "unknown" and out["notify"] is True


def test_a_confirmed_backup_or_live_claim_sends_nothing():
    # backup_may_act false on an otherwise failed slot: a backup already bought or holds the claim.
    assert _decide("errored", False) == {"action": "skip", "reason": "errored"}


@pytest.mark.parametrize("day", ["2026-10-31", "2026-11-26"])  # Saturday, Thanksgiving
def test_a_closed_market_sends_nothing_and_never_probes(day):
    probe = _probe("off", True)
    out = bd.decide(day=day, slot="0935", now=_at("09:51", day), calendar=CALENDAR, probe=probe, already_sent=False)
    assert out == {"action": "skip", "reason": "market_closed"}
    assert probe.calls == []


@pytest.mark.parametrize("now, reason", [("09:49", "too_early"), ("10:30", "too_late"), ("11:00", "too_late")])
def test_outside_the_window_sends_nothing(now, reason):
    assert _decide(now=now) == {"action": "skip", "reason": reason}


def test_a_slot_already_dispatched_today_is_never_sent_twice():
    assert _decide(sent=True) == {"action": "skip", "reason": "already_sent"}


def test_a_naive_clock_is_refused():
    with pytest.raises(ValueError):
        bd.decide(day=DAY, slot="0935", now=datetime(2026, 10, 29, 9, 51), calendar=CALENDAR,
                  probe=_probe("off", True), already_sent=False)


# -- the message -------------------------------------------------------------------------------------------------------


def test_the_message_carries_the_filled_prompt_and_the_planner():
    message = bd.build_message("0935", sender_address="me@example.com", prompt_text=PROMPT, planner_bytes=b"PK\x03\x04")
    assert isinstance(message, EmailMessage)
    assert message["To"] == "computer@perplexity.com"
    assert message["From"] == "me@example.com"
    assert "4412c3c4" in message["Subject"] and "0935" in message["Subject"]
    body = message.get_body(preferencelist=("plain",)).get_content()
    assert body.strip() == PROMPT.strip()
    attachments = list(message.iter_attachments())
    assert [part.get_filename() for part in attachments] == ["backup_planner.pyz"]
    assert attachments[0].get_content() == b"PK\x03\x04"


def test_an_unfilled_prompt_is_refused():
    with pytest.raises(ValueError):
        bd.build_message("0935", sender_address="me@example.com", prompt_text="value: ${PPLX_RUN_STATE_DIR}",
                         planner_bytes=b"PK")


@pytest.mark.parametrize("address", ["", "not-an-email", "a@b"])
def test_a_missing_or_malformed_sender_is_refused(address):
    with pytest.raises(ValueError):
        bd.build_message("0935", sender_address=address, prompt_text=PROMPT, planner_bytes=b"PK")


# -- dispatching ---------------------------------------------------------------------------------------------------------


class Sender:
    def __init__(self, fail=False):
        self.fail, self.sent = fail, []

    def __call__(self, message):
        if self.fail:
            raise OSError("smtp.example.com refused: password hunter2")
        self.sent.append(message)


def _dispatch(mode="live", local_status="off", may_act=True, sender=None, notify=None, now="09:51"):
    sender = sender or Sender()
    return bd.dispatch(day=DAY, slot="0935", now=_at(now), mode=mode, calendar=CALENDAR,
                       probe=_probe(local_status, may_act), send=sender,
                       message_for=lambda slot: bd.build_message(slot, sender_address="me@example.com",
                                                                 prompt_text=PROMPT, planner_bytes=b"PK"),
                       notify=notify or (lambda title, text: None)), sender


def test_live_mode_sends_once_marks_the_slot_and_publishes_backup_requested():
    out, sender = _dispatch()
    assert out["action"] == "sent" and len(sender.sent) == 1
    again, sender2 = _dispatch()
    assert again["action"] == "skip" and again["reason"] == "already_sent" and sender2.sent == []
    document = json.loads(slot_status.state_path().read_text(encoding="utf-8"))
    assert document["slots"]["0935"]["status"] == "backup_requested"


def test_shadow_mode_reports_what_it_would_send_and_sends_nothing():
    out, sender = _dispatch(mode="shadow")
    assert out["action"] == "would_send" and sender.sent == []
    live, _ = _dispatch()  # shadow left no marker, so the first live run still sends
    assert live["action"] == "sent"


def test_a_skip_sends_nothing():
    out, sender = _dispatch(local_status="succeeded", may_act=False)
    assert out["action"] == "skip" and sender.sent == []


def test_a_failed_send_leaves_no_marker_notifies_and_never_echoes_the_error():
    notes = []
    out, _ = _dispatch(sender=Sender(fail=True), notify=lambda title, text: notes.append((title, text)))
    assert out["action"] == "send_failed" and out["detail"] == "OSError"
    assert "hunter2" not in json.dumps(out) and "smtp.example.com" not in json.dumps(out)
    assert len(notes) == 1 and "hunter2" not in json.dumps(notes)
    retry, sender = _dispatch()
    assert retry["action"] == "sent" and len(sender.sent) == 1


def test_an_unknown_heartbeat_notifies_the_operator():
    notes = []
    out, _ = _dispatch(local_status="unknown", may_act=False, notify=lambda title, text: notes.append(title))
    assert out["action"] == "skip" and len(notes) == 1


@pytest.mark.parametrize("mode", ["", "LIVE", "on"])
def test_an_unknown_mode_is_refused(mode):
    with pytest.raises(ValueError):
        _dispatch(mode=mode)


# -- configuration, secrets and the CLI ----------------------------------------------------------------------------------


def test_the_checked_in_config_starts_in_shadow_mode():
    settings = bd.load_settings()
    assert settings["mode"] == "shadow"
    assert settings["to"] == "computer@perplexity.com"
    assert settings["check_after_minutes"] == 15


def test_the_env_file_loader_allows_the_dispatcher_settings():
    import dca_env

    for name in ("DCA_PERPLEXITY_SENDER", "DCA_SMTP_HOST", "DCA_SMTP_USER", "DCA_SMTP_PASSWORD",
                 "DCA_FILLED_PROMPTS_DIR"):
        assert name in dca_env.ALLOWED_KEYS, name


def test_slot_status_accepts_backup_requested_but_never_stands_down_on_it():
    assert "backup_requested" in slot_status.STATUSES
    document = {"schema": slot_status.SCHEMA, "trading_day": DAY, "market": "open", "updated_at": "x",
                "slots": {"0935": {"status": "backup_requested", "stamped_at": "x"}}}
    assert slot_status.is_done(document, trading_day=DAY, slot="0935") is False


def test_the_cli_prints_one_compact_line_and_never_a_secret(monkeypatch, capsys):
    monkeypatch.setenv("DCA_SMTP_PASSWORD", "hunter2")
    monkeypatch.setattr(bd, "_calendar", lambda: CALENDAR)
    monkeypatch.setattr(bd, "_probe", _probe("succeeded", False))
    assert bd.main(["run", "--now", "2026-10-29T09:51:00-04:00"]) == 0
    text = capsys.readouterr().out.strip()
    assert "\n" not in text and "hunter2" not in text
    out = json.loads(text)
    assert out["slot"] == "0935" and out["action"] == "skip"


def test_the_cli_reports_no_due_slot_between_windows(monkeypatch, capsys):
    monkeypatch.setattr(bd, "_calendar", lambda: CALENDAR)
    monkeypatch.setattr(bd, "_probe", _probe("off", True))
    assert bd.main(["run", "--now", "2026-10-29T09:40:00-04:00"]) == 0
    assert json.loads(capsys.readouterr().out) == {"action": "skip", "reason": "no_slot_due", "slot": None}


# -- the finalizer's signal ends the Mac's decision too (2026-11-03) ------------------------------------------------
# No live prompt calls local_heartbeat start/finish, so the heartbeat probe sees no lease and reports the slot as never
# started, every slot, every day. The Gate 9 finalizer now records the slot's status in the local slot-status document,
# and the Mac reads that first: a slot the primary finished today needs no backup, whatever the heartbeat says.


def test_a_slot_the_primary_finished_today_sends_nothing_and_never_probes():
    slot_status.record(DAY, "0935", "succeeded", now=_at("09:40"))
    probe = _probe("off", True)
    out = bd.decide(day=DAY, slot="0935", now=_at("09:51"), calendar=CALENDAR, probe=probe, already_sent=False)
    assert out == {"action": "skip", "reason": "primary_succeeded"}
    assert probe.calls == []


def test_a_slot_the_primary_reported_failed_still_sends():
    slot_status.record(DAY, "0935", "failed", now=_at("09:40"))
    out, sender = _dispatch()
    assert out["action"] == "sent" and len(sender.sent) == 1


def test_yesterdays_success_does_not_stand_today_down():
    slot_status.record("2026-10-28", "0935", "succeeded", now=_at("09:40", "2026-10-28"))
    out, sender = _dispatch()
    assert out["action"] == "sent" and len(sender.sent) == 1


def test_another_slots_success_does_not_stand_this_slot_down():
    slot_status.record(DAY, "1530", "succeeded", now=_at("09:40"))
    out, _ = _dispatch(mode="shadow")
    assert out["action"] == "would_send"


def test_an_unreadable_local_document_falls_back_to_the_heartbeat():
    slot_status.state_dir().mkdir(parents=True, exist_ok=True)
    slot_status.state_path().write_text("{not json", encoding="utf-8")
    out, _ = _dispatch(mode="shadow")
    assert out["action"] == "would_send"


# -- a running slot waits, a stalled one sends (2026-11-05) ------------------------------------------------------------
# The dispatcher checks 15 minutes after the slot; on 2026-11-05 the 9:35 run was still placing orders at 9:50 and
# recorded succeeded at 9:55:04. scripts/slot_status_watch.py now records in_progress with activity_at (the newest
# file in the run folder). Fresh activity means Claude is working: wait. No activity for RUNNING_FRESH_MINUTES means
# the run stalled: send, without the heartbeat probe (the live runs never feed it).


def _local_doc(**slots):
    slot_status.state_dir().mkdir(parents=True, exist_ok=True)
    slot_status.state_path().write_text(json.dumps({
        "schema": slot_status.SCHEMA, "trading_day": DAY, "market": "open", "updated_at": "x",
        "slots": slots}), encoding="utf-8")


def test_a_slot_still_writing_files_is_left_alone_and_never_probed():
    _local_doc(**{"0935": {"status": "in_progress", "stamped_at": "x", "activity_at": _at("09:48").isoformat()}})
    probe = _probe("off", True)
    out = bd.decide(day=DAY, slot="0935", now=_at("09:51"), calendar=CALENDAR, probe=probe, already_sent=False)
    assert out == {"action": "skip", "reason": "primary_running"}
    assert probe.calls == []


def test_a_slot_with_no_activity_for_the_fresh_window_counts_as_stalled_and_sends():
    stale = _at("09:51").replace(minute=51 - bd.RUNNING_FRESH_MINUTES - 1)
    _local_doc(**{"0935": {"status": "in_progress", "stamped_at": "x", "activity_at": stale.isoformat()}})
    probe = _probe("off", True)
    out = bd.decide(day=DAY, slot="0935", now=_at("09:51"), calendar=CALENDAR, probe=probe, already_sent=False)
    assert out == {"action": "send", "reason": "primary_stalled"}
    assert probe.calls == []


def test_the_fresh_window_is_ten_minutes():
    assert bd.RUNNING_FRESH_MINUTES == 10


def test_an_in_progress_entry_without_activity_falls_back_to_the_heartbeat_probe():
    _local_doc(**{"0935": {"status": "in_progress", "stamped_at": "x"}})  # the old local_heartbeat start shape
    probe = _probe("off", True)
    out = bd.decide(day=DAY, slot="0935", now=_at("09:51"), calendar=CALENDAR, probe=probe, already_sent=False)
    assert out["action"] == "send" and probe.calls == [(DAY, "0935")]


def test_a_malformed_activity_time_falls_back_to_the_heartbeat_probe():
    _local_doc(**{"0935": {"status": "in_progress", "stamped_at": "x", "activity_at": "not a time"}})
    probe = _probe("off", True)
    bd.decide(day=DAY, slot="0935", now=_at("09:51"), calendar=CALENDAR, probe=probe, already_sent=False)
    assert probe.calls == [(DAY, "0935")]
